#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T14-8 门禁实测：崩溃日志**真的**留下现场（真 Agent / 真 GUI / 真信号）。

验的是"出事之后能查"这条承诺，全部用真二进制、真信号：

  A. Agent（`python3 -m agent.main`）
     · 干净跑一次（`--check-config`）→ logs/crash/ 里**什么都不该留**；
     · `--crash-demo raise`（主线程未捕获异常）→ 报告里有原因 / traceback /
       pid / 命令行 / git 版本 / **崩之前那条日志**；
     · `--crash-demo thread`（子线程未捕获异常）→ 报告点明是哪个线程；
     · `--crash-demo segv`（真 SIGSEGV）→ faulthandler 段（`Fatal Python error` + 线程栈）；
     · 再起一次 → 启动横幅里带上一份的头尾；**同一份只报一次**；
     · 起一个真 Agent，`kill -USR1` → 会话文件里出现全线程栈（按需取证）。

  B. GUI（`gui/build/agent_gui`，offscreen）
     · 干净跑一次 → 不留文件；
     · `--crash-demo fatal` → 报告里有 qFatal 原因 + **最近若干条 Qt 消息**；
     · `--crash-demo segv` → 裸信号处理器写下 `致命信号 : SIGSEGV` + 最近消息；
     · 再起一次 → 横幅带上一份的原因；同一份只报一次；
     · 不带 `AGENT_CRASH_DIR` 跑一次 → 默认目录必须落在 `<仓库根>/logs/crash`
       （与 Agent 侧同一处），且这个目录被 git 忽略。

  C. 不变量：真实 `config/config.yaml` md5 未变、`git status` 干净、
     systemd 的 Agent / GUI 仍 active（T14-7）、本次验收没留下临时进程、
     保留份数真的有限（造 6 份、KEEP=3 → 剩 3）。

跑法（板端，仓库根）::

    python3 tests/board/t14_8_accept.py
"""
from __future__ import annotations

import hashlib
import os
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


def check(name, ok, detail=""):
    (_PASSED if ok else _FAILED).append(name)
    print("  %s %s%s" % ("✔" if ok else "✘", name, ("  —— %s" % detail) if detail else ""))
    return ok


def md5(path):
    with open(path, "rb") as handle:
        return hashlib.md5(handle.read()).hexdigest()


def reports(crash_dir):
    return sorted(f for f in os.listdir(crash_dir)
                  if f.endswith(".log") and not f.startswith("."))


def read(path):
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def latest_report(crash_dir):
    """最新一份报告的 (文件名, 正文)；没有就 (None, "")。"""
    names = reports(crash_dir)
    if not names:
        return None, ""
    name = names[-1]
    return name, read(os.path.join(crash_dir, name))


def run_agent(args, crash_dir, timeout=90, extra_env=None, crash_disable=False):
    """起一次真 Agent。返回 CompletedProcess（stdout+stderr 合并）。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    if crash_disable:
        env["AGENT_CRASH_DISABLE"] = "1"
    elif crash_dir:
        env["AGENT_CRASH_DIR"] = crash_dir
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-m", "agent.main"] + args,
        cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        encoding="utf-8", errors="replace", timeout=timeout, env=env,
    )


def run_gui(args, crash_dir, timeout=120, extra_env=None):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    if crash_dir:
        env["AGENT_CRASH_DIR"] = crash_dir
    env.update(extra_env or {})
    return subprocess.run(
        [GUI] + args, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        encoding="utf-8", errors="replace", timeout=timeout, env=env,
    )


# ---------------------------------------------------------------------------
#  A. Agent
# ---------------------------------------------------------------------------
def check_agent(crash_dir):
    print("\n== A. Agent 崩溃日志")

    # A1 干净退出不留文件
    proc = run_agent(["--check-config"], crash_dir)
    ok = proc.returncode == 0 and not reports(crash_dir)
    check("A1 干净跑一次 → logs/crash/ 不留文件", ok,
          "rc=%s 留下=%r" % (proc.returncode, reports(crash_dir)))

    # A2 主线程未捕获异常
    proc = run_agent(["--crash-demo", "raise"], crash_dir)
    names = reports(crash_dir)
    _, text = latest_report(crash_dir)
    check("A2 raise → 留下一份报告且退出码非 0", len(names) == 1 and proc.returncode != 0,
          "rc=%s 文件=%r 输出尾部=%r" % (proc.returncode, names, proc.stdout[-160:]))
    for needle, why in (
        ("未捕获异常（主线程）", "原因"),
        ("RuntimeError", "异常类型"),
        ("崩溃日志自检（--crash-demo raise）", "自检文本"),
        ("Traceback (most recent call last)", "traceback"),
        ("pid=", "pid"),
        ("命令行", "argv"),
        ("版本", "git 版本"),
        ("--- 最近", "最近日志段"),
        ("崩溃自检：制造主线程未捕获异常", "崩之前那条日志"),
    ):
        check("A2 报告里有%s" % why, needle in text)

    # A3 子线程
    proc = run_agent(["--crash-demo", "thread"], crash_dir)
    _, text = latest_report(crash_dir)
    check("A3 thread → 报告点明线程名", "线程 crash-demo" in text, "rc=%s" % proc.returncode)

    # A4 真 SIGSEGV（faulthandler）
    proc = run_agent(["--crash-demo", "segv"], crash_dir)
    _, text = latest_report(crash_dir)
    check("A4 segv → 退出码是信号（非 0）", proc.returncode != 0, "rc=%s" % proc.returncode)
    check("A4 报告里有 faulthandler 段", "Fatal Python error" in text)
    check("A4 faulthandler 段有线程栈", "Current thread" in text)

    # A5 启动横幅（同一份只报一次）
    before = len(reports(crash_dir))
    first = run_agent(["--check-config"], crash_dir)
    banner_line = [ln for ln in first.stdout.splitlines() if "上次崩溃报告" in ln]
    check("A5 启动横幅指出上一份报告", bool(banner_line),
          banner_line[0][:100] if banner_line else first.stdout[-200:])
    # 上一份是 A4 的 SIGSEGV → 它的现场是 faulthandler 段（不是"崩溃/异常"那一段）
    check("A5 横幅里带上次的现场", "上次没有干净退出" in first.stdout
          and ("Fatal Python error" in first.stdout or "崩溃/异常" in first.stdout),
          first.stdout[-200:])
    second = run_agent(["--check-config"], crash_dir)
    check("A5 同一份不重复报（.last-reported）", "上次崩溃报告" not in second.stdout)
    check("A5 报告份数没有因为重启而变", len(reports(crash_dir)) == before,
          "%d -> %d" % (before, len(reports(crash_dir))))

    # A6 按需全线程栈（kill -USR1）
    # 这里**不**起完整的 Agent：那会去抢 /tmp/agent.sock（systemd 的 Agent 正在用）。
    # 只需要"装了崩溃日志的进程"，所以用一小段引导脚本，让它自己把会话文件路径打出来。
    bootstrap = (
        "import sys, time\n"
        "sys.path.insert(0, %r)\n"
        "from agent.core.crash_log import install_crash_logging\n"
        "logger = install_crash_logging(app='agent')\n"
        "print('SESSION_FILE=%%s' %% logger.path, flush=True)\n"
        "while True: time.sleep(0.5)\n" % REPO
    )
    env = dict(os.environ)
    env["AGENT_CRASH_DIR"] = crash_dir
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen([sys.executable, "-c", bootstrap], cwd=REPO,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            encoding="utf-8", errors="replace", env=env)
    session_path = ""
    deadline = time.time() + 20
    while time.time() < deadline:
        line = proc.stdout.readline()
        if line.startswith("SESSION_FILE="):
            session_path = line.split("=", 1)[1].strip()
            break
    got_stack = False
    try:
        if session_path:
            os.kill(proc.pid, signal.SIGUSR1)
            time.sleep(1.5)
            text = read(session_path)
            got_stack = "Thread 0x" in text or "Current thread" in text
    finally:
        proc.kill()
        proc.wait(timeout=20)
        if session_path and os.path.exists(session_path):
            os.remove(session_path)          # 这份是 SIGUR1 取证用的，不当验收残留
    check("A6 kill -USR1 → 会话文件里出现全线程栈", got_stack,
          "文件=%r" % (os.path.basename(session_path) if session_path else None,))


# ---------------------------------------------------------------------------
#  B. GUI
# ---------------------------------------------------------------------------
def check_gui(crash_dir):
    print("\n== B. GUI 崩溃日志")
    for name in reports(crash_dir):
        os.remove(os.path.join(crash_dir, name))

    # B1 干净退出不留文件
    proc = run_gui(["--page", "home", "--dump-layout", "--screenshot-delay", "300"], crash_dir)
    check("B1 GUI 干净跑一次 → 不留文件",
          not reports(crash_dir), "rc=%s 留下=%r" % (proc.returncode, reports(crash_dir)))

    # B2 qFatal
    proc = run_gui(["--crash-demo", "fatal"], crash_dir)
    names = reports(crash_dir)
    _, text = latest_report(crash_dir)
    check("B2 fatal → 留下一份报告且退出码非 0", len(names) == 1 and proc.returncode != 0,
          "rc=%s 文件=%r 输出尾部=%r" % (proc.returncode, names, proc.stdout[-160:]))
    for needle, why in (
        ("qFatal: 崩溃日志自检（--crash-demo fatal）", "原因"),
        ("--- 最近", "最近消息段"),
        ("[gui] 崩溃日志:", "崩之前的日志（环形缓冲真在工作）"),
        ("pid=", "pid"),
        ("Qt       :", "Qt 版本"),
    ):
        check("B2 报告里有%s" % why, needle in text)

    # B3 信号（裸处理器）
    proc = run_gui(["--crash-demo", "segv"], crash_dir)
    _, text = latest_report(crash_dir)
    check("B3 segv → 退出码是信号（非 0）", proc.returncode != 0, "rc=%s" % proc.returncode)
    check("B3 报告里有致命信号段", "致命信号   : SIGSEGV" in text)
    check("B3 信号段带了最近消息", "[gui]" in text)

    # B4 横幅
    first = run_gui(["--page", "home", "--dump-layout", "--screenshot-delay", "300"], crash_dir)
    check("B4 启动横幅指出上一份报告", "上次崩溃报告" in first.stdout,
          [ln for ln in first.stdout.splitlines() if "上次" in ln][:1])
    check("B4 横幅带上一份的原因", "SIGSEGV" in first.stdout or "致命信号" in first.stdout)
    second = run_gui(["--page", "home", "--dump-layout", "--screenshot-delay", "300"], crash_dir)
    check("B4 同一份不重复报", "上次崩溃报告" not in second.stdout)

    # B5 默认目录（不带 AGENT_CRASH_DIR）：必须是 <仓库根>/logs/crash
    env = dict(os.environ)
    env.pop("AGENT_CRASH_DIR", None)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    env["PYTHONIOENCODING"] = "utf-8"
    want = os.path.join(REPO, "logs", "crash")
    os.makedirs(want, exist_ok=True)
    before_default = set(os.listdir(want))
    proc = subprocess.run([GUI, "--page", "home", "--dump-layout", "--screenshot-delay", "300"],
                          cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          encoding="utf-8", errors="replace", timeout=120, env=env)
    lines = [ln for ln in proc.stdout.splitlines() if "[gui] 崩溃日志:" in ln]
    check("B5 默认崩溃目录 = <仓库根>/logs/crash", bool(lines) and want + os.sep in lines[0],
          (lines[0] if lines else proc.stdout[-200:]))
    ignored = subprocess.run(["git", "check-ignore", "-v", "logs/crash/probe.log"],
                             cwd=REPO, stdout=subprocess.PIPE, universal_newlines=True)
    check("B5 logs/crash 被 git 忽略", ignored.returncode == 0, ignored.stdout.strip()[:80])
    os.makedirs(want, exist_ok=True)
    # ⚠ 不能断言"目录里没有 gui-*.log"：systemd 的 GUI 服务**正在跑**，它那一份会话文件
    #    本来就在（只有干净退出才删）。要判的是"我们这一次跑完没留下东西"。
    after = set(os.listdir(want))
    check("B5 这次干净退出没在默认目录留下文件", after == before_default,
          "新增=%r" % (sorted(after - before_default),))

    # B6 SIGTERM（systemd stop / ctest 的 terminate）要走干净退出，不能留"假崩溃"
    #    板端实测过：没有这条时，`Restart=always` 的服务每 stop/restart 一次就多一份
    #    只有表头的 gui-*.log，跑几轮 ctest 就攒了 5 份。
    def _gui_count():
        return len([f for f in os.listdir(want) if f.startswith("gui-")])

    def _systemctl(*args):
        return subprocess.run(["systemctl"] + list(args), stdout=subprocess.PIPE,
                              universal_newlines=True).returncode

    os.makedirs(want, exist_ok=True)
    _systemctl("restart", "agent-gui.service")
    time.sleep(6)
    running = _gui_count()
    stopped_ok = _systemctl("stop", "agent-gui.service") == 0
    time.sleep(4)
    after_stop = _gui_count()
    check("B6 停止 GUI 服务（SIGTERM）后它自己的会话文件被删掉",
          stopped_ok and running == 1 and after_stop == 0,
          "运行中=%d 停止后=%d" % (running, after_stop))
    _systemctl("start", "agent-gui.service")
    time.sleep(6)
    check("B6 起回来后服务正常（只有它自己那一份会话文件）",
          subprocess.run(["systemctl", "is-active", "agent-gui.service"],
                         stdout=subprocess.PIPE, universal_newlines=True).stdout.strip() == "active"
          and _gui_count() == 1,
          "文件数=%d" % _gui_count())


# ---------------------------------------------------------------------------
#  C. 不变量
# ---------------------------------------------------------------------------
def check_invariants(crash_dir, real_before):
    print("\n== C. 不变量")
    for name, digest in real_before.items():
        check("真实文件 %s 未变" % name, md5(os.path.join(REPO, name)) == digest,
              md5(os.path.join(REPO, name)))

    # 保留份数：造 6 份、KEEP=3
    keep_dir = os.path.join(TMP, "retention")
    os.makedirs(keep_dir, exist_ok=True)
    now = time.time()
    for i in range(6):
        path = os.path.join(keep_dir, "old-%02d.log" % i)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("x")
        os.utime(path, (now - 600 + i * 10,) * 2)
    run_agent(["--check-config"], keep_dir, extra_env={"AGENT_CRASH_KEEP": "3"})
    left = reports(keep_dir)
    check("C5 保留份数有限（6 份 + KEEP=3 → 3 份）", len(left) == 3, "%r" % (left,))
    check("C5 留下的是最新的那几份",
          "old-05.log" in left and "old-00.log" not in left, "%r" % (left,))

    rc = subprocess.run(["git", "status", "--porcelain"], cwd=REPO, stdout=subprocess.PIPE,
                        universal_newlines=True)
    check("板端 git status 干净", rc.returncode == 0 and not rc.stdout.strip(),
          rc.stdout.strip()[:80])

    services_ok = True
    for unit in ("agent.service", "agent-gui.service"):
        proc = subprocess.run(["systemctl", "is-active", unit], stdout=subprocess.PIPE,
                              universal_newlines=True)
        if proc.stdout.strip() != "active":
            services_ok = False
    check("常驻服务仍 active（T14-7：Agent / GUI 由 systemd 管）", services_ok)

    stray = []
    for pattern in (os.path.join(TMP, "agent"), "moonlight"):
        proc = subprocess.run(["pgrep", "-af", pattern], stdout=subprocess.PIPE,
                              universal_newlines=True)
        if proc.stdout.strip():
            stray.append(pattern)
    check("本次验收的临时进程都收掉了", not stray, "；".join(stray))


def main():
    global TMP
    TMP = tempfile.mkdtemp(prefix="t14-8-")
    crash_dir = os.path.join(TMP, "crash")
    os.makedirs(crash_dir, exist_ok=True)

    print("== T14-8 崩溃日志验收（真 Agent / 真 GUI / 真信号）")
    real_before = {name: md5(os.path.join(REPO, name)) for name in REAL_FILES
                   if os.path.exists(os.path.join(REPO, name))}
    for name, digest in real_before.items():
        print("  %-28s %s（跑完必须一样）" % (name, digest))
    print("== 临时崩溃目录: %s" % crash_dir)

    try:
        check_agent(crash_dir)
        check_gui(crash_dir)
        check_invariants(crash_dir, real_before)
    finally:
        print("\n== 结果: %s（%d 项通过%s）"
              % ("全部通过" if not _FAILED else "失败 %d 项" % len(_FAILED), len(_PASSED),
                 (", 失败: " + "、".join(_FAILED)) if _FAILED else ""))
        shutil.rmtree(TMP, ignore_errors=True)
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
