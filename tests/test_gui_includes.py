#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_gui_includes.py — GUI 源码守卫：`#include "…"` 必须能解析到真实文件

跑法:
    python tests/test_gui_includes.py

为什么要有这个守卫（T14-7b 的真实教训）
    T14-3 删掉了 `gui/src/core/config_sync.{h,cpp}`，但 `gui/src/ui/model_page.cpp`
    里那行 `#include "core/config_sync.h"` 忘了删。后果**非常安静**：

      · PC 上不编译 GUI（PC 没有 Qt），所以这个错在 PC 侧永远不会暴露；
      · 板端 `cmake --build` 确实失败了，但 `agent_gui` 与各测试二进制的**旧文件还在**，
        于是 ctest 照样能跑出一串绿 —— 那里面甚至混着已经退役的 `test_config_sync`；
      · 结果：GUI 侧改了一个多月（T14-3 的"单一写入者"），板端跑的却一直是**旧二进制**。

    这类错误的共同点是"没有任何测试会红"。所以这里把"include 能不能解析"变成
    跑得出来的检查 —— 它不需要 Qt，任何机器上都能跑。

检查范围与规则
    · 扫 `gui/src/**` 与 `gui/tests/**` 的 `.cpp/.h`；
    · 只查**引号形式**的 include（`<QWidget>` 这种是系统/第三方头，解析不了也不该管）；
    · 解析顺序按编译器那套的简化版：先看包含者自己所在目录，再看 CMake 给 GUI 的
      头目录（`gui/src`、`gui/tests`）与仓库根；
    · 只在**仓库里**找不到时报错 —— 指向构建目录里生成物（`*_autogen/`、`ui_*.h`、
      `moc_*.cpp`）的 include 一律跳过（那类文件本来就不在源码树里）。

不检查（刻意的）
  · 不检查 Qt 头是否正确、不检查编译能否通过 —— 那要在板端 `cmake --build`（见
    `docs/deploy.md` §9 与 `scripts/sync-gui.ps1 -Test`）。这个文件只保证"源码树自洽"。
"""

import re
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
GUI_DIR = _PROJECT_ROOT / "gui"
SCAN_DIRS = [GUI_DIR / "src", GUI_DIR / "tests"]

#: 头文件搜索路径（简化版；与 gui/CMakeLists.txt 的 target_include_directories 对齐）
INCLUDE_DIRS = [GUI_DIR / "src", GUI_DIR / "tests", _PROJECT_ROOT]

#: `#include "..."`（只此一种；尖括号形式不查）
_INCLUDE_RE = re.compile(r'^\s*#\s*include\s+"([^"]+)"')

#: 构建期生成的头/源（不在源码树里，跳过）。`test_x.moc` 是 CMake AUTOMOC 生成的。
_GENERATED_RE = re.compile(
    r"(^|/)(ui_[^/]+\.h|moc_[^/]+\.cpp|qrc_[^/]+\.cpp|[^/]+\.moc|.*_autogen/.*)$")


def gui_source_files():
    out = []
    for directory in SCAN_DIRS:
        if directory.is_dir():
            out.extend(sorted(p for p in directory.rglob("*")
                              if p.suffix in (".cpp", ".h")))
    return out


def quoted_includes(path):
    """返回 [(行号, 目标)]。"""
    out = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for lineno, line in enumerate(text.splitlines(), 1):
        match = _INCLUDE_RE.match(line)
        if match:
            out.append((lineno, match.group(1)))
    return out


def resolves(including_file, target):
    """包含者自己目录 -> gui/src -> gui/tests -> 仓库根。"""
    if (including_file.parent / target).is_file():
        return True
    return any((base / target).is_file() for base in INCLUDE_DIRS)


def dangling_includes():
    """[(相对路径, 行号, 目标)] —— 只在仓库里找不到的那些。"""
    broken = []
    for path in gui_source_files():
        for lineno, target in quoted_includes(path):
            if _GENERATED_RE.search(target):
                continue
            if not resolves(path, target):
                broken.append((path.relative_to(_PROJECT_ROOT).as_posix(), lineno, target))
    return broken


class TestGuiIncludesResolve(unittest.TestCase):
    """每个引号形式的 include 都要能在仓库里找到。"""

    def test_no_dangling_quoted_includes(self):
        broken = dangling_includes()
        detail = "\n  ".join("%s:%d  ->  %s" % row for row in broken)
        self.assertFalse(
            broken,
            "GUI 源码里有解析不到的 include（板端会直接编译失败，而 PC 侧看不出来）:\n  %s"
            % detail)

    def test_scan_actually_covers_the_gui(self):
        """反空转: 扫到的文件与 include 数量要够 —— 否则上面那条永远绿。"""
        files = gui_source_files()
        self.assertGreaterEqual(len(files), 40, "扫到的 GUI 文件太少: %d" % len(files))
        total = sum(len(quoted_includes(p)) for p in files)
        self.assertGreater(total, 100, "扫到的引号 include 太少: %d" % total)
        names = {p.name for p in files}
        for must in ("main.cpp", "main_window.cpp", "model_page.cpp", "config_store.h"):
            self.assertIn(must, names, "扫描范围漏了 %s" % must)

    def test_the_check_has_teeth(self):
        """反空转: T14-3 那个真实文件名喂进去必须报"找不到"。"""
        fake = GUI_DIR / "src" / "ui" / "model_page.cpp"
        self.assertTrue(fake.is_file(), "用作探针的文件不在了")
        self.assertFalse(resolves(fake, "core/config_sync.h"),
                         "config_sync.h 又在仓库里了？那这条探针要换一个已删除的头")
        # 而真实存在的头必须解析得到（不然就是解析规则写错了）
        self.assertTrue(resolves(fake, "core/config_store.h"))
        self.assertTrue(resolves(fake, "ui/model_page.h"))

    def test_generated_headers_are_exempt(self):
        """构建目录里生成的头不该被误报。"""
        self.assertTrue(_GENERATED_RE.search("ui_mainwindow.h"))
        self.assertTrue(_GENERATED_RE.search("gui_widgets_autogen/EWIEGA46WW/moc_x.cpp"))
        self.assertTrue(_GENERATED_RE.search("test_model_page.moc"))
        self.assertFalse(_GENERATED_RE.search("core/config_store.h"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
