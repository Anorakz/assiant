#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T11-0 门禁实测之一：**游戏识别标定**（板端跑）。

回答三件事（数字进 todo.md 的 T11-0 记录）:
  1. **画面锚点**（SigLIP 图像向量 + 余弦）能不能认出游戏 —— leave-one-out top-1 命中率、
     命中/误判的分数分布（用来定阈值）、混淆矩阵；
  2. **文本锚点**（把游戏名当文本编码）有没有用 —— 对照组（SigLIP 文本塔是英文图文对训的，
     中文游戏名大概率不行，这里要**量出来**而不是猜）；
  3. 运行代价 —— 模型加载耗时、单帧编码耗时、峰值内存。

外加 4) **PC 窗口/进程** 这条真值路：从板端 ssh 到 PC 读窗口标题（含中文编码两套写法对比）+ 延迟。

跑法（板端，仓库根）::

    python3 tests/board/t11_game_calib.py                 # 默认读 /home/kickpi/game_samples
    python3 tests/board/t11_game_calib.py --dir /path --no-pc

⚠ 这是**测量脚本**（要真模型 + 真截图），不是生产路径。
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

DEFAULT_DIR = "/home/kickpi/game_samples"
IMAGE_SUFFIX = (".jpg", ".jpeg", ".png", ".webp", ".bmp")


def rss_mb(peak: bool = False) -> float:
    key = "VmHWM:" if peak else "VmRSS:"
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith(key):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        pass
    return 0.0


def collect(root):
    """{游戏名: [图片路径, ...]} —— 目录名就是游戏名（你自己就是这么放的）。"""
    out = {}
    for name in sorted(os.listdir(root)):
        folder = os.path.join(root, name)
        if not os.path.isdir(folder):
            continue
        files = sorted(os.path.join(folder, f) for f in os.listdir(folder)
                       if f.lower().endswith(IMAGE_SUFFIX))
        if files:
            out[name] = files
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default=DEFAULT_DIR)
    parser.add_argument("--no-pc", action="store_true", help="跳过 PC 窗口/进程那一段")
    parser.add_argument("--limit", type=int, default=0, help="每个游戏最多用几张（0=全部）")
    args = parser.parse_args()

    from agent.config import load_config
    from agent.vision.siglip import SiglipModel
    from agent.vision.tagger import load_image
    import numpy as np

    games = collect(args.dir)
    if args.limit:
        games = {name: files[:args.limit] for name, files in games.items()}
    if len(games) < 2:
        print("至少要有 2 个游戏目录（现在 %d 个）" % len(games))
        return 2
    print("== 素材: %d 个游戏 / %d 张" % (len(games), sum(len(v) for v in games.values())))
    for name, files in games.items():
        print("   %-14s %d 张" % (name, len(files)))

    config = load_config("config")
    print("\n== 1) 加载模型 + 编码所有截图")
    model = SiglipModel.from_config(config.get("vision"))
    started = time.time()
    model.load()
    load_s = time.time() - started
    print("   模型加载 %.1fs" % load_s)

    vectors = {}
    times = []
    for name, files in games.items():
        for path in files:
            started = time.time()
            image = load_image(path)
            vec = np.asarray(model.encode_image(image), dtype=np.float32)
            ms = (time.time() - started) * 1000.0
            times.append(ms)
            vectors[path] = vec
            print("   %-14s %-34s %6.0f ms" % (name, os.path.basename(path)[:34], ms))
    print("   单帧编码: 中位 %.0f ms / 最慢 %.0f ms / 峰值内存 %.0f MB"
          % (sorted(times)[len(times) // 2], max(times), rss_mb(peak=True)))

    def cos(a, b):
        return float(np.dot(a, b))

    # ---- 2) 画面锚点 leave-one-out ----
    print("\n== 2) 画面锚点（leave-one-out, 每张拿本游戏**其余**图当锚点）")
    per_anchor, prototype = [], []
    detail = []
    for name, files in games.items():
        for path in files:
            query = vectors[path]
            best_anchor = {}
            proto = {}
            for other, others in games.items():
                anchors = [vectors[p] for p in others if p != path]
                if not anchors:
                    continue
                best_anchor[other] = max(cos(query, a) for a in anchors)
                proto[other] = cos(query, np.mean(np.stack(anchors), axis=0))
            if not best_anchor:
                continue
            top_a = max(best_anchor.items(), key=lambda kv: kv[1])
            top_p = max(proto.items(), key=lambda kv: kv[1])
            correct = best_anchor.get(name, 0.0)
            wrong = max([v for k, v in best_anchor.items() if k != name] or [0.0])
            per_anchor.append((name, top_a[0], top_a[1], correct, wrong))
            prototype.append((name, top_p[0], top_p[1]))
            detail.append({"game": name, "file": os.path.basename(path),
                           "top1_anchor": top_a[0], "score": round(top_a[1], 4),
                           "correct_score": round(correct, 4),
                           "best_wrong": round(wrong, 4), "margin": round(correct - wrong, 4)})

    hit_a = sum(1 for name, got, _, _, _ in per_anchor if got == name)
    hit_p = sum(1 for name, got, _ in prototype if got == name)
    print("   逐锚点取最大: top-1 命中 **%d/%d = %.0f%%**"
          % (hit_a, len(per_anchor), 100.0 * hit_a / max(len(per_anchor), 1)))
    print("   取平均原型  : top-1 命中 **%d/%d = %.0f%%**"
          % (hit_p, len(prototype), 100.0 * hit_p / max(len(prototype), 1)))
    for row in detail:
        mark = "✔" if row["top1_anchor"] == row["game"] else "✘"
        print("   %s %-12s %-30s top1=%-12s %.3f | 对的 %.3f / 最错的 %.3f | 余量 %+.3f"
              % (mark, row["game"], row["file"][:30], row["top1_anchor"], row["score"],
                 row["correct_score"], row["best_wrong"], row["margin"]))

    correct_scores = [row["correct_score"] for row in detail]
    wrong_scores = [row["best_wrong"] for row in detail if row["top1_anchor"] != row["game"]]
    print("   分数分布: 正确匹配 最低 %.3f / 中位 %.3f;  误判时的错误分 %s"
          % (min(correct_scores), sorted(correct_scores)[len(correct_scores) // 2],
             ("最高 %.3f" % max(wrong_scores)) if wrong_scores else "（没有误判）"))
    print("   → 建议阈值区间: %.2f ～ %.2f（低于下界会把对的也判成认不出；高于上界放过误判）"
          % (max(wrong_scores) if wrong_scores else 0.0, min(correct_scores)))

    confusion = collections.Counter()
    for row in detail:
        if row["top1_anchor"] != row["game"]:
            confusion["%s -> %s" % (row["game"], row["top1_anchor"])] += 1
    if confusion:
        print("   混淆: %s" % "、".join("%s ×%d" % (k, v) for k, v in confusion.items()))

    # ---- 3) 文本锚点（对照：游戏名当文本编码）----
    print("\n== 3) 文本锚点对照（把游戏名当文本；SigLIP 文本塔偏英文）")
    text_hit = 0
    total = 0
    for name, files in games.items():
        try:
            np.asarray(model.encode_text(name), dtype=np.float32)
        except Exception as exc:                            # noqa: BLE001
            print("   %-14s 文本编码失败: %r" % (name, exc))
            continue
        for path in files:
            scored = sorted(((cos(vectors[path], tvec2), other)
                             for other, tvec2 in
                             ((g, np.asarray(model.encode_text(g), dtype=np.float32))
                              for g in games)), reverse=True)
            total += 1
            if scored[0][1] == name:
                text_hit += 1
        print("   %-14s 文本向量已编码" % name)
    if total:
        print("   文本锚点 top-1 命中 **%d/%d = %.0f%%**"
              % (text_hit, total, 100.0 * text_hit / total))

    # ---- 4) PC 窗口/进程（真值路）----
    if not args.no_pc:
        print("\n== 4) PC 窗口/进程 这条真值路（从板端 ssh 过去读）")
        from agent.net.netease_cli import NeteaseCli
        cli = NeteaseCli.from_config(config)
        if cli is None or not cli.host:
            print("   ⚠ config 里没有 music.pc_host/pc_user，跳过")
        else:
            ps = ("$OutputEncoding=[Text.Encoding]::UTF8;"
                  "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
                  "Get-Process | Where-Object {$_.MainWindowTitle} | "
                  "Select-Object ProcessName,MainWindowTitle | ConvertTo-Json -Compress")
            for label, command in (
                    ("带 UTF-8 强制", "powershell -NoProfile -Command \"%s\"" % ps),
                    ("不带（对照）", "powershell -NoProfile -Command \""
                     "Get-Process | Where-Object {$_.MainWindowTitle} | "
                     "Select-Object ProcessName,MainWindowTitle | ConvertTo-Json -Compress\"")):
                started = time.time()
                try:
                    proc = __import__("subprocess").run(
                        cli._ssh_argv(command), stdout=__import__("subprocess").PIPE,
                        stderr=__import__("subprocess").PIPE, timeout=25)
                    out = proc.stdout.decode("utf-8", "replace").strip()
                    err = proc.stderr.decode("utf-8", "replace").strip()
                except Exception as exc:                    # noqa: BLE001
                    print("   %-12s 失败: %r" % (label, exc))
                    continue
                ms = (time.time() - started) * 1000.0
                try:
                    rows = json.loads(out) if out.startswith(("[", "{")) else []
                    if isinstance(rows, dict):
                        rows = [rows]
                    titles = [(r.get("ProcessName"), r.get("MainWindowTitle")) for r in rows][:5]
                except Exception:                           # noqa: BLE001
                    titles = []
                print("   %-12s %5.0f ms, 退出码 %s, 行数 %d"
                      % (label, ms, proc.returncode, len(titles)))
                for proc_name, title in titles:
                    print("        %-16s %s" % (proc_name, title))
                if not titles:
                    print("        stdout 前 200 字: %s" % out[:200])
                    if err:
                        print("        stderr 前 200 字: %s" % err[:200])

    print("\n== 汇总（写进 T11-0 记录）")
    print("   画面锚点 top-1 %.0f%% / 平均原型 %.0f%% / 模型加载 %.1fs / 单帧中位 %.0f ms / 峰值 %.0f MB"
          % (100.0 * hit_a / max(len(per_anchor), 1), 100.0 * hit_p / max(len(prototype), 1),
             load_s, sorted(times)[len(times) // 2], rss_mb(peak=True)))
    with open("/tmp/t11_game_calib.json", "w", encoding="utf-8") as handle:
        json.dump({"games": {k: len(v) for k, v in games.items()},
                   "hit_per_anchor": hit_a, "hit_prototype": hit_p, "total": len(per_anchor),
                   "load_s": load_s, "encode_ms_median": sorted(times)[len(times) // 2],
                   "peak_mb": rss_mb(peak=True), "detail": detail},
                  handle, ensure_ascii=False, indent=1)
    print("   明细已写 /tmp/t11_game_calib.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
