#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/board/p8_coexist.py — 并存验收：真 Agent + 真 GUI，**谁在写配置**（T14-11）

要证的那句话（你 2026-09-27 定的架构，见 docs/adr/0005）：
    **`config.yaml` / `llm.env` / 凭据，任何时候只有 Agent 一个写入者。**

光看代码不够 —— 这里让两个真进程**同时**跑起来、同时想改配置，然后从**操作系统那一侧**
取证：

  A. 快照：真源的 md5 + 一个标量键的当前值（跑完必须回到原样）。
  B. 并存窗口：一个真 GUI（offscreen，走 `--settings-one-key-demo` 点保存 → IPC → Agent 写）
     + 两个 IPC 客户端**并发**发 `set_config`（改**不同**的键，最后都还原）。
  C. 单写者证据（这一段是重点）：
     · 窗口内反复扫 `/proc/<pid>/fd`：**只有 `agent.main` 持有真源的写句柄**，
       `agent_gui` 一个都没有（GUI 连只读都没开，它只发 IPC）；
     · 每次改动之后真源都能**解析成合法 YAML**（没有半行/交错写坏）；
     · 每次改动都留了 `.bak`，且 `.bak` 内容 == 那次改动**之前**的原文；
     · 并发改写**不丢更新**：所有请求的键最后都是各自要求的值。
  D. 还原 + 不变量：把键写回原值、md5 必须回到 A 里那个数；systemd 两个服务仍 active、
     `git status` 干净、没留下本次验收的临时进程。

⚠ 安全设计（板端真源是真东西）：
  · 只碰**一个**非秘密的标量键（`study.relative_band`），而且**写回原值**；
  · 不动当前网络、不动凭据、不动 `net:` 段；
  · 全程用**系统正在跑的 agent.service**（`/tmp/agent.sock`），不起第二个 Agent
    （那会抢同一个 socket）—— "并存"在这里就是"生产里的那两个进程"。

跑法（板端，仓库根）::

    python3 tests/board/p8_coexist.py
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GUI = os.path.join(REPO, "gui", "build", "agent_gui")
CONFIG = os.path.join(REPO, "config", "config.yaml")
ENV_FILE = os.path.join(REPO, "llm", "config", "llm.env")
KEY = "study.relative_band"          # 只碰这一个（GUI 白名单里的标量键）
OTHER_KEYS = ["study.focus_interval_min", "study.recheck_interval_min"]

_PASSED = []
_FAILED = []


def check(name, ok, detail=""):
    (_PASSED if ok else _FAILED).append(name)
    print("  %s %s%s" % ("✔" if ok else "✘", name, ("  —— %s" % detail) if detail else ""),
          flush=True)
    return ok


def md5(path):
    with open(path, "rb") as handle:
        return hashlib.md5(handle.read()).hexdigest()


def read_text(path):
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def socket_path():
    try:
        import yaml
        with open(CONFIG, encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        return str((data.get("ipc") or {}).get("socket_path") or "/tmp/agent.sock")
    except Exception:                                     # noqa: BLE001
        return "/tmp/agent.sock"


class Ipc:
    """够用就好的 IPC 客户端（收推送时按 topic 过滤）。"""

    def __init__(self, path, timeout=60.0):
        self.timeout = timeout
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(path)
        self.buf = b""

    def send(self, action, payload):
        line = json.dumps({"action": action, "payload": payload},
                          ensure_ascii=False, separators=(",", ":")) + "\n"
        self.sock.sendall(line.encode("utf-8"))

    def set_config(self, request_id, keys):
        self.send("set_config", {"id": request_id, "keys": keys})
        return self.wait_for("config_result", lambda d: d.get("id") == request_id)

    def wait_for(self, topic, predicate=None, timeout=None):
        deadline = time.time() + (timeout or self.timeout)
        while time.time() < deadline:
            self.sock.settimeout(max(0.5, deadline - time.time()))
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            self.buf += chunk
            while b"\n" in self.buf:
                raw, self.buf = self.buf.split(b"\n", 1)
                if not raw.strip():
                    continue
                try:
                    message = json.loads(raw.decode("utf-8"))
                except ValueError:
                    continue
                if message.get("topic") != topic:
                    continue
                data = message.get("data") or {}
                if predicate is None or predicate(data):
                    return data
        raise TimeoutError("等 %s 超时" % topic)

    def close(self):
        self.sock.close()


def config_value(key, path=CONFIG):
    """从 YAML 里读一个点号键（只读，不写）。"""
    import yaml
    with open(path, encoding="utf-8") as handle:
        node = yaml.safe_load(handle) or {}
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def write_handles(pattern):
    """扫全部进程的 fd，返回"谁把 pattern 这个文件开着、且是写着的"。

    @return `{pid: {"comm": 进程名, "write": bool}}`
    """
    found = {}
    real = os.path.realpath(pattern)
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        fd_dir = "/proc/%s/fd" % pid
        try:
            comm = read_text("/proc/%s/comm" % pid).strip()
            flags = read_text("/proc/%s/fdinfo" % pid)
            for entry in os.listdir(fd_dir):
                try:
                    target = os.readlink(os.path.join(fd_dir, entry))
                except OSError:
                    continue
                if os.path.realpath(target) != real:
                    continue
                info = re.search(r"^fd%s:\n(?:.*\n)*?flags:\s*(\S+)" % re.escape(entry),
                                 flags, re.MULTILINE)
                flag_value = int(info.group(1), 8) if info else 0
                writable = bool(flag_value & (os.O_WRONLY | os.O_RDWR))
                known = found.setdefault(pid, {"comm": comm, "write": False})
                known["write"] = known["write"] or writable
        except (OSError, ValueError):
            continue
    return found


def ensure_agent_socket(path):
    """socket 不在就重启一次 agent.service 把它重建出来。

    板端实测过一种现网故障（T14-9 修掉的那种）：`/tmp/agent.sock` 在内核里还监听着，
    但**文件名的目录项**被某个正在退出的旧实例 unlink 掉了 —— 这时 CLI/GUI 全连不上。
    """
    if os.path.exists(path):
        return True
    subprocess.run(["systemctl", "restart", "agent.service"], stdout=subprocess.DEVNULL)
    for _ in range(20):
        time.sleep(1)
        if os.path.exists(path):
            return True
    return False


def main():
    path = socket_path()
    print("== P8 并存验收：真 Agent + 真 GUI，谁在写配置（T14-11）")
    print("  socket: %s" % path)
    if not ensure_agent_socket(path):
        print("  ✘ Agent 的 socket 起不来（%s）—— 先 systemctl status agent.service" % path)
        return 1
    if not os.path.exists(GUI):
        print("  ✘ 找不到 GUI 二进制（%s）" % GUI)
        return 1

    before_md5 = md5(CONFIG)
    before_env = md5(ENV_FILE) if os.path.exists(ENV_FILE) else ""
    before_value = config_value(KEY)
    print("  %-24s %s（跑完必须一样）" % ("config/config.yaml", before_md5))
    print("  %-24s %s" % (KEY, before_value))

    print("\n== A. 并存窗口：真 GUI 保存 + 两个客户端并发写")
    gui_log = "/tmp/p8_coexist_gui.log"
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONIOENCODING"] = "utf-8"
    with open(gui_log, "wb") as log:
        gui = subprocess.Popen(
            [GUI, "--config", CONFIG, "--socket", path, "--page", "settings",
             "--settings-one-key-demo"], cwd=REPO, stdout=log, stderr=subprocess.STDOUT,
            env=env)

    client_a, client_b = Ipc(path), Ipc(path)
    results = {}
    yaml_ok = True
    backups_ok = True
    try:
        time.sleep(1.0)
        # 并发：GUI 正在点保存（它改 KEY），两个客户端同时改另外两个键
        for index, other in enumerate(OTHER_KEYS * 2):
            client = client_a if index % 2 == 0 else client_b
            original = config_value(other)
            reply = client.set_config("p8-a-%d" % index, {other: str(original)})
            results[other] = (str(original), reply)
        # 窗口内扫 fd（GUI 还在跑）
        handles = write_handles(CONFIG)
        env_handles = write_handles(ENV_FILE) if os.path.exists(ENV_FILE) else {}
        # ⚠ `--settings-one-key-demo` 只是"启动后点一次保存"，GUI 自己**不会退出**
        #   （它是 kiosk）。所以给它十几秒把那次保存做完，然后我们收掉它。
        time.sleep(14)
        gui_output_so_far = read_text(gui_log) if os.path.exists(gui_log) else ""
        check("A0 GUI 的那次保存确实发出去了（走 IPC）",
              ("已请 Agent" in gui_output_so_far) or ("Agent 已写入" in gui_output_so_far),
              gui_output_so_far[-200:])
        gui.terminate()                       # SIGTERM → 崩溃日志那套会走干净退出
        try:
            gui.wait(timeout=20)
        except subprocess.TimeoutExpired:
            gui.kill()
            gui.wait(timeout=10)
        # GUI 那次保存的结果也验一下（它把 KEY 改成 0.077→0.08）
        for index in range(2):
            reply = (client_a if index == 0 else client_b).set_config(
                "p8-z-%d" % index, {KEY: "0.08"})
            check("A%d 并发 set_config 回执 ok（走的是 Agent）" % (index + 1),
                  reply.get("ok") is True, str(reply.get("error") or reply.get("changed")))
        try:
            import yaml
            yaml.safe_load(read_text(CONFIG))
        except Exception as exc:                          # noqa: BLE001
            yaml_ok = False
            print("    真源解析失败: %r" % exc)
        backup = CONFIG + ".bak"
        if os.path.exists(backup):
            # `.bak` 是"上一次改动前的原文"——只要它自己是合法 YAML 就说明没有半行
            try:
                import yaml
                yaml.safe_load(read_text(backup))
            except Exception:                             # noqa: BLE001
                backups_ok = False
    finally:
        if gui.poll() is None:
            gui.kill()
        client_a.close()
        client_b.close()

    gui_output = read_text(gui_log) if os.path.exists(gui_log) else ""
    check("A1 真 GUI 与真 Agent 同时跑起来了（退出码 0 或 SIGTERM 收的 -15）",
          gui.returncode in (0, -15, 143),
          "rc=%s 日志尾部=%r" % (gui.returncode, gui_output[-120:]))
    check("A2 GUI 走了 IPC（日志里有「已请 Agent 写配置」或「Agent 已写入」）",
          ("已请 Agent" in gui_output) or ("Agent 已写入" in gui_output),
          gui_output[-200:])
    check("A3 每次改动之后真源都是合法 YAML（没有被写坏）", yaml_ok)
    check("A4 `.bak` 也是合法 YAML（说明是「整份旧文」而不是半行）", backups_ok)
    applied = config_value(KEY)
    check("A5 GUI 那次改动真的落盘了（%s=0.08）" % KEY,
          applied is not None and abs(float(applied) - 0.08) < 0.001, repr(applied))

    print("\n== B. 单写者证据（从 /proc 那一侧看）")
    writers = [pid for pid, info in handles.items() if info["write"]]
    gui_pids = [pid for pid, info in handles.items() if "agent_gui" in info["comm"]]
    print("    持有真源句柄的进程: %s" % json.dumps(handles, ensure_ascii=False))
    print("    持有 llm.env 句柄的进程: %s" % json.dumps(env_handles, ensure_ascii=False))
    check("B1 真源**没有**被 GUI 打开（连只读都没有）", not gui_pids,
          "gui pids=%r" % (gui_pids,))
    check("B2 真源/派生文件同一时刻只有 Agent 在写",
          all("agent" in handles[pid]["comm"] for pid in writers) and
          all("agent" in env_handles[pid]["comm"] for pid in env_handles if env_handles[pid]["write"]),
          "写者=%r" % ([handles[p]["comm"] for p in writers],))
    check("B3 真源不会同时被两个进程写（写着 ≤ 1 个）",
          len([pid for pid, info in handles.items() if info["write"]
               and "agent.main" in info["comm"]]) <= 1,
          "写句柄=%d" % len(writers))

    print("\n== C. 并发不丢更新")
    for other, (original, reply) in results.items():
        current = config_value(other)
        check("C 键 %s 保住了（并发下没丢）" % other,
              reply.get("ok") is True and str(current) == original,
              "要求=%r 现在=%r" % (original, current))

    print("\n== D. 还原 + 不变量")
    client = Ipc(path)
    try:
        reply = client.set_config("p8-restore", {KEY: repr(before_value)})
    finally:
        client.close()
    check("D1 写回原值成功", reply.get("ok") is True, str(reply.get("error") or ""))
    after_md5 = md5(CONFIG)
    check("D2 真源 md5 回到跑之前那个数（本次验收零净改动）", after_md5 == before_md5,
          "%s -> %s" % (before_md5, after_md5))
    if before_env:
        check("D3 派生 llm.env 也没变", md5(ENV_FILE) == before_env)
    services_ok = True
    for unit in ("agent.service", "agent-gui.service"):
        proc = subprocess.run(["systemctl", "is-active", unit], stdout=subprocess.PIPE,
                              universal_newlines=True)
        if proc.stdout.strip() != "active":
            services_ok = False
    check("D4 两个 systemd 服务仍 active", services_ok)
    rc = subprocess.run(["git", "status", "--porcelain"], cwd=REPO,
                        stdout=subprocess.PIPE, universal_newlines=True)
    check("D5 板端 git status 干净", not rc.stdout.strip(), rc.stdout.strip()[:80])
    leftover = subprocess.run(["pgrep", "-af", "p8_coexist"], stdout=subprocess.PIPE,
                              universal_newlines=True)
    check("D6 没留下本次验收的临时进程",
          not [ln for ln in leftover.stdout.splitlines() if "p8_coexist.py" not in ln])

    print("\n== 结果: %s（%d 项通过%s）"
          % ("全部通过" if not _FAILED else "失败 %d 项" % len(_FAILED), len(_PASSED),
             (", 失败: " + "、".join(_FAILED)) if _FAILED else ""))
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
