#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
#  gui/tools/ipc_echo_server.py — 阶段 2 验收用的"假 Agent"
#
#  只做两件事:
#      1) 收到 GUI 发来的命令 -> 打印原始行 + action/payload
#      2) 可选地回推一条 llm 消息 (用 protocol.encode), 证明双向都通
#
#  命令的线格式 (重要)
#  ---------------------------------------------------------------------------
#  本脚本按**阶段 2 任务约定**解析 GUI 发来的 {"action": ..., "payload": ...}。
#  ⚠ 这与真 Agent 的 agent/ipc/protocol.py 不同 —— 那边 decode_full() 要求
#    {topic, data, timestamp} 信封。所以这里**不能**直接调 protocol.decode(),
#    只能自己 json.loads。将来两边统一后, 这里应换成 protocol.decode()。
#
#  与 ipc_test_server.py 的区别
#  ---------------------------------------------------------------------------
#      ipc_test_server.py  只 push (验证收)
#      ipc_echo_server.py  收命令 + 回推 (验证发 + 重连; 可被 kill 后重启)
#
#  用法
#  ---------------------------------------------------------------------------
#      终端 A: python3 gui/tools/ipc_echo_server.py [--path /tmp/agent.sock]
#      终端 B: ./gui/build/agent_gui
# ============================================================================
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from pathlib import Path

# 让脚本在任意 cwd 下都能 import agent.ipc
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.ipc import protocol  # noqa: E402


def handle_line(conn: socket.socket, line: bytes, echo: bool) -> None:
    """一条命令: 打印 + (可选) 回推。"""
    raw = line.decode("utf-8", "replace")
    print("[server] 收到: %s" % raw, flush=True)

    try:
        msg = json.loads(raw)
    except ValueError as exc:
        print("[server]   (不是合法 JSON, 忽略: %s)" % exc, flush=True)
        return

    if not isinstance(msg, dict):
        print("[server]   (不是 JSON object, 忽略)", flush=True)
        return

    action = msg.get("action")
    payload = msg.get("payload")
    print("[server]   action=%r payload=%r  (收到于 %.3f)"
          % (action, payload, time.time()), flush=True)

    if echo:
        # 回推一条 llm: 客户端应打印 [recv] llm {...}, 证明"发"完还能"收"
        text = "已收到命令 %s" % (action,)
        conn.sendall(protocol.encode(protocol.TOPIC_LLM, {"text": text}))


def serve(path: str, echo: bool) -> int:
    if os.path.exists(path):
        os.unlink(path)

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(4)
    print("[server] 监听 %s (%.3f)" % (path, time.time()), flush=True)

    try:
        # 循环 accept: GUI 断线重连后会接上新的连接 (重连验证需要)
        while True:
            conn, _ = srv.accept()
            print("[server] 客户端已连接 (%.3f)" % time.time(), flush=True)
            buf = b""
            try:
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        handle_line(conn, line, echo)
            except OSError as exc:
                print("[server] 连接异常: %r" % (exc,), flush=True)
            finally:
                conn.close()
                print("[server] 客户端断开 (%.3f)" % time.time(), flush=True)
    except KeyboardInterrupt:
        print("[server] 收到 Ctrl-C, 退出", flush=True)
    finally:
        srv.close()
        if os.path.exists(path):
            os.unlink(path)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="阶段 2 验收用假 Agent (收命令)")
    parser.add_argument("--path", default=protocol.SOCKET_PATH,
                        help="socket 路径 (默认 %s)" % protocol.SOCKET_PATH)
    parser.add_argument("--no-echo", action="store_true",
                        help="不回推 llm 消息 (只看单向收命令)")
    args = parser.parse_args()
    return serve(args.path, echo=not args.no_echo)


if __name__ == "__main__":
    sys.exit(main())
