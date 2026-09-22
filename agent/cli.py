#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent/cli.py — 板端控制 CLI（同一个 Agent 的第二条前端）

为什么有它
    GUI 是板端那块屏的前端；CLI 是给 **ssh / 脚本** 用的那一半：看一眼状态、
    发一条消息、切模式、盯推送、查日程、做体检。板端还放了个 `assistant` 启动器。

走哪条路（这里决定了它能做什么、不能做什么）
    · 只走**IPC 协议**（`docs/ipc-protocol.md` 是线上格式的唯一真源），用现成的
      `agent.ipc.local_client.LocalClient`。
    · **不 import Agent 去读它的内存** —— 所以 Agent 没跑时这里会明确说"连不上"，
      而不是给一份假状态。
    · 日程：**列表**用真的 `agent.core.scheduler` 语义自己算（不需要 Agent 在跑）；
      **"到底触发过哪条"** 只能问运行中的 Agent（协议 `query_schedule` -> `schedule`
      推送，P 系列加的），问不到就只说「已过（按时间）」，并在页脚写明原因。
    · 触发记录是**只读**的：CLI 不写配置、不改日程（配置永远是唯一真源）。

退出码
    0 = 成功 ／ 1 = 环境或连接问题 ／ 2 = 参数错（argparse 的默认行为）
"""

import argparse
import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from agent.config import ConfigError, ConfigNotFoundError, config_path, load_config
from agent.core.scheduler import Scheduler, SchedulerError
from agent.core.state_machine import StateMachine
from agent.ipc.local_client import IpcClientError, LocalClient
from agent.ipc.protocol import (
    COMMAND_CHAT_INPUT,
    COMMAND_QUERY_SCHEDULE,
    COMMAND_SWITCH_MODE,
    MODES,
    SOCKET_PATH,
    TOPIC_LLM,
    TOPIC_SCHEDULE,
    TOPIC_STATUS,
)
from agent.ipc import SCHEDULE_KIND_FIRED, SCHEDULE_KIND_STATE

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

#: 等一条推送的默认超时（秒）。Agent 只在**状态变化**时推 status，
#: 所以 status 命令必须能"等不到也不报错"。
DEFAULT_TIMEOUT = 3.0

#: watch 打印一条推送时，单个值最长多少字符（超出截断并标总长）
WATCH_VALUE_LIMIT = 120

#: 仓库根：**按 CLI 自己的位置**推（`<root>/agent/cli.py`）。
#: ⚠ 不用"配置文件的上一级"推：`--config` 可以指向仓库外的临时配置（验收/沙箱常这么干），
#:   那样就会把一堆仓库里的路径误判成"缺"。
REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
#  配置 / socket 路径（纯逻辑，可单测）
# ---------------------------------------------------------------------------
def socket_path_from(cli_value: Optional[str], config: Optional[Dict[str, Any]]) -> str:
    """优先级：`--socket` > 配置里的 `ipc.socket_path` > 协议默认值。"""
    if cli_value:
        return cli_value
    section = (config or {}).get("ipc")
    if isinstance(section, dict):
        value = section.get("socket_path")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return SOCKET_PATH


def load_plane_config(explicit_path: Optional[str] = None) -> Tuple[Dict[str, Any], Optional[str]]:
    """读配置（这里只为了拿 socket 路径、日程等）。返回 `(config, 失败原因或 None)`。

    与 `agent/main.py --config` 同一条路：设置 `AGENT_CONFIG_DIR` 后按名字加载。
    ⚠ 所以 `--config` 给的是**路径**，但加载是按**名字**找 `config.yaml` /
      `config.example.yaml` —— 文件叫别的名字会读不到（提示里会写明）。
    读不到**不是**致命错误（socket 有默认值），调用方自己决定怎么处理。
    """
    if explicit_path:
        os.environ.setdefault("AGENT_CONFIG_DIR", str(Path(explicit_path).resolve().parent))
    try:
        return load_config("config"), None
    except (ConfigError, ConfigNotFoundError) as exc:
        hint = ""
        if explicit_path:
            hint = ("（--config 给的是路径，但加载按**名字**找 config.yaml / "
                    "config.example.yaml —— 与 agent/main.py 一致；"
                    "文件叫别的名字请先改成 config.yaml）")
        return {}, "%s%s" % (exc, hint)
    except Exception as exc:                      # noqa: BLE001 - 配置坏法很多，CLI 不该崩
        return {}, "%s: %s" % (type(exc).__name__, exc)


def connect_hint(path: str, exc: BaseException) -> str:
    """连不上时给人看的一句话（含路径、原因、下一步）。"""
    return (
        "连不上 Agent（%s）：%s\n"
        "  可能原因：Agent 没在跑（板端 `python3 agent/main.py`）、"
        "socket 路径不是这个（用 --socket 指定）、或者它刚重启还没建好 socket。"
        % (path, exc)
    )


def _resolve_path(args: argparse.Namespace) -> str:
    """按优先级定 socket 路径，并在配置读不到时给一句提示。"""
    config, why = load_plane_config(args.config)
    if why:
        print("提示：读不到配置（%s），改用默认 socket 路径" % why, file=sys.stderr)
    return socket_path_from(args.socket, config)


def _status_line(data: Dict[str, Any]) -> str:
    mode = str(data.get("mode") or "?")
    stream = "串流已连接" if data.get("connected") else "串流未连接"
    return "模式 %s · %s" % (mode, stream)


def normalize_mode(value: Any) -> Optional[str]:
    """把用户写的模式规范化成协议里的全大写取值；不认识返回 None。

    协议（`docs/ipc-protocol.md` §3）里 `status.mode` 与 `switch_mode.value`
    都是**全大写** `SLEEP/IDLE/STUDY/GAME` —— 所以 CLI 收小写，发出去前转大写。
    """
    if not isinstance(value, str):
        return None
    text = value.strip().upper()
    return text if text in MODES else None


def parse_topics(value: Optional[str]) -> Optional[Set[str]]:
    """`--topics status,llm` → {'status','llm'}；没给返回 None（= 全看）。"""
    if value is None:
        return None
    items = [item.strip() for item in value.split(",")]
    topics = {item for item in items if item}
    return topics or None


def format_value(value: Any, limit: int = WATCH_VALUE_LIMIT) -> str:
    """把推送里的一个值压成"单行、可读、超长截断"。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "-"
    if isinstance(value, str):
        # ⚠ 只用 ASCII 的转义：像 "⏎" 这种符号在 GBK 终端上会让 print 直接抛
        #    UnicodeEncodeError（PC 的 cmd 就是 GBK）—— 一个字符不该把 CLI 弄崩。
        text = value.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "")
        if len(text) > limit:
            return "%s…(共 %d 字)" % (text[:limit], len(value))
        return text
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def format_push(topic: str, data: Optional[Dict[str, Any]],
                clock: Optional[datetime] = None) -> str:
    """一条推送渲染成一行：`23:51:02  status  mode=STUDY connected=true`。

    ⚠ `schedule` 单独走：它负载里是**嵌套的事实对象**，原样倒出来会是一坨 dict，
      这不是"人读的一行"（见 schedule_push_pairs）。
    """
    stamp = (clock or datetime.now()).strftime("%H:%M:%S")
    if topic == TOPIC_SCHEDULE:
        pairs = schedule_push_pairs(data or {})
    else:
        pairs = " ".join("%s=%s" % (key, format_value(value))
                         for key, value in (data or {}).items())
    return "%s  %-9s %s" % (stamp, topic, pairs)


def schedule_push_pairs(data: Dict[str, Any]) -> str:
    """`schedule` 推送压成 key=value，只挑人真正要看的字段。

    · `kind="fired"` 刚触发了一条 -> title / date / scheduled_at / fired_at
    · `kind="state"` 应答查询的快照 -> 条数 + 上限（不把 N 条事实全倒出来）
    · 不认识的 kind（将来加的）-> 原样给出来：**不装懂**，也不假装没有
    """
    kind = data.get("kind")
    if kind == SCHEDULE_KIND_FIRED:
        event = data.get("event") or {}
        parts = ["kind=%s" % SCHEDULE_KIND_FIRED]
        for key in ("title", "date", "scheduled_at", "fired_at"):
            if event.get(key) is not None:
                parts.append("%s=%s" % (key, format_value(event[key])))
        return " ".join(parts)
    if kind == SCHEDULE_KIND_STATE:
        fired = data.get("fired") or []
        return "kind=%s fired=%d limit=%s" % (
            SCHEDULE_KIND_STATE, len(fired), format_value(data.get("limit")))
    return " ".join("%s=%s" % (key, format_value(value)) for key, value in data.items())


# ---------------------------------------------------------------------------
#  子命令
# ---------------------------------------------------------------------------
async def cmd_status(args: argparse.Namespace) -> int:
    """连一次，等一条 `status` 推送，打印模式与串流连接。

    `status` 的推送时机是"状态变化"，协议里**没有**"客户端连上就补一条"——
    所以等不到时如实说明，而不是编一个"IDLE"。
    """
    path = _resolve_path(args)
    client = LocalClient(path)
    seen: Dict[str, Any] = {}
    arrived = asyncio.Event()

    def on_message(topic: str, data: dict) -> None:
        if topic == TOPIC_STATUS:
            seen.clear()
            seen.update(data)
            arrived.set()

    client.on_message(on_message)
    try:
        await client.connect()
    except IpcClientError as exc:
        print(connect_hint(path, exc), file=sys.stderr)
        return EXIT_ERROR

    try:
        print("已连上 Agent：%s" % path)
        try:
            await asyncio.wait_for(arrived.wait(), timeout=args.timeout)
        except asyncio.TimeoutError:
            print("但它还没推 status：协议上 Agent 只在状态**变化**时推，"
                  "客户端连上时不会主动补一条。")
            print("想看后续变化：assistant watch（Ctrl-C 退出）。")
            return EXIT_OK
        print(_status_line(seen))
        return EXIT_OK
    finally:
        await client.close()


async def cmd_chat(args: argparse.Namespace) -> int:
    """发一条 `chat_input`，等 `llm` 回复并打印。

    多段文本自动用空格连接（`assistant chat 你好 世界` 也行）。
    等不到回复算**失败**（exit 1）—— 这和 status 不一样：status 的"没推送"是正常的，
    而"我发了话却没回音"是用户要立刻知道的事。
    """
    text = " ".join(args.text).strip()
    if not text:
        print("chat 需要一段非空文本，例如：assistant chat 你好", file=sys.stderr)
        return EXIT_USAGE

    path = _resolve_path(args)
    client = LocalClient(path)
    reply: Dict[str, Any] = {}
    arrived = asyncio.Event()

    def on_message(topic: str, data: dict) -> None:
        if topic == TOPIC_LLM and not arrived.is_set():
            reply.clear()
            reply.update(data)
            arrived.set()

    client.on_message(on_message)
    try:
        await client.connect()
    except IpcClientError as exc:
        print(connect_hint(path, exc), file=sys.stderr)
        return EXIT_ERROR

    try:
        await client.send_command(COMMAND_CHAT_INPUT, {"text": text})
        if args.no_wait:
            print("已发送：%s" % text)
            return EXIT_OK
        try:
            await asyncio.wait_for(arrived.wait(), timeout=args.timeout)
        except asyncio.TimeoutError:
            print("发出去 %.1f 秒了还没收到 llm 回复"
                  "（Agent 可能没接上 LLM、或在忙）——用 assistant watch 看它到底在推什么。"
                  % args.timeout, file=sys.stderr)
            return EXIT_ERROR
        print("助手: %s" % (reply.get("text") or ""))
        return EXIT_OK
    finally:
        await client.close()


async def cmd_mode(args: argparse.Namespace) -> int:
    """发 `switch_mode`，等一条 `status` 确认。

    非法转换会被 Agent **拒掉**，并把真实状态推回来（协议 §4）—— 所以这里
    "等到的模式 != 想切的模式" 就是被拒，如实报出来并返回 1。
    """
    want = normalize_mode(args.value)
    if want is None:
        print("模式只认 %s（大小写不限），收到：%r" % ("/".join(MODES), args.value),
              file=sys.stderr)
        return EXIT_USAGE

    path = _resolve_path(args)
    client = LocalClient(path)
    seen: Dict[str, Any] = {}
    arrived = asyncio.Event()

    def on_message(topic: str, data: dict) -> None:
        if topic == TOPIC_STATUS:
            seen.clear()
            seen.update(data)
            arrived.set()

    client.on_message(on_message)
    try:
        await client.connect()
    except IpcClientError as exc:
        print(connect_hint(path, exc), file=sys.stderr)
        return EXIT_ERROR

    try:
        await client.send_command(COMMAND_SWITCH_MODE, {"value": want})
        if args.no_wait:
            print("已发送：切到 %s" % want)
            return EXIT_OK
        try:
            await asyncio.wait_for(arrived.wait(), timeout=args.timeout)
        except asyncio.TimeoutError:
            print("已发出 switch_mode(%s)，但 %.1f 秒内没等到 status 确认。"
                  % (want, args.timeout), file=sys.stderr)
            return EXIT_ERROR
        current = str(seen.get("mode") or "?")
        if current == want:
            print("已切到 %s" % want)
            return EXIT_OK
        print("Agent 没切过去：当前仍是 %s —— 非法转换会被状态机拒掉（协议 §4）。"
              % current, file=sys.stderr)
        return EXIT_ERROR
    finally:
        await client.close()


async def cmd_watch(args: argparse.Namespace) -> int:
    """持续打印推送，直到 Ctrl-C（或 `--count` 收够）。

    这是排障主力：GUI 上看到什么、Agent 到底推了什么，这里能看到原始 topic 与字段。
    """
    topics = parse_topics(args.topics)
    path = _resolve_path(args)
    client = LocalClient(path)
    received = 0
    stop = asyncio.Event()

    def on_message(topic: str, data: dict) -> None:
        nonlocal received
        if topics is not None and topic not in topics:
            return
        print(format_push(topic, data), flush=True)
        received += 1
        if args.count and received >= args.count:
            stop.set()

    client.on_message(on_message)
    try:
        await client.connect()
    except IpcClientError as exc:
        print(connect_hint(path, exc), file=sys.stderr)
        return EXIT_ERROR

    scope = "全部 topic" if topics is None else "只看 %s" % ",".join(sorted(topics))
    print("在听 %s 的推送（%s，Ctrl-C 退出）" % (path, scope))

    loop = asyncio.get_running_loop()
    handler_installed = False
    try:
        # POSIX：把 Ctrl-C 变成"设个事件"，这样能打印统计再干净退出；
        # Windows / 非主线程没有 add_signal_handler，退回 main() 的 KeyboardInterrupt 路径。
        loop.add_signal_handler(signal.SIGINT, stop.set)
        handler_installed = True
    except (NotImplementedError, RuntimeError, AttributeError):
        pass

    started = time.monotonic()
    try:
        await stop.wait()
    finally:
        if handler_installed:
            with contextlib.suppress(Exception):
                loop.remove_signal_handler(signal.SIGINT)
        try:
            await client.close()
        except BaseException:              # noqa: BLE001 - 取消路径下 close 可能跑不完
            pass
    print("收到 %d 条推送，用时 %.1f 秒" % (received, time.monotonic() - started))
    return EXIT_OK


class _NullBus:
    """只为构造 `Scheduler` 用的空 bus。

    CLI 是只读的：它借用 Agent 的**日程语义**（同一个 `Scheduler`），但不发消息、
    不跑循环 —— 所以给个能 `subscribe` 的空壳就够了。
    """

    def subscribe(self, *args, **kwargs):
        return lambda: True


def load_events(config: Dict[str, Any]) -> List[Any]:
    """用**真的** `Scheduler` 装载日程 —— 与 Agent 启动时同一条路。

    @raise SchedulerError 配置里的日程坏到 Agent 也起不来时（这里如实抛给调用方）
    """
    scheduler = Scheduler(state=StateMachine(), bus=_NullBus(), config=config)
    return list(scheduler.events)


def row_text(row: Dict[str, Any]) -> str:
    """一行的文本：`HH:MM[-HH:MM]  标题`。

    ⚠ 与 GUI 日程区同一行格式（`SchedulePanel::rowTextOf`）—— 这样"CLI 的日程"
      与"界面上的日程"可以直接逐行 diff，不用人眼对着两张图读。
    """
    clock = row["time"] if not row["end"] else "%s-%s" % (row["time"], row["end"])
    return "%s  %s" % (clock, row["title"])


def schedule_rows(events: List[Any], day, now: datetime,
                  facts: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """某一天的日程行（按 `(时间, 标题)` 升序）。

    `occurs_on()` 是**真的** Python 语义（recurring 看星期、oneoff 看日期）；
    `past` 只在**今天**才有意义 —— "现在时刻已经过了 start"。对明天来说
    `now > start` 恒成立（例如 12:58 看明天的 08:30），那样标"已过"就是错的。
    ⚠ 与 GUI 同一条规则（`SchedulePanel`：`past` 只作用于今天段）——C4 起真 Agent
      时正是这里露了馅（明天 08:30 被标成"已过"）。

    @param facts 运行中 Agent 报回来的触发事实（`query_schedule` 的应答）。给了就在
                 行里带上 `fired_at`（这一条**真的**被触发过的时刻）；不给就是 None。
    @note 事实与行按 **`trigger_at`**（就是 `scheduled_at` 那个字符串）对齐 ——
          这是 Agent 与 CLI 用**同一个** `ScheduleEvent.trigger_at()` 算出来的，
          所以提前量跨天（00:05 提前 10 分钟 → 前一天 23:55）也能对上。
          按 `start` 对齐会漏掉那种跨天的情况。
    @note 配置改过（比如把 start 挪了）时对不上 -> 那条历史就落不到任何行上：
          这是如实的结果（"这个时刻没触发过"），不是 bug。
    """
    by_trigger: Dict[str, Dict[str, Any]] = {}
    for fact in (facts or []):
        key = fact.get("scheduled_at")
        if key and key not in by_trigger:
            by_trigger[key] = fact

    rows = []
    now_minute = now.hour * 60 + now.minute
    is_today = (day == now.date())
    for event in events:
        if not event.occurs_on(day):
            continue
        start = "%02d:%02d" % event.start
        end = ("%02d:%02d" % event.end) if event.end else ""
        trigger_at = event.trigger_at(day).isoformat(timespec="minutes")
        rows.append({
            "time": start,
            "end": end,
            "title": event.title,
            "past": bool(is_today and now_minute > (event.start[0] * 60 + event.start[1])),
            "day": day,
            "trigger_at": trigger_at,
            "fired_at": (by_trigger.get(trigger_at) or {}).get("fired_at"),
        })
    rows.sort(key=lambda row: (row["time"], row["title"]))
    return rows


def fired_clock(fired_at: Any) -> str:
    """事实里的 `fired_at`（`YYYY-MM-DDTHH:MM:SS`）→ `HH:MM:SS`。

    只取时刻：这一行已经在"哪一天"的段里了，再打一次日期是噪音。
    解析不出来就原样返回（宁可难看，不要假装懂）。
    """
    text = str(fired_at or "")
    _, sep, clock = text.partition("T")
    return clock if sep else text


def render_schedule(days: List[Tuple[str, Any]], rows_of, now: datetime,
                    limit: int = 10, asked: bool = False) -> List[str]:
    """渲染成"人读的行"（不含页脚）。`rows_of(day)` 给当天的行。

    @param asked 是否**问到了** Agent 的触发记录（`--no-ask` / 连不上就是 False）。
                 只有问到时才敢把"过了 start 却不在记录里"的行标成「已过（未触发）」；
                 没问到时保持老样子「已过」—— 那是纯时间比较，不掺一句暗示。
    """
    lines = []
    weekday = "一二三四五六日"
    for label, day in days:
        rows = rows_of(day)
        lines.append("%s（%s 周%s）" % (label, day.isoformat(), weekday[day.weekday()]))
        if not rows:
            lines.append("  （没有日程）")
            continue
        shown = rows if limit <= 0 else rows[:limit]
        for row in shown:
            if row.get("fired_at"):
                mark = "  ← 已触发 %s" % fired_clock(row["fired_at"])
            elif row["past"] and asked:
                mark = "  ← 已过（未触发）"
            elif row["past"]:
                mark = "  ← 已过"
            else:
                mark = ""
            lines.append("  %s%s" % (row_text(row), mark))
        if len(rows) > len(shown):
            lines.append("  …还有 %d 项（--limit 可调）" % (len(rows) - len(shown)))
    return lines


SCHEDULE_FOOTER = ("⚠ 「已过」只是「现在过了 start 时刻」（按时间比较），"
                   "**不代表 Agent 已经触发过**。")

#: 问到了触发记录时用的页脚（P 系列）：这时标出来的东西**不是**时间比较的结果。
#: ⚠ 文案要对"0 条"也读得通：新起的 Agent 一条都还没触发过是最常见的情形，
#:   那时「已过（未触发）」的意思是"这个 Agent 进程没触发过"，**不是**"配置里没有"。
SCHEDULE_FOOTER_LIVE = ("✓ 触发记录来自运行中的 Agent 本人（本次 %d 条）——「未触发」"
                        "是**这个 Agent 进程**没触发过，不是配置里没有；"
                        "记录只在内存里，Agent 重启即清零。")

#: 没问到时的原因文案（`--no-ask` 与"连不上/超时"要分清，别把用户的选择说成故障）
NO_ASK_REASON = "你用了 --no-ask"


async def ask_fired(path: str, timeout: float) -> Tuple[Optional[List[Dict[str, Any]]], str]:
    """问运行中的 Agent"最近触发过哪些日程"（协议 `query_schedule`）。

    @return (facts, why)。问到了 -> (列表, "")；问不到 -> (None, 原因)
    @note 这是**只读**查询：CLI 不写配置、不改日程 —— 配置永远是唯一真源，
          改日程是人在 PC 上做的事。
    @note 问不到**不是错误**：Agent 没在跑时日程照样列得出来，只是那些"已过"
          只能按时间比较（见 render_schedule 的 asked）。
    """
    client = LocalClient(path)
    facts: Optional[List[Dict[str, Any]]] = None
    arrived = asyncio.Event()

    def on_message(topic: str, data: dict) -> None:
        nonlocal facts
        if topic == TOPIC_SCHEDULE and data.get("kind") == SCHEDULE_KIND_STATE:
            facts = list(data.get("fired") or [])
            arrived.set()

    client.on_message(on_message)
    try:
        await client.connect()
    except IpcClientError as exc:
        return None, "连不上 Agent（%s）" % exc

    try:
        try:
            await client.send_command(COMMAND_QUERY_SCHEDULE, {})
        except IpcClientError as exc:
            return None, "发不出 query_schedule（%s）" % exc
        try:
            await asyncio.wait_for(arrived.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return None, "Agent 在 %.1f 秒内没回日程快照" % timeout
        return facts, ""
    finally:
        with contextlib.suppress(Exception):
            await client.close()


async def cmd_schedule(args: argparse.Namespace) -> int:
    """列出今天/明天的日程（真的 `Scheduler` 语义），并标出**真的触发过**的那些。

    两件事分开说清楚：
      · 日程本身来自配置 + `ScheduleEvent` 语义（CLI 自己算，不需要 Agent 在跑）
      · 「已触发」来自运行中的 Agent（协议的 `query_schedule` 应答）—— 问不到就
        只说「已过」，并在页脚讲明"那是按时间比较的"（不留幻觉）
    """
    config, why = load_plane_config(args.config)
    if why:
        print("读不到配置（%s）——日程在 config.yaml 的 scheduler 段。" % why, file=sys.stderr)
        return EXIT_ERROR
    try:
        events = load_events(config)
    except SchedulerError as exc:
        print("配置里的日程读不出来：%s\n（Agent 也会因此起不来 —— 先修配置。）" % exc,
              file=sys.stderr)
        return EXIT_ERROR

    facts: Optional[List[Dict[str, Any]]] = None
    reason = NO_ASK_REASON
    if not args.no_ask:
        facts, reason = await ask_fired(_resolve_path(args), args.timeout)
        if facts is None:
            print("问不到 Agent 的触发记录：%s" % reason, file=sys.stderr)

    now = datetime.now()
    today = now.date()
    tomorrow = today + timedelta(days=1)
    wanted = []
    if not args.tomorrow:
        wanted.append(("今天", today))
    if not args.today:
        wanted.append(("明天", tomorrow))

    print("日程（共 %d 条；%s）" % (len(events), (config_path("config"))))
    for line in render_schedule(wanted,
                               lambda day: schedule_rows(events, day, now, facts=facts),
                               now, limit=args.limit, asked=facts is not None):
        print(line)

    if facts is None:
        print(SCHEDULE_FOOTER)
        print("（%s —— 所以这里只能按时间比较。）" % reason)
    else:
        print(SCHEDULE_FOOTER_LIVE % len(facts))
    return EXIT_OK


# ---------------------------------------------------------------------------
#  doctor
# ---------------------------------------------------------------------------
OK = "OK"
WARN = "警告"


def derivation_verdict(exit_code: int, output: str) -> Tuple[str, str]:
    """纯逻辑：把 `gui_config_sync`（dry-run）的输出判成 OK / 警告。

    ⚠ 派生一致性**不在这边重实现映射表**：映射表只有 `gui/src/core/config_sync.cpp`
      一份，这里只是跑它、读它的结论。
    """
    text = (output or "").strip()
    flat = " | ".join(line.strip() for line in text.splitlines() if line.strip())
    if "[sync] 无差异" in text:
        return OK, "llm.env 与 config.yaml 一致"
    if exit_code != 0:
        return WARN, "gui_config_sync 退出码 %d：%s" % (exit_code, flat[:200])
    return WARN, "llm.env 与 config.yaml 有差异：%s" % flat[:200]


def run_derivation_check(binary: str, config_file: str, env_file: str) -> Tuple[str, str]:
    """跑一次 dry-run（不写文件）。"""
    if not os.path.exists(binary):
        return WARN, "没找到 %s（板端还没构建 gui/？这一步跳过）" % binary
    if not os.path.exists(env_file):
        return WARN, "没找到 %s（还没派生过？）" % env_file
    try:
        proc = subprocess.run([binary, "--config", config_file, "--env", env_file],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              universal_newlines=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        return WARN, "跑不起来：%s" % exc
    return derivation_verdict(proc.returncode, proc.stdout or "")


async def cmd_doctor(args: argparse.Namespace) -> int:
    """体检：配置 / socket / 派生一致性 / 日程 / 关键路径，逐项 OK 或警告。"""
    checks: List[Tuple[str, str, str]] = []

    config, why = load_plane_config(args.config)
    config_file = None
    root = str(REPO_ROOT)
    if why:
        checks.append(("配置", WARN, why))
    else:
        try:
            config_file = str(config_path("config"))
            checks.append(("配置", OK, config_file))
        except Exception as exc:                     # noqa: BLE001
            checks.append(("配置", WARN, str(exc)))

    # socket：文件在不在 + 能不能连上（连上就等于"Agent 在跑"）
    socket_path = socket_path_from(args.socket, config)
    if not os.path.exists(socket_path):
        checks.append(("Agent socket", WARN,
                       "%s 不存在（Agent 没在跑？）" % socket_path))
    else:
        client = LocalClient(socket_path)
        try:
            await client.connect()
            checks.append(("Agent socket", OK, "%s 可连（Agent 在跑）" % socket_path))
        except IpcClientError as exc:
            checks.append(("Agent socket", WARN, "连不上 %s：%s" % (socket_path, exc)))
        finally:
            with contextlib.suppress(Exception):
                await client.close()

    # 派生一致性：跑 C++ 那份唯一实现
    if root:
        status, detail = run_derivation_check(
            os.path.join(root, "gui", "build", "gui_config_sync"),
            config_file,
            os.path.join(root, "llm", "config", "llm.env"),
        )
        checks.append(("派生 llm.env", status, detail))

    # 日程
    if config:
        try:
            events = load_events(config)
            checks.append(("日程", OK, "装载 %d 条（语义来自 agent/core/scheduler.py）"
                           % len(events)))
        except SchedulerError as exc:
            checks.append(("日程", WARN, "读不出来（Agent 也会起不来）：%s" % exc))

    # 关键路径（板端本地资产；PC 上没有只报信息，不算错）
    if root:
        for name in ("config/config.yaml", "llm/config/llm.env"):
            path = os.path.join(root, name)
            checks.append((name, OK if os.path.exists(path) else WARN,
                           "在" if os.path.exists(path) else "缺"))

    width = max(len(name) for name, _, _ in checks)
    for name, status, detail in checks:
        print("%-*s  %-4s  %s" % (width, name, status, detail))

    warned = [name for name, status, _ in checks if status == WARN]
    print()
    if warned:
        print("体检结果：%d 项 OK，%d 项警告（%s）"
              % (len(checks) - len(warned), len(warned), "、".join(warned)))
        return EXIT_ERROR
    print("体检结果：%d 项全部 OK" % len(checks))
    return EXIT_OK


# ---------------------------------------------------------------------------
#  命令行
# ---------------------------------------------------------------------------
def _common_options(*, suppress_defaults: bool,
                    with_timeout: bool = True) -> argparse.ArgumentParser:
    """主命令与每个子命令共用的选项，做成"父解析器"（add_help=False）。

    为什么两边都要挂一份：用户既会写 `assistant status --socket X`，也会写
    `assistant --socket X status` —— 只挂在主命令上，前一种会被子解析器拒掉。

    ⚠ 子命令那份必须 `default=argparse.SUPPRESS`：否则子解析器自己的默认值会**盖掉**
      写在子命令**前面**的真实值（argparse 的经典坑）。
    """
    parent = argparse.ArgumentParser(add_help=False)
    fallback = argparse.SUPPRESS if suppress_defaults else None
    parent.add_argument("--socket", default=fallback,
                        help="Agent 的 socket 路径（默认读配置的 ipc.socket_path）")
    parent.add_argument("--config", default=fallback,
                        help="配置文件路径（按名字找 config.yaml/config.example.yaml，"
                             "与 agent/main.py 一致）")
    if with_timeout:
        # ⚠ watch 不挂它：那条命令一直盯到 Ctrl-C / --count，--timeout 对它没有意义
        parent.add_argument("--timeout", type=float,
                            default=argparse.SUPPRESS if suppress_defaults else DEFAULT_TIMEOUT,
                            help="等一条推送的超时秒数（默认 %s）" % DEFAULT_TIMEOUT)
    return parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="assistant",
        description="RK3568 桌面助手 —— 板端控制 CLI（走 IPC 协议；日程能标出真的触发过的）",
        parents=[_common_options(suppress_defaults=False)],
    )
    sub = parser.add_subparsers(dest="command", metavar="<命令>")
    sub.required = True

    p_status = sub.add_parser("status", help="看一眼当前模式与串流连接",
                              parents=[_common_options(suppress_defaults=True)])
    p_status.set_defaults(func=cmd_status)

    p_chat = sub.add_parser("chat", help="发一条消息给 Agent 并等回复",
                            parents=[_common_options(suppress_defaults=True)])
    p_chat.add_argument("text", nargs="+", help="要发的内容（可写多段，自动用空格连接）")
    p_chat.add_argument("--no-wait", action="store_true",
                        help="发出去就返回，不等 llm 回复")
    p_chat.set_defaults(func=cmd_chat)

    p_mode = sub.add_parser("mode", help="切模式（%s，大小写不限）" % "/".join(MODES),
                            parents=[_common_options(suppress_defaults=True)])
    p_mode.add_argument("value", help="目标模式")
    p_mode.add_argument("--no-wait", action="store_true",
                        help="发出去就返回，不等 status 确认")
    p_mode.set_defaults(func=cmd_mode)

    p_watch = sub.add_parser("watch", help="持续打印 Agent 的推送（Ctrl-C 退出；无 --timeout）",
                             parents=[_common_options(suppress_defaults=True,
                                                      with_timeout=False)])
    p_watch.add_argument("--topics", help="只看这些 topic（逗号分隔；默认全看）")
    p_watch.add_argument("--count", type=int, default=0,
                         help="收够 N 条就退出（0 = 不限；给脚本/测试用）")
    p_watch.set_defaults(func=cmd_watch)

    p_schedule = sub.add_parser("schedule",
                                help="列出今天/明天的日程，并标出真的触发过的那些",
                                parents=[_common_options(suppress_defaults=True)])
    p_schedule.add_argument("--today", action="store_true", help="只看今天")
    p_schedule.add_argument("--tomorrow", action="store_true", help="只看明天")
    p_schedule.add_argument("--limit", type=int, default=10,
                            help="每天最多列几行（0 = 不限；默认 %(default)s）")
    p_schedule.add_argument("--no-ask", action="store_true",
                            help="不问 Agent，只按时间比较（离线/对比用）")
    p_schedule.set_defaults(func=cmd_schedule)

    p_doctor = sub.add_parser("doctor", help="体检：配置 / socket / 派生 / 日程 / 关键路径",
                              parents=[_common_options(suppress_defaults=True)])
    p_doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: Optional[list] = None) -> int:
    # 终端编码兜底：GBK 的控制台遇到写不出的字符（例如某些 emoji/符号）会让
    # print 抛 UnicodeEncodeError，整个 CLI 就崩了 —— 宁可显示成 "?"。
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with contextlib.suppress(Exception):
                reconfigure(errors="replace")

    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(args.func(args))
    except KeyboardInterrupt:
        print()                      # Ctrl-C：干净退出（watch 那条命令会用到）
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
