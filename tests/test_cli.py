#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_cli.py — 板端控制 CLI（agent/cli.py）单测

两类：
  · **纯逻辑**：参数解析、socket 路径优先级、连不上时的提示文案 —— 哪都能跑；
  · **真 socket**：起**真的** `LocalServer`（生产同一份实现）当对端，验证
    `status` 在"Agent 推了"与"Agent 没推"两种情况下的行为 —— 只在 POSIX 上跑
    （Windows 的 CPython 没有 AF_UNIX，与 test_ipc_local_server 同一处理）。

为什么用真 server 而不是 mock：CLI 的价值就在于"跟真 Agent 说得上话"，
mock 掉对端等于把要验的东西验没了。
"""

import asyncio
import io
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent import cli  # noqa: E402
from agent.ipc.local_server import LocalServer, UNIX_SOCKET_SUPPORTED  # noqa: E402
from agent.ipc.protocol import (  # noqa: E402
    COMMAND_CHAT_INPUT,
    COMMAND_SWITCH_MODE,
    SOCKET_PATH,
    TOPIC_LLM,
    TOPIC_MUSIC,
    TOPIC_STATUS,
)


def _run_cli(argv):
    """跑一次 CLI，返回 (退出码, 标准输出, 标准错误)。

    ⚠ 只能在**没有**运行中的事件循环时调用（`main()` 内部是 `asyncio.run`）。
    """
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


# ===========================================================================
#  纯逻辑（哪都能跑）
# ===========================================================================
class TestCliParsing(unittest.TestCase):
    def test_no_command_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as ctx:
            cli.build_parser().parse_args([])
        self.assertEqual(ctx.exception.code, 2)

    def test_status_is_wired(self):
        args = cli.build_parser().parse_args(["status"])
        self.assertEqual(args.command, "status")
        self.assertIs(args.func, cli.cmd_status)

    def test_unknown_command_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as ctx:
            cli.build_parser().parse_args(["nope"])
        self.assertEqual(ctx.exception.code, 2)

    def test_defaults(self):
        args = cli.build_parser().parse_args(["status"])
        self.assertIsNone(args.socket)
        self.assertIsNone(args.config)
        self.assertEqual(args.timeout, cli.DEFAULT_TIMEOUT)

    def test_global_options_work_before_and_after_the_command(self):
        """两种写法都必须认 —— 只挂主命令上会拒掉后一种（argparse 的经典坑）。"""
        before = cli.build_parser().parse_args(
            ["--socket", "/a.sock", "--timeout", "1.5", "status"])
        after = cli.build_parser().parse_args(
            ["status", "--socket", "/a.sock", "--timeout", "1.5"])
        for args in (before, after):
            self.assertEqual(args.socket, "/a.sock")
            self.assertEqual(args.timeout, 1.5)
            self.assertEqual(args.command, "status")

    def test_value_before_command_is_not_overwritten_by_subparser_default(self):
        args = cli.build_parser().parse_args(["--timeout", "9", "status"])
        self.assertEqual(args.timeout, 9)


class TestSocketPathPriority(unittest.TestCase):
    def test_cli_value_wins(self):
        config = {"ipc": {"socket_path": "/from/config.sock"}}
        self.assertEqual(cli.socket_path_from("/from/cli.sock", config), "/from/cli.sock")

    def test_config_section_is_used(self):
        config = {"ipc": {"socket_path": "/from/config.sock"}}
        self.assertEqual(cli.socket_path_from(None, config), "/from/config.sock")

    def test_blank_and_broken_values_fall_back(self):
        self.assertEqual(cli.socket_path_from(None, {}), SOCKET_PATH)
        self.assertEqual(cli.socket_path_from(None, None), SOCKET_PATH)
        self.assertEqual(cli.socket_path_from(None, {"ipc": "不是映射"}), SOCKET_PATH)
        self.assertEqual(cli.socket_path_from(None, {"ipc": {"socket_path": "   "}}), SOCKET_PATH)
        self.assertEqual(cli.socket_path_from(None, {"ipc": {"socket_path": 5}}), SOCKET_PATH)


class TestModeNormalization(unittest.TestCase):
    def test_accepts_any_case_and_padding(self):
        self.assertEqual(cli.normalize_mode("study"), "STUDY")
        self.assertEqual(cli.normalize_mode("  GAME "), "GAME")
        self.assertEqual(cli.normalize_mode("idle"), "IDLE")
        self.assertEqual(cli.normalize_mode("sleep"), "SLEEP")

    def test_rejects_unknown_and_non_strings(self):
        self.assertIsNone(cli.normalize_mode("nope"))
        self.assertIsNone(cli.normalize_mode(""))
        self.assertIsNone(cli.normalize_mode(None))
        self.assertIsNone(cli.normalize_mode(5))


class TestTopicFilter(unittest.TestCase):
    def test_none_means_everything(self):
        self.assertIsNone(cli.parse_topics(None))

    def test_splits_trims_and_drops_empties(self):
        self.assertEqual(cli.parse_topics("status,llm"), {"status", "llm"})
        self.assertEqual(cli.parse_topics(" status , llm ,"), {"status", "llm"})

    def test_only_separators_means_nothing_given(self):
        self.assertIsNone(cli.parse_topics(" , , "))


class TestPushFormatting(unittest.TestCase):
    def test_compact_line(self):
        line = cli.format_push("status", {"mode": "STUDY", "connected": True},
                               cli.datetime(2026, 9, 21, 23, 51, 2))
        self.assertEqual(line, "23:51:02  status    mode=STUDY connected=true")

    def test_values_are_readable(self):
        self.assertEqual(cli.format_value(True), "true")
        self.assertEqual(cli.format_value(False), "false")
        self.assertEqual(cli.format_value(None), "-")
        self.assertEqual(cli.format_value(7), "7")
        self.assertEqual(cli.format_value({"a": 1}), '{"a": 1}')

    def test_newlines_are_escaped_ascii_only(self):
        """⚠ 不能引入 GBK 打不出的符号：那会让 CLI 在 PC 的 cmd 上直接抛异常。"""
        self.assertEqual(cli.format_value("第一行\n第二行"), "第一行\\n第二行")
        self.assertEqual(cli.format_value("a\r\nb"), "a\\nb")

    def test_long_text_is_truncated_with_total_length(self):
        text = "x" * 10
        out = cli.format_value(text, limit=4)
        self.assertTrue(out.startswith("xxxx…"))
        self.assertIn("共 10 字", out)

    def test_empty_data_still_prints_topic(self):
        self.assertTrue(cli.format_push("music", {}, cli.datetime(2026, 9, 21, 1, 2, 3))
                        .startswith("01:02:03  music"))


class TestConnectHint(unittest.TestCase):
    def test_hint_names_path_cause_and_next_step(self):
        hint = cli.connect_hint("/tmp/x.sock", RuntimeError("文件不存在"))
        self.assertIn("/tmp/x.sock", hint)
        self.assertIn("文件不存在", hint)
        self.assertIn("agent/main.py", hint)


class TestCliWithoutAgent(unittest.TestCase):
    """没有 Agent 时必须是"一句人话 + 退出码 1"，而不是 traceback。"""

    def test_status_without_socket_returns_error(self):
        tmp = tempfile.mkdtemp(prefix="cli-nosock-")
        self.addCleanup(shutil.rmtree, tmp, True)
        code, out, err = _run_cli(["status", "--socket", os.path.join(tmp, "nope.sock"),
                                   "--timeout", "0.2"])
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("连不上 Agent", err)
        self.assertEqual(out.strip(), "")     # 什么都没连上，就别打印"已连上"

    def test_chat_requires_text(self):
        code, out, err = _run_cli(["chat", "   "])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("非空文本", err)

    def test_mode_rejects_unknown_value(self):
        code, out, err = _run_cli(["mode", "banana"])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("SLEEP/IDLE/STUDY/GAME", err)


# ===========================================================================
#  真 socket 端到端（POSIX only）
# ===========================================================================
class _FakeAgent:
    """一个最小的"Agent"：只做 CLI 需要的事，用**真的** LocalServer。"""

    def __init__(self, path):
        self.server = LocalServer(path)
        self.path = path
        self.commands = []          # 收到的 (action, payload)

    async def start(self):
        await self.server.start()

    async def wait_for_client(self):
        for _ in range(200):                       # 最多等 2 秒
            if self.server.client_count >= 1:
                return True
            await asyncio.sleep(0.01)
        return False

    def on_command(self, handler):
        self.server.on_command(handler)

    async def push(self, topic, data):
        return await self.server.push(topic, data)

    async def stop(self):
        await self.server.stop()

    def reply_with(self, action, topic, data_of):
        """收到 `action` 就按 `data_of(payload)` 推一条 `topic`。"""
        async def handler(received, payload):
            self.commands.append((received, payload))
            if received == action:
                await self.push(topic, data_of(payload))
        self.on_command(handler)

    def record_only(self):
        """只记命令、什么都不推（用来验 --no-wait）。"""
        def handler(received, payload):
            self.commands.append((received, payload))
        self.on_command(handler)

    async def wait_for_command(self, count=1):
        for _ in range(200):
            if len(self.commands) >= count:
                return True
            await asyncio.sleep(0.01)
        return False


@unittest.skipUnless(UNIX_SOCKET_SUPPORTED, "需要 AF_UNIX (Windows 的 CPython 不支持)")
class TestCliAgainstRealServer(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cli-status-")
        self.path = os.path.join(self.tmp, "agent.sock")
        self.agent = _FakeAgent(self.path)

    async def asyncSetUp(self):
        await self.agent.start()
        self.addAsyncCleanup(self.agent.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_status_prints_mode_when_agent_pushes(self):
        args = cli.build_parser().parse_args(["status", "--socket", self.path, "--timeout", "2"])

        async def push_soon():
            self.assertTrue(await self.agent.wait_for_client(), "CLI 没连上来")
            await self.agent.push(TOPIC_STATUS, {"mode": "STUDY", "connected": True})

        pusher = asyncio.ensure_future(push_soon())
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = await cli.cmd_status(args)
        await pusher

        self.assertEqual(code, cli.EXIT_OK)
        text = out.getvalue()
        self.assertIn("已连上 Agent", text)
        self.assertIn("模式 STUDY", text)
        self.assertIn("串流已连接", text)

    async def test_status_without_push_says_so(self):
        args = cli.build_parser().parse_args(["status", "--socket", self.path, "--timeout", "0.3"])

        async def connect_but_stay_silent():
            self.assertTrue(await self.agent.wait_for_client(), "CLI 没连上来")
            # 故意不推：模拟"Agent 在跑但状态没变化"

        watcher = asyncio.ensure_future(connect_but_stay_silent())
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = await cli.cmd_status(args)
        await watcher

        self.assertEqual(code, cli.EXIT_OK)          # 连上了就是成功
        text = out.getvalue()
        self.assertIn("已连上 Agent", text)
        self.assertIn("还没推 status", text)
        self.assertNotIn("模式 ", text)               # 不许编一个模式出来

    async def test_status_reports_stream_disconnected(self):
        args = cli.build_parser().parse_args(["status", "--socket", self.path, "--timeout", "2"])

        async def push_disconnected():
            self.assertTrue(await self.agent.wait_for_client())
            await self.agent.push(TOPIC_STATUS, {"mode": "IDLE", "connected": False})

        pusher = asyncio.ensure_future(push_disconnected())
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = await cli.cmd_status(args)
        await pusher

        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("模式 IDLE · 串流未连接", out.getvalue())

    # ---- chat ----
    async def test_chat_prints_reply(self):
        self.agent.reply_with(COMMAND_CHAT_INPUT, TOPIC_LLM,
                              lambda payload: {"text": "回：" + str(payload.get("text"))})
        args = cli.build_parser().parse_args(
            ["chat", "你好", "世界", "--socket", self.path, "--timeout", "2"])

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = await cli.cmd_chat(args)

        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("助手: 回：你好 世界", out.getvalue())      # 多段自动用空格连接
        self.assertTrue(await self.agent.wait_for_command())
        self.assertEqual(self.agent.commands[0],
                         (COMMAND_CHAT_INPUT, {"text": "你好 世界"}))

    async def test_chat_no_wait_returns_without_reply(self):
        self.agent.record_only()
        args = cli.build_parser().parse_args(
            ["chat", "在吗", "--no-wait", "--socket", self.path, "--timeout", "5"])

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = await cli.cmd_chat(args)

        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("已发送：在吗", out.getvalue())
        self.assertNotIn("助手:", out.getvalue())
        self.assertTrue(await self.agent.wait_for_command())
        self.assertEqual(self.agent.commands[0][1], {"text": "在吗"})

    async def test_chat_without_reply_is_an_error(self):
        self.agent.record_only()                 # 收到但不回
        args = cli.build_parser().parse_args(
            ["chat", "喂", "--socket", self.path, "--timeout", "0.3"])

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = await cli.cmd_chat(args)

        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("还没收到 llm 回复", err.getvalue())
        self.assertNotIn("助手:", out.getvalue())

    # ---- mode ----
    async def test_mode_confirms_through_status_push(self):
        self.agent.reply_with(COMMAND_SWITCH_MODE, TOPIC_STATUS,
                              lambda payload: {"mode": payload.get("value"),
                                               "connected": False})
        args = cli.build_parser().parse_args(
            ["mode", "study", "--socket", self.path, "--timeout", "2"])

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = await cli.cmd_mode(args)

        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("已切到 STUDY", out.getvalue())
        # 发出去的一定是协议里的全大写
        self.assertEqual(self.agent.commands[0], (COMMAND_SWITCH_MODE, {"value": "STUDY"}))

    async def test_mode_rejection_is_reported(self):
        # 状态机拒掉非法转换时会把**真实**状态推回来（协议 §4）
        self.agent.reply_with(COMMAND_SWITCH_MODE, TOPIC_STATUS,
                              lambda payload: {"mode": "IDLE", "connected": False})
        args = cli.build_parser().parse_args(
            ["mode", "game", "--socket", self.path, "--timeout", "2"])

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = await cli.cmd_mode(args)

        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("没切过去", err.getvalue())
        self.assertIn("IDLE", err.getvalue())

    # ---- watch ----
    async def _watch(self, extra, pushes):
        """起 watch（在后台任务里跑），推完 pushes 后等它自己按 --count 退出。"""
        args = cli.build_parser().parse_args(["watch", "--socket", self.path] + extra)
        out, err = io.StringIO(), io.StringIO()

        async def run():
            with redirect_stdout(out), redirect_stderr(err):
                return await cli.cmd_watch(args)

        watcher = asyncio.ensure_future(run())
        self.assertTrue(await self.agent.wait_for_client(), "watch 没连上来")
        for topic, data in pushes:
            await self.agent.push(topic, data)
        code = await asyncio.wait_for(watcher, timeout=5)
        return code, out.getvalue(), err.getvalue()

    async def test_watch_prints_pushes_until_count(self):
        code, out, err = await self._watch(
            ["--count", "2"],
            [(TOPIC_STATUS, {"mode": "STUDY", "connected": True}),
             (TOPIC_MUSIC, {"title": "夜曲", "playing": True})],
        )
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("在听", out)
        self.assertIn("mode=STUDY", out)
        self.assertIn("title=夜曲", out)
        self.assertIn("playing=true", out)
        self.assertIn("收到 2 条推送", out)

    async def test_watch_topic_filter(self):
        code, out, err = await self._watch(
            ["--count", "1", "--topics", "music"],
            [(TOPIC_STATUS, {"mode": "STUDY", "connected": True}),   # 应被过滤掉
             (TOPIC_MUSIC, {"title": "夜曲", "playing": False})],
        )
        self.assertEqual(code, cli.EXIT_OK)
        self.assertNotIn("mode=STUDY", out)
        self.assertIn("title=夜曲", out)
        self.assertIn("只看 music", out)

    async def test_watch_escapes_newlines_in_llm_text(self):
        code, out, err = await self._watch(
            ["--count", "1"],
            [(TOPIC_LLM, {"text": "第一行\n第二行"})],
        )
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("第一行\\n第二行", out)      # 单行显示，不破坏表格


if __name__ == "__main__":
    unittest.main(verbosity=2)
