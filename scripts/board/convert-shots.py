#!/usr/bin/env python3
# convert-shots.py -- 把 pull-shots.ps1 取回的裸帧转成 PNG（挑最优帧 + 剔黑屏/重复 ✓）
#   用法： python convert-shots.py --tmp <裸帧目录> --out <PNG 目录> --keep 1
#   说明：板端没有 PIL（也没 pngenc/jpegenc ✗），所以转换放 PC ✓；帧格式 = 800x1280x4 BGRA ✓
import argparse
import pathlib
from PIL import Image

W, H, FRAME = 800, 1280, 800 * 1280 * 4


def frames_of(path: pathlib.Path):
    data = path.read_bytes()
    n = len(data) // FRAME
    for i in range(n):
        img = Image.frombytes("RGBA", (W, H), data[i * FRAME:(i + 1) * FRAME], "raw", "BGRA").convert("RGB")
        small = img.resize((60, 96))
        px = list(small.getdata())
        lum = sum(sum(p) / 3 for p in px) / len(px)
        yield img, lum, len(set(px))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tmp", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--keep", type=int, default=1)
    ap.add_argument("--min-lum", type=float, default=8.0)
    ap.add_argument("--min-colors", type=int, default=30)
    a = ap.parse_args()

    tmp, out = pathlib.Path(a.tmp), pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for raw in sorted(tmp.glob("*.raw")):
        cands = [(c, im, lum) for im, lum, c in frames_of(raw) if lum > a.min_lum and c > a.min_colors]
        cands.sort(key=lambda t: t[0], reverse=True)          # 颜色最丰富的帧 ✓
        if not cands:
            rows.append((raw.name, 0, "全部黑屏/花屏 ⇒ 不落盘 ✗"))
            continue
        saved = []
        last = None
        for c, im, lum in cands:
            if last is not None and list(im.getdata()) == last:   # 与上一张完全相同就跳过 ✓
                continue
            last = list(im.getdata())
            dst = out / ("shot-%s%s.png" % (raw.stem, "" if len(saved) == 0 else "-%d" % len(saved)))
            im.save(dst, optimize=True)
            saved.append("%s（%.0f KB）" % (dst.name, dst.stat().st_size / 1024))
            if len(saved) >= a.keep:
                break
        rows.append((raw.name, len(cands), "、".join(saved) or "重复帧 ⇒ 只留第一张 ✗"))

    print("  裸帧                          可用帧  产出")
    for n, c, s in rows:
        print("  %-28s %5d  %s" % (n, c, s))
    print("  ⇒ PNG 目录：%s" % out)


if __name__ == "__main__":
    main()
