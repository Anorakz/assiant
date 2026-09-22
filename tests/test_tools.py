#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_tools.py — 工具层（Phase 7）

跑法:
    python tests/test_tools.py

覆盖:
  · 装配   `agent.tools.build_tools(router)` 把真工具造出来；**缺依赖就跳过并说清**
  · 注册   出现在 `list_tools()` / `allowed_tools()`（按状态过滤）/ 重名被拒
  · 权限   允许的状态能执行；不允许的状态**拒绝**（fail closed）
  · 参数   schema 校验（本工具没有参数，多给一个键就该被拒）
  · 执行   真调用到底层的 `show_desktop()`；异常/超时被收口成 `{"ok": False, ...}`
  · 文案   ⚠ "开关 + 无回执"必须写进 description（LLM 得知道别连着调）
"""

import asyncio
import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent import tools as tools_pkg  # noqa: E402
from agent.core.state_machine import State, StateMachine  # noqa: E402
from agent.core.tool_router import Tool, ToolRouter  # noqa: E402
from agent.tools import back_to_desktop  # noqa: E402


class FakeSender:
    """`InputSender` 的替身：只记"发过几次 show_desktop"。"""

    def __init__(self, fail: bool = False):
        self.calls = 0
        self._fail = fail

    async def show_desktop(self) -> None:
        self.calls += 1
        if self._fail:
            raise RuntimeError("native 炸了")


def make_router(state: State = State.STUDY, sender=None) -> ToolRouter:
    sm = StateMachine()
    if state is not State.IDLE:
        sm.transition(state.value, "test")
    return ToolRouter(
        state_provider=sm,
        services={"input_sender": sender if sender is not None else FakeSender()},
    )


class TestBuildTools(unittest.TestCase):
    def test_builds_the_real_tools(self):
        router = make_router()
        built = tools_pkg.build_tools(router)
        self.assertEqual([tool.name for tool in built], ["back_to_desktop"])

    def test_every_tool_module_is_covered(self):
        """反空转：清单里每个模块都要真能 import 并给出 build(services)。"""
        import importlib
        self.assertTrue(tools_pkg.TOOL_MODULES, "工具清单不该是空的")
        for name in tools_pkg.TOOL_MODULES:
            with self.subTest(module=name):
                module = importlib.import_module("agent.tools.%s" % name)
                self.assertTrue(callable(getattr(module, "build", None)),
                                "%s 缺少 build(services)" % name)

    def test_missing_dependency_skips_that_tool(self):
        router = ToolRouter(state_provider=StateMachine(), services={})
        self.assertEqual(tools_pkg.build_tools(router), [],
                         "没有 input_sender 时应当跳过，而不是造一个会炸的工具")

    def test_sender_without_show_desktop_is_skipped(self):
        class Odd:
            pass

        router = ToolRouter(state_provider=StateMachine(), services={"input_sender": Odd()})
        self.assertEqual(tools_pkg.build_tools(router), [])

    def test_a_broken_factory_does_not_break_the_others(self):
        """工厂抛异常 -> 只跳过它（这里是手工列表，验证循环的容错口径）。"""
        import importlib
        original = tools_pkg.TOOL_MODULES
        tools_pkg.TOOL_MODULES = ("back_to_desktop", "definitely_not_a_module")
        try:
            router = make_router()
            built = tools_pkg.build_tools(router)
            self.assertEqual([tool.name for tool in built], ["back_to_desktop"])
        finally:
            tools_pkg.TOOL_MODULES = original
        importlib  # noqa: B018 - 保持 import 形态可读


class TestRegistration(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.router = make_router()
        for tool in tools_pkg.build_tools(self.router):
            self.router.register(tool)

    def test_appears_in_the_llm_list(self):
        schemas = self.router.list_tools()
        self.assertEqual(len(schemas), 1)
        entry = schemas[0]
        self.assertEqual(entry["name"], "back_to_desktop")
        self.assertEqual(entry["parameters"]["type"], "object")
        self.assertIn("Win+D", entry["description"])

    def test_description_warns_about_the_toggle_and_no_receipt(self):
        """⚠ 这条是**文案防线**：不写清楚，LLM 会把它当幂等动作连着调。"""
        text = self.router.list_tools()[0]["description"]
        self.assertIn("开关", text)
        self.assertIn("没有回执", text)

    def test_allowed_tools_follows_the_state(self):
        self.assertEqual([t["name"] for t in self.router.allowed_tools()],
                         ["back_to_desktop"])           # STUDY 允许

        idle = make_router(state=State.IDLE)
        for tool in tools_pkg.build_tools(idle):
            idle.register(tool)
        self.assertEqual(idle.allowed_tools(), [], "IDLE 下不该出现在候选里")
        self.assertIn("back_to_desktop", idle.names(), "但工具本身还是注册着的")

    def test_duplicate_registration_is_rejected(self):
        with self.assertRaises(ValueError):
            for tool in tools_pkg.build_tools(self.router):
                self.router.register(tool)

    def test_services_are_reachable_from_the_router(self):
        self.assertIn("input_sender", self.router.services)


class TestExecution(unittest.IsolatedAsyncioTestCase):
    async def test_runs_the_underlying_sender(self):
        sender = FakeSender()
        router = make_router(sender=sender)
        for tool in tools_pkg.build_tools(router):
            router.register(tool)

        result = await router.execute("back_to_desktop", {})

        self.assertTrue(result["ok"], result)
        self.assertEqual(sender.calls, 1, "调用一次只该发一次")
        self.assertEqual(result["result"]["sent"], "win+d")
        self.assertIn("幂等", result["result"]["note"])

    async def test_refused_outside_the_allowed_state(self):
        sender = FakeSender()
        router = make_router(state=State.IDLE, sender=sender)
        for tool in tools_pkg.build_tools(router):
            router.register(tool)

        result = await router.execute("back_to_desktop", {})

        self.assertFalse(result["ok"])
        self.assertIn("not allowed", result["error"])
        self.assertIn("idle", result["error"])
        self.assertEqual(sender.calls, 0, "被拒时绝不能真的发出去")

    async def test_extra_argument_is_rejected_before_running(self):
        sender = FakeSender()
        router = make_router(sender=sender)
        for tool in tools_pkg.build_tools(router):
            router.register(tool)

        result = await router.execute("back_to_desktop", {"surprise": 1})

        self.assertFalse(result["ok"])
        self.assertIn("invalid arguments", result["error"])
        self.assertEqual(sender.calls, 0)

    async def test_handler_error_is_contained(self):
        router = make_router(sender=FakeSender(fail=True))
        for tool in tools_pkg.build_tools(router):
            router.register(tool)

        result = await router.execute("back_to_desktop", {})

        self.assertFalse(result["ok"])
        self.assertIn("RuntimeError", result["error"])
        self.assertIn("native 炸了", result["error"])

    async def test_unknown_tool(self):
        router = make_router()
        result = await router.execute("nope", {})
        self.assertFalse(result["ok"])
        self.assertIn("unknown tool", result["error"])

    async def test_timeout_is_reported(self):
        class Slow:
            async def show_desktop(self):
                await asyncio.sleep(5)

        router = ToolRouter(state_provider=StateMachine(), timeout_s=0.05,
                            services={"input_sender": Slow()})
        sm = router._state_provider                      # noqa: SLF001 - 测试里直接摆状态
        sm.transition("study", "test")
        for tool in tools_pkg.build_tools(router):
            router.register(tool)

        result = await router.execute("back_to_desktop", {})

        self.assertFalse(result["ok"])
        self.assertIn("timed out", result["error"])


class TestModuleShape(unittest.TestCase):
    """工具模块自身的形状（加新工具时照这个来）。"""

    def test_back_to_desktop_exports(self):
        self.assertEqual(back_to_desktop.NAME, "back_to_desktop")
        self.assertEqual(back_to_desktop.ALLOWED_STATES, (State.STUDY,))
        self.assertEqual(back_to_desktop.SCHEMA["type"], "object")
        self.assertFalse(back_to_desktop.SCHEMA["additionalProperties"])
        self.assertTrue(callable(back_to_desktop.build))


if __name__ == "__main__":
    unittest.main(verbosity=2)
