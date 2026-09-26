#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`agent/tools/schedule.py` + `Runtime.schedule_*` 的单测（T12-6）。

跑法:
    python tests/test_schedule_tool.py

三层：
  1. **归一化**：同义动词 / 中文状态 / 各种时间与星期写法 —— **只做等价改写, 不猜内容**
  2. **工具**：schema / 只在 IDLE+STUDY / 三个 action 的分派与"缺参数就如实说"
  3. **Runtime**（真临时配置 + 真 `Scheduler`）：
     · add 写进文件 -> 立刻热重载（不用重启 Agent）-> list 看得见 -> remove 删干净;
     · 一次性日程写在过去**拒绝**并把今天的日期给出来（系统提示里没有时钟）;
     · 同时刻有多条**不猜**，把候选摆出来让调用方带上 days/date;
     · 模板（`config.example.yaml`）不许写、调度器没起来只写不载、坏配置如实报。
"""
import logging
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.core.state_machine import State, StateMachine                    # noqa: E402
from agent.core.tool_router import ToolRouter                               # noqa: E402
from agent.tools import schedule as tool_module                             # noqa: E402

logging.disable(logging.CRITICAL)

BODY = (
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


def _services():
    calls = {"add": [], "list": 0, "remove": []}

    def add(values):
        calls["add"].append(values)
        return {"ok": True, "entry": values}

    def listing():
        calls["list"] += 1
        return {"ok": True, "count": 0, "entries": []}

    def remove(values):
        calls["remove"].append(values)
        return {"ok": True, "removed": values}

    return {"schedule_add": add, "schedule_list": listing, "schedule_remove": remove}, calls


# ===========================================================================
#  1. 归一化
# ===========================================================================
class TestNormalize(unittest.TestCase):
    def test_action_synonyms(self):
        for given, want in ((" ADD ", "add"), ("create", "add"), ("set", "add"),
                            ("schedule", "add"), ("new", "add"),
                            ("show", "list"), ("query", "list"), ("list", "list"),
                            ("delete", "remove"), ("cancel", "remove"), ("del", "remove")):
            with self.subTest(given=given):
                self.assertEqual(tool_module.normalize({"action": given})["action"], want)

    def test_action_is_inferred_from_the_fields(self):
        self.assertEqual(tool_module.normalize({"start": "09:00"})["action"], "add")
        self.assertEqual(tool_module.normalize({"state": "study"})["action"], "list")
        self.assertEqual(tool_module.normalize({}).get("action"), None)

    def test_chinese_and_upper_case_states(self):
        for given, want in (("学习", "study"), ("STUDY", "study"), ("Study模式", "study"),
                            ("睡眠", "sleep"), ("睡觉", "sleep"), ("空闲", "idle"),
                            ("待机", "idle"), ("游戏", "game")):
            with self.subTest(given=given):
                self.assertEqual(tool_module.normalize({"state": given})["state"], want)

    def test_unknown_state_is_left_alone(self):
        # 不猜: 认不出的状态原样留着, 由 Runtime 的语义校验如实报错
        self.assertEqual(tool_module.normalize({"state": "banana"})["state"], "banana")

    def test_clock_forms(self):
        for given, want in (("9:30", "09:30"), ("09:30", "09:30"), ("0930", "09:30"),
                            ("930", "09:30"), ("9：30", "09:30"),
                            ("9点30", "09:30"), ("9点", "09:00"), ("9点半", "09:30"),
                            ("23时5分", "23:05"), (" 7:05 ", "07:05")):
            with self.subTest(given=given):
                self.assertEqual(tool_module.normalize({"start": given})["start"], want)

    def test_broken_clock_is_left_for_the_validation(self):
        self.assertEqual(tool_module.normalize({"start": "九点"})["start"], "九点")

    def test_start_aliases(self):
        self.assertEqual(tool_module.normalize({"time": "9:00"})["start"], "09:00")
        self.assertEqual(tool_module.normalize({"at": "9:00"})["start"], "09:00")
        # 正式键已经给了就不搬（也不留一个多余的键）
        out = tool_module.normalize({"start": "10:00", "time": "9:00"})
        self.assertEqual(out["start"], "10:00")
        self.assertNotIn("time", out)

    def test_days_forms(self):
        for given, want in (("mon,wed", ["mon", "wed"]), ("周一 周三", ["mon", "wed"]),
                            (["mon", "wed"], ["mon", "wed"]), (["MON"], ["mon"]),
                            ("工作日", ["mon", "tue", "wed", "thu", "fri"]),
                            ("周末", ["sat", "sun"]), ([1, 3], ["mon", "wed"]),
                            ("星期一", ["mon"]), ("礼拜天", ["sun"])):
            with self.subTest(given=given):
                self.assertEqual(tool_module.normalize({"days": given})["days"], want)

    def test_every_day_is_an_empty_list(self):
        for given in ("每天", "daily", "everyday", "天天"):
            with self.subTest(given=given):
                self.assertEqual(tool_module.normalize({"days": given})["days"],
                                 list(tool_module.WEEKDAYS))

    def test_days_are_deduplicated_and_ordered_as_written(self):
        self.assertEqual(tool_module.normalize({"days": ["wed", "mon", "wed"]})["days"],
                         ["wed", "mon"])

    def test_date_separators(self):
        for given, want in (("2026/09/22", "2026-09-22"), ("2026.09.22", "2026-09-22"),
                            ("2026年9月22日", "2026-09-22"), ("2026-9-2", "2026-09-02")):
            with self.subTest(given=given):
                self.assertEqual(tool_module.normalize({"date": given})["date"], want)

    def test_empty_literals_are_treated_as_missing(self):
        out = tool_module.normalize({"action": "add", "state": "none", "start": "无",
                                     "days": "", "date": "null"})
        self.assertNotIn("state", out)
        self.assertNotIn("start", out)
        self.assertNotIn("days", out)
        self.assertNotIn("date", out)

    def test_the_input_is_not_mutated(self):
        given = {"action": "ADD", "state": "学习", "start": "9:30"}
        tool_module.normalize(given)
        self.assertEqual(given, {"action": "ADD", "state": "学习", "start": "9:30"})


# ===========================================================================
#  2. 工具
# ===========================================================================
class TestTool(unittest.TestCase):
    def test_schema(self):
        tool = tool_module.build(_services()[0])
        self.assertEqual(tool.name, "set_schedule")
        self.assertEqual(tool.schema["required"], ["action"])
        self.assertEqual(list(tool.schema["properties"]["action"]["enum"]),
                         list(tool_module.ACTIONS))
        self.assertEqual(list(tool.schema["properties"]["state"]["enum"]),
                         list(tool_module.STATES))
        self.assertFalse(tool.schema["additionalProperties"])
        self.assertEqual(sorted(tool.schema["properties"]),
                         ["action", "date", "days", "start", "state"])

    def test_allowed_in_idle_and_study_only(self):
        tool = tool_module.build(_services()[0])
        self.assertEqual(tool.allowed_states, {State.IDLE, State.STUDY})

    def test_description_tells_the_model_about_the_missing_clock(self):
        text = tool_module.DESCRIPTION
        self.assertIn("list", text)
        self.assertIn("别猜", text)
        self.assertIn("sleep", text)

    def test_missing_services_means_no_tool(self):
        self.assertIsNone(tool_module.build({}))
        services, _ = _services()
        services.pop("schedule_remove")
        self.assertIsNone(tool_module.build(services))
        services["schedule_list"] = "not callable"
        self.assertIsNone(tool_module.build(services))

    def test_no_timeout_of_its_own(self):
        # 本地写盘 + 重读, 毫秒级 —— 不需要像 bilibili/music 那样自己声明更长的超时
        # （None = 用路由的默认值 `DEFAULT_TIMEOUT_S`, 见 ToolRouter.execute）
        tool = tool_module.build(_services()[0])
        self.assertIsNone(tool.timeout_s)

    def test_list_dispatches(self):
        services, calls = _services()
        result = tool_module.build(services).handler(action="list")
        self.assertTrue(result["ok"])
        self.assertEqual(calls["list"], 1)

    def test_add_forwards_the_identity_fields(self):
        services, calls = _services()
        tool_module.build(services).handler(action="add", state="study", start="09:30",
                                            days=["mon", "wed"])
        self.assertEqual(calls["add"], [{"state": "study", "start": "09:30",
                                         "days": ["mon", "wed"]}])

    def test_remove_forwards_the_identity_fields(self):
        services, calls = _services()
        tool_module.build(services).handler(action="remove", state="sleep", start="23:00")
        self.assertEqual(calls["remove"], [{"state": "sleep", "start": "23:00"}])

    def test_missing_state_or_start_is_said_plainly(self):
        services, calls = _services()
        tool = tool_module.build(services)
        for args, want in (({"action": "add", "start": "09:00"}, "state"),
                           ({"action": "add", "state": "study"}, "start"),
                           ({"action": "remove", "state": "study"}, "start")):
            with self.subTest(args=args):
                result = tool.handler(**args)
                self.assertFalse(result["ok"])
                self.assertIn(want, result["error"])
        self.assertEqual(calls["add"], [])
        self.assertEqual(calls["remove"], [])

    def test_days_and_date_together_are_refused(self):
        services, calls = _services()
        result = tool_module.build(services).handler(action="add", state="study",
                                                     start="09:00", days=["mon"],
                                                     date="2026-09-26")
        self.assertFalse(result["ok"])
        self.assertIn("不能都给", result["tell_user"])
        self.assertEqual(calls["add"], [])

    def test_unknown_action_lists_the_available_ones(self):
        services, calls = _services()
        result = tool_module.build(services).handler(action="dance")
        self.assertFalse(result["ok"])
        for name in tool_module.ACTIONS:
            self.assertIn(name, result["tell_user"])
        self.assertEqual(calls["add"], [])

    def test_entry_failure_is_reported_not_raised(self):
        def boom(values):
            raise RuntimeError("配置写不进去")

        services, _ = _services()
        services["schedule_add"] = boom
        result = tool_module.build(services).handler(action="add", state="study",
                                                     start="09:00")
        self.assertFalse(result["ok"])
        self.assertIn("配置写不进去", result["tell_user"])


class TestThroughTheRouter(unittest.IsolatedAsyncioTestCase):
    def _router(self, state):
        services, calls = _services()
        machine = StateMachine()
        if state is not State.IDLE:
            machine.transition(state, "test")
        router = ToolRouter(state_provider=machine, services=services)
        router.register(tool_module.build(services))
        return router, calls

    async def test_allowed_in_idle_and_study(self):
        for state in (State.IDLE, State.STUDY):
            with self.subTest(state=state.value):
                router, calls = self._router(state)
                result = await router.execute("set_schedule",
                                             {"action": "add", "state": "study",
                                              "start": "9点30", "days": "周一"})
                self.assertTrue(result["ok"], result)
                self.assertEqual(calls["add"], [{"state": "study", "start": "09:30",
                                                 "days": ["mon"]}])

    async def test_refused_in_sleep_and_game(self):
        for state in (State.SLEEP, State.GAME):
            with self.subTest(state=state.value):
                router, calls = self._router(state)
                result = await router.execute("set_schedule", {"action": "list"})
                self.assertFalse(result["ok"])
                self.assertIn("not allowed", result["error"])
                self.assertEqual(calls["list"], 0, "被拒时 handler 一次都不该跑")


# ===========================================================================
#  3. Runtime（真临时配置 + 真 Scheduler）
# ===========================================================================
class RuntimeScheduleBase(unittest.IsolatedAsyncioTestCase):
    BODY = BODY

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sched-tool-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.path = self.tmp / "config.yaml"
        self.path.write_text(self.BODY, encoding="utf-8")

    def _runtime(self, path=None, with_scheduler=True, config=None):
        from agent.io import ChatInputBus
        from agent.main import Runtime

        target = path or self.path
        config = config if config is not None else {
            "llm": {"mode": "disabled"},
            "scheduler": {"interval_min": 1, "recurring": [], "oneoff": []},
            "gui": {"theme": "grey"},
        }
        runtime = Runtime(config=config, config_path_used=target,
                          start_native=False, start_terminal=False,
                          log=logging.getLogger("test.schedule.tool"))
        # ⚠ `Runtime.start()` 才会建这两样（这里不跑整个启动流程, 所以自己给上）——
        #   `Scheduler` 缺 state/bus 会直接抛, 那条错报在哪儿都看不出"是测试没搭好"。
        runtime.state = StateMachine()
        runtime.bus = ChatInputBus()
        if with_scheduler:
            from agent.core import Scheduler

            runtime.scheduler = Scheduler(state=runtime.state, bus=runtime.bus,
                                          config=config, config_path=target)
        return runtime

    def _text(self, path=None):
        return (path or self.path).read_text(encoding="utf-8")


class TestRuntimeSchedule(RuntimeScheduleBase):
    def test_add_writes_the_file_and_hot_reloads(self):
        runtime = self._runtime()
        self.assertEqual(len(runtime.scheduler.events), 0)

        result = runtime.schedule_add({"state": "study", "start": "09:30",
                                       "days": ["mon", "wed"]})

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["hot"], "写完要立刻热重载")
        self.assertEqual(result["schedules"], 1)
        self.assertIn("    - state: study\n      days: [mon, wed]\n      start: \"09:30\"\n",
                      self._text())
        self.assertEqual(len(runtime.scheduler.events), 1, "调度器里也立刻有了")
        self.assertEqual(runtime.scheduler.events[0].state, State.STUDY)
        self.assertEqual(runtime.scheduler.events[0].days, {0, 2})
        self.assertTrue(Path(str(self.path) + ".bak").is_file(), "留一份 .bak")
        self.assertEqual(self._text().count("scheduler:"), 1, "不许出现第二个 scheduler 段")
        self.assertTrue(self._text().endswith("gui:\n  theme: grey\n"), "后面的段不许动")

    def test_add_oneoff_with_a_date(self):
        runtime = self._runtime()
        result = runtime.schedule_add({"state": "sleep", "start": "23:00",
                                       "date": "2099-01-02"})
        self.assertTrue(result["ok"], result)
        self.assertIn('      date: "2099-01-02"\n', self._text())
        self.assertEqual(runtime.scheduler.events[0].on.isoformat(), "2099-01-02")

    def test_list_shows_entries_and_todays_date(self):
        runtime = self._runtime()
        runtime.schedule_add({"state": "study", "start": "09:30", "days": ["mon"]})
        result = runtime.schedule_list()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["entries"][0]["state"], "study")
        self.assertEqual(result["entries"][0]["start"], "09:30")
        self.assertEqual(result["entries"][0]["days"], ["mon"])
        self.assertEqual(result["entries"][0]["kind"], "recurring")
        # 系统提示里没有时钟: list 必须把"今天几号"给出来
        from datetime import date

        self.assertEqual(result["today"], date.today().isoformat())
        self.assertEqual(len(result["now"]), 16)
        self.assertIn(result["weekday"], tool_module.WEEKDAYS)

    def test_list_reports_entries_it_cannot_read(self):
        self.path.write_text("scheduler:\n  recurring:\n    - start: \"08:30\"\n",
                            encoding="utf-8")
        runtime = self._runtime(with_scheduler=False)
        result = runtime.schedule_list()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["count"], 0)
        self.assertEqual(len(result["problems"]), 1)
        self.assertIn("没有 state", result["problems"][0])
        self.assertIn("note", result, "读不出来的条目要如实说")

    def test_add_is_idempotent(self):
        runtime = self._runtime()
        runtime.schedule_add({"state": "study", "start": "09:30", "days": ["mon"]})
        before = self._text()
        again = runtime.schedule_add({"state": "study", "start": "09:30", "days": ["mon"]})
        self.assertTrue(again["ok"])
        self.assertTrue(again["already"])
        self.assertIn("已经有一条一样的了", again["note"])
        self.assertEqual(self._text(), before, "查重命中一个字节都不改")

    def test_a_past_oneoff_is_refused_with_todays_date(self):
        runtime = self._runtime()
        result = runtime.schedule_add({"state": "sleep", "start": "23:00",
                                       "date": "2020-01-01"})
        self.assertFalse(result["ok"])
        from datetime import date

        self.assertIn(date.today().isoformat(), result["tell_user"])
        self.assertNotIn("2020-01-01", self._text(), "死日程不许写进去")

    def test_bad_state_is_refused_by_the_real_semantics(self):
        runtime = self._runtime()
        result = runtime.schedule_add({"state": "banana", "start": "09:00"})
        self.assertFalse(result["ok"])
        self.assertEqual(runtime.scheduler.events, [])
        self.assertNotIn("banana", self._text())

    def test_bad_start_is_refused(self):
        runtime = self._runtime()
        result = runtime.schedule_add({"state": "study", "start": "25:00"})
        self.assertFalse(result["ok"])
        self.assertIn("25:00", result["tell_user"] + result["error"])

    def test_remove_by_state_and_start(self):
        runtime = self._runtime()
        runtime.schedule_add({"state": "study", "start": "09:30"})
        result = runtime.schedule_remove({"state": "study", "start": "09:30"})
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["hot"])
        self.assertEqual(result["kind"], "recurring")
        self.assertEqual(self._text(), self.BODY, "删干净后逐字节回到原文")
        self.assertEqual(runtime.scheduler.events, [])

    def test_remove_does_not_guess_between_same_time_entries(self):
        runtime = self._runtime()
        runtime.schedule_add({"state": "study", "start": "09:30"})
        runtime.schedule_add({"state": "study", "start": "09:30", "days": ["mon"]})
        before = self._text()

        result = runtime.schedule_remove({"state": "study", "start": "09:30"})

        self.assertFalse(result["ok"])
        self.assertIn("2 条", result["tell_user"])
        self.assertEqual(len(result["entries"]), 2, "把候选摆出来")
        self.assertEqual(self._text(), before, "不猜 —— 一条都不许删")

    def test_remove_with_days_picks_the_right_one(self):
        runtime = self._runtime()
        runtime.schedule_add({"state": "study", "start": "09:30"})
        runtime.schedule_add({"state": "study", "start": "09:30", "days": ["mon"]})
        result = runtime.schedule_remove({"state": "study", "start": "09:30",
                                          "days": ["mon"]})
        self.assertTrue(result["ok"], result)
        text = self._text()
        self.assertNotIn("days: [mon]", text)
        self.assertIn("- state: study\n      start: \"09:30\"\n", text)

    def test_remove_nothing_found_lists_what_is_there(self):
        runtime = self._runtime()
        runtime.schedule_add({"state": "study", "start": "09:30"})
        result = runtime.schedule_remove({"state": "game", "start": "09:30"})
        self.assertFalse(result["ok"])
        self.assertIn("没找到", result["tell_user"])
        self.assertIn("09:30", result["tell_user"], "把手上的日程报出来")

    def test_remove_without_a_scheduler_still_writes_the_file(self):
        runtime = self._runtime(with_scheduler=False)
        runtime.schedule_add({"state": "study", "start": "09:30"})
        result = runtime.schedule_remove({"state": "study", "start": "09:30"})
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["hot"])
        self.assertEqual(self._text(), self.BODY)

    def test_without_a_scheduler_add_says_restart(self):
        runtime = self._runtime(with_scheduler=False)
        result = runtime.schedule_add({"state": "study", "start": "09:30"})
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["hot"])
        self.assertIn("调度器没在跑", result["note"])
        self.assertIn("start: \"09:30\"", self._text(), "配置照写")

    def test_template_is_never_written(self):
        example = self.tmp / "config.example.yaml"
        example.write_text(self.BODY, encoding="utf-8")
        runtime = self._runtime(path=example, with_scheduler=False)

        added = runtime.schedule_add({"state": "study", "start": "09:30"})
        removed = runtime.schedule_remove({"state": "study", "start": "09:30"})

        self.assertFalse(added["ok"])
        self.assertIn("模板", added["tell_user"])
        self.assertFalse(removed["ok"])
        self.assertIn("模板", removed["tell_user"])
        self.assertEqual(example.read_text(encoding="utf-8"), self.BODY, "模板一个字节不动")

    def test_template_can_still_be_listed(self):
        example = self.tmp / "config.example.yaml"
        example.write_text(self.BODY, encoding="utf-8")
        runtime = self._runtime(path=example, with_scheduler=False)
        result = runtime.schedule_list()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["count"], 0)

    def test_injected_config_has_no_path_so_it_says_so(self):
        runtime = self._runtime(with_scheduler=False)
        runtime.config_path_used = None
        for result in (runtime.schedule_list(),
                       runtime.schedule_add({"state": "study", "start": "09:30"}),
                       runtime.schedule_remove({"state": "study", "start": "09:30"})):
            with self.subTest(result=result):
                self.assertFalse(result["ok"])
                self.assertIn("不知道日程写在哪个文件", result["tell_user"])

    def test_flow_style_sequence_is_refused_honestly(self):
        self.path.write_text("scheduler:\n  recurring: []\n  oneoff: [{state: study, "
                             "date: 2026-09-26, start: \"09:00\"}]\n", encoding="utf-8")
        runtime = self._runtime(with_scheduler=False)
        result = runtime.schedule_add({"state": "study", "start": "09:30"})
        self.assertTrue(result["ok"], "加 recurring 不受 oneoff 的 flow 风格影响")
        blocked = runtime.schedule_add({"state": "sleep", "start": "22:00",
                                        "date": "2099-01-02"})
        self.assertFalse(blocked["ok"])
        self.assertIn("flow", blocked["tell_user"])

    def test_the_real_config_file_is_left_alone(self):
        """⚠ 这一组用例**只碰临时文件** —— 仓库/板端那份 config.yaml 一个字节都不动。"""
        real = _PROJECT_ROOT / "config" / "config.yaml"
        before = real.read_bytes() if real.is_file() else None
        runtime = self._runtime()
        runtime.schedule_add({"state": "study", "start": "09:30"})
        runtime.schedule_list()
        runtime.schedule_remove({"state": "study", "start": "09:30"})
        after = real.read_bytes() if real.is_file() else None
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main(verbosity=2)
