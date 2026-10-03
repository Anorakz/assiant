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
     T8-6: 使用次数（"换成了另一张"才算一次 / `sort=used_asc` 挑用得最少的）

⚠ 不测 GUI 画得像不像（那是 `gui/tests/test_image_fit.cpp` 与板端截图的事）,
  也不测真实图片能不能解码（Agent 不解码）。
⚠ 标签索引本身的算法（词表向量、IP 锚点、排序）在 `tests/test_tag_index.py`。
"""

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


class TestStepAnchor(unittest.TestCase):
    """T7-4: `anchor=` —— "这一次把哪张当成当前"（挑图时"从第 1 名重挑"靠它）。"""

    def _deck(self):
        root = make_dir("01_a.png", "02_b.png", "03_c.png")
        return WallpaperDeck(root), root

    def test_anchor_none_starts_from_the_top(self):
        deck, root = self._deck()
        deck.step(1)                                   # 游标 = 01_a（候选里的第 1 张）
        pool = [os.path.join(root, n) for n in ("03_c.png", "01_a.png")]
        index, path, total = deck.step(1, pool=pool, anchor=None)
        self.assertEqual((index, os.path.basename(path), total), (0, "03_c.png", 2),
                         "anchor=None = 假装没选过 -> step=1 是候选第 1 名，而不是游标的后一张")

    def test_anchor_none_with_step_zero_also_gives_the_top(self):
        # step=0（"重推当前这张"）在"还没有当前"时给最好的那张 —— 不能给最后一名
        deck, root = self._deck()
        pool = [os.path.join(root, n) for n in ("03_c.png", "02_b.png")]
        self.assertEqual(os.path.basename(deck.step(0, pool=pool, anchor=None)[1]), "03_c.png")

    def test_anchor_none_with_a_negative_step_gives_the_last(self):
        deck, root = self._deck()
        pool = [os.path.join(root, n) for n in ("03_c.png", "02_b.png")]
        self.assertEqual(os.path.basename(deck.step(-1, pool=pool, anchor=None)[1]), "02_b.png")

    def test_an_anchor_path_continues_from_there(self):
        deck, root = self._deck()
        pool = [os.path.join(root, n) for n in ("03_c.png", "02_b.png", "01_a.png")]
        index, path, _ = deck.step(1, pool=pool, anchor=pool[0])
        self.assertEqual((index, os.path.basename(path)), (1, "02_b.png"),
                         "给了 anchor 就从它往后翻（'再换一张同类的'）")

    def test_the_cursor_is_still_updated_by_the_anchor_call(self):
        deck, root = self._deck()
        pool = [os.path.join(root, n) for n in ("03_c.png", "02_b.png")]
        deck.step(1, pool=pool, anchor=None)
        self.assertEqual(deck.current(), pool[0])

    def test_without_anchor_the_cursor_decides(self):
        deck, root = self._deck()
        deck.step(1)                                   # 01_a
        pool = [os.path.join(root, n) for n in ("02_b.png", "01_a.png")]
        self.assertEqual(os.path.basename(deck.step(1, pool=pool)[1]), "02_b.png",
                         "不给 anchor = 老行为（游标在候选里 -> 往后挪一位）")


# ===========================================================================
#  2) 工具
# ===========================================================================
class TestToolBuild(unittest.TestCase):
    """合并后的壁纸工具（T8-5b）: 一个 `action` 管翻页/挑图/看标签。"""

    def _services(self, calls, tags=None):
        def advance(step=1, match=None, sort=None):
            calls.append((step, match))
            return {"ok": True, "path": "/w/1.png", "index": 0, "total": 1, "pushed": True}

        return {"next_wallpaper": advance,
                "wallpaper_tags": tags or (lambda ip_query=None, limit=5: {"ok": True})}

    def test_missing_service_skips_the_tool(self):
        # 两个入口缺一个就不装 —— 模型看到的 action 列表必须与真实可用的完全一致
        self.assertIsNone(wallpaper_tool.build({}))
        self.assertIsNone(wallpaper_tool.build({"next_wallpaper": "not callable"}))
        self.assertIsNone(wallpaper_tool.build({"next_wallpaper": lambda step=1, match=None, sort=None: {}}))
        self.assertIsNone(wallpaper_tool.build({"wallpaper_tags": lambda **kw: {}}))

    def test_builds_with_the_services(self):
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
        self.assertIn("action", tool.description, "T8-5b: 动作靠 action 分派")

    def test_schema_forbids_extra_arguments(self):
        tool = wallpaper_tool.build(self._services([]))
        self.assertFalse(tool.schema["additionalProperties"])
        self.assertEqual(tool.schema["properties"]["action"]["type"], "string")
        self.assertEqual(tool.schema["properties"]["match"]["type"], "string")
        self.assertEqual(tool.schema["properties"]["action"]["enum"],
                         list(wallpaper_tool.ACTIONS))

    def test_schema_requires_action(self):
        # T8-5b: action 必填（老版本靠 step 的默认值, 现在"干什么"必须说出来）
        tool = wallpaper_tool.build(self._services([]))
        self.assertEqual(tool.schema["required"], ["action"])
        self.assertNotIn("step", tool.schema["properties"],
                         "step 已经收进 action（prev/repeat）, 不该再留一个入口")


class TestVocabHint(unittest.TestCase):
    """T7-4（b）: 把**当前词表**写进 description（堵住模型编造标签那条路）。"""

    def _services(self, config=None):
        return {"next_wallpaper": lambda step=1, match=None, sort=None: {"ok": True},
                "wallpaper_tags": lambda ip_query=None, limit=5: {"ok": True},
                "config": config if config is not None else {}}

    def test_description_lists_every_axis_and_label(self):
        tool = wallpaper_tool.build(self._services())
        text = tool.description
        self.assertIn("可用标签", text)
        for axis in ("scene=", "tone=", "mood="):
            self.assertIn(axis, text)
        # 抽查两个真标签（词表在 tag_vocab.py 里，改了词表这里也该跟着对）
        from agent.vision import tag_vocab
        for label in (tag_vocab.SCENE.texts[0], tag_vocab.TONE.texts[0]):
            self.assertIn(label, text, "每个轴上的标签都要列出来")

    def test_config_overrides_are_appended(self):
        config = {"wallpaper": {"tagging": {"vocab": {"scene": ["cyberpunk", "mecha"]}}}}
        tool = wallpaper_tool.build(self._services(config))
        self.assertIn("cyberpunk", tool.description)
        self.assertIn("mecha", tool.description)

    def test_without_config_it_still_lists_the_default_vocabulary(self):
        # 词表默认值在代码里（tag_vocab.py），配置只是"按轴追加" ——
        # 所以没有 config 时**照样**该把默认词表告诉模型（少一个信息来源而已）
        tool = wallpaper_tool.build({"next_wallpaper": lambda step=1, match=None, sort=None: {},
                                     "wallpaper_tags": lambda ip_query=None, limit=5: {}})
        self.assertIn("可用标签", tool.description)
        self.assertIn("scene=", tool.description)
        self.assertIn("只改显示", tool.description, "基础说明一个字都不能少")

    def test_a_broken_config_value_is_ignored(self):
        # 配置里 vocab 写成了字符串: 当作没有追加（不该抛）
        config = {"wallpaper": {"tagging": {"vocab": "oops"}}}
        tool = wallpaper_tool.build(self._services(config))
        self.assertIn("可用标签", tool.description)

    def test_a_long_vocabulary_is_truncated_with_a_pointer(self):
        original = wallpaper_tool.VOCAB_TEXT_LIMIT
        wallpaper_tool.VOCAB_TEXT_LIMIT = 60
        try:
            tool = wallpaper_tool.build(self._services())
            self.assertIn('action="tags"', tool.description,
                          "截断了就要告诉模型去哪儿看完整清单")
            self.assertLessEqual(len(tool.description),
                                 len(wallpaper_tool.DESCRIPTION) + 60 + 40)
        finally:
            wallpaper_tool.VOCAB_TEXT_LIMIT = original

    def test_hint_helper_is_quiet_on_nonsense(self):
        # 配置形状不对 = 没有追加项，但**默认词表照旧**（这是对的：词表主要活在代码里）
        self.assertIn("可用标签", wallpaper_tool.vocab_hint(None))
        self.assertEqual(wallpaper_tool.vocab_hint(None),
                         wallpaper_tool.vocab_hint("not a mapping"))


class TestWallpaperTagsAction(unittest.TestCase):
    """`action="tags"`（T7-3 的 `list_wallpaper_tags` 并进来的那个只读动作）。

    ⚠ T7-4 的收紧必须还在: 板端实测模型会把**问句**填进 ip_query
      （"这个作品最像哪几张"），失败后又把"可用的 IP 名"当成壁纸标签答给用户 ——
      所以说明里必须写死"只填作品名 / 不要填问句"。
    """

    def _services(self, calls):
        def lookup(ip_query=None, limit=5):
            calls.append((ip_query, limit))
            return {"ok": True, "count": 3, "axes": {}}

        return {"next_wallpaper": lambda step=1, match=None, sort=None: {"ok": True},
                "wallpaper_tags": lookup}

    def test_ip_query_description_says_it_is_a_work_name_only(self):
        tool = wallpaper_tool.build(self._services([]))
        description = tool.schema["properties"]["ip_query"]["description"]
        self.assertIn("作品名", description)
        self.assertIn("不要填问句", description)
        self.assertIn("作品名", tool.description)
        self.assertIn("不是问题", tool.description)

    def test_handler_forwards_the_arguments(self):
        calls = []
        tool = wallpaper_tool.build(self._services(calls))
        result = tool.handler(action="tags", ip_query="EVA", limit=2)
        self.assertEqual(calls, [("EVA", 2)])
        self.assertTrue(result["ok"])

    def test_idle_and_study_only(self):
        tool = wallpaper_tool.build(self._services([]))
        self.assertEqual(tool.allowed_states, {State.IDLE, State.STUDY})


class TestToolExecution(unittest.IsolatedAsyncioTestCase):
    def _router(self, state=State.IDLE):
        calls = []
        tags = []

        def advance(step=1, match=None, sort=None, stage=False):
            calls.append((step, match, sort, stage))
            return {"ok": True, "path": "/w/%d.png" % step, "index": 0, "total": 3,
                    "pushed": not stage, "staged": stage}

        def lookup(ip_query=None, limit=5):
            tags.append((ip_query, limit))
            return {"ok": True, "count": 3, "axes": {}}

        router = ToolRouter(state_provider=_state_machine(state),
                            services={"next_wallpaper": advance, "wallpaper_tags": lookup})
        for tool in build_tools(router):
            router.register(tool)
        return router, calls, tags

    async def test_next_is_the_forward_step(self):
        router, calls, _tags = self._router()
        result = await router.execute("next_wallpaper", {"action": "next"})
        self.assertTrue(result["ok"])
        self.assertEqual(calls, [(1, None, None, False)], "工具只是把入参转给 Runtime 的入口")
        self.assertEqual(result["result"]["path"], "/w/1.png")

    async def test_prev_and_repeat_map_to_the_other_steps(self):
        router, calls, _tags = self._router()
        await router.execute("next_wallpaper", {"action": "prev"})
        await router.execute("next_wallpaper", {"action": "repeat"})
        self.assertEqual(calls, [(-1, None, None, False), (0, None, None, False)])

    async def test_pick_forwards_match(self):
        router, calls, _tags = self._router()
        result = await router.execute("next_wallpaper",
                                      {"action": "pick", "match": "scene=anime"})
        self.assertEqual(calls, [(1, "scene=anime", None, False)])
        self.assertTrue(result["ok"])

    async def test_stage_only_prepares_the_next_one(self):
        """T10-3: `action=stage` 只把一张放进"下一个"（**不切屏**）—— 走 stage=True。"""
        router, calls, _tags = self._router()
        result = await router.execute("next_wallpaper", {"action": "STAGE"})
        self.assertEqual(calls, [(1, None, None, True)])
        self.assertTrue(result["ok"])
        self.assertTrue(result["result"]["staged"])
        self.assertFalse(result["result"]["pushed"], "stage 不切屏")
        await router.execute("next_wallpaper", {"action": "stage", "match": "ip=EVA"})
        self.assertEqual(calls[-1], (1, "ip=EVA", None, True), "带了条件也照样只预备")

    async def test_least_and_most_are_usage_picks(self):
        """T8-6: 板端实测 0.6B **不会**为"用得最少"去设 `sort=` —— 所以做成单字 action。"""
        router, calls, _tags = self._router()
        self.assertTrue((await router.execute("next_wallpaper", {"action": "least"}))["ok"])
        self.assertEqual(calls, [(1, None, "used_asc", False)])
        await router.execute("next_wallpaper", {"action": "MOST_USED"})
        await router.execute("next_wallpaper", {"action": "used_asc"})
        self.assertEqual(calls[1:], [(1, None, "used_desc", False), (1, None, "used_asc", False)],
                         "同义写法（most_used / used_asc）都要落到 least / most 上")

    async def test_least_with_a_match_stays_inside_that_pool(self):
        # "挑一张我用得最少的**风景**壁纸" -> 在最像的那批里挑用得最少的
        router, calls, _tags = self._router()
        await router.execute("next_wallpaper", {"action": "least", "match": "scene=landscape"})
        self.assertEqual(calls, [(1, "scene=landscape", "used_asc", False)])

    async def test_sort_alone_means_a_usage_pick(self):
        router, calls, _tags = self._router()
        await router.execute("next_wallpaper", {"sort": "USED_ASC"})
        self.assertEqual(calls, [(1, None, "used_asc", False)], "只给 sort = 挑一张 + 那个排序")

    def test_stage_is_in_the_schema_and_the_description(self):
        self.assertIn("stage", wallpaper_tool.ACTIONS)
        schema = wallpaper_tool.SCHEMA["properties"]["action"]
        self.assertIn("stage", schema["enum"])
        self.assertIn("stage", wallpaper_tool.DESCRIPTION)
        self.assertIn("不切屏", wallpaper_tool.DESCRIPTION)

    async def test_pick_without_match_is_honest(self):
        router, calls, _tags = self._router()
        result = await router.execute("next_wallpaper", {"action": "pick"})
        # ⚠ 路由层只负责"跑没跑": handler 自己的失败在 result["result"] 里
        #   （provider 的 tool_failures 就是这么认的, 优先用 tell_user）
        self.assertTrue(result["ok"], "路由层算跑通了")
        payload = result["result"]
        self.assertFalse(payload["ok"])
        self.assertIn("match", payload["error"])
        self.assertIn("scene=anime", payload["tell_user"], "要给出能照着写的例子")
        self.assertEqual(calls, [], "缺参数时 handler 不碰 Runtime")

    async def test_tags_with_a_usage_sort_is_an_honest_failure(self):
        """T8-6 板端实测: 模型写过 `action="tags" + sort="used_asc"`（想换图却点了只读动作）。

        ⚠ 修前它会安静地回一份标签清单, 然后模型对用户说"已找到使用次数最少的壁纸,
          您可以在界面中看到它" —— **谎报**（屏幕根本没换）。
        """
        router, calls, tags = self._router()
        result = await router.execute("next_wallpaper",
                                      {"action": "tags", "sort": "used_asc"})
        payload = result["result"]
        self.assertEqual(calls, [])
        self.assertEqual(tags, [], "不该真去列标签 —— 那会让模型以为办成了")
        self.assertFalse(payload["ok"])
        self.assertIn("least", payload["error"], "要指出该用哪个 action")
        self.assertIn("不会换壁纸", payload["tell_user"])

    async def test_tags_is_read_only_and_goes_to_the_other_entry(self):
        router, calls, tags = self._router()
        result = await router.execute("next_wallpaper",
                                      {"action": "tags", "ip_query": "EVA", "limit": 2})
        self.assertTrue(result["ok"])
        self.assertEqual(tags, [("EVA", 2)])
        self.assertEqual(calls, [], "看标签不该换壁纸")

    async def test_unknown_action_is_refused_without_side_effects(self):
        # enum 会在**校验**这一步就拦住（模型拿到的是"合法值列表"那句错误）
        router, calls, tags = self._router()
        result = await router.execute("next_wallpaper", {"action": "dance"})
        self.assertFalse(result["ok"])
        self.assertIn("invalid arguments", result["error"])
        self.assertIn("next", result["error"], "报错里要带上合法值")
        self.assertEqual(calls, [])
        self.assertEqual(tags, [])

    def test_handler_fallback_lists_the_actions(self):
        # 直接叫 handler（绕过校验）时也要有一句能照着做的回话
        router, _calls, _tags = self._router()
        tool = router.get("next_wallpaper")
        payload = tool.handler(action="dance")
        self.assertFalse(payload["ok"])
        self.assertIn("dance", payload["error"])
        for name in wallpaper_tool.ACTIONS:
            self.assertIn(name, payload["tell_user"])

    async def test_bad_argument_is_refused_before_running(self):
        router, calls, _tags = self._router()
        result = await router.execute("next_wallpaper", {"action": 1})
        self.assertFalse(result["ok"])
        self.assertIn("invalid arguments", result["error"])
        self.assertEqual(calls, [], "参数不合法时 handler 一次都不该跑")

    async def test_missing_action_is_refused_before_running(self):
        router, calls, _tags = self._router()
        result = await router.execute("next_wallpaper", {})
        self.assertFalse(result["ok"])
        self.assertIn("action", result["error"])
        self.assertEqual(calls, [])

    async def test_empty_match_is_refused_before_running(self):
        # ⚠ T8-5c 起 `match=""` 被**归一化**成"没给"（那是空值写法）—— 于是它不再是
        #   schema 错误, 而是 handler 那句能照着做的"挑图要说明按什么挑"。
        #   两种都算"拒绝且 handler 没碰 Runtime"。
        router, calls, _tags = self._router()
        result = await router.execute("next_wallpaper", {"action": "pick", "match": ""})
        payload = result["result"]
        self.assertTrue(result["ok"], "路由层算跑通了（失败在 handler 里如实说）")
        self.assertFalse(payload["ok"])
        self.assertIn("match", payload["error"])
        self.assertEqual(calls, [], "缺 match 时不该去动壁纸游标")

    async def test_refused_in_game_mode(self):
        # 游戏模式主区是视频区, 换壁纸没意义 —— 权限表里不给 GAME
        router, calls, _tags = self._router(State.GAME)
        result = await router.execute("next_wallpaper", {"action": "next"})
        self.assertFalse(result["ok"])
        self.assertIn("not allowed", result["error"])
        self.assertEqual(calls, [])

    async def test_allowed_in_idle_and_study(self):
        tool = wallpaper_tool.build({"next_wallpaper": lambda step=1, match=None, sort=None: {},
                                     "wallpaper_tags": lambda ip_query=None, limit=5: {}})
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
        self.assertNotIn("next_wallpaper", p.COMMANDS)

    def test_bilibili_did_not_get_deleted_along_with_it(self):
        # T7-3 删的是壁纸那条; T11-6 把 B 站那条**接上了**, 所以它既存在又不是未接线
        self.assertIn(p.COMMAND_NEXT_BILIBILI, p.COMMANDS)
        self.assertNotIn(p.COMMAND_NEXT_BILIBILI, UNWIRED_COMMAND_NOTES)

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
        self.assertEqual(result["match"]["rank"], 1)

    async def test_a_new_match_ignores_where_the_cursor_happens_to_be(self):
        """T7-4 修的那处: 当前那张恰好在候选里, 也不该从它的名次往下走。

        实测踩到: 当前是 01_landscape（在候选里排第 28），说"换一张动漫的"却挑回第 29 名。
        """
        runtime, root = self._runtime()
        # 游标先落在候选的第 2 名上
        self._with_pool(runtime, root, ["03_c.png", "01_a.png"])
        runtime.wallpaper.step(1, pool=[os.path.join(root, "01_a.png")])
        result = runtime.next_wallpaper(1, match="scene=anime")
        self.assertEqual(os.path.basename(result["path"]), "03_c.png",
                         "换了条件就从**第 1 名**重挑, 不看游标现在在哪")
        self.assertEqual(result["match"]["rank"], 1)

    async def test_repeating_the_same_match_walks_down_the_ranking(self):
        runtime, root = self._runtime()
        self._with_pool(runtime, root, ["03_c.png", "01_a.png", "02_b.png"])
        first = runtime.next_wallpaper(1, match="scene=anime")
        self.assertEqual(os.path.basename(first["path"]), "03_c.png")
        second = runtime.next_wallpaper(1, match="scene=anime")
        self.assertEqual(os.path.basename(second["path"]), "01_a.png",
                         "同一个条件再来一次 -> 下一名（'再换一张同类的'）")
        self.assertEqual(second["match"]["rank"], 2)

    async def test_a_different_match_starts_from_the_top_again(self):
        runtime, root = self._runtime()
        self._with_pool(runtime, root, ["03_c.png", "01_a.png", "02_b.png"])
        runtime.next_wallpaper(1, match="scene=anime")
        runtime._tag_index.pool = [os.path.join(root, n) for n in ("02_b.png", "03_c.png")]
        result = runtime.next_wallpaper(1, match="ip=EVA")
        self.assertEqual(os.path.basename(result["path"]), "02_b.png",
                         "换了条件 -> 又是第 1 名")

    async def test_step_zero_with_a_new_match_gives_the_top_not_the_last(self):
        runtime, root = self._runtime()
        self._with_pool(runtime, root, ["03_c.png", "01_a.png"])
        result = runtime.next_wallpaper(0, match="scene=anime")
        self.assertEqual(os.path.basename(result["path"]), "03_c.png",
                         "没选过时 step=0 给最像的那张（修前会给最后一名）")

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
        self.assertIn("换壁纸没有成功", result["tell_user"],
                      "T7-4 (c): 失败要有一句**给用户看**的现成话（provider 会追加进正文）")
        self.assertIn("如实", result["instruction"])
        self.assertIsNone(runtime.wallpaper.current(), "失败时不该动游标")

    async def test_an_empty_pool_is_an_honest_failure(self):
        runtime, root = self._runtime()
        self._with_pool(runtime, root, [])          # pool=[] -> 没命中
        result = runtime.next_wallpaper(1, match="ip=Nier")
        self.assertFalse(result["ok"])
        self.assertIn("没有符合条件", result["error"])
        self.assertIn("换壁纸没有成功", result["tell_user"])

    async def test_without_tag_data_it_says_what_to_run(self):
        runtime, root = self._runtime()
        runtime._tag_index = _FakeIndex(total=0)    # 空库 = 还没打过标签
        result = runtime.next_wallpaper(1, match="scene=anime")
        self.assertFalse(result["ok"])
        self.assertIn("assistant tag", result["error"],
                      "要给出**能照做**的下一步")
        self.assertIn("换壁纸没有成功", result["tell_user"])

    async def test_a_broken_directory_failure_also_carries_tell_user(self):
        runtime, root = self._runtime(root=os.path.join(tempfile.mkdtemp(), "nope"))
        result = runtime.next_wallpaper()
        self.assertFalse(result["ok"])
        self.assertIn("换壁纸没有成功", result["tell_user"])

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
    """启动装配真的会把 services 的壁纸/音乐入口接上（不是只写在注释里）。"""

    async def test_tools_get_the_entry_points(self):
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
            # T8-5b: 音乐没开 -> next_music 自己跳过; T12-6 起 set_schedule 总是装上
            # （它只有"配置文件 + 调度器"这两个依赖, 没有配置开关）
            self.assertEqual(names, ["back_to_desktop", "next_wallpaper", "set_schedule"],
                             "back_to_desktop + next_wallpaper + set_schedule"
                             "（音乐没开时没有 next_music）")
            self.assertEqual(len(runtime.tools), 3)
        finally:
            await runtime.stop()


class TestRuntimeWallpaperUsage(unittest.IsolatedAsyncioTestCase):
    """T8-6: 使用次数 —— "换成了这一张"才算一次, 并且能挑"用得最少的"。

    ⚠ 库那层（`wall_data.bump_usage*` / `TagIndex.rank_by_usage`）在
      `tests/test_wall_data.py` 与 `tests/test_tag_index.py`；这里只说 Runtime 的行为。
    """

    def _runtime(self, counts, extra=()):
        """counts = {文件名: 已用过几次}；extra = 只放文件、**不进数据文件**的图。"""
        from agent.main import Runtime
        from agent.vision import wall_data

        root = make_dir(*(list(counts) + list(extra)))
        data_file = os.path.join(root, "wall_data.jsonl")
        wall_data.write_records(
            data_file,
            [{"path": os.path.join(root, name), "tags": {"scene": [["anime", 0.9]]},
              "used": used} for name, used in counts.items()],
            backup=False,
        )
        runtime = Runtime(
            config={"wallpaper": {"dir": root, "tagging": {"data_file": data_file}}},
            start_native=False, start_terminal=False,
            log=logging.getLogger("test.wallpaper.usage"),
        )
        runtime.wallpaper = WallpaperDeck(root)
        runtime._tag_index = None                  # 数据文件是刚写的, 别用缓存的
        return runtime, root, data_file

    def _used_in_file(self, data_file, name):
        from agent.vision import wall_data

        records, _problems = wall_data.read_records(data_file)
        for record in records:
            if os.path.basename(record["path"]) == name:
                return wall_data.usage_of(record)["used"]
        self.fail("%s 不在数据文件里" % name)

    async def test_a_real_switch_counts_one(self):
        runtime, root, data_file = self._runtime({"01_a.png": 5, "02_b.png": 0})
        result = runtime.next_wallpaper(1)
        self.assertTrue(result["ok"])
        self.assertEqual(os.path.basename(result["path"]), "01_a.png")
        self.assertEqual(self._used_in_file(data_file, "01_a.png"), 6)
        self.assertEqual(result["used"], 6, "回给模型的 used 是**记完这一次**的总数")
        self.assertEqual(self._used_in_file(data_file, "02_b.png"), 0, "别的图不动")

    async def test_re_showing_the_same_one_is_not_a_new_use(self):
        runtime, root, data_file = self._runtime({"01_a.png": 5, "02_b.png": 0})
        runtime.next_wallpaper(1)
        again = runtime.next_wallpaper(0)          # step=0 = 重推当前这张
        self.assertEqual(os.path.basename(again["path"]), "01_a.png")
        self.assertEqual(self._used_in_file(data_file, "01_a.png"), 6,
                         "重推当前这张不算新的使用")
        self.assertEqual(again["used"], 6)

    async def test_the_gui_reconnect_push_does_not_count_again(self):
        runtime, root, data_file = self._runtime({"01_a.png": 0, "02_b.png": 0})
        runtime.next_wallpaper(1)                  # 真换了一次
        runtime.push_current_wallpaper()           # 客户端断开重连, 补推同一张
        runtime.push_current_wallpaper()
        self.assertEqual(self._used_in_file(data_file, "01_a.png"), 1,
                         "补推是\"同步显示\", 不是换图")

    async def test_the_initial_screen_is_not_counted(self):
        """开机/重启后 GUI 连上来的第一次补推不算 —— 不是用户换的。"""
        runtime, root, data_file = self._runtime({"01_a.png": 0, "02_b.png": 0})
        runtime.push_current_wallpaper()
        runtime.push_current_wallpaper()
        self.assertEqual(self._used_in_file(data_file, "01_a.png"), 0)
        runtime.next_wallpaper(1)                  # 之后真换一张还是要计
        self.assertEqual(self._used_in_file(data_file, "02_b.png"), 1)

    async def test_sort_used_asc_picks_the_least_shown(self):
        runtime, root, data_file = self._runtime({"01_a.png": 5, "02_b.png": 0, "03_c.png": 2})
        result = runtime.next_wallpaper(1, sort="used_asc")
        self.assertTrue(result["ok"])
        self.assertEqual(os.path.basename(result["path"]), "02_b.png",
                         "全库按使用次数升序 -> 第 1 名是用得最少的")
        self.assertEqual(result["sort"], "used_asc")
        self.assertEqual(result["total"], 3, "没给 match 时全库都进候选")
        self.assertEqual(self._used_in_file(data_file, "02_b.png"), 1)

    async def test_sort_used_desc_picks_the_most_shown(self):
        runtime, root, data_file = self._runtime({"01_a.png": 5, "02_b.png": 0, "03_c.png": 2})
        result = runtime.next_wallpaper(1, sort="used_desc")
        self.assertEqual(os.path.basename(result["path"]), "01_a.png")
        self.assertEqual(self._used_in_file(data_file, "01_a.png"), 6)

    async def test_asking_twice_never_hands_back_the_same_one(self):
        """连说两次"再挑一张用得最少的"要真的换一张, 而且往**用得少**的方向走。

        ⚠ 两种错法都在板端见过（T8-6 实测）:
          · 从"当前这张在排序里的名次"往后翻 -> 第二次跳到 index=**39/40**（用得多的一头）;
          · 不跳过当前这张 -> 第二次拿到同一张。
        现在的规则: **在"屏幕上没有"的那些里挑用得最少的**。
        """
        runtime, root, data_file = self._runtime(
            {"01_a.png": 5, "02_b.png": 0, "03_c.png": 0, "04_d.png": 2})
        first = runtime.next_wallpaper(1, sort="used_asc")
        second = runtime.next_wallpaper(1, sort="used_asc")
        self.assertEqual(os.path.basename(first["path"]), "02_b.png")
        self.assertEqual(os.path.basename(second["path"]), "03_c.png",
                         "第二张也是**没被用过**的（不是刚看过的那张, 更不是用得多的一头）")
        self.assertEqual(self._used_in_file(data_file, "02_b.png"), 1)
        self.assertEqual(self._used_in_file(data_file, "03_c.png"), 1)
        self.assertEqual(self._used_in_file(data_file, "01_a.png"), 5, "用得多的那张没被碰")
        self.assertEqual(self._used_in_file(data_file, "04_d.png"), 2)

    async def test_the_least_pick_skips_what_is_already_on_screen(self):
        """屏幕上那张哪怕就是最少用的, 也不该"挑"回它（用户是要**换**一张）。"""
        runtime, root, data_file = self._runtime({"01_a.png": 0, "02_b.png": 0})
        runtime.wallpaper.step(1)                  # 屏幕上先放 01_a（还没计过使用）
        result = runtime.next_wallpaper(1, sort="used_asc")
        self.assertEqual(os.path.basename(result["path"]), "02_b.png")

    async def test_most_also_skips_the_current_one(self):
        runtime, root, data_file = self._runtime({"01_a.png": 3, "02_b.png": 5})
        result = runtime.next_wallpaper(1, sort="used_desc")
        self.assertEqual(os.path.basename(result["path"]), "02_b.png")
        again = runtime.next_wallpaper(1, sort="used_desc")
        self.assertEqual(os.path.basename(again["path"]), "01_a.png")

    async def test_with_a_single_image_the_skip_does_not_empty_the_pool(self):
        runtime, root, data_file = self._runtime({"01_a.png": 0})
        result = runtime.next_wallpaper(1, sort="used_asc")
        self.assertTrue(result["ok"], "只有一张时不能把自己筛没了")
        self.assertEqual(os.path.basename(result["path"]), "01_a.png")

    async def test_a_plain_switch_walks_forward(self):
        """T8-6 顺手修的 T7-4 遗留 bug: 不给 match 的"换一张"以前每次都挑回第一张。

        实测（修前）: 连叫 4 次 `next_wallpaper(1)` 都是 `01_a.png` —— 因为给
        `step()` 传了 `anchor=None`（= "假装还没选过"）。普通换图必须按游标走。
        """
        runtime, root, data_file = self._runtime({"01_a.png": 0, "02_b.png": 0, "03_c.png": 0})
        forward = [os.path.basename(runtime.next_wallpaper(1)["path"]) for _ in range(4)]
        self.assertEqual(forward,
                         ["01_a.png", "02_b.png", "03_c.png", "01_a.png"], "翻到头回绕")
        backward = [os.path.basename(runtime.next_wallpaper(-1)["path"]) for _ in range(2)]
        self.assertEqual(backward, ["03_c.png", "02_b.png"], "往回也一样")
        self.assertEqual([self._used_in_file(data_file, n)
                          for n in ("01_a.png", "02_b.png", "03_c.png")],
                         [2, 2, 2], "每真换一次都各自 +1（01 被换到两次）")

    async def test_a_bad_sort_is_an_honest_failure(self):
        runtime, root, data_file = self._runtime({"01_a.png": 0})
        result = runtime.next_wallpaper(1, sort="plays_asc")
        self.assertFalse(result["ok"])
        self.assertIn("used_asc", result["error"], "错误里要说清楚能用什么")
        self.assertIn("换壁纸没有成功", result["tell_user"])
        self.assertEqual(self._used_in_file(data_file, "01_a.png"), 0, "失败时不计数")

    async def test_sort_without_tag_data_says_what_to_run(self):
        from agent.main import Runtime
        from agent.vision import wall_data

        root = make_dir("01_a.png")
        runtime = Runtime(
            config={"wallpaper": {"dir": root, "tagging": {
                "data_file": os.path.join(root, wall_data.DEFAULT_DATA_FILE)}},
            },
            start_native=False, start_terminal=False,
            log=logging.getLogger("test.wallpaper.usage"),
        )
        runtime.wallpaper = WallpaperDeck(root)
        runtime._tag_index = None
        result = runtime.next_wallpaper(1, sort="used_asc")
        self.assertFalse(result["ok"])
        self.assertIn("assistant tag", result["error"],
                      "没标签数据 -> 给一句能照做的（按使用次数也要先打标签）")

    async def test_an_image_outside_the_data_file_is_not_counted(self):
        runtime, root, data_file = self._runtime({"01_a.png": 3}, extra=("00_new.png",))
        result = runtime.next_wallpaper(1)         # 名字排在前面 -> 挑到没打过标签的那张
        self.assertTrue(result["ok"], "没进过数据文件的图照样能显示")
        self.assertEqual(os.path.basename(result["path"]), "00_new.png")
        self.assertEqual(result["used"], 0, "它不在数据文件里 -> 没次数可计")
        self.assertEqual(self._used_in_file(data_file, "01_a.png"), 3, "别的图不受影响")

    async def test_a_broken_data_file_does_not_break_switching(self):
        """统计坏了是**统计**的事: 换壁纸照样成功（只记 warning）。"""
        runtime, root, data_file = self._runtime({"01_a.png": 0, "02_b.png": 0})
        with open(data_file, "w", encoding="utf-8") as handle:
            handle.write("{这不是 JSON\n")
        runtime._tag_index = None
        result = runtime.next_wallpaper(1)
        self.assertTrue(result["ok"])
        self.assertEqual(result["used"], 0, "读不到次数就按 0")
        self.assertEqual(os.path.basename(result["path"]), "01_a.png")

    async def test_the_tags_summary_reports_usage(self):
        runtime, root, data_file = self._runtime({"01_a.png": 5, "02_b.png": 0, "03_c.png": 2})
        summary = runtime.wallpaper_tags()
        self.assertTrue(summary["ok"])
        usage = summary["usage"]
        self.assertEqual(usage["least_used"][0]["name"], "02_b.png")
        self.assertEqual(usage["most_used"][0]["name"], "01_a.png")
        self.assertEqual(usage["never_used"], 1, "一次都没用过的张数")
        self.assertIn("used_asc", summary["note"],
                      "tags 回话里要告诉模型怎么挑\"用得最少的\"")


class TestWindow(unittest.TestCase):
    """T10-1: 三格窗口（上一个 / 当前 / 下一个）—— 你定的那套走法。

    `prev` **只留 1 张**（真实走过的那张）; `next` 是"要来的一张", 由调用方（画像/指定）塞进来;
    推进 = `next` 变 `current`; 后退 = 反过来; `prev` 空着时退回老规则（按文件名往前翻）。
    """

    def _deck(self, *names):
        root = make_dir(*(names or ("01_a.png", "02_b.png", "03_c.png")))
        return WallpaperDeck(root), root

    def _name(self, path):
        return os.path.basename(path)

    def test_a_fresh_window_is_empty(self):
        deck, _root = self._deck()
        self.assertEqual(deck.window(),
                         {"prev": None, "current": None, "next": None,
                          "history_size": 0, "ready": False})

    def test_set_next_fills_only_that_slot(self):
        deck, root = self._deck()
        deck.step(1)                                  # current = 01_a
        deck.set_next(os.path.join(root, "03_c.png"))
        window = deck.window()
        self.assertEqual(self._name(window["current"]), "01_a.png")
        self.assertEqual(self._name(window["next"]), "03_c.png")
        self.assertIsNone(window["prev"])
        self.assertTrue(window["ready"])
        self.assertEqual(deck.set_next(""), None, "空串 = 清掉这一格")

    def test_advance_moves_next_into_current_and_keeps_one_history(self):
        deck, root = self._deck()
        deck.step(1)
        deck.set_next(os.path.join(root, "03_c.png"))
        index, path, total = deck.advance()
        self.assertEqual(self._name(path), "03_c.png")
        self.assertEqual((index, total), (2, 3), "报的是它在目录里的位置")
        self.assertEqual(deck.current(), path)
        window = deck.window()
        self.assertEqual(self._name(window["prev"]), "01_a.png", "原来那张进历史")
        self.assertIsNone(window["next"], "推完就空着 —— 要不要重算由调用方决定")
        self.assertEqual(window["history_size"], 1)

    def test_advance_without_a_next_is_an_honest_error(self):
        deck, _root = self._deck()
        deck.step(1)
        with self.assertRaises(WallpaperError) as ctx:
            deck.advance()
        self.assertIn("下一个", str(ctx.exception))
        self.assertIn("set_next", str(ctx.exception))
        self.assertEqual(self._name(deck.current()), "01_a.png", "失败时不动窗口")

    def test_advance_refuses_a_next_that_vanished(self):
        deck, root = self._deck()
        deck.step(1)
        gone = os.path.join(root, "03_c.png")
        deck.set_next(gone)
        os.remove(gone)
        with self.assertRaises(WallpaperError) as ctx:
            deck.advance()
        self.assertIn("不在壁纸目录里", str(ctx.exception))
        self.assertEqual(self._name(deck.current()), "01_a.png", "失败时不动窗口")

    def test_back_swaps_with_prev(self):
        deck, root = self._deck()
        deck.step(1)                                  # 01_a
        deck.set_next(os.path.join(root, "03_c.png"))
        deck.advance()                                # 03_c, prev = 01_a
        index, path, total = deck.back()
        self.assertEqual(self._name(path), "01_a.png")
        self.assertEqual((index, total), (0, 3))
        window = deck.window()
        self.assertIsNone(window["prev"], "退过一次之后就没有更早的了（只留 1 张）")
        self.assertEqual(self._name(window["next"]), "03_c.png", "再往后走能回到刚才那张")
        self.assertEqual(window["history_size"], 0)

    def test_back_then_forward_returns_to_where_we_were(self):
        deck, root = self._deck()
        deck.step(1)
        deck.set_next(os.path.join(root, "03_c.png"))
        deck.advance()
        deck.back()
        _index, path, _total = deck.advance()
        self.assertEqual(self._name(path), "03_c.png", "back 之后 next 就是刚才那张")

    def test_back_without_history_falls_back_to_the_old_rule(self):
        """A5（你定的）: prev 空着 -> 按文件名往前翻一张, 并把翻之前那张记成 prev。"""
        deck, _root = self._deck()
        deck.snapshot(initialise=True)                # 屏幕上先有第一张（prev 还是空的）
        self.assertEqual(self._name(deck.current()), "01_a.png")
        self.assertIsNone(deck.window()["prev"])
        _index, path, _total = deck.back()
        self.assertEqual(self._name(path), "03_c.png", "按文件名往前翻（越界回绕）")
        self.assertEqual(self._name(deck.window()["prev"]), "01_a.png",
                         "翻之前那张记成 prev（\"上一张\"不给空话）")

    def test_back_without_history_gives_the_last_by_name(self):
        deck, _root = self._deck()
        _index, path, _total = deck.back()
        self.assertEqual(self._name(path), "03_c.png", "没选过时往前翻 = 最后一张")
        self.assertIsNone(deck.window()["prev"], "一张都没走过, 没有历史可记")

    def test_step_keeps_history_and_clears_a_stale_next(self):
        deck, root = self._deck()
        deck.step(1)
        deck.set_next(os.path.join(root, "03_c.png"))
        deck.step(1)                                  # 普通翻页 -> 02_b
        window = deck.window()
        self.assertEqual(self._name(window["current"]), "02_b.png")
        self.assertEqual(self._name(window["prev"]), "01_a.png")
        self.assertIsNone(window["next"], "换了一张 -> 旧的 next 不算数了")

    def test_repeat_does_not_touch_the_window(self):
        deck, root = self._deck()
        deck.step(1)
        deck.set_next(os.path.join(root, "03_c.png"))
        deck.step(0)                                  # 重推当前这张
        window = deck.window()
        self.assertEqual(self._name(window["current"]), "01_a.png")
        self.assertEqual(self._name(window["next"]), "03_c.png", "重推不是\"换了一张\"")
        self.assertIsNone(window["prev"])

    def test_the_window_survives_a_rescan(self):
        deck, root = self._deck()
        deck.step(1)
        deck.set_next(os.path.join(root, "03_c.png"))
        with open(os.path.join(root, "00_new.png"), "w", encoding="utf-8") as handle:
            handle.write("x")
        self.assertEqual(self._name(deck.window()["next"]), "03_c.png", "路径不受新图影响")
        _index, path, _total = deck.advance()
        self.assertEqual(self._name(path), "03_c.png")


class _ProfileIndex:
    """只给"按画像挑"用的替身: 按 spec 给排序（真 TagIndex 也是这个形状）。"""

    def __init__(self, pools=None):
        self.pools = dict(pools or {})
        self.calls = []

    def count(self):
        return 3

    def match(self, spec, presets=None, wallpaper_dir=None, limit=None):
        from agent.vision.tag_index import MatchResult

        self.calls.append((spec, wallpaper_dir))
        return MatchResult(pool=list(self.pools.get(spec, [])), kind="ip", note="ip",
                           detail={})

    def usage_of(self, path):
        return {"used": 0, "last_used": None}

    def tags_of(self, path):
        return {"mood": [["calm", 0.4]]} if str(path).endswith("c.png") else {}


class TestWindowDrivenSwitching(unittest.IsolatedAsyncioTestCase):
    """T10-3: `Runtime.next_wallpaper` 走三格窗口（stage / pick / next / prev）。

    ⚠ 用 `IsolatedAsyncioTestCase`: 板端 Python 3.8 里 `Runtime.__init__` 建 asyncio 原语
      需要当前线程有事件循环（见 tests/test_read_intents.py 里那段说明）。
    """

    def _runtime(self, root=None, profile=None, index=None):
        import json
        import tempfile

        from agent.core.user_profile import PROFILE_VERSION
        from agent.main import Runtime

        root = root or make_dir("01_a.png", "02_b.png", "03_c.png")
        profile_file = os.path.join(tempfile.mkdtemp(prefix="profile-"), "user_profile.jsonl")
        if profile is not None:
            with open(profile_file, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(dict(profile, version=PROFILE_VERSION),
                                        ensure_ascii=False) + "\n")
        runtime = Runtime(
            config={"wallpaper": {"dir": root,
                                  "tagging": {"data_file": "/tmp/wall_data.jsonl",
                                              "ip_presets": {"EVA": {"anchors": ["01_a.png"]}}}}},
            start_native=False, start_terminal=False,
            log=logging.getLogger("test.wallpaper.window"))
        runtime.wallpaper = WallpaperDeck(root)
        runtime.profile_file = profile_file
        runtime._tag_index = index or _ProfileIndex()
        return runtime, root

    def _names(self, window):
        return {key: (os.path.basename(value) if value else None)
                for key, value in window.items() if key in ("prev", "current", "next")}

    async def test_stage_only_prepares_the_next_one(self):
        """你定的第 3 条①③: 只更新"下一个" —— 不切屏、不推送、不计使用次数。"""
        runtime, root = self._runtime()
        runtime._tag_index = _FakeIndex(pool=[os.path.join(root, "03_c.png"),
                                              os.path.join(root, "01_a.png")],
                                        kind="axis")
        pushed = []
        runtime.on_wallpaper = lambda path, index: pushed.append(path)
        result = runtime.next_wallpaper(stage=True, match="scene=anime")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["staged"])
        self.assertFalse(result["pushed"])
        self.assertEqual(pushed, [], "stage 不切屏")
        self.assertEqual(self._names(result["window"])["next"], "03_c.png")
        self.assertIsNone(self._names(result["window"])["current"], "屏幕没动")
        self.assertEqual(runtime._last_shown_wallpaper, "", "stage 不算「换成了另一张」")

    async def test_pick_replaces_next_then_advances_then_refills(self):
        runtime, root = self._runtime()
        runtime._tag_index = _FakeIndex(pool=[os.path.join(root, "03_c.png")], kind="axis")
        pushed = []
        runtime.on_wallpaper = lambda path, index: pushed.append(os.path.basename(path))
        result = runtime.next_wallpaper(1, match="scene=anime")
        self.assertEqual(os.path.basename(result["path"]), "03_c.png")
        self.assertEqual(pushed, ["03_c.png"], "pick 会立刻切")
        window = self._names(result["window"])
        self.assertEqual(window["current"], "03_c.png")
        self.assertEqual(window["next"], "01_a.png", "切完**又塞了一个**下一个（无画像时按文件名）")
        self.assertIsNone(window["prev"], "第一张没有历史")

    async def test_plain_next_advances_the_window(self):
        runtime, root = self._runtime()
        first = runtime.next_wallpaper(1)
        second = runtime.next_wallpaper(1)
        self.assertEqual(os.path.basename(first["path"]), "01_a.png")
        self.assertEqual(os.path.basename(second["path"]), "02_b.png")
        window = self._names(second["window"])
        self.assertEqual(window["prev"], "01_a.png", "prev 只留 1 张")
        self.assertEqual(window["next"], "03_c.png")

    async def test_prev_walks_back_and_keeps_the_way_forward(self):
        runtime, root = self._runtime()
        runtime.next_wallpaper(1)                     # 01_a
        runtime.next_wallpaper(1)                     # 02_b
        back = runtime.next_wallpaper(-1)             # 回 01_a
        self.assertEqual(os.path.basename(back["path"]), "01_a.png")
        self.assertEqual(self._names(back["window"])["next"], "02_b.png",
                         "退回来之后还能往前走回去")

    async def test_the_profile_drives_the_next_one(self):
        """画像可用时 -> "下一个"按理应按画像挑（画像里最喜欢 EVA, 排序把 c 放第一）。"""
        root = make_dir("01_a.png", "02_b.png", "03_c.png")
        profile = {"walls": {"ip": [{"name": "EVA", "weight": 0.9, "parts": {"hits": 3}}]},
                   "music": {"artist": []}, "mood": {"label": "calm", "ok": True}, "thin": []}
        index = _ProfileIndex({"ip=EVA": [os.path.join(root, "03_c.png"),
                                          os.path.join(root, "01_a.png"),
                                          os.path.join(root, "02_b.png")]})
        runtime, _root = self._runtime(root=root, profile=profile, index=index)
        chosen = runtime._choose_next()
        self.assertEqual(chosen["basis"], "profile")
        self.assertEqual(os.path.basename(chosen["path"]), "03_c.png")
        self.assertTrue(any("像 EVA" in reason for reason in chosen["why"]),
                        "挑图理由要说清是「像哪个 IP」: %s" % chosen["why"])
        result = runtime.next_wallpaper(1)
        self.assertEqual(os.path.basename(result["path"]), "03_c.png",
                         "画像说最喜欢 EVA -> 最像 EVA 的那张进「下一个」")
        self.assertIn("ip=EVA", [call[0] for call in index.calls])

    async def test_an_unusable_profile_falls_back_to_the_filename_order(self):
        profile = {"walls": {"ip": [{"name": "EVA", "weight": 0.9}]},
                   "music": {"artist": []}, "mood": {"label": "unknown", "ok": False},
                   "thin": ["壁纸用量"]}
        runtime, root = self._runtime(profile=profile)
        chosen = runtime._choose_next()
        self.assertEqual(chosen["basis"], "order", "样本薄 + 心情不知道 -> 回退")
        result = runtime.next_wallpaper(1)
        self.assertEqual(os.path.basename(result["path"]), "01_a.png",
                         "回退就是按文件名（老行为）, 不是画像乱挑")

    async def test_a_profile_build_refreshes_the_next_one(self):
        runtime, root = self._runtime()
        calls = []
        runtime._refill_next = lambda: calls.append("refill")
        await runtime._build_profile_task({"reason": "test"})
        self.assertEqual(calls, ["refill"], "画像构建完要重挑一次「下一个」（你定的第 3 条②）")

    async def test_a_profile_build_also_applies_the_queue_effects(self):
        """T10-5: 画像构建完还要动队列（负反馈清队列 / 心情变了重置）—— 上一条要带过去。"""
        runtime, root = self._runtime()
        seen = []
        runtime._refill_next = lambda: None
        runtime._apply_profile_effects = lambda record, previous=None: seen.append(
            (str((record.get("mood") or {}).get("label")), previous))
        await runtime._build_profile_task({"reason": "test"})
        self.assertEqual(len(seen), 1, "构建完要调一次队列动作")
        self.assertIsNone(seen[0][1], "第一次构建没有上一条可比")


if __name__ == "__main__":
    unittest.main(verbosity=2)
