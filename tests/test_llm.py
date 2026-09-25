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
from unittest import mock

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
    """假 client —— **刻意学 openai SDK 的严格性**: 未知的顶层关键字要抛 TypeError。

    ⚠ T8-5c-4 板端实测踩到过: 我们往 `create()` 传了个 SDK 不认的顶层关键字
    （`chat_template_kwargs=`），PC 上的替身**照单全收**, 板端却
    `TypeError: create() got an unexpected keyword argument` → 整轮降级。
    替身比现实宽松 = 这类 bug 只在板端暴露, 所以这里把允许的键写死。
    """

    #: openai SDK 真认的顶层关键字（我们只会用这些）
    ALLOWED = ("model", "messages", "tools", "tool_choice", "max_tokens",
               "temperature", "extra_body")

    def __init__(self, script):
        self._script = list(script)
        self.requests = []

    def create(self, **kwargs):
        unexpected = sorted(set(kwargs) - set(self.ALLOWED))
        if unexpected:
            raise TypeError("create() got an unexpected keyword argument %r"
                            % unexpected[0])
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


def make_edge(script, **kwargs):
    """edge 后端 + 假 client —— 与 make_cloud 同一套替身, 所以工具循环是**同一份代码**。"""
    return EdgeBackend(client=_FakeOpenAI(script), **kwargs)


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
        edge = make_edge([text_response("from edge")])
        provider = LLMProvider(mode="edge", edge_backend=edge)
        self.assertEqual(await provider.chat("hi", {}), "from edge")
        self.assertEqual(len(edge.client.requests), 1)

    async def test_edge_plain_chat_carries_config_values_and_no_think(self):
        # T2: edge 的纯文本调用也要走真后端, 并且把 llm.max_tokens/temperature 带上
        edge = make_edge([text_response("好")], model="qwen3-0.6b", max_tokens=512,
                         temperature=0.7)
        provider = LLMProvider(mode="edge", edge_backend=edge)
        self.assertEqual(await provider.chat("hi", {}), "好")

        request = edge.client.requests[0]
        self.assertEqual(request["model"], "qwen3-0.6b")
        self.assertEqual(request["max_tokens"], 512)
        self.assertEqual(request["temperature"], 0.7)
        # Qwen3 的软开关: 不加它, 0.6B 会把 token 预算烧在思考上, 正文是空的
        self.assertIn("/no_think", request["messages"][0]["content"])
        # ⚠ T8-5b: **两处都写** —— user 消息末尾那份才是真正把思考压住的那份
        #   （板端 6 条实测: 只在 system 里时有一条想了 339 字 / 211 s;
        #    两处都写之后 6 条思考全为 0 字, 成功率 1/6 → 4/6）
        self.assertIn("/no_think", request["messages"][-1]["content"])
        # 纯文本调用不带工具
        self.assertNotIn("tools", request)

    async def test_edge_without_no_think_does_not_add_it(self):
        edge = make_edge([text_response("x")], no_think=False)
        provider = LLMProvider(mode="edge", edge_backend=edge)
        await provider.chat("hi", {})
        self.assertNotIn("/no_think", edge.client.requests[0]["messages"][0]["content"])
        self.assertNotIn("/no_think", edge.client.requests[0]["messages"][-1]["content"])

    async def test_no_think_goes_after_the_user_text_not_before(self):
        # 顺序别搞反: 软开关要在**这一句之后**（贴住"该你答了"的位置）
        edge = make_edge([text_response("x")])
        provider = LLMProvider(mode="edge", edge_backend=edge)
        await provider.chat("现在几点", {})
        content = edge.client.requests[0]["messages"][-1]["content"]
        self.assertTrue(content.startswith("现在几点"), content)
        self.assertTrue(content.endswith("/no_think"), content)

    async def test_edge_requests_carry_the_template_switch(self):
        """T8-5c-4 不变量: edge 的**每一次**请求都要关思考 —— 两层一起上。

        ① user 消息末尾的 `/no_think`（模型学过的软开关）
        ② `chat_template_kwargs={"enable_thinking": false}`（llama.cpp 的模板开关,
           模板里为假时直接吐一个空思考块 —— 与第几轮无关, 所以它在"工具结果那一轮"
           也照样成立）

        ⚠ ② 必须走 `extra_body=`: openai SDK 不接受未知的**顶层**关键字
        （板端实测: 直接传顶层会 TypeError → 整轮降级）。替身 client 现在就学这条严格性。
        """
        edge = make_edge([text_response("x")])
        provider = LLMProvider(mode="edge", edge_backend=edge)
        await provider.chat("hi", {})
        self.assertEqual(edge.client.requests[0]["extra_body"],
                         {"chat_template_kwargs": {"enable_thinking": False}})

    async def test_every_round_of_the_tool_loop_is_thinking_free(self):
        """工具循环的**每一轮**（含带工具结果那一轮）都必须关思考 —— 这才是"不变量"。"""
        router = ToolRouter(state_provider=StateMachine())      # 初始 IDLE
        router.register(Tool(
            name="get_time",
            description="取当前时间",
            schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=lambda **k: "12:00",
            allowed_states={State.IDLE},
        ))
        edge = make_edge([
            tool_response("call_1", "get_time", "{}"),
            text_response("现在是 12:00"),
        ])
        provider = LLMProvider(mode="edge", tools=router, edge_backend=edge)
        result = await provider.chat_with_tools("几点了", {})
        self.assertTrue(result["ok"], result)

        self.assertEqual(len(edge.client.requests), 2, "两轮请求")
        for index, request in enumerate(edge.client.requests):
            with self.subTest(round=index):
                self.assertEqual(request["extra_body"],
                                 {"chat_template_kwargs": {"enable_thinking": False}},
                                 "第 %d 轮没带模板开关" % (index + 1))
                users = [m for m in request["messages"] if m.get("role") == "user"]
                self.assertTrue(users, "每一轮都该看得见那条 user 消息")
                self.assertIn("/no_think", users[-1]["content"],
                              "第 %d 轮的 user 侧软开关丢了" % (index + 1))
        # 第二轮的最后一条是工具结果（不是 user）—— 这正是"只靠软开关不可靠"的原因
        self.assertEqual(edge.client.requests[1]["messages"][-1]["role"], "tool")

    async def test_no_think_off_means_no_template_switch(self):
        edge = make_edge([text_response("x")], no_think=False)
        provider = LLMProvider(mode="edge", edge_backend=edge)
        await provider.chat("hi", {})
        self.assertNotIn("extra_body", edge.client.requests[0])

    async def test_cloud_never_gets_the_llama_template_switch(self):
        # `chat_template_kwargs` 是 llama.cpp 的东西: 发给云端 API 会被当成未知参数
        cloud = make_cloud([text_response("ok")])
        provider = LLMProvider(mode="cloud", cloud_backend=cloud)
        await provider.chat("hi", {})
        self.assertNotIn("extra_body", cloud.client.requests[0])

    async def test_the_fake_client_is_as_strict_as_the_real_sdk(self):
        """替身必须**比我们严**（板端那次 TypeError 就是替身太宽松漏掉的）: """
        edge = make_edge([text_response("x")])
        with self.assertRaises(TypeError) as ctx:
            edge.client.chat.completions.create(model="m", messages=[],
                                                chat_template_kwargs={"enable_thinking": False})
        self.assertIn("chat_template_kwargs", str(ctx.exception))

    async def test_edge_default_backend_points_at_local_llama_server(self):
        # 不注入后端时按 config 的 llm: 段造 (llm.port / model_name / local_api_key)
        provider = LLMProvider(
            mode="edge",
            config_loader=lambda: {
                "llm": {
                    "mode": "edge",
                    "port": 9123,
                    "model_name": "qwen3-0.6b",
                    "local_api_key": "sk-local",
                    "max_tokens": 256,
                    "temperature": 0.2,
                    "model_path": "/home/kickpi/model/qwen3.gguf",
                }
            },
        )
        backend = provider.edge_backend
        self.assertEqual(backend.base_url, "http://127.0.0.1:9123/v1")
        self.assertEqual(backend.model, "qwen3-0.6b")
        self.assertEqual(backend.api_key, "sk-local")
        self.assertEqual(backend.max_tokens, 256)
        self.assertEqual(backend.temperature, 0.2)
        self.assertEqual(backend.model_path, "/home/kickpi/model/qwen3.gguf")

    async def test_edge_config_gaps_fall_back_to_defaults(self):
        # 键写漏/写坏不该让 provider 起不来, 各自退回默认值
        provider = LLMProvider(
            mode="edge",
            config_loader=lambda: {"llm": {"mode": "edge", "port": "not a port",
                                           "max_tokens": "很多", "temperature": None}},
        )
        backend = provider.edge_backend
        self.assertEqual(backend.base_url, "http://127.0.0.1:9000/v1")
        self.assertEqual(backend.model, "qwen3-0.6b")
        self.assertIsNone(backend.max_tokens)
        self.assertIsNone(backend.temperature)

    async def test_edge_request_failure_propagates_in_chat(self):
        # chat() 的约定: 失败照抛 (调用方要能知道模型挂了)
        provider = LLMProvider(
            mode="edge", edge_backend=make_edge([ConnectionError("connection refused")])
        )
        with self.assertRaises(ConnectionError):
            await provider.chat("hi", {})

    async def test_switching_mode_switches_backend(self):
        engine = RuleEngine(rules=[Rule("r", r".*", lambda m, t, c: "rules")])
        provider = LLMProvider(
            mode="disabled",
            rule_engine=engine,
            edge_backend=make_edge([text_response("edge")]),
            cloud_backend=make_cloud([text_response("cloud")]),
        )
        self.assertEqual(await provider.chat("x", {}), "rules")
        provider.set_mode("edge")
        self.assertEqual(await provider.chat("x", {}), "edge")
        provider.set_mode("cloud")
        self.assertEqual(await provider.chat("x", {}), "cloud")

    async def test_disabled_never_touches_cloud_or_edge(self):
        edge = make_edge([text_response("edge")])
        cloud = make_cloud([text_response("cloud")])
        provider = LLMProvider(
            mode="disabled", edge_backend=edge, cloud_backend=cloud
        )
        await provider.chat("x", {})
        await provider.chat_with_tools("x", {})
        self.assertEqual(edge.client.requests, [])
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
        # 用 sys.modules["openai"] = None **模拟**"SDK 没装"。
        #
        # 不能指望它真的没装: 板端是有 openai 的 (部署 llm/ 时装的), 于是这里会
        # 真的去连 API, 报出来的是 "Network is unreachable" 而不是我们想验的安装
        # 提示 —— 环境一变测试就红, 而它验的本来不是环境。
        # (sys.modules 里放 None 时, `import openai` 会抛 ImportError)
        backend = CloudBackend(api_key="sk-test")
        provider = LLMProvider(mode="cloud", cloud_backend=backend)
        with mock.patch.dict(sys.modules, {"openai": None}):
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
        # degraded 是 T2 加的: 非空 = 这条是规则兜底的, 不是模型答的
        # tool_failures 是 T7-4 加的: 失败过的工具（已经写进 text 末尾）
        self.assertEqual(
            set(result),
            {"ok", "text", "tool_calls", "error", "mode", "degraded", "tool_failures"}
        )
        self.assertIsNone(result["degraded"])
        self.assertEqual(result["tool_failures"], [])

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


class TestToolFailuresAreToldToTheUser(unittest.IsolatedAsyncioTestCase):
    """T7-4 (c): 工具失败了, **正文里必须有一句**。

    为什么非做不可（板端实测, 2026-09-22）: 工具明明返回
    `{"ok": false, "error": "scene 轴上没有 'darkness'…"}`, 0.6B 模型回给用户的却是
    "已更换为宁静的深色风景"。工具结果只有模型看得见, 所以失败得由**机制**保证出现
    （追加进 `text`）, 不能指望模型转述。
    """

    FAILURE = {"ok": False, "error": "scene 轴上没有 'darkness' 这条标签",
               "tell_user": "换壁纸没有成功：scene 轴上没有 'darkness' 这条标签"}

    def _router(self, handler=None, states=(State.IDLE,)):
        router = ToolRouter(state_provider=StateMachine())     # 初始 IDLE
        router.register(Tool(
            name="next_wallpaper",
            description="换壁纸",
            schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=handler if handler is not None else (lambda **k: dict(self.FAILURE)),
            allowed_states=set(states),
        ))
        return router

    async def test_a_handler_failure_is_appended_to_the_reply(self):
        cloud = make_cloud([
            tool_response("call_1", "next_wallpaper", "{}"),
            text_response("已更换为宁静的深色风景。"),
        ])
        provider = LLMProvider(mode="cloud", tools=self._router(), cloud_backend=cloud)
        result = await provider.chat_with_tools("换一张安静的深色风景", {})

        self.assertIn("已更换", result["text"], "模型自己的话照旧留着")
        self.assertIn("换壁纸没有成功", result["text"], "机制补的那句必须在正文里")
        self.assertEqual(result["tool_failures"],
                         ["⚠ 换壁纸没有成功：scene 轴上没有 'darkness' 这条标签"])

    async def test_a_router_level_failure_is_appended_too(self):
        # 工具只在 STUDY 允许, 当前 IDLE -> 路由层拒绝（这正是"SLEEP/GAME 下换不了"那条路）
        cloud = make_cloud([
            tool_response("call_1", "next_wallpaper", "{}"),
            text_response("好的，已经换好了。"),
        ])
        provider = LLMProvider(mode="cloud", tools=self._router(states=(State.STUDY,)),
                               cloud_backend=cloud)
        result = await provider.chat_with_tools("换一张", {})
        self.assertIn("没有成功", result["text"])
        self.assertIn("not allowed", result["text"], "路由层的原因也要带上")

    async def test_unknown_tool_is_covered(self):
        cloud = make_cloud([
            tool_response("call_1", "definitely_not_a_tool", "{}"),
            text_response("好。"),
        ])
        provider = LLMProvider(mode="cloud", tools=self._router(), cloud_backend=cloud)
        result = await provider.chat_with_tools("x", {})
        self.assertIn("unknown tool", result["text"])

    async def test_success_adds_nothing(self):
        cloud = make_cloud([
            tool_response("call_1", "next_wallpaper", "{}"),
            text_response("好，换了。"),
        ])
        provider = LLMProvider(
            mode="cloud", tools=self._router(handler=lambda **k: {"ok": True, "path": "/w/1.png"}),
            cloud_backend=cloud)
        result = await provider.chat_with_tools("换一张", {})
        self.assertEqual(result["tool_failures"], [])
        self.assertEqual(result["text"], "好，换了。", "成功时一个字都不该多加")

    async def test_the_note_is_the_whole_text_when_the_model_said_nothing(self):
        # 正文为空时, text 就该只剩那句如实说明, 不是 "None\n\n⚠ …"
        cloud = make_cloud([
            tool_response("call_1", "next_wallpaper", "{}"),
            text_response(None),
        ])
        provider = LLMProvider(mode="cloud", tools=self._router(), cloud_backend=cloud)
        result = await provider.chat_with_tools("换一张", {})
        self.assertTrue(result["text"].startswith("⚠ "), result["text"])
        self.assertNotIn("None", result["text"])

    def test_duplicate_failures_are_collapsed(self):
        from agent.llm.provider import _result

        calls = [
            {"name": "next_wallpaper", "args": {},
             "result": {"ok": True, "result": dict(self.FAILURE)}},
            {"name": "next_wallpaper", "args": {},
             "result": {"ok": True, "result": dict(self.FAILURE)}},
        ]
        result = _result(True, "好", calls, None, "cloud")
        self.assertEqual(len(result["tool_failures"]), 1, "同一句说一遍就够")

    def test_notes_fall_back_to_error_when_there_is_no_tell_user(self):
        from agent.llm.provider import _result

        calls = [{"name": "list_wallpaper_tags", "args": {},
                  "result": {"ok": True, "result": {"ok": False, "error": "还没有标签数据"}}}]
        result = _result(True, "", calls, None, "cloud")
        self.assertEqual(result["text"], "⚠ list_wallpaper_tags 没有成功：还没有标签数据")


class TestDisabledPath(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_chat_with_tools_uses_rules(self):
        engine = RuleEngine(rules=[Rule("r", r".*", lambda m, t, c: "规则答复")])
        provider = LLMProvider(mode="disabled", rule_engine=engine)
        result = await provider.chat_with_tools("随便", {})
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "规则答复")
        self.assertEqual(result["tool_calls"], [], "disabled 不该假装调了工具")
        self.assertEqual(result["mode"], "disabled")

    async def test_rule_engine_error_is_captured(self):
        class _BadEngine:
            async def respond(self, text, ctx):
                raise RuntimeError("规则引擎也炸了")

        provider = LLMProvider(mode="disabled", rule_engine=_BadEngine())
        result = await provider.chat_with_tools("hi", {})
        self.assertFalse(result["ok"])
        self.assertIn("规则引擎也炸了", result["error"])

class TestEdgeWithTools(unittest.IsolatedAsyncioTestCase):
    """T2: edge 走**同一个**工具循环 (你选的 b —— 给 edge 补 function-call 解析)。

    替身是同一套假 openai client, 所以这里验的其实是"循环对 edge 也成立":
    工具被真执行、结果回喂模型、状态权限照旧、轮数照旧。
    """

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

    async def test_edge_plain_reply_without_tools(self):
        provider = LLMProvider(mode="edge", edge_backend=make_edge([text_response("edge 回复")]))
        result = await provider.chat_with_tools("hi", {})
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "edge 回复")
        self.assertEqual(result["tool_calls"], [])
        self.assertIsNone(result["degraded"])
        self.assertEqual(result["mode"], "edge")

    async def test_edge_runs_the_tool_loop(self):
        edge = make_edge([
            tool_response("call_1", "get_time", "{}"),
            text_response("现在是 12:00"),
        ])
        provider = LLMProvider(mode="edge", tools=self._router(), edge_backend=edge)
        result = await provider.chat_with_tools("几点了", {})

        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "现在是 12:00")
        self.assertEqual(len(result["tool_calls"]), 1)
        self.assertEqual(result["tool_calls"][0]["result"], {"ok": True, "result": "12:00"})
        # 两轮: 第一轮要工具, 第二轮给答复; 而且第一轮就带了工具清单
        self.assertEqual(len(edge.client.requests), 2)
        self.assertEqual(edge.client.requests[0]["tools"][0]["function"]["name"], "get_time")
        self.assertEqual(edge.client.requests[0]["tool_choice"], "auto")
        # 第二轮要把工具结果回喂回去
        roles = [m.get("role") for m in edge.client.requests[1]["messages"]]
        self.assertIn("tool", roles)

    async def test_edge_tool_round_limit_is_the_same(self):
        edge = make_edge([tool_response("c%d" % i, "get_time", "{}") for i in range(10)])
        provider = LLMProvider(
            mode="edge", tools=self._router(), edge_backend=edge, max_tool_rounds=3
        )
        result = await provider.chat_with_tools("x", {})
        self.assertFalse(result["ok"])
        self.assertIn("exceeded 3 rounds", result["error"])
        self.assertEqual(len(result["tool_calls"]), 3)

    async def test_edge_backend_down_degrades_to_rules_and_says_so(self):
        # 板端 llama-server 没起来时: 答复照给 (规则兜底), 但必须标出这是降级
        engine = RuleEngine(rules=[Rule("r", r".*", lambda m, t, c: "规则答复")])
        provider = LLMProvider(
            mode="edge",
            tools=self._router(),
            edge_backend=make_edge([ConnectionError("connection refused")]),
            rule_engine=engine,
        )
        result = await provider.chat_with_tools("几点了", {})

        self.assertTrue(result["ok"], "降级是为了还能答话, 不是让整轮失败")
        # 正文里必须带那句说明 —— GUI 只看正文, 看不到日志
        self.assertTrue(result["text"].startswith("（板端模型没有响应"), result["text"])
        self.assertIn("规则答复", result["text"])
        self.assertEqual(result["tool_calls"], [], "后端都没答上, 不该有工具调用")
        self.assertIsNone(result["error"])
        self.assertIn("ConnectionError", result["degraded"])
        self.assertIn("connection refused", result["degraded"])

    async def test_edge_degrade_also_reports_when_rules_fail(self):
        class _BadEngine:
            async def respond(self, text, ctx):
                raise RuntimeError("规则引擎也炸了")

        provider = LLMProvider(
            mode="edge",
            edge_backend=make_edge([ConnectionError("refused")]),
            rule_engine=_BadEngine(),
        )
        result = await provider.chat_with_tools("hi", {})
        self.assertFalse(result["ok"])
        self.assertIn("ConnectionError", result["error"])
        self.assertIn("规则兜底也失败", result["error"])

    async def test_edge_empty_text_with_thinking_degrades_with_a_reason(self):
        # 板端实测的坑: Qwen3 把预算烧在思考上 -> content 空 + finish_reason=length
        def _thinking_only():
            response = _FakeResponse(content=None)
            response.choices[0].finish_reason = "length"
            response.choices[0].message.reasoning_content = "嗯" * 40
            return response

        engine = RuleEngine(rules=[Rule("r", r".*", lambda m, t, c: "规则答复")])
        provider = LLMProvider(
            mode="edge", edge_backend=make_edge([_thinking_only()]), rule_engine=engine
        )
        result = await provider.chat_with_tools("你好", {})

        self.assertTrue(result["ok"])
        self.assertIn("规则答复", result["text"])
        self.assertIn("finish_reason=length", result["degraded"])
        self.assertIn("思考", result["degraded"])

    async def test_edge_empty_text_without_thinking_degrades_too(self):
        engine = RuleEngine(rules=[Rule("r", r".*", lambda m, t, c: "规则答复")])
        provider = LLMProvider(
            mode="edge",
            edge_backend=make_edge([_FakeResponse(content="")]),
            rule_engine=engine,
        )
        result = await provider.chat_with_tools("你好", {})
        self.assertIn("规则答复", result["text"])
        self.assertIn("没有给出正文", result["degraded"])

    async def test_cloud_empty_text_keeps_the_old_behaviour(self):
        # 明确记一条边界: cloud 的空正文**不降级**(保持既有行为), 只有 edge 会
        provider = LLMProvider(
            mode="cloud",
            cloud_backend=make_cloud([_FakeResponse(content="")]),
            rule_engine=RuleEngine(rules=[Rule("r", r".*", lambda m, t, c: "规则答复")]),
        )
        result = await provider.chat_with_tools("你好", {})
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "")
        self.assertIsNone(result["degraded"])

    async def test_edge_is_ready_is_offline_but_honest(self):
        # 注入了 client -> 能发请求; 缺 base_url/model -> 明确不可用
        self.assertTrue(make_edge([text_response("x")]).is_ready())
        self.assertFalse(EdgeBackend(base_url="", model="").is_ready())
        # 描述里要有"连哪儿 + 哪个模型", 板端排障就看这一行
        described = make_edge([text_response("x")], model="m").describe()
        self.assertIn("model=m", described)
        self.assertIn("llama-server", described)


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
