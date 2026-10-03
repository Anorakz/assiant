#!/usr/bin/env python3
"""tests/test_config_tiers.py — 配置项两套权限（T15-4 任务 1）

覆盖:
  · `USER_KEYS` ↔ **GUI 设置页实际能改的键** 逐条一致（机械反查 `settings_page.cpp`）
  · `tier_of()` 自洽: user ∪ root == 模板里的标量键, 无交集, 未知/结构级/自定义子键报错
  · **安全默认**: 模板里新增的键自动落 root 级（拿"真模板 + 一个假键"验）
  · 反空转: 上面两条都真的在查东西（清单为空 / 提取为空 / 校验没牙 -> 红）

运行:
    python3 tests/test_config_tiers.py
"""
import re
import sys
import tempfile
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import yaml  # noqa: E402

from agent.core import config_tiers as tiers  # noqa: E402
from agent.core.settings_config import SettingsConfigError, known_paths  # noqa: E402

EXAMPLE = _PROJECT_ROOT / "config" / "config.example.yaml"
PAGE = _PROJECT_ROOT / "gui" / "src" / "ui" / "settings_page.cpp"

#: GUI 设置页里形如 `QStringLiteral("study.enabled")` 的字面量
LITERAL = re.compile(r'"([a-z_][a-z0-9_]*(?:\.[a-z0-9_]+)+)"')
#: 动态拼键的那半截（`QStringLiteral("gui.wake.") + region`）
WAKE_PREFIX = "gui.wake."
#: 四个区域的取值（就写在同一个文件的 `for (const QString& region : {...})` 里）
REGION = re.compile(r'QStringLiteral\("(top|bottom|left|right)"\)')


def template_sections() -> set:
    with open(EXAMPLE, encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return set(data)


def gui_page_keys() -> set:
    """机械反查设置页会发的键。

    只看"(点号路径, 且真在模板里)"的字面量 —— 这样 `"config.yaml"` / `"gui.wake"` /
    凭据名（`SESSDATA` 没有点）都不会被误收。
    """
    text = PAGE.read_text(encoding="utf-8")
    known = set(known_paths(str(EXAMPLE)))
    found = {match.group(1) for match in LITERAL.finditer(text) if match.group(1) in known}
    if WAKE_PREFIX in text:
        for region in {m.group(1) for m in REGION.finditer(text)}:
            expanded = WAKE_PREFIX + region
            if expanded in known:
                found.add(expanded)
    return found


class TestUserTierMatchesTheSettingsPage(unittest.TestCase):
    """`USER_KEYS` 必须与 GUI 设置页**逐条一致**（这是"两级权限"的地基）。

    gui 加一项而 CLI 没跟上 -> 用户级清单少了一个键（那一项在 shell 里改不了）;
    CLI 自己加一项 user 级而界面没有 -> 那就出现了"只有命令行能改的 user 项"，
    与定的口径冲突。两个方向都得红。
    """

    def test_no_missing_and_no_extra(self):
        page = gui_page_keys()
        declared = set(tiers.USER_KEYS)
        self.assertEqual(
            page, declared,
            "USER_KEYS 与 GUI 设置页不一致:\n"
            "  · 界面有、清单里没有: %s\n"
            "  · 清单里有、界面没有: %s"
            % (sorted(page - declared) or "无", sorted(declared - page) or "无"))

    def test_the_comparison_is_not_vacuous(self):
        page = gui_page_keys()
        self.assertGreaterEqual(len(page), 20,
                                "只从设置页里认出 %d 个键 —— 提取规则坏了（多半是正则或路径）"
                                % len(page))
        for must in ("gui.debug", "study.enabled", "study.relative_band",
                     "bilibili.game_watch.interval_s", "bilibili.cookie_file",
                     "profile.trigger_chars", "gui.wake.top", "gui.wake.idle_ms"):
            self.assertIn(must, page, "提取结果里少了 %s —— 提取规则漏了这种写法" % must)

    def test_every_user_key_is_a_settable_scalar(self):
        known = set(known_paths(str(EXAMPLE)))
        unknown = sorted(set(tiers.USER_KEYS) - known)
        self.assertEqual(unknown, [], "USER_KEYS 里有模板里不存在的键: %s" % unknown)


class TestTierOf(unittest.TestCase):
    """`tier_of()` 的口径与安全默认。"""

    def test_user_and_root_partition_the_template(self):
        settable = set(known_paths(str(EXAMPLE)))
        user, root = tiers.tiers(str(EXAMPLE))
        self.assertEqual(set(user) | set(root), settable,
                         "user + root 必须正好等于模板里的标量键")
        self.assertEqual(set(user) & set(root), set(), "两级不能有交集")
        self.assertGreater(len(user), 0, "user 级清单空了 —— 权限模型没有意义")
        self.assertGreater(len(root), 0, "root 级清单空了 —— 权限模型没有意义")

    def test_known_tiers(self):
        self.assertEqual(tiers.tier_of("study.relative_band", str(EXAMPLE)), tiers.USER_TIER)
        self.assertEqual(tiers.tier_of("gui.wake.idle_ms", str(EXAMPLE)), tiers.USER_TIER)
        self.assertEqual(tiers.tier_of("study.adapt", str(EXAMPLE)), tiers.ROOT_TIER)
        self.assertEqual(tiers.tier_of("scheduler.interval_min", str(EXAMPLE)), tiers.ROOT_TIER)

    def test_not_settable_paths_are_refused(self):
        # 结构级（序列）、用户自定义子键（键名是用户定的）、压根不存在的键
        for bad in ("scheduler.recurring", "study.process_names.code.exe",
                    "wallpaper.tagging.ip_presets.EVA", "nope.nothing", "", "gui"):
            with self.assertRaises(tiers.ConfigTierError, msg="%r 不该被判成可设置的标量键" % bad):
                tiers.tier_of(bad, str(EXAMPLE))

    def test_a_brand_new_template_key_defaults_to_root(self):
        """**安全默认**: 模板里新增的键自动落 root（想升 user 必须显式写进清单）。"""
        text = EXAMPLE.read_text(encoding="utf-8")
        patched = text.replace("study:\n", "study:\n  zz_task1_probe: 1\n", 1)
        self.assertIn("zz_task1_probe", patched, "没插进去 —— 模板结构变了？")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.example.yaml"
            path.write_text(patched, encoding="utf-8")
            self.assertIn("study.zz_task1_probe", known_paths(str(path)))
            self.assertEqual(tiers.tier_of("study.zz_task1_probe", str(path)), tiers.ROOT_TIER)
            self.assertNotIn("study.zz_task1_probe", tiers.USER_KEYS)

    def test_the_user_list_check_has_teeth(self):
        """反空转: 清单里混进一个模板没有的键 -> `user_keys()` 必须报错。"""
        bogus = frozenset(set(tiers.USER_KEYS) | {"study.zz_not_in_template"})
        original = tiers.USER_KEYS
        try:
            tiers.USER_KEYS = bogus                     # type: ignore[misc]
            with self.assertRaises(tiers.ConfigTierError):
                tiers.user_keys(str(EXAMPLE))
        finally:
            tiers.USER_KEYS = original                  # type: ignore[misc]


class TestSummary(unittest.TestCase):
    def test_summary_counts(self):
        data = tiers.summary(str(EXAMPLE))
        self.assertEqual(data["counts"]["user"], len(tiers.USER_KEYS))
        self.assertEqual(data["counts"]["settable"],
                         data["counts"]["user"] + data["counts"]["root"])
        self.assertEqual(sorted(data["user"]), data["user"], "清单要排序（便于人读/对比）")

    def test_settings_config_error_type_is_shared(self):
        """`ConfigTierError` 与写入器的错误都是 ValueError 家族 —— CLI 好统一处理。"""
        self.assertTrue(issubclass(tiers.ConfigTierError, ValueError))
        self.assertTrue(issubclass(SettingsConfigError, ValueError))


if __name__ == "__main__":
    unittest.main(verbosity=2)
