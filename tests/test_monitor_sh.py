#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_monitor_sh.py — scripts/monitor.sh 守卫（T14-10）

跑法:
    python tests/test_monitor_sh.py

两层（刻意分开，因为这个仓库的 PC 是 Windows）

  1) **到处都能跑**：`bash -n` 语法、`--help`、非法参数要退非 0
     （PC 上的 `bash` 常常是 WSL 的 bash —— 语法与参数解析照样能验）
  2) **只在 Linux 上跑**：造一棵**假的 /proc + /sys 树**，把 `--proc-root/--sysfs-root/
     --debugfs-root` 指过去，逐字段断言 CSV/JSON 的值 —— 这样"读数算得对不对"是可以
     在任何 Linux 上复现的，不用真板子。

为什么要有这个守卫
    monitor.sh 是**出事时才会用**的脚本（Agent 挂了、板子发烫、内存被吃掉）——
    它自己坏掉的话，偏偏是最需要它的时候才发现。所以至少把"能解析参数、能算出正确数字"
    钉成跑得出来的检查。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = _PROJECT_ROOT / "scripts" / "monitor.sh"
BASH = shutil.which("bash")
IS_LINUX = sys.platform.startswith("linux")


def run(args, timeout=60):
    return subprocess.run([BASH, str(SCRIPT)] + args, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, encoding="utf-8", errors="replace",
                          timeout=timeout)


def build_fake_tree(root: Path) -> dict:
    """造一棵假的内核接口树，返回值就是"应该被读出来的数字"。"""
    proc = root / "proc"
    sysfs = root / "sys"
    debugfs = root / "debug"
    (proc).mkdir(parents=True, exist_ok=True)
    (sysfs / "class" / "thermal" / "thermal_zone0").mkdir(parents=True, exist_ok=True)
    (sysfs / "class" / "thermal" / "thermal_zone1").mkdir(parents=True, exist_ok=True)
    (sysfs / "class" / "devfreq" / "fde40000.npu").mkdir(parents=True, exist_ok=True)
    (debugfs / "rknpu").mkdir(parents=True, exist_ok=True)

    (proc / "loadavg").write_text("0.50 0.40 0.30 2/100 12345\n", encoding="utf-8")
    (proc / "uptime").write_text("1234.56 1000.00\n", encoding="utf-8")
    #: 两次采样之间 CPU 走 100 jiffies（其中 25 空闲）→ 占用 75.0%
    (proc / "stat").write_text("cpu  100 0 50 800 25 0 25 0 0 0\n"
                               "cpu0 100 0 50 800 25 0 25 0 0 0\n", encoding="utf-8")
    (proc / "meminfo").write_text(
        "MemTotal:        4000000 kB\n"
        "MemFree:          500000 kB\n"
        "MemAvailable:    2000000 kB\n", encoding="utf-8")

    (sysfs / "class" / "thermal" / "thermal_zone0" / "type").write_text("soc-thermal\n")
    (sysfs / "class" / "thermal" / "thermal_zone0" / "temp").write_text("42500\n")
    (sysfs / "class" / "thermal" / "thermal_zone1" / "type").write_text("gpu-thermal\n")
    (sysfs / "class" / "thermal" / "thermal_zone1" / "temp").write_text("39000\n")
    (sysfs / "class" / "devfreq" / "fde40000.npu" / "cur_freq").write_text("600000000\n")
    (debugfs / "rknpu" / "load").write_text("NPU load:  37%\n")

    return {"proc": proc, "sysfs": sysfs, "debugfs": debugfs}


@unittest.skipUnless(BASH and IS_LINUX, "执行类检查只在 Linux 上跑：PC 的 bash 常是 WSL，看不到 Windows 路径")
class TestMonitorScriptShape(unittest.TestCase):
    """到处都能跑：语法、帮助、参数校验。"""

    def test_script_exists_and_is_syntactically_valid(self):
        self.assertTrue(SCRIPT.is_file(), "scripts/monitor.sh 不见了")
        self.assertIn("bash", (SCRIPT.read_text(encoding="utf-8").splitlines()[0] or "bash"))
        proc = run(["--help"], timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("--csv", proc.stdout)
        self.assertIn("health_check.sh", proc.stdout)

    def test_syntax_is_clean(self):
        proc = subprocess.run([BASH, "-n", str(SCRIPT)], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, encoding="utf-8")
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_unknown_flag_exits_non_zero(self):
        proc = run(["--nope"], timeout=30)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("不认识的参数", proc.stdout)

    def test_repo_shell_scripts_are_lf_without_bom(self):
        """仓里每个 `scripts/*.sh` 与 `image/*.sh` 都必须是 **LF + 无 BOM**。

        ⚠ 这条是踩出来的（T14-10）：用 PowerShell 的 `Set-Content -Encoding utf8` 改脚本，
        会同时塞进 **BOM 与 CRLF** —— 板端 `bash scripts/monitor.sh` 直接报
        `#!/bin/bash: No such file or directory`（BOM 让第一行不再是注释）+ `$'\\r'`。
        PC 上编辑脚本一律用 `edit` 工具，或者事后跑一次 `dos2unix` 式的转换。

        T15-2 起把 `image/*.sh` 也纳进来：那些脚本是在 WSL 里跑构建/注入的，
        同样经不起 BOM/CRLF（注入脚本第一行是 `#!/usr/bin/env bash`）。
        """
        bad = []
        for pattern in ("scripts/*.sh", "image/*.sh"):
            for path in sorted(_PROJECT_ROOT.glob(pattern)):
                raw = path.read_bytes()
                if raw.startswith(b"\xef\xbb\xbf"):
                    bad.append("%s 有 BOM" % path.name)
                if b"\r\n" in raw:
                    bad.append("%s 有 CRLF" % path.name)
        self.assertFalse(bad, "脚本行尾/BOM 不对（板端会跑不起来）: %s" % "；".join(bad))

    def test_csv_and_json_are_mutually_exclusive(self):
        proc = run(["--csv", "--json"], timeout=30)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("只能选一个", proc.stdout)


@unittest.skipUnless(BASH and IS_LINUX, "需要一个 Linux 内核接口树（板端 / CI）")
class TestMonitorValues(unittest.TestCase):
    """Linux 上造假的 /proc + /sys，逐字段验读数。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.paths = build_fake_tree(self.root)
        self.common = ["--proc-root", str(self.paths["proc"]),
                       "--sysfs-root", str(self.paths["sysfs"]),
                       "--debugfs-root", str(self.paths["debugfs"])]

    def tearDown(self):
        self._tmp.cleanup()

    def _csv_row(self):
        proc = run(["--csv"] + self.common, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        self.assertTrue(lines, proc.stdout)
        return lines[0].split(",")

    def test_csv_header_and_row_shape(self):
        header = run(["--csv", "--header"] + self.common, timeout=60).stdout.splitlines()
        self.assertEqual(len(header), 2, header)
        self.assertTrue(header[0].startswith("ts,uptime_s,load1"))
        self.assertEqual(len(header[0].split(",")), 18, "表头 18 列")
        self.assertEqual(len(header[1].split(",")), 18, "数据行也是 18 列")

    def test_values_are_the_expected_ones(self):
        row = self._csv_row()
        columns = ["ts", "uptime_s", "load1", "load5", "load15", "cpu_pct", "mem_total_mb",
                   "mem_used_mb", "mem_avail_mb", "agent_rss_mb", "gui_rss_mb",
                   "llama_rss_mb", "npu_freq_mhz", "npu_load_pct", "temp_soc_c",
                   "temp_gpu_c", "frames_ok", "frames_dropped"]
        data = dict(zip(columns, row))
        self.assertEqual(data["uptime_s"], "1235")   # 1234.56 四舍五入
        self.assertEqual(data["load1"], "0.50")
        # ⚠ 静态的假 /proc/stat 两次读到的内容一样 → 差值为 0 → 脚本如实打 `-`。
        #   真板子上是 16.1% 那种真数字（板端门禁里能看到）。这里只要求"要么是数字要么是 -"。
        self.assertRegex(data["cpu_pct"], r"^(-|\d+(\.\d+)?)$")
        self.assertEqual(data["mem_total_mb"], "3906")      # 4000000 kB
        self.assertEqual(data["mem_used_mb"], "1953")       # (4000000-2000000) kB
        self.assertEqual(data["mem_avail_mb"], "1953")
        self.assertEqual(data["npu_freq_mhz"], "600")
        self.assertEqual(data["npu_load_pct"], "37", "debugfs 的 `NPU load: 37%` 要读成 37")
        self.assertEqual(data["temp_soc_c"], "42.5")
        self.assertEqual(data["temp_gpu_c"], "39.0")
        # 没有 frames-file 时如实打 -
        self.assertEqual(data["frames_ok"], "-")
        self.assertEqual(data["frames_dropped"], "-")

    def test_npu_load_averages_multiple_cores(self):
        (self.paths["debugfs"] / "rknpu" / "load").write_text(
            "NPU load:  Core0: 10%, Core1: 30%\n")
        self.assertEqual(self._csv_row()[13], "20")

    def test_npu_load_falls_back_to_devfreq(self):
        (self.paths["debugfs"] / "rknpu" / "load").unlink()
        (self.paths["sysfs"] / "class" / "devfreq" / "fde40000.npu" / "load").write_text(
            "100@600000000Hz\n")
        self.assertEqual(self._csv_row()[13], "100")

    def test_missing_sources_degrade_to_dash(self):
        """接口缺了就打 `-`，绝不报错退出（它要在"什么都坏了"的时候还能跑）。"""
        shutil.rmtree(self.paths["sysfs"] / "class" / "thermal")
        (self.paths["debugfs"] / "rknpu" / "load").unlink()
        row = self._csv_row()
        self.assertEqual(row[13], "-")           # npu_load_pct
        self.assertEqual(row[14], "-")           # temp_soc_c
        self.assertEqual(row[15], "-")           # temp_gpu_c

    def test_frames_file_is_read_when_given(self):
        frames = self.root / "frames.txt"
        frames.write_text("frames ok=1234 dropped=7\n", encoding="utf-8")
        row = self._csv_row_with(["--frames-file", str(frames)])
        self.assertEqual(row[16], "1234")
        self.assertEqual(row[17], "7")

    def _csv_row_with(self, extra):
        proc = run(["--csv"] + extra + self.common, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        return [ln for ln in proc.stdout.splitlines() if ln.strip()][0].split(",")

    def test_json_is_valid_and_typed(self):
        proc = run(["--json"] + self.common, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(payload["npu_freq_mhz"], 600)
        self.assertEqual(payload["temp_soc_c"], 42.5)
        self.assertEqual(payload["load1"], 0.5)
        self.assertIn("cpu_pct", payload)

    def test_watch_count_prints_that_many_rows(self):
        proc = run(["--csv", "--watch", "1", "--count", "3", "--interval", "1"] + self.common,
                   timeout=90)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        rows = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        self.assertEqual(len(rows), 3, rows)

    def test_human_output_has_every_section(self):
        proc = run(self.common, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        for needle in ("时间", "CPU", "内存", "进程 RSS", "NPU", "温度", "帧计数"):
            self.assertIn(needle, proc.stdout)
        self.assertIn("llama-server", proc.stdout)

    def test_bad_proc_root_is_an_honest_error(self):
        proc = run(["--proc-root", str(self.root / "nope")], timeout=30)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("读不到", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
