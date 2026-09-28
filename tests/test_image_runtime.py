#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_image_runtime.py — 运行时依赖清单守卫（T15-2-7，R8）

R8 说的是"仓库没有 Python 依赖声明"。T15-2-7 的答案在
`image/check-runtime-deps.py` 的 `PY_MODULES`：把仓库里**真实出现过**的第三方
import 逐个定性（required / optional / host）。

这个文件把那件事反过来钉住：**用 AST 扫仓库的真实 import，凡是没被定性的就报红**。
于是"有人加了个新库、镜像里却没有"这种漏项，会在 CI 里当场暴露，
而不是等到板上 `ImportError` 才发现。

同时也守住"清单不许自相矛盾"：
  · required 的模块必须在 `PY_IN_IMAGE` 里有落点（否则 check-runtime-deps 不知道去哪找）；
  · host / optional 的模块**不该**出现在 `PY_IN_IMAGE` 里（镜像里本来就没有它们）。
"""
from __future__ import annotations

import ast
import importlib.util
import os
import pathlib
import sys
import sysconfig
import unittest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHECKER = _ROOT / "image" / "check-runtime-deps.py"
SCAN_DIRS = ("agent", "scripts", "tests", "gui/tools")
#: 仓库自己的包/模块，不算第三方
LOCAL = {"agent", "config", "tools", "io", "core", "ui", "pages", "state",
         "llm", "vision", "tests", "scripts", "native", "gui", "widgets"}
_LOCAL_NAMES = None


def is_stdlib(name: str) -> bool:
    """是不是标准库。

    ⚠ 不能只靠 `sys.stdlib_module_names`：那是 **3.10+** 才有的，
    CI 跑的是 **3.8**（与板端解释器同版本）——只有它的话，3.8 上会把
    `argparse`/`asyncio` 这些全判成"第三方"，用例直接红（第一次就是这么红的）。
    所以再补一条跨版本可靠的判据：找到模块的位置，看它在不在 stdlib 目录里。
    """
    if name in sys.builtin_module_names:
        return True
    if name in getattr(sys, "stdlib_module_names", ()):
        return True
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError):
        return False
    if spec is None:
        return False
    if spec.origin in (None, "built-in", "frozen"):
        return True
    origin = str(spec.origin)
    if "site-packages" in origin or "dist-packages" in origin:
        return False
    stdlib_dir = sysconfig.get_paths().get("stdlib", "")
    return bool(stdlib_dir) and origin.startswith(stdlib_dir)


def is_local(name: str) -> bool:
    """名字在不在仓库里（同名 .py / 包目录）——用来把 `siglip`、`pair_sunshine`
    这类"仓库内部的包/脚本"和真正的第三方库区分开。

    ⚠ 用**一次性剪枝遍历**算出来（第一版每问一次就 rglob 两遍，仓库里
    conda/ 与 native/third_party/ 几十万个文件，直接把用例跑超时了）。
    """
    global _LOCAL_NAMES
    if _LOCAL_NAMES is None:
        _LOCAL_NAMES = set(LOCAL)
        skip_dirs = {"build", "build-host", "build-rk3568", "conda", "temp",
                     ".git", "__pycache__", "node_modules"}
        for root, dirs, files in os.walk(_ROOT):
            dirs[:] = [d for d in dirs
                       if d not in skip_dirs and "third_party" not in d]
            for f in files:
                if f.endswith(".py"):
                    _LOCAL_NAMES.add(f[:-3])
            if "__init__.py" in files:
                _LOCAL_NAMES.add(os.path.basename(root))
    return name in _LOCAL_NAMES


def load_checker():
    spec = importlib.util.spec_from_file_location("check_runtime_deps", str(_CHECKER))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def repo_third_party() -> dict:
    """AST 扫出仓库真实 import 的第三方顶层模块 -> 出现的文件集合。"""
    found = {}
    for top in SCAN_DIRS:
        for p in (_ROOT / top).rglob("*.py"):
            try:
                tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
            except (SyntaxError, OSError):
                continue
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    if node.level or not node.module:
                        continue
                    names = [node.module.split(".")[0]]
                for n in names:
                    if is_local(n) or is_stdlib(n):
                        continue
                    found.setdefault(n, set()).add(str(p.relative_to(_ROOT)))
    return found


class TestDependencyManifest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_checker()
        cls.found = repo_third_party()

    def test_checker_loads_and_has_tables(self):
        self.assertTrue(self.mod.PY_MODULES, "PY_MODULES 不该是空的")
        self.assertTrue(self.mod.MANIFEST, "MANIFEST 不该是空的")

    def test_every_real_import_is_classified(self):
        """仓库里出现的每个第三方模块都必须在 PY_MODULES 里被定性。

        这是 R8 的核心守卫：新增 import 而没定性 = CI 红。
        """
        unclassified = sorted(m for m in self.found if m not in self.mod.PY_MODULES)
        detail = {m: sorted(self.found[m])[:3] for m in unclassified}
        self.assertEqual(unclassified, [],
                         "这些第三方模块没在 image/check-runtime-deps.py 的 PY_MODULES 里定性：%r"
                         % detail)

    def test_classification_values_are_known(self):
        for mod, entry in self.mod.PY_MODULES.items():
            self.assertEqual(len(entry), 3, "%s 的定性应该是三元组" % mod)
            cls, where, why = entry
            self.assertIn(cls, ("required", "optional", "host"), "%s 的类别不对" % mod)
            self.assertTrue(where and why, "%s 的落点/说明不该为空" % mod)

    def test_required_modules_have_an_image_location(self):
        for mod, (cls, _where, _why) in self.mod.PY_MODULES.items():
            if cls == "required":
                self.assertIn(mod, self.mod.PY_IN_IMAGE,
                              "required 的 %s 必须在 PY_IN_IMAGE 里有落点" % mod)

    def test_host_and_optional_modules_are_not_expected_in_the_image(self):
        for mod, (cls, _where, _why) in self.mod.PY_MODULES.items():
            if cls in ("host", "optional"):
                self.assertNotIn(mod, self.mod.PY_IN_IMAGE,
                                 "%s 是 %s，不该出现在 PY_IN_IMAGE 里" % (mod, cls))

    def test_manifest_paths_are_relative_and_sane(self):
        for kind, rel, why in self.mod.MANIFEST:
            self.assertFalse(rel.startswith("/"), "MANIFEST 路径应为 target 相对路径: %s" % rel)
            self.assertTrue(why, "%s 缺少 why（为什么要它）" % rel)
            self.assertIn(kind, ("bin", "lib", "gst", "plugin", "qml", "font", "llm"),
                          "未知的类别: %s" % kind)

    def test_llama_runtime_is_in_the_manifest(self):
        """llama-server 与起停脚本是 T15-2-7 的出口之一，别被删掉。"""
        names = [rel for _k, rel, _w in self.mod.MANIFEST]
        self.assertTrue(any(n.endswith("llm/bin/llama-server") for n in names),
                        "MANIFEST 里必须盯着 llama-server")
        for s in ("start.sh", "stop.sh"):
            self.assertTrue(any(n.endswith("llm/scripts/" + s) for n in names),
                            "MANIFEST 里必须盯着 llm/scripts/" + s)


if __name__ == "__main__":
    unittest.main(verbosity=2)
