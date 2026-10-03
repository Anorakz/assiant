#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T15-1 1b 取证：无 X 形态下"软键盘"（Qt 虚拟键盘）弹没弹、画没画、能不能打字、挡不挡布局。

为什么要跑四种平台（都是实测撞出来的，不是猜的）
  · Qt 虚拟键盘是**独立窗口** -> `--screenshot`（QWidget::grab）抓不到它；
  · EGLFS/KMS **不写 /dev/fb0**（1a 实测）-> fb 像素只能在 linuxfb 下取；
  · linuxfb **没有 alpha 合成**：面板那个"半透明整屏窗口"在 fb 上变成**不透明白底**，
    所以 linuxfb 能证明"键盘真画出来了"，但证明不了"app 还看得见"；
  · 能同时给出"键盘在底部 + app 还在"的只有**有合成**的平台：这里用 Qt 自带的 `vnc`
    平台（`gst-launch-1.0 rfbsrc` 抓一帧），它是无 X 的；
  · 键盘的坐标/输入必须用 **EGLFS + 旋转**（= 产品形态，逻辑屏 1280×800）。

四段跑，每段只主张一件事
  P1 linuxfb + 焦点：fb 快照"点输入框前 vs 后" -> 键盘真的画在窗口底部（像素 + PNG 复核）
  P2 EGLFS + 旋转：`[ui] 无 X（eglfs）…` 日志、屏 1280×800、主窗口没被改小、
     键盘窗口可见、IM_VISIBLE=1、QML 不缺模块
  P3 EGLFS + uinput 虚拟触摸屏点键盘：`--dump-input` 逐秒打印输入框里真装着的文本（能打字）
  P4 vnc 平台 + rfbsrc：一帧合成图 -> 键盘贴在**逻辑屏底部**、上面的 app 界面还在（不挡布局）

收尾一定复原桌面：slim -> xrandr-startup（oneshot，不 restart 会丢旋转）-> agent-gui。
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time

REPO = "/home/kickpi/myproject/assitant"
GUI = os.path.join(REPO, "gui", "build", "agent_gui")
CFG = os.path.join(REPO, "config", "config.yaml")
FB = "/dev/fb0"
FAKE_SOCK = "/tmp/t151b_fake.sock"
LOG_P1 = "/tmp/t151b_P1_linuxfb.log"
LOG_P2 = "/tmp/t151b_P2_eglfs.log"
LOG_P3 = "/tmp/t151b_P3_type.log"
LOG_P4 = "/tmp/t151b_P4_vnc.log"
PNG_P4 = "/tmp/t151b_P4_vnc.png"
TOUCH_NAME = "t151b-uitouch"
RUNTIME_DIR = "/tmp/t151b_xdg"
VNC_PORT = 5961

STEP = 8            # fb 采样步长（px）
PIXEL_TOL = 12      # 单通道差 > 12 才算"变了"
W_LOG, H_LOG = 1280, 800        # EGLFS + QT_QPA_EGLFS_ROTATION=90 / vnc:size 的逻辑屏

W, H, STRIDE, BPP = 0, 0, 0, 0
TOUCH = None
TOUCH_NODE = ""
PANEL_RECT = None
TAP_DIRECTION = "ccw"      # 逻辑 -> 竖屏 fb 的映射方向（实测出来的，见 README/结论）


def say(msg):
    print(msg, flush=True)


def systemctl(*args):
    # ⚠ 必须有 timeout：xrandr-startup 那种 oneshot 会卡在 ExecStartPre（见文件头），
    #   `systemctl restart` 就会一直等 —— 实测把整个测试钩死在收尾那一步。
    try:
        return subprocess.run(["systemctl", *args], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=60).returncode
    except subprocess.TimeoutExpired:
        say("    !! systemctl %s 超时 60s（单元卡住了？继续往下走）" % " ".join(args))
        return 124


def out(cmd, timeout=20):
    try:
        return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              universal_newlines=True, timeout=timeout).stdout.strip()
    except subprocess.TimeoutExpired:
        say("    !! 命令超时 %ds：%s" % (timeout, " ".join(cmd)))
        return ""


def x_ready():
    """X 真能连上吗。⚠ 探测必须带 `timeout`：DRM 被卡住时 `xrandr` **不报错也不返回**
    （板端实测躺了 2 分钟+），所以这里既给 xrandr 套 timeout，也给整个调用套一层。"""
    try:
        res = subprocess.run(["timeout", "3", "xrandr", "--query"],
                             env=dict(os.environ, DISPLAY=":0"),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
        return res.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def heal_stale_x_lock():
    """Xorg 被 SIGKILL/异常退出会留下陈旧的 /tmp/.X0-lock，slim 就再也起不来
    （实测报 `Server is already active for display 0`）。没有 Xorg 进程时先清掉。"""
    if (out(["pgrep", "-c", "Xorg"]) or "0") != "0":
        return
    if not os.path.exists("/tmp/.X0-lock"):
        return
    say("    发现陈旧 /tmp/.X0-lock（当前没有 Xorg 进程）-> 清掉再起 slim")
    for path in ("/tmp/.X0-lock", "/tmp/.X11-unix/X0"):
        try:
            os.unlink(path)
        except OSError:
            pass


# --------------------------------------------------------------------- fb ---
def fb_geom():
    with open("/sys/class/graphics/fb0/virtual_size", encoding="utf-8") as handle:
        w, h = [int(v) for v in handle.read().strip().split(",")]
    stride = int(open("/sys/class/graphics/fb0/stride", encoding="utf-8").read().strip())
    bpp = int(open("/sys/class/graphics/fb0/bits_per_pixel", encoding="utf-8").read().strip())
    return w, h, stride, bpp


def fb_raw():
    with open(FB, "rb") as handle:
        return handle.read(STRIDE * H)


def sample_grid(raw):
    rows = []
    for y in range(0, H, STEP):
        base = y * STRIDE
        rows.append([raw[base + x * 4:base + x * 4 + 3] for x in range(0, W, STEP)])
    return rows


def grid_diff(a, b):
    nrows, ncols = len(a), len(a[0])
    changed = 0
    pts = []
    for i in range(nrows):
        ra, rb = a[i], b[i]
        for j in range(ncols):
            pa, pb = ra[j], rb[j]
            if (abs(pa[0] - pb[0]) > PIXEL_TOL or abs(pa[1] - pb[1]) > PIXEL_TOL
                    or abs(pa[2] - pb[2]) > PIXEL_TOL):
                changed += 1
                pts.append((j, i))
    bbox = None
    if pts:
        bbox = (min(p[0] for p in pts), min(p[1] for p in pts),
                max(p[0] for p in pts), max(p[1] for p in pts))
    return changed, nrows * ncols, bbox


def row_edges(grid, y0, y1):
    """[y0,y1) 采样行里的强边界数：键盘那种"一堆键框 + 字"会明显多。"""
    edges = 0
    for i in range(y0, min(y1, len(grid))):
        row = grid[i]
        for j in range(len(row) - 1):
            p, q = row[j], row[j + 1]
            if (abs(p[0] - q[0]) + abs(p[1] - q[1]) + abs(p[2] - q[2])) > 90:
                edges += 1
    return edges


def save_png(raw, path, timeout=60):
    try:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgra",
                        "-s", "%dx%d" % (W, H), "-i", "-", path],
                       input=raw, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        say("    !! ffmpeg 存 PNG 失败/超时：%s" % exc)
        return False
    return os.path.exists(path)


def png_rgb(path, timeout=90):
    try:
        res = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "rawvideo",
                              "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        say("    !! ffmpeg 解 PNG 失败/超时：%s" % exc)
        return b""
    return res.stdout


def png_size(path, timeout=30):
    try:
        res = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v",
                              "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
                             stdout=subprocess.PIPE, universal_newlines=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return 0, 0
    try:
        w, h = res.stdout.strip().split(",")
        return int(w), int(h)
    except ValueError:
        return 0, 0


# ----------------------------------------------------------------- uinput ---
UINPUT = "/dev/uinput"
UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_ABSBIT = 0x40045564, 0x40045565, 0x40045567
UI_DEV_CREATE, UI_DEV_DESTROY = 0x5501, 0x5502
UI_ABS_SETUP, UI_DEV_SETUP = 0x401c5504, 0x405c5503
EV_SYN, EV_KEY, EV_ABS, SYN_REPORT = 0x00, 0x01, 0x03, 0
BTN_TOUCH, ABS_MT_SLOT = 0x14A, 0x2F
ABS_MT_POSITION_X, ABS_MT_POSITION_Y, ABS_MT_TRACKING_ID = 0x35, 0x36, 0x39


class VirtualTouch:
    """uinput 虚拟触摸屏（MT-B）。EGLFS/linuxfb 都靠 libinput 自动认设备
    （实测日志：`libinput: event6 - t151b-uitouch: is tagged by udev as: Touchscreen`）。"""

    def __init__(self, name, max_x, max_y):
        self.tid = 1
        self.fd = os.open(UINPUT, os.O_WRONLY)
        for ev in (EV_SYN, EV_KEY, EV_ABS):
            fcntl.ioctl(self.fd, UI_SET_EVBIT, ev)
        fcntl.ioctl(self.fd, UI_SET_KEYBIT, BTN_TOUCH)
        for code, mx in ((ABS_MT_SLOT, 9), (ABS_MT_TRACKING_ID, 65535),
                         (ABS_MT_POSITION_X, max_x), (ABS_MT_POSITION_Y, max_y)):
            fcntl.ioctl(self.fd, UI_SET_ABSBIT, code)
            fcntl.ioctl(self.fd, UI_ABS_SETUP, struct.pack("<H2x6i", code, 0, 0, mx, 0, 0, 0))
        setup = (struct.pack("<4H", 0x03, 0x1234, 0x5678, 1)
                 + name.encode()[:79].ljust(80, b"\0") + struct.pack("<I", 0))
        fcntl.ioctl(self.fd, UI_DEV_SETUP, setup)
        fcntl.ioctl(self.fd, UI_DEV_CREATE)
        time.sleep(0.8)

    def _emit(self, etype, code, value):
        os.write(self.fd, struct.pack("<qqHHi", 0, 0, etype, code, value))

    def tap(self, x, y):
        self._emit(EV_ABS, ABS_MT_SLOT, 0)
        self._emit(EV_ABS, ABS_MT_TRACKING_ID, self.tid)
        self.tid += 1
        self._emit(EV_ABS, ABS_MT_POSITION_X, x)
        self._emit(EV_ABS, ABS_MT_POSITION_Y, y)
        self._emit(EV_KEY, BTN_TOUCH, 1)
        self._emit(EV_SYN, SYN_REPORT, 0)
        time.sleep(0.05)
        self._emit(EV_ABS, ABS_MT_TRACKING_ID, -1)
        self._emit(EV_KEY, BTN_TOUCH, 0)
        self._emit(EV_SYN, SYN_REPORT, 0)

    def close(self):
        for fn in (lambda: fcntl.ioctl(self.fd, UI_DEV_DESTROY), lambda: os.close(self.fd)):
            try:
                fn()
            except OSError:
                pass


def find_event_node(name):
    for entry in sorted(os.listdir("/sys/class/input")):
        if not entry.startswith("event"):
            continue
        try:
            with open("/sys/class/input/%s/device/name" % entry, encoding="utf-8") as handle:
                if handle.read().strip() == name:
                    return "/dev/input/" + entry
        except OSError:
            continue
    return None


# --------------------------------------------------------------- 假 Agent ---
def start_fake_agent(mode="STUDY"):
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
        status = {"topic": "status", "data": {"mode": mode, "connected": True},
                  "timestamp": time.time()}
        try:
            conn.sendall((json.dumps(status, ensure_ascii=False) + "\n").encode("utf-8"))
            while True:
                time.sleep(1)
                conn.sendall((json.dumps(status) + "\n").encode("utf-8"))
        except OSError:
            pass

    threading.Thread(target=serve, daemon=True).start()
    return server


# ------------------------------------------------------------------ 跑 GUI ---
def run_gui(tag, log_path, platform, extra_args, events, timeout=120, kill_after=None):
    env = dict(os.environ)
    env.update({
        "QT_QPA_PLATFORM": platform,
        "QT_QUICK_BACKEND": "software",
        "QT_IM_MODULE": "qtvirtualkeyboard",
        "QT_LOGGING_RULES": "qt.virtualkeyboard=true;qt.qpa.input=true",
        "XDG_RUNTIME_DIR": RUNTIME_DIR,
        "HOME": "/root",
    })
    if platform.startswith("eglfs"):
        env["QT_QPA_EGLFS_INTEGRATION"] = "eglfs_kms"
        env["QT_QPA_EGLFS_ROTATION"] = "90"
    collected = {"shots": {}, "tag": tag}
    with open(log_path, "wb") as handle:
        proc = subprocess.Popen(
            [GUI, "--config", CFG, "--socket", FAKE_SOCK, "--page", "home"] + extra_args,
            cwd=REPO, stdout=handle, stderr=subprocess.STDOUT, env=env)
        t0 = time.time()
        try:
            for at, kind, arg in sorted(events, key=lambda e: e[0]):
                wait = t0 + at - time.time()
                if wait > 0:
                    time.sleep(wait)
                stamp = "%.1fs" % (time.time() - t0)
                if kind == "snap":
                    collected["shots"][arg] = fb_raw()
                    say("    [%s] t=%s 取 fb 快照 %s" % (tag, stamp, arg))
                else:
                    say("    [%s] t=%s %s" % (tag, stamp, getattr(arg, "__name__", "call")))
                    arg(collected)
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        if kill_after is not None and proc.poll() is None:
            proc.terminate()
            time.sleep(1)
    collected["rc"] = proc.returncode
    say("    [%s] GUI 退出码=%s" % (tag, proc.returncode))
    return collected


def log_lines(path):
    return open(path, encoding="utf-8", errors="replace").read().splitlines()


def field(lines, prefix, index):
    for ln in lines:
        if ln.startswith(prefix):
            parts = ln.split("\t")[1:]
            if index < len(parts):
                return parts[index]
    return None


def all_of(lines, prefix):
    return [ln.split("\t")[1:] for ln in lines if ln.startswith(prefix)]


def restore_desktop():
    say("[收尾] 复原桌面（slim -> xrandr-startup -> agent-gui）")
    if TOUCH is not None:
        TOUCH.close()
    if os.path.exists(FAKE_SOCK):
        os.unlink(FAKE_SOCK)
    heal_stale_x_lock()                         # 陈旧 X 锁会让 slim 起不来
    systemctl("start", "slim.service")
    for _ in range(20):                         # 等 X 真能连上再转屏（每次都带 timeout）
        time.sleep(1)
        if x_ready():
            break
    if x_ready():
        systemctl("restart", "xrandr-startup.service")
        time.sleep(3)
    else:
        say("    !! X 没就绪：跳过转屏（xrandr-startup 自己的 ExecStartPre 已带 timeout，"
            "不会再卡住 boot）")
    systemctl("restart", "agent-gui.service")
    time.sleep(10)
    rot = out(["bash", "-lc",
               "DISPLAY=:0 timeout 5 xrandr --query 2>/dev/null"
               " | grep -oE 'DSI-1 connected [0-9x+]+ [a-z]+'"])
    say("[收尾] slim=%s agent=%s gui=%s xrandr=%s"
        % (out(["systemctl", "is-active", "slim.service"]),
           out(["systemctl", "is-active", "agent.service"]),
           out(["systemctl", "is-active", "agent-gui.service"]), rot or "(xrandr 没答复/超时)"))


def tap_panel(_collected):
    """在**逻辑屏下半部**扫点（虚拟键盘就在那里）。
    Qt 的 DesktopInputPanel = "整屏（半透明）窗口 + 键盘贴窗口底部"，所以窗口几何必然是
    整屏 —— 位置不能靠窗口几何，只能靠像素（P1/P4）。"""
    px, py, pw, ph = PANEL_RECT if PANEL_RECT else (0, 0, W_LOG, H_LOG)
    xs_log = [px + int(pw * f) for f in (0.20, 0.40, 0.60, 0.80)]
    ys_log = [py + int(ph * f) for f in (0.62, 0.70, 0.78, 0.86, 0.94)]
    points = []
    for ly in ys_log:
        for lx in xs_log:
            if TAP_DIRECTION == "cw":
                points.append((W - 1 - ly, lx))
            else:
                points.append((ly, H - 1 - lx))
    say("      面板窗口=%d,%d %dx%d  方向=%s  扫点 %d 下（逻辑 y=%s）"
        % (px, py, pw, ph, TAP_DIRECTION, len(points), ys_log))
    for x, y in points:
        TOUCH.tap(x, y)
        time.sleep(0.12)
    _collected["taps"] = points


def capture_vnc(path):
    """从 Qt 的 vnc 平台抓一帧合成图。
    为什么不用 `gst-launch-1.0 rfbsrc`：板端那个 rfbsrc 一连就 std::runtime_error 崩掉
    （实测 rc=-6），所以这里自己写一个最小 RFB 客户端（按服务端版本协商，只要 Raw 编码）。"""
    grab = RfbGrab("127.0.0.1", VNC_PORT, timeout=20)
    try:
        w, h, rgb, pixel = grab.read_full()
    finally:
        grab.close()
    save_rgb_png(rgb, w, h, path)
    return 0, "%dx%d bpp=%d 编码 Raw -> %s" % (w, h, pixel, path)


class RfbGrab:
    """最小 RFB（VNC）客户端：握手 -> SetPixelFormat(32bpp) -> Raw -> 整屏一帧。"""

    def __init__(self, host, port, timeout=20):
        self.sock = socket.create_connection((host, port), timeout=timeout)

    def _recv(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise OSError("RFB 连接被关掉（只收到 %d/%d 字节）" % (len(buf), n))
            buf += chunk
        return buf

    def _u8(self):
        return self._recv(1)[0]

    def _u16(self):
        return struct.unpack(">H", self._recv(2))[0]

    def _u32(self):
        return struct.unpack(">I", self._recv(4))[0]

    def read_full(self):
        # ⚠ 先读**服务端**的版本：Qt 的 vnc 平台只会说 `RFB 003.003`（实测），而 3.3 的握手
        # 没有"安全类型列表"—— 直接给一个 u32 安全类型，也没有 SecurityResult。
        # 之前按 3.8 写（先发自己的版本、再读列表）会被服务端当场断链（只收到 0/1 字节）。
        server_version = self._recv(12).decode("ascii", "replace").strip()
        matched = re.match(r"RFB (\d{3})\.(\d{3})", server_version)
        if not matched:
            raise OSError("不是 RFB 服务端：%r" % server_version)
        major, minor = int(matched.group(1)), int(matched.group(2))
        if (major, minor) > (3, 8):
            major, minor = 3, 8
        self.sock.sendall(("RFB %03d.%03d\n" % (major, minor)).encode("ascii"))
        if (major, minor) >= (3, 7):
            count = self._u8()
            if count == 0:
                raise OSError("服务端拒绝：%s"
                              % self._recv(self._u32()).decode("utf-8", "replace"))
            types = [self._u8() for _ in range(count)]
            if 1 not in types:
                raise OSError("服务端不要 None 认证，只给 %s" % types)
            self.sock.sendall(b"\x01")                   # security: None
            if self._u32() != 0:
                raise OSError("SecurityResult 不为 0")
        else:
            sec = self._u32()
            if sec != 1:
                raise OSError("3.3 的安全类型 %d 不是 None" % sec)
        self.sock.sendall(b"\x01")                       # ClientInit: shared
        width, height = self._u16(), self._u16()
        pixel = self._recv(16)
        self._recv(self._u32())                          # 服务器名字
        (bits, depth, big_endian, _true, _rmax, _gmax, _bmax,
         rshift, gshift, bshift) = struct.unpack(">BBBBHHHBBB", pixel[:13])
        say("      RFB %03d.%03d 屏=%dx%d；服务端像素格式 bits=%d depth=%d 大端=%d shifts=%d/%d/%d"
            % (major, minor, width, height, bits, depth, big_endian, rshift, gshift, bshift))
        # SetPixelFormat: 32bpp / depth 24 / true colour / R<<16 G<<8 B<<0（小端就是 BGRA）
        fmt = struct.pack(">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
        self.sock.sendall(b"\x00\x00\x00\x00" + fmt)
        self.sock.sendall(struct.pack(">BBH", 2, 0, 1) + struct.pack(">i", 0))   # SetEncodings: Raw
        self.sock.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, width, height))    # 整屏请求
        while True:
            kind = self._u8()
            if kind == 0:                                # FramebufferUpdate
                self._u8()
                rects = self._u16()
                frame = bytearray(width * height * 4)
                for _ in range(rects):
                    x, y, w, h = self._u16(), self._u16(), self._u16(), self._u16()
                    enc = struct.unpack(">i", self._recv(4))[0]
                    if enc != 0:                         # DesktopSize 之类的伪编码：跳过
                        continue
                    data = self._recv(w * h * 4)
                    for row in range(h):
                        src = row * w * 4
                        dst = ((y + row) * width + x) * 4
                        frame[dst:dst + w * 4] = data[src:src + w * 4]
                if (bits, big_endian, rshift, gshift, bshift) == (32, 0, 16, 8, 0):
                    # 线上 = 每像素 B,G,R,X（小端 0x00RRGGBB）-> 切片快路径
                    out = bytearray(width * height * 3)
                    out[0::3] = frame[2::4]
                    out[1::3] = frame[1::4]
                    out[2::3] = frame[0::4]
                    rgb = bytes(out)
                else:                                     # 别的格式：逐像素（慢但不会错）
                    order = "big" if big_endian else "little"
                    out = bytearray(width * height * 3)
                    for i in range(width * height):
                        value = int.from_bytes(frame[i * 4:i * 4 + 4], order)
                        out[i * 3] = (value >> rshift) & 0xFF
                        out[i * 3 + 1] = (value >> gshift) & 0xFF
                        out[i * 3 + 2] = (value >> bshift) & 0xFF
                    rgb = bytes(out)
                return width, height, rgb, bits
            if kind == 1:                                # SetColourMapEntries
                self._u8()
                _first, n = self._u16(), self._u16()
                self._recv(n * 6)
            elif kind == 2:                              # Bell
                pass
            elif kind == 3:                              # ServerCutText
                self._recv(3)
                self._recv(self._u32())
            else:
                raise OSError("不认识的 RFB 消息类型 %d" % kind)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def main():
    global W, H, STRIDE, BPP, TOUCH, TOUCH_NODE, PANEL_RECT, TAP_DIRECTION
    W, H, STRIDE, BPP = fb_geom()
    say("== T15-1 1b：无 X 下的软键盘取证")
    say("[0] fb: %dx%d stride=%d bpp=%d（竖屏）；逻辑屏 %dx%d"
        % (W, H, STRIDE, BPP, W_LOG, H_LOG))
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    os.chmod(RUNTIME_DIR, 0o700)

    TOUCH = VirtualTouch(TOUCH_NAME, W - 1, H - 1)
    TOUCH_NODE = find_event_node(TOUCH_NAME) or ""
    say("[0] uinput 虚拟触摸屏 %s -> %s" % (TOUCH_NAME, TOUCH_NODE or "（没找到！）"))
    server = start_fake_agent()
    say("[0] 假 Agent: %s（STUDY：非 GAME 才有对话输入框；它写不了盘）" % FAKE_SOCK)
    md5_before = (out(["md5sum", CFG]).split() or ["?"])[0]
    say("[0] config.yaml md5（前）=%s" % md5_before)

    abort = 0
    try:
        say("[0] 停桌面让出 DRM/fb")
        systemctl("stop", "agent-gui.service")
        systemctl("stop", "slim.service")
        time.sleep(6)
        say("    Xorg 进程数=%s" % (out(["pgrep", "-c", "Xorg"]) or "0"))

        # ---------------- P1：linuxfb 像素证据 ----------------
        say("[P1] linuxfb + 焦点：fb 快照「点输入框前 vs 后」-> 键盘真画出来了吗")
        p1 = run_gui("P1", LOG_P1, "linuxfb",
                     ["--focus-input-demo", "--dump-input", "--screenshot-delay", "9000"],
                     [(1.0, "snap", "pre"), (3.5, "snap", "post")])
        pre, post = sample_grid(p1["shots"]["pre"]), sample_grid(p1["shots"]["post"])
        changed, total, bbox = grid_diff(pre, post)
        say("[P1] 变化采样点 %d/%d = %.2f%%；bbox(采样)=%s"
            % (changed, total, 100.0 * changed / total, bbox))
        band0 = int(H * 0.72 / STEP)          # 只看 fb 底部 28%：面板窗口里键盘所在的那条
        e_pre, e_post = row_edges(pre, band0, len(pre)), row_edges(post, band0, len(post))
        say("[P1] fb 底部 y>=%d 的强边界：点焦点前 %d 条 -> 点焦点后 %d 条（%s）"
            % (band0 * STEP, e_pre, e_post,
               "OK：多出几百条 = 真画了键" if e_post > e_pre + 300 else "!! 没多出内容"))
        p1_ink_ok = e_post > e_pre + 300
        top_uni = len(set(bytes(p) for row in post[:band0] for p in row))
        say("[P1] 键盘上方（面板窗口的底）颜色只有 %d 种 -> %s"
            % (top_uni, "linuxfb 没 alpha 合成，半透明面板在此变成不透明白底（已知，"
                        "所以产品必须用 EGLFS/vnc 这种有合成的平台）" if top_uni <= 3 else "有内容"))
        save_png(p1["shots"]["pre"], "/tmp/t151b_P1_pre.png")
        save_png(p1["shots"]["post"], "/tmp/t151b_P1_post.png")
        say("[P1] 存帧 /tmp/t151b_P1_pre.png /tmp/t151b_P1_post.png")
        if p1["rc"] == -11:
            say("[P1] 注：linuxfb 下退出时 SIGSEGV（崩溃日志最后是 hideInputPanel），"
                "发生在 dump 之后，不影响上面的证据")
        p1_lines = log_lines(LOG_P1)
        for ln in p1_lines:
            if "InputPanel.qml" in ln and "not installed" in ln:
                say("[P1] !! QML 报错: " + ln.strip()[:170])

        # ---------------- P2：EGLFS（产品形态） ----------------
        say("[P2] EGLFS + 旋转 90（=产品形态）：屏 1280×800、键盘窗口可见、IM_VISIBLE=1")
        run_gui("P2", LOG_P2, "eglfs",
                     ["--focus-input-demo", "--dump-input", "--screenshot-delay", "9000"], [])
        p2_lines = log_lines(LOG_P2)
        nox = [ln for ln in p2_lines if "[ui] 无 X" in ln]
        old_path = [ln for ln in p2_lines if ("没找到 onboard 进程" in ln or "软键盘不可用" in ln
                                             or "软键盘弹出失败" in ln)]
        qml_err = [ln for ln in p2_lines if "not installed" in ln and "module" in ln]
        say("[P2] `[ui] 无 X（…）` 日志 %d 条：" % len(nox))
        for ln in nox[:3]:
            say("      " + ln.strip()[:170])
        say("[P2] 老路（onboard）报错 %d 条 %s；QML 缺模块 %d 条 %s"
            % (len(old_path), "OK" if not old_path else "!!", len(qml_err),
               "OK" if not qml_err else "!!"))
        for ln in old_path[:3] + qml_err[:3]:
            say("      !! " + ln.strip()[:170])
        for ln in p2_lines:
            if ln.startswith(("SCREEN", "IM_VISIBLE", "IM_RECT", "WINDOW", "INPUT_TEXT",
                              "INPUT_LEN", "INPUT_RECT", "FOCUS_WIDGET")):
                say("      " + ln.strip()[:170])
        for ln in p2_lines:
            if "repositionView" in ln:
                say("      " + ln.strip()[:170])
                break
        scr = field(p2_lines, "SCREEN\t", 1)
        im_vis = field(p2_lines, "IM_VISIBLE\t", 0)
        input_rect = None
        rect_text = field(p2_lines, "INPUT_RECT\t", 0)
        if rect_text:
            m = re.match(r"(-?\d+),(-?\d+) (\d+)x(\d+)", rect_text)
            if m:
                input_rect = tuple(int(v) for v in m.groups())
        # Qt 自己报的键盘矩形（EGLFS = 产品形态下量的，最权威）：0,400 1280x400
        kb_top = None
        im_rect_text = field(p2_lines, "IM_RECT\t", 0)
        if im_rect_text:
            m = re.match(r"(-?\d+),(-?\d+) (\d+)x(\d+)", im_rect_text)
            if m:
                kb_top = int(m.group(2))
        main_win = None
        panel_ok = False
        for w in all_of(p2_lines, "WINDOW\t"):
            if len(w) < 3:
                continue
            if "InputView" in w[0]:
                panel_ok = w[2] == "visible=1"
                m = re.match(r"(\d+),(\d+) (\d+)x(\d+)", w[1])
                if m:
                    PANEL_RECT = tuple(int(v) for v in m.groups())
            if "QWidgetWindow" in w[0]:
                main_win = w[1]
        scr_ok = bool(scr and "1280x800" in scr)
        main_ok = bool(main_win and "1280x800" in main_win)
        say("[P2] 逻辑屏=%s %s；主窗口=%s %s；键盘窗口可见=%s"
            % (scr, "OK" if scr_ok else "!!", main_win, "OK（没被改小）" if main_ok else "!!",
               "是" if panel_ok else "否"))
        # 「不挡布局」的判据：键盘上沿 vs 输入框下沿（都在 EGLFS 产品形态下量的）
        kb_cover_ok = False
        if input_rect and kb_top is not None:
            ix, iy, iw, ih = input_rect
            kb_cover_ok = iy + ih <= kb_top
            say("[P2] 输入框 y=%d..%d（x=%d..%d）；键盘上沿 y=%d -> %s"
                % (iy, iy + ih, ix, ix + iw, kb_top,
                   "OK：输入框整个在键盘上方（不挡）" if kb_cover_ok
                   else "!! 键盘压住输入框了（不挡布局没做到）"))
        else:
            say("[P2] !! 缺 INPUT_RECT / IM_RECT，判不了'挡没挡住'")

        # ---------------- P3：uinput 打字 ----------------
        typed_text, typed_max, typed_dir = "", 0, ""
        for attempt in ("ccw", "cw"):
            TAP_DIRECTION = attempt
            say("[P3] EGLFS + uinput 点键盘下半部（映射方向试 %s）：能不能打进去字" % attempt)
            p3 = run_gui("P3-%s" % attempt, LOG_P3, "eglfs",
                         ["--focus-input-demo", "--dump-input", "--screenshot-delay", "11000"],
                         [(4.0, "call", tap_panel)])
            p3_lines = log_lines(LOG_P3)
            samples = [ln for ln in p3_lines if ln.startswith("INPUT_SAMPLE")]
            say("[P3] 退出码=%s；逐秒采样：" % p3["rc"])
            for ln in samples:
                say("      " + ln.strip()[:170])
            for ln in p3_lines:
                if ln.startswith(("INPUT_TEXT", "INPUT_LEN", "FOCUS_WIDGET", "IM_VISIBLE")):
                    say("      " + ln.strip()[:170])
            for ln in samples:                     # 取峰值：后面可能点到退格/回车
                parts = ln.split("\t")
                if len(parts) >= 3 and len(parts[2]) > typed_max:
                    typed_max, typed_text, typed_dir = len(parts[2]), parts[2], attempt
            if typed_max:
                say("[P3] 方向 %s 打进去了（峰值 %d 字）：%s" % (attempt, typed_max, typed_text))
                break
            say("[P3] 方向 %s 没打进去字，换方向重试" % attempt)

        # ---------------- P5：收起键盘后布局要复原 ----------------
        say("[P5] EGLFS 不给输入框焦点（键盘不该弹）：布局应当回到原样")
        run_gui("P5", "/tmp/t151b_P5_nofocus.log", "eglfs",
                     ["--dump-input", "--screenshot-delay", "5000"], [])
        p5_lines = log_lines("/tmp/t151b_P5_nofocus.log")
        p5_im = field(p5_lines, "IM_VISIBLE\t", 0)
        p5_rect = None
        t = field(p5_lines, "INPUT_RECT\t", 0)
        if t:
            m = re.match(r"(-?\d+),(-?\d+) (\d+)x(\d+)", t)
            if m:
                p5_rect = tuple(int(v) for v in m.groups())
        restore_ok = False
        if p5_rect and kb_top is not None:
            restore_ok = p5_rect[1] + p5_rect[3] > kb_top
            say("[P5] IM_VISIBLE=%s；输入框 y=%d..%d -> %s"
                % (p5_im, p5_rect[1], p5_rect[1] + p5_rect[3],
                   "OK：没键盘时回到下半屏（让位是临时的）" if restore_ok
                   else "!! 没键盘时输入框还悬在上面：让位没复原"))
        else:
            say("[P5] !! 没拿到 INPUT_RECT")

        # ---------------- P4：vnc 合成图 ----------------
        say("[P4] vnc 平台抓一帧合成图：键盘贴逻辑屏底部、上面的 app 界面还在、不挡输入框")
        p4_ok = False
        # ⚠ 这里别重置 kb_cover_ok：它是 P2 用 EGLFS（产品形态）的 INPUT_RECT/IM_RECT
        #   算出来的判据，P4 只是拿键盘条带位置做交叉验证。
        band_top, band_h, top_colors = 0, 0, 0
        try:
            proc = subprocess.Popen(
                [GUI, "--config", CFG, "--socket", FAKE_SOCK, "--page", "home",
                 "--focus-input-demo", "--dump-input", "--screenshot-delay", "14000"],
                cwd=REPO, stdout=open(LOG_P4, "wb"), stderr=subprocess.STDOUT,
                env=dict(os.environ, QT_QPA_PLATFORM="vnc:size=%dx%d:depth=32:port=%d"
                         % (W_LOG, H_LOG, VNC_PORT), QT_QUICK_BACKEND="software",
                         QT_IM_MODULE="qtvirtualkeyboard", XDG_RUNTIME_DIR=RUNTIME_DIR,
                         HOME="/root",
                         QT_LOGGING_RULES="qt.virtualkeyboard=true"))
            time.sleep(6)
            rc, msg = capture_vnc(PNG_P4)
            say("[P4] RFB 抓帧 rc=%s %s" % (rc, msg))
            proc.wait(timeout=40)
            say("[P4] vnc GUI 退出码=%s" % proc.returncode)
            if os.path.exists(PNG_P4):
                pw, ph = png_size(PNG_P4)
                rgb = png_rgb(PNG_P4)
                say("[P4] 抓到的帧 %dx%d（%d 字节 RGB）" % (pw, ph, len(rgb)))
                if pw == W_LOG and len(rgb) >= pw * ph * 3:
                    # 逐行"强边界"剖面：键盘那几行密密麻麻，app 那几行稀疏
                    profile = []
                    for y in range(0, ph, 4):
                        base = y * pw * 3
                        edges = 0
                        for x in range(0, pw - 4, 4):
                            o = base + x * 3
                            if (abs(rgb[o] - rgb[o + 4]) + abs(rgb[o + 1] - rgb[o + 5])
                                    + abs(rgb[o + 2] - rgb[o + 6])) > 90:
                                edges += 1
                        profile.append((y, edges))
                    peak = max(e for _, e in profile)
                    hot = [y for y, e in profile if e > peak * 0.5]
                    if hot:
                        band_top, band_h = hot[0], hot[-1] - hot[0]
                    top_colors = len(set(rgb[(y * pw + x) * 3:(y * pw + x) * 3 + 3]
                                         for y in range(0, int(ph * 0.4), 3)
                                         for x in range(0, pw, 3)))
                    say("[P4] 高边界带（=键盘）y=%d..%d（高 %d / 屏高 %d = %.0f%%）"
                        % (band_top, band_top + band_h, band_h, ph,
                           100.0 * band_h / max(ph, 1)))
                    say("[P4] 上半屏颜色 %d 种（%s）" % (top_colors,
                        "app 界面还在" if top_colors > 20 else
                        "只有 1 种：vnc/linuxfb 这类**栅格平台没有 alpha 合成**，"
                        "半透明面板在这里变成不透明白底 —— 所以合成后的画面只能靠"
                        "EGLFS（它的窗口合成器带 alpha 混合），这里只用它量键盘条带位置"))
                    # 键盘条带位置与 Qt 自报的键盘矩形对一下（两套独立证据）
                    if kb_top is not None:
                        say("[P4] 与 Qt 自报的键盘上沿 y=%d 对比：%s"
                            % (kb_top, "一致（差 %d px）" % abs(band_top - kb_top)
                               if abs(band_top - kb_top) <= 80 else "!! 差得远，存疑"))
                    p4_ok = band_top > ph * 0.45 and abs((kb_top or band_top) - band_top) <= 80
                    save_rgb_png(rgb, pw, ph, "/tmp/t151b_P4_vnc_raw.png")
        except (OSError, subprocess.SubprocessError) as exc:
            say("[P4] !! 抓帧失败：%s" % exc)

        ok = bool(nox) and not old_path and not qml_err and scr_ok and main_ok and panel_ok \
            and im_vis == "1" and p1_ink_ok and typed_max > 0 and p4_ok and kb_cover_ok \
            and restore_ok
        say("[结论] 无 X 软键盘：新路径日志=%s / onboard 老路报错=%d / QML 缺模块=%d / "
            "屏 1280x800=%s / 主窗口没变=%s / 键盘窗口可见=%s / IM_VISIBLE=%s / "
            "键盘画出来=%s / 打进去 %d 字(方向 %s) / 键盘条带位置对得上=%s / "
            "没挡输入框=%s / 收键盘后复原=%s -> %s"
            % ("有" if nox else "无", len(old_path), len(qml_err), scr_ok, main_ok,
               panel_ok, im_vis, p1_ink_ok, typed_max, typed_dir or "-", p4_ok,
               kb_cover_ok, restore_ok, "PASS" if ok else "FAIL"))
        if not ok:
            abort = 1
    finally:
        server.close()
        restore_desktop()
        md5_after = (out(["md5sum", CFG]).split() or ["?"])[0]
        say("[收尾] config.yaml md5（后）=%s %s"
            % (md5_after, "OK（没被改）" if md5_after == md5_before else "!! 被改了"))
        if md5_after != md5_before:
            abort = 1
    return abort


def save_rgb_png(rgb, w, h, path):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
                    "-s", "%dx%d" % (w, h), "-i", "-", path],
                   input=rgb, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return os.path.exists(path)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        restore_desktop()
        sys.exit(130)
