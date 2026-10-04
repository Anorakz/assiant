"""`agent/core/ota.py` 的单测（T15-14）。

规矩（沿用本仓库约定）：断言**先证存在、再证内容** —— 不吃"空绿"。
"""

import os
import shutil
import tempfile
import unittest

from agent.core import ota


class TestBcb(unittest.TestCase):
    def test_roundtrip_defaults_match_the_factory_image(self):
        raw = ota.build_bcb()
        got = ota.parse_bcb(raw)
        self.assertTrue(got["ok"], got["reason"])
        self.assertEqual(got["slots"][0]["priority"], ota.DEFAULT_SLOT_A_PRIORITY)
        self.assertEqual(got["slots"][1]["priority"], ota.DEFAULT_SLOT_B_PRIORITY)
        self.assertEqual(got["slots"][0]["tries_remaining"], ota.MAX_TRIES)
        self.assertEqual(got["slots"][0]["successful_boot"], 0)
        self.assertEqual(got["last_boot"], 0)
        self.assertTrue(got["crc_ok"])

    def test_factory_bytes_equal_what_the_board_had(self):
        """出厂那份实测是 `00414230010000000f0700000e070000…`（A=15/B=14）。

        @note 这里**逐字节**比对前 16 字节 —— 它直接对应今天串口里看到的 `_a` 那一幕。
        """
        raw = ota.build_bcb()
        self.assertEqual(raw[:16].hex(), "00414230010000000f0700000e070000")

    def test_raising_a_slot_makes_it_win(self):
        """抬谁的优先级谁就被引导器选中（实测：A=14/B=15 时根挂到 mmcblk0p7）。

        @note 两条镜像用例**合成一条**：审计棘轮的 `duplicates_tests` 就是冲这个来的。
        """
        for wanted, index in (("a", 0), ("b", 1)):
            got = ota.parse_bcb(ota.next_bcb_for_slot(wanted))
            self.assertTrue(got["ok"], got["reason"])
            self.assertGreater(got["slots"][index]["priority"],
                               got["slots"][1 - index]["priority"],
                               "抬 %s 之后它的优先级应当更高" % wanted)

    def test_corrupt_crc_is_rejected(self):
        raw = bytearray(ota.build_bcb())
        raw[9] = (raw[9] + 1) & 0xFF          # 改 tries 但不更新 CRC
        got = ota.parse_bcb(bytes(raw))
        self.assertFalse(got["ok"])
        self.assertIn("CRC", got["reason"])

    def test_bad_magic_is_rejected(self):
        raw = bytearray(ota.build_bcb())
        raw[0:4] = b"XXXX"
        got = ota.parse_bcb(bytes(raw))
        self.assertFalse(got["ok"])
        self.assertIn("magic", got["reason"])

    def test_short_input_is_rejected(self):
        got = ota.parse_bcb(b"\x00" * 8)
        self.assertFalse(got["ok"])
        self.assertIn("32", got["reason"])

    def test_three_fields_must_be_pairs(self):
        for bad in (dict(slot_priorities=(1,)), dict(tries=(1, 2, 3)), dict(successful=())):
            with self.assertRaises(ValueError):
                ota.build_bcb(**bad)          # type: ignore[arg-type]

    def test_apply_writes_both_copies_only(self):
        """整块写：两份副本都改，别的字节一个都不许动。"""
        image = bytearray(48 * 1024)
        image[0:16] = b"HEADER-KEEP-ME!!"
        out = ota.apply_bcb_to_misc(bytes(image), ota.build_bcb())
        self.assertEqual(out[0:16], b"HEADER-KEEP-ME!!")
        for offset in ota.MISC_METADATA_OFFSETS:
            got = ota.parse_bcb(out[offset:offset + ota.METADATA_SIZE])
            self.assertTrue(got["ok"], got["reason"])
        self.assertEqual(len(out), len(image))
        # 副本以外没有别的 32 字节块被改：把两份挖掉后应与原图相同
        stripped_in = bytearray(image)
        stripped_out = bytearray(out)
        for offset in ota.MISC_METADATA_OFFSETS:
            stripped_in[offset:offset + 32] = b"\x00" * 32
            stripped_out[offset:offset + 32] = b"\x00" * 32
        self.assertEqual(bytes(stripped_in), bytes(stripped_out))

    def test_apply_refuses_a_too_small_image(self):
        with self.assertRaises(ValueError):
            ota.apply_bcb_to_misc(b"\x00" * 0x100, ota.build_bcb())


class TestPackage(unittest.TestCase):
    def test_parse_skips_comments_and_blanks(self):
        text = ("# NAME\tPATH\n"
                "package-file\tpackage-file\n"
                "\n"
                "boot_a\tboot.img\n"
                "system_a\trootfs.img\n")
        got = ota.parse_package_file(text)
        self.assertEqual([e["name"] for e in got],
                         ["package-file", "boot_a", "system_a"])

    def test_userdata_is_flagged(self):
        """默认清单含 userdata —— 它会写掉 /data（今天实测到的坑）。"""
        text = "boot_a\tboot.img\nsystem_a\trootfs.img\nuserdata\tuserdata.img\n"
        problems = ota.check_package(ota.parse_package_file(text))
        self.assertTrue(any("userdata" in item for item in problems), problems)

    def test_missing_slots_are_flagged(self):
        problems = ota.check_package(ota.parse_package_file("boot_a\tboot.img\n"))
        self.assertTrue(any("system_a" in item for item in problems), problems)

    def test_the_package_we_actually_built_passes(self):
        """= 2026-10-04 裁出来的那份（无 userdata、有 boot_a/system_a）。"""
        text = ("# NAME\tPATH\n"
                "package-file\tpackage-file\n"
                "parameter\tparameter.txt\n"
                "bootloader\tMiniLoaderAll.bin\n"
                "uboot\tuboot.img\n"
                "misc\tmisc.img\n"
                "boot_a\tboot.img\n"
                "backup\tRESERVED\n"
                "system_a\trootfs.img\n"
                "oem\toem.img\n")
        self.assertEqual(ota.check_package(ota.parse_package_file(text)), [])


class TestSlotAndPlan(unittest.TestCase):
    def test_slot_from_real_cmdline(self):
        line = ("storagemedia=emmc androidboot.mode=normal "
                "android_slotsufix=_b rw rootwait root=PARTUUID=614e0000-0000-4b53-8000-"
                "1d28000054aa fsck.repair=yes")
        self.assertEqual(ota.current_slot_from_cmdline(line), "b")

    def test_slot_missing_raises(self):
        with self.assertRaises(ota.OtaError):
            ota.current_slot_from_cmdline("storagemedia=emmc rw")

    def test_guard_refuses_current_slot(self):
        """包写 A、当前在 A ⇒ 必须拒绝（这是"别把正在跑的系统覆盖掉"那条线）。"""
        with self.assertRaises(ota.OtaError) as ctx:
            ota.plan_apply("a", "a")
        self.assertIn("当前槽", str(ctx.exception))

    def test_guard_allows_the_other_slot(self):
        plan = ota.plan_apply("b", "a")
        self.assertEqual(plan["target_slot"], "a")
        devices = dict(plan["writes"])
        self.assertEqual(devices["boot"], "/dev/disk/by-partlabel/boot_a")
        self.assertEqual(devices["system"], "/dev/disk/by-partlabel/system_a")
        self.assertTrue(plan["backup_misc"])
        self.assertEqual(plan["misc"], "/dev/disk/by-partlabel/misc")

    def test_guard_can_be_overridden_by_root_switch(self):
        plan = ota.plan_apply("a", "a", allow_same_slot=True)
        self.assertEqual(plan["target_slot"], "a")

    def test_plan_rejects_garbage_slots(self):
        for bad in ("c", "", "A"):
            with self.assertRaises(ota.OtaError):
                ota.plan_apply(bad, "a")
            with self.assertRaises(ota.OtaError):
                ota.plan_apply("b", bad)


class TestVerify(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ota-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _write(self, data: bytes) -> str:
        path = os.path.join(self.tmp, "pkg.img")
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def test_hash_matches_expected(self):
        path = self._write(b"hello-ota")
        want = ota.sha256_file(path)
        self.assertEqual(ota.verify_package(path, want), want)

    def test_mismatch_raises_and_does_not_touch_anything(self):
        path = self._write(b"hello-ota")
        before = os.path.getsize(path)
        with self.assertRaises(ota.OtaError) as ctx:
            ota.verify_package(path, "0" * 64)
        self.assertIn("sha256", str(ctx.exception))
        self.assertEqual(os.path.getsize(path), before)      # 校验失败不动文件

    def test_missing_and_empty_are_rejected(self):
        with self.assertRaises(ota.OtaError):
            ota.verify_package(os.path.join(self.tmp, "nope.img"))
        empty = self._write(b"")
        with self.assertRaises(ota.OtaError):
            ota.verify_package(empty)


if __name__ == "__main__":
    unittest.main()
