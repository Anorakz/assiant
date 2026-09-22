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
  2) GUI 专用配置的残留物不许复活: 工作区里不许再出现那份模板，
     `.gitignore` 与 `config/config.example.yaml`（都会送到板端）里不许再为它留规则。
  3) 反空转: 证明扫描真的读到了 agent/ 的源码，且里面真的出现过 config.yaml
     （否则第 1 条永远绿）。

范围说明
    本守卫只管**仓库内容**。"这台机器上还留着旧副本" 有两种正常来源，都不算回归:
    板端 HEAD 落后于仓库、或者文件被本机 `.git/info/exclude` 忽略。所以判据里
    带一次 `git check-ignore`: 被本机忽略的旧副本放过，工作区里**未被忽略**的
    副本才算回归。
"""

import re
import subprocess
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

#: 进库、会送到板端的文件里不许出现的字面量: (正则, 为什么)
FORBIDDEN_IN_SHIPPED_CONFIG = [
    (r"gui\.yaml",
     "GUI 专用配置已经删除 —— 真源只有 config/config.yaml"),
    (r"进行中",
     "归一化 D 系列已完成, 不该再写'进行中'"),
]

#: 真源模板, 它必须体现"GUI 没有自己的配置"。
EXAMPLE_CONFIG = "config/config.example.yaml"


def _locally_ignored(rel_paths):
    """返回其中被**本机** git 忽略的那些（`.gitignore` + `.git/info/exclude`）。

    这是"仓库内容"与"这台机器上有什么"的分界: 板端用 `.git/info/exclude`
    保留它自己的资产, 那些不该让守卫变红。git 不在 / 不是仓库时返回空集合。
    """
    if not rel_paths:
        return set()
    try:
        proc = subprocess.run(
            ["git", "check-ignore", "--stdin"],
            cwd=str(_PROJECT_ROOT),
            input="\n".join(rel_paths),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            universal_newlines=True,
        )
    except (OSError, ValueError):
        return set()
    if proc.returncode not in (0, 1):        # 128 = 不是 git 仓库
        return set()
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


#: 唯一允许**提到**派生文件的地方：运维 CLI。
#:
#: 为什么放行：`agent/cli.py` 不是 Agent 运行时，是给人用的操作工具；它的 `doctor`
#: 要回答"派生文件跟真源一致吗"，做法是把路径交给**那份唯一的 C++ 实现**
#: （`gui_config_sync` 的 dry-run），自己绝不把 `llm.env` 当配置读。
#: 为了不让这个例外变成后门，下面另有一条更精确的检查：**可以提名字，不许读它**。
CLI_EXEMPT = "agent/cli.py"

#: 命中"读派生文件"的写法（在 CLI 里也要拦）
READS_PATTERN = r"(open|read_text|readlines|read|load|loads)\s*\("


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
                    if not re.search(pattern, line, re.IGNORECASE):
                        continue
                    if rel == CLI_EXEMPT and pattern == r"llm\.env":
                        # 运维 CLI 允许**提到**它（见 CLI_EXEMPT 的说明），
                        # 但同一行里不许有"读文件"的调用
                        if not re.search(READS_PATTERN, line):
                            continue
                        why = ("运维 CLI 可以提到派生文件、但不许把它当配置**读**；"
                               "要判一致性请交给 gui_config_sync")
                    found.append("%s:%d  命中 /%s/\n      %s\n      原因: %s"
                                 % (rel, lineno, pattern, line.strip(), why))

        if found:
            self.fail("agent/ 里出现了 %d 处不该有的配置字面量 "
                      "(Agent 只读 %s; 如果确实是注释里说明'不读它', 请改写措辞):\n  %s"
                      % (len(found), TRUTH_PATH, "\n  ".join(found)))

    def test_cli_exemption_cannot_hide_a_real_read(self):
        """反空转：证明"放行 cli.py"没有把"读派生文件"也一起放过去。

        直接把一段**会读文件**的样本喂给判断逻辑，它必须被拦下。
        """
        sample = 'with open(env_path) as handle:  # llm.env'
        self.assertTrue(re.search(r"llm\.env", sample, re.IGNORECASE))
        self.assertTrue(re.search(READS_PATTERN, sample),
                        "READS_PATTERN 没抓住 open(...) —— 例外就成了后门")

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

    def test_template_is_not_in_the_working_tree(self):
        # 被本机忽略的旧副本放过（板端 / 落后于仓库的 checkout 都属正常）；
        # 工作区里一份**未被忽略**的副本才是回归。
        ignored = _locally_ignored(GONE_FILES)
        for rel in GONE_FILES:
            if (_PROJECT_ROOT / rel).exists() and rel not in ignored:
                self.fail("%s 应该已经被删除（界面参数在 config/config.yaml 的 gui: 段）。"
                          "如果这台机器只是落后于仓库，把它删掉，"
                          "或者写进本机 .git/info/exclude。" % rel)

    def test_gitignore_has_no_rule_for_it(self):
        gitignore = (_PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        for pattern in GONE_GITIGNORE_PATTERNS:
            self.assertNotIn(pattern, gitignore,
                             ".gitignore 里还留着 %s 的规则；文件都删了, 规则也该删" % pattern)

    def test_example_config_declares_the_single_truth(self):
        """真源模板里必须能看出"GUI 没有自己的配置"。"""
        text = (_PROJECT_ROOT / EXAMPLE_CONFIG).read_text(encoding="utf-8")
        self.assertIn("gui:", text,
                      "%s 里应该有 gui: 段（GUI 的界面参数住在这里）" % EXAMPLE_CONFIG)
        for pattern, why in FORBIDDEN_IN_SHIPPED_CONFIG:
            hits = [ln.strip() for ln in text.splitlines()
                    if re.search(pattern, ln, re.IGNORECASE)]
            if hits:
                self.fail("%s 里还有 %d 行命中 /%s/:\n      %s\n      原因: %s"
                          % (EXAMPLE_CONFIG, len(hits), pattern,
                             "\n      ".join(hits), why))

    def test_scan_is_not_vacuous(self):
        """证明这两个文件真的被读到了（否则上面的断言永远绿）。"""
        gitignore = (_PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("config/*.yaml", gitignore,
                      ".gitignore 读进来的内容不对, 检查路径")
        self.assertTrue((_PROJECT_ROOT / EXAMPLE_CONFIG).is_file(),
                        "%s 不见了" % EXAMPLE_CONFIG)


if __name__ == "__main__":
    unittest.main(verbosity=2)
