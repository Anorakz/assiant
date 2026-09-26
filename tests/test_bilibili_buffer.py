#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`agent/core/bilibili_buffer.py`（缓冲代理）的单测 —— **全程离线**。

假掉三样东西（都是外部世界）: ffmpeg 进程（假数据流）、FIFO 写端（记字节）、/proc/meminfo。
于是"15 s 门槛 / 播放中 15 s 封顶 / 暂停延到 60 s / 内存水位停读 / 播过即释放 /
上游断了重取直链重连"这些规矩都能在开发机上断言。

⚠ 两套传输各测各的（T11-10 起产品默认是 **http**）:
  · `TestHttpTransport` —— 起**真的**本机 http 服务, 用 `urllib` 真去读（推荐的那条路）;
  · 其余类用 `transport="fifo"`（夹具就是为 FIFO 语义写的: 假写端/假管道/EPIPE），
    它现在只留给 `dd`/`cat` 这类哑读端排障。
"""
import io
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.core.bilibili_buffer import (                             # noqa: E402
    DEFAULT_INITIAL_S,
    DEFAULT_MAX_S,
    BufferError,
    BilibiliBuffer,
    ffmpeg_argv,
    seconds_to_bytes,
)

#: 码率取整数好算: 800_000 bit/s = 100 KB/s -> 1 s == 100_000 字节
BPS = 800_000
ITEM = {"bvid": "BV1xx411c7mD", "title": "标题"}
PLAIN = {"kind": "plain", "quality": 64, "quality_label": "720P", "bps": BPS,
         "url": "https://upos.example/x.mp4", "size": 1, "length_ms": 1}
DASH = {"kind": "dash", "quality": 80, "quality_label": "1080P", "bps": BPS,
        "video": {"url": "https://upos.example/v.m4s", "bps": 700000, "id": 80},
        "audio": {"url": "https://upos.example/a.m4s", "bps": 100000}}


class FakeProc(object):
    """假 ffmpeg：按约定吐字节；可以"吐到一半断掉"，也可以"吐完就卡住"（模拟网络慢）。"""

    def __init__(self, data, *, fail_after=None, code=1, chunk=65536, then_stall=False):
        self._stream = io.BytesIO(data)
        self._left = None if fail_after is None else int(fail_after)
        self._code = None
        self._exit_code = int(code)
        self._then_stall = bool(then_stall)
        self._closed = False
        self.stdout = self
        self.terminated = False

    def read(self, size):
        if self._left is not None:
            if self._left <= 0:
                self._code = self._exit_code          # 断线：EOF + 非 0 退出码
                return b""
            size = min(size, self._left)
            self._left -= size
        data = self._stream.read(size)
        if not data:
            if self._then_stall and not self._closed:
                while not self._closed:                # 像真管道那样"卡住"（网络慢）
                    time.sleep(0.02)
                return b""
            if self._code is None:
                self._code = 0                         # 正常放完
            return b""
        time.sleep(0.001)
        return data

    def poll(self):
        return self._code

    def terminate(self):
        self.terminated = True
        self._closed = True
        self._code = -15

    def wait(self, timeout=None):
        return self._code


class FakeWriter(object):
    """假 FIFO 写端：记下写进来的字节；`paused=True` 时**卡住不消费**（模拟播放器暂停）。"""

    def __init__(self, delay=0.0):
        self.chunks = []
        self.closed = False
        self.paused = False
        self.delay = float(delay)
        self._resume = threading.Event()
        self._resume.set()

    def write(self, data):
        while self.paused and not self.closed:         # 播放器暂停 -> 管道写不进去
            self._resume.wait(0.05)
        if self.closed:
            raise BrokenPipeError("管道关了")
        if self.delay:
            time.sleep(self.delay)
        self.chunks.append(bytes(data))
        return len(data)

    def close(self):
        self.closed = True
        self._resume.set()

    def set_paused(self, paused):
        self.paused = bool(paused)
        if not paused:
            self._resume.set()
        else:
            self._resume.clear()

    def total(self):
        return sum(len(chunk) for chunk in self.chunks)


class FakeApi(object):
    def __init__(self, streams=None, error=None):
        self.streams = list(streams or [PLAIN])
        self.error = error
        self.calls = 0
        self.bvids = []

    def playurl(self, bvid, cid=None, prefer="plain"):
        self.calls += 1
        self.bvids.append(bvid)
        if self.error is not None:
            raise self.error
        if len(self.streams) > 1:
            return self.streams.pop(0)
        return self.streams[0]

    def media_headers(self, bvid):
        return {"User-Agent": "UA", "Referer": "https://www.bilibili.com/video/%s" % bvid}

    def ffmpeg_input_args(self, bvid, url):
        return ["-user_agent", "UA",
                "-headers", "Referer: https://www.bilibili.com/video/%s\r\n" % bvid,
                "-i", url]


class Harness(object):
    """把注入件拼起来，方便用例里改一处。"""

    def __init__(self, *, seconds_of_data=60, fail_after=None, code=1, writer_delay=0.0,
                 free_mb=3000.0, initial_s=15.0, max_s=60.0, retries=2, api=None,
                 then_stall=False, transport="fifo"):
        self.api = api or FakeApi()
        self.procs = []
        self.argvs = []
        self.writers = []
        self.free_mb = float(free_mb)
        self.data = bytes([7]) * int(BPS / 8.0 * seconds_of_data)
        self.fail_after = fail_after
        self.code = code
        self.writer_delay = writer_delay
        self.then_stall = then_stall
        # ⚠ 默认走 **fifo**（这个夹具就是为 FIFO 语义写的: 假写端、假管道、EPIPE…）;
        #   T11-10 起产品的默认传输是 http, 那条路在 TestHttpTransport 里单测。
        self.buf = BilibiliBuffer(
            self.api, fifo_dir="/tmp", initial_s=initial_s, max_s=max_s, retries=retries,
            spawn=self.spawn, open_writer=self.open_writer,
            create_fifo=lambda path: None, transport=transport,
            meminfo=lambda: {"MemAvailable": self.free_mb})

    def spawn(self, argv):
        self.argvs.append(list(argv))
        proc = FakeProc(self.data, fail_after=self.fail_after, code=self.code,
                        then_stall=self.then_stall)
        self.procs.append(proc)
        return proc

    def open_writer(self, path):
        writer = FakeWriter(delay=self.writer_delay)
        self.writers.append(writer)
        return writer

    @property
    def writer(self):
        return self.writers[0] if self.writers else None

    def start(self, **kwargs):
        return self.buf.start(ITEM, stream=kwargs.pop("stream", PLAIN), **kwargs)

    def wait(self, seconds=0.2):
        time.sleep(seconds)


def wait_until(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def read_raw(url, seconds, timeout=0.3):
    """把一条 http 响应**原样**读一会儿（chunked 的框也留着, 好判断有没有被收尾）。

    @param seconds 读多久（限速期间连接会一直开着 —— 所以必须给个上限）
    """
    import socket as socketlib

    rest = url.split("//", 1)[1]
    hostport, _, path = rest.partition("/")
    host, _, port = hostport.partition(":")
    sock = socketlib.create_connection((host or "127.0.0.1", int(port or 80)), timeout=3.0)
    raw = b""
    try:
        sock.sendall(("GET /%s HTTP/1.1\r\nHost: %s\r\n\r\n" % (path, hostport)).encode())
        sock.settimeout(timeout)
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                block = sock.recv(65536)
            except (socketlib.timeout, OSError):
                continue
            if not block:
                break
            raw += block
    finally:
        sock.close()
    return raw


def parse_chunked_body(raw):
    """把 chunked 响应解成 `(payload, ended)`。

    @param ended 收到 `0\\r\\n\\r\\n`（= 服务端把这条流**收尾**了 —— 播放器会当 EOS）吗
    """
    _, sep, body = raw.partition(b"\r\n\r\n")
    if not sep:
        return b"", False
    payload = b""
    while True:
        line, sep, tail = body.partition(b"\r\n")
        if not sep:
            break
        try:
            size = int(line.split(b";")[0].strip() or b"0", 16)
        except ValueError:
            break
        if size == 0:
            return payload, True
        if len(tail) < size:
            break
        payload += tail[:size]
        body = tail[size + 2:] if len(tail) >= size + 2 else b""
    return payload, False


class TestHelpers(unittest.TestCase):

    def test_seconds_to_bytes(self):
        self.assertEqual(seconds_to_bytes(800_000, 15), 1_500_000)
        self.assertEqual(seconds_to_bytes(0, 15), seconds_to_bytes(2_000_000, 15))
        self.assertEqual(seconds_to_bytes("乱写", 0), 0)

    def test_ffmpeg_argv_plain(self):
        argv = ffmpeg_argv(FakeApi(), PLAIN, "BV1")
        self.assertEqual(argv[0], "ffmpeg")
        self.assertIn("-c", argv)
        self.assertIn("copy", argv)
        self.assertIn("mpegts", argv)
        self.assertEqual(argv[-1], "pipe:1")
        self.assertEqual(argv.count("-i"), 1)
        self.assertIn("Referer: https://www.bilibili.com/video/BV1\r\n", argv)

    def test_ffmpeg_argv_dash_has_two_inputs(self):
        argv = ffmpeg_argv(FakeApi(), DASH, "BV1")
        self.assertEqual(argv.count("-i"), 2)
        self.assertIn("https://upos.example/v.m4s", argv)
        self.assertIn("https://upos.example/a.m4s", argv)

    def test_ffmpeg_argv_seek_before_inputs(self):
        argv = ffmpeg_argv(FakeApi(), PLAIN, "BV1", seek_s=12.5)
        self.assertIn("-ss", argv)
        self.assertLess(argv.index("-ss"), argv.index("-i"))
        self.assertEqual(argv[argv.index("-ss") + 1], "12.500")


class TestStartAndGate(unittest.TestCase):

    def test_start_spawns_ffmpeg_with_headers(self):
        h = Harness()
        h.start()
        try:
            self.assertEqual(len(h.argvs), 1)
            self.assertEqual(h.buf.bvid, "BV1xx411c7mD")
            self.assertTrue(h.buf.path.endswith("bilibili-BV1xx411c7mD.ts"))
            self.assertEqual(h.buf.state, "buffering")
        finally:
            h.buf.stop()

    def test_gate_opens_only_after_the_15s_threshold(self):
        h = Harness(seconds_of_data=30)
        opened = []
        h.buf.on_ready = opened.append
        h.start()
        try:
            self.assertTrue(h.buf.wait_ready(3.0))
            self.assertTrue(h.buf.ready)
            self.assertEqual(opened, [h.buf.path])
            # 账目: 窗口 + 已写 + 在途 = 从 ffmpeg 读到的总量（**必须一次读完**, 否则会少算一块）
            self.assertGreaterEqual(h.buf.progress()["progress_s"], 15.0)
        finally:
            h.buf.stop()

    def test_not_enough_data_is_honest(self):
        h = Harness(seconds_of_data=5, then_stall=True)   # 只来了 5 s 就不再来料了
        h.start()
        try:
            self.assertFalse(h.buf.wait_ready(0.5))
            self.assertFalse(h.buf.ready)
            self.assertIn("网络慢", h.buf.why)
        finally:
            h.buf.stop()

    def test_open_gate_forces_start(self):
        h = Harness(seconds_of_data=5, then_stall=True)
        h.start()
        try:
            h.wait(0.2)
            h.buf.open_gate()
            self.assertTrue(h.buf.ready)
            self.assertIn("没攒够", h.buf.why)
        finally:
            h.buf.stop()

    def test_missing_bvid_is_refused(self):
        h = Harness()
        with self.assertRaises(BufferError):
            h.buf.start({"title": "没有 bvid"}, stream=PLAIN)

    def test_playurl_failure_is_a_buffer_error(self):
        h = Harness(api=FakeApi(error=RuntimeError("被 B 站风控了（HTTP 412）")))
        with self.assertRaises(BufferError) as caught:
            h.buf.start(ITEM)
        self.assertIn("风控", str(caught.exception))


class TestBuffering(unittest.TestCase):

    def test_cap_is_15s_while_playing_and_60s_when_paused(self):
        h = Harness(seconds_of_data=120)
        self.assertEqual(h.buf.cap_s(), 15.0)
        h.buf.set_playing(False)
        # ⚠ T11-10e: **起播前**的 playing=false 不是暂停（GUI 在 setMedia 之后会这么报），
        #   所以先按真顺序播一次, 再暂停。
        self.assertEqual(h.buf.cap_s(), DEFAULT_INITIAL_S)
        h.buf.set_playing(True)                          # 真播起来了
        h.buf.set_playing(False)                         # 这才是暂停
        self.assertEqual(h.buf.cap_s(), DEFAULT_MAX_S)
        h.buf.set_playing(True)
        self.assertEqual(h.buf.cap_s(), DEFAULT_INITIAL_S)

    def test_window_does_not_exceed_the_playing_cap(self):
        h = Harness(seconds_of_data=120, writer_delay=0.02)     # 播放器消费得慢
        h.start()
        try:
            self.assertTrue(h.buf.wait_ready(3.0))
            h.wait(0.5)
            buffered = h.buf.buffered_s()
            self.assertLessEqual(buffered, DEFAULT_INITIAL_S + 1.0)
        finally:
            h.buf.stop()

    def test_pausing_lets_the_window_grow_past_15s(self):
        h = Harness(seconds_of_data=120)
        h.start()
        try:
            self.assertTrue(h.buf.wait_ready(3.0))
            self.assertTrue(wait_until(lambda: h.writer is not None, 2.0),
                            "写线程还没开管道")
            h.writer.set_paused(True)                # 播放器暂停 = 管道那头不读了
            h.buf.set_playing(True)                  # ⚠ T11-10e: 先"真播过"才算暂停（见上一条用例）
            h.buf.set_playing(False)
            self.assertTrue(wait_until(lambda: h.buf.buffered_s() > 20.0, 3.0),
                            "暂停后窗口没长起来: %.1f s" % h.buf.buffered_s())
            self.assertLessEqual(h.buf.buffered_s(), DEFAULT_MAX_S + 1.0)
        finally:
            h.buf.stop()

    def test_memory_watermark_stops_prefetching(self):
        h = Harness(seconds_of_data=120, free_mb=100.0)         # 低于 400 MB 水位
        h.start()
        try:
            h.wait(0.4)
            frozen = h.buf.buffered_s()
            h.wait(0.4)
            self.assertAlmostEqual(frozen, h.buf.buffered_s(), delta=0.3)
        finally:
            h.buf.stop()

    def test_written_bytes_are_released_from_the_window(self):
        h = Harness(seconds_of_data=120)
        h.start()
        try:
            self.assertTrue(h.buf.wait_ready(3.0))
            self.assertTrue(wait_until(lambda: h.buf.written_s() > 3.0, 3.0))
            self.assertLessEqual(h.buf.buffered_s(), h.buf.cap_s() + 1.0)
            self.assertGreater(h.buf.written_s(), 0.0)
            self.assertGreater(h.writers[0].total(), 0)
        finally:
            h.buf.stop()

    def test_stop_releases_everything_and_is_idempotent(self):
        h = Harness()
        h.start()
        self.assertTrue(h.buf.wait_ready(3.0))
        h.buf.stop()
        self.assertEqual(h.buf.snapshot()["bytes"], 0)
        self.assertEqual(h.buf.path, "")
        self.assertTrue(h.procs[0].terminated)
        h.buf.stop()                                    # 再来一次不炸
        self.assertFalse(h.buf.ready)


class TestHttpTransport(unittest.TestCase):
    """T11-10: **http 传输**（产品默认）—— 起真服务, 真去读。

    为什么必须换: 板端实测 `playbin uri=file://<FIFO>`（QMediaPlayer 的后端）**一个字节都不读**
    （FIFO `stat` size=0、不可 seek, preroll 过不去）; 换成 `http://127.0.0.1:…` + chunked
    之后 static/live 两种供法播放器都能持续读。这个类把那条链路的**服务端**钉住。
    """

    def harness(self, seconds_of_data=60, **kwargs):
        h = Harness(seconds_of_data=seconds_of_data, **kwargs)
        h.buf.transport = "http"
        h.buf.port = 0                        # 随机端口, 免得撞上板端/开发机上别的东西
        return h

    def start(self, h, timeout=6.0):
        h.buf.start(dict(ITEM))
        self.assertTrue(h.buf.wait_ready(timeout), "没攒够门槛")
        self.assertTrue(h.buf.stream_target(), "开闸了却没有地址")
        return h.buf.stream_target()

    def test_the_url_shape_and_token(self):
        h = self.harness()
        try:
            url = self.start(h)
            buf = h.buf
            self.assertTrue(url.startswith("http://127.0.0.1:%d/stream/%s?v=" % (buf.port,
                                                                                ITEM["bvid"])),
                            url)
            self.assertTrue(buf.token and buf.token in url)
            self.assertEqual(buf.snapshot()["url"], url)
            self.assertEqual(buf.snapshot()["transport"], "http")
            # 旧字段仍然在（老客户端/验收脚本读它）：path 为空, stream = URL
            self.assertEqual(buf.path, "")
            self.assertEqual(buf.snapshot()["stream"], url)
        finally:
            h.buf.stop()

    def test_it_really_serves_the_stream(self):
        h = self.harness()
        try:
            url = self.start(h)
            with urllib.request.urlopen(url, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.headers.get("Content-Type"), "video/mp2t")
                # 没有总长度 = 播放器当它直播流（顺序读、不 seek）
                self.assertIsNone(resp.headers.get("Content-Length"))
                block = resp.read(200000)
            self.assertEqual(block, h.data[:200000], "喂出来的字节必须就是 ffmpeg 那几个字节")
            # 播过即释放: 窗口里那一段没了, 已喂出去的秒数上去了
            self.assertGreater(h.buf.written_s(), 1.5)
            self.assertLess(len(h.buf._window), len(h.data))
        finally:
            h.buf.stop()

    def test_the_client_counter_goes_up_and_down(self):
        h = self.harness()
        try:
            url = self.start(h)
            resp = urllib.request.urlopen(url, timeout=5)
            self.assertTrue(self.wait(lambda: h.buf.snapshot()["clients"] == 1, 3.0),
                            "连上了但没登记客户端")
            resp.read(1000)
            resp.close()
            self.assertTrue(self.wait(lambda: h.buf.snapshot()["clients"] == 0, 3.0),
                            "走了但没销号")
        finally:
            h.buf.stop()

    def test_a_wrong_token_or_path_is_refused(self):
        h = self.harness()
        try:
            url = self.start(h)
            base = url.split("?")[0]
            for bad in (base + "?v=deadbeef", base.replace(ITEM["bvid"], "BV0"), url.replace(
                    "/stream/", "/etc/")):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(bad, timeout=5)
                self.assertIn(caught.exception.code, (403, 404))
        finally:
            h.buf.stop()

    def test_only_bound_to_localhost(self):
        h = self.harness()
        try:
            self.start(h)
            self.assertEqual(h.buf._server.server_address[0], "127.0.0.1",
                             "只许绑本机（不对外）")
        finally:
            h.buf.stop()

    def test_stop_closes_the_server(self):
        h = self.harness()
        url = self.start(h)
        with urllib.request.urlopen(url, timeout=5) as resp:
            resp.read(1000)
        h.buf.stop()
        self.assertEqual(h.buf.url, "")
        with self.assertRaises(Exception):
            urllib.request.urlopen(url, timeout=2)

    def test_a_busy_port_falls_back_to_another_one(self):
        import socket as socketlib

        holder = socketlib.socket(socketlib.AF_INET, socketlib.SOCK_STREAM)
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        busy = holder.getsockname()[1]
        h = self.harness()
        h.buf.port = busy
        try:
            url = self.start(h)
            self.assertTrue(url.startswith("http://127.0.0.1:%d/" % h.buf.port), url)
            self.assertNotEqual(h.buf.port, busy, "端口被占时该换一个, 而不是开不了闸")
        finally:
            h.buf.stop()
            holder.close()

    def test_the_gate_hands_over_an_http_url_not_a_path(self):
        h = self.harness()
        got = []
        h.buf.on_ready = got.append
        try:
            url = self.start(h)
            self.assertEqual(got, [url], "开闸交给播放器的必须是那个 http 地址")
            self.assertTrue(got[0].startswith("http://"))
        finally:
            h.buf.stop()

    # ------------------------------------------------------------------
    #  T11-10e: 板端验收抓到的两个真问题（"限速"被当成"放完了" / 起播前的
    #  playing=false 被当成"暂停"）—— 都在这里钉死。
    # ------------------------------------------------------------------

    def test_lead_cap_makes_the_client_wait_instead_of_eos(self):
        """**限速期间连接必须还开着、还在慢慢喂** —— 不能写成 EOS。

        板端实测（T11-9/T11-10 验收）: 播放器一口气吞到封顶 -> 服务端"0.2 秒没等到料"
        就收工写 chunked 结束块 -> 播放器报 `EndOfMedia` -> GUI 报 `eof` -> Agent 自动
        下一集 -> 换条又把流收掉 -> **一路连跳 6 条**（每条 1~9 秒就"放完了"）。
        """
        h = self.harness(seconds_of_data=600, initial_s=1.0)     # 封顶 1 s: 几下就撞上
        try:
            url = self.start(h)
            raw = read_raw(url, 5.0)
            payload, ended = parse_chunked_body(raw)
            got_s = len(payload) / (BPS / 8.0)
            self.assertFalse(ended, "被限速挡住时**不该**收到 chunked 结束块（= 播放器会当放完）")
            self.assertGreater(got_s, 2.0,
                               "限速期间还得继续慢慢喂（实测只喂了 %.1f s）" % got_s)
            self.assertFalse(h.buf.finished(), "还在喂的时候不能说'放完了'")
            self.assertNotIn(h.buf.state, ("ended", "stopped", "error"))
        finally:
            h.buf.stop()

    def test_nothing_is_served_before_the_gate_opens(self):
        """没攒够 15 s（没开闸）-> **一个字节都不喂**（以前"没开闸=放行"，门槛被作废）。"""
        # 卡住不吐（`then_stall`）: 只有 10 s 料, 门槛 90 s -> 永远不会开闸
        h = self.harness(seconds_of_data=10, initial_s=90.0, then_stall=True)
        try:
            h.buf.start(dict(ITEM))
            self.assertTrue(wait_until(lambda: h.buf.buffered_s() > 1.0, 3.0), "假 ffmpeg 没吐料")
            self.assertFalse(h.buf.ready, "没攒够门槛就不该 ready")
            self.assertFalse(h.buf._lead_ok(), "没开闸 -> 不喂")
            payload, ended = parse_chunked_body(read_raw(h.buf.stream_target(), 1.5))
            self.assertEqual(payload, b"", "没开闸却喂了 %d 字节" % len(payload))
            self.assertFalse(ended, "没开闸也不该把连接收掉（它只是在等）")
        finally:
            h.buf.stop()

    def test_false_playing_before_start_is_not_a_pause(self):
        """GUI 在 `setMedia()` 之后头几秒报的 `playing=false` **不是暂停**（那会儿还没起播）。"""
        h = self.harness(seconds_of_data=600)
        buf = h.buf
        try:
            self.assertTrue(buf.snapshot()["playing"], "默认按播放中算")
            buf.set_playing(False)                     # 起播前那几条
            self.assertEqual(buf.cap_s(), 15.0, "起播前的 playing=false 不能把封顶放到 60 s")
            self.assertFalse(buf.snapshot()["paused"])
            self.assertFalse(buf.snapshot()["saw_playing"])
            buf.set_playing(True)                      # 真播起来了
            self.assertTrue(buf.snapshot()["saw_playing"])
            buf.set_playing(False)                     # 这下才是暂停
            self.assertEqual(buf.cap_s(), 60.0)
            self.assertTrue(buf.snapshot()["paused"])
        finally:
            buf.stop()

    def test_finished_is_our_own_truth(self):
        """`finished()` = **我们自己**说放完了（ffmpeg 正常收尾）, 不看播放器怎么说。"""
        h = self.harness(seconds_of_data=600, initial_s=1.0)
        try:
            self.start(h)
            self.assertFalse(h.buf.finished(), "还在喂的时候不算放完")
        finally:
            h.buf.stop()
        self.assertFalse(h.buf.finished(), "被我们收工（换条/清空）更不算放完")

        done = Harness(seconds_of_data=3, initial_s=1.0)     # 比门槛还短 -> 很快放完
        done.start()
        try:
            self.assertTrue(done.buf.wait_ready(3.0), "门槛没开")
            self.assertTrue(wait_until(lambda: done.buf.finished(), 5.0),
                            "短片放完该报 ended（现在 state=%s）" % done.buf.state)
        finally:
            done.buf.stop()

    @staticmethod
    def wait(predicate, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False


class TestUpstreamRecovery(unittest.TestCase):

    def test_upstream_break_restarts_with_seek_and_reports_it(self):
        h = Harness(seconds_of_data=60, fail_after=int(BPS / 8.0 * 20))
        h.start()
        try:
            self.assertTrue(h.buf.wait_ready(3.0))
            self.assertTrue(wait_until(lambda: len(h.argvs) >= 2, 4.0), "没重连")
            second = h.argvs[1]
            self.assertIn("-ss", second)
            seek = float(second[second.index("-ss") + 1])
            self.assertGreater(seek, 0.0)
            # 起播时直链是外面给的, 所以这一次 API 调用**就是重连时重取的那一次**
            self.assertEqual(h.api.calls, 1)
            self.assertEqual(h.api.bvids, ["BV1xx411c7mD"])
            notes = h.buf.notes()
            self.assertTrue(any("重取直链" in note for note in notes), notes)
            self.assertEqual(h.buf.restarts, 1)
        finally:
            h.buf.stop()

    def test_retries_exhausted_reports_error(self):
        h = Harness(seconds_of_data=60, fail_after=int(BPS / 8.0 * 3), retries=1)
        h.start()
        try:
            self.assertTrue(wait_until(lambda: h.buf.state == "error", 4.0),
                            "状态没变成 error: %s" % h.buf.state)
            self.assertIn("上游断了", h.buf.why)
            self.assertIn("重连过", h.buf.why)
            self.assertTrue(h.buf.notes())
        finally:
            h.buf.stop()

    def test_short_video_ends_normally(self):
        h = Harness(seconds_of_data=3)                  # 比门槛还短
        h.start()
        try:
            self.assertTrue(wait_until(lambda: h.buf.state in ("ended", "serving", "paused"),
                                       3.0), "状态: %s" % h.buf.state)
            self.assertEqual(h.buf.restarts, 0)         # 放完**不该**被当成"上游断了"
        finally:
            h.buf.stop()

    def test_normal_end_is_not_treated_as_an_upstream_break(self):
        """实测踩过: `poll()` 还没回收到退出码（None）时被误判成断线 -> 无脑重连。"""
        h = Harness(seconds_of_data=20)                 # 短片: 攒够 15 s 后很快就放完
        h.start()
        try:
            self.assertTrue(h.buf.wait_ready(3.0))
            self.assertTrue(wait_until(lambda: h.buf.state == "ended", 5.0),
                            "状态没到 ended: %s（why=%s）" % (h.buf.state, h.buf.why))
            self.assertEqual(h.buf.restarts, 0)
            self.assertEqual(h.buf.notes(), [])
            self.assertIn("放完了", h.buf.why)
            # 收尾: 剩下的料喂完 -> 关管道（= 给播放器 EOS）
            self.assertTrue(wait_until(lambda: h.writer is not None and h.writer.closed, 3.0),
                            "管道写端没关")
            self.assertEqual(h.writer.total(), len(h.data))
        finally:
            h.buf.stop()

    def test_snapshot_shape(self):
        h = Harness()
        h.start()
        try:
            self.assertTrue(h.buf.wait_ready(3.0))
            snap = h.buf.snapshot()
            for key in ("state", "ready", "path", "bvid", "buffered_s", "written_s",
                        "cap_s", "playing", "bytes", "ffmpeg_alive", "restarts", "why"):
                self.assertIn(key, snap)
            self.assertTrue(snap["ffmpeg_alive"])
            self.assertEqual(snap["bvid"], "BV1xx411c7mD")
        finally:
            h.buf.stop()

    def test_start_twice_replaces_the_previous_one(self):
        h = Harness()
        h.start()
        try:
            self.assertTrue(h.buf.wait_ready(3.0))
            h.start()                                    # 换片
            self.assertEqual(len(h.argvs), 2)
            self.assertTrue(h.procs[0].terminated)
            self.assertEqual(h.buf.written_s(), 0.0)
        finally:
            h.buf.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
