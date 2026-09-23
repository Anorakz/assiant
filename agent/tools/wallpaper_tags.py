# ============================================================================
#  agent/tools/wallpaper_tags.py — "库里有什么壁纸标签" 工具（Phase 7 T7-3）
#
#  它做什么: 让 LLM **先看一眼**再挑图 —— 库里有哪些标签、各有几张；
#            给了 `ip_query` 就换成"某个作品最像哪几张"的排名。
#
#  为什么必须要有这个工具
#  ---------------------------------------------------------------------------
#  标签是**词表决定的上限**（零样本分类选不出词表外的东西，见 docs/tagging.md §2），
#  而词表在配置文件/代码里，模型看不见。没有这个工具，模型只能猜
#  "有 anime 吗？"，猜错了 `next_wallpaper(match=…)` 就会如实报错 —— 用户白等一轮。
#  所以它是 next_wallpaper 的**前置查询**: 清单 -> 挑条件 -> 换图。
#
#  ⚠ 三件让 LLM 知道的事（写进 description）
#  ---------------------------------------------------------------------------
#    1. **只读**: 它不换壁纸、不写任何文件（换壁纸是 next_wallpaper 的事）
#    2. `ip_query` 用的是配置里给的锚点（works are 少样本检索, 不是模型认识作品名）;
#       没有锚点的作品会**如实说没有**, 不会瞎猜
#    3. 分数是**余弦**, 只能用来排序, 不是"准确率"
# ============================================================================

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from ..core.state_machine import State
from ..core.tool_router import Tool

__all__ = ["NAME", "ALLOWED_STATES", "DESCRIPTION", "SCHEMA", "build"]

_log = logging.getLogger(__name__)

NAME = "list_wallpaper_tags"

#: 与 next_wallpaper 同一套状态（只读, 但归在"壁纸"这一类动作里）。
ALLOWED_STATES = (State.IDLE, State.STUDY)

DESCRIPTION = (
    "看看壁纸库里有哪些标签、各有几张 —— 挑图之前先看一眼，免得猜错标签名。"
    "给了 ip_query（**只填作品名**，例如 \"EVA\"）就换成\"这个作品最像哪几张\"的排名。"
    "⚠ 只读，不换壁纸；ip_query 不是问题、不是句子；分数是余弦（只能排序，不是准确率）。"
)

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "ip_query": {
            "type": "string",
            "minLength": 1,
            "maxLength": 32,
            "description": "作品名，例如 EVA、Nier、GitS（只能填名字，不要填问句或句子）",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 20,
            "description": "每轴最多列几条标签 / 最多回几张图（默认 5）",
        },
    },
    "additionalProperties": False,
}


def build(services: Dict[str, Any]) -> Optional[Tool]:
    """造工具。缺 `wallpaper_tags` 入口就返回 None（并说清为什么）。"""
    lookup = (services or {}).get("wallpaper_tags")
    if lookup is None:
        _log.warning("tools: list_wallpaper_tags 需要 services['wallpaper_tags']，"
                     "但装配里没有 —— 跳过")
        return None
    if not callable(lookup):
        _log.warning("tools: services['wallpaper_tags'] 不是可调用的 —— 跳过 list_wallpaper_tags")
        return None

    def handler(ip_query: Optional[str] = None, limit: int = 5) -> Dict[str, Any]:
        return lookup(ip_query, limit)

    return Tool(
        name=NAME,
        description=DESCRIPTION,
        schema=SCHEMA,
        handler=handler,
        allowed_states=set(ALLOWED_STATES),
    )
