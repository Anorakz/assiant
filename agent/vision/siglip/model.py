# ============================================================================
#  agent/vision/siglip/model.py — 完整 SigLIP（图像塔 + 文本塔）推理门面
#
#  对外就 5 个动作
#  ---------------------------------------------------------------------------
#      encode_image(img)            -> (768,) 单位向量
#      encode_text(text)            -> (768,) 单位向量
#      similarity(img, text)        -> float 余弦
#      similarities(img, [text...]) -> [float]
#      rank(img, [text...])         -> [(text, score)] 降序
#
#  设计要点（都是实测逼出来的）
#  ---------------------------------------------------------------------------
#  · **单实例复用**：实测反复 load/release 会累积内存（release 只归还一部分），
#    所以模型只该建一次；close() 留给进程退出。
#  · **一次前向出两个塔**：模型是融合图（2 输入 2 输出），所以算图像 embedding
#    也必须喂一份 input_ids（用全 0 占位），算文本 embedding 也必须喂一张图
#    （用全 0 图占位）。实测两个塔互不影响（换图 text_embeds 不变，反之亦然），
#    所以占位是安全的。
#  · **文本 embedding 有缓存**：不同图配同一批候选文本是高频场景（图像检索/
#    零样本分类），缓存能省掉重复前向。
#  · 归一化：模型输出已近似单位长度，但仍在 Python 侧统一 L2 归一化一次，
#    保证下游点积就是余弦。Python 侧归一化必须关闭（已烧进图里）。
#
#  来历：从板端实验树 `sig/siglip/model.py` 搬入，唯一改动是把
#  `from_env()` 换成 `from_config()`（配置来自 config.yaml 的 vision: 段）。
# ============================================================================

from __future__ import annotations

import os
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .config import SiglipConfig
from .errors import SiglipInputError, SiglipRuntimeError
from .tokenizer import SiglipTokenizer

__all__ = ["SiglipModel"]


class SiglipModel(object):
    """完整 SigLIP 的推理门面（可注入 runtime / tokenizer 以便单测）。"""

    def __init__(self, config: SiglipConfig, runtime: Any = None,
                 tokenizer: Any = None, text_cache_size: int = 256) -> None:
        self.config = config
        self._runtime = runtime
        self.tokenizer = tokenizer or SiglipTokenizer(
            config.tokenizer_path, text_len=config.text_len,
            pad_id=config.text_pad_id, truncate=config.text_truncate)
        self._owned_runtime = runtime is None
        self._text_cache: Dict[str, Any] = {}
        self._text_cache_size = int(text_cache_size)

    # ------------------------------------------------------------ 构造 ---
    @classmethod
    def from_config(cls, section: Optional[Mapping[str, Any]] = None,
                    runtime: Any = None, tokenizer: Any = None) -> "SiglipModel":
        """从 `config.yaml` 的 `vision:` 段构造（None = 全用默认值）。

        ⚠ 构造**不加载模型**（不碰 rknnlite）：真正加载在第一次推理或 load()。
        """
        return cls(SiglipConfig.from_config(section), runtime=runtime, tokenizer=tokenizer)

    # ------------------------------------------------------------ 生命周期 ---
    @property
    def ready(self) -> bool:
        return self._runtime is not None

    def load(self) -> "SiglipModel":
        """显式加载运行时（幂等）。不调用也行，首次推理会自动加载。"""
        if self._runtime is None:
            from .runtime import RknnRuntime

            self._runtime = RknnRuntime(
                self.config.model_path, image_dtype=self.config.image_dtype,
                text_dtype=self.config.text_input_dtype, npu_core=self.config.npu_core,
                verbose=self.config.verbose, warmup_runs=self.config.warmup_runs)
            # 预热由 RknnRuntime 内部按 warmup_runs 做，这里不重复跑
        return self

    def close(self) -> None:
        if self._runtime is not None and self._owned_runtime:
            close = getattr(self._runtime, "close", None)
            if callable(close):
                close()
        self._runtime = None
        self._text_cache.clear()

    def __enter__(self) -> "SiglipModel":
        return self.load()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------ 内部 ---
    def _np_dtype(self):
        import numpy as np

        return np.dtype(self.config.image_dtype)

    def _empty_image(self):
        import numpy as np

        return np.zeros((1, self.config.image_height, self.config.image_width,
                         self.config.image_channel), dtype=self._np_dtype())

    def _empty_ids(self):
        import numpy as np

        return np.zeros((1, self.config.text_len), dtype=np.int64)

    def _check_image(self, image):
        """把输入规整成 (1,H,W,C) 并校验。"""
        import numpy as np

        if image is None:
            raise SiglipInputError("image 不能为 None")
        arr = np.asarray(image)
        if arr.ndim == 3:
            arr = arr[None, ...]
        if arr.ndim != 4:
            raise SiglipInputError("image 期望 (H,W,C) 或 (1,H,W,C)，实际 shape=%s" % (arr.shape,))
        want = (1, self.config.image_height, self.config.image_width, self.config.image_channel)
        if tuple(arr.shape) != want:
            raise SiglipInputError("image 形状应为 %s，实际 %s" % (want, tuple(arr.shape)))
        if arr.dtype != self._np_dtype():
            if np.issubdtype(arr.dtype, np.floating):
                raise SiglipInputError(
                    "image dtype 应为 %s，实际 %s —— 注意：喂 0-1 浮点会静默劣化判别力，"
                    "必须喂 0-255 原始量程" % (self.config.image_dtype, arr.dtype))
            arr = arr.astype(self._np_dtype())
        return arr

    @staticmethod
    def _unit(vec):
        import numpy as np

        v = np.asarray(vec, dtype=np.float32).reshape(-1)
        n = float(np.linalg.norm(v))
        return v / n if n > 0 else v

    def _post(self, raw, want_dim: bool = True):
        import numpy as np

        v = np.asarray(raw, dtype=np.float32).reshape(-1)
        if want_dim and v.shape[0] != self.config.embed_dim:
            raise SiglipRuntimeError(
                "embedding 维度应为 %d，实际 %d" % (self.config.embed_dim, v.shape[0]))
        return self._unit(v) if self.config.l2_normalize else v

    # ------------------------------------------------------------ 编码 ---
    def encode_image(self, image):
        """(H,W,C) uint8 0-255 → (768,) 单位向量。"""
        self.load()
        arr = self._check_image(image)
        img_emb, _ = self._runtime.run(arr, self._empty_ids())
        return self._post(img_emb)

    def encode_text(self, text: str, use_cache: bool = True):
        """文本 → (768,) 单位向量。相同文本会走缓存（不同图配同一批候选文本是高频场景）。"""
        if use_cache and isinstance(text, str) and text in self._text_cache:
            return self._text_cache[text].copy()
        self.load()
        ids = self.tokenizer.encode(text)          # 内含类型/空串校验
        _, txt_emb = self._runtime.run(self._empty_image(), ids)
        vec = self._post(txt_emb)
        if use_cache and isinstance(text, str):
            if len(self._text_cache) >= self._text_cache_size:
                self._text_cache.pop(next(iter(self._text_cache)))
            self._text_cache[text] = vec
        return vec.copy()

    # ------------------------------------------------------------ 相似度 ---
    def similarity(self, image, text: str) -> float:
        """图像与单条文本的余弦相似度。"""
        return float(self._post_embeds(self.encode_image(image), self.encode_text(text)))

    @staticmethod
    def _post_embeds(img_vec, txt_vec) -> float:
        return float((img_vec * txt_vec).sum())

    def similarities(self, image, texts: Sequence[str]) -> List[float]:
        """图像 embedding 只算一次；文本逐个算（命中缓存则零成本）。"""
        if not texts:
            raise SiglipInputError("texts 不能为空")
        img_vec = self.encode_image(image)
        return [float((img_vec * self.encode_text(t)).sum()) for t in texts]

    def rank(self, image, texts: Sequence[str], top_k: Optional[int] = None
             ) -> List[Tuple[str, float]]:
        """按相似度降序返回 [(text, score)]。"""
        scores = self.similarities(image, texts)
        pairs = sorted(zip(list(texts), scores), key=lambda kv: kv[1], reverse=True)
        return pairs[:top_k] if top_k else pairs

    def best(self, image, texts: Sequence[str]) -> Tuple[str, float]:
        """取最高分的一条（零样本分类/检索的最常用入口）。"""
        return self.rank(image, texts, top_k=1)[0]

    # ------------------------------------------------------------ 诊断 ---
    def stats(self) -> Dict[str, Any]:
        rt = self._runtime
        return {
            "model": os.path.basename(str(self.config.model_path)),
            "ready": self.ready,
            "calls": getattr(rt, "calls", 0),
            "text_cache": len(self._text_cache),
            "load_s": getattr(rt, "load_s", None),
            "init_s": getattr(rt, "init_s", None),
            "config": self.config.describe(),
        }

    def __repr__(self) -> str:
        return "<SiglipModel ready=%s %s>" % (self.ready, self.config.describe())
