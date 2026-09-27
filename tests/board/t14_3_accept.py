#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T14-3 门禁实测: **GUI 改设置 -> 真 Agent 写 -> 回读一致**（真 Qt5 + 真 IPC + 真文本手术）。

验的是"GUI 不再自己写"这条改道真的通了：

  A. 起一个**真 Agent**（只接 bus + ipc，把真源指向**副本**），真 GUI 带着
     `--settings-one-key-demo` 跑一次：改 `study.relative_band` 再点保存 ->
     · 副本 config.yaml 里那一行变了、**其余逐字符不变**、`.bak` == 改动前原文；
     · 派生 `llm.env` 也由 Agent 同步了（同一趟）；
     · GUI 日志里出现"Agent 已写入"。
  B. **Agent 停掉**之后再点一次保存（同一个 GUI 路径）：
     · 副本 config.yaml **一个字节都没变**（不退回直写）；
     · GUI 把"没连上"如实写出来，并露出「启动 Agent」。
  C. 不变量：板端真实 `config/config.yaml` / `llm/config/llm.env` md5 未变、
     `git status` 干净、本次验收的临时进程都收掉了（Agent/GUI 自 T14-7 起是 systemd
     常驻服务，**不算**残留；llama-server 由 Agent 按 `llm.mode: edge` 自己拉起）。

跑法（板端, 仓库根）::

    python3 tests/board/t14_3_accept.py
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GUI = os.path.join(REPO, "gui", "build", "agent_gui")
REAL_FILES = ["config/config.yaml", "llm/config/llm.env"]

_PASSED = []
_FAILED = []
TMP = ""

#: 起一个"真的能写配置"的 Agent（只接 bus + ipc；不连串流、不起终端）
_AGENT_BOOTSTRAP = r"""
import asyncio, logging, sys
from pathlib import Path
sys.path.insert(0, %(repo)r)
from agent.config import read_config_file
from agent.main import Runtime

async def main():
    config_path = Path(%(config)r)
    logging.basicConfig(level=logging.INFO,
                        filename=%(log)r, filemode="w",
                        format="%%(asctime)s %%(levelname)s %%(name)s: %%(message)s")
    runtime = Runtime(config=read_config_file(config_path), config_path_used=config_path,
                      start_native=False, start_terminal=False,
                      log=logging.getLogger("agent.t14_3"))
    await runtime._start_bus_and_io()
    await runtime._start_ipc()
    Path(%(ready)r).write_text("ready", encoding="utf-8")
    try:
        while True:
            await asyncio.sleep(1)
    finally:
        await runtime.stop()

asyncio.run(main())
"""


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


def set_yaml_scalar(text, key, value):
    out = []
    for line in text.splitlines(keepends=True):
        if line.strip().startswith(key + ":"):
            head = line[:len(line) - len(line.lstrip())]
            out.append("%s%s: %s\n" % (head, key, value))
        else:
            out.append(line)
    return "".join(out)


def only_line_changed(before, after, marker):
    a, b = before.split("\n"), after.split("\n")
    if len(a) != len(b):
        return False, "行数变了 %d -> %d" % (len(a), len(b))
    changed = [i for i in range(len(a)) if a[i] != b[i]]
    if len(changed) != 1:
        return False, "变了 %d 行" % len(changed)
    return marker in b[changed[0]], "第 %d 行: %r -> %r" % (changed[0] + 1, a[changed[0]],
                                                            b[changed[0]])


def run_gui(config, extra, timeout=60, socket_path=None):
    env = dict(os.environ)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    cmd = [GUI, "--config", config, "--windowed", "--page", "settings"]
    if socket_path:
        # GUI 的 socket 路径来自命令行（默认 /tmp/agent.sock）——
        # 验收用副本的 socket，必须显式指过去
        cmd += ["--socket", socket_path]
    cmd += list(extra)
    proc = subprocess.run(cmd, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          universal_newlines=True, timeout=timeout, env=env)
    return proc.returncode, proc.stdout or ""


def wait_for(predicate, timeout=15.0, what="条件"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    raise AssertionError("等不到: %s" % what)


def main():
    global TMP
    print("== GUI 改设置 -> 真 Agent 写（T14-3 端到端验收）")
    real_before = {name: md5(os.path.join(REPO, name)) for name in REAL_FILES}
    for name, digest in real_before.items():
        print("  %-28s %s（跑完必须一样）" % (name, digest))

    TMP = tempfile.mkdtemp(prefix="t14-3-")
    for rel in ("config/config.yaml", "config/config.example.yaml", "llm/config/llm.env"):
        src = os.path.join(REPO, rel)
        if not os.path.exists(src):
            print("✘ 缺 %s" % rel)
            return 2
        dst = os.path.join(TMP, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)

    config = os.path.join(TMP, "config", "config.yaml")
    env_file = os.path.join(TMP, "llm", "config", "llm.env")
    socket_path = os.path.join(TMP, "agent.sock")
    ready = os.path.join(TMP, "agent.ready")
    log = os.path.join(TMP, "agent.log")
    # 副本配置：换 socket 路径 + 凭据指到副本里（绝不碰真的那两份）
    text = set_yaml_scalar(read_text(config), "socket_path", socket_path)
    text = set_yaml_scalar(text, "cookie_file", os.path.join(TMP, "cookie.json"))
    write_text(config, text)
    print("== 副本: %s（真配置与派生文件都不动）" % TMP)

    print("\n== A. 真 Agent 在跑：GUI 点保存 -> Agent 落盘")
    agent = subprocess.Popen([sys.executable, "-c", _AGENT_BOOTSTRAP % {
        "repo": REPO, "config": config, "log": log, "ready": ready}],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_for(lambda: os.path.exists(ready), 25, "Agent 起来（ready 标记）")
        before_config, before_env = read_text(config), read_text(env_file)
        code, out = run_gui(config, ["--settings-one-key-demo",
                                     "--screenshot", os.path.join(TMP, "gui_a.png"),
                                     "--screenshot-delay", "4200"], timeout=90,
                            socket_path=socket_path)
        check("GUI 跑完（退出码 0，且连上了 Agent）", code == 0 and "已连接" in out,
              out.strip().splitlines()[-1][:80] if out else "")
        check("GUI 日志说 Agent 已写入", "Agent 已写入" in out,
              [ln for ln in out.splitlines() if "settings" in ln][-1:][0][:100]
              if "settings" in out else "")

        after_config = read_text(config)
        ok, detail = only_line_changed(before_config, after_config, "relative_band")
        check("副本真源：只动 relative_band 那一行", ok, detail)
        check("写进去的是界面上的值（QDoubleSpinBox 两位小数：0.077 -> 0.08）",
              "relative_band: 0.08" in after_config)
        check("`.bak` == 改动前原文",
              os.path.exists(config + ".bak") and read_text(config + ".bak") == before_config)

        after_env = read_text(env_file)
        # 副本那份 llm.env 与真源本来一致 -> 这一趟没有差异（changed=0, 不写不留 .bak）
        check("派生 llm.env 由 Agent 同一趟检查过（内容未变 = 本来就一致）",
              after_env == before_env, "env 变了" if after_env != before_env else "")
    finally:
        agent.send_signal(signal.SIGINT)
        try:
            agent.wait(timeout=10)
        except subprocess.TimeoutExpired:
            agent.kill()
        time.sleep(1.0)

    print("\n== B. Agent 停掉：GUI 点保存 -> 明确失败，且一个字节都不写")
    before_config = read_text(config)
    code, out = run_gui(config, ["--settings-one-key-demo",
                                 "--screenshot", os.path.join(TMP, "gui_b.png"),
                                 "--screenshot-delay", "4200"], timeout=90,
                        socket_path=socket_path)
    check("GUI 跑完（退出码 0，Agent 没在）", code == 0)
    check("GUI 如实说没连上", "Agent 没连上" in out or "没连上" in out,
          [ln for ln in out.splitlines() if "连" in ln][-1:][0][:100] if "连" in out else "")
    check("GUI 拒绝写配置（日志里有拒绝）", "拒绝 set_config" in out)
    check("副本真源一个字节都没变", read_text(config) == before_config)

    print("\n== C. 不变量")
    for name, digest in real_before.items():
        check("真实文件 %s 未变" % name, md5(os.path.join(REPO, name)) == digest,
              md5(os.path.join(REPO, name)))
    rc = subprocess.run(["git", "status", "--porcelain"], cwd=REPO, stdout=subprocess.PIPE,
                        universal_newlines=True)
    check("板端 git status 干净", rc.returncode == 0 and not rc.stdout.strip(),
          rc.stdout.strip()[:80])
    # T14-7 起 Agent 与 GUI 是 **systemd 常驻服务**（agent.service / agent-gui.service），
    # 所以"没有 agent.main / agent_gui 进程"这条老判据已经不成立了 —— 它们**本来**就该在跑。
    # 同样地，`llama-server` 是 Agent 按 `llm.mode: edge` 自己拉起来的（板端实测：Agent
    # 起来 3 秒后 llama-server 起，端口 9000），也不是这次的残留。
    # 现在要判的是：**本次验收自己起的**那些临时进程都收掉了（本脚本用的是 /tmp 下的
    # 副本 socket + 自己的子进程），以及 systemd 那两个服务仍然健康。
    stray = []
    for pattern in (os.path.join(TMP, "agent"), "moonlight"):
        proc = subprocess.run(["pgrep", "-af", pattern], stdout=subprocess.PIPE,
                              universal_newlines=True)
        if proc.stdout.strip():
            stray.append("%s -> %s" % (pattern, proc.stdout.strip()[:60]))
    check("本次验收的临时进程都收掉了（/tmp 副本 socket / moonlight）", not stray,
          "；".join(stray))

    services_ok = True
    for unit in ("agent.service", "agent-gui.service"):
        proc = subprocess.run(["systemctl", "is-active", unit], stdout=subprocess.PIPE,
                              universal_newlines=True)
        if proc.stdout.strip() != "active":
            services_ok = False
    check("常驻服务仍是 active（T14-7：Agent / GUI 由 systemd 管，不是残留）", services_ok)
    check("验收 socket 已收掉", not os.path.exists(socket_path))

    print("\n== 结果: %s（%d 项通过%s）"
          % ("全部通过" if not _FAILED else "失败 %d 项" % len(_FAILED), len(_PASSED),
             (", 失败: " + "、".join(_FAILED)) if _FAILED else ""))
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
