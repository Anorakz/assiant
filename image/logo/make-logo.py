#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把用户的 PNG 转成 Rockchip 启动 logo 用的 BMP。

T15-2-11 启动画面：
  · 屏原生 1080x1920（竖），内核按 DTB 里的 logo,width/height/offset/bpp 贴图，
    只支持 bpp ∈ {16,24,32}（源码 rockchip_drm_logo.c 的 switch）。
  · 所以出一张 **与屏等大的 24bpp BMP**（1080x1920）：这样"居中还是左上"都无所谓，
    整屏就是这张图，不依赖任何摆放逻辑。
  · 布局：原图等比缩放到宽 1080，垂直居中，其余填黑 —— 与厂商 logo（横图居中）一致的观感。
    另有 --fill 模式：旋转 90° 铺满整屏（图像内容是躺着的，只有"机器横着用"时才合适）。

用法:
    python make-logo.py <输入.png> <输出.bmp> [--fill] [--rotate-ccw]
"""
import sys
import pathlib
from PIL import Image

PANEL_W, PANEL_H = 1080, 1920


def build_fit(path_in: str) -> Image.Image:
    """等比缩到宽度铺满，垂直居中，黑底。"""
    src = Image.open(path_in).convert("RGB")
    scale = PANEL_W / src.width
    new = (PANEL_W, max(1, round(src.height * scale)))
    src = src.resize(new, Image.LANCZOS)
    canvas = Image.new("RGB", (PANEL_W, PANEL_H), (0, 0, 0))
    canvas.paste(src, (0, (PANEL_H - new[1]) // 2))
    return canvas


def build_fill(path_in: str, ccw: bool = False) -> Image.Image:
    """旋转 90° 后铺满整屏（会裁掉溢出部分）。"""
    src = Image.open(path_in).convert("RGB")
    src = src.rotate(90 if ccw else -90, expand=True)
    scale = max(PANEL_W / src.width, PANEL_H / src.height)
    new = (max(PANEL_W, round(src.width * scale)), max(PANEL_H, round(src.height * scale)))
    src = src.resize(new, Image.LANCZOS)
    left = (src.width - PANEL_W) // 2
    top = (src.height - PANEL_H) // 2
    return src.crop((left, top, left + PANEL_W, top + PANEL_H))


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if len(args) != 2:
        print(__doc__)
        return 2
    src, dst = args
    img = build_fill(src, ccw="--rotate-ccw" in flags) if "--fill" in flags else build_fit(src)
    img.save(dst, format="BMP")     # PIL 对 RGB 图存 24bpp BMP
    out = pathlib.Path(dst)
    head = out.read_bytes()[:54]
    bpp = int.from_bytes(head[28:30], "little")
    w = int.from_bytes(head[18:22], "little")
    h = int.from_bytes(head[22:26], "little")
    print("写出 %s: %dx%d %dbpp, %d 字节" % (out, w, h, bpp, out.stat().st_size))
    return 0 if bpp == 24 else 1


if __name__ == "__main__":
    sys.exit(main())
