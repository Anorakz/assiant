# ============================================================================
#  agent/tools/back_to_desktop.py — "回到桌面" 工具（Phase 7）
#
#  它做什么: 让**主机**（被串流的 Windows 机器）回到桌面 —— 经 native 的
#            `InputSender.show_desktop()` 发一次 **Win+D**。
#
#  为什么不是"锁屏"（这条决定有来历）
#  ---------------------------------------------------------------------------
#  Windows 会过滤**合成输入**的 Win+L：实测连主机本机的 keybd_event 合成也锁不上，
#  与 Ctrl+Alt+Del 同属安全动作。所以按用户的决定改成 "回到桌面"，而 Win+D 在真机上
#  是通的。机制早在 Phase 6 就位（agent/io/input_sender.py::show_desktop）。
#
#  ⚠ 两件必须让 LLM 知道的事（都写进 description）
#  ---------------------------------------------------------------------------
#    1. 它是**开关**：发第二次等于切回来
#    2. **没有回执**：实测连按两次里有一次没落地（第 2 次丢了，第 3 次才还原）。
#       所以一次调用只发一次，**别把它当幂等动作连着调**。
# ============================================================================

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from ..core.state_machine import State
from ..core.tool_router import Tool

__all__ = ["NAME", "ALLOWED_STATES", "DESCRIPTION", "SCHEMA", "build"]

_log = logging.getLogger(__name__)

NAME = "back_to_desktop"

#: 只允许在 STUDY 下用：这是"学习该收尾了"的动作（按约定，别在 GAME/IDLE 里乱按）。
ALLOWED_STATES = (State.STUDY,)

DESCRIPTION = (
    "让串流主机回到桌面（发一次 Win+D）。"
    "⚠ 它是开关动作且没有回执：连按两次等于来回切，一次调用只发一次，不要连着调用。"
)

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


def build(services: Dict[str, Any]) -> Optional[Tool]:
    """造工具。缺 `input_sender` 就返回 None（并说清为什么）。"""
    sender = (services or {}).get("input_sender")
    if sender is None:
        _log.warning("tools: back_to_desktop 需要 input_sender，但装配里没有 —— 跳过这个工具")
        return None
    if not hasattr(sender, "show_desktop"):
        _log.warning("tools: input_sender 没有 show_desktop()（native 没接？）—— 跳过 back_to_desktop")
        return None

    async def handler() -> Dict[str, Any]:
        await sender.show_desktop()
        return {
            "sent": "win+d",
            "note": "已发出一次；这是开关动作且没有回执，不要当幂等动作重复调用",
        }

    return Tool(
        name=NAME,
        description=DESCRIPTION,
        schema=SCHEMA,
        handler=handler,
        allowed_states=set(ALLOWED_STATES),
    )
