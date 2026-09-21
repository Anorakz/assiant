#!/usr/bin/env python3
"""
tests/test_chat_bus.py — ChatInputBus 单测

运行:
    python tests/test_chat_bus.py

覆盖:
  · 事件格式固定为 {source, text, timestamp} 三字段
  · FIFO 顺序; 多源混投仍按到达顺序
  · get() 空队列阻塞; 有事件立刻返回
  · get_nowait() 空队列返回 None; 有事件返回
  · timestamp 是墙上时间 (time.time 量级)
  · push 返回入队的那条事件
  · 跨事件循环使用被明确拒绝
  · 有界队列的背压
"""

import asyncio
import sys
import time
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.io import ChatInputBus  # noqa: E402
from agent.io.chat_bus import EVENT_FIELDS  # noqa: E402


class TestChatBusFormat(unittest.TestCase):
    """事件格式 (不需要事件循环的部分)。"""

    def test_event_fields_constant(self):
        self.assertEqual(EVENT_FIELDS, ("source", "text", "timestamp"))

    def test_construct_outside_loop_is_allowed(self):
        # 模块级单例/import 期构造不该因为"没有事件循环"而失败
        bus = ChatInputBus()
        self.assertIsNotNone(bus)
        self.assertIn("ChatInputBus", repr(bus))

    def test_repr_before_use(self):
        self.assertIn("size=0", repr(ChatInputBus()))


class TestChatBus(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bus = ChatInputBus()

    # ------------------------------------------------------------- 格式 ---
    async def test_push_returns_event_with_exact_fields(self):
        event = await self.bus.push("gui", "hello")
        self.assertEqual(set(event), {"source", "text", "timestamp"})
        self.assertEqual(event["source"], "gui")
        self.assertEqual(event["text"], "hello")
        self.assertIsInstance(event["timestamp"], float)

    async def test_get_returns_same_event(self):
        pushed = await self.bus.push("terminal", "ls -la")
        got = await self.bus.get()
        self.assertEqual(got, pushed)

    async def test_timestamp_is_wall_clock(self):
        before = time.time()
        event = await self.bus.push("gui", "x")
        after = time.time()
        self.assertGreaterEqual(event["timestamp"], before)
        self.assertLessEqual(event["timestamp"], after)

    async def test_text_is_not_modified(self):
        # bus 不做解析: 换行、Unicode、空串都原样存放
        for text in ("多行\n第二行", "", " 前后空格 ", "emoji 🎮"):
            with self.subTest(text=text):
                event = await self.bus.push("gui", text)
                self.assertEqual((await self.bus.get())["text"], text)
                self.assertEqual(event["text"], text)

    async def test_source_is_opaque(self):
        # 不校验来源: 只要字符串就收 (三个约定值之外的也照收)
        await self.bus.push("whatever", "x")
        self.assertEqual((await self.bus.get())["source"], "whatever")

    # ------------------------------------------------------------- 顺序 ---
    async def test_fifo_order(self):
        for i in range(5):
            await self.bus.push("gui", "m%d" % i)
        got = [(await self.bus.get())["text"] for _ in range(5)]
        self.assertEqual(got, ["m0", "m1", "m2", "m3", "m4"])

    async def test_three_sources_share_one_order(self):
        # 第三个来源名是随便取的: bus 对 source 完全不解释 (曾经这里是
        # "host_keyboard", 那条生产者已在 Phase 6 收尾时删除)。
        await self.bus.push("terminal", "t")
        await self.bus.push("gui", "g")
        await self.bus.push("ipc", "h")
        got = [(await self.bus.get())["source"] for _ in range(3)]
        self.assertEqual(got, ["terminal", "gui", "ipc"])

    # ----------------------------------------------------------- 阻塞 ---
    async def test_get_blocks_until_push(self):
        received = []

        async def consumer():
            received.append(await self.bus.get())

        task = asyncio.create_task(consumer())
        await asyncio.sleep(0.05)
        self.assertEqual(received, [], "还没有事件时 get() 不该返回")

        await self.bus.push("gui", "late")
        await asyncio.wait_for(task, timeout=1.0)
        self.assertEqual(received[0]["text"], "late")

    async def test_multiple_consumers_each_get_one(self):
        consumer_a, consumer_b = [], []

        async def take(sink):
            sink.append(await self.bus.get())

        ta = asyncio.create_task(take(consumer_a))
        tb = asyncio.create_task(take(consumer_b))
        await asyncio.sleep(0)
        await self.bus.push("gui", "one")
        await self.bus.push("gui", "two")
        await asyncio.wait_for(asyncio.gather(ta, tb), timeout=1.0)
        self.assertEqual({consumer_a[0]["text"], consumer_b[0]["text"]}, {"one", "two"})

    # -------------------------------------------------------- get_nowait ---
    async def test_get_nowait_empty_returns_none(self):
        self.assertIsNone(self.bus.get_nowait())

    async def test_get_nowait_returns_event(self):
        await self.bus.push("gui", "now")
        event = self.bus.get_nowait()
        self.assertIsNotNone(event)
        self.assertEqual(event["text"], "now")
        # 取走了就没了
        self.assertIsNone(self.bus.get_nowait())

    async def test_get_nowait_does_not_block(self):
        start = time.monotonic()
        self.bus.get_nowait()
        self.assertLess(time.monotonic() - start, 0.05)

    # ------------------------------------------------------------- 诊断 ---
    async def test_qsize_and_empty(self):
        self.assertTrue(self.bus.empty())
        self.assertEqual(self.bus.qsize(), 0)
        await self.bus.push("gui", "1")
        await self.bus.push("gui", "2")
        self.assertFalse(self.bus.empty())
        self.assertEqual(self.bus.qsize(), 2)
        await self.bus.get()
        self.assertEqual(self.bus.qsize(), 1)

    # ----------------------------------------------------------- 背压 ---
    async def test_maxsize_applies_backpressure(self):
        bus = ChatInputBus(maxsize=1)
        await bus.push("gui", "first")

        # 队列满: 第二次 push 必须等待消费者腾位置
        blocked = asyncio.Event()

        async def producer():
            await bus.push("gui", "second")
            blocked.set()

        task = asyncio.create_task(producer())
        await asyncio.sleep(0.05)
        self.assertFalse(blocked.is_set(), "队列满时 push 应当阻塞")

        self.assertEqual((await bus.get())["text"], "first")
        await asyncio.wait_for(task, timeout=1.0)
        self.assertTrue(blocked.is_set())
        self.assertEqual((await bus.get())["text"], "second")

    # --------------------------------------------------------- 跨 loop ---
    async def test_cross_loop_use_is_rejected(self):
        await self.bus.push("gui", "bound to loop 1")
        await self.bus.get()

        def run_in_new_loop():
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(self.bus.get_nowait())
            finally:
                loop.close()

        with self.assertRaises(RuntimeError):
            run_in_new_loop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
