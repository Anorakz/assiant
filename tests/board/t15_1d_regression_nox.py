#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T15-1 1d 回归：**无 X 形态**下跑完整功能回归清单 + 空闲 CPU/内存数字。

为什么这么设计
  · T14 那几个验收脚本（t14_3 / t14_8 / t14_9 / p8_coexist）本来就用
    `QT_QPA_PLATFORM=offscreen` 驱动 GUI —— 它们**不依赖 X**，但都会检查
    `agent-gui.service` 是 active。所以"无 X 回归"必须让常驻 GUI **真的以无 X 形态在跑**：
    测试期给 `agent-gui.service` 加一个 drop-in（`QT_QPA_PLATFORM=eglfs` 等），
    停掉 slim，再把服务起起来 —— 这样服务是真的、平台也是真的。
  · 页面回归（home/model/system/settings）必须在 EGLFS 下**独占 DRM** 跑，所以那一步
    要先把常驻 GUI 停掉，跑完再起回来。
  · 空闲数字用 /proc 自己采（GUI / Agent 进程 + 整机），不用额外依赖。

顺序
  0  基线（config md5 / 服务状态 / free）
  1  装 drop-in -> 停 agent-gui、停 slim -> 确认 Xorg=0
  2  起 agent-gui（EGLFS 无 X）-> 确认 active + 真的加载了 libqeglfs
  3  T14 四个验收脚本（offscreen，服务在跑）—— 逐个记 rc 与"== 结果"行
  4  停服务 -> 四页截图（EGLFS 独占）-> 起服务
  5  空闲采样 60s（整机 CPU / GUI RSS+CPU / Agent RSS）
  6  收尾：删 drop-in、复原桌面（slim -> xrandr-startup -> agent-gui）+ 校验 md5
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time

REPO = "/home/kickpi/myproject/assitant"
GUI = os.path.join(REPO, "gui", "build", "agent_gui")
CFG = os.path.join(REPO, "config", "config.yaml")
UNIT = "/etc/systemd/system/agent-gui.service"
UNIT_BAK = "/tmp/agent-gui.service.bak"
NOX_UNIT = os.path.join(REPO, "systemd", "agent-gui-nox.service")
PAGES = ("home", "model", "system", "settings")
T14_SCRIPTS = ("t14_3_accept.py", "t14_8_accept.py", "t14_9_accept.py", "p8_coexist.py")
IDLE_SECONDS = 60
IDLE_STEP = 5.0

def say(msg):
    print(msg, flush=True)


def run(cmd, timeout=300, env=None):
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             universal_newlines=True, timeout=timeout, env=env,
                             cwd=REPO)
        return res.returncode, res.stdout or ""
    except subprocess.TimeoutExpired as exc:
        return 124, "命令超时 %ds：%s\n%s" % (timeout, " ".join(cmd), exc.output or "")
    except OSError as exc:
        return 127, "起不来：%s" % exc


def systemctl(*args, **kw):
    return run(["systemctl", *args], timeout=kw.get("timeout", 60))[0]


def is_active(unit):
    return run(["systemctl", "is-active", unit], timeout=20)[1].strip()


def x_ready():
    try:
        res = subprocess.run(["timeout", "3", "xrandr", "--query"],
                             env=dict(os.environ, DISPLAY=":0"),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
        return res.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def heal_stale_x_lock():
    xorg = run(["pgrep", "-c", "Xorg"], timeout=20)[1].strip() or "0"
    if xorg != "0" or not os.path.exists("/tmp/.X0-lock"):
        return
    say("    发现陈旧 /tmp/.X0-lock -> 清掉再起 slim")
    for path in ("/tmp/.X0-lock", "/tmp/.X11-unix/X0"):
        try:
            os.unlink(path)
        except OSError:
            pass


def main_pid(unit):
    text = run(["systemctl", "show", "-p", "MainPID", unit], timeout=20)[1]
    m = re.search(r"MainPID=(\d+)", text)
    return int(m.group(1)) if m else 0


def proc_stat(pid):
    """返回 (cpu_seconds, rss_kb)。读不到返回 (None, None)。"""
    try:
        with open("/proc/%d/stat" % pid) as handle:
            parts = handle.read().rsplit(")", 1)[1].split()
        ticks = int(parts[11]) + int(parts[12])          # utime + stime
        hz = os.sysconf("SC_CLK_TCK")
        cpu = ticks / float(hz)
        rss = 0
        with open("/proc/%d/status" % pid) as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1])
                    break
        return cpu, rss
    except (OSError, IndexError, ValueError):
        return None, None


def sys_cpu():
    """返回 (busy_total, idle) 两个累计值（jiffies）。"""
    with open("/proc/stat") as handle:
        parts = handle.readline().split()[1:]
    vals = [int(p) for p in parts]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return sum(vals), idle


def top_consumers(seconds=5.0, limit=5):
    """按 /proc/<pid>/stat 的 CPU 增量挑最忙的几个进程（不依赖 top 的输出格式）。"""
    def snapshot():
        data = {}
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                with open("/proc/%d/stat" % pid) as handle:
                    parts = handle.read().rsplit(")", 1)[1].split()
                data[pid] = (int(parts[11]) + int(parts[12]),
                             parts[0].strip(), read_cmdline(pid))
            except (OSError, IndexError, ValueError):
                continue
        return data

    def read_cmdline(pid):
        try:
            with open("/proc/%d/cmdline" % pid, "rb") as handle:
                return handle.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()[:60]
        except OSError:
            return "?"

    first = snapshot()
    time.sleep(seconds)
    second = snapshot()
    hz = float(os.sysconf("SC_CLK_TCK"))
    rows = []
    for pid, (ticks, name, cmd) in second.items():
        if pid == os.getpid() or pid not in first:
            continue
        delta = ticks - first[pid][0]
        if delta > 0:
            rows.append((100.0 * delta / hz / seconds, pid, name, cmd))
    rows.sort(reverse=True)
    return rows[:limit]


def png_stats(path):
    """(宽, 高, 不同颜色数, 强边界数)。解码失败给 (0,0,0,0)。"""
    try:
        size = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v",
                               "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
                              stdout=subprocess.PIPE, universal_newlines=True, timeout=30).stdout
        w, h = [int(v) for v in size.strip().split(",")]
        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "rawvideo",
                              "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=90).stdout
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0, 0, 0, 0
    colors, edges = set(), 0
    for y in range(0, h, 6):
        base = y * w * 3
        for x in range(0, w - 6, 6):
            o = base + x * 3
            colors.add(raw[o:o + 3])
            if (abs(raw[o] - raw[o + 6]) + abs(raw[o + 1] - raw[o + 7])
                    + abs(raw[o + 2] - raw[o + 8])) > 90:
                edges += 1
    return w, h, len(colors), edges


def restore_desktop():
    say("[收尾] 还原 agent-gui.service（X 形态）+ 复原桌面")
    if os.path.exists(UNIT_BAK):
        subprocess.run(["cp", UNIT_BAK, UNIT], timeout=30)
    # 兜底：万一还留着测试期的 drop-in，一并清掉
    dropin_dir = UNIT + ".d"
    if os.path.isdir(dropin_dir):
        for name in os.listdir(dropin_dir):
            try:
                os.unlink(os.path.join(dropin_dir, name))
            except OSError:
                pass
        try:
            os.rmdir(dropin_dir)
        except OSError:
            pass
    systemctl("daemon-reload")
    systemctl("stop", "agent-gui.service")
    heal_stale_x_lock()
    systemctl("start", "slim.service")
    for _ in range(20):
        time.sleep(1)
        if x_ready():
            break
    if x_ready():
        systemctl("restart", "xrandr-startup.service")
        time.sleep(3)
    else:
        say("    !! X 没就绪：跳过转屏（单元自身带 timeout，不会卡住 boot）")
    systemctl("restart", "agent-gui.service")
    time.sleep(10)
    rot = run(["bash", "-lc",
               "DISPLAY=:0 timeout 5 xrandr --query 2>/dev/null"
               " | grep -oE 'DSI-1 connected [0-9x+]+ [a-z]+'"], timeout=30)[1].strip()
    say("[收尾] slim=%s agent=%s gui=%s xrandr=%s"
        % (is_active("slim.service"), is_active("agent.service"),
           is_active("agent-gui.service"), rot or "(xrandr 没答复/超时)"))


def main():
    checks = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok)))
        say("  [%s] %s%s" % ("✔" if ok else "✘", name, (" —— " + detail) if detail else ""))
        return bool(ok)

    md5_before = run(["md5sum", CFG], timeout=20)[1].split()[0]
    say("== T15-1 1d：无 X 形态的功能回归 + 空闲数字")
    say("[0] config.yaml md5（前）=%s" % md5_before)
    say("[0] free：%s" % run(["bash", "-lc", "free -m | sed -n 2p"], timeout=20)[1].strip())

    abort = 0
    gui_svc_active_before = is_active("agent-gui.service") == "active"
    try:
        # ---- 1. 切到无 X 形态 ----
        say("[1] 用 systemd/agent-gui-nox.service 顶替 agent-gui.service（无 X 形态）-> 停 slim")
        # ⚠ 不能只加 drop-in：X 形态的单元写着 `Requires=display-manager.service`，
        #   启动时会把 X 一起拉起来（实测 pgrep Xorg 0 -> 1），于是 EGLFS 跟 Xorg 抢
        #   DRM master，t14_8 的"停服务后会话文件被删掉"会假失败。drop-in 也删不掉
        #   Requires/After（systemd 245 的 After= 清空不生效），所以整份替换、跑完还原。
        subprocess.run(["cp", UNIT, UNIT_BAK], timeout=30)
        subprocess.run(["cp", NOX_UNIT, UNIT], timeout=30)
        systemctl("daemon-reload")
        systemctl("stop", "agent-gui.service")
        systemctl("stop", "slim.service")
        systemctl("stop", "xrandr-startup.service")
        time.sleep(5)
        xorg_now = run(["pgrep", "-c", "Xorg"], timeout=20)[1].strip() or "0"
        check("X 已让出（Xorg 进程 0）", xorg_now == "0", "pgrep Xorg=%s" % xorg_now)

        # ---- 2. 常驻 GUI 在无 X 下起来 ----
        say("[2] 起 agent-gui（EGLFS，无 X）")
        systemctl("start", "agent-gui.service")
        time.sleep(12)
        gui_pid = main_pid("agent-gui.service")
        maps = ""
        if gui_pid:
            maps = run(["bash", "-lc", "grep -c libqeglfs /proc/%d/maps" % gui_pid],
                       timeout=20)[1].strip()
        env_text = run(["systemctl", "show", "-p", "Environment", "agent-gui.service"],
                       timeout=20)[1]
        check("agent-gui.service active（无 X 形态）", is_active("agent-gui.service") == "active",
              "pid=%d" % gui_pid)
        check("GUI 真的加载了 libqeglfs（不是 xcb）", maps not in ("", "0"), "maps 命中=%s" % maps)
        check("服务环境里有 QT_QPA_PLATFORM=eglfs", "QT_QPA_PLATFORM=eglfs" in env_text)
        xorg_now = run(["pgrep", "-c", "Xorg"], timeout=20)[1].strip() or "0"
        check("起 GUI 没把 X 带起来（Requires 已去掉）", xorg_now == "0",
              "pgrep Xorg=%s（应为 0）" % xorg_now)

        # ---- 3. T14 验收脚本（offscreen，服务在跑）----
        say("[3] T14 验收脚本（它们本来就走 offscreen；现在服务是**无 X**形态）")
        for script in T14_SCRIPTS:
            started = time.time()
            rc, output = run(["python3", os.path.join("tests", "board", script)], timeout=900)
            last = ""
            for line in output.splitlines():
                if line.strip().startswith("== 结果"):
                    last = line.strip()
            took = time.time() - started
            check("%s（rc=%d，%.0fs）" % (script, rc, took), rc == 0 and "失败" not in last,
                  last or output.strip().splitlines()[-1][:120] if output.strip() else "无输出")

        # ---- 4. 四页回归（EGLFS 独占 DRM，所以先停服务）----
        say("[4] 四页截图回归（EGLFS 独占 DRM：先停常驻 GUI，之后起回来）")
        systemctl("stop", "agent-gui.service")
        time.sleep(3)
        env = dict(os.environ)
        env.update({"QT_QPA_PLATFORM": "eglfs", "QT_QPA_EGLFS_INTEGRATION": "eglfs_kms",
                    "QT_QPA_EGLFS_ROTATION": "90", "QT_QUICK_BACKEND": "software",
                    "QT_IM_MODULE": "qtvirtualkeyboard", "XDG_RUNTIME_DIR": "/tmp/t151d_xdg",
                    "HOME": "/root"})
        os.makedirs("/tmp/t151d_xdg", exist_ok=True)
        os.chmod("/tmp/t151d_xdg", 0o700)
        for page in PAGES:
            shot = "/tmp/t151d_%s.png" % page
            log = "/tmp/t151d_%s.log" % page
            with open(log, "wb") as handle:
                rc = subprocess.run([GUI, "--config", CFG, "--socket", "/tmp/agent.sock",
                                     "--page", page, "--screenshot", shot,
                                     "--screenshot-delay", "6000"],
                                    cwd=REPO, stdout=handle, stderr=subprocess.STDOUT,
                                    env=env, timeout=120).returncode
            w, h, colors, edges = png_stats(shot) if os.path.exists(shot) else (0, 0, 0, 0)
            text = open(log, encoding="utf-8", errors="replace").read()
            errs = [ln for ln in text.splitlines()
                    if "error" in ln.lower() or "could not" in ln.lower()]
            check("页 %s：退出码 0 + 截图 1280x800 + 有内容" % page,
                  rc == 0 and (w, h) == (1280, 800) and colors > 30 and edges > 50,
                  "rc=%d 截图=%dx%d 颜色=%d 边界=%d 错误行=%d" % (rc, w, h, colors, edges,
                                                                len(errs)))
            say("      %s（%s）" % (shot, "、".join(errs[:2]) if errs else "无错误行"))
        systemctl("start", "agent-gui.service")
        time.sleep(10)
        check("页面回归后 agent-gui 又起来了", is_active("agent-gui.service") == "active")

        # ---- 5. 空闲采样 ----
        say("[5] 空闲采样 %ds（不动它：home 页静止）" % IDLE_SECONDS)
        gui_pid = main_pid("agent-gui.service")
        agent_pid = main_pid("agent.service")
        gui0, gui_rss0 = proc_stat(gui_pid)
        agent0, agent_rss0 = proc_stat(agent_pid)
        sys0, idle0 = sys_cpu()
        t0 = time.time()
        gui_cpu_max = 0.0
        gui_rss_max = gui_rss0 or 0
        agent_rss_max = agent_rss0 or 0
        samples = 0
        while time.time() - t0 < IDLE_SECONDS:
            time.sleep(IDLE_STEP)
            sys1, idle1 = sys_cpu()
            g1, g_rss = proc_stat(gui_pid)
            a1, a_rss = proc_stat(agent_pid)
            dt = time.time() - t0
            if g1 is not None and gui0 is not None:
                gui_cpu_max = max(gui_cpu_max, (g1 - gui0) / IDLE_STEP * 100.0)
            if g_rss:
                gui_rss_max = max(gui_rss_max, g_rss)
            if a_rss:
                agent_rss_max = max(agent_rss_max, a_rss)
            samples += 1
            if samples % 4 == 0:
                total = sys1 - sys0
                busy = 100.0 * (total - (idle1 - idle0)) / max(total, 1)
                say("      t=%4.0fs 整机忙 %.1f%%  GUI %.1f%%/%.0fMB  Agent %.0fMB"
                    % (dt, busy, (g1 - gui0) / max(dt, 0.1) * 100.0, (g_rss or 0) / 1024.0,
                       (a_rss or 0) / 1024.0))
        total = sys1 - sys0
        busy_avg = 100.0 * (total - (idle1 - idle0)) / max(total, 1)
        gui_cpu_avg = ((g1 - gui0) / max(time.time() - t0, 0.1) * 100.0
                       if (g1 is not None and gui0 is not None) else -1)
        say("[5] 空闲 %ds：整机忙 **%.1f%%**（空闲 %.1f%%）；GUI CPU 均值 **%.1f%%** / 峰值 %.1f%%，"
            "RSS **%.0f MB**；Agent RSS **%.0f MB**"
            % (IDLE_SECONDS, busy_avg, 100 - busy_avg, gui_cpu_avg, gui_cpu_max,
               gui_rss_max / 1024.0, agent_rss_max / 1024.0))
        say("[5] free（无 X，GUI 在跑）：%s"
            % run(["bash", "-lc", "free -m | sed -n 2p"], timeout=20)[1].strip())
        say("[5] 空闲时最忙的进程（5s 采样，给 T15-5/T15-6 用）：")
        for cpu, pid, name, cmd in top_consumers(5.0):
            say("      %5.1f%%  pid=%-6d %-16s %s" % (cpu, pid, name, cmd))
        # 判据只钉"我们的 GUI 有没有异常忙"：整机忙里还有 Agent/LLM/音乐轮询的份额，
        # 那是 T15-5/T15-6 的活儿，1d 负责把数字和凶手一起留档。
        check("GUI 空闲 CPU 均值 < 10%", 0 <= gui_cpu_avg < 10.0, "%.1f%%" % gui_cpu_avg)
        check("GUI RSS < 200 MB", gui_rss_max < 200 * 1024, "%.0f MB" % (gui_rss_max / 1024.0))
        check("整机空闲忙 < 40%（>40% 基本就是忙等/打满了）", busy_avg < 40.0,
              "%.1f%%（其中 GUI %.1f%%）" % (busy_avg, gui_cpu_avg))
    finally:
        restore_desktop()
        md5_after = run(["md5sum", CFG], timeout=20)[1].split()[0]
        say("[收尾] config.yaml md5（后）=%s %s"
            % (md5_after, "OK（没被改）" if md5_after == md5_before else "!! 被改了"))
        if md5_after != md5_before:
            checks.append(("config.yaml 没被改", False))
            abort = 1
        if not gui_svc_active_before:
            say("[收尾] 注：跑之前 agent-gui 本来就不是 active（不是本次弄的）")

    failed = [name for name, ok in checks if not ok]
    say("\n== 结果: %s（%d 项通过%s）"
        % ("全部通过" if not failed else "失败 %d 项" % len(failed), len(checks) - len(failed),
           ("，失败: " + "、".join(failed)) if failed else ""))
    return 1 if failed or abort else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        restore_desktop()
        sys.exit(130)
