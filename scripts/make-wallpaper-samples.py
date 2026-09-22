#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/make-wallpaper-samples.py — 造几张**纯色**壁纸样张（Phase 7 T3）

为什么有这个脚本
    壁纸切换接上之后要验的是"**任意分辨率/任意比例**的图都能被归一化显示"
    （GUI 的做法是"等比缩放 + 居中裁切"，见 gui/src/core/image_fit.cpp）。
    所以需要几张**尺寸差别很大**的图当输入; 它们只是测试素材, 不该入库,
    于是在板端现场生成 —— 不依赖 Pillow / ImageMagick, 只用标准库 (zlib + struct)。

    每张图 = 一个纯色底 + 一圈 3px 的对比色边框 + 左上角一个小方块。
    ⚠ 为什么不是"光秃秃一片纯色": 纯色看不出**裁切与变形**（等比填满时边框会被裁掉
      一部分, 这正是要看的）。底色仍然是纯色, 边框只用来量"有没有被拉变形 /
      有没有居中裁掉两边"。

跑法
    python3 scripts/make-wallpaper-samples.py                      # 默认 /home/kickpi/wallpapers
    python3 scripts/make-wallpaper-samples.py --dir /tmp/wallpaper-test
    python3 scripts/make-wallpaper-samples.py --list                # 只列出会造哪些, 不写文件
"""

from __future__ import annotations

import argparse
import struct
import sys
import zlib
from pathlib import Path

#: (文件名, 宽, 高, 底色, 这是给谁看的说明)
SAMPLES = [
    ("01_landscape_1280x800.png", 1280, 800, (0x2E, 0x5A, 0x88),
     "桌面/串流画面比例 (16:10) —— 与 kiosk 完全一致, 应当铺满且**不裁**"),
    ("02_portrait_800x1280.png", 800, 1280, (0x1F, 0x6F, 0x4A),
     "屏幕的原生竖屏 (10:16) —— 横向要裁掉很多, 边框左右应当被裁平"),
    ("03_wide_1920x1080.png", 1920, 1080, (0x8A, 0x4B, 0x1F),
     "16:9 横图 —— 比屏幕更宽, 上下要裁一点"),
    ("04_tall_1080x1920.png", 1080, 1920, (0x6B, 0x2F, 0x7A),
     "9:16 竖图 —— 极窄, 放大后左右裁得最狠"),
    ("05_ultrawide_2560x1080.png", 2560, 1080, (0x7A, 0x70, 0x1F),
     "21:9 超宽 —— 长边超出屏幕很多 (顺带验大图不被内存卡住)"),
    ("06_extreme_600x1200.png", 600, 1200, (0x55, 0x1F, 0x1F),
     "1:2 细长 —— 极端比例, 最容易看出变形"),
    ("07_square_1024x1024.png", 1024, 1024, (0x33, 0x33, 0x3A),
     "正方形 —— 两个方向裁得一样多, 用来对中"),
    ("08_circle_1280x1600.png", 1280, 1600, (0x1B, 0x4B, 0x3A),
     "竖图 + 正中一个**白圆** —— 这条专给「有没有被拉伸」用: "
     "等比填满时它还是正圆, 一旦被拉伸就会变成明显的椭圆"),
]

#: 边框与角标颜色（浅色, 在任何底色上都看得见）
BORDER = (0xF5, 0xF5, 0xF5)
BORDER_PX = 3
MARKER_PX = 24

#: 第 8 张那个圆的半径（源图像素）。半径要足够大, 拉伸 2 倍才一眼看得出来
CIRCLE_RADIUS = 250


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def write_png(path: Path, width: int, height: int, pixels: bytes) -> None:
    """写一张 8bit RGB 的 PNG（每行前面那个 0 是 PNG 的 filter 字节）。"""
    stride = width * 3
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        raw += pixels[y * stride:(y + 1) * stride]
    png = b"\x89PNG\r\n\x1a\n"
    png += _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += _chunk(b"IEND", b"")
    path.write_bytes(png)


def render(width: int, height: int, colour, circle: bool = False) -> bytes:
    """纯色底 + 一圈边框 + 左上角角标（circle=True 再在正中画一个白圆）。"""
    pixels = bytearray(bytes(colour) * (width * height))

    def put(x: int, y: int, rgb) -> None:
        if 0 <= x < width and 0 <= y < height:
            offset = (y * width + x) * 3
            pixels[offset:offset + 3] = bytes(rgb)

    for y in range(height):
        for x in range(width):
            if x < BORDER_PX or y < BORDER_PX or x >= width - BORDER_PX or y >= height - BORDER_PX:
                put(x, y, BORDER)
    for y in range(BORDER_PX, BORDER_PX + MARKER_PX):
        for x in range(BORDER_PX, BORDER_PX + MARKER_PX):
            put(x, y, BORDER)

    if circle:
        cx, cy = width / 2.0, height / 2.0
        outer, inner = CIRCLE_RADIUS, CIRCLE_RADIUS - 6      # 6px 粗的白圈
        for y in range(int(cy - outer), int(cy + outer) + 1):
            for x in range(int(cx - outer), int(cx + outer) + 1):
                distance = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
                if inner <= distance <= outer:
                    put(x, y, BORDER)
    return bytes(pixels)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="造纯色壁纸样张（测试归一化显示用）")
    parser.add_argument("--dir", default="/home/kickpi/wallpapers",
                        help="写到哪个目录（默认 /home/kickpi/wallpapers）")
    parser.add_argument("--list", action="store_true", help="只列出会造什么，不写文件")
    args = parser.parse_args(argv)

    if args.list:
        for name, width, height, _, note in SAMPLES:
            print("%-32s %4dx%-5d %s" % (name, width, height, note))
        return 0

    target = Path(args.dir)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print("建不了目录 %s: %s\n（换 --dir 或用 root 跑）" % (target, exc), file=sys.stderr)
        return 1

    for name, width, height, colour, note in SAMPLES:
        path = target / name
        write_png(path, width, height, render(width, height, colour, circle="circle" in name))
        print("%-32s %4dx%-5d %6d KB  %s"
              % (name, width, height, path.stat().st_size // 1024, note))
    print("\n共 %d 张 -> %s" % (len(SAMPLES), target))
    return 0


if __name__ == "__main__":
    sys.exit(main())
