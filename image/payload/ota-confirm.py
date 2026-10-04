#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ota-confirm.py — OTA 之后的"这次算成功吗"（T15-14-a）

## 为什么要有它（和 `ab-mark.py` 的分工）

`ab-mark.py` 只会"把**当前**槽标成功" ✓ —— 它不问系统到底好不好。
而 A/B 的规矩是：**新槽起来了、而且真的能用，才算成功**；不确认就让 `tries_remaining`
继续递减，直到引导器判它不可信、**自动回退到旧槽** ✓（这条回退 2026-10-04 被真实观察到 ✓）。

所以这个脚本负责"判定"，判过了才去调 `ab-mark.py` ：

    判据（本项目约定）：`agent` 与 `agent-gui` 都 active ✓ 且 **IPC 通** ✓

任何一条不满足（或超时，默认 90 s）⇒ **什么都不标** ✗ ⇒ 天然的失败回退 ✓。

## 用法

    ota-confirm.py [--timeout 90] [--socket /tmp/agent.sock] [--json <结果文件>]

退出码：0 确认成功；1 判定不通过（超时/服务不 active/IPC 不通）；2 用法或环境问题。

幂等：结果文件里已是 `"ok": true` 就直接返回 0 ✓（重启后再跑一次不会重复标）。
"""
import argparse
import json
import os
import subprocess
import sys
import time

DEFAULT_SOCKET = "/tmp/agent.sock"
DEFAULT_JSON = "/data/assistant/ota/confirm.json"
REQUIRED_SERVICES = ("agent", "agent-gui")
MARK_TOOL = "/usr/lib/assistant/ab-mark.py"


def log(text: str) -> None:
    print("   %s" % text, flush=True)


def services_active(units=REQUIRED_SERVICES) -> bool:
    """两个服务是不是都 active（`systemctl is-active` 一次问完 ✓）。"""
    try:
        out = subprocess.run(["systemctl", "is-active", *units],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        log("问服务状态失败：%s" % exc)
        return False
    states = out.stdout.split()
    log("%s -> %s" % ("/".join(units), " ".join(states) or "（无输出）"))
    return len(states) >= len(units) and all(item == "active" for item in states)


def ipc_ok(socket_path: str, timeout_s: int = 8) -> bool:
    """IPC 通不通：直接跑 `assistant status`（用户向入口，最贴近"系统真的能用" ✓）。

    @note 不去 import 仓库内部模块：板上跑的是装在 `/usr/lib/assistant` 的那一份，
          而且 `assistant` 这个入口本身就会连 socket ✓（输出里有"已连上 Agent" ✓）。
    """
    if not os.path.exists(socket_path):
        log("socket 不在：%s" % socket_path)
        return False
    try:
        out = subprocess.run(["assistant", "status", "--timeout", str(timeout_s)],
                             capture_output=True, text=True, timeout=timeout_s + 5)
    except (OSError, subprocess.SubprocessError) as exc:
        log("跑 assistant status 失败：%s" % exc)
        return False
    text = (out.stdout or "") + (out.stderr or "")
    connected = "已连上" in text or "agent.sock" in text
    log("IPC：%s" % ("通 ✓" if connected else "不通 ✗（%s）" % text.strip().splitlines()[:1]))
    return connected


def mark_current_slot() -> bool:
    """判过了才标当前槽成功（`ab-mark.py` 幂等 ✓）。"""
    for candidate in (MARK_TOOL, "/data/assistant/ota/bin/ab-mark.py"):
        if os.path.isfile(candidate):
            try:
                out = subprocess.run(["python3", candidate], capture_output=True,
                                     text=True, timeout=30)
            except (OSError, subprocess.SubprocessError) as exc:
                log("调 ab-mark 失败：%s" % exc)
                return False
            tail = (out.stdout or "").strip().splitlines()[-1:]
            log("ab-mark：%s" % (tail[0] if tail else "（无输出）"))
            return out.returncode == 0
    log("找不到 ab-mark.py（既不在 %s 也不在 /data）" % MARK_TOOL)
    return False


def write_result(path: str, payload: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        log("结果写进 %s" % path)
    except OSError as exc:                       # 写不了结果不该影响评判
        log("（结果没写下来：%s）" % exc)


def already_confirmed(path: str) -> bool:
    try:
        with open(path, encoding="utf-8") as handle:
            return bool(json.load(handle).get("ok"))
    except (OSError, ValueError):
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=int, default=90, help="总超时（秒），默认 90")
    ap.add_argument("--socket", default=DEFAULT_SOCKET)
    ap.add_argument("--json", default=DEFAULT_JSON)
    ap.add_argument("--force", action="store_true", help="忽略已有的成功结果，重判一次")
    args = ap.parse_args()

    if not args.force and already_confirmed(args.json):
        log("%s 里已经是 ok ✓（幂等，不再重判）" % args.json)
        return 0

    started = time.time()
    deadline = started + max(5, args.timeout)
    ok = False
    while time.time() < deadline:
        if services_active() and ipc_ok(args.socket):
            ok = True
            break
        time.sleep(3)

    elapsed = round(time.time() - started, 1)
    payload = {"ok": ok, "elapsed_s": elapsed, "timeout_s": args.timeout,
               "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    if ok:
        # ⚠ 判定通过 ≠ 确认成功：**标成功那一步也必须成** ✓
        #   （原来这里写成 `payload["marked"] or ok` —— 标记失败还报成功 = 谎报 ✗，
        #    单测 test_mark_failure_is_reported_as_failure 当场把它揪出来了 ✓）
        marked = mark_current_slot()
        payload["marked"] = marked
        payload["ok"] = bool(marked)
        log("判定：通过 ✓（%.1fs）标成功：%s" % (elapsed, "成 ✓" if marked else "败 ✗"))
    else:
        log("判定：不通过 ✗（等满 %ds）—— 不标成功，让 tries 继续递减以便自动回退" % args.timeout)
    write_result(args.json, payload)
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
