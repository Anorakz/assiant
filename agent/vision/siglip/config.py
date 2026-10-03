# ============================================================================
#  agent/vision/siglip/config.py — SigLIP 推理配置
#
#  配置从哪来（与板端实验树的唯一差别）
#  ---------------------------------------------------------------------------
#  本包是从板端实验树 `sig/siglip/`（已实测验证）搬进仓库的，行为不变，差别只有一处：
#      · 实验树: sig/config/sig.env（KEY=VALUE 的 sh 片段）
#      · 仓库版: config/config.yaml 的 `vision:` 段 —— 与"配置只有一个真源"一致
#
#  哪些能配、哪些写死（这条边界是故意的）
#  ---------------------------------------------------------------------------
#  能配（`vision:` 段）—— 都是"这台机器上文件放哪儿 / 怎么跑"的事:
#      model_path / tokenizer_path / runtime_lib / verbose / warmup_runs
#  写死（下面的常量）—— 这些是**模型契约**, 不是用户偏好:
#      256×256、3 通道、nhwc、uint8 **原始 0-255**、64 token、pad=1、768 维、
#      L2 归一化、只能按余弦。
#      为什么写死: 改错任何一条都不会报错, 只会**静默劣化**。实测过的两个例子 ——
#        · 喂 0-1 浮点: 不报错、输出也不为 0, 但判别力全丢（不同图余弦全 0.98+）
#        · int8 模型的文本塔: 不同文本得到几乎相同的 embedding（0/3 对角占优）
#      换模型 = 改这里的常量 + 重跑参考向量, 而不是"在配置里试试"。
#
#  validate() 不查文件在不在（那是运行期的事），只查**契约自洽**:
#  它拦的是"有人手改常量改出了自相矛盾的一组值"。
# ============================================================================

from __future__ import annotations

import os
from typing import Any, Mapping, Optional

from .errors import SiglipConfigError

__all__ = [
    "SiglipConfig",
    "DEFAULT_MODEL_PATH",
    "DEFAULT_TOKENIZER_PATH",
    "DEFAULT_RUNTIME_LIB",
    "IMAGE_SIZE",
    "IMAGE_CHANNEL",
    "TEXT_LEN",
    "TEXT_PAD_ID",
    "EMBED_DIM",
    "RUNTIME_MIN_VERSION",
    "CONFIG_SECTION",
]

#: 这份配置读的是 config.yaml 里的哪一段
CONFIG_SECTION = "vision"

# ---- 能配的默认值（板端布局；config.example.yaml 里有同样的注释）------------
DEFAULT_MODEL_PATH = "/home/kickpi/model/siglip_full.rknn"
DEFAULT_TOKENIZER_PATH = "/home/kickpi/model/siglip_tokenizer/tokenizer.json"
DEFAULT_RUNTIME_LIB = "/usr/lib/librknnrt.so"

# ---- 模型契约（写死，见文件头）----------------------------------------------
#: 输入图像边长（模型 = google/siglip-base-patch16-256）
IMAGE_SIZE = 256
IMAGE_CHANNEL = 3
#: rknnlite 只接受 nhwc（传 nchw 直接 KeyError）
IMAGE_LAYOUT = "nhwc"
#: **原始量程**，不是 0-1
IMAGE_DTYPE = "uint8"
IMAGE_MEAN = 127.5
IMAGE_STD = 127.5

#: 文本长度固定 64，且模型**没有 attention_mask** —— pad 位会被真实 attend
TEXT_LEN = 64
#: pad_token = `</s>` = 1（HF SiglipTokenizer 默认；实测比 0 的匹配分略高）
TEXT_PAD_ID = 1
TEXT_TRUNCATE = True

#: 输入/输出名（只用于报错信息，rknnlite 按位置喂）
IMAGE_INPUT_NAME = "pixel_values"
IMAGE_EMBEDS_NAME = "image_embeds"
TEXT_INPUT_NAME = "input_ids"
TEXT_INPUT_DTYPE = "int64"
TEXT_EMBEDS_NAME = "text_embeds"

EMBED_DIM = 768
OUTPUT_DTYPE = "float32"
#: Python 侧统一 L2 归一化一次，保证点积就是余弦
L2_NORMALIZE = True
#: 归一化（(x-127.5)/127.5）**已烧进 rknn 计算图**，Python 侧再做一次就是错的
NORMALIZE_IN_GRAPH = True
#: 只能按余弦排序：logit_scale / logit_bias 没随模型导出，拿不到标定概率
SIMILARITY = "cosine"
#: RK3568 上 core_mask 只被 RK3588 支持，传了 init 直接 -1
NPU_CORE = "auto"

#: librknnrt 至少要这个版本才能加载 model version 6（1.4.0 会报 invalid model version）
RUNTIME_MIN_VERSION = "1.6.0"

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def _bool(value: Any, key: str, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise SiglipConfigError("vision.%s 必须是 true/false，实际为 %r" % (key, value))


def _int(value: Any, key: str, default: int) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise SiglipConfigError("vision.%s 必须是整数，实际为 %r" % (key, value))
    try:
        return int(value)
    except (TypeError, ValueError):
        raise SiglipConfigError("vision.%s 必须是整数，实际为 %r" % (key, value))


def _text(value: Any, default: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


class SiglipConfig(object):
    """完整 SigLIP（双塔）的推理配置。

    属性名与板端实验树保持一致（`model_path` / `image_width` / `text_len` …），
    这样 model.py / runtime.py / tokenizer.py 是从实验树原样搬过来的。
    """

    __slots__ = (
        "model_path", "model_name", "precision", "target_platform",
        "tokenizer_path", "text_len", "text_pad_id", "text_input_name",
        "text_input_dtype", "text_embeds_name", "text_truncate",
        "image_input_name", "image_embeds_name",
        "image_width", "image_height", "image_channel", "image_layout",
        "image_dtype", "normalize", "mean", "std",
        "embed_dim", "output_dtype", "l2_normalize", "similarity",
        "npu_core", "verbose", "warmup_runs",
        "runtime_lib", "runtime_min_version", "source",
    )

    def __init__(self, **kw: Any) -> None:
        for name in self.__slots__:
            setattr(self, name, kw.get(name))

    # ------------------------------------------------------------ 构造 ---
    @classmethod
    def from_config(cls, section: Optional[Mapping[str, Any]] = None) -> "SiglipConfig":
        """从 `config.yaml` 的 `vision:` 段构造。

        @param section `vision:` 段的 dict；None/缺键一律退回默认值
                       （板端路径就是默认值，所以不配这一段也能跑）
        @raise SiglipConfigError 值类型不对，或契约自相矛盾
        """
        cfg = section if isinstance(section, Mapping) else {}
        conf = cls(
            model_path=_text(cfg.get("model_path"), DEFAULT_MODEL_PATH),
            model_name=_text(cfg.get("model_name"), "siglip-base-patch16-256"),
            precision=_text(cfg.get("precision"), "fp16"),
            target_platform=_text(cfg.get("target_platform"), "rk3568"),
            tokenizer_path=_text(cfg.get("tokenizer_path"), DEFAULT_TOKENIZER_PATH),
            runtime_lib=_text(cfg.get("runtime_lib"), DEFAULT_RUNTIME_LIB),
            runtime_min_version=RUNTIME_MIN_VERSION,
            verbose=_bool(cfg.get("verbose"), "verbose", False),
            warmup_runs=_int(cfg.get("warmup_runs"), "warmup_runs", 1),
            source="config.yaml#%s" % CONFIG_SECTION,
            # ---- 以下全是模型契约（写死）----
            text_len=TEXT_LEN,
            text_pad_id=TEXT_PAD_ID,
            text_input_name=TEXT_INPUT_NAME,
            text_input_dtype=TEXT_INPUT_DTYPE,
            text_embeds_name=TEXT_EMBEDS_NAME,
            text_truncate=TEXT_TRUNCATE,
            image_input_name=IMAGE_INPUT_NAME,
            image_embeds_name=IMAGE_EMBEDS_NAME,
            image_width=IMAGE_SIZE,
            image_height=IMAGE_SIZE,
            image_channel=IMAGE_CHANNEL,
            image_layout=IMAGE_LAYOUT,
            image_dtype=IMAGE_DTYPE,
            normalize=not NORMALIZE_IN_GRAPH,
            mean=IMAGE_MEAN,
            std=IMAGE_STD,
            embed_dim=EMBED_DIM,
            output_dtype=OUTPUT_DTYPE,
            l2_normalize=L2_NORMALIZE,
            similarity=SIMILARITY,
            npu_core=NPU_CORE,
        )
        conf.validate()
        return conf

    # ------------------------------------------------------------ 校验 ---
    def validate(self) -> None:
        if self.image_layout != "nhwc":
            # 实测 rk3568 的 rknnlite 只接受 nhwc（传 nchw 会 KeyError）
            raise SiglipConfigError("image_layout 只支持 nhwc，实际 %r" % self.image_layout)
        if self.image_channel != 3:
            raise SiglipConfigError("image_channel 必须是 3，实际 %r" % self.image_channel)
        if self.image_dtype != "uint8":
            raise SiglipConfigError(
                "image_dtype 必须是 uint8（喂 0-1 浮点会静默劣化判别力），实际 %r"
                % self.image_dtype)
        if self.text_len <= 0:
            raise SiglipConfigError("text_len 必须为正，实际 %r" % self.text_len)
        if self.text_pad_id < 0:
            raise SiglipConfigError("text_pad_id 不能为负，实际 %r" % self.text_pad_id)
        if self.embed_dim <= 0:
            raise SiglipConfigError("embed_dim 必须为正，实际 %r" % self.embed_dim)
        if self.similarity != "cosine":
            raise SiglipConfigError("similarity 目前只支持 cosine（logit_scale/bias 未随模型导出）")
        if self.npu_core not in ("auto", "", "0", "0_1", "0_1_2"):
            raise SiglipConfigError("npu_core 非法: %r" % self.npu_core)
        if self.warmup_runs is None or int(self.warmup_runs) < 0:
            # "-1 次预热"没有意义；0 是合法的（= 不预热）
            raise SiglipConfigError("warmup_runs 必须 >= 0，实际 %r" % (self.warmup_runs,))
        if self.normalize:
            # 模型的 mean/std 已烧进计算图，Python 侧再归一化就是错的
            raise SiglipConfigError(
                "Python 侧归一化必须关闭：归一化已烧进 rknn（mean=std=127.5），"
                "在 Python 侧再做一次会得到垃圾向量")

    # ------------------------------------------------------------ 摘要 ---
    def describe(self) -> str:
        return ("SigLIP(%s, %s/%s) in=%dx%dx%d %s %s text_len=%d pad=%d out=%d %s core=%s"
                % (os.path.basename(str(self.model_path)), self.model_name, self.precision,
                   self.image_width, self.image_height, self.image_channel,
                   self.image_layout, self.image_dtype, self.text_len, self.text_pad_id,
                   self.embed_dim, self.output_dtype, self.npu_core))

    def __repr__(self) -> str:
        return "<SiglipConfig %s>" % self.describe()
