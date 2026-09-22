#!/usr/bin/env python3
"""
tests/test_ipc_local_server.py — agent/ipc/local_server.py 单测

运行:
    python tests/test_ipc_local_server.py

分两层, 因为 **Windows 的 CPython 根本没有 socket.AF_UNIX** (连
asyncio.start_unix_server 都不导出):

  TestClientSession*   收发逻辑 (_ClientSession)。只依赖 asyncio 的 reader/writer
                       接口, 用假 stream 驱动 —— 所以在 Windows / WSL / 板端
                       都能跑, 覆盖切帧、坏消息丢弃、发送队列背压。
  TestLocalServerSocket 真 socket 端到端 (bind/accept/push/命令/残留文件)。
                       只在 POSIX 上跑, Windows 上整类 skip。

跳过的那一类在 test-python.ps1 的输出里会显示成 skipped, 不会假装通过。

覆盖:
  常量/平台    UNIX_SOCKET_SUPPORTED、默认路径与队列长度
  模式转换     mode_to_wire / mode_from_wire 的大小写与非法值
  构造        默认 path、repr、on_command 注册与清空
  生命周期    start 幂等 / stop 幂等 / 没启动就 push
  切帧        一条/多条/跨 chunk/\r\n/空行/超长行
  错误容忍    坏 JSON、非 object、缺 timestamp 一律丢弃但连接继续
  背压        队列满丢最旧、不踢客户端、push 不阻塞
  真 socket   权限 0600、广播、命令回调、残留 socket、活实例不被抢、
              路径不是 socket 时拒绝删除、父目录自动创建、stop 删文件
"""

import asyncio
import atexit
import contextlib
import json
import logging
import os
import shutil
import socket
import stat
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.core.state_machine import State, StateMachine  # noqa: E402
from agent.ipc import (  # noqa: E402
    NO_SCHEDULER_NOTE,
    SCHEDULE_KIND_FIRED,
    SCHEDULE_KIND_STATE,
    build_ipc,
)
from agent.ipc.local_server import (  # noqa: E402
    DEFAULT_QUEUE_SIZE,
    UNIX_SOCKET_SUPPORTED,
    IpcServerError,
    LocalServer,
    NullServer,
    _ClientSession,
    mode_from_wire,
    mode_to_wire,
)
from agent.ipc.protocol import (  # noqa: E402
    COMMAND_CHAT_INPUT,
    COMMAND_NEXT_BILIBILI,
    COMMAND_NEXT_WALLPAPER,
    COMMAND_QUERY_SCHEDULE,
    COMMAND_SWITCH_MODE,
    MAX_LINE_BYTES,
    SOCKET_PATH,
    TOPIC_LLM,
    TOPIC_SCHEDULE,
    TOPIC_STATUS,
    IpcProtocolError,
    decode,
    encode,
    encode_command,
)

# 测试里不关心日志内容, 但会**故意**制造一堆 WARNING (坏消息/超长行/慢客户端)
# —— 关掉它们, 否则 unittest 的输出会被这些预期内的告警淹没
logging.getLogger("agent.ipc").setLevel(logging.CRITICAL)
logging.getLogger("agent.ipc.local_server").setLevel(logging.CRITICAL)
logging.getLogger("agent.test.ipc").setLevel(logging.CRITICAL)


# ===========================================================================
#  测试替身 / 工具
# ===========================================================================
class _RecordingWriter:
    """最小的 StreamWriter 替身。

    _ClientSession 只用 write / drain / close / wait_closed 四个方法, 所以不需要
    真的 transport —— 这也是它能脱离 AF_UNIX 被单测的原因。

    @param blocking True 时 drain() 会卡在 gate 上, 用来把"客户端消费不过来"
                    这个状态**确定性地**造出来 (否则要靠塞满 socket 缓冲区, 很飘)。
    """

    def __init__(self, blocking: bool = False) -> None:
        self.written = bytearray()
        self.closed = False
        self.drain_calls = 0
        self._gate = asyncio.Event()
        if not blocking:
            self._gate.set()

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        self.drain_calls += 1
        await self._gate.wait()

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    def release(self) -> None:
        self._gate.set()


def _short() -> bytes:
    """一条**很短**的合法命令 (约 45 字节)。

    超长行用例把上限压到 64~96 字节, 而一条真的 chat_input 光信封就 70+ 字节
    —— 用它当"超长行后面的好消息"会连带被丢, 那是用例自己的错。所以这里用
    空的 payload + 短 action。
    """
    return encode_command("ping", {})


async def _wait_until(predicate, timeout: float = 3.0, what: str = "条件") -> None:
    """自旋等某个状态成立 (异步测试里等 accept / 等回调都靠它)。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("%s 在 %.1fs 内没有成立" % (what, timeout))


def _command(action: str = COMMAND_CHAT_INPUT, **payload) -> bytes:
    """一条完整的 GUI 命令线: {"action", "payload"} (命令方向**没有** timestamp)。"""
    return encode_command(action, payload or {"text": "hi"})


#: build_ipc 的出方向用例各自要一个独立 socket 路径。
#: **绝不碰 /tmp/agent.sock** —— 板端跑测试时真 Agent 可能正占着它。
_BUILD_TMP = tempfile.mkdtemp(prefix="ipc-build-")
atexit.register(shutil.rmtree, _BUILD_TMP, ignore_errors=True)
_BUILD_SEQ = [0]


def _tmp_socket_path() -> str:
    _BUILD_SEQ[0] += 1
    return os.path.join(_BUILD_TMP, "agent-%d.sock" % _BUILD_SEQ[0])


# ===========================================================================
#  平台 / 常量
# ===========================================================================
class TestPlatformAndDefaults(unittest.TestCase):
    def test_unix_socket_supported_matches_platform(self):
        expected = hasattr(socket, "AF_UNIX") and hasattr(asyncio, "start_unix_server")
        self.assertEqual(UNIX_SOCKET_SUPPORTED, expected)
        if sys.platform == "win32":
            # 这一条是给"以后有人想在 Windows 上跑 Agent"留的说明
            self.assertFalse(UNIX_SOCKET_SUPPORTED)

    def test_default_path_is_protocol_path(self):
        self.assertEqual(LocalServer().path, SOCKET_PATH)
        self.assertEqual(SOCKET_PATH, "/tmp/agent.sock")
        self.assertEqual(LocalServer("/tmp/other.sock").path, "/tmp/other.sock")

    def test_default_queue_size_is_positive(self):
        self.assertGreater(DEFAULT_QUEUE_SIZE, 0)

    def test_repr(self):
        server = LocalServer("/tmp/x.sock")
        text = repr(server)
        self.assertIn("/tmp/x.sock", text)
        self.assertIn("running=False", text)
        self.assertIn("clients=0", text)


# ===========================================================================
#  模式名大小写转换
# ===========================================================================
class TestModeConversion(unittest.TestCase):
    def test_to_wire_from_state_enum(self):
        # StateMachine.State.value 是小写, IPC 上必须是全大写
        self.assertEqual(mode_to_wire(State.SLEEP), "SLEEP")
        self.assertEqual(mode_to_wire(State.IDLE), "IDLE")
        self.assertEqual(mode_to_wire(State.STUDY), "STUDY")
        self.assertEqual(mode_to_wire(State.GAME), "GAME")

    def test_to_wire_from_string(self):
        self.assertEqual(mode_to_wire("study"), "STUDY")
        self.assertEqual(mode_to_wire("STUDY"), "STUDY")
        self.assertEqual(mode_to_wire("  game  "), "GAME")

    def test_to_wire_rejects_bad_values(self):
        for bad in ("banana", "", "  ", 123, None, ["study"]):
            with self.assertRaises(IpcProtocolError):
                mode_to_wire(bad)

    def test_from_wire_is_lowercase(self):
        self.assertEqual(mode_from_wire("STUDY"), "study")
        self.assertEqual(mode_from_wire("game"), "game")
        self.assertEqual(mode_from_wire(" Idle "), "idle")

    def test_from_wire_rejects_bad_values(self):
        for bad in ("banana", "", 1, None, {"mode": "study"}):
            with self.assertRaises(IpcProtocolError):
                mode_from_wire(bad)

    def test_round_trip_matches_state_values(self):
        for state in State:
            self.assertEqual(mode_from_wire(mode_to_wire(state)), state.value)

    def test_error_is_value_error(self):
        # 与 protocol.py 的错误保持一致: 调用方 except ValueError 也能兜住
        with self.assertRaises(ValueError):
            mode_from_wire("nope")


# ===========================================================================
#  构造 / 注册 (不需要跑起来)
# ===========================================================================
class TestServerSkeleton(unittest.IsolatedAsyncioTestCase):
    async def test_not_running_initially(self):
        server = LocalServer("/tmp/nope.sock")
        self.assertFalse(server.is_running)
        self.assertEqual(server.client_count, 0)
        self.assertEqual(server.received, 0)
        self.assertEqual(server.dropped, 0)
        self.assertEqual(server.pushed, 0)

    async def test_push_without_clients_is_not_an_error(self):
        server = LocalServer("/tmp/nope.sock")
        self.assertEqual(await server.push(TOPIC_STATUS, {"mode": "IDLE"}), 0)

    async def test_stop_without_start_is_noop(self):
        server = LocalServer("/tmp/nope.sock")
        await server.stop()
        await server.stop()
        self.assertFalse(server.is_running)

    async def test_push_rejects_bad_payload(self):
        # 参数写错要在**调用点**就炸, 而不是悄悄丢掉
        server = LocalServer("/tmp/nope.sock")
        with self.assertRaises(IpcProtocolError):
            await server.push(TOPIC_STATUS, ["not", "an", "object"])

    async def test_on_command_registers_and_clears(self):
        server = LocalServer("/tmp/nope.sock")

        def handler(action, payload):
            return None

        self.assertIs(server.on_command(handler), handler)
        self.assertEqual(server._callbacks, [handler])
        self.assertIsNone(server.on_command(None))
        self.assertEqual(server._callbacks, [])

    async def test_on_command_rejects_non_callable(self):
        server = LocalServer("/tmp/nope.sock")
        with self.assertRaises(IpcServerError):
            server.on_command("not a function")

    async def test_dispatch_ignores_unknown_topic(self):
        server = LocalServer("/tmp/nope.sock")
        seen = []
        server.on_command(lambda a, p: seen.append((a, p)))

        await server._dispatch("bogus_topic", {"x": 1})
        self.assertEqual(seen, [], "未知 topic 必须忽略 (向前兼容)")
        self.assertEqual(server.received, 1, "但它确实是一条解出来的消息")

    async def test_dispatch_without_callback_is_ignored(self):
        server = LocalServer("/tmp/nope.sock")
        await server._dispatch(COMMAND_SWITCH_MODE, {"value": "STUDY"})
        self.assertEqual(server.received, 1)

    async def test_dispatch_calls_sync_callbacks_in_order(self):
        server = LocalServer("/tmp/nope.sock")
        order = []
        server.on_command(lambda a, p: order.append(("first", a, p)))
        server.on_command(lambda a, p: order.append(("second", a, p)))

        await server._dispatch(COMMAND_CHAT_INPUT, {"text": "你好"})
        self.assertEqual(
            order,
            [
                ("first", COMMAND_CHAT_INPUT, {"text": "你好"}),
                ("second", COMMAND_CHAT_INPUT, {"text": "你好"}),
            ],
        )

    async def test_dispatch_awaits_async_callbacks(self):
        server = LocalServer("/tmp/nope.sock")
        seen = []

        async def slow(action, payload):
            await asyncio.sleep(0)
            seen.append((action, payload))

        server.on_command(slow)
        await server._dispatch(COMMAND_CHAT_INPUT, {"text": "x"})
        self.assertEqual(seen, [(COMMAND_CHAT_INPUT, {"text": "x"})])

    async def test_dispatch_survives_callback_exception(self):
        server = LocalServer("/tmp/nope.sock")
        seen = []

        def boom(action, payload):
            raise RuntimeError("回调炸了")

        async def boom_async(action, payload):
            raise RuntimeError("异步回调炸了")

        server.on_command(boom)
        server.on_command(boom_async)
        server.on_command(lambda a, p: seen.append(a))

        await server._dispatch(COMMAND_CHAT_INPUT, {"text": "x"})   # 不该抛
        self.assertEqual(seen, [COMMAND_CHAT_INPUT], "一个回调炸了不该影响后面的")

    async def test_note_drop_counts(self):
        server = LocalServer("/tmp/nope.sock")
        server._note_drop("whatever")
        server._note_drop("again")
        self.assertEqual(server.dropped, 2)


# ===========================================================================
#  空实现 (Windows 上的降级)
# ===========================================================================
class TestNullServer(unittest.IsolatedAsyncioTestCase):
    async def test_interface_is_complete_and_silent(self):
        server = NullServer(reason="测试用")
        self.assertFalse(server.is_running)
        self.assertEqual(server.client_count, 0)
        self.assertIsNone(await server.stop())
        self.assertEqual(await server.push(TOPIC_STATUS, {"mode": "IDLE"}), 0)

        def handler(action, payload):
            return None

        self.assertIs(server.on_command(handler), handler)

    async def test_start_does_not_raise(self):
        server = NullServer(reason="测试用")
        await server.start()          # 只记一条 WARNING, 不抛
        self.assertFalse(server.is_running)


# ===========================================================================
#  build_ipc 的接线
# ===========================================================================
class TestBuildIpc(unittest.IsolatedAsyncioTestCase):
    async def test_returns_working_server_or_null_server(self):
        server = build_ipc(None, {})
        try:
            if UNIX_SOCKET_SUPPORTED:
                self.assertIsInstance(server, LocalServer)
                self.assertEqual(server.path, SOCKET_PATH)
            else:
                self.assertIsInstance(server, NullServer)
        finally:
            await server.stop()

    async def test_socket_path_comes_from_config(self):
        if not UNIX_SOCKET_SUPPORTED:
            self.skipTest("需要 AF_UNIX")
        server = build_ipc(None, {"ipc": {"socket_path": "/tmp/from-config.sock"}})
        try:
            self.assertEqual(server.path, "/tmp/from-config.sock")
        finally:
            await server.stop()

    async def test_bad_queue_size_falls_back_to_default(self):
        if not UNIX_SOCKET_SUPPORTED:
            self.skipTest("需要 AF_UNIX")
        for bad in ("many", 0, -3, True, None):
            server = build_ipc(None, {"ipc": {"queue_size": bad}})
            try:
                self.assertEqual(server.queue_size, DEFAULT_QUEUE_SIZE, repr(bad))
            finally:
                await server.stop()

    async def test_chat_input_goes_to_bus(self):
        from agent.ipc import _make_command_handler
        from agent.io.chat_bus import ChatInputBus

        bus = ChatInputBus()
        handler = _make_command_handler(bus)

        await handler(COMMAND_CHAT_INPUT, {"text": "帮我看看这个"})
        event = bus.get_nowait()
        self.assertIsNotNone(event)
        self.assertEqual(event["source"], "gui", "GUI 来的消息要标成 gui")
        self.assertEqual(event["text"], "帮我看看这个")

    async def test_chat_input_without_text_is_ignored(self):
        from agent.ipc import _make_command_handler
        from agent.io.chat_bus import ChatInputBus

        bus = ChatInputBus()
        handler = _make_command_handler(bus)

        for payload in ({}, {"text": ""}, {"text": "   "}, {"text": 42}):
            await handler(COMMAND_CHAT_INPUT, payload)
        self.assertEqual(bus.qsize(), 0)

    async def test_chat_input_without_bus_is_ignored(self):
        from agent.ipc import _make_command_handler

        handler = _make_command_handler(None)
        await handler(COMMAND_CHAT_INPUT, {"text": "喂"})   # 不该抛

    async def test_unwired_commands_are_ignored_not_crashed(self):
        from agent.ipc import _make_command_handler
        from agent.io.chat_bus import ChatInputBus

        bus = ChatInputBus()
        handler = _make_command_handler(bus)

        await handler(COMMAND_SWITCH_MODE, {"value": "STUDY"})     # 还没接线
        await handler(COMMAND_SWITCH_MODE, {"value": "banana"})    # 非法模式
        await handler(COMMAND_SWITCH_MODE, {})
        await handler(COMMAND_NEXT_WALLPAPER, {})
        await handler("something_new", {})

        self.assertEqual(bus.qsize(), 0, "未接线的命令不能变成聊天输入")

    async def test_switch_mode_reads_the_value_key(self):
        """D2: switch_mode 的 payload 键是 **value**。

        GUI 发的是 {"value": "STUDY"}（docs/ipc-protocol.md §4 与 gui-agent-integration.md §4
        都这么写）; 而 status **推送**里的 "mode" 是反方向的另一个字段, 别混。
        这里只钉"键"这一件事: 认了 value 就不会报"缺少 value"(D5 之后走的是
        "没有状态机"那条路, 因为这里故意不给 runtime); 给了老键 mode 必须点名字段拒掉。
        """
        from agent.ipc import _make_command_handler

        handler = _make_command_handler(None)   # 故意不给 runtime

        with self.assertLogs("agent.ipc", level="WARNING") as caught:
            await handler(COMMAND_SWITCH_MODE, {"value": "STUDY"})
        joined = "\n".join(caught.output)
        self.assertNotIn("缺少 value", joined)
        self.assertIn("没有状态机", joined, "没有 runtime 时要说清是缺状态机")

        with self.assertLogs("agent.ipc", level="WARNING") as caught:
            await handler(COMMAND_SWITCH_MODE, {"mode": "STUDY"})
        joined = "\n".join(caught.output)
        self.assertIn("缺少 value 字段", joined, "老键 mode 必须被明确拒掉")
        self.assertNotIn("收到 switch_mode", joined)

    # ---- Phase 6 D4: 出方向推送 (status / llm) ----

    class _StubRuntime:
        """只带 build_ipc 需要的那两样东西: state 与 on_reply。

        状态机注入 connected_check, 免得 is_connected() 去碰 native。
        """

        def __init__(self, connected=True):
            self.state = StateMachine(connected_check=lambda: connected)
            self.on_reply = None

    def test_without_runtime_only_inbound_is_wired(self):
        # 向后兼容: 老签名 (bus, config) 行为不变 —— 不注册回调、不碰 on_reply
        runtime = self._StubRuntime()
        server = build_ipc(None, {"ipc": {"socket_path": "/tmp/legacy.sock"}})
        self.assertEqual(runtime.state.callback_count, 0)
        self.assertIsNone(runtime.on_reply)

    async def test_runtime_wires_both_directions(self):
        runtime = self._StubRuntime()
        server = build_ipc(None, {"ipc": {"socket_path": "/tmp/wired.sock"}}, runtime=runtime)
        try:
            self.assertEqual(runtime.state.callback_count, 1, "status 推送要挂到 on_change")
            self.assertTrue(callable(runtime.on_reply), "llm 推送要挂到 on_reply")
        finally:
            await server.stop()

    async def test_null_server_still_wires_without_crashing(self):
        # Windows (没有 AF_UNIX) 走 NullServer 分支: 接线不能炸 (push 是空实现)
        from agent.ipc import _wire_outbound
        from agent.ipc.local_server import NullServer

        runtime = self._StubRuntime()
        server = NullServer(reason="test")
        _wire_outbound(server, runtime)

        self.assertEqual(runtime.state.callback_count, 1)
        runtime.state.transition(State.STUDY, "test")   # 不该抛
        runtime.on_reply("x")
        await asyncio.sleep(0)

    async def test_mode_change_reaches_a_connected_gui(self):
        if not UNIX_SOCKET_SUPPORTED:
            self.skipTest("需要 AF_UNIX")
        runtime = self._StubRuntime(connected=True)
        server = build_ipc(None, {"ipc": {"socket_path": _tmp_socket_path()}}, runtime=runtime)
        await server.start()
        writer = None
        try:
            reader, writer = await asyncio.open_unix_connection(server.path)
            await _wait_until(lambda: server.client_count == 1, what="GUI 连上")

            self.assertTrue(runtime.state.transition(State.STUDY, "test"))

            topic, data = decode(await asyncio.wait_for(reader.readline(), 2))
            self.assertEqual(topic, TOPIC_STATUS)
            self.assertEqual(data["mode"], "STUDY", "线上取值必须是大写")
            self.assertIs(data["connected"], True)
        finally:
            if writer is not None:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
            await server.stop()

    async def test_reply_reaches_a_connected_gui(self):
        if not UNIX_SOCKET_SUPPORTED:
            self.skipTest("需要 AF_UNIX")
        runtime = self._StubRuntime()
        server = build_ipc(None, {"ipc": {"socket_path": _tmp_socket_path()}}, runtime=runtime)
        await server.start()
        writer = None
        try:
            reader, writer = await asyncio.open_unix_connection(server.path)
            await _wait_until(lambda: server.client_count == 1, what="GUI 连上")

            runtime.on_reply("已经切换到学习模式。")

            topic, data = decode(await asyncio.wait_for(reader.readline(), 2))
            self.assertEqual(topic, TOPIC_LLM)
            self.assertEqual(data, {"text": "已经切换到学习模式。"})
        finally:
            if writer is not None:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
            await server.stop()

    async def test_push_after_stop_is_ignored(self):
        # server 停了之后状态还会变 (调度器/其它组件), 不该抛也不该刷屏
        runtime = self._StubRuntime()
        server = build_ipc(None, {"ipc": {"socket_path": _tmp_socket_path()}}, runtime=runtime)
        await server.start()
        await server.stop()

        runtime.state.transition(State.STUDY, "after-stop")   # 不该抛
        runtime.on_reply("x")
        await asyncio.sleep(0)

    # ---- Phase 6 D5: switch_mode 真的驱动状态机 ----

    async def test_switch_mode_transitions_the_state_machine(self):
        from agent.ipc import _make_command_handler
        from agent.io.chat_bus import ChatInputBus

        runtime = self._StubRuntime()
        handler = _make_command_handler(ChatInputBus(), runtime=runtime)

        self.assertEqual(runtime.state.current(), State.IDLE)
        await handler(COMMAND_SWITCH_MODE, {"value": "STUDY"})
        self.assertEqual(runtime.state.current(), State.STUDY, "命令要真的切状态")

        await handler(COMMAND_SWITCH_MODE, {"value": "IDLE"})
        self.assertEqual(runtime.state.current(), State.IDLE)

    async def test_illegal_switch_mode_pushes_the_real_state_back(self):
        """非法转换: 拒绝 + 把**真实**状态推回 GUI (docs §4 承诺过这件事)。

        IDLE -> STUDY 合法; STUDY -> GAME 非法 (状态机的表里 STUDY 只能回 IDLE)。
        """
        from agent.ipc import _make_command_handler
        from agent.io.chat_bus import ChatInputBus

        runtime = self._StubRuntime()
        pushed = []
        handler = _make_command_handler(
            ChatInputBus(), runtime=runtime, push=lambda topic, data: pushed.append((topic, data))
        )

        await handler(COMMAND_SWITCH_MODE, {"value": "STUDY"})
        self.assertEqual(runtime.state.current(), State.STUDY)
        self.assertEqual(pushed, [], "成功时由 on_change 推送, 这里不该再推一条")

        await handler(COMMAND_SWITCH_MODE, {"value": "GAME"})      # STUDY -> GAME 非法
        self.assertEqual(runtime.state.current(), State.STUDY, "非法转换不能改状态")
        self.assertEqual(len(pushed), 1, "要把真实状态推回去")
        topic, data = pushed[0]
        self.assertEqual(topic, TOPIC_STATUS)
        self.assertEqual(data["mode"], "STUDY", "推的是**当前真实**状态, 不是请求的 GAME")

    async def test_switch_mode_without_runtime_is_ignored(self):
        from agent.ipc import _make_command_handler

        handler = _make_command_handler(None, runtime=None)
        # 不该抛 (老 factory 不给 runtime 时就是这条路)
        await handler(COMMAND_SWITCH_MODE, {"value": "STUDY"})

    # ---- Phase 6 D7: 未接线的命令要回一句说明 (GUI 上别点了没反应) ----

    async def test_unwired_commands_reply_with_a_note(self):
        from agent.ipc import _make_command_handler

        pushed = []
        handler = _make_command_handler(
            None, runtime=None, push=lambda topic, data: pushed.append((topic, data))
        )

        # ⚠ T3 起 next_wallpaper **接上下游了**, 所以它不再走"未接线"这条路 ——
        #   它有自己的处理器 (见 tests/test_wallpaper.py 的 TestIpcCommand)。
        await handler(COMMAND_NEXT_BILIBILI, {})

        self.assertEqual(len(pushed), 1, "未接线的命令要回一句")
        topic, data = pushed[0]
        self.assertEqual(topic, TOPIC_LLM, "走 llm 通道 (GUI 显示成助手气泡)")
        self.assertIn("接入", data["text"], "说明要讲清没接入")
        self.assertIn("Phase 7", data["text"])

    async def test_next_wallpaper_without_a_runtime_replies_too(self):
        # 老 factory 不给 runtime 时: 不是"未接线", 而是"模块没接进来" —— 同样要回话
        from agent.ipc import NO_WALLPAPER_NOTE, _make_command_handler

        pushed = []
        handler = _make_command_handler(
            None, runtime=None, push=lambda topic, data: pushed.append((topic, data))
        )
        await handler(COMMAND_NEXT_WALLPAPER, {})

        self.assertEqual(len(pushed), 1)
        self.assertEqual(pushed[0][0], TOPIC_LLM)
        self.assertEqual(pushed[0][1]["text"], NO_WALLPAPER_NOTE)

    async def test_unknown_action_does_not_spam_llm(self):
        # 不认识的 action 只记 warning: 它是版本不一致的正常现象, 不该往聊天里塞话
        from agent.ipc import _make_command_handler

        pushed = []
        handler = _make_command_handler(
            None, runtime=None, push=lambda topic, data: pushed.append((topic, data))
        )
        await handler("something_new", {})
        self.assertEqual(pushed, [])

    async def test_unwired_command_without_push_is_harmless(self):
        from agent.ipc import _make_command_handler

        handler = _make_command_handler(None)
        await handler(COMMAND_NEXT_WALLPAPER, {})   # 不该抛

    async def test_unwired_command_note_reaches_a_real_client(self):
        if not UNIX_SOCKET_SUPPORTED:
            self.skipTest("需要 AF_UNIX")
        server = build_ipc(None, {"ipc": {"socket_path": _tmp_socket_path()}})
        await server.start()
        writer = None
        try:
            reader, writer = await asyncio.open_unix_connection(server.path)
            await _wait_until(lambda: server.client_count == 1, what="GUI 连上")

            # ⚠ T3 起换成了 next_bilibili: next_wallpaper 已经接上下游,
            #   走的是"真的换一张"那条路（见 tests/test_wallpaper.py）。
            writer.write(encode_command(COMMAND_NEXT_BILIBILI, {}))
            await writer.drain()

            topic, data = decode(await asyncio.wait_for(reader.readline(), 2))
            self.assertEqual(topic, TOPIC_LLM)
            self.assertIn("接入", data["text"])
        finally:
            if writer is not None:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
            await server.stop()

    async def test_switch_mode_round_trip_over_a_real_socket(self):
        """D5 的验收形态: GUI 发 switch_mode -> Agent 切状态 -> 推 status 回来。"""
        if not UNIX_SOCKET_SUPPORTED:
            self.skipTest("需要 AF_UNIX")
        runtime = self._StubRuntime(connected=False)
        server = build_ipc(None, {"ipc": {"socket_path": _tmp_socket_path()}}, runtime=runtime)
        await server.start()
        writer = None
        try:
            reader, writer = await asyncio.open_unix_connection(server.path)
            await _wait_until(lambda: server.client_count == 1, what="GUI 连上")

            writer.write(encode_command(COMMAND_SWITCH_MODE, {"value": "STUDY"}))
            await writer.drain()

            topic, data = decode(await asyncio.wait_for(reader.readline(), 2))
            self.assertEqual(topic, TOPIC_STATUS)
            self.assertEqual(data["mode"], "STUDY")
            self.assertIs(data["connected"], False)
            self.assertEqual(runtime.state.current(), State.STUDY)

            # 再来一条非法的: 应当收到一条**真实状态**的 status (仍是 STUDY)
            writer.write(encode_command(COMMAND_SWITCH_MODE, {"value": "GAME"}))
            await writer.drain()
            topic, data = decode(await asyncio.wait_for(reader.readline(), 2))
            self.assertEqual((topic, data["mode"]), (TOPIC_STATUS, "STUDY"))
            self.assertEqual(runtime.state.current(), State.STUDY)
        finally:
            if writer is not None:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
            await server.stop()

    # ---- P 系列: 日程触发事实 (schedule topic / query_schedule) ----

    class _StubScheduler:
        """只带 ipc 层真正用到的那三样: recent_fired() / history_limit / on_fire。

        @note 故意**不是**真 Scheduler: 这一层只依赖"能报触发过什么"这个能力,
              所以用替身才能证明"换个实现也接得上"。真 Scheduler 另有一条用例。
        """

        def __init__(self, facts=None, limit=50):
            self._facts = list(facts or [])
            self.history_limit = limit
            self.on_fire = None

        def recent_fired(self, limit=None):
            if limit is None:
                return [dict(f) for f in self._facts]
            return [dict(f) for f in self._facts[-limit:]]

    class _RuntimeWithScheduler:
        """state + on_reply + scheduler (后两个可以有, 也可以没有)。"""

        def __init__(self, scheduler=None, connected=True):
            self.state = StateMachine(connected_check=lambda: connected)
            self.on_reply = None
            self.scheduler = scheduler

    FACT = {
        "title": "午休",
        "date": "2026-09-22",
        "scheduled_at": "2026-09-22T13:00",
        "fired_at": "2026-09-22T13:00:03",
        "actions": [{"type": "message", "text": "日程提醒：午休", "timestamp": 1.0}],
    }

    async def test_scheduler_gets_the_on_fire_hook(self):
        runtime = self._RuntimeWithScheduler(self._StubScheduler())
        server = build_ipc(None, {"ipc": {"socket_path": _tmp_socket_path()}},
                           runtime=runtime)
        try:
            self.assertTrue(callable(runtime.scheduler.on_fire),
                            "接上 runtime.scheduler 就要挂 on_fire")
        finally:
            await server.stop()

    async def test_fired_event_reaches_a_connected_client(self):
        """实时那条: 日程真的触发 -> 客户端收到 schedule{kind:"fired"}。"""
        if not UNIX_SOCKET_SUPPORTED:
            self.skipTest("需要 AF_UNIX")
        scheduler = self._StubScheduler()
        runtime = self._RuntimeWithScheduler(scheduler)
        server = build_ipc(None, {"ipc": {"socket_path": _tmp_socket_path()}},
                           runtime=runtime)
        await server.start()
        writer = None
        try:
            reader, writer = await asyncio.open_unix_connection(server.path)
            await _wait_until(lambda: server.client_count == 1, what="GUI 连上")

            scheduler.on_fire(dict(self.FACT))     # 调度器触发了一条

            topic, data = decode(await asyncio.wait_for(reader.readline(), 2))
            self.assertEqual(topic, TOPIC_SCHEDULE)
            self.assertEqual(data["kind"], SCHEDULE_KIND_FIRED)
            self.assertEqual(data["event"], self.FACT)
        finally:
            if writer is not None:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
            await server.stop()

    async def test_query_schedule_pushes_the_snapshot(self):
        from agent.ipc import _make_command_handler

        scheduler = self._StubScheduler([self.FACT], limit=7)
        runtime = self._RuntimeWithScheduler(scheduler)
        pushed = []
        handler = _make_command_handler(
            None, runtime=runtime, push=lambda topic, data: pushed.append((topic, data))
        )

        await handler(COMMAND_QUERY_SCHEDULE, {})

        self.assertEqual(len(pushed), 1)
        topic, data = pushed[0]
        self.assertEqual(topic, TOPIC_SCHEDULE)
        self.assertEqual(data["kind"], SCHEDULE_KIND_STATE)
        self.assertEqual(data["fired"], [self.FACT])
        self.assertEqual(data["limit"], 7, "limit 报的是上限, 不是这次带了几条")
        # now 要能被客户端解析回来 (它按这个显示"这是什么时候问的")
        datetime.strptime(data["now"], "%Y-%m-%dT%H:%M:%S")

    async def test_query_schedule_snapshot_is_a_copy(self):
        from agent.ipc import _make_command_handler

        scheduler = self._StubScheduler([self.FACT])
        runtime = self._RuntimeWithScheduler(scheduler)
        pushed = []
        handler = _make_command_handler(
            None, runtime=runtime, push=lambda topic, data: pushed.append((topic, data))
        )
        await handler(COMMAND_QUERY_SCHEDULE, {})
        pushed[0][1]["fired"][0]["title"] = "改坏了"
        self.assertEqual(scheduler.recent_fired()[0]["title"], "午休")

    async def test_query_schedule_without_scheduler_replies_with_a_note(self):
        from agent.ipc import _make_command_handler

        pushed = []
        handler = _make_command_handler(
            None, runtime=self._RuntimeWithScheduler(None),
            push=lambda topic, data: pushed.append((topic, data)),
        )

        with self.assertLogs("agent.ipc", level="WARNING") as caught:
            await handler(COMMAND_QUERY_SCHEDULE, {})

        self.assertEqual(pushed, [(TOPIC_LLM, {"text": NO_SCHEDULER_NOTE})],
                         "没有调度器要回一句说明, 不能静默")
        self.assertIn("没有调度器", "\n".join(caught.output))

    async def test_query_schedule_without_push_is_harmless(self):
        from agent.ipc import _make_command_handler

        handler = _make_command_handler(
            None, runtime=self._RuntimeWithScheduler(self._StubScheduler())
        )
        await handler(COMMAND_QUERY_SCHEDULE, {})      # 不该抛

    async def test_no_scheduler_does_not_break_the_other_pushes(self):
        """"有就接" 的意思: 少了调度器只少推一类, status/llm 照旧。"""
        runtime = self._RuntimeWithScheduler(None)
        server = build_ipc(None, {"ipc": {"socket_path": _tmp_socket_path()}},
                           runtime=runtime)
        try:
            self.assertEqual(runtime.state.callback_count, 1)
            self.assertTrue(callable(runtime.on_reply))
        finally:
            await server.stop()

    async def test_query_schedule_round_trip_over_a_real_socket(self):
        """验收形态: 发 query_schedule -> 收到 schedule{kind:"state"}。

        这条把三件事串起来: server 的 COMMANDS 白名单 (认这条命令)、命令处理分支、
        push 到线上 —— 任何一环漏了它都会红。
        """
        if not UNIX_SOCKET_SUPPORTED:
            self.skipTest("需要 AF_UNIX")
        scheduler = self._StubScheduler([self.FACT])
        runtime = self._RuntimeWithScheduler(scheduler)
        server = build_ipc(None, {"ipc": {"socket_path": _tmp_socket_path()}},
                           runtime=runtime)
        await server.start()
        writer = None
        try:
            reader, writer = await asyncio.open_unix_connection(server.path)
            await _wait_until(lambda: server.client_count == 1, what="GUI 连上")

            writer.write(encode_command(COMMAND_QUERY_SCHEDULE, {}))
            await writer.drain()

            topic, data = decode(await asyncio.wait_for(reader.readline(), 2))
            self.assertEqual(topic, TOPIC_SCHEDULE)
            self.assertEqual(data["kind"], SCHEDULE_KIND_STATE)
            self.assertEqual(data["fired"], [self.FACT])
        finally:
            if writer is not None:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
            await server.stop()

    async def test_snapshot_uses_a_real_scheduler(self):
        """真 Scheduler -> 真 fact -> 真快照 (P1 的产出与 P3 的壳不漂移)。"""
        from agent.core import Scheduler
        from agent.ipc import _make_command_handler

        class _Bus:
            async def push(self, source, text):
                return {"source": source, "text": text, "timestamp": 1.0}

        runtime = self._RuntimeWithScheduler(None)
        scheduler = Scheduler(
            state=runtime.state, bus=_Bus(),
            config={"recurring": [{"title": "站会", "start": "09:30"}]},
        )
        runtime.scheduler = scheduler
        pushed = []
        handler = _make_command_handler(
            None, runtime=runtime, push=lambda topic, data: pushed.append((topic, data))
        )

        await handler(COMMAND_QUERY_SCHEDULE, {})          # 还没触发过
        self.assertEqual(pushed[-1][1]["fired"], [])
        self.assertEqual(pushed[-1][1]["limit"], scheduler.history_limit)

        await scheduler.check_schedule(datetime(2026, 9, 16, 9, 30, 4))
        await handler(COMMAND_QUERY_SCHEDULE, {})

        data = pushed[-1][1]
        self.assertEqual([f["title"] for f in data["fired"]], ["站会"])
        self.assertEqual(data["fired"][0]["fired_at"], "2026-09-16T09:30:04")
        self.assertEqual(data["fired"][0]["date"], "2026-09-16")
        self.assertEqual(data["fired"][0]["actions"][0]["type"], "message")

    async def test_client_connect_hook_fires_before_any_command(self):
        """T6: 新 GUI 连上 -> 立刻补推当前壁纸（`wallpaper` 只在变化时推, 连上收不到）。

        这条只在板端真跑（PC 上没有 AF_UNIX 会 skip）—— 端到端就靠它。
        """
        if not UNIX_SOCKET_SUPPORTED:
            self.skipTest("需要 AF_UNIX")

        pushed = []

        def on_connect():
            pushed.append("called")

        server = LocalServer(path=_tmp_socket_path(), on_client_connect=on_connect)
        await server.start()
        writer = None
        try:
            reader, writer = await asyncio.open_unix_connection(server.path)
            await _wait_until(lambda: server.client_count == 1, what="GUI 连上")
            await _wait_until(lambda: pushed, what="连上回调被叫")
            self.assertEqual(pushed, ["called"], "连上就该叫一次, 不用等客户端发命令")
        finally:
            if writer is not None:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
            await server.stop()

    async def test_a_broken_connect_hook_keeps_the_connection(self):
        """补推炸了不该把刚建立的连接弄断（也不该影响后续命令）。

        走**真装配**（build_ipc）: 这样补推回调、命令分发、推送入口都是生产路径。
        """
        if not UNIX_SOCKET_SUPPORTED:
            self.skipTest("需要 AF_UNIX")

        class _BrokenRuntime:
            def push_current_wallpaper(self):
                raise RuntimeError("补推炸了")

        server = build_ipc(None, {"ipc": {"socket_path": _tmp_socket_path()}},
                           runtime=_BrokenRuntime())
        await server.start()
        writer = None
        try:
            reader, writer = await asyncio.open_unix_connection(server.path)
            await _wait_until(lambda: server.client_count == 1, what="GUI 连上")
            # 连接仍然活着: 发一条未接线命令, 该收到那句"还没接入"的说明
            writer.write(encode_command(COMMAND_NEXT_BILIBILI, {}))
            await writer.drain()
            topic, data = decode(await asyncio.wait_for(reader.readline(), 2))
            self.assertEqual(topic, TOPIC_LLM, "后续命令照常处理")
            self.assertIn("接入", data["text"])
        finally:
            if writer is not None:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
            await server.stop()


# ===========================================================================
#  切帧 / 错误容忍 / 背压 (跨平台: 假 stream)
# ===========================================================================
class _SessionTestCase(unittest.IsolatedAsyncioTestCase):
    max_line_bytes = MAX_LINE_BYTES
    queue_size = 8

    def setUp(self):
        self.log = logging.getLogger("agent.test.ipc")
        self.messages = []
        self.drops = []

    def make_session(self, data=b"", writer=None, on_message=None, feed_eof=True,
                     max_line_bytes=None, queue_size=None):
        limit = self.max_line_bytes if max_line_bytes is None else max_line_bytes
        reader = asyncio.StreamReader(limit=limit + 1)
        if data:
            reader.feed_data(data)
        if feed_eof:
            reader.feed_eof()
        writer = writer if writer is not None else _RecordingWriter()
        session = _ClientSession(
            reader=reader,
            writer=writer,
            on_message=on_message or (lambda t, d: self.messages.append((t, d))),
            on_drop=self.drops.append,
            max_line_bytes=limit,
            queue_size=self.queue_size if queue_size is None else queue_size,
            logger=self.log,
        )
        # run() 里会 close(), 幂等; 万一测试中途失败也不留 pending task
        self.addAsyncCleanup(session.close)
        return session, reader, writer

    async def run_session(self, data=b"", **kwargs):
        session, reader, writer = self.make_session(data, **kwargs)
        await session.run()
        return session, reader, writer


class TestClientSessionFraming(_SessionTestCase):
    async def test_single_message(self):
        await self.run_session(_command(text="你好"))
        self.assertEqual(self.messages, [(COMMAND_CHAT_INPUT, {"text": "你好"})])
        self.assertEqual(self.drops, [])

    async def test_two_messages_in_one_chunk(self):
        data = _command(text="一") + _command(text="二")
        session, _, _ = await self.run_session(data)
        self.assertEqual([m[1]["text"] for m in self.messages], ["一", "二"])
        self.assertEqual(session.received, 2)

    async def test_message_split_across_chunks(self):
        # 一次 TCP/unix 读常常只到一半, 必须能拼回来
        session, reader, _ = self.make_session(feed_eof=False)
        task = asyncio.create_task(session.run())
        raw = _command(text="分两段")
        reader.feed_data(raw[:10])
        await asyncio.sleep(0)
        self.assertEqual(self.messages, [], "半条消息不能被解析")
        reader.feed_data(raw[10:])
        reader.feed_eof()
        await asyncio.wait_for(task, 2)
        self.assertEqual(self.messages, [(COMMAND_CHAT_INPUT, {"text": "分两段"})])

    async def test_crlf_is_tolerated(self):
        await self.run_session(_command(text="crlf").rstrip(b"\n") + b"\r\n")
        self.assertEqual(self.messages[0][1]["text"], "crlf")

    async def test_empty_line_is_dropped_but_connection_survives(self):
        session, _, _ = await self.run_session(b"\n" + _command(text="活着"))
        self.assertEqual(session.dropped, 1)
        self.assertEqual(len(self.drops), 1)
        self.assertEqual(self.messages, [(COMMAND_CHAT_INPUT, {"text": "活着"})])

    async def test_bad_json_is_dropped(self):
        session, _, _ = await self.run_session(b"{not json}\n" + _command(text="ok"))
        self.assertEqual(session.dropped, 1)
        self.assertEqual(self.messages, [(COMMAND_CHAT_INPUT, {"text": "ok"})])

    async def test_non_object_message_is_dropped(self):
        # JSON 合法但不是 object
        session, _, _ = await self.run_session(b'[1,2,3]\n')
        self.assertEqual(session.dropped, 1)
        self.assertEqual(self.messages, [])

    async def test_topic_shaped_command_is_dropped(self):
        # 老格式 ({"topic","data","timestamp"}) 发到命令方向 -> 缺 action, 明确丢弃。
        # 这条是 D1 的分水岭: 命令方向只认 {"action","payload"}, 不做两种格式的兼容
        # —— 兼容就等于"文档说 A、代码也收 B", 正是要收敛掉的东西。
        line = json.dumps({"topic": COMMAND_CHAT_INPUT, "data": {"text": "x"},
                           "timestamp": 1.0})
        session, _, _ = await self.run_session(line.encode() + b"\n")
        self.assertEqual(session.dropped, 1, "topic 形态的命令必须被拒")
        self.assertEqual(self.messages, [])

    async def test_invalid_utf8_is_dropped(self):
        session, _, _ = await self.run_session(b"\xff\xfe\xfd\n" + _command(text="ok"))
        self.assertEqual(session.dropped, 1)
        self.assertEqual(len(self.messages), 1)

    async def test_unknown_action_reaches_upper_layer(self):
        # 会话层只切帧, 不认识 action —— 过滤是 LocalServer._dispatch 的事
        await self.run_session(_command(action="brand_new_thing", text="x"))
        self.assertEqual(self.messages, [("brand_new_thing", {"text": "x"})])

    async def test_many_bad_lines_do_not_close_connection(self):
        data = b"garbage\n" * 20 + _command(text="最后一条")
        session, _, _ = await self.run_session(data)
        self.assertEqual(session.dropped, 20)
        self.assertEqual(self.messages, [(COMMAND_CHAT_INPUT, {"text": "最后一条"})])

    async def test_eof_with_partial_message_is_dropped(self):
        session, _, _ = await self.run_session(b'{"action":"chat_input","payload":{')
        self.assertEqual(session.dropped, 1)
        self.assertEqual(self.messages, [])
        self.assertEqual(self.drops, ["truncated at EOF"])

    async def test_received_counter(self):
        session, _, _ = await self.run_session(_command(text="a") + _command(text="b"))
        self.assertEqual(session.received, 2)
        self.assertEqual(session.dropped, 0)


class TestClientSessionOversize(_SessionTestCase):
    # 故意把上限压小, 方便造超长行。96 仍然装得下 _short() (约 42 字节)。
    max_line_bytes = 96

    async def test_oversize_line_is_dropped_once_and_connection_survives(self):
        oversize = b"x" * 200 + b"\n"
        session, _, _ = await self.run_session(oversize + _short())
        self.assertEqual(session.dropped, 1, "一条超长行只能记一次")
        self.assertEqual(self.messages, [("ping", {})])

    async def test_two_oversize_lines(self):
        session, _, _ = await self.run_session(
            b"y" * 300 + b"\n" + b"z" * 300 + b"\n" + _short()
        )
        self.assertEqual(session.dropped, 2)
        self.assertEqual(self.messages, [("ping", {})])

    async def test_oversize_without_newline_closes_connection(self):
        # 丢掉的内容超过预算 -> 认为对端在灌垃圾, 断开, 后面的消息不再处理
        session, _, _ = await self.run_session(b"x" * 80000 + _short())
        self.assertEqual(session.dropped, 1)
        self.assertEqual(self.messages, [])
        self.assertEqual(self.drops, ["oversize without newline"])

    async def test_line_exactly_at_limit_is_ok(self):
        # 上限内的消息不许被误杀。命令方向没有 timestamp, 行长完全由文本决定:
        # 先量出空文本有多长, 再补字符到"正好等于上限"。
        base = _command(text="")
        pad = 5
        limit = len(base) - 1 + pad
        raw = _command(text="a" * pad)
        self.assertEqual(len(raw) - 1, limit, "用例构造错了")

        session, _, _ = await self.run_session(raw, max_line_bytes=limit)
        self.assertEqual(session.dropped, 0)
        self.assertEqual(len(self.messages), 1)


class TestClientSessionBackpressure(_SessionTestCase):
    queue_size = 2

    async def test_enqueue_writes_exact_bytes(self):
        session, _, writer = self.make_session(feed_eof=False)
        pump = asyncio.create_task(session._pump_loop())
        session._pump = pump
        payload = encode(TOPIC_STATUS, {"mode": "STUDY", "connected": True})
        self.assertTrue(session.enqueue(payload))
        await _wait_until(lambda: bytes(writer.written) == payload, what="字节写出")
        self.assertEqual(writer.drain_calls, 1)

    async def test_queue_full_drops_oldest_and_keeps_client(self):
        writer = _RecordingWriter(blocking=True)
        session, _, _ = self.make_session(feed_eof=False, writer=writer)
        pump = asyncio.create_task(session._pump_loop())
        session._pump = pump

        first = encode(TOPIC_STATUS, {"n": 1})
        second = encode(TOPIC_STATUS, {"n": 2})
        third = encode(TOPIC_STATUS, {"n": 3})
        fourth = encode(TOPIC_STATUS, {"n": 4})

        # 第一条已经被取走并写进 writer, 现在 drain 卡住了
        self.assertTrue(session.enqueue(first))
        await _wait_until(lambda: bytes(writer.written) == first, what="第一条写出")

        self.assertTrue(session.enqueue(second))
        self.assertTrue(session.enqueue(third))
        # 队列满了 -> 丢最旧的 second, 收下 fourth
        self.assertFalse(session.enqueue(fourth))
        self.assertFalse(session.closing, "消费不过来只能丢消息, 不能踢客户端")

        writer.release()
        expected = first + third + fourth
        await _wait_until(lambda: bytes(writer.written) == expected, what="丢最旧后补上")
        self.assertEqual(bytes(writer.written), expected)
        self.assertNotIn(second, bytes(writer.written))

    async def test_enqueue_after_close_returns_false(self):
        session, _, _ = self.make_session(feed_eof=False)
        await session.close()
        self.assertTrue(session.closing)
        self.assertFalse(session.enqueue(b"x\n"))

    async def test_close_is_idempotent(self):
        session, _, writer = self.make_session(feed_eof=False)
        await session.close()
        await session.close()
        self.assertTrue(writer.closed)

    async def test_enqueue_flood_does_not_block(self):
        session, _, writer = self.make_session(feed_eof=False)
        writer._gate.clear()               # 永远卡住 drain
        pump = asyncio.create_task(session._pump_loop())
        session._pump = pump

        # 1000 条进去必须立刻返回 (靠丢最旧保持有界), 不能把调用方挂住
        async def flood():
            for i in range(1000):
                session.enqueue(encode(TOPIC_STATUS, {"n": i}))

        await asyncio.wait_for(flood(), 3)
        self.assertFalse(session.closing)


class TestClientSessionCallbacks(_SessionTestCase):
    async def test_callback_exception_does_not_kill_session(self):
        calls = []

        def boom(topic, payload):
            calls.append(topic)
            raise RuntimeError("炸")

        session, _, _ = await self.run_session(
            _command(text="a") + _command(text="b"), on_message=boom
        )
        self.assertEqual(calls, [COMMAND_CHAT_INPUT, COMMAND_CHAT_INPUT])
        self.assertEqual(session.received, 2, "回调抛错也要算收到了")

    async def test_async_callback_is_awaited_in_order(self):
        seen = []

        async def handler(topic, payload):
            await asyncio.sleep(0)
            seen.append(payload["text"])

        await self.run_session(
            _command(text="1") + _command(text="2") + _command(text="3"),
            on_message=handler,
        )
        self.assertEqual(seen, ["1", "2", "3"], "命令必须按顺序处理")

    async def test_repr(self):
        session, _, _ = await self.run_session(_command(text="x"))
        self.assertIn("received=1", repr(session))


# ===========================================================================
#  真 socket 端到端 (POSIX only)
# ===========================================================================
@unittest.skipUnless(UNIX_SOCKET_SUPPORTED, "需要 AF_UNIX (Windows 的 CPython 不支持)")
class TestLocalServerSocket(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ipc-server-")
        self.path = os.path.join(self.tmp, "agent.sock")
        self.servers = []

    async def asyncTearDown(self):
        for server in self.servers:
            await server.stop()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_server(self, path=None, **kwargs):
        # 注意用 `is None` 而不是 `or`: path="" 也是要测的输入
        server = LocalServer(self.path if path is None else path, **kwargs)
        self.servers.append(server)
        return server

    async def connect(self, path=None):
        """连上并**等到 server 那边真的 accept 了**。

        accept 是异步的: 刚 open_unix_connection 完, server._sessions 里还没有这个
        连接, 这时候 push() 会当成"没有 GUI"直接丢掉。真实 GUI 是先连上再等推送,
        不存在这个问题; 测试要自己等这一刻, 否则断言会变成随机失败。
        """
        reader, writer = await asyncio.open_unix_connection(path or self.path)
        self.addAsyncCleanup(_close_writer, writer)
        return reader, writer

    async def wait_for_clients(self, server, count=1):
        await _wait_until(
            lambda: server.client_count == count, what="server accept %d 个连接" % count
        )

    # ---- 生命周期 ----
    async def test_start_creates_socket_with_0600(self):
        server = self.make_server()
        await server.start()
        self.assertTrue(server.is_running)
        self.assertEqual(server.client_count, 0)
        self.assertTrue(os.path.exists(self.path))
        self.assertTrue(stat.S_ISSOCK(os.lstat(self.path).st_mode))
        mode = stat.S_IMODE(os.lstat(self.path).st_mode)
        self.assertEqual(mode, 0o600, "socket 权限应该是 %s, 实际 %s" % (oct(0o600), oct(mode)))

    async def test_start_is_idempotent(self):
        server = self.make_server()
        await server.start()
        await server.start()
        self.assertTrue(server.is_running)

    async def test_stop_unlinks_socket_file(self):
        server = self.make_server()
        await server.start()
        await server.stop()
        self.assertFalse(server.is_running)
        self.assertFalse(os.path.exists(self.path), "stop() 必须把 socket 文件删掉")
        with self.assertRaises((FileNotFoundError, ConnectionRefusedError, OSError)):
            await asyncio.open_unix_connection(self.path)

    async def test_parent_directories_are_created(self):
        nested = os.path.join(self.tmp, "a", "b", "agent.sock")
        server = self.make_server(path=nested)
        await server.start()
        self.assertTrue(os.path.exists(nested))

    async def test_parent_that_is_a_file_is_refused(self):
        blocker = os.path.join(self.tmp, "blocker")
        with open(blocker, "w", encoding="utf-8") as handle:
            handle.write("x")
        server = self.make_server(path=os.path.join(blocker, "agent.sock"))
        with self.assertRaises(IpcServerError):
            await server.start()

    async def test_empty_path_is_refused(self):
        server = self.make_server(path="")
        with self.assertRaises(IpcServerError):
            await server.start()

    async def test_regular_file_at_path_is_refused_not_deleted(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("重要数据")
        server = self.make_server()
        with self.assertRaises(IpcServerError):
            await server.start()
        self.assertTrue(os.path.exists(self.path), "不是 socket 就不能删")
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "重要数据")

    async def test_stale_socket_file_is_replaced(self):
        # 模拟上次被 kill -9: 文件还在, 但没人 listen
        raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        raw.bind(self.path)
        raw.close()
        self.assertTrue(os.path.exists(self.path))

        server = self.make_server()
        await server.start()
        self.assertTrue(server.is_running)
        reader, _ = await self.connect()
        await self.wait_for_clients(server)
        await server.push(TOPIC_STATUS, {"mode": "IDLE"})
        topic, data = decode(await asyncio.wait_for(reader.readline(), 2))
        self.assertEqual(topic, TOPIC_STATUS)

    async def test_live_instance_is_not_stolen(self):
        first = self.make_server()
        await first.start()

        # ⚠ 顺序很关键: 先建立并确认**我们自己的**连接, 再去触发"第二个实例"。
        #
        # second.start() 里的"残留 socket 探测"会**真的连上** first, 而那条连接在
        # first 侧是**异步**登记、**异步**注销的。如果先探测再连接, 那么
        # "client_count >= 1" 完全可能被那条探测连接满足 —— push() 于是推给了它
        # (板端日志里就是 fd=8 BrokenPipe), 我们自己的 reader 什么都收不到。
        #
        # 反过来写就没有这个歧义: 探测连接只可能**多出**一个瞬时 session, 而
        # push() 是广播, 我们这条连接一定收得到。
        reader, _ = await self.connect()
        await self.wait_for_clients(first, 1)

        second = self.make_server()
        with self.assertRaises(IpcServerError) as ctx:
            await second.start()
        self.assertIn("另一个 agent 实例", str(ctx.exception))

        # 关键: 第一个实例的 socket 文件必须原封不动, 还能正常服务
        self.assertTrue(os.path.exists(self.path))
        self.assertTrue(first.is_running)

        await first.push(TOPIC_STATUS, {"mode": "GAME"})
        topic, data = decode(await asyncio.wait_for(reader.readline(), 2))
        self.assertEqual(data["mode"], "GAME")

    # ---- 发送 ----
    async def test_push_reaches_gui(self):
        server = self.make_server()
        await server.start()
        reader, _ = await self.connect()
        await self.wait_for_clients(server, 1)

        sent = {"mode": "STUDY", "connected": True}
        self.assertEqual(await server.push(TOPIC_STATUS, sent), 1)

        line = await asyncio.wait_for(reader.readline(), 2)
        topic, data = decode(line)
        self.assertEqual(topic, TOPIC_STATUS)
        self.assertEqual(data, sent)
        self.assertEqual(server.pushed, 1)

    async def test_push_broadcasts_to_all_clients(self):
        server = self.make_server()
        await server.start()
        reader_a, _ = await self.connect()
        reader_b, _ = await self.connect()
        await self.wait_for_clients(server, 2)

        self.assertEqual(await server.push("music", {"title": "歌"}), 2)
        for reader in (reader_a, reader_b):
            topic, data = decode(await asyncio.wait_for(reader.readline(), 2))
            self.assertEqual((topic, data), ("music", {"title": "歌"}))

    async def test_push_preserves_order(self):
        server = self.make_server()
        await server.start()
        reader, _ = await self.connect()
        await self.wait_for_clients(server, 1)

        for i in range(20):
            await server.push(TOPIC_STATUS, {"n": i})
        got = []
        for _ in range(20):
            topic, data = decode(await asyncio.wait_for(reader.readline(), 2))
            got.append(data["n"])
        self.assertEqual(got, list(range(20)))

    async def test_unicode_survives_the_socket(self):
        server = self.make_server()
        await server.start()
        reader, _ = await self.connect()
        await self.wait_for_clients(server, 1)

        await server.push("llm", {"text": "你好, 世界 🌏"})
        topic, data = decode(await asyncio.wait_for(reader.readline(), 2))
        self.assertEqual(data["text"], "你好, 世界 🌏")

    async def test_push_with_no_client_returns_zero(self):
        server = self.make_server()
        await server.start()
        self.assertEqual(await server.push(TOPIC_STATUS, {"mode": "IDLE"}), 0)

    async def test_slow_client_does_not_block_or_get_disconnected(self):
        server = self.make_server(queue_size=2)
        await server.start()
        _, writer = await self.connect()          # 连上但不读
        await self.wait_for_clients(server, 1)

        payload = {"text": "x" * 4096}

        async def flood():
            for _ in range(500):
                await server.push("llm", payload)

        await asyncio.wait_for(flood(), 10)       # 不能挂住
        self.assertEqual(server.client_count, 1, "消费不过来也不该被踢掉")
        self.assertFalse(writer.is_closing())

    # ---- 接收 ----
    async def test_command_reaches_callback(self):
        server = self.make_server()
        seen = []
        server.on_command(lambda action, payload: seen.append((action, payload)))
        await server.start()

        _, writer = await self.connect()
        writer.write(encode_command(COMMAND_CHAT_INPUT, {"text": "开灯"}))
        await writer.drain()

        await _wait_until(lambda: seen, what="命令回调")
        self.assertEqual(seen, [(COMMAND_CHAT_INPUT, {"text": "开灯"})])
        self.assertEqual(server.received, 1)

    async def test_async_command_callback(self):
        server = self.make_server()
        seen = []

        async def handler(action, payload):
            await asyncio.sleep(0)
            seen.append((action, payload))

        server.on_command(handler)
        await server.start()

        _, writer = await self.connect()
        writer.write(encode_command(COMMAND_SWITCH_MODE, {"value": "STUDY"}))
        await writer.drain()
        await _wait_until(lambda: seen, what="异步命令回调")
        self.assertEqual(seen, [(COMMAND_SWITCH_MODE, {"value": "STUDY"})])

    async def test_bad_line_does_not_break_connection(self):
        server = self.make_server()
        seen = []
        server.on_command(lambda action, payload: seen.append(payload))
        await server.start()

        _, writer = await self.connect()
        writer.write(b"{not json}\n")
        writer.write(b"[]\n")
        writer.write("\n".encode())
        writer.write(encode_command(COMMAND_CHAT_INPUT, {"text": "还在"}))
        await writer.drain()

        await _wait_until(lambda: seen, what="坏消息之后的好消息")
        self.assertEqual(seen, [{"text": "还在"}])
        self.assertEqual(server.dropped, 3)
        self.assertEqual(server.client_count, 1, "坏消息不能断连接")

    async def test_gui_can_reconnect(self):
        server = self.make_server()
        await server.start()

        _, first = await self.connect()
        await self.wait_for_clients(server, 1)
        first.close()
        await self.wait_for_clients(server, 0)

        reader, _ = await self.connect()
        await self.wait_for_clients(server, 1)
        await server.push(TOPIC_STATUS, {"mode": "SLEEP"})
        topic, data = decode(await asyncio.wait_for(reader.readline(), 2))
        self.assertEqual(data["mode"], "SLEEP")

    async def test_stop_disconnects_clients(self):
        server = self.make_server()
        await server.start()
        reader, _ = await self.connect()
        await self.wait_for_clients(server, 1)

        await server.stop()
        try:
            rest = await asyncio.wait_for(reader.read(), 2)
        except (ConnectionResetError, BrokenPipeError):
            rest = b""
        self.assertEqual(rest, b"", "stop() 应该让 GUI 读到 EOF")
        self.assertEqual(server.client_count, 0)


async def _close_writer(writer):
    writer.close()
    try:
        await writer.wait_closed()
    except (ConnectionResetError, BrokenPipeError):
        pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
