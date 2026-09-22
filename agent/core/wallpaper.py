# ============================================================================
#  agent/core/wallpaper.py — 壁纸目录的"下一张"（Phase 7 T3）
#
#  它是什么: 一个目录 + 一个游标。**不碰 IPC、不碰 GUI、不读图片内容** ——
#            只回答"下一张是哪张"。
#
#  谁用它（两条入口共用这一个类, 所以"下一张"的语义只有一份）
#  ---------------------------------------------------------------------------
#      agent/tools/wallpaper.py              LLM 调 next_wallpaper 工具
#      agent/main.py::Runtime.next_wallpaper GUI 的 next_wallpaper 命令（主区"下一张"）
#
#  游标语义（为什么不是"下标 +1"）
#  ---------------------------------------------------------------------------
#  · 每次调用都**重新列一遍目录** —— 你可以随时往里丢新图, 不用重启 Agent。
#  · 因此游标记的是**当前那张的路径**, 不是下标: 新图插在前面也不会让"下一张"跳回去。
#    当前那张不在了（删了/改名了）就从第一张重新开始。
#  · step=0 = 重新推当前那张（GUI 连晚了想补一张时有用）; step<0 = 往前翻。
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
from typing import List, Optional, Sequence, Tuple

__all__ = [
    "WallpaperDeck",
    "WallpaperError",
    "IMAGE_SUFFIXES",
    "DEFAULT_WALLPAPER_DIR",
]

_log = logging.getLogger(__name__)

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

    def count(self) -> int:
        """目录里能用的图片数。**目录坏了算 0** —— 它只用于日志/诊断, 不该抛。

        （真正换壁纸走 step(), 那里目录坏了要报错给用户看。）
        """
        try:
            return len(self.scan())
        except WallpaperError:
            return 0

    def step(self, step: int = 1) -> Tuple[int, str, int]:
        """往前/往后翻 step 张, 更新游标。

        @return (index, path, total) —— index 是它在**这次**列表里的下标（从 0 开始）
        @raise WallpaperError 目录用不了, 或者一张图都没有; step 不是整数
        @note 越界**回绕**（最后一张的下一张 = 第一张）: 目标是"一张一张翻着看",
              翻到头停住反而要多想一步。
        """
        if isinstance(step, bool) or not isinstance(step, int):
            raise WallpaperError("step 必须是整数（正数往后、负数往前), 得到 %r" % (step,))

        images = self.scan()
        if not images:
            raise WallpaperError(
                "壁纸目录里没有图片（认这些后缀: %s）: %s"
                % (", ".join(self.suffixes), self.directory)
            )

        # 游标是路径, 所以重列目录后仍然指着同一张; 它不在了就从头开始
        base = images.index(self._current) if self._current in images else -1
        target = (base + step) % len(images)
        self._current = images[target]
        return target, images[target], len(images)

    def __repr__(self) -> str:
        return "<WallpaperDeck %s (%d 张)>" % (self.directory, self.count())
