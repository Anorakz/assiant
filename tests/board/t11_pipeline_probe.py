#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T11-0 门禁实测之二：**全链路**（ffmpeg -c copy 合流 → 本地 HTTP → GStreamer 播放）。

验四件事:
  1. `ffmpeg -c copy` 能把 **DASH 两路（HEVC 视频 + AAC 音频）** 合成一条可顺序播的流；
  2. 我们的"内存缓冲 + 本地 Range 服务"这层原型能把它喂给播放器（**不做转码**）；
  3. 播放端真的用 **`mppvideodec`（VPU 硬解）** 而不是软解；
  4. 全链路 CPU / 内存 / 起播延迟（15 s 门槛要多久凑够）。

跑法（板端，仓库根）::

    python3 tests/board/t11_pipeline_probe.py                 # 默认 DASH 1080P, 播 60 s
    python3 tests/board/t11_pipeline_probe.py --quality plain  # 单文件 720P 对照
    python3 tests/board/t11_pipeline_probe.py --serve-only     # 只起服务（给 GUI 的 --video 用）

⚠ 这是**门禁探针**（单文件、跑完就退出）；正式实现是 `agent/core/bilibili_buffer.py`（T11-3）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
import http.cookiejar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, "/home/kickpi/myproject/assitant")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def rss_mb(pid):
    try:
        with open("/proc/%d/status" % pid, encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        pass
    return 0.0


def cpu_seconds(pid):
    try:
        with open("/proc/%d/stat" % pid, encoding="utf-8") as handle:
            parts = handle.read().split()
        ticks = os.sysconf("SC_CLK_TCK")
        return (int(parts[13]) + int(parts[14])) / float(ticks)
    except Exception:                                        # noqa: BLE001
        return 0.0


def children_cpu():
    import resource

    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


# ---------------------------------------------------------------------------
#  上游：拿 B 站直链（匿名 + cookie 都试)
# ---------------------------------------------------------------------------
def pick_streams(quality):
    from agent.config import load_config

    load_config("config")
    sess = ""
    try:
        with open("config/bilibili_cookie.json", encoding="utf-8") as handle:
            sess = json.load(handle).get("SESSDATA") or ""
    except Exception:                                        # noqa: BLE001
        pass
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def get(url, cookie=False, parse=True):
        head = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}
        if cookie and sess:
            head["Cookie"] = "SESSDATA=%s" % sess
        req = urllib.request.Request(url, headers=head)
        with opener.open(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", "replace")
        return json.loads(body) if parse else body

    get("https://www.bilibili.com/", parse=False)
    search = get("https://api.bilibili.com/x/web-interface/search/type"
                 "?search_type=video&keyword=lunasaymaybe&order=totalrank")
    item = search["data"]["result"][0]
    view = get("https://api.bilibili.com/x/web-interface/view?bvid=%s" % item["bvid"])["data"]
    print("   视频: %s  %s" % (item["bvid"], item["title"][:30]))
    if quality == "dash":
        data = get("https://api.bilibili.com/x/player/playurl?bvid=%s&cid=%s&qn=112&fnval=16"
                   "&platform=pc&high_quality=0" % (item["bvid"], view["cid"]), cookie=True)["data"]
        videos = sorted((data.get("dash") or {}).get("video") or [],
                        key=lambda d: -int(d.get("id", 0)))
        audios = sorted((data.get("dash") or {}).get("audio") or [],
                        key=lambda d: -int(d.get("bandwidth", 0)))
        if not videos:
            print("   !! 这条视频拿不到 DASH（cookie 有效吗？）")
            return None
        best, audio = videos[0], audios[0]
        bps = int(best["bandwidth"]) + int(audio.get("bandwidth", 0))
        print("   DASH: 视频 id=%s %d kbps + 音频 %d kbps"
              % (best["id"], int(best["bandwidth"]) // 1024, int(audio.get("bandwidth", 0)) // 1024))
        return {"kind": "dash", "video": best["baseUrl"], "audio": audio["baseUrl"],
                "bps": bps, "bvid": item["bvid"]}
    data = get("https://api.bilibili.com/x/player/playurl?bvid=%s&cid=%s&qn=80&fnval=1"
               "&platform=html5&high_quality=0" % (item["bvid"], view["cid"]))["data"]
    seg = data["durl"][0]
    bps = seg["size"] * 8.0 / (seg["length"] / 1000.0)
    print("   单文件: quality=%s %.0f kbps" % (data.get("quality"), bps / 1000.0))
    return {"kind": "plain", "url": seg["url"], "bps": bps, "bvid": item["bvid"]}


# ---------------------------------------------------------------------------
#  缓冲 + 本地 Range 服务（门禁原型：内存里长、按需等）
# ---------------------------------------------------------------------------
class Buffer(object):
    def __init__(self, bps):
        self.data = bytearray()
        self.eof = False
        self.cond = threading.Condition()
        self.bps = max(bps, 1.0)
        self.started = time.time()
        self.first_client = None

    def feed(self, chunk):
        with self.cond:
            self.data.extend(chunk)
            self.cond.notify_all()

    def close(self):
        with self.cond:
            self.eof = True
            self.cond.notify_all()

    def seconds(self):
        return len(self.data) * 8.0 / self.bps

    def wait_for(self, offset, timeout=60.0):
        deadline = time.time() + timeout
        with self.cond:
            while len(self.data) <= offset and not self.eof:
                left = deadline - time.time()
                if left <= 0:
                    return False
                self.cond.wait(min(left, 0.5))
            return len(self.data) > offset

    def slice_from(self, offset, want=65536):
        with self.cond:
            return bytes(self.data[offset:offset + want])


def make_handler(buffer, total_estimate):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):                        # 静音
            pass

        def do_GET(self):
            if self.path.startswith("/status"):
                body = json.dumps({"buffered": len(buffer.data), "seconds": round(buffer.seconds(), 1),
                                   "eof": buffer.eof}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            match = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range", "") or "")
            start = int(match.group(1)) if match else 0
            if buffer.first_client is None:
                buffer.first_client = time.time()
                print("   ▶ 播放器连上来了: %s（已缓冲 %.1f s）"
                      % (self.path, buffer.seconds()))
            total = total_estimate or 0
            # ⚠ 门禁原型的规矩: **知道总长才回 206**；不知道就老老实实 200+chunked 从 0 顺序喂
            #   （206 却不带 Content-Range 会让 souphttpsrc/decodebin 直接抛 std::runtime_error —— 实测踩到）。
            #   正式实现（T11-3）会把总长算出来并支持任意 Range（seek 要用）。
            if match and not total:
                if start:
                    print("   ⚠ 播放器要 Range %s 但上游总长未知 —— 从 0 顺序喂（门禁原型不支持 seek）"
                          % self.headers.get("Range"))
                start, match = 0, None
            ok = buffer.wait_for(start, timeout=30)
            if not ok:
                if buffer.eof and start >= len(buffer.data):
                    self.send_error(416, "beyond the end")   # 播到尾巴了：让播放器当 EOF 处理
                else:
                    self.send_error(504, "buffer underrun")
                return
            self.send_response(206 if match else 200)
            self.send_header("Content-Type", "video/mp4")
            if match and total:
                end = (int(match.group(2)) if match.group(2) else total - 1)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, total))
                self.send_header("Content-Length", str(max(end - start + 1, 0)))
            else:
                self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            offset = start
            try:
                while True:
                    if not buffer.wait_for(offset, timeout=30):
                        break
                    chunk = buffer.slice_from(offset)
                    if not chunk:
                        if buffer.eof:
                            break
                        continue
                    if match and total:
                        self.wfile.write(chunk)
                    else:
                        self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                    offset += len(chunk)
                    with buffer.cond:
                        if buffer.eof and offset >= len(buffer.data):
                            break
                if not (match and total):
                    self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--quality", choices=("dash", "plain"), default="dash")
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--max-s", type=float, default=60.0, help="内存窗口上限（秒）")
    parser.add_argument("--serve-only", action="store_true")
    args = parser.parse_args()

    print("== 1) 取流")
    streams = pick_streams(args.quality)
    if not streams:
        return 2
    estimate_total = None
    if streams["kind"] == "plain":
        estimate_total = None                       # 单文件我们也走流式（不知道总长也没关系）

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "info", "-fflags", "+genpts"]
    # ⚠ 实测（T11-0）: ffmpeg 拉 B 站 CDN 直链**必须带 UA + Referer**，否则两个 CDN 都回
    #   403 Forbidden（urllib 那次能成是因为我手动带了头）。每个 `-i` 前的头只作用于那个输入。
    headers = "Referer: https://www.bilibili.com/video/%s\r\n" % streams["bvid"]
    for url in ([streams["video"], streams["audio"]] if streams["kind"] == "dash"
                else [streams["url"]]):
        cmd += ["-user_agent", UA, "-headers", headers, "-i", url]
    cmd += ["-c", "copy", "-movflags", "frag_keyframe+empty_moov", "-f", "mp4", "pipe:1"]
    print("== 2) 起 ffmpeg: ffmpeg … %d 个输入 -c copy → pipe:1" % (2 if streams["kind"] == "dash" else 1))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    ff_log = []

    def read_ff():
        for line in proc.stderr:
            ff_log.append(line.decode("utf-8", "replace"))

    threading.Thread(target=read_ff, daemon=True).start()

    buffer = Buffer(streams["bps"])
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(buffer, estimate_total))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/stream/%s" % (args.port, streams["bvid"])
    print("   本地地址: %s" % url)

    def pump():
        """把 ffmpeg 的输出搬进内存窗口；**带背压** —— 窗口够了就不读，
        ffmpeg 写满管道自然阻塞（实测它比实时快 68×，不背压会几秒灌完整部视频）。"""
        try:
            while True:
                if buffer.seconds() >= args.max_s and not buffer.eof:
                    time.sleep(0.2)
                    continue
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                buffer.feed(chunk)
        finally:
            buffer.close()

    threading.Thread(target=pump, daemon=True).start()

    print("== 3) 等 15 s 门槛（按 %.0f kbps 估算）" % (streams["bps"] / 1000.0))
    started = time.time()
    while buffer.seconds() < 15.0 and not buffer.eof and time.time() - started < 60:
        time.sleep(0.2)
    print("   凑够 15 s 用了 %.1fs（实际缓冲 %.1f s, %d KB）"
          % (time.time() - started, buffer.seconds(), len(buffer.data) // 1024))
    if not buffer.data:                                   # ffmpeg 没吐东西 -> 如实说清
        print("   ✘ ffmpeg 一段数据都没吐（退出码 %s）；stderr 末尾:" % proc.poll())
        for line in "".join(ff_log).strip().splitlines()[-6:]:
            print("        %s" % line)
        return 3

    if args.serve_only:
        print("== serve-only: 保持服务, Ctrl-C 退出（GUI 用: --video %s）" % url)
        try:
            while True:
                time.sleep(5)
        except KeyboardInterrupt:
            pass
        return 0

    print("== 4) 播放 %.0f s（GStreamer：souphttpsrc → decodebin → fakesink）" % args.seconds)
    gst_cmd = ["gst-launch-1.0", "-v", "souphttpsrc", "location=%s" % url, "!", "decodebin", "!",
               "fakesink", "sync=true"]
    cpu0, wall0 = children_cpu(), time.time()
    gst = subprocess.Popen(gst_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    log = []

    def read_gst():
        for line in gst.stderr:
            log.append(line.decode("utf-8", "replace"))

    threading.Thread(target=read_gst, daemon=True).start()
    peak_ff, peak_srv = 0.0, 0.0
    deadline = time.time() + args.seconds
    while time.time() < deadline and gst.poll() is None:
        peak_ff = max(peak_ff, rss_mb(proc.pid))
        peak_srv = max(peak_srv, rss_mb(os.getpid()))
        time.sleep(0.3)
    playing = gst.poll() is None
    if playing:
        gst.terminate()
        time.sleep(1)
    wall = time.time() - wall0
    cpu = children_cpu() - cpu0
    text = "".join(log)
    decoders = sorted(set(re.findall(r"(mppvideodec|avdec_h264|avdec_h265|mppvideodec_h265)",
                                     text)))
    print("   播放 %s（%.0f s）| 全链路 CPU %.1f s = 每分钟视频 %.1f s | ffmpeg 峰值 %.0f MB / 服务 %.0f MB"
          % ("还在放" if playing else "自己结束了", wall, cpu, cpu / max(wall, 1) * 60.0,
             peak_ff, peak_srv))
    print("   播放器选中的解码器: %s" % (decoders or "（日志里没看到，需另查）"))
    print("   缓冲现状: %.1f s（%.1f MB）" % (buffer.seconds(), len(buffer.data) / 1048576.0))
    if gst.returncode not in (0, None, -15):
        print("   ⚠ gst 退出码 %s；日志头 25 行:" % gst.returncode)
        for line in text.strip().splitlines()[:25]:
            print("        %s" % line)
        print("        …（tail）")
        for line in text.strip().splitlines()[-6:]:
            print("        %s" % line)
    proc.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
