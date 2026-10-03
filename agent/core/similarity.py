# -*- coding: utf-8 -*-
"""agent/core/similarity.py —— 余弦相似度的唯一实现（T15-3 / 3-6b）

原来两份：`core/game_anchors.py` 与 `vision/tag_index.py`。两者在**长度不一致**时的
行为不同 —— 前者直接 0.0，后者用 zip 会**静默截断**（短的为准）算出个没有意义的数。
收敛取**更严格**那版：长度不一致 → 0.0（"比不了"就不比，而不是给个假分数）。
"""
from __future__ import annotations

import math
from typing import Sequence


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """两向量的余弦相似度。

    @return 长度不一致 / 任一为空 / 任一模长为 0 → **0.0**（不当成相似）
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = norm_a = norm_b = 0.0
    for left, right in zip(a, b):
        dot += left * right
        norm_a += left * left
        norm_b += right * right
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return float(dot / math.sqrt(norm_a * norm_b))
