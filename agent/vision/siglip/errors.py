# ============================================================================
#  agent/vision/siglip/errors.py — SigLIP 模块异常层次
#
#  为什么要单独一层：调用方需要区分"配置错了"（改环境）、"输入错了"（调用方 bug）、
#  "运行时错了"（模型/驱动问题）——三者的处置动作完全不同。全部继承 SiglipError，
#  这样只想兜底的人 except SiglipError 也能一次接住。
#
#  来历：本包从板端实验树 `sig/siglip/` 搬进仓库（已实测验证）。行为不变，
#  只有配置来源变了 —— 见 config.py 的文件头。
# ============================================================================

from __future__ import annotations

__all__ = [
    "SiglipError",
    "SiglipConfigError",
    "SiglipTokenizeError",
    "SiglipInputError",
    "SiglipRuntimeError",
]


class SiglipError(RuntimeError):
    """SigLIP 模块的基类异常。"""


class SiglipConfigError(SiglipError):
    """配置缺字段 / 值非法 / 模型或 tokenizer 文件不存在。"""


class SiglipTokenizeError(SiglipError, ValueError):
    """文本无法编码（空串、类型不对、tokenizer 文件损坏）。"""


class SiglipInputError(SiglipError, ValueError):
    """图像/文本输入不符合模型契约（形状、dtype、取值）。"""


class SiglipRuntimeError(SiglipError):
    """RKNN 加载/初始化/推理失败。"""
