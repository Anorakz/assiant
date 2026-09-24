#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_merged_tools.py — 三个工具（Phase 7 T8-5b）

跑法:
    python tests/test_merged_tools.py

被测的是 `agent/tools/` 里**合并后**的三个工具:

    next_wallpaper      壁纸的一切: next/prev/repeat/pick/tags
    next_music          音乐的一切: enqueue/clear_queue/list/search/status/tag/volume
    back_to_desktop     无参数: 让主机回桌面（只 STUDY）

合并的由来（T8-5 板端实测）: 7 个工具的工具清单占第一轮 prompt 的 **90%**
（1699 / 1898 token），第二轮直接涨到 2131 把 ctx 打爆。所以"一个工具 + `action`"
既是省上下文，也是让模型少认几个名字。

覆盖:
  1) **缺依赖就跳过**: 音乐没开（services 里是一整组 None）时 `next_music` 不装;
     壁纸少一个入口（翻页/看标签）时 `next_wallpaper` 整个不装 —— 模型看到的
     action 列表必须与真实可用的完全一致
  2) **只转调**: handler 把参数递给 Runtime 的入口（工具不自己挑歌、不自己 ssh）
  3) **action 分派**: 每个 action 落到哪个入口、缺参数时怎么如实说
  4) **统一标签语法**: `tag="mood=energetic"` / 多条 / 打标签必须带轴
  5) **schema**: 必填 `action`、枚举、范围、`additionalProperties: false`
  6) **description**: 该说的都说了（PC 出声 / 30 秒计次 / 没有音频特征 / handle 分工）
"""

import logging
import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.core.state_machine import State  # noqa: E402
from agent.core.tool_router import ToolRouter  # noqa: E402
from agent.tools import back_to_desktop, build_tools, music, wallpaper  # noqa: E402

logging.disable(logging.CRITICAL)

ALL_MODULES = (back_to_desktop, wallpaper, music)


def fake_services(**overrides):
    """三个工具要的全部入口的替身: 记下调用, 返回一个像 Runtime 那样的结果。"""
    calls = []

    def advance(step=1, match=None):
        calls.append(("advance", step, match))
        return {"ok": True, "path": "/w/1.png", "index": 0, "total": 1, "pushed": True}

    def tags(ip_query=None, limit=5):
        calls.append(("tags", ip_query, limit))
        return {"ok": True, "count": 3, "axes": {}}

    def music_list(tag=None, axis=None, sort="plays_asc", limit=10):
        calls.append(("list", tag, axis, sort, limit))
        return {"ok": True, "count": 1, "tracks": [{"id": "1", "name": "A", "plays": 0}]}

    def music_search(keyword, limit=10):
        calls.append(("search", keyword, limit))
        return {"ok": True, "count": 1, "tracks": [{"id": "1", "name": "A"}]}

    def music_state():
        calls.append(("state",))
        return {"ok": True, "track_id": "1", "title": "A", "playing": True}

    def music_enqueue(track_id=None, keyword=None, tag=None, sort="plays_asc",
                      limit=1, replace=False):
        calls.append(("enqueue", track_id, keyword, tag, sort, limit, replace))
        return {"ok": True, "queued": ["1"], "started": True, "queue": {"size": 1, "index": 0}}

    def music_queue_clear():
        calls.append(("clear",))
        return {"ok": True, "cleared": 2, "queue": {"size": 0, "index": -1}}

    def music_queue_state():
        calls.append(("queue",))
        return {"ok": True, "size": 2, "index": 0, "current": "1",
                "ids": ["1", "2"], "tracks": [{"id": "1", "name": "A"}]}

    def music_tag(tags_, track_id=None, remove=False):
        calls.append(("tag", tags_, track_id, remove))
        return {"ok": True, "track_id": track_id or "1", "tags": tags_}

    def music_control(action, **kwargs):
        calls.append(("control", action, kwargs))
        return {"ok": True, "action": action}

    services = {
        "input_sender": type("S", (), {"show_desktop": lambda self: None})(),
        "next_wallpaper": advance, "wallpaper_tags": tags,
        "music_list": music_list, "music_search": music_search,
        "music_state": music_state, "music_enqueue": music_enqueue,
        "music_queue_clear": music_queue_clear, "music_queue_state": music_queue_state,
        "music_tag": music_tag, "music_control": music_control,
    }
    services.update(overrides)
    return services, calls


def build_one(module, **overrides):
    services, calls = fake_services(**overrides)
    return module.build(services), calls


# ===========================================================================
#  1) 缺依赖就跳过（而且整个工具一起跳过）
# ===========================================================================
class TestSkippedWithoutDependencies(unittest.TestCase):
    def test_music_is_not_built_when_any_entry_is_missing(self):
        # music.enabled=false 时 Runtime 给的就是一整组 None
        self.assertIsNone(music.build({}))
        services, _calls = fake_services()
        for name in ("music_enqueue", "music_queue_clear", "music_list", "music_search",
                     "music_state", "music_queue_state", "music_tag", "music_control"):
            with self.subTest(missing=name):
                self.assertIsNone(music.build(dict(services, **{name: None})),
                                  "少一个入口就不装 —— 否则模型看到的 action 与真实可用不一致")
        self.assertIsNone(music.build(dict(services, **{"music_enqueue": "not callable"})))

    def test_wallpaper_needs_both_entries(self):
        self.assertIsNone(wallpaper.build({}))
        self.assertIsNone(wallpaper.build({"next_wallpaper": lambda step=1, match=None: {}}))
        self.assertIsNone(wallpaper.build({"wallpaper_tags": lambda **kw: {}}))
        self.assertIsNone(wallpaper.build({"next_wallpaper": "x", "wallpaper_tags": "y"}))

    def test_desktop_needs_the_input_sender(self):
        self.assertIsNone(back_to_desktop.build({}))
        self.assertIsNone(back_to_desktop.build({"input_sender": object()}))

    def test_build_tools_installs_three_when_everything_is_wired(self):
        services, _calls = fake_services()
        router = ToolRouter(services=services)
        names = [t.name for t in build_tools(router)]
        self.assertEqual(names, ["back_to_desktop", "next_wallpaper", "next_music"],
                         "T8-5b: 模型只该看到三个工具, 顺序按模块清单稳定")

    def test_build_tools_without_music_still_installs_two(self):
        services, _calls = fake_services()
        for name in ("music_list", "music_search", "music_state", "music_enqueue",
                     "music_queue_clear", "music_queue_state", "music_tag", "music_control"):
            services[name] = None
        router = ToolRouter(services=services)
        names = [t.name for t in build_tools(router)]
        self.assertEqual(names, ["back_to_desktop", "next_wallpaper"])


# ===========================================================================
#  2/5) schema 与状态
# ===========================================================================
class TestSchemaAndStates(unittest.TestCase):
    def test_all_declare_states(self):
        for module in ALL_MODULES:
            with self.subTest(tool=module.NAME):
                tool = module.build(fake_services()[0])
                self.assertTrue(tool.allowed_states)
                self.assertFalse(tool.allowed_states - {State.IDLE, State.STUDY},
                                 "只有壁纸/音乐是 IDLE+STUDY, 回桌面是 STUDY")

    def test_all_forbid_extra_arguments(self):
        for module in ALL_MODULES:
            with self.subTest(tool=module.NAME):
                tool = module.build(fake_services()[0])
                self.assertFalse(tool.schema["additionalProperties"])
                if module is back_to_desktop:
                    self.assertEqual(tool.schema["properties"], {}, "回桌面没有参数")
                else:
                    self.assertEqual(tool.schema["required"], ["action"],
                                     "action 必填 —— 干什么必须说出来")

    def test_action_enums_are_the_actions_constant(self):
        for module in (wallpaper, music):
            with self.subTest(tool=module.NAME):
                tool = module.build(fake_services()[0])
                self.assertEqual(tool.schema["properties"]["action"]["enum"],
                                 list(module.ACTIONS))

    def test_back_to_desktop_has_no_parameters(self):
        tool = back_to_desktop.build(fake_services()[0])
        self.assertEqual(tool.schema["properties"], {})
        self.assertEqual(tool.allowed_states, {State.STUDY})


# ===========================================================================
#  3) 壁纸: action 分派
# ===========================================================================
class TestWallpaperActions(unittest.TestCase):
    def test_next_prev_repeat_map_to_steps(self):
        tool, calls = build_one(wallpaper)
        tool.handler(action="next")
        tool.handler(action="prev")
        tool.handler(action="repeat")
        self.assertEqual(calls, [("advance", 1, None), ("advance", -1, None),
                                 ("advance", 0, None)])

    def test_pick_forwards_match(self):
        tool, calls = build_one(wallpaper)
        tool.handler(action="pick", match="ip=EVA")
        self.assertEqual(calls, [("advance", 1, "ip=EVA")])

    def test_next_with_match_is_accepted_as_a_pick(self):
        """T8-5b 板端实测: 模型写 `action="next"` 又给 `match`（"在最像的几张里翻"）——
        这在老版本就是合法写法, 合并后**必须继续认**, 否则白丢一轮。"""
        tool, calls = build_one(wallpaper)
        tool.handler(action="next", match="scene=landscape")
        tool.handler(action="prev", match="mood=calm")
        self.assertEqual(calls, [("advance", 1, "scene=landscape"),
                                 ("advance", -1, "mood=calm")])
        tool.handler(action="next")
        self.assertEqual(calls[-1], ("advance", 1, None), "不给 match 还是普通翻页")

    def test_next_with_ip_query_is_treated_as_match(self):
        """同一批实测里模型还写过 `action="next"` + `ip_query="scene=landscape"`
        —— `ip_query` 只有 tags 用得上, 翻页类动作下当 match 用（宽容一点）。"""
        tool, calls = build_one(wallpaper)
        tool.handler(action="next", ip_query="scene=landscape")
        self.assertEqual(calls, [("advance", 1, "scene=landscape")])
        tool.handler(action="tags", ip_query="EVA", limit=2)
        self.assertEqual(calls[-1], ("tags", "EVA", 2), "tags 照旧走 ip_query")
        tool.handler(action="next", match="mood=calm", ip_query="scene=anime")
        self.assertEqual(calls[-1], ("advance", 1, "mood=calm"), "给了 match 就不看 ip_query")

    def test_pick_without_match_is_honest(self):
        tool, calls = build_one(wallpaper)
        payload = tool.handler(action="pick")
        self.assertFalse(payload["ok"])
        self.assertIn("match", payload["error"])
        self.assertEqual(calls, [])

    def test_tags_is_read_only(self):
        tool, calls = build_one(wallpaper)
        tool.handler(action="tags", ip_query="EVA", limit=2)
        self.assertEqual(calls, [("tags", "EVA", 2)])

    def test_action_is_case_and_space_tolerant(self):
        tool, calls = build_one(wallpaper)
        tool.handler(action=" NEXT ")
        self.assertEqual(calls, [("advance", 1, None)])


# ===========================================================================
#  3/4) 音乐: action 分派 + 统一标签语法
# ===========================================================================
class TestMusicActions(unittest.TestCase):
    def test_enqueue_defaults_to_one_track(self):
        tool, calls = build_one(music)
        payload = tool.handler(action="enqueue", tag="mood=energetic")
        self.assertTrue(payload["ok"])
        self.assertEqual(calls, [("enqueue", None, None, "mood=energetic", "plays_asc", 1, False)])

    def test_enqueue_forwards_id_keyword_sort_limit_replace(self):
        tool, calls = build_one(music)
        tool.handler(action="enqueue", track_id="9")
        tool.handler(action="enqueue", keyword="JANE DOE", limit=3)
        tool.handler(action="enqueue", sort="recent", limit=5, replace=True)
        self.assertEqual(calls[0], ("enqueue", "9", None, None, "plays_asc", 1, False))
        self.assertEqual(calls[1], ("enqueue", None, "JANE DOE", None, "plays_asc", 3, False))
        self.assertEqual(calls[2], ("enqueue", None, None, None, "recent", 5, True))

    def test_clear_queue_goes_to_the_clear_entry(self):
        tool, calls = build_one(music)
        payload = tool.handler(action="clear_queue")
        self.assertTrue(payload["ok"])
        self.assertEqual(calls, [("clear",)])

    def test_list_parses_the_shared_tag_grammar(self):
        tool, calls = build_one(music)
        tool.handler(action="list", tag="mood=energetic")
        tool.handler(action="list", tag="energetic")
        tool.handler(action="list", tag="mood=energetic/calm", limit=5)
        self.assertEqual(calls[0], ("list", "energetic", "mood", "plays_asc", 10))
        self.assertEqual(calls[1], ("list", "energetic", None, "plays_asc", 10),
                         "只写标签名 = 所有轴里找")
        self.assertEqual(calls[2], ("list", ["energetic", "calm"], "mood", "plays_asc", 5),
                         "同一轴多条 = 任一命中")

    def test_list_with_a_broken_tag_is_honest(self):
        tool, calls = build_one(music)
        payload = tool.handler(action="list", tag="=")
        self.assertFalse(payload["ok"])
        self.assertEqual(calls, [])

    def test_search_requires_a_keyword(self):
        tool, calls = build_one(music)
        payload = tool.handler(action="search")
        self.assertFalse(payload["ok"])
        self.assertIn("keyword", payload["error"])
        tool.handler(action="search", keyword="EVA", limit=3)
        self.assertEqual(calls, [("search", "EVA", 3)])

    def test_status_reports_the_queue_too(self):
        tool, calls = build_one(music)
        payload = tool.handler(action="status")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["queue"]["size"], 2, "状态里带上队列（内部用, 不推 GUI）")
        self.assertEqual([c[0] for c in calls], ["state", "queue"])

    def test_tag_parses_the_shared_grammar(self):
        tool, calls = build_one(music)
        tool.handler(action="tag", set_tag="mood=燃", track_id="9")
        tool.handler(action="tag", set_tag="mood=燃; style=rock", remove=True)
        self.assertEqual(calls[0], ("tag", {"mood": ["燃"]}, "9", False))
        self.assertEqual(calls[1], ("tag", {"mood": ["燃"], "style": ["rock"]}, None, True))

    def test_tag_requires_an_axis(self):
        tool, calls = build_one(music)
        payload = tool.handler(action="tag", set_tag="燃")
        self.assertFalse(payload["ok"])
        self.assertIn("mood", payload["tell_user"], "要给出照着写的例子")
        self.assertEqual(calls, [])

    def test_tag_without_set_tag_is_honest(self):
        tool, calls = build_one(music)
        payload = tool.handler(action="tag")
        self.assertFalse(payload["ok"])
        self.assertIn("set_tag", payload["error"])
        self.assertEqual(calls, [])

    def test_volume_goes_through_the_control_entry(self):
        tool, calls = build_one(music)
        tool.handler(action="volume", level=40)
        self.assertEqual(calls, [("control", "volume", {"level": 40})])

    def test_volume_without_level_is_honest(self):
        tool, calls = build_one(music)
        payload = tool.handler(action="volume")
        self.assertFalse(payload["ok"])
        self.assertEqual(calls, [])

    def test_unknown_action_lists_the_legal_ones(self):
        tool, calls = build_one(music)
        payload = tool.handler(action="dance")
        self.assertFalse(payload["ok"])
        for name in music.ACTIONS:
            self.assertIn(name, payload["tell_user"])
        self.assertEqual(calls, [])

    def test_there_is_no_transport_action(self):
        # T8-5b 你定的: 播放/暂停/上一首/下一首是 GUI 的活, 不在工具里
        for name in ("play", "pause", "resume", "stop", "next", "prev", "seek"):
            self.assertNotIn(name, music.ACTIONS, "%s 该由 GUI 按钮决定" % name)

    def test_music_tool_declares_its_own_timeout(self):
        # enqueue 在"没在放"时会同步起播（ssh + schtasks, 板端实测 5~8 s）——
        # 路由默认的 5 s 会把它误判成 timed out, 所以这个工具自己声明更长的超时
        tool = music.build(fake_services()[0])
        self.assertIsNotNone(tool.timeout_s)
        self.assertGreater(tool.timeout_s, 5.0)
        self.assertIsNone(wallpaper.build(fake_services()[0]).timeout_s,
                          "壁纸那两个入口都是本机操作, 用路由默认值就行")


# ===========================================================================
#  6) description 该说的都说了
# ===========================================================================
class TestDescriptions(unittest.TestCase):
    def test_music_says_pc_thirty_seconds_and_no_audio_features(self):
        text = music.build(fake_services()[0]).description
        self.assertIn("PC", text, "要说清声音从 PC 出")
        self.assertIn("30 秒", text, "要说清播放次数怎么算")
        self.assertIn("音频特征", text, "不许让模型声称'我听得出来是爵士'")
        self.assertIn("界面按钮", text, "要说清 transport 不在这里")

    def test_music_says_it_does_not_touch_files(self):
        text = music.build(fake_services()[0]).description
        self.assertIn("队列", text)

    def test_desktop_warns_about_the_toggle_and_no_receipt(self):
        text = back_to_desktop.build(fake_services()[0]).description
        self.assertIn("Win+D", text)
        self.assertIn("开关", text)
        self.assertIn("没有回执", text)
        self.assertLess(len(text), 60, "简单工具尽量短（省上下文）")

    def test_wallpaper_still_says_display_only(self):
        text = wallpaper.build(fake_services()[0]).description
        self.assertIn("只改显示", text)
        self.assertIn("没有回执", text)


# ===========================================================================
#  7) 执行路径（真路由 + 替身服务）
# ===========================================================================
class TestExecution(unittest.IsolatedAsyncioTestCase):
    def _router(self):
        services, calls = fake_services()
        router = ToolRouter(services=services)
        for tool in build_tools(router):
            router.register(tool)
        return router, calls

    async def test_calls_run_through_the_router(self):
        router, calls = self._router()

        result = await router.execute("next_music", {"action": "status"})
        self.assertTrue(result["ok"], result)

        result = await router.execute("next_music", {"action": "enqueue", "tag": "mood=calm"})
        self.assertTrue(result["ok"])
        self.assertTrue(result["result"]["started"])

        result = await router.execute("next_music", {"action": "clear_queue"})
        self.assertTrue(result["ok"])

        result = await router.execute("next_music",
                                      {"action": "tag", "set_tag": "mood=calm"})
        self.assertTrue(result["ok"])

        result = await router.execute("next_wallpaper", {"action": "next"})
        self.assertTrue(result["ok"])

        self.assertEqual([c[0] for c in calls],
                         ["state", "queue", "enqueue", "clear", "tag", "advance"])

    async def test_bad_arguments_are_refused_before_running(self):
        router, calls = self._router()

        for args in ({"action": "dance"}, {"sort": "loudest"}, {}):
            with self.subTest(args=args):
                result = await router.execute("next_music", args)
                self.assertFalse(result["ok"])
                self.assertIn("invalid arguments", result["error"])
        self.assertEqual(calls, [], "参数不合法时 handler 一次都不该跑")

    async def test_sleep_refuses_every_tool(self):
        services, calls = fake_services()
        from agent.core.state_machine import StateMachine

        machine = StateMachine()
        router = ToolRouter(state_provider=machine, services=services)
        for tool in build_tools(router):
            router.register(tool)
        machine.transition(State.SLEEP, "test")
        for name, args in (("next_music", {"action": "status"}),
                           ("next_wallpaper", {"action": "next"}),
                           ("back_to_desktop", {})):
            with self.subTest(tool=name):
                result = await router.execute(name, args)
                self.assertFalse(result["ok"])
                self.assertIn("not allowed", result["error"])
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
