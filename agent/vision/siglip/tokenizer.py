# ============================================================================
#  agent/vision/siglip/tokenizer.py — SigLIP 文本端分词（tokenizer.json 封装）
#
#  为什么用 tokenizer.json + tokenizers 库
#  ---------------------------------------------------------------------------
#  · 板端没有 transformers/sentencepiece，而 `tokenizers` 有 cp38 aarch64 wheel
#  · tokenizer.json 自带 normalizer/pre-tokenizer，行为与 HF 的 SiglipTokenizer
#    完全一致（含 do_lower_case、空白规整、32000 词表）
#
#  定长与 padding（关键）
#  ---------------------------------------------------------------------------
#  · 模型 input_ids 形状固定 [1, 64]，**且没有 attention_mask 输入** ——
#    也就是说 pad 位置会被真实 attend。实测 pad=1(`</s>`，HF 默认 pad_token)
#    比 pad=0 的匹配分略高，故采用 pad_id=1。
#  · 超出 64 直接截断（与 HF truncation=True 一致）。
#
#  可注入 backend
#  ---------------------------------------------------------------------------
#  backend 只需实现 `encode(text) -> 有 .ids 的对象` 与 `get_vocab_size()`；
#  单测用它替换真实 tokenizers.Tokenizer，不需要装 tokenizers 也能跑。
#
#  来历：从板端实验树 `sig/siglip/tokenizer.py` 原样搬入（已实测验证）。
# ============================================================================

from __future__ import annotations

import os
from typing import Any, List, Optional, Sequence

from .errors import SiglipConfigError, SiglipTokenizeError

__all__ = ["SiglipTokenizer"]


class SiglipTokenizer(object):
    """把一段文本变成 (1, text_len) 的 int64 token id 数组。"""

    def __init__(self, path: str, text_len: int = 64, pad_id: int = 1,
                 truncate: bool = True, backend: Any = None) -> None:
        if not isinstance(text_len, int) or text_len <= 0:
            raise SiglipConfigError("text_len 必须是正整数，实际 %r" % (text_len,))
        self.path = path
        self.text_len = text_len
        self.pad_id = int(pad_id)
        self.truncate = bool(truncate)
        self._backend = backend if backend is not None else self._load(path)

    # ------------------------------------------------------------ 加载 ---
    @staticmethod
    def _load(path: str):
        if not os.path.exists(path):
            raise SiglipConfigError("tokenizer 文件不存在: %s" % path)
        try:
            from tokenizers import Tokenizer  # 延迟导入：单测可注入 backend 后不需要它
        except ImportError as exc:
            raise SiglipConfigError(
                "缺少 tokenizers 库（板端装法：pip3 install --no-deps tokenizers==0.20.3）：%s" % exc)
        try:
            return Tokenizer.from_file(path)
        except Exception as exc:                      # noqa: BLE001
            raise SiglipConfigError("tokenizer.json 解析失败: %s" % exc)

    @property
    def vocab_size(self) -> int:
        try:
            return int(self._backend.get_vocab_size())
        except Exception:                             # noqa: BLE001
            return -1

    # ------------------------------------------------------------ 编码 ---
    def tokens(self, text: str) -> List[int]:
        """只做分词，**不加 padding**（调试/测试用）。"""
        if not isinstance(text, str):
            raise SiglipTokenizeError("文本必须是 str，实际 %s" % type(text).__name__)
        if not text.strip():
            raise SiglipTokenizeError("文本不能为空")
        ids = list(self._backend.encode(text).ids)
        if not ids:
            raise SiglipTokenizeError("分词结果为空: %r" % text)
        return ids

    def encode(self, text: str):
        """编码为 (1, text_len) 的 int64 数组；超长截断、不足补 pad_id。

        @note 先校验文本再 import numpy（搬运后的小改动）：这样"空串/类型不对"永远报
              SiglipTokenizeError，而不是"这台机器没 numpy"这种与调用方无关的错。
        """
        ids = self.tokens(text)
        import numpy as np

        if len(ids) > self.text_len:
            if not self.truncate:
                raise SiglipTokenizeError(
                    "文本 %d 个 token 超过 text_len=%d 且未开启截断" % (len(ids), self.text_len))
            ids = ids[: self.text_len]
        padded = ids + [self.pad_id] * (self.text_len - len(ids))
        return np.asarray([padded], dtype=np.int64)

    def encode_batch(self, texts: Sequence[str]):
        """批量编码为 (N, text_len) int64。"""
        import numpy as np

        if not texts:
            raise SiglipTokenizeError("texts 不能为空")
        return np.concatenate([self.encode(t) for t in texts], axis=0)

    def decode(self, ids: Sequence[int]) -> Optional[str]:
        """仅用于调试：把 id 还原成文本（backend 不支持时返回 None）。"""
        try:
            return self._backend.decode([int(i) for i in ids], skip_special_tokens=False)
        except Exception:                             # noqa: BLE001
            return None

    def __repr__(self) -> str:
        return "<SiglipTokenizer len=%d pad=%d vocab=%d %s>" % (
            self.text_len, self.pad_id, self.vocab_size, os.path.basename(self.path))
