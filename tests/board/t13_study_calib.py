#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T13-4 门禁实测: **学习内容判定的数据管线 + 板端标定**（板端跑，只写 /tmp 与 config/ 的派生数据）。

要回答的问题（数字进 T13-4 的验收报告）:

  ① **管线怎么复现**: 板子看到的不是 `screenshot.png`，是 Sunshine 按 `sunshine.width/height`
     编出来的帧，再经 `native/preprocess.cpp` 的**点采样**变成 256×256。所以标定必须
     先把整屏截图缩到流分辨率，再按 native 的整数映射**取点**。
     ⚠ `preprocess.cpp` 是**点采样不是面积平均**: `rx = (dx * roi.w) / 256`（整数除），
       1280 宽时就是"每 5 列取 1 列" —— 细笔画（文字）会走样。这条差异到底伤多少，
       用三种对照管线量出来。
  ② **四路方法谁行**: M1 文本提示词（零样本，仓库里实测过会塌）/ M2 图像锚点
     （逐条 + 类原型两个打法）/ M3 融合 / M4 加进程名（截图数据集上测不了，只给上界）。
  ③ **起始阈值**: 用真实分数分布给出 `confident_score` / `confident_margin` 的**初值**，
     并报出这个初值下的"判不出来"比例与**两个方向**的错误数（误打扰 / 监督失效）。
  ④ 代价: 单帧编码 + 匹配耗时、峰值内存。

跑法（板端，仓库根）::

    python3 tests/board/t13_study_calib.py --plan            # 只看数据集与管线, 不加载模型
    python3 tests/board/t13_study_calib.py                   # 详细报告（h1 = 真管线假设）
    python3 tests/board/t13_study_calib.py --all             # 四种管线对照（实测 ≈10 分钟）
    python3 tests/board/t13_study_calib.py --apply           # 顺手把锚点与起始阈值落到 config/

⚠ 这是**测量脚本**，不是生产路径。数据集只有 39 张（doc/real/anime 各 4–5 张），
  所以它给的是**初值**，不是"标定完了" —— 真正的收敛靠 `study_watch.py` 在运行期
  用带标签样本自己挪（一次 0.01，护栏在那边）。
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import statistics
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

DEFAULT_DATASET = "/home/kickpi/study_dataset"
IMAGE_SUFFIX = (".jpg", ".jpeg", ".png", ".webp", ".bmp")

#: 开发机（Windows + GBK 控制台）上跑 `--plan` 时别因为一个 ⚠ 就崩（板端是 UTF-8, 无所谓）。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")     # type: ignore[attr-defined]
except Exception:                                                  # noqa: BLE001
    pass

#: 生产口径的类 → 大类（与 `study_anchors.DEFAULT_CLASSES` 一致；配置改了这里要跟着改）。
CATEGORY_OF = {"code": "study", "doc": "study", "real": "study",
               "anime": "not_study", "game": "not_study"}

#: 流分辨率（板端 `config/config.yaml` 的 `sunshine.width/height`）。
STREAM_W, STREAM_H = 1280, 720

#: 四条候选管线。h1 是我们**认为**的真管线（拉伸到流分辨率 + native 点采样）。
HYPOTHESES = {
    "h1": "整屏 → 拉伸 %d×%d → native 点采样 → 256×256（真管线假设）" % (STREAM_W, STREAM_H),
    "h2": "整屏 → 等比 %d×800 → native 点采样（若流保持了 16:10）" % STREAM_W,
    "h3": "整屏 → INTER_AREA 一步到 256×256（不用点采样: 好看但**不是**真管线）",
    "h4": "整屏 → %d×%d → INTER_AREA 到 256×256（同分辨率, 只换掉点采样）" % (STREAM_W, STREAM_H),
}

#: M1 文本提示词: **英文裸串**、每类若干条、取该类的最高分
#: （仓库口径见 `agent/vision/tag_vocab.py`: SigLIP 是英文图文对训的，裸串是分布内用法）。
PROMPTS = {
    "code": ["source code", "code editor", "programming", "terminal window", "ide"],
    "doc": ["a document", "text page", "pdf document", "notes", "spreadsheet",
            "reading a paper"],
    "real": ["a photograph of a room", "a photo of a person", "camera photo",
             "a real world scene"],
    "anime": ["anime illustration", "anime girl", "cartoon drawing", "manga page"],
    "game": ["video game screenshot", "game menu", "3d game world", "gameplay"],
}

CLASSES = ["code", "doc", "real", "anime", "game"]

#: 提示词摊平（**一次调完**用的; 见 `text_scores` 的 ⚠）。
FLAT_PROMPTS = [(name, text) for name in CLASSES for text in PROMPTS[name]]


# ---------------------------------------------------------------------------
#  管线: 直接用生产里那一份（`agent/vision/frame_pipeline.py`）
# ---------------------------------------------------------------------------
def frame_of(raw, hypothesis, cv2):
    """按指定管线把一张 BGR 图变成模型帧 —— 实现只有 `frame_pipeline` 一份, 这里只做映射。

    h1 = 真管线（拉伸到 1280×720 + native 点采样）; h2 = 保持 16:10 的变体;
    h3 = 整屏一步面积平均（理想重采样, 不是真管线）; h4 = 同 h1 的分辨率但换掉点采样。
    """
    from agent.vision import frame_pipeline as fp

    if hypothesis == "h1":
        return fp.to_model_frame(raw, stream=(STREAM_W, STREAM_H), mode="native")
    if hypothesis == "h2":
        return fp.to_model_frame(raw, stream=(STREAM_W, 800), mode="native")
    if hypothesis == "h3":
        return fp.to_model_frame(raw, mode="direct")
    if hypothesis == "h4":
        return fp.to_model_frame(raw, stream=(STREAM_W, STREAM_H), mode="area")
    raise SystemExit("不认识的管线: %s" % hypothesis)


def native_points(width, height, size=256):
    """`preprocess.cpp` 的整数映射（薄封装到生产那份实现, 标定时用来打印采样行号）。"""
    from agent.vision.frame_pipeline import native_sample_index

    return native_sample_index(height, size), native_sample_index(width, size)


def collect(dataset):
    """{子标签: [图片路径…]} —— 目录名就是子标签（数据集就是这么放的）。"""
    out = {}
    for name in sorted(os.listdir(dataset)):
        folder = os.path.join(dataset, name)
        if not os.path.isdir(folder):
            continue
        files = sorted(os.path.join(folder, name2) for name2 in os.listdir(folder)
                       if name2.lower().endswith(IMAGE_SUFFIX))
        if files:
            out[name] = files
    return out


def rss_mb(peak=True):
    key = "VmHWM:" if peak else "VmRSS:"
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith(key):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        pass
    return 0.0


# ---------------------------------------------------------------------------
#  指标
# ---------------------------------------------------------------------------
def confusion(true_labels, pred_labels, labels):
    """{真: {预测: 计数}} + 准确率。空预测（判不出来）单独记在 `""`。"""
    table = {name: collections.Counter() for name in labels}
    for truth, guess in zip(true_labels, pred_labels):
        table.setdefault(truth, collections.Counter())[guess or ""] += 1
    total = len(true_labels)
    right = sum(1 for truth, guess in zip(true_labels, pred_labels) if truth == guess)
    return table, (right / float(total) if total else 0.0)


def category_prototype_holdout(vectors, labels):
    """M5: **大类原型 + 相对分** —— 直接回答"像不像学习"这个问题。

    为什么要它: 生产口径的 `category_score` 是"大类里最像的**那一条锚点**"的绝对余弦,
    而 UI 截图之间本来就长得像 —— 实测所有帧的绝对余弦挤在 0.72–0.98, 于是 study 与
    not_study 的分布**重叠**, 单一绝对阈值会把 **69%** 的帧判成"判不出来"（h1 实测）。
    这里换成"大类原型 + 相对分"再看一遍:
      · 大类原型 = 留一之后该类所有锚点的**单位向量平均方向**（与 `match(method="prototype")` 同一件事）;
      · 相对分 = `cos(帧, study 原型) - cos(帧, not_study 原型)` —— 只看"更像哪边"。

    @return [(预测大类, study 余弦, not_study 余弦, 相对分), ...]
    """
    from agent.core.study_anchors import _dot, _unit

    units = [_unit(list(vector)) for vector in vectors]
    categories = [CATEGORY_OF[name] for name in labels]
    out = []
    for held in range(len(units)):
        prototypes = {}
        for category in ("study", "not_study"):
            members = [units[index] for index in range(len(units))
                       if index != held and categories[index] == category]
            if not members:
                prototypes[category] = None
                continue
            width = len(members[0])
            mean = [0.0] * width
            for member in members:
                for axis, value in enumerate(member):
                    mean[axis] += value
            prototypes[category] = _unit([value / len(members) for value in mean])
        if prototypes.get("study") is None or prototypes.get("not_study") is None:
            out.append(("", 0.0, 0.0, 0.0))
            continue
        study = _dot(units[held], prototypes["study"])
        not_study = _dot(units[held], prototypes["not_study"])
        relative = study - not_study
        out.append(("study" if relative > 0 else "not_study", study, not_study, relative))
    return out


def relative_sweep(true_cats, rel_rows, bands=(0.0, 0.01, 0.02, 0.05, 0.10)):
    """相对分 + 一条"没把握带"（|相对分| < band -> 判不出来）的取舍表。"""
    out = []
    for band in bands:
        decided = []
        for (_guess, _s, _n, relative), truth in zip(rel_rows, true_cats):
            if abs(relative) < band:
                decided.append("")
            else:
                decided.append("study" if relative > 0 else "not_study")
        errors = two_class_errors(true_cats, decided)
        right = sum(1 for truth, guess in zip(true_cats, decided) if truth == guess)
        errors["band"] = band
        errors["accuracy"] = right / float(len(true_cats)) if true_cats else 0.0
        out.append(errors)
    return out


def print_matrix(table, labels, indent="    "):
    short = {name: name[:4] for name in labels}
    header = indent + "%-8s" % "真\\预测" + "".join("%7s" % short[name] for name in labels)
    print(header + "%7s" % "(判不出)")
    for truth in labels:
        row = table.get(truth, collections.Counter())
        cells = "".join("%7d" % row.get(name, 0) for name in labels)
        print(indent + "%-8s" % truth + cells + "%7d" % row.get("", 0))


def two_class_errors(true_cats, pred_cats):
    """两个方向的错误数（它们的代价完全不同）+ 判不出来的比例。"""
    nag = sum(1 for t, p in zip(true_cats, pred_cats) if t == "study" and p == "not_study")
    miss = sum(1 for t, p in zip(true_cats, pred_cats) if t == "not_study" and p == "study")
    unknown = sum(1 for p in pred_cats if not p)
    total = len(true_cats)
    return {"nag": nag, "miss": miss, "unknown": unknown,
            "unknown_rate": (unknown / float(total)) if total else 0.0,
            "nag_rate": (nag / float(total)) if total else 0.0,
            "miss_rate": (miss / float(total)) if total else 0.0}


def quantile(values, q):
    """样本分位数（线性插值）。样本这么少（4–12 个），报出来主要是**别装精确**。"""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = q * (len(ordered) - 1)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    frac = pos - low
    return ordered[low] * (1 - frac) + ordered[high] * frac


def clamp(value, low, high):
    return max(low, min(high, value))


# ---------------------------------------------------------------------------
#  方法
# ---------------------------------------------------------------------------
def text_scores(model, frame):
    """M1: 每个子标签取"它那几条提示词里的最高分"。

    ⚠ **一次把 21 条提示词算完**, 别按类别分 5 次调 `model.similarities()` ——
      它每次都会重新 `encode_image()`: 39 张 × 5 次 = 195 次重复编码,
      h1 实测 **356 s**; 一次调完只要 39 次 ≈ 70 s。分数完全一样（同一批文本向量）。
    """
    flat = model.similarities(frame, [text for _name, text in FLAT_PROMPTS])
    out = {name: -1.0 for name in CLASSES}
    for (name, _text), score in zip(FLAT_PROMPTS, flat):
        out[name] = max(out[name], float(score))
    return out


def anchor_leave_one_out(vectors, labels, method="anchor", tmp_dir="/tmp"):
    """M2: **留一法** —— 用生产代码（`StudyAnchors.load()` + `.match()`）判每一张。

    @return [(预测子标签, 大类分, 大类余量, 子标签分, 子标签余量, 锚点条数), ...]
    @note 为什么不自己写余弦: 标定要反映**生产里那套**（归一化点积、大类聚合、余量定义），
          自己再写一份就等于标了个别的东西。这里每折把 38 条锚点写成一个小 jsonl 再 load()。
    """
    import tempfile

    from agent.core.study_anchors import StudyAnchors

    rows = []
    for label, (cls, vec) in enumerate(zip(labels, vectors)):
        rows.append(json.dumps({"cls": cls, "vector": encode_anchor(vec), "shot": "",
                                "source": "calib", "note": "loo",
                                "created_at": "2026-01-01T00:00:00"}, ensure_ascii=False))
    out = []
    for held in range(len(vectors)):
        handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False,
                                            dir=tmp_dir, encoding="utf-8")
        try:
            for index, row in enumerate(rows):
                if index != held:
                    handle.write(row + "\n")
            handle.close()
            library = StudyAnchors(handle.name, keep_shots=False)
            library.load()
            hit = library.match(list(vectors[held]), method=method)
        finally:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
        if not hit:
            out.append(("", 0.0, 0.0, 0.0, 0.0, 0))
            continue
        out.append((str(hit["cls"]), float(hit["category_score"]),
                    float(hit["category_margin"]), float(hit["score"]),
                    float(hit["margin"]), int(hit["anchors"])))
    return out


def encode_anchor(vector):
    from agent.core.study_anchors import encode

    return encode(vector)


def fuse(anchor_scores, text_scores_map):
    """M3: 两路**各自标准化**后再合成 —— 图像余弦与文本相似度不是一个尺度, 不能直接平均。

    @return {子标签: 融合分}（z-score 平均）
    """
    def zscores(mapping):
        values = [mapping[name] for name in CLASSES]
        mean = statistics.mean(values)
        spread = statistics.pstdev(values) or 1.0
        return {name: (mapping[name] - mean) / spread for name in CLASSES}

    left, right = zscores(anchor_scores), zscores(text_scores_map)
    return {name: (left[name] + right[name]) / 2.0 for name in CLASSES}


def best_of(scores):
    ranked = sorted(scores.items(), key=lambda item: -item[1])
    top, second = ranked[0], (ranked[1] if len(ranked) > 1 else ("", 0.0))
    return top[0], float(top[1]), float(top[1] - second[1])


def category_of(scores):
    """{子标签: 分} -> {大类: 该类里最高的分}。"""
    out = {}
    for name, value in scores.items():
        category = CATEGORY_OF.get(name)
        if category and (category not in out or value > out[category]):
            out[category] = value
    return out


# ---------------------------------------------------------------------------
#  主流程
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--hypothesis", default="h1", choices=sorted(HYPOTHESES))
    parser.add_argument("--all", action="store_true", help="四种管线全跑（≈5 分钟）")
    parser.add_argument("--limit", type=int, default=0, help="每个类最多几张（0=全部）")
    parser.add_argument("--apply", action="store_true",
                        help="把锚点与起始阈值写进 config/（派生数据, gitignored）")
    parser.add_argument("--json", default="", help="把数字也写一份到这个路径")
    parser.add_argument("--plan", action="store_true", help="只看数据集与管线, 不加载模型")
    args = parser.parse_args()

    if not os.path.isdir(args.dataset):
        print("数据集不在: %s" % args.dataset)
        return 2
    groups = collect(args.dataset)
    if args.limit:
        groups = {name: files[:args.limit] for name, files in groups.items()}
    unknown_classes = sorted(set(groups) - set(CLASSES))
    if unknown_classes:
        print("数据集里有不认识的目录: %s（认得的: %s）"
              % ("、".join(unknown_classes), " / ".join(CLASSES)))
        return 2
    total = sum(len(v) for v in groups.values())
    print("== 数据集 %s: %d 张 / %d 类" % (args.dataset, total, len(groups)))
    for name in CLASSES:
        if name in groups:
            print("   %-6s %2d 张   -> %s" % (name, len(groups[name]), CATEGORY_OF[name]))

    try:
        import cv2
    except ImportError:
        cv2 = None
    try:
        import numpy as np
    except ImportError:
        np = None

    print("\n== 管线对照（同一张图, 四种还原方式）")
    if cv2 is None or np is None:
        print("   ⚠ 这台机器缺 cv2 / numpy（开发机就是这样）—— 管线探测跳过;")
        print("     真正跑标定要在板端（板端 cv2 4.8.0 + numpy 1.24.4 已装）。")
        for key in sorted(HYPOTHESES):
            print("   %s: %s" % (key, HYPOTHESES[key]))
        print("\n--plan: 只看到这里（不加载模型）。")
        return 0
    probe_path = groups[CLASSES[0]][0]
    raw = cv2.imread(probe_path)
    if raw is None:
        print("读不出图片: %s" % probe_path)
        return 2
    print("   原图: %d×%d（%s）" % (raw.shape[1], raw.shape[0], os.path.basename(probe_path)))
    ys, xs = native_points(STREAM_W, STREAM_H)
    print("   native 点采样（%d×%d）取的行号: %s … 共 %d 个不同值"
          % (STREAM_W, STREAM_H, ys[:8], len(set(ys))))
    for key in sorted(HYPOTHESES):
        print("   %s: %s" % (key, HYPOTHESES[key]))
    if args.plan:
        print("\n--plan: 只看到这里（不加载模型）。")
        return 0

    from agent.config import load_config
    from agent.vision.siglip import SiglipModel

    config = load_config("config")
    print("\n== 1) 加载 SigLIP")
    started = time.time()
    model = SiglipModel.from_config(config.get("vision"))
    model.load()
    print("   模型加载 %.1f s（峰值 RSS %.0f MB）" % (time.time() - started, rss_mb()))

    hypotheses = sorted(HYPOTHESES) if args.all else [args.hypothesis]
    report = {"dataset": args.dataset, "counts": {k: len(v) for k, v in groups.items()},
              "stream": [STREAM_W, STREAM_H], "hypotheses": {}, "suggested": {}}

    for hypothesis in hypotheses:
        print("\n" + "=" * 74)
        print("== 管线 %s: %s" % (hypothesis, HYPOTHESES[hypothesis]))
        vectors, labels, paths, frames = [], [], [], []
        encode_s = 0.0
        for name in CLASSES:
            for path in groups.get(name, []):
                raw = cv2.imread(path)
                if raw is None:
                    print("   ⚠ 读不出: %s" % path)
                    continue
                frame = frame_of(raw, hypothesis, cv2)
                started = time.time()
                vector = model.encode_image(frame)
                encode_s += time.time() - started
                vectors.append([float(v) for v in np.asarray(vector).reshape(-1)])
                labels.append(name)
                paths.append(path)
                frames.append(frame)
        print("   编码 %d 张: 共 %.1f s（%.2f s/张，峰值 RSS %.0f MB）"
              % (len(vectors), encode_s, encode_s / max(1, len(vectors)), rss_mb()))

        true_cats = [CATEGORY_OF[name] for name in labels]

        # M1 文本提示词（用的是同一批帧 —— 不重读、不重建, 免得把管线耗时算到文本头上）
        started = time.time()
        text_pairs = [text_scores(model, frame) for frame in frames]
        text_s = time.time() - started
        text_pred = [best_of(scores)[0] for scores in text_pairs]
        text_cat = [best_of(category_of(scores))[0] for scores in text_pairs]
        text_table, text_acc = confusion(labels, text_pred, CLASSES)
        text_err = two_class_errors(true_cats, text_cat)

        # M2 图像锚点（留一法, 逐条 + 类原型）
        started = time.time()
        anchor_holdout = anchor_leave_one_out(vectors, labels, "anchor")
        anchor_s = time.time() - started
        proto_holdout = anchor_leave_one_out(vectors, labels, "prototype")
        anchor_pred = [row[0] for row in anchor_holdout]
        anchor_cat = [CATEGORY_OF.get(row[0], "") for row in anchor_holdout]
        anchor_table, anchor_acc = confusion(labels, anchor_pred, CLASSES)
        anchor_err = two_class_errors(true_cats, anchor_cat)
        proto_pred = [row[0] for row in proto_holdout]
        proto_table, proto_acc = confusion(labels, proto_pred, CLASSES)

        # M3 融合（按子标签分融合）
        fuse_pred = []
        for index, scores in enumerate(text_pairs):
            anchor_scores = {name: 0.0 for name in CLASSES}
            for other, row in enumerate(anchor_holdout):
                if other == index:
                    continue
                cosine = _quick_cosine(vectors[index], vectors[other])
                name = labels[other]
                anchor_scores[name] = max(anchor_scores[name], cosine)
            fuse_pred.append(best_of(fuse(anchor_scores, scores))[0])
        fuse_table, fuse_acc = confusion(labels, fuse_pred, CLASSES)
        fuse_cat = [CATEGORY_OF.get(name, "") for name in fuse_pred]
        fuse_err = two_class_errors(true_cats, fuse_cat)

        # M4 = M2 + 进程名（截图数据集里没有进程信息 -> 只给**上界**）
        m4_cat = [cat if cat else truth for cat, truth in zip(anchor_cat, true_cats)]
        m4_err = two_class_errors(true_cats, m4_cat)

        # M5 = 大类原型 + 相对分（直接回答"像不像学习"）
        rel_rows = category_prototype_holdout(vectors, labels)
        rel_pred = [row[0] for row in rel_rows]
        rel_table, rel_acc = confusion(true_cats, rel_pred, ["study", "not_study"])
        rel_errors = two_class_errors(true_cats, rel_pred)
        rel_study = [row[3] for row, name in zip(rel_rows, labels)
                     if CATEGORY_OF[name] == "study"]
        rel_not = [row[3] for row, name in zip(rel_rows, labels)
                   if CATEGORY_OF[name] == "not_study"]
        sweep = relative_sweep(true_cats, rel_rows)

        print("\n   -- M1 文本提示词（零样本）: 5 类 %.0f%% / 2 类 %.0f%%（编码 %.1f s）"
              % (text_acc * 100, (1 - text_err["nag_rate"] - text_err["miss_rate"]
                                  - text_err["unknown_rate"]) * 100, text_s))
        print_matrix(text_table, CLASSES)
        print("      两个方向: 学习被判成非学习(误打扰) %d / 非学习被判成学习(监督失效) %d / 判不出 %d"
              % (text_err["nag"], text_err["miss"], text_err["unknown"]))
        print("\n   -- M2 图像锚点（留一法, 逐条锚点）: 5 类 %.0f%%（匹配 %.1f s）"
              % (anchor_acc * 100, anchor_s))
        print_matrix(anchor_table, CLASSES)
        print("      两个方向: 误打扰 %d / 监督失效 %d / 判不出 %d（判不出来比例 %.0f%%）"
              % (anchor_err["nag"], anchor_err["miss"], anchor_err["unknown"],
                 anchor_err["unknown_rate"] * 100))
        print("\n   -- M2' 图像锚点（留一法, 类原型）: 5 类 %.0f%%" % (proto_acc * 100))
        print_matrix(proto_table, CLASSES)
        print("\n   -- M3 融合（图像锚点 + 文本, 各自 z-score 后平均）: 5 类 %.0f%%"
              % (fuse_acc * 100))
        print_matrix(fuse_table, CLASSES)
        print("      两个方向: 误打扰 %d / 监督失效 %d / 判不出 %d"
              % (fuse_err["nag"], fuse_err["miss"], fuse_err["unknown"]))
        print("\n   -- M4 图像锚点 + 进程名: **截图数据集上测不了**（数据集里没有「当时 PC 上跑着什么」）;")
        print("      上界（假设进程名总是对且总是有）: 误打扰 %d / 监督失效 %d / 判不出 0"
              % (m4_err["nag"], m4_err["miss"]))

        print("\n   -- M5 大类原型 + 相对分（这才是「像不像学习」的直接打法）: 2 类 %.0f%%"
              % (rel_acc * 100))
        print_matrix(rel_table, ["study", "not_study"])
        print("      两个方向: 误打扰 %d / 监督失效 %d" % (rel_errors["nag"], rel_errors["miss"]))
        if rel_study and rel_not:
            print("      相对分分布: study 最小 %.3f / 中位 %.3f / 最大 %.3f"
                  % (min(rel_study), statistics.median(rel_study), max(rel_study)))
            print("                  not_study 最小 %.3f / 中位 %.3f / 最大 %.3f"
                  % (min(rel_not), statistics.median(rel_not), max(rel_not)))
            separable = min(rel_study) > max(rel_not)
            print("      两个大类在相对分上%s（study 的最小 %.3f vs not_study 的最大 %.3f）"
                  % ("**分得开**" if separable else "**仍有重叠**",
                     min(rel_study), max(rel_not)))
        print("      没把握带（|相对分| < band 就判不出来）的取舍:")
        print("        %-8s %-10s %-10s %-10s %-10s" % ("band", "准确率", "误打扰", "监督失效", "判不出"))
        for row in sweep:
            print("        %-8.2f %-10s %-10d %-10d %-10d（%.0f%%）"
                  % (row["band"], "%.0f%%" % (row["accuracy"] * 100), row["nag"], row["miss"],
                     row["unknown"], row["unknown_rate"] * 100))

        # 分数分布 -> 起始阈值
        study_scores = [row[1] for row, label in zip(anchor_holdout, labels)
                        if CATEGORY_OF[label] == "study"]
        not_scores = [row[1] for row, label in zip(anchor_holdout, labels)
                      if CATEGORY_OF[label] == "not_study"]
        study_margins = [row[2] for row, label in zip(anchor_holdout, labels)
                         if CATEGORY_OF[label] == "study"]
        not_margins = [row[2] for row, label in zip(anchor_holdout, labels)
                       if CATEGORY_OF[label] == "not_study"]
        suggest_score = clamp(round(0.5 * ((quantile(study_scores, 0.05) or 0.0)
                                          + (quantile(not_scores, 0.95) or 0.0)), 2), 0.55, 0.95)
        suggest_margin = clamp(round(0.5 * ((quantile(study_margins, 0.05) or 0.0)
                                           + (quantile(not_margins, 0.95) or 0.0)), 2), 0.01, 0.20)
        print("\n   -- 分数分布（生产口径: 大类分 / 大类余量, 留一法）")
        for name, values in (("study", study_scores), ("not_study", not_scores)):
            if values:
                print("      %-10s 大类分 最小 %.3f / 5%% %.3f / 中位 %.3f / 95%% %.3f / 最大 %.3f"
                      % (name, min(values), quantile(values, 0.05), statistics.median(values),
                         quantile(values, 0.95), max(values)))
        for name, values in (("study", study_margins), ("not_study", not_margins)):
            if values:
                print("      %-10s 大类余量 最小 %.3f / 中位 %.3f / 最大 %.3f"
                      % (name, min(values), statistics.median(values), max(values)))
        decided = [cat if (row[1] >= suggest_score and row[2] >= suggest_margin) else ""
                   for row, cat in zip(anchor_holdout, anchor_cat)]
        at_threshold = two_class_errors(true_cats, decided)
        print("      => 起始阈值建议: confident_score=%.2f, confident_margin=%.2f"
              % (suggest_score, suggest_margin))
        print("         在这个初值上: 误打扰 %d / 监督失效 %d / 判不出 %d（%.0f%%）"
              % (at_threshold["nag"], at_threshold["miss"], at_threshold["unknown"],
                 at_threshold["unknown_rate"] * 100))

        report["hypotheses"][hypothesis] = {
            "m1_text": {"acc5": round(text_acc, 4), "errors": text_err},
            "m2_anchor": {"acc5": round(anchor_acc, 4), "errors": anchor_err},
            "m2_prototype": {"acc5": round(proto_acc, 4)},
            "m3_fusion": {"acc5": round(fuse_acc, 4), "errors": fuse_err},
            "m4_upper_bound": m4_err,
            "m5_relative": {"acc2": round(rel_acc, 4), "errors": rel_errors,
                            "study_relative": [round(v, 4) for v in rel_study],
                            "not_study_relative": [round(v, 4) for v in rel_not],
                            "bands": sweep},
            "scores": {"study": study_scores, "not_study": not_scores,
                       "study_margin": study_margins, "not_margin": not_margins},
            "suggested": {"confident_score": suggest_score,
                          "confident_margin": suggest_margin,
                          "at_threshold": at_threshold},
            "seconds": {"encode_each": round(encode_s / max(1, len(vectors)), 3),
                        "text": round(text_s, 2), "anchor_match": round(anchor_s, 3)},
        }
        if hypothesis == args.hypothesis:
            report["suggested"] = report["hypotheses"][hypothesis]["suggested"]

    if args.all and len(hypotheses) > 1:
        print("\n" + "=" * 74)
        print("== 四种管线对照（2 类准确率 = 1 - 误打扰率 - 监督失效率 - 判不出率）")
        print("   %-6s %-12s %-12s %-12s %-16s" % ("管线", "M1 文本", "M2 锚点", "M3 融合",
                                                   "M5 大类原型"))
        for key in hypotheses:
            row = report["hypotheses"][key]

            def acc_of(entry):
                errors = entry["errors"]
                return 1 - errors["nag_rate"] - errors["miss_rate"] - errors["unknown_rate"]
            print("   %-6s %-12s %-12s %-12s %-16s"
                  % (key,
                     "%.0f%% (5类%.0f%%)" % (acc_of(row["m1_text"]) * 100,
                                             row["m1_text"]["acc5"] * 100),
                     "%.0f%% (5类%.0f%%)" % (acc_of(row["m2_anchor"]) * 100,
                                             row["m2_anchor"]["acc5"] * 100),
                     "%.0f%% (5类%.0f%%)" % (acc_of(row["m3_fusion"]) * 100,
                                             row["m3_fusion"]["acc5"] * 100),
                     "%.0f%% (误打扰 %d / 失效 %d)"
                     % (row["m5_relative"]["acc2"] * 100,
                        row["m5_relative"]["errors"]["nag"],
                        row["m5_relative"]["errors"]["miss"])))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
        print("\n数字也写了一份: %s" % args.json)

    if args.apply:
        _apply(args, groups, cv2, model, report)

    print("\n峰值 RSS: %.0f MB" % rss_mb())
    print("⚠ 39 张样本（doc/real/anime 各 4–5 张）只够定**初值**; 收敛靠运行期的带标签样本。")
    return 0


def _quick_cosine(left, right):
    from agent.core.study_anchors import cosine

    return cosine(left, right)


def _apply(args, groups, cv2, model, report):
    """把锚点与起始阈值落到 config/（都是 gitignored 的派生数据）。"""
    import numpy as np

    from agent.core.study_anchors import StudyAnchors
    from agent.core.study_stats import StudyStats

    hypothesis = args.hypothesis
    suggested = report["suggested"] or {}
    relative = (report["hypotheses"].get(hypothesis, {}).get("m5_relative") or {})
    bands = relative.get("bands") or []
    # 选"没把握带"的口径 —— 这是**人的取舍**, 写死在这里免得每次跑出不同答案:
    #   ① 先保证**一次都不误打扰**（把学习判成非学习会弹气泡/弹桌面, 这是最不能忍的）;
    #   ② 在满足①的 band 里要求 监督失效 ≤ 1 且 判不出 ≤ 10%（这两个都算"监督没起作用"）;
    #   ③ 取满足②的**最小** band（判得多一点、少一点"判不出来"）。
    #   一个都没有 -> 回退到"误打扰=0 里失效最少"的那个。
    band = None
    safe = [row for row in bands if row["nag"] == 0]
    pick = [row for row in safe if row["miss"] <= 1 and row["unknown_rate"] <= 0.10]
    if pick:
        band = min(row["band"] for row in pick)
    elif safe:
        fewest = min(row["miss"] for row in safe)
        band = min(row["band"] for row in safe if row["miss"] == fewest)

    anchors = StudyAnchors(keep_shots=False)
    try:
        anchors.load()                                       # ⚠ 先 load: reset() 才清得干净
    except Exception as exc:                                 # noqa: BLE001
        print("   ⚠ 旧锚点库读不了（照样清空重播）: %s" % exc)
    removed = anchors.reset()
    added = 0
    for name in CLASSES:
        for path in groups.get(name, []):
            raw = cv2.imread(path)
            if raw is None:
                continue
            vector = [float(v) for v in np.asarray(
                model.encode_image(frame_of(raw, hypothesis, cv2))).reshape(-1)]
            anchors.add(cls=name, vector=vector, source="calib",
                        note="T13-4 标定 / %s / %s" % (hypothesis, os.path.basename(path)))
            added += 1
    stats = StudyStats()
    stats.load()
    for key, value in (("confident_score", suggested.get("confident_score")),
                       ("confident_margin", suggested.get("confident_margin")),
                       ("relative_band", band)):
        if value is not None:
            stats.set_threshold(key, value)
    stats.set_threshold("learn_score", 0.90)
    stats.note("T13-4 标定: %d 张截图 / 管线 %s（清掉了旧的 %d 条锚点）; "
               "相对分带 %s（口径: 先保证误打扰=0, 再取监督失效最少的那个 band）"
               % (added, hypothesis, removed, band))
    stats.bump("calib_images", added)
    stats.save()
    print("\n== --apply 落盘")
    print("   锚点: %d 条 -> %s（%s）" % (added, anchors.path, anchors.counts()))
    print("   起始阈值: %s -> %s" % (stats.thresholds(), stats.path))
    if band is not None:
        print("   ⚠ 绝对阈值那一对（confident_score/margin）**在这套数据上不好用**"
              "（两个大类的绝对分重叠, 会判出 60%+ 的「判不出来」）；")
        print("     真正该用的是大类原型 + **相对分带** %.2f。T13-5 按哪条规则接, 等你定。" % band)
    print("\n   贴进 config.yaml 的 study 段可以是:")
    print("     study:")
    print("       enabled: true")
    print("       relative_band: %.2f" % (band if band is not None else 0.05))
    print("       confident_score: %.2f" % (suggested.get("confident_score") or 0.80))
    print("       confident_margin: %.2f" % (suggested.get("confident_margin") or 0.03))
    print("       focus_interval_min: 30")
    print("       recheck_interval_min: 5")
    print("       max_failures: 3")
    print("       cooldown_min: 30")


if __name__ == "__main__":
    sys.exit(main())
