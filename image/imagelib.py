#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""image/imagelib.py — 两个检查器共用的"要看哪棵 target 树"（T15-2-10）

为什么需要它（这一节就是 T15-2-10 抓到的 bug）
---------------------------------------------------------------------------
整机构建（`./build.sh all`）与单包构建（`image/sdk-make.sh`）用的是**两棵不同的
buildroot output 树**，而它们只差一层同名目录（`docs/image.md` §5.8）：

    buildroot/output/<CFG>/target            ← 单包构建（2-5/2-6/2-7 那棵）
    buildroot/output/<CFG>/<CFG>/target      ← 整机构建（**update.img 的来源**）

两个检查器当初都按**单包那棵**写死了路径。于是 T15-2-9 整机构建成功之后，
"刷板前验证"看到的其实是另一棵树 —— 真问题（`prepare-rknnlite.sh` 把 wheel 装进了
单包树，整机镜像里根本没有 `rknnlite`）就这么从眼皮底下过去了。
所以这里把"选树"这件事收成一处，两个检查器都调它，并**把选中的是哪棵打印出来**。

优先级（越靠前越优先）
---------------------------------------------------------------------------
    1) 命令行 `--target <目录>`
    2) 环境变量 `IMG_TARGET`
    3) `buildroot/output/<CFG>/<CFG>/target`（整机构建 —— update.img 的来源）
    4) `buildroot/output/<CFG>/target`（单包构建）

第 3 条优先于第 4 条是刻意的：**验收要盯着真正会被烧进板子的那棵树**。
"""
from __future__ import annotations

import os
from pathlib import Path

CFG_DEFAULT = "rockchip_rk3568_kickpi_k1mini_release"

#: 树的种类标记（打印时会带上，避免又看错树）
INTEGRATED = "integrated"
SINGLE = "single"


def cfg() -> str:
    """buildroot 的 output 配置名（可由 IMG_CFG 覆盖，与影像脚本一致）。"""
    return os.environ.get("IMG_CFG", CFG_DEFAULT)


def candidates(sdk) -> list:
    """按优先级返回 [(target 目录, 种类)]（不判断是否存在）。"""
    c = cfg()
    base = Path(sdk) / "buildroot" / "output"
    return [(base / c / c / "target", INTEGRATED), (base / c / "target", SINGLE)]


def kind_label(kind: str) -> str:
    return ("整机构建树（update.img 的来源）" if kind == INTEGRATED
            else "单包构建树（**不是** update.img 的来源）")


def resolve_target(sdk, explicit=None):
    """选定 target 树。返回 (Path|None, 说明文字)。

    找不到时返回 (None, 原因)，由调用方决定怎么报错 —— 这里不抛异常，
    是因为两个检查器的出错话术不一样。
    """
    if explicit:
        p = Path(explicit)
        if p.is_dir():
            return p, "--target 指定"
        return None, "--target 指定的目录不存在：%s" % p

    env = os.environ.get("IMG_TARGET")
    if env:
        p = Path(env)
        if p.is_dir():
            return p, "IMG_TARGET 指定"
        return None, "IMG_TARGET 指向的目录不存在：%s" % p

    found = [(p, k) for p, k in candidates(sdk) if p.is_dir()]
    if found:
        p, k = found[0]
        return p, kind_label(k)

    paths = "、".join(str(p) for p, _k in candidates(sdk))
    return None, "两棵 output 树都没有 target 目录（%s）" % paths


def site_packages(target: Path) -> Path:
    """target 里的 site-packages（按实际 python3.x 目录名找，不写死版本）。"""
    for p in sorted(Path(target).glob("usr/lib/python3.*/site-packages")):
        return p
    return Path(target) / "usr/lib/python3.11/site-packages"
