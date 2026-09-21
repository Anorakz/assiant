#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_config_source_guard.py — 配置真源守卫（归一化 D 系列）

跑法:
    python tests/test_config_source_guard.py

守的是一条**单向**关系:

    config/config.yaml  ──派生──▶  llm/config/llm.env
      (唯一真源, Agent 只读这份)      (喂 llama-server 的派生文件)

为什么要有这个文件
    D 系列之前有三份配置，GUI 的模型测试页一次写三份、Agent 只认一份，于是
    "界面上改了参数，运行时不生效" 而且**没有任何测试会变红**。清理干净之后，
    这个守卫负责让它别再长回来 —— 谁把派生文件重新接回 Agent，或者把 GUI 自己
    那份配置模板捡回来，这里立刻失败。

检查三件事:

  1) `agent/` 的 Python 源码里不许出现 GUI 专用配置名，也不许出现派生文件名
     —— Agent 只认 config/config.yaml。
  2) GUI 专用配置的残留物不许复活: 那份模板文件必须不存在，
     `.gitignore` 里也不许再为它留规则。
  3) 反空转: 证明扫描真的读到了 agent/ 的源码，且里面真的出现过 config.yaml
     （否则第 1 条永远绿）。
"""

import re
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: 扫描范围: Agent 的 Python 源码。
AGENT_DIR = _PROJECT_ROOT / "agent"

#: 不许出现在 agent/ 里的字面量: (正则, 为什么)
FORBIDDEN_IN_AGENT = [
    (r"gui\.yaml",
     "Agent 不许读 GUI 的配置 —— 真源只有 config/config.yaml"),
    (r"llm\.env",
     "llm/config/llm.env 是喂 llama-server 的**派生**文件, 不是应用配置; "
     "Agent 读它就会踩上「改真源不生效」"),
]

#: 真源路径, 反空转检查用。
TRUTH_PATH = "config/config.yaml"

#: D 系列删掉的残留物, 不许复活。
GONE_FILES = ["gui/config/gui.yaml.example"]
GONE_GITIGNORE_PATTERNS = ["gui/config/gui.yaml"]


def _agent_python_files():
    """agent/ 下要扫描的 .py（跳过 __pycache__），排序稳定。"""
    if not AGENT_DIR.is_dir():
        return []
    return sorted(p for p in AGENT_DIR.rglob("*.py") if "__pycache__" not in p.parts)


class TestAgentCopiesOnlyOneConfig(unittest.TestCase):
    """Agent 只认 config/config.yaml。"""

    def test_no_gui_or_derived_config_literals(self):
        found = []
        for path in _agent_python_files():
            text = path.read_text(encoding="utf-8")
            rel = path.relative_to(_PROJECT_ROOT).as_posix()
            for lineno, line in enumerate(text.splitlines(), 1):
                for pattern, why in FORBIDDEN_IN_AGENT:
                    if re.search(pattern, line, re.IGNORECASE):
                        found.append("%s:%d  命中 /%s/\n      %s\n      原因: %s"
                                     % (rel, lineno, pattern, line.strip(), why))

        if found:
            self.fail("agent/ 里出现了 %d 处不该有的配置字面量 "
                      "(Agent 只读 %s; 如果确实是注释里说明'不读它', 请改写措辞):\n  %s"
                      % (len(found), TRUTH_PATH, "\n  ".join(found)))

    def test_the_scan_actually_covers_something(self):
        """防止路径写错导致"零命中"这种假绿灯。"""
        files = _agent_python_files()
        self.assertGreaterEqual(len(files), 10,
                                "扫到的 agent/ 源码太少: %r" % (files,))

        # 真源路径必须真的出现在 agent/ 里 —— 否则上面那条检查等于什么都没查
        mentions = 0
        for path in files:
            if TRUTH_PATH in path.read_text(encoding="utf-8"):
                mentions += 1
        self.assertGreater(mentions, 0,
                           "agent/ 里没有任何文件提到 %s, 扫描范围可能不对" % TRUTH_PATH)


class TestNoGuiConfigLeftovers(unittest.TestCase):
    """GUI 专用配置的残留物不许复活。"""

    def test_template_is_gone(self):
        for rel in GONE_FILES:
            self.assertFalse((_PROJECT_ROOT / rel).exists(),
                             "%s 应该已经被删除（界面参数在 config/config.yaml 的 gui: 段）"
                             % rel)

    def test_gitignore_has_no_rule_for_it(self):
        gitignore = (_PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        for pattern in GONE_GITIGNORE_PATTERNS:
            self.assertNotIn(pattern, gitignore,
                             ".gitignore 里还留着 %s 的规则；文件都删了, 规则也该删" % pattern)

    def test_scan_is_not_vacuous(self):
        """证明 .gitignore 真的被读到了（否则 assertNotIn 永远绿）。"""
        gitignore = (_PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("config/*.yaml", gitignore,
                      ".gitignore 读进来的内容不对, 检查路径")


if __name__ == "__main__":
    unittest.main(verbosity=2)
