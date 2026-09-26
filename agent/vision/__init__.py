# ============================================================================
#  agent/vision/__init__.py — 视觉层: ROI 解析 + SigLIP 图像/文本编码
#
#      parse_roi("100,100,200,200")  -> {"x","y","w","h","x2","y2","area","space"}
#      siglip.SiglipModel            -> 真 RKNN 双塔 (图像塔 + 文本塔), 见下
#
#  ⚠ 只有**一条** SigLIP 路径（T13-1 收口）
#  ---------------------------------------------------------------------------
#  · `siglip.SiglipModel` —— **真实现**（从板端实验树 sig/ 搬进仓库, 读 config.yaml 的
#    `vision:` 段）, 走 NPU。图像塔 → 向量（打标签 / 检索 / 认游戏 / 学习监督）,
#    文本塔 → 零样本分类。**这是唯一的编码入口**。
#  · 曾经还有一个 `siglip_encoder.SigLIPEncoder`（确定性伪随机向量的 mock, `ready` 恒 False,
#    注释里写着"给实时帧→embedding 那条链路占位, 目前没有调用方"）—— **T13-1 已删**:
#    真帧编码由 `SiglipModel.encode_image()` 承担, 留一个永远不 ready 的第二套接口
#    只会让人分不清"走哪条"。（`tests/test_docs.py` 的黑名单盯着它别回来。）
#
#  ⚠ 本包不强制依赖 numpy / rknnlite / tokenizers: 真模型在**调用时**才报带安装提示的错
#    （三样都延迟导入）, 所以宿主机上照样能 import 本包。
#
#  T7-3 起这里还多了一条**不碰 NPU** 的路: `tag_index.TagIndex` —— 读数据文件里
#  已经存好的向量做挑图（纯 Python 点积, 开发机也能跑）。它只读、不写、不读配置。
# ============================================================================

from .roi import DEFAULT_ROI, ROI_SPACE, RoiError, is_valid_roi, parse_roi
from .siglip import SiglipConfig, SiglipError, SiglipModel
from .tag_index import MatchResult, TagIndex, TagIndexError

__all__ = [
    # ROI
    "parse_roi",
    "is_valid_roi",
    "RoiError",
    "ROI_SPACE",
    "DEFAULT_ROI",
    # SigLIP —— 真 RKNN 双塔（图像 + 文本；打标签 / 检索 / 零样本分类）
    "SiglipModel",
    "SiglipConfig",
    "SiglipError",
    # 标签索引（T7-3）—— 读 config/wall_data.jsonl，纯 Python 挑图
    "TagIndex",
    "TagIndexError",
    "MatchResult",
]
