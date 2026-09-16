# ============================================================================
#  agent/vision/__init__.py — 视觉层: ROI 解析 + SigLIP 图像编码
#
#      parse_roi("100,100,200,200")  -> {"x","y","w","h","x2","y2","area","space"}
#      SigLIPEncoder(model_path)     -> encode(256×256×3) -> (dim,) float32
#
#  ⚠ SigLIPEncoder 当前是 **mock**: 返回由图像内容决定的确定性伪随机向量,
#    不做真实推理。ready 恒为 False。真实现 (RKNN) 只需替换 encode()。
#
#  ⚠ 本包不强制依赖 numpy: 没有 numpy 时 encode() 退化成 list[float],
#    接口不变 (宿主机上就没装 numpy)。
# ============================================================================

from .roi import DEFAULT_ROI, ROI_SPACE, RoiError, is_valid_roi, parse_roi
from .siglip_encoder import (
    DEFAULT_EMBEDDING_DIM,
    EXPECTED_SHAPE,
    SigLIPEncoder,
    SigLIPError,
)

__all__ = [
    # ROI
    "parse_roi",
    "is_valid_roi",
    "RoiError",
    "ROI_SPACE",
    "DEFAULT_ROI",
    # SigLIP
    "SigLIPEncoder",
    "SigLIPError",
    "DEFAULT_EMBEDDING_DIM",
    "EXPECTED_SHAPE",
]
