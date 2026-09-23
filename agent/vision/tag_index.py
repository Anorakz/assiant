# ============================================================================
#  agent/vision/tag_index.py — 标签索引 + 锚点检索（Phase 7 T7-3）
#
#  它是什么
#  ---------------------------------------------------------------------------
#  把 `config/wall_data.jsonl`（T7-2 打的标签 + 向量）读进来，回答两个问题:
#     1) 库里有哪些标签、各有几张  -> 给 LLM 看"我能挑什么"
#     2) "哪几张像 X"             -> 按标签挑, 或者按 **IP 锚点** 挑
#
#  为什么纯 Python、不过 NPU
#  ---------------------------------------------------------------------------
#  图像向量与词表向量都已经躺在数据文件里了，挑图就是**点积** ——
#  768 维 × 40 张 = 3 万次乘加，纯 Python 也是毫秒级。所以:
#     · Agent 挑图**不再拉 NPU**（打标签才需要 NPU，那是离线的 `assistant tag`）
#     · 开发机（没 numpy）也能跑同一份代码，测试不必 skip
#
#  为什么能算"标签没进 top-k"的分数
#  ---------------------------------------------------------------------------
#  图片记录里只存了每轴 top-k（默认 3）的标签，但**词表头**（第一行）存了全部
#  32 条标签的向量。所以 "scene=anime" 的分数可以现场用
#  `图像向量 · anime向量` 重新算 —— 不受 top-k 截断影响（`_label_vector`）。
#  数据文件没有词表头时（旧文件）退回"只看存下来的标签"，并**如实说明**。
#
#  IP 为什么是"锚点检索"而不是分类
#  ---------------------------------------------------------------------------
#  见 docs/tagging.md §3: SigLIP 不认识作品名，中文/英文作品名过文本塔基本是噪声。
#  这里做的事: 把 `wallpaper.tagging.ip_presets` 里那个 IP 的**锚点图**向量求平均
#  当原型，再与全库算余弦。锚点图必须**已经打过标签**（先跑 `assistant tag`），
#  没打过的锚点会被跳过并如实报出来。
#
#  边界（谁知道什么）
#  ---------------------------------------------------------------------------
#  · 本模块**只读**数据文件，一个字节都不写（唯一写者是 `assistant tag --apply`）。
#  · 它**不认识配置文件**: 数据文件路径、ip_presets、壁纸目录都由调用方
#    （`agent/main.py::Runtime`）从 config.yaml 取好再传进来 —— 这样它既能被
#    单独测试，也不会变成"第二个读配置的地方"。
#  · 分数是**余弦**（不是概率，也没有校准过的阈值）: 排序有意义，绝对值不要当置信度。
# ============================================================================

from __future__ import annotations

import logging
import math
import os
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from . import wall_data

__all__ = [
    "TagIndex",
    "TagIndexError",
    "MatchResult",
    "IP_KEY",
    "DEFAULT_LIMIT",
]

_log = logging.getLogger(__name__)

#: 挑图结果默认给几条（给 LLM 看的清单不该太长）
DEFAULT_LIMIT = 5

#: `match` 语法里 IP 用的"轴名"（`ip=EVA`）—— 不是真轴，是路由到锚点检索的开关
IP_KEY = "ip"


class TagIndexError(ValueError):
    """数据文件读不了 / 记录不像标签数据。

    继承 ValueError，与仓库里其他"输入不对"的错误一致。
    """


# ---------------------------------------------------------------------------
#  匹配结果
# ---------------------------------------------------------------------------
class MatchResult(object):
    """一次 `match=` 的解析结果。

    @ivar pool   命中的图片路径，**按相关度降序**（挑图就按这个顺序翻）
    @ivar kind   "ip" / "label" / "axis"（给日志与回话用）
    @ivar note   一句**给人看**的说明（会经工具结果回到对话里），可以空
    @ivar error  非空 = 没解析成功，`pool` 一定是空的
    @ivar detail 额外事实（标签、轴、分数、跳过的锚点…），给回话用
    """

    def __init__(self, pool: Sequence[str] = (), kind: str = "", note: str = "",
                 error: Optional[str] = None,
                 detail: Optional[Mapping[str, Any]] = None) -> None:
        self.pool = list(pool)
        self.kind = kind
        self.note = note
        self.error = error
        self.detail: Dict[str, Any] = dict(detail or {})

    @property
    def ok(self) -> bool:
        """解析成功了吗（成功也可能 pool 为空 —— 那是"库是空的"）。"""
        return self.error is None

    def __repr__(self) -> str:
        return "<MatchResult ok=%s kind=%s pool=%d%s>" % (
            self.ok, self.kind or "-", len(self.pool),
            " error=%s" % self.error if self.error else "")


# ---------------------------------------------------------------------------
#  向量小工具（纯 Python）
# ---------------------------------------------------------------------------
def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    """两个等长向量的点积（长度不等就按短的算 —— 数据坏了不该让挑图崩）。"""
    return float(sum(x * y for x, y in zip(a, b)))


def _norm(vector: Sequence[float]) -> float:
    return math.sqrt(sum(x * x for x in vector))


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """余弦相似度。**自己除模长**，不假设两边已经归一化。

    @note 数据文件里存的是 L2 归一化过的向量，理论上直接点积就是余弦；
          这里仍然除一遍模长: 万一哪天向量来源变了（或有人手改了文件），
          "排序悄悄变了"比"多算一次开方"贵得多。
    """
    na, nb = _norm(a), _norm(b)
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return _dot(a, b) / (na * nb)


def normalise(vector: Sequence[float]) -> List[float]:
    """L2 归一化（零向量原样返回）。"""
    n = _norm(vector)
    if n <= 0.0:
        return list(vector)
    return [x / n for x in vector]


# ---------------------------------------------------------------------------
#  索引
# ---------------------------------------------------------------------------
class TagIndex(object):
    """库里的标签 + 向量（只读）。

    典型用法::

        index = TagIndex.from_file("/…/config/wall_data.jsonl")
        index.label_counts()                       # 有哪些标签、各有几张
        index.match("scene=anime", presets, dir)   # 按标签挑
        index.match("ip=EVA", presets, dir)        # 按锚点挑
    """

    def __init__(self, records: Iterable[Mapping[str, Any]] = (),
                 vocab: Optional[Mapping[str, Any]] = None,
                 problems: Iterable[str] = (),
                 data_file: str = "") -> None:
        """
        @param records  图片记录（`wall_data.load()["records"]`）
        @param vocab    词表头记录（`load()["vocab"]`，可能没有）——
                        有它才能算"没进 top-k 的标签"的分数
        @param problems 读文件时记下的问题（原样带出来，给工具如实回话）
        """
        self.data_file = str(data_file or "")
        self.vocab = dict(vocab) if isinstance(vocab, Mapping) else None
        self.problems = [str(p) for p in problems or ()]
        self._records: List[Dict[str, Any]] = [
            dict(r) for r in records or ()
            if isinstance(r, Mapping) and str(r.get("path") or "")
        ]
        self._by_path: Dict[str, Dict[str, Any]] = {
            self._path_of(r): r for r in self._records
        }
        self._vectors: Dict[str, List[float]] = {}
        for record in self._records:
            vector = self._decode_embedding(record)
            if vector is not None:
                self._vectors[self._path_of(record)] = vector
        #: 记录里出现过的轴（按首次出现的顺序，稳）
        self._axes: List[str] = []
        for record in self._records:
            tags = record.get("tags")
            if not isinstance(tags, Mapping):
                continue
            for axis in tags:
                if str(axis) not in self._axes:
                    self._axes.append(str(axis))

    # ------------------------------------------------------------ 构造 ---
    @classmethod
    def from_file(cls, path: str) -> "TagIndex":
        """从数据文件建索引。**文件不存在 = 空索引**（还没打过标签，不是错误）。"""
        try:
            loaded = wall_data.load(path)
        except wall_data.WallDataError as exc:
            raise TagIndexError(str(exc))
        return cls(loaded["records"], loaded["vocab"], loaded["problems"], data_file=path)

    # ------------------------------------------------------------ 基本 ---
    @staticmethod
    def _path_of(record: Mapping[str, Any]) -> str:
        return str(record.get("path"))

    def _decode_embedding(self, record: Mapping[str, Any]) -> Optional[List[float]]:
        blob = record.get("embedding")
        if not isinstance(blob, str) or not blob:
            return None
        try:
            return wall_data.decode_embedding(blob)
        except wall_data.WallDataError as exc:
            _log.warning("tag_index: %s 的向量解不开 (%s) —— 这张图只按标签用",
                         record.get("path"), exc)
            return None

    def __len__(self) -> int:
        return len(self._records)

    def count(self) -> int:
        return len(self._records)

    def records(self) -> List[Dict[str, Any]]:
        """图片记录（原样，顺序就是文件里的顺序）。"""
        return list(self._records)

    def paths(self) -> List[str]:
        return [self._path_of(r) for r in self._records]

    def axes(self) -> List[str]:
        """记录里出现过的轴名（空数据文件 → 空列表）。"""
        return list(self._axes)

    def has_vector(self, path: str) -> bool:
        return path in self._vectors

    def vector_of(self, path: str) -> Optional[List[float]]:
        vector = self._vectors.get(path)
        return list(vector) if vector is not None else None

    def tags_of(self, path: str) -> Dict[str, List[List[Any]]]:
        """这张图存下来的 top-k 标签 {轴: [[标签, 分数], …]}。"""
        record = self._by_path.get(path)
        tags = record.get("tags") if record else None
        if not isinstance(tags, Mapping):
            return {}
        return {str(axis): [list(pair) for pair in pairs or ()]
                for axis, pairs in tags.items()}

    # ------------------------------------------------------------ 词表 ---
    def label_vector(self, axis: str, label: str) -> Optional[List[float]]:
        """从**词表头**取某条标签的向量（没有词表头/没这条标签 → None）。

        @note 这是"能算 top-k 之外的标签"的关键: 词表头存了全部标签的向量。
        """
        if not isinstance(self.vocab, Mapping):
            return None
        embeds = self.vocab.get("embeds")
        labels = self.vocab.get("axes")
        if not isinstance(embeds, Mapping) or not isinstance(labels, Mapping):
            return None
        axis_labels = [str(x) for x in (labels.get(axis) or ())]
        blobs = list(embeds.get(axis) or ())
        try:
            position = axis_labels.index(str(label))
        except ValueError:
            return None
        if position >= len(blobs):
            return None
        try:
            return wall_data.decode_vector(blobs[position], dtype="float32")
        except wall_data.WallDataError as exc:
            _log.warning("tag_index: 词表里 %s/%s 的向量解不开 (%s)", axis, label, exc)
            return None

    def has_vocab_vectors(self) -> bool:
        """词表头里有没有可用的标签向量（没有就只能看存下来的 top-k）。"""
        return isinstance(self.vocab, Mapping) and bool(self.vocab.get("embeds"))

    def labels(self, axis: str) -> List[str]:
        """某个轴上**词表里**的标签（没有词表头就退回记录里出现过的）。"""
        if isinstance(self.vocab, Mapping):
            labels = self.vocab.get("axes")
            if isinstance(labels, Mapping) and labels.get(axis):
                return [str(x) for x in labels[axis]]
        found: List[str] = []
        for record in self._records:
            for pair in (record.get("tags") or {}).get(axis) or ():
                if isinstance(pair, (list, tuple)) and pair and str(pair[0]) not in found:
                    found.append(str(pair[0]))
        return found

    # ------------------------------------------------------------ 统计 ---
    def label_counts(self, axis: Optional[str] = None, top: Optional[int] = None,
                     top_k: int = 3) -> Dict[str, List[Dict[str, Any]]]:
        """每轴各标签命中几张（**按记录里存下来的 top-k 标签**数，不重算）。

        @param top    每轴最多给几条（None = 全给）
        @param top_k  "算命中"要看前几名 —— 默认 3，与打标签时的 top-k 对齐
        @return {轴: [{"label": …, "count": n}, …]}（按张数降序、同数按标签名）
        """
        wanted = [axis] if axis else self.axes()
        out: Dict[str, List[Dict[str, Any]]] = {}
        for name in wanted:
            bucket: Dict[str, int] = {}
            for record in self._records:
                pairs = (record.get("tags") or {}).get(name) or ()
                for pair in list(pairs)[:max(1, int(top_k))]:
                    if isinstance(pair, (list, tuple)) and pair:
                        key = str(pair[0])
                        bucket[key] = bucket.get(key, 0) + 1
            items = [{"label": label, "count": n} for label, n in bucket.items()]
            items.sort(key=lambda item: (-item["count"], item["label"]))
            out[name] = items[:top] if top else items
        return out

    def known_labels(self) -> Dict[str, List[str]]:
        """{轴: [标签, …]} —— 词表里有就全给（给"你说这个词我不认识"时列候选用）。"""
        return {axis: self.labels(axis) for axis in (self.axes() or list(self._vocab_axes()))}

    def _vocab_axes(self) -> List[str]:
        if isinstance(self.vocab, Mapping):
            labels = self.vocab.get("axes")
            if isinstance(labels, Mapping):
                return [str(a) for a in labels]
        return []

    def summarise(self, top: Optional[int] = DEFAULT_LIMIT) -> Dict[str, Any]:
        """数据文件摘要（`list_wallpaper_tags` 不带参数时回的就是它）。"""
        return {
            "count": len(self._records),
            "data_file": self.data_file,
            "axes": self.label_counts(top=top),
            "with_vectors": len(self._vectors),
            "vocab_vectors": self.has_vocab_vectors(),
            "problems": list(self.problems),
        }

    # ------------------------------------------------------------ 排序 ---
    def rank_by_vector(self, vector: Sequence[float],
                       limit: Optional[int] = None,
                       exclude: Iterable[str] = ()) -> List[Dict[str, Any]]:
        """按与 `vector` 的余弦给全库排序。

        @return [{"path", "score", "tags"}, …]，分数降序
        """
        skip = set(exclude or ())
        scored: List[Dict[str, Any]] = []
        for path, other in self._vectors.items():
            if path in skip:
                continue
            scored.append({"path": path, "score": cosine(vector, other),
                           "tags": self.tags_of(path)})
        scored.sort(key=lambda item: (-item["score"], item["path"]))
        return scored[:limit] if limit else scored

    def rank_by_label(self, axis: str, label: str,
                      limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """按"与某条标签的余弦"排序（**不受 top-k 截断影响**，只要有词表头）。

        @return [{"path", "score", "tags"}, …]；没有词表头时退回"只认存下来的标签"
                （命中者用它存下来的那个分数）
        """
        vector = self.label_vector(axis, label)
        if vector is not None and self._vectors:
            return self.rank_by_vector(vector, limit=limit)

        scored: List[Dict[str, Any]] = []
        for record in self._records:
            for pair in (record.get("tags") or {}).get(axis) or ():
                if isinstance(pair, (list, tuple)) and len(pair) >= 2 \
                        and str(pair[0]) == str(label):
                    scored.append({"path": self._path_of(record), "score": float(pair[1]),
                                   "tags": self.tags_of(self._path_of(record))})
                    break
        scored.sort(key=lambda item: (-item["score"], item["path"]))
        return scored[:limit] if limit else scored

    def rank_by_labels(self, axis: str, labels: Sequence[str],
                       limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """**多条标签的"任一命中"**排序：每张图取它在这些标签里的**最高分**。

        为什么要它: 板端实测模型会把一串标签用斜杠拼在一起当一条用
        （`scene=space/technology/fantasy/anime`）。老实报错当然也算"如实",
        但用户的意图显然是"这几个里随便挑一张像的" —— 所以按"任一命中"算,
        `detail["why"]` 里记下**是哪条标签**给它打的分。

        @return [{"path", "score", "tags", "label"}, …]，分数降序（同分按路径）
        """
        wanted = [str(x) for x in labels or () if str(x).strip()]
        if not wanted:
            return []
        if len(wanted) == 1:
            hits = self.rank_by_label(axis, wanted[0], limit=limit)
            return [dict(hit, label=wanted[0]) for hit in hits]

        best: Dict[str, Dict[str, Any]] = {}
        for label in wanted:
            for hit in self.rank_by_label(axis, label):
                path = str(hit["path"])
                current = best.get(path)
                if current is None or hit["score"] > current["score"]:
                    best[path] = dict(hit, label=label)
        scored = sorted(best.values(), key=lambda item: (-item["score"], item["path"]))
        return scored[:limit] if limit else scored

    # ------------------------------------------------------------ 锚点 ---
    def anchor_prototype(self, anchors: Sequence[str]) -> Tuple[Optional[List[float]],
                                                               List[str], List[str]]:
        """把锚点图的向量求平均当 IP 原型（L2 归一化）。

        @return (原型向量 | None, 用上的锚点, 没用上的锚点)
                · 原型是 None = 一个锚点都没用上（都没打过标签 / 都没有向量）
                · 没算进去的锚点 **如实返回**, 不假装它参与了
        """
        used: List[str] = []
        missing: List[str] = []
        total: Optional[List[float]] = None
        for path in anchors or ():
            vector = self._vectors.get(str(path))
            if vector is None:
                missing.append(str(path))
                continue
            used.append(str(path))
            total = list(vector) if total is None else [a + b for a, b in zip(total, vector)]
        if total is None:
            return None, used, missing
        return normalise(total), used, missing

    # ------------------------------------------------------------ 挑图 ---
    def match(self, spec: str, presets: Optional[Mapping[str, Any]] = None,
              wallpaper_dir: Optional[str] = None,
              limit: Optional[int] = None) -> MatchResult:
        """把一句 `match=` 解析成"按相关度排好的图片列表"。

        语法（三条，都很短）::

            scene=anime      某个轴上的某条标签（轴名必须是真轴）
            anime            只写标签名 -> 在**所有轴**里找同名标签
            ip=EVA           某个 IP 的锚点原型最像的图（锚点来自配置）

        @param presets       `wallpaper.tagging.ip_presets`（{名字: {anchors: [...]}} 或 {名字: [文件]}）
        @param wallpaper_dir 锚点文件名相对它解析
        @param limit         最多给几条（None = 全给）
        """
        text = spec.strip() if isinstance(spec, str) else ""
        if not text:
            return MatchResult(error="match 是空的 —— 要么别给，要么写成 scene=anime / ip=EVA")

        key, value = _split_spec(text)
        if key is None:                       # 只写了一个标签名（或 "=anime" 这种手滑）
            return self._match_bare_label(value, limit=limit)
        if key == IP_KEY:
            return self._match_ip(value, presets, wallpaper_dir, limit=limit)
        if key in self.axes() or key in self._vocab_axes():
            return self._match_axis(key, value, limit=limit)
        known = self.axes() or self._vocab_axes()
        return MatchResult(
            error="不认识的轴 %r：可用的有 %s；IP 要写成 ip=名字（例如 ip=EVA）"
                  % (key, "、".join(known) if known else "（数据文件里一个轴都没有）"))

    # ---- match 的三个分支 ----
    def _match_axis(self, axis: str, value: str, limit: Optional[int]) -> MatchResult:
        """`轴=标签`（**可以用 `/`、`,`、空格 写多条**，按"任一命中"算）。"""
        known = self.labels(axis)
        wanted, unknown = _pick_labels(value, known)
        if not wanted:
            return MatchResult(error="%s 轴上没有 %s 这条标签：可用的有 %s"
                                     % (axis, value, "、".join(known)))
        hits = self.rank_by_labels(axis, wanted, limit=limit)
        # ⚠ 措辞要小心: 这是**排序**不是"命中" —— 分数没标定（docs/tagging.md §8），
        #   所以任何"符合/命中 N 张"的说法都会骗人。全库都有分数, 只是高低不同。
        shown = "/".join(wanted) if len(wanted) > 1 else wanted[0]
        note = "%s=%s：全库 %d 张按相似度排序（最像的在最前；分数是余弦，没标定过）" % (
            axis, shown, len(self))
        if len(wanted) > 1:
            note = "%s=%s（%d 条标签**任一命中**，每张取最高分）：全库 %d 张按相似度排序" % (
                axis, shown, len(wanted), len(self))
        if unknown:
            note += "（忽略了不认识的标签: %s）" % "、".join(unknown)
        if not self.has_vocab_vectors():
            note += "（数据文件里没有词表向量，只能看打标签时存下来的 top-k）"
        return MatchResult(pool=[h["path"] for h in hits], kind="axis", note=note,
                           detail={"axis": axis, "label": wanted[0], "labels": wanted,
                                   "unknown": unknown,
                                   "why": {str(h["path"]): h.get("label") for h in hits
                                           if len(wanted) > 1},
                                   "scores": _score_map(hits),
                                   "top": hits[:DEFAULT_LIMIT]})

    def _match_bare_label(self, value: str, limit: Optional[int]) -> MatchResult:
        """只写了标签名（**也可以写多条**）—— 在所有轴里找同名标签。"""
        all_axes = self.axes() or self._vocab_axes()
        every = sorted({x for labels in self.known_labels().values() for x in labels})
        wanted, unknown = _pick_labels(value, every)
        if not wanted:
            return MatchResult(
                error="词表里没有 %s 这条标签：有的是 %s"
                      % (value, "、".join(every) if every else "（数据文件里没有任何标签）"))

        hits: List[Dict[str, Any]] = []
        used_axes: List[str] = []
        for axis in all_axes:
            axis_labels = [label for label in wanted if label in self.labels(axis)]
            if not axis_labels:
                continue
            used_axes.append(axis)
            hits.extend(self.rank_by_labels(axis, axis_labels))
        hits.sort(key=lambda item: (-item["score"], item["path"]))
        # 同一张图可能同时命中两个轴（或两条标签）—— 去重（留分高的那次）
        unique: Dict[str, Dict[str, Any]] = {}
        for hit in hits:
            unique.setdefault(hit["path"], hit)
        ordered = list(unique.values())
        if limit:
            ordered = ordered[:limit]
        shown = "/".join(wanted)
        note = "%s：全库 %d 张按相似度排序（标签在 %s 轴上；分数没标定过）" % (
            shown, len(self), "、".join(used_axes))
        if len(wanted) > 1:
            note += "；多标签按**任一命中**算"
        if unknown:
            note += "（忽略了不认识的标签: %s）" % "、".join(unknown)
        return MatchResult(pool=[h["path"] for h in ordered], kind="label", note=note,
                           detail={"label": wanted[0], "labels": wanted,
                                   "unknown": unknown, "axes": used_axes,
                                   "why": {str(h["path"]): h.get("label") for h in ordered
                                           if len(wanted) > 1},
                                   "scores": _score_map(ordered),
                                   "top": ordered[:DEFAULT_LIMIT]})

    def _match_ip(self, name: str, presets: Optional[Mapping[str, Any]],
                  wallpaper_dir: Optional[str], limit: Optional[int]) -> MatchResult:
        table = {str(k): v for k, v in (presets or {}).items()}
        if not table:
            return MatchResult(
                error="配置里没有 ip_presets —— IP 检索要靠锚点图，先在 "
                      "config.yaml 的 wallpaper.tagging.ip_presets 里给这个作品放 3-5 张锚点图")

        # 名字大小写不敏感: LLM 很可能把 EVA 写成 eva
        picked = table.get(name)
        if picked is None:
            for key, value in table.items():
                if key.lower() == name.lower():
                    picked, name = value, key
                    break
        if picked is None:
            return MatchResult(
                error="ip_presets 里没有 %r：可用的有 %s"
                      % (name, "、".join(sorted(table))))

        anchors = [str(x) for x in _anchors_of(picked)]
        if not anchors:
            return MatchResult(error="%s 在 ip_presets 里没有 anchors（一张锚点图都没有）" % name)
        resolved = [_resolve(anchor, wallpaper_dir) for anchor in anchors]
        prototype, used, missing = self.anchor_prototype(resolved)
        if prototype is None:
            return MatchResult(
                error="%s 的锚点图还没打过标签（向量不在数据文件里）：%s —— "
                      "先跑 assistant tag" % (name, "、".join(os.path.basename(p) for p in missing)))
        hits = self.rank_by_vector(prototype, limit=limit)
        note = "%s：用 %d 张锚点图的平均向量检索，全库 %d 张按相似度排序" % (
            name, len(used), len(self))
        if missing:
            note += "（%d 张锚点没打过标签，没算进去: %s）" % (
                len(missing), "、".join(os.path.basename(p) for p in missing))
        return MatchResult(pool=[h["path"] for h in hits], kind="ip", note=note,
                           detail={"ip": name, "anchors": used, "missing": missing,
                                   "scores": _score_map(hits), "top": hits[:DEFAULT_LIMIT]})

    def __repr__(self) -> str:
        return "<TagIndex %d 张, 轴=%s, 词表向量=%s>" % (
            len(self._records), ",".join(self._axes) or "-", self.has_vocab_vectors())


# ---------------------------------------------------------------------------
#  match 语法的零件
# ---------------------------------------------------------------------------
def _split_spec(text: str) -> Tuple[Optional[str], str]:
    """把 `scene=anime` / `ip: EVA` 拆成 (键, 值)。

    @return (None, 原文本) = 没有 `=`/`:`（只写了一个标签名）
    @note 键统一小写（轴名本来就是小写；`IP`/`ip` 都认）；
          `=anime`（键是空的）按"只写了标签名"处理 —— 用户手滑不该报错。
    """
    for separator in ("=", ":"):
        position = text.find(separator)
        if position < 0:
            continue
        key = text[:position].strip().lower()
        value = text[position + 1:].strip()
        if not key:
            return None, value
        return key, value
    return None, text.strip()


def _pick_labels(value: str, known: Sequence[str]) -> Tuple[List[str], List[str]]:
    """把 `space/technology/fantasy` 这样的值拆成多条标签，并对上词表。

    @param known 词表里真有的标签（**大小写敏感** —— 标签本身是小写英文裸串）
    @return (认得的标签, 不认得的标签)，各自保持书写顺序、去重
    @note 分隔符认 `/ , ; 、 ，` 与空白: 板端实测模型会用斜杠把一串标签拼起来
          （`scene=space/technology/fantasy/anime`）, 也会用逗号或空格。词表里的标签
          都是**不含空白**的裸串, 所以这样切不会误伤。
    @note 认得的照用、不认得的**如实报出来**（`unknown`），不是静默丢掉。
    """
    parts = [part for part in _LABEL_SPLIT_RE.split(value or "") if part]
    if not parts:
        parts = [(value or "").strip()] if (value or "").strip() else []
    wanted: List[str] = []
    unknown: List[str] = []
    table = {str(label): str(label) for label in known or ()}
    for part in parts:
        label = table.get(part)
        if label is None:
            if part not in unknown:
                unknown.append(part)
            continue
        if label not in wanted:
            wanted.append(label)
    return wanted, unknown


#: 拆多标签用的分隔符（半角/全角斜杠逗号分号 + 顿号 + 空白）
_LABEL_SPLIT_RE = re.compile(r"[\/,;、，；\s]+")


def _anchors_of(preset: Any) -> List[str]:
    """从 ip_presets 的一项里取锚点列表。

    两种写法都认: `{query: …, anchors: [a.png, b.png]}` 与 `[a.png, b.png]`。
    """
    if isinstance(preset, Mapping):
        anchors = preset.get("anchors")
        if isinstance(anchors, str):
            return [anchors]
        if isinstance(anchors, (list, tuple)):
            return [str(x) for x in anchors]
        return []
    if isinstance(preset, str):
        return [preset]
    if isinstance(preset, (list, tuple)):
        return [str(x) for x in preset]
    return []


def _resolve(path: str, wallpaper_dir: Optional[str]) -> str:
    """锚点路径: 绝对路径原样用；相对路径**相对壁纸目录**（与配置注释一致）。"""
    text = str(path)
    if os.path.isabs(text) or not wallpaper_dir:
        return os.path.normpath(text)
    return os.path.normpath(os.path.join(str(wallpaper_dir), text))


def _score_map(hits: Sequence[Mapping[str, Any]]) -> Dict[str, float]:
    """{路径: 分数}（回话里带上，便于"为什么是这张"）。"""
    return {str(hit["path"]): round(float(hit["score"]), 4) for hit in hits}


def preset_names(presets: Optional[Mapping[str, Any]]) -> List[str]:
    """ip_presets 里的名字（给 `list_wallpaper_tags` 回话用），排序稳定。"""
    return sorted(str(k) for k in (presets or {}))
