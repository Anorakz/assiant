#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent/cli.py — 板端控制 CLI（同一个 Agent 的第二条前端）

为什么有它
    GUI 是板端那块屏的前端；CLI 是给 **ssh / 脚本** 用的那一半：看一眼状态、
    发一条消息、切模式、盯推送、查日程、做体检。板端还放了个 `assistant` 启动器。

走哪条路（这里决定了它能做什么、不能做什么）
    · 只走**现有 IPC 协议**（`docs/ipc-protocol.md` 是线上格式的唯一真源），
      用现成的 `agent.ipc.local_client.LocalClient`；不新增线格式。
    · **不 import Agent 去读它的内存** —— 所以 Agent 没跑时这里会明确说"连不上"，
      而不是给一份假状态。
    · 后果：看到不"Agent 已经触发过哪条日程"（那是运行中进程的内存状态）。
      日程展开用**真的** `agent.core.scheduler` 语义，但只标"**已过（按时间）**"；
      要真的触发记录，得先扩协议（本次不做，见 docs/cli.md 的边界）。

退出码
    0 = 成功 ／ 1 = 环境或连接问题 ／ 2 = 参数错（argparse 的默认行为）
"""

import argparse
import asyncio
import contextlib
import json
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from agent.config import ConfigError, ConfigNotFoundError, load_config
from agent.ipc.local_client import IpcClientError, LocalClient
from agent.ipc.protocol import (
    COMMAND_CHAT_INPUT,
    COMMAND_SWITCH_MODE,
    MODES,
    SOCKET_PATH,
    TOPIC_LLM,
    TOPIC_STATUS,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

#: 等一条推送的默认超时（秒）。Agent 只在**状态变化**时推 status，
#: 所以 status 命令必须能"等不到也不报错"。
DEFAULT_TIMEOUT = 3.0

#: watch 打印一条推送时，单个值最长多少字符（超出截断并标总长）
WATCH_VALUE_LIMIT = 120


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
    """读配置（这里只为了拿 socket 路径等）。返回 `(config, 失败原因或 None)`。

    与 `agent/main.py --config` 同一条路：设置 `AGENT_CONFIG_DIR` 后按名字加载。
    读不到**不是**致命错误（socket 有默认值），调用方给一句提示就行。
    """
    if explicit_path:
        os.environ.setdefault("AGENT_CONFIG_DIR", str(Path(explicit_path).resolve().parent))
    try:
        return load_config("config"), None
    except (ConfigError, ConfigNotFoundError) as exc:
        return {}, str(exc)
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
    """一条推送渲染成一行：`23:51:02  status  mode=STUDY connected=true`。"""
    stamp = (clock or datetime.now()).strftime("%H:%M:%S")
    pairs = " ".join("%s=%s" % (key, format_value(value))
                     for key, value in (data or {}).items())
    return "%s  %-9s %s" % (stamp, topic, pairs)


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


# ---------------------------------------------------------------------------
#  命令行
# ---------------------------------------------------------------------------
def _common_options(*, suppress_defaults: bool) -> argparse.ArgumentParser:
    """主命令与每个子命令共用的三个选项，做成"父解析器"（add_help=False）。

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
                        help="配置文件路径（默认 config/config.yaml）")
    parent.add_argument("--timeout", type=float,
                        default=argparse.SUPPRESS if suppress_defaults else DEFAULT_TIMEOUT,
                        help="等一条推送的超时秒数（默认 %s）" % DEFAULT_TIMEOUT)
    return parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="assistant",
        description="RK3568 桌面助手 —— 板端控制 CLI（走现有 IPC 协议，不新增线格式）",
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

    p_watch = sub.add_parser("watch", help="持续打印 Agent 的推送（Ctrl-C 退出）",
                             parents=[_common_options(suppress_defaults=True)])
    p_watch.add_argument("--topics", help="只看这些 topic（逗号分隔；默认全看）")
    p_watch.add_argument("--count", type=int, default=0,
                         help="收够 N 条就退出（0 = 不限；给脚本/测试用）")
    p_watch.set_defaults(func=cmd_watch)

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
