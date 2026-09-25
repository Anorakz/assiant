#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_tool_permissions.py — 工具的状态权限表（Phase 7 T4）

跑法:
    python tests/test_tool_permissions.py

为什么单独一个文件
    权限不是"某个工具的实现细节", 而是一张**表**。表就得只有一处写下来, 并且
    "加了一个工具却没决定它在哪些状态可用"必须**立刻红**。所以这里:

      · `EXPECTED` 是唯一写下来的期望矩阵（4 个状态 × 每个工具）
      · 表里的每个工具都要真的注册得到, 注册得到的每个工具都要在表里 —— 双向对齐
      · 除了"表对不对", 还要验"**真的拒绝了**": 每个不允许的组合都跑一次
        `ToolRouter.execute()`, 要求被拒 **且 handler 一次都没跑**（副作用计数为 0）
      · 给 LLM 看的清单也要按状态过滤（T4 顺手修的那一处, 见
        `agent/llm/provider.py::_advertised_tools`）

这张表（你 T4 拍的板；T7-3 加过 list_wallpaper_tags；T8-5b 合并成**三个**工具）:

    | 状态  | back_to_desktop | next_wallpaper | next_music |
    | SLEEP |        ✗        |       ✗        |     ✗      |
    | IDLE  |        ✗        |       ✓        |     ✓      |
    | STUDY |        ✓        |       ✓        |     ✓      |
    | GAME  |        ✗        |       ✗        |     ✗      |

    T8-5b 把七个工具合并成三个（每个工具用 `action` 分派具体动作）:
      · next_wallpaper = 翻页 / 按内容挑 / 看标签（原来的 next_wallpaper + list_wallpaper_tags）
      · next_music     = 排进队列 / 清空队列 / 看清单 / 搜 / 状态 / 打标签 / 音量
                         （原来的音乐四件套; transport 交给 GUI 按钮）
      · back_to_desktop = 回到桌面（无参数, 只 STUDY）
    合并的动机是 **prompt 预算**: T8-5 实测 7 个工具的工具清单占第一轮 prompt 的 90%
    （1699 / 1898 token）, 第二轮 2131 直接撞穿 ctx。合并后模型只认 3 个名字。

为什么 SLEEP / GAME 一个都不给
    · SLEEP = "睡眠": 不让模型动系统里的任何东西, 最保守的一档
    · GAME  = 主区是视频区, 换壁纸等于白换; 而"回到桌面"是**学习收尾**的动作
      （T1 的决定, 工具说明里也写着"别在 GAME/IDLE 里乱按"）

⚠ T7-3 撤掉了 T6① 的一半
    T6① 曾规定"GUI 的 `next_wallpaper` **命令**也受这张表约束"（靠
    `ToolRouter.allowed_in_current_state()`）。T7-3 按需求删掉了手动换壁纸 ——
    按钮、同名 IPC 命令、以及那个只服务于它的路由方法**一起**下线了。
    现在这张表只管**工具**（而工具只有对话这一条路能调到, 见
    `agent/main.py::Runtime.next_wallpaper`）。
    ⚠ 音乐按钮（T8-4 的 music_play_pause 等）**不在**这张表里: 它们是 GUI 的
      transport（播放/暂停/上一首/下一首）, 不决定放什么; 音乐开关是 `music.enabled`。
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

from agent.core.state_machine import State, StateMachine  # noqa: E402
from agent.core.tool_router import ToolRouter  # noqa: E402
from agent.core.wallpaper import WallpaperDeck  # noqa: E402
from agent.llm import LLMProvider  # noqa: E402
from agent.tools import TOOL_MODULES, build_tools  # noqa: E402

logging.disable(logging.CRITICAL)

#: 唯一写下来的权限表: 工具名 -> 允许的状态集合。加工具时**必须**在这里加一行。
EXPECTED = {
    # 回到桌面: 只有 STUDY（学习收尾）, 且**无参数**
    "back_to_desktop": {State.STUDY},
    # 壁纸的一切（翻页 / 按内容挑 / 看标签）—— 都由这一个工具 + action 承担
    "next_wallpaper": {State.IDLE, State.STUDY},
    # 音乐的一切（排队列 / 清空 / 清单 / 搜 / 状态 / 标签 / 音量）
    "next_music": {State.IDLE, State.STUDY},
}

#: T8-5b: 全部工具就是这三个（几次断言要一起数）
TOOL_NAMES = ("back_to_desktop", "next_wallpaper", "next_music")

#: 对应的**模块**名（`TOOL_MODULES` 里写的是模块名, 不是工具名）
TOOL_MODULE_NAMES = ("back_to_desktop", "wallpaper", "music")

#: 有**必填**参数的工具, 给一份合法参数（回桌面没有参数）。
#: ⚠ 这不是"权限表"的一部分, 只是让"放行"那条断言真的走到 handler;
#:   刻意选**只碰一个入口**的动作（status 会同时问状态与队列 = 两格）。
_SAMPLE_ARGS = {
    "next_wallpaper": {"action": "next"},
    "next_music": {"action": "clear_queue"},
}

#: 四个状态各自**应该**看到哪些工具（由 EXPECTED 推出来, 不手写第二份）
EXPECTED_BY_STATE = {
    state: {name for name, states in EXPECTED.items() if state in states}
    for state in State
}


def make_dir(*names):
    """造一个临时壁纸目录（文件内容无所谓, 只看后缀）。"""
    root = tempfile.mkdtemp(prefix="perm_wallpaper_")
    for name in names:
        with open(os.path.join(root, name), "w", encoding="utf-8") as handle:
            handle.write("not really an image")
    return root


def go_to(machine, state):
    """走到目标状态。

    状态机的规矩是"任何切换都必须经过 IDLE"（IDLE 是唯一公共锚点），初始状态就是 IDLE ——
    所以这里先退回 IDLE 再进目标状态，同一个 machine 反复调用也成立。
    """
    if machine.current() is state:
        return
    if machine.current() is not State.IDLE:
        machine.transition(State.IDLE, "test")
    if state is not State.IDLE:
        machine.transition(state, "test")
    assert machine.current() is state, machine.current()


#: 计数替身里的每一格（每个工具一个入口 —— 一个工具跑一次, 恰好点亮一格）
CALL_KEYS = ("desktop", "wallpaper", "tags", "music_list", "music_search", "music_state",
             "music_enqueue", "music_queue_clear", "music_queue_state", "music_tag",
             "music_control")


def _no_calls():
    """全 0 的计数替身。"""
    return {key: 0 for key in CALL_KEYS}


def make_router(machine=None):
    """真路由 + 三个真工具, 依赖换成**计数替身**（这样能验"handler 到底跑没跑"）。

    ⚠ 依赖给不齐是有意的**反向断言**的落点: 少了某个入口, `build_tools()` 就把那个工具
      整个跳过（音乐没开就是这样消失的）, 于是
      `test_every_registered_tool_is_in_the_table` 立刻红 —— 所以这里每个入口都给。

    @param machine 状态源; 不给就自己建一个（初始 IDLE）
    """
    calls = _no_calls()

    class _Sender:
        async def show_desktop(self):
            calls["desktop"] += 1

    def _next_wallpaper(step=1, match=None, sort=None):
        calls["wallpaper"] += 1
        return {"ok": True, "path": "/w/1.png", "index": 0, "total": 1, "pushed": True}

    def _wallpaper_tags(ip_query=None, limit=5):
        calls["tags"] += 1
        return {"ok": True, "count": 1, "axes": {}}

    def _music_list(tag=None, axis=None, sort="plays_asc", limit=10):
        calls["music_list"] += 1
        return {"ok": True, "count": 1, "tracks": [{"id": "1", "name": "x"}]}

    def _music_search(keyword, limit=10):
        calls["music_search"] += 1
        return {"ok": True, "count": 1, "tracks": [{"id": "1", "name": "x"}]}

    def _music_state():
        calls["music_state"] += 1
        return {"ok": True, "track_id": "1", "playing": True}

    def _music_enqueue(track_id=None, keyword=None, tag=None, sort="plays_asc",
                       limit=1, replace=False):
        calls["music_enqueue"] += 1
        return {"ok": True, "queued": ["1"], "started": True}

    def _music_queue_clear():
        calls["music_queue_clear"] += 1
        return {"ok": True, "cleared": 1}

    def _music_queue_state():
        calls["music_queue_state"] += 1
        return {"ok": True, "size": 1, "index": 0, "ids": ["1"], "tracks": []}

    def _music_tag(tags, track_id=None, remove=False):
        calls["music_tag"] += 1
        return {"ok": True, "id": track_id or "1"}

    def _music_control(action, **kwargs):
        calls["music_control"] += 1
        return {"ok": True, "action": action}

    machine = machine if machine is not None else StateMachine()
    router = ToolRouter(
        state_provider=machine,
        services={"input_sender": _Sender(), "next_wallpaper": _next_wallpaper,
                  "wallpaper_tags": _wallpaper_tags,
                  "music_list": _music_list, "music_search": _music_search,
                  "music_state": _music_state, "music_enqueue": _music_enqueue,
                  "music_queue_clear": _music_queue_clear,
                  "music_queue_state": _music_queue_state,
                  "music_tag": _music_tag, "music_control": _music_control},
    )
    for tool in build_tools(router):
        router.register(tool)
    return machine, router, calls


def _machine_in(state):
    """一个**已经走到** state 的状态机（T6 的"真 Runtime"用例要拿它当 runtime.state）。"""
    machine = StateMachine()
    go_to(machine, state)
    return machine


class TestTheTable(unittest.TestCase):
    def test_every_registered_tool_is_in_the_table(self):
        _, router, _ = make_router()
        registered = set(router.names())
        self.assertEqual(
            registered, set(EXPECTED),
            "注册的工具与权限表对不上 —— 加了新工具就必须在 EXPECTED 里决定它在哪些状态可用",
        )

    def test_every_module_has_a_table_row(self):
        # TOOL_MODULES 是"加了工具就加一行"的那份清单, 两份清单不能各走各的
        table_names = set(EXPECTED)
        self.assertEqual(
            table_names,
            set(TOOL_NAMES),
            "权限表少了一行或多了一行: %s" % sorted(table_names),
        )
        self.assertEqual(len(TOOL_MODULES), len(table_names),
                         "TOOL_MODULES 与权限表的条数不一样: %s" % (TOOL_MODULES,))
        self.assertEqual(tuple(TOOL_MODULES), TOOL_MODULE_NAMES,
                         "T8-5b: 只有三个工具模块, 且顺序稳定")
        _machine, router, _calls = make_router()
        # `router.names()` 是**按名字排序**的（权限表也是按名字查）, 所以这里比集合
        self.assertEqual(set(router.names()), set(TOOL_NAMES),
                         "模块名 -> 工具名: 三个模块正好造出三个工具")

    def test_each_tool_allows_exactly_the_states_in_the_table(self):
        _, router, _ = make_router()
        for name, allowed in EXPECTED.items():
            self.assertEqual(
                router.get(name).allowed_states, allowed,
                "%s 的 allowed_states 与权限表不一致" % name,
            )

    def test_the_table_is_not_vacuous(self):
        # 反空转: 表里不能全是空集, 也不能每个状态都一样（那样这条测试等于没查）
        self.assertTrue(any(EXPECTED.values()), "权限表里一个允许项都没有?")
        self.assertGreater(len({frozenset(v) for v in EXPECTED.values()}), 1,
                           "所有工具的权限集合都一样 —— 表可能写错了")


class TestRouterFollowsTheTable(unittest.TestCase):
    def test_allowed_tools_per_state(self):
        machine, router, _ = make_router()
        for state in State:
            go_to(machine, state)
            names = {tool["name"] for tool in router.allowed_tools()}
            self.assertEqual(names, EXPECTED_BY_STATE[state],
                             "%s 下能用的工具与表不一致" % state.value)

    def test_list_tools_is_state_independent(self):
        # list_tools() 是"注册了什么", allowed_tools() 才是"现在能用什么" —— 两者别混
        machine, router, _ = make_router()
        everything = {t["name"] for t in router.list_tools()}
        self.assertEqual(everything, set(EXPECTED))
        go_to(machine, State.GAME)
        self.assertEqual({t["name"] for t in router.list_tools()}, everything,
                         "list_tools() 不该随状态变 —— 按状态过滤是 allowed_tools() 的事")

    def test_sleep_and_game_allow_nothing(self):
        # 你 T4 拍的那两条: 睡眠与游戏模式一个工具都不给
        machine, router, _ = make_router()
        for state in (State.SLEEP, State.GAME):
            go_to(machine, state)
            self.assertEqual(router.allowed_tools(), [],
                             "%s 下不该有任何工具" % state.value)


class TestForbiddenCombinationsAreReallyRefused(unittest.IsolatedAsyncioTestCase):
    """表说"不允许"的组合, 必须**真的**被拒, 而且 handler 一次都不跑。"""

    async def test_every_forbidden_pair_is_refused_without_side_effects(self):
        checked = 0
        for state in State:
            for name, allowed in EXPECTED.items():
                if state in allowed:
                    continue
                machine, router, calls = make_router()
                go_to(machine, state)
                result = await router.execute(name, {})
                self.assertFalse(result["ok"], "%s/%s 应当被拒" % (state.value, name))
                self.assertIn("not allowed", result["error"])
                self.assertEqual(calls, _no_calls(),
                                 "%s/%s 被拒时 handler 不该跑" % (state.value, name))
                checked += 1
        # 反空转: 真的验到了组合（SLEEP 3 + GAME 3 + IDLE 1 = 7）
        self.assertEqual(checked, 7, "遍历到的禁止组合数不对: %d" % checked)

    async def test_every_allowed_pair_really_runs(self):
        checked = 0
        for state in State:
            for name in EXPECTED_BY_STATE[state]:
                machine, router, calls = make_router()
                go_to(machine, state)
                # 有必填参数的工具（next_wallpaper/next_music 的 action）得给一份合法参数
                # —— 否则会卡在 schema 校验上, 验的就不是权限了。
                result = await router.execute(name, dict(_SAMPLE_ARGS.get(name, {})))
                self.assertTrue(result["ok"], "%s/%s 应当放行: %r"
                                % (state.value, name, result))
                self.assertEqual(sum(calls.values()), 1,
                                 "%s/%s 放行时 handler 应当正好跑一次" % (state.value, name))
                checked += 1
        # 反空转: 真的验到了允许的组合（STUDY 3 + IDLE 2 = 5）
        self.assertEqual(checked, 5, "遍历到的允许组合数不对: %d" % checked)


class TestModelOnlySeesAllowedTools(unittest.IsolatedAsyncioTestCase):
    """给 LLM 的清单也要按状态过滤（T4 修的那一处）。"""

    class _FakeCompletions:
        def __init__(self):
            self.requests = []

        def create(self, **kwargs):
            self.requests.append(kwargs)

            class _Message:
                content = "好"
                tool_calls = None

            class _Choice:
                message = _Message()
                finish_reason = "stop"

            class _Response:
                choices = [_Choice()]

            return _Response()

    def _provider(self, state):
        from agent.llm import CloudBackend

        machine, router, _ = make_router()
        go_to(machine, state)
        backend = CloudBackend(client=self._client())
        return LLMProvider(mode="cloud", tools=router, cloud_backend=backend), backend

    def _client(self):
        class _Chat:
            def __init__(self):
                self.completions = TestModelOnlySeesAllowedTools._FakeCompletions()

        class _Client:
            def __init__(self):
                self.chat = _Chat()

        return _Client()

    async def test_study_sees_every_allowed_tool(self):
        provider, backend = self._provider(State.STUDY)
        await provider.chat_with_tools("x", {})
        request = backend.client.chat.completions.requests[0]
        self.assertEqual([t["function"]["name"] for t in request["tools"]],
                         ["back_to_desktop", "next_music", "next_wallpaper"],
                         "STUDY 下三个工具都给, 按名字排序")

    async def test_idle_sees_no_back_to_desktop(self):
        provider, backend = self._provider(State.IDLE)
        await provider.chat_with_tools("x", {})
        request = backend.client.chat.completions.requests[0]
        self.assertEqual([t["function"]["name"] for t in request["tools"]],
                         ["next_music", "next_wallpaper"],
                         "清单按名字排序, 顺序也要稳定; IDLE 只是少了 back_to_desktop")

    async def test_sleep_and_game_advertise_no_tools_at_all(self):
        for state in (State.SLEEP, State.GAME):
            provider, backend = self._provider(state)
            await provider.chat_with_tools("x", {})
            request = backend.client.chat.completions.requests[0]
            self.assertNotIn("tools", request,
                             "%s 下不该给模型任何工具候选" % state.value)
            self.assertNotIn("tool_choice", request)


class TestTheRevertedT6Rule(unittest.IsolatedAsyncioTestCase):
    """T7-3: "GUI 命令也受这张表约束"这条(T6①)**按要求撤掉了**。

    手动换壁纸（GUI 按钮 + 同名 IPC 命令）已删除, 换壁纸只剩对话一条路 ——
    所以那个只服务于 GUI 命令的路由方法也一起删除。这条测试把"删除"钉成机械可查的
    事实: 方法没了、命令没了、命令处理器对它是未知命令（只记日志, 不假装成功）。

    ⚠ 这不是权限被削弱: 工具那张表**照旧**是唯一判据, 而工具只能从对话调到
      （`ToolRouter.execute()` 拦），所以"能不能在这个状态换壁纸"仍然只有一个答案。
    """

    def test_the_router_method_is_gone(self):
        from agent.core.tool_router import ToolRouter

        self.assertFalse(hasattr(ToolRouter, "allowed_in_current_state"),
                         "它唯一的调用方（GUI 命令）已删除, 方法也该删除而不是留着当摆设")
        # 但工具权限本身照旧
        _, router, _ = make_router()
        self.assertFalse(router.is_allowed("back_to_desktop"), "IDLE 下不给回到桌面")
        self.assertTrue(router.is_allowed("next_wallpaper"))

    async def test_the_old_command_is_now_unknown(self):
        from agent.ipc import _make_command_handler

        pushed = []
        handler = _make_command_handler(bus=None, runtime=None,
                                        push=lambda topic, data: pushed.append((topic, data)))
        await handler("next_wallpaper", {})
        self.assertEqual(pushed, [], "老客户端发老命令: 只记日志, 不假装换好了")


class TestTheConnectPush(unittest.IsolatedAsyncioTestCase):
    """T6: 新 GUI 连上时补推当前壁纸（`wallpaper` 是"变化才推", 连上不会自动收到）。"""

    async def test_build_ipc_wires_the_hook(self):
        from agent.ipc import build_ipc

        runtime = self._runtime_with_deck()
        server = build_ipc(None, {"ipc": {"socket_path": ""}}, runtime)
        self.assertTrue(callable(getattr(server, "on_client_connect", None)),
                        "build_ipc 该把'连上补推'挂在 server 上（空实现也要收下这个参数）")

    async def test_the_hook_selects_and_pushes_the_first_one(self):
        # 真按 server 的路径叫一次: 该把第一张选出来并推出去
        from agent.ipc.local_server import LocalServer

        runtime = self._runtime_with_deck()
        seen = []
        runtime.on_wallpaper = lambda path, index: seen.append((path, index))
        server = LocalServer(path="/tmp/never-started.sock",
                             on_client_connect=runtime.push_current_wallpaper)
        await server._notify_client_connect()
        self.assertEqual(len(seen), 1, "连上就该推一张")
        self.assertEqual(os.path.basename(seen[0][0]), "01_a.png")

    def _runtime_with_deck(self):
        from agent.main import Runtime

        runtime = Runtime(config={"wallpaper": {"dir": make_dir("01_a.png")}},
                          start_native=False, start_terminal=False,
                          log=logging.getLogger("test.permissions"))
        runtime.wallpaper = WallpaperDeck(runtime.config["wallpaper"]["dir"])
        return runtime

    async def test_a_broken_hook_does_not_break_the_wiring(self):
        from agent.ipc.local_server import LocalServer

        def boom():
            raise RuntimeError("补推炸了")

        server = LocalServer(path="/tmp/never-started.sock", on_client_connect=boom)
        await server._notify_client_connect()      # 不该抛
        # 没有 hook 的 server 是空操作
        await LocalServer(path="/tmp/never-started.sock")._notify_client_connect()


class TestDeckAndToolsAreTheRealOnes(unittest.TestCase):

    def test_wallpaper_tool_needs_both_entries(self):
        # 壁纸目录不存在也不该让工具消失（目录问题是运行期的, 见 tools/wallpaper.py）——
        # 但**入口**缺一个就整个不装: 模型看到的 action 列表必须与真实可用的完全一致
        empty = tempfile.mkdtemp()
        deck = WallpaperDeck(empty)
        self.assertEqual(deck.count(), 0)
        router = ToolRouter(services={"next_wallpaper": lambda step=1, match=None, sort=None: {"ok": True}})
        self.assertEqual([t.name for t in build_tools(router)], [],
                         "少了 wallpaper_tags -> 整个 next_wallpaper 不装")
        both = ToolRouter(services={"next_wallpaper": lambda step=1, match=None, sort=None: {"ok": True},
                                    "wallpaper_tags": lambda ip_query=None, limit=5: {}})
        self.assertEqual([t.name for t in build_tools(both)], ["next_wallpaper"])

    def test_every_tool_declares_states_explicitly(self):
        # 空集合 = 任何状态都不允许（fail closed）—— 工具不该"忘了写"就变成全放行
        _, router, _ = make_router()
        for name in router.names():
            self.assertTrue(router.get(name).allowed_states,
                            "%s 没有声明 allowed_states" % name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
