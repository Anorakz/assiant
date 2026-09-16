# ============================================================================
#  agent/io/chat_bus.py — Chat Input Bus: 三个输入源汇合到一条事件流
#
#  三个数据源
#  ---------------------------------------------------------------------------
#      terminal     : 终端里敲的一行字
#      gui          : PySide6 界面输入框
#      host_keyboard: 主机(被控端)键盘回传的事件  ← host_input_reader 投递
#
#  统一成同一种事件, 下游只看 bus, 不关心来源:
#
#      {"source": str, "text": str, "timestamp": float}
#
#  设计要点
#  ---------------------------------------------------------------------------
#  · 事件格式固定三个字段, **不做**内容解析: text 里放什么由投递方决定
#    (主机键盘事件如何变成 text 是 host_input_reader 的事, 不是 bus 的事)
#  · 每条事件独立取值, 不合并、不去重、不改写
#  · timestamp 用 time.time() (墙上时间, 不是 monotonic) —— 下游可能要拿它
#    和用户看到的日志时间对齐, monotonic 没有可比性
#  · get() 会一直等到有事件为止, 不返回 None; 想非阻塞就用 get_nowait()
#
#  事件循环
#  ---------------------------------------------------------------------------
#  asyncio.Queue 必须绑定到一个事件循环, 而"绑定"发生在第一次使用它的时候。
#  所以这里 **延迟创建** Queue (第一次 push/get 时才建), 这样:
#      · 可以在没有 loop 的时候构造 ChatInputBus (模块级单例、import 期都安全)
#      · 每个实例绑定到它第一次使用时的那个 loop
#  同一个实例跨多个 loop 使用是不支持的 (asyncio 本身的限制), 会明确报错。
# ============================================================================

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional

__all__ = ["ChatInputBus", "EVENT_FIELDS"]

#: 事件字段 —— 顺序即文档
EVENT_FIELDS = ("source", "text", "timestamp")


class ChatInputBus:
    """把多个输入源的事件串成一条 asyncio 队列。

    典型用法::

        bus = ChatInputBus()
        await bus.push("gui", "帮我看看这个")
        ev = await bus.get()        # 阻塞直到有事件
        ev2 = bus.get_nowait()      # 没有就返回 None
    """

    def __init__(self, maxsize: int = 0) -> None:
        """
        @param maxsize 队列上限; 0 = 无上限 (默认)。
                       有上限时 push() 会阻塞等待有空位 —— 用于给上游背压。
        """
        self._maxsize = maxsize
        self._queue: Optional[asyncio.Queue] = None
        self._loop = None

    # ------------------------------------------------------------ 内部 ---
    def _ensure_queue(self) -> asyncio.Queue:
        """取队列, 必要时创建并绑定当前事件循环。"""
        loop = asyncio.get_running_loop()  # 没有 loop 时直接抛, 报错点就在调用处

        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=self._maxsize)
            self._loop = loop
            return self._queue

        if self._loop is not loop:
            raise RuntimeError(
                "ChatInputBus 不能跨事件循环使用 "
                "(已绑定到一个 loop, 现在在另一个 loop 上访问)"
            )
        return self._queue

    # ------------------------------------------------------------ 写入 ---
    async def push(self, source: str, text: str) -> Dict[str, Any]:
        """投递一条事件。

        @param source 来源标记, 约定用 "terminal" / "gui" / "host_keyboard"
        @param text   事件内容原样存放, bus 不做解析
        @return 实际入队的事件 dict (便于调用方拿 timestamp)
        """
        event = {
            "source": source,
            "text": text,
            "timestamp": time.time(),
        }
        await self._ensure_queue().put(event)
        return event

    # ------------------------------------------------------------ 读取 ---
    async def get(self) -> Dict[str, Any]:
        """取一条事件; 队列空时**阻塞等待**。"""
        return await self._ensure_queue().get()

    def get_nowait(self) -> Optional[Dict[str, Any]]:
        """非阻塞取一条事件; 没有就返回 None。

        @note 这是同步方法 (签名要求如此): 空队列时它不等, 立刻返回 None。
        """
        queue = self._ensure_queue()
        try:
            return queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    # ------------------------------------------------------------ 诊断 ---
    def qsize(self) -> int:
        """当前积压条数。需要在事件循环里调用。"""
        return self._ensure_queue().qsize()

    def empty(self) -> bool:
        return self._ensure_queue().empty()

    def __repr__(self) -> str:
        size = self._queue.qsize() if self._queue is not None else 0
        return "<ChatInputBus size=%d maxsize=%d>" % (size, self._maxsize)
