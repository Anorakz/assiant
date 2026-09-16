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
  快捷键     "[key X]" 与 JSON 两种文本、modifier 位掩码、修饰键分开按、
             按错键不触发、按住不放不重复触发、非 host_keyboard 来源忽略、
             解析不了的文本跳过、配置两种写法 (list / dict)
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
    DEFAULT_INTERVAL_MIN,
    DEFAULT_WINDOW_MIN,
    Scheduler,
    SchedulerError,
    State,
    StateMachine,
    parse_clock,
    parse_hotkey_config,
    parse_hotkey_text,
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
#  快捷键
# ===========================================================================
class TestParseHotkeyText(unittest.TestCase):
    def test_plain_key(self):
        event = parse_hotkey_text("[key A]")
        self.assertEqual(event.key, "a")
        self.assertEqual(event.modifiers, set())
        self.assertEqual(event.action, "press")

    def test_key_with_modifier_mask(self):
        event = parse_hotkey_text("[key C modifier=6]")
        self.assertEqual(event.key, "c")
        self.assertEqual(event.modifiers, {"ctrl", "alt"})

    def test_named_key(self):
        self.assertEqual(parse_hotkey_text("[key space]").key, "space")
        self.assertEqual(parse_hotkey_text("[key enter]").key, "enter")

    def test_all_modifier_bits(self):
        self.assertEqual(parse_hotkey_text("[key x modifier=1]").modifiers, {"shift"})
        self.assertEqual(parse_hotkey_text("[key x modifier=2]").modifiers, {"ctrl"})
        self.assertEqual(parse_hotkey_text("[key x modifier=4]").modifiers, {"alt"})
        self.assertEqual(parse_hotkey_text("[key x modifier=8]").modifiers, {"meta"})
        self.assertEqual(parse_hotkey_text("[key x modifier=15]").modifiers,
                         {"shift", "ctrl", "alt", "meta"})

    def test_unknown_modifier_bits_ignored(self):
        self.assertEqual(parse_hotkey_text("[key x modifier=16]").modifiers, set())

    def test_json_form(self):
        text = json.dumps({"type": "key", "key": "c", "modifier": 6, "action": "press"})
        event = parse_hotkey_text(text)
        self.assertEqual(event.key, "c")
        self.assertEqual(event.modifiers, {"ctrl", "alt"})

    def test_json_vk_code(self):
        text = json.dumps({"type": "key", "key": 65, "modifier": 0})
        self.assertEqual(parse_hotkey_text(text).key, "a")

    def test_json_non_key_type_ignored(self):
        self.assertIsNone(parse_hotkey_text(json.dumps({"type": "mouse", "x": 1})))

    def test_non_key_text_returns_none(self):
        for bad in ("[mouse x=1 y=2 action=press]", "hello", "", "   ",
                    "[key ]", "[key  ]", None, 123, "[]"):
            with self.subTest(bad=bad):
                self.assertIsNone(parse_hotkey_text(bad))

    def test_bad_json_returns_none(self):
        self.assertIsNone(parse_hotkey_text("{not json"))


class TestParseHotkeyConfig(unittest.TestCase):
    def test_list_form(self):
        config = [{"keys": ["ctrl", "alt", "s"], "action": {"state": "study"}}]
        bindings = parse_hotkey_config(config)
        self.assertEqual(len(bindings), 1)
        self.assertEqual(bindings[0].keys, ("ctrl", "alt", "s"))
        self.assertEqual(bindings[0].main_key, "s")
        self.assertEqual(bindings[0].modifiers, frozenset({"ctrl", "alt"}))

    def test_string_form_in_list(self):
        bindings = parse_hotkey_config([{"keys": "ctrl+alt+s", "state": "study"}])
        self.assertEqual(bindings[0].keys, ("ctrl", "alt", "s"))
        # 直接写 state 也算 action
        self.assertEqual(bindings[0].action["state"], "study")

    def test_dict_form(self):
        bindings = parse_hotkey_config({"ctrl+alt+s": {"state": "study"}})
        self.assertEqual(len(bindings), 1)
        self.assertEqual(bindings[0].keys, ("ctrl", "alt", "s"))

    def test_modifier_order_normalized(self):
        # 写 "s+ctrl" 也要归一成修饰键在前
        bindings = parse_hotkey_config({"s+ctrl": {"state": "study"}})
        self.assertEqual(bindings[0].keys, ("ctrl", "s"))

    def test_aliases(self):
        bindings = parse_hotkey_config({"control+menu+x": {}})
        self.assertEqual(bindings[0].keys, ("ctrl", "alt", "x"))

    def test_empty_config(self):
        self.assertEqual(parse_hotkey_config(None), [])
        self.assertEqual(parse_hotkey_config([]), [])
        self.assertEqual(parse_hotkey_config({}), [])

    def test_duplicate_binding_rejected(self):
        with self.assertRaises(SchedulerError):
            parse_hotkey_config([
                {"keys": ["ctrl", "s"], "state": "study"},
                {"keys": ["ctrl", "s"], "state": "game"},
            ])

    def test_missing_keys_rejected(self):
        with self.assertRaises(SchedulerError):
            parse_hotkey_config([{"action": {"state": "study"}}])

    def test_bad_keys_type_rejected(self):
        with self.assertRaises(SchedulerError):
            parse_hotkey_config([{"keys": 123}])

    def test_empty_keys_rejected(self):
        with self.assertRaises(SchedulerError):
            parse_hotkey_config([{"keys": "+"}])

    def test_bad_top_level_type(self):
        with self.assertRaises(SchedulerError):
            parse_hotkey_config("ctrl+s")


class TestListenHotkey(unittest.IsolatedAsyncioTestCase):
    """快捷键识别: 直接驱动 _on_bus_event, 不依赖后台循环。"""

    def _scheduler(self, hotkeys, **kwargs):
        config = {"scheduler": {"hotkeys": hotkeys}}
        bus = kwargs.pop("bus", None) or FakeBus()
        scheduler, state, _, _ = make_scheduler(config, bus=bus, **kwargs)
        return scheduler, state, bus

    async def test_hotkey_fires(self):
        scheduler, state, bus = self._scheduler(
            [{"keys": ["ctrl", "alt", "s"], "action": {"state": "study", "prompt": "开始学习"}}]
        )
        await scheduler._on_bus_event(
            {"source": "host_keyboard", "text": "[key s modifier=6]", "timestamp": 5.0}
        )
        self.assertIs(state.current(), State.STUDY)
        self.assertEqual(bus.events[0]["source"], "scheduler")
        self.assertEqual(bus.events[0]["text"], "开始学习")
        self.assertEqual(scheduler.stats["hotkey_hits"], 1)

    async def test_hotkey_with_separately_pressed_modifiers(self):
        # 修饰键的 press 事件没有 modifier 位时, 靠"跟踪按住状态"也能命中
        scheduler, state, _ = self._scheduler(
            [{"keys": ["ctrl", "s"], "action": {"state": "study"}}]
        )
        await scheduler._on_bus_event({"source": "host_keyboard", "text": "[key ctrl]"})
        await scheduler._on_bus_event({"source": "host_keyboard", "text": "[key s]"})
        self.assertIs(state.current(), State.STUDY)

    async def test_modifier_release_clears_it(self):
        scheduler, state, _ = self._scheduler(
            [{"keys": ["ctrl", "s"], "action": {"state": "study"}}]
        )
        await scheduler._on_bus_event({"source": "host_keyboard", "text": "[key ctrl]"})
        await scheduler._on_bus_event({"source": "host_keyboard", "text": "[key ctrl release]"})
        await scheduler._on_bus_event({"source": "host_keyboard", "text": "[key x]"})
        self.assertIs(state.current(), State.IDLE, "修饰键已抬起, 不该命中")

    async def test_release_is_parsed(self):
        event = parse_hotkey_text("[key ctrl release]")
        self.assertEqual(event.key, "ctrl")
        self.assertEqual(event.action, "release")
        # "up" / "released" 也认
        self.assertEqual(parse_hotkey_text("[key c up]").action, "release")
        self.assertEqual(parse_hotkey_text("[key c released]").action, "release")

    async def test_wrong_modifiers_do_not_fire(self):
        scheduler, state, _ = self._scheduler(
            [{"keys": ["ctrl", "s"], "action": {"state": "study"}}]
        )
        await scheduler._on_bus_event(
            {"source": "host_keyboard", "text": "[key s modifier=4]"}  # 只有 alt
        )
        self.assertIs(state.current(), State.IDLE)

    async def test_wrong_key_does_not_fire(self):
        scheduler, state, _ = self._scheduler(
            [{"keys": ["ctrl", "s"], "action": {"state": "study"}}]
        )
        await scheduler._on_bus_event(
            {"source": "host_keyboard", "text": "[key x modifier=2]"}
        )
        self.assertIs(state.current(), State.IDLE)

    async def test_key_repeat_does_not_refire(self):
        # 按住不放时主机会反复发 press; 只能触发一次
        scheduler, _, bus = self._scheduler(
            [{"keys": ["ctrl", "s"], "action": {"prompt": "x"}}]
        )
        for _ in range(5):
            await scheduler._on_bus_event(
                {"source": "host_keyboard", "text": "[key s modifier=2]"}
            )
        self.assertEqual(len(bus.events), 1, "按住不放不该重复触发")
        # 抬起后可以再次触发
        await scheduler._on_bus_event({"source": "host_keyboard", "text": "[key s release]"})
        await scheduler._on_bus_event(
            {"source": "host_keyboard", "text": "[key s modifier=2]"}
        )
        self.assertEqual(len(bus.events), 2)

    async def test_other_sources_ignored(self):
        # 终端里打一句像按键的文本, 不该触发状态切换
        scheduler, state, _ = self._scheduler(
            [{"keys": ["ctrl", "s"], "action": {"state": "study"}}]
        )
        for source in ("terminal", "gui", "scheduler"):
            await scheduler._on_bus_event(
                {"source": source, "text": "[key s modifier=2]"}
            )
        self.assertIs(state.current(), State.IDLE)

    async def test_unparsable_text_skipped(self):
        scheduler, state, _ = self._scheduler(
            [{"keys": ["ctrl", "s"], "action": {"state": "study"}}]
        )
        for text in ("你好", "", "[mouse x=1 y=2 action=press]", "{bad json"):
            await scheduler._on_bus_event({"source": "host_keyboard", "text": text})
        self.assertIs(state.current(), State.IDLE)
        self.assertEqual(scheduler.stats["hotkey_hits"], 0)

    async def test_non_dict_event_skipped(self):
        scheduler, _, _ = self._scheduler([{"keys": ["ctrl", "s"], "action": {}}])
        for bad in (None, "text", 123, []):
            self.assertIsNone(await scheduler._on_bus_event(bad))

    async def test_modifier_key_alone_does_not_fire(self):
        scheduler, state, _ = self._scheduler(
            [{"keys": ["ctrl"], "action": {"state": "study"}}]
        )
        # 单独一个 ctrl 被当成"按住修饰键", 不参与主键匹配
        await scheduler._on_bus_event({"source": "host_keyboard", "text": "[key ctrl]"})
        self.assertIs(state.current(), State.IDLE)

    async def test_json_form_fires(self):
        scheduler, state, _ = self._scheduler(
            [{"keys": ["ctrl", "alt", "s"], "action": {"state": "study"}}]
        )
        await scheduler._on_bus_event({
            "source": "host_keyboard",
            "text": json.dumps({"type": "key", "key": "s", "modifier": 6, "action": "press"}),
        })
        self.assertIs(state.current(), State.STUDY)

    async def test_bus_timestamp_recorded(self):
        scheduler, _, _ = self._scheduler(
            [{"keys": ["ctrl", "s"], "action": {"prompt": "x"}}]
        )
        detail = await scheduler._on_bus_event(
            {"source": "host_keyboard", "text": "[key s modifier=2]", "timestamp": 42.5}
        )
        self.assertEqual(detail["bus_timestamp"], 42.5)
        self.assertEqual(detail["key"], "s")
        self.assertEqual(detail["modifiers"], ["ctrl"])


class TestListenHotkeyLoop(unittest.IsolatedAsyncioTestCase):
    """走真实 bus 的订阅版本 (验证 listen_hotkey 与 ChatInputBus 配合)。"""

    async def test_subscription_fires_and_does_not_steal_events(self):
        bus = ChatInputBus()
        config = {"scheduler": {"hotkeys": [
            {"keys": ["ctrl", "alt", "g"], "action": {"state": "game"}}]}}
        state = StateMachine()
        scheduler = Scheduler(state=state, bus=bus, config=config)

        await scheduler.start()
        await asyncio.sleep(0)

        # 终端用户消息 + 一条快捷键 —— 两者都在同一条 bus 上
        await bus.push("terminal", "帮我查一下天气")
        await bus.push("host_keyboard", "[key ctrl]")
        await bus.push("host_keyboard", "[key alt]")
        await bus.push("host_keyboard", "[key g]")
        await asyncio.sleep(0.05)

        self.assertIs(state.current(), State.GAME, "快捷键应当切到 GAME")
        await scheduler.stop()

        # 订阅者只看不取: 四条事件一条都不该被吃掉
        remaining = []
        while True:
            event = bus.get_nowait()
            if event is None:
                break
            remaining.append((event["source"], event["text"]))
        self.assertIn(("terminal", "帮我查一下天气"), remaining,
                      "终端消息绝不能被调度器吃掉")
        self.assertIn(("host_keyboard", "[key g]"), remaining,
                      "按键事件也该留给下游")
        # 触发消息也在队列里 (不是被自己消费掉)
        self.assertIn(("scheduler", "快捷键 ctrl+alt+g"), remaining)

    async def test_stop_unsubscribes(self):
        bus = ChatInputBus()
        scheduler = Scheduler(
            state=StateMachine(), bus=bus,
            config={"scheduler": {"hotkeys": [{"keys": ["ctrl", "g"], "action": {}}]}},
        )
        await scheduler.start()
        await asyncio.sleep(0)   # 让 listen_hotkey 协程真正跑起来并订阅
        self.assertEqual(bus.subscriber_count, 1)
        await scheduler.stop()
        self.assertEqual(bus.subscriber_count, 0, "stop 后必须注销订阅")

    async def test_fake_bus_without_subscribe_warns(self):
        # 只提供 get() 的替身: 监听退化, 但要有明确 warning (静默失效更难查)
        scheduler, state, _, _ = make_scheduler(
            {"scheduler": {"hotkeys": [{"keys": ["ctrl", "g"], "action": {"state": "game"}}]}}
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
