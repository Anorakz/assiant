#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
#  gui/tools/fake_agent.py — 人工验收用的"假 Agent"（合并版）
#
#  用途：在没有真 Agent 的时候端到端验 GUI 的 IPC 客户端
#  （gui/src/services/local_client.*）。两个角色合一：
#
#      · **推**（默认）: 连上后按脚本推一组消息，验证 GUI 的**收**
#      · 加 `--echo`   : 进入长驻模式，收 GUI 的命令并打印，验证 GUI 的**发**
#                        与断线重连（可 kill 后重启）
#
#  编码全部走 agent/ipc/protocol.py —— 与真 Agent 用同一套实现，不会出现
#  "两边各自照文档手写导致字段名不一致"。收命令用 protocol.decode_command()，
#  与真 Agent 收命令是同一份解析。
#
#  用法
#  ---------------------------------------------------------------------------
#      # 推脚本化的边界用例（4 个 topic + 拆包 + 4 种坏消息 + 恢复）
#      python3 gui/tools/fake_agent.py [--path /tmp/agent.sock]
#
#      # 只推某几条
#      python3 gui/tools/fake_agent.py --push status,music --hold 10
#
#      # 收命令（打印 action/payload）并回推一条 llm
#      python3 gui/tools/fake_agent.py --echo
#
#  与 gui/tests/local_server.py 的分工
#  ---------------------------------------------------------------------------
#      本脚本    人工验收用, 输出给人看
#      那个       QTest 的真对端, 被 test_local_client 用 QProcess 拉起,
#                 输出格式是测试的同步协议 (LISTENING/CONNECTED/RECV), 不要改
# ============================================================================
from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from pathlib import Path
from typing import List, Tuple

# 让脚本在任意 cwd 下都能 import agent.ipc
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.ipc import protocol  # noqa: E402

#: (给操作者看的说明, 真正要 send 的字节)。说明只打印在本地, 不上线 ——
#: 线上一条非 JSON 的注释都会变成客户端那边的"丢弃非法 JSON 行"warning。
Step = Tuple[str, bytes]

#: push 之后停一下: 让客户端多触发几次 readyRead (更接近真实节奏)
DEFAULT_INTERVAL_S = 0.05

#: 一段"GAME"状态, 专供拆包用例(同一条消息分两次 send)
_GAME_STATUS = protocol.encode(protocol.TOPIC_STATUS,
                               {"mode": protocol.MODE_GAME, "connected": False})


# ---------------------------------------------------------------------------
#  推送序列
# ---------------------------------------------------------------------------
def _split_pair(whole: bytes) -> List[Step]:
    """把一条消息拆成"前半截(无换行)"+"后半截(带换行)"两步。"""
    cut = len(whole) // 2
    return [
        ("拆包: 前半截 (无换行)", whole[:cut]),
        ("拆包: 后半截 + 换行", whole[cut:]),
    ]


def build_messages(tokens: List[str]) -> List[Step]:
    """把 token 列表展开成要按顺序 send 的步骤。"""
    steps: List[Step] = []
    for token in tokens:
        if token == "scripted":
            steps.extend(_scripted())
        elif token == "status":
            steps.append(("status / STUDY",
                          protocol.encode(protocol.TOPIC_STATUS,
                                          {"mode": protocol.MODE_STUDY,
                                           "connected": True})))
        elif token == "llm":
            steps.append(("llm / 中文",
                          protocol.encode(protocol.TOPIC_LLM,
                                          {"text": "已经切换到学习模式。"})))
        elif token == "wallpaper":
            steps.append(("wallpaper / 路径+序号",
                          protocol.encode(protocol.TOPIC_WALLPAPER,
                                          {"path": "/home/kickpi/wallpapers/04.jpg",
                                           "index": 3})))
        elif token == "music":
            steps.append(("music / 中文标题",
                          protocol.encode(protocol.TOPIC_MUSIC,
                                          {"title": "夜曲", "playing": True})))
        elif token == "split":
            steps.extend(_split_pair(_GAME_STATUS))
        elif token == "badjson":
            steps.append(("坏消息: 非法 JSON", b"{this is not json}\n"))
        elif token == "nonobject":
            steps.append(("坏消息: 是 JSON 但不是 object", b"[1,2,3]\n"))
        elif token == "data-scalar":
            steps.append(("坏消息: data 不是 object",
                          b'{"topic":"status","data":123,"timestamp":1.0}\n'))
        elif token == "no-ts":
            steps.append(("坏消息: 缺 timestamp",
                          b'{"topic":"status","data":{}}\n'))
        elif token == "unknown":
            steps.append(("未知 topic (期望被静默忽略)",
                          protocol.encode("something_new", {"x": 1})))
        else:
            raise SystemExit(
                "fake_agent.py: 不认识的 --push token: %r\n"
                "  可用: scripted status llm wallpaper music split "
                "badjson nonobject data-scalar no-ts unknown" % (token,))
    return steps


def _scripted() -> List[Step]:
    """默认的一组边界用例: 正常 -> 拆包 -> 坏消息 -> 坏消息之后仍能收。"""
    steps: List[Step] = []

    # ---- A) 4 个已知 topic, 正常收 ----
    steps += build_messages(["status", "llm", "wallpaper", "music"])

    # ---- B) 同一条消息故意拆成两次 send (模拟粘包/拆包) ----
    steps += _split_pair(_GAME_STATUS)

    # ---- C) 坏消息: 每条都应被丢弃 + qWarning, 且连接不断 ----
    steps += build_messages(["badjson", "nonobject", "data-scalar",
                             "no-ts", "unknown"])

    # ---- D) 再发一条合法消息: 证明坏消息没有把连接搞坏 ----
    steps.append(("坏消息之后的合法 llm",
                  protocol.encode(protocol.TOPIC_LLM,
                                  {"text": "坏消息不会断开连接。"})))
    return steps


# ---------------------------------------------------------------------------
#  角色一: 只推
# ---------------------------------------------------------------------------
def serve_push(path: str, steps: List[Step], hold: float, interval: float) -> int:
    if os.path.exists(path):
        os.unlink(path)

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(path)
        srv.listen(1)
        print("[fake-agent] 监听 %s" % path, flush=True)

        conn, _ = srv.accept()
        try:
            print("[fake-agent] GUI 已连接, 开始 push (%d 步)" % len(steps), flush=True)
            for note, chunk in steps:
                print("[fake-agent]   -> %-42s (%d 字节)" % (note, len(chunk)),
                      flush=True)
                conn.sendall(chunk)
                time.sleep(interval)
            print("[fake-agent] push 完毕, 保持连接 %.1fs" % hold, flush=True)
            time.sleep(hold)
        finally:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()
    finally:
        srv.close()
        if os.path.exists(path):
            os.unlink(path)

    print("[fake-agent] 退出", flush=True)
    return 0


# ---------------------------------------------------------------------------
#  角色二: 收命令 (+ 可选回推)
# ---------------------------------------------------------------------------
def handle_line(conn: socket.socket, line: bytes, reply: bool) -> None:
    """一条命令: 打印 + (可选) 回推。"""
    raw = line.decode("utf-8", "replace")
    print("[fake-agent] 收到: %s" % raw, flush=True)

    try:
        action, payload = protocol.decode_command(line)
    except protocol.IpcProtocolError as exc:
        print("[fake-agent]   (非法命令, 忽略: %s)" % exc, flush=True)
        return

    print("[fake-agent]   action=%r payload=%r  (收到于 %.3f)"
          % (action, payload, time.time()), flush=True)

    if reply:
        # 回推一条 llm: 客户端应打印 [recv] llm {...}, 证明"发"完还能"收"
        conn.sendall(protocol.encode(protocol.TOPIC_LLM,
                                     {"text": "已收到命令 %s" % (action,)}))


def serve_echo(path: str, reply: bool) -> int:
    if os.path.exists(path):
        os.unlink(path)

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(4)
    print("[fake-agent] 监听 %s (%.3f)  回推=%s"
          % (path, time.time(), "开" if reply else "关"), flush=True)

    try:
        # 循环 accept: GUI 断线重连后会接上新的连接 (重连验证需要)
        while True:
            conn, _ = srv.accept()
            print("[fake-agent] 客户端已连接 (%.3f)" % time.time(), flush=True)
            buf = b""
            try:
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        handle_line(conn, line, reply)
            except OSError as exc:
                print("[fake-agent] 连接异常: %r" % (exc,), flush=True)
            finally:
                conn.close()
                print("[fake-agent] 客户端断开 (%.3f)" % time.time(), flush=True)
    except KeyboardInterrupt:
        print("[fake-agent] 收到 Ctrl-C, 退出", flush=True)
    finally:
        srv.close()
        if os.path.exists(path):
            os.unlink(path)
    return 0


# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="人工验收用假 Agent: 默认只推, --echo 则收命令")
    parser.add_argument("--path", default=protocol.SOCKET_PATH,
                        help="socket 路径 (默认 %s)" % protocol.SOCKET_PATH)
    parser.add_argument("--push", default="scripted",
                        help="连上后按顺序推什么, 逗号分隔 (默认 scripted = "
                             "一组边界用例; 也可 status,llm,wallpaper,music,"
                             "split,badjson,nonobject,data-scalar,no-ts,unknown)")
    parser.add_argument("--echo", action="store_true",
                        help="进入收命令模式: 循环 accept, 打印每条命令的 "
                             "action/payload (直到 Ctrl-C)")
    parser.add_argument("--no-reply", action="store_true",
                        help="配合 --echo: 只收不回推 llm")
    parser.add_argument("--hold", type=float, default=3.0,
                        help="推送模式下 push 完保持连接的秒数 (默认 3.0)")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S,
                        help="两次 push 之间的间隔秒数 (默认 %.2f)"
                             % DEFAULT_INTERVAL_S)
    args = parser.parse_args()

    if args.echo:
        return serve_echo(args.path, reply=not args.no_reply)

    tokens = [t.strip() for t in args.push.split(",") if t.strip()]
    if not tokens:
        raise SystemExit("fake_agent.py: --push 不能为空")
    return serve_push(args.path, build_messages(tokens), args.hold, args.interval)


if __name__ == "__main__":
    sys.exit(main())
