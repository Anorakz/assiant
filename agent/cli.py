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
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from agent.config import ConfigError, ConfigNotFoundError, load_config
from agent.ipc.local_client import IpcClientError, LocalClient
from agent.ipc.protocol import SOCKET_PATH, TOPIC_STATUS

EXIT_OK = 0
EXIT_ERROR = 1

#: 等一条推送的默认超时（秒）。Agent 只在**状态变化**时推 status，
#: 所以 status 命令必须能"等不到也不报错"。
DEFAULT_TIMEOUT = 3.0


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
            print("想看后续变化，就持续订阅推送。")
            return EXIT_OK
        print(_status_line(seen))
        return EXIT_OK
    finally:
        await client.close()


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

    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(args.func(args))
    except KeyboardInterrupt:
        print()                      # Ctrl-C：干净退出（watch 那条命令会用到）
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
