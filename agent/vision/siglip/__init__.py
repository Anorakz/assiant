# ============================================================================
#  agent/vision/siglip/__init__.py — 完整 SigLIP（双塔）推理模块
#
#  对外只暴露稳定的几个名字；内部实现（runtime/tokenizer 细节）不在这里透出，
#  避免调用方依赖私有实现。
#
#  典型用法
#  ---------------------------------------------------------------------------
#      from agent.vision.siglip import SiglipModel
#
#      model = SiglipModel.from_config(vision_section)
#      emb   = model.encode_image(img_uint8_256)        # (768,) 单位向量
#      best  = model.best(img_uint8_256, ["a mountain", "a city at night"])
#
#  注意：import 本模块**不会**依赖 rknnlite / tokenizers / numpy —— 它们都是
#  延迟导入的，所以没有 NPU 的宿主机也能 import 并跑纯逻辑单测（注入假 runtime）。
#
#  来历：本包从板端实验树 `sig/siglip/`（实测验证过）搬进仓库，唯一差别是配置
#  来源换成 config/config.yaml 的 `vision:` 段 —— 见 config.py 文件头。
# ============================================================================

from .config import (
    CONFIG_SECTION,
    EMBED_DIM,
    IMAGE_SIZE,
    RUNTIME_MIN_VERSION,
    TEXT_LEN,
    SiglipConfig,
)
from .errors import (
    SiglipConfigError,
    SiglipError,
    SiglipInputError,
    SiglipRuntimeError,
    SiglipTokenizeError,
)
from .model import SiglipModel
from .runtime import RknnRuntime
from .tokenizer import SiglipTokenizer

__all__ = [
    "SiglipModel",
    "SiglipConfig",
    "SiglipTokenizer",
    "RknnRuntime",
    "SiglipError",
    "SiglipConfigError",
    "SiglipTokenizeError",
    "SiglipInputError",
    "SiglipRuntimeError",
    "CONFIG_SECTION",
    "IMAGE_SIZE",
    "TEXT_LEN",
    "EMBED_DIM",
    "RUNTIME_MIN_VERSION",
]
