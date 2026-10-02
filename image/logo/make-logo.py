#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把准备的图转成 Rockchip 启动 logo 用的 BMP（T15-2-11）。

机制（SDK 源码 + 板端双向确认）：
  · 启动图 = 内核树根的 `logo.bmp`（u-boot 用）/ `logo_kernel.bmp`（内核用），
    由 `mk-kernel.sh` 经 `scripts/resource_tool` 打进 `resource.img` → 进
    boot.img 的 FIT 的 resource 子镜像；DTB 里的
    `logo,offset/width/height/`**`bpp`** 由该工具按 BMP 头写入，
    内核 `rockchip_drm_logo.c` 按它贴图，**bpp 只支持 16/24/32**；路由是
    `logo,mode = "center"`。
  · 所以：BMP 做成**与屏等大 1080x1920、24bpp**，居中即整屏，不依赖摆放逻辑。

三种摆放方式（按需选）：
    （默认）     等比缩到宽度铺满、垂直居中、其余黑 —— 横图居中的观感
    --asis       原样使用（只做必要缩放）；**图已经是 1080x1920 且方向正确时用这个**
    --fill       旋转 90° 后铺满（裁掉溢出）—— 想让横图占满竖屏时用
                 （--rotate-ccw 可换成逆时针）
    --r180       再转 180°（与上面几种可叠加）—— **T15-2-11 实况**：用户把图转了 90°
                 后刷进板子，屏上看着**上下颠倒**（说明还差 180°），于是用这个补上。
                 记这条是因为"文件里的方向"和"屏上的方向"没有直觉关系，只能实测。

用法:
    python make-logo.py <输入> <输出.bmp> [--asis|--fill] [--rotate-ccw] [--r180]
"""
import sys
from PIL import Image

PANEL_W, PANEL_H = 1080, 1920


def _flatten(img: Image.Image) -> Image.Image:
    """带透明通道的先合成到黑底（BMP 没有 alpha，透明区域不该变成垃圾像素）。"""
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, (0, 0, 0, 255))
        img = Image.alpha_composite(bg, img)
    return img.convert("RGB")


def build_fit(path_in: str) -> Image.Image:
    """等比缩到宽度铺满，垂直居中，黑底。"""
    src = _flatten(Image.open(path_in))
    scale = PANEL_W / src.width
    new = (PANEL_W, max(1, round(src.height * scale)))
    src = src.resize(new, Image.LANCZOS)
    canvas = Image.new("RGB", (PANEL_W, PANEL_H), (0, 0, 0))
    canvas.paste(src, (0, (PANEL_H - new[1]) // 2))
    return canvas


def build_asis(path_in: str) -> Image.Image:
    """原样使用（尺寸不符时才缩放，不旋转、不裁切）。"""
    src = _flatten(Image.open(path_in))
    if src.size != (PANEL_W, PANEL_H):
        src = src.resize((PANEL_W, PANEL_H), Image.LANCZOS)
    return src


def build_fill(path_in: str, ccw: bool = False) -> Image.Image:
    """旋转 90° 后铺满整屏（裁掉溢出）。"""
    src = _flatten(Image.open(path_in))
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
    with Image.open(src) as probe:
        print("输入: %s  %s %sx%s" % (src, probe.format, probe.width, probe.height))
    if "--fill" in flags:
        img = build_fill(src, ccw="--rotate-ccw" in flags)
        how = "fill(旋转铺满)"
    elif "--asis" in flags:
        img = build_asis(src)
        how = "asis(原样)"
    else:
        img = build_fit(src)
        how = "fit(按宽铺满居中)"
    if "--r180" in flags:
        img = img.transpose(Image.ROTATE_180)
        how += "+180°"
    img.save(dst, format="BMP")
    head = open(dst, "rb").read(54)
    bpp = int.from_bytes(head[28:30], "little")
    w = int.from_bytes(head[18:22], "little")
    h = int.from_bytes(head[22:26], "little")
    import os
    print("写出 %s: %dx%d %dbpp, %.2f MB  [%s]" % (dst, w, h, bpp, os.path.getsize(dst) / 1048576, how))
    ok = (w, h) == (PANEL_W, PANEL_H) and bpp == 24
    if not ok:
        print("!! 期望 %dx%d 24bpp" % (PANEL_W, PANEL_H))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
