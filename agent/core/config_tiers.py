# -*- coding: utf-8 -*-
"""agent/core/config_tiers.py —— 配置项的**两套权限**（T15-4）

谁在用: `assistant set` / `assistant shell`（CLI 侧）与 `agent/ipc/__init__.py`（GUI 侧，
走 `set_config`）。口径是用户 2026-10-03 定的：

  · **user 级** = **GUI 设置页能改的那些键**（`USER_KEYS`）。刻意与界面**对等**：
    界面上看得到的东西, 命令行里也该是最普通的操作（不新增 GUI 项, 只把 CLI 对齐）。
  · **root 级** = 模板里**其余的标量键**。CLI 独有 —— 必须在 `assistant shell` 里
    `mode root` 之后才允许写（用法见 docs/cli.md, 边界见 docs/config-sources.md §3.5）。
  · **不进体系** = 用户自定义子键（`study.process_names.*` 这类, 键名由用户定）与
    结构级键（序列/映射, 例如 `scheduler.recurring`）—— `assistant set` 本来就拒绝,
    要手改文件；`tier_of()` 对它们**直接报错**, 不给默认级（免得被当成"可以写"）。

安全默认: 模板里**新增**的键自动落 **root 级** —— 想升成 user 级必须显式写进
`USER_KEYS`, 而那样会立刻被 `tests/test_config_tiers.py` 要求"GUI 页里也得有它"
（两侧双向钉住, 见那条测试的说明）。

⚠ `USER_KEYS` 是**逐字清单**, 不是从 GUI 源码里读出来的: 板端只有编译产物、没有
   C++ 源码可读, 所以清单只能住在 Python 侧, 由测试拿 `gui/src/ui/settings_page.cpp`
   反查（漏一个 / 多一个都会红）。

⚠ 键清单的**唯一真源仍然是 `config/config.example.yaml`**（本模块只是给它贴标签）：
   `settable_keys()` 直接转发 `settings_config.known_paths()`，不另写一套模板解析。

⚠ 为什么"拒 root"不写在写入器里: `settings_config` 需要知道"哪些键是 root", 而本模块
   需要它的 `known_paths` —— 两边互相 import 就成环。所以**tier 感知的入口放在这里**
   （`plan_changes_for_tier()`, T15-4 任务 3 接入）：写入器保持不认识 tier,
   由本模块做闸门, 并用守卫扫"除本模块外没人直接调写入器"。
"""
from __future__ import annotations

from typing import Any, Dict, FrozenSet, List, Optional, Tuple

__all__ = [
    "ConfigTierError", "USER_TIER", "ROOT_TIER", "USER_KEYS",
    "settable_keys", "tier_of", "tiers", "user_keys", "root_keys", "summary",
    "refuse_root", "plan_changes_for_tier", "apply_changes_for_tier",
]

#: 级名（就用这两个字符串, 别处不要再造同义词）
USER_TIER = "user"
ROOT_TIER = "root"


class ConfigTierError(ValueError):
    """这个路径不是**可设置的标量键**（不在模板里, 或是结构级/用户自定义子键）。"""


#: user 级 = GUI 设置页能改的键（2026-10-03 的 `settings_page.cpp`，25 个）。
#: 顺序按界面（通用页 → 学习 → 游戏检测 → 画像压缩），便于人对着界面对。
#: ⚠ 加一项就得同时改 `gui/src/ui/settings_page.cpp`，否则 `test_config_tiers.py`
#:   的"逐条一致"会红 —— 这正是我们要的。
USER_KEYS: FrozenSet[str] = frozenset((
    # ---- 通用页 ----
    "gui.debug",
    "gui.fullscreen",
    "gui.start_page",
    "gui.input_source",
    "gui.wake.top",
    "gui.wake.bottom",
    "gui.wake.left",
    "gui.wake.right",
    "gui.wake.idle_ms",
    "gui.video_overlay.mode",
    "gui.video_overlay.idle_ms",
    # ---- 学习监督 ----
    "study.enabled",
    "study.focus_interval_min",
    "study.recheck_interval_min",
    "study.max_failures",
    "study.cooldown_min",
    "study.cooldown_probe_min",
    "study.relative_band",
    # ---- 游戏检测 ----
    "bilibili.game_watch.enabled",
    "bilibili.game_watch.interval_s",
    "bilibili.game_watch.confident_score",
    "bilibili.cookie_file",
    # ---- 画像压缩 ----
    "profile.enabled",
    "profile.trigger_chars",
    "profile.trigger_turns",
))


def settable_keys(template: Optional[str] = None) -> List[str]:
    """模板里所有**可设置的标量键**（点号路径，排序）—— 转发写入器的唯一实现。"""
    from agent.core.settings_config import known_paths

    return known_paths(template)


def tier_of(path: str, template: Optional[str] = None) -> str:
    """这个键属于哪一级。

    @param path 点号路径（`study.relative_band`）
    @return `"user"`（在 `USER_KEYS` 里）或 `"root"`（模板里其余的标量键）
    @raise ConfigTierError 不是可设置的标量键（不在模板里 / 结构级 / 用户自定义子键）
    """
    name = str(path or "").strip()
    if name in USER_KEYS:
        return USER_TIER
    if name in set(settable_keys(template)):
        return ROOT_TIER
    raise ConfigTierError("%r 不是可设置的标量键（模板里没有它，或者是结构级/自定义子键）"
                          % (path,))


def user_keys(template: Optional[str] = None) -> List[str]:
    """user 级清单（排序）—— 顺便校验"清单里的键必须真在模板里"。

    @raise ConfigTierError `USER_KEYS` 里有模板中不存在的键（加键时漏改模板 / 写错名字）
    """
    known = set(settable_keys(template))
    missing = sorted(USER_KEYS - known)
    if missing:
        raise ConfigTierError(
            "USER_KEYS 里有模板里不存在的键: %s（加设置项要先往 "
            "config/config.example.yaml 加键）" % ", ".join(missing))
    return sorted(USER_KEYS)


def root_keys(template: Optional[str] = None) -> List[str]:
    """root 级清单（排序）= 模板里的标量键 − `USER_KEYS`。"""
    return sorted(set(settable_keys(template)) - USER_KEYS)


def tiers(template: Optional[str] = None) -> Tuple[List[str], List[str]]:
    """-> (user 清单, root 清单)，都排好序。"""
    return user_keys(template), root_keys(template)


def summary(template: Optional[str] = None) -> Dict[str, Any]:
    """给 `assistant set --list-tiers` / 自查用的一眼可读汇总。"""
    user, root = tiers(template)
    everything = set(settable_keys(template))
    return {
        "user": user,
        "root": root,
        "counts": {"user": len(user), "root": len(root), "settable": len(everything)},
    }


# ---------------------------------------------------------------------------
#  闸门：tier 感知的写入入口（**改配置请一律走这两个**）
# ---------------------------------------------------------------------------
#: 只有本模块可以直接调 `settings_config.plan_changes/apply_changes`（守门的那一层）。
WRITER_ENTRY = "agent/core/config_tiers.py"


def refuse_root(paths: Any, *, allow_root: bool = False,
                template: Optional[str] = None) -> None:
    """user 权限下碰到 root 级键 -> 抛 `ConfigTierError`。

    @param paths    要改的键（字典 / 序列都行）
    @param allow_root 只有进了 root 体系（`assistant shell` 里 `mode root`）才给 True
    @note 不认识的键（结构级 / 自定义子键 / 不存在）**一律放行**给写入器去报它自己那句 ——
          这一层只管权限, 不重复一套"这个键能不能改"的文案。
    """
    if allow_root:
        return
    blocked = []
    for path in paths:
        try:
            if tier_of(path, template) == ROOT_TIER:
                blocked.append(str(path))
        except ConfigTierError:
            continue
    if blocked:
        raise ConfigTierError(
            "%s 是 **root 级**设置项 —— 只有板端 root 控制台里 `assistant shell` → `mode root` "
            "之后才能改（GUI / IPC 一律改不了；看两级清单：`assistant set --list-tiers`）。"
            % "、".join(sorted(blocked)))


def plan_changes_for_tier(changes: Any, *, allow_root: bool = False,
                          **kwargs: Any) -> List[Dict[str, Any]]:
    """`settings_config.plan_changes` + **权限闸门**（推荐入口）。

    @note 名字带 `_for_tier` 是刻意的：审计器的"同名不同体"会把这儿的 `plan_changes` 与
          写入器里那个当成撞名 —— 它们确实干的是同一件事的两个层次（裸的 / 带闸门的），
          所以名字上就分开。
    @raise ConfigTierError user 权限下要改 root 级键（**动任何文件之前**）
    """
    refuse_root(changes, allow_root=allow_root, template=kwargs.get("template"))
    from agent.core import settings_config

    return settings_config.plan_changes(changes, **kwargs)


def apply_changes_for_tier(changes: Any, *, allow_root: bool = False,
                           **kwargs: Any) -> Dict[str, Any]:
    """`settings_config.apply_changes` + **权限闸门**（推荐入口）。

    @note 闸门在这**再查一遍**：`plan` 与 `apply` 是两次调用，中间谁都能改主意。
    """
    refuse_root(changes, allow_root=allow_root, template=kwargs.get("template"))
    from agent.core import settings_config

    return settings_config.apply_changes(changes, **kwargs)
