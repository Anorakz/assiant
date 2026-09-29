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
import tempfile
import unittest
from unittest import mock

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


class TestPayloadIsItsOwnCategory(unittest.TestCase):
    """T15-2-10：payload（agent/GUI/native/默认配置）必须与 MANIFEST 分开看。

    为什么单独一类：MANIFEST 缺了 = 构建/配方错了（必须修）；payload 缺了 =
    **unit 文件已经指向它、但东西还没做**（docs/image.md §5.6 末段写明留给后面）。
    前者让构建红，后者让构建**照样绿**、板子起来之后服务反复重启 ——
    正是刷板前验证必须看见、而普通"构建成功"看不见的那一类。
    """

    @classmethod
    def setUpClass(cls):
        cls.mod = load_checker()

    def test_payload_paths_live_under_the_assistant_root(self):
        self.assertTrue(self.mod.PAYLOAD, "PAYLOAD 不该是空的")
        for kind, rel, why in self.mod.PAYLOAD:
            self.assertFalse(rel.startswith("/"), "应为 target 相对路径: %s" % rel)
            self.assertTrue(rel.startswith("usr/lib/assistant/"),
                            "payload 都在 /usr/lib/assistant 下（D7：代码进 rootfs）: %s" % rel)
            self.assertTrue(why, "%s 缺少 why" % rel)
            self.assertIn(kind, ("agent", "gui", "native", "config", "doc"),
                          "未知的 payload 类别: %s" % kind)

    def test_payload_and_manifest_do_not_overlap(self):
        """同一个路径不该既算"必须有"又算"还没做"——那是自相矛盾的清单。"""
        man = {rel for _k, rel, _w in self.mod.MANIFEST}
        pay = {rel for _k, rel, _w in self.mod.PAYLOAD}
        self.assertEqual(man & pay, set())

    def test_units_point_at_paths_that_are_watched(self):
        """unit 里 ExecStart / Documentation 指向的路径必须都在清单里被盯着。

        （这就是为什么 payload 表里连 docs/*.md 都有：unit 的 Documentation=
        指向它们。清单与 unit 对不上的话，改名只会静默漂。）
        """
        pay = {rel for _k, rel, _w in self.mod.PAYLOAD}
        for rel in ("usr/lib/assistant/agent/main.py",
                    "usr/lib/assistant/gui/agent_gui",
                    "usr/lib/assistant/Readme.md",
                    "usr/lib/assistant/docs/gui.md",
                    "usr/lib/assistant/docs/image.md",
                    "usr/lib/assistant/config/config.example.yaml",
                    "usr/lib/assistant/config/user_profile.example.yaml"):
            self.assertIn(rel, pay)


class TestWhichTargetTreeTheCheckersLookAt(unittest.TestCase):
    """T15-2-10 的根因守卫：**整机构建与单包构建是两棵树**，只差一层同名目录。

    T15-2-9 整机镜像里漏掉 rknnlite，就是因为检查器（和 post-build 调的那个脚本）
    都看/写单包那棵树。`image/imagelib.py` 把选树收在一处，这里钉住优先级：

        --target  >  IMG_TARGET  >  整机构建树（update.img 的来源）  >  单包树
    """

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(_ROOT / "image"))
        import imagelib
        cls.lib = imagelib

    def _sdk(self, tmp, trees):
        """造一棵假 SDK：trees 是 ("integrated"|"single",) 的组合。"""
        sdk = pathlib.Path(tmp)
        c = "test_cfg"
        base = sdk / "buildroot" / "output"
        for kind in trees:
            p = (base / c / c / "target" if kind == "integrated" else base / c / "target")
            (p / "usr/lib").mkdir(parents=True)
        return sdk

    def test_integrated_wins_when_both_exist(self):
        with tempfile.TemporaryDirectory() as t:
            with mock.patch.dict(os.environ, {"IMG_CFG": "test_cfg"}, clear=False):
                os.environ.pop("IMG_TARGET", None)
                sdk = self._sdk(t, ("single", "integrated"))
                target, why = self.lib.resolve_target(sdk)
                self.assertTrue(target)
                self.assertIn("整机构建", why)
                self.assertEqual(target.parts[-2:], ("test_cfg", "target"))
                self.assertEqual(target.parts.count("test_cfg"), 2,
                                 "选中的必须是**嵌一层**那棵树")

    def test_single_is_the_fallback(self):
        with tempfile.TemporaryDirectory() as t:
            with mock.patch.dict(os.environ, {"IMG_CFG": "test_cfg"}, clear=False):
                os.environ.pop("IMG_TARGET", None)
                sdk = self._sdk(t, ("single",))
                target, why = self.lib.resolve_target(sdk)
                self.assertEqual(target.parts.count("test_cfg"), 1)
                self.assertIn("单包", why)

    def test_explicit_target_wins_and_must_exist(self):
        with tempfile.TemporaryDirectory() as t:
            with mock.patch.dict(os.environ, {"IMG_CFG": "test_cfg"}, clear=False):
                sdk = self._sdk(t, ("single", "integrated"))
                other = pathlib.Path(t) / "elsewhere"
                other.mkdir()
                target, why = self.lib.resolve_target(sdk, str(other))
                self.assertEqual(target, other)
                self.assertIn("--target", why)
                target, why = self.lib.resolve_target(sdk, str(pathlib.Path(t) / "nope"))
                self.assertIsNone(target)
                self.assertIn("不存在", why)

    def test_img_target_env_is_honoured(self):
        with tempfile.TemporaryDirectory() as t:
            with mock.patch.dict(os.environ, {"IMG_CFG": "test_cfg"}, clear=False):
                sdk = self._sdk(t, ("single", "integrated"))
                other = pathlib.Path(t) / "from_env"
                other.mkdir()
                with mock.patch.dict(os.environ, {"IMG_TARGET": str(other)}):
                    target, why = self.lib.resolve_target(sdk)
                self.assertEqual(target, other)
                self.assertIn("IMG_TARGET", why)

    def test_missing_trees_says_which_paths_it_looked_at(self):
        with tempfile.TemporaryDirectory() as t:
            with mock.patch.dict(os.environ, {"IMG_CFG": "test_cfg"}, clear=False):
                os.environ.pop("IMG_TARGET", None)
                target, why = self.lib.resolve_target(pathlib.Path(t))
                self.assertIsNone(target)
                self.assertIn("target", why)


class TestPostBuildFeedsTheRightTree(unittest.TestCase):
    """T15-2-10：post-build 必须把 `$TARGET_DIR` 传给 prepare-rknnlite.sh。

    那一次的实际后果：不传 → 脚本自己算落点 → 算成单包那棵 → 整机镜像里没有
    rknnlite，而构建**全绿**（脚本还因为旧树里有 T15-2-7 的戳而打印"已经一致"）。
    这两个文件是跨文件不变量，缺一边这个 bug 就会回来。
    """

    @classmethod
    def setUpClass(cls):
        cls.post = (_ROOT / "image" / "board" / "rockchip" / "kickpi" / "k1mini"
                    / "post-build.sh").read_text(encoding="utf-8")
        cls.rknn = (_ROOT / "image" / "prepare-rknnlite.sh").read_text(encoding="utf-8")

    def test_post_build_passes_target_dir(self):
        self.assertIn('--target "$TARGET_DIR"', self.post,
                      "post-build 调 prepare-rknnlite.sh 时必须传 --target \"$TARGET_DIR\"")

    def test_rknnlite_script_accepts_target(self):
        self.assertIn("--target", self.rknn)
        self.assertIn("TARGET_DIR", self.rknn, "还要认 buildroot 导出的 TARGET_DIR")

    def test_rknnlite_script_no_longer_derives_the_tree_from_the_build_dir(self):
        """第一版用 `$OUT/build/python3-*` 推 python 版本 —— 那正是在算错误的树。

        现在 python 版本从 **target 树自己**（usr/lib/python3.x）看。
        """
        self.assertNotIn("/build/python3-", self.rknn)
        self.assertIn('"$TARGET"/usr/lib/python3.', self.rknn)

    def test_installer_ships_imagelib(self):
        """两个检查器都 `import imagelib`；只拷检查器不拷它 = SDK 里直接 ImportError。"""
        install = (_ROOT / "image" / "install-into-sdk.sh").read_text(encoding="utf-8")
        self.assertIn("image/imagelib.py tools/assistant/imagelib.py", install)
        self.assertIn("image/preflash-check.sh tools/assistant/preflash-check.sh", install)


if __name__ == "__main__":
    unittest.main(verbosity=2)
