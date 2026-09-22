#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_schedule_config.py — 触发后删掉那条一次性日程（R3）

跑法:
    python tests/test_schedule_config.py

三层：
  1. **文本级**：`remove_oneoff_entry()` 在各种 YAML 写法下的行为 —— 只删属于那条的行，
     其余**逐字节**不变；不认识的写法（flow 风格）给理由、不改文件
  2. **文件级**：`remove_fired_oneoff()` 落盘 + `.bak`；没匹配上就什么都不动；
     写盘失败原样抛 OSError（调用方记 WARNING）
  3. **Scheduler 集成**：开关打开/关闭、只动 oneoff、失败不影响触发

⚠ 这里大量用"逐字节比对"：这类手术最容易出的问题是"删对了、但顺手改了别的行"。
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


def matches_exactly(title, date_iso, start):
    """测试用匹配器：按字符串比（Scheduler 里那份会归一化时钟/日期）。"""
    def matches(fields):
        return (fields.get("title") == title
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
        "    - title: 每天喝水\n"
        "      start: \"10:00\"\n"
        "  oneoff:\n"
        "    - title: 项目评审\n"
        "      date: 2026-09-22\n"
        "      start: \"14:00\"\n"
        "      end: \"15:30\"\n"
        "    - title: 体检\n"
        "      date: 2026-10-08\n"
        "      start: \"08:00\"\n"
    )

    def test_removes_the_first_item_and_keeps_everything_else(self):
        new_text, why = schedule_config.remove_oneoff_entry(
            self.BODY, matches_exactly("项目评审", "2026-09-22", "14:00"))
        self.assertEqual(why, "")
        self.assertEqual(
            new_text,
            "scheduler:\n"
            "  interval_min: 1\n"
            "  recurring:\n"
            "    - title: 每天喝水\n"
            "      start: \"10:00\"\n"
            "  oneoff:\n"
            "    - title: 体检\n"
            "      date: 2026-10-08\n"
            "      start: \"08:00\"\n")

    def test_removes_the_last_item(self):
        new_text, why = schedule_config.remove_oneoff_entry(
            self.BODY, matches_exactly("体检", "2026-10-08", "08:00"))
        self.assertEqual(why, "")
        self.assertTrue(new_text.endswith(
            "  oneoff:\n"
            "    - title: 项目评审\n"
            "      date: 2026-09-22\n"
            "      start: \"14:00\"\n"
            "      end: \"15:30\"\n"))

    def test_removing_the_only_item_leaves_an_empty_sequence(self):
        body = "oneoff:\n  - title: 只有这条\n    date: 2026-09-22\n    start: \"14:00\"\n"
        new_text, why = schedule_config.remove_oneoff_entry(
            body, matches_exactly("只有这条", "2026-09-22", "14:00"))
        self.assertEqual(why, "")
        self.assertEqual(new_text, "oneoff: []\n")   # 不留一个空值看着像写错了

    def test_top_level_layout_works_too(self):
        body = ("oneoff:\n"
                "  - title: 甲\n    date: 2026-09-22\n    start: \"14:00\"\n"
                "  - title: 乙\n    date: 2026-09-23\n    start: \"14:00\"\n")
        new_text, why = schedule_config.remove_oneoff_entry(
            body, matches_exactly("甲", "2026-09-22", "14:00"))
        self.assertEqual(why, "")
        self.assertEqual(new_text,
                         "oneoff:\n"
                         "  - title: 乙\n    date: 2026-09-23\n    start: \"14:00\"\n")

    def test_comment_between_keys_goes_with_the_entry(self):
        body = ("oneoff:\n"
                "  - title: 甲\n"
                "    # 这条要提前三天准备\n"
                "    date: 2026-09-22\n"
                "    start: \"14:00\"\n"
                "  - title: 乙\n    date: 2026-09-23\n    start: \"14:00\"\n")
        new_text, why = schedule_config.remove_oneoff_entry(
            body, matches_exactly("甲", "2026-09-22", "14:00"))
        self.assertEqual(why, "")
        self.assertNotIn("提前三天准备", new_text)
        self.assertEqual(new_text,
                         "oneoff:\n"
                         "  - title: 乙\n    date: 2026-09-23\n    start: \"14:00\"\n")

    def test_comment_before_the_next_entry_stays(self):
        """紧贴在**下一条**前面的注释留着 —— 宁可有行悬空注释, 也不误删信息。"""
        body = ("oneoff:\n"
                "  - title: 甲\n    date: 2026-09-22\n    start: \"14:00\"\n"
                "  # 乙是每周例会的替代品\n"
                "  - title: 乙\n    date: 2026-09-23\n    start: \"14:00\"\n")
        new_text, why = schedule_config.remove_oneoff_entry(
            body, matches_exactly("甲", "2026-09-22", "14:00"))
        self.assertEqual(why, "")
        self.assertIn("# 乙是每周例会的替代品", new_text)
        self.assertEqual(new_text,
                         "oneoff:\n"
                         "  # 乙是每周例会的替代品\n"
                         "  - title: 乙\n    date: 2026-09-23\n    start: \"14:00\"\n")

    def test_blank_lines_between_entries_stay(self):
        body = ("oneoff:\n"
                "  - title: 甲\n    date: 2026-09-22\n    start: \"14:00\"\n"
                "\n"
                "  - title: 乙\n    date: 2026-09-23\n    start: \"14:00\"\n")
        new_text, _ = schedule_config.remove_oneoff_entry(
            body, matches_exactly("甲", "2026-09-22", "14:00"))
        self.assertTrue(new_text.startswith("oneoff:\n\n"))
        self.assertIn("乙", new_text)

    def test_crlf_is_preserved(self):
        body = ("oneoff:\r\n"
                "  - title: 甲\r\n    date: 2026-09-22\r\n    start: \"14:00\"\r\n"
                "  - title: 乙\r\n    date: 2026-09-23\r\n    start: \"14:00\"\r\n")
        new_text, why = schedule_config.remove_oneoff_entry(
            body, matches_exactly("甲", "2026-09-22", "14:00"))
        self.assertEqual(why, "")
        self.assertEqual(new_text,
                         "oneoff:\r\n"
                         "  - title: 乙\r\n    date: 2026-09-23\r\n    start: \"14:00\"\r\n")
        self.assertNotIn("\n", new_text.replace("\r\n", ""))

    def test_trailing_comment_on_a_value_is_ignored(self):
        body = ("oneoff:\n"
                "  - title: 甲   # 下午那场\n"
                "    date: 2026-09-22   # 周二\n"
                "    start: \"14:00\"  # 别迟到\n")
        new_text, why = schedule_config.remove_oneoff_entry(
            body, matches_exactly("甲", "2026-09-22", "14:00"))
        self.assertEqual(why, "")
        self.assertEqual(new_text, "oneoff: []\n")

    def test_flow_style_is_refused_with_a_reason(self):
        body = 'oneoff: [{title: 甲, date: 2026-09-22, start: "14:00"}]\n'
        new_text, why = schedule_config.remove_oneoff_entry(
            body, matches_exactly("甲", "2026-09-22", "14:00"))
        self.assertEqual(new_text, body, "不认识的写法不许改文件")
        self.assertIn("flow 风格", why)

    def test_empty_sequence_is_not_found(self):
        body = "oneoff: []\n"
        new_text, why = schedule_config.remove_oneoff_entry(
            body, matches_exactly("甲", "2026-09-22", "14:00"))
        self.assertEqual(new_text, body)
        self.assertIn("没有匹配", why)

    def test_missing_oneoff_key_is_not_found(self):
        body = "llm:\n  mode: disabled\n"
        new_text, why = schedule_config.remove_oneoff_entry(
            body, matches_exactly("甲", "2026-09-22", "14:00"))
        self.assertEqual(new_text, body)
        self.assertIn("没有匹配", why)

    def test_duplicate_titles_differ_by_date(self):
        body = ("oneoff:\n"
                "  - title: 复盘\n    date: 2026-09-22\n    start: \"20:00\"\n"
                "  - title: 复盘\n    date: 2026-09-23\n    start: \"20:00\"\n")
        new_text, why = schedule_config.remove_oneoff_entry(
            body, matches_exactly("复盘", "2026-09-23", "20:00"))
        self.assertEqual(why, "")
        self.assertEqual(new_text,
                         "oneoff:\n"
                         "  - title: 复盘\n    date: 2026-09-22\n    start: \"20:00\"\n")

    def test_recurring_with_the_same_title_is_untouched(self):
        body = ("recurring:\n"
                "  - title: 站会\n    days: [mon]\n    start: \"09:30\"\n"
                "oneoff:\n"
                "  - title: 站会\n    date: 2026-09-22\n    start: \"09:30\"\n")
        new_text, why = schedule_config.remove_oneoff_entry(
            body, matches_exactly("站会", "2026-09-22", "09:30"))
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
            self.BODY, matches_exactly("项目评审", "2026-09-22", "14:00"))
        self.assertEqual(why, "")
        self.assertEqual(span, (6, 10))       # 6..9 行（0 基）

    def test_find_oneoff_block_reports_why_not(self):
        span, why = schedule_config.find_oneoff_block(
            self.BODY, matches_exactly("不存在", "2026-09-22", "14:00"))
        self.assertIsNone(span)
        self.assertIn("没有匹配", why)


# ===========================================================================
#  2. 文件级
# ===========================================================================
class TestRemoveFiredOneoffFile(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="schedcfg-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.path = self.tmp / "config.yaml"
        self.original = ("scheduler:\n"
                         "  oneoff:\n"
                         "    - title: 项目评审\n"
                         "      date: 2026-09-22\n"
                         "      start: \"14:00\"\n"
                         "    - title: 体检\n"
                         "      date: 2026-10-08\n"
                         "      start: \"08:00\"\n")
        self.path.write_text(self.original, encoding="utf-8")

    def test_writes_the_new_text_and_a_backup(self):
        removed = schedule_config.remove_fired_oneoff(
            self.path, matches_exactly("项目评审", "2026-09-22", "14:00"))
        self.assertTrue(removed)
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("项目评审", text)
        self.assertIn("体检", text)
        backup = Path(str(self.path) + ".bak")
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_text(encoding="utf-8"), self.original)

    def test_no_match_touches_nothing(self):
        removed = schedule_config.remove_fired_oneoff(
            self.path, matches_exactly("不存在", "2026-09-22", "14:00"))
        self.assertFalse(removed)
        self.assertEqual(self.path.read_text(encoding="utf-8"), self.original)
        self.assertFalse(Path(str(self.path) + ".bak").exists(), "没删就别留 .bak")

    def test_missing_file_raises_oserror(self):
        with self.assertRaises(OSError):
            schedule_config.remove_fired_oneoff(
                self.tmp / "nope.yaml", matches_exactly("甲", "2026-09-22", "14:00"))

    @unittest.skipIf(os.name == "nt", "只读目录在 Windows 上不拦 root/管理员")
    @unittest.skipIf(IS_ROOT, "root 无视目录权限（板端就是 root）")
    def test_read_only_directory_raises_oserror(self):
        os.chmod(str(self.tmp), stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(os.chmod, str(self.tmp), stat.S_IRWXU)
        with self.assertRaises(OSError):
            schedule_config.remove_fired_oneoff(
                self.path, matches_exactly("项目评审", "2026-09-22", "14:00"))


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
            lines += ["  recurring:", "    - title: 每天喝水", "      start: \"09:00\""]
        if oneoff:
            lines += ["  oneoff:",
                      "    - title: 项目评审",
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
                                                    "oneoff": [{"title": "项目评审",
                                                                "date": self.ONE_DAY,
                                                                "start": "14:00"}]}},
                              config_path=str(self.path))

        fired = await scheduler.check_schedule(datetime(2026, 9, 22, 14, 0, 3))

        self.assertEqual(len(fired), 1)
        self.assertEqual(len(bus.events), 1, "触发本身照做")
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("项目评审", text)
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
                              config={"scheduler": {"oneoff": [{"title": "项目评审",
                                                                "date": self.ONE_DAY,
                                                                "start": "14:00"}]}},
                              config_path=str(self.path))
        self.assertFalse(scheduler.remove_fired_oneoff)
        await scheduler.check_schedule(datetime(2026, 9, 22, 14, 0, 3))
        self.assertIn("项目评审", self.path.read_text(encoding="utf-8"))

    async def test_recurring_is_never_removed(self):
        self._write(remove_fired=True, oneoff=False, recurring=True)
        scheduler = Scheduler(state=StateMachine(), bus=FakeBus(),
                              config={"scheduler": {"remove_fired_oneoff": True,
                                                    "recurring": [{"title": "每天喝水",
                                                                   "start": "09:00"}]}},
                              config_path=str(self.path))
        await scheduler.check_schedule(datetime(2026, 9, 22, 9, 0, 3))
        self.assertIn("每天喝水", self.path.read_text(encoding="utf-8"))

    async def test_write_failure_does_not_break_the_fire(self):
        self._write(remove_fired=True)
        if os.name == "nt" or IS_ROOT:
            self.skipTest("只读目录在 Windows / root 下拦不住")
        scheduler = Scheduler(state=StateMachine(), bus=FakeBus(),
                              config={"scheduler": {"remove_fired_oneoff": True,
                                                    "oneoff": [{"title": "项目评审",
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
                                                    "oneoff": [{"title": "项目评审",
                                                                "date": self.ONE_DAY,
                                                                "start": "14:00"}]}},
                              config_path=None)
        fired = await scheduler.check_schedule(datetime(2026, 9, 22, 14, 0, 3))
        self.assertEqual(len(fired), 1)
        self.assertIn("项目评审", self.path.read_text(encoding="utf-8"))

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
                             "  oneoff:\n    - title: 项目评审\n"
                             "      date: 2026-09-22\n      start: \"14:00\"\n",
                             encoding="utf-8")
        scheduler = Scheduler(state=StateMachine(), bus=FakeBus(),
                              config={"scheduler": {"remove_fired_oneoff": True,
                                                    "oneoff": [{"title": "项目评审",
                                                                "date": self.ONE_DAY,
                                                                "start": "14:00"}]}},
                              config_path=str(self.path))
        # 把文件里那条的 title 改掉 -> 匹配不上 -> 文件不动
        self.path.write_text(self.path.read_text(encoding="utf-8").replace("项目评审", "别的"),
                             encoding="utf-8")
        before = self.path.read_text(encoding="utf-8")
        fired = await scheduler.check_schedule(datetime(2026, 9, 22, 14, 0, 3))
        self.assertEqual(len(fired), 1)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before, "对不上就不许删")


if __name__ == "__main__":
    unittest.main(verbosity=2)
