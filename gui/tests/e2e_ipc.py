#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
#  gui/tests/e2e_ipc.py — 任务4: C++ GUI ⇄ Python IPC server 双向联调
#
#  步骤 (与任务书一一对应)
#  ---------------------------------------------------------------------------
#      1. 起 Python Agent 的 IPC server (只起 IPC, 不起其他组件), 后台
#      2. 起 C++ GUI (后台, headless: QT_QPA_PLATFORM=offscreen)
#      3. 等待 2 秒
#      4. Python 侧 push 一条 status
#      5. 检查 C++ 侧日志中收到
#      6. C++ 侧发一条 switch_mode 命令
#      7. 检查 Python 侧日志中收到
#      8. 清理进程 + socket 文件
#
#  跑法
#  ---------------------------------------------------------------------------
#      cmake -S gui -B gui/build && cmake --build gui/build -j4
#      python3 gui/tests/e2e_ipc.py                 # 默认 /tmp/agent.sock
#      cd gui/build && ctest --output-on-failure     # 作为集成测试 (独立 socket)
#
#  实现说明
#  ---------------------------------------------------------------------------
#  · Python 侧是**独立进程**: `python3 -m agent.ipc.server --control-stdin`。
#    它的 stdin 就是控制通道 (push status / push llm <文本> / quit),
#    见 agent/ipc/server.py 的 --control-stdin。
#  · C++ 侧是独立进程: agent_gui <socket>。它的 stdin 就是命令通道
#    (普通文本 -> chat_input; @<action> <json> -> 原样发), 见 gui/src/main.cpp。
#  · 两个进程的 stdout+stderr 都由后台线程收集, "检查日志"就是对日志做包含断言。
#  · agent_gui 目前是 QCoreApplication (没有窗口), offscreen 设了也无害;
#    等 Widgets 主窗口落地后, 同一个脚本不用改就能真的 headless 跑起来。
# ============================================================================
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GUI = REPO_ROOT / "gui" / "build" / "agent_gui"
DEFAULT_SOCKET = "/tmp/agent.sock"

TOTAL_STEPS = 8
_step_no = 0


class StepFailed(Exception):
    """某一步没通过 —— 打印原因 + 两份日志后退出 1。"""


# ---------------------------------------------------------------------------
#  输出
# ---------------------------------------------------------------------------
def report(text: str) -> None:
    global _step_no
    _step_no += 1
    print("[%d/%d] %-50s" % (_step_no, TOTAL_STEPS, text), end=" ", flush=True)


def report_ok(detail: str = "") -> None:
    print("OK" + (("  " + detail) if detail else ""), flush=True)


def first_line_containing(text: str, needle: str) -> str:
    for line in text.splitlines():
        if needle in line:
            return line.strip()
    return ""


# ---------------------------------------------------------------------------
#  后台进程 + 日志收集
# ---------------------------------------------------------------------------
class Proc:
    """一个后台进程; stdout/stderr 合并后由后台线程逐行收集。"""

    def __init__(self, name: str, argv: List[str], env: Optional[dict] = None) -> None:
        self.name = name
        self.argv = argv
        self._lines: List[str] = []
        self._lock = threading.Lock()
        self.proc = subprocess.Popen(
            argv,
            cwd=str(REPO_ROOT),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self._thread = threading.Thread(target=self._pump, name="log-" + name, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            with self._lock:
                self._lines.append(line.rstrip("\n"))

    def log(self) -> str:
        with self._lock:
            return "\n".join(self._lines)

    def send(self, line: str) -> None:
        """往子进程 stdin 写一行 (等价于"在它的终端里敲回车")。"""
        assert self.proc.stdin is not None
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def wait_log(self, needle: str, timeout: float = 5.0) -> bool:
        """轮询直到日志里出现 needle (或进程退出 / 超时)。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if needle in self.log():
                return True
            if self.proc.poll() is not None and needle not in self.log():
                return False       # 进程都退了, 再等也没用
            time.sleep(0.05)
        return needle in self.log()

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        for stream in (self.proc.stdin, self.proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        self._thread.join(timeout=1.0)

    def log_tail(self, n: int = 8) -> str:
        lines = self.log().splitlines()
        return "\n".join(lines[-n:])


# ---------------------------------------------------------------------------
#  工具
# ---------------------------------------------------------------------------
def socket_in_use(path: str) -> bool:
    """路径上是不是真的有个进程在 listen (区分"活着的 Agent"和"崩溃残留")。"""
    if not os.path.exists(path):
        return False
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.3)
        probe.connect(path)
        return True
    except OSError:
        return False
    finally:
        probe.close()


# ---------------------------------------------------------------------------
#  主流程
# ---------------------------------------------------------------------------
def run(args: argparse.Namespace) -> int:
    gui_bin = Path(args.gui)
    if not gui_bin.exists():
        print("找不到 GUI 可执行文件: %s\n先构建: cmake -S gui -B gui/build && "
              "cmake --build gui/build -j4" % gui_bin, file=sys.stderr)
        return 2

    socket_path = args.socket
    if socket_in_use(socket_path):
        print("!! %s 上已经有进程在 listen (可能是真的 Agent), 先停掉它, "
              "或用 --socket 换一个路径" % socket_path, file=sys.stderr)
        return 2

    server: Optional[Proc] = None
    gui: Optional[Proc] = None

    try:
        # ---- 1) Python: 只起 IPC server ------------------------------------
        report("启动 Python IPC server (只起 IPC): %s" % socket_path)
        server = Proc(
            "py-ipc",
            [sys.executable, "-m", "agent.ipc.server",
             "--socket", socket_path, "--mode", "STUDY", "--control-stdin"],
        )
        if not server.wait_log("LISTENING", timeout=8):
            raise StepFailed("Python IPC server 没起来:\n" + server.log())
        report_ok("pid=%d" % server.proc.pid)

        # ---- 2) C++ GUI, headless ------------------------------------------
        report("启动 C++ GUI (headless, QT_QPA_PLATFORM=offscreen)")
        gui_env = os.environ.copy()
        gui_env["QT_QPA_PLATFORM"] = "offscreen"
        gui = Proc("cpp-gui", [str(gui_bin), socket_path], env=gui_env)
        if not gui.wait_log("[ipc] 已连接", timeout=8):
            raise StepFailed("C++ GUI 没连上 Python IPC server:\n" + gui.log())
        report_ok("pid=%d" % gui.proc.pid)

        # ---- 3) 等 2 秒 ----------------------------------------------------
        report("等待 2 秒")
        time.sleep(2.0)
        report_ok()

        # ---- 4) Python push 一条 status ------------------------------------
        report("Python 侧 push 一条 status")
        server.send("push status")
        if not server.wait_log("push status", timeout=5):
            raise StepFailed("push 控制命令没被执行:\n" + server.log())
        report_ok()

        # ---- 5) 检查 C++ 侧收到 --------------------------------------------
        report("检查 C++ 侧日志中收到 status")
        if not gui.wait_log("[recv] status", timeout=5):
            raise StepFailed("C++ 侧没有收到 status:\n" + gui.log())
        status_line = first_line_containing(gui.log(), "[recv] status")
        if '"mode":"STUDY"' not in status_line:
            raise StepFailed("status 内容不对: %s" % status_line)
        report_ok(status_line)

        # ---- 6) C++ 发一条 switch_mode -------------------------------------
        report("C++ 侧发一条 switch_mode 命令")
        gui.send('@switch_mode {"value":"GAME"}')
        if not gui.wait_log("[ipc] 发送", timeout=3):
            raise StepFailed("C++ 侧没有发出命令:\n" + gui.log())
        report_ok()

        # ---- 7) 检查 Python 侧收到 -----------------------------------------
        report("检查 Python 侧日志中收到 switch_mode")
        if not server.wait_log("action=switch_mode", timeout=5):
            raise StepFailed("Python 侧没有收到 switch_mode:\n" + server.log())
        cmd_line = first_line_containing(server.log(), "action=switch_mode")
        if "GAME" not in cmd_line:
            raise StepFailed("switch_mode 内容不对: %s" % cmd_line)
        report_ok(cmd_line)

    except StepFailed as exc:
        print("FAIL", flush=True)
        print("\n----- 失败原因 -----\n%s" % exc, flush=True)
        _dump_logs(gui, server)
        return 1
    finally:
        # ---- 8) 清理 (无论成功失败都要清) ---------------------------------
        show_step = _step_no >= 7
        if show_step:
            report("清理进程 + socket 文件")
        cleaned = _cleanup(gui, server, socket_path)
        if show_step:
            report_ok(cleaned)

    if args.verbose:
        _dump_logs(gui, server)

    print("\n=== PASS: 双向联调通过 "
          "(Python push status -> C++ 收到; C++ switch_mode -> Python 收到) ===")
    return 0


def _cleanup(gui: Optional[Proc], server: Optional[Proc], socket_path: str) -> str:
    detail = []
    for name, proc in (("C++ GUI", gui), ("Python IPC", server)):
        if proc is None:
            continue
        proc.stop()
        detail.append("%s(pid=%d) 已退出" % (name, proc.proc.pid))
    try:
        if os.path.exists(socket_path):
            os.remove(socket_path)
            detail.append("socket 已删")
    except OSError as exc:
        detail.append("socket 删除失败: %r" % (exc,))
    return "; ".join(detail)


def _dump_logs(gui: Optional[Proc], server: Optional[Proc]) -> None:
    print("\n===== C++ GUI 日志 =====")
    print(gui.log() if gui is not None else "(未启动)")
    print("\n===== Python IPC 日志 =====")
    print(server.log() if server is not None else "(未启动)")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="任务4: C++ GUI ⇄ Python IPC server 双向联调")
    parser.add_argument("--gui", default=str(DEFAULT_GUI),
                        help="agent_gui 可执行文件路径 (默认 %s)" % DEFAULT_GUI)
    parser.add_argument("--socket", default=DEFAULT_SOCKET,
                        help="IPC socket 路径 (默认 %s)" % DEFAULT_SOCKET)
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="通过后也打印两份日志")
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
