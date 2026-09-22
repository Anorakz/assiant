# ============================================================================
#  agent/core/scheduler.py — 日程检查 + 定时触发 + 终端命令监听
#
#  它在链路里的位置
#  ---------------------------------------------------------------------------
#      Scheduler.start()
#        ├── 日程循环:   每 interval_min 分钟 check_schedule()  → 到点就触发
#        └── 命令循环:   从 ChatInputBus 旁观 source=="terminal" 的整行文本
#                        → 与配置里的命令名比 → 命中就触发
#
#  触发动作只有两种 (按约定)
#  ---------------------------------------------------------------------------
#      1. 状态转换      state_machine.transition(目标状态, reason)
#      2. 发消息给 Agent bus.push("scheduler", 文本)  ← 和终端/GUI 同一个入口
#    行动作本来就是单条 dict, 组合起来是"切到 GAME 并告诉 Agent 该开局了"。
#
#  关于"同步系统时间"
#  ---------------------------------------------------------------------------
#  ⚠ 本模块**不设置**系统时间。实测板端 (Ubuntu 20.04 / Python 3.8):
#      · systemd-timesyncd 未运行, CanNTP=no, NTP 服务缺失
#      · ntplib / zoneinfo / pytz 都没装
#      · 但 System clock synchronized: yes (开机时已同步过)
#    在没有 NTP 服务、也没有客户端库的前提下,"同步时间"只能退化成
#    **校验本机时间是否明显不对** (见 sync_time())。真要联网校时, 应该在板端
#    配 systemd-timesyncd 或 chrony —— 那是运维的事, 不该由一个调度器去做
#    (它没有权限, 也会把"时间不对"变成静默的网络依赖)。
#
#  设计边界 (按约定不做的事)
#  ---------------------------------------------------------------------------
#  · **不做** cron 表达式解析 —— 配置里就是 days + "HH:MM" 这种直白写法
#  · **不做** 日程持久化 —— 只读 config; 重启后同一时间窗内会再触发一次
#    (触发记录只在内存里, 见文档 "去重" 一节)
#
#  去重 (为什么需要)
#  ---------------------------------------------------------------------------
#  check_schedule() 会被反复调用 (每 interval_min 一次, 也可能被手工调用),
#  但"到点该触发"这件事只能生效一次。所以按 (日期, 事件, 起始分钟) 记录已触发,
#  同一时间窗内重复调用不会重复触发。窗口宽度由 window_min 控制 (默认 1 分钟,
#  即"起始那一分钟")。
#
#  触发事实 (回答"到底触发过没有、什么时候")
#  ---------------------------------------------------------------------------
#  去重集合 _fired 只回答"这条触发过没" (而且是 (日期, key) 的集合), 说不出
#  "什么时候触发的"。所以 _fire() 成功后再往 _history 记一条**事实** (有界 deque,
#  默认 DEFAULT_HISTORY_LIMIT 条): title / date / scheduled_at / fired_at / actions。
#  它是给 IPC 的 "schedule" topic 与 CLI 的 `assistant schedule` 看的 —— 即
#  "本进程内真的发生过的事", 与"按时间比较出来的 已过"是两回事 (后者与 Agent
#  有没有跑、有没有触发完全无关)。
#
#  ⚠ 历史是**旁路**: 满了丢最旧的, 回调坏了只记一行日志 —— _fired / _prune_fired /
#    _in_window 的语义一个都没动, 触发判定不受它影响。
# ============================================================================

from __future__ import annotations

import asyncio
import time as _time
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Deque, Dict, List, Optional, Set, Tuple

from .state_machine import State, StateMachine

__all__ = [
    "Scheduler",
    "ScheduleEvent",
    "CommandBinding",
    "SchedulerError",
    "TERMINAL_SOURCE",
    "parse_clock",
    "normalize_command",
    "parse_command_config",
    "WEEKDAYS",
    "DEFAULT_INTERVAL_MIN",
    "DEFAULT_WINDOW_MIN",
    "DEFAULT_HISTORY_LIMIT",
]
#: 日程检查的默认间隔 (分钟) —— 需求: 根据 config 配置的时间间隔单位 min
DEFAULT_INTERVAL_MIN = 1

#: 触发窗口默认宽度 (分钟)。窗口内第一次 check 触发, 之后不再重复。
DEFAULT_WINDOW_MIN = 1

#: 触发事实保留条数上限。只是"给客户端看最近真发生了什么", 满了丢最旧的 ——
#: 它**不是**去重依据 (那个是 _fired), 也不参与任何触发判定。
DEFAULT_HISTORY_LIMIT = 50

#: 允许的星期缩写 (与 schedule.example.yaml 一致)
WEEKDAYS: Tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

#: 命令只从这个来源认 —— 终端里敲的那一行 (见 normalize_command)。
#: 为什么不认 gui: 匹配是"整行相等", 让界面里的聊天文本也能触发状态切换很难查,
#: 而终端是唯一"敲一行再回车"的地方。
TERMINAL_SOURCE = "terminal"


class SchedulerError(ValueError):
    """调度器配置/参数错误。继承 ValueError, 与其他模块一致。"""


def _log_task_exception(task: "asyncio.Task") -> None:
    """吃掉订阅者协程的异常并记下来 (没有它会有 "Task exception was never retrieved")。"""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        print("Scheduler: background action raised: %r" % (exc,))


# ---------------------------------------------------------------------------
#  时间解析
# ---------------------------------------------------------------------------
def parse_clock(text: Any) -> Tuple[int, int]:
    """把 "09:30" / "9:30" / "0930" 解析成 (hour, minute)。

    @raise SchedulerError 格式不对或超范围
    """
    if not isinstance(text, str):
        raise SchedulerError("clock must be a string like 'HH:MM', got %r" % (text,))

    raw = text.strip().replace("：", ":")  # 全角冒号容错
    if ":" in raw:
        parts = raw.split(":")
        if len(parts) != 2:
            raise SchedulerError("clock must be 'HH:MM', got %r" % (text,))
        hh, mm = parts[0].strip(), parts[1].strip()
    else:
        # "0930" 这种紧凑写法也收
        if len(raw) != 4 or not raw.isdigit():
            raise SchedulerError("clock must be 'HH:MM', got %r" % (text,))
        hh, mm = raw[:2], raw[2:]

    if not hh.isdigit() or not mm.isdigit():
        raise SchedulerError("clock must be digits 'HH:MM', got %r" % (text,))

    hour, minute = int(hh), int(mm)
    if not (0 <= hour <= 23):
        raise SchedulerError("clock hour must be 0..23, got %d (in %r)" % (hour, text))
    if not (0 <= minute <= 59):
        raise SchedulerError("clock minute must be 0..59, got %d (in %r)" % (minute, text))
    return hour, minute


def _weekday_index(value: Any) -> int:
    """'mon' / 0 / 'MON' -> 0..6 (周一=0)。"""
    if isinstance(value, int) and not isinstance(value, bool):
        if 0 <= value <= 6:
            return value
        raise SchedulerError("weekday index must be 0..6, got %r" % (value,))
    if isinstance(value, str):
        name = value.strip().lower()
        if name in WEEKDAYS:
            return WEEKDAYS.index(name)
        # 容忍 'monday' 这类完整写法
        for i, short in enumerate(WEEKDAYS):
            if name.startswith(short):
                return i
    raise SchedulerError("unknown weekday %r (expected one of %s)" % (value, list(WEEKDAYS)))


# ---------------------------------------------------------------------------
#  日程事件
# ---------------------------------------------------------------------------
@dataclass
class ScheduleEvent:
    """一条日程。

    @param source_id  来源标记, 用来生成稳定的去重 key (重复 cron 之类的不做,
                      但同一个 title 在不同天应各自触发)
    @param title      名字 (触发理由里会带上)
    @param kind       "recurring" | "oneoff"
    @param start      起始 (hour, minute)
    @param end        结束 (hour, minute); 可为 None (只关心起始)
    @param days       仅 recurring: 生效的星期 (0=周一); 空集合 = 每天
    @param on         仅 oneoff: 生效日期
    @param remind_before_min 提前多少分钟触发
    @param action     触发动作 dict: {"state": "game", "prompt": "...", "reason": "..."}
    """
    source_id: str
    title: str
    kind: str
    start: Tuple[int, int]
    end: Optional[Tuple[int, int]] = None
    days: Set[int] = field(default_factory=set)
    on: Optional[date] = None
    remind_before_min: int = 0
    action: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------ 计算 ---
    def start_minute(self) -> int:
        return self.start[0] * 60 + self.start[1]

    def trigger_minute(self) -> int:
        """应当触发的分钟数 (= 起始 - 提前量)。"""
        return self.start_minute() - int(self.remind_before_min)

    def occurs_on(self, day: date) -> bool:
        if self.kind == "oneoff":
            return self.on == day
        if self.kind == "recurring":
            return not self.days or day.weekday() in self.days
        return False

    def trigger_at(self, day: date) -> datetime:
        """当天的触发时刻 (可能落在前一天, 若提前量跨零点)。

        @note 用 (day 00:00 + 分钟数) 而不是 replace(hour=..., minute=...),
              因为提前量可能让触发时刻变成负数分钟 -> 自然落到前一天。
        """
        return datetime(day.year, day.month, day.day) + timedelta(
            minutes=self.trigger_minute()
        )

    def dedup_key(self, day: date) -> str:
        """同一天内同一条日程只触发一次。"""
        return "%s|%s|%04d-%02d-%02d|%d" % (
            self.source_id, self.title[:64], day.year, day.month, day.day,
            self.trigger_minute(),
        )
    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "kind": self.kind,
            "start": "%02d:%02d" % self.start,
            "end": None if self.end is None else "%02d:%02d" % self.end,
            "days": sorted(self.days) if self.days else "every-day",
            "date": self.on.isoformat() if self.on else None,
            "remind_before_min": self.remind_before_min,
            "action": dict(self.action),
        }

    @classmethod
    def from_config(cls, entry: Dict[str, Any], index: int) -> "ScheduleEvent":
        """从 config 的一条日程构造。

        @raise SchedulerError 缺字段/字段类型不对/时间格式不对
        """
        if not isinstance(entry, dict):
            raise SchedulerError("schedule entry #%d must be an object" % index)

        title = entry.get("title")
        if not isinstance(title, str) or not title.strip():
            raise SchedulerError("schedule entry #%d needs a non-empty title" % index)
        title = title.strip()

        has_date = entry.get("date") is not None
        has_days = entry.get("days") is not None
        if has_date and has_days:
            raise SchedulerError(
                "schedule entry %r must not have both 'date' and 'days'" % title
            )

        kind = "oneoff" if has_date else "recurring"

        if "start" not in entry:
            raise SchedulerError("schedule entry %r needs a 'start' time" % title)
        start = parse_clock(entry["start"])
        end = parse_clock(entry["end"]) if entry.get("end") is not None else None
        if end is not None and end < start:
            raise SchedulerError(
                "schedule entry %r has end before start (%s < %s)"
                % (title, entry["end"], entry["start"])
            )

        remind = entry.get("remind_before_min", 0)
        if not isinstance(remind, int) or isinstance(remind, bool) or remind < 0:
            raise SchedulerError(
                "schedule entry %r: remind_before_min must be a non-negative int, got %r"
                % (title, remind)
            )

        on: Optional[date] = None
        if has_date:
            try:
                on = date.fromisoformat(str(entry["date"]).strip())
            except ValueError:
                raise SchedulerError(
                    "schedule entry %r: date must be YYYY-MM-DD, got %r"
                    % (title, entry["date"])
                ) from None

        days: Set[int] = set()
        if has_days:
            raw_days = entry["days"]
            if not isinstance(raw_days, (list, tuple, set)):
                raise SchedulerError(
                    "schedule entry %r: days must be a list, got %r" % (title, raw_days)
                )
            for value in raw_days:
                days.add(_weekday_index(value))

        action = entry.get("action") or {}
        if not isinstance(action, dict):
            raise SchedulerError("schedule entry %r: action must be an object" % title)

        return cls(
            source_id="schedule",
            title=title,
            kind=kind,
            start=start,
            end=end,
            days=days,
            on=on,
            remind_before_min=remind,
            action=dict(action),
        )


# ---------------------------------------------------------------------------
#  终端命令
# ---------------------------------------------------------------------------
def normalize_command(text: Any) -> str:
    """把一行终端输入归一成用于比较的命令名。

    规则只有两条: **去掉首尾空白** + **大小写不敏感**。不做分词、不做前缀、
    不做缩写 —— 匹配是"整行相等", 少一层规则就少一处两边理解不一致的地方
    (这也是原来那套 `[key c modifier=6]` 文本解析被删掉的原因: 它要求生产端
    按特定格式渲染, 而那个生产端根本不存在)。

    非字符串 / 空串归一成 ""(调用方按"不匹配"处理)。
    """
    if not isinstance(text, str):
        return ""
    return text.strip().lower()


@dataclass(frozen=True)
class CommandBinding:
    """一条终端命令绑定。

    @param command 命令名, 已归一 (strip + lower): 终端里整行敲它就触发
    @param action  触发动作 dict
    @param label   配置里原本怎么写的 (回显用)
    """

    command: str
    action: Dict[str, Any]
    label: str


def parse_command_config(config: Any) -> List[CommandBinding]:
    """从 config 的 `commands` 段读命令映射。

    接受两种写法::

        commands:
          - command: "study"                  # 列表
            action: {state: study, prompt: "开始学习"}

        commands:
          "study":                            # 也接受 "命令名" 作 key
            state: study
            prompt: "开始学习"

    动作 dict 与日程事件共用一套词汇: `state` / `reason` / `prompt`。

    @raise SchedulerError 格式不对 (不是 list/dict、缺 command、命令名重复)
    """
    if config is None:
        return []
    if not isinstance(config, (list, tuple, dict)):
        raise SchedulerError(
            "commands must be a list or an object, got %r" % type(config).__name__
        )

    bindings: List[CommandBinding] = []

    if isinstance(config, dict):
        for label, action in config.items():
            command = normalize_command(label)
            if not command:
                raise SchedulerError("command name must be a non-empty string, got %r"
                                     % (label,))
            bindings.append(
                CommandBinding(command=command,
                               action=_as_action(action, label),
                               label=str(label))
            )
    else:
        for index, entry in enumerate(config):
            if not isinstance(entry, dict):
                raise SchedulerError("command entry #%d must be an object" % index)
            raw = entry.get("command")
            if raw is None:
                raise SchedulerError("command entry #%d needs 'command'" % index)
            if not isinstance(raw, str):
                raise SchedulerError(
                    "command entry #%d: command must be a string, got %r"
                    % (index, type(raw).__name__)
                )
            command = normalize_command(raw)
            if not command:
                raise SchedulerError("command entry #%d has an empty command" % index)
            bindings.append(
                CommandBinding(
                    command=command,
                    action=_as_action(entry.get("action", entry), raw),
                    label=raw,
                )
            )

    # 同一条命令绑了两次: 报错而不是偷偷用最后一个
    seen: Set[str] = set()
    for binding in bindings:
        if binding.command in seen:
            raise SchedulerError("duplicate command binding: %s" % binding.label)
        seen.add(binding.command)

    return bindings


def _as_action(value: Any, label: str) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SchedulerError("command %r action must be an object, got %r" % (label, value))
    action = dict(value)
    # 允许简写: 直接 {state: study} 而不是 {action: {state: study}}
    if "action" in action and isinstance(action["action"], dict):
        merged = dict(action["action"])
        merged.update({k: v for k, v in action.items() if k != "action"})
        action = merged
    return action


# ---------------------------------------------------------------------------
#  Scheduler
# ---------------------------------------------------------------------------
class Scheduler:
    """日程检查 + 定时触发 + 终端命令监听。

    典型用法::

        scheduler = Scheduler(state=sm, bus=bus, config=cfg)
        await scheduler.start()          # 起两个后台循环
        ...
        await scheduler.stop()

        # 也可以手动跑一轮 (测试/诊断)
        fired = await scheduler.check_schedule()
    """

    def __init__(
        self,
        state: StateMachine,
        bus: Any,
        config: Optional[Dict[str, Any]] = None,
        clock: Optional[Callable[[], datetime]] = None,
        monotonic: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], Any]] = None,
        history_limit: Optional[int] = None,
    ) -> None:
        """
        @param state      状态机 (触发动作里可以切状态)
        @param bus        ChatInputBus (触发动作里可以发消息; 终端命令从它读)
        @param config     配置 dict (可以是整个 config, 也可以只给 scheduler 段)
        @param clock      取当前时间 (测试注入)
        @param monotonic  取单调时钟 (测试注入, 用来做检查间隔)
        @param sleep      异步 sleep (测试注入)
        @param history_limit 触发事实保留条数 (默认 DEFAULT_HISTORY_LIMIT;
                          0 表示不记)。只影响查询/推送, 不影响触发与去重。
        """
        if state is None:
            raise SchedulerError("state (StateMachine) is required")
        if bus is None:
            raise SchedulerError("bus (ChatInputBus) is required")
        self._state = state
        self._bus = bus
        self._config = config or {}

        self._clock = clock or datetime.now
        self._monotonic = monotonic or _time.monotonic
        self._sleep = sleep or asyncio.sleep

        section = self._scheduler_section(self._config)

        # ---- 配置项 ----
        interval = section.get("interval_min", DEFAULT_INTERVAL_MIN)
        self.interval_min = self._positive_number(interval, "interval_min")
        window = section.get("window_min", DEFAULT_WINDOW_MIN)
        self.window_min = self._positive_number(window, "window_min")
        #: 迟到的容忍度 (分钟): 设备休眠/重启后晚了几分钟仍认这次触发。
        #: 有效可触发区间 = max(window_min, late_grace_min), 去重保证只触发一次。
        grace = section.get("late_grace_min", 0)
        self.late_grace_min = self._non_negative_int(grace, "late_grace_min")

        self._events: List[ScheduleEvent] = self._load_events(section)
        self._bindings: List[CommandBinding] = parse_command_config(section.get("commands"))

        # ---- 运行态 ----
        self._tasks: List[asyncio.Task] = []
        self._fired: Set[Tuple[date, str]] = set()
        #: 触发事实 (有界, 只供查询/推送; 见模块头 "触发事实")
        self._history_limit = (
            DEFAULT_HISTORY_LIMIT if history_limit is None
            else self._non_negative_int(history_limit, "history_limit")
        )
        self._history: Deque[Dict[str, Any]] = deque(maxlen=self._history_limit)
        #: 每次真的触发一条日程后调一次 (IPC 用它做实时推送)。默认没接。
        self._on_fire: Optional[Callable[[Dict[str, Any]], Any]] = None
        self._last_check_at: Optional[float] = None
        self._checks = 0
        self._triggers = 0
        self._command_hits = 0
        self._warnings: List[str] = []
        self._running = False
        #: listen_commands 建立的订阅句柄 (stop 时注销)
        self._unsubscribe: Optional[Callable[[], bool]] = None
        #: 最近一次命中的命令 (诊断用)
        self._last_command: Optional[Dict[str, Any]] = None

        # 旧键名 (hotkeys) 已改名为 commands。板端实盘配置里可能还留着它 ——
        # 静默忽略会让"我配了命令却没生效"变成难查的问题, 所以记一条 warning。
        if "hotkeys" in section:
            self._warnings.append(
                "配置里的 scheduler.hotkeys 已改名为 scheduler.commands, "
                "本次被忽略 (见 docs/architecure.md 与 config.example.yaml)"
            )

    # ------------------------------------------------------------ 配置 ---
    @staticmethod
    def _scheduler_section(config: Dict[str, Any]) -> Dict[str, Any]:
        """兼容"整个 config"与"只给 scheduler 段"两种传法。"""
        if not isinstance(config, dict):
            return {}
        section = config.get("scheduler")
        if isinstance(section, dict):
            return section
        return config

    @staticmethod
    def _positive_number(value: Any, name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise SchedulerError("%s must be a positive number, got %r" % (name, value))
        return float(value)

    @staticmethod
    def _non_negative_int(value: Any, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SchedulerError("%s must be a non-negative int, got %r" % (name, value))
        return value

    def _load_events(self, section: Dict[str, Any]) -> List[ScheduleEvent]:
        """把 recurring / oneoff 读成 ScheduleEvent。

        两个位置都找, 因为两种布局都合理:
          · {"scheduler": {"recurring": [...]}}   —— 日程属于调度器
          · {"recurring": [...]}                  —— 整个 dict 就是 scheduler 段
          · {"scheduler": {...}, "recurring": [...]}
                                                  —— 日程来自 schedule.yaml 那种
                                                     顶层就是 recurring/oneoff 的配置,
                                                     这里同时带了 interval 等参数
        """
        events: List[ScheduleEvent] = []
        for key in ("recurring", "oneoff"):
            entries = section.get(key)
            if entries is None:
                entries = self._config.get(key) if isinstance(self._config, dict) else None
            if entries is None:
                continue
            if not isinstance(entries, (list, tuple)):
                raise SchedulerError("%s must be a list, got %r" % (key, type(entries).__name__))
            for index, entry in enumerate(entries):
                events.append(ScheduleEvent.from_config(entry, index))
        return events

    @property
    def events(self) -> List[ScheduleEvent]:
        return list(self._events)

    @property
    def bindings(self) -> List[CommandBinding]:
        return list(self._bindings)

    # ------------------------------------------------------------ 时钟 ---
    def sync_time(self) -> Dict[str, Any]:
        """检查本机时间是否明显不对。

        ⚠ **不修改系统时间**。板端没有 NTP 服务/客户端库 (实测 CanNTP=no、
        ntplib 未装), 从用户态去设时间是做不到也不该做的 —— 真要联网校时应该
        在板端配 systemd-timesyncd / chrony。

        @return {"now":..., "plausible": bool, "warnings": [...]}
        """
        now = self._clock()
        warnings: List[str] = []

        # 板子没有 RTC 时开机会回到 1970; 或者时间被设成很久以后。
        # 用两个宽松边界只挡"明显不可能"的情况, 不做精确校时。
        if now.year < 2020:
            warnings.append(
                "system clock looks unset (%s) —— 没有 RTC/NTP 时开机可能回到 1970, "
                "日程与提醒都会失准" % now.isoformat()
            )
        elif now.year > 2100:
            warnings.append("system clock looks far in the future (%s)" % now.isoformat())

        self._warnings = warnings
        return {"now": now, "plausible": not warnings, "warnings": warnings}

    @property
    def warnings(self) -> List[str]:
        return list(self._warnings)

    # ------------------------------------------------------------ 生命周期 --
    async def start(self) -> None:
        """启动两个后台循环。重复调用是幂等的。"""
        if self._running:
            return
        self._running = True
        self.sync_time()
        self._tasks = [
            asyncio.create_task(self._schedule_loop(), name="scheduler-schedule"),
            asyncio.create_task(self.listen_commands(), name="scheduler-commands"),
        ]

    async def stop(self) -> None:
        """停止后台循环并注销订阅。可重复调用。"""
        self._running = False
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass

    @property
    def running(self) -> bool:
        return self._running

    async def _schedule_loop(self) -> None:
        """按 interval_min 间隔反复检查日程。

        间隔判定用**单调时钟**: 系统时间被校时跳一下不该让检查节奏乱掉。
        """
        interval_s = self.interval_min * 60.0
        while True:
            try:
                await self.check_schedule()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 一轮失败不该停掉整个循环
                self._warnings.append("check_schedule failed: %r" % (exc,))

            self._last_check_at = self._monotonic()
            # 睡到下一个检查点; 用单调时钟算剩余量, 不依赖 sleep 的精度
            remaining = interval_s
            while remaining > 0:
                step = min(remaining, 5.0)   # 每 5 秒醒一次, 便于及时响应 stop
                await self._sleep(step)
                remaining = interval_s - (self._monotonic() - (self._last_check_at or 0))
                if not self._running:
                    return

    # ------------------------------------------------------------ 日程 ---
    async def check_schedule(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """检查"现在该做什么", 到点就触发。

        @param now 覆盖当前时间 (测试用; 也给"补跑某个时刻"留了口子)
        @return 本次真正触发的事件列表 (没有就是 [])
        @note 同一时间窗内重复调用不会重复触发 (见模块头 "去重")。
        """
        moment = now if now is not None else self._clock()
        self._checks += 1
        fired: List[Dict[str, Any]] = []

        for event in self._events:
            # 提前量可能把触发时刻推到**前一天** (例如 00:05 提前 10 分钟 -> 前一天
            # 23:55), 所以要同时看"今天"和"明天"这两个事件日:
            #   今天的事件 -> 触发点可能落在今天或昨天(已被去重挡住)
            #   明天的事件 -> 触发点可能落在今天
            # 反过来看昨天是错的: 昨天的事件其触发点只会在昨天或前天, 与本刻无关。
            for day in (moment.date(), moment.date() + timedelta(days=1)):
                if not event.occurs_on(day):
                    continue
                trigger_at = event.trigger_at(day)
                if not self._in_window(moment, trigger_at):
                    continue

                key = (day, event.dedup_key(day))
                if key in self._fired:
                    continue
                self._fired.add(key)

                detail = await self._fire(event, day, trigger_at, moment)
                fired.append(detail)

        self._prune_fired(moment.date())
        return fired

    def _in_window(self, moment: datetime, trigger_at: datetime) -> bool:
        """moment 是否落在"可触发区间"里。

        区间是 [trigger_at, trigger_at + accept_after), 其中

            accept_after = max(window_min, late_grace_min)

        · window_min      正常的时间窗 (默认 1 分钟)
        · late_grace_min  "迟到的容忍度": 设备休眠/重启后晚了几分钟, 仍然认。
                          0 (默认) 表示只认窗口内那一下。
                          设成 10 就表示"晚 10 分钟内都算这次该触发"。
                          去重仍然保证只触发一次, 所以放宽它不会重复触发。

        两者取 max 而不是相加: 相加会让"窗口 1 分钟 + 容忍 10 分钟"变成 11 分钟,
        语义上和"容忍 10 分钟"没区别, 反而更难解释。
        """
        accept_after = max(self.window_min, float(self.late_grace_min))
        delta_min = (moment - trigger_at).total_seconds() / 60.0
        return 0.0 <= delta_min < accept_after

    def _prune_fired(self, today: date) -> None:
        """丢掉不再需要的去重记录。

        去重 key 是 (哪一天, 字符串 key) —— 用日期对象做元组第一项, 清理时按它
        过滤, 不用去解析字符串。只保留今天和昨天: 提前量最多把触发推到前一天,
        更早的记录不可能再被命中。
        """
        keep = {today, today - timedelta(days=1)}
        self._fired = {item for item in self._fired if item[0] in keep}

    async def _fire(
        self, event: ScheduleEvent, day: date, trigger_at: datetime, moment: datetime
    ) -> Dict[str, Any]:
        """执行一条日程的触发动作。"""
        reason = event.action.get("reason") or ("日程: %s" % event.title)
        detail: Dict[str, Any] = {
            "kind": "schedule",
            "title": event.title,
            "date": day.isoformat(),
            "scheduled_at": trigger_at.isoformat(timespec="minutes"),
            "now": moment.isoformat(timespec="seconds"),
            "actions": [],
        }

        detail["actions"] += await self._apply_action(event.action, reason)

        text = event.action.get("prompt")
        if not text:
            text = "日程提醒：%s" % event.title
        pushed = await self._bus.push("scheduler", text)
        detail["actions"].append({"type": "message", "text": text, "timestamp": pushed["timestamp"]})

        self._record_fired(detail)
        self._triggers += 1
        return detail

    async def _apply_action(self, action: Dict[str, Any], reason: str) -> List[Dict[str, Any]]:
        """把 action dict 落到状态机/消息上。

        认得的键:
            state   目标状态 (str 或 State) —— 非法转换会被记进结果, 不抛异常
            reason  转换理由 (不给就用默认)
            prompt  要发给 Agent 的文本 (不给就没消息)
        """
        done: List[Dict[str, Any]] = []

        target = action.get("state")
        if target is not None:
            ok = self._state.transition(target, action.get("reason") or reason)
            done.append(
                {
                    "type": "state",
                    "state": getattr(target, "value", str(target)),
                    "ok": bool(ok),
                    "current": self._state.current().value,
                }
            )
        return done

    # -------------------------------------------------------- 终端命令 ---
    async def listen_commands(self) -> None:
        """订阅 bus, 把终端里的一行文本当成命令认, 命中就触发。

        ⚠ **不消费 bus 里的事件**: 用 bus.subscribe() 旁观, 而不是 get()。
        早期版本用 get() 循环读, 会把终端/GUI 的用户消息一起吃进调度器,
        下游再也看不到 (还会吃掉调度器自己 push 的触发消息)。订阅式只观察,
        事件仍然留在队列里等真正的消费者 —— 代价是**命令也会照常进 LLM**
        (见下方 @note)。

        @note 只认 source == "terminal" 的事件, 且必须**整行等于**配置里的命令名
              (去首尾空白、大小写不敏感)。其它来源 (gui) 不参与: 让界面里的
              聊天文本也能触发状态切换会很难查。

        @note ⚠ **命中不会把事件从 bus 里拿走**, 所以终端里敲 `study` 会有两个
              效果: 状态切到 STUDY, 并且 "study" 这行文本照常被主循环送给 LLM。
              这是订阅式监听的固有行为(要"只生效一次"就得让命令走一条不经过
              LLM 的通道, 那是另一个改动)。

        @note 这个方法**不返回**, 一直等到被 stop() 取消 (或订阅被取消)。
              保留它作为公开接口是为了让"起一个监听任务"这件事显式可见。
        """
        unsubscribe = self._subscribe_to_bus()
        #: 取消订阅的句柄, stop() 时用
        self._unsubscribe = unsubscribe
        try:
            # 挂住不返回; 取消这个任务即结束监听
            await asyncio.Event().wait()
        finally:
            unsubscribe()
            self._unsubscribe = None

    def _subscribe_to_bus(self) -> Callable[[], bool]:
        """把 _on_bus_event 挂到 bus 上。

        bus 需要支持 subscribe(); 只提供 get() 的替身 (测试里的简单假 bus) 会
        退化成"不监听", 并记一条 warning —— 静默不工作是更难查的问题。
        """
        subscribe = getattr(self._bus, "subscribe", None)
        if not callable(subscribe):
            self._warnings.append(
                "bus does not support subscribe(); command listening is disabled "
                "(need ChatInputBus.subscribe)"
            )

            def _noop() -> bool:
                return False

            return _noop

        def _on_event(event: Dict[str, Any]) -> None:
            # 订阅回调不能阻塞 push (我们在它的调用栈上), 也不该因为异常
            # 影响 push, 所以同步跑完 _handle 或把异常收进 warnings。
            try:
                self._dispatch_event_sync(event)
            except Exception as exc:  # noqa: BLE001
                self._warnings.append("command handling failed: %r" % (exc,))

        return subscribe(_on_event)

    def _dispatch_event_sync(self, event: Dict[str, Any]) -> None:
        """同步版本的 _on_bus_event —— 命中时把触发动作挂成后台任务。

        为什么是"挂任务"而不是直接 await: 回调在 push() 的调用栈上执行,
        不能在那里 await (会让 push 的调用方等我们做完状态转换+发消息)。
        """
        binding = self._check_event(event)
        if binding is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._warnings.append("no running loop; command action skipped")
            return
        task = loop.create_task(self._run_command_action(binding))
        task.add_done_callback(_log_task_exception)

    def _check_event(self, event: Any) -> Optional[CommandBinding]:
        """判断一条 bus 事件是不是一条已注册的命令 (纯同步, 不做任何 IO)。

        只看 source==TERMINAL_SOURCE 的事件, 并且必须**整行等于**命令名
        (去首尾空白 + 大小写不敏感, 见 normalize_command)。认不出来就返回 None
        —— 宁可漏识别, 不可误触发: 一句恰好像命令的聊天不该真的切模式。
        """
        if not isinstance(event, dict):
            return None
        if event.get("source") != TERMINAL_SOURCE:
            return None

        command = normalize_command(event.get("text"))
        if not command:
            return None

        for binding in self._bindings:
            if binding.command == command:
                self._command_hits += 1
                return binding
        return None

    async def _run_command_action(self, binding: CommandBinding) -> None:
        """执行命中命令后的动作 (状态转换 + 发消息)。"""
        reason = binding.action.get("reason") or ("命令: %s" % binding.label)
        await self._apply_action(binding.action, reason)

        text = binding.action.get("prompt")
        if not text:
            text = "命令 %s" % binding.label
        try:
            await self._bus.push("scheduler", text)
        except Exception as exc:  # noqa: BLE001
            self._warnings.append("command push failed: %r" % (exc,))
        self._triggers += 1
        self._last_command = {
            "label": binding.label,
            "command": binding.command,
        }

    async def _on_bus_event(self, event: Any) -> Optional[Dict[str, Any]]:
        """处理一条 bus 事件并等动作做完; 命中时返回触发详情。

        保留这个方法是为了让测试与"手工喂一条事件"的用法简单直接 (订阅式的
        后台任务不好断言)。生产路径走 _dispatch_event_sync。
        """
        binding = self._check_event(event)
        if binding is None:
            return None
        reason = binding.action.get("reason") or ("命令: %s" % binding.label)
        detail: Dict[str, Any] = {
            "kind": "command",
            "label": binding.label,
            "command": binding.command,
            "bus_timestamp": event.get("timestamp"),
            "actions": [],
        }
        detail["actions"] += await self._apply_action(binding.action, reason)

        text = binding.action.get("prompt")
        if not text:
            text = "命令 %s" % binding.label
        pushed = await self._bus.push("scheduler", text)
        detail["actions"].append(
            {"type": "message", "text": text, "timestamp": pushed["timestamp"]}
        )
        self._triggers += 1
        return detail

    # ------------------------------------------------------ 触发事实 (查询) ---
    @property
    def on_fire(self) -> Optional[Callable[[Dict[str, Any]], Any]]:
        """触发一条日程后的回调 (没有接就是 None)。

        @note 回调是**旁路**: 它抛异常只打一行日志, 不影响这次触发算不算成功,
              也不影响去重 —— 推送坏了不该让日程失效 (与 on_change/on_reply 同口径)。
        """
        return self._on_fire

    @on_fire.setter
    def on_fire(self, callback: Optional[Callable[[Dict[str, Any]], Any]]) -> None:
        if callback is not None and not callable(callback):
            raise SchedulerError("on_fire must be callable or None, got %r" % (callback,))
        self._on_fire = callback

    def recent_fired(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """最近触发过的日程事实 (时间正序, 只含**本进程内真的触发过**的)。

        @param limit 只要最后 limit 条 (None = 全部, 最多 history_limit 条)
        @return 事实 dict: title / date / scheduled_at / fired_at / actions
        @note 与"按时间算出来的 已过"不是一回事: 这个是"真发生过", 且**重启即清零**
              (触发记录只在内存里, 见模块头)。
        """
        if limit is None:
            return [dict(item) for item in self._history]
        count = self._non_negative_int(limit, "limit")
        if count == 0:
            return []
        return [dict(item) for item in list(self._history)[-count:]]

    def _record_fired(self, detail: Dict[str, Any]) -> None:
        """把一条触发详情记成"事实", 并通知 on_fire。

        @note 事实里把 detail 的 now 改名成 fired_at: 对客户端来说 "now" 没有意义
              (它是这次检查的时刻, 可能比 scheduled_at 晚几秒), 而"什么时候真的
              触发了"才是它要的。其余字段一一对应, 不改语义。
        """
        fact = {
            "title": detail.get("title"),
            "date": detail.get("date"),
            "scheduled_at": detail.get("scheduled_at"),
            "fired_at": detail.get("now"),
            "actions": list(detail.get("actions") or []),
        }
        self._history.append(fact)

        if self._on_fire is None:
            return
        try:
            self._on_fire(dict(fact))
        except Exception as exc:  # noqa: BLE001
            # 与 _log_task_exception 同款口径: 旁路坏了只报告, 绝不往上抛
            print("Scheduler: on_fire callback raised: %r" % (exc,))

    # ------------------------------------------------------------ 诊断 ---
    @property
    def stats(self) -> Dict[str, Any]:
        return {
            "running": self._running,
            "checks": self._checks,
            "triggers": self._triggers,
            "command_hits": self._command_hits,
            "events": len(self._events),
            "bindings": len(self._bindings),
            "fired_keys": len(self._fired),
            "fired_history": len(self._history),
            "last_command": dict(self._last_command) if self._last_command else None,
            "subscribed": self._unsubscribe is not None,
            "warnings": list(self._warnings),
        }

    def __repr__(self) -> str:
        return "<Scheduler events=%d bindings=%d interval=%gmin running=%s>" % (
            len(self._events), len(self._bindings), self.interval_min, self._running
        )
