#!/usr/bin/env python3
"""
tests/test_tool_router.py — ToolRouter 单测

运行:
    python tests/test_tool_router.py

覆盖:
  注册     重名 / 空名 / 非 Tool / handler 不可调用 / schema 写错
  清单     list_tools 给 LLM 的形态; allowed_tools 按状态过滤
  状态检查 允许状态放行、不允许状态拒绝、状态机拿不到时 fail closed
  参数校验 type / required / enum / additionalProperties / items /
           数值长度边界 / 布尔与整数之别 / 嵌套路径报错
  超时     同步 handler 与 async handler
  异常     同步与 async handler 抛异常 -> ok=False, 不向外抛
  结果     成功 -> {"ok": True, "result": ...}

不测具体工具 —— 本模块不含任何工具实现, 测试里用内联的假工具。
"""

import asyncio
import sys
import time
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.core import (  # noqa: E402
    State,
    StateMachine,
    SchemaError,
    Tool,
    ToolRouter,
    validate_args,
    validate_schema,
)


# ---------------------------------------------------------------------------
#  测试用假工具 (不是 agent/tools/ 里的真工具)
# ---------------------------------------------------------------------------
def _echo(**kwargs):
    return kwargs


async def _aecho(**kwargs):
    return kwargs


class _StateStub:
    """只有 current() 的状态提供者 —— 验证路由只依赖这一个方法。"""

    def __init__(self, state):
        self._state = state

    def current(self):
        return self._state

    def set(self, state):
        self._state = state


def make_tool(name="t", states=(State.IDLE,), handler=_echo, schema=None):
    return Tool(
        name=name,
        description="desc of " + name,
        schema=schema if schema is not None else {"type": "object"},
        handler=handler,
        allowed_states=set(states),
    )


# ===========================================================================
#  注册
# ===========================================================================
class TestRegister(unittest.TestCase):
    def setUp(self):
        self.router = ToolRouter(state_provider=_StateStub(State.IDLE))

    def test_register_and_count(self):
        self.router.register(make_tool("a"))
        self.router.register(make_tool("b"))
        self.assertEqual(len(self.router), 2)
        self.assertEqual(self.router.names(), ["a", "b"])

    def test_contains_and_get(self):
        tool = make_tool("a")
        self.router.register(tool)
        self.assertIn("a", self.router)
        self.assertNotIn("b", self.router)
        self.assertIs(self.router.get("a"), tool)
        self.assertIsNone(self.router.get("b"))

    def test_duplicate_name_rejected(self):
        self.router.register(make_tool("a"))
        with self.assertRaises(ValueError):
            self.router.register(make_tool("a"))

    def test_empty_name_rejected(self):
        for bad in ("", "   "):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.router.register(make_tool(bad))

    def test_non_tool_rejected(self):
        for bad in (None, "tool", {}, 42):
            with self.subTest(bad=bad):
                with self.assertRaises(TypeError):
                    self.router.register(bad)

    def test_non_callable_handler_rejected(self):
        with self.assertRaises(TypeError):
            self.router.register(make_tool("a", handler="not callable"))

    def test_bad_schema_rejected_at_register_time(self):
        # 写错的 schema 越早暴露越好, 不该等到第一次调用
        for bad in (
            {"type": "strng"},                     # 拼错类型
            {"type": "object", "required": "name"},  # required 不是 list
            {"type": "object", "properties": []},    # properties 不是 object
            {"type": "object", "items": {"type": "nope"}},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(SchemaError):
                    self.router.register(make_tool("x", schema=bad))

    def test_non_dict_schema_rejected(self):
        with self.assertRaises(SchemaError):
            self.router.register(make_tool("x", schema=["not", "a", "dict"]))

    def test_unregister(self):
        self.router.register(make_tool("a"))
        self.assertTrue(self.router.unregister("a"))
        self.assertFalse(self.router.unregister("a"))
        self.assertEqual(len(self.router), 0)

    def test_allowed_states_accepts_strings(self):
        tool = make_tool("a", states=("idle", "study"))
        self.assertEqual(tool.allowed_states, {State.IDLE, State.STUDY})

    def test_allowed_states_accepts_list(self):
        tool = Tool("a", "d", {"type": "object"}, _echo, [State.GAME])
        self.assertEqual(tool.allowed_states, {State.GAME})

    def test_empty_allowed_states_allowed_but_never_runnable(self):
        self.router.register(make_tool("a", states=()))
        self.assertFalse(self.router.is_allowed("a"))


class TestListTools(unittest.TestCase):
    def setUp(self):
        self.router = ToolRouter(state_provider=_StateStub(State.IDLE))
        self.router.register(make_tool("beta", states=(State.IDLE,)))
        self.router.register(make_tool("alpha", states=(State.GAME,)))

    def test_shape_for_llm(self):
        tools = self.router.list_tools()
        self.assertEqual(len(tools), 2)
        for item in tools:
            self.assertEqual(set(item), {"name", "description", "parameters"})
        self.assertEqual(tools[0]["name"], "alpha")
        self.assertEqual(tools[1]["name"], "beta")

    def test_order_is_stable_and_sorted(self):
        self.assertEqual([t["name"] for t in self.router.list_tools()], ["alpha", "beta"])
        # 再注册一个也要保持有序
        self.router.register(make_tool("aaa"))
        self.assertEqual(
            [t["name"] for t in self.router.list_tools()], ["aaa", "alpha", "beta"]
        )

    def test_parameters_is_the_schema(self):
        schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
        router = ToolRouter(state_provider=_StateStub(State.IDLE))
        router.register(make_tool("t", schema=schema))
        self.assertEqual(router.list_tools()[0]["parameters"], schema)

    def test_allowed_tools_filters_by_state(self):
        # 当前 IDLE: 只有 beta 可用
        self.assertEqual([t["name"] for t in self.router.allowed_tools()], ["beta"])
        self.router._state_provider.set(State.GAME)
        self.assertEqual([t["name"] for t in self.router.allowed_tools()], ["alpha"])

    def test_is_allowed(self):
        self.assertTrue(self.router.is_allowed("beta"))
        self.assertFalse(self.router.is_allowed("alpha"))
        self.assertFalse(self.router.is_allowed("nope"))


# ===========================================================================
#  状态检查 (权限控制)
# ===========================================================================
class TestStateGate(unittest.IsolatedAsyncioTestCase):
    async def test_allowed_state_runs(self):
        router = ToolRouter(state_provider=_StateStub(State.STUDY))
        router.register(make_tool("t", states=(State.STUDY,), handler=_echo))
        result = await router.execute("t", {"a": 1})
        self.assertEqual(result, {"ok": True, "result": {"a": 1}})

    async def test_disallowed_state_is_refused(self):
        router = ToolRouter(state_provider=_StateStub(State.SLEEP))
        called = []

        def handler(**kwargs):
            called.append(1)
            return "ran"

        router.register(make_tool("t", states=(State.STUDY,), handler=handler))
        result = await router.execute("t", {})
        self.assertFalse(result["ok"])
        self.assertIn("not allowed in state 'sleep'", result["error"])
        self.assertEqual(called, [], "被拒的工具绝不能被执行")

    async def test_error_lists_allowed_states(self):
        router = ToolRouter(state_provider=_StateStub(State.SLEEP))
        router.register(make_tool("t", states=(State.IDLE, State.STUDY)))
        result = await router.execute("t", {})
        self.assertIn("idle", result["error"])
        self.assertIn("study", result["error"])

    async def test_state_change_affects_permission(self):
        provider = _StateStub(State.IDLE)
        router = ToolRouter(state_provider=provider)
        router.register(make_tool("t", states=(State.GAME,)))

        self.assertFalse((await router.execute("t", {}))["ok"])
        provider.set(State.GAME)
        self.assertTrue((await router.execute("t", {}))["ok"])

    async def test_multiple_allowed_states(self):
        provider = _StateStub(State.GAME)
        router = ToolRouter(state_provider=provider)
        router.register(make_tool("t", states=(State.STUDY, State.GAME)))
        for state in (State.STUDY, State.GAME):
            with self.subTest(state=state):
                provider.set(state)
                self.assertTrue((await router.execute("t", {}))["ok"])

    async def test_fail_closed_when_state_unknown(self):
        # 状态机拿不到 -> 拒绝执行, 而不是放行
        class _Broken:
            def current(self):
                raise RuntimeError("状态机挂了")

        router = ToolRouter(state_provider=_Broken())
        called = []
        router.register(make_tool("t", states=(State.IDLE,),
                                  handler=lambda **k: called.append(1)))
        result = await router.execute("t", {})
        self.assertFalse(result["ok"])
        self.assertIn("cannot determine current state", result["error"])
        self.assertEqual(called, [])

    async def test_provider_without_current_is_fail_closed(self):
        router = ToolRouter(state_provider=object())
        router.register(make_tool("t"))
        self.assertFalse((await router.execute("t", {}))["ok"])

    async def test_default_state_provider_is_a_state_machine(self):
        # 不传 state_provider 时自建一个 StateMachine (初始 IDLE)
        router = ToolRouter()
        self.assertIsInstance(router._state_provider, StateMachine)
        self.assertIs(router.current_state(), State.IDLE)

    async def test_works_with_real_state_machine(self):
        sm = StateMachine()
        router = ToolRouter(state_provider=sm)
        router.register(make_tool("t", states=(State.GAME,)))

        self.assertFalse((await router.execute("t", {}))["ok"])
        self.assertTrue(sm.transition(State.GAME, "想玩游戏"))
        self.assertTrue((await router.execute("t", {}))["ok"])

    async def test_unknown_tool_reported_before_state_check(self):
        router = ToolRouter(state_provider=_StateStub(State.IDLE))
        result = await router.execute("nope", {})
        self.assertFalse(result["ok"])
        self.assertIn("unknown tool", result["error"])


# ===========================================================================
#  参数校验
# ===========================================================================
class TestValidateArgs(unittest.TestCase):
    def test_type_ok(self):
        validate_args({"type": "object"}, {})

    def test_wrong_top_level_type(self):
        with self.assertRaises(SchemaError):
            validate_args({"type": "object"}, [1, 2])
        with self.assertRaises(SchemaError):
            validate_args({"type": "array"}, {"a": 1})

    def test_boolean_is_not_integer(self):
        # JSON 里 boolean 与 integer 是不同类型; Python 里 bool 是 int 子类
        with self.assertRaises(SchemaError):
            validate_args({"type": "integer"}, True)
        with self.assertRaises(SchemaError):
            validate_args({"type": "number"}, False)

    def test_boolean_ok(self):
        validate_args({"type": "boolean"}, True)

    def test_integer_and_number(self):
        validate_args({"type": "integer"}, 3)
        validate_args({"type": "number"}, 3.5)
        validate_args({"type": "number"}, 3)
        with self.assertRaises(SchemaError):
            validate_args({"type": "integer"}, 3.5)

    def test_union_type(self):
        schema = {"type": ["string", "null"]}
        validate_args(schema, "x")
        validate_args(schema, None)
        with self.assertRaises(SchemaError):
            validate_args(schema, 3)

    def test_required(self):
        schema = {"type": "object", "required": ["name"]}
        validate_args(schema, {"name": "x"})
        with self.assertRaises(SchemaError) as ctx:
            validate_args(schema, {})
        self.assertIn("missing required property 'name'", str(ctx.exception))

    def test_property_type(self):
        schema = {
            "type": "object",
            "properties": {"port": {"type": "integer"}},
        }
        validate_args(schema, {"port": 8080})
        with self.assertRaises(SchemaError) as ctx:
            validate_args(schema, {"port": "8080"})
        self.assertIn("$.port", str(ctx.exception))

    def test_nested_path_in_error(self):
        schema = {
            "type": "object",
            "properties": {
                "sunshine": {
                    "type": "object",
                    "properties": {"host": {"type": "string"}},
                }
            },
        }
        with self.assertRaises(SchemaError) as ctx:
            validate_args(schema, {"sunshine": {"host": 123}})
        self.assertIn("$.sunshine.host", str(ctx.exception))

    def test_missing_property_is_allowed_unless_required(self):
        schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
        validate_args(schema, {})  # 不 required 就可不给

    def test_additional_properties_false(self):
        schema = {
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "additionalProperties": False,
        }
        validate_args(schema, {"x": 1})
        with self.assertRaises(SchemaError) as ctx:
            validate_args(schema, {"x": 1, "y": 2})
        self.assertIn("unexpected property", str(ctx.exception))

    def test_additional_properties_default_is_permissive(self):
        schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
        validate_args(schema, {"x": 1, "extra": "anything"})

    def test_additional_properties_schema(self):
        schema = {
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "additionalProperties": {"type": "string"},
        }
        validate_args(schema, {"x": 1, "label": "hi"})
        with self.assertRaises(SchemaError):
            validate_args(schema, {"label": 5})

    def test_enum(self):
        schema = {"type": "string", "enum": ["a", "b"]}
        validate_args(schema, "a")
        with self.assertRaises(SchemaError) as ctx:
            validate_args(schema, "c")
        self.assertIn("is not one of", str(ctx.exception))

    def test_array_items(self):
        schema = {"type": "array", "items": {"type": "integer"}}
        validate_args(schema, [1, 2, 3])
        with self.assertRaises(SchemaError) as ctx:
            validate_args(schema, [1, "two"])
        self.assertIn("$[1]", str(ctx.exception))

    def test_array_of_objects(self):
        schema = {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["k"],
                "properties": {"k": {"type": "string"}},
            },
        }
        validate_args(schema, [{"k": "a"}, {"k": "b"}])
        with self.assertRaises(SchemaError) as ctx:
            validate_args(schema, [{"k": "a"}, {}])
        self.assertIn("$[1]", str(ctx.exception))

    def test_empty_array_ok(self):
        validate_args({"type": "array", "items": {"type": "integer"}}, [])

    def test_numeric_bounds(self):
        schema = {"type": "integer", "minimum": 1, "maximum": 10}
        validate_args(schema, 1)
        validate_args(schema, 10)
        with self.assertRaises(SchemaError):
            validate_args(schema, 0)
        with self.assertRaises(SchemaError):
            validate_args(schema, 11)

    def test_string_length_bounds(self):
        schema = {"type": "string", "minLength": 2, "maxLength": 4}
        validate_args(schema, "abc")
        with self.assertRaises(SchemaError):
            validate_args(schema, "a")
        with self.assertRaises(SchemaError):
            validate_args(schema, "abcde")

    def test_null_type(self):
        validate_args({"type": "null"}, None)


class TestValidateSchema(unittest.TestCase):
    def test_valid_schemas_pass(self):
        for schema in (
            {"type": "object"},
            {"type": "object", "properties": {}},
            {"type": ["string", "null"]},
            {"type": "array", "items": {"type": "integer"}},
            {"type": "object", "additionalProperties": False},
            {"type": "object", "additionalProperties": {"type": "string"}},
            {},
        ):
            with self.subTest(schema=schema):
                validate_schema(schema)

    def test_non_dict_rejected(self):
        for bad in (None, [], "x", 1):
            with self.subTest(bad=bad):
                with self.assertRaises(SchemaError):
                    validate_schema(bad)

    def test_unknown_type_rejected(self):
        with self.assertRaises(SchemaError):
            validate_schema({"type": "strng"})
        with self.assertRaises(SchemaError):
            validate_schema({"type": ["string", "nope"]})

    def test_permissive_ignores_unsupported_keywords(self):
        # 默认放行: format 这类关键字不生效, 但也不报错
        validate_schema({"type": "string", "format": "date-time"})

    def test_strict_mode_rejects_unsupported_keywords(self):
        with self.assertRaises(SchemaError) as ctx:
            validate_schema({"type": "string", "format": "date-time"}, permissive=False)
        self.assertIn("format", str(ctx.exception))

    def test_strict_router_rejects_unsupported_keyword(self):
        router = ToolRouter(state_provider=_StateStub(State.IDLE), permissive_schema=False)
        with self.assertRaises(SchemaError):
            router.register(make_tool("t", schema={"type": "object", "allOf": []}))


class TestArgsThroughRouter(unittest.IsolatedAsyncioTestCase):
    async def test_valid_args_reach_handler(self):
        router = ToolRouter(state_provider=_StateStub(State.IDLE))
        router.register(make_tool("t", schema={
            "type": "object",
            "required": ["n"],
            "properties": {"n": {"type": "integer"}},
            "additionalProperties": False,
        }))
        result = await router.execute("t", {"n": 7})
        self.assertEqual(result, {"ok": True, "result": {"n": 7}})

    async def test_invalid_args_never_reach_handler(self):
        router = ToolRouter(state_provider=_StateStub(State.IDLE))
        called = []
        router.register(make_tool("t", schema={
            "type": "object",
            "required": ["n"],
            "properties": {"n": {"type": "integer"}},
        }, handler=lambda **k: called.append(k)))

        result = await router.execute("t", {"n": "seven"})
        self.assertFalse(result["ok"])
        self.assertIn("invalid arguments", result["error"])
        self.assertIn("$.n", result["error"])
        self.assertEqual(called, [])

    async def test_none_args_treated_as_empty(self):
        router = ToolRouter(state_provider=_StateStub(State.IDLE))
        router.register(make_tool("t"))
        self.assertEqual(await router.execute("t"), {"ok": True, "result": {}})
        self.assertEqual(await router.execute("t", None), {"ok": True, "result": {}})

    async def test_missing_required_reported(self):
        router = ToolRouter(state_provider=_StateStub(State.IDLE))
        router.register(make_tool("t", schema={
            "type": "object", "required": ["a", "b"],
        }))
        result = await router.execute("t", {})
        self.assertFalse(result["ok"])
        self.assertIn("missing required property", result["error"])


# ===========================================================================
#  执行: 正常 / 异步 handler / 返回值
# ===========================================================================
class TestExecute(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.router = ToolRouter(state_provider=_StateStub(State.IDLE))

    async def test_sync_handler_result(self):
        self.router.register(make_tool("t", handler=lambda **k: 42))
        self.assertEqual(await self.router.execute("t", {}), {"ok": True, "result": 42})

    async def test_async_handler_result(self):
        self.router.register(make_tool("t", handler=_aecho))
        self.assertEqual(
            await self.router.execute("t", {"x": 1}),
            {"ok": True, "result": {"x": 1}},
        )

    async def test_handler_returning_none(self):
        self.router.register(make_tool("t", handler=lambda **k: None))
        self.assertEqual(await self.router.execute("t", {}), {"ok": True, "result": None})

    async def test_handler_returning_complex_value(self):
        payload = {"a": [1, 2], "b": {"c": None}}
        self.router.register(make_tool("t", handler=lambda **k: payload))
        self.assertEqual(await self.router.execute("t", {}), {"ok": True, "result": payload})

    async def test_timeout_override_per_router(self):
        router = ToolRouter(state_provider=_StateStub(State.IDLE), timeout_s=0.05)
        router.register(make_tool("t", handler=lambda **k: time.sleep(1.0)))
        result = await router.execute("t", {})
        self.assertFalse(result["ok"])
        self.assertIn("timed out after 0.05", result["error"])


# ===========================================================================
#  超时
# ===========================================================================
class TestTimeout(unittest.IsolatedAsyncioTestCase):
    async def test_sync_handler_timeout(self):
        router = ToolRouter(state_provider=_StateStub(State.IDLE), timeout_s=0.05)
        router.register(make_tool("slow", handler=lambda **k: time.sleep(0.5)))
        started = time.monotonic()
        result = await router.execute("slow", {})
        elapsed = time.monotonic() - started

        self.assertFalse(result["ok"])
        self.assertIn("timed out", result["error"])
        self.assertIn("'slow'", result["error"])
        self.assertLess(elapsed, 0.4, "应当在超时后很快返回, 而不是等 handler 跑完")

    async def test_async_handler_timeout(self):
        async def slow(**kwargs):
            await asyncio.sleep(0.5)
            return "never"

        router = ToolRouter(state_provider=_StateStub(State.IDLE), timeout_s=0.05)
        router.register(make_tool("slow", handler=slow))
        result = await router.execute("slow", {})
        self.assertFalse(result["ok"])
        self.assertIn("timed out", result["error"])

    async def test_async_handler_is_actually_cancelled(self):
        cancelled = []

        async def slow(**kwargs):
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

        router = ToolRouter(state_provider=_StateStub(State.IDLE), timeout_s=0.05)
        router.register(make_tool("slow", handler=slow))
        await router.execute("slow", {})
        await asyncio.sleep(0)  # 让取消传播
        self.assertTrue(cancelled, "async handler 应当被真正 cancel")

    async def test_fast_handler_within_timeout(self):
        router = ToolRouter(state_provider=_StateStub(State.IDLE), timeout_s=1.0)
        router.register(make_tool("fast", handler=lambda **k: "ok"))
        self.assertEqual(await router.execute("fast", {}),
                         {"ok": True, "result": "ok"})

    async def test_tool_can_declare_its_own_timeout(self):
        """T8-5b: 会同步走一趟 PC 的工具（音乐排队列后起播要 ssh + schtasks）声明自己的
        超时 —— 路由的全局 5 s 会把它误判成 timed out（板端实测踩到过）。"""
        router = ToolRouter(state_provider=_StateStub(State.IDLE), timeout_s=0.05)
        router.register(make_tool("slow", handler=lambda **k: time.sleep(0.3)))
        result = await router.execute("slow", {})
        self.assertFalse(result["ok"], "路由默认 0.05 s: 照旧要超时")

        patient = ToolRouter(state_provider=_StateStub(State.IDLE), timeout_s=0.05)
        tool = make_tool("patient", handler=lambda **k: time.sleep(0.3))
        tool.timeout_s = 3.0
        patient.register(tool)
        result = await patient.execute("patient", {})
        self.assertTrue(result["ok"], "工具自己声明了 3 s -> 不该被 0.05 s 掐掉: %r" % result)

    async def test_tool_timeout_is_reported_with_the_tool_value(self):
        router = ToolRouter(state_provider=_StateStub(State.IDLE), timeout_s=0.05)
        tool = make_tool("slow", handler=lambda **k: time.sleep(0.5))
        tool.timeout_s = 0.1
        router.register(tool)
        result = await router.execute("slow", {})
        self.assertIn("0.1s", result["error"], "报错里的秒数要用这个工具自己的值")

    async def test_timeout_does_not_kill_router(self):
        router = ToolRouter(state_provider=_StateStub(State.IDLE), timeout_s=0.05)
        router.register(make_tool("slow", handler=lambda **k: time.sleep(0.3)))
        router.register(make_tool("fast", handler=lambda **k: "fine"))

        self.assertFalse((await router.execute("slow", {}))["ok"])
        self.assertTrue((await router.execute("fast", {}))["ok"],
                        "一次超时不该影响后续调用")

    async def test_sync_timeout_does_not_block_event_loop(self):
        router = ToolRouter(state_provider=_StateStub(State.IDLE), timeout_s=0.05)
        router.register(make_tool("slow", handler=lambda **k: time.sleep(0.3)))
        ticks = []

        async def ticker():
            for _ in range(10):
                ticks.append(1)
                await asyncio.sleep(0.005)

        await asyncio.gather(router.execute("slow", {}), ticker())
        self.assertGreater(len(ticks), 1, "同步 handler 不该卡住事件循环")


# ===========================================================================
#  异常
# ===========================================================================
class TestExceptions(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.router = ToolRouter(state_provider=_StateStub(State.IDLE))

    async def test_sync_exception_captured(self):
        def boom(**kwargs):
            raise RuntimeError("工具炸了")

        self.router.register(make_tool("t", handler=boom))
        result = await self.router.execute("t", {})
        self.assertFalse(result["ok"])
        self.assertIn("RuntimeError", result["error"])
        self.assertIn("工具炸了", result["error"])

    async def test_async_exception_captured(self):
        async def boom(**kwargs):
            raise ValueError("参数有问题")

        self.router.register(make_tool("t", handler=boom))
        result = await self.router.execute("t", {})
        self.assertFalse(result["ok"])
        self.assertIn("ValueError", result["error"])

    async def test_execute_never_raises(self):
        # 工具里偶尔会抛 BaseException 子类; 违反"execute 永不抛"的契约会很难查
        def boom(**kwargs):
            raise BaseException("很糟")  # noqa: TRY002 - 刻意用 BaseException 子类

        self.router.register(make_tool("t", handler=boom))
        result = await self.router.execute("t", {})
        self.assertFalse(result["ok"])
        self.assertIn("很糟", result["error"])

    async def test_keyboard_interrupt_is_not_swallowed(self):
        # 进程级信号不该被当成"某个工具失败了"
        def boom(**kwargs):
            raise KeyboardInterrupt()

        self.router.register(make_tool("t", handler=boom))
        with self.assertRaises(KeyboardInterrupt):
            await self.router.execute("t", {})

    async def test_system_exit_is_not_swallowed(self):
        def boom(**kwargs):
            raise SystemExit(2)

        self.router.register(make_tool("t", handler=boom))
        with self.assertRaises(SystemExit):
            await self.router.execute("t", {})

    async def test_error_message_has_no_exception_repr_noise(self):
        def boom(**kwargs):
            raise RuntimeError("干净的消息")

        self.router.register(make_tool("t", handler=boom))
        self.assertEqual((await self.router.execute("t", {}))["error"],
                         "RuntimeError: 干净的消息")

    async def test_router_usable_after_exception(self):
        def boom(**kwargs):
            raise RuntimeError("x")

        self.router.register(make_tool("bad", handler=boom))
        self.router.register(make_tool("good", handler=lambda **k: "ok"))

        for _ in range(3):
            self.assertFalse((await self.router.execute("bad", {}))["ok"])
            self.assertTrue((await self.router.execute("good", {}))["ok"])

    async def test_cancelled_error_propagates(self):
        # 调用方被取消时不能让路由把它吞成 ok=False, 否则上层取消会失灵
        async def slow(**kwargs):
            await asyncio.sleep(10)

        router = ToolRouter(state_provider=_StateStub(State.IDLE), timeout_s=10)
        router.register(make_tool("slow", handler=slow))

        task = asyncio.create_task(router.execute("slow", {}))
        await asyncio.sleep(0.02)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_handler_signature_mismatch_is_captured(self):
        # handler 不接受参数却给了参数 -> TypeError, 也走统一错误通道
        self.router.register(make_tool("t", handler=lambda: "no kwargs"))
        result = await self.router.execute("t", {"x": 1})
        self.assertFalse(result["ok"])
        self.assertIn("TypeError", result["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
