#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_tool_normalize.py — 工具参数归一化（Phase 7 T8-5c）

跑法:
    python tests/test_tool_normalize.py

为什么单独一个文件
    板端实测反复证明: 0.6B **填得进参数, 但常常填错位置/填成老写法**
    （`action="play"`、`action="next"` + `match`、把条件塞进 `ip_query`、
    `track_id="none"`…）。这些**都是等价写法**, 不该白丢一轮。

    所以 T8-5c 定了条约定: **按语义接受, 不按参数名挑刺** —— 每个工具可以声明一个
    **纯函数** `normalize(args) -> args`, 路由在 **schema 校验之前**跑它
    （`agent/core/tool_router.py::_normalized_args`）。

这个文件钉三件事:
  1) 规则表: 每种真实错法 -> 归一化后的规范形（一张表就是文档）
  2) **归一化在校验之前**（否则 `action="play"` 这种 enum 外写法根本轮不到被救）
  3) 边界: 归一化**只做等价改写**（不改语义、不猜内容、不吞非法值）、
     自己炸了也不影响工具、没声明 normalize 的工具原样通过
"""

import logging
import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.core.state_machine import State, StateMachine  # noqa: E402
from agent.core.tool_router import Tool, ToolRouter  # noqa: E402
from agent.tools import back_to_desktop, music, wallpaper  # noqa: E402

logging.disable(logging.CRITICAL)


def services(**overrides):
    def noop(*a, **k):
        return {"ok": True}

    out = {
        "next_wallpaper": noop, "wallpaper_tags": noop,
        "music_list": noop, "music_search": noop, "music_state": noop,
        "music_enqueue": noop, "music_queue_clear": noop, "music_queue_state": noop,
        "music_tag": noop, "music_control": noop,
        "input_sender": type("S", (), {"show_desktop": lambda self: None})(),
    }
    out.update(overrides)
    return out


def calls_services(module):
    """造工具 + 记下入口收到的参数。"""
    seen = []

    def record(*a, **k):
        seen.append((a, k))
        return {"ok": True}

    tool = module.build(services(**{
        key: record for key in (
            "next_wallpaper", "wallpaper_tags", "music_list", "music_search",
            "music_state", "music_enqueue", "music_queue_clear", "music_queue_state",
            "music_tag", "music_control")
    }))
    return tool, seen


# ===========================================================================
#  1) 规则表: 真实错法 -> 规范形
# ===========================================================================
class TestWallpaperRules(unittest.TestCase):
    CASES = [
        # (模型可能这样写, 归一化后)
        ({"action": " NEXT "}, {"action": "next"}),
        ({"action": "PREV"}, {"action": "prev"}),
        # 板端实测: 条件给了、action 却写成翻页 —— 等价合法（老版本就是这么写的）
        ({"action": "next", "match": "scene=anime"},
         {"action": "next", "match": "scene=anime"}),
        # 板端实测: 条件被塞进了 ip_query
        ({"action": "next", "ip_query": "scene=landscape"},
         {"action": "next", "ip_query": "scene=landscape", "match": "scene=landscape"}),
        # 老参数名
        ({"step": 1}, {"action": "next"}),
        ({"step": -1}, {"action": "prev"}),
        ({"step": 0}, {"action": "repeat"}),
        # 空值写法 = 没给
        ({"action": "next", "match": ""}, {"action": "next"}),
        ({"action": "next", "match": "none"}, {"action": "next"}),
        ({"action": "tags", "ip_query": "null"}, {"action": "tags"}),
        # 只给 match: 语义就是"挑一张"
        ({"match": "ip=EVA"}, {"action": "pick", "match": "ip=EVA"}),
        # T8-6: 按用量的各种写法都落到 least / most 上
        ({"action": "LEAST_USED"}, {"action": "least"}),
        ({"action": "fewest"}, {"action": "least"}),
        ({"action": "most_used"}, {"action": "most"}),
        ({"action": " used_asc "}, {"action": "least"}),
        # T8-6: 只给 sort（没有 action）-> least/most；落成 pick 会撞"pick 需要 match"
        ({"sort": "USED_ASC"}, {"action": "least", "sort": "used_asc"}),
        ({"sort": "used_desc"}, {"action": "most", "sort": "used_desc"}),
        # T8-6: 带内容条件也照收（"挑一张我用得最少的风景壁纸"）
        ({"action": "least", "match": "scene=landscape"},
         {"action": "least", "match": "scene=landscape"}),
        # 已经规范的输入**一个字都不改**
        ({"action": "pick", "match": "scene=anime", "limit": 3},
         {"action": "pick", "match": "scene=anime", "limit": 3}),
        ({"action": "tags", "ip_query": "EVA", "limit": 2},
         {"action": "tags", "ip_query": "EVA", "limit": 2}),
    ]

    def test_rules(self):
        for raw, want in self.CASES:
            with self.subTest(raw=raw):
                self.assertEqual(wallpaper.normalize(dict(raw)), want)

    def test_does_not_mutate_the_input(self):
        raw = {"action": " next ", "ip_query": "scene=anime"}
        before = dict(raw)
        wallpaper.normalize(raw)
        self.assertEqual(raw, before)

    def test_pick_without_match_is_left_alone(self):
        # 归一化**不替模型补内容** —— 缺 match 就该让 handler 如实报"挑图要说明按什么挑"
        self.assertEqual(wallpaper.normalize({"action": "pick"}), {"action": "pick"})


class TestMusicRules(unittest.TestCase):
    CASES = [
        ({"action": " ENQUEUE "}, {"action": "enqueue"}),
        # 老工具名 / 同义写法（板端实测模型会用老名字）
        ({"action": "play"}, {"action": "enqueue"}),
        ({"action": "play_music"}, {"action": "enqueue"}),
        ({"action": "add"}, {"action": "enqueue"}),
        ({"action": "clear"}, {"action": "clear_queue"}),
        ({"action": "list_music_library"}, {"action": "list"}),
        # 没给 action: 按给出来的字段推断（都是"语义就一个"的情形）
        ({"keyword": "JANE DOE"}, {"action": "enqueue", "keyword": "JANE DOE"}),
        ({"tag": "mood=calm"}, {"action": "enqueue", "tag": "mood=calm"}),
        ({"sort": "recent", "limit": 3},
         {"action": "enqueue", "sort": "recent", "limit": 3}),
        ({"set_tag": "mood=燃"}, {"action": "tag", "set_tag": "mood=燃"}),
        ({"level": 30}, {"action": "volume", "level": 30}),
        # 空值字面量（板端实测见过 track_id="none"）
        ({"action": "enqueue", "track_id": "none"}, {"action": "enqueue"}),
        ({"action": "enqueue", "track_id": ""}, {"action": "enqueue"}),
        ({"action": "enqueue", "track_id": "null"}, {"action": "enqueue"}),
        ({"action": "search", "keyword": "  JANE DOE  "},
         {"action": "search", "keyword": "JANE DOE"}),
        # 数字写成字符串（schema 会拒字符串 -> 归一化先转）
        ({"action": "enqueue", "limit": "3"}, {"action": "enqueue", "limit": 3}),
        ({"action": "volume", "level": "30"}, {"action": "volume", "level": 30}),
        # action=tag 时把标签放进了 tag（那是筛选用的）-> 等价搬到 set_tag
        ({"action": "tag", "tag": "mood=燃"},
         {"action": "tag", "set_tag": "mood=燃"}),
        # 已经规范的输入一个字都不改
        ({"action": "enqueue", "track_id": "2747166493", "replace": True},
         {"action": "enqueue", "track_id": "2747166493", "replace": True}),
        ({"action": "tag", "set_tag": "mood=燃", "remove": True},
         {"action": "tag", "set_tag": "mood=燃", "remove": True}),
    ]

    def test_rules(self):
        for raw, want in self.CASES:
            with self.subTest(raw=raw):
                self.assertEqual(music.normalize(dict(raw)), want)

    def test_unknown_action_is_not_guessed(self):
        # 猜不出来的**不许乱改**: 原样交给校验/handler 去如实报错
        self.assertEqual(music.normalize({"action": "dance"}), {"action": "dance"})

    def test_title_like_ids_are_kept(self):
        # 只清"空值字面量", 不动看起来像 id 的东西（编造的 id 由 Runtime 去 PC 校验挡）
        self.assertEqual(music.normalize({"action": "enqueue", "track_id": "track1"}),
                         {"action": "enqueue", "track_id": "track1"})

    def test_does_not_mutate_the_input(self):
        raw = {"action": "play", "track_id": "none"}
        before = dict(raw)
        music.normalize(raw)
        self.assertEqual(raw, before)


class TestToolsDeclareNormalize(unittest.TestCase):
    def test_wallpaper_and_music_declare_it(self):
        self.assertTrue(callable(wallpaper.build(services()).normalize))
        self.assertTrue(callable(music.build(services()).normalize))

    def test_back_to_desktop_has_nothing_to_normalize(self):
        # 没参数的简单工具不需要（约定: 没声明 = 原样通过）
        self.assertIsNone(back_to_desktop.build(services()).normalize)


# ===========================================================================
#  2) 归一化在 **schema 校验之前**（不然 enum 外的写法根本轮不到被救）
# ===========================================================================
class TestNormalizeRunsBeforeValidation(unittest.IsolatedAsyncioTestCase):
    def _router(self, module):
        seen = []

        def record(*a, **k):
            seen.append((a, k))
            return {"ok": True}

        router = ToolRouter(state_provider=StateMachine(),
                            services=services(**{
                                key: record for key in (
                                    "next_wallpaper", "wallpaper_tags", "music_list",
                                    "music_search", "music_state", "music_enqueue",
                                    "music_queue_clear", "music_queue_state",
                                    "music_tag", "music_control")}))
        router.register(module.build(router.services))
        return router, seen

    async def test_old_wallpaper_step_argument_still_works(self):
        router, seen = self._router(wallpaper)
        result = await router.execute("next_wallpaper", {"step": -1})
        self.assertTrue(result["ok"], result)
        self.assertEqual(seen, [((-1, None, None), {})], "step=-1 应当被当成 prev")

    async def test_enum_foreign_action_is_rescued(self):
        # `action="play"` 不在 enum 里 —— 没有"校验前归一化"就会停在 invalid arguments
        router, seen = self._router(music)
        result = await router.execute("next_music", {"action": "play", "tag": "mood=calm"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(seen[0][1].get("tag"), "mood=calm")

    async def test_string_numbers_pass_validation(self):
        router, _seen = self._router(music)
        result = await router.execute("next_music", {"action": "volume", "level": "30"})
        self.assertTrue(result["ok"], result)

    async def test_still_refuses_real_nonsense(self):
        router, seen = self._router(music)
        result = await router.execute("next_music", {"action": "dance"})
        self.assertFalse(result["ok"])
        self.assertIn("invalid arguments", result["error"])
        self.assertEqual(seen, [], "归一化救不了的照样被拒, handler 不跑")


# ===========================================================================
#  3) 边界: 归一化自己炸了 / 没声明
# ===========================================================================
class TestNormalizeBoundaries(unittest.IsolatedAsyncioTestCase):
    def _router_with(self, tool):
        router = ToolRouter(state_provider=StateMachine(), services={})
        router.register(tool)
        return router

    async def test_a_broken_normalize_does_not_break_the_tool(self):
        seen = []

        def boom(args):
            raise RuntimeError("归一化炸了")

        tool = Tool(name="t", description="d", schema={"type": "object", "properties": {}},
                    handler=lambda **k: seen.append(k) or "ok",
                    allowed_states={State.IDLE}, normalize=boom)
        result = await self._router_with(tool).execute("t", {})
        self.assertTrue(result["ok"], "归一化自己出错不该让工具失败")
        self.assertEqual(seen, [{}])

    async def test_a_normalize_that_returns_nonsense_is_ignored(self):
        tool = Tool(name="t", description="d", schema={"type": "object", "properties": {}},
                    handler=lambda **k: "ok", allowed_states={State.IDLE},
                    normalize=lambda args: "not a dict")
        result = await self._router_with(tool).execute("t", {})
        self.assertTrue(result["ok"], result)

    async def test_tool_without_normalize_keeps_the_raw_args(self):
        seen = []
        tool = Tool(name="t", description="d",
                    schema={"type": "object", "properties": {"x": {"type": "integer"}}},
                    handler=lambda x=None: seen.append(x) or "ok",
                    allowed_states={State.IDLE})
        result = await self._router_with(tool).execute("t", {"x": 7})
        self.assertTrue(result["ok"])
        self.assertEqual(seen, [7])


if __name__ == "__main__":
    unittest.main(verbosity=2)
