# -*- coding: utf-8 -*-
"""agent/core/anchor_io.py —— 两个锚点库共用的两段机械动作（T15-3 / 3-6b）

谁在用: `agent/core/game_anchors.py`（认游戏）与 `agent/core/study_anchors.py`
（认"这一屏像不像学习内容"）。这两库里各有一段**逐字节相同**的实现:

  · `vectors()` 里"逐行解码向量, 解不出来的跳过它自己";
  · `_save_shot()` 里"按 `<截图目录>/<标签>/<时间戳>.jpg` 落一张图 + 返回给人看的路径"。

为什么单独一个文件、而不是放在其中**一个**库里: 这段动作不属于任何一方的语义
（游戏库不是学习库的下层, 反过来也不是）—— 两个库各自 import 它, 谁也不欠谁。

⚠ 这里**只做机械动作, 不写日志**: 文案（是哪个库、什么口气）留在各自的模块里,
   所以"解不出来记一条"由调用方以回调交进来; 也**不 import 那两个库**（否则成环）,
   解码器同样由调用方传进来。
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable, Iterable, List, Mapping, Optional, Tuple


def decode_rows(rows: Iterable[Mapping[str, Any]], key: str,
                decoder: Callable[[str], List[float]],
                on_error: Optional[Callable[[Exception], None]] = None
                ) -> List[Tuple[str, List[float]]]:
    """锚点行 -> [(标签, 向量)]；**一条解不出来只跳过它自己**（不拖垮整库）。

    @param key      哪一列是标签（游戏库是 `game`, 学习库是 `cls`）
    @param decoder  形状 `decoder(blob) -> 向量`（由调用方传, 免得这里 import 锚点库成环）
    @param on_error 形状 `on_error(异常) -> None`；调用方用它"如实记一条"（不许静默）
    @return 顺序与 `rows` 一致, 跳过的不占位
    """
    out: List[Tuple[str, List[float]]] = []
    for row in rows:
        try:
            out.append((str(row[key]), decoder(str(row["vector"]))))
        except Exception as exc:                    # noqa: BLE001 - 单条坏数据不该让整库读不出来
            if on_error is not None:
                on_error(exc)
    return out


def save_shot(shot_dir: str, name: str, shot: bytes, *, when: Optional[float] = None,
              root: str = "") -> str:
    """把一张截图落成 `<shot_dir>/<name>/<时间戳>.jpg`, 返回**给人看的路径**。

    @param when 时间戳（None = 现在）—— 只用来起文件名
    @param root 仓库根；给了就返回相对路径（便于人按仓库根去找那张图）
    @return 相对 `root` 的路径；跨盘算不出来 -> 绝对路径
    @raise OSError 写不进去（磁盘满 / 没权限）—— 由调用方决定记不记、算不算失败
    """
    folder = os.path.join(shot_dir, name)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(when or time.time()))
    path = os.path.join(folder, "%s.jpg" % stamp)
    os.makedirs(folder, exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(shot)
    if not root:
        return path
    try:
        return os.path.relpath(path, root)
    except ValueError:
        # 跨盘（Windows 上截图目录在别的盘）算不出相对路径 -> 如实记绝对路径
        return path
