# ============================================================================
#  agent/io/host_input_reader.py — 主机键盘事件 → Chat Input Bus
#
#  对应的 native 接口 (native/binding.cpp)
#  ---------------------------------------------------------------------------
#      agent_native.host_input_rb.read_all()  -> list[dict]  (取走全部未读事件)
#      agent_native.host_input_rb.read_latest() / size() / capacity() / overruns()
#
#  事件 dict 的字段 (由 binding_utils.h 的 event_to_dict 决定)
#  ---------------------------------------------------------------------------
#      {"type": "key"|"mouse", "modifier": int, "key": int, "x": int, "y": int,
#       "action": "press"|"release", "pressed": bool, "timestamp_ns": int}
#
#  职责边界 (刻意做小)
#  ---------------------------------------------------------------------------
#  · 只做: 轮询 → 把每条事件渲染成一段 text → 投递到 bus
#  · **不做** 键盘事件解析 —— native 返回什么就投什么, 这里只是把结构化 dict
#    展开成可读字符串 (下游要结构化数据的话, 应该另开一个直接读 native 的通道,
#    而不是让 bus 承载语义)
#  · **不做** 快捷键识别 / 组合键语义 —— 那是 scheduler 的事
#  · **不做** 输入合法性校验
#
#  轮询而不是回调
#  ---------------------------------------------------------------------------
#  moonlight 的输入回调跑在它自己的线程上, 直接用回调 + asyncio 需要
#  call_soon_threadsafe 跨线程投递; 而 host_input_rb 本来就是给"消费者主动读"
#  设计的 (有 unread 游标和覆盖统计)。这里选轮询: 逻辑简单、天然在事件循环里、
#  丢事件有 overruns() 可观测。
# ============================================================================

from __future__ import annotations

import asyncio
from typing import Any, List, Optional

from ._native import get_native, run_native

__all__ = ["HostInputReader", "event_to_text", "DEFAULT_INTERVAL_MS"]

#: 归属的子系统名 (决定用哪个专属执行器)
_SUBSYS = "host_input_rb"

#: 默认轮询间隔: 50ms ≈ 20Hz。人打字远达不到这个频率, 足够跟手又不费 CPU。
DEFAULT_INTERVAL_MS = 50

#: 这些键的展示名 (仅用于把事件渲染成可读 text, 不参与任何按键语义判断)
_KEY_NAMES = {
    0x08: "backspace",
    0x09: "tab",
    0x0D: "enter",
    0x1B: "esc",
    0x20: "space",
    0x25: "left",
    0x26: "up",
    0x27: "right",
    0x28: "down",
    0x2E: "delete",
    0x10: "shift",
    0x11: "ctrl",
    0x12: "alt",
}


def _key_label(key: int) -> str:
    """把 VK 码渲染成可读标签。

    先查具名表, 再退回可打印 ASCII —— 顺序很重要: 空格(0x20) 也是"可打印"的,
    但渲染成一个空格会得到 "[key  ]" 这种看不见内容的文本, 不如 "[key space]"。
    """
    name = _KEY_NAMES.get(key)
    if name is not None:
        return name
    if 0x20 <= key < 0x7F:
        return chr(key)
    return "vk:%d" % key


def event_to_text(event: dict) -> str:
    """把一条 host_input_rb 事件渲染成 bus 的 text。

    刻意保持"机械": 不识别组合键, 不合并连续按键 —— 只做一次可读化。
    """
    kind = event.get("type")

    if kind == "key":
        label = _key_label(int(event.get("key", 0)))
        modifier = int(event.get("modifier", 0))
        if modifier:
            return "[key %s modifier=%d]" % (label, modifier)
        return "[key %s]" % label

    if kind == "mouse":
        return "[mouse x=%d y=%d action=%s]" % (
            int(event.get("x", 0)),
            int(event.get("y", 0)),
            event.get("action", "?"),
        )

    # 未知类型: 原样带上, 不丢事件 (丢了更难查)
    return "[%s %r]" % (kind, event)


class HostInputReader:
    """轮询 host_input_rb 并把事件投递到 ChatInputBus。

    典型用法::

        bus = ChatInputBus()
        reader = HostInputReader()
        await reader.start_polling(bus)     # 后台轮询
        ...
        await reader.stop()
    """

    def __init__(self, native: Any = None) -> None:
        """
        @param native 注入的 native 模块 (测试用); None = 按需 import agent_native
        """
        self._native = native
        self._task: Optional[asyncio.Task] = None
        self._bus = None
        self._interval_ms = DEFAULT_INTERVAL_MS
        self._polls = 0

    # ------------------------------------------------------------ 内部 ---
    def _rb(self) -> Any:
        native = self._native if self._native is not None else get_native()
        return native.host_input_rb

    async def _poll_once(self) -> int:
        """取走全部未读事件并投递; 返回投递条数。"""
        events: List[dict] = await run_native(_SUBSYS, self._rb().read_all)
        for event in events:
            await self._bus.push("host_keyboard", event_to_text(event))
        return len(events)

    async def _loop(self) -> None:
        interval = max(0, self._interval_ms) / 1000.0
        while True:
            try:
                await self._poll_once()
                self._polls += 1
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                # 单次轮询失败 (例如 native 在读的瞬间出问题) 不该让整个轮询停掉;
                # 下一轮继续。真正的错误诊断交给 native 层的 status()/日志。
                pass
            await asyncio.sleep(interval)

    # ------------------------------------------------------------ 控制 ---
    async def start_polling(self, bus, interval_ms: int = DEFAULT_INTERVAL_MS) -> None:
        """启动后台轮询。

        @param bus         目标 ChatInputBus
        @param interval_ms 轮询间隔(毫秒); 0 表示不限速 (只在测试里有意义)
        @note 重复调用是**幂等**的: 已经在跑就直接返回, 不会起第二个任务。
        """
        if self.is_running():
            return
        self._bus = bus
        self._interval_ms = interval_ms
        # create_task 而不是 ensure_future: 我们就是要在当前 loop 上起后台任务
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """停止轮询。可重复调用。"""
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------ 诊断 ---
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def polls(self) -> int:
        """已完成的轮询轮数 (诊断用)。"""
        return self._polls
