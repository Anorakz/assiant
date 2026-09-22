#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_wallpaper.py — 壁纸"下一张"（Phase 7 T3）

跑法:
    python tests/test_wallpaper.py

覆盖三件东西（正好是 T3 那三层）:

  1) `agent/core/wallpaper.py::WallpaperDeck` —— 列目录与游标
     后缀过滤 / 按文件名排序 / 目录不存在与空目录各自报什么 / 回绕 / step=0 /
     游标记的是**路径**（加新图、删当前图之后"下一张"仍然合理）/ 坏 step
  2) `agent/tools/wallpaper.py` —— LLM 那个工具
     缺依赖跳过 / 出现在 list_tools 里 / 状态权限（GAME 不给）/ step 参数校验 /
     工具**只转调** Runtime 的入口（不自己挑图、不自己推 IPC）
  3) `agent/main.py::Runtime.next_wallpaper` + IPC 命令 —— 两条入口共用一份实现
     换一张 -> 调 on_wallpaper 钩子 / 没有 IPC 时也能算（pushed=False）/
     目录坏了返回 ok=False + 给人看的原因 / 命令处理器把失败变成一句 llm 说明

⚠ 不测 GUI 画得像不像（那是 `gui/tests/test_image_fit.cpp` 与板端截图的事）,
  也不测真实图片能不能解码（Agent 不解码）。
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
from agent.ipc import NO_WALLPAPER_NOTE, UNWIRED_COMMAND_NOTES  # noqa: E402
from agent.ipc import protocol as p  # noqa: E402
from agent.ipc import _handle_next_wallpaper  # noqa: E402
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


# ===========================================================================
#  2) 工具
# ===========================================================================
class TestToolBuild(unittest.TestCase):
    def _services(self, calls):
        def advance(step=1):
            calls.append(step)
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

    def test_schema_forbids_extra_arguments(self):
        tool = wallpaper_tool.build(self._services([]))
        self.assertFalse(tool.schema["additionalProperties"])
        self.assertEqual(tool.schema["properties"]["step"]["type"], "integer")


class TestToolExecution(unittest.IsolatedAsyncioTestCase):
    def _router(self, state=State.IDLE):
        calls = []

        def advance(step=1):
            calls.append(step)
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
        self.assertEqual(calls, [1], "工具只是把入参转给 Runtime 的入口")
        self.assertEqual(result["result"]["path"], "/w/1.png")

    async def test_step_is_forwarded(self):
        router, calls = self._router()
        result = await router.execute("next_wallpaper", {"step": -1})
        self.assertEqual(calls, [-1])
        self.assertEqual(result["result"]["path"], "/w/-1.png")

    async def test_bad_argument_is_refused_before_running(self):
        router, calls = self._router()
        result = await router.execute("next_wallpaper", {"step": "next"})
        self.assertFalse(result["ok"])
        self.assertIn("invalid arguments", result["error"])
        self.assertEqual(calls, [], "参数不合法时 handler 一次都不该跑")

    async def test_refused_in_game_mode(self):
        # 游戏模式主区是视频区, 换壁纸没意义 —— 权限表里不给 GAME
        router, calls = self._router(State.GAME)
        result = await router.execute("next_wallpaper", {})
        self.assertFalse(result["ok"])
        self.assertIn("not allowed", result["error"])
        self.assertEqual(calls, [])

    async def test_allowed_in_idle_and_study(self):
        tool = wallpaper_tool.build({"next_wallpaper": lambda step=1: {}})
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
#  3) Runtime 入口 + IPC 命令
# ===========================================================================
class _FakeRuntime:
    """只带 next_wallpaper 的替身（够 IPC 命令处理器用）。"""

    def __init__(self, result):
        self.result = result
        self.calls = 0

    def next_wallpaper(self, step=1):
        self.calls += 1
        return self.result


class TestIpcCommand(unittest.TestCase):
    def test_next_wallpaper_is_no_longer_unwired(self):
        self.assertNotIn(
            p.COMMAND_NEXT_WALLPAPER, UNWIRED_COMMAND_NOTES,
            "T3 接上下游了, 这句'还没接入'的说明必须撤掉",
        )
        self.assertIn(p.COMMAND_NEXT_BILIBILI, UNWIRED_COMMAND_NOTES,
                      "B 站那条还没接, 别一起删了")

    def test_success_pushes_nothing_extra(self):
        pushed = []
        runtime = _FakeRuntime({"ok": True, "path": "/w/1.png", "index": 0})
        _handle_next_wallpaper(runtime, lambda topic, data: pushed.append((topic, data)))
        self.assertEqual(runtime.calls, 1)
        self.assertEqual(pushed, [], "成功时不往对话区写话, 界面反馈就是壁纸变了")

    def test_failure_becomes_a_readable_llm_note(self):
        pushed = []
        runtime = _FakeRuntime({"ok": False, "error": "壁纸目录不存在: /w"})
        _handle_next_wallpaper(runtime, lambda topic, data: pushed.append((topic, data)))
        self.assertEqual(len(pushed), 1)
        topic, data = pushed[0]
        self.assertEqual(topic, p.TOPIC_LLM)
        self.assertIn("壁纸目录不存在", data["text"])

    def test_without_a_wallpaper_module_it_still_says_something(self):
        pushed = []
        _handle_next_wallpaper(object(), lambda topic, data: pushed.append((topic, data)))
        self.assertEqual(pushed[0][1]["text"], NO_WALLPAPER_NOTE)

    def test_no_push_entry_does_not_crash(self):
        _handle_next_wallpaper(_FakeRuntime({"ok": False, "error": "x"}), None)


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
            self.assertEqual(len(runtime.tools), 2, "back_to_desktop + next_wallpaper")
        finally:
            await runtime.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
