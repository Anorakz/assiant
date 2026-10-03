# ============================================================================
#  agent/tools/schedule.py — "日程设置" 工具（Phase 7 T12-6）
#
#  它做什么: 一个工具管日程的增删查（`action` 决定干哪件）:
#
#      action="add"     加一条日程（**给 state + start**; 每周用 days, 仅一次用 date）
#      action="list"    只读: 现有日程 + 今天的日期（写 date 之前先用它拿日期）
#      action="remove"  删一条（给 state + start; 同时刻有多条时再给 days / date）
#
#  日程的内容就是**时间 + 状态**（T12-4 你定的）: 到点把设备切到那个状态,
#  切换严格走状态机（跨模式自动经 IDLE 释放/接管资源, 见 docs/architecture.md）。
#  ⚠ 它**不会**"到点叫醒模型说句话" —— 到点只切状态 + 推一行展示文本给界面。
#
#  为什么只在 IDLE / STUDY 可见
#  ---------------------------------------------------------------------------
#  · 与壁纸/音乐同一档: 睡眠时不该让模型动系统里的任何东西, GAME 的主区是视频;
#  · "什么时候切到什么模式"是**日常安排**, 说话的场景就是空闲与学习。
#  ⚠ 排出来的日程**可以**切到 sleep / game: 那只是被执行的动作, 与"现在能不能调工具"无关。
#
#  ⚠ 让 LLM 知道的三件事（都写进 description, 不然 0.6B 会自己脑补）
#  ---------------------------------------------------------------------------
#    1. 系统提示里**没有当前时间**: 要写 `date`（一次性）就先 `action="list"` 拿今天的日期,
#       **别自己猜**; 每周重复的日程（days）不需要日期。
#    2. 星期用 mon..sun（也认"周一"）; 不给 days = 每天。
#    3. 删的时候要给 state + start —— "9 点那条"这种说法不够, 得知道切到哪个状态。
#
#  为什么加的是**工具**而不是某个工具的一个 action（你 T12-6 的原话）
#  ---------------------------------------------------------------------------
#  "加入一个日程设置到工具列表" —— 日程与壁纸/音乐/视频是四件不同的事, 塞进任何一个
#  现有工具的 action 里都会让那个工具的语义变成两件事。代价如实记在这里: 工具清单
#  变长了（`tests/test_merged_tools.py::TestPromptBudget` 量着, 见那个文件里的预算注记）。
# ============================================================================

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from ..core.state_machine import State
from ..core.tool_router import Tool
from ._common import EMPTY_VALUES, action_of, clean_text

__all__ = ["NAME", "ALLOWED_STATES", "ACTIONS", "STATES", "DESCRIPTION", "SCHEMA",
           "WEEKDAYS", "normalize", "build"]

_log = logging.getLogger(__name__)

NAME = "set_schedule"

#: 与壁纸/音乐同一档（见模块头"为什么只在 IDLE/STUDY 可见"）。
ALLOWED_STATES = (State.IDLE, State.STUDY)

#: `action` 的取值
ACTIONS = ("add", "list", "remove")

#: 日程能切到的状态（= 状态机那四个; 小写, 与配置里一致）
STATES = ("sleep", "idle", "study", "game")

#: 星期缩写（与 config/config.example.yaml、`agent/core/scheduler.py::WEEKDAYS` 同一套）
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

DESCRIPTION = (
    "日程：到点自动切模式（sleep 睡眠 / idle 空闲 / study 学习 / game 游戏）。"
    "add 加一条：必给 state 与 start=HH:MM；每周用 days=mon..sun 或 周一（不给=每天），"
    "只一次用 date=YYYY-MM-DD。list 看现有日程与今天的日期。"
    "remove 删一条：给 state+start，同时刻有多条时再给 days/date。"
    "⚠ 系统没告诉你今天几号：要写 date 就先 list 拿日期，别猜。"
)

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(ACTIONS),
            "description": "add 加 / list 看 / remove 删",
        },
        "state": {
            "type": "string",
            "enum": list(STATES),
            "description": "到点切到哪个状态（add/remove 必填）",
        },
        "start": {
            "type": "string",
            "minLength": 4,
            "maxLength": 5,
            "description": "时刻 HH:MM，例如 23:00（add/remove 必填）",
        },
        "days": {
            "type": "array",
            "items": {"type": "string"},
            "description": "每周哪几天（mon..sun / 周一）；不给 = 每天",
        },
        "date": {
            "type": "string",
            "minLength": 10,
            "maxLength": 10,
            "description": "只在这一天 YYYY-MM-DD（先 list 拿今天日期）",
        },
    },
    "required": ["action"],
    "additionalProperties": False,
}

#: 老名字/同义写法 -> 现在的 action（模型偶尔会用别的动词）
_ACTION_SYNONYMS = {
    "create": "add", "new": "add", "insert": "add", "set": "add",
    "set_schedule": "add", "schedule": "add", "add_schedule": "add",
    "show": "list", "query": "list", "get": "list", "ls": "list", "read": "list",
    "delete": "remove", "del": "remove", "cancel": "remove", "drop": "remove",
    "remove_schedule": "remove",
}

#: 中文/别名 -> 状态（用户嘴上说的那几种说法, 模型常常照抄）
_STATE_ALIASES = {
    "sleep": "sleep", "睡眠": "sleep", "睡觉": "sleep", "休息": "sleep",
    "idle": "idle", "空闲": "idle", "待机": "idle", "闲置": "idle",
    "study": "study", "学习": "study",
    "game": "game", "游戏": "game",
}

#: 星期别名（与配置里那套 WEEKDAYS 对齐）
_DAY_ALIASES = {
    "mon": 0, "monday": 0, "周一": 0, "星期一": 0, "礼拜一": 0,
    "tue": 1, "tues": 1, "tuesday": 1, "周二": 1, "星期二": 1, "礼拜二": 1,
    "wed": 2, "wednesday": 2, "周三": 2, "星期三": 2, "礼拜三": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "周四": 3, "星期四": 3, "礼拜四": 3,
    "fri": 4, "friday": 4, "周五": 4, "星期五": 4, "礼拜五": 4,
    "sat": 5, "saturday": 5, "周六": 5, "星期六": 5, "礼拜六": 5,
    "sun": 6, "sunday": 6, "周日": 6, "周天": 6, "星期日": 6, "星期天": 6,
    "礼拜日": 6, "礼拜天": 6,
}

#: 整组的说法（"每天"/"工作日"）
_DAY_GROUPS = {
    "每天": [0, 1, 2, 3, 4, 5, 6], "每日": [0, 1, 2, 3, 4, 5, 6],
    "天天": [0, 1, 2, 3, 4, 5, 6], "all": [0, 1, 2, 3, 4, 5, 6],
    "everyday": [0, 1, 2, 3, 4, 5, 6], "daily": [0, 1, 2, 3, 4, 5, 6],
    "工作日": [0, 1, 2, 3, 4], "weekday": [0, 1, 2, 3, 4], "workday": [0, 1, 2, 3, 4],
    "周末": [5, 6], "weekend": [5, 6],
}

#: 空值写法 —— 公共那套（壁纸 / B 站 / 音乐同款, 见 `_common.EMPTY_VALUES`）
#: **外加**「不填」这一个（模型偶尔这么写；另外三个工具不认它, 别跟着抄）
_EMPTY_VALUES = EMPTY_VALUES + ("不填",)

#: `start` 的同义键（模型偶尔换个名字）
_START_ALIASES = ("time", "at", "clock", "when", "begin", "start_time")
#: `days` / `date` 的同义键
_DAYS_ALIASES = ("weekdays", "weekday", "repeat", "day")
_DATE_ALIASES = ("day_date", "on", "oneoff_date")


def _clean_text(value: Any) -> Optional[str]:
    """字符串字段清洗: 去空白; 空值字面量 -> None（= 没给）。

    @note 就是公共那件 `_common.clean_text`, 只是这边多认一个「不填」（见 `_EMPTY_VALUES`）。
    """
    return clean_text(value, _EMPTY_VALUES)


def _normalize_clock(value: Any) -> Optional[str]:
    """把各种机械写法归一成 `HH:MM`（**只做等价改写, 不解析自然语言**）。

    认: `9:30` / `09:30` / `0930` / 全角冒号 / `9点30` / `9点` / `9点半` / `23时5分`。
    认不出就原样返回（让 Runtime 那边如实报"时间要写成 HH:MM"）。
    """
    text = _clean_text(value)
    if text is None:
        return None
    original = text
    text = text.replace("：", ":").replace("点", ":").replace("时", ":")
    text = text.replace("分", "").replace("半", "30").strip()
    if ":" in text:
        head, _, tail = text.partition(":")
        head, tail = head.strip(), (tail.strip() or "00")     # "9点" -> "9:"
        if head.isdigit() and tail.isdigit():
            return "%02d:%02d" % (int(head), int(tail))
        return original                                 # 认不出: 原样留着, 让校验如实报错
    if text.isdigit() and len(text) in (3, 4):        # 930 / 0930
        return "%02d:%02d" % (int(text[:-2]), int(text[-2:]))
    return original


def _normalize_days(value: Any) -> Optional[List[str]]:
    """星期 -> `["mon", "wed"]`（配置里那套写法）。

    认: 列表 / `"mon,wed"` / `"周一 周三"` / `"每天"` / `"工作日"` / 数字 1..7（1=周一）。
    @return None = 没给（= 每天）; [] = 明确"每天"
    """
    if value is None:
        return None
    if isinstance(value, str) and _clean_text(value) is None:
        return None                                     # "none" / "无" / "" = 没给
    items: List[Any] = value if isinstance(value, (list, tuple)) else [value]
    out: List[str] = []
    for item in items:
        if item is None:
            continue
        if not isinstance(item, str):
            item = str(item)
        for token in item.replace("、", ",").replace("，", ",").replace("/", ",").split(","):
            for piece in token.replace("和", " ").replace("及", " ").split():
                text = piece.strip().lower()
                if not text:
                    continue
                if text in _DAY_GROUPS:
                    out.extend(WEEKDAYS[index] for index in _DAY_GROUPS[text])
                    continue
                if text in _DAY_ALIASES:
                    out.append(WEEKDAYS[_DAY_ALIASES[text]])
                    continue
                if text.isdigit() and 1 <= int(text) <= 7:
                    out.append(WEEKDAYS[int(text) - 1])     # 1=周一（与"周1"一致）
                    continue
                out.append(text)                            # 认不出: 原样留着, 让校验如实报错
    seen: List[str] = []
    for name in out:
        if name not in seen:
            seen.append(name)
    return seen


def _normalize_date(value: Any) -> Optional[str]:
    """日期 -> `YYYY-MM-DD`（只换分隔符 + 补零; 认不出就原样留着让校验报错）。"""
    text = _clean_text(value)
    if text is None:
        return None
    text = (text.replace("/", "-").replace(".", "-")
                .replace("年", "-").replace("月", "-").replace("日", "").strip("-"))
    match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", text)
    if match:
        return "%04d-%02d-%02d" % tuple(int(part) for part in match.groups())
    return text


def normalize(args: Dict[str, Any]) -> Dict[str, Any]:
    """参数归一化（T8-5c 那套）—— **只做等价改写, 不猜内容**。

    规则（每条都写明来源）:

    ============================================  ==========================================
    模型可能这样写                                 归一化成
    ============================================  ==========================================
    `action=" ADD "` / 大写                       小写 + 去空白
    `action="create"` / `"set"` / `"schedule"`      `add`（同义动词; 板端实测模型爱换词）
    `action="delete"` / `"cancel"`                  `remove`
    `action="show"` / `"query"`                     `list`
    没给 action, 但给了 `start`/`days`/`date`       `add`（语义就是"加一条"）
    没给 action, 只给了 `state`                     `list`（"看看日程"最常见）
    `state="学习"` / `"STUDY"` / `"Study模式"`       `study`（中文说法与大小写）
    `start="9:30"` / `"930"` / `"9点30"` / `"9点"`   `09:30` / `09:00`（机械改写）
    `start` 写进了 `time` / `at` / `when` 等键      搬到 `start`
    `days="mon,wed"` / `"周一 周三"` / `"工作日"`     `["mon","wed"]` / `["mon".."fri"]`
    `days=[1,3]`（数字）                             `["mon","wed"]`（1=周一）
    `date="2026/09/22"` / `"2026.09.22"`             `"2026-09-22"`（只换分隔符）
    任何字段写成 `"none"` / `"无"` / `""`            删掉（当没给）
    ============================================  ==========================================

    @return 新的参数字典（不改入参）
    """
    out = dict(args or {})

    action = action_of(out, _ACTION_SYNONYMS)
    if not action:
        if any(key in out for key in ("start", "days", "date") + _START_ALIASES):
            action = "add"
        elif "state" in out:
            action = "list"
    if action:
        out["action"] = action

    # 同义键 -> 正式键（只在正式键没给的时候搬）
    for alias in _START_ALIASES:
        if alias in out and not _clean_text(out.get("start")):
            out["start"] = out.pop(alias)
        else:
            out.pop(alias, None)
    for alias in _DAYS_ALIASES:
        if alias in out and out.get("days") is None:
            out["days"] = out.pop(alias)
        else:
            out.pop(alias, None)
    for alias in _DATE_ALIASES:
        if alias in out and not _clean_text(out.get("date")):
            out["date"] = out.pop(alias)
        else:
            out.pop(alias, None)

    state = _clean_text(out.get("state"))
    if state is not None:
        text = state.strip().lower()
        for suffix in ("模式", "mode"):
            if text.endswith(suffix) and len(text) > len(suffix):
                text = text[: -len(suffix)]
        out["state"] = _STATE_ALIASES.get(text, text)
    else:
        out.pop("state", None)

    start = _normalize_clock(out.get("start"))
    if start is None:
        out.pop("start", None)
    else:
        out["start"] = start

    if "days" in out:
        days = _normalize_days(out.get("days"))
        if days is None:
            out.pop("days", None)
        else:
            out["days"] = days

    date = _normalize_date(out.get("date"))
    if date is None:
        out.pop("date", None)
    else:
        out["date"] = date

    return out


def build(services: Dict[str, Any]) -> Optional[Tool]:
    """造工具。三个运行时入口缺一个就跳过（并说清缺哪个）。

    @note 依赖的是**运行时入口**（`agent/main.py::Runtime.schedule_*`）——
          它们自己会处理"配置文件写不了 / 调度器没起来"这些运行期问题, 并如实回话。
    """
    needed = ("schedule_add", "schedule_list", "schedule_remove")
    services = services or {}
    missing = [name for name in needed if not callable(services.get(name))]
    if missing:
        _log.warning("tools: set_schedule 需要 services 里的 %s，装配里缺 %s —— 跳过",
                     "/".join(needed), "、".join(missing))
        return None

    def _need(what: str) -> Dict[str, Any]:
        return {"ok": False, "tell_user": "这条日程指令少了 %s" % what,
                "error": "缺少 %s" % what}

    def handler(action: str = "", state: Optional[str] = None,
                start: Optional[str] = None, days: Optional[List[str]] = None,
                date: Optional[str] = None) -> Dict[str, Any]:
        # ⚠ 归一化已经在路由层跑过（`Tool.normalize`, 规则表见 `normalize()`）——
        #   这里只看规范形: 同义动词 / 中文状态 / 各种时间写法都已经被改写。
        choice = str(action or "").strip().lower()

        if choice == "list":
            return services["schedule_list"]()

        if choice not in ("add", "remove"):
            return {"ok": False,
                    "tell_user": "我看不懂这个日程动作（可用的是 %s）" % "/".join(ACTIONS),
                    "error": "不认识的 action %r（可用的: %s）" % (action, "、".join(ACTIONS))}

        if not state:
            return _need("state（到点切到哪个状态: sleep/idle/study/game）")
        if not start:
            return _need("start（几点，HH:MM）")
        if date and days:
            return {"ok": False,
                    "tell_user": "一条日程要么每周重复（days），要么只那一天（date），不能都给",
                    "error": "days 与 date 同时给了"}

        values = {"state": state, "start": start}
        if days is not None:
            values["days"] = list(days)
        if date:
            values["date"] = date

        entry = services["schedule_add"] if choice == "add" else services["schedule_remove"]
        try:
            return entry(values)
        except Exception as exc:                            # noqa: BLE001 - 如实回给模型
            _log.warning("set_schedule: %s 失败: %r", choice, exc)
            return {"ok": False, "tell_user": "日程没改成：%s" % exc, "error": str(exc)}

    return Tool(name=NAME, description=DESCRIPTION, schema=SCHEMA, handler=handler,
                allowed_states=set(ALLOWED_STATES), normalize=normalize)
