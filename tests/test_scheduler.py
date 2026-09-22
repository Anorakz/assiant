#!/usr/bin/env python3
"""
tests/test_scheduler.py — Scheduler 单测

运行:
    python tests/test_scheduler.py

覆盖:
  配置解析   interval_min / window_min / late_grace_min 校验; recurring/oneoff;
             parse_clock; remind_before_min; 各种写错的配置
  定时触发   到点触发一次、重复调用不重复触发、窗口外不触发、
             提前量 (remind_before_min)、oneoff 只在指定日期、
             recurring 只看配置的星期、跨天的提前量
  触发动作   状态转换 (含非法转换被记下)、发消息到 bus (source="scheduler")
  触发事实   只记真的触发过的、条数有界、拿到的是副本、on_fire 收到事实、
             回调抛异常不影响触发与去重、历史与去重集合的生命周期不同
  终端命令   整行匹配 (去首尾空白 + 大小写不敏感)、中文命令、只认 source=="terminal"、
             其它来源忽略、认不出来的文本跳过、敲两次生效两次、
             配置两种写法 (list / dict)
  生命周期   start/stop 幂等、stop 后不再触发、start 会检查时钟
  sync_time 不修改系统时间、明显不对的时钟给 warning
"""

import asyncio
import json
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.core import (  # noqa: E402
    DEFAULT_HISTORY_LIMIT,
    DEFAULT_INTERVAL_MIN,
    DEFAULT_WINDOW_MIN,
    Scheduler,
    SchedulerError,
    State,
    StateMachine,
    normalize_command,
    parse_clock,
    parse_command_config,
)
from agent.io import ChatInputBus  # noqa: E402


# ---------------------------------------------------------------------------
#  测试替身
# ---------------------------------------------------------------------------
class FakeClock:
    """可拨动的时钟。"""

    def __init__(self, moment: datetime):
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, **kwargs) -> None:
        self.moment += timedelta(**kwargs)

    def set(self, moment: datetime) -> None:
        self.moment = moment


class FakeMonotonic:
    """单调时钟: 由 sleep() 推进, 这样调度循环不用真的等。"""

    def __init__(self):
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeBus:
    """只记录 push 的假 bus (给不关心读取的用例用)。"""

    def __init__(self):
        self.events = []

    async def push(self, source, text):
        event = {"source": source, "text": text, "timestamp": 1.0 + len(self.events)}
        self.events.append(event)
        return event

    async def get(self):
        await asyncio.sleep(3600)
        raise AssertionError("不该被调用")


def make_scheduler(config=None, **kwargs):
    """常规构造: 状态机 + 假 bus + 可拨时钟。"""
    state = kwargs.pop("state", None) or StateMachine()
    bus = kwargs.pop("bus", None) or FakeBus()
    clock = kwargs.pop("clock", None) or FakeClock(datetime(2026, 9, 16, 9, 30, 0))
    scheduler = Scheduler(state=state, bus=bus, config=config or {}, clock=clock, **kwargs)
    return scheduler, state, bus, clock


# 2026-09-16 是周三 (weekday()==2)
WEDNESDAY = date(2026, 9, 16)
MONDAY = date(2026, 9, 14)


# ===========================================================================
#  parse_clock
# ===========================================================================
class TestParseClock(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(parse_clock("09:30"), (9, 30))
        self.assertEqual(parse_clock("9:30"), (9, 30))
        self.assertEqual(parse_clock("23:59"), (23, 59))
        self.assertEqual(parse_clock("00:00"), (0, 0))

    def test_whitespace_and_fullwidth_colon(self):
        self.assertEqual(parse_clock("  09：30 "), (9, 30))

    def test_compact_form(self):
        self.assertEqual(parse_clock("0930"), (9, 30))
        self.assertEqual(parse_clock("0000"), (0, 0))

    def test_invalid(self):
        for bad in ("", "9", "09:60", "24:00", "-1:00", "aa:bb", "09:30:00",
                    "09300", 930, None):
            with self.subTest(bad=bad):
                with self.assertRaises(SchedulerError):
                    parse_clock(bad)

    def test_error_is_valueerror(self):
        self.assertTrue(issubclass(SchedulerError, ValueError))


# ===========================================================================
#  配置解析
# ===========================================================================
class TestConfig(unittest.TestCase):
    def test_defaults(self):
        scheduler, _, _, _ = make_scheduler()
        self.assertEqual(scheduler.interval_min, DEFAULT_INTERVAL_MIN)
        self.assertEqual(scheduler.window_min, DEFAULT_WINDOW_MIN)
        self.assertEqual(scheduler.late_grace_min, 0)
        self.assertEqual(scheduler.events, [])
        self.assertEqual(scheduler.bindings, [])

    def test_accepts_whole_config_or_section(self):
        section = {"scheduler": {"interval_min": 5}}
        whole = {"scheduler": {"interval_min": 5}, "llm": {"mode": "disabled"}}
        self.assertEqual(make_scheduler(section)[0].interval_min, 5)
        self.assertEqual(make_scheduler(whole)[0].interval_min, 5)

    def test_interval_from_config(self):
        scheduler, _, _, _ = make_scheduler({"scheduler": {"interval_min": 15}})
        self.assertEqual(scheduler.interval_min, 15.0)

    def test_bad_interval(self):
        for bad in (0, -1, "5", None, True):
            with self.subTest(bad=bad):
                with self.assertRaises(SchedulerError):
                    make_scheduler({"scheduler": {"interval_min": bad}})

    def test_bad_window(self):
        for bad in (0, -3, "1"):
            with self.subTest(bad=bad):
                with self.assertRaises(SchedulerError):
                    make_scheduler({"scheduler": {"window_min": bad}})

    def test_bad_grace(self):
        for bad in (-1, "5", 1.5, True):
            with self.subTest(bad=bad):
                with self.assertRaises(SchedulerError):
                    make_scheduler({"scheduler": {"late_grace_min": bad}})

    def test_requires_state_and_bus(self):
        with self.assertRaises(SchedulerError):
            Scheduler(state=None, bus=FakeBus(), config={})
        with self.assertRaises(SchedulerError):
            Scheduler(state=StateMachine(), bus=None, config={})

    def test_events_loaded_from_both_lists(self):
        config = {
            "recurring": [
                {"title": "站会", "days": ["mon", "wed"], "start": "09:30", "end": "09:45"},
            ],
            "oneoff": [
                {"title": "评审", "date": "2026-09-16", "start": "14:00"},
            ],
        }
        scheduler, _, _, _ = make_scheduler(config)
        self.assertEqual(len(scheduler.events), 2)
        kinds = sorted(e.kind for e in scheduler.events)
        self.assertEqual(kinds, ["oneoff", "recurring"])

    def test_event_fields(self):
        config = {"recurring": [{
            "title": "站会", "days": ["wed"], "start": "09:30",
            "end": "09:45", "remind_before_min": 5,
            "action": {"state": "study", "prompt": "该开会了"},
        }]}
        event = make_scheduler(config)[0].events[0]
        self.assertEqual(event.title, "站会")
        self.assertEqual(event.start, (9, 30))
        self.assertEqual(event.end, (9, 45))
        self.assertEqual(event.days, {2})
        self.assertEqual(event.remind_before_min, 5)
        self.assertEqual(event.trigger_minute(), 9 * 60 + 25)
        self.assertEqual(event.action["state"], "study")

    def test_event_days_accept_multiple_spellings(self):
        config = {"recurring": [{"title": "x", "start": "09:00",
                                 "days": ["mon", "TUE", "wednesday", 4]}]}
        event = make_scheduler(config)[0].events[0]
        self.assertEqual(event.days, {0, 1, 2, 4})

    def test_event_without_days_means_every_day(self):
        config = {"recurring": [{"title": "x", "start": "09:00"}]}
        event = make_scheduler(config)[0].events[0]
        self.assertEqual(event.days, set())
        self.assertTrue(event.occurs_on(MONDAY))
        self.assertTrue(event.occurs_on(WEDNESDAY))

    def test_missing_title(self):
        with self.assertRaises(SchedulerError):
            make_scheduler({"recurring": [{"start": "09:00"}]})

    def test_missing_start(self):
        with self.assertRaises(SchedulerError):
            make_scheduler({"recurring": [{"title": "x"}]})

    def test_bad_start(self):
        with self.assertRaises(SchedulerError):
            make_scheduler({"recurring": [{"title": "x", "start": "25:00"}]})

    def test_end_before_start(self):
        with self.assertRaises(SchedulerError):
            make_scheduler({"recurring": [
                {"title": "x", "start": "10:00", "end": "09:00"}]})

    def test_both_date_and_days_rejected(self):
        with self.assertRaises(SchedulerError):
            make_scheduler({"recurring": [
                {"title": "x", "start": "09:00", "days": ["mon"], "date": "2026-09-16"}]})

    def test_bad_date(self):
        with self.assertRaises(SchedulerError):
            make_scheduler({"oneoff": [{"title": "x", "start": "09:00", "date": "16/09/2026"}]})

    def test_bad_remind(self):
        for bad in (-1, "5", 1.5):
            with self.subTest(bad=bad):
                with self.assertRaises(SchedulerError):
                    make_scheduler({"recurring": [
                        {"title": "x", "start": "09:00", "remind_before_min": bad}]})

    def test_bad_days_type(self):
        with self.assertRaises(SchedulerError):
            make_scheduler({"recurring": [{"title": "x", "start": "09:00", "days": "mon"}]})

    def test_recurring_must_be_a_list(self):
        with self.assertRaises(SchedulerError):
            make_scheduler({"recurring": {"title": "x"}})

    def test_entry_must_be_object(self):
        with self.assertRaises(SchedulerError):
            make_scheduler({"recurring": ["not an object"]})

    def test_bad_action_type(self):
        with self.assertRaises(SchedulerError):
            make_scheduler({"recurring": [
                {"title": "x", "start": "09:00", "action": "study"}]})


# ===========================================================================
#  定时触发
# ===========================================================================
class TestCheckSchedule(unittest.IsolatedAsyncioTestCase):
    async def test_fires_at_start_time(self):
        config = {"recurring": [{"title": "站会", "days": ["wed"], "start": "09:30"}]}
        scheduler, _, bus, _ = make_scheduler(config)
        fired = await scheduler.check_schedule(datetime(2026, 9, 16, 9, 30, 10))
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0]["title"], "站会")
        self.assertEqual(fired[0]["kind"], "schedule")
        self.assertEqual(len(bus.events), 1)
        self.assertEqual(bus.events[0]["source"], "scheduler")
        self.assertIn("站会", bus.events[0]["text"])

    async def test_does_not_fire_before_window(self):
        config = {"recurring": [{"title": "站会", "start": "09:30"}]}
        scheduler, _, bus, _ = make_scheduler(config)
        self.assertEqual(await scheduler.check_schedule(datetime(2026, 9, 16, 9, 29, 0)), [])
        self.assertEqual(bus.events, [])

    async def test_does_not_fire_after_window(self):
        config = {"recurring": [{"title": "站会", "start": "09:30"}]}
        scheduler, _, bus, _ = make_scheduler(config)
        # 默认 window_min=1 -> 09:31 已经出窗
        self.assertEqual(await scheduler.check_schedule(datetime(2026, 9, 16, 9, 31, 0)), [])
        self.assertEqual(bus.events, [])

    async def test_fires_only_once_within_window(self):
        # 这是核心去重: 每 interval_min 都会 check, 但只能触发一次
        config = {"recurring": [{"title": "站会", "start": "09:30"}]}
        scheduler, _, bus, _ = make_scheduler(config)
        first = await scheduler.check_schedule(datetime(2026, 9, 16, 9, 30, 5))
        again = await scheduler.check_schedule(datetime(2026, 9, 16, 9, 30, 40))
        self.assertEqual(len(first), 1)
        self.assertEqual(again, [], "同一时间窗内不该重复触发")
        self.assertEqual(len(bus.events), 1)

    async def test_fires_again_next_day(self):
        config = {"recurring": [{"title": "每天", "start": "09:30"}]}
        scheduler, _, bus, _ = make_scheduler(config)
        self.assertEqual(len(await scheduler.check_schedule(datetime(2026, 9, 16, 9, 30, 0))), 1)
        self.assertEqual(len(await scheduler.check_schedule(datetime(2026, 9, 17, 9, 30, 0))), 1)
        self.assertEqual(len(bus.events), 2)

    async def test_recurring_only_on_configured_days(self):
        config = {"recurring": [{"title": "周一站会", "days": ["mon"], "start": "09:30"}]}
        scheduler, _, _, _ = make_scheduler(config)
        self.assertEqual(await scheduler.check_schedule(datetime(2026, 9, 16, 9, 30, 0)), [])
        self.assertEqual(len(await scheduler.check_schedule(datetime(2026, 9, 14, 9, 30, 0))), 1)

    async def test_oneoff_only_on_its_date(self):
        config = {"oneoff": [{"title": "评审", "date": "2026-09-16", "start": "14:00"}]}
        scheduler, _, _, _ = make_scheduler(config)
        self.assertEqual(await scheduler.check_schedule(datetime(2026, 9, 15, 14, 0, 0)), [])
        self.assertEqual(len(await scheduler.check_schedule(datetime(2026, 9, 16, 14, 0, 0))), 1)
        self.assertEqual(await scheduler.check_schedule(datetime(2026, 9, 17, 14, 0, 0)), [])

    async def test_remind_before_shifts_trigger_earlier(self):
        config = {"recurring": [{
            "title": "站会", "start": "09:30", "remind_before_min": 10}]}
        scheduler, _, _, _ = make_scheduler(config)
        # 09:20 正是触发点
        self.assertEqual(len(await scheduler.check_schedule(datetime(2026, 9, 16, 9, 20, 0))), 1)

    async def test_remind_before_crossing_midnight(self):
        config = {"recurring": [{
            "title": "跨天", "start": "00:05", "remind_before_min": 10}]}
        scheduler, _, _, _ = make_scheduler(config)
        # 触发点落在前一天 23:55
        self.assertEqual(
            len(await scheduler.check_schedule(datetime(2026, 9, 16, 23, 55, 0))), 1
        )

    async def test_late_grace_allows_late_fire(self):
        config = {
            "scheduler": {"late_grace_min": 10},
            "recurring": [{"title": "站会", "start": "09:30"}],
        }
        scheduler, _, _, _ = make_scheduler(config)
        # 晚 5 分钟仍然认, 且只触发一次
        self.assertEqual(len(await scheduler.check_schedule(datetime(2026, 9, 16, 9, 35, 0))), 1)
        self.assertEqual(await scheduler.check_schedule(datetime(2026, 9, 16, 9, 36, 0)), [])

    async def test_events_can_live_in_scheduler_section(self):
        # 另一种布局: 日程写在 scheduler 段里面
        config = {"scheduler": {
            "interval_min": 5,
            "recurring": [{"title": "x", "start": "09:30"}],
        }}
        scheduler, _, _, _ = make_scheduler(config)
        self.assertEqual(scheduler.interval_min, 5.0)
        self.assertEqual(len(scheduler.events), 1)

    async def test_late_grace_still_bounded(self):
        config = {
            "scheduler": {"late_grace_min": 5},
            "recurring": [{"title": "站会", "start": "09:30"}],
        }
        scheduler, _, _, _ = make_scheduler(config)
        self.assertEqual(await scheduler.check_schedule(datetime(2026, 9, 16, 9, 40, 0)), [])

    async def test_multiple_events(self):
        config = {"recurring": [
            {"title": "a", "start": "09:30"},
            {"title": "b", "start": "09:30"},
        ]}
        scheduler, _, _, _ = make_scheduler(config)
        fired = await scheduler.check_schedule(datetime(2026, 9, 16, 9, 30, 0))
        self.assertEqual(sorted(f["title"] for f in fired), ["a", "b"])

    async def test_uses_injected_clock_by_default(self):
        config = {"recurring": [{"title": "x", "start": "09:30"}]}
        # 默认时钟在 09:30 整点, 先把它拨到整点之前
        clock = FakeClock(datetime(2026, 9, 16, 9, 0, 0))
        scheduler, _, _, _ = make_scheduler(config, clock=clock)
        self.assertEqual([], await scheduler.check_schedule())
        clock.set(datetime(2026, 9, 16, 9, 30, 0))
        self.assertEqual(len(await scheduler.check_schedule()), 1)

    async def test_default_message_when_no_prompt(self):
        config = {"recurring": [{"title": "站会", "start": "09:30"}]}
        scheduler, _, bus, _ = make_scheduler(config)
        await scheduler.check_schedule(datetime(2026, 9, 16, 9, 30, 0))
        self.assertIn("站会", bus.events[0]["text"])

    async def test_custom_prompt_used(self):
        config = {"recurring": [{
            "title": "站会", "start": "09:30", "action": {"prompt": "去开会"}}]}
        scheduler, _, bus, _ = make_scheduler(config)
        await scheduler.check_schedule(datetime(2026, 9, 16, 9, 30, 0))
        self.assertEqual(bus.events[0]["text"], "去开会")

    # ------------------------------------------------------- 触发动作 ---
    async def test_action_transitions_state(self):
        config = {"recurring": [{
            "title": "开工", "start": "09:30", "action": {"state": "study"}}]}
        scheduler, state, _, _ = make_scheduler(config)
        fired = await scheduler.check_schedule(datetime(2026, 9, 16, 9, 30, 0))
        self.assertIs(state.current(), State.STUDY)
        state_action = [a for a in fired[0]["actions"] if a["type"] == "state"][0]
        self.assertTrue(state_action["ok"])
        self.assertEqual(state_action["current"], "study")

    async def test_action_reports_illegal_transition(self):
        # STUDY -> GAME 不合法; 不该抛异常, 只在结果里记 ok=False
        config = {"recurring": [
            {"title": "先学习", "start": "09:30", "action": {"state": "study"}},
            {"title": "再游戏", "start": "09:30", "action": {"state": "game"}},
        ]}
        scheduler, state, _, _ = make_scheduler(config)
        fired = await scheduler.check_schedule(datetime(2026, 9, 16, 9, 30, 0))
        self.assertEqual(len(fired), 2)
        results = [a for f in fired for a in f["actions"] if a["type"] == "state"]
        self.assertEqual(sorted(r["ok"] for r in results), [False, True])

    async def test_action_with_reason(self):
        config = {"recurring": [{
            "title": "开工", "start": "09:30",
            "action": {"state": "study", "reason": "该学习了"}}]}
        scheduler, state, _, _ = make_scheduler(config)
        await scheduler.check_schedule(datetime(2026, 9, 16, 9, 30, 0))
        self.assertEqual(state.last_reason, "该学习了")

    async def test_both_state_and_message(self):
        config = {"recurring": [{
            "title": "开局", "start": "09:30",
            "action": {"state": "game", "prompt": "该玩游戏了"}}]}
        scheduler, state, bus, _ = make_scheduler(config)
        await scheduler.check_schedule(datetime(2026, 9, 16, 9, 30, 0))
        self.assertIs(state.current(), State.GAME)
        self.assertEqual(bus.events[0]["text"], "该玩游戏了")

    async def test_stats(self):
        config = {"recurring": [{"title": "x", "start": "09:30"}]}
        scheduler, _, _, _ = make_scheduler(config)
        await scheduler.check_schedule(datetime(2026, 9, 16, 9, 30, 0))
        stats = scheduler.stats
        self.assertEqual(stats["checks"], 1)
        self.assertEqual(stats["triggers"], 1)
        self.assertEqual(stats["events"], 1)


# ===========================================================================
#  触发事实 (history / on_fire)
# ===========================================================================
class TestFiredHistory(unittest.IsolatedAsyncioTestCase):
    """P1: 记下"真的触发过什么、什么时候" —— 给 IPC/CLI 查询用。

    ⚠ 这组用例要钉死的边界是"历史是旁路": 它坏了、满了、被读走了, 都不该影响
      触发判定与去重 (_fired)。
    """

    CONFIG = {"recurring": [{"title": "站会", "start": "09:30"}]}

    async def test_empty_before_any_fire(self):
        scheduler, _, _, _ = make_scheduler(self.CONFIG)
        self.assertEqual(scheduler.recent_fired(), [])
        self.assertEqual(scheduler.stats["fired_history"], 0)

    async def test_records_the_fact_after_fire(self):
        scheduler, _, bus, _ = make_scheduler(self.CONFIG)
        await scheduler.check_schedule(datetime(2026, 9, 16, 9, 30, 10))

        facts = scheduler.recent_fired()
        self.assertEqual(len(facts), 1)
        fact = facts[0]
        self.assertEqual(fact["title"], "站会")
        self.assertEqual(fact["date"], "2026-09-16")
        self.assertEqual(fact["scheduled_at"], "2026-09-16T09:30")
        # fired_at 是**真的触发时刻** (检查那一刻), 不是日程写的时刻
        self.assertEqual(fact["fired_at"], "2026-09-16T09:30:10")
        self.assertEqual(len(fact["actions"]), 1)
        self.assertEqual(fact["actions"][0]["type"], "message")
        self.assertEqual(fact["actions"][0]["text"], bus.events[0]["text"])

    async def test_no_fire_no_history(self):
        scheduler, _, _, _ = make_scheduler(self.CONFIG)
        # 窗口外看一眼: 什么都没触发, 历史就该是空的 (不能记"看过了")
        await scheduler.check_schedule(datetime(2026, 9, 16, 9, 29, 0))
        self.assertEqual(scheduler.recent_fired(), [])
        self.assertEqual(scheduler.stats["fired_history"], 0)

    async def test_dedup_does_not_add_a_second_fact(self):
        scheduler, _, _, _ = make_scheduler(self.CONFIG)
        await scheduler.check_schedule(datetime(2026, 9, 16, 9, 30, 5))
        await scheduler.check_schedule(datetime(2026, 9, 16, 9, 30, 40))  # 同窗, 被去重
        self.assertEqual(len(scheduler.recent_fired()), 1)

    async def test_history_is_bounded_and_drops_the_oldest(self):
        scheduler, _, _, _ = make_scheduler(self.CONFIG, history_limit=2)
        for day in (16, 17, 18):
            await scheduler.check_schedule(datetime(2026, 9, day, 9, 30, 0))
        facts = scheduler.recent_fired()
        self.assertEqual(len(facts), 2, "maxlen=2 就该只留最近两条")
        self.assertEqual([f["date"] for f in facts], ["2026-09-17", "2026-09-18"])

    async def test_history_limit_zero_records_nothing_but_still_fires(self):
        scheduler, _, bus, _ = make_scheduler(self.CONFIG, history_limit=0)
        fired = await scheduler.check_schedule(datetime(2026, 9, 16, 9, 30, 0))
        self.assertEqual(len(fired), 1, "关掉历史不该影响触发")
        self.assertEqual(len(bus.events), 1)
        self.assertEqual(scheduler.recent_fired(), [])

    async def test_recent_fired_limit_takes_the_tail(self):
        scheduler, _, _, _ = make_scheduler(self.CONFIG, history_limit=5)
        for day in (16, 17, 18):
            await scheduler.check_schedule(datetime(2026, 9, day, 9, 30, 0))
        self.assertEqual(len(scheduler.recent_fired()), 3)
        self.assertEqual(len(scheduler.recent_fired(limit=2)), 2)
        self.assertEqual(
            [f["date"] for f in scheduler.recent_fired(limit=2)],
            ["2026-09-17", "2026-09-18"],
        )
        self.assertEqual(scheduler.recent_fired(limit=0), [])

    async def test_recent_fired_returns_copies(self):
        scheduler, _, _, _ = make_scheduler(self.CONFIG)
        await scheduler.check_schedule(datetime(2026, 9, 16, 9, 30, 0))
        scheduler.recent_fired()[0]["title"] = "改坏了"
        self.assertEqual(scheduler.recent_fired()[0]["title"], "站会",
                         "拿到的应是副本, 改不动真源")

    async def test_default_history_limit_is_the_documented_one(self):
        scheduler, _, _, _ = make_scheduler(self.CONFIG)
        self.assertEqual(scheduler.history_limit, DEFAULT_HISTORY_LIMIT)

    async def test_history_limit_is_the_cap_not_the_count(self):
        scheduler, _, _, _ = make_scheduler(self.CONFIG, history_limit=7)
        self.assertEqual(scheduler.history_limit, 7)
        await scheduler.check_schedule(datetime(2026, 9, 16, 9, 30, 0))
        self.assertEqual(scheduler.history_limit, 7, "上限不随条数变")
        self.assertEqual(scheduler.stats["fired_history"], 1, "条数是另一回事")

    async def test_bad_history_limit_is_rejected(self):
        with self.assertRaises(SchedulerError):
            make_scheduler(self.CONFIG, history_limit=-1)
        with self.assertRaises(SchedulerError):
            make_scheduler(self.CONFIG, history_limit="50")

    async def test_on_fire_gets_the_fact(self):
        scheduler, _, _, _ = make_scheduler(self.CONFIG)
        seen = []
        scheduler.on_fire = seen.append
        await scheduler.check_schedule(datetime(2026, 9, 16, 9, 30, 7))
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["title"], "站会")
        self.assertEqual(seen[0]["fired_at"], "2026-09-16T09:30:07")

    async def test_on_fire_sees_what_recent_fired_keeps(self):
        scheduler, _, _, _ = make_scheduler(self.CONFIG)
        seen = []
        scheduler.on_fire = seen.append
        await scheduler.check_schedule(datetime(2026, 9, 16, 9, 30, 0))
        self.assertEqual(seen, scheduler.recent_fired())

    async def test_on_fire_rejects_non_callable(self):
        scheduler, _, _, _ = make_scheduler(self.CONFIG)
        with self.assertRaises(SchedulerError):
            scheduler.on_fire = "不是函数"
        scheduler.on_fire = None          # 允许清掉
        self.assertIsNone(scheduler.on_fire)

    async def test_broken_on_fire_does_not_break_the_fire(self):
        """旁路坏了: 这次触发仍然算数, 去重照旧, 历史照记。"""
        scheduler, _, bus, _ = make_scheduler(self.CONFIG)

        def explode(_fact):
            raise RuntimeError("推送炸了")

        scheduler.on_fire = explode
        first = await scheduler.check_schedule(datetime(2026, 9, 16, 9, 30, 3))
        again = await scheduler.check_schedule(datetime(2026, 9, 16, 9, 30, 30))

        self.assertEqual(len(first), 1)
        self.assertEqual(again, [], "回调炸了不能把去重一起炸掉")
        self.assertEqual(len(bus.events), 1)
        self.assertEqual(scheduler.stats["triggers"], 1)
        self.assertEqual(len(scheduler.recent_fired()), 1)

    async def test_history_outlives_the_dedup_prune(self):
        """两个集合的生命周期不同: _fired 只留今天/昨天, 历史按条数留。"""
        scheduler, _, _, _ = make_scheduler(self.CONFIG)
        await scheduler.check_schedule(datetime(2026, 9, 16, 9, 30, 0))
        await scheduler.check_schedule(datetime(2026, 9, 20, 9, 30, 0))
        self.assertEqual(len(scheduler.recent_fired()), 2, "历史跨天还在")
        self.assertEqual(scheduler.stats["fired_keys"], 1, "去重集合只留今天/昨天")

    async def test_stats_reports_history_size(self):
        scheduler, _, _, _ = make_scheduler(self.CONFIG)
        await scheduler.check_schedule(datetime(2026, 9, 16, 9, 30, 0))
        self.assertEqual(scheduler.stats["fired_history"], 1)


# ===========================================================================
#  终端命令
# ===========================================================================
class TestNormalizeCommand(unittest.TestCase):
    def test_strips_whitespace(self):
        self.assertEqual(normalize_command("  study  "), "study")
        self.assertEqual(normalize_command("\t回桌面\n"), "回桌面")

    def test_case_insensitive(self):
        self.assertEqual(normalize_command("Study"), "study")
        self.assertEqual(normalize_command("STUDY"), normalize_command("study"))

    def test_unusable_input_becomes_empty(self):
        for bad in (None, 123, [], {}, True):
            with self.subTest(bad=bad):
                self.assertEqual(normalize_command(bad), "")
        self.assertEqual(normalize_command("   "), "")

    def test_inner_text_is_not_touched(self):
        # 只去首尾空白: 中间的空格/标点是命令名的一部分
        self.assertEqual(normalize_command(" go home "), "go home")


class TestParseCommandConfig(unittest.TestCase):
    def test_list_form(self):
        config = [{"command": "study", "action": {"state": "study", "prompt": "开始学习"}}]
        bindings = parse_command_config(config)
        self.assertEqual(len(bindings), 1)
        self.assertEqual(bindings[0].command, "study")
        self.assertEqual(bindings[0].label, "study")
        self.assertEqual(bindings[0].action["state"], "study")
        self.assertEqual(bindings[0].action["prompt"], "开始学习")

    def test_split_action_is_merged(self):
        # 允许 {action: {...}} 与直接写两种形态
        bindings = parse_command_config(
            [{"command": "study", "action": {"state": "study"}}])
        self.assertEqual(bindings[0].action["state"], "study")

    def test_dict_form(self):
        bindings = parse_command_config({"study": {"state": "study"}})
        self.assertEqual(len(bindings), 1)
        self.assertEqual(bindings[0].command, "study")
        # 直接写 state 也算 action
        self.assertEqual(bindings[0].action["state"], "study")

    def test_command_is_normalized(self):
        bindings = parse_command_config({"  Study ": {}})
        self.assertEqual(bindings[0].command, "study")
        self.assertEqual(bindings[0].label, "  Study ", "label 保留原样以便回显")

    def test_empty_config(self):
        self.assertEqual(parse_command_config(None), [])
        self.assertEqual(parse_command_config([]), [])
        self.assertEqual(parse_command_config({}), [])

    def test_duplicate_command_rejected(self):
        with self.assertRaises(SchedulerError):
            parse_command_config([
                {"command": "study", "state": "study"},
                {"command": "Study", "state": "game"},   # 归一后同名
            ])

    def test_missing_command_rejected(self):
        with self.assertRaises(SchedulerError):
            parse_command_config([{"action": {"state": "study"}}])

    def test_bad_command_type_rejected(self):
        with self.assertRaises(SchedulerError):
            parse_command_config([{"command": 123}])

    def test_empty_command_rejected(self):
        with self.assertRaises(SchedulerError):
            parse_command_config([{"command": "   "}])

    def test_bad_top_level_type(self):
        with self.assertRaises(SchedulerError):
            parse_command_config("study")


class TestLegacyHotkeysKey(unittest.TestCase):
    """旧键名不再生效, 但必须**说出来** —— 静默忽略最难查。"""

    def test_legacy_key_is_ignored_and_warned(self):
        scheduler, _, _, _ = make_scheduler(
            {"scheduler": {"hotkeys": [{"keys": ["ctrl", "s"], "state": "study"}]}}
        )
        self.assertEqual(scheduler.bindings, [], "旧键名不该再产生绑定")
        self.assertTrue(
            any("commands" in w for w in scheduler.warnings),
            "应在 warnings 里指出 hotkeys 已改名为 commands: %r" % (scheduler.warnings,),
        )

    def test_command_key_does_not_warn(self):
        scheduler, _, _, _ = make_scheduler(
            {"scheduler": {"commands": [{"command": "study", "state": "study"}]}}
        )
        self.assertEqual(len(scheduler.bindings), 1)
        self.assertFalse(any("hotkeys" in w for w in scheduler.warnings))


class TestListenCommands(unittest.IsolatedAsyncioTestCase):
    """命令识别: 直接驱动 _on_bus_event, 不依赖后台循环。"""

    def _scheduler(self, commands, **kwargs):
        config = {"scheduler": {"commands": commands}}
        bus = kwargs.pop("bus", None) or FakeBus()
        scheduler, state, _, _ = make_scheduler(config, bus=bus, **kwargs)
        return scheduler, state, bus

    async def test_command_fires(self):
        scheduler, state, bus = self._scheduler(
            [{"command": "study", "action": {"state": "study", "prompt": "开始学习"}}]
        )
        await scheduler._on_bus_event(
            {"source": "terminal", "text": "study", "timestamp": 5.0}
        )
        self.assertIs(state.current(), State.STUDY)
        self.assertEqual(bus.events[0]["source"], "scheduler")
        self.assertEqual(bus.events[0]["text"], "开始学习")
        self.assertEqual(scheduler.stats["command_hits"], 1)

    async def test_whitespace_and_case_tolerated(self):
        scheduler, state, _ = self._scheduler(
            [{"command": "study", "action": {"state": "study"}}]
        )
        await scheduler._on_bus_event({"source": "terminal", "text": "  StUdY  "})
        self.assertIs(state.current(), State.STUDY)

    async def test_chinese_command(self):
        scheduler, state, _ = self._scheduler(
            [{"command": "回桌面", "action": {"state": "idle"}}]
        )
        await scheduler._on_bus_event({"source": "terminal", "text": " 回桌面 "})
        self.assertIs(state.current(), State.IDLE)

    async def test_wrong_command_does_not_fire(self):
        scheduler, state, _ = self._scheduler(
            [{"command": "study", "action": {"state": "study"}}]
        )
        for text in ("stud", "study please", "studyx", "开始学习"):
            with self.subTest(text=text):
                await scheduler._on_bus_event({"source": "terminal", "text": text})
        self.assertIs(state.current(), State.IDLE, "只有整行相等才算命中")

    async def test_other_sources_ignored(self):
        # 界面/别的来源打出一句像命令的文本, 不该触发状态切换
        scheduler, state, _ = self._scheduler(
            [{"command": "study", "action": {"state": "study"}}]
        )
        for source in ("gui", "scheduler", "host_keyboard"):
            await scheduler._on_bus_event({"source": source, "text": "study"})
        self.assertIs(state.current(), State.IDLE)
        self.assertEqual(scheduler.stats["command_hits"], 0)

    async def test_unusable_text_skipped(self):
        scheduler, state, _ = self._scheduler(
            [{"command": "study", "action": {"state": "study"}}]
        )
        for text in ("", "   ", None, 123, "[key s modifier=2]"):
            await scheduler._on_bus_event({"source": "terminal", "text": text})
        self.assertIs(state.current(), State.IDLE)
        self.assertEqual(scheduler.stats["command_hits"], 0)

    async def test_non_dict_event_skipped(self):
        scheduler, _, _ = self._scheduler([{"command": "study", "action": {}}])
        for bad in (None, "text", 123, []):
            self.assertIsNone(await scheduler._on_bus_event(bad))

    async def test_repeated_command_fires_each_time(self):
        # 终端是"敲一行回车", 没有按键重复那回事 —— 敲两次就该生效两次
        scheduler, _, bus = self._scheduler(
            [{"command": "study", "action": {"prompt": "x"}}]
        )
        for _ in range(3):
            await scheduler._on_bus_event({"source": "terminal", "text": "study"})
        self.assertEqual(len(bus.events), 3)
        self.assertEqual(scheduler.stats["command_hits"], 3)

    async def test_bus_timestamp_recorded(self):
        scheduler, _, _ = self._scheduler(
            [{"command": "study", "action": {"prompt": "x"}}]
        )
        detail = await scheduler._on_bus_event(
            {"source": "terminal", "text": "study", "timestamp": 42.5}
        )
        self.assertEqual(detail["bus_timestamp"], 42.5)
        self.assertEqual(detail["command"], "study")
        self.assertEqual(detail["kind"], "command")

    async def test_no_bindings_means_no_hit(self):
        scheduler, state, _ = self._scheduler([])
        self.assertIsNone(
            await scheduler._on_bus_event({"source": "terminal", "text": "study"}))
        self.assertIs(state.current(), State.IDLE)


class TestListenCommandsLoop(unittest.IsolatedAsyncioTestCase):
    """走真实 bus 的订阅版本 (验证 listen_commands 与 ChatInputBus 配合)。"""

    async def test_subscription_fires_and_does_not_steal_events(self):
        bus = ChatInputBus()
        config = {"scheduler": {"commands": [
            {"command": "game", "action": {"state": "game"}}]}}
        state = StateMachine()
        scheduler = Scheduler(state=state, bus=bus, config=config)

        await scheduler.start()
        await asyncio.sleep(0)

        # 普通聊天 + 一条命令 —— 两者都在同一条 bus 上
        await bus.push("terminal", "帮我查一下天气")
        await bus.push("terminal", "game")
        await asyncio.sleep(0.05)

        self.assertIs(state.current(), State.GAME, "命令应当切到 GAME")
        await scheduler.stop()

        # 订阅者只看不取: 两条事件一条都不该被吃掉
        remaining = []
        while True:
            event = bus.get_nowait()
            if event is None:
                break
            remaining.append((event["source"], event["text"]))
        self.assertIn(("terminal", "帮我查一下天气"), remaining,
                      "终端消息绝不能被调度器吃掉")
        self.assertIn(("terminal", "game"), remaining,
                      "命令那行也留在队列里 —— 这是订阅式的固有行为, 见 listen_commands 的 @note")
        # 触发消息也在队列里 (不是被自己消费掉)
        self.assertIn(("scheduler", "命令 game"), remaining)

    async def test_stop_unsubscribes(self):
        bus = ChatInputBus()
        scheduler = Scheduler(
            state=StateMachine(), bus=bus,
            config={"scheduler": {"commands": [{"command": "game", "action": {}}]}},
        )
        await scheduler.start()
        await asyncio.sleep(0)   # 让 listen_commands 协程真正跑起来并订阅
        self.assertEqual(bus.subscriber_count, 1)
        await scheduler.stop()
        self.assertEqual(bus.subscriber_count, 0, "stop 后必须注销订阅")

    async def test_fake_bus_without_subscribe_warns(self):
        # 只提供 get() 的替身: 监听退化, 但要有明确 warning (静默失效更难查)
        scheduler, state, _, _ = make_scheduler(
            {"scheduler": {"commands": [{"command": "game", "action": {"state": "game"}}]}}
        )
        await scheduler.start()
        await asyncio.sleep(0)
        await scheduler.stop()
        self.assertTrue(any("subscribe" in w for w in scheduler.warnings))
        self.assertIs(state.current(), State.IDLE)


# ===========================================================================
#  生命周期与时钟
# ===========================================================================
class TestLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_start_is_idempotent(self):
        scheduler, _, _, _ = make_scheduler(
            {"recurring": [{"title": "x", "start": "09:30"}]}
        )
        await scheduler.start()
        self.assertTrue(scheduler.running)
        tasks = list(scheduler._tasks)
        await scheduler.start()
        self.assertEqual(scheduler._tasks, tasks, "重复 start 不该再起任务")
        await scheduler.stop()

    async def test_stop_is_idempotent(self):
        scheduler, _, _, _ = make_scheduler()
        await scheduler.stop()
        await scheduler.start()
        await scheduler.stop()
        await scheduler.stop()
        self.assertFalse(scheduler.running)

    async def test_start_checks_clock(self):
        scheduler, _, _, clock = make_scheduler()
        clock.set(datetime(1970, 1, 1, 0, 0, 0))
        await scheduler.start()
        self.assertFalse(scheduler.sync_time()["plausible"])
        self.assertTrue(scheduler.warnings)
        await scheduler.stop()

    async def test_schedule_loop_fires_then_stops(self):
        bounded = FakeMonotonic()

        async def fake_sleep(seconds):
            bounded.advance(seconds)
            await asyncio.sleep(0)

        # 时钟停在窗口内: 循环会反复 check, 但去重保证只触发一次
        clock = FakeClock(datetime(2026, 9, 16, 9, 30, 10))
        state = StateMachine()
        bus = FakeBus()
        scheduler = Scheduler(
            state=state, bus=bus,
            config={"scheduler": {"interval_min": 1},
                    "recurring": [{"title": "x", "start": "09:30",
                                   "action": {"state": "study"}}]},
            clock=clock,
            monotonic=bounded,
            sleep=fake_sleep,
        )
        await scheduler.start()
        for _ in range(50):
            if bus.events:
                break
            await asyncio.sleep(0)
        await scheduler.stop()

        self.assertIs(state.current(), State.STUDY)
        self.assertEqual(len(bus.events), 1)
        self.assertFalse(scheduler.running)

    async def test_stop_prevents_further_firing(self):
        scheduler, _, bus, clock = make_scheduler(
            {"recurring": [{"title": "x", "start": "09:30"}]}
        )
        await scheduler.start()
        await scheduler.stop()
        clock.set(datetime(2026, 9, 16, 9, 30, 0))
        # 循环已经取消, 手动检查仍可用 (这是刻意的: check_schedule 是公开接口)
        self.assertEqual(len(await scheduler.check_schedule()), 1)


class TestSyncTime(unittest.TestCase):
    def test_plausible_clock(self):
        scheduler, _, _, _ = make_scheduler()
        result = scheduler.sync_time()
        self.assertTrue(result["plausible"])
        self.assertEqual(result["warnings"], [])
        self.assertIsInstance(result["now"], datetime)

    def test_unset_clock_warns(self):
        clock = FakeClock(datetime(1970, 1, 1, 0, 0, 0))
        scheduler, _, _, _ = make_scheduler(clock=clock)
        result = scheduler.sync_time()
        self.assertFalse(result["plausible"])
        self.assertIn("1970", result["warnings"][0])

    def test_far_future_clock_warns(self):
        clock = FakeClock(datetime(2200, 1, 1))
        scheduler, _, _, _ = make_scheduler(clock=clock)
        self.assertFalse(scheduler.sync_time()["plausible"])

    def test_does_not_modify_system_time(self):
        # 明确不设时间: sync_time 只读不写
        clock = FakeClock(datetime(2026, 9, 16, 9, 0, 0))
        scheduler, _, _, _ = make_scheduler(clock=clock)
        before = clock.moment
        scheduler.sync_time()
        self.assertEqual(clock.moment, before)


class TestRepr(unittest.TestCase):
    def test_repr(self):
        scheduler, _, _, _ = make_scheduler(
            {"recurring": [{"title": "x", "start": "09:00"}]}
        )
        text = repr(scheduler)
        self.assertIn("events=1", text)
        self.assertIn("running=False", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
