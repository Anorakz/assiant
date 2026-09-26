# ============================================================================
#  agent/core/game_anchors.py — **游戏锚点库**（Phase 7 T11-4）
#
#  锚点是什么: 一张**截图** + 它的 SigLIP 图像向量 + 它属于哪个游戏。
#            认游戏就是"这一帧像不像某个已知锚点"（余弦），跟壁纸那边的 IP 锚点检索同源。
#
#  为什么用锚点（T11-0 实测的结论）:
#      · 拿**游戏名当文本**去编码，SigLIP 的文本塔对这类专有名词不靠谱（实测 13 张里
#        10 张"蒙对"，样本太小不能作数）-> **去掉文本锚点**。
#      · 图像锚点 leave-one-out top-1 **9/13 = 69%**：能用，但**不够准**（galgame 之间
#        互相混淆），所以只让它做"很有把握的那部分"，其余交给 PC 进程名裁决（见 game_watch）。
#
#  自学习（老板定的"纠错机制"）: 当**画面**与**进程名**对不上时，以进程为准，
#      并把这一帧**登记成那个游戏的锚点** —— 用得越多越准。
#
#  文件（都进 `.gitignore`，是**派生数据**，换台机器就该重学）:
#      config/game_anchors.jsonl     一行一个锚点: game + 向量(base64 float16) + 截图路径 + 来源 + 时间
#      config/game_anchors/<游戏>/   截图文件本身（自学习时存一张, 供人复核"它当时看到了什么"）
#
#  ⚠ 向量编解码**复用 `agent/vision/wall_data.py`** 的 base64(float16)（一张 ≈ 2 KB），
#    不另造一套格式；余弦用**纯 Python**（锚点几十条, 768 维, 不需要 numpy —— 这样开发机
#    没装 numpy 也能跑单测）。
# ============================================================================

from __future__ import annotations

import json
import logging
import math
import os
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = ["GameAnchors", "AnchorError", "cosine", "DEFAULT_ANCHOR_FILE", "DEFAULT_SHOT_DIR"]

_log = logging.getLogger(__name__)

#: 锚点索引文件（进 .gitignore）。
DEFAULT_ANCHOR_FILE = "config/game_anchors.jsonl"
#: 截图目录（进 .gitignore）。
DEFAULT_SHOT_DIR = "config/game_anchors"


class AnchorError(RuntimeError):
    """锚点库读不了/写不了（消息给人看）。"""


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def resolve_anchor_file(configured: Optional[str] = None) -> str:
    """把配置里的锚点文件路径解析成绝对路径（相对路径按**仓库根**，与其它数据文件同款）。"""
    text = str(configured or "").strip() or DEFAULT_ANCHOR_FILE
    if os.path.isabs(text):
        return os.path.normpath(text)
    return os.path.normpath(os.path.join(_repo_root(), text))


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """两向量的余弦（纯 Python）。**长度不一致或全零 -> 0.0**（不当成相似）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = norm_a = norm_b = 0.0
    for left, right in zip(a, b):
        dot += left * right
        norm_a += left * left
        norm_b += right * right
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return float(dot / math.sqrt(norm_a * norm_b))


class GameAnchors(object):
    """锚点库（内存里一份 + 落盘一份）。

    典型用法::

        anchors = GameAnchors()
        anchors.load()
        best = anchors.match(vector)               # -> {"game","score","runner_up","margin"}
        anchors.add(game="初雪樱", vector=vec, shot_bytes=jpeg, note="进程说是它")
    """

    def __init__(self, path: Optional[str] = None, *, shot_dir: Optional[str] = None,
                 keep_shots: bool = True, log: Optional[logging.Logger] = None) -> None:
        """
        @param keep_shots 自学习时要不要把截图存下来（默认存 —— 出问题要能回看"它当时看到了什么"）
        """
        self.path = resolve_anchor_file(path)
        self.shot_dir = str(shot_dir or os.path.join(_repo_root(), DEFAULT_SHOT_DIR))
        self.keep_shots = bool(keep_shots)
        self.log = log or _log
        self._rows: List[Dict[str, Any]] = []

    # ------------------------------------------------------------ 读 ---
    def __len__(self) -> int:
        return len(self._rows)

    def games(self) -> List[str]:
        """现在认得哪些游戏（去重、排序）。"""
        return sorted({str(row.get("game") or "") for row in self._rows if row.get("game")})

    def rows(self) -> List[Dict[str, Any]]:
        return [dict(row) for row in self._rows]

    def load(self) -> int:
        """读索引文件（不在 = 空库, 不是错误）。@return 读进来几条。"""
        self._rows = []
        if not os.path.exists(self.path):
            return 0
        try:
            with open(self.path, encoding="utf-8") as handle:
                for number, line in enumerate(handle, 1):
                    text = line.strip()
                    if not text:
                        continue
                    try:
                        row = json.loads(text)
                    except ValueError as exc:
                        self.log.warning("game_anchors: 第 %d 行不是 JSON（跳过）: %s", number, exc)
                        continue
                    if isinstance(row, Mapping) and row.get("game") and row.get("vector"):
                        self._rows.append(dict(row))
        except OSError as exc:
            raise AnchorError("锚点文件读不了（%s）: %s" % (self.path, exc)) from exc
        self.log.info("game_anchors: %d 个锚点 / %d 个游戏", len(self._rows), len(self.games()))
        return len(self._rows)

    def vectors(self) -> List[Tuple[str, List[float]]]:
        """[(游戏, 向量)] —— 解不出来的锚点**跳过并如实记一条**（不静默）。"""
        out: List[Tuple[str, List[float]]] = []
        for row in self._rows:
            try:
                out.append((str(row["game"]), decode(str(row["vector"]))))
            except Exception as exc:                       # noqa: BLE001
                self.log.warning("game_anchors: 一条锚点解不出来（跳过）: %s", exc)
        return out

    # ------------------------------------------------------------ 比 ---
    def match(self, vector: Sequence[float]) -> Optional[Dict[str, Any]]:
        """这一帧最像哪个游戏。

        @return None = 库是空的/向量解不出来;
                否则 {"game","score","runner_up","margin","anchors"} ——
                `margin` = 第一名与**别的游戏**里最好那个的差（同游戏的其他锚点不算对手）。
        """
        if not vector:
            return None
        best: Dict[str, float] = {}
        counts: Dict[str, int] = {}
        for game, anchor in self.vectors():
            score = cosine(vector, anchor)
            counts[game] = counts.get(game, 0) + 1
            if game not in best or score > best[game]:
                best[game] = score
        if not best:
            return None
        ranked = sorted(best.items(), key=lambda item: -item[1])
        top_game, top_score = ranked[0]
        runner = ranked[1][1] if len(ranked) > 1 else 0.0
        return {"game": top_game, "score": round(top_score, 4), "runner_up": round(runner, 4),
                "margin": round(top_score - runner, 4), "anchors": counts.get(top_game, 0)}

    # ------------------------------------------------------------ 写 ---
    def add(self, *, game: str, vector: Sequence[float], shot: Optional[bytes] = None,
            note: str = "", source: str = "auto", when: Optional[float] = None) -> Dict[str, Any]:
        """加一个锚点（并落盘 + 存截图）。

        @param shot 截图的原始字节（jpg/png）; None = 只记向量（不存图）
        @return 这一行（`shot` 字段是**仓库相对路径**, 便于人去找那张图）
        @raise AnchorError 写不了（磁盘满/没权限）—— 由调用方决定是忽略还是报给用户
        """
        name = str(game or "").strip()
        if not name:
            raise AnchorError("锚点得有个游戏名")
        values = [float(item) for item in vector or ()]
        if not values:
            raise AnchorError("锚点得有个向量")
        shot_rel = ""
        if shot and self.keep_shots:
            shot_rel = self._save_shot(name, shot, when=when)
        row = {"game": name, "vector": encode(values), "shot": shot_rel,
               "source": str(source or "auto"), "note": str(note or ""),
               "created_at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                           time.localtime(when or time.time()))}
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError as exc:
            raise AnchorError("锚点写不进去（%s）: %s" % (self.path, exc)) from exc
        self._rows.append(row)
        self.log.info("game_anchors: 记了一个锚点 %s（%s, 现在 %d 个）", name, note or "-",
                      len(self._rows))
        return dict(row)

    def _save_shot(self, game: str, shot: bytes, *, when: Optional[float] = None) -> str:
        folder = os.path.join(self.shot_dir, game)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(when or time.time()))
        path = os.path.join(folder, "%s.jpg" % stamp)
        try:
            os.makedirs(folder, exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(shot)
        except OSError as exc:
            self.log.warning("game_anchors: 截图没存下来（忽略）: %s", exc)
            return ""
        try:
            return os.path.relpath(path, _repo_root())
        except ValueError:
            # 跨盘（Windows 上截图目录在别的盘）算不出相对路径 -> 如实记绝对路径
            return path


def encode(vector: Sequence[float]) -> str:
    """向量 -> base64(float16)，复用壁纸那边的编解码（一张 ≈ 2 KB）。"""
    from agent.vision.wall_data import encode_embedding

    return encode_embedding([float(item) for item in vector])


def decode(blob: str) -> List[float]:
    """base64(float16) -> 向量。"""
    from agent.vision.wall_data import decode_embedding

    return decode_embedding(str(blob))
