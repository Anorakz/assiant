#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_crash_log.py — 崩溃日志守卫（T14-8）

跑法:
    python tests/test_crash_log.py

为什么用**子进程**跑
    崩溃日志的大部分价值都在"进程死了以后磁盘上留下什么"，而那要真的死一次：
    · 未捕获异常（主线程 / 子线程）
    · SIGSEGV（native 层段错误的等价物）
    所以这里用 `python -c` 起子进程、让它真的崩，再回来查文件 —— 在同一个进程里
    模拟不出"进程没了但文件还在"。

守什么
    1) 未捕获异常会写报告：里面有 traceback、pid、argv、以及**最近几条日志**；
    2) 段错误（SIGSEGV）会写报告：faulthandler 段里有 `Fatal Python error` 与线程栈；
    3) **正常退出不留文件**（`close_cleanly()` 删掉本次会话文件）——
       否则 logs/crash/ 会塞满"什么都没发生"的空报告，真报告反而找不到；
    4) 保留份数有限（超出按时间删旧的）；
    5) 启动横幅：能读到上一份报告的头尾、**同一份只报一次**（`.last-reported`）；
    6) `AGENT_CRASH_DISABLE` 真的什么都不做。

⚠ Windows 上跳过 SIGSEGV 那条（信号语义不同，faulthandler 拿不到同样的现场）。
"""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
#: 直接 `python tests/test_crash_log.py` 时 sys.path[0] 是 tests/，得自己把仓库根放进来
sys.path.insert(0, str(_PROJECT_ROOT))

from agent.core.crash_log import CrashLogger, read_report_head_tail   # noqa: E402

#: 子进程里跑的引导脚本：装好崩溃日志再按需崩。
#: `PROBE_CLEAN` 非空 = 照**生产那套**用 `atexit` 收尾（Agent 就是这么挂的）；
#: `PROBE_WITNESS` 指一个文件，由**先注册**的 atexit 处理写出来 —— atexit 是 LIFO，
#: 所以 witness 出现 ⟹ `close_cleanly` 已经跑过了（用它才能证明"收尾跑过但没删报告"）。
_BOOTSTRAP = r"""
import atexit, logging, os, sys
from pathlib import Path
sys.path.insert(0, %(repo)r)
from agent.core.crash_log import install_crash_logging

log = logging.getLogger("agent")
log.setLevel(logging.INFO)
log.addHandler(logging.StreamHandler(sys.stdout))
crash = install_crash_logging(app=%(app)r, log=log)
log.info("崩溃日志自检：装好了 -> %%s", crash.path)
print("SESSION_FILE=%%s" %% crash.path)

if os.environ.get("PROBE_CLEAN"):
    witness = os.environ.get("PROBE_WITNESS")
    if witness:
        atexit.register(lambda: Path(witness).write_text("atexit-ran", encoding="utf-8"))
    atexit.register(crash.close_cleanly)          # LIFO：它比 witness 先跑
    print("CLEANUP_REGISTERED")

%(body)s
print("BODY_DONE")
"""


def run_child(body: str, crash_dir: Path, app: str = "probe", extra_env=None):
    """起一个子进程：装好崩溃日志 → 执行 body。返回 CompletedProcess。

    ⚠ `encoding="utf-8"` 必须写死：Windows 的 locale 是 GBK，而子进程打的是 UTF-8 中文，
    用 `universal_newlines=True` 会在**解码**时抛 UnicodeDecodeError（PC 上必踩）。
    """
    script = _BOOTSTRAP % {"repo": str(_PROJECT_ROOT), "app": app, "body": body}
    env = dict(os.environ)
    env["AGENT_CRASH_DIR"] = str(crash_dir)
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("AGENT_CRASH_DISABLE", None)
    env.pop("PROBE_CLEAN", None)
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        encoding="utf-8", errors="replace", env=env, timeout=120,
    )


def run_plain(body: str, crash_dir: Path, extra_env=None):
    """起一个"裸"子进程：只设 AGENT_CRASH_DIR，body 自己决定装不装崩溃日志。

    给"启动横幅"那几条用 —— 真实顺序是**同一个实例**先 install 再问上一份是谁，
    直接用 run_child 会替我们把会话文件建好，反而把"上一份"变成它自己。
    """
    env = dict(os.environ)
    env["AGENT_CRASH_DIR"] = str(crash_dir)
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("AGENT_CRASH_DISABLE", None)
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-c", body],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        encoding="utf-8", errors="replace", env=env, timeout=120,
    )


class TestCrashLogger(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "crash"
        self.dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _reports(self):
        return sorted(self.dir.glob("*.log"))

    # ---------------------------------------------------------------- 1) 异常
    def test_uncaught_exception_writes_a_report(self):
        proc = run_child("raise RuntimeError('自检：主线程炸了')", self.dir)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        reports = self._reports()
        self.assertEqual(len(reports), 1, "应该正好留下一份报告: %r" % (reports,))
        text = reports[0].read_text(encoding="utf-8")
        self.assertIn("RuntimeError", text)
        self.assertIn("自检：主线程炸了", text)
        self.assertIn("未捕获异常（主线程）", text)
        self.assertIn("pid=", text)                    # 头部信息
        self.assertIn("命令行", text)
        # 最近日志缓冲也要进报告（崩之前那条 info）
        self.assertIn("崩溃日志自检：装好了", text)
        self.assertIn("最近", text)

    def test_uncaught_exception_in_a_thread_writes_a_report(self):
        body = (
            "import threading\n"
            "def boom():\n"
            "    raise ValueError('自检：线程里炸了')\n"
            "t = threading.Thread(target=boom, name='worker-7')\n"
            "t.start(); t.join()\n"
        )
        proc = run_child(body, self.dir)
        reports = self._reports()
        self.assertEqual(len(reports), 1, proc.stdout)
        text = reports[0].read_text(encoding="utf-8")
        self.assertIn("自检：线程里炸了", text)
        self.assertIn("线程 worker-7", text)

    # -------------------------------------------------------------- 2) 段错误
    @unittest.skipIf(sys.platform.startswith("win"), "Windows 上 SIGSEGV 语义不同")
    def test_segfault_writes_a_faulthandler_report(self):
        body = (
            "import ctypes\n"
            "ctypes.string_at(0)          # 真的段错误\n"
        )
        proc = run_child(body, self.dir)
        self.assertNotEqual(proc.returncode, 0)
        reports = self._reports()
        self.assertEqual(len(reports), 1, proc.stdout)
        text = reports[0].read_text(encoding="utf-8")
        self.assertIn("Fatal Python error", text)
        self.assertIn("Current thread", text)
        self.assertIn("ctypes", text)                  # 栈里有崩的那行

    # ------------------------------------------------------- 3) 正常退出不留文件
    def test_clean_exit_removes_the_session_file(self):
        witness = Path(self._tmp.name) / "witness.txt"
        proc = run_child("pass", self.dir,
                         extra_env={"PROBE_CLEAN": "1", "PROBE_WITNESS": str(witness)})
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("BODY_DONE", proc.stdout)
        self.assertTrue(witness.is_file(), "atexit 收尾没跑起来，这条用例就没意义了")
        self.assertEqual(self._reports(), [],
                         "正常退出不该留报告（否则真报告会被淹）: %r" % (self._reports(),))

    def test_reported_crash_survives_a_cleanup_call(self):
        """关键：写完报告的进程即使走到 close_cleanly（生产用 atexit 那条路）也不能把报告删了。

        未捕获异常退出时 atexit **照样会跑**，无脑删文件就会把刚写好的现场删掉。
        witness 证明「收尾确实跑过」；报告还在 + 里面有 traceback 证明「没被删」。
        """
        witness = Path(self._tmp.name) / "witness-raise.txt"
        proc = run_child("raise RuntimeError('别删我')", self.dir,
                         extra_env={"PROBE_CLEAN": "1", "PROBE_WITNESS": str(witness)})
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertTrue(witness.is_file(), "atexit 收尾没跑起来，这条用例就没意义了")
        reports = self._reports()
        self.assertEqual(len(reports), 1, "报告被 close_cleanly 删掉了！: %r" % (proc.stdout,))
        text = reports[0].read_text(encoding="utf-8")
        self.assertIn("别删我", text)
        self.assertIn("未捕获异常（主线程）", text)

    def test_no_cleanup_leaves_the_file_behind(self):
        """反过来钉住：没走 close_cleanly（崩了 / 被 kill -9）就该有文件。"""
        proc = run_child("pass", self.dir)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("BODY_DONE", proc.stdout)
        self.assertEqual(len(self._reports()), 1, proc.stdout)

    # ------------------------------------------------------------- 4) 保留份数
    def test_retention_keeps_only_the_newest(self):
        """保留策略数的是**历史报告**，本次会话文件不算（它正常退出会被删掉）。

        所以这里用一次**干净退出**的会话去触发 prune：6 份旧报告 + KEEP=3 → 剩最新的 3 份。
        （如果哪天把"本次会话"也算进去，就会白扔一份 —— 板端验收实测过这个 off-by-one。）
        """
        for i in range(6):
            (self.dir / ("old-%02d.log" % i)).write_text("x", encoding="utf-8")
            os.utime(self.dir / ("old-%02d.log" % i), (time.time() - (60 - i),) * 2)
        proc = run_child("pass", self.dir,
                         extra_env={"AGENT_CRASH_KEEP": "3", "PROBE_CLEAN": "1"})
        self.assertEqual(proc.returncode, 0, proc.stdout)
        reports = self._reports()
        self.assertEqual(len(reports), 3, "%r" % ([p.name for p in reports],))
        self.assertFalse((self.dir / "old-00.log").exists(), "最老的应该被删掉")
        self.assertFalse((self.dir / "old-02.log").exists(), "第 4 老的（old-02）也该被删")
        self.assertTrue((self.dir / "old-03.log").exists(), "第 3 老的要留着")
        self.assertTrue((self.dir / "old-05.log").exists(), "最新的那份旧报告要留着")

    def test_retention_counts_the_live_session_separately(self):
        """本次会话**崩了**时会变成一份报告：那时最多 keep+1 份（下次启动裁回来）。"""
        for i in range(6):
            (self.dir / ("old-%02d.log" % i)).write_text("x", encoding="utf-8")
            os.utime(self.dir / ("old-%02d.log" % i), (time.time() - (60 - i),) * 2)
        proc = run_child("raise RuntimeError('这次崩了')", self.dir,
                         extra_env={"AGENT_CRASH_KEEP": "3"})
        self.assertNotEqual(proc.returncode, 0)
        reports = self._reports()
        self.assertEqual(len(reports), 4, "%r" % ([p.name for p in reports],))
        self.assertFalse((self.dir / "old-00.log").exists())
        self.assertTrue(any("这次崩了" in p.read_text(encoding="utf-8") for p in reports))

    # ------------------------------------------------------------- 5) 启动横幅
    def test_banner_reads_previous_report_and_reports_once(self):
        first = run_child("raise RuntimeError('第一份')", self.dir)
        self.assertNotEqual(first.returncode, 0)
        # 第二次启动：横幅里应该出现上一份的内容。
        # ⚠ 这里不能用 run_child（它会替我们把会话文件建好，于是"上一份"就成了它自己），
        #   要模拟真实顺序：**同一个实例**先 install（建自己的会话文件）再问上一份是谁。
        second = run_plain(
            "import logging, sys\n"
            "sys.path.insert(0, %r)\n"
            "from agent.core.crash_log import install_crash_logging\n"
            "log = logging.getLogger('agent2'); log.addHandler(logging.StreamHandler(sys.stdout))\n"
            "logger = install_crash_logging(app='probe2', log=log)\n"
            "print('BANNER_BEGIN'); print(logger.previous_report_banner()); print('BANNER_END')\n"
            "logger.close_cleanly()\n" % str(_PROJECT_ROOT),
            self.dir,
        )
        out = second.stdout
        banner = out.split("BANNER_BEGIN", 1)[1].split("BANNER_END", 1)[0]
        self.assertIn("上次没有干净退出", banner)
        self.assertIn("第一份", banner, "横幅要带上次的现场（含 traceback）")
        self.assertIn("第一份", out.split("BANNER_END", 1)[0] + banner)

        # 第三次：同一份不该再报（否则每次重启都刷同一个老崩溃）
        third = run_plain(
            "import logging, sys\n"
            "sys.path.insert(0, %r)\n"
            "from agent.core.crash_log import install_crash_logging\n"
            "log = logging.getLogger('agent3'); log.addHandler(logging.StreamHandler(sys.stdout))\n"
            "logger = install_crash_logging(app='probe3', log=log)\n"
            "print('BANNER_BEGIN'); print(logger.previous_report_banner()); print('BANNER_END')\n"
            "logger.close_cleanly()\n" % str(_PROJECT_ROOT),
            self.dir,
        )
        third_banner = third.stdout.split("BANNER_BEGIN", 1)[1].split("BANNER_END", 1)[0]
        self.assertNotIn("上次没有干净退出", third_banner,
                         "同一份报告只该报一次（.last-reported）")
        # 但文件还在（只是不再刷横幅）
        self.assertTrue((self.dir / ".last-reported").is_file())

    def test_banner_is_bounded(self):
        """横幅不能把整份报告倒进日志（长报告只取头尾）。"""
        from agent.core.crash_log import read_report_head_tail
        long_report = self.dir / "agent-20260101-000000-1.log"
        long_report.write_text(
            "\n".join("LINE-%03d" % i for i in range(300)), encoding="utf-8")
        text = read_report_head_tail(long_report, head=5, tail=5)
        self.assertLessEqual(len(text.splitlines()), 11, "头 5 + 省略行 + 尾 5")
        self.assertIn("省略", text)
        self.assertIn("LINE-000", text)
        self.assertIn("LINE-299", text)
        self.assertNotIn("LINE-100", text, "中间的行不该出现")

    # ---------------------------------------------------------------- 6) 关闭
    def test_disable_env_makes_it_a_noop(self):
        proc = run_child("raise RuntimeError('不该被记')", self.dir,
                         extra_env={"AGENT_CRASH_DISABLE": "1"})
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self._reports(), [], "AGENT_CRASH_DISABLE=1 时不该写任何文件")

    def test_write_report_path_is_under_the_crash_dir(self):
        proc = run_child("raise RuntimeError('x')", self.dir, app="agent")
        report = self._reports()[0]
        self.assertEqual(report.parent, self.dir)
        self.assertTrue(report.name.startswith("agent-"), report.name)
        self.assertIn(str(report), proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
