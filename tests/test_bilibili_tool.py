#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`agent/tools/bilibili.py` 的单测（**不联网**：入口是替身）。

钉住的是"模型只能干这一件事"这套边界:
  · 只有一个必填参数 `keyword`, **没有 action**（清队列/切集只走 Agent, 不进工具）;
  · **只在 GAME 可见**（你定的 D6）;
  · 关键词原样交给运行时入口, **不自己搜、不自己碰队列**;
  · 空关键词/缺入口/入参错位都**如实**（不猜内容）;
  · 说明里写清"只排不播 / 板端播放 / 别承诺高清"—— 免得 0.6B 自己脑补。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.core.state_machine import State                        # noqa: E402
from agent.core.tool_router import ToolRouter                     # noqa: E402
from agent.tools import bilibili                                  # noqa: E402


class TestSchema(unittest.TestCase):

    def test_only_one_required_parameter(self):
        tool = bilibili.build({"bilibili_search": lambda keyword="": {}})
        self.assertEqual(tool.name, "bilibili_search")
        self.assertEqual(tool.schema["required"], ["keyword"])
        self.assertEqual(sorted(tool.schema["properties"]), ["keyword"])
        self.assertFalse(tool.schema["additionalProperties"])
        self.assertNotIn("action", tool.schema["properties"],
                         "清队列/切集那些**不该**出现在模型能看到的参数里")

    def test_only_game_state(self):
        tool = bilibili.build({"bilibili_search": lambda keyword="": {}})
        self.assertEqual(tool.allowed_states, {State.GAME})
        self.assertEqual(bilibili.ALLOWED_STATES, (State.GAME,))

    def test_declares_its_own_timeout(self):
        tool = bilibili.build({"bilibili_search": lambda keyword="": {}})
        self.assertEqual(tool.timeout_s, bilibili.TIMEOUT_S)
        self.assertGreater(bilibili.TIMEOUT_S, 5.0,
                           "搜一次要走网络, 比路由默认的 5 s 长")

    def test_description_says_the_important_things(self):
        text = bilibili.DESCRIPTION
        self.assertIn("只排不播", text)
        self.assertIn("板端", text)
        self.assertIn("GAME", text)
        self.assertIn("360P", text)                   # 别承诺高清
        self.assertIn("cookie", text)
        self.assertIn("别调", text)                    # 用户没点名就别调

    def test_keyword_length_is_bounded(self):
        tool = bilibili.build({"bilibili_search": lambda keyword="": {}})
        keyword = tool.schema["properties"]["keyword"]
        self.assertEqual(keyword["minLength"], 1)
        self.assertLessEqual(keyword["maxLength"], 64)


class TestNormalize(unittest.TestCase):

    def test_strips_whitespace(self):
        self.assertEqual(bilibili.normalize({"keyword": "  Luna say maybe  "}),
                         {"keyword": "Luna say maybe"})

    def test_empty_literals_are_treated_as_missing(self):
        for value in ("", "   ", "none", "null", "N/A", "无", "空"):
            with self.subTest(value=value):
                self.assertEqual(bilibili.normalize({"keyword": value}), {})

    def test_none_is_dropped(self):
        self.assertEqual(bilibili.normalize({"keyword": None}), {})

    def test_list_takes_the_first_non_empty(self):
        self.assertEqual(bilibili.normalize({"keyword": ["", " 初雪樱 ", "hoi4"]}),
                         {"keyword": "初雪樱"})

    def test_alias_keys_are_moved(self):
        for alias in ("query", "q", "text", "search", "content"):
            with self.subTest(alias=alias):
                self.assertEqual(bilibili.normalize({alias: " stellaris "}),
                                 {"keyword": "stellaris"})
        # keyword 已经有值时, 别名直接丢掉（别猜"哪个才是真的"）
        self.assertEqual(bilibili.normalize({"keyword": "a", "query": "b"}), {"keyword": "a"})

    def test_does_not_invent_content(self):
        self.assertEqual(bilibili.normalize({}), {})
        # 不认识的键**原样留着**（归一化只做等价改写）—— 由 schema 如实拒掉, 这里不替它猜
        self.assertEqual(bilibili.normalize({"other": "x"}), {"other": "x"})


class TestBuildAndHandler(unittest.TestCase):

    def test_missing_entry_skips_the_tool(self):
        self.assertIsNone(bilibili.build({}))
        self.assertIsNone(bilibili.build({"bilibili_search": None}))
        self.assertIsNone(bilibili.build({"bilibili_search": "not callable"}))

    def test_handler_forwards_the_keyword(self):
        seen = []

        def entry(keyword=""):
            seen.append(keyword)
            return {"ok": True, "count": 12, "index": 0}

        tool = bilibili.build({"bilibili_search": entry})
        result = tool.handler(keyword="Luna say maybe")
        self.assertEqual(seen, ["Luna say maybe"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 12)

    def test_empty_keyword_is_an_honest_failure(self):
        tool = bilibili.build({"bilibili_search": lambda keyword="": {"ok": True}})
        result = tool.handler(keyword="   ")
        self.assertFalse(result["ok"])
        self.assertIn("关键词", result["tell_user"])
        self.assertIn("keyword", result["error"])

    def test_entry_failure_is_reported_not_raised(self):
        def entry(keyword=""):
            raise RuntimeError("被 B 站风控了（HTTP 412）—— 等几分钟再试")

        tool = bilibili.build({"bilibili_search": entry})
        result = tool.handler(keyword="x")
        self.assertFalse(result["ok"])
        self.assertIn("风控", result["tell_user"])
        self.assertIn("风控", result["error"])


class TestThroughTheRouter(unittest.IsolatedAsyncioTestCase):

    def router(self, state):
        from agent.core.state_machine import StateMachine

        machine = StateMachine()
        if state is not State.IDLE:
            machine.transition(state, "test")
        seen = []

        def entry(keyword=""):
            seen.append(keyword)
            return {"ok": True, "count": 3, "index": 0, "keyword": keyword}

        router = ToolRouter(state_provider=machine,
                            services={"bilibili_search": entry})
        router.register(bilibili.build({"bilibili_search": entry}))
        return router, seen

    async def test_allowed_in_game(self):
        router, seen = self.router(State.GAME)
        result = await router.execute("bilibili_search", {"keyword": "hoi4"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(seen, ["hoi4"])

    async def test_refused_outside_game(self):
        for state in (State.IDLE, State.STUDY, State.SLEEP):
            with self.subTest(state=state.value):
                router, seen = self.router(state)
                result = await router.execute("bilibili_search", {"keyword": "hoi4"})
                self.assertFalse(result["ok"])
                self.assertIn("not allowed", result["error"])
                self.assertEqual(seen, [], "被拒时 handler 一次都不该跑")

    async def test_alias_key_survives_the_router(self):
        # 归一化在**校验前**跑: 模型写 query= 也能过 schema（T8-5c 的那条规矩）
        router, seen = self.router(State.GAME)
        result = await router.execute("bilibili_search", {"query": " stellaris "})
        self.assertTrue(result["ok"], result)
        self.assertEqual(seen, ["stellaris"])

    async def test_empty_keyword_is_refused_before_running(self):
        router, seen = self.router(State.GAME)
        result = await router.execute("bilibili_search", {"keyword": "none"})
        self.assertFalse(result["ok"])
        self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
