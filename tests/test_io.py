#!/usr/bin/env python3
"""
tests/test_io.py — ImageReader / HostInputReader / InputSender 单测

运行:
    python tests/test_io.py

全部用 tests/mocks/mock_agent_native.py 注入替身, 不需要交叉编译的 .so。

覆盖:
  ImageReader
    · read_latest / read_by_timestamp 转发给 native 并原样返回
    · 没有帧时返回 None (不是异常、不是空数组)
    · size / capacity / overruns
    · native 调用发生在**同一个**线程 (SPSC 约束)
  HostInputReader
    · 轮询把事件投递到 bus, source == "host_keyboard"
    · 不丢事件: read_all 一次取走多条, 全部投递且保序
    · start_polling 幂等; stop 可重复调用
    · 停止后不再投递
    · 单次轮询抛异常不会让轮询整体停掉
    · 事件文本渲染 (key / mouse / 未知类型 / 修饰键)
  InputSender
    · send_key 的字符串 -> VK 归一 (单字符 ord, 整数透传)
    · modifier 归一 ("ctrl" / ["ctrl","alt"] / int / None)
    · action 小写归一
    · send_hotkey / send_mouse 转发
    · 所有 native 调用都在执行器线程上 (不占用事件循环线程)
"""

import asyncio
import sys
import threading
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.io import (  # noqa: E402
    ChatInputBus,
    HostInputReader,
    ImageReader,
    InputSender,
    event_to_text,
    resolve_key,
    resolve_modifier,
)
from agent.io import _native as native_mod  # noqa: E402
from tests.mocks.mock_agent_native import MockNative  # noqa: E402


class _IoTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.native = MockNative()
        # 记录每个 native 调用发生在哪个线程, 用来验证"不占用事件循环线程"
        self._call_threads = []

    async def asyncTearDown(self):
        native_mod.reset_executors()


# ===========================================================================
#  ImageReader
# ===========================================================================
class TestImageReader(_IoTestBase):
    async def test_read_latest_returns_none_when_no_frames(self):
        reader = ImageReader(native=self.native)
        self.assertIsNone(await reader.read_latest())

    async def test_read_latest_returns_frame(self):
        reader = ImageReader(native=self.native)
        pushed = self.native.push_image(ts=1000)
        frame = await reader.read_latest()
        self.assertIs(frame, pushed, "应当原样返回 native 给的对象")

    async def test_read_latest_shape_when_numpy_available(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("没有 numpy")
        reader = ImageReader(native=self.native)
        self.native.push_image(ts=1)
        frame = await reader.read_latest()
        self.assertIsInstance(frame, np.ndarray)
        self.assertEqual(frame.shape, (256, 256, 3))
        self.assertEqual(frame.dtype, np.uint8)

    async def test_read_latest_is_passed_through_repeatedly(self):
        # native 的 read_latest 是非破坏性的, 本层不去重 (去重策略属于上层)
        reader = ImageReader(native=self.native)
        pushed = self.native.push_image(ts=1)
        self.assertIs(await reader.read_latest(), pushed)
        self.assertIs(await reader.read_latest(), pushed)

    async def test_read_by_timestamp_picks_not_later_than_ts(self):
        reader = ImageReader(native=self.native)
        self.native.push_image(ts=1000)
        second = self.native.push_image(ts=2000)
        self.native.push_image(ts=3000)

        self.assertIs(await reader.read_by_timestamp(2500), second)
        self.assertIsNone(await reader.read_by_timestamp(500), "早于所有帧 -> None")

    async def test_read_by_timestamp_forwards_ts(self):
        reader = ImageReader(native=self.native)
        await reader.read_by_timestamp(123456)
        self.assertEqual(self.native.last_call("image_rb.read_by_timestamp"),
                         ("image_rb.read_by_timestamp", 123456))

    async def test_diagnostics(self):
        reader = ImageReader(native=self.native)
        self.native.push_image(ts=1)
        self.native.push_image(ts=2)
        self.native.image_overruns = 7
        self.assertEqual(await reader.size(), 2)
        self.assertEqual(await reader.capacity(), 300)
        self.assertEqual(await reader.overruns(), 7)

    async def test_native_calls_happen_off_the_event_loop_thread(self):
        reader = ImageReader(native=self.native)
        self.native.push_image(ts=1)
        loop_thread = threading.get_ident()

        seen = []
        original = self.native.image_rb.read_latest

        def spy():
            seen.append(threading.get_ident())
            return original()

        self.native.image_rb.read_latest = spy
        await reader.read_latest()

        self.assertTrue(seen, "应当调用到 native")
        self.assertNotEqual(seen[0], loop_thread, "native 调用不该在事件循环线程上")

    async def test_all_reads_use_one_thread(self):
        """SPSC: 同一 reader 的多次读取必须落在同一个线程上。"""
        reader = ImageReader(native=self.native)
        self.native.push_image(ts=1)
        seen = []
        original = self.native.image_rb.read_latest

        def spy():
            seen.append(threading.get_ident())
            return original()

        self.native.image_rb.read_latest = spy
        for _ in range(5):
            await reader.read_latest()

        self.assertEqual(len(set(seen)), 1, "多次读取必须同线程, 实际 %s" % set(seen))

    async def test_event_loop_not_blocked(self):
        reader = ImageReader(native=self.native)
        self.native.push_image(ts=1)
        ticks = []

        async def ticker():
            for _ in range(20):
                ticks.append(1)
                await asyncio.sleep(0.001)

        await asyncio.gather(ticker(), reader.read_latest())
        self.assertGreater(len(ticks), 0, "读帧期间事件循环应当还能跑别的协程")


# ===========================================================================
#  HostInputReader
# ===========================================================================
class TestHostInputReader(_IoTestBase):
    async def asyncSetUp(self):
        self.bus = ChatInputBus()

    async def test_no_events_means_no_bus_events(self):
        reader = HostInputReader(native=self.native)
        await reader.start_polling(self.bus, interval_ms=1)
        await asyncio.sleep(0.05)
        await reader.stop()
        self.assertIsNone(self.bus.get_nowait())

    async def test_delivers_host_events_to_bus(self):
        self.native.push_host_event(key=ord("A"))
        reader = HostInputReader(native=self.native)
        await reader.start_polling(self.bus, interval_ms=1)

        event = await asyncio.wait_for(self.bus.get(), timeout=1.0)
        await reader.stop()

        self.assertEqual(event["source"], "host_keyboard")
        self.assertEqual(event["text"], "[key A]")
        self.assertIsInstance(event["timestamp"], float)

    async def test_delivers_all_events_in_order(self):
        for ch in ("h", "i"):
            self.native.push_host_event(key=ord(ch))
        reader = HostInputReader(native=self.native)
        await reader.start_polling(self.bus, interval_ms=1)

        texts = []
        for _ in range(2):
            texts.append((await asyncio.wait_for(self.bus.get(), timeout=1.0))["text"])
        await reader.stop()

        self.assertEqual(texts, ["[key h]", "[key i]"], "不丢事件且保持顺序")

    async def test_multiple_events_delivered_in_one_poll(self):
        for i in range(5):
            self.native.push_host_event(key=ord("a") + i)
        reader = HostInputReader(native=self.native)
        await reader.start_polling(self.bus, interval_ms=50)

        texts = []
        for _ in range(5):
            texts.append((await asyncio.wait_for(self.bus.get(), timeout=1.0))["text"])
        await reader.stop()
        self.assertEqual(len(texts), 5)

    async def test_start_polling_is_idempotent(self):
        reader = HostInputReader(native=self.native)
        await reader.start_polling(self.bus, interval_ms=1)
        first_task = reader._task
        await reader.start_polling(self.bus, interval_ms=1)
        self.assertIs(reader._task, first_task, "重复启动不该起第二个任务")
        await reader.stop()

    async def test_is_running_reflects_state(self):
        reader = HostInputReader(native=self.native)
        self.assertFalse(reader.is_running())
        await reader.start_polling(self.bus, interval_ms=1)
        self.assertTrue(reader.is_running())
        await reader.stop()
        self.assertFalse(reader.is_running())

    async def test_stop_is_idempotent(self):
        reader = HostInputReader(native=self.native)
        await reader.stop()  # 没启动过
        await reader.start_polling(self.bus, interval_ms=1)
        await reader.stop()
        await reader.stop()  # 重复停

    async def test_stop_halts_delivery(self):
        reader = HostInputReader(native=self.native)
        await reader.start_polling(self.bus, interval_ms=1)
        await reader.stop()

        self.native.push_host_event(key=ord("Z"))
        await asyncio.sleep(0.05)
        self.assertIsNone(self.bus.get_nowait(), "停止后不该再投递")

    async def test_poll_error_does_not_kill_the_loop(self):
        calls = {"n": 0}
        original = self.native.host_input_rb.read_all

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return original()

        self.native.host_input_rb.read_all = flaky
        reader = HostInputReader(native=self.native)
        await reader.start_polling(self.bus, interval_ms=1)

        self.native.push_host_event(key=ord("K"))
        event = await asyncio.wait_for(self.bus.get(), timeout=1.0)
        await reader.stop()

        self.assertEqual(event["text"], "[key K]", "一次轮询失败后应继续轮询")
        self.assertGreater(calls["n"], 1)

    async def test_poll_counter(self):
        reader = HostInputReader(native=self.native)
        await reader.start_polling(self.bus, interval_ms=1)
        await asyncio.sleep(0.05)
        await reader.stop()
        self.assertGreater(reader.polls, 0)

    async def test_native_poll_runs_off_event_loop_thread(self):
        loop_thread = threading.get_ident()
        seen = []
        original = self.native.host_input_rb.read_all

        def spy():
            seen.append(threading.get_ident())
            return original()

        self.native.host_input_rb.read_all = spy
        reader = HostInputReader(native=self.native)
        await reader.start_polling(self.bus, interval_ms=1)
        await asyncio.sleep(0.05)
        await reader.stop()

        self.assertTrue(seen)
        self.assertTrue(all(t != loop_thread for t in seen))


class TestEventToText(unittest.TestCase):
    """事件 -> bus text 的渲染 (纯函数, 不需要事件循环)。"""

    def test_key_printable(self):
        self.assertEqual(
            event_to_text({"type": "key", "key": ord("A"), "modifier": 0}), "[key A]"
        )

    def test_key_named(self):
        self.assertEqual(event_to_text({"type": "key", "key": 0x0D}), "[key enter]")
        self.assertEqual(event_to_text({"type": "key", "key": 0x20}), "[key space]")

    def test_key_unknown_vk(self):
        self.assertEqual(event_to_text({"type": "key", "key": 0xFE}), "[key vk:254]")

    def test_key_with_modifier(self):
        text = event_to_text({"type": "key", "key": ord("C"), "modifier": 2})
        self.assertEqual(text, "[key C modifier=2]")

    def test_mouse(self):
        text = event_to_text(
            {"type": "mouse", "x": 10, "y": 20, "action": "release"}
        )
        self.assertEqual(text, "[mouse x=10 y=20 action=release]")

    def test_unknown_type_is_not_dropped(self):
        text = event_to_text({"type": "gamepad", "button": 3})
        self.assertIn("gamepad", text)

    def test_unicode_key_label(self):
        # 可打印范围外的键码不该抛异常
        self.assertTrue(event_to_text({"type": "key", "key": 0x4E2D}).startswith("[key"))


# ===========================================================================
#  InputSender
# ===========================================================================
class TestResolveHelpers(unittest.TestCase):
    def test_resolve_key_single_char(self):
        self.assertEqual(resolve_key("A"), 65)
        self.assertEqual(resolve_key("a"), 65)   # 小写字母 -> 大写 VK (见下)
        self.assertEqual(resolve_key(" "), 32)

    def test_resolve_key_lowercase_letters_map_to_the_uppercase_vk(self):
        # Windows 的字母 VK 是大写 ASCII。这里以前断言的是 ord("a") == 97,
        # 而 0x61 其实是 VK_NUMPAD1 —— 于是 send_key("ctrl","c") 会发成
        # Ctrl+数字键盘3。2026-09-21 查 C2(锁屏)时发现, 一并修正断言。
        for lower, upper in (("a", "A"), ("c", "C"), ("l", "L"), ("s", "S"), ("z", "Z")):
            self.assertEqual(resolve_key(lower), ord(upper), "小写 %r" % lower)
            self.assertEqual(resolve_key(lower), resolve_key(upper))
        # 数字与标点不受影响 (它们本来就不是大写问题)
        self.assertEqual(resolve_key("7"), 0x37)
        self.assertEqual(resolve_key("/"), 0x2F)

    def test_resolve_key_int_passthrough(self):
        self.assertEqual(resolve_key(65), 65)
        self.assertEqual(resolve_key(0x0D), 13)

    def test_resolve_key_named(self):
        # 具名键 -> VK 码 (而不是把 "enter" 这样的字符串丢给 native 的 uint32)
        self.assertEqual(resolve_key("enter"), 0x0D)
        self.assertEqual(resolve_key("ENTER"), 0x0D)
        self.assertEqual(resolve_key(" esc "), 0x1B)
        self.assertEqual(resolve_key("f1"), 0x70)
        self.assertEqual(resolve_key("f24"), 0x87)
        self.assertEqual(resolve_key("pageup"), 0x21)

    def test_resolve_key_unknown_name_passthrough(self):
        # 不做校验: 认不出来的原样往下传, 由 native 报错
        self.assertEqual(resolve_key("banana"), "banana")

    def test_resolve_modifier_none(self):
        self.assertEqual(resolve_modifier(None), 0)

    def test_resolve_modifier_int_is_bitmask(self):
        self.assertEqual(resolve_modifier(3), 3)
        self.assertEqual(resolve_modifier(0x02), 0x02)

    def test_resolve_modifier_names_to_bitmask(self):
        self.assertEqual(resolve_modifier("ctrl"), 0x02)
        self.assertEqual(resolve_modifier("CTRL"), 0x02)
        self.assertEqual(resolve_modifier("shift"), 0x01)
        self.assertEqual(resolve_modifier("alt"), 0x04)
        self.assertEqual(resolve_modifier("win"), 0x08)

    def test_resolve_modifier_single_char_is_ord(self):
        # 容错: 有人会直接传 "\x02"
        self.assertEqual(resolve_modifier("\x02"), 2)

    def test_resolve_modifier_sequence_or_ed(self):
        self.assertEqual(resolve_modifier(["ctrl", "alt"]), 0x02 | 0x04)
        self.assertEqual(resolve_modifier(("ctrl", "shift")), 0x02 | 0x01)
        self.assertEqual(resolve_modifier({"ctrl"}), 0x02)

    def test_resolve_modifier_empty_sequence(self):
        self.assertEqual(resolve_modifier([]), 0)

    def test_resolve_modifier_unknown_name_passthrough(self):
        self.assertEqual(resolve_modifier("banana"), "banana")


class TestInputSender(_IoTestBase):
    async def test_send_key_single_char(self):
        sender = InputSender(native=self.native)
        await sender.send_key("ctrl", "C", "down")
        # "ctrl" -> 修饰键位掩码 0x02; "C" -> VK 67
        self.assertEqual(self.native.last_call("send_key"), ("send_key", 0x02, 67, "down"))

    async def test_send_key_lowercase_letter_reaches_native_as_the_uppercase_vk(self):
        # 生产里 LLM/工具层更可能给小写, 这条守住"到 native 手上必须是大写 VK"
        sender = InputSender(native=self.native)
        await sender.send_key("ctrl", "c", "down")
        self.assertEqual(self.native.last_call("send_key"), ("send_key", 0x02, 67, "down"))

    async def test_send_key_never_passes_a_string_modifier_to_native(self):
        # 回归保护: native 的 modifier 是 uint32, 传字符串会在板上报类型错
        sender = InputSender(native=self.native)
        await sender.send_key("ctrl", "A", "down")
        modifier = self.native.last_call("send_key")[1]
        self.assertIsInstance(modifier, int)

    async def test_send_key_action_is_lowercased(self):
        sender = InputSender(native=self.native)
        await sender.send_key(None, "A", "DOWN")
        self.assertEqual(self.native.last_call("send_key")[3], "down")
        await sender.send_key(None, "A", " Release ")
        self.assertEqual(self.native.last_call("send_key")[3], "release")

    async def test_send_key_int_modifier_passthrough(self):
        sender = InputSender(native=self.native)
        await sender.send_key(2, "A", "down")
        self.assertEqual(self.native.last_call("send_key"), ("send_key", 2, 65, "down"))

    async def test_send_key_int_key_passthrough(self):
        sender = InputSender(native=self.native)
        await sender.send_key(None, 0x0D, "press")
        self.assertEqual(self.native.last_call("send_key"), ("send_key", 0, 13, "press"))

    async def test_send_key_named_key(self):
        sender = InputSender(native=self.native)
        await sender.send_key(None, "enter", "down")
        self.assertEqual(self.native.last_call("send_key"), ("send_key", 0, 0x0D, "down"))

    async def test_send_key_modifier_sequence(self):
        sender = InputSender(native=self.native)
        await sender.send_key(["ctrl", "shift"], "A", "down")
        self.assertEqual(self.native.last_call("send_key")[1], 0x02 | 0x01)

    async def test_send_hotkey(self):
        sender = InputSender(native=self.native)
        await sender.send_hotkey(["ctrl", "alt", "S"])
        # hotkey 要的是**修饰键的 VK 码** (Ctrl=0x11, Alt=0x12), 不是位掩码
        self.assertEqual(
            self.native.last_call("send_hotkey"),
            ("send_hotkey", [0x11, 0x12, 83]),
        )

    async def test_send_hotkey_named_and_single_char(self):
        sender = InputSender(native=self.native)
        await sender.send_hotkey(["ctrl", "enter"])
        self.assertEqual(self.native.last_call("send_hotkey"),
                         ("send_hotkey", [0x11, 0x0D]))

    async def test_send_hotkey_empty(self):
        sender = InputSender(native=self.native)
        await sender.send_hotkey([])
        self.assertEqual(self.native.last_call("send_hotkey"), ("send_hotkey", []))

    async def test_send_mouse(self):
        sender = InputSender(native=self.native)
        await sender.send_mouse(10, 20, "left")
        self.assertEqual(self.native.last_call("send_mouse"), ("send_mouse", 10, 20, "left"))

    async def test_send_mouse_action_none_and_upper(self):
        sender = InputSender(native=self.native)
        await sender.send_mouse(1, 2, None)
        self.assertEqual(self.native.last_call("send_mouse"), ("send_mouse", 1, 2, None))
        await sender.send_mouse(1, 2, "MOVE")
        self.assertEqual(self.native.last_call("send_mouse")[3], "move")

    async def test_send_mouse_coerces_coords(self):
        sender = InputSender(native=self.native)
        await sender.send_mouse("5", "6", None)  # type: ignore[arg-type]
        self.assertEqual(self.native.last_call("send_mouse"), ("send_mouse", 5, 6, None))

    async def test_calls_happen_off_event_loop_thread(self):
        sender = InputSender(native=self.native)
        loop_thread = threading.get_ident()
        seen = []

        def spy(modifier, key, action):
            seen.append(threading.get_ident())

        self.native.send_key = spy
        await sender.send_key(None, "A", "down")
        self.assertTrue(seen)
        self.assertNotEqual(seen[0], loop_thread, "native 调用不该在事件循环线程上")

    async def test_many_sends_do_not_starve_loop(self):
        sender = InputSender(native=self.native)
        ticks = []

        async def ticker():
            for _ in range(50):
                ticks.append(1)
                await asyncio.sleep(0)

        await asyncio.gather(ticker(), sender.send_hotkey(["ctrl", "S"]))
        self.assertGreater(len(ticks), 0)


# ===========================================================================
#  端到端: 三源 -> bus -> 消费者 -> 回发输入
# ===========================================================================
class TestPipelineEndToEnd(_IoTestBase):
    """把四个组件串起来跑一遍, 确认它们能协同工作。"""

    async def test_three_sources_to_bus_to_sender(self):
        bus = ChatInputBus()
        reader = HostInputReader(native=self.native)
        sender = InputSender(native=self.native)

        # 主机键盘侧: 预先塞两条, 由轮询投递
        self.native.push_host_event(key=ord("h"))
        self.native.push_host_event(key=ord("i"))
        await reader.start_polling(bus, interval_ms=1)

        # 终端 + GUI 侧: 直接投递
        await bus.push("terminal", "go home")
        await bus.push("gui", "看一下截图")

        # 消费者: 收满 4 条
        consumed = []
        for _ in range(4):
            consumed.append(await asyncio.wait_for(bus.get(), timeout=2.0))
        await reader.stop()

        sources = sorted(e["source"] for e in consumed)
        self.assertEqual(
            sources, ["gui", "host_keyboard", "host_keyboard", "terminal"]
        )
        self.assertTrue(all(isinstance(e["timestamp"], float) for e in consumed))

        # 消费者把结果回发给主机
        for event in consumed:
            await sender.send_key("ctrl", "A", "down")
            await sender.send_key("ctrl", "A", "up")
        self.assertEqual(len(self.native.calls_named("send_key")), 8)
        self.assertEqual(
            self.native.calls_named("send_key")[0], ("send_key", 0x02, 65, "down")
        )

    async def test_image_and_input_run_concurrently_without_blocking(self):
        """图像读取与输入轮询同时跑, 事件循环不被阻塞。"""
        bus = ChatInputBus()
        image = ImageReader(native=self.native)
        reader = HostInputReader(native=self.native)
        self.native.push_image(ts=1)
        self.native.push_host_event(key=ord("x"))

        ticks = []

        async def ticker():
            for _ in range(30):
                ticks.append(1)
                await asyncio.sleep(0.001)

        await reader.start_polling(bus, interval_ms=1)
        try:
            frame, event, _ = await asyncio.gather(
                image.read_latest(),
                asyncio.wait_for(bus.get(), timeout=2.0),
                ticker(),
            )
        finally:
            await reader.stop()

        self.assertIsNotNone(frame)
        self.assertEqual(event["text"], "[key x]")
        self.assertGreater(len(ticks), 0, "并发期间事件循环应当仍在跑")


# ===========================================================================
#  native 解析 (缺 .so 时的报错)
# ===========================================================================
class TestNativeResolution(unittest.TestCase):
    def setUp(self):
        native_mod.reset_native()

    def tearDown(self):
        native_mod.reset_native()

    def test_injected_mock_is_used(self):
        mock = MockNative()
        native_mod.set_native(mock)
        self.assertIs(native_mod.get_native(), mock)

    def test_reset_removes_injection(self):
        native_mod.set_native(MockNative())
        native_mod.reset_native()
        # 恢复到"按需 import"; 宿主机上没有 .so, 所以应当报带提示的 ImportError
        try:
            native_mod.get_native()
        except ImportError as exc:
            self.assertIn("agent_native", str(exc))
            self.assertIn("set_native", str(exc))
        # 某些机器上确实装了 .so, 那就不该抛 —— 两种都算通过

    def test_get_native_is_not_called_at_import_time(self):
        # import agent.io 本身不该需要 .so (否则宿主机根本 import 不了)
        import importlib

        for name in ("agent.io", "agent.io.chat_bus", "agent.io.image_reader",
                     "agent.io.input_sender", "agent.io.host_input_reader"):
            with self.subTest(module=name):
                importlib.import_module(name)
        self.assertIsNotNone(native_mod)


if __name__ == "__main__":
    unittest.main(verbosity=2)
