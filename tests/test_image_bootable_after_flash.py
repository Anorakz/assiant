#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_image_bootable_after_flash.py — 「刷完就能启动」的守卫（T15-2-12 修 2）

背景（板端实测两次，2026-10-02）：刷完镜像后黑屏、掉 fastboot，串口
`No bootable slots found.`；读 misc@0x800 是两个槽都不可引导且 tries=0 ——
只要这次启动失败一次，两个槽立刻全死，而"开机标记成功"的 ab-mark 要等系统
起来才能跑 → 死锁。根因是**刷机不写 misc**（gen_package_file 只收存在的镜像），
元数据一直是上一次的残留。

修法三件事，本文件把它们钉住：
  ① 两份板级 defconfig 都开 RK_MISC=y + RK_MISC_CUSTOM=y + RK_MISC_IMG=...；
  ② image/make-misc-img.py 生成的镜像，0x800 处必须是**合法且可引导**的 AVB 元数据；
  ③ install-into-sdk.sh 要把它生成到 chip 目录（SDK 的 mk-misc.sh 从那里取）。
"""
import importlib.util
import pathlib
import struct
import subprocess
import sys
import tempfile
import unittest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
IMG = _ROOT / "image"
BOARD = IMG / "device" / "rockchip" / ".chips" / "rk3566_rk3568"
REL_BOARD = BOARD / "rockchip_rk3568_kickpi_k1mini_release_defconfig"
DEV_BOARD = BOARD / "rockchip_rk3568_kickpi_k1mini_dev_defconfig"
MAKE_MISC = IMG / "make-misc-img.py"
INSTALL = IMG / "install-into-sdk.sh"

MISC_OFFSET = 0x800


def load_module(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestBoardDefconfigsAskForMisc(unittest.TestCase):
    def test_both_flavors_enable_the_misc_target(self):
        for p in (REL_BOARD, DEV_BOARD):
            text = p.read_text(encoding="utf-8")
            self.assertIn("RK_MISC=y", text, "%s 没开 misc 目标" % p.name)
            self.assertIn("RK_MISC_CUSTOM=y", text, "%s 没用自定义镜像" % p.name)
            self.assertIn('RK_MISC_IMG="misc-assistant-ab.img"', text, "%s 没指定镜像名" % p.name)

    def test_flavors_still_differ_only_in_base_cfg(self):
        def cfg(p):
            return [l.rstrip("\n") for l in p.read_text(encoding="utf-8").splitlines()
                    if l.strip() and not l.lstrip().startswith("#")]
        rel, dev = cfg(REL_BOARD), cfg(DEV_BOARD)
        diff = [(a, b) for a, b in zip(rel, dev) if a != b]
        self.assertEqual(len(diff), 1, "两份板级 defconfig 只该差一处：%r" % (diff,))


class TestGeneratedMiscImage(unittest.TestCase):
    """真跑一遍生成器，再按 AVB 格式解析 —— 不是"文件在就算过"。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = load_module(MAKE_MISC, "make_misc_img")
        cls.tmp = tempfile.NamedTemporaryFile(suffix=".img", delete=False)
        cls.tmp.close()
        r = subprocess.run([sys.executable, str(MAKE_MISC), cls.tmp.name],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True, timeout=120)
        cls.rc, cls.out = r.returncode, r.stdout
        cls.data = pathlib.Path(cls.tmp.name).read_bytes()

    def test_generator_succeeds_and_fits_old_tools(self):
        self.assertEqual(self.rc, 0, self.out)
        # SDK 的 08-misc.sh 注释：老 Windows 工具不接受 > 64K
        self.assertLessEqual(len(self.data), 64 * 1024, "misc 镜像必须 ≤ 64 KB")

    def test_magic_version_and_crc(self):
        md = self.data[MISC_OFFSET:MISC_OFFSET + 32]
        self.assertEqual(md[0:4], b"\x00AB0", "AVB magic 必须是 \\0AB0")
        self.assertEqual((md[4], md[5]), (1, 0), "版本应是 1.0")
        crc = int.from_bytes(md[28:32], "big")          # **大端**
        self.assertEqual(crc, self.mod.crc32_ieee(md[0:28]), "CRC 不符（必须覆盖前 28 字节）")

    def test_both_slots_are_bootable(self):
        """可引导判定：priority > 0 且（successful_boot 或 tries_remaining > 0）。"""
        md = self.data[MISC_OFFSET:MISC_OFFSET + 32]
        for idx, name in ((0, "槽A"), (1, "槽B")):
            priority, tries, successful = md[8 + idx * 4], md[9 + idx * 4], md[10 + idx * 4]
            self.assertGreater(priority, 0, "%s priority 必须 > 0（0 = 判死）" % name)
            self.assertTrue(successful == 1 or tries > 0,
                            "%s 既没成功标记、tries 又是 0 → 不可引导" % name)

    def test_last_boot_points_at_a_slot(self):
        md = self.data[MISC_OFFSET:MISC_OFFSET + 32]
        self.assertIn(md[16], (0, 1), "last_boot 应是槽号")


class TestInstallerGeneratesIt(unittest.TestCase):
    def test_install_script_generates_into_chip_dir(self):
        text = INSTALL.read_text(encoding="utf-8")
        self.assertIn("make-misc-img.py", text)
        self.assertIn("device/rockchip/.chips/rk3566_rk3568/misc-assistant-ab.img", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
