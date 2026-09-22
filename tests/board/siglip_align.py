#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/board/siglip_align.py — 搬运对齐测试（T7-1 的验收证据，**板端**跑）

守的是什么
    SigLIP 实现是从板端实验树 `sig/siglip/` 搬进仓库的（`agent/vision/siglip/`）。
    搬运"没搬坏"必须能证明，而不是靠读代码。做法：**同一张图、同一个模型**分别过
    两条路径，比对
      · image embedding 的余弦（必须 >= 0.999）
      · 零样本排序是否完全一致（同一批候选文本）
      · 数值是否逐位相同（同模型同时序时应当完全相同）

跑法（板端，仓库根目录下）
    python3 tests/board/siglip_align.py
    python3 tests/board/siglip_align.py --images a.png b.png   # 指定图片

退出码: 0 = 对齐; 1 = 没对齐（会打印差异）; 2 = 环境不满足（缺模型/依赖）—— 这时**不算失败**，
        但也不算通过（脚本会说明缺什么）。
"""

from __future__ import annotations

import argparse
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

#: 默认拿三张**比例差别很大**的图：横、竖、极端（都在你的壁纸目录里）
DEFAULT_IMAGES = [
    "/home/kickpi/wallpapers/01_landscape_1280x800.png",
    "/home/kickpi/wallpapers/02_portrait_800x1280.png",
    "/home/kickpi/wallpapers/06_extreme_600x1200.png",
]

#: 零样本排序用的候选文本（顺序必须两边一致）
CAPTIONS = [
    "a blue landscape", "a dark red plain wall", "a green background",
    "a photo of a bus on a street", "a cat sleeping on a sofa",
]


def vision_section():
    """从仓库配置里取 `vision:` 段（与生产同一条路径）。

    ⚠ live 里没有这一段时退回模板 —— 用 yaml 直接读，因为
      `agent.config.load_config()` 只认白名单里的名字（config / user_profile）。
    """
    import agent.config as agent_config
    import yaml

    live = os.path.join(_ROOT, "config", "config.yaml")
    if os.path.isfile(live):
        try:
            data = agent_config.load_config("config")
        except Exception:                                  # noqa: BLE001
            data = None
        section = data.get("vision") if isinstance(data, dict) else None
        if isinstance(section, dict):
            return section

    example = os.path.join(_ROOT, "config", "config.example.yaml")
    if os.path.isfile(example):
        try:
            with open(example, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
        except Exception:                                  # noqa: BLE001
            data = None
        section = data.get("vision") if isinstance(data, dict) else None
        if isinstance(section, dict):
            return section
    return {}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="SigLIP 搬运对齐测试（板端）")
    parser.add_argument("--images", nargs="*", default=None, help="要比对的图片路径")
    parser.add_argument("--repo-model", default=None, help="覆盖仓库版的模型路径")
    args = parser.parse_args(argv)

    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        print("环境不满足: %s（板端才有 cv2/numpy）" % exc)
        return 2

    section = vision_section()
    if args.repo_model:
        section = dict(section, model_path=args.repo_model)

    # ---- 仓库版 ----
    from agent.vision.siglip import IMAGE_SIZE, SiglipModel

    try:
        repo = SiglipModel.from_config(section)
        repo.load()
    except Exception as exc:                                # noqa: BLE001
        print("环境不满足: 仓库版加载失败: %r" % (exc,))
        return 2

    # ---- 实验树原版（板端本地，没搬进仓库）----
    sig_root = os.path.join(_ROOT, "sig")
    if sig_root not in sys.path:
        sys.path.insert(0, sig_root)
    try:
        from siglip import SiglipModel as UpstreamModel          # type: ignore

        upstream = UpstreamModel.from_env(os.path.join(sig_root, "config", "sig.env"))
        upstream.load()
    except Exception as exc:                                # noqa: BLE001
        repo.close()
        print("环境不满足: 实验树 sig/ 原版加载失败: %r" % (exc,))
        print("（对齐测试需要 sig/ 与它的 sig.env —— 那是板端本地实验树）")
        return 2

    images = [p for p in (args.images or DEFAULT_IMAGES)]
    print("仓库版: %s" % repo.config.describe())
    print("原  版: %s" % upstream.config.describe())
    print()
    print("%-46s %-12s %-12s %s" % ("图片", "余弦(仓库,原版)", "最大逐位差", "排序一致"))

    worst = 1.0
    all_sorted = True
    checked = 0
    for path in images:
        if not os.path.exists(path):
            print("%-46s %s" % (os.path.basename(path), "跳过（文件不在）"))
            continue
        raw = cv2.imread(path)
        if raw is None:
            print("%-46s %s" % (os.path.basename(path), "跳过（读不出来）"))
            continue
        # 官方口径：直接 resize 到 256×256（不保比例）+ BGR→RGB + 原始 0-255
        img = cv2.resize(cv2.cvtColor(raw, cv2.COLOR_BGR2RGB), (IMAGE_SIZE, IMAGE_SIZE))

        a = np.asarray(repo.encode_image(img), dtype=np.float32)
        b = np.asarray(upstream.encode_image(img), dtype=np.float32)
        cos = float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12))
        diff = float(np.abs(a - b).max())

        ra = [t for t, _ in repo.rank(img, CAPTIONS)]
        rb = [t for t, _ in upstream.rank(img, CAPTIONS)]
        same = ra == rb
        all_sorted = all_sorted and same
        worst = min(worst, cos)
        checked += 1
        print("%-46s %-12.6f %-12.2e %s" % (os.path.basename(path)[:44], cos, diff,
                                            "是" if same else "**否**"))

    # 文本塔也要对：同一句话的 embedding
    ta = np.asarray(repo.encode_text("a mountain landscape"), dtype=np.float32)
    tb = np.asarray(upstream.encode_text("a mountain landscape"), dtype=np.float32)
    text_cos = float(np.dot(ta, tb) / (np.linalg.norm(ta) * np.linalg.norm(tb) + 1e-12))

    repo.close()
    upstream.close()

    print()
    print("文本塔余弦 = %.6f" % text_cos)
    print("检查了 %d 张图；最差图像余弦 = %.6f" % (checked, worst))
    ok = checked > 0 and worst >= 0.999 and text_cos >= 0.999 and all_sorted
    print("结论: %s" % ("对齐（搬运没有改变行为）" if ok else "**没对齐**，看上面的数字"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
