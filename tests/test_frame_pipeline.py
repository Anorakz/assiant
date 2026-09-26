#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`frame_pipeline.py` 的单测 —— 把"截图还原成板子看到的那一帧"钉死。

最要紧的一条: **点采样公式必须与 `native/preprocess.cpp` 逐位一致**。
它不是面积平均 —— 1280 宽时就是"每 5 列取 1 列", 写错了标定量出来的就是另一条管线
（而且不会报错, 只会静静地变准/变不准）。所以 `native_sample_index()` 是纯 Python,
开发机（没 numpy/cv2）上也跑得动, 公式本身永远有人盯着。

带 numpy/cv2 的用例在开发机上 skip、在板端真跑（与 `tests/test_siglip.py` 同一口径）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.vision import frame_pipeline as fp                          # noqa: E402

try:
    import numpy as np
except ImportError:                                   # pragma: no cover - 开发机走这里
    np = None

try:
    import cv2
except ImportError:                                   # pragma: no cover - 开发机走这里
    cv2 = None

needs_numpy = unittest.skipIf(np is None, "需要 numpy（开发机上没有，板端才有）")
needs_cv2 = unittest.skipIf(cv2 is None or np is None, "需要 cv2 + numpy（板端才有）")


def cpp_index(index, side, size=256):
    """`preprocess.cpp` 的写法, **照着 C++ 再抄一遍**当基准（不要调用被测函数）。"""
    rx = (index * side) // size
    if rx >= side:
        rx = side - 1
    return rx


class TestNativeIndex(unittest.TestCase):
    """纯 Python 那半 —— 开发机也跑。"""

    def test_256_is_identity(self):
        self.assertEqual(fp.native_sample_index(256), list(range(256)))

    def test_1280_is_every_5th_pixel(self):
        table = fp.native_sample_index(1280)
        self.assertEqual(len(table), 256)
        self.assertEqual(table[:6], [0, 5, 10, 15, 20, 25])
        self.assertEqual(table[-1], 1275)
        self.assertEqual(sorted(set(table)), list(range(0, 1280, 5)))

    def test_it_matches_the_cpp_formula_for_awkward_sizes(self):
        for side in (720, 800, 1080, 1599, 2559, 100, 37):
            table = fp.native_sample_index(side)
            self.assertEqual(table, [cpp_index(index, side) for index in range(256)],
                             "side=%d 与 C++ 的整数映射不一致" % side)

    def test_non_integer_ratios_are_uneven_on_purpose(self):
        """720/256 不是整数 -> 采样行**故意不均匀**（有的隔 2 行、有的隔 3 行）。"""
        table = fp.native_sample_index(720)
        steps = sorted({table[index + 1] - table[index] for index in range(255)})
        self.assertEqual(steps, [2, 3])
        self.assertEqual(table[0], 0)
        self.assertEqual(table[-1], 717)

    def test_it_never_leaves_the_image(self):
        for side in (1, 2, 255, 256, 257, 1000):
            table = fp.native_sample_index(side)
            self.assertEqual(len(table), 256)
            self.assertTrue(all(0 <= value < side for value in table),
                            "side=%d 采到界外了: %s" % (side, table))

    def test_a_degenerate_side_does_not_explode(self):
        self.assertEqual(fp.native_sample_index(0), [0] * 256)
        self.assertEqual(fp.native_sample_index(-5), [0] * 256)

    def test_upscaling_repeats_source_pixels(self):
        """比 256 小的时候是放大: 同一个源像素被取多次（C++ 就是这么夹的）。"""
        table = fp.native_sample_index(100)
        self.assertEqual(len(set(table)), 100)
        self.assertTrue(all(0 <= value <= 99 for value in table))

    def test_the_two_axes_sample_differently_at_1280x720(self):
        """1280×720 上横竖**不一样**: 列是整齐的每 5 列, 行是 2/3 交替 —— 别假设对称。"""
        rows = fp.native_sample_index(720)
        cols = fp.native_sample_index(1280)
        self.assertEqual(sorted({cols[i + 1] - cols[i] for i in range(255)}), [5])
        self.assertEqual(sorted({rows[i + 1] - rows[i] for i in range(255)}), [2, 3])
        self.assertEqual(len(set(rows)), 256)            # 行都取到了不同值（向下采样, 不是放大）


class TestNativeSample(unittest.TestCase):
    @needs_numpy
    def test_it_picks_the_pixels_the_table_says(self):
        grid = np.arange(16 * 16 * 3, dtype=np.uint8).reshape(16, 16, 3)
        out = fp.native_sample(grid, size=4)
        ys = fp.native_sample_index(16, 4)
        xs = fp.native_sample_index(16, 4)
        self.assertEqual(out.shape, (4, 4, 3))
        for row in range(4):
            for col in range(4):
                self.assertTrue(np.array_equal(out[row, col], grid[ys[row], xs[col]]))

    @needs_numpy
    def test_a_thin_line_can_be_missed_by_point_sampling(self):
        """**点采样的代价**: 宽度 1 的细线有 4/5 的概率被跳过（面积平均不会）。"""
        image = np.zeros((256, 256, 3), dtype=np.uint8)
        image[:, 7] = 255                                # 第 7 列是一条细线
        canvas = np.zeros((720, 1280, 3), dtype=np.uint8)
        canvas[:, 35] = 255                              # 1280 里的第 35 列 -> 采样点 7
        kept = fp.native_sample(canvas, 256)
        self.assertEqual(int(kept[:, 7].max()), 255)
        canvas[:, 36] = 255                              # 挪一列就取不到了
        canvas[:, 35] = 0
        missed = fp.native_sample(canvas, 256)
        self.assertEqual(int(missed.max()), 0)


class TestToModelFrame(unittest.TestCase):
    @needs_cv2
    def test_shape_dtype_and_channel_order(self):
        raw = np.zeros((1599, 2559, 3), dtype=np.uint8)
        raw[:, :, 2] = 200                               # BGR 里 B=200 -> RGB 的 R=200
        frame = fp.to_model_frame(raw)
        self.assertEqual(frame.shape, (1, 256, 256, 3))
        self.assertEqual(frame.dtype, np.uint8)
        self.assertEqual(int(frame[0, 10, 10, 0]), 200)
        self.assertEqual(int(frame[0, 10, 10, 2]), 0)

    @needs_cv2
    def test_native_and_area_differ(self):
        """两条对照管线结果**必须不同** —— 否则标定量的就是同一条（点采样白测了）。"""
        rng = np.random.RandomState(7)
        raw = rng.randint(0, 255, size=(1599, 2559, 3)).astype(np.uint8)
        native = fp.to_model_frame(raw, mode="native")
        area = fp.to_model_frame(raw, mode="area")
        self.assertFalse(np.array_equal(native, area))
        self.assertEqual(native.shape, area.shape)

    @needs_cv2
    def test_native_equals_manual_stream_then_sample(self):
        rng = np.random.RandomState(11)
        raw = rng.randint(0, 255, size=(1599, 2559, 3)).astype(np.uint8)
        canvas = fp.stream_canvas(raw, (1280, 720))
        manual = fp.native_sample(canvas, 256)
        frame = fp.to_model_frame(raw, mode="native")
        self.assertTrue(np.array_equal(frame[0], cv2.cvtColor(manual, cv2.COLOR_BGR2RGB)))

    @needs_cv2
    def test_a_canvas_that_is_already_the_stream_size_is_not_resized(self):
        canvas = np.zeros((720, 1280, 3), dtype=np.uint8)
        canvas[100, 100] = 255
        self.assertTrue(np.array_equal(fp.stream_canvas(canvas, (1280, 720)), canvas))

    @needs_cv2
    def test_a_bad_mode_is_refused(self):
        raw = np.zeros((720, 1280, 3), dtype=np.uint8)
        with self.assertRaises(fp.FramePipelineError):
            fp.to_model_frame(raw, mode="magic")

    @needs_cv2
    def test_from_screenshot_reads_a_real_file(self):
        import tempfile

        raw = np.zeros((1599, 2559, 3), dtype=np.uint8)
        raw[:, :, 0] = 123
        path = os.path.join(tempfile.mkdtemp(prefix="frame-pipe-"), "shot.png")
        self.assertTrue(cv2.imwrite(path, raw))
        frame = fp.from_screenshot(path)
        self.assertEqual(frame.shape, (1, 256, 256, 3))
        self.assertEqual(int(frame[0, 5, 5, 2]), 123)     # BGR 的 B -> RGB 的 B

    @needs_cv2
    def test_a_missing_file_says_so(self):
        with self.assertRaises(fp.FramePipelineError):
            fp.from_screenshot("/definitely/not/here.png")

    def test_the_defaults_match_the_board_config(self):
        self.assertEqual(fp.DEFAULT_STREAM, (1280, 720))
        self.assertEqual(fp.MODEL_SIZE, 256)


if __name__ == "__main__":
    unittest.main(verbosity=2)
