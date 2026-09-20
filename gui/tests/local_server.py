#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
#  gui/tests/local_server.py — LocalClient 单测用的 Python 对端 (LocalServer)
#
#  由 gui/tests/test_local_client.cpp 用 QProcess 拉起, 不是 mock:
#  真的 bind 一个 unix domain socket, 真的收发, 与真 Agent 用同一套
#  agent/ipc/protocol.py 编码。
#
#  --push 控制"连上之后先推什么" (按顺序, 逗号分隔):
#      status    一条合法 status  {"mode":"STUDY","connected":true}
#      llm       一条合法 llm     {"text": LLM_TEXT}
#      badjson   一行非法 JSON    (验证客户端丢弃 + 不崩 + 不触发信号)
#      unknown   一个未知 topic   (验证客户端忽略而不是报错)
#  推完之后进入收行循环, 每收到一行打印 "RECV <原始行>"。
#
#  打印约定 (测试靠它同步, 不要随便改)
#  ---------------------------------------------------------------------------
#      LISTENING <path>    已经 bind + listen, 可以连了
#      CONNECTED           有客户端连上
#      RECV <line>         收到一行命令 (原始 JSON, 不含换行)
#      DISCONNECTED        客户端断开, 回去 accept 下一个
#
#  用法
#  ---------------------------------------------------------------------------
#      python3 local_server.py --path /tmp/x.sock --push status,llm
# ============================================================================
from __future__ import annotations

import argparse
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

#: llm 推送的文本。**必须**与 test_local_client.cpp 里的 kLlmText 一致。
LLM_TEXT = "单测用的 llm 推送"

#: 两次 push 之间的小停顿: 让客户端多触发几次 readyRead (更接近真实节奏)。
#: 顺序仍然严格保持 —— 测试靠"后一条到达"证明"前一条已处理完"。
PUSH_GAP_S = 0.02


def build_push(token: str) -> bytes:
    """把一个 token 变成要 send 的字节 (含结尾换行)。"""
    if token == "status":
        return protocol.encode(
            protocol.TOPIC_STATUS,
            {"mode": protocol.MODE_STUDY, "connected": True},
        )
    if token == "llm":
        return protocol.encode(protocol.TOPIC_LLM, {"text": LLM_TEXT})
    if token == "badjson":
        return b"{this is not json}\n"
    if token == "unknown":
        return protocol.encode("an_unknown_topic_for_test", {"x": 1})
    raise SystemExit("local_server.py: 不认识的 --push token: %r" % (token,))


def serve(path: str, pushes: list) -> int:
    if os.path.exists(path):
        os.unlink(path)

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(4)
    print("LISTENING %s" % path, flush=True)

    try:
        # 循环 accept: 客户端断线重连后会接上新连接 (重连用例需要)
        while True:
            conn, _ = srv.accept()
            print("CONNECTED", flush=True)
            try:
                for token in pushes:
                    conn.sendall(build_push(token))
                    time.sleep(PUSH_GAP_S)

                buf = b""
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        print("RECV %s" % line.decode("utf-8", "replace"), flush=True)
            except OSError as exc:
                print("SOCKET_ERROR %r" % (exc,), flush=True)
            finally:
                conn.close()
                print("DISCONNECTED", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        srv.close()
        if os.path.exists(path):
            os.unlink(path)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="LocalClient 单测用的 Python 对端")
    parser.add_argument("--path", required=True, help="unix socket 路径")
    parser.add_argument("--push", default="status,llm",
                        help="连上后按顺序推什么: status,llm,badjson,unknown (逗号分隔)")
    args = parser.parse_args()

    pushes = [t.strip() for t in args.push.split(",") if t.strip()]
    return serve(args.path, pushes)


if __name__ == "__main__":
    sys.exit(main())
