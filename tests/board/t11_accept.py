#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T11 板端验收: **B 站视频**（Phase 7 T11-9）。

跑法（板端, 仓库根, 要有网 + 真 GUI + `config/bilibili_cookie.json`）::

    python3 tests/board/t11_accept.py                    # 全套
    python3 tests/board/t11_accept.py --no-gui           # 不拉真 GUI（只验 Agent 侧）
    python3 tests/board/t11_accept.py --no-vision        # 不跑 SigLIP（省 900 MB 内存与时间）
    python3 tests/board/t11_accept.py --no-netcut        # 跳过 iptables 那一段
    python3 tests/board/t11_accept.py --keyword "..."    # 换个关键词

验的是这七件事（**真代码 + 真网络 + 真 GUI + 真视频**, 只有两处是"替身"且都写在下面）:

    A. 真搜索 -> 队列 = 3 × 预览栏格数 + 真封面地址（预览图由**真 GUI** 去下, 见 C 的日志）
    B. cookie 三态（有 / 空 / 失效）各给一句**不一样**的话
    C. **真 GUI** 端到端: 点预览图 -> 15 s 起播门槛 -> `video_state.position_s` 递增
       -> 上一集/下一集换条 -> 旧缓冲释放（FIFO 没了 + RSS 回落）
    D. 暂停 -> 预取从 15 s 放宽到 60 s（到水位就停）
    E. 断网（iptables REJECT 掉 api.bilibili.com）-> **诚实报错** -> 规则撤掉后能继续
    F. 双路识别**真跑**（真 SigLIP + 真锚点副本 + 假进程读数）-> 不一致时**锚点副本多一行**;
       有对话关键词 -> **一帧都不抓**（模型都没加载）
    G. **真数据一个字节都不动**（跑前跑后 md5 对一遍）+ 跑完不留 ffmpeg/GUI 进程

两处替身（都是为了不碰真数据/真 PC, 且都打印出来）:
    · 锚点库指**副本**（真文件在 `config/game_anchors.jsonl`）;
    · 进程那条路用一个**假读数**（真 PC 上现在没在跑那些游戏）, 用来逼出"两路不一致"。

⚠ 这是**验收脚本**（要真网络/真 GUI/真数据）, 不进 `scripts/test-python.*` 的常规清单。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import logging
import os
import re
import shutil
import socket as socketlib
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
GUI_BIN = os.path.join(REPO, "gui", "build", "agent_gui")

# 真机截图/窗口要用板端那块屏（ssh 进来的 shell 不一定带这两个变量）
os.environ.setdefault("DISPLAY", ":0")
os.environ.setdefault("XAUTHORITY", "/var/run/slim.auth")

#: 真数据/真配置（跑前跑后都要逐字节一致）
REAL_FILES = ("config/config.yaml", "config/game_anchors.jsonl", "config/wall_data.jsonl",
              "config/music_library.jsonl", "config/user_profile.jsonl",
              "config/bilibili_cookie.json")
#: 认游戏用的截图（T11-0 标定的那批）
SAMPLES_DIR = "/home/kickpi/game_samples"
#: 断网那一段要 REJECT 的主机
API_HOST = "api.bilibili.com"

_FIXED = 6          # 预览栏格数（队列目标 = 3 × 6 = 18）；真 GUI 会再上报它自己算出来的
_FAILED = []


def check(name, ok, detail=""):
    print("  %s %s%s" % ("✔" if ok else "✘", name, ("  [%s]" % detail) if detail else ""))
    if not ok:
        _FAILED.append(name)
    return bool(ok)


def skip(name, why):
    print("  － %s（跳过: %s）" % (name, why))


def note(text):
    print("    · %s" % text)


def md5(path):
    if not os.path.exists(path):
        return "-"
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rss_mb():
    """本进程 RSS（MB）—— 旧缓冲释放后该回落。"""
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return float(line.split()[1]) / 1024.0
    except OSError:
        pass
    return 0.0


async def wait_for(predicate, timeout_s, what, interval=0.25):
    """等一个条件成立（返回它等到了没）。

    ⚠ **必须是 async**（用 `await asyncio.sleep`）：Agent 的 IPC 就长在**这个事件循环**上 ——
      用阻塞的 `time.sleep` 等，服务端那一侧一个字都处理不了（GUI 连上了、命令也发了，
      但没人读）。这个坑第一次跑就踩了个正着（预览栏 0 格 + 一条命令都没到）。
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


def run(cmd, timeout=60):
    """跑一条命令, 回 (rc, stdout+stderr)。"""
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=timeout)
        return proc.returncode, proc.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, str(exc)


# ---------------------------------------------------------------------------
#  真 GUI 那一段的驱动
# ---------------------------------------------------------------------------
class GuiRun(object):
    """起一个**真 GUI**（全屏 kiosk）连到我们的 socket, 退出时保证杀掉。"""

    def __init__(self, sock, log_path, *args):
        self.sock = sock
        self.log_path = log_path
        self.extra = list(args)
        self.proc = None
        self.handle = None

    def start(self, delay_ms=60000):
        env = dict(os.environ)
        env.setdefault("DISPLAY", ":0")
        env.setdefault("XAUTHORITY", "/var/run/slim.auth")
        self.handle = open(self.log_path, "wb")
        cmd = [GUI_BIN, "--socket", self.sock, "--idle-ms", str(delay_ms)] + self.extra
        print("  起 GUI: %s" % " ".join(cmd))
        self.proc = subprocess.Popen(cmd, stdout=self.handle, stderr=subprocess.STDOUT, env=env)
        return self.proc

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        if self.handle is not None:
            self.handle.close()
            self.handle = None

    def log_text(self):
        if not os.path.exists(self.log_path):
            return ""
        with open(self.log_path, "rb") as handle:
            return handle.read().decode("utf-8", "replace")


def scrot(path):
    rc, out = run(["scrot", "-o", path], timeout=20)
    return rc == 0 and os.path.exists(path), out.strip()


# ---------------------------------------------------------------------------
#  A. 真搜索 -> 队列 = 3N
# ---------------------------------------------------------------------------
def part_a_search(runtime, keyword):
    print("\n== A. 真搜索 -> 队列 = 3 × 预览栏格数（真 API, 真封面地址）")
    started = time.time()
    out = runtime.bilibili_search(keyword)
    print("  bilibili_search(%r) -> ok=%s count=%s index=%s（%.1fs）"
          % (keyword, out.get("ok"), out.get("count"), out.get("index"), time.time() - started))
    if not check("搜索成功", out.get("ok"), str(out.get("why") or "")):
        return None
    state = runtime.bilibili_state()
    target = int(state.get("target") or 0)
    count = int(state.get("count") or 0)
    print("  队列: 窗口 %d 条 / 目标 %d 条（viewport=%s）; source=%s keyword=%s"
          % (count, target, state.get("viewport"), state.get("source"), state.get("keyword")))
    check("目标 = 3 × 格数（3 × %d = %d）" % (_FIXED, 3 * _FIXED), target == 3 * _FIXED, str(target))
    check("窗口按目标填满（不足时如实少给）", count == min(target, count) and count > 0, str(count))
    check("载荷带 queue（T11-7 补的那个漏项）", isinstance(state.get("queue"), list)
          and len(state["queue"]) == count, str(len(state.get("queue") or [])))
    first = (state.get("queue") or [{}])[0]
    print("  第 1 条: %s | %s | %s | %s"
          % (first.get("bvid"), str(first.get("title"))[:28], first.get("duration_text"),
             str(first.get("cover"))[:52]))
    check("每条都有 bvid/标题/封面/地址", all(first.get(k) for k in ("bvid", "title", "cover", "url")))
    check("封面是 B 站 CDN 地址（GUI 自己带 UA+Referer 去下）",
          str(first.get("cover") or "").startswith("http"), str(first.get("cover"))[:40])
    return state


# ---------------------------------------------------------------------------
#  B. cookie 三态
# ---------------------------------------------------------------------------
class _StubBuffer(object):
    """只为了"模拟这条视频只给到 X"——`_bilibili_quality_note()` 只看 `buffer.stream`。"""

    def __init__(self, quality, label):
        self.stream = {"quality": quality, "quality_label": label}


def part_b_cookie(runtime, tmp, keyword):
    print("\n== B. cookie 三态（有 / 空 / 失效）各给一句不一样的话")
    from agent.net.bilibili_api import BilibiliApi

    api = runtime._bilibili_api
    if api is None:
        skip("cookie 三态", "bilibili 没起来")
        return
    print("  cookie 文件 = %s（存在=%s, 有 SESSDATA=%s）"
          % (api.cookie_file, os.path.exists(api.cookie_file), api.cookie_present))

    # ---- 1) 有 ----
    info = api.login(refresh=True)
    print("  ① 有 cookie: login -> code=%s isLogin=%s（%s）"
          % (info.get("code"), info.get("logged_in"), info.get("why") or "-"))
    check("有 cookie 时登录态自检能返回结论", "logged_in" in info, str(info.get("why")))

    # 用**同一段** 720P 的流去比三态的措辞（否则比的是不同视频，没意义）
    real_buffer = runtime._buffer
    keep_api = runtime._bilibili_api
    runtime._buffer = _StubBuffer(64, "720P")
    note_720 = runtime._bilibili_quality_note()
    runtime._buffer = _StubBuffer(80, "1080P")
    note_1080 = runtime._bilibili_quality_note()
    runtime._buffer = real_buffer
    print("  ① 有 cookie + 720P: %r" % (note_720,))
    print("  ① 有 cookie + 1080P: %r（1080P 及以上不念叨）" % (note_1080,))
    if real_buffer is not None:
        quality = int((real_buffer.stream or {}).get("quality") or 0)
        note("这一条**真实**的清晰度 = %s（quality=%s）"
             % ((real_buffer.stream or {}).get("quality_label"), quality))
    check("清晰度 >= 1080P 时不念叨", note_1080 == "", note_1080)
    check("有 cookie 但这条只给 720P -> 说'和登不登录无关'",
          "登不登录无关" in note_720, note_720)

    # ---- 2) 空 ----
    empty = BilibiliApi.from_config({"enabled": True,
                                     "cookie_file": os.path.join(tmp, "no_such_cookie.json")})
    runtime._bilibili_api = empty
    runtime._buffer = _StubBuffer(64, "720P")
    note_empty = runtime._bilibili_quality_note()
    runtime._buffer = real_buffer
    runtime._bilibili_api = keep_api
    print("  ② 空 cookie + 同样 720P: %r" % (note_empty,))
    check("空 cookie -> 明确说'没配 config/bilibili_cookie.json'",
          "bilibili_cookie.json" in note_empty, note_empty)
    check("空 cookie 时匿名（cookie_present=False）", empty.cookie_present is False)
    check("① 与 ② 是**两句不一样**的话", note_720 != note_empty and bool(note_720))

    # ---- 3) 失效 ----
    bad_path = os.path.join(tmp, "bad_cookie.json")
    with open(bad_path, "w", encoding="utf-8") as handle:
        json.dump({"SESSDATA": "this-is-not-a-real-sessdata"}, handle)
    bad = BilibiliApi.from_config({"enabled": True, "cookie_file": bad_path})
    bad_info = bad.login(refresh=True)
    print("  ③ 失效 cookie: login -> code=%s why=%s" % (bad_info.get("code"), bad_info.get("why")))
    why = str(bad_info.get("why") or "")
    check("失效 cookie 被判成 -101（或明确说无效）",
          bad_info.get("code") == -101 or "无效" in why or "失效" in why, why)
    # ⚑ 真问题（这次验收抓到的）：失效时 B 站的 playurl **不报错**（直接当匿名），
    #   所以"cookie 过期"只能靠 nav 看出来 —— Runtime 在**起播前**会问一次并把那句话
    #   记进 `auth_note`（`_check_bilibili_cookie()`），这里就走**同一条**代码路径。
    keep_api = runtime._bilibili_api
    runtime._bilibili_api = bad
    runtime._check_bilibili_cookie()
    runtime._buffer = _StubBuffer(64, "720P")
    note_bad = runtime._bilibili_quality_note()
    runtime._buffer = real_buffer
    runtime._bilibili_api = keep_api
    print("  ③ 起播前问过之后留下的话: %r" % (note_bad,))
    check("失效 cookie 被记下来（auth_note 非空）", bool(bad.auth_note), str(bad.auth_note)[:60])
    check("失效 cookie 时那句话明说 cookie 与怎么修",
          "cookie" in note_bad and "bilibili_cookie.json" in note_bad, note_bad)
    check("③ 与 ① 是两句不一样的话", note_bad != note_720)
    # 顺带记一笔：这条真视频在失效 cookie 下实际能放什么（如实记录, 不当断言）
    stream, err = None, ""
    try:
        found = api.search(keyword, limit=1)["items"]
        if found:
            bvid = found[0]["bvid"]
            cid = api.view(bvid)["cid"]
            stream = bad.playurl(bvid, cid)
    except Exception as exc:                          # noqa: BLE001 - 风控/网络都算它自己的事
        err = repr(exc)
    if stream is None:
        note("失效 cookie 下没能试成直链（%s）" % (err or "搜索没结果"))
    else:
        note("失效 cookie 下 B 站把这条当匿名：退回 %s / %s（**它不报错** —— 这正是为什么要提前问）"
             % (stream.get("kind"), stream.get("quality_label")))


# ---------------------------------------------------------------------------
#  C/D. 真 GUI：起播 / 进度 / 换条 / 暂停预取
# ---------------------------------------------------------------------------
async def part_c_playback(runtime, sock, seen, tmp):
    print("\n== C. 真 GUI 端到端（点预览图 -> 15 s 门槛 -> 真播 -> 换条）")
    if not os.path.exists(GUI_BIN):
        skip("真 GUI 播放", "没找到 %s（先跑 scripts/sync-gui.ps1）" % GUI_BIN)
        return
    shot = os.path.join(tmp, "t11-9-playing.png")
    # ⚠ `--video-pause-demo` 让 **GUI 自己**在 25 秒时点一次播放/暂停（D 段要用它）：
    #   从 Agent 那侧塞一条 playing=false 会被 GUI 每 2 秒的进度回报覆盖回去。
    gui = GuiRun(sock, os.path.join(tmp, "gui1.log"),
                 "--bilibili-pick-demo", "0", "--video-pause-demo", "25000")
    gui.start()
    try:
        # ⚠ **必须把 GUI 切进 GAME 模式**（而且要在它连上之后切）：视频区（预览栏 + 地址栏）
        #   只在游戏模式里露出来 —— 不切的话那块页面根本没被排版（实测预览栏可见宽度只有
        #   默认的 100 px），于是"格数上报"没有；视频页不可见还会让 GStreamer 不出画面。
        #   走的是真路径：状态机 on_change -> ipc 推 status{mode}（与按界面按钮同一条）。
        await wait_for(lambda: int(getattr(runtime.ipc, "clients", 0) or 0) >= 1, 20, "GUI 连上")
        from agent.core import State

        moved = runtime.state.transition(State.GAME, "t11-9 验收: 视频区只在 GAME 里露出来")
        print("  切到 GAME: %s（当前 %s）" % (moved, runtime.state.current()))
        await asyncio.sleep(0.5)
        # ⚠ `seen` 记的是 **Runtime 那一层的动作名**（pick/next/prev/viewport/video_state），
        #   不是线格式的 `bilibili_pick` —— 线格式由 C++/Python 单测钉着。
        got_pick = await wait_for(lambda: any(a == "pick" for a, _, _ in seen), 25,
                                  "GUI 点预览图")
        check("GUI 真的点了预览图（Agent 收到 pick）", got_pick,
              str([a for a, _, _ in seen][-3:]))
        # 预览栏格数上报（连上后自己算出来的；GAME 切过去 + 排版好才量得出宽度）
        got_viewport = await wait_for(lambda: any(a == "viewport" for a, _, _ in seen), 20,
                                      "预览栏格数")
        viewport = [p for a, p, _ in seen if a == "viewport"]
        print("  GUI 上报格数: %s" % (viewport[:3] or "-"))
        check("GUI 上报了预览栏格数（viewport）", got_viewport and bool(viewport))
        # 封面（真 CDN, 带 UA+Referer）
        covers = len(re.findall(r"封面到了", gui.log_text()))
        print("  真 GUI 下到的封面: %d 张" % covers)
        check("真 GUI 从 CDN 下到了封面（UA+Referer 有效）", covers > 0, "%d 张" % covers)

        # ---- 15 s 起播门槛 ----
        # ⚠ 门槛一到，写端就开始往管道灌、播放器就开吃 —— 之后 `buffered_s` 会**掉下来**，
        #   所以"门槛够不够"要看 **buffered + written**（= 一共从 ffmpeg 拉进来多少秒）。
        started = time.time()
        first_ready = None
        while time.time() - started < 45:
            snap = runtime._buffer.snapshot() if runtime._buffer is not None else {}
            if snap.get("ready"):
                first_ready = snap
                break
            await asyncio.sleep(0.1)
        buffer = runtime._buffer
        elapsed = time.time() - started
        if first_ready is None:
            print("  45 s 内没就绪：%s" % (buffer.snapshot() if buffer else "-"))
            check("缓冲就绪（15 s 门槛）", False, "超时")
        else:
            got_s = (float(first_ready.get("buffered_s") or 0)
                     + float(first_ready.get("written_s") or 0)
                     + float(first_ready.get("inflight_s") or 0))
            print("  缓冲就绪: ready=%s buffered=%.1fs written=%.1fs inflight=%.1fs 合计=%.1fs"
                  "（点下去算 %.1fs）"
                  % (first_ready.get("ready"), first_ready.get("buffered_s") or 0.0,
                     first_ready.get("written_s") or 0.0, first_ready.get("inflight_s") or 0.0,
                     got_s, elapsed))
            check("攒够 15 s 才开闸（buffered+written ≥ 15）", got_s >= 15.0, "%.1fs" % got_s)
            check("缓冲对象报的门槛就是配置里的 15 s",
                  abs(float(buffer.initial_s) - 15.0) < 0.01, str(buffer.initial_s))
        state = runtime.bilibili_state()
        print("  stream=%s（FIFO 存在=%s）; quality=%s"
              % (state.get("stream"), os.path.exists(str(state.get("stream"))), state.get("quality")))
        check("推给 GUI 的是**本地 FIFO 路径**（不是 HTTP URL）",
              str(state.get("stream") or "").startswith("/"), str(state.get("stream")))
        check("FIFO 真的建出来了", os.path.exists(str(state.get("stream") or "")))

        # ---- position_s 递增 ----
        def states():
            return [(p.get("position_s"), p.get("playing"))
                    for a, p, _ in seen if a == "video_state"]

        # 真播是 1× 的：给它 25 秒，攒够几个**不一样**的位置就算在放
        deadline = time.time() + 25
        while time.time() < deadline:
            if len({round(float(p or 0), 1) for p, _ in states() if float(p or 0) > 0}) >= 3:
                break
            await asyncio.sleep(0.5)
        rows = states()
        print("  前几次 video_state: %s" % (rows[:6],))
        check("GUI 每 2 秒回报 video_state（≥3 次）", len(rows) >= 3, "%d 次" % len(rows))
        positions = [float(p or 0) for p, _ in rows]
        check("position_s 在涨（真在放）",
              len({round(p, 1) for p in positions if p > 0}) >= 2,
              "%s" % sorted({round(p, 1) for p in positions})[:6])
        check("playing=true（不是暂停态）", any(bool(flag) for _, flag in rows))

        # ---- 截图（多抓几张, 至少一张里有字） ----
        ok_shot, out = scrot(shot)
        check("真机截图落盘", ok_shot and os.path.getsize(shot) > 0, shot if ok_shot else out)
        sizes = [os.path.getsize(shot)] if ok_shot else []
        for i in range(2):
            time.sleep(2)
            more = shot.replace(".png", "-%d.png" % i)
            good, _ = scrot(more)
            if good:
                sizes.append(os.path.getsize(more))
        print("  截图大小: %s" % sizes)

        # ---- 旧缓冲释放 + 换条（下一集 / 上一集 走真 IPC） ----
        old = runtime._buffer
        old_path = str(getattr(old, "path", "") or "")
        before_rss = rss_mb()
        send_command(sock, "next_bilibili", {})
        moved = await wait_for(lambda: runtime._buffer is not old, 25, "换条")
        check("下一集：换了新的一条（新缓冲对象）", moved)
        released = await wait_for(lambda: not os.path.exists(old_path), 15, "旧 FIFO 释放")
        print("  旧 FIFO %s 还在吗: %s; 旧缓冲 state=%s"
              % (old_path, os.path.exists(old_path), old.snapshot().get("state") if old else "-"))
        check("旧缓冲立刻释放（FIFO 被删掉）", released,
              str(old.snapshot().get("state") if old else ""))
        after_rss = rss_mb()
        print("  Agent RSS: %.0f MB -> %.0f MB" % (before_rss, after_rss))
        check("换条后没有一堆 ffmpeg 堆着（RSS 没暴涨）", after_rss - before_rss < 120,
              "%+.0f MB" % (after_rss - before_rss))
        send_command(sock, "prev_bilibili", {})
        check("上一集：Agent 收到 prev", await wait_for(
            lambda: any(a == "prev" for a, _, _ in seen), 8, "prev"))

        # ---- D. 暂停 -> 预取放宽到 60 s（**GUI 自己暂停**, FIFO 读端还活着） ----
        print("\n== D. 暂停 -> 预取从 15 s 放宽到 60 s（由 GUI 自己点暂停）")
        buffer = runtime._buffer
        if buffer is None:
            skip("暂停预取", "没有缓冲对象")
        else:
            # ⚠ 只看**这一段之后**新到的回报：前面"放完"那几条也是 playing=false，
            #   不按下标看的话会立刻匹配上（第一次跑就踩了 —— 于是根本没等到暂停）。
            mark = len(seen)
            paused = await wait_for(
                lambda: any(a == "video_state" and p.get("playing") is False
                            and not p.get("eof") for a, p, _ in seen[mark:]),
                45, "GUI 报 playing=false")
            await asyncio.sleep(1.0)                    # 让 set_playing 落到缓冲上
            print("  GUI 自己报了 playing=false: %s（cap_s=%.0f s）" % (paused, buffer.cap_s()))
            check("GUI 点暂停后如实回报 playing=false", paused)
            check("暂停后封顶从 15 s 放到 60 s", buffer.cap_s() >= 60.0, "%.0f" % buffer.cap_s())
            grew = await wait_for(
                lambda: float(buffer.snapshot().get("buffered_s") or 0) > 20.0, 45, "暂停预取")
            snap = buffer.snapshot()
            print("  暂停后: %s" % snap)
            check("暂停时确实继续预取（> 20 s）", grew,
                  "%.1fs" % float(snap.get("buffered_s") or 0))
            # 恢复：GUI 的暂停按钮只能按一次（demo 就一次），这里从 Agent 侧把它放回去 ——
            # 此时 GUI 的进度回报也是 playing=true，两边一致，不会被覆盖。
            send_command(sock, "video_state", {"playing": True, "position_s": 3.0})
            await wait_for(lambda: buffer.cap_s() <= 15.5, 5, "恢复后封顶变小")
            check("恢复播放后封顶回到 15 s", buffer.cap_s() <= 15.5, "%.0f" % buffer.cap_s())
    finally:
        gui.stop()
        await asyncio.sleep(0.5)
    print("  这一段的命令全表: %s" % [(a, r.get("ok"), r.get("tell_user") or r.get("error") or "")
                                      for a, _, r in seen])
    if not any(a == "pick" for a, _, _ in seen):
        print("  --- GUI 日志（诊断用）---")
        print("\n".join(gui.log_text().splitlines()[-12:]))


async def part_d_pause(runtime, sock, seen):
    """D 段已在 `part_c_playback` 里跑（**必须趁 GUI 还活着**：FIFO 的读端就是它）。

    这里只在"没跑 GUI"（--no-gui）时给一句说明，免得日志里少一段让人以为漏了。
    """
    if runtime._buffer is None or runtime._buffer.ready:
        return
    skip("暂停预取", "没有缓冲对象（--no-gui 时不会起播）")


def send_command(sock, action, payload=None):
    """像 GUI 那样发一条命令（真 IPC 线格式, 不阻塞事件循环）。"""
    from agent.ipc import protocol

    line = protocol.encode_command(action, payload or {})
    client = socketlib.socket(socketlib.AF_UNIX, socketlib.SOCK_STREAM)
    try:
        client.settimeout(3.0)
        client.connect(sock)
        client.sendall(line)
    finally:
        client.close()


# ---------------------------------------------------------------------------
#  C2. 上一集/下一集那两个**按钮**（真 GUI 拉着点）
# ---------------------------------------------------------------------------
async def part_c2_buttons(runtime, sock, seen, tmp):
    print("\n== C2. 真 GUI 的「上一集 / 下一集」按钮（命令真的发出去了吗）")
    if not os.path.exists(GUI_BIN):
        skip("按钮那两条命令", "没找到 GUI 二进制")
        return
    start = len(seen)                     # ⚠ 按**下标**算新增：动作名会重复（前面已经 next/prev 过）
    gui = GuiRun(sock, os.path.join(tmp, "gui2.log"),
                 "--prev-bilibili-demo", "--next-bilibili-demo")
    gui.start()
    try:
        await wait_for(lambda: int(getattr(runtime.ipc, "clients", 0) or 0) >= 1, 20, "GUI2 连上")
        from agent.core import State

        runtime.state.transition(State.IDLE, "t11-9: 回 IDLE 再进 GAME, 好让 status 再推一次")
        runtime.state.transition(State.GAME, "t11-9: 第二个 GUI 也要在游戏模式里")
        await wait_for(lambda: {"prev", "next"} <= {a for a, _, _ in seen[start:]}, 25, "两个按钮")
        now = [a for a, _, _ in seen[start:]]
        print("  这一轮新到的命令: %s" % now)
        check("「上一集」发出 prev_bilibili", "prev" in now)
        check("「下一集」发出 next_bilibili", "next" in now)
        log = gui.log_text()
        check("GUI 侧日志能看到那两个动作（按钮真的被按了）",
              "点了上一集" in log or "点了下一集" in log, log[-120:].replace("\n", " "))
    finally:
        gui.stop()
        await asyncio.sleep(0.3)
    print("  这一段新增: %s" % [(a, r.get("ok"), r.get("error") or "")
                                for a, _, r in seen[start:]])
    if not {"prev", "next"} & set(now):
        print("  --- GUI2 日志（诊断用）---")
        print("\n".join(gui.log_text().splitlines()[-14:]))


# ---------------------------------------------------------------------------
#  E. 断网 -> 诚实报错 -> 恢复
# ---------------------------------------------------------------------------
def part_e_netcut(runtime, keyword, enabled):
    print("\n== E. 断网（iptables REJECT）-> 诚实报错 -> 撤掉规则能继续")
    if not enabled:
        skip("断网那一段", "--no-netcut")
        return
    rc, _ = run(["which", "iptables"], timeout=10)
    if rc != 0:
        skip("断网那一段", "板端没有 iptables")
        return
    rc, out = run(["getent", "hosts", API_HOST], timeout=10)
    ips = []
    try:
        import socket as _socket

        for info in _socket.getaddrinfo(API_HOST, 443, _socket.AF_INET):
            ips.append(info[4][0])
    except OSError as exc:
        note("解析 %s 失败: %r" % (API_HOST, exc))
    ips = sorted(set(ips))
    if not ips:
        skip("断网那一段", "解析不出 %s 的地址" % API_HOST)
        return
    rules = []
    try:
        for ip in ips:                       # ⚠ **所有** IPv4 都堵上（解析出来常常不止一个）
            rule = ["iptables", "-I", "OUTPUT", "-d", ip, "-p", "tcp", "--dport", "443",
                    "-j", "REJECT"]
            rc, out = run(rule, timeout=10)
            if rc == 0:
                rules.append(ip)
                note("已 REJECT %s:443" % ip)
        if not rules:
            skip("断网那一段", "iptables 规则加不上")
            return
        # 正控：先证明"确实堵上了"（否则后面"搜到了"到底是网络还是规则没生效都说不清）
        rc, out = run(["curl", "-sS", "-m", "6", "-o", "/dev/null", "-w", "%{http_code}",
                       "https://%s/x/web-interface/nav" % API_HOST], timeout=20)
        print("  正控: curl 探一下被堵的接口 -> rc=%s %s" % (rc, (out or "").strip()[:40]))
        check("规则真的生效了（curl 也连不上）", rc != 0, "rc=%s %s" % (rc, out.strip()[:40]))
        from agent.net.bilibili_api import BilibiliError, BilibiliNetworkError

        def honest(text):
            text = str(text)
            return bool(text.strip()) and ("过一会" in text or "网络" in text
                                           or "连" in text or "HTTP" in text)

        failure = ""
        try:
            runtime.bilibili.search("断网测试", source="dialogue")
        except (BilibiliNetworkError, BilibiliError) as exc:
            failure = str(exc)
        if failure:
            print("  断网后的原话: %s" % failure)
            check("断网后搜索失败，并如实报错", True)
            check("断网时那句话能照做", honest(failure), failure[:60])
        else:
            # ⚑ T11-9 实测：板端到 api.bilibili.com **还有 IPv6 通路**，只堵 IPv4 堵不住
            #   （curl 走 v4 已经连不上，urllib 换了一条路照样通）。所以再用**真的**
            #   transport 打一个不可路由的地址（TEST-NET-1）验"网络失败会如实说"。
            note("IPv4 已堵住但搜索仍成功 —— 板端到 api.bilibili.com 还有 IPv6 通路")
            import logging as _logging

            import agent.net.bilibili_api as api_mod
            from agent.net.bilibili_api import BilibiliApi, UrllibTransport

            keep = api_mod.SEARCH_URL
            probe = ""
            try:
                api_mod.SEARCH_URL = "https://192.0.2.1/x/web-interface/search/type"
                dead = BilibiliApi(cookie_file="", transport=UrllibTransport(4.0),
                                   log=_logging.getLogger("t11.netcut"))
                dead.search("断网测试", page=1, limit=1)
            except BilibiliError as exc:
                probe = str(exc)
            finally:
                api_mod.SEARCH_URL = keep
            print("  补充（指向不可达地址 192.0.2.1）: %s" % (probe or "居然成功了？"))
            check("断网时报的是能照做的话（这一条走的是真 socket 失败）", honest(probe), probe[:60])
    finally:
        for ip in rules:
            rc, out = run(["iptables", "-D", "OUTPUT", "-d", ip, "-p", "tcp",
                           "--dport", "443", "-j", "REJECT"], timeout=10)
            note("撤掉 %s 的规则（rc=%s）" % (ip, rc))
    left = run(["iptables", "-S", "OUTPUT"], timeout=10)[1]
    check("规则撤干净了（不留 REJECT）", "REJECT" not in left or API_HOST not in left,
          "OUTPUT 链里还有 REJECT" if "REJECT" in left else "")
    recovered = runtime.bilibili_search(keyword)
    check("恢复后搜索又能用了", bool(recovered.get("ok")), str(recovered.get("why") or ""))


# ---------------------------------------------------------------------------
#  F. 双路识别真跑 + 关键词优先
# ---------------------------------------------------------------------------
def part_f_game_watch(runtime, tmp, enabled):
    print("\n== F. 双路识别真跑（真 SigLIP + 真锚点**副本** + 假进程读数）")
    if not enabled:
        skip("识别那一段", "--no-vision")
        return
    from agent.core.game_anchors import GameAnchors
    from agent.core.game_watch import GameWatcher

    real_anchors = os.path.join(REPO, "config", "game_anchors.jsonl")
    if not os.path.exists(real_anchors):
        skip("识别那一段", "没有 %s" % real_anchors)
        return
    copy_path = os.path.join(tmp, "anchors.jsonl")
    shutil.copyfile(real_anchors, copy_path)
    anchors = GameAnchors(copy_path, shot_dir=os.path.join(tmp, "shots"))
    anchors.load()
    before_lines = len(anchors)
    print("  锚点副本: %d 条 / 游戏 %s" % (before_lines, anchors.games()))

    samples = {}
    if os.path.isdir(SAMPLES_DIR):
        for name in sorted(os.listdir(SAMPLES_DIR)):
            folder = os.path.join(SAMPLES_DIR, name)
            if not os.path.isdir(folder):
                continue
            files = [os.path.join(folder, f) for f in sorted(os.listdir(folder))
                     if f.lower().endswith((".png", ".jpg", ".jpeg"))]
            if files:
                samples[name] = files[0]
    if not samples:
        skip("识别那一段", "没有 %s 下的截图素材" % SAMPLES_DIR)
        return
    print("  素材: %s" % ", ".join("%s(%s)" % (k, os.path.basename(v))
                                  for k, v in list(samples.items())[:5]))

    try:
        from agent.vision.tagger import load_image
    except Exception as exc:                          # noqa: BLE001
        skip("识别那一段", "load_image 不可用: %r" % exc)
        return

    # ---- 有对话关键词 -> 一帧都不抓（模型都不加载） ----
    class _Probe(object):
        def __init__(self, games):
            self._games = games

        def snapshot(self):
            return {"ok": True, "games": {g: "fake" for g in self._games}, "why": ""}

    watcher = GameWatcher(anchors, probe=_Probe([]), learn=True,
                          log=logging.getLogger("t11.acc.watch"))
    frame_path = list(samples.values())[0]
    frame = load_image(frame_path)
    decision = watcher.tick(frame, state="GAME", has_keyword=True)
    print("  有对话关键词 -> tick: skipped=%r; 模型加载了吗=%s"
          % (decision.get("skipped"), watcher.model_loaded))
    check("有对话关键词时**不认**（跳过）", bool(decision.get("skipped")), decision.get("skipped", ""))
    check("有对话关键词时连模型都不加载", watcher.model_loaded is False)
    check("这一跳没有动过锚点", len(anchors) == before_lines)

    # ---- 无关键词: 真跑一次（加载 SigLIP / 真编码 / 双路比对 / 自纠错） ----
    game_in_anchors = anchors.games()[0] if anchors.games() else ""
    fake_game = "stellaris" if game_in_anchors != "stellaris" else "white album"
    watcher.probe = _Probe([fake_game])
    started = time.time()
    loaded = watcher.ensure_model("GAME")
    print("  SigLIP 加载: %s（%.1fs）" % (loaded, time.time() - started))
    if not check("SigLIP 真的加载起来了", loaded):
        skip("识别那一段", "模型没加载（内存不够/模型文件不在？）")
        return
    started = time.time()
    result = watcher.identify(frame)
    print("  认出: game=%r source=%s score=%.3f margin=%.3f process=%s learned=%s"
          % (result.get("game"), result.get("source"), result.get("score"),
             result.get("margin"), result.get("process_games"), result.get("learned")))
    note(result.get("note") or "")
    check("认得出一张真截图（画面那条路给出了候选）", float(result.get("score") or 0) > 0,
          "%.3f" % float(result.get("score") or 0))
    check("两路不一致时**以进程为准**（source=process/corrected）",
          result.get("source") in ("process", "corrected"), str(result.get("source")))
    check("不一致的那一帧被登记成该游戏的锚点（jsonl 多一行）",
          len(anchors) == before_lines + 1, "%d -> %d" % (before_lines, len(anchors)))
    learned = anchors._rows[-1] if anchors._rows else {}
    print("  新锚点: game=%s source=%s shot=%s"
          % (learned.get("game"), learned.get("source"), os.path.basename(str(learned.get("shot")))))
    check("新锚点记的是进程说的那个游戏", str(learned.get("game")) == fake_game,
          "%s vs %s" % (learned.get("game"), fake_game))
    watcher.unload()
    print("  卸载后 RSS: %.0f MB" % rss_mb())


# ---------------------------------------------------------------------------
async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--keyword", default="luna say maybe")
    parser.add_argument("--no-gui", action="store_true", help="不拉真 GUI")
    parser.add_argument("--no-vision", action="store_true", help="不跑 SigLIP")
    parser.add_argument("--no-netcut", action="store_true", help="跳过 iptables 那一段")
    parser.add_argument("--parts", default="abcdef",
                        help="只跑哪几段（小写字母, 默认 abcdef 全跑; 调试用, 例如 --parts cd）")
    args = parser.parse_args()
    want = set(args.parts.lower())

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    from agent.config import load_config
    from agent.main import Runtime

    print("== 真数据 md5（跑之前）")
    real_before = {name: md5(os.path.join(REPO, name)) for name in REAL_FILES}
    for name, digest in real_before.items():
        print("  %-34s %s" % (name, digest))

    tmp = tempfile.mkdtemp(prefix="t11-")
    sock = os.path.join(tmp, "t11.sock")
    config = json.loads(json.dumps(load_config("config"), default=str))
    config.setdefault("ipc", {})["socket_path"] = sock
    section = dict(config.get("bilibili") or {})
    section["enabled"] = True
    section["queue"] = {"viewport_fallback": _FIXED, "max": 60}
    section["buffer"] = {"dir": "/tmp", "initial_s": 15, "max_s": 60, "mem_watermark_mb": 400}
    section["game_watch"] = {"enabled": True, "interval_s": 60, "confident_score": 0.82,
                             "confident_margin": 0.05,
                             "anchor_file": os.path.join(tmp, "anchors.jsonl"),
                             "process_names": {}, "mem_watermark_mb": 400}
    config["bilibili"] = section
    print("  临时目录: %s" % tmp)

    runtime = Runtime(config=config, start_native=False, start_terminal=False,
                      log=logging.getLogger("t11"))
    seen = []            # (action, payload, result) —— GUI 发来的命令
    pushes = []          # Agent 推出去的 topic 负载
    gui_live = None
    try:
        await runtime._start_state_and_tools()
        await runtime._start_bilibili()
        await runtime._start_ipc()

        # 记录 GUI 命令与推送（都在真路径上包一层, 不改行为）
        real_control = runtime.bilibili_control

        def spy_control(action, payload=None):
            try:
                out = real_control(action, payload)
            except Exception as exc:                  # noqa: BLE001 - 抛了也要留痕
                seen.append((action, dict(payload or {}),
                             {"ok": False, "error": "SPY 抓到异常: %r" % exc}))
                raise
            seen.append((action, dict(payload or {}), dict(out or {})))
            return out

        runtime.bilibili_control = spy_control
        inner_push = runtime.on_bilibili

        def spy_push(payload):
            pushes.append(dict(payload))
            return inner_push(payload) if inner_push else None

        runtime.on_bilibili = spy_push

        state = part_a_search(runtime, args.keyword) if "a" in want else runtime.bilibili_state()
        if state is None or not state.get("count"):
            print("\n队列是空的（搜索没成功？），后面几段跳过")
        else:
            if "b" in want:
                part_b_cookie(runtime, tmp, args.keyword)
            if "c" in want and not args.no_gui:
                await part_c_playback(runtime, sock, seen, tmp)
                await part_c2_buttons(runtime, sock, seen, tmp)
            elif "c" in want:
                skip("真 GUI 那两段", "--no-gui")
            if "d" in want:
                await part_d_pause(runtime, sock, seen)
            if "e" in want:
                part_e_netcut(runtime, args.keyword, not args.no_netcut)
        if "f" in want:
            part_f_game_watch(runtime, tmp, not args.no_vision)
    finally:
        if gui_live is not None:
            gui_live.stop()
        await runtime.stop()

    # ---- G. 真数据没动 + 不留进程 ----
    print("\n== G. 真数据与残留")
    real_after = {name: md5(os.path.join(REPO, name)) for name in REAL_FILES}
    for name, digest in real_after.items():
        same = real_before[name] == digest
        print("  %-34s %s %s" % (name, digest, "没动 ✔" if same else "**变了** ✘"))
        if not same:
            _FAILED.append("真文件被改: %s" % name)
    leftovers = []
    for _ in range(12):                     # ffmpeg 收到 TERM 要一点时间才退
        leftovers = []
        for name in ("ffmpeg", "agent_gui"):
            rc, out = run(["pgrep", "-a", name], timeout=10)
            if rc == 0 and out.strip():
                leftovers.append("%s: %s" % (name, out.strip().splitlines()[0][:80]))
        if not leftovers:
            break
        time.sleep(0.5)
    check("没有留下 ffmpeg / GUI 进程", not leftovers, "；".join(leftovers))

    print("\n== 结果: %s" % ("全部通过" if not _FAILED else
                            "失败 %d 项: %s" % (len(_FAILED), "、".join(_FAILED))))
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
