#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_chat_memory.py — 纯对话记忆（Phase 7 T9-1）

跑法:
    python tests/test_chat_memory.py

这个文件钉五件事:
  1) **只收"纯对话"**（gui / terminal）—— 定时提示、测试注入、系统消息都不算
  2) **有界**（8000 字 / 80 条；单条太长时只留最近的）
  3) **一个字节都不写盘**（你 T9 定的："不落盘"）—— 这条是**反空转**的:
     把 `open` 换成"一调就炸"，再跑一遍所有写入路径
  4) **记下当时是哪个模式**（idle/study/game/sleep）—— T9-2 判心情的"场景"依据
  5) **Runtime 接线**: 用户话 + 回复各记一条, 直连回复也记, 非对话来源不记

⚠ 用 `IsolatedAsyncioTestCase`（Python 3.8 的 `Runtime.__init__` 要事件循环, 见
  tests/test_read_intents.py 里那段说明）。
"""

import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.core.chat_memory import (  # noqa: E402
    CHAT_SOURCES,
    DEFAULT_KEEP_AFTER_SETTLE,
    DEFAULT_MAX_CHARS,
    DEFAULT_MAX_ENTRIES,
    ROLE_ASSISTANT,
    ROLE_USER,
    ChatMemory,
    Entry,
)

logging.disable(logging.CRITICAL)


class TestWhatCountsAsChat(unittest.TestCase):
    """只收 gui/terminal 的"人跟助手说话"。"""

    def test_only_chat_sources_are_recorded(self):
        memory = ChatMemory()
        self.assertIsNotNone(memory.add_user("你好", source="gui"))
        self.assertIsNotNone(memory.add_user("你好", source="terminal"))
        for source in ("schedule", "ipc", "test", "bus", "?", ""):
            with self.subTest(source=source):
                self.assertIsNone(memory.add_user("这句不该进来", source=source),
                                  "%r 不是纯对话" % source)
        self.assertEqual([e.text for e in memory.entries()], ["你好", "你好"])
        self.assertEqual(CHAT_SOURCES, ("gui", "terminal"), "口径就是这两个")

    def test_blank_text_is_not_recorded(self):
        memory = ChatMemory()
        for text in ("", "   ", "\n\t ", None):
            self.assertIsNone(memory.add_user(text, source="gui"))
        self.assertEqual(len(memory), 0)

    def test_a_reply_from_a_non_chat_turn_is_not_recorded(self):
        memory = ChatMemory()
        self.assertIsNone(memory.add_reply("定时提示的回复", source="schedule"))
        self.assertIsNotNone(memory.add_reply("正常回复", source="gui"))

    def test_bad_role_is_a_programming_error(self):
        memory = ChatMemory()
        with self.assertRaises(ValueError):
            memory.add("system", "系统消息", source="gui")


class TestBounded(unittest.TestCase):
    def test_entries_are_capped(self):
        memory = ChatMemory(max_entries=3, max_chars=10 ** 6)
        for i in range(5):
            memory.add_user("第 %d 句" % i, source="gui")
        self.assertEqual([e.text for e in memory.entries()],
                         ["第 2 句", "第 3 句", "第 4 句"], "挤掉最老的")

    def test_chars_are_capped(self):
        memory = ChatMemory(max_entries=100, max_chars=20)
        for i in range(10):
            memory.add_user("x" * 8, source="gui")
        self.assertLessEqual(memory.chars(), 20)
        self.assertEqual(len(memory), 2, "每条 8 字 -> 只留得下 2 条")

    def test_a_single_huge_message_keeps_its_tail(self):
        memory = ChatMemory(max_chars=100)
        entry = memory.add_user("老" * 200 + "最近说的这句", source="gui")
        self.assertTrue(entry.truncated)
        self.assertEqual(memory.chars(), 100, "一条最多占满整个预算（有界是硬性质）")
        self.assertTrue(entry.text.startswith("…"))
        self.assertTrue(entry.text.endswith("最近说的这句"), "留**最近**说的那半句")

    def test_stats_are_reported(self):
        memory = ChatMemory()
        memory.add_user("a" * 5, source="gui", state="study")
        memory.add_reply("b" * 7, source="gui", state="study")
        stats = memory.stats()
        self.assertEqual(stats["chars"], 12)
        self.assertEqual(stats["entries"], 2)
        self.assertEqual(stats["turns"], 1, "轮 = 用户说了几条")
        self.assertEqual(stats["states"], ["study"])


class TestRolesAndScenario(unittest.TestCase):
    def test_role_state_and_timestamp_are_recorded(self):
        memory = ChatMemory(clock=lambda: 1234.5)
        user = memory.add_user("换一张动漫的", source="gui", state="idle")
        reply = memory.add_reply("好", source="gui", state="study")
        self.assertEqual((user.role, user.ts, user.state), (ROLE_USER, 1234.5, "idle"))
        self.assertEqual((reply.role, reply.ts, reply.state), (ROLE_ASSISTANT, 1234.5, "study"))
        self.assertEqual(memory.turns(), 1)
        self.assertEqual(user.to_dict()["state"], "idle")

    def test_missing_state_is_recorded_as_empty_not_guessed(self):
        memory = ChatMemory()
        entry = memory.add_user("在吗", source="gui")
        self.assertEqual(entry.state, "")
        self.assertNotIn("states", [k for k, v in memory.stats().items() if v] or [])


class TestSettle(unittest.TestCase):
    def test_settle_keeps_the_tail_and_restarts_the_count(self):
        memory = ChatMemory()
        for i in range(10):
            memory.add_user("第 %d 句" % i, source="gui")
            memory.add_reply("回复 %d" % i, source="gui")
        before = memory.chars()
        dropped = memory.settle()
        self.assertEqual(dropped, 20 - DEFAULT_KEEP_AFTER_SETTLE)
        self.assertEqual(len(memory), DEFAULT_KEEP_AFTER_SETTLE)
        self.assertLess(memory.chars(), before, "触发计数从剩下的那几句重新长")
        self.assertEqual(memory.pending_chars(), 0, "触发计数归零（保留的尾巴不算）")
        self.assertEqual(memory.entries()[-1].text, "回复 9", "留下的是最新那几条")

        memory.add_user("结算之后又说了一句", source="gui")
        self.assertEqual(memory.entries()[-1].text, "结算之后又说了一句")

    def test_settle_with_nothing_to_do_is_zero(self):
        memory = ChatMemory()
        self.assertEqual(memory.settle(), 0)
        memory.add_user("就一句", source="gui")
        self.assertEqual(memory.settle(), 0, "还不到保留条数 -> 一条都不用丢")

    def test_pending_is_what_the_trigger_counts(self):
        """触发口径是"自上次结算以来攒的"——保留的那几条不算（T9-3 接线时抓到的坑）。

        不然保留的尾巴自己就能顶满触发线, 于是每轮都满足条件、构建一遍又一遍。
        """
        memory = ChatMemory()
        memory.add_user("x" * 100, source="gui")
        memory.add_reply("y" * 50, source="gui")
        self.assertEqual((memory.pending_chars(), memory.pending_turns()), (150, 1))
        self.assertEqual(memory.settle(), 0)
        self.assertEqual(memory.chars(), 150, "全清? 不 —— 还不到保留条数, 一条没丢")
        self.assertEqual(memory.pending_chars(), 0, "但触发计数归零了")
        self.assertEqual(memory.pending_turns(), 0)
        memory.add_user("新的一句", source="gui")
        self.assertEqual(memory.pending_chars(), 4, "从 0 重新长")
        self.assertEqual(memory.pending_turns(), 1)

    def test_pending_survives_eviction(self):
        """内存挤掉老条目**不该**把触发计数也挤掉（挤掉的是话, 不是"攒过"这件事）。"""
        memory = ChatMemory(max_entries=2, max_chars=10 ** 6)
        for index in range(4):
            memory.add_user("第 %d 句" % index, source="gui")
        self.assertEqual(len(memory), 2)
        self.assertEqual(memory.chars(), 10, "内存里只剩 2 条")
        self.assertEqual(memory.pending_chars(), 20, "但攒过 4 条 —— 触发照旧算这 4 条")
        self.assertEqual(memory.pending_turns(), 4)


class TestExcerpt(unittest.TestCase):
    def test_excerpt_prefers_the_newest_and_keeps_chronology(self):
        memory = ChatMemory()
        memory.add_user("很久以前说的话", source="gui", state="idle")
        memory.add_reply("很久以前的回复", source="gui", state="idle")
        memory.add_user("最近说的话", source="gui", state="study")
        memory.add_reply("最近的回复", source="gui", state="study")
        text = memory.excerpt(max_chars=40)
        self.assertIn("最近说的话", text, "预算不够先丢最老的")
        self.assertNotIn("很久以前说的话", text)
        self.assertLess(text.index("最近说的话"), text.index("最近的回复"), "按时间顺序")
        self.assertTrue(text.startswith("[study] "), "带场景标记")

    def test_excerpt_without_state_markers(self):
        memory = ChatMemory()
        memory.add_user("你好", source="gui", state="idle")
        self.assertEqual(memory.excerpt(with_state=False), "用户: 你好")

    def test_excerpt_is_empty_when_nothing_was_said(self):
        self.assertEqual(ChatMemory().excerpt(), "")

    def test_excerpt_of_a_single_huge_entry_keeps_its_tail(self):
        memory = ChatMemory()
        memory.add_user("老" * 100 + "最后这句", source="gui", state="study")
        text = memory.excerpt(max_chars=20)
        self.assertLessEqual(len(text), 20)
        self.assertTrue(text.endswith("最后这句"), "最新那句自己就超预算 -> 留它最近的一半")

    def test_excerpt_never_exceeds_the_budget(self):
        memory = ChatMemory()
        for i in range(20):
            memory.add_user("第 %d 句" % i, source="gui", state="study")
        for budget in (10, 30, 100, 500):
            with self.subTest(budget=budget):
                self.assertLessEqual(len(memory.excerpt(max_chars=budget)), budget)


class TestNothingHitsTheDisk(unittest.TestCase):
    """你 T9 定的"不落盘" —— 用"一 open 就炸"来反空转地证明。"""

    def test_operations_never_open_a_file(self):
        memory = ChatMemory()

        def boom(*args, **kwargs):
            raise AssertionError("纯对话记忆不该碰文件: %r" % (args[:1],))

        with mock.patch("builtins.open", boom):
            for i in range(120):
                memory.add_user("第 %d 句话" % i, source="gui", state="study")
                memory.add_reply("回复 %d" % i, source="gui", state="study")
            memory.chars()
            memory.turns()
            memory.entries(5)
            memory.stats()
            memory.excerpt(200)
            memory.settle()
        self.assertLessEqual(len(memory), DEFAULT_MAX_ENTRIES)
        self.assertLessEqual(memory.chars(), DEFAULT_MAX_CHARS)
        self.assertEqual(DEFAULT_MAX_CHARS, 8000)
        self.assertEqual(Entry.__module__, "agent.core.chat_memory")


class TestRuntimeWiring(unittest.IsolatedAsyncioTestCase):
    """`Runtime.handle_event` / `_deliver_reply` 把对话记进内存（含直连回复）。"""

    def _runtime(self):
        from agent.main import Runtime

        return Runtime(config={"ipc": {"socket_path": ""}},
                       start_native=False, start_terminal=False,
                       log=logging.getLogger("test.chat_memory"))

    async def test_user_turn_and_reply_are_recorded_with_the_mode(self):
        runtime = self._runtime()
        seen = []
        runtime.on_reply = lambda text: seen.append(text)

        class FakeLLM(object):
            async def chat_with_tools(self, text, context):
                return {"ok": True, "text": "好的，这就换", "tool_calls": []}

        runtime.llm = FakeLLM()
        runtime.state = None                    # 状态机没起来 -> "unknown"
        reply = await runtime.handle_event({"source": "gui", "text": "换一张壁纸"})
        self.assertEqual(reply, "好的，这就换")
        entries = runtime.chat_memory.entries()
        self.assertEqual([(e.role, e.text) for e in entries],
                         [(ROLE_USER, "换一张壁纸"), (ROLE_ASSISTANT, "好的，这就换")])
        self.assertEqual(entries[0].state, "unknown", "拿不到模式就说 unknown, 不猜")
        self.assertEqual(runtime.chat_memory.turns(), 1)
        self.assertEqual(seen, [reply])

    async def test_a_direct_answer_is_recorded_too(self):
        runtime = self._runtime()
        runtime.llm = None                      # 直连不该碰模型
        runtime.state = None
        # 装上假的只读入口（等价于 read_services() 里那几个）
        runtime.read_services = lambda: {"music_state": lambda: {
            "ok": True, "track_id": None, "title": "", "playing": False,
            "position_s": 0, "duration_s": 0}}
        reply = await runtime.handle_event({"source": "gui", "text": "现在在放什么"})
        self.assertIn("没在放歌", reply)
        self.assertEqual(len(runtime.chat_memory), 2, "直连的问答也是对话")
        self.assertEqual(runtime.chat_memory.turns(), 1)

    async def test_scheduler_events_do_not_enter_the_memory(self):
        runtime = self._runtime()

        class FakeLLM(object):
            async def chat_with_tools(self, text, context):
                return {"ok": True, "text": "到点了，该学习了", "tool_calls": []}

        runtime.llm = FakeLLM()
        runtime.state = None
        await runtime.handle_event({"source": "schedule", "text": "该学习了"})
        self.assertEqual(len(runtime.chat_memory), 0, "定时提示不是纯对话")

    async def test_a_failed_turn_records_the_user_line_but_no_reply(self):
        runtime = self._runtime()

        class BrokenLLM(object):
            async def chat_with_tools(self, text, context):
                raise RuntimeError("模型炸了")

        runtime.llm = BrokenLLM()
        runtime.state = None
        reply = await runtime.handle_event({"source": "gui", "text": "在吗"})
        self.assertIsNone(reply)
        self.assertEqual([e.text for e in runtime.chat_memory.entries()], ["在吗"],
                         "用户说过的话要留下（那是语料）, 回复没有就没有")


if __name__ == "__main__":
    unittest.main(verbosity=2)
