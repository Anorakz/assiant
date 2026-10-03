# -*- coding: utf-8 -*-
"""agent/tools/_common.py —— 五个工具的参数归一化里**同一套机械步骤**（T15-3 / 3-6b）

谁在用: `agent/tools/{bilibili,music,schedule,wallpaper}.py` 的 `normalize()`。
它们各自那份 `normalize()` 的**语义规则**（哪个同义键搬到哪儿、哪个字段怎么解释）
留在各自模块里 —— 那是那个工具的语义, 不是公共件; 这里只放**逐字相同**的几步:

  · 空值写法表（模型把"没给"写成 `"none"` / `"无"` / `""` 这些字面量）;
  · `clean_text()`: 去首尾空白 + 空值字面量 -> None;
  · `action_of()`: `action` 的去空白 + 小写 + 同义词表。

⚠ 收敛口径是**等价改写**, 所以两处都按"只有字符串才动它"办: 非字符串的 `action`
   （比如模型给了个数字）**原样返回**, 由各工具后面那句 `if action:` 自己判断 ——
   行为与收敛前逐字一致。
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

#: 空值写法 —— 模型有时候把"没给"写成这些字面量（板端实测见过 `track_id="none"`）
EMPTY_VALUES = ("", "none", "null", "nil", "n/a", "na", "-", "无", "空")


def clean_text(value: Any, empty_values: Sequence[str] = EMPTY_VALUES) -> Optional[str]:
    """字符串字段清洗: 去首尾空白; 空值字面量 -> None（= 没给）。

    @param empty_values 空值写法表（默认公共那套；schedule 多认一个「不填」）
    """
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in empty_values:
        return None
    return text


def action_of(args: Mapping[str, Any], synonyms: Mapping[str, str]) -> Any:
    """`action` 的机械改写: 去首尾空白 + 小写 + 同义词表（认不出 -> 原样）。

    @return 字符串 -> 改写后的字符串（空串就是空串, 不编一个默认出来）;
            非字符串 -> **原样返回**（调用方自己会当"这不是个能用的 action"）
    """
    action = args.get("action")
    if not isinstance(action, str):
        return action
    action = action.strip().lower()
    return synonyms.get(action, action)
