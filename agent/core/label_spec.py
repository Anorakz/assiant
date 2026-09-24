#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent/core/label_spec.py — 标签/条件写法的**统一小语法**（T8-5b）

为什么要有它
    壁纸的 `match=` 与音乐的 `tag=` / `set_tag=` 其实是同一件事："某个轴上的某条标签"。
    但 T7/T8 各写各的解析 —— 壁纸在 `agent/vision/tag_index.py` 里有一套
    `_split_spec` / `_pick_labels`，音乐那边干脆收一个 `{轴: [值]}` 的 JSON 对象。
    结果是 ① 模型要学两套写法（板端实测小模型本来就容易填错）② 工具 schema 里多一个
    对象参数，白白占上下文（T8-5 实测工具清单占第一轮 prompt 的 90%）。
    这里把它收成**一处纯字符串语法**，壁纸 / 音乐 / 工具都调它。

语法（就这三条）
    ``anime``                   只写标签名 —— 在**所有轴**里找同名标签
    ``scene=anime``             某个轴上的某条标签
    ``scene=anime/landscape``   同一个轴上多条 —— **任一命中**（分隔符 `/ , ; 、 ， ； 空格`）
    ``mood=燃; style=rock``     多个轴 —— 段之间用 `;` / `；`
    ``ip=EVA``                  壁纸保留键：`ip` 走锚点检索（这里只负责拆出键值）

⚠ 三条边界（都有测试钉着）
    1. **它不认识词表**：不认识的标签**原样返回**。谁校验由调用方决定 —— 壁纸那边把
       不认识的放进 `unknown` 如实报出来，音乐写标签时直接写进库里。
    2. **轴名小写归一**（`Scene=anime` == `scene=anime`；轴名本来就是小写的英文）；
       **标签值保持原样**（词表里是小写裸串，但音乐 tag 可能是中文，不能改）。
    3. **不猜**：写错了（例如把两个轴用空格连起来）就照拆 —— 多轴一律要 `;`，
       因为空格和 `/` 在**值**这一层本来就是分隔符（T7 板端实测模型会写
       `scene=space/technology/anime`）。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

__all__ = ["EMPTY_AXIS", "split_key", "split_values", "parse", "parse_one",
           "format_tags", "is_qualified"]

#: 没有写轴时用的键（"" = "所有轴里找"）。写成常量免得各处硬编码空串。
EMPTY_AXIS = ""

#: 段与段之间的分隔符（多轴）—— 只用分号，见文件头第 3 条边界。
_SEGMENT_SPLIT_RE = re.compile(r"[;；]+")

#: 值内部的分隔符（同一个轴上多条标签）。与 `agent/vision/tag_index.py` 的老行为一致:
#: 半角/全角斜杠、逗号、分号、顿号、空白都算。
_VALUE_SPLIT_RE = re.compile(r"[\/,;、，；\s]+")


def split_values(value: Any) -> List[str]:
    """把 `anime/landscape` 这样的值拆成多条标签（去空白、去重、保序）。

    @return 空串 / None -> `[]`（调用方自己决定这是"没写"还是"写错了"）
    """
    text = str(value) if value is not None else ""
    out: List[str] = []
    for part in _VALUE_SPLIT_RE.split(text):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out


def split_key(text: Any) -> Tuple[Optional[str], str]:
    """把 `scene=anime` / `ip: EVA` 拆成 (轴, 值)。

    @return (None, 原文本) = 没有 `=`/`:` —— 只写了一个标签名
    @note 轴名小写；`=anime`（键是空的）按"只写了标签名"处理 —— 手滑不该报错。
    """
    raw = str(text) if text is not None else ""
    for separator in ("=", ":"):
        position = raw.find(separator)
        if position < 0:
            continue
        key = raw[:position].strip().lower()
        value = raw[position + 1:].strip()
        if not key:
            return None, value
        return key, value
    return None, raw.strip()


def _segments(spec: Any) -> List[str]:
    raw = str(spec) if spec is not None else ""
    return [part.strip() for part in _SEGMENT_SPLIT_RE.split(raw) if part.strip()]


def parse(spec: Any) -> Dict[str, List[str]]:
    """把一条（或多条、用 `;` 隔开的）标签规格解析成 `{轴: [值…]}`。

    @return 没写轴的那些值放在 `EMPTY_AXIS`（`""`）键下
    @note 空输入 -> `{}`（不是 `{"": []}`）—— 调用方用 `if not parsed` 判空即可。
    """
    out: Dict[str, List[str]] = {}
    for segment in _segments(spec):
        key, value = split_key(segment)
        bucket = out.setdefault(key or EMPTY_AXIS, [])
        for label in split_values(value):
            if label not in bucket:
                bucket.append(label)
    return {key: values for key, values in out.items() if values}


def parse_one(spec: Any) -> Tuple[Optional[str], List[str]]:
    """只要**第一段**：`(轴 或 None, [值…])`。

    壁纸的 `match=` 与音乐的筛选 `tag=` 都是"一条"语义（IP 检索本来就是单个名字）。
    """
    segments = _segments(spec)
    if not segments:
        return None, []
    key, value = split_key(segments[0])
    return key, split_values(value)


def format_tags(tags: Optional[Mapping[str, Sequence[str]]]) -> str:
    """把 `{轴: [值…]}` 写回字符串（回话里给人看 / 记日志用）。

    @return 例如 `"mood=燃; style=rock"`；没轴的段只写值；空 -> `""`
    """
    parts: List[str] = []
    for key, values in (tags or {}).items():
        labels = [str(v) for v in (values or []) if str(v)]
        if not labels:
            continue
        parts.append("%s=%s" % (key, "/".join(labels)) if key else "/".join(labels))
    return "; ".join(parts)


def is_qualified(spec: Any) -> bool:
    """写没写轴（`"mood=燃"` -> True，`"燃"` -> False）。"""
    key, _ = parse_one(spec)
    return key is not None
