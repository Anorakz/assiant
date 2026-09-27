#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_adr.py — ADR 守卫（T14-1）

跑法:
    python tests/test_adr.py

守三件事（都是"写了 ADR 就等于承诺过"的机械部分）:

  1) **文件命名与编号**：`docs/adr/NNNN-kebab-case.md`，四位编号、从 0001 连号、不重号；
  2) **必需小节**：每篇都得有 `## 背景` / `## 决定` / `## 理由` / `## 替代方案` /
     `## 后果` / `## 参考`，以及一行 `- **状态**：…`（缺了就说明这篇还没写完，
     别把它当成"决定已记录"）；
  3) **索引与文件一一对应**：`docs/adr/README.md` 的表格里每个编号都要解析到真实文件，
     每个文件也都要在索引里有一行 —— 防"写了一篇 ADR 忘了登记"。

为什么要有这个守卫
    ADR 的价值全在"下次有人想改回来时能读到"。而 ADR 一旦与目录脱节（缺小节、
    索引漏登记），它就退化成一篇普通的说明文 —— 不会让任何测试变红，只会让人
    在错误的假设上继续往前走。这里把"结构性完整"变成跑得出来的检查。

**不**检查（刻意的）:
  · 不检查内容对不对、理由是否充分 —— 那是人读的事，机器只能查形式；
  · 不检查 ADR 与 `docs/*.md` 的说法是否一致 —— 冲突时以代码与现状文档为准
    （见 `docs/adr/README.md` 的分工说明）。
"""

import re
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
ADR_DIR = _PROJECT_ROOT / "docs" / "adr"
INDEX = ADR_DIR / "README.md"

#: 文件名: 四位编号 + kebab-case
_FILENAME_RE = re.compile(r"^(\d{4})-([a-z0-9]+(?:-[a-z0-9]+)*)\.md$")

#: 必需小节（`## ` 开头，按这个顺序出现在文件里）
REQUIRED_SECTIONS = ["## 背景", "## 决定", "## 理由", "## 替代方案", "## 后果", "## 参考"]

#: 状态行
_STATUS_RE = re.compile(r"^- \*\*状态\*\*：\S", re.MULTILINE)

#: 索引表格里的一行: `| [0001](0001-ipc-unix-socket.md) | 标题 | 状态 | 一句话 |`
_INDEX_ROW_RE = re.compile(r"^\|\s*\[(\d{4})\]\(([^)]+)\)\s*\|", re.MULTILINE)


def adr_files():
    """目录里的 ADR 文件（不含索引本身），按编号排序。"""
    if not ADR_DIR.is_dir():
        return []
    return sorted(p for p in ADR_DIR.glob("*.md") if p.name != INDEX.name)


def index_entries():
    """索引里登记过的 (编号, 目标) —— 目标只取文件名，允许 `0001-x.md` 或 `./0001-x.md`。"""
    if not INDEX.is_file():
        return []
    out = []
    for number, target in _INDEX_ROW_RE.findall(INDEX.read_text(encoding="utf-8")):
        out.append((number, target.split("/")[-1]))
    return out


class TestAdrFiles(unittest.TestCase):
    """文件命名、编号连号、必需小节。"""

    def test_directory_exists_and_is_scanned(self):
        """反空转: 目录真的在、真的扫到了文件（否则下面两条永远绿）。"""
        self.assertTrue(ADR_DIR.is_dir(), "docs/adr/ 不见了")
        files = adr_files()
        self.assertGreaterEqual(len(files), 4, "扫到的 ADR 太少: %r" % ([p.name for p in files],))

    def test_names_are_numbered_and_kebab(self):
        bad = []
        for path in adr_files():
            match = _FILENAME_RE.match(path.name)
            if not match:
                bad.append("%s（应为 NNNN-kebab-case.md）" % path.name)
                continue
            if len(match.group(2)) < 3:
                bad.append("%s（标题段太短）" % path.name)
        self.assertFalse(bad, "文件名不合规: %s" % "；".join(bad))

    def test_numbers_are_unique_and_contiguous(self):
        numbers = []
        for path in adr_files():
            match = _FILENAME_RE.match(path.name)
            if match:
                numbers.append(int(match.group(1)))
        self.assertEqual(len(numbers), len(set(numbers)),
                         "编号重复: %r" % (sorted(numbers),))
        expected = list(range(1, len(numbers) + 1))
        self.assertEqual(sorted(numbers), expected,
                         "编号不连号（缺号或跳号）: %r，应为 %r" % (sorted(numbers), expected))

    def test_each_adr_has_the_required_sections(self):
        bad = []
        for path in adr_files():
            text = path.read_text(encoding="utf-8")
            missing = []
            if not _STATUS_RE.search(text):
                missing.append("- **状态**：…")
            for section in REQUIRED_SECTIONS:
                if section not in text:
                    missing.append(section)
            if missing:
                bad.append("%s 缺: %s" % (path.name, "、".join(missing)))
        self.assertFalse(bad, "有 ADR 没写完（缺小节/缺状态行）:\n  %s" % "\n  ".join(bad))

    def test_sections_are_in_order(self):
        """小节顺序一致（读起来才像同一套模板）。"""
        bad = []
        for path in adr_files():
            text = path.read_text(encoding="utf-8")
            positions = [text.find(section) for section in REQUIRED_SECTIONS]
            if any(pos < 0 for pos in positions):
                continue          # 缺小节已由另一条报出
            if positions != sorted(positions):
                bad.append(path.name)
        self.assertFalse(bad, "小节顺序不对: %s" % "、".join(bad))


class TestAdrIndex(unittest.TestCase):
    """索引与文件一一对应。"""

    def test_index_exists_and_has_rows(self):
        self.assertTrue(INDEX.is_file(), "docs/adr/README.md 不见了（索引是入口）")
        self.assertGreaterEqual(len(index_entries()), 4, "索引里没有登记任何 ADR")

    def test_every_file_is_registered(self):
        registered = {target for _number, target in index_entries()}
        missing = [p.name for p in adr_files() if p.name not in registered]
        self.assertFalse(missing, "有 ADR 没在索引里登记: %s" % "、".join(missing))

    def test_every_row_points_at_a_real_file(self):
        # 索引里可能提到"待登记"的编号（正文说明），但表格行必须指向真实文件
        broken = []
        for number, target in index_entries():
            if not (ADR_DIR / target).is_file():
                broken.append("%s -> %s" % (number, target))
        self.assertFalse(broken, "索引指到了不存在的文件: %s" % "；".join(broken))

    def test_number_in_row_matches_filename(self):
        mismatch = []
        for number, target in index_entries():
            if not target.startswith(number):
                mismatch.append("%s -> %s" % (number, target))
        self.assertFalse(mismatch, "索引行的编号与文件名对不上: %s" % "；".join(mismatch))


if __name__ == "__main__":
    unittest.main(verbosity=2)
