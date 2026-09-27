#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T14-2 门禁实测: **set_config 的板端端到端验收**（真 Agent / 真 IPC / 真文本手术）。

验的是"GUI 求 Agent 写配置"这条新路真的通了，而且**只动该动的**：

  A. **一次成功的写入**（真 socket）: 连上真 Agent -> 发 `set_config{id, keys}` ->
     收到 `config_result` 回执（id 原样带回、ok=true、changed=1）-> 真源里那一行变了、
     其余字节未动、`.bak` == 改动前原文 -> **同一个回执里 llm.env 也同步了**（派生那一步）。
  B. **非法键被拒** -> 回执 ok=false + 原话, **一个字节都没写**（真源与派生文件的 md5 都不变）。
  C. **凭据**（`credentials`）-> 写进 `bilibili.cookie_file` 指的**另一个文件**；
     键名写错时连配置那一行也不落盘（不留中间态）。
  D. **不变量**: 板端**真实**的 `config/config.yaml` / `llm/config/llm.env` /
     `config/bilibili_cookie.json` md5 全未变（验收全程用副本）; `git status` 干净;
     socket 收掉; 没留下 Agent 进程。

跑法（板端, 仓库根）::

    python3 tests/board/t14_2_accept.py

⚠ 这是**验收脚本**, 不是生产路径: 它自己起一个真 Runtime（只起 bus + ipc, 不连串流），
   把 true config 指向 **副本**, 所以板端真配置一个字节都不会动。
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import shutil
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

REPO = _ROOT
TMP = ""
_PASSED = []
_FAILED = []

#: 跑完必须一个字节没变的**真实**文件（验收全程用副本）。
REAL_FILES = ["config/config.yaml", "llm/config/llm.env", "config/bilibili_cookie.json"]


# ---------------------------------------------------------------------------
#  小工具（与 t13_accept.py 同款）
# ---------------------------------------------------------------------------
def check(name, ok, detail=""):
    (_PASSED if ok else _FAILED).append(name)
    print("  %s %s%s" % ("✔" if ok else "✘", name, ("  —— %s" % detail) if detail else ""))
    return bool(ok)


def note(text):
    print("     %s" % text)


def md5(path):
    if not os.path.exists(path):
        return "(不在)"
    with open(path, "rb") as handle:
        return hashlib.md5(handle.read()).hexdigest()


def read_text(path):
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return handle.read()


def write_text(path, text):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


async def wait_for(predicate, timeout=5.0, what="条件"):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    raise AssertionError("等不到: %s" % what)


class Recorder(object):
    """收集 Agent 推来的 topic（真的从 socket 上收）。"""

    def __init__(self):
        self.messages = []

    def __call__(self, topic, data):
        self.messages.append((topic, data))

    def replies(self, topic):
        return [data for name, data in self.messages if name == topic]


# ---------------------------------------------------------------------------
#  验收
# ---------------------------------------------------------------------------
def scan_llm_port(env_text):
    for line in env_text.splitlines():
        if line.startswith("LLM_PORT="):
            return line.split("=", 1)[1].strip()
    return None


# ---------------------------------------------------------------------------
async def main():
    global TMP
    print("== set_config · 板端端到端验收（T14-2）")

    real_before = {name: md5(os.path.join(REPO, name)) for name in REAL_FILES}
    print("== 真实文件 md5（跑之前；全程用副本，这几份必须原样）")
    for name, digest in real_before.items():
        print("  %-32s %s" % (name, digest))

    TMP = tempfile.mkdtemp(prefix="t14-2-")
    for relative in ("config/config.yaml", "config/config.example.yaml", "llm/config/llm.env"):
        source = os.path.join(REPO, relative)
        if not os.path.exists(source):
            print("✘ 缺 %s —— 板端还没部署 llm/ 或 config/？" % relative)
            return 2
        target = os.path.join(TMP, relative)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(source, target)
    print("== 副本目录: %s（真配置与派生文件都不动）" % TMP)

    config = os.path.join(TMP, "config", "config.yaml")
    env = os.path.join(TMP, "llm", "config", "llm.env")
    socket_path = os.path.join(TMP, "agent.sock")

    # 副本配置：换 socket 路径 + 凭据文件指到副本里（绝不能碰真的那两份）
    cookie = os.path.join(TMP, "bilibili_cookie.json")
    text = read_text(config)
    text = _set_yaml_scalar(text, "socket_path", socket_path)
    text = _set_yaml_scalar(text, "cookie_file", cookie)
    write_text(config, text)

    from agent.config import read_config_file
    from agent.ipc.local_client import LocalClient
    from agent.ipc.protocol import COMMAND_SET_CONFIG, TOPIC_CONFIG_RESULT
    from agent.main import Runtime
    from pathlib import Path

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(logging.INFO)
    root.addHandler(logging.FileHandler(os.path.join(TMP, "agent.log"), mode="w",
                                        encoding="utf-8"))

    runtime = Runtime(config=read_config_file(config),
                      config_path_used=Path(config),
                      start_native=False, start_terminal=False,
                      log=logging.getLogger("agent.t14_2"))
    recorder = Recorder()
    client = None

    async def on_message(topic, data):
        recorder(topic, data)

    try:
        # ⚠ 先让 Agent 把 socket 建出来, 再连 —— 反过来会 FileNotFoundError
        await runtime._start_bus_and_io()
        await runtime._start_ipc()
        await wait_for(lambda: runtime.ipc is not None, 5, "IPC 起来了")
        check("真 Agent 的 IPC 起在副本 socket 上", os.path.exists(socket_path), socket_path)

        client = LocalClient(path=socket_path)
        client.on_message(on_message)      # ⚠ 要先注册再连（读循环起来就开始分发）
        await client.connect()
        await runtime._start_bus_and_io()
        await runtime._start_ipc()
        await wait_for(lambda: runtime.ipc is not None, 5, "IPC 起来了")
        check("真 Agent 的 IPC 起在副本 socket 上", os.path.exists(socket_path), socket_path)

        print("\n== A. 一次成功的写入（真 socket + 真手术 + 真派生）")
        # 第一轮：改 llm.port —— 这一轮同时验"真源改了 + 同一回执里 llm.env 也派生好了"
        before_env = read_text(env)
        want_port = "9317" if scan_llm_port(before_env) != "9317" else "9318"
        await client.send_command(COMMAND_SET_CONFIG,
                                  {"id": "t14-2-port", "keys": {"llm.port": want_port}})
        await wait_for(lambda: recorder.replies(TOPIC_CONFIG_RESULT), 10, "第一轮回执")
        reply = recorder.replies(TOPIC_CONFIG_RESULT)[-1]
        check("第一轮 ok=true（改 llm.port）", reply.get("ok") is True, str(reply.get("error"))[:80])
        after_env = read_text(env)
        check("同一个回执里 llm.env 派生到了 LLM_PORT=%s" % want_port,
              scan_llm_port(after_env) == want_port, "实际 %s" % scan_llm_port(after_env))
        check("回执里 llm_env.ok=true 且 changed>=1",
              reply["llm_env"]["ok"] and reply["llm_env"]["changed"] >= 1,
              str(reply.get("llm_env")))
        check("llm.env 除目标行外未动", _only_line_changed(before_env, after_env, "LLM_PORT"),
              _first_diff(before_env, after_env))

        # 第二轮：改 study 那一行 —— 只动一行 + .bak + llm.env 这次没有差异
        recorder.messages.clear()
        before_config, before_env = read_text(config), read_text(env)
        await client.send_command(COMMAND_SET_CONFIG,
                                  {"id": "t14-2-a", "keys": {"study.relative_band": "0.077"}})
        await wait_for(lambda: recorder.replies(TOPIC_CONFIG_RESULT), 10, "config_result 回执")
        reply = recorder.replies(TOPIC_CONFIG_RESULT)[-1]
        check("回执里 id 原样带回", reply.get("id") == "t14-2-a", str(reply.get("id")))
        check("回执 ok=true", reply.get("ok") is True, str(reply.get("error")))
        check("回执 changed == 1", reply.get("changed") == 1, str(reply.get("changed")))
        check("回执给了 .bak 路径", bool(reply.get("backup")), str(reply.get("backup")))

        after_config = read_text(config)
        check("真源里那一行变了", "relative_band: 0.077" in after_config)
        check("只动了那一行（逐字符相等）",
              _only_line_changed(before_config, after_config, "relative_band"),
              _first_diff(before_config, after_config))
        check("`.bak` == 改动前原文", read_text(reply["backup"]) == before_config
              if reply.get("backup") else False)
        check("第二轮 llm.env 没被白写（值本来就对 -> changed=0）",
              reply["llm_env"]["ok"] and reply["llm_env"]["changed"] == 0
              and read_text(env) == before_env, str(reply.get("llm_env")))

        print("\n== B. 非法键被拒（一个字节都不写）")
        md5_config, md5_env = md5(config), md5(env)
        recorder.messages.clear()
        await client.send_command(COMMAND_SET_CONFIG,
                                  {"id": "t14-2-b", "keys": {"study.nope": "1"}})
        await wait_for(lambda: recorder.replies(TOPIC_CONFIG_RESULT), 10, "拒绝的回执")
        reply = recorder.replies(TOPIC_CONFIG_RESULT)[-1]
        check("回执 ok=false + 原话", reply["ok"] is False and bool(reply["error"]),
              reply.get("error", "")[:80])
        check("真源 md5 未变", md5(config) == md5_config)
        check("派生文件 md5 未变", md5(env) == md5_env)

        print("\n== C. 凭据（另一个文件）")
        recorder.messages.clear()
        await client.send_command(COMMAND_SET_CONFIG,
                                  {"id": "t14-2-c1", "keys": {},
                                   "credentials": {"SESSDATA": "t14-2-sess"}})
        await wait_for(lambda: recorder.replies(TOPIC_CONFIG_RESULT), 10, "凭据回执")
        reply = recorder.replies(TOPIC_CONFIG_RESULT)[-1]
        check("凭据写进副本 cookie 文件", reply["ok"] and os.path.exists(cookie)
              and "t14-2-sess" in read_text(cookie), str(reply.get("error")))
        check("凭据文件是**副本**里的那个（真那份没动）", cookie.startswith(TMP), cookie)

        md5_config = md5(config)
        recorder.messages.clear()
        await client.send_command(COMMAND_SET_CONFIG,
                                  {"id": "t14-2-c2", "keys": {"study.relative_band": "0.081"},
                                   "credentials": {"SEESSDATA": "typo"}})
        await wait_for(lambda: recorder.replies(TOPIC_CONFIG_RESULT), 10, "凭据笔误的回执")
        reply = recorder.replies(TOPIC_CONFIG_RESULT)[-1]
        check("凭据笔误 -> ok=false", reply["ok"] is False, reply.get("error", "")[:80])
        check("配置那一行也没落盘（不留中间态）", md5(config) == md5_config
              and "0.081" not in read_text(config))

        print("\n== C2. 派生一致性用 Python 那份实现（不再依赖板端构建 gui/）")
        from agent.cli import derivation_check
        status, detail = derivation_check(os.path.join(TMP, "config", "config.yaml"), env)
        check("副本上算得出一致（回执刚同步过）", status == "OK", "%s / %s" % (status, detail[:80]))
        real_status, real_detail = derivation_check(os.path.join(REPO, "config", "config.yaml"),
                                                    os.path.join(REPO, "llm", "config", "llm.env"))
        note("板端真实那一对: %s —— %s" % (real_status, real_detail[:100]))
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                await client.close()
        with contextlib.suppress(Exception):
            await runtime.stop()
        await asyncio.sleep(0.5)

    print("\n== D. 不变量（真文件 / 干净收场）")
    for name, digest in real_before.items():
        check("真实文件 %s 一个字节没动" % name, md5(os.path.join(REPO, name)) == digest,
              md5(os.path.join(REPO, name)))
    import subprocess

    rc = subprocess.run(["git", "status", "--porcelain"], cwd=REPO, stdout=subprocess.PIPE,
                        universal_newlines=True)
    check("板端 git status 干净", rc.returncode == 0 and not rc.stdout.strip(),
          rc.stdout.strip()[:80])
    check("验收 socket 已收掉", not os.path.exists(socket_path))
    leftovers = []
    for name in ("agent.main", "llama-server", "moonlight"):
        proc = subprocess.run(["pgrep", "-af", name], stdout=subprocess.PIPE, universal_newlines=True)
        if proc.stdout.strip():
            leftovers.append("%s: %s" % (name, proc.stdout.strip().splitlines()[0][:60]))
    check("没留下 Agent / llama-server / moonlight 进程", not leftovers, "；".join(leftovers))

    print("\n== 结果: %s（%d 项通过%s）"
          % ("全部通过" if not _FAILED else "失败 %d 项" % len(_FAILED), len(_PASSED),
             (", 失败: " + "、".join(_FAILED)) if _FAILED else ""))
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if _FAILED else 0


# ---------------------------------------------------------------------------
def _set_yaml_scalar(text, key, value):
    """把 `  <key>: …` 那行换成 `<key>: <value>`（只用于造验收副本）。"""
    out = []
    for line in text.splitlines(keepends=True):
        if line.strip().startswith(key + ":"):
            head = line[:len(line) - len(line.lstrip())]
            out.append("%s%s: %s\n" % (head, key, value))
        else:
            out.append(line)
    return "".join(out)


def _first_diff(before, after):
    a, b = before.split("\n"), after.split("\n")
    for index in range(max(len(a), len(b))):
        left = a[index] if index < len(a) else "(缺)"
        right = b[index] if index < len(b) else "(缺)"
        if left != right:
            return "第 %d 行: %r -> %r" % (index + 1, left, right)
    return "（没有差异）"


def _only_line_changed(before, after, marker):
    """只有含 marker 的那一行变了（行数不变、其余逐行相等）。"""
    a, b = before.split("\n"), after.split("\n")
    if len(a) != len(b):
        return False
    changed = [index for index in range(len(a)) if a[index] != b[index]]
    if len(changed) != 1:
        return False
    return marker in b[changed[0]]


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
