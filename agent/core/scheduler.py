# ============================================================================
#  agent/core/scheduler.py — 日程检查 + 定时触发 + 快捷键监听
#
#  它在链路里的位置
#  ---------------------------------------------------------------------------
#      Scheduler.start()
#        ├── 日程循环:   每 interval_min 分钟 check_schedule()  → 到点就触发
#        └── 快捷键循环: 从 ChatInputBus 读事件 → 识别组合键 → 触发
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
# ============================================================================

from __future__ import annotations

import asyncio
import json
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from .state_machine import State, StateMachine

__all__ = [
    "Scheduler",
    "ScheduleEvent",
    "HotkeyBinding",
    "HotkeyEvent",
    "SchedulerError",
    "parse_clock",
    "parse_hotkey_text",
    "parse_hotkey_config",
    "WEEKDAYS",
    "DEFAULT_INTERVAL_MIN",
    "DEFAULT_WINDOW_MIN",
]
#: 日程检查的默认间隔 (分钟) —— 需求: 根据 config 配置的时间间隔单位 min
DEFAULT_INTERVAL_MIN = 1

#: 触发窗口默认宽度 (分钟)。窗口内第一次 check 触发, 之后不再重复。
DEFAULT_WINDOW_MIN = 1

#: 允许的星期缩写 (与 schedule.example.yaml 一致)
WEEKDAYS: Tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

#: 修饰键名 -> Scheduler 内部用的位 (与 native MODIFIER_* 无关, 只是本模块的标记)
_MOD_BITS: Dict[str, int] = {
    "shift": 0x01,
    "ctrl": 0x02,
    "control": 0x02,
    "alt": 0x04,
    "menu": 0x04,
    "meta": 0x08,
    "win": 0x08,
    "super": 0x08,
    "cmd": 0x08,
}

#: 只看修饰键本身时用的反查表
_MOD_KEY_NAMES: Dict[str, str] = {
    "shift": "shift",
    "ctrl": "ctrl",
    "control": "ctrl",
    "alt": "alt",
    "menu": "alt",
    "meta": "meta",
    "win": "meta",
    "super": "meta",
    "cmd": "meta",
}


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
#  快捷键
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class HotkeyBinding:
    """一条快捷键绑定。

    @param keys   组合键, 已归一: ("ctrl","alt","s") —— 修饰键在前, 主键在后
    @param action 触发动作 dict
    @param label  配置里原本怎么写的 (回显用)
    """

    keys: Tuple[str, ...]
    action: Dict[str, Any]
    label: str

    @property
    def main_key(self) -> str:
        return self.keys[-1]

    @property
    def modifiers(self) -> frozenset:
        return frozenset(self.keys[:-1])

    def matches(self, main_key: str, modifiers: Set[str]) -> bool:
        return self.main_key == main_key and self.modifiers == frozenset(modifiers)


@dataclass
class HotkeyEvent:
    """从 bus 文本里解析出来的一条按键事件。

    @param key      主键名 (已归一成小写)
    @param modifiers 当前按住的修饰键集合
    @param action   "press" | "release"
    """

    key: str
    modifiers: Set[str]
    action: str


def parse_hotkey_text(text: Any) -> Optional[HotkeyEvent]:
    """把 bus 里的一行文本解析成按键事件; 解析不了返回 None。

    支持两种形态:
      1. host_input_reader 的渲染格式:
             "[key ctrl]"             按下 Ctrl
             "[key c modifier=6]"     按住 Ctrl+Alt 时按下 c
             "[key c release]"        抬起 c
             "[key space]"
      2. 结构化 JSON (如果将来 bus 里投的是结构化事件):
             '{"type":"key","key":"c","modifier":6,"action":"press"}'

    ⚠ 注意: 真实实现里 host_input_reader 只投递**按下**事件 (native 的 read_all
    只回事件列表, 而 event_to_text 不区分按下/抬起时都渲染成 "[key X]")。
    这里仍然支持 "release" 是为了 (a) 结构化 JSON 形态下能正确跟踪修饰键抬起,
    (b) 将来 native 侧开始投递 release 时不用改解析器。

    解析不了就返回 None —— 调用方 (listen_hotkey) 跳过它。这让"从不认识的
    文本里认出快捷键"变成"宁可漏识别, 不可误触发"。
    """
    if not isinstance(text, str):
        return None

    raw = text.strip()
    if not raw:
        return None

    # --- 形态 2: JSON ---
    if raw.startswith("{"):
        try:
            payload = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(payload, dict) or payload.get("type") != "key":
            return None
        key = _normalize_key(payload.get("key"))
        if key is None:
            return None
        modifiers = _modifiers_from_mask(payload.get("modifier", 0))
        action = str(payload.get("action", "press")).lower()
        return HotkeyEvent(key=key, modifiers=modifiers, action=action)

    # --- 形态 1: "[key X]" / "[key X modifier=N]" / "[key X release]" ---
    if not (raw.startswith("[key ") and raw.endswith("]")):
        return None

    body = raw[len("[key "):-1].strip()
    if not body:
        return None

    action = "press"          # 默认按下: host_input_reader 只渲染按下
    modifier_mask = 0

    if " modifier=" in body:
        body, _, mask_text = body.partition(" modifier=")
        try:
            modifier_mask = int(mask_text.strip())
        except ValueError:
            modifier_mask = 0

    # "ctrl release" / "c released" / "c up"
    tokens = body.split()
    if len(tokens) >= 2 and tokens[-1].lower() in ("release", "released", "up"):
        action = "release"
        tokens = tokens[:-1]
    body = " ".join(tokens).strip()
    if not body:
        return None

    key = _normalize_key(body)
    if key is None:
        return None

    modifiers = _modifiers_from_mask(modifier_mask)
    return HotkeyEvent(key=key, modifiers=modifiers, action=action)


def _normalize_key(value: Any) -> Optional[str]:
    """把键归一成小写名字。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        # VK 码 -> 可打印 ASCII 用字符, 其余给 "vk:N"
        if 0x20 <= value < 0x7F:
            return chr(value).lower()
        return "vk:%d" % value
    if isinstance(value, str):
        text = value.strip().lower()
        if not text:
            return None
        # 单字符直接用; 具名键原样 (host_input_reader 已把 VK 渲染成名字)
        return text
    return None


def _modifiers_from_mask(mask: Any) -> Set[str]:
    """位掩码 -> {'ctrl','alt',...}。"""
    if isinstance(mask, bool) or not isinstance(mask, int):
        try:
            mask = int(mask)
        except (TypeError, ValueError):
            return set()
    out = set()
    for name, bit in (("shift", 0x01), ("ctrl", 0x02), ("alt", 0x04), ("meta", 0x08)):
        if mask & bit:
            out.add(name)
    return out


def parse_hotkey_config(config: Any) -> List[HotkeyBinding]:
    """从 config 里读快捷键映射。

    接受两种写法::

        hotkeys:
          - keys: ["ctrl", "alt", "s"]        # 列表
            action: {state: study, prompt: "开始学习"}

        hotkeys:
          "ctrl+alt+s":                        # 也接受 "组合键字符串" 作 key
            state: study
            prompt: "开始学习"

    @raise SchedulerError 格式不对
    """
    if config is None:
        return []
    if not isinstance(config, (list, tuple, dict)):
        raise SchedulerError("hotkeys must be a list or an object, got %r" % type(config).__name__)

    bindings: List[HotkeyBinding] = []

    if isinstance(config, dict):
        items = [(label, action) for label, action in config.items()]
        for label, action in items:
            keys = tuple(_split_combo(label))
            bindings.append(HotkeyBinding(keys=keys, action=_as_action(action, label), label=str(label)))
        return bindings

    for index, entry in enumerate(config):
        if not isinstance(entry, dict):
            raise SchedulerError("hotkey entry #%d must be an object" % index)
        raw_keys = entry.get("keys")
        if raw_keys is None:
            raise SchedulerError("hotkey entry #%d needs 'keys'" % index)
        if isinstance(raw_keys, str):
            keys = tuple(_split_combo(raw_keys))
            label = raw_keys
        elif isinstance(raw_keys, (list, tuple)):
            keys = tuple(_normalize_key(k) for k in raw_keys)
            label = "+".join(str(k) for k in raw_keys)
            if any(k is None for k in keys):
                raise SchedulerError("hotkey entry #%d has an unusable key" % index)
        else:
            raise SchedulerError(
                "hotkey entry #%d: keys must be a list or 'a+b' string" % index
            )
        if not keys:
            raise SchedulerError("hotkey entry #%d has empty keys" % index)

        bindings.append(
            HotkeyBinding(
                keys=tuple(keys),
                action=_as_action(entry.get("action", entry), label),
                label=label,
            )
        )

    # 同一个组合键绑了两次: 报错而不是偷偷用最后一个
    seen: Set[Tuple[str, ...]] = set()
    for binding in bindings:
        if binding.keys in seen:
            raise SchedulerError("duplicate hotkey binding: %s" % binding.label)
        seen.add(binding.keys)

    return bindings


def _split_combo(text: str) -> List[str]:
    """'ctrl+alt+s' -> ['ctrl','alt','s'], 修饰键排到前面。"""
    if not isinstance(text, str) or not text.strip():
        raise SchedulerError("hotkey combo must be a non-empty string, got %r" % (text,))
    parts = [p.strip().lower() for p in text.replace(" ", "").split("+") if p.strip()]
    if not parts:
        raise SchedulerError("hotkey combo %r has no keys" % (text,))

    mods = [p for p in parts if p in _MOD_KEY_NAMES]
    rest = [p for p in parts if p not in _MOD_KEY_NAMES]
    return [_MOD_KEY_NAMES[m] for m in mods] + rest


def _as_action(value: Any, label: str) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SchedulerError("hotkey %r action must be an object, got %r" % (label, value))
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
    """日程检查 + 定时触发 + 快捷键监听。

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
    ) -> None:
        """
        @param state      状态机 (触发动作里可以切状态)
        @param bus        ChatInputBus (触发动作里可以发消息; 快捷键从它读)
        @param config     配置 dict (可以是整个 config, 也可以只给 scheduler 段)
        @param clock      取当前时间 (测试注入)
        @param monotonic  取单调时钟 (测试注入, 用来做检查间隔)
        @param sleep      异步 sleep (测试注入)
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
        self._bindings: List[HotkeyBinding] = parse_hotkey_config(section.get("hotkeys"))

        # ---- 运行态 ----
        self._tasks: List[asyncio.Task] = []
        self._fired: Set[Tuple[date, str]] = set()
        self._held_modifiers: Set[str] = set()
        self._held_keys: Set[str] = set()
        self._last_check_at: Optional[float] = None
        self._checks = 0
        self._triggers = 0
        self._hotkey_hits = 0
        self._warnings: List[str] = []
        self._running = False
        #: listen_hotkey 建立的订阅句柄 (stop 时注销)
        self._unsubscribe: Optional[Callable[[], bool]] = None
        #: 最近一次命中的快捷键 (诊断用)
        self._last_hotkey: Optional[Dict[str, Any]] = None

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
    def bindings(self) -> List[HotkeyBinding]:
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
            asyncio.create_task(self.listen_hotkey(), name="scheduler-hotkey"),
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

    # ------------------------------------------------------------ 快捷键 ---
    async def listen_hotkey(self) -> None:
        """订阅 bus, 从主机键盘事件里识别快捷键, 命中就触发。

        ⚠ **不消费 bus 里的事件**: 用 bus.subscribe() 旁观, 而不是 get()。
        早期版本用 get() 循环读, 会把终端/GUI 的用户消息一起吃进调度器,
        下游再也看不到 (还会吃掉调度器自己 push 的触发消息)。订阅式只观察,
        事件仍然留在队列里等真正的消费者。

        @note 只认 source == "host_keyboard" 的事件 (需求: Host Input RB 的 source)。
              其它来源 (终端/GUI) 的文本即便长得像按键也不参与 —— 否则用户在
              终端里打一句 "[key c]" 就能触发状态切换。

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
                "bus does not support subscribe(); hotkey listening is disabled "
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
                self._warnings.append("hotkey handling failed: %r" % (exc,))

        return subscribe(_on_event)

    def _dispatch_event_sync(self, event: Dict[str, Any]) -> None:
        """同步版本的 _on_bus_event —— 命中时把触发动作挂成后台任务。

        为什么是"挂任务"而不是直接 await: 回调在 push() 的调用栈上执行,
        不能在那里 await (会让 push 的调用方等我们做完状态转换+发消息)。
        """
        detail = self._check_event(event)
        if detail is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._warnings.append("no running loop; hotkey action skipped")
            return
        task = loop.create_task(self._run_hotkey_action(detail))
        task.add_done_callback(_log_task_exception)

    def _check_event(self, event: Any) -> Optional[Tuple[HotkeyBinding, HotkeyEvent, Set[str]]]:
        """判断一条 bus 事件是否命中快捷键 (纯同步, 不做任何 IO)。"""
        if not isinstance(event, dict):
            return None
        if event.get("source") != "host_keyboard":
            return None

        parsed = parse_hotkey_text(event.get("text"))
        if parsed is None:
            return None

        # --- 维护"当前按住什么" ---
        # 修饰键自己按下/抬起时更新按住集合, 并且**不**参与主键匹配
        if parsed.key in _MOD_KEY_NAMES:
            canonical = _MOD_KEY_NAMES[parsed.key]
            if parsed.action == "release":
                self._held_modifiers.discard(canonical)
            else:
                self._held_modifiers.add(canonical)
            return None

        if parsed.action == "release":
            self._held_keys.discard(parsed.key)
            return None

        # 主键按住不放时会反复产生 press (主机端的按键重复)。只认第一次,
        # 否则"按住 ctrl+alt+s"会触发几十次状态切换。
        if parsed.key in self._held_keys:
            return None
        self._held_keys.add(parsed.key)

        # 事件里报的修饰键位掩码, 与"我们自己跟踪到的按住集合"取并集:
        # 前者是主机当时的状态, 后者能兜住"修饰键的 press 事件没被投递过来"
        # (例如调度器晚于按键才开始监听)。
        active = set(parsed.modifiers) | self._held_modifiers

        for binding in self._bindings:
            if binding.matches(parsed.key, active):
                self._hotkey_hits += 1
                return (binding, parsed, active)
        return None

    async def _run_hotkey_action(
        self, hit: Tuple[HotkeyBinding, HotkeyEvent, Set[str]]
    ) -> None:
        """执行命中快捷键后的动作 (状态转换 + 发消息)。"""
        binding, parsed, active = hit
        reason = binding.action.get("reason") or ("快捷键: %s" % binding.label)
        await self._apply_action(binding.action, reason)

        text = binding.action.get("prompt")
        if not text:
            text = "快捷键 %s" % binding.label
        try:
            await self._bus.push("scheduler", text)
        except Exception as exc:  # noqa: BLE001
            self._warnings.append("hotkey push failed: %r" % (exc,))
        self._triggers += 1
        self._last_hotkey = {
            "label": binding.label,
            "key": parsed.key,
            "modifiers": sorted(active),
        }

    async def _on_bus_event(self, event: Any) -> Optional[Dict[str, Any]]:
        """处理一条 bus 事件并等动作做完; 命中时返回触发详情。

        保留这个方法是为了让测试与"手工喂一条事件"的用法简单直接 (订阅式的
        后台任务不好断言)。生产路径走 _dispatch_event_sync。
        """
        hit = self._check_event(event)
        if hit is None:
            return None
        binding, parsed, active = hit
        reason = binding.action.get("reason") or ("快捷键: %s" % binding.label)
        detail: Dict[str, Any] = {
            "kind": "hotkey",
            "label": binding.label,
            "key": parsed.key,
            "modifiers": sorted(active),
            "bus_timestamp": event.get("timestamp"),
            "actions": [],
        }
        detail["actions"] += await self._apply_action(binding.action, reason)

        text = binding.action.get("prompt")
        if not text:
            text = "快捷键 %s" % binding.label
        pushed = await self._bus.push("scheduler", text)
        detail["actions"].append(
            {"type": "message", "text": text, "timestamp": pushed["timestamp"]}
        )
        self._triggers += 1
        return detail

    # ------------------------------------------------------------ 诊断 ---
    @property
    def stats(self) -> Dict[str, Any]:
        return {
            "running": self._running,
            "checks": self._checks,
            "triggers": self._triggers,
            "hotkey_hits": self._hotkey_hits,
            "events": len(self._events),
            "bindings": len(self._bindings),
            "fired_keys": len(self._fired),
            "held_modifiers": sorted(self._held_modifiers),
            "last_hotkey": dict(self._last_hotkey) if self._last_hotkey else None,
            "subscribed": self._unsubscribe is not None,
            "warnings": list(self._warnings),
        }

    def __repr__(self) -> str:
        return "<Scheduler events=%d bindings=%d interval=%gmin running=%s>" % (
            len(self._events), len(self._bindings), self.interval_min, self._running
        )
