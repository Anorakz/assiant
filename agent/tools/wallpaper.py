# ============================================================================
#  agent/tools/wallpaper.py — "换壁纸" 工具（Phase 7 T3；T7-3 起能按内容挑）
#
#  它做什么: 让板子屏幕上的背景图换一张 —— 默认按文件名翻下一页；给了 `match`
#            就**先按内容筛**（标签 / IP），再在筛出来的候选里翻（相关度最高的在前）。
#            最后把结果推给 GUI（topic `wallpaper`, 线格式见 docs/ipc-protocol.md §3）。
#
#  ⚠ T7-3 的入口变化: **手动换壁纸都删掉了**
#  ---------------------------------------------------------------------------
#      T3   GUI 主区「下一张」按钮 ┐
#           LLM 工具 next_wallpaper ├─▶ Runtime.next_wallpaper(step)
#      T6   + 同名 IPC 命令受状态权限表约束
#      T7-3 GUI 按钮、IPC 命令**删掉**；工具加上 `match=`
#                                      └─▶ Runtime.next_wallpaper(step, match)
#                                              └─ WallpaperDeck.step(step, pool)
#  也就是说: 现在**只有对话**能换壁纸（"换一张安静的深色风景"），
#  因为"换成什么样"这件事只有自然语言说得清，按钮只能按文件名翻。
#
#  ⚠ 让 LLM 知道的三件事（都写进 description）
#  ---------------------------------------------------------------------------
#    1. 只改**显示**, 不动任何文件（不是删除/移动壁纸）
#    2. match 的三条写法（轴=标签 / 只写标签 / ip=名字）—— 写错了会**如实报错**,
#       不会"随便换一张糊弄过去"
#    3. 推给 GUI 之后**没有回执**（GUI 是否真的画上去了, Agent 不知道）
# ============================================================================

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from ..core.state_machine import State
from ..core.tool_router import Tool

__all__ = ["NAME", "ALLOWED_STATES", "DESCRIPTION", "SCHEMA", "build"]

_log = logging.getLogger(__name__)

NAME = "next_wallpaper"

#: 允许在哪些状态里换壁纸。
#: GAME 不在内: 主区那时是视频区, 换壁纸等于白换（SLEEP 要不要放行留给 T4 的状态表定）。
ALLOWED_STATES = (State.IDLE, State.STUDY)

DESCRIPTION = (
    "把板子屏幕上的背景图换成另一张。"
    "step=1 往后翻、-1 往前翻、0 重推当前这张。"
    "想按内容挑就给 match：\"scene=anime\"（场景轴上的某条标签）、"
    "\"anime\"（只写标签名，各轴里找）、\"ip=EVA\"（某个作品，按锚点图检索）。"
    "不知道库里有什么标签时，先用 list_wallpaper_tags 看一眼。"
    "⚠ 只改显示, 不动任何文件; 推给界面后没有回执。"
)

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "step": {
            "type": "integer",
            "minimum": -50,
            "maximum": 50,
            "description": "在候选里往前翻几张（负数=往回翻, 0=重推当前这张）; 默认 1",
        },
        "match": {
            "type": "string",
            "minLength": 1,
            "maxLength": 64,
            "description": "挑图条件（不写=按文件名翻下一页）: "
                           "\"scene=anime\" / \"anime\" / \"ip=EVA\"",
        },
    },
    "additionalProperties": False,
}


def build(services: Dict[str, Any]) -> Optional[Tool]:
    """造工具。缺 `next_wallpaper` 入口就返回 None（并说清为什么）。

    ⚠ 依赖的是**运行时那个入口**（`agent/main.py::Runtime.next_wallpaper`), 不是壁纸目录:
      目录不存在/是空的属于**运行期**问题, 调的时候才知道（那时才该报错给用户看）。
      启动时目录还没建好, 不该让这个工具消失。
    """
    advance = (services or {}).get("next_wallpaper")
    if advance is None:
        _log.warning("tools: next_wallpaper 需要 services['next_wallpaper']，但装配里没有 —— 跳过")
        return None
    if not callable(advance):
        _log.warning("tools: services['next_wallpaper'] 不是可调用的 —— 跳过 next_wallpaper")
        return None

    def handler(step: int = 1, match: Optional[str] = None) -> Dict[str, Any]:
        return advance(step, match)

    return Tool(
        name=NAME,
        description=DESCRIPTION,
        schema=SCHEMA,
        handler=handler,
        allowed_states=set(ALLOWED_STATES),
    )
