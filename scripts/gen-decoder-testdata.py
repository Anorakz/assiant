#!/usr/bin/env python3
"""
scripts/gen-decoder-testdata.py — 生成解码器冒烟测试的夹具 (纯色码流 + 期望值)

跑法 (WSL / 有 ffmpeg 的 Linux):
    python3 scripts/gen-decoder-testdata.py

产物 (tests/data/):
    color_1280x720_8bit.h265    生产分辨率, 和 moonlight 实流同一条路
    color_1280x720_8bit.h264    同一画面, H.264 —— 验"按协商格式初始化"
    color_1272x720_8bit.h265    宽度**故意不对齐** (1272 不是 16/64 倍数),
                                MPP 的 hor_stride 会是 1280 > 1272 → 逼出
                                "收拢 stride" 那段代码
    <每个码流>.expect           逐帧期望的 Y/U/V

为什么要有这个脚本 (而不是把码流当二进制丢进仓库就不管):
    tests/data/ 里的 .h265/.h264 是**夹具**, 谁都得能重新生成、能看懂里面是什么。
    所以夹具和生成它的脚本一起进仓库。

为什么直接写 YUV 而不是 color=red 那种 RGB 源
-------------------------------------------------------------------------------
    用 RGB 源的话, "期望的 YUV" 取决于 swscale 选了哪个矩阵 (bt601/bt709) 和
    范围 (limited/full), 还得靠"解出来测一下"反推 —— 那样测的其实是 FFmpeg 的
    颜色转换, 不是我们的解码器。这里直接写 YUV 平面, 期望值是**构造出来的**,
    测的就纯粹是"MPP 有没有把这块 YUV 正确解出来"。
    (写进去的是标准的 limited-range BT.601 纯红/绿/蓝, 方便肉眼认。)

为什么是这几个颜色
-------------------------------------------------------------------------------
    帧序: 红 绿 蓝 红 绿 蓝。三个颜色在 Y/U/V 三个分量上**都**拉得很开, 所以:
      · U/V 平面搞反 (NV12 vs NV21)  → 立刻可见
      · 平面顺序错 / stride 收拢错    → 立刻可见
      · 只用一个颜色的话, 上面两种错都看不出来
    每帧是纯色, 所以每个平面内部必须**所有像素完全相等** —— 行 stride 少收/多收
    会把 padding 的垃圾读进来, 表现为"平面内部不一致", 同样一眼可见。
"""

import os
import subprocess
import sys

# ---------------------------------------------------------------------------
#  夹具定义
# ---------------------------------------------------------------------------
#: (名字, (Y, U, V)) —— 标准 limited-range BT.601 的纯色
RED = ("RED", (81, 90, 240))
GREEN = ("GREEN", (145, 54, 34))
BLUE = ("BLUE", (41, 240, 102))
PALETTE = {c[0]: c[1] for c in (RED, GREEN, BLUE)}

#: 帧序 (两个循环, 顺便验解码顺序)
SEQUENCE = ["RED", "GREEN", "BLUE", "RED", "GREEN", "BLUE"]

#: 要生成的码流: (文件名, 编码器, 容器/格式, 宽, 高)
STREAMS = [
    ("color_1280x720_8bit.h265", "libx265", "hevc", 1280, 720),
    ("color_1272x720_8bit.h265", "libx265", "hevc", 1272, 720),
    ("color_1280x720_8bit.h264", "libx264", "h264", 1280, 720),
]

#: 有损编码对纯色块的 DC 重建通常精确, 允许 ±TOL 的偏差
TOL = 3

#: bframes=0 很关键: 解码顺序 == 显示顺序, 测试里才能断言"第 k 帧就是第 k 个颜色"
#: repeat-headers=1: 每个 IDR 都带 VPS/SPS/PPS, 板端从任意位置起解都能自洽
XPARAMS = "bframes=0:keyint=6:min-keyint=6:repeat-headers=1:log-level=error"


def ffmpeg():
    return os.environ.get("FFMPEG", "ffmpeg")


def run(cmd, **kw):
    try:
        return subprocess.run(cmd, check=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, **kw)
    except FileNotFoundError:
        sys.exit("找不到 %s; 装 ffmpeg 或用 FFMPEG=/path/to/ffmpeg 指定" % cmd[0])
    except subprocess.CalledProcessError as exc:
        sys.exit("命令失败: %s\n%s" % (" ".join(cmd), exc.stderr.decode("utf-8", "replace")))


# ---------------------------------------------------------------------------
#  1) 直接构造 raw yuv420p
# ---------------------------------------------------------------------------
def write_raw_yuv(path, width, height, sequence):
    """按 sequence 写一段纯色 yuv420p: 每帧 Y 平面全填 Y, U/V 平面各填 U/V。"""
    cw, ch = width // 2, height // 2
    with open(path, "wb") as handle:
        for name in sequence:
            y, u, v = PALETTE[name]
            handle.write(bytes([y]) * (width * height))
            handle.write(bytes([u]) * (cw * ch))
            handle.write(bytes([v]) * (cw * ch))


# ---------------------------------------------------------------------------
#  2) 编码成 Annex-B 基本流
# ---------------------------------------------------------------------------
def encode(raw_path, dst, encoder, fmt, width, height):
    run([
        ffmpeg(), "-y", "-v", "error",
        "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", "%dx%d" % (width, height),
        "-framerate", "10", "-i", raw_path,
        "-c:v", encoder, "-x265-params", XPARAMS, "-x264-params", XPARAMS,
        "-f", fmt, dst,
    ])


# ---------------------------------------------------------------------------
#  3) 用 ffmpeg 软解回来核对, 并写 .expect
# ---------------------------------------------------------------------------
def decode_to_frames(src, width, height):
    """软解成 rawvideo, 按帧切好返回 [(name_guess, (y,u,v))]。"""
    raw = run([
        ffmpeg(), "-v", "error", "-i", src,
        "-f", "rawvideo", "-pix_fmt", "yuv420p", "-",
    ]).stdout

    fsize = width * height * 3 // 2
    if not fsize or len(raw) % fsize:
        sys.exit("%s: 解出 %d 字节, 不是帧大小 %d 的整数倍" % (src, len(raw), fsize))

    frames = []
    for i in range(len(raw) // fsize):
        f = raw[i * fsize:(i + 1) * fsize]
        yplane = f[:width * height]
        uplane = f[width * height:width * height + (width // 2) * (height // 2)]
        vplane = f[width * height + (width // 2) * (height // 2):]
        for label, plane in (("Y", yplane), ("U", uplane), ("V", vplane)):
            if len(set(plane)) != 1:
                sys.exit("%s 第 %d 帧的 %s 平面不均匀 (%d 种取值) —— 夹具本身有问题"
                         % (src, i, label, len(set(plane))))
        frames.append((yplane[0], uplane[0], vplane[0]))
    return frames


def check_against_source(src, frames, sequence):
    """软解结果必须和"写进去的颜色"对得上, 否则夹具不可信。"""
    if len(frames) != len(sequence):
        sys.exit("%s: 期望 %d 帧, 实际 %d 帧" % (src, len(sequence), len(frames)))
    for i, (name, got) in enumerate(zip(sequence, frames)):
        want = PALETTE[name]
        worst = max(abs(a - b) for a, b in zip(want, got))
        if worst > TOL:
            sys.exit("%s 第 %d 帧 (%s): 期望 %s, 实测 %s, 差 %d > %d"
                     % (src, i, name, want, got, worst, TOL))


def write_expect(dst, width, height, frames):
    """第一行 "width height frames", 之后每行 "y u v" (实测值)。"""
    lines = ["%d %d %d" % (width, height, len(frames))]
    lines += ["%d %d %d" % f for f in frames]
    with open(dst, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(root, "tests", "data")
    os.makedirs(out_dir, exist_ok=True)
    raw_path = os.path.join(out_dir, "_tmp_frames.yuv")

    print("=== 生成纯色测试夹具 -> %s ===" % out_dir)
    print("    帧序: %s" % " ".join(SEQUENCE))
    try:
        for name, encoder, fmt, width, height in STREAMS:
            raw_path_w = raw_path
            write_raw_yuv(raw_path_w, width, height, SEQUENCE)

            dst = os.path.join(out_dir, name)
            encode(raw_path_w, dst, encoder, fmt, width, height)

            frames = decode_to_frames(dst, width, height)
            check_against_source(name, frames, SEQUENCE)
            write_expect(dst + ".expect", width, height, frames)

            print("  %-28s %6d bytes  %dx%d  %d 帧"
                  % (name, os.path.getsize(dst), width, height, len(frames)))
            for i, (nm, yuv) in enumerate(zip(SEQUENCE, frames)):
                print("      f%d %-6s y=%3d u=%3d v=%3d" % (i, nm, yuv[0], yuv[1], yuv[2]))
    finally:
        if os.path.exists(raw_path):
            os.unlink(raw_path)

    print()
    print("=== tests/data/ ===")
    for entry in sorted(os.listdir(out_dir)):
        print("  %-32s %7d bytes" % (entry, os.path.getsize(os.path.join(out_dir, entry))))


if __name__ == "__main__":
    main()
