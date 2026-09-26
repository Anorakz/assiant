#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T12 板端验收 — **严格状态路径 + 日程到点 + 工具写入**（真 Runtime / 真 Scheduler / 真 CLI）。

跑法（板端, 仓库根）::

    python3 tests/board/t12_accept.py                  # 全跑（含"真等一分钟"那段, 约 5~7 分钟）
    python3 tests/board/t12_accept.py --no-real-clock  # 跳过真时钟那一段（快跑）
    python3 tests/board/t12_accept.py --no-llm-service # 不管 llama-server 的起停

分段（都用**真代码**: 真 Runtime + 真 Scheduler + 真 CLI 子进程 + 真 Unix socket）:

    A. **注入时刻**的日程触发: `GAME --(日程到点)--> IDLE --(第二跳)--> SLEEP`
       · 逐跳的"离开/进入"次序（spy 记下每一步的 state）—— 这就是 T12 要的
         "先到 idle 释放那个模式的东西, 再从 idle 释放, 最后到 sleep"
       · 离开 GAME 时**真的**停了缓冲、清了队列、卸了 SigLIP（GAME 里它是常驻的）
       · 展示行真的上线（`日程到点：切到 SLEEP（HH:MM）`）, 且**不进对话总线**（spy 盯 bus）
       · 事实里带两跳 `steps`; R3 把那条一次性日程从**临时配置**里删掉
    A2. **llama-server**: 启动时起得来; 进 SLEEP 停、离开 SLEEP 起（`/v1/models` 探活）
    B. **真时钟**的日程触发（`interval_min=1` 的检查循环自己跑到点）: 真 CLI `watch`
       从线上收到 `status(IDLE)` / `status(SLEEP)` / `schedule(fired)` / `llm(展示行)` 四条
    C. **工具写入 + 热重载**: `runtime.schedule_add/list/remove`（工具的三条 Runtime 入口）
       → 调度器**立刻**多一条/少一条 + 文件逐字节回原文
    D. **CLI**: `assistant schedule` 的行格式（`HH:MM  状态` + `← 已触发`）、
       `assistant mode sleep` 的两跳文案（"路上经过 IDLE"）
    E. 真 `config/config.yaml` md5 不变；不留进程（ffmpeg / agent_gui / llama-server）

⚠ 这是**验收脚本**（要板端 python3.8 + 真 socket）, 不进 `scripts/test-python.*` 的常规清单。
⚠ 它**只写临时配置**（`/tmp/t12-*/config.yaml`）: 真 `config/config.yaml` 只读来当模板 + md5 核对。
⚠ 没联网/没 cookie 也能跑: 视频那一路只验"离开 GAME 时缓冲与队列被放掉"（不会真去下视频）。
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
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                                  line_buffering=True)

from agent.config import load_config                                 # noqa: E402
from agent.core.state_machine import State                           # noqa: E402
from agent.ipc.protocol import decode                                # noqa: E402
from agent.main import Runtime                                       # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
REAL_FILES = ("config/config.yaml",)
SAMPLES_DIR = "/home/kickpi/game_samples"

#: 展示行的形状（与 agent/ipc/__init__.py::_schedule_fired_text 一致）
LINE_RE = re.compile(r"^日程到点：切到 (SLEEP|IDLE|STUDY|GAME)（\d\d:\d\d）$")
#: 日程行的形状（`HH:MM  状态`, 后面可能跟着 `← 已触发 HH:MM:SS`）
ROW_RE = re.compile(r"^\d\d:\d\d  (SLEEP|IDLE|STUDY|GAME)(\s+←.*)?$")

_FAILED = []
_TMP = ""


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


def slurp(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def run(cmd, timeout=60):
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=timeout)
        return proc.returncode, proc.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, str(exc)


async def wait_for(predicate, timeout_s, what, interval=0.25):
    """等一个条件成立（**async**: 阻塞 sleep 会把同一个事件循环上的 IPC 卡死）。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    note("等「%s」超时（%.0f 秒）" % (what, timeout_s))
    return False


async def run_cli(sock, *args, timeout=30):
    """跑一次**真 CLI 子进程**（`python3 -m agent.cli …`），回 (rc, stdout, stderr)。"""
    cmd = [sys.executable, "-m", "agent.cli", "--socket", sock, "--timeout", "6"] + list(args)
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=REPO, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "", "（%.0f 秒没跑完）" % timeout
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


class PushReader(object):
    """一条**原始客户端连接**（真 Unix socket）—— 用来抓 Agent 推出来的每一条。"""

    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer

    @classmethod
    async def open(cls, sock):
        reader, writer = await asyncio.open_unix_connection(sock)
        return cls(reader, writer)

    async def next(self, timeout=15):
        line = await asyncio.wait_for(self.reader.readline(), timeout)
        return decode(line)

    async def collect(self, topics, count, timeout=20):
        """收够 count 条**属于 topics** 的推送（别的 topic 丢掉）。

        @note 新客户端连上时 Agent 会补推壁纸/音乐/队列几行（见 `on_client_connect`）——
              所以不能天真地"读 N 行", 得按 topic 过滤。
        """
        wanted = set(topics)
        got = []
        deadline = time.time() + timeout
        while len(got) < count and time.time() < deadline:
            try:
                topic, data = await self.next(timeout=max(0.5, deadline - time.time()))
            except (asyncio.TimeoutError, ValueError):
                break
            if topic in wanted:
                got.append((topic, data))
        return got

    def close(self):
        try:
            self.writer.close()
        except Exception:                                 # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
#  临时配置
# ---------------------------------------------------------------------------
def build_config(sock, oneoff, *, manage_llm):
    """从**真配置**派生一份临时配置（真文件只读, 一个字节都不动）。"""
    config = json.loads(json.dumps(load_config("config"), default=str))
    config.setdefault("ipc", {})["socket_path"] = sock
    config["llm"] = dict(config.get("llm") or {})
    config["llm"]["manage_service"] = bool(manage_llm)
    if not manage_llm:
        config["llm"]["mode"] = "disabled"          # 不碰 llama-server, 快且确定
    section = dict(config.get("scheduler") or {})
    section.update({"interval_min": 1, "window_min": 1, "late_grace_min": 0,
                    "remove_fired_oneoff": True, "recurring": [], "oneoff": list(oneoff)})
    config["scheduler"] = section
    bilibili = dict(config.get("bilibili") or {})
    bilibili["enabled"] = True                      # 队列要在（离开 GAME 时真的清一次）
    watch = dict(bilibili.get("game_watch") or {})
    watch.update({"enabled": False,                 # 不 ssh 到 PC, 也不让循环自己抓画面
                  "interval_s": 3600,
                  "anchor_file": os.path.join(_TMP, "anchors.jsonl")})
    bilibili["game_watch"] = watch
    config["bilibili"] = bilibili
    return config


def write_config(path, config):
    import yaml
    with open(path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(config, handle, allow_unicode=True, sort_keys=False)


def next_minute(now, seconds=95):
    """挑一个**还没到**的时刻（下一分钟的整点）—— 日程写它, 检查循环自己会走到。"""
    return (now + timedelta(seconds=seconds)).replace(second=0, microsecond=0)


# ---------------------------------------------------------------------------
#  A. 注入时刻的触发（逐跳释放 + 展示行 + 事实 + R3 删除）
# ---------------------------------------------------------------------------
async def part_a(runtime, sock, trigger_at, log_path):
    print("\n== A. 注入时刻的日程触发: GAME -> IDLE -> SLEEP")
    releases, enters, bus_pushes, vision = [], [], [], []

    real_release, real_enter = runtime._release_state, runtime._enter_state
    real_vision, real_bus_push = runtime._release_vision, runtime.bus.push

    def spy_release(state):
        releases.append(state.value)
        return real_release(state)

    def spy_enter(state):
        enters.append(state.value)
        return real_enter(state)

    def spy_vision(why):
        vision.append(why)
        return real_vision(why)

    async def spy_bus_push(source, text):
        bus_pushes.append((source, text))
        return await real_bus_push(source, text)

    runtime._release_state = spy_release
    runtime._enter_state = spy_enter
    runtime._release_vision = spy_vision
    runtime.bus.push = spy_bus_push

    result = runtime.state.transition_to(State.GAME, "T12 验收 A")
    check("先进 GAME（走状态图, 不是直接赋值）",
          bool(result.get("ok")) and runtime.state.current() is State.GAME,
          str(result.get("steps")))

    # GAME 里 SigLIP 是**常驻**的（`_game_watch_loop` 每 2 秒 `ensure_model`）——
    # 真加载得上（要 NPU + 模型文件）才谈得上"第一跳把它卸下来"。
    loaded_before = False
    if runtime._game_watch is None:
        skip("SigLIP 常驻", "没有 game_watch（bilibili 没开）")
    else:
        loaded_before = await wait_for(lambda: runtime._game_watch.model_loaded, 30,
                                       "GAME 里 SigLIP 常驻")
        if loaded_before:
            note("GAME 里 SigLIP 真的加载了（常驻）")
        else:
            skip("SigLIP 真加载", "模型没上来（NPU/模型文件?）—— 只验释放钩子")
    queue_before = runtime.bilibili_state().get("count") if runtime.bilibili else None

    reader = await PushReader.open(sock)
    try:
        # ⚠ 等服务端**真的注册**了这个客户端（`open_unix_connection` 返回 ≠ 服务端已 accept）:
        #   不等就可能"推送发出去时 0 个客户端", 于是什么都收不到。
        await wait_for(lambda: int(getattr(runtime.ipc, "client_count", 0) or 0) >= 1,
                       5, "客户端连上（client_count>=1）")
        releases[:] = []
        enters[:] = []
        bus_pushes[:] = []
        vision[:] = []

        fired = await runtime.scheduler.check_schedule(trigger_at + timedelta(seconds=5))
        check("到点那一刻真的触发了 1 条", len(fired) == 1, str(len(fired)))
        check("逐跳离开: 先 GAME 再 IDLE", releases == ["game", "idle"], str(releases))
        check("逐跳进入: 先 IDLE 再 SLEEP", enters == ["idle", "sleep"], str(enters))
        check("最后停在 SLEEP", runtime.state.current() is State.SLEEP,
              runtime.state.current().value)
        check("第一跳就调了「释放视觉」（离开 GAME）", bool(vision) and vision[0] == "离开 GAME",
              str(vision))
        check("离开 GAME 后视频流已清空", runtime._bilibili_stream == "",
              repr(runtime._bilibili_stream))
        if runtime.bilibili is not None:
            check("离开 GAME 后队列是空的", runtime.bilibili_state().get("count") == 0,
                  "before=%s after=%s" % (queue_before, runtime.bilibili_state().get("count")))
        if loaded_before:
            check("第一跳把 SigLIP **真的**卸下来了",
                  runtime._game_watch.model_loaded is False)

        facts = runtime.scheduler.recent_fired()
        fact = facts[-1] if facts else {}
        steps = (fact.get("actions") or [{}])[0].get("steps") or []
        check("事实里 state=sleep", fact.get("state") == "sleep", str(fact.get("state")))
        check("事实里带两跳 steps",
              steps == [{"from": "game", "to": "idle"}, {"from": "idle", "to": "sleep"}],
              str(steps))
        import yaml as _yaml
        with open(runtime.config_path_used, encoding="utf-8") as handle:
            oneoffs = (_yaml.safe_load(handle) or {}).get("scheduler", {}).get("oneoff") or []
        check("R3: 那条一次性日程已从**临时配置**里删掉", oneoffs == [], str(oneoffs))

        pushed = await reader.collect(("status", "schedule", "llm"), 4, timeout=20)
        topics = [topic for topic, _ in pushed]
        check("线上收到 status / status / schedule / llm",
              topics == ["status", "status", "schedule", "llm"], str(topics))
        modes = [data.get("mode") for topic, data in pushed if topic == "status"]
        check("两条 status 是 IDLE -> SLEEP", modes == ["IDLE", "SLEEP"], str(modes))
        line = next((str(data.get("text") or "") for topic, data in pushed if topic == "llm"), "")
        check("展示行是「日程到点：切到 SLEEP（HH:MM）」",
              bool(LINE_RE.match(line)) and line.endswith(trigger_at.strftime("%H:%M）")), line)
        check("展示行**没有**进对话总线（不叫 LLM）", bus_pushes == [], str(bus_pushes))

        text = slurp(log_path)
        check("日志里有「离开 GAME —— 视频停了、队列清了、SigLIP 放掉了」",
              "离开 GAME —— 视频停了、队列清了、SigLIP 放掉了" in text)
        check("日志里有「离开 IDLE」那一行（第二跳照样走一遍）", "离开 IDLE" in text)
    finally:
        reader.close()
        runtime._release_state = real_release
        runtime._enter_state = real_enter
        runtime._release_vision = real_vision
        runtime.bus.push = real_bus_push


# ---------------------------------------------------------------------------
#  B. 真时钟的触发（检查循环自己跑到点; 真 CLI watch 从线上收）
# ---------------------------------------------------------------------------
async def part_b(runtime, sock, log_path, after):
    print("\n== B. 真时钟的日程触发（interval_min=1 的循环自己等）")
    # ⚠ 必须挑一个**与 A 段不同**的分钟: 去重键是 `来源|状态|日期|分钟`
    #   （`ScheduleEvent.dedup_key`, **不含 oneoff/recurring**）—— 同一分钟、同一个目标状态
    #   的两条会被当成"同一条", A 段注入触发过的那一分钟就不会再触发（这是刻意的去重,
    #   不是 bug: 同一时刻切到同一个状态, 做一次就够了）。
    now = datetime.now()
    trigger_at = after + timedelta(minutes=1)
    while trigger_at <= now + timedelta(seconds=20):
        trigger_at += timedelta(minutes=1)
    print("  日程写在 %s（现在 %s）—— 等它自己到点"
          % (trigger_at.strftime("%H:%M"), now.strftime("%H:%M:%S")))

    added = runtime.schedule_add({"state": "sleep", "start": trigger_at.strftime("%H:%M")})
    if not check("把这条日程加进临时配置（走工具那条入口）",
                 added.get("ok") and added.get("hot") and runtime.scheduler.events,
                 str(added.get("note")) or "%d 条日程" % len(runtime.scheduler.events)):
        return
    runtime.state.transition_to(State.GAME, "T12 验收 B")

    watcher = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "agent.cli", "--socket", sock, "--timeout", "6",
        "watch", "--topics", "status,schedule,llm", "--count", "4",
        cwd=REPO, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(watcher.communicate(), timeout=300)
        text = out.decode("utf-8", "replace")
    except asyncio.TimeoutError:
        watcher.kill()
        await watcher.wait()
        text = ""
        note("watch 等了 300 秒还没收够 4 条")
    print("  $ assistant watch --topics status,schedule,llm --count 4")
    for line in (text or "（没有输出）").strip().splitlines()[-6:]:
        print("    %s" % line)
    check("真 CLI 收到 mode=IDLE", "mode=IDLE" in text)
    check("真 CLI 收到 mode=SLEEP", "mode=SLEEP" in text)
    check("真 CLI 收到 kind=fired 且 state=sleep",
          "kind=fired" in text and "state=sleep" in text)
    check("真 CLI 收到那行展示文本", "日程到点：切到 SLEEP" in text)
    check("Agent 真的停在了 SLEEP（真时钟那次也一样）",
          runtime.state.current() is State.SLEEP, runtime.state.current().value)
    check("日志里有第二次「离开 GAME」",
          slurp(log_path).count("离开 GAME —— 视频停了") >= 2,
          str(slurp(log_path).count("离开 GAME —— 视频停了")))


# ---------------------------------------------------------------------------
#  C. 工具写入 + 热重载（真 Runtime 的三条入口）
# ---------------------------------------------------------------------------
async def part_c(runtime):
    print("\n== C. 工具写入 + 热重载（schedule_add / list / remove）")
    path = runtime.config_path_used
    with open(path, "rb") as handle:
        before = handle.read()

    def study_events():
        """调度器里"09:30 切 study"那几条（用它数, 不数总数）。"""
        return [e for e in runtime.scheduler.events
                if e.state.value == "study" and e.start == (9, 30)]

    check("C 起点: 临时配置里没有这条", study_events() == [])

    added = runtime.schedule_add({"state": "study", "start": "09:30",
                                  "days": ["mon", "wed"]})
    check("add: 写进文件且**立刻**热重载",
          added.get("ok") and added.get("hot") and len(study_events()) == 1,
          "hot=%s events=%d" % (added.get("hot"), len(study_events())))
    check("add: 写成 canonical 形",
          '    - state: study\n      days: [mon, wed]\n      start: "09:30"\n'
          in slurp(path))

    listing = runtime.schedule_list()
    check("list: 给得出刚加的那条 + 今天的日期",
          listing.get("ok")
          and any(entry.get("state") == "study" and entry.get("start") == "09:30"
                  and entry.get("days") == ["mon", "wed"]
                  for entry in listing.get("entries") or [])
          and listing.get("today") == datetime.now().date().isoformat(),
          "today=%s entries=%s" % (listing.get("today"), listing.get("entries")))

    again = runtime.schedule_add({"state": "study", "start": "09:30",
                                  "days": ["mon", "wed"]})
    check("add 幂等: 查重命中就不写第二遍",
          again.get("ok") and again.get("already") and len(study_events()) == 1,
          str(again.get("note")))

    removed = runtime.schedule_remove({"state": "study", "start": "09:30",
                                      "days": ["mon", "wed"]})
    check("remove: 删掉并热重载",
          removed.get("ok") and removed.get("hot") and study_events() == [],
          str(removed.get("kind")))
    with open(path, "rb") as handle:
        after = handle.read()
    check("删完**逐字节**回到原文", after == before,
          "%d -> %d 字节" % (len(before), len(after)))
    check("文本级写过 -> 留了 .bak", os.path.exists(path + ".bak"))


# ---------------------------------------------------------------------------
#  D. CLI（行格式 + 两跳文案）
# ---------------------------------------------------------------------------
async def part_d(runtime, sock):
    print("\n== D. CLI（`assistant schedule` / `assistant mode`）")
    rc, out, err = await run_cli(sock, "schedule")
    print("  $ assistant schedule -> rc=%s" % rc)
    for line in (out or err).strip().splitlines()[:8]:
        print("    %s" % line)
    check("schedule 退出码 0", rc == 0, err.strip()[:120])
    rows = [line.strip() for line in out.splitlines() if ROW_RE.match(line.strip())]
    check("行格式是 `HH:MM  状态`（大写）", bool(rows), str(rows[:3]))
    check("尾巴里那条标了「已触发」", "已触发" in out, out.strip()[:200])

    rc1, out1, err1 = await run_cli(sock, "mode", "game")
    check("mode game 成功", rc1 == 0, (out1 or err1).strip()[:120])
    rc2, out2, err2 = await run_cli(sock, "mode", "sleep")
    print("  $ assistant mode sleep -> rc=%s\n    %s" % (rc2, (out2 or err2).strip()))
    check("mode sleep: 退出码 0 且写明「路上经过 IDLE」",
          rc2 == 0 and "已切到 SLEEP" in out2 and "IDLE" in out2, (out2 or err2).strip()[:160])
    check("真的到了 SLEEP（CLI 与 Agent 两边一致）",
          runtime.state.current() is State.SLEEP, runtime.state.current().value)


# ---------------------------------------------------------------------------
#  A2. llama-server 的起停（manage_service=true）
# ---------------------------------------------------------------------------
async def part_llm_started(runtime):
    print("\n== A2. llama-server（manage_service=true）")
    if runtime.llm_service is None:
        skip("llama-server 起停", "没建起 LlamaService（看 llm.manage_service）")
        return False
    # ⚠ `LlamaService` 没有 status(): 能不能用只有一个真凭据 —— `/v1/models` 带 key 通不通
    ok = await wait_for_ready(runtime.llm_service, 60)
    check("启动时 llama-server 起得来（/v1/models 通）", ok,
          "port=%s" % runtime.llm_service.port)
    return ok


async def wait_for_ready(service, timeout_s):
    """等 llama-server 真能应答（`is_ready()` 是一次 HTTP, 别在事件循环里阻塞太久）。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        ready = await asyncio.get_running_loop().run_in_executor(None, service.is_ready)
        if ready:
            return True
        await asyncio.sleep(1.0)
    return False


async def part_llm_cycle(runtime, log_path):
    if runtime.llm_service is None:
        return
    stopped = not await wait_for_ready(runtime.llm_service, 25)
    check("进 SLEEP -> llama-server 停了（/v1/models 不通了）", stopped,
          "port=%s" % runtime.llm_service.port)
    result = runtime.state.transition_to(State.IDLE, "T12 验收")
    check("SLEEP -> IDLE 合法", bool(result.get("ok")), str(result.get("why")))
    started = await wait_for_ready(runtime.llm_service, 60)
    check("离开 SLEEP -> llama-server 起回来", started,
          "port=%s" % runtime.llm_service.port)
    text = slurp(log_path)
    check("日志里有「进入 SLEEP -> 停 llama-server」", "进入 SLEEP -> 停 llama-server" in text)
    check("日志里有「离开 SLEEP -> 起 llama-server」", "离开 SLEEP -> 起 llama-server" in text)
    runtime.state.transition_to(State.SLEEP, "T12 验收收尾")   # 收尾: 别把服务留在跑


# ---------------------------------------------------------------------------
#  E. 真文件 / 残留
# ---------------------------------------------------------------------------
def part_e(real_before, tmp_config, managed_llm):
    print("\n== E. 真配置与残留")
    for name in REAL_FILES:
        digest = md5(os.path.join(REPO, name))
        same = real_before[name] == digest
        print("  %-24s %s %s" % (name, digest, "没动 ✔" if same else "**变了** ✘"))
        check("真 %s 没动" % name, same)
    names = ["ffmpeg", "agent_gui"] + (["llama-server"] if managed_llm else [])
    leftovers = []
    for _ in range(16):
        leftovers = []
        for name in names:
            rc, out = run(["pgrep", "-a", name], timeout=10)
            if rc == 0 and out.strip():
                leftovers.append("%s: %s" % (name, out.strip().splitlines()[0][:80]))
        if not leftovers:
            break
        time.sleep(0.5)
    check("没留下 %s 进程" % " / ".join(names), not leftovers, "；".join(leftovers))


# ---------------------------------------------------------------------------
async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-real-clock", action="store_true",
                        help="跳过 B 段（真等一分钟那一段）")
    parser.add_argument("--no-llm-service", action="store_true",
                        help="不管 llama-server 的起停（快跑; 也就不验「进 SLEEP 停服务」）")
    args = parser.parse_args()

    global _TMP
    print("== 真数据 md5（跑之前）")
    real_before = {name: md5(os.path.join(REPO, name)) for name in REAL_FILES}
    for name, digest in real_before.items():
        print("  %-24s %s" % (name, digest))

    _TMP = tempfile.mkdtemp(prefix="t12-")
    sock = os.path.join(_TMP, "t12.sock")
    config_path = os.path.join(_TMP, "config.yaml")
    log_path = os.path.join(_TMP, "agent.log")

    # 日志: 给**根**记录器加一个文件 handler —— 这样 `agent.*` 里所有子模块
    # （scheduler / ipc / game_watch…）的日志都进同一个文件, 断言才看得到。
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(logging.INFO)
    file_handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(file_handler)
    logging.getLogger("agent").setLevel(logging.INFO)

    now = datetime.now()
    trigger_at = next_minute(now)
    if trigger_at <= now:
        trigger_at += timedelta(minutes=1)
    print("  临时目录: %s（第一条日程写在 %s）" % (_TMP, trigger_at.strftime("%H:%M")))

    config = build_config(sock, [{"state": "sleep", "date": trigger_at.date().isoformat(),
                                  "start": trigger_at.strftime("%H:%M")}],
                          manage_llm=not args.no_llm_service)
    write_config(config_path, config)

    runtime = Runtime(config=config, config_path_used=config_path,
                      start_native=False, start_terminal=False,
                      log=logging.getLogger("agent.t12"))
    managed = False
    try:
        await runtime._start_bus_and_io()
        # B 站那一路要起: 离开 GAME 时"停视频 + 清队列 + 卸 SigLIP"才有真东西可放
        # （`game_watch.enabled=false` 只是不起 PC 探针, 观察器与队列照建）
        await runtime._start_bilibili()
        await runtime._start_state_and_tools()
        await runtime._start_scheduler()
        await runtime._start_ipc()
        if not args.no_llm_service:
            await runtime._start_llm_service()
            managed = await part_llm_started(runtime)
        else:
            skip("llama-server 那一段", "--no-llm-service")

        await part_a(runtime, sock, trigger_at, log_path)
        if not args.no_real_clock:
            await part_b(runtime, sock, log_path, trigger_at)
        else:
            skip("B 段（真时钟）", "--no-real-clock")
        await part_c(runtime)
        await part_d(runtime, sock)
        if managed:
            await part_llm_cycle(runtime, log_path)
    finally:
        try:
            await runtime.stop()
        finally:
            part_e(real_before, config_path, managed)
            shutil.rmtree(_TMP, ignore_errors=True)

    print("\n== 结果: %s" % ("全部通过" if not _FAILED else
                            "失败 %d 项: %s" % (len(_FAILED), "、".join(_FAILED))))
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
