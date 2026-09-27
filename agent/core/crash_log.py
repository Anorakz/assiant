#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent/core/crash_log.py — 崩溃日志（T14-8）

板端没有调试器，出问题只能靠留在磁盘上的东西。这个模块管两件事：

1. **这次进程为什么没起来 / 为什么死了**：
   · `faulthandler.enable(file=…)` —— 段错误（native 层 MPP / moonlight / pybind11）
     会在崩溃瞬间把所有线程的 Python 栈写进本次会话文件（`os._exit` / SIGSEGV 都能留下）；
   · `sys.excepthook` / `threading.excepthook` —— 未捕获异常（含子线程里的）
     连同**最近若干条日志**一起写进去；
   · 每个进程一开始就把"这一次会话"的文件建好（带 pid / argv / 配置路径 / git 版本），
     **正常退出时把它删掉** —— 所以 `logs/crash/` 里剩下的每一份都是"上一次没干净退出"。

2. **上次到底崩在哪**：本次启动时把上一份报告的头尾（各若干行）打印到日志里，
   这样 systemd 的 `logs/agent.out` 里直接能看到上次崩溃的现场，不用去翻目录。

文件与保留
    logs/crash/agent-YYYYmmdd-HHMMSS-<pid>.log      一次会话/一份崩溃报告
    logs/crash/.last-reported                        启动横幅已经报过哪一份（避免刷屏）
    默认只留最新 20 份（`AGENT_CRASH_KEEP` 可改；超出的按修改时间删）

环境变量
    AGENT_CRASH_DIR      改崩溃目录（验收/测试用）
    AGENT_CRASH_KEEP     保留份数
    AGENT_CRASH_DISABLE  非空 = 完全不装（跑纯逻辑测试时用）

不做什么（刻意的）
    · 不做 core dump 分析、不装信号处理器去"拦住"崩溃进程 —— 该崩就崩，
      由 systemd（`Restart=on-failure`）拉回来，这里只负责**留下现场**；
    · 不把 native 层的 C++ 栈弄进来（那要 core dump / gdb；这里能拿到 Python 栈
      与线程名，已经够定位"是哪个循环/哪次调用"）。
"""

from __future__ import annotations

import faulthandler
import logging
import os
import platform
import signal
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Deque, Dict, List, Optional

__all__ = [
    "CrashLogger",
    "DEFAULT_KEEP",
    "LogTailBuffer",
    "default_crash_dir",
    "git_revision",
    "install_crash_logging",
    "read_report_head_tail",
]

#: 默认保留多少份报告（多了没用，日志目录也不该无限长）
DEFAULT_KEEP = 20

#: 崩溃报告里"最近日志"留多少行
LOG_TAIL_LINES = 60

_ENV_DIR = "AGENT_CRASH_DIR"
_ENV_KEEP = "AGENT_CRASH_KEEP"
_ENV_DISABLE = "AGENT_CRASH_DISABLE"

#: 启动横幅里上一份报告取头/尾各多少行
_BANNER_HEAD = 10
_BANNER_TAIL = 25

#: 每行截断长度（防止一条巨型报文把日志撑爆）
_LINE_LIMIT = 300


def default_crash_dir() -> Path:
    """崩溃目录：`$AGENT_CRASH_DIR`，否则 <仓库根>/logs/crash。"""
    override = os.environ.get(_ENV_DIR)
    if override:
        return Path(override)
    # agent/core/crash_log.py → 仓库根是 parents[2]
    return Path(__file__).resolve().parents[2] / "logs" / "crash"


def git_revision(repo_root: Optional[Path] = None) -> str:
    """仓库当前提交（读 .git/HEAD，**不跑 git 进程** —— 启动路径上不该 fork）。"""
    root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[2]
    head = root / ".git" / "HEAD"
    try:
        text = head.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return ""
    if text.startswith("ref:"):
        ref = root / ".git" / text.split(":", 1)[1].strip()
        try:
            return ref.read_text(encoding="utf-8").strip()[:12]
        except (OSError, ValueError):
            return ""
    return text[:12]                       # 分离头指针：HEAD 里就是 sha


def _clip(text: str, limit: int = _LINE_LIMIT) -> str:
    text = text.rstrip("\n")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


class LogTailBuffer(logging.Handler):
    """挂在 agent logger 上的环形缓冲：留住最近 N 条日志，崩溃时一起写进报告。

    为什么要它：崩溃报告只有 traceback 时经常看不出"崩之前发生了什么"，
    而 `logs/agent.log` 可能很大；把最近的几十行**同一份文件里**给出来最省事。
    """

    def __init__(self, capacity: int = 200) -> None:
        super().__init__(level=logging.DEBUG)
        self.capacity = capacity
        self._lines: Deque[str] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._lines.append(_clip(self.format(record)))
        except Exception:                     # 日志缓冲绝不能再抛
            pass

    def lines(self) -> List[str]:
        return list(self._lines)

    def text(self, limit: Optional[int] = None) -> str:
        lines = self.lines()
        if limit is not None:
            lines = lines[-limit:]
        return "\n".join(lines)


class CrashLogger:
    """一次进程会话的崩溃日志。`install()` 之后才真正生效。"""

    def __init__(
        self,
        app: str = "agent",
        crash_dir: Optional[Path] = None,
        keep: Optional[int] = None,
        context: Optional[Dict[str, str]] = None,
        log: Optional[logging.Logger] = None,
    ) -> None:
        self.app = app
        self.dir = Path(crash_dir) if crash_dir is not None else default_crash_dir()
        self.keep = keep if keep is not None else self._env_keep()
        self.context: Dict[str, str] = dict(context or {})
        self.log = log
        self._installed = False
        self._clean_closed = False
        self._reported = False               # 写过报告吗（决定 close_cleanly 删不删文件）
        self._fh_file = None                  # faulthandler 用的文件对象（保持打开）
        self._tail: Optional[LogTailBuffer] = None
        self._prev_hook = None
        self._prev_thread_hook = None
        self._prev_unraisable = None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.path = self.dir / ("%s-%s-%d.log" % (self.app, stamp, os.getpid()))

    # -- 环境 ---------------------------------------------------------------
    @staticmethod
    def _env_keep() -> int:
        raw = os.environ.get(_ENV_KEEP)
        if not raw:
            return DEFAULT_KEEP
        try:
            value = int(raw)
        except ValueError:
            return DEFAULT_KEEP
        return value if value > 0 else DEFAULT_KEEP

    # -- 安装 ---------------------------------------------------------------
    def install(self, enable_faulthandler: bool = True) -> "CrashLogger":
        """装好钩子并把"本次会话"的文件建出来。

        @param enable_faulthandler 关掉它 = 只留未捕获异常（给单测用，免得抢全局信号）
        @return self（方便链式调用）
        """
        if self._installed:
            return self
        if os.environ.get(_ENV_DISABLE):
            self._installed = True
            return self

        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except (OSError, ValueError) as exc:
            # 建不出目录就什么都别做（不能再把启动流程打断）
            if self.log is not None:
                self.log.warning("崩溃目录建不出来 (%s): %s", self.dir, exc)
            self._installed = True
            return self

        self._write_header()
        self._prune()

        if enable_faulthandler:
            try:
                self._fh_file = open(self.path, "a", encoding="utf-8")
                faulthandler.enable(file=self._fh_file, all_threads=True)
            except (OSError, ValueError, RuntimeError) as exc:
                self._fh_file = None
                if self.log is not None:
                    self.log.warning("faulthandler 装不上: %s", exc)

        self._prev_hook = sys.excepthook
        sys.excepthook = self._excepthook
        if hasattr(threading, "excepthook"):
            self._prev_thread_hook = threading.excepthook
            threading.excepthook = self._thread_excepthook
        if hasattr(sys, "unraisablehook"):
            self._prev_unraisable = sys.unraisablehook
            sys.unraisablehook = self._unraisablehook

        self._installed = True
        return self

    def attach_log_tail(self, logger: logging.Logger, capacity: int = 200) -> LogTailBuffer:
        """给某个 logger 挂上"最近 N 条"缓冲（崩溃报告里会带上它）。"""
        handler = LogTailBuffer(capacity=capacity)
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addHandler(handler)
        self._tail = handler
        return handler

    # -- 写报告 -------------------------------------------------------------
    def _header_lines(self) -> List[str]:
        try:
            uname = " ".join(platform.uname())
        except Exception:                      # pragma: no cover - 平台怪癖
            uname = platform.platform()
        lines = [
            "=" * 78,
            "板端助手 %s —— 会话/崩溃日志（T14-8）" % self.app,
            "=" * 78,
            "开始时间 : %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
            "进程     : pid=%d ppid=%d" % (os.getpid(), os.getppid()),
            "命令行   : %s" % " ".join(sys.argv),
            "工作目录 : %s" % os.getcwd(),
            "解释器   : %s" % sys.version.replace("\n", " "),
            "系统     : %s" % uname,
        ]
        for key, value in sorted(self.context.items()):
            lines.append("%-9s: %s" % (key, value))
        lines += [
            "-" * 78,
            "⚠ 正常退出时这个文件会被删掉；它留在 logs/crash/ 里 = 上一次没干净退出。",
            "  真要抓现场：崩溃后看本文件的 traceback / faulthandler 段。",
            "  手动要一份全线程栈：kill -USR1 <pid>（本模块装了 SIGUSR1 钩子时会打印）。",
            "-" * 78,
            "",
        ]
        return lines

    def _write_header(self) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as handle:
                handle.write("\n".join(self._header_lines()))
                handle.write("\n")
                handle.flush()
        except (OSError, ValueError):
            pass

    def recent_log_text(self, limit: int = LOG_TAIL_LINES) -> str:
        if self._tail is None:
            return ""
        return self._tail.text(limit=limit)

    def write_report(
        self,
        reason: str,
        exc: Optional[BaseException] = None,
        extra: Optional[str] = None,
    ) -> Optional[Path]:
        """把"为什么死"追加进本次会话文件。返回文件路径（写不进去就 None）。"""
        if os.environ.get(_ENV_DISABLE):
            return None
        # ⚠ 顺序刻意是「先最近日志、后崩溃原因」：
        #   ① 时间顺序本来就该这样（日志发生在崩溃之前）；
        #   ② 启动横幅只取报告**头尾**（`previous_report_banner`），原因放最后才一定看得见 ——
        #      放中间的话，日志一多就会被省略掉，横幅里只剩一坨日志。
        chunks: List[str] = [""]
        tail = self.recent_log_text()
        if tail:
            chunks += ["--- 最近 %d 条日志 ---" % LOG_TAIL_LINES, tail, ""]
        chunks += [
            "!" * 78,
            "崩溃/异常 : %s" % reason,
            "时间       : %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
            "!" * 78,
            "",
        ]
        if exc is not None:
            import traceback

            chunks.append("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        if extra:
            chunks.append(extra if extra.endswith("\n") else extra + "\n")
        try:
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write("\n".join(chunks))
                handle.flush()
                os.fsync(handle.fileno())
        except (OSError, ValueError):
            return None
        self._reported = True
        return self.path

    # -- 钩子 ---------------------------------------------------------------
    def _excepthook(self, exc_type, exc, tb) -> None:
        self.write_report("未捕获异常（主线程）", exc)
        if self.log is not None:
            self.log.error("未捕获异常，现场已写入 %s", self.path, exc_info=(exc_type, exc, tb))
        else:                                  # pragma: no cover - 没 logger 时至少别静默
            sys.stderr.write("未捕获异常，现场已写入 %s\n" % self.path)
        if self._prev_hook is not None and self._prev_hook is not sys.__excepthook__:
            self._prev_hook(exc_type, exc, tb)

    def _thread_excepthook(self, args) -> None:
        exc = args.exc_value
        name = getattr(args.thread, "name", "?")
        self.write_report("未捕获异常（线程 %s）" % name, exc)
        if self.log is not None:
            self.log.error("线程 %s 未捕获异常，现场已写入 %s", name, self.path,
                           exc_info=(args.exc_type, exc, args.exc_traceback))
        if self._prev_thread_hook is not None:
            self._prev_thread_hook(args)

    def _unraisablehook(self, args) -> None:
        exc = args.exc_value
        where = getattr(args, "object", None)
        self.write_report("回调里未捕获的异常（unraisable，object=%r）" % (where,), exc)
        if self._prev_unraisable is not None:
            self._prev_unraisable(args)

    # -- 收尾 / 保留 --------------------------------------------------------
    def close_cleanly(self) -> None:
        """收尾：**没有记录过崩溃**才删掉本次会话文件。

        为什么要区分：正常退出与"未捕获异常"都会走到这里（`atexit` 两条路都会跑），
        如果无脑删文件，那条 uncaught exception 报告就会被自己删掉 —— 最该留的东西没了。

        @note 幂等；崩在信号里（SIGSEGV）时根本不会有人调它，文件自然留着。
        """
        if self._clean_closed:
            return
        self._clean_closed = True
        if os.environ.get(_ENV_DISABLE):
            return
        try:
            faulthandler.disable()
        except Exception:
            pass
        if self._fh_file is not None:
            try:
                self._fh_file.close()
            except Exception:
                pass
            self._fh_file = None
        if self._reported:
            return                            # 报告已经写了，留着
        try:
            self.path.unlink()
        except OSError:
            pass

    def _prune(self) -> List[Path]:
        """只留最新 keep 份报告（跳过 `.last-reported` 这类点文件）。"""
        removed: List[Path] = []
        try:
            reports = sorted(
                (p for p in self.dir.glob("*.log") if p.is_file() and not p.name.startswith(".")),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return removed
        for stale in reports[self.keep:]:
            try:
                stale.unlink()
                removed.append(stale)
            except OSError:
                pass
        return removed

    # -- 上次的报告 ---------------------------------------------------------
    def previous_report(self) -> Optional[Path]:
        """上一次留下的报告（不含本次会话文件），按修改时间取最新。"""
        try:
            candidates = [
                p for p in self.dir.glob("*.log")
                if p.is_file() and p.resolve() != self.path.resolve()
            ]
        except OSError:
            return None
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.stat().st_mtime)

    def previous_report_banner(self, head: int = _BANNER_HEAD, tail: int = _BANNER_TAIL) -> str:
        """给启动日志用的横幅；已经报过同一份就返回空串。

        用 `.last-reported` 记住报过谁 —— 否则每次重启都会把同一份老崩溃再刷一遍，
        反而把"这次是不是又崩了"淹掉。
        """
        path = self.previous_report()
        if path is None:
            return ""
        marker = self.dir / ".last-reported"
        try:
            if marker.read_text(encoding="utf-8").strip() == path.name:
                return ""
        except (OSError, ValueError):
            pass
        body = read_report_head_tail(path, head=head, tail=tail)
        if not body:
            return ""
        try:
            marker.write_text(path.name + "\n", encoding="utf-8")
        except (OSError, ValueError):
            pass
        return (
            "上次没有干净退出：%s（%s）\n%s"
            % (path.name, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(path.stat().st_mtime)),
               body)
        )


def read_report_head_tail(path: Path, head: int = _BANNER_HEAD, tail: int = _BANNER_TAIL) -> str:
    """读一份报告的头 head 行 + 尾 tail 行（中间省略），每行都截断。"""
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, ValueError):
        return ""
    if not lines:
        return ""
    lines = [_clip(line) for line in lines]
    if len(lines) <= head + tail:
        return "\n".join(lines)
    omitted = len(lines) - head - tail
    return "\n".join(
        lines[:head] + ["… （中间省略 %d 行）…" % omitted] + lines[-tail:]
    )


def install_crash_logging(
    app: str = "agent",
    log: Optional[logging.Logger] = None,
    crash_dir: Optional[Path] = None,
    context: Optional[Dict[str, str]] = None,
    keep: Optional[int] = None,
    sigusr1_dump: bool = True,
) -> CrashLogger:
    """一行装好：建会话文件 → 装钩子 → 给 logger 挂最近日志缓冲。

    @param sigusr1_dump 额外把 SIGUSR1 注册成"手动 dump 全线程栈"
                        （`kill -USR1 <pid>`，板端排查卡死时很顺手）
    """
    logger = CrashLogger(app=app, crash_dir=crash_dir, keep=keep, context=context, log=log)
    logger.install()
    if log is not None:
        logger.attach_log_tail(log)
    if sigusr1_dump:
        sig = getattr(signal, "SIGUSR1", None)          # Windows 上没有这个信号
        if sig is not None:
            try:
                faulthandler.register(sig, all_threads=True, chain=False)
            except (OSError, ValueError, RuntimeError):
                pass
    return logger
