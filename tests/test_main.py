#!/usr/bin/env python3
"""
tests/test_main.py — agent/main.py 的装配与生命周期单测

运行:
    python tests/test_main.py

覆盖:
  生命周期   start 顺序 = 依赖顺序; stop 顺序 = 严格反向; 重复 stop 安全
  异常隔离   单个组件启动失败 -> 其余照常启动, failures 有记录
  主循环     serve() 收到 stop_event 后退出; max_events 到了主动退出
  消息处理   handle_event 走通 LLM(规则兜底) 并返回回复; LLM 失败不影响服务
  日志       setup_logging 落盘 + 幂等 (不重复挂 handler)
  配置       配置读不到 -> 退出码 1; 配置不合法 -> 退出码 1

不测真实 native / 真实模型 (都不存在): 一律用 start_native=False + 注入配置。
"""

import asyncio
import atexit
import contextlib
import importlib
import logging
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.core.state_machine import State, StateMachine  # noqa: E402
from agent.io import _native as native_mod  # noqa: E402
from agent.ipc import (  # noqa: E402
    COMMAND_CHAT_INPUT,
    UNIX_SOCKET_SUPPORTED,
    LocalServer,
    NullServer,
    encode_command,
)
from agent.main import Runtime, run, setup_logging  # noqa: E402
from agent.net import LaunchResult, ServerInfo, SessionStart, SunshineError  # noqa: E402

#: 测试用的 socket 一律放在临时目录里, **绝不碰 /tmp/agent.sock** ——
#: 板端跑测试时真 Agent 可能正占着那个路径, 撞上去只会得到"另一个实例在监听"。
_IPC_TMP = tempfile.mkdtemp(prefix="agent-test-ipc-")
atexit.register(shutil.rmtree, _IPC_TMP, ignore_errors=True)
_IPC_SEQ = [0]


def _ipc_socket_path() -> str:
    """每次调用给一个**唯一**路径, 免得同进程里两个 Runtime 抢同一个文件。"""
    _IPC_SEQ[0] += 1
    return os.path.join(_IPC_TMP, "agent-%d.sock" % _IPC_SEQ[0])


#: 装配顺序 (与模块头的第 1..13 步一致; 只列会注册成组件的)
EXPECTED_ORDER = [
    "native",
    "chat_bus",
    "io",
    # T8-4: 音乐（music.enabled=false 时同样什么都不做, 组件照旧注册）。
    # ⚠ 它在 state_machine/tool_router **之前** —— 音乐四个工具建的时候就要知道
    #   "音乐开没开"（没开整个不装）, 所以 self.music 必须先就位（T8-5）。
    "music",
    # T11-6: B 站视频（同样: 没开时这一步什么都不做, 组件照旧注册）。
    # ⚠ 也在 tool_router **之前** —— `bilibili_search` 那条工具入口建的时候就要就位
    #   （没开就整个不装, 与音乐同一套）。
    "bilibili",
    "state_machine",
    "tool_router",
    # T7-4: 本机 llama-server 的启停（mode 不是 edge 或开关没开时这一步**什么都不做**,
    # 但组件照旧注册 —— 组件列表是"步骤"的清单, 不是"真跑了什么"的清单）
    "llm_service",
    "llm_provider",
    # T9-3: 用户画像（**不是工具**: 不进 TOOL_MODULES、模型看不到）。
    # 它在 llm_provider 之后 —— 判心情那一步要问模型; 步骤本身只读配置。
    "profile",
    "scheduler",
    "ipc",
    "terminal_input",
]


def quiet_config(**overrides):
    """一份不连网、不读 stdin、不落盘的最小配置。

    ipc.socket_path 指向临时目录: 每个 Runtime 一个独立 socket 文件。
    """
    cfg = {
        "llm": {"mode": "disabled"},
        "sunshine": {},
        "terminal": {"enabled": False},
        "scheduler": {"interval_min": 1},
        "ipc": {"socket_path": _ipc_socket_path()},
    }
    cfg.update(overrides)
    return cfg


def make_runtime(**kwargs):
    kwargs.setdefault("config", quiet_config())
    kwargs.setdefault("start_native", False)
    kwargs.setdefault("start_terminal", False)
    kwargs.setdefault("log", logging.getLogger("agent.test"))
    return Runtime(**kwargs)


class TestLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_start_registers_components_in_dependency_order(self):
        rt = make_runtime()
        await rt.start()
        try:
            names = [c.name for c in rt._components]
            # 跳过的组件 (native / terminal_input) 不注册 —— 它们没有需要收尾的资源。
            # 要验证的是**顺序**: 实际注册的必须是 EXPECTED_ORDER 的子序列。
            expected_present = [n for n in EXPECTED_ORDER if n in names]
            self.assertEqual(names, expected_present)
            self.assertIn("chat_bus", names)
            self.assertIn("scheduler", names)
            self.assertLess(names.index("chat_bus"), names.index("io"))
            self.assertLess(names.index("state_machine"), names.index("tool_router"))
            self.assertLess(names.index("tool_router"), names.index("llm_provider"))
            self.assertLess(names.index("llm_provider"), names.index("scheduler"))
        finally:
            await rt.stop()

    async def test_started_flag_reflects_success(self):
        rt = make_runtime()
        await rt.start()
        try:
            by_name = {c.name: c for c in rt._components}
            self.assertTrue(by_name["chat_bus"].started)
            self.assertTrue(by_name["state_machine"].started)
            self.assertTrue(by_name["llm_provider"].started)
            self.assertTrue(by_name["scheduler"].started)
        finally:
            await rt.stop()

    async def test_components_are_wired(self):
        rt = make_runtime()
        await rt.start()
        try:
            self.assertIsNotNone(rt.bus)
            self.assertIsNotNone(rt.state)
            self.assertIsNotNone(rt.tools)
            self.assertIsNotNone(rt.llm)
            self.assertIsNotNone(rt.scheduler)
            self.assertIsNotNone(rt.image_reader)
            self.assertIsNotNone(rt.input_sender)
            # scheduler 必须挂在同一个 bus 与 state 上, 否则命令/日程不生效
            self.assertIs(rt.scheduler._bus, rt.bus)
            self.assertIs(rt.scheduler._state, rt.state)
            self.assertIs(rt.tools._state_provider, rt.state)
            self.assertIs(rt.llm.tools, rt.tools)
        finally:
            await rt.stop()

    async def test_stop_runs_in_reverse_order(self):
        rt = make_runtime()
        await rt.start()

        calls = []

        async def spy(component):
            calls.append(component.name)

        for component in rt._components:
            component.do_stop = (lambda c: (lambda: spy(c)))(component)

        started_order = [c.name for c in rt._components]
        await rt.stop()
        self.assertEqual(calls, list(reversed(started_order)),
                         "stop 必须严格按 start 的反向顺序")

    async def test_stop_is_idempotent(self):
        rt = make_runtime()
        await rt.start()
        await rt.stop()
        await rt.stop()          # 第二次不该炸
        self.assertFalse(any(c.started for c in rt._components))

    async def test_stop_without_start_is_safe(self):
        rt = make_runtime()
        await rt.stop()

    async def test_stop_sets_stop_event(self):
        rt = make_runtime()
        await rt.start()
        self.assertFalse(rt.stop_event.is_set())
        await rt.stop()
        self.assertTrue(rt.stop_event.is_set())


class TestFailureIsolation(unittest.IsolatedAsyncioTestCase):
    """两层保护都要验:

       内层 _guarded()   —— 组件自己的 start 失败: 组件已注册, started=False
       外层 _guard_step() —— 步骤里组件之外的代码失败: 组件根本没注册

    只测其中一层的话, "单组件失败不影响其他组件"只成立一半。
    """

    async def test_inner_layer_marks_component_not_started(self):
        from agent.main import _Component

        rt = make_runtime()

        async def boom() -> None:
            raise RuntimeError("组件自己的 start 炸了")

        component = _Component("demo", boom)
        ok = await rt._guarded(component)

        self.assertFalse(ok)
        self.assertFalse(component.started, "失败的组件不该标成已启动")
        self.assertIsInstance(component.start_error, RuntimeError)
        self.assertIn(component, rt._components, "仍要注册, 这样 stop 时会走到它")
        self.assertEqual(rt.failures[0][0], "demo")
        self.assertIn("组件自己的 start 炸了", rt.failures[0][1])

    async def test_outer_layer_contains_step_level_bug(self):
        # 步骤里"组件之外"的代码出错 (例如配置解析 int("abc")) 也必须被挡住
        rt = make_runtime()

        async def boom() -> None:
            raise ValueError("配置解析炸了")

        rt._start_scheduler = boom
        await rt.start()
        try:
            names = {c.name: c for c in rt._components}
            self.assertNotIn("scheduler", names, "步骤死掉时组件不会被注册")
            # 其余步骤照常
            self.assertTrue(names["chat_bus"].started)
            self.assertTrue(names["state_machine"].started)
            self.assertTrue(names["llm_provider"].started)
            self.assertIsNotNone(rt.bus)
            self.assertIsNotNone(rt.llm)
            # 失败被记录, 且带上了步骤名
            self.assertEqual(len(rt.failures), 1)
            self.assertEqual(rt.failures[0][0], "scheduler")
            self.assertIn("配置解析炸了", rt.failures[0][1])
        finally:
            await rt.stop()

    async def test_failure_does_not_block_later_steps(self):
        # 中间某步失败, 后面的组件仍要起来
        rt = make_runtime()

        async def boom() -> None:
            raise RuntimeError("llm 起不来")

        rt._start_llm = boom
        await rt.start()
        try:
            names = {c.name: c for c in rt._components}
            self.assertNotIn("llm_provider", names)
            self.assertTrue(names["scheduler"].started, "llm 失败不该拦住 scheduler")
            self.assertIsNotNone(rt.scheduler)
        finally:
            await rt.stop()

    async def test_real_config_bug_is_contained(self):
        # 真实场景: sunshine.width 写成非数字 -> int() 抛错。不该让整个进程起不来
        rt = make_runtime(config=quiet_config(
            sunshine={"host": "127.0.0.1", "app": "Desktop", "width": "abc"},
        ), start_native=True)
        await rt.start()
        try:
            # native 步骤失败被记下, 其余组件照常
            self.assertTrue(any(name == "native" for name, _ in rt.failures))
            self.assertIsNotNone(rt.bus)
            self.assertIsNotNone(rt.llm)
            self.assertIsNotNone(rt.scheduler)
        finally:
            await rt.stop()

    async def test_stop_still_runs_cleanly_when_a_step_failed(self):
        rt = make_runtime()

        async def boom() -> None:
            raise RuntimeError("x")

        rt._start_state_and_tools = boom
        await rt.start()
        await rt.stop()          # 不该抛
        self.assertTrue(rt.failures)

    async def test_stop_error_does_not_stop_cleanup(self):
        rt = make_runtime()
        await rt.start()

        stopped = []

        async def bad_stop() -> None:
            raise RuntimeError("停止时炸了")

        for component in rt._components:
            if component.name == "scheduler":
                component.do_stop = bad_stop
            else:
                component.do_stop = (lambda c: (lambda: stopped.append(c.name)))(component)

        await rt.stop()          # 不该抛
        self.assertTrue(stopped, "一个组件停止失败不该让其余组件漏停")

    async def test_missing_tools_module_is_not_a_failure(self):
        # agent/tools/ **整个 import 不进来**时 (Phase 7 之前的处境):
        # 工具为空是预期状态, 不算启动失败。这里真的把那个 import 打断来钉住这条。
        real_import = importlib.import_module

        def fake_import(name, *args, **kwargs):
            if name == "agent.tools" or name.startswith("agent.tools."):
                raise ImportError("模拟 agent/tools 不存在")
            return real_import(name, *args, **kwargs)

        rt = make_runtime()
        with mock.patch("agent.main.importlib.import_module", side_effect=fake_import):
            await rt.start()
        try:
            self.assertEqual(rt.failures, [])
            self.assertEqual(len(rt.tools), 0)
        finally:
            await rt.stop()

    async def test_real_tools_module_registers_its_tools(self):
        # Phase 7 起 agent/tools/ 有真工具了: 启动时必须真的装进来。
        # 只断言"非空 + 认得 back_to_desktop", 数量留给 tests/test_tools.py 去钉。
        rt = make_runtime()
        await rt.start()
        try:
            self.assertEqual(rt.failures, [])
            self.assertGreaterEqual(len(rt.tools), 1)
            names = [t["name"] for t in rt.tools.list_tools()]
            self.assertIn("back_to_desktop", names)
        finally:
            await rt.stop()

    async def test_ipc_component_starts(self):
        # agent/ipc/ 现在有 build_ipc() 了: 它必须真的起来, 且不算失败
        rt = make_runtime()
        await rt.start()
        try:
            self.assertEqual(rt.failures, [])
            if UNIX_SOCKET_SUPPORTED:
                self.assertIsInstance(rt.ipc, LocalServer)
                self.assertTrue(rt.ipc.is_running)
                self.assertTrue(os.path.exists(rt.ipc.path))
                self.assertIn("ipc", [c.name for c in rt._components])
            else:
                # Windows: 明确降级成空实现, 而不是让启动报错
                self.assertIsInstance(rt.ipc, NullServer)
        finally:
            path = getattr(rt.ipc, "path", None)
            await rt.stop()
        if UNIX_SOCKET_SUPPORTED:
            self.assertFalse(os.path.exists(path), "stop() 要删掉 socket 文件")

    async def test_ipc_without_factory_is_not_a_failure(self):
        # 还没有 server 的状态 (agent/ipc 里只有 protocol.py) 只记 WARNING
        import agent.ipc as ipc_pkg

        saved = ipc_pkg.build_ipc
        del ipc_pkg.build_ipc
        try:
            rt = make_runtime()
            await rt.start()
            try:
                self.assertIsNone(rt.ipc)
                self.assertEqual(rt.failures, [])
            finally:
                await rt.stop()
        finally:
            ipc_pkg.build_ipc = saved

    async def test_gui_command_reaches_bus(self):
        # 端到端: GUI -> socket -> ipc -> bus (source="gui")
        if not UNIX_SOCKET_SUPPORTED:
            self.skipTest("需要 AF_UNIX")
        rt = make_runtime()
        await rt.start()
        try:
            reader, writer = await asyncio.open_unix_connection(rt.ipc.path)
            try:
                writer.write(encode_command(COMMAND_CHAT_INPUT, {"text": "给我讲个故事"}))
                await writer.drain()

                for _ in range(200):
                    if rt.bus.qsize():
                        break
                    await asyncio.sleep(0.01)
                event = rt.bus.get_nowait()
                self.assertIsNotNone(event, "GUI 的命令没有进 bus")
                self.assertEqual(event["source"], "gui")
                self.assertEqual(event["text"], "给我讲个故事")
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            await rt.stop()

    async def test_bad_llm_mode_degrades_but_starts(self):
        rt = make_runtime(config=quiet_config(llm={"mode": "banana"}))
        await rt.start()
        try:
            self.assertEqual(rt.llm.mode(), "disabled")
            self.assertEqual(rt.failures, [], "非法模式应降级, 不该算组件失败")
        finally:
            await rt.stop()

    async def test_board_alias_maps_to_edge(self):
        rt = make_runtime(config=quiet_config(llm={"mode": "board"}))
        await rt.start()
        try:
            self.assertEqual(rt.llm.mode(), "edge")
        finally:
            await rt.stop()


class TestServeLoop(unittest.IsolatedAsyncioTestCase):
    async def test_serve_exits_on_stop_event(self):
        rt = make_runtime()
        await rt.start()
        task = asyncio.create_task(rt.serve())
        await asyncio.sleep(0.02)
        rt.stop_event.set()
        await asyncio.wait_for(task, timeout=2.0)
        await rt.stop()

    async def test_serve_processes_bus_events(self):
        rt = make_runtime()
        await rt.start()
        task = asyncio.create_task(rt.serve())
        await asyncio.sleep(0.02)
        await rt.bus.push("terminal", "你好")
        for _ in range(100):
            if rt.stats["events"] >= 1:
                break
            await asyncio.sleep(0.01)
        rt.stop_event.set()
        await asyncio.wait_for(task, timeout=2.0)
        await rt.stop()
        self.assertEqual(rt.stats["events"], 1)
        self.assertEqual(rt.stats["replies"], 1)
        self.assertEqual(rt.stats["llm_errors"], 0)

    async def test_max_events_stops_the_loop(self):
        rt = make_runtime(max_events=2)
        await rt.start()
        task = asyncio.create_task(rt.serve())
        await asyncio.sleep(0.02)
        for i in range(5):
            await rt.bus.push("terminal", "消息%d" % i)
        await asyncio.wait_for(task, timeout=2.0)
        await rt.stop()
        self.assertEqual(rt.stats["events"], 2, "处理满 max_events 就该退出")

    async def test_serve_without_bus_raises(self):
        rt = make_runtime()
        with self.assertRaises(RuntimeError):
            await rt.serve()

    async def test_serve_exits_when_stop_event_already_set(self):
        rt = make_runtime()
        await rt.start()
        rt.stop_event.set()
        await asyncio.wait_for(rt.serve(), timeout=2.0)
        await rt.stop()


class TestHandleEvent(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.rt = make_runtime()
        await self.rt.start()

    async def asyncTearDown(self):
        await self.rt.stop()

    async def test_rule_engine_reply(self):
        reply = await self.rt.handle_event(
            {"source": "terminal", "text": "你好", "timestamp": 1.0}
        )
        self.assertIsInstance(reply, str)
        self.assertIn("简易模式", reply)

    async def test_time_reply(self):
        reply = await self.rt.handle_event(
            {"source": "gui", "text": "现在几点", "timestamp": 1.0}
        )
        self.assertIn("现在是", reply)

    async def test_unknown_input_gets_default_reply(self):
        reply = await self.rt.handle_event(
            {"source": "terminal", "text": "把窗帘关上", "timestamp": 1.0}
        )
        self.assertIn("听不懂", reply)

    async def test_stats_accumulate(self):
        for text in ("你好", "现在几点"):
            await self.rt.handle_event({"source": "terminal", "text": text, "timestamp": 1.0})
        self.assertEqual(self.rt.stats["events"], 2)
        self.assertEqual(self.rt.stats["replies"], 2)
        self.assertEqual(self.rt.stats["llm_errors"], 0)

    # ---- Phase 6 D4: 回复要能推出去 (IPC 用 on_reply 钩子接) ----

    async def test_reply_reaches_the_hook(self):
        seen = []

        def hook(text):
            seen.append(text)

        self.rt.on_reply = hook
        reply = await self.rt.handle_event(
            {"source": "gui", "text": "你好", "timestamp": 1.0}
        )
        self.assertEqual(seen, [reply], "算出来的回复必须原样交给钩子")

    async def test_async_reply_hook_is_awaited(self):
        seen = []

        async def hook(text):
            await asyncio.sleep(0)
            seen.append(text)

        self.rt.on_reply = hook
        await self.rt.handle_event({"source": "gui", "text": "你好", "timestamp": 1.0})
        self.assertEqual(len(seen), 1, "异步钩子要被 await, 而不是丢掉协程")

    async def test_broken_reply_hook_does_not_break_the_reply(self):
        def hook(text):
            raise RuntimeError("推送炸了")

        self.rt.on_reply = hook
        reply = await self.rt.handle_event(
            {"source": "gui", "text": "你好", "timestamp": 1.0}
        )
        self.assertIsInstance(reply, str, "钩子出错不该影响这条回复已经算成功")
        self.assertEqual(self.rt.stats["llm_errors"], 0)

    async def test_reply_hook_is_none_before_ipc_wires_it(self):
        # 默认 None: 没有 IPC 接线时行为与 D4 之前完全一样
        rt = make_runtime()
        self.assertIsNone(rt.on_reply, "IPC 还没接线, 不该有钩子")

    async def test_ipc_wires_the_reply_hook_on_start(self):
        # D4: rt.start() 会走到 _start_ipc(), 由 build_ipc(..., runtime=rt) 接上钩子
        # (Windows 上是 NullServer, 同样会接线 —— 只是 push 是空实现)
        self.assertTrue(callable(self.rt.on_reply),
                        "IPC 起来之后回复钩子必须有人接")

    async def test_failed_llm_does_not_fire_the_hook(self):
        seen = []

        class _FailingLLM:
            def __init__(self):
                self.tools = None

            async def chat_with_tools(self, text, context):
                return {"ok": False, "error": "模型炸了", "text": ""}

        self.rt.llm = _FailingLLM()
        self.rt.on_reply = seen.append
        await self.rt.handle_event({"source": "gui", "text": "x", "timestamp": 1.0})
        self.assertEqual(seen, [], "失败没有回复, 就不该推一条空的 llm 给 GUI")

    async def test_llm_failure_does_not_raise(self):
        class _BrokenLLM:
            def __init__(self):
                self.tools = None

            async def chat_with_tools(self, text, context):
                raise RuntimeError("模型炸了")

        self.rt.llm = _BrokenLLM()
        reply = await self.rt.handle_event(
            {"source": "terminal", "text": "你好", "timestamp": 1.0}
        )
        self.assertIsNone(reply)
        self.assertEqual(self.rt.stats["llm_errors"], 1)

    async def test_llm_ok_false_is_counted(self):
        class _FailingLLM:
            def __init__(self):
                self.tools = None

            async def chat_with_tools(self, text, context):
                return {"ok": False, "text": "", "tool_calls": [], "error": "timeout",
                        "mode": "cloud"}

        self.rt.llm = _FailingLLM()
        self.assertIsNone(
            await self.rt.handle_event({"source": "terminal", "text": "x", "timestamp": 1.0})
        )
        self.assertEqual(self.rt.stats["llm_errors"], 1)

    async def test_tool_calls_are_counted_and_logged(self):
        class _ToolLLM:
            def __init__(self):
                self.tools = None

            async def chat_with_tools(self, text, context):
                return {
                    "ok": True, "text": "done", "mode": "cloud", "error": None,
                    "tool_calls": [
                        {"name": "get_time", "args": {}, "result": {"ok": True, "result": "12:00"}},
                        {"name": "bad", "args": {}, "result": {"ok": False, "error": "x"}},
                    ],
                }

        self.rt.llm = _ToolLLM()
        reply = await self.rt.handle_event(
            {"source": "terminal", "text": "几点了", "timestamp": 1.0}
        )
        self.assertEqual(reply, "done")
        self.assertEqual(self.rt.stats["tool_calls"], 2)

    async def test_context_carries_state_and_source(self):
        seen = {}

        class _SpyLLM:
            def __init__(self):
                self.tools = None

            async def chat_with_tools(self, text, context):
                seen.update(context)
                return {"ok": True, "text": "ok", "tool_calls": [], "error": None,
                        "mode": "disabled"}

        self.rt.llm = _SpyLLM()
        await self.rt.handle_event({"source": "gui", "text": "hi", "timestamp": 1.0})
        self.assertEqual(seen["state"], "idle")
        self.assertEqual(seen["source"], "gui")
        self.assertIn("connected", seen)


class TestLogging(unittest.TestCase):
    """setup_logging 的行为。

    它改的是 agent 根 logger 的全局状态, 所以每个用例前后都把 handler 存/复原。
    ⚠ 用 mkdtemp + addCleanup 而不是 `with TemporaryDirectory()`: FileHandler 会
      一直占着文件, Windows 上必须**先 close handler 再删目录**, 否则报
      PermissionError (WinError 32)。
    """

    def setUp(self):
        self.root = logging.getLogger("agent")
        self.saved = list(self.root.handlers)
        self.had_flag = getattr(self.root, "_agent_configured", False)
        self.root.handlers.clear()
        if hasattr(self.root, "_agent_configured"):
            delattr(self.root, "_agent_configured")

        self.tmpdir = tempfile.mkdtemp(prefix="agentlog_")
        # 清理顺序: 先关 handler (释放文件句柄) 再删目录
        self.addCleanup(shutil.rmtree, self.tmpdir, True)

    def tearDown(self):
        for handler in self.root.handlers:
            try:
                handler.close()
            except Exception:  # noqa: BLE001
                pass
        self.root.handlers.clear()
        for handler in self.saved:
            self.root.addHandler(handler)
        if self.had_flag:
            self.root._agent_configured = True
        elif hasattr(self.root, "_agent_configured"):
            delattr(self.root, "_agent_configured")

    def _flush(self):
        for handler in self.root.handlers:
            handler.flush()

    def test_writes_to_file(self):
        path = Path(self.tmpdir) / "sub" / "agent.log"   # 父目录也该自动建
        setup_logging(str(path), console=False)
        logging.getLogger("agent.smoke").info("测试日志")
        self._flush()

        self.assertTrue(path.is_file(), "应当创建日志文件(含父目录)")
        self.assertIn("测试日志", path.read_text(encoding="utf-8"))

    def test_is_idempotent(self):
        setup_logging("", console=True)
        first = len(self.root.handlers)
        setup_logging("", console=True)
        self.assertEqual(len(self.root.handlers), first, "重复调用不该重复挂 handler")

    def test_log_path_can_be_disabled(self):
        setup_logging("", console=False)
        self.assertEqual(self.root.handlers, [], "log_path='' 时不该挂文件 handler")

    def test_unwritable_path_does_not_raise(self):
        # 日志落盘失败不该让程序起不来 (控制台还有一份)。
        # NUL 会让 Path.mkdir 抛 ValueError; 只 catch OSError 是接不住的。
        setup_logging("\0invalid\0/agent.log", console=False)   # 不该抛

    def test_unwritable_directory_does_not_raise(self):
        # 用一个"父路径是文件"的目录, 触发 OSError
        blocker = Path(self.tmpdir) / "blocker"
        blocker.write_text("x", encoding="utf-8")
        setup_logging(str(blocker / "agent.log"), console=False)  # 不该抛

    def test_format_includes_level_and_logger(self):
        path = Path(self.tmpdir) / "agent.log"
        setup_logging(str(path), console=False)
        logging.getLogger("agent.smoke").warning("注意")
        self._flush()
        line = path.read_text(encoding="utf-8").strip()
        self.assertIn("WARNING", line)
        self.assertIn("agent.smoke", line)
        self.assertIn("注意", line)


class TestRunEntry(unittest.IsolatedAsyncioTestCase):
    async def test_run_returns_zero_and_stops_cleanly(self):
        stop_event = asyncio.Event()
        logger = logging.getLogger("agent.test.run")

        async def stop_soon():
            await asyncio.sleep(0.05)
            stop_event.set()

        asyncio.create_task(stop_soon())
        code = await run(
            config=quiet_config(),
            log=logger,
            start_terminal=False,
            start_native=False,
            stop_event=stop_event,
        )
        self.assertEqual(code, 0)

    async def test_run_exits_on_run_seconds(self):
        code = await run(
            config=quiet_config(),
            log=logging.getLogger("agent.test.run2"),
            run_seconds=0.05,
            start_terminal=False,
            start_native=False,
        )
        self.assertEqual(code, 0)

    async def test_run_without_config_file_returns_1(self):
        import os
        from agent import config as cfg

        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get(cfg.CONFIG_DIR_ENV)
            os.environ[cfg.CONFIG_DIR_ENV] = tmp
            cfg.clear_cache()
            try:
                code = await run(
                    config=None,
                    log=logging.getLogger("agent.test.run3"),
                    start_terminal=False,
                    start_native=False,
                )
                self.assertEqual(code, 1, "找不到配置应当明确返回 1")
            finally:
                cfg.clear_cache()
                if old is None:
                    os.environ.pop(cfg.CONFIG_DIR_ENV, None)
                else:
                    os.environ[cfg.CONFIG_DIR_ENV] = old

    async def test_run_with_invalid_config_returns_1(self):
        import os
        from agent import config as cfg

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "config.yaml").write_text("llm:\n  mode: [unclosed\n",
                                                   encoding="utf-8")
            old = os.environ.get(cfg.CONFIG_DIR_ENV)
            os.environ[cfg.CONFIG_DIR_ENV] = tmp
            cfg.clear_cache()
            try:
                code = await run(
                    config=None,
                    log=logging.getLogger("agent.test.run4"),
                    start_terminal=False,
                    start_native=False,
                )
                self.assertEqual(code, 1, "配置非法应当明确返回 1")
            finally:
                cfg.clear_cache()
                if old is None:
                    os.environ.pop(cfg.CONFIG_DIR_ENV, None)
                else:
                    os.environ[cfg.CONFIG_DIR_ENV] = old


class TestNativeSunshineHandshake(unittest.IsolatedAsyncioTestCase):
    """native 那一步的两段式: Python 做 HTTPS 握手, native 只接字段 (Phase 6 B1)。

    真实网络与真实 native 都替掉 —— 这里验的是**接线**:
    握手结果有没有原样递到 start_with_session, 以及失败时话有没有说对地方。
    """

    def setUp(self):
        native_mod.reset_native()

    def tearDown(self):
        native_mod.reset_native()

    def _fake_native(self, start_ok=True, error=""):
        fake = mock.MagicMock()
        fake.moonlight.launch_url_query_parameters.return_value = "&corever=1"
        fake.moonlight.start_with_session.return_value = start_ok
        fake.moonlight.status.return_value = {"error": error}
        native_mod.set_native(fake)
        return fake

    @staticmethod
    def _fake_client(endpoint="resume", session_url="rtsp://192.168.137.1:48010"):
        client = mock.MagicMock()
        client.server_info.return_value = ServerInfo(
            hostname="Anorak_Host",
            app_version="7.1.431.-1",
            gfe_version="3.23.0.74",
            codec_mode_support=0x1F0301,
            pair_status=1,
        )
        client.resolve_app_id.return_value = "881448767"
        client.start_session.return_value = SessionStart(
            LaunchResult(session_url=session_url, status_code=200), endpoint
        )
        return client

    async def _run_with(self, fake_client, fake_native, **sunshine):
        cfg = {"host": "192.168.137.1", "app": "Desktop"}
        cfg.update(sunshine)
        ctor = mock.patch("agent.net.SunshineClient", return_value=fake_client)
        with ctor as patched:
            rt = make_runtime(
                config=quiet_config(sunshine=cfg), start_native=True
            )
            await rt.start()
            try:
                return rt, patched
            finally:
                await rt.stop()

    async def test_handshake_result_is_handed_to_native(self):
        fake_native = self._fake_native()
        rt, patched = await self._run_with(self._fake_client(), fake_native)

        self.assertEqual(rt.failures, [], "native 这步不该失败")

        # 1) 参数原样过去: host/app/尺寸 + 握手拿到的三个字段 + sessionUrl
        args = fake_native.moonlight.start_with_session.call_args[0]
        self.assertEqual(args[:5], ("192.168.137.1", "Desktop", 1280, 720, 60))
        self.assertEqual(
            args[5:],
            ("7.1.431.-1", "3.23.0.74", 0x1F0301, "rtsp://192.168.137.1:48010"),
        )

        # 2) 客户端是按 sunshine.* 配出来的, 且扩展参数取自 native (不抄进 Python)
        client_kwargs = patched.call_args[1]
        self.assertEqual(patched.call_args[0], ("192.168.137.1", 47984))
        self.assertEqual(client_kwargs["launch_extra_query"], "&corever=1")
        self.assertIsNone(client_kwargs["cert"], "没配 cert 就传 None, 不用空串")

    async def test_https_port_can_be_overridden_by_config(self):
        fake_native = self._fake_native()
        _, patched = await self._run_with(
            self._fake_client(), fake_native, https_port=47985,
            cert="/tmp/client.pem", key="/tmp/client.key",
        )
        self.assertEqual(patched.call_args[0], ("192.168.137.1", 47985))
        self.assertEqual(patched.call_args[1]["cert"], "/tmp/client.pem")
        self.assertEqual(patched.call_args[1]["key"], "/tmp/client.key")

    async def test_handshake_failure_is_contained_and_never_reaches_native(self):
        fake_native = self._fake_native()
        fake_client = mock.MagicMock()
        fake_client.server_info.side_effect = SunshineError(
            "HTTPS 401: 客户端证书未被主机授权"
        )

        rt, _ = await self._run_with(fake_client, fake_native)

        # native 这步失败被记下, 但不能拦住后面的组件 (native 只是可选增强)
        failed = [name for name, _ in rt.failures]
        self.assertIn("native", failed)
        self.assertIsNotNone(rt.bus, "native 失败不该影响其他组件")

        # 握手都没成就绝不能去动 native (否则会把"会话被占"这类真原因盖掉)
        fake_native.moonlight.start_with_session.assert_not_called()

        detail = [d for name, d in rt.failures if name == "native"][0]
        self.assertIn("401", str(detail), "要把主机的原话带出来")

    async def test_native_failure_does_not_blame_the_handshake(self):
        # 握手成功、native 失败 -> 提示必须指向 native, 不能再叫人去查证书
        fake_native = self._fake_native(start_ok=False, error="LiStartConnection failed, rc=-1")

        rt, _ = await self._run_with(self._fake_client(), fake_native)

        detail = str([d for name, d in rt.failures if name == "native"][0])
        self.assertIn("LiStartConnection failed", detail)
        self.assertIn("握手本身是成功的", detail)
        self.assertNotIn("证书", detail, "握手已经成功了, 不该再让人去查证书")

    async def test_resume_endpoint_is_used_when_the_host_is_busy(self):
        # 主机上挂着别的客户端时 start_session 会给 endpoint=resume;
        # 这里确认 sessionUrl 照样被送到 native (共存路径不能只在单测里通)
        fake_native = self._fake_native()
        await self._run_with(
            self._fake_client(endpoint="resume", session_url="rtsp://127.0.0.1:48010"),
            fake_native,
        )
        args = fake_native.moonlight.start_with_session.call_args[0]
        self.assertEqual(args[-1], "rtsp://127.0.0.1:48010")


class TestStateRelease(unittest.IsolatedAsyncioTestCase):
    """T12-1: **离开一个模式要先把那个模式的东西放掉**, 再去下一个状态。

    你定的规矩: 从 GAME/STUDY 去 SLEEP 不是一跳, 而是 `GAME -> IDLE -> SLEEP`;
    所以"释 GAME（视频 + 队列 + SigLIP）→ 释 IDLE（现在没有专属资源）→ 进 SLEEP
    （停 llama-server）"这个次序必须**按步**发生。这里的假件只记"谁在什么时候被叫到"。
    """

    def _runtime(self):
        rt = make_runtime()
        self.events = []

        class _Service:
            def start(self_inner):
                self.events.append("llama:start")
                return (True, "起")

            def stop(self_inner):
                self.events.append("llama:stop")
                return (True, "停")

        class _Watcher:
            def __init__(self_inner):
                self_inner.model_loaded = True

            def unload(self_inner):
                self_inner.model_loaded = False
                self.events.append("siglip:unload")

        class _Queue:
            def clear(self_inner):
                self.events.append("queue:clear")
                return {"ok": True, "why": "清空了", "count": 0, "index": 0, "current": None}

        rt.llm_service = _Service()
        rt._game_watch = _Watcher()
        rt.bilibili = _Queue()

        def stop_buffer():
            self.events.append("video:stop")
            rt._bilibili_stream = ""

        rt._stop_buffer = stop_buffer
        rt._push_bilibili = lambda **extra: self.events.append("push:bilibili") or True
        # 起停服务本来走后台线程: 这里同步跑, 好断言次序
        rt._run_service_op = lambda op, what: op()
        # 状态机单独造一个（`make_runtime()` 不启动组件, 状态机是 start 时才建的）,
        # 但挂的是 **Runtime 真那个** 回调 —— 要验的就是它的次序。
        machine = StateMachine()
        machine.on_change(rt._on_state_change)
        return rt, machine

    async def test_game_to_sleep_releases_in_order(self):
        rt, machine = self._runtime()
        self.assertTrue(machine.transition(State.GAME, "先玩"))

        out = machine.transition_to(State.SLEEP, "日程: 该睡了")
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.events, [
            "video:stop",        # ① 离开 GAME: 先停视频
            "queue:clear",       # ② 再把队列/封面清掉
            "push:bilibili",     # ③ 推给 GUI（空队列 + 空 stream = 回占位）
            "siglip:unload",     # ④ 卸载常驻模型
            "llama:stop",        # ⑤ 最后才进 SLEEP 停服务（IDLE 那一跳没有专属资源）
        ])
        self.assertIs(machine.current(), State.SLEEP)

    async def test_study_to_sleep_only_unloads_the_model(self):
        rt, machine = self._runtime()
        self.assertTrue(machine.transition(State.STUDY, "学习"))
        self.events.clear()
        self.assertTrue(machine.transition_to(State.SLEEP, "睡")["ok"])
        self.assertEqual(self.events, ["siglip:unload", "llama:stop"])
    async def test_leaving_sleep_starts_the_service_again(self):
        rt, machine = self._runtime()
        self.assertTrue(machine.transition(State.SLEEP, "睡"))
        self.assertEqual(self.events, ["llama:stop"])
        self.events.clear()
        self.assertTrue(machine.transition_to(State.STUDY, "醒了")["ok"])
        self.assertEqual(self.events, ["llama:start"], "离开 SLEEP = 把服务起回来（T7-4）")

    async def test_idle_release_is_a_noop_but_does_not_crash(self):
        rt, machine = self._runtime()
        self.assertTrue(machine.transition_to(State.GAME, "玩")["ok"])
        self.events.clear()
        self.assertTrue(machine.transition_to(State.STUDY, "换学习")["ok"])   # 经 IDLE
        # GAME 的释放照跑; IDLE 那一跳现在没有专属资源（什么都不加）
        self.assertEqual(self.events, ["video:stop", "queue:clear", "push:bilibili",
                                       "siglip:unload"])

    async def test_without_a_watcher_or_queue_nothing_breaks(self):
        rt, machine = self._runtime()
        rt._game_watch = None
        rt.bilibili = None
        self.assertTrue(machine.transition(State.GAME, "玩"))
        self.events.clear()
        out = machine.transition_to(State.SLEEP, "睡")
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.events, ["video:stop", "llama:stop"],
                         "没开 B 站时不推空载荷（队列本来就不存在）")

    async def test_the_hook_is_wired_by_the_runtime_itself(self):
        """T12-7: 释放钩子必须**由 Runtime 自己**挂上（不能靠测试手挂）。

        板端验收（`tests/board/t12_accept.py`）第一次跑就抓到: 这个钩子原来挂在
        `_start_llm_service()` 里 —— 于是 `llm.mode=disabled` / `manage_service=false`
        时状态照样跳两跳, 但"离开 GAME 停视频/清队列/卸 SigLIP"**一次都没发生**。
        上面那几条用例都是自己 `machine.on_change(rt._on_state_change)` 挂的, 所以全都
        是绿的 —— 这条专门盯"装配"这一步。
        """
        rt = make_runtime()
        await rt._start_state_and_tools()
        self.assertEqual(rt.state.callback_count, 1,
                         "建状态机时就该把释放钩子挂上（恰好一次）")

        calls = []
        rt._release_state = lambda state: calls.append(("release", state.value))
        rt._enter_state = lambda state: calls.append(("enter", state.value))
        self.assertTrue(rt.state.transition(State.GAME, "玩"))
        rt.state.transition_to(State.SLEEP, "睡")
        self.assertEqual(calls, [("release", "idle"), ("enter", "game"),
                                 ("release", "game"), ("enter", "idle"),
                                 ("release", "idle"), ("enter", "sleep")],
                         "每一次状态变化都要走「先释放、再进入」")

    async def test_llm_service_does_not_wire_the_hook_again(self):
        """T12-7: `_start_llm_service()` 不许再挂一遍（那会**每次状态变化跑两遍释放**）。"""
        rt = make_runtime()
        await rt._start_state_and_tools()
        before = rt.state.callback_count
        await rt._start_llm_service()
        self.assertEqual(rt.state.callback_count, before,
                         "挂第二遍 = 释放清单跑两次（卸载/清队列都会重复）")


class TestBilibiliWiring(unittest.IsolatedAsyncioTestCase):
    """T11-6: B 站接进 Runtime 的那几条线（**全程离线**: 队列/缓冲都用替身）。

    钉的是三条你定的规矩:
      ① 队列内容只有两个来源（对话关键词 / 画面认出的游戏）;
      ② **播放只由 GUI 操作触发**（`bilibili_control`）, 工具那条路只搜不播;
      ③ 只有**心情**…（不对）—— 只有 **GAME** 模式才抓画面认游戏, 且**有对话关键词就不抓**。
    """

    class _Queue:
        """假队列: 记下每次调用, 回一个像真队列那样的结果。"""

        def __init__(self):
            self.calls = []
            self.keyword = ""
            self.source = ""
            self.current_item = {"bvid": "BV1", "title": "标题"}
            self.index = 0
            #: T11-7: GUI 预览栏靠 `bilibili_state()["queue"]` 渲染 —— 假的也要有
            self.items = [{"bvid": "BV1", "title": "标题"},
                          {"bvid": "BV2", "title": "第二条"}]

        def target(self):
            return 18

        def state(self):
            return {"keyword": self.keyword, "source": self.source, "count": 18,
                    "index": self.index, "target": 18, "viewport": 6, "num_pages": 50,
                    "first_page": 1, "last_page": 1, "cached": 20, "at_head": False,
                    "at_tail": False, "why": "", "current": self.current_item}

        def search(self, keyword, source="dialogue", order="totalrank"):
            self.calls.append(("search", keyword, source))
            self.keyword, self.source = keyword, source
            return {"ok": True, "why": "", "count": 18, "index": 0, "keyword": keyword,
                    "source": source, "current": self.current_item}

        def move(self, delta):
            self.calls.append(("move", delta))
            return {"ok": True, "why": "", "index": 0, "count": 18, "current": self.current_item}

        def pick(self, index):
            self.calls.append(("pick", index))
            return {"ok": True, "why": "", "index": 0, "count": 18, "current": self.current_item}

        def set_viewport(self, visible):
            self.calls.append(("viewport", visible))
            return 8

        def clear(self):
            self.calls.append(("clear",))
            self.keyword = ""
            return {"ok": True, "why": "清空了", "count": 0, "index": 0, "current": None}

        def current(self):
            return dict(self.current_item)

    class _Buffer:
        """假缓冲: 只给 Runtime 真正问到的那几个口子。

        ⚠ T11-10e: `finished()` 是**我们自己的真值**（ffmpeg 正常收尾 = 整条片拉完了），
          自动下一集必须问它 —— 播放器报的 `eof` 在"我们主动收流（换条/清空）"时也会来一次。
        """

        def __init__(self, finished=True, playing=True, saw_playing=True):
            self._finished = bool(finished)
            self.state = "ended" if finished else "serving"
            self.playing_calls = []
            self.playing = bool(playing)
            self.saw_playing = bool(saw_playing)

        def set_playing(self, playing):
            self.playing_calls.append(bool(playing))
            self.playing = bool(playing)

        def finished(self):
            return self._finished

        def snapshot(self):
            return {"state": self.state, "playing": self.playing,
                    "saw_playing": self.saw_playing, "bvid": "BV1", "title": "标题"}

    def _runtime(self, *, gui=True):
        rt = make_runtime()
        rt.bilibili = self._Queue()
        rt._bilibili_api = type("Api", (), {"cookie_present": False})()
        rt._push_bilibili = lambda **extra: True          # 推送单独测, 这里不掺和
        #: T11-10f: 播放控制要 GUI 去执行 —— 默认"有一个客户端连上"
        #: T11-10f: 播放控制要 GUI 去执行 —— 默认"有一个客户端连上"。
        #: ⚠ 属性名是 `client_count`（LocalServer 的真实名字）—— 板端验收抓到过
        #:   写成 `clients` 时 getattr 的默认值把"有 GUI"悄悄吃成 False。
        rt.ipc = type("Ipc", (), {"client_count": 1 if gui else 0})()
        return rt

    async def test_the_gui_gate_reads_the_real_client_counter(self):
        """⚠ T11-10f 板端验收抓到的坑: 我一开始写的是 `ipc.clients`。

        `clients` 这个属性**不存在** —— `getattr(..., 0)` 的默认值把它悄悄吃成 0,
        于是"有 GUI 连上"永远为假、播放控制永远被拒（CLI 报"没有 GUI 连上"），
        而单测因为假 ipc 也写成 `clients` 全绿。这条把"假的和真的同名"钉住。
        """
        from agent.ipc.local_server import NullServer

        self.assertTrue(hasattr(NullServer(), "client_count"),
                        "真 server 的客户端计数器叫 client_count")
        fake = self._runtime().ipc
        self.assertTrue(hasattr(fake, "client_count"), "假 ipc 必须与真 server 同名")
        self.assertFalse(hasattr(fake, "clients"), "`clients` 这个属性从来不存在")

        rt = self._runtime()
        rt.ipc = NullServer()                     # 真 server（没人连）
        rt._buffer = self._Buffer()
        out = rt.bilibili_control("control", {"action": "pause"})
        self.assertFalse(out["ok"])
        self.assertIn("没有 GUI 连上", out["tell_user"])

    async def test_search_fills_the_queue_from_dialogue(self):
        rt = self._runtime()
        out = rt.bilibili_search("Luna say maybe")
        self.assertTrue(out["ok"])
        self.assertEqual(out["count"], 18)
        self.assertEqual(rt.bilibili.calls, [("search", "Luna say maybe", "dialogue")])
        self.assertEqual(rt.bilibili.source, "dialogue")

    async def test_search_without_the_client_is_honest(self):
        rt = make_runtime()
        out = rt.bilibili_search("x")
        self.assertFalse(out["ok"])
        self.assertIn("bilibili", out["tell_user"])

    async def test_next_prev_and_pick_start_playback(self):
        rt = self._runtime()
        played = []
        rt._bilibili_play = lambda: played.append("play") or {"ok": True}
        self.assertTrue(rt.bilibili_control("next")["ok"])
        self.assertTrue(rt.bilibili_control("prev")["ok"])
        self.assertTrue(rt.bilibili_control("pick", {"index": 3})["ok"])
        self.assertEqual(played, ["play", "play", "play"], "按钮按下就该开始放那一集")
        self.assertEqual(rt.bilibili.calls, [("move", 1), ("move", -1), ("pick", 3)])

    async def test_viewport_and_video_state_do_not_play(self):
        rt = self._runtime()
        played = []
        rt._bilibili_play = lambda: played.append("play") or {"ok": True}
        self.assertTrue(rt.bilibili_control("viewport", {"visible": 8})["ok"])
        self.assertEqual(rt.bilibili_control("viewport", {"visible": 8})["target"], 18)
        self.assertTrue(rt.bilibili_control("video_state",
                                            {"position_s": 3.0, "playing": True})["ok"])
        self.assertEqual(played, [], "上报格数/回报进度**不该**开始播放（你定的）")

    async def test_eof_starts_the_next_one(self):
        rt = self._runtime()
        rt._buffer = self._Buffer(finished=True)        # 缓冲说"真放完了"
        played = []
        rt._bilibili_play = lambda: played.append("play") or {"ok": True}
        out = rt.bilibili_control("video_state", {"eof": True, "playing": False})
        self.assertTrue(out["ok"])
        self.assertTrue(out["eof"])
        self.assertEqual(played, ["play"], "真放完了就自动下一集")
        self.assertEqual(rt.bilibili.calls, [("move", 1)])
        self.assertEqual(rt._buffer.playing_calls, [False])

    async def test_eof_without_a_finished_buffer_does_not_advance(self):
        """⚠ T11-10e 板端验收抓到的连跳: 播放器报 eof, 但我们自己的真值没到"放完" -> **不跳集**。

        换条/清空时我们主动把流收掉, 播放器照样收到一次干净的 EOS —— 照单全收就会
        "换条 -> 报 eof -> 自动下一集 -> 又收流 -> 又报 eof" 一路连跳（板端实测 6 条）。
        """
        rt = self._runtime()
        rt._buffer = self._Buffer(finished=False)       # 还在喂 / 被我们收工了
        played = []
        rt._bilibili_play = lambda: played.append("play") or {"ok": True}
        out = rt.bilibili_control("video_state", {"eof": True, "playing": False})
        self.assertTrue(out["ok"], "这不是错误, 只是不当成放完")
        self.assertTrue(out.get("ignored"))
        self.assertEqual(played, [], "缓冲没到 ended 就不该自动下一集")
        self.assertEqual(rt.bilibili.calls, [], "队列也不该动")
        self.assertEqual(rt._buffer.playing_calls, [False], "暂停/播放还是要如实记到缓冲上")

    async def test_eof_without_a_buffer_at_all_is_ignored(self):
        rt = self._runtime()
        rt._buffer = None
        rt._bilibili_play = lambda: (_ for _ in ()).throw(AssertionError("不该播"))
        out = rt.bilibili_control("video_state", {"eof": True})
        self.assertTrue(out.get("ignored"))
        self.assertEqual(rt.bilibili.calls, [])

    async def test_eof_at_the_end_is_quiet(self):
        rt = self._runtime()
        rt._buffer = self._Buffer(finished=True)
        rt.bilibili.move = lambda delta: {"ok": False, "why": "后面没有了"}
        rt._bilibili_play = lambda: (_ for _ in ()).throw(AssertionError("不该播"))
        out = rt.bilibili_control("video_state", {"eof": True})
        self.assertTrue(out["ok"], "放完最后一集不是错误")
        self.assertIn("后面没有了", out["note"])

    async def test_unknown_action_is_rejected(self):
        rt = self._runtime()
        self.assertFalse(rt.bilibili_control("dance")["ok"])

    # ---------------------------------------------------------------- T11-10f
    #  "让播放器播放/暂停"（CLI 的 `assistant video play|pause|toggle` 走这条）。
    #  画面在 GUI 的播放器里，所以 Agent 只能**推一条 control 下去**；这几条钉的是:
    #  该拒的拒（没在播 / 没 GUI / 不认识的 action）、该推的推、toggle 用我们自己的真值解析。

    async def test_control_is_refused_without_a_playing_video(self):
        rt = self._runtime()
        rt._buffer = None
        out = rt.bilibili_control("control", {"action": "pause"})
        self.assertFalse(out["ok"])
        self.assertIn("没有在播的视频", out["tell_user"])

    async def test_control_is_refused_without_a_gui(self):
        rt = self._runtime(gui=False)
        rt._buffer = self._Buffer()
        out = rt.bilibili_control("control", {"action": "pause"})
        self.assertFalse(out["ok"], "没 GUI 就是没人按那颗按钮 —— 不能假装成功")
        self.assertIn("没有 GUI 连上", out["tell_user"])
        self.assertEqual(rt._buffer.playing_calls, [], "拒绝了就别动缓冲")

    async def test_an_unknown_control_is_refused(self):
        rt = self._runtime()
        rt._buffer = self._Buffer()
        out = rt.bilibili_control("control", {"action": "rewind"})
        self.assertFalse(out["ok"])
        self.assertIn("play / pause / toggle", out["tell_user"])

    async def test_pause_pushes_the_control_and_flips_the_buffer(self):
        rt = self._runtime()
        rt._buffer = self._Buffer(playing=True, saw_playing=True)
        pushed = []
        rt._push_bilibili = lambda **extra: pushed.append(extra) or True

        out = rt.bilibili_control("control", {"action": "pause"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["control"], "pause")
        self.assertEqual(rt._buffer.playing_calls, [False], "预取封顶要立刻跟着变")
        self.assertEqual(len(pushed), 1)
        control = pushed[0].get("control") or {}
        self.assertEqual(control.get("action"), "pause")
        self.assertIsInstance(control.get("seq"), int)
        self.assertGreaterEqual(control["seq"], 1)
        # 还欠一条"播放器回报"：GUI 的下一句 video_state 一到就补上（见下面那条）
        self.assertEqual(rt._bilibili_pending_control, control)

    async def test_play_and_seq_increases(self):
        rt = self._runtime()
        rt._buffer = self._Buffer(playing=False, saw_playing=True)
        pushed = []
        rt._push_bilibili = lambda **extra: pushed.append(extra) or True
        rt.bilibili_control("control", {"action": "play"})
        rt.bilibili_control("control", {"action": "pause"})
        seqs = [p["control"]["seq"] for p in pushed]
        self.assertEqual([p["control"]["action"] for p in pushed], ["play", "pause"])
        self.assertEqual(seqs, sorted(set(seqs)), "seq 必须单调递增（GUI 靠它去重）")
        self.assertLess(seqs[0], seqs[1])

    async def test_toggle_uses_our_own_truth(self):
        rt = self._runtime()
        pushed = []
        rt._push_bilibili = lambda **extra: pushed.append(extra) or True
        # 真在放 -> toggle 应该解析成 pause
        rt._buffer = self._Buffer(playing=True, saw_playing=True)
        self.assertEqual(rt.bilibili_control("control", {"action": "toggle"})["control"],
                         "pause")
        # 还是暂停 -> 解析成 play
        rt._buffer = self._Buffer(playing=False, saw_playing=True)
        self.assertEqual(rt.bilibili_control("control", {"action": "toggle"})["control"], "play")
        # ⚠ 还没真播过（`playing` 默认就是 True）-> 不能当成"在放" -> play
        rt._buffer = self._Buffer(playing=True, saw_playing=False)
        self.assertEqual(rt.bilibili_control("control", {"action": "toggle"})["control"], "play")

    async def test_the_players_report_becomes_a_control_result(self):
        rt = self._runtime()
        rt._buffer = self._Buffer(playing=True, saw_playing=True)
        pushed = []
        rt._push_bilibili = lambda **extra: pushed.append(extra) or True
        rt.bilibili_control("control", {"action": "pause"})
        pushed.clear()

        # GUI 的下一句回报（这才是"播放器真的按了"）-> 转手推给所有客户端
        out = rt.bilibili_control("video_state", {"position_s": 1.0, "playing": False})
        self.assertTrue(out["ok"])
        self.assertIsNone(rt._bilibili_pending_control, "回报过了就不该再欠着")
        self.assertEqual(len(pushed), 1)
        result = pushed[0].get("control_result") or {}
        self.assertEqual(result.get("action"), "pause")
        self.assertEqual(result.get("playing"), False)
        self.assertEqual(result.get("seq"), 1)

        # 后面再来几句普通回报 -> 不会没完没了地推
        rt.bilibili_control("video_state", {"position_s": 2.0, "playing": False})
        self.assertEqual(len(pushed), 1)

    async def test_the_reconnect_push_carries_no_control(self):
        """补推**不带** `control`/`control_result` —— 否则刚连上的客户端会重放旧命令。"""
        rt = self._runtime()
        rt._buffer = self._Buffer(playing=True, saw_playing=True)
        state = rt.bilibili_state()
        self.assertNotIn("control", state)
        self.assertNotIn("control_result", state)
        self.assertTrue(state["buffer"]["playing"])       # 但"在不在放"要在（CLI 的幂等靠它）

    async def test_state_carries_the_queue_the_preview_bar_renders(self):
        """⚠ T11-7 补的漏项: 载荷里**必须有 `queue`**, 否则 GUI 预览栏是空的。"""
        rt = self._runtime()
        state = rt.bilibili_state()
        self.assertTrue(state["ok"])
        self.assertEqual([item["bvid"] for item in state["queue"]], ["BV1", "BV2"])
        self.assertEqual(state["current"]["bvid"], "BV1")
        self.assertEqual(state["index"], 0)

    async def test_without_bilibili_the_commands_say_why(self):
        rt = make_runtime()
        out = rt.bilibili_control("next")
        self.assertFalse(out["ok"])
        self.assertIn("bilibili", out["tell_user"])

    async def test_play_without_a_queue_is_honest(self):
        rt = self._runtime()
        rt.bilibili.current = lambda: None
        out = rt._bilibili_play()
        self.assertFalse(out["ok"])
        self.assertIn("队列是空的", out["tell_user"])

    async def test_quality_note_mentions_the_missing_cookie(self):
        rt = self._runtime()
        rt._buffer = type("Buf", (), {"stream": {"quality": 16, "quality_label": "360P"}})()
        note = rt._bilibili_quality_note()
        self.assertIn("360P", note)
        self.assertIn("bilibili_cookie.json", note)

    async def test_quality_note_is_quiet_for_1080p(self):
        rt = self._runtime()
        rt._buffer = type("Buf", (), {"stream": {"quality": 80, "quality_label": "1080P"}})()
        self.assertEqual(rt._bilibili_quality_note(), "")

    async def test_watch_loop_skips_when_not_in_game(self):
        """观察循环只在 GAME 里抓帧 —— 别的状态连帧都不读（你定的）。"""
        rt = self._runtime()
        rt.state = type("S", (), {"current": lambda self: type("C", (), {"value": "study"})()})()
        reads = []

        class _Reader:
            async def read_latest(self):
                reads.append(1)
                return "frame"

        rt.image_reader = _Reader()

        class _Watch:
            def __init__(self):
                self.ticks = []

            def ensure_model(self, state):
                self.ticks.append(("ensure", state))
                return True

            def tick(self, frame, **kwargs):
                self.ticks.append(("tick", frame))
                return {"game": "hoi4", "skipped": ""}

            def notes(self):
                return []

        rt._game_watch = _Watch()
        task = asyncio.ensure_future(rt._game_watch_loop())
        await asyncio.sleep(rt._GAME_WATCH_TICK_S + 0.6)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self.assertEqual(reads, [], "STUDY 里不该读画面")
        self.assertTrue(any(item[0] == "ensure" for item in rt._game_watch.ticks),
                        "模型常驻/卸载那条策略每轮都要走")

    async def test_watch_loop_does_not_capture_with_a_dialogue_keyword(self):
        rt = self._runtime()
        rt.bilibili.keyword = "luna"
        rt.bilibili.source = "dialogue"
        rt.state = type("S", (), {"current": lambda self: type("C", (), {"value": "game"})()})()
        reads = []

        class _Reader:
            async def read_latest(self):
                reads.append(1)
                return "frame"

        rt.image_reader = _Reader()

        class _Watch:
            def ensure_model(self, state):
                return True

            def tick(self, frame, **kwargs):
                raise AssertionError("有对话关键词时一帧都不该抓")

            def notes(self):
                return []

        rt._game_watch = _Watch()
        task = asyncio.ensure_future(rt._game_watch_loop())
        await asyncio.sleep(rt._GAME_WATCH_TICK_S + 0.6)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self.assertEqual(reads, [], "有对话关键词 -> 不抓帧（你定的）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
