#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T13-6 门禁实测: **学习内容监督的板端端到端验收**（真 Runtime / 真 NPU / 真帧 / 真 IPC）。

验的是"这功能真的会按你定的规矩动"，不是"算法多准"（准确率 T13-4 量过）:

  A. **真帧**这一路: 起 native/io（真 moonlight 连 PC）-> 抓一帧 -> 真 SigLIP 编码 ->
     真锚点库判定; 顺手把"流分辨率的几何假设"查清楚（问 PC 当前显示分辨率 + 看有没有黑边）。
  B. **三条升级链**（用假时钟, 不真等 15 分钟）:
       ① 不像学习 -> 气泡「现在是学习时间」（**不算一次不通过**）;
       ② 提醒后连续 3 次不像 -> **返回桌面** + 冷却 30 分钟（冷却里只看不动）;
       ③ 判成学习 -> 计数清零、下一次 30 分钟后;
       ④ **判不出来（带内）-> 一个键都不按**（"不确定时绝不按键"）。
     其中①的**气泡**要从真 IPC 的 `llm` topic 上看到原文（不是看代码）。
  C. **动态阈值可见**: 喂带标签样本 -> 没把握带**正好动 0.01** 且有理由 -> 落盘 -> 重启后还在 ->
     `freeze()` 之后不动。
  D. **不变量**: 真 `config/config.yaml` 一个字节没动（md5）; 板端 `git status` 干净;
     验收用的锚点/统计是**副本**（真那两份不动）; 没留下 moonlight / llama-server 进程。

跑法（板端, 仓库根）::

    python3 tests/board/t13_accept.py                 # 全套（要 PC 开着 Sunshine）
    python3 tests/board/t13_accept.py --no-stream     # 不连串流（B/C/D 照跑, A 段用数据集截图）

⚠ 这是**验收脚本**, 不是生产路径。它**不按真的 Win+D**（那会动你 PC 的窗口）——
  弹桌面那一步用替身记数, 真 `show_desktop()` 早就由 T4 的工具验收过。
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

REPO = _ROOT
DATASET = "/home/kickpi/study_dataset"
TMP = ""
_FAILED = []
_PASSED = []

#: 真数据文件（跑完必须一个字节没变）。
REAL_FILES = ["config/config.yaml", "config/study_anchors.jsonl", "config/study_stats.json"]


# ---------------------------------------------------------------------------
#  小工具（与 t12_accept.py 同款）
# ---------------------------------------------------------------------------
def check(name, ok, detail=""):
    (_PASSED if ok else _FAILED).append(name)
    print("  %s %s%s" % ("✔" if ok else "✘", name, ("  —— %s" % detail) if detail else ""))
    return bool(ok)


def skip(name, why):
    print("  ⊘ %s（%s）" % (name, why))


def note(text):
    print("     · %s" % text)


def md5(path):
    try:
        with open(path, "rb") as handle:
            return hashlib.md5(handle.read()).hexdigest()
    except OSError:
        return "(没有这个文件)"


def run(cmd, timeout=60):
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        return proc.returncode, proc.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, str(exc)


async def wait_for(predicate, timeout_s, what, interval=0.25):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    note("等「%s」超时（%.0f 秒）" % (what, timeout_s))
    return False


def decode(line):
    try:
        return json.loads(line.decode("utf-8", "replace"))
    except (ValueError, AttributeError):
        return None


class PushReader(object):
    """一条**原始客户端连接**（真 Unix socket）—— 抓 Agent 推出来的每一条。"""

    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer

    @classmethod
    async def open(cls, sock):
        reader, writer = await asyncio.open_unix_connection(sock)
        return cls(reader, writer)

    async def collect(self, topics, count, timeout=20):
        wanted, got = set(topics), []
        deadline = time.time() + timeout
        while len(got) < count and time.time() < deadline:
            try:
                line = await asyncio.wait_for(self.reader.readline(),
                                              max(0.5, deadline - time.time()))
            except asyncio.TimeoutError:
                break
            envelope = decode(line)
            if isinstance(envelope, dict) and envelope.get("topic") in wanted:
                got.append(envelope)
        return got

    def close(self):
        with contextlib.suppress(Exception):
            self.writer.close()


class FakeClock(object):
    """假时钟: 升级链要真等 15 分钟, 验收不可能等。"""

    def __init__(self, start=1700000000.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)
        return self.now


class FakeSender(object):
    """替身: 只记"发过几次 Win+D"（真发会动 PC 的窗口）。"""

    def __init__(self):
        self.wins = 0

    async def show_desktop(self):
        self.wins += 1


class FrameReader(object):
    """替身帧源: 每次给一张**真** 256×256 帧（从数据集截图按真管线还原出来）。"""

    def __init__(self, frame):
        self.frame = frame
        self.reads = 0

    async def read_latest(self):
        self.reads += 1
        return self.frame


# ---------------------------------------------------------------------------
#  配置与素材
# ---------------------------------------------------------------------------
def build_config(sock, *, anchors, stats, band):
    """从**真配置**派生一份临时配置（真文件只读）。"""
    from agent.config import load_config

    config = json.loads(json.dumps(load_config("config"), default=str))
    config.setdefault("ipc", {})["socket_path"] = sock
    config["llm"] = dict(config.get("llm") or {})
    config["llm"]["mode"] = "disabled"                  # 不碰 llama-server
    config["llm"]["manage_service"] = False
    bilibili = dict(config.get("bilibili") or {})
    bilibili["enabled"] = True                          # 组件顺序与真机一致
    watch = dict(bilibili.get("game_watch") or {})
    watch["enabled"] = False                            # 不 ssh 问进程, 也不让它抓画面
    watch["interval_s"] = 3600
    bilibili["game_watch"] = watch
    config["bilibili"] = bilibili
    config["study"] = {
        "enabled": True,
        "anchor_file": anchors,                         # **副本**（真那两份不动）
        "stats_file": stats,
        "relative_band": band,
        "remind": True,
        "back_to_desktop": True,
        "process_names": {},                            # 验收不依赖 PC 进程名
    }
    return config


def load_frames():
    """数据集里每类取一张, 过**真管线**（`frame_pipeline`）变成板子看到的 256×256。"""
    from agent.vision import frame_pipeline as fp

    out = {}
    for name in ("code", "doc", "real", "anime", "game"):
        folder = os.path.join(DATASET, name)
        if not os.path.isdir(folder):
            continue
        for entry in sorted(os.listdir(folder)):
            path = os.path.join(folder, entry)
            try:
                out[name] = fp.from_screenshot(path)
                break
            except Exception as exc:                    # noqa: BLE001
                note("读不出 %s: %s" % (path, exc))
    return out


def scan_dataset(watcher):
    """数据集**逐张**过真管线 + 真 NPU, 回 [{"cls","path","relative","verdict","frame"}]。

    ⚠ 这是**自匹配**（这些帧就是锚点库的来源, T13-4 播的种）—— 所以它**不是准确率**,
      只说明"判定这条路通不通、带的分界落在哪儿"。真正的准确率（留一法）在 T13-4 的报告里。
    """
    from agent.vision import frame_pipeline as fp

    rows = []
    for name in ("code", "doc", "real", "anime", "game"):
        folder = os.path.join(DATASET, name)
        if not os.path.isdir(folder):
            continue
        for entry in sorted(os.listdir(folder)):
            path = os.path.join(folder, entry)
            try:
                frame = fp.from_screenshot(path)
            except Exception as exc:                    # noqa: BLE001
                note("读不出 %s: %s" % (path, exc))
                continue
            decision = watcher.verdict(frame)
            rows.append({"cls": name, "path": path, "relative": decision["relative"],
                         "verdict": decision["verdict"], "frame": frame})
    return rows


def pick(rows, *, want_study):
    """挑一张**真的落在带外**的帧（升级链要验的是链, 不是分类器）。

    @return (row 或 None, 为什么)
    """
    low = None
    for row in rows:
        relative = row["relative"]
        if relative is None:
            continue
        if want_study and relative > 0 and (low is None or relative > low["relative"]):
            low = row
        if not want_study and relative < 0 and (low is None or relative < low["relative"]):
            low = row
    if low is None:
        return None, "数据集里没有落在带外的帧（相对分全是 0 / 算不出来）"
    return low, "挑了相对分 %+.3f 的「%s」（%s）" % (low["relative"], low["cls"],
                                                  os.path.basename(low["path"]))


def host_display():
    """问 PC: 现在显示分辨率是多少（串流时 Sunshine 可能把主机模式切成客户端的）。

    @note 只读 WMI, **不走交互桌面** —— 所以 sshd 在 session 0 也能问到（窗口标题才拿不到）。
    """
    from agent.config import load_config
    from agent.net.netease_cli import NeteaseCli

    music = dict((load_config("config") or {}).get("music") or {})
    helper = NeteaseCli(host=str(music.get("pc_host") or ""), user=str(music.get("pc_user") or ""),
                        port=int(music.get("pc_port") or 22),
                        ssh=str(music.get("ssh") or "ssh"), timeout_s=20.0)
    if not music.get("pc_host"):
        return ""
    remote = ('powershell -NoProfile -Command "'
              'Get-CimInstance Win32_VideoController | '
              'Select-Object -First 1 -ExpandProperty CurrentHorizontalResolution; '
              'Get-CimInstance Win32_VideoController | '
              'Select-Object -First 1 -ExpandProperty CurrentVerticalResolution"')
    rc, out = run(helper._ssh_argv(remote), timeout=25)
    return out.strip() if rc == 0 else ""


def black_bars(frame):
    """一帧上/下/左/右各有几行/列是**接近全黑**的（letterbox 会留黑边）。

    @note 这条能区分"拉伸铺满"与"等比缩放留黑边" —— 也就是 h1 与 h2 的差别。
    """
    import numpy as np

    arr = np.asarray(frame)
    if arr.ndim == 4:
        arr = arr[0]
    rows = arr.mean(axis=(1, 2))
    cols = arr.mean(axis=(0, 2))
    bright_rows = np.where(rows > 12)[0]
    bright_cols = np.where(cols > 12)[0]
    if not len(bright_rows) or not len(bright_cols):
        return {"top": len(rows), "bottom": len(rows), "left": len(cols), "right": len(cols)}
    return {"top": int(bright_rows[0]),
            "bottom": int(len(rows) - 1 - bright_rows[-1]),
            "left": int(bright_cols[0]),
            "right": int(len(cols) - 1 - bright_cols[-1])}


# ---------------------------------------------------------------------------
#  A. 真帧 + 几何
# ---------------------------------------------------------------------------
async def part_a(runtime, frames, no_stream):
    print("\n== A. 真帧这一路（真 moonlight 帧 / 真 NPU / 真锚点库）")
    import numpy as np

    from agent.core.state_machine import State

    watcher = runtime._study_watch
    # ⚠ 先把状态切到 STUDY: 共用的那份 SigLIP 是**按状态常驻**的（认游戏那个循环每 2 秒
    #   `ensure_model(state)`）—— 不在 STUDY/GAME 时它会把模型卸掉, 那时编码拿不到向量。
    if runtime.state.current() is not State.STUDY:
        runtime.state.transition_to(State.STUDY, "T13-6 验收: 进学习时间")
        await asyncio.sleep(0.5)
    if runtime._game_watch is not None:
        runtime._game_watch.ensure_model("STUDY")
    real_frame = None
    black_frames = 0
    if no_stream:
        skip("真串流帧", "--no-stream")
    else:
        ok = await wait_for(lambda: runtime.image_reader is not None, 10, "image_reader 就位")
        if ok:
            # ⚠ 刚起来那一帧常常是**全黑**的（解码器/编码器还没吐真画面）—— 所以要
            #   一直等到"有内容"的一帧（最多 30 s）; 全都黑就是环境问题, 如实说。
            for _ in range(60):
                frame = await runtime.image_reader.read_latest()
                if frame is not None:
                    if float(np.asarray(frame).max()) > 8:
                        real_frame = frame
                        break
                    black_frames += 1
                await asyncio.sleep(0.5)
    if real_frame is None and not no_stream and black_frames:
        skip("真串流帧有内容", "拿到 %d 帧但**全是黑的** —— PC 那边多半锁屏/显示器休眠了；"
                            "这一条要 PC 醒着才能量" % black_frames)
    if real_frame is None and not no_stream and not black_frames:
        skip("真串流帧", "一帧都没来（串流没起来？）")
    if real_frame is not None:
        arr = np.asarray(real_frame)
        check("真串流帧到手: (256, 256, 3) uint8 且有内容",
              arr.shape == (256, 256, 3) and arr.dtype == np.uint8,
              "shape=%s dtype=%s 均值=%.1f 最大=%d%s"
              % (arr.shape, arr.dtype, arr.mean(), arr.max(),
                 "（前面丢掉了 %d 帧全黑的）" % black_frames if black_frames else ""))
        resolution = host_display()
        if resolution:
            numbers = [line.strip() for line in resolution.splitlines() if line.strip()]
            note("PC 当前显示分辨率: %s" % " × ".join(numbers[:2]))
            if numbers[:2] == ["1280", "720"]:
                check("PC 显示分辨率 == 流分辨率（Sunshine 把主机切成 1280×720 了）", True,
                      "那「整屏截图 → 1280×720」这一步在真机上就是**主机自己**做的")
            else:
                note("PC 显示分辨率 %s ≠ 流分辨率 1280×720 —— 说明 Sunshine 是在**缩放**"
                     % "×".join(numbers[:2]))
        else:
            skip("问 PC 显示分辨率", "ssh 不通 / 没配 pc_host")
        bars = black_bars(real_frame)
        dark_cols = bars["left"] + bars["right"]
        note("真帧黑边（上/下/左/右）: %s（整帧均值 %.1f）" % (bars, arr.mean()))
        if arr.mean() < 40:
            note("⚠ 画面整体很暗 —— 黑边这条**判不出来**（暗画面本身就接近「黑」）; "
                 "要判几何得让 PC 显示亮内容, 或者读 PC 的分辨率（下面那条）")
        elif dark_cols > 40:
            note("几何: 左右各有 %d 列接近全黑 -> 内容比 16:9 窄, 两侧补黑"
                 % (dark_cols // 2))
        elif bars["top"] + bars["bottom"] > 20:
            note("几何: 上下有黑边 -> 内容比 16:9 矮, 上下补黑")
        else:
            note("几何: 四边都没有大片黑边 -> 画面铺满 16:9")
        decision = watcher.verdict(real_frame)
        note("真帧判定: %s（来源 %s，相对分 %s，band %.2f）%s"
             % (decision["verdict"], decision["source"] or "-", decision["relative"],
                decision["band"], decision["note"][:60]))
        check("真帧能算出相对分（两个大类原型都在）", decision["relative"] is not None)

    check("数据集五类各一张都能过真管线", len(frames) == 5, "拿到 %s" % sorted(frames))


# ---------------------------------------------------------------------------
#  A2. 数据集全量扫一遍（自匹配, 只看"带的分界落在哪儿"）
# ---------------------------------------------------------------------------
def part_a2(rows):
    print("\n== A2. 数据集 %d 张全过真 NPU（⚠ 自匹配: 这些帧就是锚点库的来源）" % len(rows))
    from agent.core.study_watch import (VERDICT_NOT_STUDY, VERDICT_STUDY, VERDICT_UNKNOWN)

    check("全量扫完（每张都算出了相对分）",
          bool(rows) and all(row["relative"] is not None for row in rows),
          "%d 张" % len(rows))
    table = {}
    for row in rows:
        table.setdefault(row["cls"], {}).setdefault(row["verdict"], 0)
        table[row["cls"]][row["verdict"]] += 1
    for name in ("code", "doc", "real", "anime", "game"):
        if name in table:
            counts = table[name]
            note("%-6s（%s 类）: study %d / not_study %d / unknown %d"
                 % (name, "学习" if name in ("code", "doc", "real") else "非学习",
                    counts.get(VERDICT_STUDY, 0), counts.get(VERDICT_NOT_STUDY, 0),
                    counts.get(VERDICT_UNKNOWN, 0)))
    study_rows = [row for row in rows if row["cls"] in ("code", "doc", "real")]
    naughty_rows = [row for row in rows if row["cls"] in ("anime", "game")]
    wrong_way = [row for row in study_rows if row["verdict"] == VERDICT_NOT_STUDY]
    check("自匹配下**没有一张学习帧被判成不像学习**（那会误弹气泡）",
          not wrong_way, "%d 张" % len(wrong_way))
    note("非学习帧里落在带内的: %d/%d（带内 = unknown, 不会打扰你）"
         % (len([r for r in naughty_rows if r["verdict"] == VERDICT_UNKNOWN]),
            len(naughty_rows)))


# ---------------------------------------------------------------------------
#  B. 三条升级链（假时钟 + 真帧 + 真 IPC 气泡）
# ---------------------------------------------------------------------------
async def part_b(runtime, sock, frames, rows, no_stream):
    print("\n== B. 升级链: 提醒 -> 3 次 -> 返回桌面 -> 冷却（假时钟, 不真等）")
    from agent.core.study_watch import (ACTION_BACK_TO_DESKTOP, ACTION_REMIND, REMIND_TEXT,
                                        VERDICT_NOT_STUDY, VERDICT_STUDY, VERDICT_UNKNOWN)

    watcher = runtime._study_watch
    clock = FakeClock()
    watcher.clock = clock                                # 只换时钟, 逻辑全真
    watcher.reset_cycle()
    # ⚠ 背景那个 **2 秒心跳**会按**真时钟**改 `_next_at`（那是产品行为, 没错）—— 但假时钟
    #   的确定性链会被它一句"还没到点"挡掉。所以这一段先把它停下来, ⑤ 再起一个新的。
    task, runtime._study_task = runtime._study_task, None
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    from agent.core.state_machine import State

    if runtime.state.current() is not State.STUDY:
        runtime.state.transition_to(State.STUDY, "T13-6 验收")
    await asyncio.sleep(0.2)

    # ⚠ 升级链验的是**链**, 不是分类器: 所以在**量出来的**结果里挑两张真落在带外的帧
    #   （"数据集里有没有这样的帧"本身就是 A2 那个表要回答的事）。
    study_row, why_study = pick(rows, want_study=True)
    naughty_row, why_naughty = pick(rows, want_study=False)
    note("选帧: %s；%s" % (why_study, why_naughty))
    if study_row is None or naughty_row is None:
        skip("升级链", "数据集里挑不出「带外」的帧 —— 这一轮带太宽或库分不开")
        return
    study_frame = study_row["frame"]
    naughty_frame = naughty_row["frame"]

    # ① 不像学习 -> 提醒（**不算一次不通过**）
    decision = watcher.tick(naughty_frame, state="study")
    check("① 不像学习 -> 提醒, 且**不计入**那 3 次",
          decision["action"] == ACTION_REMIND and decision["failures"] == 0
          and decision["verdict"] == VERDICT_NOT_STUDY,
          "verdict=%s action=%s failures=%s skipped=%s 相对分=%s"
          % (decision["verdict"], decision["action"], decision["failures"],
             decision["skipped"] or "-", decision["relative"]))
    check("气泡原话 = 「%s」" % REMIND_TEXT, decision["text"] == REMIND_TEXT,
          decision["text"])

    # ② 5 分钟一查, 三次之后弹桌面
    seen = []
    for _ in range(3):
        clock.advance(5 * 60)
        seen.append(watcher.tick(naughty_frame, state="study"))
    check("② 第 1/2 次只记数不动作",
          [item["failures"] for item in seen[:2]] == [1, 2]
          and seen[0]["action"] == "" and seen[1]["action"] == "")
    check("② 第 3 次 -> 返回桌面, 且**不补第二句话**",
          seen[2]["action"] == ACTION_BACK_TO_DESKTOP and seen[2]["text"] == "",
          seen[2]["note"][:60])
    clock.advance(60)
    cooling = watcher.tick(naughty_frame, state="study")
    check("② 冷却里**只看不动**", cooling["action"] == "" and "冷却" in cooling["note"])

    # ③ 判成学习 -> 清零 + 下一次 30 分钟
    clock.advance(29 * 60)
    watcher._cooldown_until = 0.0                        # 直接结束冷却, 验"回到正轨"那一条
    decision = watcher.tick(study_frame, state="study")
    check("③ 判成学习 -> failures 清零、下一次 30 分钟",
          decision["verdict"] == VERDICT_STUDY and decision["failures"] == 0
          and abs(decision["next_in_s"] - 30 * 60) < 2,
          "next_in_s=%.0f" % decision["next_in_s"])

    # ④ 判不出来 -> 一个键都不按（把带调到 0.5, 真帧也落在带内）
    watcher.stats.set_threshold("relative_band", 0.5)
    before = (runtime._study_stats.counter("reminded"),
              runtime._study_stats.counter("back_to_desktop"))
    watcher._next_at = 0.0
    sender = FakeSender()
    runtime.input_sender = sender
    decision = watcher.tick(study_frame, state="study")
    after = (runtime._study_stats.counter("reminded"),
             runtime._study_stats.counter("back_to_desktop"))
    check("④ 带内 -> unknown、**不提醒不弹桌面**、计数不动",
          decision["verdict"] == VERDICT_UNKNOWN and decision["action"] == ""
          and before == after, "band=0.5, 相对分 %s" % decision["relative"])
    watcher.stats.set_threshold("relative_band", 0.05)

    # ⑤ 气泡**真的从 IPC 出去了**（真 socket, 真 topic）
    if no_stream:
        skip("IPC 气泡", "--no-stream")
    else:
        reader = await PushReader.open(sock)
        loop_task = None
        try:
            watcher.reset_cycle()
            runtime.image_reader = FrameReader(naughty_frame)   # 让循环自己判一次
            loop_task = asyncio.ensure_future(runtime._study_watch_loop())
            pushes = await reader.collect(["llm"], 1, timeout=20)
            text = json.dumps(pushes[0], ensure_ascii=False) if pushes else ""
            check("⑤ 提醒**真的推到了 IPC 的 llm topic**（气泡原话）",
                  bool(pushes) and REMIND_TEXT in text, text[:110])
        finally:
            if loop_task is not None:
                loop_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await loop_task
            reader.close()
            runtime.image_reader = None


# ---------------------------------------------------------------------------
#  C. 动态阈值可见（挪 0.01 / 有理由 / 落盘 / 重启还在 / freeze 停）
# ---------------------------------------------------------------------------
async def part_c(runtime, stats_copy):
    print("\n== C. 没把握带能自己挪（一次 0.01、有理由、落盘、重启还在）")
    watcher = runtime._study_watch
    watcher.stats.set_threshold("relative_band", 0.05)
    watcher.reset_cycle()
    before = watcher.relative_band
    for _ in range(watcher.min_labeled):
        watcher.learn_score(0.10, label="study", margin=0.10)
        watcher.learn_score(-0.10, label="not_study", margin=0.10)
    moved = watcher.adapt()
    after = watcher.relative_band
    check("喂够带标签样本 -> 带**正好挪一步 0.01**",
          moved["moved"] and abs(abs(before - after) - 0.01) < 1e-9,
          "%.3f -> %.3f" % (before, after))
    notes = [item["text"] for item in watcher.stats.notes()]
    check("每次挪都写了**人能看懂的理由**",
          any("没把握带" in text and "一次只动 0.01" in text for text in notes),
          (notes[-1][:90] if notes else ""))
    watcher.stats.save()
    with open(stats_copy, encoding="utf-8") as handle:
        saved = json.load(handle)
    check("挪过的带**落盘**了（study_stats.json 里看得到）",
          abs(float(saved["thresholds"]["relative_band"]) - after) < 1e-9,
          "文件里 %.3f" % float(saved["thresholds"]["relative_band"]))

    from agent.core.study_stats import StudyStats
    from agent.core.study_anchors import StudyAnchors
    from agent.core.study_watch import StudyWatcher

    again = StudyWatcher(StudyAnchors(runtime._study_anchors.path),
                         stats=StudyStats(stats_copy), encoder=None)
    again.stats.load()
    check("**重启后**还是学到的那个带（不退回标定初值）",
          abs(again.relative_band - after) < 1e-9,
          "新实例读到 %.3f" % again.relative_band)

    again.freeze()
    before_frozen = again.relative_band
    for _ in range(again.min_labeled):
        again.learn_score(0.20, label="study")
        again.learn_score(0.10, label="not_study")
    again.adapt()
    check("freeze() 之后一个字都不挪", abs(again.relative_band - before_frozen) < 1e-9)


# ---------------------------------------------------------------------------
#  D. 不变量
# ---------------------------------------------------------------------------
def part_d(real_before, sock):
    print("\n== D. 不变量（真文件没动 / 干净收场）")
    for name, digest in real_before.items():
        check("真文件 %s 一个字节没动" % name, md5(os.path.join(REPO, name)) == digest,
              md5(os.path.join(REPO, name)))
    rc, out = run(["git", "status", "--porcelain"], timeout=20)
    check("板端 git status 干净（派生数据都被忽略）", rc == 0 and not out.strip(), out.strip()[:80])
    leftovers = []
    for name in ("moonlight", "llama-server", "agent.cli"):
        rc, out = run(["pgrep", "-a", name], timeout=10)
        if rc == 0 and out.strip():
            leftovers.append("%s: %s" % (name, out.strip().splitlines()[0][:70]))
    check("没留下 moonlight / llama-server / CLI 进程", not leftovers, "；".join(leftovers))
    check("验收用的 socket 已经收掉（没留下 /tmp 里的套接字）", not os.path.exists(sock))


# ---------------------------------------------------------------------------
async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-stream", action="store_true",
                        help="不连串流（跳过真帧与 IPC 气泡那两段）")
    args = parser.parse_args()

    global TMP
    print("== 学习内容监督 · 板端端到端验收（T13-6）")
    print("== 真数据 md5（跑之前）")
    real_before = {name: md5(os.path.join(REPO, name)) for name in REAL_FILES}
    for name, digest in real_before.items():
        print("  %-28s %s" % (name, digest))

    TMP = tempfile.mkdtemp(prefix="t13-")
    sock = os.path.join(TMP, "t13.sock")
    anchors_copy = os.path.join(TMP, "study_anchors.jsonl")
    stats_copy = os.path.join(TMP, "study_stats.json")
    for name, target in (("config/study_anchors.jsonl", anchors_copy),
                         ("config/study_stats.json", stats_copy)):
        source = os.path.join(REPO, name)
        if os.path.exists(source):
            shutil.copy2(source, target)
    print("  临时目录: %s（锚点/统计用的是**副本**）" % TMP)

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(logging.INFO)
    log_path = os.path.join(TMP, "agent.log")
    handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)

    from agent.main import Runtime

    config = build_config(sock, anchors=anchors_copy, stats=stats_copy, band=0.05)
    runtime = Runtime(config=config, start_native=not args.no_stream, start_terminal=False,
                      log=logging.getLogger("agent.t13"))
    try:
        await runtime._start_bus_and_io()
        await runtime._start_native()                    # 真串流（moonlight 连 PC）
        await runtime._start_bilibili()
        await runtime._start_study()
        await runtime._start_state_and_tools()
        await runtime._start_ipc()
        if not args.no_stream:
            await wait_for(lambda: runtime.image_reader is not None, 20, "io 层就位")
        frames = load_frames()
        await part_a(runtime, frames, args.no_stream)
        rows = scan_dataset(runtime._study_watch) if not args.no_stream else []
        if rows:
            part_a2(rows)
        else:
            skip("数据集全量扫（自匹配）", "--no-stream（没模型就不扫了）")
        await part_b(runtime, sock, frames, rows, args.no_stream)
        await part_c(runtime, stats_copy)
        if runtime._study_anchors is not None:
            check("验收全程锚点库是副本（真那两份没被写）",
                  runtime._study_anchors.path == anchors_copy, runtime._study_anchors.path)
    finally:
        with contextlib.suppress(Exception):
            await runtime.stop()
        await asyncio.sleep(1.0)
        part_d(real_before, sock)
        shutil.rmtree(TMP, ignore_errors=True)

    print("\n== 结果: %s（%d 项通过%s）"
          % ("全部通过" if not _FAILED else "失败 %d 项" % len(_FAILED), len(_PASSED),
             (", 失败: " + "、".join(_FAILED)) if _FAILED else ""))
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
