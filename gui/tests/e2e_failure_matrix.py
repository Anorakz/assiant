#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""e2e_failure_matrix.py — 异常态矩阵第 2 行的判据（T15-16 遗留②）

为什么必须走**端到端** ✓（第 119–120 轮查出来的 ✓）
---------------------------------------------------------------------------
  那句「Agent 没连上（命令没发出去）」的**信号接线**在
  `main_window.cpp` 的 `applyConfig()` 里（`:1179` 函数头、`:1195-1209` 接线 ✓）——
  而 `applyConfig()` 是**私有**的 ✗ ⇒ 单测里**裸造窗口**跑不到它 ✗
  ⇒ 信号没人接 ⇒ 界面静默（探针实测：`wifiRequested` 发了 1 次、提示 label 数 **0** ✓）。
  ⇒ 所以判据走**产品入口**：带 `--config` 启动 ✓ ⇒ `applyConfig()` 真的跑 ✓ ⇒ 接线成立 ✓
  ⇒ **不改任何产品 API** ✓✓。

判什么 ✓
---------------------------------------------------------------------------
  GUI 的 `--dump-wifi` 会把设置页**真实渲染出来**的两行打出来（`main.cpp:750-751` ✓）：
      WIFI_RESULT <设置页结果行>
      WIFI_STATUS <顶栏状态行>
  没 Agent 时，结果行里**必须**出现「命令没发出去」✓（实测见第 120 轮 ✓）。
  另外顺带看一条**同族**提示 ✓：`[ui] 输入源没能保存：Agent 没连上` ✓（白捡一格证据 ✓）。

⚠ 前提与诚实 ✗
---------------------------------------------------------------------------
  · 它测的是「**没有** Agent」这条路 ✓ ⇒ 若 `AGENT_SOCKET`（默认 `/tmp/agent.sock`）**存在** ✓
    （有真 Agent 在跑 ✓），命令会**发成功** ✗ ⇒ 这条判据**没意义** ✓ ⇒ 脚本**退出 77** ✓，
    由 CMake 的 `SKIP_RETURN_CODE 77` 标成 **Skipped** ✓ —— **绝不假绿** ✗。
  · 它**不启动**对端 ✓（与 `e2e_ipc.py` 相反 ✓）：这里要的**正是"没人接"** ✓。
"""
import argparse
import os
import subprocess
import sys

NEEDLE_RESULT = "命令没发出去"
NEEDLE_ALSO = "输入源没能保存"


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("--gui", required=True, help="agent_gui 可执行文件")
    ap.add_argument("--config",
                    default=os.path.join(here, "..", "..", "config", "config.example.yaml"),
                    help="仓库里那份模板 ✓（真 config.yaml 是每台机器自己的 ✓，CI 上没有 ✓）")
    ap.add_argument("--socket", default="/tmp/agent.sock", help="Agent 的 socket 路径（存在就 skip ✓）")
    ap.add_argument("--delay", type=int, default=1000, help="--screenshot-delay（--dump-wifi 会再等 8 秒 ✓）")
    args = ap.parse_args()

    if os.path.exists(args.socket):
        print("SKIP: %s 存在（有 Agent 在跑）—— 本条判据测的是**没有 Agent** 时的失败路径" % args.socket)
        return 77

    if not os.path.isfile(args.config):
        print("SKIP: 找不到配置 %s" % args.config)
        return 77

    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"          # 宿主没有 DSI，offscreen 足够 ✓
    env["AGENT_SOCKET"] = args.socket             # 让 GUI 也去找同一个（不存在的）socket ✓
    cmd = [args.gui, "--config", args.config, "--page", "settings",
           "--dump-wifi", "--screenshot-delay", str(args.delay)]
    print("跑: " + " ".join(cmd))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
    except subprocess.TimeoutExpired:
        print("FAIL: GUI 60 秒内没退出 ✗")
        return 1
    out = (proc.stdout or "") + (proc.stderr or "")

    result = [ln for ln in out.splitlines() if ln.startswith("WIFI_RESULT")]
    print("WIFI_RESULT 行：" + (result[0] if result else "（没有 ✗）"))

    problems = []
    if not result:
        problems.append("没有 WIFI_RESULT 行 ✗（--dump-wifi 没跑成？）")
    elif NEEDLE_RESULT not in result[0]:
        problems.append("结果行里没有「%s」✗：%s" % (NEEDLE_RESULT, result[0]))
    if NEEDLE_ALSO not in out:
        problems.append("同族提示「%s」也没出现 ✗" % NEEDLE_ALSO)

    if problems:
        for p in problems:
            print("FAIL: " + p)
        return 1
    print("PASS: 没 Agent 时，设置页把「%s」如实说出来了 ✓（同族提示也在 ✓）" % NEEDLE_RESULT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
