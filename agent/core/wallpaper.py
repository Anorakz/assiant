# ============================================================================
#  agent/core/wallpaper.py — 壁纸目录的"下一张"（Phase 7 T3；T7-3 起支持"只在挑出来的
#                            那几张里翻"）
#
#  它是什么: 一个目录 + 一个游标。**不碰 IPC、不碰 GUI、不读图片内容** ——
#            只回答"下一张是哪张"。
#
#  谁用它（**只有一条入口**: 对话里 LLM 调 next_wallpaper 工具）
#  ---------------------------------------------------------------------------
#      agent/tools/wallpaper.py     LLM 调 next_wallpaper 工具
#        └─ agent/main.py::Runtime.next_wallpaper(step, match)
#             └─ WallpaperDeck.step(step, pool)   ← "下一张"的语义只有这一份
#
#  ⚠ T7-3 的需求变更: **手动换壁纸的入口都删掉了** —— GUI 主区那个「下一张」按钮、
#    以及配套的 `next_wallpaper` IPC 命令（T3 加的、T6 还给它接上了状态权限表）。
#    理由: 标签化之后"换成什么样"应该由**对话**表达（"换一张安静的深色风景"）,
#    按钮只能"按文件名翻下一张", 反而更容易让画面和意图对不上。
#    所以这类现在只剩一条路: 对话 -> LLM -> 工具 -> 这里。
#
#  游标语义（为什么不是"下标 +1"）
#  ---------------------------------------------------------------------------
#  · 每次调用都**重新列一遍目录** —— 你可以随时往里丢新图, 不用重启 Agent。
#  · 因此游标记的是**当前那张的路径**, 不是下标: 新图插在前面也不会让"下一张"跳回去。
#    当前那张不在了（删了/改名了）就从第一张重新开始。
#  · step=0 = 重新推当前那张; step<0 = 往前翻。越界**回绕**。
#  · `pool` 给了就只在这个候选列表里翻, **列表顺序就是优先级**（T7-3 的挑图:
#    相关度从高到低排好传进来, 于是 step=1 = "最像的那张"）。
#
#  读不到/解不开的图**照样算一张**
#  ---------------------------------------------------------------------------
#  这里只看"后缀像图片 + 当前进程读得动"。真的解码失败是 GUI 的事 ——
#  GUI 读不到会给兜底底色 + 一条橙字提示（docs/gui-agent-integration.md §2）。
#  在 Agent 里预解码一遍既慢又要引入图像库, 不划算。
# ============================================================================

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

__all__ = [
    "WallpaperDeck",
    "WallpaperError",
    "IMAGE_SUFFIXES",
    "DEFAULT_WALLPAPER_DIR",
]

_log = logging.getLogger(__name__)

#: `step(anchor=…)` 的默认值: 代表"用游标里那张"（与显式传 `None` 区分开 ——
#: `None` 的含义是"假装还没选过"）。用哨兵而不是 `None`, 是因为后者有意义。
_CURSOR: Any = object()

#: 认得的图片后缀（小写比较）。刻意不含 .gif —— Qt 能显示但"壁纸"要的是静态图。
IMAGE_SUFFIXES: Tuple[str, ...] = (".jpg", ".jpeg", ".png", ".webp", ".bmp")

#: 默认壁纸目录。板端放在 /home/kickpi/wallpapers（仓库**外面**, 所以不入库）;
#: 配置里的 `wallpaper.dir` 可以改成别处。
DEFAULT_WALLPAPER_DIR = "/home/kickpi/wallpapers"


class WallpaperError(ValueError):
    """壁纸目录用不了（不存在 / 不是目录 / 一张图都没有）。

    继承 ValueError, 与仓库里其他模块的错误约定一致。
    消息是**给人看的**: 它会经工具结果 / llm 推送显示到界面上。
    """


class WallpaperDeck:
    """一个壁纸目录 + 一个游标。

    典型用法::

        deck = WallpaperDeck("/home/kickpi/wallpapers")
        index, path, total = deck.step()        # 下一张
        index, path, total = deck.step(-1)      # 上一张
    """

    def __init__(
        self,
        directory: Optional[str] = None,
        suffixes: Sequence[str] = IMAGE_SUFFIXES,
    ) -> None:
        """
        @param directory 壁纸目录; None/空 -> DEFAULT_WALLPAPER_DIR
        @param suffixes  认哪些后缀（测试里可以换成 (".txt",) 之类）
        """
        text = directory.strip() if isinstance(directory, str) else ""
        self.directory = text or DEFAULT_WALLPAPER_DIR
        self.suffixes = tuple(
            s.lower() if s.startswith(".") else "." + s.lower() for s in suffixes
        )
        #: 当前那张的**路径**（不是下标, 见模块头）
        self._current: Optional[str] = None

    # ------------------------------------------------------------ 列目录 ---
    def scan(self) -> List[str]:
        """列出目录里像图片的文件（绝对路径, 顺序稳定）。

        @raise WallpaperError 目录不存在 / 不是目录
        @note 顺序按**文件名**（小写优先比较, 再按原名）—— 与 `ls` 给人的直觉一致,
              而且加了新图也不会让整列乱掉。名字前面的编号（01_/02_）就是给人排顺序用的。
        """
        root = Path(self.directory)
        try:
            if not root.exists():
                raise WallpaperError(
                    "壁纸目录不存在: %s（先建目录, 或者改配置里的 wallpaper.dir）"
                    % self.directory
                )
            if not root.is_dir():
                raise WallpaperError("壁纸路径不是目录: %s" % self.directory)
            entries = [e for e in os.scandir(str(root))]
        except OSError as exc:
            raise WallpaperError("壁纸目录读不了 (%s): %s" % (self.directory, exc)) from exc

        picked: List[str] = []
        for entry in entries:
            try:
                if not entry.is_file():
                    continue
            except OSError:
                continue
            name = entry.name
            if Path(name).suffix.lower() not in self.suffixes:
                continue
            if not os.access(entry.path, os.R_OK):
                _log.warning("wallpaper: 跳过读不了的文件 %s", entry.path)
                continue
            picked.append(os.path.abspath(entry.path))

        picked.sort(key=lambda p: (os.path.basename(p).lower(), os.path.basename(p)))
        return picked

    # ------------------------------------------------------------ 游标 ---
    def current(self) -> Optional[str]:
        """当前那张的路径（还没选过就是 None）。"""
        return self._current

    def snapshot(self, initialise: bool = False) -> Optional[Tuple[int, str, int]]:
        """当前那张的 (index, path, total) —— **不动游标**（纯读）。

        @param initialise True 时"还没选过"就选第一张（只改游标, 不推送）——
                          给"GUI 刚连上, 补推当前壁纸"用: 一张都没选过时也得有个初始画面。
        @return None = 目录用不了/没有图片（读不到就当没有, 不抛 —— 调用方是补推, 不是换图）
        """
        try:
            images = self.scan()
        except WallpaperError:
            return None
        if not images:
            return None
        if self._current in images:
            return images.index(self._current), self._current, len(images)
        if not initialise:
            return None
        self._current = images[0]
        return 0, images[0], len(images)

    def count(self) -> int:
        """目录里能用的图片数。**目录坏了算 0** —— 它只用于日志/诊断, 不该抛。

        （真正换壁纸走 step(), 那里目录坏了要报错给用户看。）
        """
        try:
            return len(self.scan())
        except WallpaperError:
            return 0

    def step(self, step: int = 1, pool: Optional[Sequence[str]] = None,
             anchor: Any = _CURSOR) -> Tuple[int, str, int]:
        """往前/往后翻 step 张, 更新游标。

        @param step 正数往后、负数往前、0 = 重推当前那张
        @param pool 只在这些图里翻（**顺序即优先级**, T7-3 的挑图用）:
                    None = 目录里的全部图（按文件名顺序，T3 的老行为）。
                    pool 里已经不在目录里的图会被**丢掉**（图被删了不该挑出个空路径）。
        @param anchor **这次翻页把哪张当成"当前"**（只影响这一次的起点, 不改游标语义）:
                    · 不给（默认）= 用游标里那张（老行为）
                    · `None` = **假装还没选过** → 从候选头/尾开始:
                      `step>0` → 第 1 名（挑图时就是"最像的那张"）;
                      `step<=0` → 也是第 1 名（`step=0` 时"重推当前"没有意义, 给最好的那张）
                      或最后一名（`step<0`）
                    · 给一个路径 = 从它往后/往前翻（"再换一张同类的"就靠它）
        @return (index, path, total) —— index 是它在**这次翻的那份列表**里的下标
        @raise WallpaperError 目录用不了 / 一张图都没有 / step 不是整数 /
                              pool 给了但里面一张能用的都没有
        @note 越界**回绕**（最后一张的下一张 = 第一张）: 目标是"一张一张翻着看",
              翻到头停住反而要多想一步。
        @note ⚠ T7-4 修的一处: 以前"当前那张不在候选里"时统一按 `-1` 算,
              于是 `step=0` 会算出 `(-1+0) % n == n-1` —— **推最后一名**（最不像的那张）。
              现在按 `step` 的正负决定起点, 并且调图那条路会显式传 `anchor=None`
              （"这次是新的挑选, 从第 1 名开始"）, 见 `agent/main.py::next_wallpaper`。
        """
        if isinstance(step, bool) or not isinstance(step, int):
            raise WallpaperError("step 必须是整数（正数往后、负数往前), 得到 %r" % (step,))

        images = self.scan()
        if not images:
            raise WallpaperError(
                "壁纸目录里没有图片（认这些后缀: %s）: %s"
                % (", ".join(self.suffixes), self.directory)
            )

        candidates = images
        if pool is not None:
            available = set(images)
            candidates = [str(path) for path in pool if str(path) in available]
            if not candidates:
                raise WallpaperError(
                    "挑出来的图一张都不在壁纸目录里了"
                    "（目录: %s；换一批条件试试, 或重新跑 assistant tag）" % self.directory
                )

        # 游标是路径, 所以重列目录后仍然指着同一张
        current = self._current if anchor is _CURSOR else anchor
        base = candidates.index(current) if current in candidates else None
        if base is None:
            # 没有"当前"可用: step>0 从 -1 起（step=1 → 第 1 名）;
            # step<=0 从 0 起（step=0 → 第 1 名, step=-1 → 最后一名）
            base = -1 if step > 0 else 0
        target = (base + step) % len(candidates)
        self._current = candidates[target]
        return target, candidates[target], len(candidates)

    def __repr__(self) -> str:
        return "<WallpaperDeck %s (%d 张)>" % (self.directory, self.count())
