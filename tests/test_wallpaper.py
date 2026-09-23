#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_wallpaper.py — 壁纸"下一张"（Phase 7 T3；T7-3 起能按内容挑）

跑法:
    python tests/test_wallpaper.py

覆盖三件东西（正好是那三层）:

  1) `agent/core/wallpaper.py::WallpaperDeck` —— 列目录与游标
     后缀过滤 / 按文件名排序 / 目录不存在与空目录各自报什么 / 回绕 / step=0 /
     游标记的是**路径**（加新图、删当前图之后"下一张"仍然合理）/ 坏 step /
     T7-3: `pool=` 只在候选里翻（顺序即优先级、不在目录里的候选被丢掉）
  2) `agent/tools/wallpaper.py` —— LLM 那个工具
     缺依赖跳过 / 出现在 list_tools 里 / 状态权限（GAME 不给）/ 参数校验 /
     工具**只转调** Runtime 的入口（不自己挑图、不自己推 IPC）/ T7-3: `match` 透传
  3) `agent/main.py::Runtime.next_wallpaper` —— **唯一**入口（T7-3 起只有对话走它）
     换一张 -> 调 on_wallpaper 钩子 / 没有 IPC 时也能算（pushed=False）/
     目录坏了返回 ok=False + 给人看的原因 / match 解析失败如实报错 /
     T7-3: `next_wallpaper` **IPC 命令**已删除（"手动换壁纸"按要求下线）

⚠ 不测 GUI 画得像不像（那是 `gui/tests/test_image_fit.cpp` 与板端截图的事）,
  也不测真实图片能不能解码（Agent 不解码）。
⚠ 标签索引本身的算法（词表向量、IP 锚点、排序）在 `tests/test_tag_index.py`。
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

from agent.core.state_machine import State  # noqa: E402
from agent.core.tool_router import ToolRouter  # noqa: E402
from agent.core.wallpaper import (  # noqa: E402
    DEFAULT_WALLPAPER_DIR,
    WallpaperDeck,
    WallpaperError,
)
from agent.ipc import UNWIRED_COMMAND_NOTES  # noqa: E402
from agent.ipc import protocol as p  # noqa: E402
from agent.tools import wallpaper as wallpaper_tool  # noqa: E402
from agent.tools import build_tools  # noqa: E402

logging.disable(logging.CRITICAL)          # 让"跳过工具"那条 warning 别刷屏


def make_dir(*names):
    """造一个临时目录, 里面放上这些文件名（内容无所谓, 只看后缀与可读性）。"""
    root = tempfile.mkdtemp(prefix="wallpaper_")
    for name in names:
        with open(os.path.join(root, name), "w", encoding="utf-8") as handle:
            handle.write("not really an image")
    return root


def names_of(paths):
    return [os.path.basename(p) for p in paths]


# ===========================================================================
#  1) WallpaperDeck
# ===========================================================================
class TestScan(unittest.TestCase):
    def test_only_image_suffixes_and_sorted_by_name(self):
        root = make_dir("02_b.png", "01_a.jpg", "notes.txt", "10_j.webp", "00_z.PNG")
        self.assertEqual(
            names_of(WallpaperDeck(root).scan()),
            ["00_z.PNG", "01_a.jpg", "02_b.png", "10_j.webp"],
        )

    def test_subdirectories_are_not_images(self):
        root = make_dir("01_a.jpg")
        os.mkdir(os.path.join(root, "02_dir.png"))     # 名字像图片, 但它是目录
        self.assertEqual(names_of(WallpaperDeck(root).scan()), ["01_a.jpg"])

    def test_empty_dir_scans_empty_without_raising(self):
        self.assertEqual(WallpaperDeck(make_dir()).scan(), [])

    def test_missing_dir_says_how_to_fix_it(self):
        deck = WallpaperDeck(os.path.join(tempfile.mkdtemp(), "nope"))
        with self.assertRaises(WallpaperError) as ctx:
            deck.scan()
        message = str(ctx.exception)
        self.assertIn("不存在", message)
        self.assertIn("wallpaper.dir", message, "要告诉人改哪个配置键")

    def test_a_file_instead_of_a_dir_is_refused(self):
        path = os.path.join(make_dir("01_a.jpg"), "01_a.jpg")   # 真的存在, 但是个文件
        with self.assertRaises(WallpaperError) as ctx:
            WallpaperDeck(path).scan()
        self.assertIn("不是目录", str(ctx.exception))

    def test_default_dir_is_the_board_path(self):
        self.assertEqual(WallpaperDeck().directory, DEFAULT_WALLPAPER_DIR)
        self.assertEqual(WallpaperDeck("").directory, DEFAULT_WALLPAPER_DIR)
        self.assertEqual(WallpaperDeck("   ").directory, DEFAULT_WALLPAPER_DIR)

    def test_count_never_raises(self):
        self.assertEqual(WallpaperDeck("/no/such/dir").count(), 0)
        self.assertEqual(WallpaperDeck(make_dir("01_a.jpg")).count(), 1)


class TestStep(unittest.TestCase):
    def test_first_step_goes_to_the_first_image(self):
        root = make_dir("01_a.jpg", "02_b.png")
        deck = WallpaperDeck(root)
        self.assertEqual(deck.step(), (0, os.path.join(root, "01_a.jpg"), 2))
        self.assertEqual(deck.step(), (1, os.path.join(root, "02_b.png"), 2))

    def test_wraps_around_both_ways(self):
        root = make_dir("01_a.jpg", "02_b.png", "03_c.png")
        deck = WallpaperDeck(root)
        # 还没选过时, step(N) = 第 N 张（step(1) -> 第 1 张, 见上面那条）
        self.assertEqual(os.path.basename(deck.step(2)[1]), "02_b.png")
        self.assertEqual(os.path.basename(deck.step(2)[1]), "01_a.jpg", "到底了要绕回去")
        self.assertEqual(os.path.basename(deck.step(-1)[1]), "03_c.png", "往前也要绕")

    def test_step_zero_repushes_the_same_one(self):
        root = make_dir("01_a.jpg", "02_b.png")
        deck = WallpaperDeck(root)
        deck.step()
        self.assertEqual(os.path.basename(deck.step(0)[1]), "01_a.jpg")

    def test_cursor_is_a_path_so_new_files_do_not_shift_it(self):
        # 这是"游标记路径而不是下标"的理由: 往前面插一张新图, 下一张仍然合理
        root = make_dir("02_b.png", "03_c.png")
        deck = WallpaperDeck(root)
        self.assertEqual(os.path.basename(deck.step()[1]), "02_b.png")
        with open(os.path.join(root, "01_a.png"), "w", encoding="utf-8") as handle:
            handle.write("x")                          # 插到最前面
        self.assertEqual(os.path.basename(deck.step()[1]), "03_c.png")

    def test_deleted_current_starts_over_instead_of_lying(self):
        root = make_dir("01_a.png", "02_b.png")
        deck = WallpaperDeck(root)
        deck.step()                                    # -> 01
        os.remove(os.path.join(root, "01_a.png"))
        self.assertEqual(os.path.basename(deck.step()[1]), "02_b.png")

    def test_current_is_reported(self):
        root = make_dir("01_a.png")
        deck = WallpaperDeck(root)
        self.assertIsNone(deck.current())
        deck.step()
        self.assertEqual(deck.current(), os.path.join(root, "01_a.png"))

    def test_empty_dir_raises_with_a_readable_reason(self):
        with self.assertRaises(WallpaperError) as ctx:
            WallpaperDeck(make_dir()).step()
        self.assertIn("没有图片", str(ctx.exception))

    def test_bad_step_is_refused(self):
        deck = WallpaperDeck(make_dir("01_a.png"))
        for bad in ("1", 1.5, None, True):
            with self.assertRaises(WallpaperError):
                deck.step(bad)

    def test_repr_never_raises_on_a_broken_dir(self):
        self.assertIn("WallpaperDeck", repr(WallpaperDeck("/no/such/dir")))


class TestSnapshot(unittest.TestCase):
    """`snapshot()` —— 不换图、只看"现在是哪张"(T6: 给"连上补推"用)。"""

    def test_nothing_selected_and_no_initialise_is_none(self):
        deck = WallpaperDeck(make_dir("01_a.png"))
        self.assertIsNone(deck.snapshot(), "还没选过就不该瞎编一张")
        self.assertIsNone(deck.current())

    def test_initialise_picks_the_first_and_sets_the_cursor(self):
        root = make_dir("02_b.png", "01_a.png")
        deck = WallpaperDeck(root)
        self.assertEqual(deck.snapshot(initialise=True),
                         (0, os.path.join(root, "01_a.png"), 2))
        self.assertEqual(deck.current(), os.path.join(root, "01_a.png"),
                         "初始化要真的落下游标, 否则下次'下一张'会重复第一张")

    def test_reports_the_index_of_the_current_one(self):
        root = make_dir("01_a.png", "02_b.png", "03_c.png")
        deck = WallpaperDeck(root)
        deck.step(2)                                    # -> 02_b
        self.assertEqual(deck.snapshot(), (1, os.path.join(root, "02_b.png"), 3))

    def test_broken_or_empty_dir_is_none_not_an_exception(self):
        self.assertIsNone(WallpaperDeck("/no/such/dir").snapshot(initialise=True))
        self.assertIsNone(WallpaperDeck(make_dir()).snapshot(initialise=True))

    def test_current_file_removed_falls_back_to_the_first_when_initialising(self):
        root = make_dir("01_a.png", "02_b.png")
        deck = WallpaperDeck(root)
        deck.step()
        os.remove(os.path.join(root, "01_a.png"))
        self.assertIsNone(deck.snapshot(), "不初始化时: 当前那张没了就是没有")
        self.assertEqual(os.path.basename(deck.snapshot(initialise=True)[1]), "02_b.png")


class TestStepWithPool(unittest.TestCase):
    """T7-3: `pool=` 只在挑出来的候选里翻（**顺序即优先级**）。"""

    def test_pool_order_is_the_priority(self):
        root = make_dir("01_a.png", "02_b.png", "03_c.png")
        deck = WallpaperDeck(root)
        best_first = [os.path.join(root, n) for n in ("03_c.png", "01_a.png")]
        # 还没选过 + step=1 -> 候选的第一张 = 最像的那张
        self.assertEqual(os.path.basename(deck.step(1, pool=best_first)[1]), "03_c.png")
        self.assertEqual(os.path.basename(deck.step(1, pool=best_first)[1]), "01_a.png")
        self.assertEqual(os.path.basename(deck.step(1, pool=best_first)[1]), "03_c.png",
                         "候选里翻到头要绕回去")

    def test_total_is_the_candidate_count_not_the_dir_count(self):
        root = make_dir("01_a.png", "02_b.png", "03_c.png")
        deck = WallpaperDeck(root)
        index, path, total = deck.step(1, pool=[os.path.join(root, "02_b.png")])
        self.assertEqual((index, os.path.basename(path), total), (0, "02_b.png", 1))

    def test_candidates_outside_the_dir_are_dropped(self):
        # 图被删了/改名了: 候选里那一条要丢掉, 而不是推出一个找不到的路径
        root = make_dir("01_a.png", "02_b.png")
        deck = WallpaperDeck(root)
        pool = [os.path.join(root, "99_gone.png"), os.path.join(root, "02_b.png")]
        self.assertEqual(os.path.basename(deck.step(1, pool=pool)[1]), "02_b.png")

    def test_a_pool_with_nothing_left_says_so(self):
        root = make_dir("01_a.png")
        deck = WallpaperDeck(root)
        with self.assertRaises(WallpaperError) as ctx:
            deck.step(1, pool=[os.path.join(root, "99_gone.png")])
        self.assertIn("一张都不在", str(ctx.exception))

    def test_current_not_in_pool_starts_from_the_first_candidate(self):
        root = make_dir("01_a.png", "02_b.png", "03_c.png")
        deck = WallpaperDeck(root)
        deck.step(1)                                     # 当前 = 01_a（不在候选里）
        pool = [os.path.join(root, "03_c.png"), os.path.join(root, "01_a.png")]
        self.assertEqual(os.path.basename(deck.step(1, pool=pool)[1]), "03_c.png",
                         "换了 match 之后从候选第一名开始, 而不是从旧游标往后挪")

    def test_no_pool_keeps_the_old_filename_order(self):
        root = make_dir("03_c.png", "01_a.png")
        deck = WallpaperDeck(root)
        self.assertEqual(os.path.basename(deck.step(1)[1]), "01_a.png")


# ===========================================================================
#  2) 工具
# ===========================================================================
class TestToolBuild(unittest.TestCase):
    def _services(self, calls):
        def advance(step=1, match=None):
            calls.append((step, match))
            return {"ok": True, "path": "/w/1.png", "index": 0, "total": 1, "pushed": True}

        return {"next_wallpaper": advance}

    def test_missing_service_skips_the_tool(self):
        self.assertIsNone(wallpaper_tool.build({}))
        self.assertIsNone(wallpaper_tool.build({"next_wallpaper": "not callable"}))

    def test_builds_with_the_service(self):
        calls = []
        tool = wallpaper_tool.build(self._services(calls))
        self.assertIsNotNone(tool)
        self.assertEqual(tool.name, "next_wallpaper")

    def test_registered_by_build_tools(self):
        router = ToolRouter(services=self._services([]))
        names = [t.name for t in build_tools(router)]
        self.assertIn("next_wallpaper", names, "agent/tools/ 的清单里要带上它")

    def test_description_warns_about_display_only_and_no_receipt(self):
        tool = wallpaper_tool.build(self._services([]))
        self.assertIn("只改显示", tool.description)
        self.assertIn("没有回执", tool.description)
        self.assertIn("match", tool.description, "T7-3: 得让模型知道能按内容挑")

    def test_schema_forbids_extra_arguments(self):
        tool = wallpaper_tool.build(self._services([]))
        self.assertFalse(tool.schema["additionalProperties"])
        self.assertEqual(tool.schema["properties"]["step"]["type"], "integer")
        self.assertEqual(tool.schema["properties"]["match"]["type"], "string")

    def test_schema_allows_calling_without_match(self):
        # 不传 match = 老行为（按文件名翻页），所以它不能进 required
        tool = wallpaper_tool.build(self._services([]))
        self.assertNotIn("required", tool.schema)


class TestToolExecution(unittest.IsolatedAsyncioTestCase):
    def _router(self, state=State.IDLE):
        calls = []

        def advance(step=1, match=None):
            calls.append((step, match))
            return {"ok": True, "path": "/w/%d.png" % step, "index": 0, "total": 3,
                    "pushed": True}

        router = ToolRouter(state_provider=_state_machine(state),
                            services={"next_wallpaper": advance})
        for tool in build_tools(router):
            router.register(tool)
        return router, calls

    async def test_default_step_is_one(self):
        router, calls = self._router()
        result = await router.execute("next_wallpaper", {})
        self.assertTrue(result["ok"])
        self.assertEqual(calls, [(1, None)], "工具只是把入参转给 Runtime 的入口")
        self.assertEqual(result["result"]["path"], "/w/1.png")

    async def test_step_is_forwarded(self):
        router, calls = self._router()
        result = await router.execute("next_wallpaper", {"step": -1})
        self.assertEqual(calls, [(-1, None)])
        self.assertEqual(result["result"]["path"], "/w/-1.png")

    async def test_match_is_forwarded(self):
        router, calls = self._router()
        result = await router.execute("next_wallpaper", {"step": 1, "match": "scene=anime"})
        self.assertEqual(calls, [(1, "scene=anime")])
        self.assertTrue(result["ok"])

    async def test_bad_argument_is_refused_before_running(self):
        router, calls = self._router()
        result = await router.execute("next_wallpaper", {"step": "next"})
        self.assertFalse(result["ok"])
        self.assertIn("invalid arguments", result["error"])
        self.assertEqual(calls, [], "参数不合法时 handler 一次都不该跑")

    async def test_empty_match_is_refused_before_running(self):
        router, calls = self._router()
        result = await router.execute("next_wallpaper", {"match": ""})
        self.assertFalse(result["ok"])
        self.assertEqual(calls, [])

    async def test_refused_in_game_mode(self):
        # 游戏模式主区是视频区, 换壁纸没意义 —— 权限表里不给 GAME
        router, calls = self._router(State.GAME)
        result = await router.execute("next_wallpaper", {})
        self.assertFalse(result["ok"])
        self.assertIn("not allowed", result["error"])
        self.assertEqual(calls, [])

    async def test_allowed_in_idle_and_study(self):
        tool = wallpaper_tool.build({"next_wallpaper": lambda step=1, match=None: {}})
        self.assertEqual(tool.allowed_states, {State.IDLE, State.STUDY})


def _state_machine(state):
    from agent.core.state_machine import StateMachine

    machine = StateMachine()
    path = {
        State.IDLE: [],
        State.STUDY: [State.IDLE, State.STUDY],
        State.GAME: [State.IDLE, State.GAME],
        State.SLEEP: [State.SLEEP],
    }[state]
    for step in path:
        machine.transition(step, "test")
    return machine


# ===========================================================================
#  3) Runtime 入口（T7-3 起只有对话走它）+ "手动换壁纸"确实下线了
# ===========================================================================
class TestTheManualEntryPointsAreGone(unittest.IsolatedAsyncioTestCase):
    """T7-3 的需求: **手动换壁纸删掉** —— 换壁纸只走对话。

    这条测试是"删干净了"的机械证据: 协议里没有那条命令, 未接线说明表里也没有,
    命令处理器收到它只会当未知命令忽略（回一句 warning, 不崩）。
    """

    def test_the_command_constant_is_gone(self):
        self.assertFalse(hasattr(p, "COMMAND_NEXT_WALLPAPER"),
                         "next_wallpaper 命令已删除（换壁纸只走对话）")
        self.assertNotIn("next_wallpaper", p.COMMANDS)

    def test_it_is_not_in_the_unwired_table_either(self):
        # 它既不是"已接线"也不是"未接线": 这条命令**不存在**了
        self.assertNotIn("next_wallpaper", UNWIRED_COMMAND_NOTES)
        self.assertIn(p.COMMAND_NEXT_BILIBILI, UNWIRED_COMMAND_NOTES,
                      "B 站那条还没接, 别一起删了")

    async def test_the_command_handler_ignores_it(self):
        from agent.ipc import _make_command_handler

        pushed = []
        handler = _make_command_handler(bus=None, runtime=None,
                                        push=lambda topic, data: pushed.append(topic))
        await handler("next_wallpaper", {})          # 未知命令: 不该抛、不该回话
        self.assertEqual(pushed, [], "未知命令不该假装成功（也不该回一句'已换好'）")


class _FakeIndex:
    """标签索引的替身（Runtime 只用到这几个方法，接口与真 TagIndex 一致）。"""

    def __init__(self, pool=(), kind="axis", note="note", error=None, scores=None,
                 total=3):
        self.pool = list(pool)
        self.kind = kind
        self.note = note
        self.error = error
        self.scores = dict(scores or {})
        self.total = total
        self.data_file = "/tmp/wall_data.jsonl"
        self.calls = []

    def count(self):
        return self.total

    def match(self, spec, presets=None, wallpaper_dir=None, limit=None):
        from agent.vision.tag_index import MatchResult

        self.calls.append((spec, wallpaper_dir))
        if self.error:
            return MatchResult(error=self.error)
        return MatchResult(pool=list(self.pool), kind=self.kind, note=self.note,
                           detail={"scores": dict(self.scores)})


class TestRuntimePickByMatch(unittest.IsolatedAsyncioTestCase):
    """`Runtime.next_wallpaper(step, match)` —— 按内容挑（T7-3 的核心）。"""

    def _runtime(self, root=None):
        from agent.main import Runtime

        root = root or make_dir("01_a.png", "02_b.png", "03_c.png")
        runtime = Runtime(config={"wallpaper": {"dir": root}},
                          start_native=False, start_terminal=False,
                          log=logging.getLogger("test.wallpaper.match"))
        runtime.wallpaper = WallpaperDeck(root)
        return runtime, root

    def _with_pool(self, runtime, root, order):
        scores = {os.path.join(root, order[0]): 0.62} if order else {}
        index = _FakeIndex(pool=[os.path.join(root, n) for n in order], scores=scores)
        runtime._tag_index = index
        return index

    async def test_match_picks_the_best_first(self):
        runtime, root = self._runtime()
        self._with_pool(runtime, root, ["03_c.png", "01_a.png"])
        result = runtime.next_wallpaper(1, match="scene=anime")
        self.assertTrue(result["ok"])
        self.assertEqual(os.path.basename(result["path"]), "03_c.png",
                         "候选已按相关度排好, step=1 就是最像的那张")
        self.assertEqual(result["total"], 2, "total 报的是候选数, 不是目录里的张数")
        self.assertAlmostEqual(result["score"], 0.62)
        self.assertEqual(result["match"]["spec"], "scene=anime")
        self.assertEqual(result["match"]["kind"], "axis")

    async def test_second_call_walks_down_the_ranking(self):
        runtime, root = self._runtime()
        self._with_pool(runtime, root, ["03_c.png", "01_a.png"])
        runtime.next_wallpaper(1, match="scene=anime")
        self.assertEqual(os.path.basename(runtime.next_wallpaper(1, match="scene=anime")["path"]),
                         "01_a.png")

    async def test_without_match_it_is_still_the_old_behaviour(self):
        runtime, root = self._runtime()
        index = self._with_pool(runtime, root, ["03_c.png"])
        result = runtime.next_wallpaper(1)
        self.assertEqual(os.path.basename(result["path"]), "01_a.png",
                         "没给 match 就按文件名翻页")
        self.assertEqual(index.calls, [], "不给 match 时**不该**去碰标签索引")
        self.assertNotIn("match", result)

    async def test_a_broken_match_is_an_honest_failure(self):
        runtime, root = self._runtime()
        runtime._tag_index = _FakeIndex(error="词表里没有 'xxx' 这条标签")
        result = runtime.next_wallpaper(1, match="scene=xxx")
        self.assertFalse(result["ok"])
        self.assertIn("没有", result["error"])
        self.assertIsNone(runtime.wallpaper.current(), "失败时不该动游标")

    async def test_an_empty_pool_is_an_honest_failure(self):
        runtime, root = self._runtime()
        self._with_pool(runtime, root, [])          # pool=[] -> 没命中
        result = runtime.next_wallpaper(1, match="ip=Nier")
        self.assertFalse(result["ok"])
        self.assertIn("没有符合条件", result["error"])

    async def test_without_tag_data_it_says_what_to_run(self):
        runtime, root = self._runtime()
        runtime._tag_index = _FakeIndex(total=0)    # 空库 = 还没打过标签
        result = runtime.next_wallpaper(1, match="scene=anime")
        self.assertFalse(result["ok"])
        self.assertIn("assistant tag", result["error"],
                      "要给出**能照做**的下一步")

    async def test_wallpaper_tags_reports_the_summary(self):
        runtime, root = self._runtime()
        index = _FakeIndex(total=3)
        index.summarise = lambda top=None: {"count": 3, "axes": {"scene": []},
                                            "data_file": index.data_file}
        runtime._tag_index = index
        result = runtime.wallpaper_tags()
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 3)
        self.assertIn("next_wallpaper", result["note"])

    async def test_wallpaper_tags_without_data_explains(self):
        runtime, root = self._runtime()
        runtime._tag_index = _FakeIndex(total=0)
        result = runtime.wallpaper_tags()
        self.assertFalse(result["ok"])
        self.assertIn("assistant tag", result["error"])

    async def test_wallpaper_tags_ip_query_without_anchors_is_honest(self):
        runtime, root = self._runtime()
        runtime._tag_index = _FakeIndex(error="ip_presets 里没有 'ZZZ'：可用的有 EVA")
        result = runtime.wallpaper_tags(ip_query="ZZZ")
        self.assertFalse(result["ok"])
        self.assertIn("EVA", result["error"])


class TestRuntimeWiring(unittest.IsolatedAsyncioTestCase):
    """`Runtime.next_wallpaper` 是**唯一**入口 —— 用真 Runtime 验一遍。

    ⚠ 这几条必须是 async 的（本类继承 IsolatedAsyncioTestCase）: `Runtime.__init__`
      里要建 `asyncio.Event()`，而 **Python 3.8（板端）不允许在没有运行中的事件循环时
      建它** —— 写成同步用例只在开发机（3.14）上绿，板端一跑就
      `RuntimeError: There is no current event loop`。这类"只在板端红"的坑，
      板端套件是唯一的守门人。
    """

    def _runtime(self, root=None):
        from agent.main import Runtime

        runtime = Runtime(config={"wallpaper": {"dir": root or make_dir("01_a.png")}},
                          start_native=False, start_terminal=False,
                          log=logging.getLogger("test.wallpaper"))
        runtime.wallpaper = WallpaperDeck(root or runtime.config["wallpaper"]["dir"])
        return runtime

    async def test_step_pushes_through_the_hook(self):
        runtime = self._runtime()
        seen = []
        runtime.on_wallpaper = lambda path, index: seen.append((path, index))
        result = runtime.next_wallpaper()
        self.assertTrue(result["ok"])
        self.assertEqual(seen, [(result["path"], 0)])
        self.assertTrue(result["pushed"])

    async def test_without_ipc_it_still_advances(self):
        runtime = self._runtime()
        result = runtime.next_wallpaper()
        self.assertTrue(result["ok"], "没有 IPC 只是推不出去, 不该算失败")
        self.assertFalse(result["pushed"])
        self.assertEqual(runtime.wallpaper.current(), result["path"])

    async def test_broken_dir_is_an_honest_failure(self):
        runtime = self._runtime(root=os.path.join(tempfile.mkdtemp(), "nope"))
        result = runtime.next_wallpaper()
        self.assertFalse(result["ok"])
        self.assertIn("不存在", result["error"])

    async def test_push_failure_does_not_break_the_switch(self):
        runtime = self._runtime()

        def boom(path, index):
            raise RuntimeError("推送炸了")

        runtime.on_wallpaper = boom
        result = runtime.next_wallpaper()
        self.assertTrue(result["ok"], "推送失败不该让'换壁纸'失败")
        self.assertFalse(result["pushed"])

    async def test_without_a_deck_it_says_so(self):
        runtime = self._runtime()
        runtime.wallpaper = None
        result = runtime.next_wallpaper()
        self.assertFalse(result["ok"])
        self.assertIn("壁纸", result["error"])

    async def test_config_dir_is_read_from_the_wallpaper_section(self):
        root = make_dir("01_a.jpg")
        runtime = self._runtime(root=root)
        self.assertEqual(runtime.wallpaper.directory, root)

    # ---- T6: GUI 刚连上时补推当前壁纸 ----

    async def test_push_current_repeats_the_current_one(self):
        root = make_dir("01_a.png", "02_b.png")
        runtime = self._runtime(root=root)
        seen = []
        runtime.on_wallpaper = lambda path, index: seen.append((path, index))
        runtime.next_wallpaper()                       # -> 01_a
        seen.clear()
        self.assertTrue(runtime.push_current_wallpaper())
        self.assertEqual(seen, [(os.path.join(root, "01_a.png"), 0)],
                         "补推的是**当前**那张, 不是下一张")

    async def test_push_current_initialises_when_nothing_was_selected(self):
        root = make_dir("01_a.png", "02_b.png")
        runtime = self._runtime(root=root)
        seen = []
        runtime.on_wallpaper = lambda path, index: seen.append((path, index))
        self.assertTrue(runtime.push_current_wallpaper())
        self.assertEqual(seen, [(os.path.join(root, "01_a.png"), 0)],
                         "一张都没选过时给个初始画面（第一张）")

    async def test_push_current_does_not_advance_on_the_second_call(self):
        root = make_dir("01_a.png", "02_b.png")
        runtime = self._runtime(root=root)
        seen = []
        runtime.on_wallpaper = lambda path, index: seen.append((path, index))
        runtime.push_current_wallpaper()
        runtime.push_current_wallpaper()               # 两个 GUI 先后连上
        self.assertEqual([os.path.basename(p) for p, _ in seen], ["01_a.png", "01_a.png"])

    async def test_push_current_is_false_when_there_is_nothing_to_push(self):
        runtime = self._runtime(root=os.path.join(tempfile.mkdtemp(), "nope"))
        runtime.on_wallpaper = lambda path, index: None
        self.assertFalse(runtime.push_current_wallpaper())
        self.assertFalse(self._runtime().push_current_wallpaper(),
                         "没有 IPC 推送入口时也返回 False")

    async def test_push_current_is_not_gated_by_the_state_table(self):
        # 补推是"同步显示", 不是"换一张": SLEEP/GAME 下也该能补
        from agent.core.state_machine import State

        root = make_dir("01_a.png")
        runtime = self._runtime(root=root)
        runtime.state = _state_machine(State.SLEEP)      # _runtime() 没 start(), 自己摆一个
        seen = []
        runtime.on_wallpaper = lambda path, index: seen.append((path, index))
        self.assertTrue(runtime.push_current_wallpaper())
        self.assertEqual(len(seen), 1)


class TestRuntimeStartBuildsTheDeck(unittest.IsolatedAsyncioTestCase):
    """启动装配真的会把 services['next_wallpaper'] 接上（不是只写在注释里）。"""

    async def test_tools_get_the_entry_point(self):
        from agent.main import Runtime

        root = make_dir("01_a.png")
        runtime = Runtime(
            config={"wallpaper": {"dir": root}, "ipc": {"socket_path": ""}},
            start_native=False, start_terminal=False,
            log=logging.getLogger("test.wallpaper.start"),
        )
        await runtime.start()
        try:
            self.assertIsNotNone(runtime.wallpaper)
            self.assertEqual(runtime.wallpaper.directory, root)
            names = [t["name"] for t in runtime.tools.list_tools()]
            self.assertIn("next_wallpaper", names)
            self.assertIn("list_wallpaper_tags", names)
            self.assertEqual(len(runtime.tools), 3,
                             "back_to_desktop + next_wallpaper + list_wallpaper_tags")
        finally:
            await runtime.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
