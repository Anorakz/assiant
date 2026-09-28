#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_image_parameter.py — 分区表守卫（T15-2-4）

`image/check-parameter.py` 是纯算术校验器（连续性 / A-B 成对 / 容量 / 最小尺寸）。
这里把它钉进 CI：

  1. **我们的 A/B 分区表必须通过** —— 这是即将刷进板子的表，写错一个十六进制数字
     就是"分区重叠 / rootfs 被覆盖"，而且只有刷完才看得出来；
  2. **校验器必须有牙**：故意造三种坏表（有缝、缺 _b、段太小）都必须被判失败 ——
     否则第 1 条就只是"永远绿"。
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
CHECKER = _ROOT / "image" / "check-parameter.py"
OUR_TABLE = (_ROOT / "image" / "device" / "rockchip" / ".chips" / "rk3566_rk3568"
             / "parameter-assistant-ab.txt")


def run_checker(path, *extra):
    return subprocess.run([sys.executable, str(CHECKER), str(path), *extra],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          universal_newlines=True, timeout=60)


def table(body: str) -> Path:
    tmp = Path(tempfile.mkdtemp()) / "parameter.txt"
    tmp.write_text("FIRMWARE_VER: 1.0\nTYPE: GPT\nCMDLINE: mtdparts=:" + body + "\n",
                   encoding="utf-8")
    return tmp


class TestOurTable(unittest.TestCase):
    def test_our_ab_table_exists(self):
        self.assertTrue(OUR_TABLE.is_file(), "我们的 A/B 分区表不见了: %s" % OUR_TABLE)

    def test_our_ab_table_is_valid(self):
        proc = run_checker(OUR_TABLE)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("[OK]", proc.stdout)

    def test_our_ab_table_has_room_for_the_rootfs_and_models(self):
        """system_a 要装得下 rootfs（先按 1.5 GiB 留量），userdata 要装得下模型。"""
        proc = run_checker(OUR_TABLE, "--need", "system_a=%d" % (1536 * 2048))
        self.assertEqual(proc.returncode, 0, proc.stdout)


def our_body() -> str:
    """从**我们真实的**分区表里取 mtdparts body（不抄一遍，免得两处失同步）。"""
    for line in OUR_TABLE.read_text(encoding="utf-8").splitlines():
        if line.startswith("CMDLINE:") and "mtdparts=" in line:
            return line.split("mtdparts=:", 1)[1].strip()
    raise AssertionError("我们的分区表里没有 mtdparts: %s" % OUR_TABLE)


class TestCheckerHasTeeth(unittest.TestCase):
    """反空转：坏表必须被抓住。变异的是**真表的 body**。"""

    BASE = our_body()

    def test_a_valid_table_passes(self):
        proc = run_checker(table(self.BASE))
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_gap_between_partitions_fails(self):
        broken = self.BASE.replace("@0x00028000(boot_b)", "@0x00030000(boot_b)")
        proc = run_checker(table(broken))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("有缝或重叠", proc.stdout)

    def test_missing_ab_pair_fails(self):
        broken = self.BASE.replace("@0x00658000(system_b)", "@0x00658000(other_b)")
        proc = run_checker(table(broken))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("A/B 必须成对", proc.stdout)

    def test_too_small_partition_fails_the_need_check(self):
        proc = run_checker(table(self.BASE), "--need", "system_a=%d" % (8 * 1024 * 1024))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("需求", proc.stdout)

    def test_missing_grow_partition_fails(self):
        broken = self.BASE.replace("-@0x00c98000(userdata:grow)",
                                   "0x00100000@0x00c98000(userdata)")
        proc = run_checker(table(broken))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("可增长分区", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
