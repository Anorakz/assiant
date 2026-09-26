#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_schedule_parity.py — 跨实现一致性守卫（C++ 日程解析 vs Python 真语义）

跑法:
    python tests/test_schedule_parity.py            # 校验（默认）
    python tests/test_schedule_parity.py --write    # 重新生成期望

为什么需要它
    GUI 的日程区按方案 C2 直接读 config.yaml 的 scheduler 段，于是 C++ 里
    （gui/src/core/schedule_model.cpp）必须**镜像** agent/core/scheduler.py 的语义。
    镜像会漂：哪天 Python 侧改了 parse_clock / _weekday_index / occurs_on，
    C++ 侧不会自动跟着改，而且不会有任何测试变红。这个守卫就是那道红线。

怎么守（四步，缺一不可）
    1. `tests/data/schedule_parity/*.yaml` 是**手写**的输入夹具；
    2. `tests/data/schedule_parity/*.expect.json` 是期望，由本文件用**真的**
       `Scheduler` / `ScheduleEvent` 生成（不是手抄的，也不是 C++ 生成的）；
    3. 本文件默认重新生成一遍并与入库的期望逐字段比对 —— **Python 语义变了就红**；
    4. C++ 侧 `gui/tests/test_schedule_model.cpp::parityWithPythonFixtures` 读同一份
       夹具 + 同一份期望做断言 —— **C++ 镜像变了也红**。

    ⚠ 重新生成请在**板端**做（`python3.8`）—— 那是 Agent 真正跑的解释器。
      不同 Python 版本对日期的宽容度不一样（3.11+ 的 `date.fromisoformat` 收
      `"20260920"` 这种紧凑写法，3.8 不收），所以夹具只用**各版本行为一致**的写法。

显示规则（本守卫固定的"展开"规则；两边必须一致）
    · 固定两段：今天 / 明天；每段按 (time, title) 升序
    · `past` = 今天段里 now 已过 start（纯时间比较，**不代表** Agent 触发过）
    · 不展示 remind_before_min（那是 Agent 的触发语义）
    · 时间统一成两位 `HH:MM`
"""

import json
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import yaml  # noqa: E402

from agent.core import Scheduler, SchedulerError, StateMachine  # noqa: E402

#: 两边写死同一个"现在"。2026-09-21 是**周一**，所以 mon/0 命中今天、tue/1 命中明天。
NOW = datetime(2026, 9, 21, 15, 0)

FIXTURE_DIR = _PROJECT_ROOT / "tests" / "data" / "schedule_parity"

#: 至少这么多份夹具（反空转：改了目录/glob 导致一个都没扫到时要报出来）
MIN_FIXTURES = 6


class _NullBus:
    """Scheduler 只需要一个能 subscribe 的 bus（它不看 bus 的内容）。"""

    def subscribe(self, *args, **kwargs):        # noqa: D401
        return lambda: True


def _load_events(text):
    """用**真的** Scheduler 装载夹具 → (events, 错误类型名 或 None)。

    走 Scheduler 而不是单个 ScheduleEvent.from_config：这样连
    `_load_events` 的"逐键回落顶层"和"整份拒绝"的行为一起被钉住。
    """
    data = yaml.safe_load(text)
    try:
        scheduler = Scheduler(state=StateMachine(), bus=_NullBus(), config=data)
    except SchedulerError:
        return [], "SchedulerError"
    except Exception as exc:                     # noqa: BLE001 - 记下类型就够了
        return [], type(exc).__name__
    return list(scheduler.events), None


def _expand(events):
    """今天 / 明天两段（显示规则见模块开头）。

    @note T12-4: 行 = **时间 + 状态**（没有 title/end 了）。
    """
    today = NOW.date()
    tomorrow = today + timedelta(days=1)
    now_minute = NOW.hour * 60 + NOW.minute

    days = []
    for label, day in (("今天", today), ("明天", tomorrow)):
        rows = []
        for event in events:
            if not event.occurs_on(day):         # 真的 Python 语义
                continue
            start_minute = event.start[0] * 60 + event.start[1]
            rows.append({
                "time": "%02d:%02d" % event.start,
                "state": event.state.value,
                "past": bool(label == "今天" and now_minute > start_minute),
            })
        rows.sort(key=lambda row: (row["time"], row["state"]))
        days.append({"label": label, "date": day.isoformat(), "rows": rows})
    return days


def expectation_for(path):
    """一份夹具的期望（就是 C++ 侧要比的那个 dict）。"""
    text = path.read_text(encoding="utf-8")
    events, error_name = _load_events(text)
    return {
        "fixture": path.name,
        "now": NOW.isoformat(),
        "python_ok": error_name is None,
        "python_error": error_name,
        "total_in_config": len(events),
        "days": _expand(events),
    }


def _fixtures():
    return sorted(FIXTURE_DIR.glob("*.yaml"))


def _expect_path(fixture):
    return fixture.with_suffix(".expect.json")


def _describe_diff(path, committed, actual):
    lines = ["%s: 入库的期望与现在的 Python 语义不一致" % path.name]
    for key in sorted(set(committed) | set(actual)):
        if committed.get(key) != actual.get(key):
            lines.append("  字段 %s:" % key)
            lines.append("    入库: %s" % json.dumps(committed.get(key), ensure_ascii=False))
            lines.append("    现在: %s" % json.dumps(actual.get(key), ensure_ascii=False))
    return "\n".join(lines)


class TestScheduleParity(unittest.TestCase):
    """C++ 的日程镜像必须与 Python 真语义对齐（经由入库的期望文件）。"""

    def test_expectations_are_current(self):
        fixtures = _fixtures()
        self.assertGreaterEqual(len(fixtures), MIN_FIXTURES,
                                "夹具太少，扫描目录可能不对：%r" % (FIXTURE_DIR,))
        problems = []
        for path in fixtures:
            expected_path = _expect_path(path)
            if not expected_path.is_file():
                problems.append("%s: 缺少期望文件（跑 --write 生成）" % expected_path.name)
                continue
            committed = json.loads(expected_path.read_text(encoding="utf-8"))
            actual = expectation_for(path)
            if committed != actual:
                problems.append(_describe_diff(path, committed, actual))
        if problems:
            self.fail("有 %d 份夹具对不上（改语义要同时改 C++ 镜像与文档，然后 --write）:\n%s"
                      % (len(problems), "\n".join(problems)))

    def test_scan_is_not_vacuous(self):
        """防止"一份夹具都没扫到"这种假绿灯，也防止期望文件全是空的。"""
        fixtures = _fixtures()
        self.assertGreaterEqual(len(fixtures), MIN_FIXTURES)

        with_rows = 0
        with_failure = 0
        for path in fixtures:
            data = expectation_for(path)
            if any(day["rows"] for day in data["days"]):
                with_rows += 1
            if not data["python_ok"]:
                with_failure += 1
        self.assertGreater(with_rows, 0, "没有任何夹具产出日程行，夹具或展开规则写错了")
        self.assertGreater(with_failure, 0, "没有任何'Python 拒绝'的夹具，坏数据路径没被覆盖")

    def test_now_is_the_documented_monday(self):
        self.assertEqual(NOW.weekday(), 0)       # 0 = 周一
        self.assertEqual(NOW.date().isoformat(), "2026-09-21")


def main(argv):
    write = "--write" in argv
    fixtures = _fixtures()
    if not fixtures:
        print("没有找到夹具：%s" % FIXTURE_DIR)
        return 1

    changed = 0
    for path in fixtures:
        expected_path = _expect_path(path)
        data = expectation_for(path)
        text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if write:
            expected_path.write_text(text, encoding="utf-8")
            print("写入 %s" % expected_path.name)
            changed += 1
        else:
            committed = (expected_path.read_text(encoding="utf-8")
                         if expected_path.is_file() else None)
            if committed != text:
                print("不一致: %s" % expected_path.name)
                changed += 1
    if write:
        print("共写入 %d 份期望" % changed)
        return 0
    print("共 %d 份夹具，%d 份不一致" % (len(fixtures), changed))
    return 0 if changed == 0 else 1


if __name__ == "__main__":
    if "--write" in sys.argv:
        sys.exit(main(sys.argv))
    unittest.main(verbosity=2)
