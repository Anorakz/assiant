#!/usr/bin/env python3
"""tests/test_config_keys.py — 配置键的守卫（T15-4 任务 4）

盯的是这一轮**真踩到的**两类坑：

  ① **代码在读、模板里没有**的键 —— 那种键"能调但没人知道能调"（`assistant set` 改不了、
     文档也没有）。任务 4 之前有 8 个：`study.learn_score`、`music.poll_interval_s`、
     `music.ssh`、`sunshine.unique_id`、`llm.edge_base_url`、`profile.file`、
     `vision.precision`、`vision.target_platform`。这里逐个钉死"模板里有它 + 代码真在读"。
  ② 本轮新增的 17 个 root 级键：**在模板里 + 属 root 级 + 类型与代码读法一致**。

⚠ 曾经想写成"**全量**双向扫描"（代码读的每个键 ↔ 模板里的每个标量），但扫描口径
   （裸键名 / 点号路径 / 助手函数实参 / GUI 的 C++ 字面量 / 动态拼键 / 容器名 / 文件名）
   需要再收敛一轮才配当守卫 —— 现在留的是**定点版**：错不了，也不会天天红。
   全量版记在 `docs/audit-code.md` §11 的后续里。

运行:
    python3 tests/test_config_keys.py
"""
import re
import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.core import config_tiers                              # noqa: E402
from agent.core.settings_config import known_paths               # noqa: E402

EXAMPLE = _PROJECT_ROOT / "config" / "config.example.yaml"
AGENT = _PROJECT_ROOT / "agent"
GUI_SRC = _PROJECT_ROOT / "gui" / "src"

#: 任务 4 之前"代码在读、模板里没有"的 8 个键（现在都转正了）
TURNED_LEGAL = (
    "study.learn_score", "music.poll_interval_s", "music.ssh", "sunshine.unique_id",
    "llm.edge_base_url", "profile.file", "vision.precision", "vision.target_platform",
)

#: 任务 4 新增的 17 个 root 级键 -> 模板字面量应当是哪种类型
NEW_ROOT_KEYS = {
    "study.learn_score": "float",
    "study.adapt_step": "float",
    "study.ewma_alpha": "float",
    "music.poll_interval_s": "float",
    "music.ssh": "str",
    "bilibili.buffer.feed_stall_s": "int",
    "llm.edge_base_url": "str",
    "sunshine.unique_id": "str",
    "scheduler.history_limit": "int",
    "profile.file": "str",
    "profile.memory_max_chars": "int",
    "profile.memory_max_entries": "int",
    "profile.memory_keep_after_settle": "int",
    "vision.precision": "str",
    "vision.target_platform": "str",
    "crash_log.keep": "int",
    "crash_log.tail_lines": "int",
}


def source_text(*folders: Path) -> str:
    chunks = []
    for folder in folders:
        for path in sorted(folder.rglob("*")):
            if path.suffix in (".py", ".cpp", ".h", ".hpp", ".cc") and path.is_file():
                chunks.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(chunks)


def is_read_in_code(key: str, text: str) -> bool:
    """这个键**在代码里被读过**吗（裸键名或点号路径出现在读的样子里）。"""
    bare = key.rsplit(".", 1)[-1]
    dotted = key
    patterns = (
        r'\.get\(\s*["\']%s["\']' % re.escape(bare),
        r'_cfg\(\s*["\'][^"\']+["\']\s*,\s*["\']%s["\']' % re.escape(bare),
        r'_(?:text|bool|int|float|num)\(\s*["\']%s["\']' % re.escape(bare),
        r'\[\s*["\']%s["\']\s*\]' % re.escape(bare),
        r'["\']%s["\']' % re.escape(dotted),
    )
    return any(re.search(pattern, text) for pattern in patterns)


class TestKeysTheCodeReadsAreInTheTemplate(unittest.TestCase):
    """① 那 8 个"在读但没进模板"的键：模板里有 + 代码真在读（两边都钉）。"""

    def setUp(self):
        self.paths = set(known_paths(str(EXAMPLE)))
        self.agent_text = source_text(AGENT)
        self.gui_text = source_text(GUI_SRC)

    def test_they_are_all_in_the_template_now(self):
        missing = [key for key in TURNED_LEGAL if key not in self.paths]
        self.assertEqual(missing, [], "这些键又不在模板里了（改不了、也没文档）：%s" % missing)

    def test_the_code_really_reads_them(self):
        """反空转：每个键都得真有一处读法 —— 否则"转正"只是往模板里塞了一行。"""
        not_read = [key for key in TURNED_LEGAL if not is_read_in_code(key, self.agent_text)]
        self.assertEqual(not_read, [], "这些键在 agent/ 里找不到读法：%s" % not_read)

    def test_the_scanner_is_not_vacuous(self):
        self.assertTrue(is_read_in_code("study.relative_band", self.agent_text))
        self.assertFalse(is_read_in_code("study.zz_not_a_key", self.agent_text))
        self.assertGreater(len(self.paths), 100, "模板里的标量键太少：%d" % len(self.paths))


class TestTheNewRootKeys(unittest.TestCase):
    """② 本轮新增的 17 个键：在模板里、属 root 级、类型与读法一致。"""

    def setUp(self):
        self.paths = set(known_paths(str(EXAMPLE)))
        self.template_text = EXAMPLE.read_text(encoding="utf-8")

    def _kind(self, key: str) -> str:
        name = key.rsplit(".", 1)[-1]
        for line in self.template_text.splitlines():
            stripped = line.strip()
            if not stripped.startswith(name + ":"):
                continue
            value = stripped.split(":", 1)[1].split("#", 1)[0].strip()
            if value.lower() in ("true", "false"):
                return "bool"
            try:
                int(value)
                return "int"
            except ValueError:
                pass
            try:
                float(value)
                return "float"
            except ValueError:
                return "str"
        self.fail("模板里找不到 %s" % key)

    def test_all_of_them_are_in_the_template(self):
        missing = sorted(key for key in NEW_ROOT_KEYS if key not in self.paths)
        self.assertEqual(missing, [], "新增的键没进模板：%s" % missing)

    def test_all_of_them_are_root_tier(self):
        """新增的键**一律 root 级** —— 与"不再新增 GUI 配置项"这条决定对齐。"""
        wrong = {key: config_tiers.tier_of(key, str(EXAMPLE))
                 for key in NEW_ROOT_KEYS
                 if config_tiers.tier_of(key, str(EXAMPLE)) != config_tiers.ROOT_TIER}
        self.assertEqual(wrong, {}, "这些新键不是 root 级：%s" % wrong)

    def test_their_template_types_match_how_the_code_reads_them(self):
        wrong = {key: self._kind(key) for key, want in NEW_ROOT_KEYS.items()
                 if self._kind(key) != want}
        self.assertEqual(wrong, {}, "模板字面量的类型与代码读法不一致：%s" % wrong)


if __name__ == "__main__":
    unittest.main(verbosity=2)
