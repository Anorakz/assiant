#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
#  gui/tools/ipc_test_server.py — 验收用的"假 Agent"
#
#  只 push, 不处理 command。它的存在是为了在真正的 Agent IPC server 写出来
#  之前, 能端到端验证 gui/src/services/local_client.*:
#
#      · 4 个已知 topic 都能收到并 emit 对应信号
#      · 一条消息被拆成两次 send 也能正确拼回来 (按 \n 切分, 不按字节数)
#      · 坏消息 (非法 JSON / 非 object / data 不是 object / 缺 timestamp)
#        被丢弃并 qWarning, **连接不断**
#      · 不认识的 topic 被忽略而不是报错
#
#  编码全部走 agent/ipc/protocol.py —— 与真 Agent 用同一套实现, 不会"两边
#  各自照文档手写导致字段名不一致"。
#
#  用法
#  ---------------------------------------------------------------------------
#      终端 A: python3 gui/tools/ipc_test_server.py [--path /tmp/agent.sock]
#      终端 B: ./gui/build/agent_gui
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

#: (给服务端看的说明, 真正要 send 的字节)。说明只打印在服务端, 不上线 ——
#: 线上一条非 JSON 的注释都会变成客户端的 "丢弃非法 JSON 行" warning。
Script = List[Tuple[str, bytes]]


def build_script() -> Script:
    """要按顺序 send 的字节流 (分多次 send, 更接近真实 socket 节奏)。"""
    steps: Script = []

    # ---- A) 4 个已知 topic, 正常收 ----
    steps.append(("A) status / STUDY",
                  protocol.encode(protocol.TOPIC_STATUS,
                                  {"mode": protocol.MODE_STUDY, "connected": True})))
    steps.append(("A) llm / 中文",
                  protocol.encode(protocol.TOPIC_LLM,
                                  {"text": "已经切换到学习模式。"})))
    steps.append(("A) wallpaper / 路径+序号",
                  protocol.encode(protocol.TOPIC_WALLPAPER,
                                  {"path": "/home/kickpi/wallpapers/04.jpg", "index": 3})))
    steps.append(("A) music / 中文标题",
                  protocol.encode(protocol.TOPIC_MUSIC,
                                  {"title": "夜曲", "playing": True})))

    # ---- B) 同一条消息故意拆成两次 send (模拟粘包/拆包) ----
    whole = protocol.encode(protocol.TOPIC_STATUS,
                            {"mode": protocol.MODE_GAME, "connected": False})
    cut = len(whole) // 2
    steps.append(("B) 同一条 status 的前半截 (无换行)",
                  whole[:cut]))
    steps.append(("B) 后半截 + 换行",
                  whole[cut:]))

    # ---- C) 坏消息: 每条都应被丢弃 + qWarning, 且连接不断 ----
    steps.append(("C) 非法 JSON",
                  b"{this is not json}\n"))
    steps.append(("C) 是 JSON 但不是 object",
                  b"[1,2,3]\n"))
    steps.append(("C) data 不是 object",
                  b'{"topic":"status","data":123,"timestamp":1.0}\n'))
    steps.append(("C) 缺 timestamp",
                  b'{"topic":"status","data":{}}\n'))
    steps.append(("C) 未知 topic (期望被静默忽略)",
                  protocol.encode("something_new", {"x": 1})))

    # ---- D) 再发一条合法消息: 证明坏消息没有把连接搞坏 ----
    steps.append(("D) 坏消息之后的合法 llm",
                  protocol.encode(protocol.TOPIC_LLM,
                                  {"text": "坏消息不会断开连接。"})))

    return steps


def serve(path: str, hold: float) -> int:
    if os.path.exists(path):
        os.unlink(path)

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(path)
        srv.listen(1)
        print("[server] 监听 %s" % path, flush=True)

        conn, _ = srv.accept()
        try:
            print("[server] GUI 已连接, 开始 push", flush=True)
            for note, chunk in build_script():
                print("[server]   -> %-42s (%d 字节)" % (note, len(chunk)), flush=True)
                conn.sendall(chunk)
                time.sleep(0.05)   # 让 readyRead 多次触发, 更接近真实节奏
            print("[server] push 完毕, 保持连接 %.1fs" % hold, flush=True)
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

    print("[server] 退出", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="验收用假 Agent (只 push)")
    parser.add_argument("--path", default=protocol.SOCKET_PATH,
                        help="socket 路径 (默认 %s)" % protocol.SOCKET_PATH)
    parser.add_argument("--hold", type=float, default=3.0,
                        help="push 完后保持连接的秒数, 方便看客户端断线日志")
    args = parser.parse_args()
    return serve(args.path, args.hold)


if __name__ == "__main__":
    sys.exit(main())
