#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_docs.py — 文档守卫：相对链接有效 + 过时声明不得回归

跑法:
    python tests/test_docs.py

检查两件事:

  1) **相对链接必须指向存在的文件**
     `Readme.md` 与 `docs/**/*.md` 里的 `[文本](目标)`，目标是相对路径时，
     必须能相对该 md 所在目录解析到真实文件/目录。
     外链 (`http://` `https://` `mailto:`) 与纯锚点 (`#标题`) 不检查。

  2) **过时声明不得出现**（STALE_CLAIMS 黑名单，大小写不敏感）
     这些是"文档说 A、代码做 B"里已经被清掉的说法。每发现一种新的漂移，
     就往 STALE_CLAIMS 里加一条 —— 这样它就不会再回来。

为什么单独有这个文件
    文档漂移不会让任何测试变红，所以它总是最后才被发现的（Phase 6 就吃过一次：
    `ipc-protocol.md` 说命令用 topic 信封、GUI 实际发 action 信封，两边都"有文档"
    却互相矛盾）。这个守卫把"改代码时顺手改文档"从自觉变成跑得出来的约束。

注意
    扫描范围是**现状类文档**（Readme + docs/）。`todo.md` 是历史记录，里面出现旧词
    （例如 Phase 8 计划里提到的技术选项）是合理的，不在扫描范围内。

    还有一类要排除：**本机独有的文档**。板端 `docs/` 下留着两份旧 GUI 方案
    （它们在板端的 `.git/info/exclude` 里，不属于任何分支）。它们是历史材料，
    说"gui.yaml 是真源"完全正常，不该让守卫变红。判据用 `git check-ignore`：
    被本机忽略的文件不算仓库的文档。
"""

import re
import subprocess
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
#  扫描范围
# ---------------------------------------------------------------------------
#: 现状类文档 (相对仓库根)。todo.md 是历史记录, 故意不在内。
DOC_FILES = ["Readme.md"]
DOC_GLOBS = ["docs/*.md", "docs/**/*.md"]

# ---------------------------------------------------------------------------
#  过时声明黑名单: (正则, 为什么)
# ---------------------------------------------------------------------------
#: 每条都对应一次真实的漂移。加新条目时写清"为什么它过时了"。
STALE_CLAIMS = [
    (r"ZeroMQ",
     "IPC 早就改成同机 Unix domain socket (/tmp/agent.sock) 了, 没有消息队列"),
    (r"\bzmq\b",
     "同上: 没有 zmq 客户端/服务端"),
    (r"PySide6",
     "GUI 是板端的 Qt5 C++ 程序 (gui/src), 不是 PC 上的 PySide6"),
    (r"\b5555\b|\b5556\b",
     "ZeroMQ 时代的端口, 现在这条链路上没有 TCP 端口"),
    (r"/opt/agent",
     "板端仓库路径是 /home/kickpi/myproject/assitant"),
    (r"test-board\.ps1",
     "板端套件由 scripts/run-board-tests.ps1 驱动 (它跑的是共享的 test-python.sh)"),
    (r"实现未做",
     "IPC 的 server/client 都已经实现并在跑"),
    (r"gui\.yaml|gui/config/",
     "GUI 已经没有自己的配置文件了: 界面参数并进 config/config.yaml 的 gui: 段, "
     "gui/config/ 目录连同模板一起删除 (归一化 D 系列)"),
    (r"同步到[^\n]{0,40}llm\.env",
     "llm/config/llm.env 是**派生**文件, 不是被同步的真源 —— "
     "方向只有 config.yaml → llm.env 一个"),
]


def _locally_ignored(paths):
    """返回其中被**本机** git 忽略的那些（`.gitignore` + `.git/info/exclude`）。

    git 不在 / 不是仓库时返回空集合 —— 那就退化成"全都扫"，与过去的行为一致。
    """
    if not paths:
        return set()
    try:
        proc = subprocess.run(
            ["git", "check-ignore", "--stdin"],
            cwd=str(_PROJECT_ROOT),
            input="\n".join(str(p) for p in paths),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            universal_newlines=True,
        )
    except (OSError, ValueError):
        return set()
    if proc.returncode not in (0, 1):        # 128 = 不是 git 仓库
        return set()
    return {Path(line.strip()).resolve() for line in proc.stdout.splitlines() if line.strip()}


def _doc_paths():
    """收集要扫描的 md 文件 (存在才收, 排序稳定)。本机忽略的不算。"""
    out = []
    for rel in DOC_FILES:
        p = _PROJECT_ROOT / rel
        if p.is_file():
            out.append(p)
    for pattern in DOC_GLOBS:
        out.extend(p for p in _PROJECT_ROOT.glob(pattern) if p.is_file())
    # 去重 + 稳定顺序
    paths = sorted(set(out))
    ignored = _locally_ignored(paths)
    return [p for p in paths if p.resolve() not in ignored]


#: markdown 链接: [文本](目标)。目标里不含 ')' 就够用了 (本仓库没有带括号的路径)。
_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")

#: 不检查的目标: 外链 / 纯锚点 / 空
_SKIP_PREFIX_RE = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.\-]*:|#|$)")


class TestDocLinksExist(unittest.TestCase):
    """相对链接必须能解析到存在的文件。"""

    def test_relative_links_resolve(self):
        broken = []
        for path in _doc_paths():
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), 1):
                for target in _LINK_RE.findall(line):
                    target = target.strip().strip("<>")
                    if _SKIP_PREFIX_RE.match(target):
                        continue          # http(s): / mailto: / #anchor
                    # 去掉锚点与可选标题: "a.md#x" / 'a.md "标题"'
                    rel = target.split("#", 1)[0].split(None, 1)[0].strip()
                    if not rel:
                        continue
                    resolved = (path.parent / rel).resolve()
                    if not resolved.exists():
                        broken.append("%s:%d  ->  %s" % (
                            path.relative_to(_PROJECT_ROOT).as_posix(), lineno, target))

        if broken:
            self.fail("有 %d 个相对链接指向不存在的文件:\n  %s"
                      % (len(broken), "\n  ".join(broken)))


class TestNoStaleClaims(unittest.TestCase):
    """已被清掉的旧说法不得回来。"""

    def test_stale_claims_are_gone(self):
        found = []
        for path in _doc_paths():
            text = path.read_text(encoding="utf-8")
            rel = path.relative_to(_PROJECT_ROOT).as_posix()
            for lineno, line in enumerate(text.splitlines(), 1):
                for pattern, why in STALE_CLAIMS:
                    if re.search(pattern, line, re.IGNORECASE):
                        found.append("%s:%d  命中 /%s/\n      %s\n      原因: %s"
                                     % (rel, lineno, pattern, line.strip(), why))

        if found:
            self.fail("文档里出现了 %d 处过时声明 (改掉, 或如果是合理例外就调整"
                      " STALE_CLAIMS 并说明原因):\n  %s"
                      % (len(found), "\n  ".join(found)))

    def test_the_scan_actually_covers_something(self):
        """防止正则写错导致"零命中"这种假绿灯。"""
        paths = _doc_paths()
        self.assertGreaterEqual(len(paths), 5, "扫描到的 md 太少: %r" % (paths,))
        names = {p.name for p in paths}
        for must in ("Readme.md", "ipc-protocol.md", "architecture.md", "config-sources.md"):
            self.assertIn(must, names, "扫描范围漏了 %s" % must)


class TestLinksAreActuallyExtracted(unittest.TestCase):
    """同理: 证明链接提取真的抓到了东西（否则 test_relative_links_resolve 永远绿）。"""

    def test_found_some_links(self):
        total = 0
        for path in _doc_paths():
            total += len(_LINK_RE.findall(path.read_text(encoding="utf-8")))
        self.assertGreater(total, 0, "一个 markdown 链接都没提取到, 正则或文档有问题")


if __name__ == "__main__":
    unittest.main(verbosity=2)
