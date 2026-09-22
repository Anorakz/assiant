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

from datetime import date, datetime  # noqa: E402

from agent import cli, config  # noqa: E402
from agent.ipc.local_server import LocalServer, UNIX_SOCKET_SUPPORTED  # noqa: E402
from agent.ipc.protocol import (  # noqa: E402
    COMMAND_CHAT_INPUT,
    COMMAND_QUERY_SCHEDULE,
    COMMAND_SWITCH_MODE,
    SOCKET_PATH,
    TOPIC_LLM,
    TOPIC_MUSIC,
    TOPIC_SCHEDULE,
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
                               datetime(2026, 9, 21, 23, 51, 2))
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
        self.assertTrue(cli.format_push("music", {}, datetime(2026, 9, 21, 1, 2, 3))
                        .startswith("01:02:03  music"))

    # ---- P 系列: schedule 推送要打得像人话, 不能倒 dict ----

    FACT = {"title": "午休", "date": "2026-09-22", "scheduled_at": "2026-09-22T13:00",
            "fired_at": "2026-09-22T13:00:03",
            "actions": [{"type": "message", "text": "日程提醒：午休", "timestamp": 1.0}]}

    def test_schedule_fired_push_is_a_readable_line(self):
        line = cli.format_push(TOPIC_SCHEDULE, {"kind": "fired", "event": self.FACT},
                               datetime(2026, 9, 22, 13, 0, 4))
        self.assertEqual(
            line,
            "13:00:04  schedule  kind=fired title=午休 date=2026-09-22 "
            "scheduled_at=2026-09-22T13:00 fired_at=2026-09-22T13:00:03")
        self.assertNotIn("actions", line, "别把事实里那一坨 actions 原样倒出来")

    def test_schedule_state_push_shows_count_and_limit(self):
        line = cli.format_push(
            TOPIC_SCHEDULE,
            {"kind": "state", "now": "2026-09-22T13:05:00", "limit": 50,
             "fired": [self.FACT, self.FACT]},
            datetime(2026, 9, 22, 13, 5, 1))
        self.assertEqual(line, "13:05:01  schedule  kind=state fired=2 limit=50")

    def test_unknown_schedule_kind_is_shown_verbatim(self):
        """将来加 kind 时: 老 CLI 要把它原样打出来, 而不是装懂或假装没有。"""
        line = cli.format_push(TOPIC_SCHEDULE, {"kind": "later", "x": 1},
                               datetime(2026, 9, 22, 13, 5, 1))
        self.assertIn("kind=later", line)
        self.assertIn("x=1", line)


class TestScheduleRendering(unittest.TestCase):
    """日程渲染用的是**真的** ScheduleEvent（不是替身）。"""

    @staticmethod
    def _events():
        from agent.core.scheduler import ScheduleEvent
        return [
            ScheduleEvent.from_config({"title": "每天喝水", "start": "10:00"}, 0),
            ScheduleEvent.from_config({"title": "周一学习", "days": ["mon"], "start": "09:00"}, 1),
            ScheduleEvent.from_config({"title": "周二站会", "days": ["tue"],
                                       "start": "09:30", "end": "09:45"}, 2),
            ScheduleEvent.from_config({"title": "项目评审", "date": "2026-09-22",
                                       "start": "14:00"}, 3),
        ]

    def test_rows_only_for_that_day_and_sorted(self):
        events = self._events()
        monday = date(2026, 9, 21)
        rows = cli.schedule_rows(events, monday, datetime(2026, 9, 21, 8, 0))
        # 周一：每天喝水 + 周一学习；项目评审与周二站会不该出现
        self.assertEqual([row["title"] for row in rows], ["周一学习", "每天喝水"])

    def test_row_text_matches_the_gui_format(self):
        """⚠ 与 GUI 的行格式一致，才能"CLI 的日程"和"界面上的日程"直接 diff。"""
        events = self._events()
        tuesday = date(2026, 9, 22)
        rows = cli.schedule_rows(events, tuesday, datetime(2026, 9, 22, 8, 0))
        texts = [cli.row_text(row) for row in rows]
        self.assertEqual(texts, ["09:30-09:45  周二站会", "10:00  每天喝水",
                                 "14:00  项目评审"])

    def test_past_is_a_plain_time_comparison(self):
        events = self._events()
        day = date(2026, 9, 21)
        rows = cli.schedule_rows(events, day, datetime(2026, 9, 21, 9, 30))
        past = {row["title"]: row["past"] for row in rows}
        self.assertTrue(past["周一学习"])       # 09:00 已过
        self.assertFalse(past["每天喝水"])      # 10:00 还没到

    def test_tomorrow_is_never_marked_past(self):
        """C4 起真 Agent 时抓到的 bug：明天 08:30 曾被标成「已过」。

        `now > start` 对明天恒成立，所以 `past` 必须**只在今天**成立。
        """
        events = self._events()
        tuesday = date(2026, 9, 22)
        rows = cli.schedule_rows(events, tuesday, datetime(2026, 9, 21, 12, 58))
        self.assertTrue(rows, "周二应当有日程")
        self.assertTrue(all(not row["past"] for row in rows),
                        "明天的行永远不该标「已过」：%r" % (rows,))

    def test_watch_rejects_a_timeout_option(self):
        """watch 一直盯到 Ctrl-C / --count，所以它**不接受** --timeout（C4 顺手改的）。

        ⚠ 不能用 `hasattr(args, "timeout")` 判断：主解析器那份默认值仍会出现在
          namespace 里；"挂没挂这个选项"要看的是**给 watch 传它会不会被拒**。
        """
        with self.assertRaises(SystemExit) as ctx:
            cli.build_parser().parse_args(["watch", "--timeout", "5"])
        self.assertEqual(ctx.exception.code, cli.EXIT_USAGE)

    def test_render_marks_empty_day_and_truncates(self):
        events = self._events()
        now = datetime(2026, 9, 21, 7, 0)
        lines = cli.render_schedule(
            [("今天", date(2026, 9, 21)), ("明天", date(2026, 9, 22))],
            lambda day: cli.schedule_rows(events, day, now),
            now, limit=1)
        text = "\n".join(lines)
        self.assertIn("今天（2026-09-21 周一）", text)
        self.assertIn("还有 1 项", text)         # limit=1 截掉了第二条
        self.assertIn("明天（2026-09-22 周二）", text)

    def test_render_says_so_when_a_day_is_empty(self):
        from agent.core.scheduler import ScheduleEvent
        # 只放"周一才有"的一条，这样周三那天真的是空的
        # （用 self._events() 就不行：里面那条"每天喝水"每天都有）
        events = [ScheduleEvent.from_config(
            {"title": "只在周一", "days": ["mon"], "start": "09:00"}, 0)]
        now = datetime(2026, 9, 23, 7, 0)                  # 周三
        lines = cli.render_schedule([("今天", date(2026, 9, 23))],
                                    lambda day: cli.schedule_rows(events, day, now), now, 10)
        self.assertIn("（没有日程）", "\n".join(lines))

    def test_footer_does_not_claim_the_agent_fired_anything(self):
        """文案防线：不许让 CLI 的"已过"被读成"Agent 已经触发过"。"""
        self.assertIn("按时间", cli.SCHEDULE_FOOTER)
        self.assertIn("不代表", cli.SCHEDULE_FOOTER)

    # ---- P 系列: 真的触发过的那几条要标出来 ----

    TODAY = date(2026, 9, 22)

    @staticmethod
    def _fact(title, scheduled_at, fired_at):
        return {"title": title, "date": scheduled_at[:10], "scheduled_at": scheduled_at,
                "fired_at": fired_at, "actions": [{"type": "message", "text": "x",
                                                   "timestamp": 1.0}]}

    def test_fired_row_is_marked_with_the_real_time(self):
        events = self._events()
        now = datetime(2026, 9, 22, 15, 0)          # 14:00 那条已经过了
        facts = [self._fact("项目评审", "2026-09-22T14:00", "2026-09-22T14:00:02")]
        rows = cli.schedule_rows(events, self.TODAY, now, facts=facts)
        by_title = {row["title"]: row for row in rows}
        self.assertEqual(by_title["项目评审"]["fired_at"], "2026-09-22T14:00:02")

        lines = cli.render_schedule([("今天", self.TODAY)],
                                    lambda day: rows, now, limit=10, asked=True)
        text = "\n".join(lines)
        self.assertIn("14:00  项目评审  ← 已触发 14:00:02", text)
        self.assertNotIn("项目评审  ← 已过", text)

    def test_past_row_says_unfired_only_when_we_could_ask(self):
        """问到了才能说"未触发"；没问到时只说"已过" —— 别把"没问"说成"没触发"。"""
        events = self._events()
        now = datetime(2026, 9, 22, 15, 0)
        rows = cli.schedule_rows(events, self.TODAY, now, facts=[])

        asked = "\n".join(cli.render_schedule([("今天", self.TODAY)], lambda day: rows,
                                             now, limit=10, asked=True))
        self.assertIn("← 已过（未触发）", asked)

        not_asked = "\n".join(cli.render_schedule([("今天", self.TODAY)], lambda day: rows,
                                                  now, limit=10, asked=False))
        self.assertIn("← 已过", not_asked)
        self.assertNotIn("未触发", not_asked)

    def test_future_row_is_not_marked(self):
        events = self._events()
        now = datetime(2026, 9, 22, 8, 0)
        rows = cli.schedule_rows(events, self.TODAY, now, facts=[])
        text = "\n".join(cli.render_schedule([("今天", self.TODAY)], lambda day: rows,
                                             now, limit=10, asked=True))
        self.assertIn("14:00  项目评审", text)
        self.assertNotIn("项目评审  ←", text)

    def test_facts_do_not_change_the_row_text(self):
        """⚠ `row_text` 是"与 GUI 逐行 diff"的接口: 加了事实也不能动它。"""
        events = self._events()
        now = datetime(2026, 9, 22, 15, 0)
        facts = [self._fact("项目评审", "2026-09-22T14:00", "2026-09-22T14:00:02"),
                 self._fact("每天喝水", "2026-09-22T10:00", "2026-09-22T10:00:01")]
        plain = [cli.row_text(r) for r in cli.schedule_rows(events, self.TODAY, now)]
        with_facts = [cli.row_text(r) for r in cli.schedule_rows(events, self.TODAY, now,
                                                                facts=facts)]
        self.assertEqual(plain, with_facts)

    def test_remind_before_fact_lands_on_the_event_day(self):
        """提前量把触发点推到**前一天**时, 事实仍要落到事件日那一行上。

        这就是"按 trigger_at 对齐、而不是按 start 对齐"的原因: 只按 start 找,
        跨天那条永远匹配不上（它的 scheduled_at 是前一天 23:55）。
        """
        from agent.core.scheduler import ScheduleEvent
        events = [ScheduleEvent.from_config(
            {"title": "深夜小结", "start": "00:05", "remind_before_min": 10}, 0)]
        day = date(2026, 9, 22)
        facts = [self._fact("深夜小结", "2026-09-21T23:55", "2026-09-21T23:55:00")]

        rows = cli.schedule_rows(events, day, datetime(2026, 9, 22, 1, 0), facts=facts)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["trigger_at"], "2026-09-21T23:55")
        self.assertEqual(rows[0]["fired_at"], "2026-09-21T23:55:00")

    def test_a_fact_does_not_leak_to_another_day(self):
        events = self._events()
        facts = [self._fact("每天喝水", "2026-09-21T10:00", "2026-09-21T10:00:01")]
        rows = cli.schedule_rows(events, self.TODAY, datetime(2026, 9, 22, 15, 0),
                                 facts=facts)
        self.assertEqual([r["fired_at"] for r in rows], [None, None, None],
                         "昨天触发的那条不能标到今天头上")

    def test_fired_clock_strips_the_date(self):
        self.assertEqual(cli.fired_clock("2026-09-22T13:00:03"), "13:00:03")
        self.assertEqual(cli.fired_clock("看不懂"), "看不懂")   # 不装懂
        self.assertEqual(cli.fired_clock(None), "")

    def test_asked_with_no_facts_is_not_a_claim(self):
        """Agent 说"一条都没触发过"时, 绝不能有任何一行显示「已触发」。"""
        events = self._events()
        now = datetime(2026, 9, 22, 23, 0)
        rows = cli.schedule_rows(events, self.TODAY, now, facts=[])
        text = "\n".join(cli.render_schedule([("今天", self.TODAY)], lambda day: rows,
                                             now, limit=10, asked=True))
        self.assertNotIn("已触发", text)
        self.assertIn("已过（未触发）", text)


class TestDerivationVerdict(unittest.TestCase):
    def test_no_diff_is_ok(self):
        status, detail = cli.derivation_verdict(0, "[sync] dry-run（未写文件）\n[sync] 无差异\n")
        self.assertEqual(status, cli.OK)
        self.assertIn("一致", detail)

    def test_diff_is_a_warning(self):
        status, detail = cli.derivation_verdict(0, "[sync] dry-run\n--- a\n+++ b\n-LLM_PORT=1\n")
        self.assertEqual(status, cli.WARN)
        self.assertIn("有差异", detail)

    def test_nonzero_exit_is_a_warning_with_the_code(self):
        status, detail = cli.derivation_verdict(2, "[sync] 文件不存在: /tmp/x.env")
        self.assertEqual(status, cli.WARN)
        self.assertIn("退出码 2", detail)


class TestDerivationCheck(unittest.TestCase):
    def test_missing_binary_is_a_warning_not_a_crash(self):
        tmp = tempfile.mkdtemp(prefix="cli-doc-")
        self.addCleanup(shutil.rmtree, tmp, True)
        status, detail = cli.run_derivation_check(
            os.path.join(tmp, "nope"), os.path.join(tmp, "config.yaml"),
            os.path.join(tmp, "llm.env"))
        self.assertEqual(status, cli.WARN)
        self.assertIn("没找到", detail)

    @unittest.skipUnless(UNIX_SOCKET_SUPPORTED, "需要可执行脚本（POSIX）")
    def test_runs_the_real_binary_and_reads_its_verdict(self):
        tmp = tempfile.mkdtemp(prefix="cli-doc-run-")
        self.addCleanup(shutil.rmtree, tmp, True)
        binary = os.path.join(tmp, "gui_config_sync")
        with open(binary, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\necho '[sync] dry-run（未写文件）'\necho '[sync] 无差异'\n")
        os.chmod(binary, 0o755)
        config_file = os.path.join(tmp, "config.yaml")
        env_file = os.path.join(tmp, "llm.env")
        for path in (config_file, env_file):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("x\n")

        status, detail = cli.run_derivation_check(binary, config_file, env_file)
        self.assertEqual(status, cli.OK)
        self.assertIn("一致", detail)


class TestScheduleCommand(unittest.TestCase):
    """不需要 socket：`schedule` 只读配置 + 用真的 Scheduler 展开。"""

    def _write_config(self, body):
        tmp = tempfile.mkdtemp(prefix="cli-sched-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = Path(tmp) / "config.yaml"
        path.write_text(body, encoding="utf-8")
        return path

    def _run(self, path, extra=()):
        # ⚠ 两件事：
        #   1) AGENT_CONFIG_DIR 是"按名字加载"用的；一个进程里只认第一次 setdefault，
        #      所以测试里直接覆盖环境变量，跑完还原。
        #   2) agent.config.load_config **有缓存**（生产上是对的：一个进程读一次）。
        #      测试里必须 clear_cache()，否则第二个用例读到的还是第一份配置。
        old = os.environ.get("AGENT_CONFIG_DIR")
        os.environ["AGENT_CONFIG_DIR"] = str(path.parent)
        config.clear_cache()

        def restore():
            if old is None:
                os.environ.pop("AGENT_CONFIG_DIR", None)
            else:
                os.environ["AGENT_CONFIG_DIR"] = old
            config.clear_cache()

        self.addCleanup(restore)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["schedule", "--config", str(path)] + list(extra))
        return code, out.getvalue(), err.getvalue()

    def test_lists_today_and_tomorrow(self):
        path = self._write_config(
            "scheduler:\n"
            "  recurring:\n"
            "    - title: 每天喝水\n"
            "      start: \"10:00\"\n"
            "    - title: 只在周一\n"
            "      days: [mon]\n"
            "      start: \"09:00\"\n")
        code, out, err = self._run(path)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("日程（共 2 条", out)
        self.assertIn("每天喝水", out)
        self.assertIn("今天（", out)
        self.assertIn("明天（", out)
        self.assertIn("不代表", out)          # 页脚必须带上"不代表已触发"

    def test_empty_schedule_is_not_an_error(self):
        path = self._write_config("llm:\n  mode: disabled\n")
        code, out, err = self._run(path)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("共 0 条", out)
        self.assertIn("（没有日程）", out)

    def test_broken_schedule_is_reported_honestly(self):
        path = self._write_config("scheduler:\n  recurring:\n    - title: 坏的\n      start: \"99:99\"\n")
        code, out, err = self._run(path)
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("读不出来", err)
        self.assertIn("Agent 也会因此起不来", err)

    def test_today_only_flag(self):
        path = self._write_config("scheduler:\n  recurring:\n    - title: 每天\n      start: \"10:00\"\n")
        code, out, err = self._run(path, ["--today"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("今天（", out)
        self.assertNotIn("明天（", out)

    def test_unreadable_config_is_reported_with_the_naming_hint(self):
        tmp = tempfile.mkdtemp(prefix="cli-sched-bad-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = Path(tmp) / "my-config.yaml"          # 名字不对
        path.write_text("llm: {mode: disabled}\n", encoding="utf-8")
        code, out, err = self._run(path)
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("读不到配置", err)
        self.assertIn("config.yaml", err)             # 提示名字要求

    # ---- P 系列: 问 Agent 这条路 (问不到要如实说) ----

    def test_no_ask_skips_the_agent_and_says_so(self):
        path = self._write_config(
            "scheduler:\n  recurring:\n    - title: 每天喝水\n      start: \"10:00\"\n")
        code, out, err = self._run(path, ["--no-ask"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("不代表", out)                   # 还是那句按时间比较
        self.assertIn("--no-ask", out, "要讲明是**你**让它别问的")
        self.assertNotIn("问不到 Agent", err, "--no-ask 时不该去连 Agent")

    def test_ask_failure_falls_back_and_says_why(self):
        """Agent 没跑: 日程照样列出来, 但"已过"必须说明是按时间算的。"""
        tmp = tempfile.mkdtemp(prefix="cli-nosock-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = self._write_config(
            "scheduler:\n  recurring:\n    - title: 每天喝水\n      start: \"10:00\"\n")
        missing = os.path.join(tmp, "nope.sock")
        code, out, err = self._run(path, ["--socket", missing, "--timeout", "0.2"])

        self.assertEqual(code, cli.EXIT_OK, "问不到触发记录不是失败")
        self.assertIn("问不到 Agent 的触发记录", err)
        self.assertIn("连不上 Agent", err)
        self.assertIn("每天喝水", out)
        self.assertIn("不代表", out)
        self.assertNotIn("已触发", out, "没问到就一条都不能标")
        self.assertNotIn("未触发", out, "没问到也不许替 Agent 说「未触发」")

    def test_ask_with_no_answer_is_not_reported_as_a_connection_error(self):
        """连上了但没回: 文案要说"没回那一条", 不能糊成"连不上"。

        ⚠ "真连上但不回"需要真 socket（下面 TestCliAgainstRealServer 里那条）；
          这里只钉住"路径不存在 -> 说的是连不上", 两种原因别混成一句。
        """
        tmp = tempfile.mkdtemp(prefix="cli-nosock2-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = self._write_config(
            "scheduler:\n  recurring:\n    - title: 每天喝水\n      start: \"10:00\"\n")
        code, out, err = self._run(path, ["--socket", os.path.join(tmp, "x.sock"),
                                          "--timeout", "0.2"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("连不上 Agent", err)
        self.assertNotIn("没回", err)


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

    async def test_watch_shows_a_schedule_fired_push_as_one_readable_line(self):
        fact = {"title": "午休", "date": "2026-09-22", "scheduled_at": "2026-09-22T13:00",
                "fired_at": "2026-09-22T13:00:03",
                "actions": [{"type": "message", "text": "日程提醒：午休",
                             "timestamp": 1.0}]}
        code, out, err = await self._watch(
            ["--count", "1"],
            [(TOPIC_SCHEDULE, {"kind": "fired", "event": fact})],
        )
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("kind=fired", out)
        self.assertIn("title=午休", out)
        self.assertIn("fired_at=2026-09-22T13:00:03", out)
        self.assertNotIn("actions", out, "别把嵌套的那坨原样倒出来")

    # ---- P 系列: schedule 问 Agent 要触发记录 ----

    def _write_config(self, body):
        tmp = tempfile.mkdtemp(prefix="cli-live-sched-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = Path(tmp) / "config.yaml"
        path.write_text(body, encoding="utf-8")
        return path

    async def test_ask_fired_returns_the_facts(self):
        fact = {"title": "站会", "date": "2026-09-22", "scheduled_at": "2026-09-22T09:30",
                "fired_at": "2026-09-22T09:30:04", "actions": []}
        self.agent.reply_with(COMMAND_QUERY_SCHEDULE, TOPIC_SCHEDULE,
                              lambda _payload: {"kind": "state", "now": "2026-09-22T09:31:00",
                                                "limit": 50, "fired": [fact]})

        facts, why = await cli.ask_fired(self.path, 2)

        self.assertEqual(why, "")
        self.assertEqual(facts, [fact])
        self.assertEqual(self.agent.commands, [(COMMAND_QUERY_SCHEDULE, {})],
                         "查询命令的 payload 必须是 {}（协议 §4）")

    async def test_ask_fired_times_out_when_the_agent_stays_silent(self):
        """连上了但不回: 原因要落在"没回快照"上, 不能糊成"连不上"。"""
        self.agent.record_only()
        facts, why = await cli.ask_fired(self.path, 0.2)
        self.assertIsNone(facts)
        self.assertIn("没回日程快照", why)
        self.assertNotIn("连不上", why)

    async def test_schedule_marks_fired_through_a_real_agent(self):
        """端到端: 真 server 应答 -> cmd_schedule 打出「已触发 HH:MM:SS」。"""
        today = date.today()
        fact = {"title": "站会", "date": today.isoformat(),
                "scheduled_at": "%sT09:30" % today.isoformat(),
                "fired_at": "%sT09:30:04" % today.isoformat(), "actions": []}
        self.agent.reply_with(COMMAND_QUERY_SCHEDULE, TOPIC_SCHEDULE,
                              lambda _payload: {"kind": "state", "now": "x", "limit": 50,
                                                "fired": [fact]})
        path = self._write_config(
            "ipc:\n  socket_path: %s\n" % self.path +
            "scheduler:\n  recurring:\n    - title: 站会\n      start: \"09:30\"\n")

        old = os.environ.get("AGENT_CONFIG_DIR")
        os.environ["AGENT_CONFIG_DIR"] = str(path.parent)
        config.clear_cache()

        def restore():
            if old is None:
                os.environ.pop("AGENT_CONFIG_DIR", None)
            else:
                os.environ["AGENT_CONFIG_DIR"] = old
            config.clear_cache()

        self.addCleanup(restore)

        args = cli.build_parser().parse_args(
            ["schedule", "--config", str(path), "--today", "--timeout", "2"])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = await cli.cmd_schedule(args)

        self.assertEqual(code, cli.EXIT_OK)
        text = out.getvalue()
        self.assertIn("09:30  站会  ← 已触发 09:30:04", text)
        self.assertIn("来自运行中的 Agent", text, "页脚要说明这份记录的出处")
        self.assertNotIn("不代表", text, "问到了就不该再用「按时间」那套免责话术")
        self.assertEqual(err.getvalue(), "", "问到了就不该有「问不到」的噪音")

    async def test_schedule_without_the_agent_still_lists_but_says_why(self):
        """同一个真 server, 但**不**应答查询: 列表照出, 页脚退回"按时间"。"""
        self.agent.record_only()
        today = date.today()
        path = self._write_config(
            "ipc:\n  socket_path: %s\n" % self.path +
            "scheduler:\n  recurring:\n    - title: 站会\n      start: \"09:30\"\n")

        old = os.environ.get("AGENT_CONFIG_DIR")
        os.environ["AGENT_CONFIG_DIR"] = str(path.parent)
        config.clear_cache()

        def restore():
            if old is None:
                os.environ.pop("AGENT_CONFIG_DIR", None)
            else:
                os.environ["AGENT_CONFIG_DIR"] = old
            config.clear_cache()

        self.addCleanup(restore)

        args = cli.build_parser().parse_args(
            ["schedule", "--config", str(path), "--today", "--timeout", "0.2"])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = await cli.cmd_schedule(args)

        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("站会", out.getvalue())
        self.assertIn("不代表", out.getvalue())
        self.assertIn("没回日程快照", err.getvalue())
        self.assertIn(today.isoformat(), out.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
