#!/usr/bin/env python3
"""
tests/test_llm.py — LLMProvider / RuleEngine 单测

运行:
    python tests/test_llm.py

覆盖:
  三模式切换   edge / cloud / disabled; 从 config 读; 运行时 set_mode;
               非法模式降级到 disabled; 配置读失败降级
  cloud mock   chat 文本路径、chat_with_tools 工具循环 (含多轮)、
               SDK 缺失报错、模型报错 -> ok=False、轮数用尽
  rule 匹配    内置规则 (问候/时间/状态/能力/停止/告别)、自定义规则、
               顺序即优先级、未命中默认话术、handler 出错降级
  约定         chat 抛异常 vs chat_with_tools 不抛; disabled 不调模型

不测真实 edge 推理 (mock) / 不测 prompt 模板 / 不测 token 统计。
"""

import asyncio
import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.core import State, StateMachine, Tool, ToolRouter  # noqa: E402
from agent.llm import (  # noqa: E402
    DEFAULT_MODE,
    MODES,
    CloudBackend,
    EdgeBackend,
    LLMProvider,
    OpenAIClientError,
    Rule,
    RuleEngine,
)


# ---------------------------------------------------------------------------
#  OpenAI SDK 的假替身
# ---------------------------------------------------------------------------
class _FakeFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, call_id, name, arguments):
        self.id = call_id
        self.type = "function"
        self.function = _FakeFunction(name, arguments)


class _FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, message):
        self.message = message


class _FakeResponse:
    def __init__(self, content=None, tool_calls=None):
        self.choices = [_FakeChoice(_FakeMessage(content, tool_calls))]


class _FakeCompletions:
    def __init__(self, script):
        self._script = list(script)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if not self._script:
            return _FakeResponse(content="(no more scripted responses)")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeChat:
    def __init__(self, script):
        self.completions = _FakeCompletions(script)


class _FakeOpenAI:
    """够用的 openai.OpenAI 替身。"""

    def __init__(self, script):
        self.chat = _FakeChat(script)

    @property
    def requests(self):
        return self.chat.completions.requests


def text_response(text):
    return _FakeResponse(content=text)


def tool_response(call_id, name, args_json):
    return _FakeResponse(content=None, tool_calls=[_FakeToolCall(call_id, name, args_json)])


def make_cloud(script, **kwargs):
    return CloudBackend(client=_FakeOpenAI(script), **kwargs)


# ===========================================================================
#  RuleEngine
# ===========================================================================
class TestRuleEngine(unittest.IsolatedAsyncioTestCase):
    async def test_builtin_greeting(self):
        engine = RuleEngine()
        reply = await engine.respond("你好", {})
        self.assertIn("简易模式", reply)
        self.assertEqual(engine.last_rule, "greeting")

    async def test_builtin_greeting_english(self):
        engine = RuleEngine()
        self.assertEqual((await engine.respond("hello", {}), engine.last_rule)[1], "greeting")

    async def test_builtin_time_uses_injected_clock(self):
        from datetime import datetime

        engine = RuleEngine()
        fixed = datetime(2026, 9, 16, 13, 45, 0)
        reply = await engine.respond("现在几点", {"now": lambda: fixed})
        self.assertIn("2026-09-16 13:45:00", reply)
        self.assertEqual(engine.last_rule, "time")

    async def test_builtin_state_reads_state_string(self):
        engine = RuleEngine()
        reply = await engine.respond("当前状态", {"state": "study"})
        self.assertIn("study", reply)
        self.assertEqual(engine.last_rule, "state")

    async def test_builtin_state_reads_state_enum(self):
        engine = RuleEngine()
        reply = await engine.respond("当前状态", {"state": State.GAME})
        self.assertIn("game", reply)

    async def test_builtin_state_reads_state_machine(self):
        sm = StateMachine()
        sm.transition(State.STUDY, "x")
        engine = RuleEngine()
        reply = await engine.respond("在干什么", {"state_machine": sm})
        self.assertIn("study", reply)

    async def test_builtin_capability(self):
        engine = RuleEngine()
        reply = await engine.respond("你能做什么", {})
        self.assertEqual(engine.last_rule, "capability")
        self.assertIn("简易模式", reply)

    async def test_builtin_stop(self):
        engine = RuleEngine()
        self.assertEqual(await engine.respond("停", {}), "好的，已停止。")
        self.assertEqual(await engine.respond("stop", {}), "好的，已停止。")

    async def test_builtin_farewell(self):
        engine = RuleEngine()
        self.assertEqual(await engine.respond("再见", {}), "再见。")
        self.assertEqual(engine.last_rule, "farewell")

    async def test_unknown_input_uses_default_reply(self):
        engine = RuleEngine()
        reply = await engine.respond("把窗帘关上", {})
        self.assertIn("听不懂", reply)
        self.assertIsNone(engine.last_rule)

    async def test_matching_is_case_insensitive(self):
        engine = RuleEngine()
        self.assertEqual(engine.last_rule, None)
        await engine.respond("HELLO", {})
        self.assertEqual(engine.last_rule, "greeting")

    async def test_first_matching_rule_wins(self):
        engine = RuleEngine(rules=[
            Rule("first", r"test", lambda m, t, c: "first"),
            Rule("second", r"test", lambda m, t, c: "second"),
        ])
        self.assertEqual(await engine.respond("test", {}), "first")
        self.assertEqual(engine.last_rule, "first")

    async def test_custom_rules_replace_builtins(self):
        engine = RuleEngine(rules=[Rule("only", r"x", lambda m, t, c: "custom")])
        self.assertEqual(await engine.respond("x", {}), "custom")
        # 内置规则已被替换, 所以"你好"走默认话术
        self.assertIn("听不懂", await engine.respond("你好", {}))

    async def test_add_rule_appends(self):
        engine = RuleEngine(rules=[])
        engine.add_rule(Rule("a", r"a", lambda m, t, c: "A"))
        engine.add_rule(Rule("b", r"b", lambda m, t, c: "B"))
        self.assertEqual([r.name for r in engine.rules], ["a", "b"])
        self.assertEqual(await engine.respond("b", {}), "B")

    async def test_add_rule_prepend_takes_priority(self):
        engine = RuleEngine(rules=[Rule("base", r"x", lambda m, t, c: "base")])
        engine.add_rule(Rule("over", r"x", lambda m, t, c: "over"), prepend=True)
        self.assertEqual(await engine.respond("x", {}), "over")

    async def test_remove_rule(self):
        engine = RuleEngine()
        self.assertTrue(engine.remove_rule("greeting"))
        self.assertFalse(engine.remove_rule("greeting"))
        self.assertNotIn("greeting", [r.name for r in engine.rules])

    async def test_handler_receives_match_and_input(self):
        seen = {}

        def handler(m, text, ctx):
            seen["group"] = m.group(0)
            seen["text"] = text
            seen["ctx"] = ctx
            return "ok"

        engine = RuleEngine(rules=[Rule("cap", r"(?P<w>hi)", handler)])
        await engine.respond("say hi now", {"k": 1})
        self.assertEqual(seen["group"], "hi")
        self.assertEqual(seen["text"], "say hi now")
        self.assertEqual(seen["ctx"], {"k": 1})

    async def test_handler_exception_falls_back_to_default(self):
        def boom(m, t, c):
            raise RuntimeError("规则炸了")

        engine = RuleEngine(rules=[Rule("bad", r"x", boom)])
        reply = await engine.respond("x", {})
        self.assertIn("听不懂", reply)
        self.assertEqual(engine.last_rule, "bad", "仍应记录命中了哪条")

    async def test_respond_never_raises_on_weird_input(self):
        engine = RuleEngine()
        for weird in (None, 123, [], {}, object()):
            with self.subTest(weird=weird):
                reply = await engine.respond(weird, None)
                self.assertIsInstance(reply, str)

    async def test_context_defaults_to_empty(self):
        engine = RuleEngine()
        self.assertIn("未知", await engine.respond("当前状态", None))

    def test_rule_rejects_non_string_pattern(self):
        with self.assertRaises(TypeError):
            Rule("bad", 123, lambda m, t, c: "x")

    def test_rule_rejects_empty_pattern_list(self):
        with self.assertRaises(ValueError):
            Rule("bad", [], lambda m, t, c: "x")

    def test_rule_rejects_bad_regex_at_construction(self):
        # 编译期就报错, 免得等到第一次匹配才发现正则写错
        with self.assertRaises(Exception):
            Rule("bad", r"([unclosed", lambda m, t, c: "x")

    def test_rule_rejects_non_callable_handler(self):
        with self.assertRaises(TypeError):
            Rule("bad", r"x", "not callable")

    def test_rule_accepts_multiple_patterns(self):
        rule = Rule("multi", [r"aaa", r"bbb"], lambda m, t, c: "hit")
        self.assertIsNotNone(rule.match("xxbbbxx"))
        self.assertIsNone(rule.match("ccc"))

    def test_add_rule_type_check(self):
        engine = RuleEngine()
        with self.assertRaises(TypeError):
            engine.add_rule("not a rule")


# ===========================================================================
#  模式解析与切换
# ===========================================================================
class TestModeSelection(unittest.IsolatedAsyncioTestCase):
    def test_modes_constant(self):
        self.assertEqual(MODES, ("edge", "cloud", "disabled"))
        self.assertEqual(DEFAULT_MODE, "disabled")

    def test_explicit_modes(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                self.assertEqual(LLMProvider(mode=mode).mode(), mode)

    def test_mode_is_case_insensitive_and_trimmed(self):
        self.assertEqual(LLMProvider(mode="  CLOUD ").mode(), "cloud")

    def test_invalid_mode_falls_back_to_disabled_and_records_error(self):
        provider = LLMProvider(mode="banana")
        self.assertEqual(provider.mode(), "disabled")
        self.assertTrue(provider.mode_errors)
        self.assertIn("banana", provider.mode_errors[0])

    def test_non_string_mode_falls_back(self):
        for bad in (123, [], {}):
            with self.subTest(bad=bad):
                self.assertEqual(LLMProvider(mode=bad).mode(), "disabled")

    def test_mode_read_from_config_loader(self):
        loader = lambda: {"llm": {"mode": "cloud"}}  # noqa: E731
        self.assertEqual(LLMProvider(mode=None, config_loader=loader).mode(), "cloud")

    def test_mode_none_without_loader_is_disabled(self):
        self.assertEqual(LLMProvider(mode=None).mode(), "disabled")

    def test_config_missing_llm_section(self):
        loader = lambda: {"sunshine": {}}  # noqa: E731
        self.assertEqual(LLMProvider(mode=None, config_loader=loader).mode(), "disabled")

    def test_config_llm_not_a_dict(self):
        loader = lambda: {"llm": "cloud"}  # noqa: E731
        self.assertEqual(LLMProvider(mode=None, config_loader=loader).mode(), "disabled")

    def test_config_loader_raising_falls_back(self):
        def boom():
            raise RuntimeError("配置读不到")

        provider = LLMProvider(mode=None, config_loader=boom)
        self.assertEqual(provider.mode(), "disabled")
        self.assertTrue(provider.mode_errors)

    def test_config_invalid_mode_falls_back(self):
        loader = lambda: {"llm": {"mode": "gpt"}}  # noqa: E731
        provider = LLMProvider(mode=None, config_loader=loader)
        self.assertEqual(provider.mode(), "disabled")
        self.assertIn("gpt", provider.mode_errors[0])

    def test_set_mode_runtime_switch(self):
        provider = LLMProvider(mode="disabled")
        self.assertEqual(provider.set_mode("edge"), "edge")
        self.assertEqual(provider.mode(), "edge")
        self.assertEqual(provider.set_mode("cloud"), "cloud")

    def test_set_mode_none_rereads_config(self):
        config = {"llm": {"mode": "edge"}}
        provider = LLMProvider(mode="disabled", config_loader=lambda: config)
        config["llm"]["mode"] = "cloud"
        self.assertEqual(provider.set_mode(None), "cloud")

    def test_set_mode_invalid_degrades(self):
        provider = LLMProvider(mode="disabled")
        self.assertEqual(provider.set_mode("nope"), "disabled")
        self.assertTrue(provider.mode_errors)

    # ---------------------------------------------------------- 分发 ----
    async def test_disabled_uses_rule_engine(self):
        engine = RuleEngine(rules=[Rule("r", r".*", lambda m, t, c: "from rules")])
        provider = LLMProvider(mode="disabled", rule_engine=engine)
        self.assertEqual(await provider.chat("anything", {}), "from rules")

    async def test_edge_uses_edge_backend(self):
        edge = EdgeBackend(reply="from edge")
        provider = LLMProvider(mode="edge", edge_backend=edge)
        self.assertEqual(await provider.chat("hi", {}), "from edge")
        self.assertEqual(len(edge.calls), 1)

    async def test_edge_mock_default_reply_mentions_not_wired(self):
        provider = LLMProvider(mode="edge")
        reply = await provider.chat("hi", {})
        self.assertIn("mock", reply)
        self.assertFalse(provider.edge_backend.is_ready())

    async def test_edge_backend_custom_responder(self):
        edge = EdgeBackend(responder=lambda text, ctx: "echo:" + text)
        provider = LLMProvider(mode="edge", edge_backend=edge)
        self.assertEqual(await provider.chat("ping", {}), "echo:ping")

    async def test_switching_mode_switches_backend(self):
        engine = RuleEngine(rules=[Rule("r", r".*", lambda m, t, c: "rules")])
        provider = LLMProvider(
            mode="disabled",
            rule_engine=engine,
            edge_backend=EdgeBackend(reply="edge"),
            cloud_backend=make_cloud([text_response("cloud")]),
        )
        self.assertEqual(await provider.chat("x", {}), "rules")
        provider.set_mode("edge")
        self.assertEqual(await provider.chat("x", {}), "edge")
        provider.set_mode("cloud")
        self.assertEqual(await provider.chat("x", {}), "cloud")

    async def test_disabled_never_touches_cloud_or_edge(self):
        edge = EdgeBackend(reply="edge")
        cloud = make_cloud([text_response("cloud")])
        provider = LLMProvider(
            mode="disabled", edge_backend=edge, cloud_backend=cloud
        )
        await provider.chat("x", {})
        await provider.chat_with_tools("x", {})
        self.assertEqual(edge.calls, [])
        self.assertEqual(cloud.client.requests, [])


# ===========================================================================
#  cloud 模式
# ===========================================================================
class TestCloudChat(unittest.IsolatedAsyncioTestCase):
    async def test_chat_returns_text(self):
        provider = LLMProvider(mode="cloud", cloud_backend=make_cloud([text_response("你好呀")]))
        self.assertEqual(await provider.chat("hi", {}), "你好呀")

    async def test_chat_sends_system_and_user_messages(self):
        cloud = make_cloud([text_response("ok")])
        provider = LLMProvider(mode="cloud", cloud_backend=cloud)
        await provider.chat("帮我看看", {})
        request = cloud.client.requests[0]
        roles = [m["role"] for m in request["messages"]]
        self.assertEqual(roles[0], "system")
        self.assertEqual(roles[-1], "user")
        self.assertEqual(request["messages"][-1]["content"], "帮我看看")

    async def test_chat_includes_context_as_system_message(self):
        cloud = make_cloud([text_response("ok")])
        provider = LLMProvider(mode="cloud", cloud_backend=cloud)
        await provider.chat("x", {"state": "study", "note": "记得带伞"})
        contents = " ".join(m["content"] or "" for m in cloud.client.requests[0]["messages"])
        self.assertIn("study", contents)
        self.assertIn("记得带伞", contents)

    async def test_chat_drops_unserializable_context(self):
        cloud = make_cloud([text_response("ok")])
        provider = LLMProvider(mode="cloud", cloud_backend=cloud)
        await provider.chat("x", {"bad": object(), "good": 1})
        contents = " ".join(m["content"] or "" for m in cloud.client.requests[0]["messages"])
        self.assertIn("good", contents)
        self.assertNotIn("bad", contents)

    async def test_chat_uses_configured_model(self):
        cloud = make_cloud([text_response("ok")], model="my-model")
        provider = LLMProvider(mode="cloud", cloud_backend=cloud)
        await provider.chat("x", {})
        self.assertEqual(cloud.client.requests[0]["model"], "my-model")

    async def test_chat_error_propagates(self):
        # chat() 是"给人看的简单接口": 失败要抛, 调用方才知道模型挂了
        provider = LLMProvider(
            mode="cloud", cloud_backend=make_cloud([RuntimeError("网络断了")])
        )
        with self.assertRaises(RuntimeError):
            await provider.chat("x", {})

    async def test_cloud_without_backend_raises(self):
        provider = LLMProvider(mode="cloud")
        with self.assertRaises(OpenAIClientError):
            await provider.chat("x", {})

    async def test_missing_sdk_error_is_actionable(self):
        # 不注入 client -> 真的去 import openai (宿主/板上都没装)
        backend = CloudBackend(api_key="sk-test")
        provider = LLMProvider(mode="cloud", cloud_backend=backend)
        with self.assertRaises(OpenAIClientError) as ctx:
            await provider.chat("x", {})
        self.assertIn("openai", str(ctx.exception))
        self.assertIn("pip3 install openai", str(ctx.exception))

    async def test_missing_api_key_is_reported(self):
        # client 不注入、api_key 也没有 -> 应当是"缺 api_key"而不是去 import
        backend = CloudBackend(client=None, api_key=None)
        provider = LLMProvider(mode="cloud", cloud_backend=backend)
        with self.assertRaises(OpenAIClientError):
            await provider.chat("x", {})

    async def test_empty_content_returns_empty_string(self):
        provider = LLMProvider(
            mode="cloud", cloud_backend=make_cloud([_FakeResponse(content=None)])
        )
        self.assertEqual(await provider.chat("x", {}), "")


class TestCloudWithTools(unittest.IsolatedAsyncioTestCase):
    def _router(self):
        router = ToolRouter(state_provider=StateMachine())  # 初始 IDLE
        router.register(Tool(
            name="get_time",
            description="取当前时间",
            schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=lambda **k: "12:00",
            allowed_states={State.IDLE},
        ))
        return router

    async def test_plain_reply_without_tools(self):
        provider = LLMProvider(
            mode="cloud",
            tools=self._router(),
            cloud_backend=make_cloud([text_response("没什么要做的")]),
        )
        result = await provider.chat_with_tools("你好", {})
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "没什么要做的")
        self.assertEqual(result["tool_calls"], [])
        self.assertIsNone(result["error"])
        self.assertEqual(result["mode"], "cloud")

    async def test_result_shape(self):
        provider = LLMProvider(mode="cloud", cloud_backend=make_cloud([text_response("x")]))
        result = await provider.chat_with_tools("hi", {})
        self.assertEqual(set(result), {"ok", "text", "tool_calls", "error", "mode"})

    async def test_tools_are_advertised_to_model(self):
        cloud = make_cloud([text_response("ok")])
        provider = LLMProvider(mode="cloud", tools=self._router(), cloud_backend=cloud)
        await provider.chat_with_tools("x", {})
        request = cloud.client.requests[0]
        self.assertIn("tools", request)
        self.assertEqual(request["tools"][0]["type"], "function")
        self.assertEqual(request["tools"][0]["function"]["name"], "get_time")

    async def test_no_tool_router_means_no_tools_kwarg(self):
        cloud = make_cloud([text_response("ok")])
        provider = LLMProvider(mode="cloud", cloud_backend=cloud)
        await provider.chat_with_tools("x", {})
        self.assertNotIn("tools", cloud.client.requests[0])

    async def test_tool_call_is_executed_and_result_returned(self):
        cloud = make_cloud([
            tool_response("call_1", "get_time", "{}"),
            text_response("现在是 12:00"),
        ])
        provider = LLMProvider(mode="cloud", tools=self._router(), cloud_backend=cloud)
        result = await provider.chat_with_tools("几点了", {})

        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "现在是 12:00")
        self.assertEqual(len(result["tool_calls"]), 1)
        call = result["tool_calls"][0]
        self.assertEqual(call["name"], "get_time")
        self.assertEqual(call["args"], {})
        self.assertEqual(call["result"], {"ok": True, "result": "12:00"})
        # 两轮请求: 第一轮要工具, 第二轮给最终答复
        self.assertEqual(len(cloud.client.requests), 2)

    async def test_tool_result_is_fed_back_to_model(self):
        cloud = make_cloud([
            tool_response("call_1", "get_time", "{}"),
            text_response("done"),
        ])
        provider = LLMProvider(mode="cloud", tools=self._router(), cloud_backend=cloud)
        await provider.chat_with_tools("x", {})

        second = cloud.client.requests[1]["messages"]
        tool_messages = [m for m in second if m.get("role") == "tool"]
        self.assertEqual(len(tool_messages), 1)
        self.assertEqual(tool_messages[0]["tool_call_id"], "call_1")
        self.assertIn("12:00", tool_messages[0]["content"])
        # 也要有那条 assistant 的 tool_calls, 否则上下文不完整
        assistant = [m for m in second if m.get("role") == "assistant"]
        self.assertTrue(assistant and assistant[0].get("tool_calls"))

    async def test_multiple_tool_calls_in_one_round(self):
        cloud = make_cloud([
            _FakeResponse(content=None, tool_calls=[
                _FakeToolCall("c1", "get_time", "{}"),
                _FakeToolCall("c2", "get_time", "{}"),
            ]),
            text_response("都好了"),
        ])
        provider = LLMProvider(mode="cloud", tools=self._router(), cloud_backend=cloud)
        result = await provider.chat_with_tools("x", {})
        self.assertEqual(len(result["tool_calls"]), 2)
        self.assertTrue(result["ok"])

    async def test_multiple_rounds(self):
        cloud = make_cloud([
            tool_response("c1", "get_time", "{}"),
            tool_response("c2", "get_time", "{}"),
            text_response("终于好了"),
        ])
        provider = LLMProvider(mode="cloud", tools=self._router(), cloud_backend=cloud)
        result = await provider.chat_with_tools("x", {})
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["tool_calls"]), 2)
        self.assertEqual(result["text"], "终于好了")

    async def test_tool_not_allowed_in_state_is_reported_but_loop_continues(self):
        sm = StateMachine()
        sm.transition(State.SLEEP, "去睡")
        router = ToolRouter(state_provider=sm)
        router.register(Tool(
            name="get_time", description="d",
            schema={"type": "object"},
            handler=lambda **k: "12:00",
            allowed_states={State.IDLE},   # SLEEP 下不允许
        ))
        cloud = make_cloud([
            tool_response("c1", "get_time", "{}"),
            text_response("这个状态下我做不了"),
        ])
        provider = LLMProvider(mode="cloud", tools=router, cloud_backend=cloud)
        result = await provider.chat_with_tools("几点了", {})

        self.assertTrue(result["ok"], "工具被拒不该让整轮失败")
        self.assertFalse(result["tool_calls"][0]["result"]["ok"])
        self.assertIn("not allowed", result["tool_calls"][0]["result"]["error"])

    async def test_unknown_tool_reported(self):
        cloud = make_cloud([
            tool_response("c1", "nonexistent", "{}"),
            text_response("好吧"),
        ])
        provider = LLMProvider(mode="cloud", tools=self._router(), cloud_backend=cloud)
        result = await provider.chat_with_tools("x", {})
        self.assertIn("unknown tool", result["tool_calls"][0]["result"]["error"])

    async def test_bad_tool_args_reported(self):
        router = ToolRouter(state_provider=StateMachine())
        router.register(Tool(
            name="echo", description="d",
            schema={"type": "object", "required": ["x"], "properties": {"x": {"type": "integer"}}},
            handler=lambda **k: k,
            allowed_states={State.IDLE},
        ))
        cloud = make_cloud([
            tool_response("c1", "echo", '{"x": "not an int"}'),
            text_response("参数不对"),
        ])
        provider = LLMProvider(mode="cloud", tools=router, cloud_backend=cloud)
        result = await provider.chat_with_tools("x", {})
        self.assertIn("invalid arguments", result["tool_calls"][0]["result"]["error"])

    async def test_malformed_tool_arguments_json(self):
        cloud = make_cloud([
            tool_response("c1", "get_time", "{not json"),
            text_response("ok"),
        ])
        provider = LLMProvider(mode="cloud", tools=self._router(), cloud_backend=cloud)
        result = await provider.chat_with_tools("x", {})
        # 解析失败退化成 {}, 不崩
        self.assertEqual(result["tool_calls"][0]["args"], {})

    async def test_api_error_returns_ok_false(self):
        provider = LLMProvider(
            mode="cloud",
            tools=self._router(),
            cloud_backend=make_cloud([RuntimeError("rate limited")]),
        )
        result = await provider.chat_with_tools("x", {})
        self.assertFalse(result["ok"])
        self.assertIn("rate limited", result["error"])
        self.assertEqual(result["text"], "")

    async def test_api_error_midway_keeps_completed_tool_calls(self):
        cloud = make_cloud([
            tool_response("c1", "get_time", "{}"),
            RuntimeError("第二轮挂了"),
        ])
        provider = LLMProvider(mode="cloud", tools=self._router(), cloud_backend=cloud)
        result = await provider.chat_with_tools("x", {})
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["tool_calls"]), 1, "已完成的调用要保留, 便于排查")

    async def test_tool_loop_round_limit(self):
        # 模型无限要求调工具: 必须停下来并明确报错
        cloud = make_cloud([tool_response("c%d" % i, "get_time", "{}") for i in range(10)])
        provider = LLMProvider(
            mode="cloud", tools=self._router(), cloud_backend=cloud, max_tool_rounds=3
        )
        result = await provider.chat_with_tools("x", {})
        self.assertFalse(result["ok"])
        self.assertIn("exceeded 3 rounds", result["error"])
        self.assertEqual(len(result["tool_calls"]), 3)
        self.assertEqual(len(cloud.client.requests), 3)

    async def test_cloud_without_backend_returns_ok_false(self):
        provider = LLMProvider(mode="cloud", tools=self._router())
        result = await provider.chat_with_tools("x", {})
        self.assertFalse(result["ok"])
        self.assertIn("cloud", result["error"])

    async def test_event_loop_not_blocked_during_cloud_call(self):
        # create() 是同步阻塞调用, 必须丢线程池, 否则会卡住事件循环
        ticks = []

        class _SlowCompletions(_FakeCompletions):
            def create(self, **kwargs):
                import time
                time.sleep(0.05)
                return super().create(**kwargs)

        class _SlowOpenAI(_FakeOpenAI):
            def __init__(self):
                self.chat = _FakeChat([])
                self.chat.completions = _SlowCompletions([text_response("slow")])

        provider = LLMProvider(
            mode="cloud", cloud_backend=CloudBackend(client=_SlowOpenAI())
        )

        async def ticker():
            for _ in range(10):
                ticks.append(1)
                await asyncio.sleep(0.005)

        text, _ = await asyncio.gather(provider.chat("x", {}), ticker())
        self.assertEqual(text, "slow")
        self.assertGreater(len(ticks), 1, "云端调用期间事件循环应当还在跑")


class TestCloudWithToolsDisabledAndEdge(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_chat_with_tools_uses_rules(self):
        engine = RuleEngine(rules=[Rule("r", r".*", lambda m, t, c: "规则答复")])
        provider = LLMProvider(mode="disabled", rule_engine=engine)
        result = await provider.chat_with_tools("随便", {})
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "规则答复")
        self.assertEqual(result["tool_calls"], [], "disabled 不该假装调了工具")
        self.assertEqual(result["mode"], "disabled")

    async def test_edge_chat_with_tools(self):
        provider = LLMProvider(mode="edge", edge_backend=EdgeBackend(reply="edge 回复"))
        result = await provider.chat_with_tools("hi", {})
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "edge 回复")
        self.assertEqual(result["tool_calls"], [])

    async def test_edge_error_is_captured(self):
        def boom(text, ctx):
            raise RuntimeError("模型崩了")

        provider = LLMProvider(mode="edge", edge_backend=EdgeBackend(responder=boom))
        result = await provider.chat_with_tools("hi", {})
        self.assertFalse(result["ok"])
        self.assertIn("模型崩了", result["error"])

    async def test_rule_engine_error_is_captured(self):
        class _BadEngine:
            async def respond(self, text, ctx):
                raise RuntimeError("规则引擎也炸了")

        provider = LLMProvider(mode="disabled", rule_engine=_BadEngine())
        result = await provider.chat_with_tools("hi", {})
        self.assertFalse(result["ok"])
        self.assertIn("规则引擎也炸了", result["error"])


class TestProviderDiagnostics(unittest.TestCase):
    def test_repr(self):
        provider = LLMProvider(mode="edge")
        self.assertIn("edge", repr(provider))

    def test_tools_property(self):
        router = ToolRouter(state_provider=StateMachine())
        provider = LLMProvider(mode="disabled", tools=router)
        self.assertIs(provider.tools, router)

    def test_mode_errors_starts_empty_for_valid_mode(self):
        self.assertEqual(LLMProvider(mode="cloud").mode_errors, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
