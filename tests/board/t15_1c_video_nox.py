#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T15-1 1c 关键测试：EGLFS（无 X）下 `QMediaPlayer` 能不能播"Agent 那种本机 HTTP 流"。

做法（等价于真 B 站流，但把不确定性拿掉）：
  1) ffmpeg 起一个**本机 MPEG-TS HTTP 流**（`-listen 1 http://127.0.0.1:9123/live.ts`），
     与我们 Agent 侧 buffer 给 GUI 的形态一致（http + chunked + MPEG-TS）；
  2) 一个**假 IPC 服务端**（AF_UNIX）顶替 Agent：推 `status{mode:GAME}` 让 GUI 切到视频区，
     再推 `bilibili{stream:…, ready:true, queue/current/…}` —— 字段照 docs/ipc-protocol.md §3；
  3) GUI 在 EGLFS + 旋转下跑，20 秒后截图（播放器要 ~15 秒预取才起播）；
  4) 收尾把桌面/GUI 复原。
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time

REPO = "/home/kickpi/myproject/assitant"
GUI = os.path.join(REPO, "gui", "build", "agent_gui")
FAKE_SOCK = "/tmp/t151_fake.sock"
TS_URL = "http://127.0.0.1:9123/live.ts"
VIDEO = "/tmp/t151_v10.mp4"


def say(msg):
    print(msg, flush=True)


def systemctl(*args):
    return subprocess.run(["systemctl", *args], stdout=subprocess.DEVNULL).returncode


def start_fake_agent():
    """假 Agent：接受一个客户端，推 status(GAME) + bilibili(stream)。"""
    if os.path.exists(FAKE_SOCK):
        os.unlink(FAKE_SOCK)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(FAKE_SOCK)
    server.listen(1)
    os.chmod(FAKE_SOCK, 0o600)

    def serve():
        try:
            conn, _ = server.accept()
        except OSError:
            return
        now = time.time()
        status = {"topic": "status", "data": {"mode": "GAME", "connected": True}, "timestamp": now}
        item = {"bvid": "BV1fake", "title": "T15-1 视频渲染测试", "author": "local",
                "duration_s": 10, "play": 1, "cover": "", "url": ""}
        bili = {"topic": "bilibili",
                "data": {"queue": [item], "index": 0, "current": item, "keyword": "(本机流)",
                         "source": "dialogue", "viewport": 6, "target": 6, "ready": True,
                         "stream": TS_URL, "quality": "720P",
                         "note": "T15-1 1c：本机 MPEG-TS 流（等价于 Agent 给的那条）"},
                "timestamp": now + 0.1}
        for message in (status, bili):
            try:
                conn.sendall((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
                time.sleep(0.4)
            except OSError:
                return
        # 之后保持连接（GUI 会一直用这条连接）
        try:
            while True:
                time.sleep(1)
                conn.sendall((json.dumps(status) + "\n").encode("utf-8"))
        except OSError:
            pass

    threading.Thread(target=serve, daemon=True).start()
    return server


def main():
    say("== T15-1 1c：EGLFS 下播本机 MPEG-TS 流")
    # 1) 本机 TS 流
    ff = subprocess.Popen(["ffmpeg", "-re", "-stream_loop", "-1", "-i", VIDEO,
                           "-c:v", "copy", "-f", "mpegts", "-listen", "1", TS_URL],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    say("  ffmpeg 流: %s（pid=%d，存活=%s）" % (TS_URL, ff.pid, ff.poll() is None))

    server = start_fake_agent()
    say("  假 Agent socket: %s" % FAKE_SOCK)

    # 2) 停桌面，让出 DRM
    systemctl("stop", "agent-gui.service")
    systemctl("stop", "slim.service")
    time.sleep(5)
    xorg = subprocess.run(["pgrep", "-c", "Xorg"], stdout=subprocess.PIPE,
                          universal_newlines=True).stdout.strip() or "0"
    say("  Xorg 进程数=%s（0 = 已让出 DRM）" % xorg)

    # 3) EGLFS + 旋转 + 真播放路径
    env = dict(os.environ)
    env.update({"QT_QPA_PLATFORM": "eglfs",
                "QT_QPA_EGLFS_INTEGRATION": "eglfs_kms",
                "QT_QPA_EGLFS_ROTATION": "90",
                "GST_PLUGIN_FEATURE_RANK": "souphttpsrc:0",   # 板端 souphttpsrc 是坏的
                "QT_LOGGING_RULES": "qt.multimedia.*=true"})
    shot, log = "/tmp/t151_1c_eglfs.png", "/tmp/t151_1c_eglfs.log"
    with open(log, "wb") as handle:
        proc = subprocess.Popen([GUI, "--config", os.path.join(REPO, "config", "config.yaml"),
                                 "--socket", FAKE_SOCK, "--page", "home",
                                 "--screenshot", shot, "--screenshot-delay", "20000"],
                                cwd=REPO, stdout=handle, stderr=subprocess.STDOUT, env=env)
        proc.wait(timeout=120)
    say("  GUI 退出码=%s  截图=%s 字节"
        % (proc.returncode, os.path.getsize(shot) if os.path.exists(shot) else "（无）"))

    text = open(log, encoding="utf-8", errors="replace").read()
    say("  --- 视频/播放器相关日志 ---")
    for line in text.splitlines():
        low = line.lower()
        if any(key in low for key in ("[video]", "media", "player", "gst", "state", "error",
                                      "playing", "buffering")):
            say("    " + line[:150])
    say("  --- 错误行 ---")
    for line in text.splitlines():
        if any(key in line.lower() for key in ("error", "could not", "fail", "not found")):
            say("    " + line[:150])

    # 4) 复原
    ff.terminate()
    server.close()
    if os.path.exists(FAKE_SOCK):
        os.unlink(FAKE_SOCK)
    say("  === 复原桌面 ===")
    systemctl("start", "slim.service")
    time.sleep(6)
    systemctl("restart", "xrandr-startup.service")
    time.sleep(3)
    systemctl("restart", "agent-gui.service")
    time.sleep(10)
    rot = subprocess.run(["bash", "-lc",
                          "DISPLAY=:0 xrandr --query 2>/dev/null | grep -oE 'DSI-1 connected [0-9x+]+ [a-z]+'"],
                         stdout=subprocess.PIPE, universal_newlines=True).stdout.strip()
    say("  slim=%s gui=%s rot=%s"
        % (subprocess.run(["systemctl", "is-active", "slim.service"], stdout=subprocess.PIPE,
                          universal_newlines=True).stdout.strip(),
           subprocess.run(["systemctl", "is-active", "agent-gui.service"], stdout=subprocess.PIPE,
                          universal_newlines=True).stdout.strip(), rot))
    say("  截图存在=%s" % os.path.exists(shot))
    return 0


if __name__ == "__main__":
    sys.exit(main())
