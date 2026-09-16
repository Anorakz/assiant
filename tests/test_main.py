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
import logging
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.main import Runtime, run, setup_logging  # noqa: E402

#: 装配顺序 (与模块头的第 1..11 步一致; 只列会注册成组件的)
EXPECTED_ORDER = [
    "native",
    "chat_bus",
    "io",
    "state_machine",
    "tool_router",
    "llm_provider",
    "scheduler",
    "ipc",
    "terminal_input",
]


def quiet_config(**overrides):
    """一份不连网、不读 stdin、不落盘的最小配置。"""
    cfg = {
        "llm": {"mode": "disabled"},
        "sunshine": {},
        "terminal": {"enabled": False},
        "scheduler": {"interval_min": 1},
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
            self.assertIsNotNone(rt.host_input_reader)
            # scheduler 必须挂在同一个 bus 与 state 上, 否则快捷键/日程不生效
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
        # agent/tools/ 还没实现: 工具为空是预期状态, 不算启动失败
        rt = make_runtime()
        await rt.start()
        try:
            self.assertEqual(rt.failures, [])
            self.assertEqual(len(rt.tools), 0)
        finally:
            await rt.stop()

    async def test_missing_ipc_module_is_not_a_failure(self):
        rt = make_runtime()
        await rt.start()
        try:
            self.assertIsNone(rt.ipc)
            self.assertEqual(rt.failures, [])
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
