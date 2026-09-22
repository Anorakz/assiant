# ============================================================================
#  agent/tools/wallpaper.py — "换壁纸" 工具（Phase 7 T3）
#
#  它做什么: 让板子屏幕上的背景图换成**下一张** —— 从配置的壁纸目录里按文件名顺序
#            翻, 然后把结果推给 GUI（topic `wallpaper`, 线格式见 docs/ipc-protocol.md §3）。
#
#  同一个动作有**两条入口**, 共用同一份实现
#  ---------------------------------------------------------------------------
#      GUI 主区右下角「下一张」（命令 next_wallpaper）  ┐
#                                                      ├─▶ Runtime.next_wallpaper(step)
#      LLM 调工具 next_wallpaper(step)                 ┘        └─ WallpaperDeck.step()
#  所以工具这里不自己挑图、不自己推 IPC: 它只是把 LLM 的入参转给同一个入口。
#  这样"下一张是哪张"只有一个地方说了算（agent/core/wallpaper.py）。
#
#  ⚠ 让 LLM 知道的三件事（都写进 description）
#  ---------------------------------------------------------------------------
#    1. 只改**显示**, 不动任何文件（不是删除/移动壁纸）
#    2. 是"翻页"不是"指定某张": 想要特定图得先知道目录里有什么 —— 那属于**标签化**
#       之后的事（见 todo.md 的"按内容挑图"）
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
    "把板子屏幕上的背景图换成下一张（壁纸目录里按文件名翻页）。"
    "step=1 下一张、-1 上一张、0 重推当前这张。"
    "⚠ 只改显示, 不动任何文件; 它按顺序翻页, 不能指定某一张; 推给界面后没有回执。"
)

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "step": {
            "type": "integer",
            "minimum": -50,
            "maximum": 50,
            "description": "往前翻几张（负数=往回翻, 0=重推当前这张）; 默认 1",
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

    def handler(step: int = 1) -> Dict[str, Any]:
        return advance(step)

    return Tool(
        name=NAME,
        description=DESCRIPTION,
        schema=SCHEMA,
        handler=handler,
        allowed_states=set(ALLOWED_STATES),
    )
