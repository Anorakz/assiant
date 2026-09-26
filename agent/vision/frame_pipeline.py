# ============================================================================
#  agent/vision/frame_pipeline.py — **"截图还原成板子看到的那一帧"**（Phase 13 T13-4）
#
#  为什么需要它: 板子拿到的不是 `screenshot.png`。真实链路是
#
#       PC 桌面 ──Sunshine 按 sunshine.width/height 编码──▶ 板端解码出 1280×720 的
#       YUV420P ──native/preprocess.cpp──▶ 256×256 RGB888 ──▶ SigLIP
#
#  而标定/打标签手里只有**整屏截图**（2559×1599）。要拿它代表"板子看到的画面"，
#  就得把最后两步**一模一样**地复现出来 —— 尤其是 native 那一步, 它不是面积平均:
#
#      src_x = roi.x + (dx * roi.w) / 256          // 整数除, 见 native/preprocess.cpp
#
#  1280 宽时 `(dx * 1280) / 256 = dx * 5` —— **每 5 列取 1 列、每 5 行取 1 行**,
#  不插值、不平均。细笔画（文字）会走样, 这是"整屏 256×256 判学习"最可能的精度瓶颈,
#  所以标定必须**照原样**复现, 而不是"顺手用 cv2 好好缩一下"（那样量出来的是另一条管线）。
#
#  谁用它:
#      · `tests/board/t13_study_calib.py`（T13-4 标定: 39 张截图 -> 起始锚点/阈值）
#      · `assistant study label --image`（T13-8: 没有活体串流时, 用一张截图给锚点库打标签）
#      · T13-6 的验收（真帧 vs 截图还原, 两边对照）
#
#  ⚠ 纯函数、**不 import 时就加载** cv2/numpy（开发机上两者都没有, 这样 `import` 不会炸;
#    用到的函数里才 import）。`native_sample_index()` 是**纯 Python**（不依赖 numpy）,
#    所以那条"点采样公式与 C++ 一致"的约束在开发机上也能被单测钉住。
#
#  ⚠ **只能近似** Sunshine 自己那步缩放（它的缩放核不公开、还跟编码器有关）。好在
#    "点采样 vs 面积平均"的差别远大于"缩放核是哪个", 标定时把它当噪声;
#    这个近似到底差多少, 由 T13-6 用**真串流帧**对照兜底。
# ============================================================================

from __future__ import annotations

from typing import Any, List, Sequence, Tuple

__all__ = [
    "MODEL_SIZE", "STREAM_WIDTH", "STREAM_HEIGHT",
    "DEFAULT_STREAM", "native_sample_index", "native_sample", "stream_canvas",
    "to_model_frame", "from_screenshot", "FramePipelineError",
]

#: 模型要的边长（与 native 的 `kRoiOutSize` 一致）。
MODEL_SIZE = 256

#: 流分辨率（板端 `config/config.yaml` 的 `sunshine.width/height`; 配置改了要传进来）。
STREAM_WIDTH = 1280
STREAM_HEIGHT = 720
DEFAULT_STREAM: Tuple[int, int] = (STREAM_WIDTH, STREAM_HEIGHT)


class FramePipelineError(RuntimeError):
    """管线跑不起来（缺 cv2/numpy、读不出图）—— 消息给人看。"""


def native_sample_index(side: int, size: int = MODEL_SIZE) -> List[int]:
    """复现 `preprocess.cpp` 的下标映射: `r = (d * side) / size`（整数除, 再夹到边上）。

    @param side 源边长度（ROI 的宽或高）
    @return 长度 `size` 的下标表
    @note 这是**点采样**: 不插值、不平均。`side < size` 时同一个源像素会被取多次（放大）。
    """
    count = max(1, int(size))
    total = int(side)
    if total <= 0:
        return [0] * count
    out = []
    for index in range(count):
        value = (index * total) // count
        if value >= total:
            value = total - 1
        out.append(value)
    return out


def _require(what: str) -> Tuple[Any, Any]:
    try:
        import cv2
        import numpy as np
    except ImportError as exc:                             # pragma: no cover - 开发机走这里
        raise FramePipelineError(
            "%s 需要 cv2 与 numpy（板端已装; 开发机上没有）: %s" % (what, exc)) from exc
    return cv2, np


def native_sample(image: Any, size: int = MODEL_SIZE) -> Any:
    """按 native 的点采样把一张图缩到 `size×size`（**不是** 面积平均）。"""
    _, np = _require("native_sample")
    height, width = image.shape[0], image.shape[1]
    ys = native_sample_index(height, size)
    xs = native_sample_index(width, size)
    return image[np.ix_(ys, xs)]


def stream_canvas(image: Any, stream: Sequence[int] = DEFAULT_STREAM) -> Any:
    """整屏截图 -> 流分辨率画布（Sunshine 那一步; 用 INTER_AREA 做降采样）。"""
    cv2, _ = _require("stream_canvas")
    width, height = int(stream[0]), int(stream[1])
    if image.shape[1] == width and image.shape[0] == height:
        return image
    return cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)


def to_model_frame(image: Any, *, stream: Sequence[int] = DEFAULT_STREAM,
                   size: int = MODEL_SIZE, mode: str = "native") -> Any:
    """一张 BGR 图 -> 模型要的 `(1, size, size, 3)` RGB uint8（原始 0-255）。

    @param mode "native" = 缩到流分辨率 + **点采样**（板子真实走的那条）;
                "area"   = 缩到流分辨率 + 面积平均到 `size`（对照组: 同分辨率, 换掉点采样）;
                "direct" = 整屏**一步**面积平均到 `size`（对照组: 理想重采样, 连流分辨率都跳过）
    @raise FramePipelineError mode 不认识 / 缺依赖
    """
    cv2, np = _require("to_model_frame")
    key = str(mode or "native").lower()
    if key == "direct":
        small = cv2.resize(image, (size, size), interpolation=cv2.INTER_AREA)
    else:
        canvas = stream_canvas(image, stream)
        if key == "native":
            small = native_sample(canvas, size)
        elif key == "area":
            small = cv2.resize(canvas, (size, size), interpolation=cv2.INTER_AREA)
        else:
            raise FramePipelineError("不认识的 mode: %r（只认 native / area / direct）"
                                     % (mode,))
    rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
    return rgb[None, ...].copy()


def from_screenshot(path: str, *, stream: Sequence[int] = DEFAULT_STREAM,
                    size: int = MODEL_SIZE, mode: str = "native") -> Any:
    """读一张截图文件 -> 模型帧（`assistant study label --image` 走这条）。"""
    cv2, _ = _require("from_screenshot")
    raw = cv2.imread(str(path))
    if raw is None:
        raise FramePipelineError("读不出图片: %s" % path)
    return to_model_frame(raw, stream=stream, size=size, mode=mode)
