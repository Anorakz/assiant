#!/usr/bin/env python3
"""
tests/test_vision.py — `parse_roi` 单测（ROI 解析）

运行:
    python tests/test_vision.py

覆盖:
  parse_roi   正常解析、空白与全角逗号容错、字段数/空字段/非整数、
              w/h <= 0、负坐标、越界、None/非字符串、is_valid_roi

⚠ T13-1 起这个文件**只管 ROI 解析**: 原来那半（`SigLIPEncoder` 的 mock 语义）
  已随空接口一起删掉 —— "板端实时帧 → 向量"只有一条真实现,
  见 `agent/vision/siglip/model.py::SiglipModel`（`tests/test_siglip.py` 盯着它）。
"""

import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.vision import (  # noqa: E402
    DEFAULT_ROI,
    ROI_SPACE,
    RoiError,
    is_valid_roi,
    parse_roi,
)


# ===========================================================================
#  parse_roi
# ===========================================================================
class TestParseRoi(unittest.TestCase):
    def test_basic(self):
        # 平面内的正常值: 原样返回, 不夹
        roi = parse_roi("10,20,30,40")
        self.assertEqual(roi["x"], 10)
        self.assertEqual(roi["y"], 20)
        self.assertEqual(roi["w"], 30)
        self.assertEqual(roi["h"], 40)
        self.assertFalse(roi["clamped"])

    def test_user_facing_example_still_parses(self):
        # 需求里给的例子 "100,100,200,200" 在 256 平面上其实是超界的
        # (100+200=300)。它必须能解析, 但区域要被夹到平面内并明确标出来。
        roi = parse_roi("100,100,200,200")
        self.assertEqual((roi["x"], roi["y"]), (100, 100))
        self.assertEqual((roi["w"], roi["h"]), (156, 156))
        self.assertEqual(roi["x2"], 256)
        self.assertTrue(roi["clamped"])
        self.assertEqual(roi["raw"], (100, 100, 200, 200))
        self.assertEqual(roi["requested"]["w"], 200)

    def test_derived_fields(self):
        roi = parse_roi("10,20,30,40")
        # x2/y2 是开区间边界, 方便直接切片
        self.assertEqual(roi["x2"], 40)
        self.assertEqual(roi["y2"], 60)
        self.assertEqual(roi["area"], 30 * 40)
        self.assertEqual(roi["space"], ROI_SPACE)

    def test_full_plane(self):
        roi = parse_roi("0,0,256,256")
        self.assertEqual(roi["x2"], 256)
        self.assertEqual(roi["y2"], 256)
        self.assertEqual(roi["area"], 256 * 256)
        self.assertFalse(roi["clamped"])

    def test_single_pixel(self):
        roi = parse_roi("255,255,1,1")
        self.assertEqual(roi["x2"], 256)
        self.assertEqual(roi["area"], 1)

    def test_whitespace_tolerated(self):
        roi = parse_roi("  10 , 20 , 30 , 40  ")
        self.assertEqual((roi["x"], roi["y"], roi["w"], roi["h"]), (10, 20, 30, 40))

    def test_fullwidth_comma_tolerated(self):
        # 中文输入法打出来的是 U+FF0C
        roi = parse_roi("10，20，30，40")
        self.assertEqual((roi["x"], roi["y"], roi["w"], roi["h"]), (10, 20, 30, 40))

    def test_zero_origin_allowed(self):
        self.assertEqual(parse_roi("0,0,10,10")["x"], 0)

    def test_returns_plain_ints(self):
        roi = parse_roi("1,2,3,4")
        for key in ("x", "y", "w", "h", "x2", "y2", "area", "space"):
            self.assertIsInstance(roi[key], int, key)
        self.assertIsInstance(roi["clamped"], bool)

    def test_key_set_is_stable(self):
        self.assertEqual(
            set(parse_roi("1,2,3,4")),
            {"x", "y", "w", "h", "x2", "y2", "area", "space",
             "clamped", "raw", "requested"},
        )

    # ------------------------------------------------------ 超界 -> 夹 ---
    def test_clamped_region_is_usable_for_slicing(self):
        roi = parse_roi("100,100,200,200")
        # 开区间 + 夹过之后一定落在平面内
        self.assertLessEqual(roi["x2"], ROI_SPACE)
        self.assertLessEqual(roi["y2"], ROI_SPACE)
        self.assertGreater(roi["w"], 0)
        self.assertGreater(roi["h"], 0)

    def test_clamped_only_in_one_axis(self):
        roi = parse_roi("200,10,100,20")
        self.assertEqual((roi["x"], roi["w"]), (200, 56))
        self.assertEqual((roi["y"], roi["h"]), (10, 20))
        self.assertTrue(roi["clamped"])

    def test_x_beyond_plane_is_rejected(self):
        # x 本身就在平面外 -> 夹完宽度为 0, 给不出可用区域
        for bad in ("256,0,10,10", "300,0,10,10", "0,256,10,10", "0,300,10,10"):
            with self.subTest(bad=bad):
                with self.assertRaises(RoiError) as ctx:
                    parse_roi(bad)
                self.assertIn("outside", str(ctx.exception))

    def test_exactly_at_boundary_is_ok(self):
        parse_roi("246,0,10,10")
        parse_roi("0,246,10,10")
        parse_roi("156,156,100,100")

    def test_not_clamped_when_within_plane(self):
        for good in ("0,0,256,256", "10,10,20,20", "246,246,10,10"):
            with self.subTest(good=good):
                self.assertFalse(parse_roi(good)["clamped"])

    # ------------------------------------------------------------ 错误 ---
    def test_wrong_field_count(self):
        for bad in ("1,2,3", "1,2,3,4,5", "1", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(RoiError):
                    parse_roi(bad)

    def test_empty_string(self):
        with self.assertRaises(RoiError):
            parse_roi("")

    def test_whitespace_only(self):
        with self.assertRaises(RoiError):
            parse_roi("   ")

    def test_empty_field(self):
        for bad in ("1,2,,4", ",2,3,4", "1,2,3,"):
            with self.subTest(bad=bad):
                with self.assertRaises(RoiError):
                    parse_roi(bad)

    def test_non_integer_fields(self):
        for bad in ("1.5,2,3,4", "a,2,3,4", "0x10,2,3,4", "1,2,3,4.0", "1,2,3,abc"):
            with self.subTest(bad=bad):
                with self.assertRaises(RoiError):
                    parse_roi(bad)

    def test_non_positive_wh(self):
        for bad in ("0,0,0,10", "0,0,10,0", "0,0,-5,10", "0,0,10,-5"):
            with self.subTest(bad=bad):
                with self.assertRaises(RoiError):
                    parse_roi(bad)

    def test_negative_xy(self):
        for bad in ("-1,0,10,10", "0,-1,10,10"):
            with self.subTest(bad=bad):
                with self.assertRaises(RoiError):
                    parse_roi(bad)

    def test_none_rejected(self):
        with self.assertRaises(RoiError):
            parse_roi(None)

    def test_non_string_rejected(self):
        for bad in (123, 1.5, [], {}, (1, 2, 3, 4)):
            with self.subTest(bad=bad):
                with self.assertRaises(RoiError):
                    parse_roi(bad)

    def test_error_is_a_valueerror(self):
        self.assertTrue(issubclass(RoiError, ValueError))
        with self.assertRaises(ValueError):
            parse_roi("nope")

    def test_error_message_is_actionable(self):
        with self.assertRaises(RoiError) as ctx:
            parse_roi("1,2,3")
        self.assertIn("exactly 4", str(ctx.exception))
        with self.assertRaises(RoiError) as ctx:
            parse_roi("0,0,-1,10")
        self.assertIn("positive", str(ctx.exception))

    def test_usable_as_slice(self):
        roi = parse_roi("10,20,30,40")
        self.assertEqual((roi["y"], roi["y2"], roi["x"], roi["x2"]), (20, 60, 10, 40))


class TestIsValidRoi(unittest.TestCase):
    def test_valid(self):
        self.assertTrue(is_valid_roi("1,2,3,4"))

    def test_invalid(self):
        for bad in ("", "1,2,3", None, 123, "0,0,0,0"):
            with self.subTest(bad=bad):
                self.assertFalse(is_valid_roi(bad))

    def test_never_raises(self):
        for bad in (object(), [], {}, 1.5, b"1,2,3,4"):
            with self.subTest(bad=bad):
                self.assertFalse(is_valid_roi(bad))


class TestRoiConstants(unittest.TestCase):
    def test_space_is_256(self):
        self.assertEqual(ROI_SPACE, 256)

    def test_default_roi_is_full_plane(self):
        self.assertEqual(DEFAULT_ROI, {"x": 0, "y": 0, "w": 256, "h": 256})


if __name__ == "__main__":
    unittest.main(verbosity=2)
