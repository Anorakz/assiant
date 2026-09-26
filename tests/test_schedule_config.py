#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_schedule_config.py — 日程配置的**文本级增删**（R3 的删除 + T12-5 的新增）

跑法:
    python tests/test_schedule_config.py

五层：
  1. **文本级（删）**：`remove_oneoff_entry()` 在各种 YAML 写法下的行为 —— 只删属于那条的行，
     其余**逐字节**不变；不认识的写法（flow 风格）给理由、不改文件
  1b. **行区间**：`oneoff:` 后面还有别的段时，区间必须在缩进回退那一行收住
     （T12-5 翻出来的老 bug：以前会一路吃到文件尾，"删一条把 `gui:` 整段删掉"）
  1c/1d. **通用查找 + 新增**：`find_entry()` / `add_entry()` —— 加在序列尾部、缩进跟着文件走、
     `key: []` 重写成块状写法、绝不新建第二个 `scheduler:` 段、CRLF 原样、查重命中一个字节都不改
  1e. **往返**：写进去的文本用**真的** `ScheduleEvent.from_config` 读回来还是同一条日程
  2. **文件级**：落盘 + `.bak`；没匹配上/查重命中就什么都不动；写盘失败原样抛 OSError
  3. **Scheduler 集成**：开关打开/关闭、只动 oneoff、失败不影响触发

⚠ 这里大量用"逐字节比对"：这类手术最容易出的问题是"改对了、但顺手改了别的行"。
"""

import os
import shutil
import stat
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.core import Scheduler, SchedulerError  # noqa: E402
from agent.core import schedule_config  # noqa: E402
from agent.core.state_machine import StateMachine  # noqa: E402

#: 以 root 跑时**目录权限拦不住写入**（板端就是 root）—— 那种环境下"写失败"要靠别的办法造，
#: 这几条用例在 root 下跳过，而不是把断言放宽成"要么抛要么不抛"（那等于没测）。
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


def matches_exactly(state, date_iso, start):
    """测试用匹配器：按字符串比（Scheduler 里那份会归一化时钟/日期）。"""
    def matches(fields):
        return (fields.get("state") == state
                and fields.get("date") == date_iso
                and fields.get("start") == start)
    return matches


class FakeBus:
    def __init__(self):
        self.events = []

    async def push(self, source, text):
        event = {"source": source, "text": text, "timestamp": 1.0 + len(self.events)}
        self.events.append(event)
        return event


# ===========================================================================
#  1. 文本级
# ===========================================================================
class TestRemoveOneoffEntry(unittest.TestCase):
    BODY = (
        "scheduler:\n"
        "  interval_min: 1\n"
        "  recurring:\n"
        "    - state: sleep\n"
        "      start: \"10:00\"\n"
        "  oneoff:\n"
        "    - state: game\n"
        "      date: 2026-09-22\n"
        "      start: \"14:00\"\n"
        "      end: \"15:30\"\n"
        "    - state: study\n"
        "      date: 2026-10-08\n"
        "      start: \"08:00\"\n"
    )

    def test_removes_the_first_item_and_keeps_everything_else(self):
        new_text, why = schedule_config.remove_oneoff_entry(
            self.BODY, matches_exactly("game", "2026-09-22", "14:00"))
        self.assertEqual(why, "")
        self.assertEqual(
            new_text,
            "scheduler:\n"
            "  interval_min: 1\n"
            "  recurring:\n"
            "    - state: sleep\n"
            "      start: \"10:00\"\n"
            "  oneoff:\n"
            "    - state: study\n"
            "      date: 2026-10-08\n"
            "      start: \"08:00\"\n")

    def test_removes_the_last_item(self):
        new_text, why = schedule_config.remove_oneoff_entry(
            self.BODY, matches_exactly("study", "2026-10-08", "08:00"))
        self.assertEqual(why, "")
        self.assertTrue(new_text.endswith(
            "  oneoff:\n"
            "    - state: game\n"
            "      date: 2026-09-22\n"
            "      start: \"14:00\"\n"
            "      end: \"15:30\"\n"))

    def test_removing_the_only_item_leaves_an_empty_sequence(self):
        body = "oneoff:\n  - state: sleep\n    date: 2026-09-22\n    start: \"14:00\"\n"
        new_text, why = schedule_config.remove_oneoff_entry(
            body, matches_exactly("sleep", "2026-09-22", "14:00"))
        self.assertEqual(why, "")
        self.assertEqual(new_text, "oneoff: []\n")   # 不留一个空值看着像写错了

    def test_top_level_layout_works_too(self):
        body = ("oneoff:\n"
                "  - state: study\n    date: 2026-09-22\n    start: \"14:00\"\n"
                "  - state: game\n    date: 2026-09-23\n    start: \"14:00\"\n")
        new_text, why = schedule_config.remove_oneoff_entry(
            body, matches_exactly("study", "2026-09-22", "14:00"))
        self.assertEqual(why, "")
        self.assertEqual(new_text,
                         "oneoff:\n"
                         "  - state: game\n    date: 2026-09-23\n    start: \"14:00\"\n")

    def test_comment_between_keys_goes_with_the_entry(self):
        body = ("oneoff:\n"
                "  - state: study\n"
                "    # 这条要提前三天准备\n"
                "    date: 2026-09-22\n"
                "    start: \"14:00\"\n"
                "  - state: game\n    date: 2026-09-23\n    start: \"14:00\"\n")
        new_text, why = schedule_config.remove_oneoff_entry(
            body, matches_exactly("study", "2026-09-22", "14:00"))
        self.assertEqual(why, "")
        self.assertNotIn("提前三天准备", new_text)
        self.assertEqual(new_text,
                         "oneoff:\n"
                         "  - state: game\n    date: 2026-09-23\n    start: \"14:00\"\n")

    def test_comment_before_the_next_entry_stays(self):
        """紧贴在**下一条**前面的注释留着 —— 宁可有行悬空注释, 也不误删信息。"""
        body = ("oneoff:\n"
                "  - state: study\n    date: 2026-09-22\n    start: \"14:00\"\n"
                "  # 乙是每周例会的替代品\n"
                "  - state: game\n    date: 2026-09-23\n    start: \"14:00\"\n")
        new_text, why = schedule_config.remove_oneoff_entry(
            body, matches_exactly("study", "2026-09-22", "14:00"))
        self.assertEqual(why, "")
        self.assertIn("# 乙是每周例会的替代品", new_text)
        self.assertEqual(new_text,
                         "oneoff:\n"
                         "  # 乙是每周例会的替代品\n"
                         "  - state: game\n    date: 2026-09-23\n    start: \"14:00\"\n")

    def test_blank_lines_between_entries_stay(self):
        body = ("oneoff:\n"
                "  - state: study\n    date: 2026-09-22\n    start: \"14:00\"\n"
                "\n"
                "  - state: game\n    date: 2026-09-23\n    start: \"14:00\"\n")
        new_text, _ = schedule_config.remove_oneoff_entry(
            body, matches_exactly("study", "2026-09-22", "14:00"))
        self.assertTrue(new_text.startswith("oneoff:\n\n"))
        self.assertIn("state: game", new_text)

    def test_crlf_is_preserved(self):
        body = ("oneoff:\r\n"
                "  - state: study\r\n    date: 2026-09-22\r\n    start: \"14:00\"\r\n"
                "  - state: game\r\n    date: 2026-09-23\r\n    start: \"14:00\"\r\n")
        new_text, why = schedule_config.remove_oneoff_entry(
            body, matches_exactly("study", "2026-09-22", "14:00"))
        self.assertEqual(why, "")
        self.assertEqual(new_text,
                         "oneoff:\r\n"
                         "  - state: game\r\n    date: 2026-09-23\r\n    start: \"14:00\"\r\n")
        self.assertNotIn("\n", new_text.replace("\r\n", ""))

    def test_trailing_comment_on_a_value_is_ignored(self):
        body = ("oneoff:\n"
                "  - state: study   # 下午那场\n"
                "    date: 2026-09-22   # 周二\n"
                "    start: \"14:00\"  # 别迟到\n")
        new_text, why = schedule_config.remove_oneoff_entry(
            body, matches_exactly("study", "2026-09-22", "14:00"))
        self.assertEqual(why, "")
        self.assertEqual(new_text, "oneoff: []\n")

    def test_flow_style_is_refused_with_a_reason(self):
        body = 'oneoff: [{state: study, date: 2026-09-22, start: "14:00"}]\n'
        new_text, why = schedule_config.remove_oneoff_entry(
            body, matches_exactly("study", "2026-09-22", "14:00"))
        self.assertEqual(new_text, body, "不认识的写法不许改文件")
        self.assertIn("flow 风格", why)

    def test_empty_sequence_is_not_found(self):
        body = "oneoff: []\n"
        new_text, why = schedule_config.remove_oneoff_entry(
            body, matches_exactly("study", "2026-09-22", "14:00"))
        self.assertEqual(new_text, body)
        self.assertIn("没有匹配", why)

    def test_missing_oneoff_key_is_not_found(self):
        body = "llm:\n  mode: disabled\n"
        new_text, why = schedule_config.remove_oneoff_entry(
            body, matches_exactly("study", "2026-09-22", "14:00"))
        self.assertEqual(new_text, body)
        self.assertIn("没有匹配", why)

    def test_duplicate_titles_differ_by_date(self):
        body = ("oneoff:\n"
                "  - state: study\n    date: 2026-09-22\n    start: \"20:00\"\n"
                "  - state: study\n    date: 2026-09-23\n    start: \"20:00\"\n")
        new_text, why = schedule_config.remove_oneoff_entry(
            body, matches_exactly("study", "2026-09-23", "20:00"))
        self.assertEqual(why, "")
        self.assertEqual(new_text,
                         "oneoff:\n"
                         "  - state: study\n    date: 2026-09-22\n    start: \"20:00\"\n")

    def test_recurring_with_the_same_title_is_untouched(self):
        body = ("recurring:\n"
                "  - state: study\n    days: [mon]\n    start: \"09:30\"\n"
                "oneoff:\n"
                "  - state: study\n    date: 2026-09-22\n    start: \"09:30\"\n")
        new_text, why = schedule_config.remove_oneoff_entry(
            body, matches_exactly("study", "2026-09-22", "09:30"))
        self.assertEqual(why, "")
        self.assertIn("recurring", new_text, "recurring 那条必须原样留着")
        self.assertNotIn("date: 2026-09-22", new_text)

    def test_strip_scalar(self):
        self.assertEqual(schedule_config.strip_scalar('  "09:30"  '), "09:30")
        self.assertEqual(schedule_config.strip_scalar("'09:30'"), "09:30")
        self.assertEqual(schedule_config.strip_scalar("09:30  # 注释"), "09:30")
        self.assertEqual(schedule_config.strip_scalar("# 整行注释"), "")
        self.assertEqual(schedule_config.strip_scalar('"带 # 号的标题"'), "带 # 号的标题")

    def test_find_oneoff_block_reports_the_span(self):
        span, why = schedule_config.find_oneoff_block(
            self.BODY, matches_exactly("game", "2026-09-22", "14:00"))
        self.assertEqual(why, "")
        self.assertEqual(span, (6, 10))       # 6..9 行（0 基）

    def test_find_oneoff_block_reports_why_not(self):
        span, why = schedule_config.find_oneoff_block(
            self.BODY, matches_exactly("sleep", "2026-09-22", "14:00"))
        self.assertIsNone(span)
        self.assertIn("没有匹配", why)


# ===========================================================================
#  1b. 序列的行区间（T12-5 翻出来的老 bug）
# ===========================================================================
class TestSequenceSpan(unittest.TestCase):
    """`oneoff:` 后面还有别的段时，区间必须在**缩进回退那一行**收住。

    T12-5 之前 `_sequence_items` 把最后一条的 `end` 记成 `len(lines)`：删一条 oneoff
    会把后面的 `gui:` 整段一起删掉（R3 默认关着，所以一直没露头；板端真配置的
    `oneoff:` 后面就正好是 `gui:`）。"加一条"直接踩在上面，才把它翻出来。
    """

    BODY = ("scheduler:\n"
            "  oneoff:\n"
            "    - state: game\n"
            "      date: 2026-09-22\n"
            "      start: \"14:00\"\n"
            "\n"
            "gui:\n"
            "  theme: grey\n")

    #: `yaml.safe_dump` 的默认风格: **减号与键同缩进**（T12-7 板端验收抓到的漏项）
    SAFE_DUMP_BODY = ("scheduler:\n"
                      "  interval_min: 1\n"
                      "  oneoff:\n"
                      "  - state: sleep\n"
                      "    date: '2026-09-26'\n"
                      "    start: '23:01'\n"
                      "  recurring: []\n"
                      "gui:\n"
                      "  theme: grey\n")

    def test_span_stops_at_the_next_section(self):
        lines = schedule_config._split_lines(self.BODY)
        self.assertEqual(schedule_config._sequence_items(lines, 1, 2), [(2, 5)])

    def test_removing_the_last_oneoff_keeps_the_following_section(self):
        new_text, why = schedule_config.remove_oneoff_entry(
            self.BODY, matches_exactly("game", "2026-09-22", "14:00"))
        self.assertEqual(why, "")
        self.assertEqual(new_text,
                         "scheduler:\n"
                         "  oneoff: []\n"
                         "\n"
                         "gui:\n"
                         "  theme: grey\n")

    def test_removing_a_recurring_entry_keeps_oneoff_and_what_follows(self):
        body = ("scheduler:\n"
                "  recurring:\n"
                "    - state: study\n"
                "      start: \"09:00\"\n"
                "  oneoff:\n"
                "    - state: game\n"
                "      date: 2026-09-22\n"
                "      start: \"14:00\"\n"
                "gui:\n"
                "  theme: grey\n")
        new_text, why, found = schedule_config.remove_entry(
            body, lambda f: f.get("state") == "study")
        self.assertEqual(why, "")
        self.assertEqual(found["key"], "recurring")
        self.assertEqual(new_text,
                         "scheduler:\n"
                         "  recurring: []\n"
                         "  oneoff:\n"
                         "    - state: game\n"
                         "      date: 2026-09-22\n"
                         "      start: \"14:00\"\n"
                         "gui:\n"
                         "  theme: grey\n")

    # ---- T12-7: `yaml.safe_dump` 那种"减号与键同缩进"的写法 ----
    def test_safe_dump_style_items_are_found(self):
        lines = schedule_config._split_lines(self.SAFE_DUMP_BODY)
        self.assertEqual(schedule_config._sequence_items(lines, 2, 2), [(3, 6)])

    def test_safe_dump_style_oneoff_can_be_removed(self):
        new_text, why = schedule_config.remove_oneoff_entry(
            self.SAFE_DUMP_BODY, matches_exactly("sleep", "2026-09-26", "23:01"))
        self.assertEqual(why, "")
        self.assertNotIn("23:01", new_text)
        self.assertIn("  recurring: []\n", new_text, "后面的键原样留着")
        self.assertIn("gui:\n  theme: grey\n", new_text)

    def test_safe_dump_style_empty_key_gets_the_repo_style_block(self):
        # `recurring: []` 是空的 -> 没有"已有条目的缩进"可抄, 用本仓库那套（键 + 2）
        new_text, why = schedule_config.add_entry(
            self.SAFE_DUMP_BODY, {"state": "study", "start": "09:30"})
        self.assertEqual(why, "")
        self.assertIn("  recurring:\n    - state: study\n      start: \"09:30\"\n", new_text)
        self.assertIn("  - state: sleep\n", new_text, "同缩进那条 oneoff 不许被动")

    def test_safe_dump_style_non_empty_sequence_keeps_its_own_indent(self):
        new_text, why = schedule_config.add_entry(
            self.SAFE_DUMP_BODY, {"state": "study", "start": "09:30",
                                  "date": "2026-09-27"})
        self.assertEqual(why, "")
        self.assertIn("  - state: study\n    date: \"2026-09-27\"\n    start: \"09:30\"\n",
                      new_text)
        self.assertIn("  recurring: []\n", new_text, "别的键仍不许动")


# ===========================================================================
#  1c. 通用查找（recurring 与 oneoff 都找）
# ===========================================================================
class TestFindEntry(unittest.TestCase):
    BODY = ("scheduler:\n"
            "  recurring:\n"
            "    - state: study\n"
            "      days: [mon, wed]\n"
            "      start: \"09:00\"\n"
            "  oneoff:\n"
            "    - state: study\n"
            "      date: 2026-09-22\n"
            "      start: \"09:00\"\n")

    def test_reports_the_key_fields_and_span(self):
        found, why = schedule_config.find_entry(self.BODY, lambda f: f.get("days") == "[mon, wed]")
        self.assertEqual(why, "")
        self.assertEqual(found["key"], "recurring")
        self.assertEqual(found["fields"]["state"], "study")
        self.assertEqual(found["fields"]["days"], "[mon, wed]")
        self.assertEqual(found["span"], (2, 5))

    def test_oneoff_is_found_too(self):
        found, why = schedule_config.find_entry(
            self.BODY, lambda f: f.get("date") == "2026-09-22")
        self.assertEqual(why, "")
        self.assertEqual(found["key"], "oneoff")

    def test_no_match_says_so(self):
        found, why = schedule_config.find_entry(self.BODY, lambda f: False)
        self.assertIsNone(found)
        self.assertIn("没有匹配", why)

    def test_flow_style_is_reported_as_unsupported(self):
        found, why = schedule_config.find_entry(
            "recurring: [{state: study, start: \"09:00\"}]\n", lambda f: True)
        self.assertIsNone(found)
        self.assertIn("flow 风格", why)


# ===========================================================================
#  1d. 加条目（文本级）
# ===========================================================================
class TestAddEntry(unittest.TestCase):
    BODY = (
        "# 顶部注释\n"
        "llm:\n"
        "  mode: disabled\n"
        "\n"
        "scheduler:\n"
        "  interval_min: 1\n"
        "  recurring: []\n"
        "  oneoff: []\n"
        "\n"
        "gui:\n"
        "  theme: grey\n"
    )

    def test_fills_an_empty_sequence(self):
        new_text, why = schedule_config.add_entry(
            self.BODY, {"state": "study", "start": "9:30", "days": ["mon", "wed"]})
        self.assertEqual(why, "")
        self.assertIn("  recurring:\n"
                      "    - state: study\n"
                      "      days: [mon, wed]\n"
                      "      start: \"09:30\"\n"
                      "  oneoff: []\n", new_text)
        self.assertTrue(new_text.startswith("# 顶部注释\n"), "前面的字节不许动")
        self.assertTrue(new_text.endswith("gui:\n  theme: grey\n"), "后面的段不许动")

    def test_appends_after_the_last_item(self):
        once, _ = schedule_config.add_entry(self.BODY, {"state": "study", "start": "09:30"})
        twice, why = schedule_config.add_entry(once, {"state": "sleep", "start": "22:00"})
        self.assertEqual(why, "")
        self.assertIn("    - state: study\n"
                      "      start: \"09:30\"\n"
                      "    - state: sleep\n"
                      "      start: \"22:00\"\n"
                      "  oneoff: []\n", twice)

    def test_date_goes_to_oneoff(self):
        new_text, why = schedule_config.add_entry(
            self.BODY, {"state": "idle", "start": "07:05", "date": "2026-10-01"})
        self.assertEqual(why, "")
        self.assertIn("  recurring: []\n"
                      "  oneoff:\n"
                      "    - state: idle\n"
                      '      date: "2026-10-01"\n'
                      "      start: \"07:05\"\n", new_text)

    def test_entry_key_of_follows_the_reader(self):
        self.assertEqual(schedule_config.entry_key_of({"start": "09:00"}), "recurring")
        self.assertEqual(schedule_config.entry_key_of({"date": "2026-09-22"}), "oneoff")
        # 空 date 不算"有 date"（读的一侧也是这么判的）
        self.assertEqual(schedule_config.entry_key_of({"date": ""}), "recurring")

    def test_missing_key_is_created_inside_the_existing_section(self):
        body = ("scheduler:\n"
                "  interval_min: 1\n"
                "\n"
                "gui:\n"
                "  theme: grey\n")
        new_text, why = schedule_config.add_entry(body, {"state": "study", "start": "08:00"})
        self.assertEqual(why, "")
        self.assertEqual(new_text.count("scheduler:"), 1, "绝不许出现第二个 scheduler 段")
        self.assertIn("  interval_min: 1\n"
                      "  recurring:\n"
                      "    - state: study\n"
                      "      start: \"08:00\"\n"
                      "\n"
                      "gui:\n", new_text)

    def test_missing_section_is_appended_at_the_end(self):
        new_text, why = schedule_config.add_entry(
            "llm:\n  mode: disabled\n", {"state": "study", "start": "08:00"})
        self.assertEqual(why, "")
        self.assertEqual(new_text,
                         "llm:\n  mode: disabled\n"
                         "\n"
                         "scheduler:\n"
                         "  recurring:\n"
                         "    - state: study\n"
                         "      start: \"08:00\"\n")

    def test_missing_section_without_a_trailing_newline(self):
        new_text, why = schedule_config.add_entry(
            "llm:\n  mode: disabled", {"state": "study", "start": "08:00"})
        self.assertEqual(why, "")
        self.assertTrue(new_text.endswith("      start: \"08:00\"\n"))

    def test_empty_file_gets_a_whole_section(self):
        new_text, why = schedule_config.add_entry("", {"state": "study", "start": "08:00"})
        self.assertEqual(why, "")
        self.assertEqual(new_text,
                         "scheduler:\n"
                         "  recurring:\n"
                         "    - state: study\n"
                         "      start: \"08:00\"\n")

    def test_top_level_layout_is_found(self):
        body = "recurring:\n  - state: study\n    start: \"09:00\"\n"
        new_text, why = schedule_config.add_entry(body, {"state": "idle", "start": "10:00"})
        self.assertEqual(why, "")
        self.assertEqual(new_text,
                         "recurring:\n  - state: study\n    start: \"09:00\"\n"
                         "  - state: idle\n    start: \"10:00\"\n")

    def test_existing_item_indent_is_reused(self):
        body = ("scheduler:\n"
                "  recurring:\n"
                "      - state: study\n"        # 6 空格的减法也认
                "        start: \"09:00\"\n")
        new_text, why = schedule_config.add_entry(body, {"state": "idle", "start": "10:00"})
        self.assertEqual(why, "")
        self.assertIn("      - state: idle\n        start: \"10:00\"\n", new_text)

    def test_duplicate_is_refused_without_touching_the_text(self):
        once, _ = schedule_config.add_entry(self.BODY, {"state": "study", "start": "09:30"})
        twice, why = schedule_config.add_entry(
            once, {"state": "study", "start": "09:30"},
            matches=lambda f: f.get("state") == "study" and f.get("start") == "09:30")
        self.assertEqual(twice, once, "查重命中就一个字节都不改")
        self.assertIn("已经有一条一样的了", why)

    def test_a_different_entry_is_not_a_duplicate(self):
        once, _ = schedule_config.add_entry(self.BODY, {"state": "study", "start": "09:30"})
        # 查重的 predicate 描述的是"我要加的那条"（10:30），跟已有的 09:30 不是一回事
        twice, why = schedule_config.add_entry(
            once, {"state": "study", "start": "10:30"},
            matches=lambda f: f.get("state") == "study" and f.get("start") == "10:30")
        self.assertEqual(why, "")
        self.assertIn('start: "10:30"', twice)

    def test_flow_style_sequence_is_refused(self):
        body = 'scheduler:\n  recurring: [{state: study, start: "09:00"}]\n'
        new_text, why = schedule_config.add_entry(body, {"state": "idle", "start": "10:00"})
        self.assertEqual(new_text, body)
        self.assertIn("flow 风格", why)

    def test_empty_days_list_means_every_day_so_the_key_is_left_out(self):
        new_text, why = schedule_config.add_entry(
            self.BODY, {"state": "study", "start": "09:00", "days": []})
        self.assertEqual(why, "")
        self.assertIn("    - state: study\n      start: \"09:00\"\n", new_text)
        self.assertNotIn("days", new_text)

    def test_comments_and_blank_lines_inside_the_sequence_stay(self):
        body = ("scheduler:\n"
                "  recurring:\n"
                "    # 早上那条\n"
                "    - state: study\n"
                "      start: \"09:00\"\n"
                "\n"
                "  oneoff: []\n")
        new_text, why = schedule_config.add_entry(body, {"state": "idle", "start": "10:00"})
        self.assertEqual(why, "")
        self.assertIn("# 早上那条", new_text)
        self.assertIn("- state: study\n      start: \"09:00\"\n    - state: idle\n", new_text)
        self.assertIn("\n  oneoff: []\n", new_text)

    def test_crlf_is_preserved(self):
        body = self.BODY.replace("\n", "\r\n")
        new_text, why = schedule_config.add_entry(body, {"state": "study", "start": "09:30"})
        self.assertEqual(why, "")
        self.assertNotIn("\n", new_text.replace("\r\n", ""), "混进了 LF")
        self.assertIn("    - state: study\r\n      start: \"09:30\"\r\n", new_text)

    def test_quotes_make_the_scalars_strings_on_both_sides(self):
        """`9:30` 不加引号在 YAML 1.1 里是整数, `2026-09-22` 是 date 类型 —— 一律加引号。"""
        new_text, _ = schedule_config.add_entry(
            self.BODY, {"state": "study", "start": "9:30", "date": "2026-09-22"})
        self.assertIn('      date: "2026-09-22"\n', new_text)
        self.assertIn('      start: "09:30"\n', new_text)

    def test_bad_values_raise(self):
        for values in ({"start": "09:00"},                       # 缺 state
                       {"state": "study"},                       # 缺 start
                       {"state": "study", "start": "25:00"},     # 时越界
                       {"state": "study", "start": "09:61"},     # 分越界
                       {"state": "study", "start": "九点"},       # 不是时间
                       {"state": "bad token", "start": "09:00"}, # 会写坏 YAML
                       {"state": "study", "start": "09:00", "days": ["mon"],
                        "date": "2026-09-22"},                   # date 与 days 同时给
                       {"state": "study", "start": "09:00", "date": "2026/09/22"}):
            with self.subTest(values=values):
                self.assertRaises(schedule_config.ScheduleConfigError,
                                  schedule_config.add_entry, self.BODY, values)

    def test_render_clock_normalises(self):
        self.assertEqual(schedule_config._render_clock("9:30"), "09:30")
        self.assertEqual(schedule_config._render_clock("0930"), "09:30")
        self.assertEqual(schedule_config._render_clock("09：30"), "09:30")
        self.assertEqual(schedule_config._render_clock(" 23:59 "), "23:59")
        self.assertRaises(schedule_config.ScheduleConfigError,
                          schedule_config._render_clock, "9:30:00")


# ===========================================================================
#  1e. 写进去的能被**真的**读回来（文本 ↔ 语义 的往返）
# ===========================================================================
class TestRoundTripThroughTheRealReader(unittest.TestCase):
    """加进去的那段文本，必须能被真的 `ScheduleEvent.from_config` 读成同一条日程。

    这一条把"渲染"和"语义"接上：格式再好看，读回来不是那条也是白搭。
    """

    def _load(self, text):
        import yaml
        from agent.core.scheduler import ScheduleEvent
        section = yaml.safe_load(text)["scheduler"]
        events = []
        for key in ("recurring", "oneoff"):
            for index, entry in enumerate(section.get(key) or []):
                events.append(ScheduleEvent.from_config(entry, index))
        return events

    def test_recurring_with_days(self):
        text, why = schedule_config.add_entry(
            "scheduler:\n  recurring: []\n  oneoff: []\n",
            {"state": "STUDY", "start": "9:30", "days": ["mon", "wed"]})
        self.assertEqual(why, "")
        events = self._load(text)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].state.value, "study")
        self.assertEqual(events[0].start, (9, 30))
        self.assertEqual(events[0].days, {0, 2})
        self.assertIsNone(events[0].on)

    def test_oneoff_with_a_date(self):
        text, why = schedule_config.add_entry(
            "scheduler:\n  recurring: []\n  oneoff: []\n",
            {"state": "sleep", "start": "22:30", "date": "2026-09-22"})
        self.assertEqual(why, "")
        events = self._load(text)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].state.value, "sleep")
        self.assertEqual(events[0].on.isoformat(), "2026-09-22")
        self.assertEqual(events[0].start, (22, 30))

    def test_every_day_when_the_days_key_is_absent(self):
        text, _ = schedule_config.add_entry(
            "scheduler:\n  recurring: []\n  oneoff: []\n",
            {"state": "game", "start": "20:00"})
        events = self._load(text)
        self.assertEqual(events[0].days, set(), "缺 days = 每天")

    def test_a_whole_new_section_also_reads_back(self):
        text, _ = schedule_config.add_entry("llm:\n  mode: disabled\n",
                                            {"state": "study", "start": "08:00"})
        events = self._load(text)
        self.assertEqual(events[0].state.value, "study")

    def test_added_entry_survives_a_remove_round_trip(self):
        text, _ = schedule_config.add_entry(
            "scheduler:\n  recurring: []\n  oneoff: []\n",
            {"state": "study", "start": "09:30", "days": ["mon"]})
        again, why, found = schedule_config.remove_entry(
            text, lambda f: f.get("state") == "study" and f.get("start") == "09:30")
        self.assertEqual(why, "")
        self.assertEqual(found["key"], "recurring")
        self.assertEqual(again, "scheduler:\n  recurring: []\n  oneoff: []\n")


# ===========================================================================
#  2. 文件级
# ===========================================================================
class TestRemoveOneoffFromFile(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="schedcfg-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.path = self.tmp / "config.yaml"
        self.original = ("scheduler:\n"
                         "  oneoff:\n"
                         "    - state: game\n"
                         "      date: 2026-09-22\n"
                         "      start: \"14:00\"\n"
                         "    - state: study\n"
                         "      date: 2026-10-08\n"
                         "      start: \"08:00\"\n")
        self.path.write_text(self.original, encoding="utf-8")

    def test_writes_the_new_text_and_a_backup(self):
        removed = schedule_config.remove_oneoff_from_file(
            self.path, matches_exactly("game", "2026-09-22", "14:00"))
        self.assertTrue(removed)
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("2026-09-22", text)
        self.assertIn("2026-10-08", text)
        backup = Path(str(self.path) + ".bak")
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_text(encoding="utf-8"), self.original)

    def test_no_match_touches_nothing(self):
        removed = schedule_config.remove_oneoff_from_file(
            self.path, matches_exactly("sleep", "2026-09-22", "14:00"))
        self.assertFalse(removed)
        self.assertEqual(self.path.read_text(encoding="utf-8"), self.original)
        self.assertFalse(Path(str(self.path) + ".bak").exists(), "没删就别留 .bak")

    def test_missing_file_raises_oserror(self):
        with self.assertRaises(OSError):
            schedule_config.remove_oneoff_from_file(
                self.tmp / "nope.yaml", matches_exactly("study", "2026-09-22", "14:00"))

    @unittest.skipIf(os.name == "nt", "只读目录在 Windows 上不拦 root/管理员")
    @unittest.skipIf(IS_ROOT, "root 无视目录权限（板端就是 root）")
    def test_read_only_directory_raises_oserror(self):
        os.chmod(str(self.tmp), stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(os.chmod, str(self.tmp), stat.S_IRWXU)
        with self.assertRaises(OSError):
            schedule_config.remove_oneoff_from_file(
                self.path, matches_exactly("game", "2026-09-22", "14:00"))


# ===========================================================================
#  2b. 文件级：加 / 删（T12-5）
# ===========================================================================
class TestAddAndRemoveInFile(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="schedadd-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.path = self.tmp / "config.yaml"
        self.original = ("scheduler:\n"
                         "  recurring: []\n"
                         "  oneoff: []\n"
                         "\n"
                         "gui:\n"
                         "  theme: grey\n")
        self.path.write_text(self.original, encoding="utf-8")

    def test_add_writes_the_file_and_a_backup(self):
        wrote, why = schedule_config.add_entry_in_file(
            self.path, {"state": "study", "start": "09:30", "days": ["mon"]})
        self.assertTrue(wrote)
        self.assertEqual(why, "")
        text = self.path.read_text(encoding="utf-8")
        self.assertIn('    - state: study\n      days: [mon]\n      start: "09:30"\n', text)
        self.assertTrue(text.endswith("gui:\n  theme: grey\n"))
        backup = Path(str(self.path) + ".bak")
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_text(encoding="utf-8"), self.original)

    def test_add_duplicate_touches_nothing(self):
        schedule_config.add_entry_in_file(self.path, {"state": "study", "start": "09:30"})
        before = self.path.read_text(encoding="utf-8")
        wrote, why = schedule_config.add_entry_in_file(
            self.path, {"state": "study", "start": "09:30"},
            matches=lambda f: f.get("state") == "study" and f.get("start") == "09:30")
        self.assertFalse(wrote)
        self.assertIn("已经有一条一样的了", why)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_add_missing_file_raises_oserror(self):
        with self.assertRaises(OSError):
            schedule_config.add_entry_in_file(self.tmp / "nope.yaml",
                                              {"state": "study", "start": "09:30"})

    def test_remove_writes_the_file_and_a_backup(self):
        schedule_config.add_entry_in_file(self.path,
                                          {"state": "study", "start": "09:30"})
        before = self.path.read_text(encoding="utf-8")
        removed, why, found = schedule_config.remove_entry_in_file(
            self.path, lambda f: f.get("state") == "study")
        self.assertTrue(removed)
        self.assertEqual(why, "")
        self.assertEqual(found["key"], "recurring")
        self.assertEqual(self.path.read_text(encoding="utf-8"), self.original)
        self.assertEqual(Path(str(self.path) + ".bak").read_text(encoding="utf-8"), before)

    def test_remove_no_match_touches_nothing(self):
        removed, why, found = schedule_config.remove_entry_in_file(
            self.path, lambda f: f.get("state") == "study")
        self.assertFalse(removed)
        self.assertIn("没有匹配", why)
        self.assertIsNone(found)
        self.assertEqual(self.path.read_text(encoding="utf-8"), self.original)
        self.assertFalse(Path(str(self.path) + ".bak").exists(), "没删就别留 .bak")

    def test_remove_missing_file_raises_oserror(self):
        with self.assertRaises(OSError):
            schedule_config.remove_entry_in_file(self.tmp / "nope.yaml", lambda f: True)

    def test_crlf_survives_a_file_write(self):
        # ⚠ 用 bytes 写：`Path.write_text` 走文本模式，换行会被翻译 —— 造不出"确定的 CRLF"
        self.path.write_bytes(self.original.replace("\n", "\r\n").encode("utf-8"))
        schedule_config.add_entry_in_file(self.path, {"state": "study", "start": "09:30"})
        raw = self.path.read_bytes().decode("utf-8")
        self.assertNotIn("\n", raw.replace("\r\n", ""), "混进了 LF")
        self.assertEqual(raw.count("\r\n"), raw.count("\n"), "CRLF 数必须与 LF 数相等")
        # .bak 也要逐字节等于原文（含 CRLF）
        self.assertEqual(Path(str(self.path) + ".bak").read_bytes(),
                         self.original.replace("\n", "\r\n").encode("utf-8"))

    @unittest.skipIf(os.name == "nt", "只读目录在 Windows 上不拦 root/管理员")
    @unittest.skipIf(IS_ROOT, "root 无视目录权限（板端就是 root）")
    def test_read_only_directory_raises_oserror(self):
        os.chmod(str(self.tmp), stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(os.chmod, str(self.tmp), stat.S_IRWXU)
        with self.assertRaises(OSError):
            schedule_config.add_entry_in_file(self.path,
                                              {"state": "study", "start": "09:30"})


# ===========================================================================
#  3. Scheduler 集成
# ===========================================================================
class TestSchedulerRemoval(unittest.IsolatedAsyncioTestCase):
    ONE_DAY = "2026-09-22"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="schedrm-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.path = self.tmp / "config.yaml"

    def _write(self, *, remove_fired, oneoff=True, recurring=False):
        lines = ["scheduler:", "  interval_min: 1"]
        if remove_fired is not None:
            lines.append("  remove_fired_oneoff: %s" % ("true" if remove_fired else "false"))
        if recurring:
            lines += ["  recurring:", "    - state: sleep", "      start: \"09:00\""]
        if oneoff:
            lines += ["  oneoff:",
                      "    - state: game",
                      "      date: %s" % self.ONE_DAY,
                      "      start: \"14:00\""]
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return self.path

    def _point_config_dir_at_tmp(self):
        """让 `load_config("config")` 读**临时目录**里那份（并清缓存）。

        ⚠ `load_config` 按 `AGENT_CONFIG_DIR` 找文件、而且**有缓存**。不设它就会去读
          仓库/板端的真实配置 —— 这个错在 PC 上会被"退回 example 模板（0 条）"蒙过去，
          在板端（有 live config）才露出来，所以这里显式设、显式清。
        """
        os.environ["AGENT_CONFIG_DIR"] = str(self.tmp)
        self.addCleanup(os.environ.pop, "AGENT_CONFIG_DIR", None)
        from agent import config as cfg
        cfg.clear_cache()
        self.addCleanup(cfg.clear_cache)
        return cfg

    def _scheduler(self, *, config_path=True, **kwargs):
        data = self._point_config_dir_at_tmp().load_config("config")
        return Scheduler(state=StateMachine(), bus=FakeBus(), config=data,
                         config_path=str(self.path) if config_path else None, **kwargs)

    async def test_removes_the_fired_oneoff_from_the_file(self):
        self._write(remove_fired=True)
        bus = FakeBus()
        scheduler = Scheduler(state=StateMachine(), bus=bus,
                              config={"scheduler": {"remove_fired_oneoff": True,
                                                    "oneoff": [{"state": "game",
                                                                "date": self.ONE_DAY,
                                                                "start": "14:00"}]}},
                              config_path=str(self.path))

        fired = await scheduler.check_schedule(datetime(2026, 9, 22, 14, 0, 3))

        self.assertEqual(len(fired), 1)
        self.assertEqual(bus.events, [], "日程不往 bus 推文本（T12-4）")
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("14:00", text)
        self.assertTrue(Path(str(self.path) + ".bak").is_file())
        self.assertEqual(len(scheduler.recent_fired()), 1, "事实照记")

        # 删掉之后"重新起一个进程"再装载: 那条不会再被装载, 也就不会再触发。
        # ⚠ `load_config` **有缓存**（一个进程读一次，生产上这是对的行为）——
        #   要模拟"重启后再读"必须清缓存, 否则读到的还是删之前那份。
        cfg = self._point_config_dir_at_tmp()
        cfg.clear_cache()
        scheduler2 = Scheduler(state=StateMachine(), bus=FakeBus(),
                               config=cfg.load_config("config"), config_path=str(self.path))
        self.assertEqual(len(scheduler2.events), 0)

    async def test_default_is_off(self):
        self._write(remove_fired=False)
        scheduler = Scheduler(state=StateMachine(), bus=FakeBus(),
                              config={"scheduler": {"oneoff": [{"state": "game",
                                                                "date": self.ONE_DAY,
                                                                "start": "14:00"}]}},
                              config_path=str(self.path))
        self.assertFalse(scheduler.remove_fired_oneoff)
        await scheduler.check_schedule(datetime(2026, 9, 22, 14, 0, 3))
        self.assertIn("14:00", self.path.read_text(encoding="utf-8"))

    async def test_recurring_is_never_removed(self):
        self._write(remove_fired=True, oneoff=False, recurring=True)
        scheduler = Scheduler(state=StateMachine(), bus=FakeBus(),
                              config={"scheduler": {"remove_fired_oneoff": True,
                                                    "recurring": [{"state": "sleep",
                                                                   "start": "09:00"}]}},
                              config_path=str(self.path))
        await scheduler.check_schedule(datetime(2026, 9, 22, 9, 0, 3))
        self.assertIn("09:00", self.path.read_text(encoding="utf-8"))

    async def test_write_failure_does_not_break_the_fire(self):
        self._write(remove_fired=True)
        if os.name == "nt" or IS_ROOT:
            self.skipTest("只读目录在 Windows / root 下拦不住")
        scheduler = Scheduler(state=StateMachine(), bus=FakeBus(),
                              config={"scheduler": {"remove_fired_oneoff": True,
                                                    "oneoff": [{"state": "game",
                                                                "date": self.ONE_DAY,
                                                                "start": "14:00"}]}},
                              config_path=str(self.path))
        os.chmod(str(self.tmp), stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(os.chmod, str(self.tmp), stat.S_IRWXU)

        fired = await scheduler.check_schedule(datetime(2026, 9, 22, 14, 0, 3))

        self.assertEqual(len(fired), 1, "写不了配置也不能影响这次触发")
        self.assertEqual(scheduler.stats["triggers"], 1)

    async def test_without_a_config_path_nothing_happens(self):
        self._write(remove_fired=True)
        scheduler = Scheduler(state=StateMachine(), bus=FakeBus(),
                              config={"scheduler": {"remove_fired_oneoff": True,
                                                    "oneoff": [{"state": "game",
                                                                "date": self.ONE_DAY,
                                                                "start": "14:00"}]}},
                              config_path=None)
        fired = await scheduler.check_schedule(datetime(2026, 9, 22, 14, 0, 3))
        self.assertEqual(len(fired), 1)
        self.assertIn("14:00", self.path.read_text(encoding="utf-8"))

    async def test_non_bool_switch_is_a_warning_and_means_off(self):
        scheduler = Scheduler(state=StateMachine(), bus=FakeBus(),
                              config={"scheduler": {"remove_fired_oneoff": "yes"}})
        self.assertFalse(scheduler.remove_fired_oneoff)
        self.assertTrue(any("remove_fired_oneoff" in w for w in scheduler.warnings))

    async def test_unparsable_start_never_matches(self):
        """配置里那条 start 坏掉时 -> 匹配不上 -> 不删（方向安全）。"""
        from agent.core.scheduler import parse_clock
        self.assertRaises(SchedulerError, parse_clock, "坏的")
        self._write(remove_fired=True)
        self.path.write_text("scheduler:\n  remove_fired_oneoff: true\n"
                             "  oneoff:\n    - state: game\n"
                             "      date: 2026-09-22\n      start: \"14:00\"\n",
                             encoding="utf-8")
        scheduler = Scheduler(state=StateMachine(), bus=FakeBus(),
                              config={"scheduler": {"remove_fired_oneoff": True,
                                                    "oneoff": [{"state": "game",
                                                                "date": self.ONE_DAY,
                                                                "start": "14:00"}]}},
                              config_path=str(self.path))
        # 把文件里那条的状态改掉 -> 匹配不上 -> 文件不动
        self.path.write_text(self.path.read_text(encoding="utf-8").replace("state: game",
                                                                          "state: study"),
                             encoding="utf-8")
        before = self.path.read_text(encoding="utf-8")
        fired = await scheduler.check_schedule(datetime(2026, 9, 22, 14, 0, 3))
        self.assertEqual(len(fired), 1)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before, "对不上就不许删")


if __name__ == "__main__":
    unittest.main(verbosity=2)
