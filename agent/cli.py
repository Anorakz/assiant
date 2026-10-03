#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent/cli.py — 板端控制 CLI（同一个 Agent 的第二条前端）

为什么有它
    GUI 是板端那块屏的前端；CLI 是给 **ssh / 脚本** 用的那一半：看一眼状态、
    发一条消息、切模式、盯推送、查日程、做体检、清理过期的一次性日程。
    板端还放了个 `assistant` 启动器。

走哪条路（这里决定了它能做什么、不能做什么）
    · 只走**IPC 协议**（`docs/ipc-protocol.md` 是线上格式的唯一真源），用现成的
      `agent.ipc.local_client.LocalClient`。
    · **不 import Agent 去读它的内存** —— 所以 Agent 没跑时这里会明确说"连不上"，
      而不是给一份假状态。
    · 日程：**列表**用真的 `agent.core.scheduler` 语义自己算（不需要 Agent 在跑）；
      **"到底触发过哪条"** 只能问运行中的 Agent（协议 `query_schedule` -> `schedule`
      推送，P 系列加的），问不到就只说「已过（按时间）」，并在页脚写明原因。
    · 默认**只读**：CLI 不改日程、不写配置。**唯一显式例外**是 `cleanup --apply`
      （清理已经过去的一次性日程）—— 它不会自动发生，而且走的是 Agent 那套
      文本级删除（`agent/core/schedule_config.py`），不是另写一份。
      "触发了就自动删"那条在 **Agent 侧**（`scheduler.remove_fired_oneoff`）。

退出码
    0 = 成功 ／ 1 = 环境或连接问题 ／ 2 = 参数错（argparse 的默认行为）
"""

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from agent.config import ConfigError, ConfigNotFoundError, config_path, load_config
from agent.core import config_tiers
from agent.core import schedule_config
from agent.core.scheduler import Scheduler, SchedulerError, oneoff_matcher
from agent.core.state_machine import StateMachine
from agent.ipc.local_client import IpcClientError, LocalClient
from agent.ipc.protocol import (
    COMMAND_CHAT_INPUT,
    COMMAND_MUSIC_NEXT,
    COMMAND_MUSIC_PLAY_PAUSE,
    COMMAND_MUSIC_PREV,
    COMMAND_NEXT_BILIBILI,
    COMMAND_PREV_BILIBILI,
    COMMAND_QUERY_SCHEDULE,
    COMMAND_SWITCH_MODE,
    COMMAND_VIDEO_CONTROL,
    MODES,
    SOCKET_PATH,
    TOPIC_BILIBILI,
    TOPIC_LLM,
    TOPIC_MUSIC,
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

#: `music` 命令连上之后先等一小会儿**补推**（T8-4: 新客户端连上就会收到当前曲目与在不在放）。
#: 只有靠它，"播放/暂停"才做得成**语义上的**播放与暂停 —— 协议里只有一个 toggle。
MUSIC_STATE_WAIT = 0.8

#: 音乐命令 -> 线格式动作（三个都在协议里；`agent/ipc/__init__.py` 接进 `Runtime.music_control`）。
#: ⚠ 协议**只有一个** toggle（`music_play_pause`），没有单独的 play/pause —— 所以
#:   `assistant music play|pause` 靠"先问清现在在不在放，再决定发不发"来实现幂等意图。
MUSIC_COMMANDS: Dict[str, str] = {
    "play": COMMAND_MUSIC_PLAY_PAUSE,
    "pause": COMMAND_MUSIC_PLAY_PAUSE,
    "toggle": COMMAND_MUSIC_PLAY_PAUSE,
    "next": COMMAND_MUSIC_NEXT,
    "prev": COMMAND_MUSIC_PREV,
}

#: 音乐子命令能取的动作（argparse 的 choices 也用它，别写两遍）
MUSIC_ACTIONS: Tuple[str, ...] = ("play", "pause", "toggle", "next", "prev")

#: `video` 命令连上之后先等一小会儿**补推**（T11-6: 新客户端连上就会收到当前队列 + 缓冲现状，
#: 里面 `buffer.saw_playing/playing` 就是"现在在不在放"）。幂等意图靠它。
VIDEO_STATE_WAIT = 0.8

#: 视频命令 -> 线格式动作（T11-10f）。
#: ⚠ 与音乐不同: 视频这边**没有**现成的播放/暂停按钮可复用 —— GUI 那颗按钮是"本地点"的，
#:   所以新增了一条 `video_control{action}`（Agent 再推 `control` 给 GUI 去按）。
VIDEO_COMMANDS: Dict[str, str] = {
    "play": COMMAND_VIDEO_CONTROL,
    "pause": COMMAND_VIDEO_CONTROL,
    "toggle": COMMAND_VIDEO_CONTROL,
    "next": COMMAND_NEXT_BILIBILI,
    "prev": COMMAND_PREV_BILIBILI,
}

#: 视频子命令能取的动作（argparse 的 choices 也用它）
VIDEO_ACTIONS: Tuple[str, ...] = ("play", "pause", "toggle", "next", "prev")

#: `--wait-player` 最多等多久"播放器真的按了"（GUI 每 2 秒回报一次 `video_state`，
#: 收到控制后还会立刻补报一次；所以 3 秒够，实在慢就 --timeout 抬）。
VIDEO_PLAYER_WAIT = 3.0

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

    · `kind="fired"` 刚触发了一条 -> **state** / date / scheduled_at / fired_at
      （T12-4: 日程没有 title 了，事实里带的是状态）
    · `kind="state"` 应答查询的快照 -> 条数 + 上限（不把 N 条事实全倒出来）
    · 不认识的 kind（将来加的）-> 原样给出来：**不装懂**，也不假装没有
    """
    kind = data.get("kind")
    if kind == SCHEDULE_KIND_FIRED:
        event = data.get("event") or {}
        parts = ["kind=%s" % SCHEDULE_KIND_FIRED]
        for key in ("state", "date", "scheduled_at", "fired_at"):
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


def mode_path_note(modes_seen: List[str]) -> str:
    """"路上经过 …"那半句（**纯逻辑**，单测钉它）。

    @param modes_seen 一路上真的收到的 `status{mode}`（按到达顺序）
    @return 中间状态的说明；没有中间状态（0 或 1 条）就返回空串

    ⚠ T12-2：跨模式切换（`GAME -> IDLE -> SLEEP`）会推**两条** status。以前 CLI 只看
      第一条，于是"点了睡眠"会打印"没切过去（当前 IDLE）" —— 明明是成功的。现在等到
      **目标那个模式**为止，并把中间经过的状态如实说出来。
    """
    if len(modes_seen) <= 1:
        return ""
    return "（路上经过 %s）" % " → ".join(modes_seen[:-1])


async def cmd_mode(args: argparse.Namespace) -> int:
    """发 `switch_mode`，等到**目标模式**的 `status`（跨模式会经过 IDLE，两跳）。

    @note T12-2：状态机要求"任何切换都经过 IDLE"，所以从 GAME/STUDY 切 SLEEP 会推
          **两条** status（先 IDLE 再 SLEEP）。这里一直等到目标那个模式为止，并把
          中间经过的状态写进结果（`已切到 SLEEP（路上经过 IDLE）`）。
    @note 真正切不动时（已经在那个模式 / 认不出的值）Agent 会把**真实**状态推回来
          （协议 §4）—— 等不到目标就按"没切过去"如实报，并列出路上看到的。
    """
    want = normalize_mode(args.value)
    if want is None:
        hint = ""
        if str(args.value or "").strip().lower() in ("root", "user"):
            hint = ("\n  （`root` / `user` 是**配置的权限层**, 不是 Agent 模式 —— "
                    "要它们请用 `assistant shell`，在会话里 `mode root` / `mode user`。）")
        print("模式只认 %s（大小写不限），收到：%r%s"
              % ("/".join(MODES), args.value, hint), file=sys.stderr)
        return EXIT_USAGE

    path = _resolve_path(args)
    client = LocalClient(path)
    seen: Dict[str, Any] = {}
    arrived = asyncio.Event()
    modes_seen: List[str] = []

    def on_message(topic: str, data: dict) -> None:
        if topic != TOPIC_STATUS:
            return
        seen.clear()
        seen.update(data)
        # ⚠ T12-7: **每条 status 都要记下来**, 不是"只留最后一条"。
        #   `GAME -> IDLE -> SLEEP` 那两跳是**连着推**的: 只读"当前那条"会把它丢成
        #   `["SLEEP"]`, 于是明明走了两跳却打印不出「路上经过 IDLE」
        #   （板端验收 t12_accept.py 抓到的: 真 Agent 上就是连着推的）。
        mode = str(data.get("mode") or "")
        if mode and (not modes_seen or modes_seen[-1] != mode):
            modes_seen.append(mode)
        if mode == want:
            arrived.set()

    client.on_message(on_message)
    try:
        await client.connect()
    except IpcClientError as exc:
        print(connect_hint(path, exc), file=sys.stderr)
        return EXIT_ERROR

    try:
        modes_seen[:] = []
        arrived.clear()
        await client.send_command(COMMAND_SWITCH_MODE, {"value": want})
        if args.no_wait:
            print("已发送：切到 %s" % want)
            return EXIT_OK

        deadline = time.monotonic() + float(args.timeout)
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                await asyncio.wait_for(arrived.wait(), left)
            except asyncio.TimeoutError:
                break
            break                       # on_message 只在**目标模式**到达时才 set

        if modes_seen and modes_seen[-1] == want:
            print("已切到 %s%s" % (want, mode_path_note(modes_seen)))
            return EXIT_OK
        if modes_seen:
            print("Agent 没切过去：最后看到的是 %s（%.1f 秒内没等到 %s；路上经过 %s）—— "
                  "切不动时 Agent 会把真实状态推回来（协议 §4）。"
                  % (modes_seen[-1], args.timeout, want, " → ".join(modes_seen)),
                  file=sys.stderr)
            return EXIT_ERROR
        print("已发出 switch_mode(%s)，但 %.1f 秒内没等到 status 确认。"
              % (want, args.timeout), file=sys.stderr)
        return EXIT_ERROR
    finally:
        await client.close()


def music_decision(action: str, playing: Optional[bool]) -> str:
    """**纯逻辑**：这条音乐命令要不要真发出去（单测钉它）。

    @param action  `play` / `pause` / `toggle` / `next` / `prev`
    @param playing 连上补推里拿到的"现在在不在放"；**None = 没问出来**
    @return "already"（已经是那个状态，别发）/ "send"（发）

    ⚠ 为什么要有它：协议里音乐只有 **一个** `music_play_pause`（toggle），
      没有单独的 play / pause。直接发的话，`assistant music play` 在一首**正在放**的歌上
      会把它**暂停**掉 —— 那不是用户说的"播放"。所以先看状态：
      已经在放就别发（幂等）；状态问不出来就照发（宁可按 toggle 办，也要如实说出来）。
    """
    if action not in ("play", "pause") or playing is None:
        return "send"
    want_playing = (action == "play")
    return "already" if bool(playing) == want_playing else "send"


def music_state_text(data: Dict[str, Any]) -> str:
    """把一条 `music` 推送说成人话（曲目 + 在放/暂停）。"""
    title = str(data.get("title") or "").strip() or "（没说是哪首）"
    playing = data.get("playing")
    if playing is True:
        return "%s（正在播放）" % title
    if playing is False:
        return "%s（已暂停）" % title
    return title


async def cmd_music(args: argparse.Namespace) -> int:
    """音乐传输控制（给 ssh / 脚本用）：`play` / `pause` / `toggle` / `next` / `prev`。

    走的是**现成的** IPC 命令（`music_play_pause` / `music_next` / `music_prev`，
    T8-4 已接进 `Runtime.music_control`）—— 这里不新增协议。

    三条口径：
      · **成功看 `music` 推送**（Agent 成功时会推一条当前曲目/在不在放），不自己编状态；
      · **失败看 `llm` 推送**（音乐没开 / PC 上没在放 / 队列是空的，Agent 会把原因说出来）
        —— 原话照抄，退出码 1；
      · `play` / `pause` 是**幂等意图**：已经在那个状态就只打印一句、**不发命令**
        （协议只有 toggle，发了会反过来，见 `music_decision`）。
    """
    action = str(args.action)
    path = _resolve_path(args)
    client = LocalClient(path)

    state: Dict[str, Any] = {}
    failure: Dict[str, Any] = {}
    kind = {"what": ""}
    arrived = asyncio.Event()

    def on_message(topic: str, data: dict) -> None:
        if topic == TOPIC_MUSIC:
            state.clear()
            state.update(data)
            kind["what"] = "music"
            arrived.set()
        elif topic == TOPIC_LLM:
            failure.clear()
            failure.update(data)
            kind["what"] = "llm"
            arrived.set()

    client.on_message(on_message)
    try:
        await client.connect()
    except IpcClientError as exc:
        print(connect_hint(path, exc), file=sys.stderr)
        return EXIT_ERROR

    try:
        # ---- 1) 先等一小会儿连上补推的那条 music（"现在在不在放"）----
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(arrived.wait(), timeout=MUSIC_STATE_WAIT)
        playing = state.get("playing") if kind["what"] == "music" else None
        if kind["what"] == "music":
            print("现在是：%s" % music_state_text(state))

        # ---- 2) 决定发不发 ----
        if music_decision(action, playing) == "already":
            print("已经是%s了 —— 没发命令。" % ("播放" if action == "play" else "暂停"))
            return EXIT_OK

        arrived.clear()
        kind["what"] = ""
        await client.send_command(MUSIC_COMMANDS[action], {})
        if getattr(args, "no_wait", False):
            print("已发送：music %s" % action)
            return EXIT_OK

        # ---- 3) 等回执：music（成功）或 llm（失败）----
        try:
            await asyncio.wait_for(arrived.wait(), timeout=args.timeout)
        except asyncio.TimeoutError:
            print("已发出 music %s，但 %.1f 秒内没等到回执（Agent 既没推 music 也没推 llm）。"
                  % (action, args.timeout), file=sys.stderr)
            return EXIT_ERROR

        if kind["what"] == "llm":
            print("没做成：%s" % (failure.get("text") or "Agent 没说原因"), file=sys.stderr)
            return EXIT_ERROR

        text = music_state_text(state)
        want = {"play": True, "pause": False}.get(action)
        if want is not None and state.get("playing") is not want:
            print("命令发出去了，但状态还是：%s —— Agent 那边没按预期变。" % text,
                  file=sys.stderr)
            return EXIT_ERROR
        print("%s：%s" % ({"play": "播放中", "pause": "已暂停", "toggle": "现在是",
                           "next": "下一首", "prev": "上一首"}[action], text))
        return EXIT_OK
    finally:
        await client.close()


def video_playing(data: Dict[str, Any]) -> Optional[bool]:
    """从一条 `bilibili` 推送里读"现在在不在放"（**纯逻辑**，单测钉它）。

    @param data `bilibili` 推送的 data（里面 `buffer` 是缓冲的 `snapshot()`）
    @return True / False；**None = 问不出来**（没在播 / 没这个字段）

    ⚠ 为什么不能只看 `buffer.playing`：缓冲的 `playing` 默认是 True（还没起播时也是），
      真正"在放"要 `saw_playing and playing` —— 与 Agent 那边解析 `toggle` 用的是**同一条**真值。
    """
    buffer = data.get("buffer")
    if not isinstance(buffer, dict):
        return None
    if not buffer.get("saw_playing"):
        return None                       # 还没真播过 = 没法说"在放"
    return bool(buffer.get("playing"))


def video_control_action(action: str, playing: Optional[bool]) -> Optional[str]:
    """把 CLI 的动作翻成推给 GUI 的 `control.action`（**纯逻辑**）。

    @return "play" / "pause" / "toggle"；None = 这条动作不需要播放控制（next/prev）
    @note `play`/`pause` 是**幂等意图**：已经在那个状态就不用发了（返回 None 由调用方说明）；
          `toggle` 原样交给 Agent 按它自己的真值解析。
    """
    if action not in ("play", "pause", "toggle"):
        return None
    if action == "toggle":
        return "toggle"
    if playing is None:
        return action                    # 状态问不出来就照发（宁可按用户说的办）
    want = (action == "play")
    return None if bool(playing) == want else action


def video_state_text(data: Dict[str, Any]) -> str:
    """把一条 `bilibili` 推送说成人话（当前那条的视频名 + 在不在放）。"""
    current = data.get("current") if isinstance(data.get("current"), dict) else {}
    title = str(current.get("title") or "").strip()
    if not title:
        buffer = data.get("buffer") if isinstance(data.get("buffer"), dict) else {}
        title = str(buffer.get("title") or "").strip()
    if not title:
        title = "（Agent 没说放的是哪条）"
    playing = video_playing(data)
    if playing is True:
        return "%s（正在播放）" % title
    if playing is False:
        return "%s（已暂停）" % title
    return title


async def cmd_video(args: argparse.Namespace) -> int:
    """视频传输控制（给 ssh / 脚本用）：`play` / `pause` / `toggle` / `next` / `prev`。

    走的是 T11-10f 那条新命令 `video_control{action}`（play/pause/toggle）与现成的
    `next_bilibili` / `prev_bilibili`（走队列）—— 队列内容仍旧由**对话/画面**决定，
    这里只做"传输控制"，与音乐同一条口径。

    三条口径：
      · **成功看 Agent 的 `bilibili` 回执**（带 `control{action,seq}` 的那条推送）；
      · **失败看 `llm` 推送**（没在播 / 没 GUI / 到队尾），**原话照抄**，退出码 1；
      · `play` / `pause` 是**幂等意图**：已经在那个状态就只打印一句、**不发命令**
        （看的是补推里的 `buffer.saw_playing/playing`）。
      · `--wait-player` 再等一步"播放器真的按了"（GUI 的 `video_state` 回报）——
        ⚠ 协议**没有请求 id**，所以默认只能确认到"Agent 收下并推给播放器了"，
        加这个开关才等到播放器落地。`next`/`prev` 只看队列有没有真的走一格。
    """
    action = str(args.action)
    path = _resolve_path(args)
    client = LocalClient(path)

    state: Dict[str, Any] = {}          # 最近一条 bilibili 推送（补推也算）
    failure: Dict[str, Any] = {}        # 失败时 Agent 的 llm 原话
    ack: Dict[str, Any] = {}            # Agent 收下播放控制的那条 control{action,seq}
    result: Dict[str, Any] = {}         # 播放器回报的 control_result{seq,action,playing}
    kind = {"what": ""}
    arrived = asyncio.Event()
    result_arrived = asyncio.Event()

    def on_message(topic: str, data: dict) -> None:
        if topic == TOPIC_BILIBILI:
            state.clear()
            state.update(data)
            control = data.get("control")
            if isinstance(control, dict) and control:
                ack.clear()
                ack.update(control)
                kind["what"] = "ack"
                arrived.set()
            elif not arrived.is_set():
                kind["what"] = "bilibili"
                arrived.set()
            if isinstance(data.get("control_result"), dict) and data["control_result"]:
                result.clear()
                result.update(data["control_result"])
                result_arrived.set()
        elif topic == TOPIC_LLM:
            failure.clear()
            failure.update(data)
            kind["what"] = "llm"
            arrived.set()

    client.on_message(on_message)
    try:
        await client.connect()
    except IpcClientError as exc:
        print(connect_hint(path, exc), file=sys.stderr)
        return EXIT_ERROR

    try:
        # ---- 1) 先等一小会儿连上补推的那条 bilibili（"现在在不在放"）----
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(arrived.wait(), VIDEO_STATE_WAIT)
        playing = video_playing(state)
        before = dict(state)
        if state.get("count") or state.get("buffer"):
            print("现在是：%s" % video_state_text(state))

        # ---- 2) play / pause 是**幂等意图**：已经在那个状态就只打印、不发命令 ----
        control_action = video_control_action(action, playing)
        if action in ("play", "pause") and control_action is None:
            print("已经是%s了 —— 没发命令。" % ("在播" if action == "play" else "暂停"))
            return EXIT_OK

        old_current = before.get("current") if isinstance(before.get("current"), dict) else {}
        old_bvid = old_current.get("bvid") or ""
        old_index = before.get("index")

        arrived.clear()
        kind["what"] = ""
        result_arrived.clear()
        if control_action is not None:
            await client.send_command(COMMAND_VIDEO_CONTROL, {"action": control_action})
        else:
            await client.send_command(VIDEO_COMMANDS[action], {})
        if getattr(args, "no_wait", False):
            print("已发送：video %s" % action)
            return EXIT_OK

        # ---- 3) 等回执 ----
        #   播放控制: 带 `control` 的那条推送（= Agent 收下并推给 GUI 了）;
        #   next/prev: 队列真的走了一格（index 或 bvid 变了）;
        #   失败两条路都是 `llm`（没在播 / 没 GUI / 到队尾 —— Agent 的原话）。
        deadline = time.monotonic() + float(args.timeout)
        moved = False
        while time.monotonic() < deadline:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(arrived.wait(), left)
            if kind["what"] == "llm":
                break
            if control_action is not None and ack.get("action"):
                break
            if control_action is None:
                current = state.get("current") if isinstance(state.get("current"), dict) else {}
                if state.get("index") != old_index or (current.get("bvid") or "") != old_bvid:
                    moved = True
                    break
            arrived.clear()
            kind["what"] = ""

        if kind["what"] == "llm":
            print("没做成：%s" % (failure.get("text") or "Agent 没说原因"), file=sys.stderr)
            return EXIT_ERROR
        if control_action is not None and not ack.get("action"):
            print("已发出 video %s，但 %.1f 秒内没等到 Agent 的 bilibili 回执"
                  "（既没推 control 也没推 llm）。" % (action, args.timeout), file=sys.stderr)
            return EXIT_ERROR
        if control_action is None and not moved:
            print("已发出 video %s，但 %.1f 秒内队列没有走位（既没推 bilibili 也没推 llm）。"
                  % (action, args.timeout), file=sys.stderr)
            return EXIT_ERROR

        # ---- 4) 说结果 ----
        if control_action is None:
            current = state.get("current") if isinstance(state.get("current"), dict) else {}
            title = str(current.get("title") or "").strip() or "（Agent 没说放的是哪条）"
            verb = "下一集" if action == "next" else "上一集"
            print("%s：%s（正在缓冲 —— 攒够 15 秒才让播放器开；`assistant watch` 能看到"
                  "「可以播了」）" % (verb, title))
            return EXIT_OK

        resolved = str(ack.get("action") or control_action)
        verb = "播放" if resolved == "play" else "暂停"
        # ⚠ 协议**没有请求 id**, 所以默认只确认到"Agent 收下并推给 GUI 了";
        #   `--wait-player` 再等**播放器自己回报**（GUI 的 video_state 会让 Agent
        #   推一条 `control_result{seq,action,playing}` —— 那是 GUI 的真话）。
        if not getattr(args, "wait_player", False):
            print("已让播放器%s：%s" % (verb, video_state_text(state)))
            return EXIT_OK

        try:
            await asyncio.wait_for(result_arrived.wait(),
                                   max(0.5, float(args.timeout)))
        except asyncio.TimeoutError:
            print("Agent 收下了 %s（seq=%s），但 %.1f 秒内没等到播放器回报 —— "
                  "GUI 可能没连上、或者这条已经不在放了。" % (resolved, ack.get("seq"),
                                                          args.timeout), file=sys.stderr)
            return EXIT_ERROR
        want = (resolved == "play")
        actual = bool(result.get("playing"))
        if actual != want:
            print("Agent 收下了 %s，但播放器回报的是%s —— 没按预期变。"
                  % (resolved, "在播" if actual else "暂停"), file=sys.stderr)
            return EXIT_ERROR
        print("已让播放器%s：%s（播放器已回报）" % (verb, video_state_text(state)))
        return EXIT_OK
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
    """一行的文本：`HH:MM  状态`（T12-4 起日程只有 时间 + 状态）。

    ⚠ 与 GUI 日程区同一行格式（`SchedulePanel::rowTextOf`）—— 这样"CLI 的日程"
      与"界面上的日程"可以直接逐行 diff，不用人眼对着两张图读。
    """
    return "%s  %s" % (row["time"], row["state"])


def schedule_rows(events: List[Any], day, now: datetime,
                  facts: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """某一天的日程行（按 `(时间, 状态)` 升序）。

    `occurs_on()` 是**真的** Python 语义（recurring 看星期、oneoff 看日期）；
    `past` 只在**今天**才有意义 —— "现在时刻已经过了 start"。对明天来说
    `now > start` 恒成立（例如 12:58 看明天的 08:30），那样标"已过"就是错的。
    ⚠ 与 GUI 同一条规则（`SchedulePanel`：`past` 只作用于今天段）——C4 起真 Agent
      时正是这里露了馅（明天 08:30 被标成"已过"）。

    @param facts 运行中 Agent 报回来的触发事实（`query_schedule` 的应答）。给了就在
                 行里带上 `fired_at`（这一条**真的**被触发过的时刻）；不给就是 None。
    @note 事实与行按 **`trigger_at`**（就是 `scheduled_at` 那个字符串）对齐 ——
          这是 Agent 与 CLI 用**同一个** `ScheduleEvent.trigger_at()` 算出来的。
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
        trigger_at = event.trigger_at(day).isoformat(timespec="minutes")
        rows.append({
            "time": "%02d:%02d" % event.start,
            "state": event.state.value.upper(),
            "past": bool(is_today and now_minute > (event.start[0] * 60 + event.start[1])),
            "day": day,
            "trigger_at": trigger_at,
            "fired_at": (by_trigger.get(trigger_at) or {}).get("fired_at"),
        })
    rows.sort(key=lambda row: (row["time"], row["state"]))
    return rows


def fired_clock(fired_at: Any) -> str:
    """事实里的 `fired_at`（`YYYY-MM-DDTHH:MM:SS`）→ `HH:MM:SS`。

    只取时刻：这一行已经在"哪一天"的段里了，再打一次日期是噪音。
    解析不出来就原样返回（宁可难看，不要假装懂）。
    """
    text = str(fired_at or "")
    _, sep, clock = text.partition("T")
    return clock if sep else text


def row_mark(row: Dict[str, Any], asked: bool) -> str:
    """一行后面的标记（三种状态 + 空）。

    ⚠ 只有**问到了** Agent 才敢说「未触发」（`asked`）；没问到时只说「已过」——
      那是纯时间比较，不掺一句关于"触发没触发"的暗示。
    """
    if row.get("fired_at"):
        return "  ← 已触发 %s" % fired_clock(row["fired_at"])
    if row["past"] and asked:
        return "  ← 已过（未触发）"
    if row["past"]:
        return "  ← 已过"
    return ""


def render_schedule(days: List[Tuple[str, Any]], rows_of, now: datetime,
                    limit: int = 10, asked: bool = False) -> List[str]:
    """渲染成"人读的行"（不含页脚）。`rows_of(day)` 给当天的行。

    @param asked 是否**问到了** Agent 的触发记录（`--no-ask` / 连不上就是 False）。
                 只有问到时才敢把"过了 start 却不在记录里"的行标成「已过（未触发）」；
                 没问到时保持老样子「已过」—— 那是纯时间比较，不掺一句暗示。
    @param limit 每天最多几行（0 = 不限）。⚠ 这个"按天"的口径是给"整天视图"用的；
                 R 系列之后 CLI 默认走的是 `render_window()`（窗口 = 一整段连续时间，
                 配额按**窗口**算，不是按天）。
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
            lines.append("  %s%s" % (row_text(row), row_mark(row, asked)))
        if len(rows) > len(shown):
            lines.append("  …还有 %d 项（--limit 可调）" % (len(rows) - len(shown)))
    return lines


# ---------------------------------------------------------------------------
#  窗口（R 系列：CLI 与 GUI 都只看"接下来 N 小时"）
# ---------------------------------------------------------------------------
#: 默认窗口长度（小时）。CLI 的 `--hours` 默认值；GUI 侧同一口径（见 docs/gui.md）。
WINDOW_HOURS_DEFAULT = 24.0

#: "刚触发"的尾巴（分钟）。窗口只往前看，本来不该出现已经过去的条目；但**刚刚**过去的
#: 那一小段要留着 —— 否则"到点了、到底触发没有"在列表里完全看不见，而那恰好是 P 系列
#: 做出来的那层信息（`← 已触发 HH:MM:SS` / `← 已过（未触发）`）。
TAIL_MINUTES = 30


def positive_hours(text: str) -> float:
    """`--hours` 的解析：必须是正数。

    ⚠ 用 argparse 的 `type` 而不是在命令里手检：这样 `--hours 0` / `--hours abc`
      直接落进"参数错（退出码 2）"，与其它参数错误的待遇一致。
    """
    try:
        value = float(text)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("需要一个数字，得到 %r" % (text,))
    if value <= 0:
        raise argparse.ArgumentTypeError("必须是正数（小时），得到 %r" % (text,))
    return value


def window_range(now: datetime, hours: float,
                 tail_minutes: int = TAIL_MINUTES) -> Tuple[datetime, datetime]:
    """窗口 = `[now - tail, now + hours)`。起点把"最近 tail 分钟"也包进来。

    @note **分钟粒度**：把 `now` 截到分钟再算。行的时刻只有分钟（`HH:MM`），两侧
          （CLI 与 GUI 的 `ScheduleModel::applyWindow`）要用同一个口径 —— 否则
          "当前这一分钟"的那条在两边会不一样。
    """
    minute = now.replace(second=0, microsecond=0)
    return minute - timedelta(minutes=tail_minutes), minute + timedelta(hours=hours)


def window_end_text(end: datetime, now: datetime) -> str:
    """窗口终点的说法：`明天 18:26` / `09-25 18:26`（与段标题同一套口径）。"""
    label = {0: "今天", 1: "明天", 2: "后天"}.get((end.date() - now.date()).days)
    return "%s %s" % (label or end.strftime("%m-%d"), end.strftime("%H:%M"))


def day_heading(day: date, today: date) -> str:
    """段标题：今天/明天/后天带 ISO 日期；更远的直接给日期（那时"今天"没有意义）。"""
    weekday = "一二三四五六日"[day.weekday()]
    label = {0: "今天", 1: "明天", 2: "后天"}.get((day - today).days)
    if label is None:
        return "%s（周%s）" % (day.isoformat(), weekday)
    return "%s（%s 周%s）" % (label, day.isoformat(), weekday)


def _row_moment(day: date, row: Dict[str, Any]) -> datetime:
    """行在时间轴上的位置 = 那天 `start` 的那一刻。

    ⚠ 判据是**用户看到的那一刻**（start），不是 `trigger_at`：`start=14:00`、
      `remind_before_min=10` 的条目在 13:55 看是"马上要来"，用 trigger_at 判定会
      因为提醒时刻（13:50）已过而让它从列表里消失。
    """
    hour, minute = row["time"].split(":")
    return datetime(day.year, day.month, day.day, int(hour), int(minute))


def window_days(events: List[Any], now: datetime, hours: float,
                facts: Optional[List[Dict[str, Any]]] = None,
                tail_minutes: int = TAIL_MINUTES) -> List[Tuple[date, List[Dict[str, Any]]]]:
    """窗口内的日程，按天分组（只保留**有行的天**；天与行都按时间升序）。

    @param facts 运行中 Agent 的触发事实：① 给行补上 `fired_at`；② **配置里已经没有**
                 的那些（一次性日程触发后被删掉）在这里被补成行 —— 否则"刚触发"的
                 尾巴在配置里找不到人（那是 R3 之后的常态）。
    """
    begin, end = window_range(now, hours, tail_minutes)
    by_day: Dict[date, List[Dict[str, Any]]] = {}
    seen: Set[str] = set()

    day = begin.date()
    while day <= end.date():
        keep = []
        for row in schedule_rows(events, day, now, facts=facts):
            if begin <= _row_moment(day, row) < end:
                keep.append(row)
                seen.add(row["trigger_at"])
        if keep:
            by_day[day] = keep
        day += timedelta(days=1)

    for fact in (facts or []):
        key = fact.get("scheduled_at")
        if not key or key in seen:
            continue
        try:
            moment = datetime.strptime(str(key), "%Y-%m-%dT%H:%M")
        except ValueError:
            continue                      # 事实里的时刻看不懂 -> 落不到窗口上, 跳过
        if not (begin <= moment < end):
            continue
        by_day.setdefault(moment.date(), []).append({
            "time": moment.strftime("%H:%M"),
            "state": str(fact.get("state") or "").upper(),
            "past": True,
            "day": moment.date(),
            "trigger_at": key,
            "fired_at": fact.get("fired_at"),
            #: 只在事实里、配置里已经没有它了（一次性日程触发后被删）
            "from_fact": True,
        })

    for rows in by_day.values():
        rows.sort(key=lambda row: (row["time"], row["state"]))
    return sorted(by_day.items())


def render_window(events: List[Any], now: datetime, hours: float,
                  facts: Optional[List[Dict[str, Any]]] = None,
                  limit: int = 10, asked: bool = False,
                  tail_minutes: int = TAIL_MINUTES) -> List[str]:
    """渲染窗口（不含窗口说明头与页脚）。

    @param limit **整个窗口**最多几行（0 = 不限）—— 不是每天几行：窗口是一段连续时间，
                 按天各给一份配额会把近处截掉。
    """
    days = window_days(events, now, hours, facts=facts, tail_minutes=tail_minutes)
    total = sum(len(rows) for _, rows in days)

    left = None if limit <= 0 else limit
    shown: List[Tuple[date, List[Dict[str, Any]]]] = []
    for day, rows in days:
        if left is None:
            shown.append((day, rows))
            continue
        take = rows[:left]
        if take:
            shown.append((day, take))
        left -= len(take)
        if left <= 0:
            break

    lines = []
    for day, rows in shown:
        lines.append(day_heading(day, now.date()))
        for row in rows:
            lines.append("  %s%s" % (row_text(row), row_mark(row, asked)))

    shown_count = sum(len(rows) for _, rows in shown)
    if not lines:
        lines.append("（窗口内没有日程）")
    elif shown_count < total:
        lines.append("  …还有 %d 项（--limit 可调）" % (total - shown_count))
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
    """列出**接下来 N 小时**（默认 24）的日程，并标出**真的触发过**的那些。

    三件事分开说清楚：
      · 窗口：只看 `[现在 - 30 分钟, 现在 + N 小时)`（判据是行的 `start`）。所以
        "今天早上 08:30 那条"在下午看是**不会出现**的 —— 它已经不在"接下来"里了；
        留着的 30 分钟只是为了让你还能看到"刚刚那一条触发没触发"。
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
    begin, end = window_range(now, args.hours)

    print("日程（配置共 %d 条；%s）" % (len(events), (config_path("config"))))
    print("窗口：%s → %s（%g 小时；另有最近 %d 分钟里刚过去的）"
          % (begin.strftime("%H:%M"), window_end_text(end, now), args.hours, TAIL_MINUTES))
    for line in render_window(events, now, args.hours, facts=facts,
                              limit=args.limit, asked=facts is not None):
        print(line)

    if facts is None:
        print(SCHEDULE_FOOTER)
        print("（%s —— 所以这里只能按时间比较。）" % reason)
    else:
        print(SCHEDULE_FOOTER_LIVE % len(facts))
    return EXIT_OK


# ---------------------------------------------------------------------------
#  cleanup（清理已经过去的一次性日程）
# ---------------------------------------------------------------------------
def stale_oneoffs(events: List[Any], today) -> List[Any]:
    """**已经过去、不会再触发**的一次性日程（`date < today`）。

    ⚠ **今天的不算**：`late_grace_min`（迟到的容忍度）可能还认它 —— 保守起见留给 Agent
      的正常路径处理（开关打开时，真触发了就会自动删）。
    """
    return [event for event in events if event.on is not None and event.on < today]


def today_passed_oneoffs(events: List[Any], now: datetime) -> List[Any]:
    """今天**已经过了 start** 的一次性日程（只用来提示，不会被清理）。"""
    now_minute = now.hour * 60 + now.minute
    return [event for event in events
            if event.on is not None and event.on == now.date()
            and now_minute > (event.start[0] * 60 + event.start[1])]


async def cmd_cleanup(args: argparse.Namespace) -> int:
    """清理**已经过去**的一次性日程（它们不会再触发了）。

    默认**只列出**（dry-run，与 `gui_config_sync` 同一套习惯）；`--apply` 才真删 ——
    文本级删除 + `.bak` + 原子写，走的是 Agent 那套（`agent/core/schedule_config.py`），
    不是另写一份。

    @note 这是 CLI"默认只读"的**唯一显式例外**：它不会自动发生，是你敲了 `--apply`。
          自动那条在 Agent 侧：`scheduler.remove_fired_oneoff`（触发了就删）。
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

    now = datetime.now()
    stale = stale_oneoffs(events, now.date())
    passed_today = today_passed_oneoffs(events, now)

    if not stale:
        print("没有需要清理的一次性日程（只清**已经过去**的那些）。")
    else:
        print("要清理的一次性日程（已经过去、不会再触发）：")
        for event in stale:
            print("  %s  %s %s" % (event.on.isoformat(), "%02d:%02d" % event.start,
                                   event.state.value.upper()))
        print("共 %d 条；%s" % (len(stale),
                              "已开始删除" if args.apply
                              else "加 --apply 才会真删（会留一份 config.yaml.bak）"))
    if passed_today:
        print("（今天已经过了 start 的一次性日程有 %d 条，**没动** —— late_grace_min "
              "可能还认它；Agent 侧开关打开时，真触发了会自动删。）" % len(passed_today))

    if not args.apply or not stale:
        return EXIT_OK

    target = config_path("config")
    if target.name.endswith(".example.yaml"):
        print("不写模板：%s 是 example 模板，不是真源 —— 先 cp 成 config.yaml 再清理。"
              % target, file=sys.stderr)
        return EXIT_ERROR

    removed, failed = 0, []
    for event in stale:
        label = "%s %s" % (event.on.isoformat(), "%02d:%02d" % event.start)
        try:
            ok = schedule_config.remove_oneoff_from_file(target, oneoff_matcher(event))
        except OSError as exc:
            failed.append((label, str(exc)))
            continue
        if ok:
            removed += 1
            print("已删除 %s  %s" % (label, event.state.value.upper()))
        else:
            failed.append((label, "配置里对不上（可能已经删过了，或者被人改过）"))

    print("共删除 %d 条（原文件留了一份 %s.bak）。" % (removed, target.name))
    for label, reason in failed:
        print("  没删成 %s：%s" % (label, reason), file=sys.stderr)
    return EXIT_ERROR if failed else EXIT_OK


# ---------------------------------------------------------------------------
#  tag（第 8 条命令）
# ---------------------------------------------------------------------------
def tag_settings(config: Any, args: argparse.Namespace) -> Dict[str, Any]:
    """把"配置 + 命令行"合成打标签要用的设置（纯逻辑，可单测）。

    优先级: 命令行 > config.yaml > 代码默认值。
    """
    from agent.core.wallpaper import DEFAULT_WALLPAPER_DIR
    from agent.vision import wall_data

    wall = config.get("wallpaper") if isinstance(config, dict) else None
    wall = wall if isinstance(wall, dict) else {}
    tagging = wall.get("tagging") if isinstance(wall.get("tagging"), dict) else {}

    top_k = getattr(args, "top_k", None) or tagging.get("top_k")
    try:
        top_k = int(top_k)
    except (TypeError, ValueError):
        top_k = None

    return {
        "dir": getattr(args, "dir", None) or wall.get("dir") or DEFAULT_WALLPAPER_DIR,
        "data_file": wall_data.resolve_data_file(
            getattr(args, "data_file", None) or tagging.get("data_file")),
        "top_k": top_k if (top_k and top_k > 0) else None,
        "vocab_overrides": tagging.get("vocab"),
        "vision": config.get("vision") if isinstance(config.get("vision"), dict) else None,
    }


def tag_plan_text(plan: Dict[str, Any], images: int, data_file: str) -> List[str]:
    """把"要打哪些"渲染成给人看的几行（dry-run 与 --apply 共用，便于测试）。"""
    reasons: Dict[str, int] = {}
    for item in plan["to_tag"]:
        reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
    detail = "，".join("%s %d" % (name, count) for name, count in sorted(reasons.items()))
    lines = [
        "壁纸目录里 %d 张图；数据文件 %s" % (images, data_file),
        "已是最新 %d 张%s" % (plan["fresh"], ("；要打 %d 张（%s）" % (len(plan["to_tag"]), detail))
                            if plan["to_tag"] else "（没有要打的）"),
    ]
    if plan["orphans"]:
        lines.append("数据文件里有 %d 行是「图已经不在」的（--prune 可以清掉）"
                     % len(plan["orphans"]))
    return lines


async def cmd_tag(args: argparse.Namespace) -> int:
    """给壁纸打标签（SigLIP 零样本，三轴），结果写 `config/wall_data.jsonl`。

    默认**只算计划**（dry-run，与 `cleanup` 同一套习惯）；`--apply` 才真打。

    @note 这是 CLI 的**第二个会写文件的命令**，但它写的不是配置真源:
          `cleanup --apply` 写 `config.yaml`；`tag --apply` 写的是**派生数据**
          （`wall_data.jsonl`，机器生成的标签）。边界写在 docs/tagging.md。
    @note 打标签要过 NPU，**开发机上没有 numpy/cv2** —— dry-run 不需要它们，
          所以"看计划"在哪儿都能跑；`--apply` 缺依赖会明确说缺什么。
    """
    from agent.core.wallpaper import WallpaperDeck, WallpaperError
    from agent.vision import tag_vocab, tagger, wall_data

    config, why = load_plane_config(args.config)
    if why:
        print("读不到配置（%s）——壁纸目录与词表在 config.yaml 的 wallpaper 段。" % why,
              file=sys.stderr)
        return EXIT_ERROR

    settings = tag_settings(config, args)
    directory, data_file = settings["dir"], settings["data_file"]
    axes = tag_vocab.with_overrides(settings["vocab_overrides"])
    vocab8 = tag_vocab.vocab_sha8(axes)
    top_k = settings["top_k"] or tagger.DEFAULT_TOP_K

    try:
        images = WallpaperDeck(directory).scan()
    except WallpaperError as exc:
        print("壁纸目录用不了：%s" % exc, file=sys.stderr)
        return EXIT_ERROR

    loaded = wall_data.load(data_file)
    records = loaded["records"]
    for problem in loaded["problems"]:
        print("数据文件有问题：%s" % problem, file=sys.stderr)

    tagging = tagger.Tagging(settings["vision"], axes=axes, top_k=top_k)
    model8 = tagging.model_sha8()
    plan = wall_data.tag_plan(records, images, model8, vocab8, force=args.force)

    # 词表向量缓存（数据文件第一行）: 同模型同词表就复用，省掉 32 条标签的文本塔编码
    cached_vocab = loaded["vocab"] if isinstance(loaded["vocab"], dict) else None
    vocab_ok = wall_data.vocab_matches(cached_vocab, model8, vocab8, axes)
    if not vocab_ok:
        cached_vocab = None

    print("词表 %d 条标签 / %d 个轴（scene %d、tone %d、mood %d），指纹 %s%s"
          % (sum(len(axis.labels) for axis in axes), len(axes),
             len(axes[0].labels), len(axes[1].labels), len(axes[2].labels), vocab8,
             "；词表向量: 复用数据文件里的缓存" if cached_vocab else "；词表向量: 需要重新编码"))
    for line in tag_plan_text(plan, len(images), data_file):
        print(line)

    kept = list(records)
    if args.prune:
        existing = set(images)
        kept, dropped = wall_data.prune_records(records, existing)
        if dropped:
            print("--prune：清掉 %d 行（图已经不在了）：%s"
                  % (len(dropped), "、".join(os.path.basename(p) for p in dropped[:5])
                     + ("…" if len(dropped) > 5 else "")))
            if args.apply:
                wall_data.write_records(data_file, _sorted_records(kept), vocab=cached_vocab)
        else:
            print("--prune：没有需要清的行")

    todo = list(plan["to_tag"])
    limit = getattr(args, "limit", None)
    if limit is not None:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            print("--limit 需要正整数", file=sys.stderr)
            return EXIT_USAGE
        if limit <= 0:
            print("--limit 需要正整数", file=sys.stderr)
            return EXIT_USAGE
        if len(todo) > limit:
            print("本轮只打前 %d 张（--limit %d），其余的再跑一次即可" % (limit, limit))
            todo = todo[:limit]

    if not args.apply:
        if not vocab_ok:
            print("（加 --apply 时会顺手把词表向量写进数据文件第一行，下次同模型同词表就能复用）")
        print("（dry-run：加 --apply 才会真打。会写 %s，并在旁边留一份 .bak）" % data_file)
        return EXIT_OK

    if not todo and vocab_ok:
        print("没有要打的图，词表缓存也是最新的 —— 什么都不用做。")
        return EXIT_OK

    if not todo:
        print("没有要打的图；只补写词表向量缓存（下次增量打图就不用重新编码词表了）。")

    reason = tagger.check_dependencies()
    if reason:
        print("%s\n（打标签必须在板端跑：模型在 NPU 上。）" % reason, file=sys.stderr)
        return EXIT_ERROR

    # 词表向量: 有缓存就复用（省 61 s），否则重新编码 —— 编完无论如何都要写回文件
    cached_vectors = wall_data.vocab_vectors(cached_vocab, axes) if cached_vocab else None
    tagging.prepare(label_vectors=cached_vectors)
    try:
        vocab_record = wall_data.make_vocab_record(
            axes, {axis.name: [vec for _, vec in tagging.label_vectors()[axis.name]]
                   for axis in axes}, model8, vocab8)
    except (wall_data.WallDataError, AttributeError, KeyError, TypeError) as exc:
        print("词表向量存不进数据文件（%s）—— 这次先不存，标签照样打。" % exc, file=sys.stderr)
        vocab_record = cached_vocab

    # 已有的记录按 path 索引，打了哪张就替换哪张
    by_path = {str(record.get("path")): record for record in kept}
    done, failed = 0, []
    started = time.time()
    per_image: List[int] = []

    if vocab_record is not None and not todo:
        # 只有词表要补写
        try:
            wall_data.write_records(data_file, _sorted_records(by_path.values()),
                                    vocab=vocab_record)
        except wall_data.WallDataError as exc:
            print("写数据文件失败：%s" % exc, file=sys.stderr)
            return EXIT_ERROR
        print("词表向量已写入 %s（%d 条标签，%s）"
              % (data_file, sum(len(axis.labels) for axis in axes),
                 "复用缓存" if tagging.labels_from_cache else "本次编码 %.1fs" % tagging.label_s))
        return EXIT_OK

    for index, item in enumerate(todo, 1):
        path = item["path"]
        name = os.path.basename(path)
        try:
            tags, vector, ms, width, height = tagging.score(path)
        except Exception as exc:                       # noqa: BLE001 - 单张失败不拖垮整批
            failed.append((name, "%s: %s" % (type(exc).__name__, exc)))
            print("  [%d/%d] %s 失败：%s" % (index, len(todo), name, exc), file=sys.stderr)
            continue
        try:
            sha = wall_data.image_sha256(path)
            size = os.path.getsize(path)
        except (OSError, wall_data.WallDataError) as exc:
            failed.append((name, str(exc)))
            continue
        by_path[path] = wall_data.with_usage(
            wall_data.make_record(path, width, height, size, sha, ms, model8, vocab8,
                                  tags, vector),
            wall_data.usage_of(by_path.get(path)))       # T8-6: 重打不许清零使用次数
        done += 1
        per_image.append(ms)
        # **每张都写盘**: 40 张要跑一两分钟，崩在中途不该丢掉已经打好的
        try:
            wall_data.write_records(data_file, _sorted_records(by_path.values()),
                                    vocab=vocab_record)
        except wall_data.WallDataError as exc:
            print("写数据文件失败：%s" % exc, file=sys.stderr)
            return EXIT_ERROR
        top = tags.get("scene", [["?", 0]])[0][0]
        print("  [%d/%d] %s  %dx%d  %d ms  scene=%s"
              % (index, len(todo), name, width, height, ms, top))

    elapsed = time.time() - started
    median = sorted(per_image)[len(per_image) // 2] if per_image else 0
    print("打过 %d 张，失败 %d 张；总耗时 %.1fs，每张中位 %d ms（模型加载 %.1fs、词表 %s）"
          % (done, len(failed), elapsed, median, tagging.load_s,
             "复用缓存" if tagging.labels_from_cache else "编码 %.1fs" % tagging.label_s))
    for name, why in failed:
        print("  没打成 %s：%s" % (name, why), file=sys.stderr)
    print("已写入 %s（旁边留了一份 %s.bak）。" % (data_file, os.path.basename(data_file)))
    return EXIT_ERROR if failed else EXIT_OK


def _sorted_records(records: Any) -> List[Any]:
    """按 path 排序 —— 文件内容与目录顺序无关，重跑结果才可比对。"""
    return sorted(records, key=lambda item: str(item.get("path")))


# ---------------------------------------------------------------------------
#  doctor
# ---------------------------------------------------------------------------
OK = "OK"
WARN = "警告"


def derivation_check(config_file: Optional[str], env_file: Optional[str]) -> Tuple[str, str]:
    """派生一致性：用 **Python 那份唯一实现**（`agent/core/llm_env.py`）算一次差异。

    ⚠ T14-2 之前这一步是跑 C++ 的 `gui_config_sync`（dry-run）；实现搬到 Python 之后
      （见 `docs/adr/0005`）这里不再依赖板端有没有构建 gui/ —— 而且"映射表只有一份"
      这条承诺现在由**这一个模块**承担。
    """
    from agent.core import llm_env

    if not config_file or not os.path.exists(config_file):
        return WARN, "没找到 %s（真源不在，派生无从谈起）" % config_file
    if not env_file or not os.path.exists(env_file):
        return WARN, "没找到 %s（还没派生过？）" % env_file
    try:
        plans = llm_env.plan(config_path=config_file, env_path=env_file)
    except llm_env.LlmEnvError as exc:
        return WARN, "算不出来：%s" % exc
    if not plans:
        return OK, "llm.env 与 config.yaml 一致"
    diff = "；".join("%s: %s -> %s" % (item["key"],
                                       item["old"] if item["old"] is not None else "(缺)",
                                       item["new"]) for item in plans)
    return WARN, "有 %d 处差异（下一次保存会按真源覆盖）：%s" % (len(plans), diff[:200])


def run_derivation_check(config_file: str, env_file: str) -> Tuple[str, str]:
    """保留旧名字给调用方（内容已经是纯 Python）。"""
    return derivation_check(config_file, env_file)


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

    # 派生一致性：用 Python 那份唯一实现（T14-2 起不再依赖板端构建 gui/）
    if root:
        status, detail = derivation_check(
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
#  设置写入（T13-8）: `assistant set` 与 `assistant study`
# ---------------------------------------------------------------------------
#: 三组设置项 + B 站 cookie。**表驱动**: 加一个设置项 = 表里加一行（键必须存在于
#: `config.example.yaml`，否则会被 `settings_config` 拒掉）。
#: 值的类型跟着模板走, 所以这里只写"命令行开关 -> 点号路径"。
SET_GROUPS: Dict[str, Dict[str, Dict[str, str]]] = {
    "study": {
        "numbers": {
            "--relative-band": "study.relative_band",
            "--focus-interval-min": "study.focus_interval_min",
            "--recheck-interval-min": "study.recheck_interval_min",
            "--max-failures": "study.max_failures",
            "--cooldown-min": "study.cooldown_min",
            "--cooldown-probe-min": "study.cooldown_probe_min",
            "--target-unknown-rate": "study.target_unknown_rate",
            "--min-labeled": "study.min_labeled",
            "--max-anchors-per-class": "study.max_anchors_per_class",
        },
        "switches": {
            "--enabled": ("study.enabled", True),
            "--disabled": ("study.enabled", False),
            "--remind": ("study.remind", True),
            "--no-remind": ("study.remind", False),
            "--back-to-desktop": ("study.back_to_desktop", True),
            "--no-back-to-desktop": ("study.back_to_desktop", False),
            "--learn": ("study.learn", True),
            "--no-learn": ("study.learn", False),
            "--adapt": ("study.adapt", True),
            "--no-adapt": ("study.adapt", False),
            "--skip-on-keyword": ("study.skip_on_keyword", True),
            "--no-skip-on-keyword": ("study.skip_on_keyword", False),
        },
    },
    # 认游戏那条（含 B 站 cookie 是另一个组）
    "game-watch": {
        "numbers": {
            "--interval-s": "bilibili.game_watch.interval_s",
            "--confident-score": "bilibili.game_watch.confident_score",
            "--confident-margin": "bilibili.game_watch.confident_margin",
            "--mem-watermark-mb": "bilibili.game_watch.mem_watermark_mb",
        },
        "switches": {
            "--enabled": ("bilibili.game_watch.enabled", True),
            "--disabled": ("bilibili.game_watch.enabled", False),
        },
    },
    # 画像压缩（你定的: 这两个阈值也要能在界面上改）
    "profile": {
        "numbers": {
            "--trigger-chars": "profile.trigger_chars",
            "--trigger-turns": "profile.trigger_turns",
        },
        "switches": {
            "--enabled": ("profile.enabled", True),
            "--disabled": ("profile.enabled", False),
        },
    },
}

#: cookie 那条不算"设置项"（它写的是凭据文件, 不是 config.yaml）。
COOKIE_KEYS = ("SESSDATA", "bili_jct", "DedeUserID")
COOKIE_FLAGS = {"--sessdata": "SESSDATA", "--bili-jct": "bili_jct",
                "--dede-user-id": "DedeUserID"}


def _mask(value: str) -> str:
    """凭据不原样打印（只给长度与头尾各两位）。"""
    text = str(value or "")
    if len(text) <= 8:
        return "*" * len(text)
    return "%s…%s（%d 位）" % (text[:2], text[-2:], len(text))


def _config_target(args: argparse.Namespace) -> str:
    """`--config` 指向的真源路径（**并拦住"名字不对"的坑**）。

    ⚠ `--config` 给的是**路径**，但加载与文本级写入都按**目录**定位真源
      （`<目录>/config.yaml`）—— 所以传一个叫别的名字的文件（例如 `lean.yaml`）会
      **悄悄写到同目录的 config.yaml 上**。写设置这种事不能"悄悄写错文件",
      这里直接拒绝并说清怎么改（T13-8 本地冒烟踩到的）。
    """
    explicit = getattr(args, "config", None)
    if explicit:
        name = os.path.basename(str(explicit))
        if name != "config.yaml":
            raise ValueError(
                "--config 指向的是 %s，但设置写入按**目录**定位真源（%s）——\n"
                "  改下去的会是那个 config.yaml。把目标文件叫 config.yaml，"
                "或者不带 --config 用默认路径。" % (name, os.path.join(
                    os.path.dirname(os.path.abspath(str(explicit))), "config.yaml")))
    return config_path("config")


def _tier_or_none(path: str) -> Optional[str]:
    """这个键属于哪一级；**不是可设置的标量键**（结构级/自定义子键/不存在）-> None。

    @note None 一律放行给写入器去报它自己那句 —— 不在权限层重复一套错误文案。
    """
    try:
        return config_tiers.tier_of(path)
    except config_tiers.ConfigTierError:
        return None


def _print_tiers() -> int:
    """`assistant set --list-tiers`：两级权限的地图（清单 + 计数）。"""
    data = config_tiers.summary()
    counts = data["counts"]
    print("配置的两级权限（可设置的标量键共 %d 个）:" % counts["settable"])
    print("  **user 级 %d 个** —— 与 GUI 设置页**对等**（界面上能改的，命令行里也能改）:"
          % counts["user"])
    for path in data["user"]:
        print("    %s" % path)
    print("  **root 级 %d 个** —— CLI 独有；在 `assistant shell` 里 `mode root` 之后才能改:"
          % counts["root"])
    for path in data["root"]:
        print("    %s" % path)
    print("  （结构级键与用户自定义子键不在两级里：它们要**手改文件**，"
          "`assistant set` 一律拒绝。）")
    return EXIT_OK


def _refuse_root_in_user_tier(changes: Dict[str, Any], session_tier: str) -> Optional[int]:
    """user 层里想改 root 级的键 -> 打印原话 + 返回退出码；没有要拒的 -> None。

    @note `tier_of()` 读的是**当前配置目录**里的模板，所以调用方必须已经
          `load_plane_config(args.config)`（否则会拿默认目录的模板去判）。
    """
    if session_tier == config_tiers.ROOT_TIER:
        return None
    blocked = [path for path in sorted(changes)
               if _tier_or_none(path) == config_tiers.ROOT_TIER]
    if not blocked:
        return None
    print("改不了：%s 是 **root 级**设置项。\n"
          "  · 进 root 体系：`assistant shell` → 会话里 `mode root` → 再跑这条命令；\n"
          "  · 看两级清单：`assistant set --list-tiers`；\n"
          "  · 退出 root 体系：会话里 `exit`（`mode user` 也能降回）。"
          % "、".join(blocked), file=sys.stderr)
    return EXIT_USAGE


def _show_group(group: str, target: Any, session_tier: str) -> int:
    """`assistant set <组> --show`：按 **user / root 两段**列出当前值。"""
    from agent.core import settings_config as sc

    paths = list(SET_GROUPS[group]["numbers"].values()) \
        + [item[0] for item in SET_GROUPS[group]["switches"].values()]
    user_paths, root_paths, unknown = [], [], []
    for path in sorted(set(paths)):
        tier = _tier_or_none(path)
        if tier == config_tiers.USER_TIER:
            user_paths.append(path)
        elif tier == config_tiers.ROOT_TIER:
            root_paths.append(path)
        else:
            unknown.append(path)

    def _row(tag: str, path: str) -> None:
        print("  %-8s %-32s %s" % (tag, path,
                                   sc.read_value(path, target=str(target)) or "（文件里没有）"))

    print("%s 组能改的设置项（当前值 ← 点号路径）；当前权限层：**%s**" % (group, session_tier))
    for path in user_paths:
        _row("[user]", path)
    if root_paths:
        print("  ---- root 级（要 `assistant shell` → `mode root` 才能改）----")
        for path in root_paths:
            _row("[root]", path)
    for path in unknown:
        _row("[  ?  ]", path)
    return EXIT_OK


async def cmd_set(args: argparse.Namespace) -> int:
    """改设置项（**文本级**，默认只看；`--apply` 才写）。

    写的是 `config/config.yaml` 的**那一行**（注释/顺序/换行都不动, 并在旁边留一份
    `.bak`）—— 与 GUI 的 ConfigStore 同一套语义, 实现见 `agent/core/settings_config.py`。

    @note 能改的键 = `config.example.yaml` 里有的键; 模板里没有这一**段**时,
          按模板那一整段（含说明注释）新建 —— 板端真配置就是这样长出 `study:` 段的。
    @note `cookie` 那一组写的是 `config/bilibili_cookie.json`（凭据文件, 不是配置真源）,
          打印时**只给掩码**, 绝不回显 SESSDATA 原文。
    """
    from agent.core import settings_config as sc

    if getattr(args, "list_tiers", False):
        return _print_tiers()

    group = args.group
    if not group:
        print("要给一个组（%s / cookie）；只想看权限地图用 `assistant set --list-tiers`。"
              % " / ".join(sorted(SET_GROUPS)), file=sys.stderr)
        return EXIT_USAGE
    if group == "cookie":
        return await _set_cookie(args)
    if group not in SET_GROUPS:
        print("不认识的组 %r（有 %s / cookie）" % (group, " / ".join(SET_GROUPS)),
              file=sys.stderr)
        return EXIT_USAGE

    table = SET_GROUPS[group]
    changes: Dict[str, Any] = {}
    for flag, path in table["numbers"].items():
        value = getattr(args, flag.lstrip("-").replace("-", "_"), None)
        if value is not None:
            changes[path] = value
    for flag, (path, value) in table["switches"].items():
        if getattr(args, flag.lstrip("-").replace("-", "_"), False):
            changes[path] = value
    if args.set:
        for item in args.set:
            if "=" not in item:
                print("--set 要写成 键=值（例如 --set study.relative_band=0.05）", file=sys.stderr)
                return EXIT_USAGE
            key, value = item.split("=", 1)
            changes[key.strip()] = value.strip()

    # ⚠ 先把 `--config` 交给 load_plane_config（它会设 AGENT_CONFIG_DIR）——
    #   否则 `config_path()` 拿到的是**默认**配置目录, 测试/多份配置就会写错文件。
    #   （T15-4 顺手去掉这里原来**重复的一次调用**：那个 `target = config_path("config")`
    #     的值随后就被这一行覆盖了。行为不变。）
    config, why = load_plane_config(args.config)
    if why:
        print("读不到配置（%s）—— 但写的是文件本身, 继续。" % why, file=sys.stderr)
    try:
        target = _config_target(args)
    except ValueError as exc:
        print("不写：%s" % exc, file=sys.stderr)
        return EXIT_USAGE

    if not changes and not args.show:
        print("没给任何设置项。看这一组能改什么: `assistant set %s --show`" % group)
        return EXIT_OK

    # ---- 权限层（T15-4）：root 级的键只在 `assistant shell` 里 `mode root` 之后才让改 ----
    # ⚠ 放在这里（而不是 `cmd_set` 开头）是因为 `tier_of()` 读的是**当前配置目录**里的模板，
    #   而配置目录由上面的 `load_plane_config(args.config)` 定下来。
    session_tier = str(getattr(args, "tier", config_tiers.USER_TIER))
    refused = _refuse_root_in_user_tier(changes, session_tier)
    if refused is not None:
        return refused

    if args.show or not changes:
        return _show_group(group, target, session_tier)

    # ⚠ 写配置一律走 `config_tiers` 那两个**带权限闸门**的入口（裸的 `settings_config.*`
    #   只允许 `config_tiers` 自己调 —— 有守卫盯着，见 tests/test_config_tiers.py）。
    allow_root = session_tier == config_tiers.ROOT_TIER
    try:
        plans = config_tiers.plan_changes_for_tier(changes, target=str(target), allow_root=allow_root)
    except (sc.SettingsConfigError, config_tiers.ConfigTierError) as exc:
        print("改不了：%s" % exc, file=sys.stderr)
        return EXIT_ERROR

    print("要改的设置（%s）:" % target)
    for plan in plans:
        action = {"set": "改", "add-key": "加键", "add-segment": "**新建整段**"}[plan["action"]]
        print("  %-6s %-38s %s -> %s" % (action, plan["path"], plan["old"], plan["new"]))
    if not args.apply:
        print("（还没写。加 --apply 才真写；会在旁边留一份 %s.bak）" % target.name)
        return EXIT_OK

    try:
        result = config_tiers.apply_changes_for_tier(changes, target=str(target), allow_root=allow_root)
    except (sc.SettingsConfigError, config_tiers.ConfigTierError) as exc:
        print("写不进去：%s" % exc, file=sys.stderr)
        return EXIT_ERROR
    if not result["changed"]:
        print("值本来就是这些，一个字节都没动（也没留 .bak）。")
        return EXIT_OK
    print("已写 %d 项 -> %s（备份 %s）" % (result["changed"], result["path"],
                                        os.path.basename(result["backup"])))
    return EXIT_OK


async def _set_cookie(args: argparse.Namespace) -> int:
    """B 站 cookie: 写 `config/bilibili_cookie.json`（凭据文件）+ 可选 `--verify`。"""
    from agent.core import settings_credentials as creds

    config, why = load_plane_config(args.config)
    if why:
        print("读不到配置（%s）—— cookie 文件路径取默认值。" % why, file=sys.stderr)
    path = creds.cookie_path(config)

    values = {}
    for flag, key in COOKIE_FLAGS.items():
        value = getattr(args, flag.lstrip("-").replace("-", "_"), None)
        if value:
            values[key] = value.strip()

    if not values and not args.verify:
        current = creds.read_cookie(config)
        if not current:
            print("现在没有 cookie（%s 不在或为空 = 匿名, 清晰度按 B 站给多少算多少）。"
                  % path)
        else:
            print("现在有一份 cookie（%s）:" % path)
            for key in COOKIE_KEYS:
                if current.get(key):
                    print("  %-12s %s" % (key, _mask(current[key])))
        print("\n写一份: assistant set cookie --sessdata <值> [--bili-jct <值>] "
              "[--dede-user-id <值>] --apply")
        return EXIT_OK

    if values:
        print("要写进 %s 的字段:" % path)
        for key, value in values.items():
            print("  %-12s %s" % (key, _mask(value)))
        if not args.apply:
            print("（还没写。加 --apply 才真写；会在旁边留一份 .bak）")
            return EXIT_OK
        try:
            saved = creds.write_cookie(config, values)
        except creds.CredentialsError as exc:
            print("写不进去：%s" % exc, file=sys.stderr)
            return EXIT_ERROR
        print("已写 %s（备份 .bak；这个文件是**凭据**, 已进 .gitignore）" % saved)

    if args.verify:
        return _verify_cookie(config)
    return EXIT_OK


def _verify_cookie(config: Dict[str, Any]) -> int:
    """问一次 B 站: 这份 cookie 到底好不好使（复用 Agent 起播前那条同一个实现）。"""
    from agent.net.bilibili_api import BilibiliApi

    section = (config or {}).get("bilibili") or {}
    api = BilibiliApi.from_config(section, log=logging.getLogger("agent.cli"))
    if api is None:
        print("bilibili 没开（config 的 bilibili.enabled）—— 没什么可验的。")
        return EXIT_OK
    if not api.cookie_present:
        print("没有 cookie（匿名）—— 单文件清晰度按 B 站给多少算多少。")
        return EXIT_OK
    try:
        info = api.login(refresh=True)
    except Exception as exc:                              # noqa: BLE001 - 问不到要如实说
        print("问不到 B 站（%s）—— cookie 好不好使这次没验出来。" % exc, file=sys.stderr)
        return EXIT_ERROR
    if info.get("logged_in"):
        who = info.get("uname") or info.get("mid") or "（已登录）"
        print("cookie 有效：%s —— DASH 能到 1080P。" % who)
        return EXIT_OK
    print("cookie **不好使**：%s（B 站不报错, 只是把你当匿名, 清晰度会掉）"
          % (info.get("why") or "未登录"))
    return EXIT_ERROR


def _study_plane(args: argparse.Namespace):
    """读锚点库 + 统计 + 判定器（**不起 Agent**）—— `assistant study` 的公共入口。"""
    from agent.core.study_anchors import StudyAnchors
    from agent.core.study_stats import StudyStats
    from agent.core.study_watch import DEFAULT_EWMA_ALPHA, DEFAULT_STEP, StudyWatcher

    config, why = load_plane_config(args.config)
    if why:
        print("读不到配置（%s）—— 用默认路径。" % why, file=sys.stderr)
    section = dict((config or {}).get("study") or {})
    anchors = StudyAnchors(section.get("anchor_file"), classes=section.get("classes"),
                           max_per_class=int(section.get("max_anchors_per_class", 200) or 200),
                           keep_shots=False, log=logging.getLogger("agent.cli"))
    try:
        anchors.load()
    except Exception as exc:                              # noqa: BLE001 - 库坏了要如实说
        print("锚点库读不出来：%s" % exc, file=sys.stderr)
    stats = StudyStats(section.get("stats_file"), log=logging.getLogger("agent.cli"))
    stats.load()
    watcher = StudyWatcher(
        anchors, stats=stats,
        relative_band=float(section.get("relative_band", 0.05) or 0.05),
        learn_score=float(section.get("learn_score", 0.90) or 0.90),
        focus_interval_min=float(section.get("focus_interval_min", 30) or 30),
        recheck_interval_min=float(section.get("recheck_interval_min", 5) or 5),
        max_failures=int(section.get("max_failures", 3) or 3),
        cooldown_min=float(section.get("cooldown_min", 30) or 30),
        adapt=bool(section.get("adapt", True)), learn=bool(section.get("learn", True)),
        # T15-4: 自适应阈值怎么挪（模板里那两个 root 级键；常量仍是默认值）
        step=float(section.get("adapt_step", DEFAULT_STEP) or DEFAULT_STEP),
        ewma_alpha=float(section.get("ewma_alpha", DEFAULT_EWMA_ALPHA) or DEFAULT_EWMA_ALPHA))
    watcher.seed_thresholds()
    return config, section, anchors, stats, watcher


async def cmd_study(args: argparse.Namespace) -> int:
    """学习内容监督的日常操作: `status` / `check` / `label` / `freeze` / `unfreeze` / `reset`。

    @note **不用起 Agent**: `status` 直接读两份派生数据; `check`/`label` 在板端加载
      SigLIP 判一张**截图**（走 `frame_pipeline` 还原成板子看到的那一帧）。
      "看真串流帧"是 Agent 自己的循环（`_study_watch_loop`）。
    @note `freeze`/`unfreeze` 改的是配置里的 `study.adapt`（走 `assistant set` 那条
      同一个文本级写入器）; `reset` 改的是派生数据（锚点/统计）。
    """
    action = args.action
    if action in ("freeze", "unfreeze"):
        # 这两个是**直接动作**（不是"看设置项"），所以马上就写；写的是配置真源里那一行。
        from agent.core import settings_config as sc

        load_plane_config(args.config)                    # 先让 --config 生效（设 AGENT_CONFIG_DIR）
        try:
            target = _config_target(args)
        except ValueError as exc:
            print("不写：%s" % exc, file=sys.stderr)
            return EXIT_USAGE
        wanted = action == "unfreeze"
        try:
            result = sc.apply_changes({"study.adapt": wanted}, target=str(target))
        except sc.SettingsConfigError as exc:
            print("改不了：%s" % exc, file=sys.stderr)
            return EXIT_ERROR
        if not result["changed"]:
            print("study.adapt 本来就是 %s，一个字节都没动。" % ("true" if wanted else "false"))
            return EXIT_OK
        print("已把 study.adapt 设成 %s -> %s（备份 %s；判定照旧, 只是%s自己挪阈值）"
              % ("true" if wanted else "false", result["path"],
                 os.path.basename(result["backup"]), "" if wanted else "不再"))
        return EXIT_OK

    config, section, anchors, stats, watcher = _study_plane(args)
    if action == "status":
        return _study_status(args, section, anchors, stats, watcher)
    if action == "reset":
        return _study_reset(args, anchors, stats, watcher)
    if action in ("check", "label"):
        return _study_check(args, action, watcher)
    print("不认识的动作 %r" % action, file=sys.stderr)
    return EXIT_USAGE


def _study_status(args, section, anchors, stats, watcher) -> int:
    """一眼看清: 库里有什么、阈值多少、最近判成什么。"""
    import json as _json

    snap = watcher.snapshot()
    if args.json:
        print(_json.dumps({"snapshot": snap, "stats": stats.snapshot()},
                          ensure_ascii=False, indent=2))
        return EXIT_OK
    print("学习内容监督: %s" % ("开（config 的 study.enabled=true）" if section.get("enabled")
                              else "**关**（config 里 study.enabled=false）"))
    print("锚点库: %d 条 %s" % (len(anchors), snap["counts"] or "{}"))
    print("  大类: %s" % snap["categories"])
    print("判定阈值: 没把握带 %.3f（界 %s）" % (snap["thresholds"]["relative_band"],
                                              snap["thresholds"]["band_bounds"]))
    print("  标定记录（**不参与判定**）: confident_score %.2f / confident_margin %.2f"
          % (snap["thresholds"]["confident_score"], snap["thresholds"]["confident_margin"]))
    print("时间参数: 学习后 %g 分钟 / 复查 %g 分钟 / %d 次不通过 / 冷却 %g 分钟"
          % (snap["intervals_min"]["focus"], snap["intervals_min"]["recheck"],
             snap["max_failures"], snap["intervals_min"]["cooldown"]))
    print("自适应: %s；自学习: %s；停学的类: %s"
          % ("开" if snap["adapt"] else "**冻结**", "开" if snap["learn"] else "关",
             snap["paused"] or "无"))
    if snap["last"]:
        last = snap["last"]
        print("最近一次: %s（来源 %s，相对分 %s）%s"
              % (last.get("verdict"), last.get("source") or "-", last.get("relative"),
                 (last.get("note") or "")[:60]))
    counters = stats.counters()
    if counters:
        print("统计: " + "、".join("%s=%s" % (key, value)
                                  for key, value in sorted(counters.items())))
    return EXIT_OK


def _study_reset(args, anchors, stats, watcher) -> int:
    """清掉锚点（可选一类）与/或学到的阈值 —— 默认只看。"""
    cls = args.klass
    target = "全部" if cls is None else "「%s」这一类" % cls
    print("要清掉: %s的锚点%s" % (target, "，并把阈值/EWMA 回到配置初值" if args.thresholds else ""))
    if not args.apply:
        print("（还没动。加 --apply 才真清；锚点文件会留一份 .bak）")
        return EXIT_OK
    try:
        removed = anchors.reset(cls)
    except Exception as exc:                              # noqa: BLE001
        print("清不掉：%s" % exc, file=sys.stderr)
        return EXIT_ERROR
    print("清掉 %d 条锚点；现在 %d 条 %s" % (removed, len(anchors), anchors.counts()))
    if args.thresholds:
        stats.reset(keep_thresholds=False)
        watcher.seed_thresholds()
        stats.note("CLI `assistant study reset --thresholds`: 阈值与 EWMA 清回配置初值")
        stats.save()
        print("阈值回到配置初值（没把握带 %.3f）" % watcher.relative_band)
    return EXIT_OK


def _study_check(args, action, watcher) -> int:
    """判一张**截图**（`--image`），可选把它学成锚点（`label --class X --apply`）。"""
    import json as _json

    from agent.vision import frame_pipeline as fp

    if not args.image:
        print("%s 需要 --image <截图路径>（板子看到的是串流帧, 这里用截图按真管线还原）"
              % action, file=sys.stderr)
        return EXIT_USAGE
    stream = (1280, 720)
    if args.stream:
        try:
            parts = str(args.stream).lower().split("x")
            stream = (int(parts[0]), int(parts[1]))
        except (ValueError, IndexError):
            print("--stream 要写成 1280x720", file=sys.stderr)
            return EXIT_USAGE
    try:
        frame = fp.from_screenshot(args.image, stream=stream, mode=args.mode)
    except fp.FramePipelineError as exc:
        print("这一张读不出来：%s" % exc, file=sys.stderr)
        return EXIT_ERROR

    watcher._encoder = None                              # 用真模型（延迟到编码那一步才加载）
    try:
        from agent.vision.siglip import SiglipModel
    except ImportError as exc:
        print("判这一张要板端的 SigLIP（%s）—— 开发机上跑不了。" % exc, file=sys.stderr)
        return EXIT_ERROR
    config, _why = load_plane_config(args.config)
    model = SiglipModel.from_config((config or {}).get("vision"))
    try:
        vector = [float(item) for item in list(model.encode_image(frame))]
    except Exception as exc:                              # noqa: BLE001 - 缺 NPU 要如实说
        print("编码失败（%s）—— 板端才有 NPU。" % exc, file=sys.stderr)
        return EXIT_ERROR
    outcome = watcher.verdict(frame, vector=vector)
    if args.json:
        print(_json.dumps(outcome, ensure_ascii=False, indent=2))
    else:
        print("%s -> %s（来源 %s）" % (args.image, outcome["verdict"], outcome["source"] or "-"))
        print("  相对分 %+.4f（study %+.4f / not_study %+.4f，带 %.3f）"
              % (outcome["relative"] or 0.0, outcome["study_cos"] or 0.0,
                 outcome["not_study_cos"] or 0.0, outcome["band"]))
        print("  最像的子标签: %s；%s" % (outcome["cls"] or "?", outcome["note"]))
    if action == "check":
        return EXIT_OK
    if not args.klass:
        print("label 要给 --class（%s）" % " / ".join(sorted(watcher.anchors.classes)),
              file=sys.stderr)
        return EXIT_USAGE
    if not args.apply:
        print("（还没学。加 --apply 才把这一帧学成「%s」的锚点）" % args.klass)
        return EXIT_OK
    result = watcher.label(args.klass, vector=vector)
    if result["learned"]:
        print("已学成「%s」的锚点（现在 %d 条）" % (args.klass, result["anchors"]))
        return EXIT_OK
    print("没学进去：%s" % result["note"], file=sys.stderr)
    return EXIT_ERROR


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


def _add_set_parser(sub: Any) -> None:
    """`assistant set` 那一大坨开关（表驱动：`SET_GROUPS` 加一行 = 多一个开关）。

    @note 从 `build_parser()` 里抽出来（T15-4 任务 2）: 那一坨 45 行把 `build_parser`
          顶过了"超长函数 120 行"的线, 抽出来两边都清楚。
    """
    p_set = sub.add_parser(
        "set",
        help="改设置项（文本级: 只动那一行 + .bak；默认只看, --apply 才写）",
        parents=[_common_options(suppress_defaults=True, with_timeout=False)])
    p_set.add_argument("group", nargs="?", choices=sorted(SET_GROUPS) + ["cookie"],
                       help="哪一组设置: %s / cookie（B 站凭据）；配 --list-tiers 时可以不给"
                            % " / ".join(sorted(SET_GROUPS)))
    p_set.add_argument("--apply", action="store_true",
                       help="真写（config.yaml 旁边留 .bak；cookie 写 config/bilibili_cookie.json）")
    p_set.add_argument("--show", action="store_true",
                       help="只列出这一组能改的设置项与当前值（按 user / root 两级分段）")
    p_set.add_argument("--list-tiers", action="store_true",
                       help="列出配置的两级权限（user = 与 GUI 设置页对等；root = CLI 独有）")
    p_set.add_argument("--set", action="append", metavar="键=值",
                       help="按点号路径直接设（可以给多次；键必须是模板里有的）")
    # 数值项与开关（表驱动：加一行 = 多一个开关）。
    # ⚠ 同一个开关可能出现在**多组**里（`--enabled` 三组都有）—— argparse 不许重复注册,
    #   所以这里去重, 帮助文本里把"它属于哪几组"写全（处理时仍按**当前组**的表取值）。
    numbers: Dict[str, List[str]] = {}
    switches: Dict[str, List[str]] = {}
    for group, table in SET_GROUPS.items():
        for flag, path in table["numbers"].items():
            numbers.setdefault(flag, []).append("%s -> %s" % (group, path))
        for flag, (path, _value) in table["switches"].items():
            switches.setdefault(flag, []).append("%s -> %s" % (group, path))
    for flag, notes in numbers.items():
        p_set.add_argument(flag, type=float, default=None, help="；".join(notes))
    for flag, notes in switches.items():
        p_set.add_argument(flag, action="store_true", help="；".join(notes))
    # cookie 那一组
    p_set.add_argument("--sessdata", default=None, help="[cookie] B 站 SESSDATA（凭据, 不会回显）")
    p_set.add_argument("--bili-jct", default=None, help="[cookie] B 站 bili_jct（可选）")
    p_set.add_argument("--dede-user-id", default=None, help="[cookie] B 站 DedeUserID（可选）")
    p_set.add_argument("--verify", action="store_true",
                       help="[cookie] 顺手问一次 B 站, 看这份 cookie 好不好使")
    p_set.set_defaults(func=cmd_set)


def _add_study_parser(sub: Any) -> None:
    """`assistant study` 那一组（同样从 `build_parser()` 里抽出来，见 `_add_set_parser`）。"""
    p_study = sub.add_parser(
        "study",
        help="学习内容监督: status / check / label / freeze / unfreeze / reset（不用起 Agent）",
        parents=[_common_options(suppress_defaults=True, with_timeout=False)])
    p_study.add_argument("action",
                         choices=["status", "check", "label", "freeze", "unfreeze", "reset"])
    p_study.add_argument("--image", default=None, help="check/label: 一张截图（按真管线还原）")
    p_study.add_argument("--mode", default="native", choices=["native", "area", "direct"],
                         help="check/label: 用哪条还原管线（默认 native = 板子真走的那条）")
    p_study.add_argument("--stream", default=None, help="check/label: 流分辨率, 默认 1280x720")
    p_study.add_argument("--class", dest="klass", default=None,
                         help="label/reset: 子标签（code/doc/real/anime/game）")
    p_study.add_argument("--thresholds", action="store_true",
                         help="reset: 连阈值与 EWMA 一起清回配置初值")
    p_study.add_argument("--apply", action="store_true", help="label/reset: 真写（默认只看）")
    p_study.add_argument("--json", action="store_true", help="status/check: 输出 JSON")
    p_study.set_defaults(func=cmd_study)


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

    p_music = sub.add_parser(
        "music",
        help="音乐传输控制：%s（成功看 music 推送、失败看 Agent 的原话）"
             % "/".join(MUSIC_ACTIONS),
        parents=[_common_options(suppress_defaults=True)])
    p_music.add_argument("action", choices=list(MUSIC_ACTIONS),
                         help="play = 播放（已经在放就不发）；pause = 暂停（已经暂停就不发）；"
                              "toggle = 直接切一下；next / prev = 上一首下一首")
    p_music.add_argument("--no-wait", action="store_true",
                         help="发出去就返回，不等 music/llm 回执")
    p_music.set_defaults(func=cmd_music)

    p_video = sub.add_parser(
        "video",
        help="视频传输控制：%s（成功看 Agent 的 bilibili 回执、失败看它的原话）"
             % "/".join(VIDEO_ACTIONS),
        parents=[_common_options(suppress_defaults=True)])
    p_video.add_argument("action", choices=list(VIDEO_ACTIONS),
                         help="play = 播放（已经在放就不发）；pause = 暂停（已经暂停就不发）；"
                              "toggle = 直接切一下；next / prev = 队列里上一集下一集"
                              "（队列内容由对话/画面决定，这里只做传输控制）")
    p_video.add_argument("--no-wait", action="store_true",
                         help="发出去就返回，不等回执")
    p_video.add_argument("--wait-player", action="store_true",
                         help="再等一步「播放器真的按了」：等 GUI 那边回报的 control_result"
                              "（默认只确认到「Agent 收下并推给播放器了」—— 协议没有请求 id，"
                              "Agent→GUI 是单向的）")
    p_video.set_defaults(func=cmd_video)

    p_watch = sub.add_parser("watch", help="持续打印 Agent 的推送（Ctrl-C 退出；无 --timeout）",                             parents=[_common_options(suppress_defaults=True,
                                                      with_timeout=False)])
    p_watch.add_argument("--topics", help="只看这些 topic（逗号分隔；默认全看）")
    p_watch.add_argument("--count", type=int, default=0,
                         help="收够 N 条就退出（0 = 不限；给脚本/测试用）")
    p_watch.set_defaults(func=cmd_watch)

    p_schedule = sub.add_parser("schedule",
                                help="列出接下来 N 小时（默认 24）的日程，标出真的触发过的",
                                parents=[_common_options(suppress_defaults=True)])
    p_schedule.add_argument("--hours", type=positive_hours, default=WINDOW_HOURS_DEFAULT,
                            # ⚠ 这串里 %(default)g 是 argparse 的**映射**占位符, 不能再混
                            #   位置式的 %d（混了会 TypeError: format requires a mapping）
                            help="窗口长度（小时，必须是正数；默认 %(default)g）。"
                                 "窗口里还带最近 " + str(TAIL_MINUTES) + " 分钟刚过去的那些")
    p_schedule.add_argument("--limit", type=int, default=10,
                            help="整个窗口最多列几行（0 = 不限；默认 %(default)s）")
    p_schedule.add_argument("--no-ask", action="store_true",
                            help="不问 Agent，只按时间比较（离线/对比用）")
    p_schedule.set_defaults(func=cmd_schedule)

    p_doctor = sub.add_parser("doctor", help="体检：配置 / socket / 派生 / 日程 / 关键路径",
                              parents=[_common_options(suppress_defaults=True)])
    p_doctor.set_defaults(func=cmd_doctor)

    p_cleanup = sub.add_parser(
        "cleanup",
        help="清理已经过去的一次性日程（默认只看；--apply 才真删）",
        parents=[_common_options(suppress_defaults=True, with_timeout=False)])
    p_cleanup.add_argument("--apply", action="store_true",
                           help="真的删（文本级 + 原子写，并在原文件旁留一份 .bak）")
    p_cleanup.set_defaults(func=cmd_cleanup)

    p_tag = sub.add_parser(
        "tag",
        help="给壁纸打标签（SigLIP 零样本，写 config/wall_data.jsonl；默认只看）",
        parents=[_common_options(suppress_defaults=True, with_timeout=False)])
    p_tag.add_argument("--apply", action="store_true",
                       help="真打（逐张写盘，并在数据文件旁留一份 .bak）")
    p_tag.add_argument("--force", action="store_true", help="全部重打（不看指纹）")
    p_tag.add_argument("--limit", type=int, default=None, help="本轮最多打几张（分批用）")
    p_tag.add_argument("--dir", default=None, help="覆盖壁纸目录（默认取配置 wallpaper.dir）")
    p_tag.add_argument("--data-file", default=None,
                       help="覆盖数据文件（默认 config/wall_data.jsonl）")
    p_tag.add_argument("--top-k", type=int, default=None, help="每轴存几条候选（默认 3）")
    p_tag.add_argument("--prune", action="store_true",
                       help="顺手清掉「图已经不在了」的行（与 --apply 一起才真写）")
    p_tag.set_defaults(func=cmd_tag)

    _add_set_parser(sub)

    _add_study_parser(sub)

    p_shell = sub.add_parser(
        "shell",
        help="交互式会话: 一条条跑命令；会话里 `mode root` / `mode user` 切权限层, `exit` 退出")
    p_shell.add_argument("-c", "--command", dest="shell_command", action="append",
                         default=None, metavar="命令",
                         help="不交互: 跑这些命令（可给多次；每一条里还能用 `;` 分成多条，"
                              "例如 -c \"mode root; set study --adapt --apply\"）")

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

    # ⚠ `shell` 走**同步早分支**: 会话要一条一条地跑命令, 而每条命令自己就是
    #   `asyncio.run(...)`（与一次性调用逐字相同）—— `asyncio.run` 不能嵌套, 所以
    #   不能让它落进下面那个 `asyncio.run(args.func(args))` 里。
    #   （`shell` 自己不带 `func`；`-c` 的值存在 `shell_command` 里, 不与子命令名 `command` 抢。）
    if args.command == "shell":
        from agent import shell

        return shell.main(getattr(args, "shell_command", None))

    try:
        return asyncio.run(args.func(args))
    except KeyboardInterrupt:
        print()                      # Ctrl-C：干净退出（watch 那条命令会用到）
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
