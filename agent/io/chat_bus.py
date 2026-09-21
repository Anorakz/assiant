# ============================================================================
#  agent/io/chat_bus.py — Chat Input Bus: 多个输入源汇合到一条事件流
#
#  数据源
#  ---------------------------------------------------------------------------
#      terminal     : 终端里敲的一行字
#      gui          : PySide6 界面输入框
#      host_keyboard: 预留 —— 曾经设计给"主机(被控端)键盘回传", 但
#                     moonlight-common-c 没有这个 API, 生产者已删除
#                     (Phase 6 收尾)。bus 本身与来源无关, 名字留着不碍事。
#
#  统一成同一种事件, 下游只看 bus, 不关心来源:
#
#      {"source": str, "text": str, "timestamp": float}
#
#  设计要点
#  ---------------------------------------------------------------------------
#  · 事件格式固定三个字段, **不做**内容解析: text 里放什么由投递方决定
#    (按键事件如何变成 text 是投递方的事, 不是 bus 的事)
#  · 每条事件独立取值, 不合并、不去重、不改写
#  · timestamp 用 time.time() (墙上时间, 不是 monotonic) —— 下游可能要拿它
#    和用户看到的日志时间对齐, monotonic 没有可比性
#  · get() 会一直等到有事件为止, 不返回 None; 想非阻塞就用 get_nowait()
#
#  订阅 (subscribe): 只看不取
#  ---------------------------------------------------------------------------
#  bus 是**单一消费者**的队列: 一条事件被 get() 取走就没了。但除了"主力消费者"
#  (agent core), 还有只想**旁观**的角色 —— 典型是 scheduler 要从主机键盘事件里
#  识别快捷键。如果让它 get() 来读, 它会:
#      · 把终端/GUI 的用户消息一并吃掉, 下游再也看不到
#      · 吃掉自己 push 出去的触发消息
#  所以这里提供 subscribe(): 注册的回调在 push 时被通知, 事件**仍然留在队列里**
#  等下游取。订阅者只观察, 不消费。
#
#  ⚠ 订阅回调绝不能阻塞: 它是在 push() 的调用栈上被调用的, 而 push() 又可能
#    被后台轮询协程调用。所以回调里有 await 的部分不会在这里
#    等待, 而是挂一个后台任务 (见 _notify_subscribers)。
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
import inspect
import time
from typing import Any, Callable, Dict, List, Optional

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
        self._subscribers: List[Callable[[Dict[str, Any]], Any]] = []

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

        @param source 来源标记, 约定用 "terminal" / "gui"
                      ("host_keyboard" 是预留名字, 当前无生产者)
        @param text   事件内容原样存放, bus 不做解析
        @return 实际入队的事件 dict (便于调用方拿 timestamp)

        @note 订阅者 (subscribe) 在**入队之前**被通知, 但它们不消费事件 ——
              事件仍然留在队列里等 get()/get_nowait()。
        """
        event = {
            "source": source,
            "text": text,
            "timestamp": time.time(),
        }
        self._notify_subscribers(event)
        await self._ensure_queue().put(event)
        return event

    # ------------------------------------------------------------ 订阅 ---
    def subscribe(self, callback: Callable[[Dict[str, Any]], Any]) -> Callable[[], bool]:
        """注册一个"只看不取"的观察者。

        @param callback 收到 event dict 的可调用对象; 可以是普通函数, 也可以是
                        协程函数 (协程会被挂成后台任务, 不阻塞 push)
        @return 一个取消订阅的可调用对象 (调用它即注销)
        @note 回调抛出的异常会被吞掉并打印 —— 一个旁观者出错不该影响 push 的
              调用方, 更不该影响队列里的事件。
        """
        if not callable(callback):
            raise TypeError("callback must be callable")
        self._subscribers.append(callback)

        def unsubscribe() -> bool:
            try:
                self._subscribers.remove(callback)
                return True
            except ValueError:
                return False

        return unsubscribe

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def _notify_subscribers(self, event: Dict[str, Any]) -> None:
        if not self._subscribers:
            return
        for callback in list(self._subscribers):
            try:
                result = callback(event)
            except Exception as exc:  # noqa: BLE001
                print("ChatInputBus: subscriber %r raised: %r" % (callback, exc))
                continue

            if inspect.isawaitable(result):
                # 不能 await (我们是在 push 的调用栈上), 挂后台任务。
                # 回调内部的异常交给任务自己的 done 回调处理。
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    # 没有事件循环: 无法调度, 明确说明而不是静默丢弃
                    print(
                        "ChatInputBus: subscriber %r returned an awaitable but there is "
                        "no running event loop; the subscription was skipped" % (callback,)
                    )
                    continue
                task = loop.create_task(result)
                task.add_done_callback(_log_task_exception)

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
        return "<ChatInputBus size=%d maxsize=%d subscribers=%d>" % (
            size, self._maxsize, len(self._subscribers)
        )


def _log_task_exception(task: "asyncio.Task") -> None:
    """吃掉订阅者协程的异常并打印 (没有它会有 "Task exception was never retrieved")。"""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        print("ChatInputBus: subscriber task raised: %r" % (exc,))
