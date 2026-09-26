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

import argparse
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

from datetime import date, datetime, timedelta  # noqa: E402

from agent import cli, config  # noqa: E402
from agent.ipc.local_server import LocalServer, UNIX_SOCKET_SUPPORTED  # noqa: E402
from agent.ipc.protocol import (  # noqa: E402
    COMMAND_CHAT_INPUT,
    COMMAND_QUERY_SCHEDULE,
    COMMAND_SWITCH_MODE,
    SOCKET_PATH,
    TOPIC_BILIBILI,
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


def _just_passed(minutes=5, now=None):
    """造一个"**今天**刚刚过去"的时刻（给"最近 30 分钟尾巴"那几条用例）。

    ⚠ 跨午夜的坑（实测踩到，2026-09-23 00:00 跑的）：`now - 5min` 在午夜刚过时落到
      **昨天** 23:55，而配置里写的是"今天的 23:55" —— 那是**将来**，于是"已过"的断言
      假红（跟被测代码无关，是这条用例自己的时间算术）。这时改用 00:00：此刻最晚
      00:04，它一定还在 30 分钟的尾巴里。
    """
    now = now or datetime.now()
    candidate = now - timedelta(minutes=minutes)
    if candidate.date() != now.date():
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    return candidate


def _run_cli_with_config(argv, config_path):
    """跑一次 CLI，并把 `AGENT_CONFIG_DIR` **显式**指向 `config_path` 的父目录。

    ⚠ `load_plane_config()` 用的是 `os.environ.setdefault("AGENT_CONFIG_DIR", …)` ——
       **一个进程里只认第一次**。测试里不显式覆盖的话，后一个用例会读到前一个的目录
      （临时目录被删掉后还会变成"配置不存在"），那种假绿/假红最难查。
    """
    old = os.environ.get("AGENT_CONFIG_DIR")
    os.environ["AGENT_CONFIG_DIR"] = str(Path(config_path).resolve().parent)
    config.clear_cache()
    try:
        return _run_cli(argv)
    finally:
        if old is None:
            os.environ.pop("AGENT_CONFIG_DIR", None)
        else:
            os.environ["AGENT_CONFIG_DIR"] = old
        config.clear_cache()


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


class TestModePathNote(unittest.TestCase):
    """T12-2: 跨模式切换会推**两条** status —— "路上经过 …" 这半句（纯逻辑）。

    ⚠ 以前 CLI 只看**第一条**, 于是"从 GAME 点睡眠"（GAME->IDLE->SLEEP）会打印
      "没切过去（当前 IDLE）" —— 明明是成功的。现在等到目标模式为止并把中间状态说出来。
    """

    def test_no_intermediate_state_means_no_note(self):
        self.assertEqual(cli.mode_path_note([]), "")
        self.assertEqual(cli.mode_path_note(["SLEEP"]), "")
        self.assertEqual(cli.mode_path_note(["STUDY"]), "")

    def test_one_hop_through_idle(self):
        self.assertEqual(cli.mode_path_note(["IDLE", "SLEEP"]), "（路上经过 IDLE）")
        self.assertEqual(cli.mode_path_note(["IDLE", "GAME"]), "（路上经过 IDLE）")

    def test_several_hops_are_listed_in_order(self):
        self.assertEqual(cli.mode_path_note(["IDLE", "STUDY", "SLEEP"]),
                         "（路上经过 IDLE → STUDY）")


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

    FACT = {"state": "sleep", "date": "2026-09-22", "scheduled_at": "2026-09-22T13:00",
            "fired_at": "2026-09-22T13:00:03",
            "actions": [{"type": "state", "state": "sleep", "ok": True,
                         "steps": [{"from": "idle", "to": "sleep"}], "current": "sleep"}]}

    def test_schedule_fired_push_is_a_readable_line(self):
        line = cli.format_push(TOPIC_SCHEDULE, {"kind": "fired", "event": self.FACT},
                               datetime(2026, 9, 22, 13, 0, 4))
        self.assertEqual(
            line,
            "13:00:04  schedule  kind=fired state=sleep date=2026-09-22 "
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
            ScheduleEvent.from_config({"state": "sleep", "start": "10:00"}, 0),
            ScheduleEvent.from_config({"state": "study", "days": ["mon"], "start": "09:00"}, 1),
            ScheduleEvent.from_config({"state": "study", "days": ["tue"],
                                       "start": "09:30", "end": "09:45"}, 2),
            ScheduleEvent.from_config({"state": "game", "date": "2026-09-22",
                                       "start": "14:00"}, 3),
        ]

    def test_rows_only_for_that_day_and_sorted(self):
        events = self._events()
        monday = date(2026, 9, 21)
        rows = cli.schedule_rows(events, monday, datetime(2026, 9, 21, 8, 0))
        # 周一：每天 10:00 -> SLEEP + 周一 09:00 -> STUDY；oneoff（09-22）与周二的
        # 那条不该出现。（第 3 条里那个 `end` 是老字段，T12-4 起被忽略 —— 这里
        # 顺手留着，正说明"写了也不生效"。）
        self.assertEqual([row["state"] for row in rows], ["STUDY", "SLEEP"])

    def test_row_text_matches_the_gui_format(self):
        """⚠ 与 GUI 的行格式一致，才能"CLI 的日程"和"界面上的日程"直接 diff。"""
        events = self._events()
        tuesday = date(2026, 9, 22)
        rows = cli.schedule_rows(events, tuesday, datetime(2026, 9, 22, 8, 0))
        texts = [cli.row_text(row) for row in rows]
        self.assertEqual(texts, ["09:30  STUDY", "10:00  SLEEP",
                                 "14:00  GAME"])

    def test_past_is_a_plain_time_comparison(self):
        events = self._events()
        day = date(2026, 9, 21)
        rows = cli.schedule_rows(events, day, datetime(2026, 9, 21, 9, 30))
        past = {row["state"]: row["past"] for row in rows}
        self.assertTrue(past["STUDY"])       # 09:00 已过
        self.assertFalse(past["SLEEP"])      # 10:00 还没到

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
            {"state": "study", "days": ["mon"], "start": "09:00"}, 0)]
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
        facts = [self._fact("game", "2026-09-22T14:00", "2026-09-22T14:00:02")]
        rows = cli.schedule_rows(events, self.TODAY, now, facts=facts)
        by_state = {row["state"]: row for row in rows}
        self.assertEqual(by_state["GAME"]["fired_at"], "2026-09-22T14:00:02")

        lines = cli.render_schedule([("今天", self.TODAY)],
                                    lambda day: rows, now, limit=10, asked=True)
        text = "\n".join(lines)
        self.assertIn("14:00  GAME  ← 已触发 14:00:02", text)
        self.assertNotIn("GAME  ← 已过", text)

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
        self.assertIn("14:00  GAME", text)
        self.assertNotIn("GAME  ←", text)

    def test_facts_do_not_change_the_row_text(self):
        """⚠ `row_text` 是"与 GUI 逐行 diff"的接口: 加了事实也不能动它。"""
        events = self._events()
        now = datetime(2026, 9, 22, 15, 0)
        facts = [self._fact("game", "2026-09-22T14:00", "2026-09-22T14:00:02"),
                 self._fact("每天喝水", "2026-09-22T10:00", "2026-09-22T10:00:01")]
        plain = [cli.row_text(r) for r in cli.schedule_rows(events, self.TODAY, now)]
        with_facts = [cli.row_text(r) for r in cli.schedule_rows(events, self.TODAY, now,
                                                                facts=facts)]
        self.assertEqual(plain, with_facts)

    def test_a_fact_lands_on_the_row_with_the_same_scheduled_at(self):
        """事实按 `scheduled_at`（= `trigger_at`）对齐 —— 同一天不同时刻不会串行。

        @note T12-4 起没有提前量了，所以不再有"跨天对齐"那种情况；这条钉的是
              "对齐用的是 trigger_at 那一列，不是按顺序硬塞"。
        """
        from agent.core.scheduler import ScheduleEvent
        events = [ScheduleEvent.from_config({"state": "sleep", "start": "10:00"}, 0),
                  ScheduleEvent.from_config({"state": "study", "start": "14:00"}, 1)]
        day = date(2026, 9, 22)
        facts = [self._fact("study", "2026-09-22T14:00", "2026-09-22T14:00:03")]

        rows = cli.schedule_rows(events, day, datetime(2026, 9, 22, 15, 0), facts=facts)
        by_state = {row["state"]: row for row in rows}
        self.assertIsNone(by_state["SLEEP"]["fired_at"], "10:00 那条没触发过")
        self.assertEqual(by_state["STUDY"]["fired_at"], "2026-09-22T14:00:03")
        self.assertEqual(by_state["STUDY"]["trigger_at"], "2026-09-22T14:00")

    def test_a_fact_does_not_leak_to_another_day(self):
        events = self._events()
        facts = [self._fact("sleep", "2026-09-21T10:00", "2026-09-21T10:00:01")]
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


class TestScheduleWindow(unittest.TestCase):
    """R1: 窗口语义（真的 `ScheduleEvent` + **注入 now**，不依赖墙钟）。

    窗口 = `[now - 30 分钟, now + N 小时)`，判据是行的 `start`。
    """

    NOW = datetime(2026, 9, 22, 18, 26, 0)

    @staticmethod
    def _events(*specs):
        from agent.core.scheduler import ScheduleEvent
        return [ScheduleEvent.from_config(spec, i) for i, spec in enumerate(specs)]

    def _titles(self, events, hours=24, facts=None):
        return [row["state"] for _, rows in cli.window_days(events, self.NOW, hours, facts=facts)
                for row in rows]

    def test_window_range_includes_the_tail(self):
        begin, end = cli.window_range(self.NOW, 24)
        self.assertEqual(begin, datetime(2026, 9, 22, 17, 56))
        self.assertEqual(end, datetime(2026, 9, 23, 18, 26))

    def test_window_range_truncates_to_the_minute(self):
        """行的时刻只有分钟 —— 两侧（CLI / GUI 的 applyWindow）都在分钟粒度上比。"""
        begin, end = cli.window_range(datetime(2026, 9, 22, 18, 26, 40), 24)
        self.assertEqual(begin, datetime(2026, 9, 22, 17, 56))
        self.assertEqual(end, datetime(2026, 9, 23, 18, 26))

    def test_a_daily_event_only_shows_its_next_occurrence(self):
        """每天 08:30 的条目在 18:26 看：**今天早上那条在窗口外，明天早上那条在**。

        这就是窗口与"今天/明天整天"的根本区别 —— 不是"今天的不显示"，而是
        "只显示接下来 24 小时里那一次"。
        """
        events = self._events({"state": "study", "start": "08:30"})
        days = cli.window_days(events, self.NOW, 24)
        self.assertEqual([(d.isoformat(), [r["state"] for r in rs]) for d, rs in days],
                         [("2026-09-23", ["STUDY"])])

    def test_tail_keeps_what_just_passed(self):
        events = self._events({"state": "game", "date": "2026-09-22", "start": "18:10"})
        days = cli.window_days(events, self.NOW, 24)
        self.assertEqual([(d.isoformat(), [r["state"] for r in rs]) for d, rs in days],
                         [("2026-09-22", ["GAME"])])

    def test_before_the_tail_is_dropped(self):
        events = self._events({"state": "sleep", "date": "2026-09-22", "start": "17:50"})
        self.assertEqual(self._titles(events), [], "尾巴从 17:56 起, 17:50 已经在外面")

    def test_start_decides_visibility_not_trigger_at(self):
        """`remind_before_min` 把触发点推到 start 之前 —— 仍按 **start** 判可见性。

        trigger_at = 18:20（已经过去了），但 start = 18:50 是"马上要来"：用 trigger_at
        判定会让它从列表里消失，那是错的。
        """
        events = self._events({"state": "study", "date": "2026-09-22", "start": "18:50",
                               "remind_before_min": 30})
        self.assertEqual(self._titles(events), ["STUDY"])

    def test_hours_reach_further_and_the_heading_switches_to_a_date(self):
        events = self._events({"state": "study", "date": "2026-09-24", "start": "10:00"})
        self.assertEqual(self._titles(events, hours=24), [],
                         "24 小时只到明天 18:26，后天的 10:00 在外")
        self.assertEqual(self._titles(events, hours=48), ["STUDY"])

    def test_day_headings(self):
        today = date(2026, 9, 22)
        self.assertEqual(cli.day_heading(today, today), "今天（2026-09-22 周二）")
        self.assertEqual(cli.day_heading(today + timedelta(days=1), today),
                         "明天（2026-09-23 周三）")
        self.assertEqual(cli.day_heading(today + timedelta(days=2), today),
                         "后天（2026-09-24 周四）")
        self.assertEqual(cli.day_heading(today + timedelta(days=5), today),
                         "2026-09-27（周日）")

    def test_window_end_text(self):
        self.assertEqual(cli.window_end_text(datetime(2026, 9, 22, 19, 0), self.NOW),
                         "今天 19:00")
        self.assertEqual(cli.window_end_text(datetime(2026, 9, 23, 18, 26), self.NOW),
                         "明天 18:26")
        self.assertEqual(cli.window_end_text(datetime(2026, 9, 26, 18, 26), self.NOW),
                         "09-26 18:26")

    def test_limit_is_for_the_whole_window_not_per_day(self):
        events = self._events({"state": "game", "start": "19:00"},
                              {"state": "sleep", "start": "08:00"})
        text = "\n".join(cli.render_window(events, self.NOW, 24, limit=1, asked=False))
        self.assertIn("GAME", text)
        self.assertNotIn("SLEEP", text)
        self.assertIn("还有 1 项", text)

    def test_empty_window_says_so(self):
        self.assertEqual(cli.render_window([], self.NOW, 24, limit=10, asked=False),
                         ["（窗口内没有日程）"])

    def test_fired_fact_without_a_config_row_becomes_a_row(self):
        """R3 之后一次性日程会被从配置里删掉 —— 尾巴必须靠**事实**把它画出来。"""
        fact = {"state": "game", "date": "2026-09-22",
                "scheduled_at": "2026-09-22T18:10", "fired_at": "2026-09-22T18:10:05",
                "actions": []}
        self.assertEqual(cli.render_window([], self.NOW, 24, facts=[fact], limit=10,
                                           asked=True),
                         ["今天（2026-09-22 周二）",
                          "  18:10  GAME  ← 已触发 18:10:05"])

    def test_a_fact_still_in_the_config_is_not_duplicated(self):
        events = self._events({"state": "study", "date": "2026-09-22", "start": "18:10"})
        fact = {"state": "study", "date": "2026-09-22", "scheduled_at": "2026-09-22T18:10",
                "fired_at": "2026-09-22T18:10:02", "actions": []}
        lines = cli.render_window(events, self.NOW, 24, facts=[fact], limit=10, asked=True)
        self.assertEqual([line for line in lines if "STUDY" in line],
                         ["  18:10  STUDY  ← 已触发 18:10:02"])

    def test_a_fact_outside_the_window_is_dropped(self):
        fact = {"state": "game", "date": "2026-09-22",
                "scheduled_at": "2026-09-22T08:30", "fired_at": "2026-09-22T08:30:02",
                "actions": []}
        self.assertEqual(cli.render_window([], self.NOW, 24, facts=[fact], limit=10,
                                           asked=True),
                         ["（窗口内没有日程）"])

    def test_a_fact_with_an_unreadable_time_is_skipped(self):
        fact = {"state": "study", "scheduled_at": "看不懂", "fired_at": "x"}
        self.assertEqual(cli.window_days([], self.NOW, 24, facts=[fact]), [])

    def test_row_mark_three_states(self):
        self.assertEqual(cli.row_mark({"past": True, "fired_at": None}, False), "  ← 已过")
        self.assertEqual(cli.row_mark({"past": True, "fired_at": None}, True),
                         "  ← 已过（未触发）")
        self.assertEqual(cli.row_mark({"past": False, "fired_at": None}, True), "")
        self.assertEqual(cli.row_mark({"past": False, "fired_at": "2026-09-22T18:10:05"},
                                      True),
                         "  ← 已触发 18:10:05")

    def test_positive_hours(self):
        self.assertEqual(cli.positive_hours("12"), 12.0)
        self.assertEqual(cli.positive_hours("0.5"), 0.5)
        for bad in ("0", "-1", "abc", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(argparse.ArgumentTypeError):
                    cli.positive_hours(bad)


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
        # ⚠ 三件事：
        #   1) AGENT_CONFIG_DIR 是"按名字加载"用的；一个进程里只认第一次 setdefault，
        #      所以测试里直接覆盖环境变量，跑完还原。
        #   2) agent.config.load_config **有缓存**（生产上是对的：一个进程读一次）。
        #      测试里必须 clear_cache()，否则第二个用例读到的还是第一份配置。
        #   3) **默认指向一个不存在的 socket**：这一类的用例都建立在"问不到 Agent"之上
        #      （页脚要说"按时间算的，不代表已触发"）。不给 --socket 就用默认的
        #      /tmp/agent.sock —— 那台机器上**恰好在跑** Agent 时（板端验收现场就是这样）
        #      CLI 会真问到触发记录，页脚换成"来自运行中的 Agent"，于是断言假红。
        extra = list(extra)
        if "--socket" not in extra:
            extra = ["--socket", str(Path(path).parent / "nope.sock")] + extra
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

    def test_lists_the_window(self):
        """R1: 默认看**接下来 24 小时**，不再有"今天/明天整天"这回事。"""
        soon = datetime.now() + timedelta(minutes=30)      # 一定落在窗口里
        path = self._write_config(
            "scheduler:\n"
            "  recurring:\n"
            "    - state: sleep\n"
            "      start: \"%s\"\n"
            "    - state: study\n"
            "      start: \"%s\"\n" % (soon.strftime("%H:%M"),
                                     (soon + timedelta(hours=2)).strftime("%H:%M")))
        code, out, err = self._run(path)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("日程（配置共 2 条", out)
        self.assertIn("窗口：", out)
        self.assertIn("（24 小时", out)
        self.assertIn("SLEEP", out)
        self.assertIn("不代表", out)          # 页脚必须带上"不代表已触发"

    def test_hours_flag_changes_the_window_line(self):
        soon = datetime.now() + timedelta(minutes=30)
        path = self._write_config(
            "scheduler:\n  recurring:\n    - state: sleep\n      start: \"%s\"\n"
            % soon.strftime("%H:%M"))
        code, out, err = self._run(path, ["--hours", "3"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("（3 小时", out)

    def test_today_and_tomorrow_flags_are_gone(self):
        """R1 把 `--today/--tomorrow` 换成了 `--hours`：旧参数必须是参数错（退出码 2）。"""
        for flag in ("--today", "--tomorrow"):
            with self.subTest(flag=flag):
                with self.assertRaises(SystemExit) as ctx:
                    cli.build_parser().parse_args(["schedule", flag])
                self.assertEqual(ctx.exception.code, cli.EXIT_USAGE)

    def test_hours_must_be_positive(self):
        for bad in ("0", "-1", "abc"):
            with self.subTest(bad=bad):
                with self.assertRaises(SystemExit) as ctx:
                    cli.build_parser().parse_args(["schedule", "--hours", bad])
                self.assertEqual(ctx.exception.code, cli.EXIT_USAGE)

    def test_empty_schedule_is_not_an_error(self):
        path = self._write_config("llm:\n  mode: disabled\n")
        code, out, err = self._run(path)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("配置共 0 条", out)
        self.assertIn("（窗口内没有日程）", out)

    def test_broken_schedule_is_reported_honestly(self):
        path = self._write_config("scheduler:\n  recurring:\n    - state: study\n      start: \"99:99\"\n")
        code, out, err = self._run(path)
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("读不出来", err)
        self.assertIn("Agent 也会因此起不来", err)

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
            "scheduler:\n  recurring:\n    - state: sleep\n      start: \"10:00\"\n")
        code, out, err = self._run(path, ["--no-ask"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("不代表", out)                   # 还是那句按时间比较
        self.assertIn("--no-ask", out, "要讲明是**你**让它别问的")
        self.assertNotIn("问不到 Agent", err, "--no-ask 时不该去连 Agent")

    def test_ask_failure_falls_back_and_says_why(self):
        """Agent 没跑: 日程照样列出来, 但"已过"必须说明是按时间算的。"""
        tmp = tempfile.mkdtemp(prefix="cli-nosock-")
        self.addCleanup(shutil.rmtree, tmp, True)
        # ⚠ 时刻要落在窗口的**尾巴**里（最近 30 分钟）: 写死 10:00 的话, 下午跑测试
        #    那条就在窗口外了 —— 那是新语义, 不是 bug。
        just_passed = _just_passed(minutes=5)
        path = self._write_config(
            "scheduler:\n  recurring:\n    - state: sleep\n      start: \"%s\"\n"
            % just_passed.strftime("%H:%M"))
        missing = os.path.join(tmp, "nope.sock")
        code, out, err = self._run(path, ["--socket", missing, "--timeout", "0.2"])

        self.assertEqual(code, cli.EXIT_OK, "问不到触发记录不是失败")
        self.assertIn("问不到 Agent 的触发记录", err)
        self.assertIn("连不上 Agent", err)
        self.assertIn("SLEEP", out)
        self.assertIn("← 已过", out, "尾巴里那条过去了、但没有触发记录")
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
            "scheduler:\n  recurring:\n    - state: sleep\n      start: \"10:00\"\n")
        code, out, err = self._run(path, ["--socket", os.path.join(tmp, "x.sock"),
                                          "--timeout", "0.2"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("连不上 Agent", err)
        self.assertNotIn("没回", err)


class TestCleanupCommand(unittest.TestCase):
    """`assistant cleanup`：清理**已经过去**的一次性日程（默认只看，--apply 才删）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cli-clean-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = Path(self.tmp) / "config.yaml"
        self.path.write_text(
            "scheduler:\n"
            "  recurring:\n"
            "    - state: sleep\n"
            "      start: \"07:00\"\n"
            "  oneoff:\n"
            "    - state: sleep\n"
            "      date: 2026-09-20\n"
            "      start: \"14:00\"\n"
            "    - state: study\n"
            "      date: %s\n"
            "      start: \"00:01\"\n"
            "    - state: game\n"
            "      date: 2099-12-01\n"
            "      start: \"09:00\"\n" % date.today().isoformat(),
            encoding="utf-8")
        self.original = self.path.read_text(encoding="utf-8")

    def _run_with(self, path, extra=()):
        return _run_cli_with_config(["cleanup", "--config", str(path)] + list(extra), path)

    def _run(self, extra=()):
        return self._run_with(self.path, extra)

    def test_dry_run_lists_but_does_not_write(self):
        code, out, err = self._run()
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("2026-09-20", out)
        self.assertIn("SLEEP", out)
        self.assertIn("--apply", out)
        self.assertNotIn("已删除", out)
        self.assertEqual(self.path.read_text(encoding="utf-8"), self.original,
                         "dry-run 不许动文件")

    def test_today_and_future_and_recurring_are_not_touched(self):
        now = datetime.now()
        if now.hour == 0 and now.minute < 2:
            self.skipTest("刚过午夜，造不出'今天已经过了 start'这条")
        code, out, err = self._run()
        self.assertEqual(code, cli.EXIT_OK)
        listing = out.split("共")[0]                      # "要清理"那一段
        self.assertIn("2026-09-20", listing, "已经过去的那条要在清单里")
        self.assertNotIn("2099-12-01", listing, "将来的一次性日程不该进清理清单")
        self.assertNotIn(date.today().isoformat(), listing, "今天的不该进清理清单")
        self.assertIn("今天已经过了 start", out, "要提示一句'今天那条没动'")
        self.assertIn("没动", out)

    def test_apply_removes_only_the_stale_one(self):
        code, out, err = self._run(["--apply"])
        self.assertEqual(code, cli.EXIT_OK)
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("2026-09-20", text, "过去的那条要被删掉")
        self.assertIn(date.today().isoformat(), text, "今天的那条要留着")
        self.assertIn("2099-12-01", text, "将来的那条要留着")
        self.assertIn("07:00", text, "recurring 一条都不能动")
        self.assertTrue(Path(str(self.path) + ".bak").is_file(), "要留 .bak")
        self.assertEqual(Path(str(self.path) + ".bak").read_text(encoding="utf-8"),
                         self.original)

    def test_apply_twice_is_harmless(self):
        self._run(["--apply"])
        after_first = self.path.read_text(encoding="utf-8")
        code, out, err = self._run(["--apply"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("没有需要清理", out)
        self.assertEqual(self.path.read_text(encoding="utf-8"), after_first)

    def test_nothing_to_clean(self):
        self.path.write_text("scheduler:\n  recurring:\n    - state: sleep\n      start: \"07:00\"\n",
                             encoding="utf-8")
        code, out, err = self._run(["--apply"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("没有需要清理", out)

    def test_template_is_never_written(self):
        """`config.example.yaml` 是模板，`--apply` 不许碰它。

        ⚠ 要放在**独立空目录**里：`setUp` 那个目录已经有 `config.yaml` 了，
          加载器按名字找会先找到它，这条用例就测不到模板那条分支。
        """
        other = Path(tempfile.mkdtemp(prefix="cli-clean-ex-"))
        self.addCleanup(shutil.rmtree, str(other), True)
        example = other / "config.example.yaml"
        example.write_text(self.original, encoding="utf-8")
        code, out, err = self._run_with(example, ["--apply"])
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("模板", err)
        self.assertEqual(example.read_text(encoding="utf-8"), self.original)

    def test_unreadable_config(self):
        other = Path(tempfile.mkdtemp(prefix="cli-clean-bad-"))
        self.addCleanup(shutil.rmtree, str(other), True)
        bad = other / "nope.yaml"
        bad.write_text("x: 1\n", encoding="utf-8")
        code, out, err = self._run_with(bad)
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("读不到配置", err)

    def test_cleanup_rejects_a_timeout_option(self):
        """cleanup 不连 Agent，所以它不挂 `--timeout`（与 watch 同一条规矩）。"""
        with self.assertRaises(SystemExit) as ctx:
            cli.build_parser().parse_args(["cleanup", "--timeout", "3"])
        self.assertEqual(ctx.exception.code, cli.EXIT_USAGE)

    def test_stale_oneoffs_is_pure(self):
        from datetime import date as d
        events = cli.load_events({"scheduler": {
            "recurring": [{"state": "sleep", "start": "07:00"}],
            "oneoff": [{"state": "sleep", "date": "2026-09-20", "start": "14:00"},
                       {"state": "study", "date": "2026-09-22", "start": "14:00"},
                       {"state": "game", "date": "2026-12-01", "start": "14:00"}]}})
        stale = cli.stale_oneoffs(events, d(2026, 9, 22))
        self.assertEqual([e.state.value for e in stale], ["sleep"])
        self.assertEqual([e.on.isoformat() for e in stale], ["2026-09-20"])


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


class TestMusicCommand(unittest.TestCase):
    """`assistant music play|pause|toggle|next|prev`（T11-10c）—— 走**现成的**三个命令。

    ⚠ 协议里音乐只有**一个** toggle（`music_play_pause`），没有单独的 play/pause：
      所以"播放/暂停"是**幂等意图**（先问状态再决定发不发），下面把这条逻辑钉住。
    """

    def parse(self, *argv):
        return cli.build_parser().parse_args(list(argv))

    def test_every_action_parses(self):
        for action in cli.MUSIC_ACTIONS:
            args = self.parse("music", action)
            self.assertIs(args.func, cli.cmd_music)
            self.assertEqual(args.action, action)
            self.assertFalse(args.no_wait)

    def test_the_wire_commands_are_the_existing_ones(self):
        # 不新增协议：play/pause/toggle 都走 music_play_pause，next/prev 各走自己那条
        self.assertEqual(cli.MUSIC_COMMANDS["play"], "music_play_pause")
        self.assertEqual(cli.MUSIC_COMMANDS["pause"], "music_play_pause")
        self.assertEqual(cli.MUSIC_COMMANDS["toggle"], "music_play_pause")
        self.assertEqual(cli.MUSIC_COMMANDS["next"], "music_next")
        self.assertEqual(cli.MUSIC_COMMANDS["prev"], "music_prev")
        self.assertEqual(sorted(cli.MUSIC_COMMANDS), sorted(cli.MUSIC_ACTIONS))

    def test_a_bad_action_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as caught:
            self.parse("music", "dance")
        self.assertEqual(caught.exception.code, 2)          # argparse: 参数错 = 2

    def test_no_wait_is_accepted_and_socket_can_come_first(self):
        args = self.parse("--socket", "/tmp/x.sock", "music", "next", "--no-wait")
        self.assertTrue(args.no_wait)
        self.assertEqual(args.socket, "/tmp/x.sock")

    def test_play_is_idempotent_when_already_playing(self):
        # 已经在放 -> 不发命令（发了反而会暂停 —— 那不是"播放"）
        self.assertEqual(cli.music_decision("play", True), "already")
        self.assertEqual(cli.music_decision("pause", False), "already")

    def test_the_opposite_state_sends(self):
        self.assertEqual(cli.music_decision("play", False), "send")
        self.assertEqual(cli.music_decision("pause", True), "send")

    def test_unknown_state_still_sends(self):
        # 状态问不出来（Agent 没补推/还没放）-> 照发 toggle，并且**如实说**
        self.assertEqual(cli.music_decision("play", None), "send")
        self.assertEqual(cli.music_decision("pause", None), "send")

    def test_next_and_toggle_always_send(self):
        for action in ("next", "prev", "toggle"):
            self.assertEqual(cli.music_decision(action, True), "send")
            self.assertEqual(cli.music_decision(action, False), "send")
            self.assertEqual(cli.music_decision(action, None), "send")

    def test_state_text_says_what_is_playing(self):
        self.assertEqual(cli.music_state_text({"title": "夜曲", "playing": True}),
                         "夜曲（正在播放）")
        self.assertEqual(cli.music_state_text({"title": "夜曲", "playing": False}),
                         "夜曲（已暂停）")
        self.assertEqual(cli.music_state_text({"playing": True}), "（没说是哪首）（正在播放）")
        self.assertEqual(cli.music_state_text({"title": "夜曲"}), "夜曲")


@unittest.skipUnless(UNIX_SOCKET_SUPPORTED, "需要 AF_UNIX (Windows 的 CPython 不支持)")
class TestMusicAgainstRealServer(unittest.IsolatedAsyncioTestCase):
    """音乐命令对着**真的** LocalServer 跑一遍（与其它 CLI 用例同一套夹具）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cli-music-")
        self.path = os.path.join(self.tmp, "agent.sock")
        self.agent = _FakeAgent(self.path)

    async def asyncSetUp(self):
        await self.agent.start()
        self.addAsyncCleanup(self.agent.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def push_on_connect(self, title, playing):
        """真 Agent 连上时会补推一条 music（T8-4），这里照做。"""
        async def push_soon():
            self.assertTrue(await self.agent.wait_for_client(), "CLI 没连上来")
            await self.agent.push(TOPIC_MUSIC, {"title": title, "playing": playing})

        return asyncio.ensure_future(push_soon())

    async def test_play_on_an_already_playing_track_sends_nothing(self):
        args = cli.build_parser().parse_args(["music", "play", "--socket", self.path,
                                             "--timeout", "2"])
        pusher = self.push_on_connect("夜曲", True)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = await cli.cmd_music(args)
        await pusher

        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("正在播放", out.getvalue())
        self.assertIn("没发命令", out.getvalue())
        self.assertEqual(self.agent.commands, [], "已经在放就不该再发 toggle")

    async def test_pause_sends_the_toggle_and_reports_the_new_state(self):
        args = cli.build_parser().parse_args(["music", "pause", "--socket", self.path,
                                             "--timeout", "2"])
        self.agent.reply_with("music_play_pause", TOPIC_MUSIC,
                              lambda payload: {"title": "夜曲", "playing": False})
        pusher = self.push_on_connect("夜曲", True)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = await cli.cmd_music(args)
        await pusher

        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(self.agent.commands, [("music_play_pause", {})])
        self.assertIn("已暂停", out.getvalue())

    async def test_next_reports_the_new_track(self):
        args = cli.build_parser().parse_args(["music", "next", "--socket", self.path,
                                             "--timeout", "2"])
        self.agent.reply_with("music_next", TOPIC_MUSIC,
                              lambda payload: {"title": "晴天", "playing": True})
        pusher = self.push_on_connect("夜曲", True)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = await cli.cmd_music(args)
        await pusher

        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(self.agent.commands, [("music_next", {})])
        self.assertIn("晴天", out.getvalue())

    async def test_a_failure_prints_the_agents_words(self):
        args = cli.build_parser().parse_args(["music", "next", "--socket", self.path,
                                             "--timeout", "2"])
        self.agent.reply_with("music_next", TOPIC_LLM,
                              lambda payload: {"text": "队列是空的 —— 先让我放一首"})
        pusher = self.push_on_connect("夜曲", True)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = await cli.cmd_music(args)
        await pusher

        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("队列是空的", err.getvalue())

    async def test_a_state_change_that_did_not_happen_is_an_error(self):
        """Agent 回了 music，但 `playing` 没按预期变（例如 PC 上 mpv 拒了）—— 如实报错。"""
        args = cli.build_parser().parse_args(["music", "pause", "--socket", self.path,
                                             "--timeout", "2"])
        self.agent.reply_with("music_play_pause", TOPIC_MUSIC,
                              lambda payload: {"title": "夜曲", "playing": True})
        pusher = self.push_on_connect("夜曲", True)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = await cli.cmd_music(args)
        await pusher
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("没按预期变", err.getvalue())


class TestVideoCommand(unittest.TestCase):
    """`assistant video play|pause|toggle|next|prev`（T11-10f）—— 纯逻辑那部分。

    ⚠ 与音乐不同: 视频这边**新增了一条协议** `video_control{action}`（GUI 那颗按钮只有
      "本地点"，原来没有任何命令能让 Agent/CLI 去按它）。下面把线映射与"幂等意图"钉住。
    """

    def parse(self, *argv):
        return cli.build_parser().parse_args(list(argv))

    def test_every_action_parses(self):
        for action in cli.VIDEO_ACTIONS:
            args = self.parse("video", action)
            self.assertIs(args.func, cli.cmd_video)
            self.assertEqual(args.action, action)
            self.assertFalse(args.no_wait)
            self.assertFalse(args.wait_player)

    def test_the_wire_commands(self):
        # play/pause/toggle 都走新的 video_control；next/prev 走现成的那两条
        self.assertEqual(cli.VIDEO_COMMANDS["play"], "video_control")
        self.assertEqual(cli.VIDEO_COMMANDS["pause"], "video_control")
        self.assertEqual(cli.VIDEO_COMMANDS["toggle"], "video_control")
        self.assertEqual(cli.VIDEO_COMMANDS["next"], "next_bilibili")
        self.assertEqual(cli.VIDEO_COMMANDS["prev"], "prev_bilibili")
        self.assertEqual(sorted(cli.VIDEO_COMMANDS), sorted(cli.VIDEO_ACTIONS))

    def test_a_bad_action_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as caught:
            self.parse("video", "rewind")
        self.assertEqual(caught.exception.code, 2)          # argparse: 参数错 = 2

    def test_wait_player_and_no_wait_are_accepted(self):
        args = self.parse("--socket", "/tmp/x.sock", "video", "pause",
                          "--wait-player", "--no-wait")
        self.assertTrue(args.wait_player)
        self.assertTrue(args.no_wait)
        self.assertEqual(args.socket, "/tmp/x.sock")

    def test_play_pause_are_idempotent_intents(self):
        self.assertIsNone(cli.video_control_action("play", True))       # 已经在放 -> 不发
        self.assertIsNone(cli.video_control_action("pause", False))     # 已经暂停 -> 不发
        self.assertEqual(cli.video_control_action("play", False), "play")
        self.assertEqual(cli.video_control_action("pause", True), "pause")

    def test_unknown_state_still_sends_and_toggle_is_passed_through(self):
        # 状态问不出来（没在播/没补推）-> 照用户说的办
        self.assertEqual(cli.video_control_action("play", None), "play")
        self.assertEqual(cli.video_control_action("pause", None), "pause")
        # toggle 交给 Agent 按**它自己的真值**解析（不让两侧各自猜）
        for state in (True, False, None):
            self.assertEqual(cli.video_control_action("toggle", state), "toggle")

    def test_next_and_prev_need_no_playback_control(self):
        for action in ("next", "prev"):
            self.assertIsNone(cli.video_control_action(action, True))
            self.assertIsNone(cli.video_control_action(action, None))

    def test_playing_truth_is_saw_playing_and_playing(self):
        # ⚠ 缓冲的 playing 默认就是 True（还没起播时也是）—— "在放"要 saw_playing 也真
        self.assertIsNone(cli.video_playing({"buffer": {"playing": True}}))
        self.assertTrue(cli.video_playing({"buffer": {"playing": True, "saw_playing": True}}))
        self.assertFalse(cli.video_playing({"buffer": {"playing": False, "saw_playing": True}}))
        self.assertIsNone(cli.video_playing({}))                      # 没补推 -> 问不出来
        self.assertIsNone(cli.video_playing({"buffer": None}))

    def test_state_text_says_which_video_and_whether_it_plays(self):
        data = {"current": {"title": "Luna say maybe"},
                "buffer": {"playing": True, "saw_playing": True}}
        self.assertEqual(cli.video_state_text(data), "Luna say maybe（正在播放）")
        data["buffer"]["playing"] = False
        self.assertEqual(cli.video_state_text(data), "Luna say maybe（已暂停）")
        # 队列里没 title 就用缓冲里的；都没有就如实说"没说放的是哪条"
        self.assertEqual(cli.video_state_text({"buffer": {"title": "只缓冲里有"}}), "只缓冲里有")
        self.assertEqual(cli.video_state_text({}), "（Agent 没说放的是哪条）")


@unittest.skipUnless(UNIX_SOCKET_SUPPORTED, "需要 AF_UNIX (Windows 的 CPython 不支持)")
class TestVideoAgainstRealServer(unittest.IsolatedAsyncioTestCase):
    """视频命令对着**真的** LocalServer 跑一遍。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cli-video-")
        self.path = os.path.join(self.tmp, "agent.sock")
        self.agent = _FakeAgent(self.path)

    async def asyncSetUp(self):
        await self.agent.start()
        self.addAsyncCleanup(self.agent.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def payload(playing, *, saw_playing=True, index=0, title="第一条", bvid=None, extra=None):
        data = {"count": 18, "index": index,
                "current": {"bvid": bvid or ("BV%d" % index), "title": title},
                "queue": [{"bvid": "BV0", "title": "第一条"},
                          {"bvid": "BV1", "title": "第二条"}],
                "stream": "http://127.0.0.1:8765/stream/%s?v=t" % (bvid or "BV0"),
                "buffer": {"playing": playing, "saw_playing": saw_playing, "title": title,
                           "state": "serving" if playing else "paused"}}
        if extra:
            data.update(extra)
        return data

    async def push_bilibili(self, data):
        self.assertTrue(await self.agent.wait_for_client(), "CLI 没连上来")
        await self.agent.push(TOPIC_BILIBILI, data)

    def push_on_connect(self, data):
        return asyncio.ensure_future(self.push_bilibili(data))

    async def run_cli(self, *argv):
        args = cli.build_parser().parse_args(list(argv) + ["--socket", self.path,
                                                          "--timeout", "1.5"])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = await cli.cmd_video(args)
        return code, out.getvalue(), err.getvalue()

    async def test_pause_on_an_already_paused_video_sends_nothing(self):
        pusher = self.push_on_connect(self.payload(False))
        code, out, err = await self.run_cli("video", "pause")
        await pusher

        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("已经是暂停了", out)
        self.assertEqual(self.agent.commands, [], "已经暂停就不该再发命令")

    async def test_pause_sends_the_control_and_reports_the_ack(self):
        self.agent.reply_with("video_control", TOPIC_BILIBILI,
                              lambda payload: self.payload(False, extra={
                                  "control": {"action": "pause", "seq": 3}}))
        pusher = self.push_on_connect(self.payload(True))
        code, out, err = await self.run_cli("video", "pause")
        await pusher

        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertEqual(self.agent.commands, [("video_control", {"action": "pause"})])
        self.assertIn("已让播放器暂停", out)
        self.assertIn("第一条", out)

    async def test_toggle_asks_the_agent_to_decide(self):
        self.agent.reply_with("video_control", TOPIC_BILIBILI,
                              lambda payload: self.payload(True, extra={
                                  "control": {"action": "play", "seq": 4}}))
        pusher = self.push_on_connect(self.payload(False))
        code, out, err = await self.run_cli("video", "toggle")
        await pusher

        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertEqual(self.agent.commands, [("video_control", {"action": "toggle"})],
                         "toggle 原样交给 Agent 按它自己的真值解析")
        self.assertIn("已让播放器播放", out)               # Agent 解析成了 play

    async def test_wait_player_waits_for_the_players_own_report(self):
        async def handler(received, data):
            self.agent.commands.append((received, data))
            if received == "video_control":
                await self.agent.push(TOPIC_BILIBILI, self.payload(True, extra={
                    "control": {"action": "pause", "seq": 5}}))
                await asyncio.sleep(0.05)
                # GUI 的回报（Agent 转手推成 control_result）—— 这才是"播放器真的按了"
                await self.agent.push(TOPIC_BILIBILI, self.payload(False, extra={
                    "control_result": {"seq": 5, "action": "pause", "playing": False}}))

        self.agent.on_command(handler)
        pusher = self.push_on_connect(self.payload(True))
        code, out, err = await self.run_cli("video", "pause", "--wait-player")
        await pusher

        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertIn("播放器已回报", out)

    async def test_wait_player_times_out_when_the_player_never_reports(self):
        self.agent.reply_with("video_control", TOPIC_BILIBILI,
                              lambda payload: self.payload(True, extra={
                                  "control": {"action": "pause", "seq": 6}}))
        pusher = self.push_on_connect(self.payload(True))
        args = cli.build_parser().parse_args(["video", "pause", "--wait-player",
                                              "--socket", self.path, "--timeout", "0.4"])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = await cli.cmd_video(args)
        await pusher

        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("没等到播放器回报", err.getvalue())

    async def test_wait_player_reports_a_player_that_did_not_follow(self):
        async def handler(received, data):
            self.agent.commands.append((received, data))
            if received == "video_control":
                await self.agent.push(TOPIC_BILIBILI, self.payload(False, extra={
                    "control": {"action": "pause", "seq": 7}}))
                await self.agent.push(TOPIC_BILIBILI, self.payload(True, extra={
                    "control_result": {"seq": 7, "action": "pause", "playing": True}}))

        self.agent.on_command(handler)
        pusher = self.push_on_connect(self.payload(True))
        code, out, err = await self.run_cli("video", "pause", "--wait-player")
        await pusher

        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("没按预期变", err)

    async def test_a_failure_prints_the_agents_words(self):
        self.agent.reply_with("video_control", TOPIC_LLM,
                              lambda payload: {"text": "现在没有在播的视频 —— 先点一下预览图"})
        pusher = self.push_on_connect(self.payload(True))
        code, out, err = await self.run_cli("video", "pause")
        await pusher

        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("现在没有在播的视频", err)

    async def test_next_reports_the_new_title_and_says_it_is_buffering(self):
        self.agent.reply_with("next_bilibili", TOPIC_BILIBILI,
                              lambda payload: self.payload(True, index=1, title="第二条",
                                                           bvid="BV1", saw_playing=False))
        pusher = self.push_on_connect(self.payload(True))
        code, out, err = await self.run_cli("video", "next")
        await pusher

        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertEqual(self.agent.commands, [("next_bilibili", {})])
        self.assertIn("下一集", out)
        self.assertIn("第二条", out)
        self.assertIn("正在缓冲", out)                      # 攒够 15 s 才开播 —— 如实说

    async def test_no_wait_returns_without_an_ack(self):
        self.agent.record_only()
        pusher = self.push_on_connect(self.payload(True))
        code, out, err = await self.run_cli("video", "pause", "--no-wait")
        await pusher

        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("已发送：video pause", out)


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
            ["mode", "game", "--socket", self.path, "--timeout", "0.4"])

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = await cli.cmd_mode(args)

        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("没切过去", err.getvalue())
        self.assertIn("IDLE", err.getvalue())

    async def test_mode_walks_through_idle_and_says_so(self):
        """T12-2: 跨模式两跳（GAME->IDLE->SLEEP）—— 等到 SLEEP 才算成功, 并说出路径。"""
        async def on_command(action, payload):
            self.agent.commands.append((action, payload))
            if action != COMMAND_SWITCH_MODE:
                return
            await self.agent.push(TOPIC_STATUS, {"mode": "IDLE", "connected": False})
            await asyncio.sleep(0.05)
            await self.agent.push(TOPIC_STATUS,
                                  {"mode": payload.get("value"), "connected": False})

        self.agent.on_command(on_command)
        args = cli.build_parser().parse_args(
            ["mode", "sleep", "--socket", self.path, "--timeout", "2"])

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = await cli.cmd_mode(args)

        self.assertEqual(code, cli.EXIT_OK, err.getvalue())
        self.assertIn("已切到 SLEEP", out.getvalue())
        self.assertIn("路上经过 IDLE", out.getvalue())

    async def test_mode_that_never_arrives_is_reported_honestly(self):
        """只走到 IDLE 就没了（真切不过去）: 退出码 1, 并列出路上看到的。"""
        self.agent.reply_with(COMMAND_SWITCH_MODE, TOPIC_STATUS,
                              lambda payload: {"mode": "IDLE", "connected": False})
        args = cli.build_parser().parse_args(
            ["mode", "sleep", "--socket", self.path, "--timeout", "0.4"])

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = await cli.cmd_mode(args)

        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("没切过去", err.getvalue())
        self.assertIn("IDLE", err.getvalue())
        self.assertIn("路上经过 IDLE", err.getvalue())

    async def test_mode_without_any_push_still_says_so(self):
        self.agent.record_only()
        args = cli.build_parser().parse_args(
            ["mode", "game", "--socket", self.path, "--timeout", "0.3"])

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = await cli.cmd_mode(args)

        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("没等到 status 确认", err.getvalue())

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
        fact = {"state": "study", "date": "2026-09-22", "scheduled_at": "2026-09-22T13:00",
                "fired_at": "2026-09-22T13:00:03",
                "actions": [{"type": "state", "state": "study", "ok": True,
                             "steps": [{"from": "idle", "to": "study"}],
                             "why": "日程: 13:00 → study", "current": "study"}]}
        code, out, err = await self._watch(
            ["--count", "1"],
            [(TOPIC_SCHEDULE, {"kind": "fired", "event": fact})],
        )
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("kind=fired", out)
        self.assertIn("state=study", out)
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
        fact = {"state": "study", "date": "2026-09-22", "scheduled_at": "2026-09-22T09:30",
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
        """端到端: 真 server 应答 -> cmd_schedule 在**尾巴**里打出「已触发 HH:MM:SS」。

        ⚠ 窗口是"接下来 24 小时 + 最近 30 分钟"，所以这条日程必须落在**刚过去**的
          那一小段里：写死 09:30 的话，下午跑测试时它就在窗口外了（新语义，不是 bug）。
        """
        fired_at = _just_passed(minutes=4)
        start_at = fired_at - timedelta(minutes=1)
        fact = {"state": "study", "date": start_at.strftime("%Y-%m-%d"),
                "scheduled_at": start_at.strftime("%Y-%m-%dT%H:%M"),
                "fired_at": fired_at.strftime("%Y-%m-%dT%H:%M:%S"), "actions": []}
        self.agent.reply_with(COMMAND_QUERY_SCHEDULE, TOPIC_SCHEDULE,
                              lambda _payload: {"kind": "state", "now": "x", "limit": 50,
                                                "fired": [fact]})
        path = self._write_config(
            "ipc:\n  socket_path: %s\n" % self.path +
            "scheduler:\n  recurring:\n    - state: study\n      start: \"%s\"\n"
            % start_at.strftime("%H:%M"))

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
            ["schedule", "--config", str(path), "--timeout", "2"])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = await cli.cmd_schedule(args)

        self.assertEqual(code, cli.EXIT_OK)
        text = out.getvalue()
        self.assertIn("%s  STUDY  ← 已触发 %s"
                      % (start_at.strftime("%H:%M"), fired_at.strftime("%H:%M:%S")), text)
        self.assertIn("来自运行中的 Agent", text, "页脚要说明这份记录的出处")
        self.assertNotIn("不代表", text, "问到了就不该再用「按时间」那套免责话术")
        self.assertEqual(err.getvalue(), "", "问到了就不该有「问不到」的噪音")

    async def test_schedule_without_the_agent_still_lists_but_says_why(self):
        """同一个真 server, 但**不**应答查询: 列表照出, 页脚退回"按时间"。"""
        self.agent.record_only()
        start_at = _just_passed(minutes=4)
        path = self._write_config(
            "ipc:\n  socket_path: %s\n" % self.path +
            "scheduler:\n  recurring:\n    - state: study\n      start: \"%s\"\n"
            % start_at.strftime("%H:%M"))

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
            ["schedule", "--config", str(path), "--timeout", "0.2"])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = await cli.cmd_schedule(args)

        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("STUDY", out.getvalue())
        self.assertIn("← 已过", out.getvalue())
        self.assertIn("不代表", out.getvalue())
        self.assertIn("没回日程快照", err.getvalue())
        self.assertIn(start_at.date().isoformat(), out.getvalue())


# ===========================================================================
#  assistant tag（T7-2）
# ===========================================================================
class TestTagCommand(unittest.TestCase):
    """`assistant tag`：给壁纸打标签（默认只看；--apply 才写数据文件）。

    ⚠ 这一组**故意不碰模型**：dry-run 只算"哪些图要打"（纯 Python），
      所以开发机上（没有 numpy/cv2/NPU）也能跑。真打标签的验收在板端。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cli-tag-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.walls = Path(self.tmp) / "walls"
        self.walls.mkdir()
        for name in ("01_a.png", "02_b.png", "notes.txt"):
            (self.walls / name).write_bytes(b"\x89PNG\r\n\x1a\n" + name.encode("utf-8"))
        self.data = Path(self.tmp) / "wall_data.jsonl"
        self.path = Path(self.tmp) / "config.yaml"
        self.path.write_text(
            "wallpaper:\n"
            "  dir: %s\n"
            "  tagging:\n"
            "    data_file: %s\n"
            "    top_k: 2\n" % (self.walls, self.data),
            encoding="utf-8")

    def _args(self, extra=()):
        return cli.build_parser().parse_args(
            ["tag", "--config", str(self.path)] + list(extra))

    def _run(self, extra=()):
        return _run_cli_with_config(
            ["tag", "--config", str(self.path)] + list(extra), self.path)

    def test_dry_run_lists_the_plan_and_writes_nothing(self):
        code, out, err = self._run()
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("壁纸目录里 2 张图", out)          # notes.txt 不算图
        self.assertIn("要打 2 张（新图 2）", out)
        self.assertIn("dry-run", out)
        self.assertFalse(self.data.exists(), "dry-run 不该写数据文件")
        self.assertEqual(err, "")

    def test_tag_settings_reads_config_then_cli(self):
        args = self._args(["--dir", "/tmp/other", "--top-k", "5"])
        settings = cli.tag_settings({"wallpaper": {"dir": "/cfg/walls",
                                                   "tagging": {"top_k": 3}}}, args)
        self.assertEqual(settings["dir"], "/tmp/other", "命令行优先")
        self.assertEqual(settings["top_k"], 5)
        bare = cli.tag_settings({}, self._args())
        self.assertEqual(bare["top_k"], None)           # 没配就用 tagger 的默认
        self.assertTrue(bare["data_file"].endswith("wall_data.jsonl"))

    def test_plan_text_mentions_orphans_and_prune(self):
        plan = {"to_tag": [], "fresh": 1, "orphans": ["/gone.png"], "unknown": []}
        text = "\n".join(cli.tag_plan_text(plan, 2, "/data.jsonl"))
        self.assertIn("已是最新 1 张", text)
        self.assertIn("没有要打的", text)
        self.assertIn("--prune", text)

    def test_limit_must_be_positive(self):
        for bad in ("0", "-3"):
            code, out, err = self._run(["--limit", bad])
            self.assertEqual(code, cli.EXIT_USAGE, bad)
            self.assertIn("--limit", err)

    def test_apply_without_npu_says_what_is_missing(self):
        """开发机上 `--apply` 会缺 numpy/cv2/NPU —— 必须**说清缺什么**，而不是崩。"""
        if not os.path.isdir(self.walls):
            self.skipTest("没有壁纸目录")
        code, out, err = self._run(["--apply"])
        if "rknnlite" not in err and "numpy" not in err:
            self.skipTest("这台机器居然有全套依赖（板端）")
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("打标签需要这些依赖", err)
        self.assertIn("板端", err, "要说明这活只能在板端干")
        self.assertFalse(self.data.exists(), "依赖不齐时不该留下半个文件")

    def test_force_and_prune_are_accepted(self):
        code, out, err = self._run(["--force", "--prune"])
        self.assertEqual(code, cli.EXIT_OK)
        # 从没打过标签的图，原因仍是"新图"（比"强制重打"准确）
        self.assertIn("要打 2 张（新图 2）", out)
        self.assertIn("--prune", out)

    def test_missing_wallpaper_dir_is_reported(self):
        self.path.write_text("wallpaper:\n  dir: %s\n" % (Path(self.tmp) / "nope"),
                             encoding="utf-8")
        code, out, err = self._run()
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("壁纸目录用不了", err)

    def test_no_config_is_reported(self):
        missing = Path(self.tmp) / "nowhere" / "config.yaml"
        code, out, err = _run_cli_with_config(
            ["tag", "--config", str(missing)], missing)
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("读不到配置", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
