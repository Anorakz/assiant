#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent/core/chat_memory.py — **纯对话记忆**（Phase 7 T9-1）

它是什么
    "用户跟助手说过的那几句话"的一段**有界内存**。每条记下 **谁说的 / 说了什么 /
    什么时候 / 说的时候板子在哪个模式**（idle/study/game/sleep）—— 最后这一项是 T9-2
    判"当前心情"的**场景**依据（你 T9 定的: 心情看"对话表现 + 当时场景"）。

它不是什么（三条边界, 都写死在代码里）
    1. **不是模型的上下文**。`agent/llm/provider.py::_build_messages()` 每轮都是
       `system + 上下文 + 这一句 user 文本`, 板端 LLM **无状态** —— 所以"清空记忆"
       不会让模型的某一轮少看东西。它是我们**自己攒**的一小段, 只为画像服务。
    2. **不落盘、不写任何文件**（你定的）: 进程退出就没了; 画像本身才落盘（T9-2）。
    3. **只收"纯对话"**: `source ∈ {gui, terminal}`。scheduler 的定时提示、测试注入、
       IPC 的系统消息都不算"用户在跟助手说话"。

它被谁用
    · `Runtime.handle_event()` 收用户话时 `add_user()`
    · `Runtime._deliver_reply()` 发回复时 `add_reply()`
    · T9-3 每轮结束问一句 `chars() >= 触发线（2000）` → 命中就构建画像并 `settle()`

⚠ 两个容易搞错的语义（都有测试钉着）
    · **"轮"= 用户说了几条**（`turns()`）—— 助手的回复不算一轮, 它跟着那一轮。
    · `settle()` **不是全清**: 保留最后 `keep_after_settle`(6) 条。构建画像是"把攒下的
      对话结算掉", 而那几句最新的话下一个画像还用得上。
    · ⚠ **触发口径是 `pending_chars()`（自上次构建以来攒的）, 不是 `chars()`（内存里一共多少）**:
      结算后保留的那 6 条**不算**下一次的触发量 —— 否则"保留的尾巴"可能自己就超过触发线,
      于是每轮都满足条件、构建一遍又一遍（T9-3 的接线测试当场抓到过这一幕）。
      `settle()` 把 pending 归零, 计数从 0 重新长。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

__all__ = [
    "CHAT_SOURCES",
    "ROLE_USER",
    "ROLE_ASSISTANT",
    "DEFAULT_MAX_CHARS",
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_KEEP_AFTER_SETTLE",
    "Entry",
    "ChatMemory",
]

#: 算"纯对话"的来源（其它来源不进记忆）—— 与 `Runtime.handle_event` 里的 source 对齐
CHAT_SOURCES = ("gui", "terminal")

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
_ROLES = (ROLE_USER, ROLE_ASSISTANT)

#: 记忆的**上界**（防无限涨）: 8000 字 / 80 条。触发线（2000 字）在 T9-3,
#: 比它小 —— 所以"攒满触发"总是先于"被挤掉"发生（挤掉的话会先丢最老的那几句）。
DEFAULT_MAX_CHARS = 8000
DEFAULT_MAX_ENTRIES = 80

#: `settle()` 之后保留几条（见模块头的说明）
DEFAULT_KEEP_AFTER_SETTLE = 6


@dataclass(frozen=True)
class Entry:
    """记忆里的一条。`state` 是**说这句话的时候**板子的模式（拿不到就是 ""）。"""

    role: str
    text: str
    ts: float
    state: str = ""
    truncated: bool = False        # 这一条太长被截过尾巴（只留最近的部分）

    @property
    def chars(self) -> int:
        return len(self.text)

    def to_dict(self) -> Dict[str, Any]:
        return {"role": self.role, "text": self.text, "ts": self.ts,
                "state": self.state, "truncated": self.truncated}

    def __repr__(self) -> str:
        return "<Entry %s %s %r>" % (self.role, self.state or "-", self.text[:20])


class ChatMemory:
    """纯对话的有界内存（**不落盘**）。"""

    def __init__(
        self,
        max_chars: int = DEFAULT_MAX_CHARS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        keep_after_settle: int = DEFAULT_KEEP_AFTER_SETTLE,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._max_chars = max(1, int(max_chars))
        self._max_entries = max(1, int(max_entries))
        self._keep_after_settle = max(0, int(keep_after_settle))
        self._clock = clock or time.time
        self._entries: List[Entry] = []
        #: 自上次 `settle()` 以来攒了多少（**触发口径** —— 见模块头那段说明）
        self._pending_chars = 0
        self._pending_turns = 0

    # ------------------------------------------------------------ 记 ---
    def add(self, role: str, text: str, source: str = "gui",
            state: str = "") -> Optional[Entry]:
        """记一条。

        @return 记进去的那条；**没记**（来源不是纯对话 / 内容空的）返回 None
        @raise ValueError role 不是 user/assistant（那是调用方写错了, 不该悄悄吞掉）
        @note 单条超过 `max_chars` 时**只留最近的**（前面加 `…`）—— 保持"有界"这条硬性质。
        """
        if role not in _ROLES:
            raise ValueError("role 只能是 %s, 得到 %r" % ("/".join(_ROLES), role))
        if str(source or "") not in CHAT_SOURCES:
            return None
        body = str(text or "").strip()
        if not body:
            return None

        truncated = False
        if len(body) > self._max_chars:
            body = "…" + body[-(self._max_chars - 1):]
            truncated = True

        entry = Entry(role=role, text=body, ts=float(self._clock()),
                      state=str(state or ""), truncated=truncated)
        self._entries.append(entry)
        self._pending_chars += entry.chars
        if role == ROLE_USER:
            self._pending_turns += 1
        self._evict()
        return entry

    def add_user(self, text: str, source: str = "gui", state: str = "") -> Optional[Entry]:
        return self.add(ROLE_USER, text, source=source, state=state)

    def add_reply(self, text: str, source: str = "gui", state: str = "") -> Optional[Entry]:
        return self.add(ROLE_ASSISTANT, text, source=source, state=state)

    # ------------------------------------------------------------ 读 ---
    def entries(self, limit: Optional[int] = None) -> List[Entry]:
        """按时间顺序（老 -> 新）取条目；`limit` 只要最后几条。"""
        if limit is None:
            return list(self._entries)
        return list(self._entries)[-max(0, int(limit)):]

    def chars(self) -> int:
        """现在攒了多少字（user + assistant 合计）—— T9-3 的触发口径。"""
        return sum(entry.chars for entry in self._entries)

    def turns(self) -> int:
        """用户说了几条（助手的回复不算一轮）。"""
        return sum(1 for entry in self._entries if entry.role == ROLE_USER)

    def pending_chars(self) -> int:
        """**自上次 `settle()` 以来**攒了多少字 —— T9-3 的触发口径就是它。"""
        return self._pending_chars

    def pending_turns(self) -> int:
        """自上次 `settle()` 以来用户说了几条（`trigger_turns` 兜底用）。"""
        return self._pending_turns

    def stats(self) -> Dict[str, Any]:
        """给日志/画像记录用的一眼摘要（不落盘, 只是算出来）。"""
        states = [entry.state for entry in self._entries if entry.state]
        return {"chars": self.chars(), "entries": len(self._entries),
                "turns": self.turns(), "states": sorted(set(states)),
                "pending_chars": self._pending_chars,
                "pending_turns": self._pending_turns,
                "since": self._entries[0].ts if self._entries else None,
                "last": self._entries[-1].ts if self._entries else None}

    def excerpt(self, max_chars: int = 800, with_state: bool = True) -> str:
        """给**判心情**用的那段文字（T9-2 会把它交给模型）。

        @param max_chars 最多多少字（**从最新往回取**, 再按时间顺序拼回去 ——
                         预算不够时先丢最老的话, 不是最新的）
        @param with_state True 时每条前面带 `[study]` 这种场景标记
        @return ""（还没攒下东西）
        @note 整行放不下就**不放半行**（除了最新那条: 它自己就超预算时留它的最近一半,
              前面加 `…`）—— 半行老内容对判心情只是噪音。
        """
        budget = max(1, int(max_chars))
        picked: List[str] = []
        used = 0                                  # 已用字数（含行与行之间的换行）
        for entry in reversed(self._entries):
            prefix = "[%s] " % (entry.state or "?") if with_state else ""
            who = "用户: " if entry.role == ROLE_USER else "助手: "
            line = "%s%s%s" % (prefix, who, entry.text)
            gap = 1 if picked else 0
            room = budget - used - gap
            if len(line) <= room:
                picked.append(line)
                used += gap + len(line)
                continue
            if not picked and room > 1:           # 最新这条自己就超预算 -> 留它的最近一半
                picked.append("…" + line[-(room - 1):])
            break
        return "\n".join(reversed(picked))

    # ------------------------------------------------------------ 结算 ---
    def settle(self) -> int:
        """构建完画像后"结算"：只保留最后 `keep_after_settle` 条, 并把**触发计数归零**。

        @return 丢掉了几条（日志用）
        @note **不是全清**（见模块头）: 最新那几句下一个画像还用得上; 但它们**不计入**
              下一次的触发量（`pending_*` 归 0）—— 否则保留的尾巴自己就能顶满触发线。
        """
        keep = self._keep_after_settle
        dropped = max(0, len(self._entries) - keep)
        if dropped:
            self._entries = self._entries[-keep:] if keep else []
        self._pending_chars = 0
        self._pending_turns = 0
        return dropped

    # ------------------------------------------------------------ 内部 ---
    def _evict(self) -> None:
        """把最老的挤出去, 直到满足两个上界（刚加进来的那条一定留得住）。"""
        while self._entries and (len(self._entries) > self._max_entries
                                 or self.chars() > self._max_chars):
            self._entries.pop(0)

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self):
        """按时间顺序遍历（`list(memory)` 能用 —— 画像内核就是直接吃 entries 的）。"""
        return iter(list(self._entries))

    def __repr__(self) -> str:
        return "<ChatMemory %d 条 / %d 字 / %d 轮>" % (len(self._entries), self.chars(),
                                                      self.turns())
