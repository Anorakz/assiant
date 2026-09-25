#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_read_intents.py — 只读问句直连（Phase 7 T8-5c）

跑法:
    python tests/test_read_intents.py

为什么要有它（板端实测）
    T8-5b 那批 6 条提示词里, 0.6B 对"现在在放什么"**不肯调 status**, 直接把问题复读一遍。
    这类问句的答案只存在于设备状态里 —— 模型既看不到也编不出来。所以 T8-5c 定了分工:

        只读问句 -> 直连（真实数据拼一句话, **0 次模型推理**）
        写意图   -> 照旧进模型

这个文件钉四件事:
  1) **该直连的都直连**（一批真实说法都要命中, 并且答案是真实数据拼的）
  2) **不该直连的一个都不碰**（写意图不能被截胡: "放首歌"/"换张壁纸"/"小声点"…）
  3) **拿不到数据交回模型**（依赖缺 = 不算命中; 空库/没标签数据 = 给一句能照做的）
  4) **Runtime 接线**: `handle_event` 命中直连时**模型一次都不调用**（用"调用就炸"的替身验）

⚠ T8-6 加"哪张壁纸用得最少"（`wallpaper_usage`）时踩到过截胡: 写成
   `用得最少的壁纸` 这种松规则会把**写意图**"换一张用得最少的壁纸"抢走
   （用户是要换图, 不是要报表）—— 所以那条规则带护栏: 整句要有疑问词、
   且不许出现"换一张/放一首/挑一张"这类动作。见 `agent/core/read_intents.py`。
"""

import asyncio
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.core import read_intents  # noqa: E402

logging.disable(logging.CRITICAL)


# ---------------------------------------------------------------------------
#  替身: 四个只读入口（都返回像 Runtime 那样的结构）
# ---------------------------------------------------------------------------
def fake_services(**overrides):
    calls = []

    def music_state():
        calls.append("music_state")
        return {"ok": True, "track_id": "2747166493", "title": "JANE DOE",
                "artist": "米津玄師, 宇多田ヒカル", "album": "JANE DOE",
                "position_s": 75, "duration_s": 236, "playing": True, "plays": 3, "tags": {}}

    def music_list(tag=None, axis=None, sort="plays_asc", limit=10):
        calls.append("music_list")
        return {"ok": True, "count": 2, "sort": sort, "tag": tag,
                "tracks": [{"id": "2", "name": "残酷な天使のテーゼ", "plays": 0,
                            "tags": {}, "matched": None},
                           {"id": "1", "name": "JANE DOE", "plays": 3, "tags": {}}],
                "summary": {"count": 2, "total_plays": 3, "never_played": 1, "tags": {}}}

    def wallpaper_tags(ip_query=None, limit=5):
        calls.append("wallpaper_tags")
        return {"ok": True, "count": 40,
                "axes": {"scene": [{"label": "landscape", "count": 12},
                                   {"label": "anime", "count": 7}],
                         "tone": [{"label": "dark", "count": 5}]},
                "usage": {"total_used": 57, "with_usage": 37, "never_used": 3,
                          "least_used": [{"name": "07_sky.png", "used": 0, "last_used": None},
                                         {"name": "31_sea.png", "used": 0, "last_used": None}],
                          "most_used": [{"name": "01_city.png", "used": 9,
                                         "last_used": "2026-09-24T10:00:00"}]}}

    def wallpaper_state():
        calls.append("wallpaper_state")
        return {"index": 2, "path": "/home/kickpi/wallpapers/03_city.png", "total": 40}

    services = {"music_state": music_state, "music_list": music_list,
                "wallpaper_tags": wallpaper_tags, "wallpaper_state": wallpaper_state}
    services.update(overrides)
    return services, calls


def answer(text, **overrides):
    services, calls = fake_services(**overrides)
    return read_intents.answer(text, services), calls


# ===========================================================================
#  1) 该直连的都直连
# ===========================================================================
class TestReadQuestionsAreAnswered(unittest.TestCase):
    STATUS = ["现在在放什么", "在放什么歌", "现在放的啥", "当前播放是什么", "在听哪首歌",
              "放的是哪首", "what is playing"]
    LIBRARY = ["库里有什么歌", "本地库有哪些音乐", "歌单里都有啥", "有什么歌", "有几首歌"]
    TAGS = ["有哪些壁纸标签", "壁纸标签有哪些", "壁纸有哪些风格", "有什么标签"]
    USAGE = ["哪张壁纸我用得最少", "我用得最多的壁纸是哪张", "壁纸都用过几次",
             "壁纸用得最少的是哪张", "哪些壁纸我还没怎么用过"]
    NOW = ["现在是什么壁纸", "当前显示的是哪张", "现在是第几张图"]

    def _hit(self, text, intent):
        found, calls = answer(text)
        self.assertIsNotNone(found, "%r 应当直连" % text)
        self.assertEqual(found.intent, intent, "%r 命中的意图不对" % text)
        return found, calls

    def test_music_status(self):
        for text in self.STATUS:
            with self.subTest(text=text):
                found, calls = self._hit(text, "music_status")
                self.assertIn("JANE DOE", found.text)
                self.assertIn("1:15/3:56", found.text, "进度是真实值, 不是编的")
                self.assertIn("米津玄師", found.text)
                self.assertEqual(calls, ["music_state"], "只用状态入口, 不碰别的")

    def test_music_status_when_nothing_plays(self):
        found, _calls = answer("现在在放什么", music_state=lambda: {
            "ok": True, "track_id": None, "title": "", "playing": False,
            "position_s": 0, "duration_s": 0})
        self.assertIn("没在放歌", found.text)

    def test_music_library(self):
        for text in self.LIBRARY:
            with self.subTest(text=text):
                found, calls = self._hit(text, "music_library")
                self.assertIn("2 首歌", found.text)
                self.assertIn("残酷な天使のテーゼ", found.text, "把听得最少的那几首报出来")
                self.assertEqual(calls, ["music_list"])

    def test_wallpaper_tags(self):
        for text in self.TAGS:
            with self.subTest(text=text):
                found, calls = self._hit(text, "wallpaper_tags")
                self.assertIn("40 张", found.text)
                self.assertIn("scene", found.text)
                self.assertIn("landscape(12)", found.text)
                self.assertEqual(calls, ["wallpaper_tags"])

    def test_wallpaper_usage(self):
        for text in self.USAGE:
            with self.subTest(text=text):
                found, calls = self._hit(text, "wallpaper_usage")
                self.assertIn("07_sky.png", found.text, "报的是真实数据里用得最少的那张")
                self.assertIn("0 次", found.text)
                self.assertIn("01_city.png", found.text)
                self.assertEqual(calls, ["wallpaper_tags"], "用量跟 tags 走同一个只读入口")

    def test_wallpaper_now(self):
        for text in self.NOW:
            with self.subTest(text=text):
                found, calls = self._hit(text, "wallpaper_now")
                self.assertIn("3/40", found.text)
                self.assertIn("03_city.png", found.text)
                self.assertEqual(calls, ["wallpaper_state"])

    def test_answers_come_from_the_real_data(self):
        # 换个数据 -> 答案跟着换（证明不是固定话术）
        found, _ = answer("现在在放什么", music_state=lambda: {
            "ok": True, "track_id": "1", "title": "别的歌", "artist": "A",
            "position_s": 0, "duration_s": 60, "playing": True, "plays": 0})
        self.assertIn("别的歌", found.text)
        self.assertIn("0:00/1:00", found.text)


# ===========================================================================
#  2) 不该直连的一个都不碰（写意图不能被截胡）
# ===========================================================================
class TestWriteIntentsAreNotStolen(unittest.TestCase):
    WRITES = [
        "放一首听得最少的歌", "放首歌", "随便放首歌", "换一首", "下一首",
        "把音量调到 30", "小声点", "清空播放队列", "把 JANE DOE 加进队列",
        "换一张壁纸", "换一张安静的深色风景壁纸", "下一张壁纸", "回到桌面",
        "现在几点", "你好", "今天天气怎么样", "帮我写一首诗", "暂停", "停一下",
        # T8-6: "用得最少的壁纸"是**写意图**（要换图）—— 只读规则不许抢
        "换一张用得最少的壁纸", "把用得最少的壁纸换掉", "来一张最少用的壁纸",
        "给我挑一张壁纸",
    ]

    def test_no_write_intent_matches(self):
        services, _calls = fake_services()
        for text in self.WRITES:
            with self.subTest(text=text):
                self.assertIsNone(read_intents.answer(text, services),
                                  "%r 是写意图/别的意思, 不该被直连截胡" % text)


# ===========================================================================
#  3) 拿不到数据: 交回模型 / 给一句能照做的
# ===========================================================================
class TestMissingData(unittest.TestCase):
    def test_missing_dependency_is_not_a_hit(self):
        # 音乐没开 -> 那两条规则不算命中（交回模型, 别在这里硬答）
        found, _calls = answer("现在在放什么", music_state=None)
        self.assertIsNone(found)
        found, _calls = answer("库里有什么歌", music_list=None)
        self.assertIsNone(found)

    def test_entry_says_not_available_is_answered_honestly(self):
        # 入口自己说"不行"（音乐没开/标签没打）-> 把那句能照做的话原样给用户
        found, _calls = answer("现在在放什么",
                               music_state=lambda: {"ok": False,
                                                    "error": "音乐没开（config.yaml 的 music.enabled）"})
        self.assertIn("音乐没开", found.text)

        found, _calls = answer("有哪些壁纸标签", wallpaper_tags=lambda ip_query=None, limit=5: {
            "ok": False, "tell_user": "还没有壁纸标签数据 —— 先在板端跑 assistant tag --apply。"})
        self.assertIn("assistant tag --apply", found.text)

    def test_empty_library_says_what_to_do(self):
        found, _calls = answer("库里有什么歌", music_list=lambda **kw: {
            "ok": True, "count": 0, "tracks": [], "summary": {"count": 0, "total_plays": 0}})
        self.assertIn("空的", found.text)
        self.assertIn("搜一首", found.text, "空的就要说怎么办")

    def test_empty_wallpaper_tags_falls_back_to_the_model(self):
        # 有响应但一个标签都没有 -> 直连**不硬说**, 交回模型
        found, _calls = answer("有哪些壁纸标签",
                               wallpaper_tags=lambda **kw: {"ok": True, "count": 0, "axes": {}})
        self.assertIsNone(found)

    def test_no_wallpaper_selected_falls_back(self):
        found, _calls = answer("现在是什么壁纸", wallpaper_state=lambda: {})
        self.assertIsNone(found)

    def test_usage_without_the_usage_block_falls_back(self):
        # 老数据文件（还没有 used 字段）-> 没有用量可说, 交回模型
        found, _calls = answer("哪张壁纸我用得最少",
                               wallpaper_tags=lambda **kw: {"ok": True, "count": 40, "axes": {}})
        self.assertIsNone(found)

    def test_a_broken_handler_does_not_break_the_turn(self):
        def boom():
            raise RuntimeError("入口炸了")

        found, _calls = answer("现在在放什么", music_state=boom)
        self.assertIsNone(found, "直连失败就交回模型, 不能把一轮对话搞崩")

    def test_usage_when_nothing_has_been_shown_yet(self):
        # 板端第一次问就撞上的: 全都是 0 时**别报"用得最多的是谁"**（那只是文件名顺序）
        found, _calls = answer("哪张壁纸我用得最少", wallpaper_tags=lambda **kw: {
            "ok": True, "count": 40, "axes": {},
            "usage": {"total_used": 0, "never_used": 40,
                      "least_used": [{"name": "01_a.png", "used": 0}],
                      "most_used": [{"name": "zz_b.png", "used": 0}]}})
        self.assertIn("一次都没换过", found.text)
        self.assertNotIn("用得最多", found.text, "都是 0 次就别说谁最多")

    def test_looks_like_read_is_side_effect_free(self):
        services, calls = fake_services()
        self.assertTrue(read_intents.looks_like_read("现在在放什么", services))
        self.assertEqual(calls, [], "只看匹配, 不调入口")


# ===========================================================================
#  4) Runtime 接线: 命中直连时模型一次都不调用
# ===========================================================================
class TestRuntimeShortcut(unittest.IsolatedAsyncioTestCase):
    """⚠ 用 `IsolatedAsyncioTestCase`（而不是普通 TestCase）: Python 3.8 的
    `Runtime.__init__` 会建 `asyncio.Lock`, 那要求当前线程**有一个事件循环**;
    `asyncio.run()` 跑完会把循环撤掉, 后面再建 Runtime 就炸（板端 3.8 实测踩到）。
    """

    def _runtime(self, **config):
        from agent.main import Runtime

        runtime = Runtime(config=dict({"ipc": {"socket_path": ""}}, **config),
                          start_native=False, start_terminal=False,
                          log=logging.getLogger("test.read"))
        runtime.state = None
        return runtime

    async def test_read_question_never_reaches_the_model(self):
        runtime = self._runtime()

        class ExplodingLLM:
            def __init__(self):
                self.calls = 0

            async def chat_with_tools(self, text, context):
                self.calls += 1
                raise AssertionError("只读问句不该进模型")

        runtime.llm = ExplodingLLM()
        replies = []
        runtime.on_reply = lambda text: replies.append(text)

        # 装上假的只读入口（等价于 Runtime.read_services() 里那四个）
        services, calls = fake_services()
        runtime.read_services = lambda: services

        text = await runtime.handle_event({"source": "test", "text": "现在在放什么"})
        self.assertIn("JANE DOE", text)
        self.assertEqual(runtime.llm.calls, 0, "模型一次都没被调用")
        self.assertEqual(replies, [text], "回复照旧交给 IPC 钩子")
        self.assertEqual(runtime.stats["replies"], 1)
        self.assertEqual(calls, ["music_state"])

    async def test_write_intent_still_goes_to_the_model(self):
        runtime = self._runtime()

        class FakeLLM:
            def __init__(self):
                self.texts = []

            async def chat_with_tools(self, text, context):
                self.texts.append(text)
                return {"ok": True, "text": "好", "tool_calls": []}

        runtime.llm = FakeLLM()
        runtime.read_services = lambda: fake_services()[0]
        reply = await runtime.handle_event({"source": "test", "text": "放首歌"})
        self.assertEqual(reply, "好")
        self.assertEqual(runtime.llm.texts, ["放首歌"], "写意图照旧进模型")

    async def test_default_read_services_report_missing_music(self):
        # 没起音乐（self.music is None）时, 那两个键必须是 None -> 直连不命中
        runtime = self._runtime()
        services = runtime.read_services()
        self.assertIsNone(services["music_state"])
        self.assertIsNone(services["music_list"])
        self.assertTrue(callable(services["wallpaper_tags"]))
        self.assertTrue(callable(services["wallpaper_state"]))

    async def test_wallpaper_snapshot_is_read_only(self):
        from agent.core.wallpaper import WallpaperDeck

        root = tempfile.mkdtemp(prefix="read-wall-")
        for name in ("01_a.png", "02_b.png"):
            with open(os.path.join(root, name), "w", encoding="utf-8") as handle:
                handle.write("x")
        runtime = self._runtime(wallpaper={"dir": root})
        runtime.wallpaper = WallpaperDeck(root)
        self.assertEqual(runtime.wallpaper_snapshot(), {},
                         "一张都没选过 = 不知道（不假装知道）")

        runtime.wallpaper.step(1)                    # 选一张（换壁纸是工具的事, 这里只是布置场景）
        first = runtime.wallpaper_snapshot()
        self.assertEqual(first["total"], 2)
        self.assertEqual(first["index"], 0, "游标是 0 基")
        self.assertIn("01_a.png", first["path"])
        self.assertEqual(runtime.wallpaper_snapshot(), first, "直连再问一次不该动游标")


if __name__ == "__main__":
    unittest.main(verbosity=2)
