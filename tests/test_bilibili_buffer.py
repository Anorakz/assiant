#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`agent/core/bilibili_buffer.py`（缓冲代理）的单测 —— **全程离线**。

假掉三样东西（都是外部世界）: ffmpeg 进程（假数据流）、FIFO 写端（记字节）、/proc/meminfo。
于是"15 s 门槛 / 播放中 15 s 封顶 / 暂停延到 60 s / 内存水位停读 / 播过即释放 /
上游断了重取直链重连"这些规矩都能在开发机上断言。
"""
import io
import os
import sys
import threading
import time
import unittest

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
                 then_stall=False):
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
        self.buf = BilibiliBuffer(
            self.api, fifo_dir="/tmp", initial_s=initial_s, max_s=max_s, retries=retries,
            spawn=self.spawn, open_writer=self.open_writer,
            create_fifo=lambda path: None,
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
