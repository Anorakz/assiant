#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_user_profile.py — 用户画像内核（Phase 7 T9-2）

跑法:
    python tests/test_user_profile.py

钉五件事:
  1) **前两路是权重, 不是计数**: 权重 = 占比配方（IP 0.6 用量 / 0.3 提及 / 0.1 近期;
     歌手 0.7 播放 / 0.3 提及）, 每个分量都留了分子分母能复核
  2) **负反馈清零**: 负向词 + 实体名同时出现才算; 画像里权重与分量归 0,
     并且**源文件也清 0**（走那两个唯一写者）; 继承下来的 muted 每次重建都要重新套用
  3) **心情调模型**: 提示词带"当时场景"; 只认封闭词表; 解析不出/模型炸了 -> unknown（不编）
  4) **落盘**: `config/user_profile.jsonl` 一次一行、原子写、本模块是唯一写者
  5) **不存对话原文**（只存长度）—— 唯一留下的原话是 `muted_text`（破坏性动作的凭据）

⚠ 用 `IsolatedAsyncioTestCase`（`build_profile` 要 await 模型, 且 py3.8 的 Runtime 说明
  见 tests/test_chat_memory.py 模块头）。
"""

import json
import logging
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.core.chat_memory import ROLE_ASSISTANT, ROLE_USER, ChatMemory  # noqa: E402
from agent.core.user_profile import (  # noqa: E402
    MOOD_LABELS,
    MOOD_UNKNOWN,
    ProfileError,
    Sources,
    append_record,
    artist_weights,
    ask_mood,
    build_profile,
    count_hits,
    detect_negative_feedback,
    latest_record,
    mood_prompt,
    mood_tags,
    parse_mood,
    profile_basis,
    rank_tracks,
    rank_wallpapers,
    read_records,
    repo_sources,
    similar_tracks,
    wallpaper_weights,
)

logging.disable(logging.CRITICAL)

NOW = datetime(2026, 9, 25, 23, 40, 0)


def make_memory(pairs, states=None):
    """(角色, 文本) 列表 -> ChatMemory（states 可选, 每条一个模式）。"""
    memory = ChatMemory()
    for index, (role, text) in enumerate(pairs):
        state = (states or [])[index] if states and index < len(states) else "study"
        if role == ROLE_USER:
            memory.add_user(text, source="gui", state=state)
        else:
            memory.add_reply(text, source="gui", state=state)
    return memory


class FakeLLM(object):
    """替身: 判心情走 `chat`（记下提示词）; 对话走 `chat_with_tools`（回一句现成的）。"""

    def __init__(self, reply="calm", exc=None):
        self.reply = reply
        self.exc = exc
        self.prompts = []
        self.turns = []

    async def chat(self, user_input, context=None):
        self.prompts.append(user_input)
        if self.exc is not None:
            raise self.exc
        return self.reply

    async def chat_with_tools(self, text, context):
        self.turns.append(text)
        return {"ok": True, "text": "好的", "tool_calls": []}


def fake_sources(ips=("EVA", "Nier"), hits=None, mentions_ok=True, tracks=None,
                 profile_file="", wall_file="", library_file=""):
    """假的依赖口子: 记下清零调用, 返回预置的图/歌。"""
    cleared_walls = []
    cleared_plays = []

    def match_ip(name):
        return list((hits or {}).get(name, []))

    def clear_wall_usage(paths):
        cleared_walls.append(list(paths))
        return len(paths)

    def clear_plays(ids):
        cleared_plays.append(list(ids))
        return len(ids)

    walls = []
    for _name, paths in (hits or {}).items():
        for path in paths:
            walls.append({"path": path, "used": 1})
    sources = Sources(list_walls=lambda: walls,
                      match_ip=match_ip,
                      list_tracks=lambda: list(tracks or []),
                      clear_wall_usage=clear_wall_usage,
                      clear_plays=clear_plays,
                      ip_names=list(ips),
                      profile_file=profile_file, wall_file=wall_file,
                      library_file=library_file)
    return sources, cleared_walls, cleared_plays


def a_track(track_id="1", artists=("米津玄師",), plays=4, last_played=None):
    return {"id": track_id, "artists": "/".join(artists), "plays": plays,
            "tags": {"artist": list(artists)}, "last_played": last_played}


# ===========================================================================
#  1) 权重（纯函数, 直接算）
# ===========================================================================
class TestWallpaperWeights(unittest.TestCase):
    def test_the_recipe_is_the_documented_one(self):
        rows = wallpaper_weights(
            usages={"EVA": 6, "Nier": 2},
            hits={"EVA": ["/w/1.png", "/w/2.png"], "Nier": ["/w/9.png"]},
            mentions={"EVA": 3, "Nier": 1},
            lasts={"EVA": (NOW - timedelta(days=1)).isoformat(), "Nier": None},
            now=NOW)
        self.assertEqual([row["name"] for row in rows], ["EVA", "Nier"], "权重高的在前")
        top = rows[0]
        self.assertAlmostEqual(top["parts"]["usage_share"], 0.75)
        self.assertAlmostEqual(top["parts"]["mention_share"], 0.75)
        self.assertAlmostEqual(top["parts"]["recency_share"], 1.0, places=4)
        self.assertAlmostEqual(top["weight"], 0.6 * 0.75 + 0.3 * 0.75 + 0.1 * 1.0, places=4)
        self.assertEqual(rows[1]["parts"]["recency_share"], 0.0,
                         "Nier 没有近期用量 -> 近期那一项拿 0")
        self.assertAlmostEqual(rows[1]["weight"], 0.6 * 0.25 + 0.3 * 0.25, places=4)

    def test_recency_decays_over_thirty_days(self):
        rows = wallpaper_weights({"A": 0}, {}, {}, {"A": (NOW - timedelta(days=15)).isoformat()},
                                 now=NOW)
        self.assertAlmostEqual(rows[0]["parts"]["recency"], 0.5, places=2)
        old = wallpaper_weights({"A": 0}, {}, {}, {"A": (NOW - timedelta(days=90)).isoformat()},
                                now=NOW)
        self.assertEqual(old[0]["parts"]["recency"], 0.0)

    def test_no_evidence_is_an_empty_list_not_a_guess(self):
        self.assertEqual(wallpaper_weights({}, {}, {}, {}), [])

    def test_zero_totals_do_not_divide_by_zero(self):
        rows = wallpaper_weights({"A": 0, "B": 0}, {}, {}, {})
        self.assertEqual([row["weight"] for row in rows], [0.0, 0.0])


class TestArtistWeights(unittest.TestCase):
    def test_the_recipe_is_the_documented_one(self):
        rows = artist_weights({"米津玄師": 6, "别的": 2}, {"米津玄師": 1, "别的": 1})
        top = rows[0]
        self.assertEqual(top["name"], "米津玄師")
        self.assertAlmostEqual(top["parts"]["plays_share"], 0.75)
        self.assertAlmostEqual(top["parts"]["mention_share"], 0.5)
        self.assertAlmostEqual(top["weight"], 0.7 * 0.75 + 0.3 * 0.5, places=4)

    def test_no_tracks_is_an_empty_list(self):
        self.assertEqual(artist_weights({}, {}), [])


# ===========================================================================
#  2) 负反馈
# ===========================================================================
class TestNegativeFeedback(unittest.TestCase):
    def test_cue_plus_name_is_required(self):
        memory = make_memory([(ROLE_USER, "不想听米津玄師了"),
                              (ROLE_USER, "这个不错"),                    # 没有负向词
                              (ROLE_USER, "不想听了"),                    # 没有实体名
                              (ROLE_USER, "别看 EVA 了")])
        found = detect_negative_feedback(memory.entries(), ["EVA"], ["米津玄師"])
        self.assertEqual([(f.axis, f.name) for f in found], [("artist", "米津玄師"), ("ip", "EVA")])

    def test_assistant_lines_are_not_evidence(self):
        memory = make_memory([(ROLE_ASSISTANT, "那以后不看 EVA 了")])
        self.assertEqual(detect_negative_feedback(memory.entries(), ["EVA"], []), [])

    def test_unknown_names_produce_nothing(self):
        memory = make_memory([(ROLE_USER, "不想听周杰伦了")])
        self.assertEqual(detect_negative_feedback(memory.entries(), ["EVA"], ["米津玄師"]), [],
                         "实体名对不上就一条都不产生（宁可不猜）")

    def test_english_cues_work_too(self):
        memory = make_memory([(ROLE_USER, "no more EVA please")])
        found = detect_negative_feedback(memory.entries(), ["EVA"], [])
        self.assertEqual([f.name for f in found], ["EVA"])


class TestFeedbackZeroesBothLayers(unittest.IsolatedAsyncioTestCase):
    async def _build(self, entries, *, history=(), tracks=None, hits=None):
        sources, cleared_walls, cleared_plays = fake_sources(hits=hits, tracks=tracks)
        record = await build_profile(entries, sources=sources, llm=None, now=NOW,
                                     history=history)
        return record, cleared_walls, cleared_plays

    async def test_ip_feedback_clears_the_profile_and_the_source_file(self):
        memory = make_memory([(ROLE_USER, "EVA 的图看腻了")])
        record, cleared_walls, cleared_plays = await self._build(
            memory.entries(), hits={"EVA": ["/w/1.png", "/w/2.png", "/w/3.png"]})
        row = [r for r in record["walls"]["ip"] if r["name"] == "EVA"][0]
        self.assertEqual(row["weight"], 0.0)
        self.assertEqual(row["parts"]["hits"], 0)
        self.assertEqual(row["parts"]["mentions"], 0)
        self.assertTrue(row["muted_at"], "记下什么时候被清零的")
        self.assertIn("看腻", row["muted_text"], "留原话（破坏性动作要能追溯）")
        self.assertEqual(cleared_walls, [["/w/1.png", "/w/2.png", "/w/3.png"]],
                         "**源文件也清 0**（走唯一写者）")
        self.assertEqual(cleared_plays, [])
        self.assertEqual(record["cleared"]["wall_images"], 3)

    async def test_artist_feedback_clears_plays_in_the_library(self):
        memory = make_memory([(ROLE_USER, "不想听米津玄師了")])
        record, cleared_walls, cleared_plays = await self._build(
            memory.entries(), tracks=[a_track("1", plays=5), a_track("2", artists=("别的",))])
        row = [r for r in record["music"]["artist"] if r["name"] == "米津玄師"][0]
        self.assertEqual(row["weight"], 0.0)
        self.assertEqual(row["parts"]["plays"], 0)
        self.assertEqual(cleared_plays, [["1"]], "只清那个歌手的歌")
        self.assertEqual(cleared_walls, [])
        self.assertEqual(record["cleared"]["tracks"], 1)

    async def test_a_mute_is_reapplied_on_every_rebuild(self):
        """源文件清 0 之后用量会长回来 —— 所以 muted 必须在每次重建时重新套用。"""
        first, _w, _p = await self._build(make_memory([(ROLE_USER, "EVA 看腻了")]).entries(),
                                          hits={"EVA": ["/w/1.png"]})
        second, cleared_walls, _p = await self._build(
            make_memory([(ROLE_USER, "换一张壁纸")]).entries(), history=[first],
            hits={"EVA": ["/w/1.png", "/w/2.png"]})       # 源文件里又有了用量
        row = [r for r in second["walls"]["ip"] if r["name"] == "EVA"][0]
        self.assertEqual(row["weight"], 0.0, "老用量不许把它顶回来")
        self.assertEqual(row["parts"]["hits"], 0)
        self.assertIn("看腻", row["muted_text"])
        self.assertEqual(cleared_walls, [], "这次没有新的负反馈 -> 不再重复清")

    async def test_unmatched_feedback_is_recorded_but_changes_nothing(self):
        memory = make_memory([(ROLE_USER, "不想听周杰伦了")])
        sources, cleared_walls, cleared_plays = fake_sources(tracks=[a_track()])
        record = await build_profile(memory.entries(), sources=sources, llm=None, now=NOW)
        self.assertEqual(record["feedback"], [])
        self.assertEqual(record["unmatched_feedback"], [], "识别不出实体 -> 连记录都不产生")
        self.assertEqual((cleared_walls, cleared_plays), ([], []))


# ===========================================================================
#  3) 心情（调模型）
# ===========================================================================
class TestMoodPromptAndParsing(unittest.TestCase):
    def test_the_prompt_carries_the_scene_and_the_closed_vocabulary(self):
        prompt = mood_prompt("[study] 用户: 有点累了", ["study"], clock="23:40", reason="攒满了")
        for label in MOOD_LABELS:
            self.assertIn(label, prompt)
        self.assertIn("study", prompt)
        self.assertIn("23:40", prompt)
        self.assertIn("有点累了", prompt)

    def test_parsing_accepts_the_vocabulary_and_refuses_to_guess(self):
        self.assertEqual(parse_mood("calm"), "calm")
        self.assertEqual(parse_mood("CALM."), "calm")
        self.assertEqual(parse_mood("疲惫"), "tired")
        self.assertEqual(parse_mood("unknown"), MOOD_UNKNOWN)
        self.assertIsNone(parse_mood(""), "空回答不算")
        self.assertIsNone(parse_mood("我觉得他可能有点累吧"), "一整句话 -> 不采信")
        self.assertIsNone(parse_mood("tired and calm"), "两个词 -> 不采信")


class TestAskMood(unittest.IsolatedAsyncioTestCase):
    async def test_a_good_answer_is_used(self):
        result = await ask_mood(FakeLLM("tired"), "提问")
        self.assertEqual(result["label"], "tired")
        self.assertTrue(result["ok"])

    async def test_saying_unknown_is_an_honest_answer_but_not_a_mood(self):
        """模型回 "unknown" 是合法回答, 但 `ok` 必须是 False（板端实测: 曾经是 True）。"""
        result = await ask_mood(FakeLLM("unknown"), "提问")
        self.assertEqual(result["label"], MOOD_UNKNOWN)
        self.assertFalse(result["ok"], "unknown 不算判出了心情")

    async def test_a_broken_answer_lands_on_unknown_not_a_guess(self):
        result = await ask_mood(FakeLLM("我觉得他有点累"), "提问")
        self.assertEqual(result["label"], MOOD_UNKNOWN)
        self.assertFalse(result["ok"])
        self.assertIn("有点累", result["raw"], "原话留着, 好复核")

    async def test_a_failing_model_does_not_raise(self):
        result = await ask_mood(FakeLLM(exc=RuntimeError("模型炸了")), "提问")
        self.assertEqual(result["label"], MOOD_UNKNOWN)
        self.assertIn("模型炸了", result["error"])


class TestBuildProfileMood(unittest.IsolatedAsyncioTestCase):
    async def test_mood_comes_from_the_model_with_the_scene(self):
        memory = make_memory([(ROLE_USER, "今天有点累"), (ROLE_ASSISTANT, "那早点休息")],
                             states=["study", "study"])
        sources, _w, _p = fake_sources()
        llm = FakeLLM("tired")
        record = await build_profile(memory.entries(), sources=sources, llm=llm, now=NOW,
                                     clock="23:40")
        self.assertEqual(record["mood"]["label"], "tired")
        self.assertEqual(record["mood"]["zh"], "疲惫")
        self.assertTrue(record["mood"]["ok"])
        self.assertIn("[study]", llm.prompts[0], "场景（当时哪个模式）要进提示词")

    async def test_without_a_model_it_says_unknown_not_a_guess(self):
        memory = make_memory([(ROLE_USER, "今天有点累")])
        sources, _w, _p = fake_sources()
        record = await build_profile(memory.entries(), sources=sources, llm=None, now=NOW)
        self.assertEqual(record["mood"]["label"], MOOD_UNKNOWN)
        self.assertIn("没有模型", record["mood"]["error"])
        self.assertFalse(record["mood"]["ok"])

    async def test_confidence_is_low_when_the_samples_are_thin(self):
        memory = make_memory([(ROLE_USER, "累")])
        sources, _w, _p = fake_sources()
        record = await build_profile(memory.entries(), sources=sources, llm=FakeLLM("tired"),
                                     now=NOW)
        self.assertEqual(record["mood"]["confidence"], "low")
        self.assertIn("对话", record["thin"])

    async def test_the_dialogue_text_is_not_persisted(self):
        memory = make_memory([(ROLE_USER, "今天有点累")])
        sources, _w, _p = fake_sources()
        record = await build_profile(memory.entries(), sources=sources, llm=FakeLLM("tired"),
                                     now=NOW)
        blob = json.dumps(record, ensure_ascii=False)
        self.assertNotIn("今天有点累", blob, "记忆不落盘 -> 画像里也不许有原文")
        self.assertGreater(record["mood"]["excerpt_chars"], 0, "只留长度, 好知道看了多少")


# ===========================================================================
#  4) 记录与落盘
# ===========================================================================
class TestBuildProfileRecord(unittest.IsolatedAsyncioTestCase):
    async def test_the_record_has_the_three_blocks_and_samples(self):
        memory = make_memory([(ROLE_USER, "放点米津玄師的歌"), (ROLE_USER, "EVA 那张不错")])
        sources, _w, _p = fake_sources(
            hits={"EVA": ["/w/1.png", "/w/2.png"]},
            tracks=[a_track("1", plays=3, last_played="2026-09-24T21:00:00")])
        record = await build_profile(memory.entries(), sources=sources, llm=FakeLLM("relaxed"),
                                     now=NOW, trigger={"reason": "chat_chars", "chars": 2004,
                                                       "turns": 2})
        self.assertEqual(record["version"], 1)
        self.assertEqual(set(record), {"version", "built_at", "trigger", "samples", "walls",
                                       "music", "mood", "feedback", "unmatched_feedback",
                                       "muted", "cleared", "thin"})
        self.assertEqual(record["trigger"]["chars"], 2004)
        eva = [r for r in record["walls"]["ip"] if r["name"] == "EVA"][0]
        self.assertEqual(eva["parts"]["hits"], 2)
        self.assertEqual(eva["parts"]["mentions"], 1, "用户提过一次 EVA")
        self.assertEqual([r["name"] for r in record["music"]["artist"]], ["米津玄師"])
        self.assertEqual(record["samples"]["plays"], 3)
        self.assertEqual(record["samples"]["chat_turns"], 2)

    async def test_a_broken_anchor_is_recorded_not_silently_zeroed(self):
        """板端实测: 锚点/目录配错时 IP 权重一直是 0（而且是静默的）—— 现在如实记下来。"""

        def boom(_name):
            raise ProfileError("ip=EVA 检索不了: 锚点图还没打过标签")

        sources, _w, _p = fake_sources(ips=("EVA",))
        sources.match_ip = boom
        record = await build_profile(make_memory([(ROLE_USER, "在吗")]).entries(),
                                     sources=sources, llm=None, now=NOW)
        self.assertIn("EVA", record["walls"]["anchor_problems"])
        self.assertIn("锚点图还没打过标签", record["walls"]["anchor_problems"]["EVA"])
        self.assertEqual(record["walls"]["ip"][0]["weight"], 0.0)

    async def test_hits_come_from_the_ranking_with_the_used_images(self):
        """真的走一遍: 排序给出来 -> 前 K 里有用过的 -> 权重非 0（板端就是这个口径）。"""
        memory = make_memory([(ROLE_USER, "换一张 EVA 那样的")])
        sources, _w, _p = fake_sources(ips=("EVA",))
        sources.match_ip = lambda _name: ["/w/1.png", "/w/2.png", "/w/9.png"]
        sources.list_walls = lambda: [
            {"path": "/w/1.png", "used": 2, "last_used": "2026-09-24T21:00:00"},
            {"path": "/w/9.png", "used": 1, "last_used": "2026-09-20T21:00:00"},
        ]
        record = await build_profile(memory.entries(), sources=sources, llm=None, now=NOW)
        row = record["walls"]["ip"][0]
        self.assertEqual(row["parts"]["hits"], 2, "用过的 2 张都排在前面")
        self.assertEqual(row["images"], ["/w/1.png", "/w/9.png"])
        self.assertGreater(row["weight"], 0)

    async def test_a_broken_wall_file_is_an_honest_error(self):
        def boom():
            raise ProfileError("壁纸数据读不了: 磁盘坏了")

        sources, _w, _p = fake_sources()
        sources.list_walls = boom
        with self.assertRaises(ProfileError):
            await build_profile(make_memory([(ROLE_USER, "在吗")]).entries(),
                                sources=sources, llm=None, now=NOW)


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="profile-")
        self.path = os.path.join(self.tmp, "user_profile.jsonl")

    def test_append_keeps_one_record_per_build(self):
        append_record(self.path, {"version": 1, "built_at": "a"})
        append_record(self.path, {"version": 1, "built_at": "b"})
        records = read_records(self.path)
        self.assertEqual([r["built_at"] for r in records], ["a", "b"])
        self.assertEqual(latest_record(self.path)["built_at"], "b")

    def test_reading_a_missing_file_is_empty_not_an_error(self):
        self.assertEqual(read_records(os.path.join(self.tmp, "nope.jsonl")), [])
        self.assertIsNone(latest_record(os.path.join(self.tmp, "nope.jsonl")))

    def test_a_bad_line_is_skipped_not_fatal(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write('{"a": 1}\n这不是 JSON\n{"a": 2}\n')
        self.assertEqual([r["a"] for r in read_records(self.path)], [1, 2])

    def test_the_write_is_atomic(self):
        append_record(self.path, {"a": 1})
        leftovers = [name for name in os.listdir(self.tmp) if name.startswith(".")]
        self.assertEqual(leftovers, [], "原子写不留半截临时文件")
        with open(self.path, "r", encoding="utf-8") as handle:
            self.assertTrue(handle.read().endswith("\n"), "一行一条, 结尾有换行")


class TestItIsNotATool(unittest.IsolatedAsyncioTestCase):
    """你 T9 定的: **不是工具**, 是 Agent 结构的一部分 —— 模型看不到、也调不到。

    ⚠ 用 `IsolatedAsyncioTestCase` + `async def`: 板端 Python 3.8 里
      `Runtime.__init__` 会建 `asyncio.Event()`, 那要求当前线程**有事件循环** ——
      写成同步用例只在开发机（3.14）上绿, 板端一跑就
      `RuntimeError: There is no current event loop`（这个坑板端当场抓到了）。
    """

    def test_the_tool_modules_have_no_profile_tool(self):
        from agent.tools import TOOL_MODULES

        # T11-5 起多了只在 GAME 可见的 bilibili、T12-6 起多了日程 set_schedule ——
        # 但**没有**画像那一个
        self.assertEqual(TOOL_MODULES,
                         ("back_to_desktop", "wallpaper", "music", "bilibili", "schedule"))
        self.assertNotIn("user_profile", TOOL_MODULES)
        self.assertNotIn("profile", TOOL_MODULES)

    def test_there_is_no_tool_module_for_it(self):
        import agent.tools as tools

        with self.assertRaises(ImportError):
            __import__("agent.tools.user_profile")
        self.assertFalse(hasattr(tools, "user_profile"))

    async def test_the_runtime_advertises_only_the_three_tools(self):
        from agent.main import Runtime

        runtime = Runtime(config={"ipc": {"socket_path": ""}}, start_native=False,
                          start_terminal=False, log=logging.getLogger("test.profile"))
        runtime.tools = None                     # 还没起工具那一步 -> 清单为空
        self.assertIsNone(runtime.tools)
        for name in ("build_user_profile", "user_profile", "profile"):
            self.assertFalse(hasattr(runtime, name), "画像不该长成一个工具方法")


class TestRuntimeTrigger(unittest.IsolatedAsyncioTestCase):
    """T9-3: 攒到触发线 -> **Agent 自己**在后台构建一次（不赌模型, 也不堵对话）。"""

    def _runtime(self, tmp, **profile_cfg):
        from unittest import mock

        import agent.core.user_profile as profile_module
        from agent.main import Runtime

        self.profile_path = os.path.join(tmp, "user_profile.jsonl")
        self.sources, self.cleared_walls, self.cleared_plays = fake_sources(
            hits={"EVA": ["/w/1.png", "/w/2.png"]}, tracks=[a_track("1", plays=4)])
        self.patcher = mock.patch.object(profile_module, "repo_sources",
                                         lambda config=None: self.sources)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        config = {"ipc": {"socket_path": ""},
                  "profile": dict({"file": self.profile_path, "trigger_chars": 20,
                                   "trigger_turns": 99}, **profile_cfg)}
        self.runtime = Runtime(config=config, start_native=False, start_terminal=False,
                               log=logging.getLogger("test.profile"))
        self.llm = FakeLLM("calm")
        self.runtime.llm = self.llm
        self.runtime.state = None
        return self.runtime

    async def _one_turn(self, text="今天有点累，想看点安静的东西", source="gui"):
        await self.runtime._start_profile()
        self.runtime.read_services = lambda: {}          # 别让直连把话截走
        return await self.runtime.handle_event({"source": source, "text": text})

    async def test_below_the_threshold_nothing_happens(self):
        runtime = self._runtime(tempfile.mkdtemp(prefix="profile-rt-"))
        await self._one_turn("嗨")
        self.assertIsNone(runtime._profile_task, "没攒够就不该构建")
        self.assertEqual(read_records(self.profile_path), [])

    async def test_crossing_the_threshold_builds_in_the_background(self):
        runtime = self._runtime(tempfile.mkdtemp(prefix="profile-rt-"))
        await self._one_turn("今天有点累，想看点安静的东西，别太亮也别太吵，谢谢")
        task = runtime._profile_task
        self.assertIsNotNone(task, "攒够了就该起一个后台构建")
        record = await task
        self.assertIsNotNone(record)
        self.assertEqual(record["trigger"]["reason"], "chat_chars")
        self.assertEqual(record["mood"]["label"], "calm", "心情来自模型")
        records = read_records(self.profile_path)
        self.assertEqual(len(records), 1, "一次构建一行")
        self.assertLessEqual(len(runtime.chat_memory), 6, "构建完要结算（只留最新几条）")
        self.assertEqual(runtime.chat_memory.pending_chars(), 0,
                         "**触发计数归零**：保留的那几条不算下一次的量")
        runtime._profile_tick()
        self.assertIsNone(runtime._profile_task, "结算之后不该立刻又构建一遍")

    async def test_the_turn_is_not_blocked_by_the_build(self):
        runtime = self._runtime(tempfile.mkdtemp(prefix="profile-rt-"))
        reply = await self._one_turn("今天有点累，想看点安静的东西，别太亮也别太吵，谢谢")
        self.assertIsNotNone(reply, "回复照旧")
        self.assertIsNotNone(runtime._profile_task, "构建在后台排队")
        await runtime._profile_task

    async def test_scheduler_turns_do_not_trigger_anything(self):
        runtime = self._runtime(tempfile.mkdtemp(prefix="profile-rt-"))
        await self._one_turn("该学习了该学习了该学习了该学习了该学习了该学习了", source="schedule")
        self.assertIsNone(runtime._profile_task, "定时提示不是纯对话")
        self.assertEqual(read_records(self.profile_path), [])

    async def test_disabled_means_no_build(self):
        runtime = self._runtime(tempfile.mkdtemp(prefix="profile-rt-"), enabled=False)
        await self._one_turn("今天有点累，想看点安静的东西，别太亮也别太吵，谢谢")
        self.assertIsNone(runtime._profile_task)
        self.assertEqual(read_records(self.profile_path), [])

    async def test_turns_are_the_fallback_trigger(self):
        runtime = self._runtime(tempfile.mkdtemp(prefix="profile-rt-"),
                                trigger_chars=10 ** 6, trigger_turns=2)
        await self._one_turn("嗨")
        self.assertIsNone(runtime._profile_task)
        await self._one_turn("在吗")
        self.assertIsNotNone(runtime._profile_task, "字数不够但轮数够了也要构建")
        record = await runtime._profile_task
        self.assertEqual(record["trigger"]["reason"], "chat_turns")
        self.assertEqual(record["trigger"]["turns"], 2)

    async def test_a_failed_build_does_not_break_the_turn_and_backs_off(self):
        runtime = self._runtime(tempfile.mkdtemp(prefix="profile-rt-"))

        def boom():
            raise ProfileError("壁纸数据读不了")           # 构建第一步就炸

        self.sources.list_walls = boom
        reply = await self._one_turn("今天有点累，想看点安静的东西，别太亮也别太吵，谢谢")
        self.assertIsNotNone(reply, "画像失败绝不能影响对话")
        task = runtime._profile_task
        self.assertIsNone(await task, "失败 -> 没有记录")
        self.assertEqual(read_records(self.profile_path), [])
        self.assertGreater(runtime.chat_memory.pending_chars(), 20, "失败**不结算**（下次再试）")
        self.assertGreater(runtime._profile_failed_at, 0, "记下失败时间")
        runtime._profile_tick()
        self.assertIsNone(runtime._profile_task, "60 秒内不重试（免得每轮都问一次模型）")

    async def test_a_failing_model_only_costs_the_mood(self):
        """判心情失败**不算构建失败** —— 记录照落, 心情写 unknown（有据可查）。"""
        runtime = self._runtime(tempfile.mkdtemp(prefix="profile-rt-"))
        runtime.llm = FakeLLM(exc=RuntimeError("模型炸了"))
        reply = await self._one_turn("今天有点累，想看点安静的东西，别太亮也别太吵，谢谢")
        self.assertIsNotNone(reply)
        record = await runtime._profile_task
        self.assertIsNotNone(record, "照样落一行")
        self.assertEqual(record["mood"]["label"], MOOD_UNKNOWN)
        self.assertFalse(record["mood"]["ok"])
        self.assertIn("模型炸了", record["mood"]["error"])
        self.assertEqual(len(read_records(self.profile_path)), 1)
        self.assertEqual(runtime.chat_memory.pending_chars(), 0, "构建成功 -> 照旧结算")

    async def test_a_broken_wall_file_is_only_a_warning(self):
        runtime = self._runtime(tempfile.mkdtemp(prefix="profile-rt-"))

        def boom():
            raise ProfileError("壁纸数据读不了")

        self.sources.list_walls = boom
        reply = await self._one_turn("今天有点累，想看点安静的东西，别太亮也别太吵，谢谢")
        self.assertIsNotNone(reply)
        self.assertIsNone(await runtime._profile_task)
        self.assertEqual(read_records(self.profile_path), [])

    async def test_negative_feedback_also_zeroes_the_source_files(self):
        runtime = self._runtime(tempfile.mkdtemp(prefix="profile-rt-"))
        await self._one_turn("EVA 的图我看腻了，别再给我看了，换点别的吧谢谢")
        await runtime._profile_task
        self.assertEqual(self.cleared_walls, [["/w/1.png", "/w/2.png"]],
                         "源文件（wall_data.jsonl）也要清 0")

    async def test_the_stop_step_cancels_a_running_build(self):
        runtime = self._runtime(tempfile.mkdtemp(prefix="profile-rt-"))
        await self._one_turn("今天有点累，想看点安静的东西，别太亮也别太吵，谢谢")
        task = runtime._profile_task
        self.assertIsNotNone(task)
        component = [c for c in runtime._components if c.name == "profile"][0]
        await component.do_stop()
        self.assertIsNone(runtime._profile_task)


class TestCountHits(unittest.TestCase):
    """T9-4 板端实测改的口径: `match("ip=…")` 给的是**全库排序**, 不是筛过的集合。"""

    def test_hits_are_the_used_images_inside_the_top_k(self):
        ranking = ["a", "b", "c", "d", "e"]
        self.assertEqual(count_hits(ranking, ["d", "a"], top_k=2), ["a"])
        self.assertEqual(count_hits(ranking, ["d", "a"], top_k=4), ["a", "d"],
                         "按相似度顺序返回")
        self.assertEqual(count_hits(ranking, ["e"], top_k=2), [], "排在后面就不算像")

    def test_top_k_defaults_to_how_many_the_user_used(self):
        ranking = ["a", "b", "c", "d", "e"]
        self.assertEqual(count_hits(ranking, ["b", "e"]), ["b"],
                         "用过 2 张 -> 看前 2 名里有几张是用过的")

    def test_empty_inputs_are_empty_not_an_error(self):
        self.assertEqual(count_hits([], ["a"]), [])
        self.assertEqual(count_hits(["a"], []), [])


class TestRepoSourcesWiring(unittest.TestCase):
    """`repo_sources()` 必须把 **wallpaper.dir** 传给锚点检索。

    板端 T9-4 实测: 漏传时 `match("ip=…")` 一个候选都给不出来, 而且**不报错** ——
    画像里的 IP 权重就永远是 0（只有"提及"那一项）。
    """

    def test_the_wallpaper_dir_reaches_the_anchor_lookup(self):
        import agent.vision.tag_index as tag_index

        seen = {}

        class FakeIndex(object):
            def count(self):
                return 1

            def match(self, spec, presets=None, wallpaper_dir=None, limit=None):
                seen["spec"] = spec
                seen["dir"] = wallpaper_dir
                from agent.vision.tag_index import MatchResult

                return MatchResult(pool=["/w/a.png"], kind="ip", note="", detail={})

        original = tag_index.TagIndex.from_file
        tag_index.TagIndex.from_file = classmethod(lambda cls, path: FakeIndex())
        try:
            config = {"wallpaper": {"dir": "/w", "tagging": {
                "ip_presets": {"EVA": {"anchors": ["Rei_1.png"]}}}}}
            sources = repo_sources(config)
            self.assertEqual(sources.wall_dir, "/w")
            self.assertEqual(sources.match_ip("EVA"), ["/w/a.png"])
            self.assertEqual(seen["spec"], "ip=EVA")
            self.assertEqual(seen["dir"], "/w", "锚点文件名相对 wallpaper.dir 解析")
        finally:
            tag_index.TagIndex.from_file = original

    def test_without_a_configured_dir_it_falls_back_to_the_default(self):
        from agent.core.wallpaper import DEFAULT_WALLPAPER_DIR

        sources = repo_sources({})
        self.assertEqual(sources.wall_dir, DEFAULT_WALLPAPER_DIR)
        self.assertEqual(sources.ip_names, [])


def a_profile(*, ip=None, artist=None, mood="calm", thin=()):
    """一份最小画像（够 T10 的排序用）。"""
    return {
        "version": 1,
        "walls": {"ip": [{"name": name, "weight": weight, "parts": {"hits": 1}}
                         for name, weight in (ip or {}).items()]},
        "music": {"artist": [{"name": name, "weight": weight, "parts": {"plays": 3}}
                             for name, weight in (artist or {}).items()]},
        "mood": {"label": mood, "ok": mood not in ("", "unknown")},
        "thin": list(thin),
    }


class TestProfileBasis(unittest.TestCase):
    def test_a_rich_profile_is_usable(self):
        basis = profile_basis(a_profile(ip={"EVA": 0.7}, artist={"米津玄師": 0.5}))
        self.assertEqual(basis, {"ip": True, "artist": True, "mood": True,
                                 "wallpaper": True, "music": True})

    def test_thin_samples_are_not_usable(self):
        basis = profile_basis(a_profile(ip={"EVA": 0.7}, artist={"米津玄師": 0.5},
                                        thin=["壁纸用量", "音乐库"]))
        self.assertFalse(basis["ip"], "`thin` 里点名的轴一律当没有（宁可回退）")
        self.assertFalse(basis["artist"])
        self.assertTrue(basis["mood"], "心情与样本薄不薄无关")
        self.assertTrue(basis["wallpaper"], "还能靠心情挑")
        self.assertTrue(basis["music"], "一样: 心情还可用")
        thin_all = profile_basis(a_profile(ip={"EVA": 0.7}, artist={"米津玄師": 0.5},
                                           mood="unknown", thin=["壁纸用量", "音乐库"]))
        self.assertFalse(thin_all["wallpaper"])
        self.assertFalse(thin_all["music"], "样本薄 + 心情不知道 -> 整条回退")

    def test_an_unknown_mood_gives_no_preference(self):
        basis = profile_basis(a_profile(ip={"EVA": 0.7}, mood="unknown"))
        self.assertFalse(basis["mood"])
        self.assertEqual(mood_tags("unknown"), ())

    def test_no_profile_is_not_usable(self):
        self.assertEqual(profile_basis(None),
                         {"ip": False, "artist": False, "mood": False,
                          "wallpaper": False, "music": False})


class TestRankWallpapers(unittest.TestCase):
    CANDIDATES = ["/w/a.png", "/w/b.png", "/w/c.png"]

    def _rank(self, profile, **kwargs):
        similarity = kwargs.pop("ip_similarity", None) or (
            lambda name, path: {"EVA": {"/w/a.png": 0.9, "/w/b.png": 0.2,
                                        "/w/c.png": 0.1}}.get(name, {}).get(path, 0.0))
        mood_of = kwargs.pop("mood_of", None) or (
            lambda path: ["calm"] if path == "/w/b.png" else [])
        usage = kwargs.pop("usage", None) or (lambda path: 0)
        return rank_wallpapers(profile, self.CANDIDATES, ip_similarity=similarity,
                               mood_of=mood_of, usage=usage, **kwargs)

    def test_the_best_match_wins_and_says_why(self):
        rows = self._rank(a_profile(ip={"EVA": 0.7}))
        self.assertEqual([row["path"] for row in rows], ["/w/a.png", "/w/b.png", "/w/c.png"])
        top = rows[0]
        self.assertAlmostEqual(top["parts"]["ip"], 0.7 * 0.9, places=4)
        self.assertIn("像 EVA", top["why"][0])
        self.assertIn("还没看过", top["why"])

    def test_a_mood_hit_beats_a_slightly_better_ip_match(self):
        """心情是**加分项**: 权重很低时它能翻盘（IP 权重 0.1 -> 0.07 的主信号）。"""
        rows = self._rank(a_profile(ip={"EVA": 0.1}))
        self.assertEqual(rows[0]["path"], "/w/b.png")
        self.assertIn("命中 calm", rows[0]["why"])

    def test_a_strong_ip_preference_still_leads(self):
        """但主信号够强时心情盖不过去（0.7 的 IP 权重 = 0.49 的主信号 > 0.2 的心情分）。"""
        rows = self._rank(a_profile(ip={"EVA": 0.7}))
        self.assertEqual(rows[0]["path"], "/w/a.png", "最像最喜欢的 IP 的那张优先")

    def test_the_less_used_one_wins_a_tie(self):
        usage = {"/w/a.png": 5, "/w/b.png": 0}
        rows = self._rank(a_profile(ip={"EVA": 0.7}),
                          ip_similarity=lambda name, path: 0.5,
                          mood_of=lambda path: [],
                          usage=lambda path: usage.get(path, 0))
        self.assertEqual(rows[0]["path"], "/w/b.png", "\"没怎么看过\"的优先")
        self.assertIn("还没看过", rows[0]["why"])

    def test_without_a_usable_profile_it_says_so_and_still_sorts(self):
        rows = self._rank(a_profile(ip={}, mood="unknown"))
        self.assertEqual(len(rows), 3)
        self.assertTrue(all("回退" in row["why"][0] for row in rows),
                        "算不出偏好就如实说（调用方据此回退老规则）")

    def test_limit_is_honoured(self):
        rows = self._rank(a_profile(ip={"EVA": 0.7}), limit=1)
        self.assertEqual(len(rows), 1)


class TestRankTracks(unittest.TestCase):
    TRACKS = [
        {"id": "1", "name": "A", "tags": {"artist": ["米津玄師"], "mood": ["calm"]}, "plays": 4},
        {"id": "2", "name": "B", "tags": {"artist": ["别人"], "mood": ["calm"]}, "plays": 0},
        {"id": "3", "name": "C", "tags": {"artist": ["米津玄師"], "mood": ["energetic"]}, "plays": 0},
    ]

    def test_artist_weight_leads_and_says_why(self):
        rows = rank_tracks(a_profile(artist={"米津玄師": 0.8}), self.TRACKS)
        self.assertEqual([row["id"] for row in rows][:2], ["1", "3"],
                         "喜欢的歌手在前; 同歌手之间按\"听得少的\"")
        self.assertIn("喜欢 米津玄師", rows[0]["why"][0])

    def test_a_mood_hit_helps(self):
        rows = rank_tracks(a_profile(mood="calm"), self.TRACKS)
        self.assertEqual(rows[-1]["id"], "3", "不命中心情的那首排在最后")
        self.assertEqual({rows[0]["id"], rows[1]["id"]}, {"1", "2"}, "命中的两首在前")
        self.assertIn("命中 calm", rows[0]["why"])

    def test_excludes_and_muted_names(self):
        rows = rank_tracks(a_profile(artist={"米津玄師": 0.8}), self.TRACKS,
                           exclude=["1"], muted_artists=["米津玄師"])
        self.assertEqual([row["id"] for row in rows], ["2"], "队列里已有的/不想听的都别给")
        rows = rank_tracks(a_profile(artist={"米津玄師": 0.8}), self.TRACKS,
                           muted_tracks=["C"])
        self.assertEqual([row["id"] for row in rows], ["1", "2"], "点名不想听的那首也排除")

    def test_a_frontier_track_is_still_offered_when_the_profile_is_empty(self):
        rows = rank_tracks(a_profile(artist={}, mood="unknown"), self.TRACKS)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all("回退" in row["why"][0] for row in rows))


class TestSimilarTracks(unittest.TestCase):
    TRACKS = [
        {"id": "1", "name": "A", "tags": {"artist": ["米津玄師"], "mood": ["calm"],
                                          "genre": ["日语"]}},
        {"id": "2", "name": "B", "tags": {"artist": ["宇多田ヒカル"], "mood": ["calm"],
                                          "genre": ["日语"]}},
        {"id": "3", "name": "C", "tags": {"artist": ["别人"], "mood": ["energetic"],
                                          "genre": ["欧美"]}},
    ]

    def test_same_artist_or_shared_mood(self):
        self.assertEqual(similar_tracks(self.TRACKS, self.TRACKS[0]), ["1", "2"],
                         "自己 + 同 mood 的（2）; 不同歌手又不同 mood 的（3）不进来")

    def test_genre_alone_is_not_enough_by_default(self):
        tracks = [{"id": "1", "name": "A", "tags": {"genre": ["日语"], "mood": ["calm"]}},
                  {"id": "9", "name": "Z", "tags": {"genre": ["日语"], "mood": ["x"]}}]
        self.assertEqual(similar_tracks(tracks, tracks[0]), ["1"],
                         "默认不看 genre（板端那口袋太大, 一清就清空整个库）")
        self.assertEqual(similar_tracks(tracks, tracks[0], tags=("genre", "mood")),
                         ["1", "9"], "要开就显式配上")

    def test_an_unknown_target_is_empty(self):
        self.assertEqual(similar_tracks(self.TRACKS, {}), [])
        self.assertEqual(similar_tracks(self.TRACKS, None), [])


class TestTrackLevelFeedback(unittest.IsolatedAsyncioTestCase):
    async def test_naming_a_song_clears_the_similar_ones_too(self):
        """你定的第 5 条: 不想听某一首 -> **类似的全部**（同歌手/同 mood）+ T9 行为。"""
        tracks = [
            {"id": "1", "name": "JANE DOE", "plays": 5, "last_played": "t",
             "tags": {"artist": ["米津玄師"], "mood": ["calm"]}},
            {"id": "2", "name": "别的", "plays": 3, "last_played": "t",
             "tags": {"artist": ["米津玄師"], "mood": ["energetic"]}},
            {"id": "9", "name": "无关", "plays": 4, "last_played": "t",
             "tags": {"artist": ["别人"], "mood": ["energetic"]}},
        ]
        memory = make_memory([(ROLE_USER, "不想听 JANE DOE 了")])
        sources, _w, cleared_plays = fake_sources(tracks=tracks)
        record = await build_profile(memory.entries(), sources=sources, llm=None, now=NOW)
        self.assertEqual(record["cleared"]["track_ids"], ["1", "2"],
                         "同歌手的（2）一起清; 无关那首（9）不动")
        self.assertEqual(cleared_plays, [["1", "2"]])
        muted_ids = [item["name"] for item in record["muted"]["track"]]
        self.assertEqual(muted_ids, ["1", "2"], "记进 muted（T10-5 清队列 / 补歌时不再补）")
        self.assertEqual(record["feedback"][0]["axis"], "track")
        self.assertEqual(record["feedback"][0]["affected"], ["1", "2"])

    async def test_naming_an_artist_clears_its_songs(self):
        tracks = [{"id": "1", "name": "A", "plays": 5, "tags": {"artist": ["米津玄師"]}},
                  {"id": "2", "name": "B", "plays": 1, "tags": {"artist": ["别人"]}}]
        memory = make_memory([(ROLE_USER, "不想听米津玄師了")])
        sources, _w, cleared_plays = fake_sources(tracks=tracks)
        record = await build_profile(memory.entries(), sources=sources, llm=None, now=NOW)
        self.assertEqual(cleared_plays, [["1"]])
        self.assertEqual(record["cleared"]["track_ids"], ["1"],
                         "给 T10-5 清队列用的 id 列表")


if __name__ == "__main__":
    unittest.main(verbosity=2)