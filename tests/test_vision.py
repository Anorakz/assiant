#!/usr/bin/env python3
"""
tests/test_vision.py — parse_roi / SigLIPEncoder 单测

运行:
    python tests/test_vision.py

numpy 在宿主机上通常没装, 所以本测试**不假定有 numpy**:
  · 有 numpy   -> 校验 dtype / shape / 归一化 / 确定性
  · 没 numpy   -> 校验退化成 list[float] 的那条路径
两条路径都跑, 保证"接口不随环境变"。

覆盖:
  parse_roi   正常解析、空白与全角逗号容错、字段数/空字段/非整数、
              w/h <= 0、负坐标、越界、None/非字符串、is_valid_roi
  SigLIP      mock 语义 (ready=False / 确定性 / 内容敏感 / 维度可配)、
              形状校验、构造参数校验、退化路径、不加载模型文件
"""

import math
import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.vision import (  # noqa: E402
    DEFAULT_EMBEDDING_DIM,
    DEFAULT_ROI,
    EXPECTED_SHAPE,
    ROI_SPACE,
    RoiError,
    SigLIPEncoder,
    SigLIPError,
    is_valid_roi,
    parse_roi,
)

try:
    import numpy as np
except ImportError:
    np = None

HAS_NUMPY = np is not None


def make_image(fill=0, shape=(256, 256, 3)):
    """造一张测试图; 有 numpy 用 ndarray, 没有就用 None 之外的占位对象。

    没有 numpy 时 encode() 的 numpy 分支走不到, 但形状校验仍需要 .shape,
    所以给一个最小的形状替身。
    """
    if HAS_NUMPY:
        return np.full(shape, fill, dtype=np.uint8)

    class _FakeImage:
        def __init__(self, shape, fill):
            self.shape = shape
            self._fill = fill

        def tobytes(self):
            return bytes([self._fill & 0xFF]) * (shape[0] * shape[1] * shape[2])

    return _FakeImage(shape, fill)


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


# ===========================================================================
#  SigLIPEncoder
# ===========================================================================
class TestSigLIPConstruction(unittest.TestCase):
    def test_basic(self):
        enc = SigLIPEncoder("models/siglip.rknn")
        self.assertEqual(enc.model_path, "models/siglip.rknn")
        self.assertEqual(enc.dim, DEFAULT_EMBEDDING_DIM)
        self.assertEqual(enc.expected_shape, EXPECTED_SHAPE)

    def test_ready_is_false_because_mock(self):
        # ready=False 是有意的: 上层据此标注"视觉能力不可用"
        self.assertFalse(SigLIPEncoder("m.rknn").ready)

    def test_model_file_not_loaded_or_checked(self):
        # 允许"先写代码、模型后到位"
        enc = SigLIPEncoder("/nonexistent/path/model.rknn")
        self.assertFalse(enc.ready)

    def test_empty_model_path_rejected(self):
        for bad in ("", "   ", None, 123):
            with self.subTest(bad=bad):
                with self.assertRaises(SigLIPError):
                    SigLIPEncoder(bad)

    def test_bad_dim_rejected(self):
        for bad in (0, -1, 1.5, "768"):
            with self.subTest(bad=bad):
                with self.assertRaises(SigLIPError):
                    SigLIPEncoder("m.rknn", dim=bad)

    def test_bad_shape_rejected(self):
        for bad in ((256, 256), (256, 256, 3, 1), (0, 256, 3), (256, -1, 3)):
            with self.subTest(bad=bad):
                with self.assertRaises(SigLIPError):
                    SigLIPEncoder("m.rknn", expected_shape=bad)

    def test_custom_dim_and_shape(self):
        enc = SigLIPEncoder("m.rknn", dim=16, expected_shape=(8, 8, 3))
        self.assertEqual(enc.dim, 16)
        self.assertEqual(enc.expected_shape, (8, 8, 3))

    def test_repr(self):
        self.assertIn("ready=False", repr(SigLIPEncoder("m.rknn")))


class TestSigLIPEncode(unittest.TestCase):
    def setUp(self):
        self.enc = SigLIPEncoder("models/siglip.rknn", dim=16)

    def test_encode_returns_vector_of_right_length(self):
        out = self.enc.encode(make_image(7))
        self.assertEqual(len(out), 16)

    def test_encode_counts_calls(self):
        self.assertEqual(self.enc.encoded_count, 0)
        self.enc.encode(make_image(1))
        self.enc.encode(make_image(2))
        self.assertEqual(self.enc.encoded_count, 2)

    def test_deterministic_for_same_image(self):
        # mock 的核心约定: 同一张图 -> 同一向量
        a = self.enc.encode(make_image(5))
        b = self.enc.encode(make_image(5))
        self.assertEqual(list(a), list(b))

    def test_different_images_give_different_vectors(self):
        a = self.enc.encode(make_image(1))
        b = self.enc.encode(make_image(2))
        self.assertNotEqual(list(a), list(b))

    def test_deterministic_across_instances(self):
        other = SigLIPEncoder("models/siglip.rknn", dim=16)
        self.assertEqual(list(self.enc.encode(make_image(9))),
                         list(other.encode(make_image(9))))

    def test_accepts_custom_expected_shape(self):
        enc = SigLIPEncoder("m.rknn", dim=4, expected_shape=(8, 8, 3))
        self.assertEqual(len(enc.encode(make_image(0, shape=(8, 8, 3)))), 4)

    def test_all_values_are_finite(self):
        for value in self.enc.encode(make_image(3)):
            self.assertTrue(math.isfinite(float(value)))

    # ------------------------------------------------------- 形状校验 ---
    def test_wrong_shape_rejected(self):
        for bad in ((128, 128, 3), (256, 256), (256, 256, 4), (256, 256, 3, 1)):
            with self.subTest(bad=bad):
                with self.assertRaises(SigLIPError):
                    self.enc.encode(make_image(0, shape=bad))

    def test_shape_error_message_names_both_shapes(self):
        with self.assertRaises(SigLIPError) as ctx:
            self.enc.encode(make_image(0, shape=(64, 64, 3)))
        text = str(ctx.exception)
        self.assertIn("(256, 256, 3)", text)
        self.assertIn("(64, 64, 3)", text)

    def test_object_without_shape_rejected(self):
        for bad in (None, 42, "not an image", object(), [1, 2, 3]):
            with self.subTest(bad=bad):
                with self.assertRaises(SigLIPError):
                    self.enc.encode(bad)

    def test_failed_encode_does_not_count(self):
        with self.assertRaises(SigLIPError):
            self.enc.encode(make_image(0, shape=(8, 8, 3)))
        self.assertEqual(self.enc.encoded_count, 0, "失败的调用不该计入")

    # ------------------------------------------------- 有/无 numpy 两条 ---
    @unittest.skipUnless(HAS_NUMPY, "需要 numpy")
    def test_numpy_path_dtype_and_norm(self):
        out = self.enc.encode(make_image(4))
        self.assertIsInstance(out, np.ndarray)
        self.assertEqual(out.dtype, np.float32)
        self.assertEqual(out.shape, (16,))
        # 归一化到单位长度, 下游可以直接点积
        self.assertAlmostEqual(float(np.linalg.norm(out)), 1.0, places=5)

    @unittest.skipUnless(HAS_NUMPY, "需要 numpy")
    def test_numpy_path_accepts_other_dtypes(self):
        img = np.zeros((256, 256, 3), dtype=np.float32)
        self.assertEqual(len(self.enc.encode(img)), 16)

    @unittest.skipIf(HAS_NUMPY, "该用例只在没有 numpy 的环境跑")
    def test_fallback_path_returns_list(self):
        out = self.enc.encode(make_image(6))
        self.assertIsInstance(out, list)
        self.assertEqual(len(out), 16)
        for value in out:
            self.assertIsInstance(value, float)

    @unittest.skipIf(HAS_NUMPY, "该用例只在没有 numpy 的环境跑")
    def test_fallback_path_is_deterministic(self):
        self.assertEqual(list(self.enc.encode(make_image(8))),
                         list(self.enc.encode(make_image(8))))

    @unittest.skipIf(HAS_NUMPY, "该用例只在没有 numpy 的环境跑")
    def test_fallback_path_normalized(self):
        out = self.enc.encode(make_image(2))
        self.assertAlmostEqual(sum(v * v for v in out) ** 0.5, 1.0, places=5)

    def test_interface_is_same_with_or_without_numpy(self):
        # 不管哪条路径, 返回值都得支持 len() 和逐元素浮点比较
        out = self.enc.encode(make_image(11))
        self.assertEqual(len(out), 16)
        floats = [float(v) for v in out]
        self.assertEqual(len(floats), 16)


class TestSigLIPWithRealNumpyIfAvailable(unittest.TestCase):
    """把 mock 的行为和 numpy 的生成器对一下, 确认种子确实是内容决定的。"""

    @unittest.skipUnless(HAS_NUMPY, "需要 numpy")
    def test_seed_depends_on_every_pixel(self):
        enc = SigLIPEncoder("m.rknn", dim=8)
        base = np.zeros((256, 256, 3), dtype=np.uint8)
        modified = base.copy()
        modified[255, 255, 2] = 1  # 只改最后一个像素
        self.assertNotEqual(list(enc.encode(base)), list(enc.encode(modified)),
                            "内容变一个字节, 向量就该变")

    @unittest.skipUnless(HAS_NUMPY, "需要 numpy")
    def test_same_content_different_object_same_vector(self):
        enc = SigLIPEncoder("m.rknn", dim=8)
        a = np.full((256, 256, 3), 33, dtype=np.uint8)
        b = np.full((256, 256, 3), 33, dtype=np.uint8)
        self.assertIsNot(a, b)
        self.assertEqual(list(enc.encode(a)), list(enc.encode(b)))

    @unittest.skipUnless(HAS_NUMPY, "需要 numpy")
    def test_dim_768_default_works(self):
        enc = SigLIPEncoder("m.rknn")
        out = enc.encode(np.zeros((256, 256, 3), dtype=np.uint8))
        self.assertEqual(out.shape, (768,))


if __name__ == "__main__":
    unittest.main(verbosity=2)
