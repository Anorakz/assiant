#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/board/tag_quality.py — 标签质量与 IP 检索的**量化**验收（T7，板端跑）

它回答三个问题（都用人工真值 `tests/data/wallpaper_tags/truth.json`）:
  1. 三轴命中率: top-1 / top-2 各轴多少，错的是哪几张
  2. 预处理 A/B: 官方口径（压扁 256×256） vs 中心裁切，哪个 top-1 更高
  3. IP 锚点检索: leave-one-out 自检索（拿掉一张锚点，看能否被自己的原型找回）
     + 跨 IP 混淆（用 A 的原型检索全库，命中的是不是 A 的图）

⚠ 这是**测量脚本**，不是生产路径: 它直接读 wall_data.jsonl 里已经存好的向量
   做前两项之外的检索（IP 那项纯 CPU），预处理 A/B 那项要过 NPU 重算一遍。

跑法（板端，仓库根目录）:
    python3 tests/board/tag_quality.py                 # 全部
    python3 tests/board/tag_quality.py --skip-ab       # 跳过预处理 A/B（省时间）
    python3 tests/board/tag_quality.py --top 5         # IP 检索看前几名

退出码: 0 = 跑完（无论数字好坏）; 2 = 环境不满足（缺图/缺依赖/缺数据文件）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Tuple

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

TRUTH = os.path.join(_ROOT, "tests", "data", "wallpaper_tags", "truth.json")
WALLS = "/home/kickpi/wallpapers"


def load_truth() -> Dict[str, Any]:
    with open(TRUTH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def load_data(path: str) -> Dict[str, Dict[str, Any]]:
    from agent.vision import wall_data

    records, problems = wall_data.read_records(path)
    for problem in problems:
        print("数据文件有问题: %s" % problem, file=sys.stderr)
    return {os.path.basename(str(r.get("path"))): r for r in records}


def data_path(config: Dict[str, Any]) -> str:
    from agent.vision import wall_data

    wall = config.get("wallpaper") if isinstance(config, dict) else None
    wall = wall if isinstance(wall, dict) else {}
    tagging = wall.get("tagging") if isinstance(wall.get("tagging"), dict) else {}
    return wall_data.resolve_data_file(tagging.get("data_file"))


# ---------------------------------------------------------------------------
#  1) 三轴命中率
# ---------------------------------------------------------------------------
def axis_accuracy(truth: Dict[str, Any], records: Dict[str, Dict[str, Any]],
                  axes: Tuple[str, ...]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for axis in axes:
        top1 = top2 = total = 0
        wrong: List[str] = []
        for name, expected in truth["images"].items():
            want = expected.get(axis)
            if not want:
                continue
            record = records.get(name)
            if record is None:
                continue
            pairs = (record.get("tags") or {}).get(axis) or []
            labels = [p[0] for p in pairs]
            total += 1
            if labels[:1] == [want]:
                top1 += 1
            if want in labels[:2]:
                top2 += 1
            else:
                wrong.append("%s（真值 %s，判成 %s）"
                             % (name, want, "、".join(labels[:2]) or "无"))
        out[axis] = {"total": total, "top1": top1, "top2": top2, "wrong": wrong}
    return out


# ---------------------------------------------------------------------------
#  2) 预处理 A/B（要过 NPU）
# ---------------------------------------------------------------------------
def preprocess_ab(truth: Dict[str, Any], axes, top_k: int, limit: int = 0) -> Dict[str, Any]:
    import cv2
    import numpy as np

    from agent.config import load_config
    from agent.vision import tag_vocab
    from agent.vision.tagger import Tagging, preprocess, score_axes

    wall = (load_config("config").get("wallpaper") or {})
    tagging_cfg = (wall.get("tagging") or {})
    axes_full = tag_vocab.with_overrides(tagging_cfg.get("vocab"))

    tagging = Tagging((load_config("config").get("vision") or {}), axes=axes_full, top_k=top_k)
    tagging.prepare()
    labels = tagging._labels                                    # noqa: SLF001 - 台架脚本

    names = [n for n in truth["images"] if os.path.exists(os.path.join(WALLS, n))]
    if limit:
        names = names[:limit]

    def centre_crop(raw, size: int = 256):
        """等比缩放到短边 = size，再中心裁切（保比例，丢掉边缘）。"""
        h, w = raw.shape[:2]
        scale = max(size / float(w), size / float(h))
        resized = cv2.resize(raw, (max(size, int(round(w * scale))),
                                   max(size, int(round(h * scale)))))
        h2, w2 = resized.shape[:2]
        y0, x0 = (h2 - size) // 2, (w2 - size) // 2
        return cv2.cvtColor(resized[y0:y0 + size, x0:x0 + size], cv2.COLOR_BGR2RGB)

    result: Dict[str, Any] = {}
    for name in ["squash", "centre_crop"]:
        hits = {axis.name: [0, 0] for axis in axes_full}      # [top1, top2]
        total = 0
        for image_name in names:
            expected = truth["images"][image_name]
            raw = cv2.imread(os.path.join(WALLS, image_name))
            if raw is None:
                continue
            image = preprocess(raw) if name == "squash" else centre_crop(raw, size=256)
            tags = score_axes(tagging.model, image, axes_full, top_k=top_k,
                              label_vectors=labels)
            total += 1
            for axis in axes_full:
                pairs = tags.get(axis.name) or []
                got = [p[0] for p in pairs]
                want = expected.get(axis.name)
                if not want:
                    continue
                if got[:1] == [want]:
                    hits[axis.name][0] += 1
                if want in got[:2]:
                    hits[axis.name][1] += 1
        result[name] = {"total": total, "hits": {k: v for k, v in hits.items()}}
    tagging.model.close()
    return result


# ---------------------------------------------------------------------------
#  3) IP 锚点检索（纯 CPU）
# ---------------------------------------------------------------------------
def ip_retrieval(truth: Dict[str, Any], records: Dict[str, Dict[str, Any]],
                 presets: Dict[str, Any], top: int = 5) -> Dict[str, Any]:
    from agent.vision import wall_data

    def vector(name: str):
        record = records.get(name)
        if record is None or not record.get("embedding"):
            return None
        return wall_data.decode_embedding(record["embedding"])

    def cosine(a, b):
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(y * y for y in b) ** 0.5
        return dot / (na * nb) if na and nb else 0.0

    # ---- 原型（锚点向量均值）----
    prototypes: Dict[str, List[float]] = {}
    anchors: Dict[str, List[str]] = {}
    for label, spec in (presets or {}).items():
        names = [str(a) for a in (spec.get("anchors") or [])]
        vectors = [v for v in (vector(n) for n in names) if v]
        if not vectors:
            continue
        dim = len(vectors[0])
        mean = [sum(v[i] for v in vectors) / len(vectors) for i in range(dim)]
        norm = sum(x * x for x in mean) ** 0.5
        prototypes[str(label)] = [x / norm for x in mean] if norm else mean
        anchors[str(label)] = names

    # ---- 库里每张图的真值 IP（有的话）----
    truth_ip = {n: v.get("ip") for n, v in truth["images"].items() if v.get("ip")}

    # ---- (a) LOO 自检索: 只用 1 张锚点的 IP 拿掉它自己, 看原型还能不能认出它 ----
    # ⚠ 这里必须拿**候选自己的向量**去和原型比（`cosine(候选, proto)`）——
    #   第一版写成 `cosine(target, proto)` 放在候选循环里，于是每个候选拿到的分数
    #   完全一样（都是"被拿掉那张"自己的分数），排名退化成按文件名排序。
    #   一个"所有候选同分"的排名看着像结果，其实是假的 —— 所以下面顺手断言了分数不唯一。
    loo: List[Dict[str, Any]] = []
    for label, names in anchors.items():
        if len(names) < 2:
            continue
        for held in names:
            rest = [v for v in (vector(n) for n in names if n != held) if v]
            if not rest:
                continue
            dim = len(rest[0])
            mean = [sum(v[i] for v in rest) / len(rest) for i in range(dim)]
            norm = sum(x * x for x in mean) ** 0.5
            proto = [x / norm for x in mean] if norm else mean
            target = vector(held)
            if target is None:
                continue
            scored = []
            for name in records:
                vec = vector(name)
                if vec is not None:
                    scored.append((cosine(vec, proto), name))
            scored.sort(reverse=True)
            scores = [s for s, _ in scored]
            rank = [n for _, n in scored].index(held) + 1
            loo.append({"ip": label, "held_out": held, "rank": rank,
                        "score": round(cosine(target, proto), 4),
                        "distinct_scores": len(set(round(s, 6) for s in scores)),
                        "top1": scored[0][1],
                        "top": [(round(s, 4), n) for s, n in scored[:3]]})

    # ---- (b) 跨 IP 混淆: 每个原型检索全库, 看命中的是不是自己人 ----
    confusion: List[Dict[str, Any]] = []
    for label, proto in prototypes.items():
        ranked = sorted(((cosine(proto, vector(n)), n) for n in records
                         if vector(n) is not None), reverse=True)[:top]
        hits = [(round(score, 4), name, truth_ip.get(name))
                for score, name in ranked]
        own = [h for h in hits if h[2] == label]
        foreign = [h for h in hits if h[2] and h[2] != label]
        confusion.append({
            "ip": label,
            "top": hits,
            "own": len(own),
            "foreign_other_ip": len(foreign),
            "precision": round(len(own) / float(len(hits)), 3) if hits else 0.0,
        })
    return {"prototypes": sorted(prototypes), "loo": loo, "confusion": confusion,
            "truth_ip_count": len(truth_ip)}


# ---------------------------------------------------------------------------
#  主流程
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="标签质量与 IP 检索量化（板端）")
    parser.add_argument("--skip-ab", action="store_true", help="跳过预处理 A/B（省时间）")
    parser.add_argument("--ab-limit", type=int, default=0, help="A/B 只跑前 N 张")
    parser.add_argument("--top", type=int, default=5, help="IP 检索看前几名")
    args = parser.parse_args(argv)

    try:
        from agent.config import load_config
        from agent.vision import tag_vocab
    except ImportError as exc:
        print("环境不满足: %s" % exc)
        return 2

    if not os.path.exists(TRUTH):
        print("环境不满足: 找不到真值文件 %s" % TRUTH)
        return 2

    truth = load_truth()
    config = load_config("config")
    path = data_path(config)
    if not os.path.exists(path):
        print("环境不满足: 还没有标签数据 %s\n先跑: assistant tag --apply" % path)
        return 2

    records = load_data(path)
    axes = tag_vocab.AXES
    print("真值 %d 张（其中 %d 张有 IP）; 数据文件 %s（%d 行）"
          % (len(truth["images"]),
             sum(1 for v in truth["images"].values() if v.get("ip")),
             path, len(records)))

    print()
    print("=== 1) 三轴命中率（人工真值 vs 数据文件里的 top-k）===")
    accuracy = axis_accuracy(truth, records, tuple(a.name for a in axes))
    for axis, stat in accuracy.items():
        if not stat["total"]:
            print("  %-6s 没有真值，跳过" % axis)
            continue
        print("  %-6s top-1 %d/%d (%.0f%%)   top-2 %d/%d (%.0f%%)"
              % (axis, stat["top1"], stat["total"], 100.0 * stat["top1"] / stat["total"],
                 stat["top2"], stat["total"], 100.0 * stat["top2"] / stat["total"]))
        for line in stat["wrong"]:
            print("        错: %s" % line)

    if not args.skip_ab:
        print()
        print("=== 2) 预处理 A/B（官方口径压扁 vs 中心裁切；要过 NPU）===")
        try:
            ab = preprocess_ab(truth, axes, top_k=3, limit=args.ab_limit)
        except Exception as exc:                              # noqa: BLE001
            print("  跑不了 A/B: %r" % (exc,))
            ab = None
        if ab:
            for mode, stat in ab.items():
                pieces = []
                for axis, (t1, t2) in stat["hits"].items():
                    pieces.append("%s top-1 %d/%d" % (axis, t1, stat["total"]))
                print("  %-12s %s" % (mode, "，".join(pieces)))

    print()
    print("=== 3) IP 锚点检索（纯 CPU，用已存的向量）===")
    tagging_cfg = ((config.get("wallpaper") or {}).get("tagging") or {})
    ip = ip_retrieval(truth, records, tagging_cfg.get("ip_presets") or {}, top=args.top)
    print("  原型: %s（共 %d 个）" % ("、".join(ip["prototypes"]), len(ip["prototypes"])))
    print("  --- LOO 自检索（每个能 LOO 的锚点轮流拿掉自己）---")
    if not ip["loo"]:
        print("      没有哪个 IP 有 ≥2 张锚点，做不了 LOO")
    for row in ip["loo"]:
        print("      %-5s 拿掉 %-38s 排名 %d/%d，分 %.3f，第一名是 %s%s"
              % (row["ip"], os.path.basename(row["held_out"]), row["rank"],
                 len(records), row["score"], os.path.basename(row["top1"]),
                 "" if row.get("distinct_scores", 2) > 1 else "  ⚠ 所有候选同分（排名无效）"))
        for score, name in row.get("top", []):
            print("            %.4f  %s" % (score, name[:44]))
    print("  --- 跨 IP 检索前 %d 名（own = 命中自己 IP，foreign = 召进了别的 IP）---"
          % args.top)
    for row in ip["confusion"]:
        print("      %-5s own=%d foreign=%d precision=%.2f"
              % (row["ip"], row["own"], row["foreign_other_ip"], row["precision"]))
        for score, name, label in row["top"]:
            print("            %.4f  %-40s %s"
                  % (score, name[:40], label or "(无 IP 真值)"))

    print()
    print("说明: 命中率与检索指标**原样**记录（包括不好看的）；这脚本只测量，不改任何东西。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
