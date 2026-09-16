# ============================================================================
#  agent/vision/roi.py — ROI 字符串解析
#
#  ROI 是什么
#  ---------------------------------------------------------------------------
#  vision 模块把一帧 256×256 的图裁出一个感兴趣区域交给 SigLIP 编码, 裁剪范围
#  用一行字符串描述 (配置里写、IPC 里传)。本模块只负责把字符串变成数字。
#
#  格式
#  ---------------------------------------------------------------------------
#      "x,y,w,h"        例如 "100,100,200,200"
#
#  坐标空间与图像无关地统一按 **256×256** 算 (vision 的输出平面), 所以
#  越界能被检查出来并给出提示。为什么是 256: native 的 preprocess 输出的就是
#  256×256 RGB888 (见 native/image_rb.h 的 kFrameBytes)。
#
#  设计边界 (按约定不做的事)
#  ---------------------------------------------------------------------------
#  · **不做**图像裁剪 —— 裁剪由后续真实推理链路负责, 这里只解析
#  · **不做**图像预处理 —— native 层已经做了 (YUV→RGB888→256×256)
#  · **不做** schema 校验以外的宽容猜测: 格式不对就报错, 不猜用户想写什么
#
#  ⚠ 与 native 的对应关系: 鼠标坐标也是相对这个 256×256 平面的
#    (native/input_sender.h 的 kMouseRefWidth/Height), 所以 ROI 坐标可以直接
#    交给 InputSender.send_mouse, 不用换算。
# ============================================================================

from __future__ import annotations

from typing import Any, Dict, Union

__all__ = [
    "parse_roi",
    "is_valid_roi",
    "RoiError",
    "ROI_SPACE",
    "DEFAULT_ROI",
]

#: ROI 与鼠标坐标共同的参考平面边长 (native 的 preprocess 输出尺寸)
ROI_SPACE = 256

#: 缺省 ROI: 整个平面
DEFAULT_ROI: Dict[str, int] = {"x": 0, "y": 0, "w": ROI_SPACE, "h": ROI_SPACE}

#: 期望的字段个数
_FIELDS = ("x", "y", "w", "h")


class RoiError(ValueError):
    """ROI 字符串格式错误。

    继承 ValueError: "参数不对" 本质就是 ValueError, 调用方既能
    `except RoiError` 精确捕获, 也能 `except ValueError` 统一处理。

    @note 只用于**格式**错误 (字段数、非整数、空字段、w/h 非正、负坐标)。
          区域超界**不报错**, 而是夹到平面内并置 clamped=True —— 见 parse_roi。
    """


def parse_roi(roi_str: Union[str, None]) -> Dict[str, Any]:
    """把 "x,y,w,h" 解析成 dict。

    @param roi_str 形如 "100,100,200,200"; 允许逗号两侧有空格
    @return {"x", "y", "w", "h",     # 夹到 256×256 平面**之内**之后的值
             "x2", "y2",             # 右/下 **开区间**边界 (x2 = x + w), 可直接切片
             "area", "space",        # 面积; 坐标空间边长
             "clamped": bool,        # 是否因为超出平面被夹过
             "raw": (x, y, w, h),    # 用户原样写的值, 便于提示/日志
             "requested": {"x","y","w","h","x2","y2","area"}}  # 夹之前算出的区域
    @raise RoiError 格式不对 (字段数 / 空字段 / 非整数 / w,h <= 0 / x,y < 0)

    ⚠ 关于超界为什么不报错
    ---------------------------------------------------------------------------
    超界是**语义**问题, 不是格式问题, 而且实际很常见 (例如 "100,100,200,200"
    只是想表达"右下那一大块")。直接报错会让配置里一个手写的越界数字把整条
    视觉链路卡死; 静默截断又会让"以为覆盖了 200×200、其实只有 156×156"变成
    查不出来的怪问题。所以: **夹到平面内 + clamped=True + 保留 raw/requested**,
    调用方想告警就告警, 不告警也能直接拿去切片用。

    接受但会纠正的情况:
      · 前后空白、逗号两侧空白
      · 全角逗号 "，" (从中文输入法/文档里复制粘贴很常见)
      · x+w 或 y+h 超出 256 (夹到边界, clamped=True)

    拒绝的情况 (不猜):
      · 字段数不是 4
      · 空字符串 / 空字段 ("1,2,,4")
      · 非整数 ("1.5" / "abc" / "0x10")
      · w 或 h <= 0 (空区域没有意义, 几乎总是写错了)
      · x 或 y 为负 (平面外没有负坐标, 给不出合理的夹法)
      · 夹完之后区域为空 (例如 "250,0,10,10" 只剩 6 像素宽 —— 那仍有意义;
        "256,0,10,10" 则完全在平面外, 夹完 w=0, 报错)
    """
    if roi_str is None:
        raise RoiError("roi must be a string like 'x,y,w,h', got None")
    if not isinstance(roi_str, str):
        raise RoiError(
            "roi must be a string like 'x,y,w,h', got %s" % type(roi_str).__name__
        )

    # 全角逗号容错: 中文输入法下打出来的是 U+FF0C
    text = roi_str.strip().replace("，", ",")
    if not text:
        raise RoiError("roi must not be empty")

    parts = [p.strip() for p in text.split(",")]
    if len(parts) != len(_FIELDS):
        raise RoiError(
            "roi must have exactly %d comma-separated numbers "
            "(x,y,w,h), got %d in %r" % (len(_FIELDS), len(parts), roi_str)
        )

    values: Dict[str, int] = {}
    for name, raw in zip(_FIELDS, parts):
        if not raw:
            raise RoiError("roi field %r is empty in %r" % (name, roi_str))
        try:
            # int() 会接受 " 12 " 和 "+12", 但会拒绝 "1.5"/"abc" —— 正是想要的
            values[name] = int(raw)
        except (TypeError, ValueError):
            raise RoiError(
                "roi field %r must be an integer, got %r in %r" % (name, raw, roi_str)
            ) from None

    rx, ry, rw, rh = (values[n] for n in _FIELDS)

    if rw <= 0 or rh <= 0:
        raise RoiError(
            "roi width/height must be positive, got w=%d h=%d in %r" % (rw, rh, roi_str)
        )
    if rx < 0 or ry < 0:
        raise RoiError("roi x/y must not be negative, got x=%d y=%d" % (rx, ry))

    # 夹到平面内 (开区间语义: x2/y2 <= ROI_SPACE)
    x = min(rx, ROI_SPACE)
    y = min(ry, ROI_SPACE)
    x2 = min(rx + rw, ROI_SPACE)
    y2 = min(ry + rh, ROI_SPACE)
    w = x2 - x
    h = y2 - y

    if w <= 0 or h <= 0:
        raise RoiError(
            "roi is entirely outside the %dx%d plane: %r" % (ROI_SPACE, ROI_SPACE, roi_str)
        )

    clamped = (x, y, w, h) != (rx, ry, rw, rh)

    return {
        "x": x,
        "y": y,
        "w": w,
        "h": h,
        "x2": x2,
        "y2": y2,
        "area": w * h,
        "space": ROI_SPACE,
        "clamped": clamped,
        "raw": (rx, ry, rw, rh),
        "requested": {
            "x": rx,
            "y": ry,
            "w": rw,
            "h": rh,
            "x2": rx + rw,
            "y2": ry + rh,
            "area": rw * rh,
        },
    }


def is_valid_roi(roi_str: Any) -> bool:
    """parse_roi 的"只问不抛"版本。"""
    try:
        parse_roi(roi_str)
    except (RoiError, TypeError):
        return False
    return True
