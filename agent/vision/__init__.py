# ============================================================================
#  agent/vision/__init__.py — 视觉层: ROI 解析 + SigLIP 图像编码
#
#      parse_roi("100,100,200,200")  -> {"x","y","w","h","x2","y2","area","space"}
#      SigLIPEncoder(model_path)     -> encode(256×256×3) -> (dim,) float32   (mock)
#      siglip.SiglipModel            -> 真 RKNN 双塔 (图像塔 + 文本塔), 见下
#
#  ⚠ 两条 SigLIP 路径别搞混（Phase 7 T7-1 的现状）
#  ---------------------------------------------------------------------------
#  · `siglip_encoder.SigLIPEncoder` —— **仍是 mock**（确定性伪随机向量, ready 恒 False）,
#    给"板端实时帧 → embedding"那条链路占位用, **目前没有调用方**。
#  · `siglip.SiglipModel` —— **真实现**（从板端实验树 sig/ 搬进仓库, 读 config.yaml 的
#    `vision:` 段）, 走 NPU。它是**离线打标签 / 图像检索**用的（agent/vision/siglip/）。
#    两者接口不同: 前者只 encode 图像, 后者是完整双塔（图像 + 文本 + 余弦）。
#
#  ⚠ 本包不强制依赖 numpy / rknnlite / tokenizers: 没有它们时 encode() 退化成
#    list[float]（mock 路径）, 或者真模型在**调用时**才报带安装提示的错
#    （真模型那条路是三样都延迟导入的, 所以宿主机上照样能 import 本包）。
# ============================================================================

from .roi import DEFAULT_ROI, ROI_SPACE, RoiError, is_valid_roi, parse_roi
from .siglip_encoder import (
    DEFAULT_EMBEDDING_DIM,
    EXPECTED_SHAPE,
    SigLIPEncoder,
    SigLIPError,
)
from .siglip import SiglipConfig, SiglipError, SiglipModel

__all__ = [
    # ROI
    "parse_roi",
    "is_valid_roi",
    "RoiError",
    "ROI_SPACE",
    "DEFAULT_ROI",
    # SigLIP —— mock 的实时帧编码器
    "SigLIPEncoder",
    "SigLIPError",
    "DEFAULT_EMBEDDING_DIM",
    "EXPECTED_SHAPE",
    # SigLIP —— 真 RKNN 双塔（离线打标签 / 检索）
    "SiglipModel",
    "SiglipConfig",
    "SiglipError",
]
