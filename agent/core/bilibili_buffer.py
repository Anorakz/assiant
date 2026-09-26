# ============================================================================
#  agent/core/bilibili_buffer.py — B 站**缓冲代理**（Phase 7 T11-3）
#
#  它是什么: 一个"**边下边喂**"的管子。ffmpeg 把 B 站的流 `-c copy` 合成 **MPEG-TS**
#            写到 stdout; 我们把它收进一个**内存窗口**, 再按播放器的消费速度喂进
#            **FIFO（命名管道）** —— GUI 那边就是"播一个本地路径"。
#
#  为什么不是 HTTP 代理（T11-0 实测改的）:
#      · 板端 **`souphttpsrc` 是坏的**（连 `souphttpsrc ! fakesink` 都 SIGABRT;
#        根因是 libgstreamer 1.18 与 gstreamer1.0-plugins-good 1.16 版本错配,
#        不是内存问题 —— 详见 todo.md 的 T11-0 记录）。播放器**拉不了 http**。
#      · **FIFO + filesrc** 实测可用（`filesrc ! decodebin` 选中 `mppvideodec`）。
#      · 封装必须用 **mpegts**: mp4 写管道会 `Could not write header … Broken pipe`
#        （管道不可 seek）; mpegts 天生流式、没有索引。
#
#  缓冲规矩（老板 T11-0/D3 定的）:
#      · **15 s 门槛**: 窗口里攒够 15 s 才开闸（开闸 = 把 FIFO 给 GUI 去播）。
#      · 播放中窗口封顶 = 15 s（领先一档就够, 别白占内存）;
#        **暂停时封顶放到 60 s**（"用户停止时可以延长"）;
#      · 两条硬上限: `max_s` 与 **内存水位**（`MemAvailable < watermark` 就不读了,
#        ffmpeg 写满管道自然阻塞 —— 天然背压）。
#      · **播过即释放**: 写进 FIFO 的字节从窗口里删掉; `stop()` 把窗口整个放掉。
#      · **全程不落盘**: 视频内容只在内存窗口里（最多 60 s ≈ 15 MB @720P）。
#      · 直链过期（T11-0 实测 CDN 会 403）-> **重取直链 + 重启 ffmpeg**, 对 GUI 透明;
#        重试次数用完了如实说。
#
#  ⚠ 不支持 seek（FIFO 不可寻址; GUI 本来也没有进度条拖动）。
#  ⚠ 本模块**不碰 IPC**: 它只交出 `path()` 与 `snapshot()`, 谁推给 GUI 是 Runtime 的事。
# ============================================================================

from __future__ import annotations

import errno
import logging
import os
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional

__all__ = ["BilibiliBuffer", "BufferError", "seconds_to_bytes", "ffmpeg_argv",
           "DEFAULT_INITIAL_S", "DEFAULT_MAX_S", "DEFAULT_MEM_WATERMARK_MB"]

_log = logging.getLogger(__name__)

DEFAULT_INITIAL_S = 15.0        #: 起播门槛（你定的 15 s）
DEFAULT_MAX_S = 60.0            #: 暂停时能延到多长（你定的 60 s）
DEFAULT_MEM_WATERMARK_MB = 400.0  #: 内存水位（低于它就不再预取）
CHUNK = 65536
LOOP_SLEEP = 0.02


class BufferError(RuntimeError):
    """缓冲代理起不来/中途坏了（消息给人看、能照做）。"""


def seconds_to_bytes(bps: Any, seconds: float) -> int:
    """按码率把"秒"折成"字节"（`bps` 是 bit/s, 来自 API 的 bandwidth/size÷length）。"""
    try:
        rate = float(bps or 0)
    except (TypeError, ValueError):
        rate = 0.0
    if rate <= 0:
        rate = 2_000_000.0          # 拿不到码率就按 ~2 Mbps 估（720P 量级, 宁可多攒一点）
    return int(rate / 8.0 * max(0.0, float(seconds)))


def ffmpeg_argv(api: Any, stream: Dict[str, Any], bvid: str, *, binary: str = "ffmpeg",
                seek_s: float = 0.0) -> List[str]:
    """拼 ffmpeg 命令行: **`-c copy` 合流 -> mpegts -> stdout**。

    @param api `BilibiliApi`（用它拼"每个输入都带 UA + Referer"的参数 —— 实测不带就 403）
    @note DASH 是**两条输入**（音视频分开）, 单文件是一条; 两条都走 `-c copy`（不转码）。
    @note `-ss` 放在 `-i` 之前（按关键帧跳）, 重连时从"已经喂给播放器的大致位置"接上。
    """
    args = [binary, "-hide_banner", "-loglevel", "warning", "-fflags", "+genpts"]
    if seek_s and seek_s > 0:
        args += ["-ss", "%.3f" % float(seek_s)]
    if str(stream.get("kind")) == "dash":
        args += list(api.ffmpeg_input_args(bvid, stream["video"]["url"]))
        args += list(api.ffmpeg_input_args(bvid, stream["audio"]["url"]))
    else:
        args += list(api.ffmpeg_input_args(bvid, stream["url"]))
    args += ["-c", "copy", "-f", "mpegts", "pipe:1"]
    return args


class BilibiliBuffer(object):
    """一条视频的"下载 -> 缓冲 -> 喂给播放器"。

    典型用法（Runtime 里）::

        buf = BilibiliBuffer(api, on_ready=push_path_to_gui)
        buf.start(item)                 # 取流 + 起 ffmpeg + 开始攒
        buf.wait_ready(8.0)             # 攒够 15 s 才算就绪（就绪回调里把 path 推给 GUI）
        buf.set_playing(True)           # GUI 回报的播放/暂停（暂停时窗口放到 60 s）
        buf.stop()                      # 换片/清队列/退出
    """

    def __init__(self, api: Any, *, fifo_dir: Optional[str] = None,
                 initial_s: float = DEFAULT_INITIAL_S, max_s: float = DEFAULT_MAX_S,
                 mem_watermark_mb: float = DEFAULT_MEM_WATERMARK_MB,
                 binary: str = "ffmpeg", spawn: Optional[Callable[[List[str]], Any]] = None,
                 open_writer: Optional[Callable[[str], Any]] = None,
                 create_fifo: Optional[Callable[[str], None]] = None,
                 meminfo: Optional[Callable[[], Dict[str, float]]] = None,
                 log: Optional[logging.Logger] = None, retries: int = 2) -> None:
        """
        @param spawn       起 ffmpeg 的方式（默认 `subprocess.Popen`; 单测注入假的）
        @param open_writer 打开 FIFO 写端（默认 `open(path, "wb")` + 等读端; 单测注入假的）
        @param create_fifo 建 FIFO（默认 `os.mkfifo`; 单测注入 no-op, 免得依赖 POSIX）
        @param meminfo     读 /proc/meminfo（单测注入, 免得真去看内存）
        """
        self.api = api
        self.log = log or _log
        self.fifo_dir = str(fifo_dir or "/tmp")
        self.initial_s = max(1.0, float(initial_s or DEFAULT_INITIAL_S))
        self.max_s = max(self.initial_s, float(max_s or DEFAULT_MAX_S))
        self.mem_watermark_mb = float(mem_watermark_mb or DEFAULT_MEM_WATERMARK_MB)
        self.binary = str(binary or "ffmpeg")
        self.retries = max(0, int(retries))
        self._spawn = spawn or self._default_spawn
        self._open_writer = open_writer or self._default_open_writer
        self._create_fifo = create_fifo or self._default_create_fifo
        self._meminfo = meminfo or self._default_meminfo

        self.item: Dict[str, Any] = {}
        self.bvid = ""
        self.stream: Dict[str, Any] = {}
        self.bps = 0.0
        self.path = ""
        self.state = "idle"          #: idle|buffering|serving|paused|ended|error|stopped
        self.why = ""
        self.ready = False
        self.restarts = 0
        self.on_ready: Optional[Callable[[str], None]] = None

        self._window = bytearray()
        self._written = 0            #: 已经喂给播放器的字节
        self._inflight = 0           #: 从窗口摘下来、还没写进管道的字节
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._gate = threading.Event()
        self._playing = True
        self._proc: Any = None
        self._writer: Any = None
        self._reader: Optional[threading.Thread] = None
        self._writer_thread: Optional[threading.Thread] = None
        self._reader_done = threading.Event()
        self._notes: List[str] = []

    # ------------------------------------------------------------ 只读 ---
    def buffered_s(self) -> float:
        """窗口里还有多少**秒**的料（按码率折算）。"""
        return len(self._window) / max(1.0, self.bps / 8.0)

    def written_s(self) -> float:
        """已经喂给播放器的**秒**数（重连时用它当 `-ss`）。"""
        return self._written / max(1.0, self.bps / 8.0)

    def inflight_s(self) -> float:
        """**在途**的秒数（从窗口摘下来、还没写进管道的那一块）。

        @note 有它账才对得上: `buffered_s + written_s + inflight_s` = **从 ffmpeg 读到的总量**
              （写线程摘块与写块之间有一瞬间两边都不算, 板端跑测试时被这一块绊到过）。
        """
        return self._inflight / max(1.0, self.bps / 8.0)

    def cap_s(self) -> float:
        """当前窗口封顶: **播放中 15 s / 暂停 60 s**（你定的"暂停可以延长"）。"""
        return self.max_s if not self._playing else self.initial_s

    def snapshot(self) -> Dict[str, Any]:
        progress = self.progress()
        with self._lock:
            return {"state": self.state, "ready": bool(self.ready), "path": self.path,
                    "bvid": self.bvid, "title": str(self.item.get("title") or ""),
                    "buffered_s": progress["buffered_s"],
                    "written_s": progress["written_s"],
                    "inflight_s": progress["inflight_s"],
                    "cap_s": self.cap_s(), "playing": bool(self._playing),
                    "bytes": progress["bytes"], "ffmpeg_alive": bool(
                        self._proc is not None and getattr(self._proc, "poll", lambda: 0)() is None),
                    "restarts": self.restarts, "why": self.why}

    def progress(self) -> Dict[str, Any]:
        """**原子地**读三个数（窗口 / 已写 / 在途）。

        @note ⚠ 必须一次读完: 分别读会被读写线程"搬家"（一块从在途挪进已写）**少算一块** ——
              板端测试就是这么被绊到的（差 0.65 s 的 64 KB）。
        @note 三者之和 = **从 ffmpeg 读到的总量**（等于"已经拿到手的进度"）。
        """
        with self._lock:
            window, written, inflight = len(self._window), self._written, self._inflight
        rate = max(1.0, self.bps / 8.0)
        return {"bytes": window, "window": window, "written": written, "inflight": inflight,
                "buffered_s": round(window / rate, 3), "written_s": round(written / rate, 3),
                "inflight_s": round(inflight / rate, 3),
                "progress_s": round((window + written + inflight) / rate, 3)}

    def notes(self) -> List[str]:
        """攒下来的"要如实告诉用户的话"（例如上游断过、重连过）。取走即清空。"""
        out = list(self._notes)
        self._notes = []
        return out

    # ------------------------------------------------------------ 起停 ---
    def start(self, item: Dict[str, Any], *, playing: bool = True,
              stream: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """取流 + 起 ffmpeg + 开攒。**不阻塞**（门槛由 `wait_ready` 等）。

        @param item   队列里那一条（要 `bvid` 与 `title`）
        @param stream 已经取好的直链（省一次 API）; 不给就自己问 `api.playurl`
        @raise BufferError 取流失败/起不来（消息给人看）
        """
        self.stop()
        self.bvid = str((item or {}).get("bvid") or "").strip()
        if not self.bvid:
            raise BufferError("没有 bvid，不知道放哪个视频")
        self.item = dict(item or {})
        self._playing = bool(playing)
        if stream is None:
            stream = self._fetch_stream(self.bvid)
        self.stream = dict(stream or {})
        self.bps = float(self.stream.get("bps") or 0.0)
        self.path = os.path.join(self.fifo_dir, "bilibili-%s.ts" % self.bvid)
        self._prepare_fifo()
        self._stop.clear()
        self._gate = threading.Event()
        self._reader_done = threading.Event()
        self.ready = False
        self.state = "buffering"
        self.why = ""
        self._window = bytearray()
        self._written = 0
        self._inflight = 0
        self._spawn_ffmpeg(seek_s=0.0)
        self._reader = threading.Thread(target=self._read_loop, name="bilibili-read",
                                        daemon=True)
        self._writer_thread = threading.Thread(target=self._write_loop, name="bilibili-write",
                                               daemon=True)
        self._reader.start()
        self._writer_thread.start()
        self.log.info("bilibili: 缓冲开攒 %s（%s, %.0f kbps, 门槛 %.0f s）",
                      self.bvid, self.stream.get("kind"), self.bps / 1000.0, self.initial_s)
        return self.snapshot()

    def wait_ready(self, timeout_s: float = 10.0) -> bool:
        """等"攒够 15 s"（到了就把 FIFO 开闸并回调 `on_ready`）。@return 是否就绪。"""
        deadline = time.time() + max(0.1, float(timeout_s))
        while time.time() < deadline:
            if self._stop.is_set():
                return False
            if self._gate.is_set():
                return True
            if self._proc is not None and getattr(self._proc, "poll", lambda: 0)() is not None:
                self._handle_eof()
                if self._stop.is_set():
                    return False
            time.sleep(LOOP_SLEEP)
        self.why = self.why or ("网络慢：%.0f 秒里只攒到 %.1f s（门槛 %.0f s）"
                                % (timeout_s, self.buffered_s(), self.initial_s))
        return False

    def open_gate(self) -> None:
        """**按现有缓冲强行开闸**（门槛等不到时用; 由调用方决定并如实告诉用户）。"""
        if not self._gate.is_set():
            self.why = self.why or "没攒够 %.0f s 就先起播了（按现有缓冲）" % self.initial_s
            self._open_gate()

    def set_playing(self, playing: bool) -> None:
        """GUI 回报的播放状态 —— **暂停时窗口封顶从 15 s 放到 60 s**。"""
        self._playing = bool(playing)
        if self._playing and self.state == "paused":
            self.state = "serving"
        elif not self._playing and self.state == "serving":
            self.state = "paused"

    def stop(self) -> None:
        """停 ffmpeg + 关 FIFO + 放掉窗口 + 删管道文件（**幂等**）。"""
        self._stop.set()
        self._gate.clear()
        proc, writer = self._proc, self._writer
        threads = [t for t in (self._reader, self._writer_thread) if t is not None]
        self._proc, self._writer = None, None
        self._reader, self._writer_thread = None, None
        for closer, name in ((proc, "ffmpeg"), (writer, "FIFO")):
            if closer is None:
                continue
            try:
                if name == "ffmpeg":
                    closer.terminate()
                else:
                    closer.close()                 # 关掉写端 -> 卡在 write 的线程也会退出来
            except Exception as exc:                       # noqa: BLE001
                self.log.debug("bilibili: 关 %s 时出错（忽略）: %r", name, exc)
        self._reader_done.set()                            # 让写线程别再等料
        for thread in threads:
            if thread.is_alive():
                thread.join(timeout=2)
        if proc is not None:
            try:
                proc.wait(timeout=2)
            except Exception:                              # noqa: BLE001
                pass
        with self._lock:
            self._window = bytearray()
        if self.path:
            try:
                os.unlink(self.path)
            except OSError:
                pass
        self.path = ""
        self.ready = False
        if self.state not in ("ended", "error"):
            self.state = "stopped"

    # ------------------------------------------------------------ 内部 ---
    def _fetch_stream(self, bvid: str) -> Dict[str, Any]:
        try:
            return dict(self.api.playurl(bvid) or {})
        except Exception as exc:                           # noqa: BLE001
            raise BufferError("要不到播放地址：%s" % exc) from exc

    def _prepare_fifo(self) -> None:
        try:
            os.unlink(self.path)                           # 清掉上次留下的
        except OSError:
            pass
        try:
            self._create_fifo(self.path)
        except BufferError:
            raise
        except OSError as exc:
            raise BufferError("建不了管道 %s：%s" % (self.path, exc)) from exc

    def _spawn_ffmpeg(self, *, seek_s: float) -> None:
        argv = ffmpeg_argv(self.api, self.stream, self.bvid, binary=self.binary, seek_s=seek_s)
        try:
            self._proc = self._spawn(argv)
        except FileNotFoundError as exc:
            raise BufferError("板端没有 ffmpeg（apt install ffmpeg）: %s" % exc) from exc
        self.log.debug("bilibili: ffmpeg 起了（seek=%.1fs, %d 个参数）", seek_s, len(argv))

    def _open_gate(self) -> None:
        self.ready = True
        self.state = "serving" if self._playing else "paused"
        self._gate.set()
        callback, path = self.on_ready, self.path
        if callable(callback) and path:
            try:
                callback(path)
            except Exception as exc:                       # noqa: BLE001 - 回调坏了不该影响缓冲
                self.log.warning("bilibili: on_ready 回调出错（忽略）: %r", exc)

    def _mem_low(self) -> bool:
        """内存水位到了吗（到了就先别预取, 让 ffmpeg 堵在管道里）。"""
        if self.mem_watermark_mb <= 0:
            return False
        try:
            info = self._meminfo() or {}
        except Exception as exc:                           # noqa: BLE001
            self.log.debug("bilibili: 读内存失败（当不紧张）: %r", exc)
            return False
        available = float(info.get("MemAvailable") or info.get("MemAvailableMB") or 0.0)
        if not available:
            return False
        return available < self.mem_watermark_mb

    def _read_loop(self) -> None:
        """**读线程**（ffmpeg -> 内存窗口）: 只看"窗口够不够、内存紧不紧", 不被播放器拖住。

        @note 这就是"**暂停时还能接着预取**"的关键: 写线程卡在管道上（播放器不读了）,
              读线程照样把窗口攒到 60 s 上限（你定的"停止时可以延长"）。
        """
        while not self._stop.is_set():
            try:
                if not self._gate.is_set() and self.buffered_s() >= self.initial_s:
                    self._open_gate()
                    continue
                if self._proc is None:
                    break
                if self.buffered_s() >= self.cap_s():
                    time.sleep(LOOP_SLEEP * 5)         # 窗口满了（播放中 15 s / 暂停 60 s）
                    continue
                if self._mem_low():
                    time.sleep(LOOP_SLEEP * 10)        # 内存水位到了: 不读了, 让 ffmpeg 堵着
                    continue
                chunk = self._proc.stdout.read(CHUNK)
                if not chunk:
                    if self._handle_eof() == "ended":
                        self._reader_done.set()        # 放完了 -> 读线程收工（写线程把尾巴喂完）
                        return
                    continue
                with self._lock:
                    self._window.extend(chunk)
            except Exception as exc:                   # noqa: BLE001 - 读线程不能死
                self.log.warning("bilibili: 读流出错（忽略这轮）: %r", exc)
                time.sleep(LOOP_SLEEP * 5)

    def _write_loop(self) -> None:
        """**写线程**（内存窗口 -> FIFO）: `write` 阻塞 = 播放器消费速度 = 天然背压。

        @note 写出去的字节**立刻从窗口里删掉**（"播过即释放"）。
        @note 读端放完了（`_reader_done`）**也不立刻收工**: 先把窗口里剩下的喂完, 再关管道
              —— 关写端对播放器就是 EOS（不然会把正在放尾巴的播放器掐掉）。
        """
        while not self._stop.is_set():
            try:
                if not self._gate.is_set() or not self._window:
                    if self._reader_done.is_set() and not self._window:
                        break                          # 料喂完了 -> 关管道给播放器 EOS
                    time.sleep(LOOP_SLEEP)
                    continue
                with self._lock:
                    chunk = bytes(self._window[:CHUNK])
                    del self._window[:CHUNK]
                    self._inflight += len(chunk)
                if self._writer is None:
                    self._writer = self._open_writer(self.path)   # 开闸后才真开管道写端
                self._writer.write(chunk)
                with self._lock:
                    self._written += len(chunk)
                    self._inflight -= len(chunk)
            except Exception as exc:                   # noqa: BLE001 - 播放器关了管道就退出
                if self._stop.is_set():
                    break
                self.log.warning("bilibili: 写管道出错（停掉缓冲）: %r", exc)
                self._stop.set()
                break
        if self._reader_done.is_set() and not self._stop.is_set():
            self._close_writer()

    def _handle_eof(self) -> str:
        """ffmpeg 没输出了: 是**正常放完**还是**上游断了**（断了要重取直链重连）。

        @return "ended"（放完, 读线程收工）/ "restarted" / "error"
        @note ⚠ 实测踩过: `poll()` 经常**还没回收到退出码**（拿到 None）—— 第一版把它当"上游断",
              于是视频一放完就无脑重连, 重连两次后报"上游断了"。现在**先等一下退出码**,
              `0/None` 一律算正常结束（真断线是**非 0** 退出码, 例如 403 时的 1）。
        """
        proc = self._proc
        code = None
        if proc is not None:
            code = getattr(proc, "poll", lambda: None)()
            if code is None:
                try:
                    code = proc.wait(timeout=2)
                except Exception:                      # noqa: BLE001
                    code = None
        if code in (0, None):
            if not self._gate.is_set() and self._window:
                self._open_gate()                      # 短片: 还没到门槛就结束了 -> 有多少喂多少
            self.state = "ended"
            self.why = self.why or "放完了"
            self.log.info("bilibili: 放完了（喂了 %.1f s, 窗口还剩 %.1f s）",
                          self.written_s(), self.buffered_s())
            return "ended"
        if self.restarts >= self.retries:
            self.state = "error"
            self.why = ("上游断了（ffmpeg 退出码 %s），已经重连过 %d 次 —— 换个视频或者过一会再试"
                        % (code, self.restarts))
            self._notes.append(self.why)
            self._reader_done.set()
            return "error"
        self.restarts += 1
        note = ("上游断过一次（ffmpeg 退出码 %s），已重取直链从 %.0f s 接着放"
                % (code, self.written_s()))
        self._notes.append(note)
        self.log.warning("bilibili: %s", note)
        try:
            self.stream = self._fetch_stream(self.bvid)
            self.bps = float(self.stream.get("bps") or self.bps)
            self._spawn_ffmpeg(seek_s=self.written_s())
            return "restarted"
        except BufferError as exc:
            self.state = "error"
            self.why = str(exc)
            self._notes.append(str(exc))
            self._reader_done.set()
            return "error"

    def _close_writer(self) -> None:
        """关掉管道写端（= 给播放器一个 EOS）, 幂等。"""
        writer, self._writer = self._writer, None
        if writer is None:
            return
        try:
            writer.close()
        except Exception as exc:                           # noqa: BLE001
            self.log.debug("bilibili: 关管道时出错（忽略）: %r", exc)

    # ------------------------------------------------------- 默认实现 ---
    @staticmethod
    def _default_spawn(argv: List[str]) -> Any:
        return subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                bufsize=0)

    @staticmethod
    def _default_create_fifo(path: str) -> None:
        if os.name == "nt":                                # Windows 没有 mkfifo
            raise BufferError("这块系统上没有命名管道（FIFO）—— 缓冲代理只在板端 Linux 上跑")
        os.mkfifo(path, 0o600)

    @staticmethod
    def _default_open_writer(path: str) -> Any:
        while True:                                        # 等播放器把读端打开（mkfifo 语义）
            try:
                return open(path, "wb", buffering=0)
            except OSError as exc:
                if exc.errno not in (errno.ENXIO, errno.EINTR):
                    raise
                time.sleep(0.05)

    @staticmethod
    def _default_meminfo() -> Dict[str, float]:
        out: Dict[str, float] = {}
        try:
            with open("/proc/meminfo", encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("MemAvailable:"):
                        out["MemAvailable"] = int(line.split()[1]) / 1024.0
                        break
        except OSError:
            pass
        return out
