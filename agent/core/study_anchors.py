# ============================================================================
#  agent/core/study_anchors.py — **学习内容锚点库**（Phase 13 T13-2）
#
#  它是什么: 一张**截图** + 它的 SigLIP 图像向量 + 它属于哪个**子标签**
#            （code / doc / real / anime / game），子标签再经配置映射到**大类**
#            （study / not_study）—— "现在屏幕上是不是学习内容"这个判定，
#            第一步就是问它"这一帧更像哪个大类"。
#
#  判定口径（T13-4 板端标定定的，别改回去）:
#      **两个大类各自的原型**（该类锚点的单位向量平均方向）+ **相对分**
#          relative = cos(帧, study 原型) - cos(帧, not_study 原型)
#      实测（39 张真截图, 板端 NPU）: 相对分口径 2 类准确率 **95–97%**;
#      而"子标签最像的那条锚点"的**绝对**余弦在两个大类之间**重叠**
#      （study 5% 分位 0.796 vs not_study 95% 分位 0.906, 且 study 最小 0.753 <
#      not_study 最大 0.906）—— 按绝对分位中点定阈值会让 **69%** 的帧判不出来。
#      所以: 判定用 `relative`（判定带在 `study_watch.py`），绝对的那几个数只做诊断。
#
#  为什么用锚点而不是文本提示词（板端实测，别改回去）:
#      `llm/multimodal_report.md` §7.2/§7.3 实测: 整屏 256×256 + 文本提示词做零样本
#      分类在 UI 截图上**塌了** —— 设置页/监视器/编辑器全被判成 "terminal"，
#      闭集准确率 58%。文本塔对"这是一屏代码吗"这种**版式**问题没有判别力，
#      所以判据走**图像锚点**（余弦），与认游戏同源。
#
#  为什么与游戏锚点库**分开**（你定的）:
#      · 用途不同: 游戏库要认"哪个作品"（小类），这里只要"像不像学习"（大类）;
#      · 游戏锚点在运行期会被"进程名纠错"**大量自我追加**，混进来会把类别分布压偏，
#        让"非学习"这条结论白捡一个来源;
#      · 生命周期不同: 游戏库在 GAME 状态攒、这里在 STUDY 状态攒。
#      两个文件各自独立，才能分别清空 / 分别复核 / 换台机器各自重学。
#
#  自学习（你定的"阈值动态修正"里**数据**那一半）: 判错的帧登记成锚点，
#      并尽量用**带标签信号**（PC 进程名 / 人工 `assistant study label`）纠正。
#      ⚠ 阈值**怎么动**是 `study_watch.py`（T13-3）的事；本模块只管
#        "存锚点、算余弦、给排序、算类原型"，外加**别长成无限大**
#        （每类上限 `max_per_class`，超了丢**最旧**的）。
#
#  文件（都进 `.gitignore`，是**派生数据**，换台机器就该重学）:
#      config/study_anchors.jsonl        一行一个锚点: 子标签 + 向量 + 截图 + 来源 + 时间
#      config/study_anchors/<子标签>/    截图本身（供人复核"它当时看到了什么"）
#      ⚠ 写入路径只有这个模块；它已登记在 `tests/test_config_source_guard.py`
#        的 `ALLOWED_WRITERS` 里（写的是**派生数据**，配置真源仍然只有 config.yaml）。
#
#  ⚠ 向量编解码与余弦**复用 `agent/core/game_anchors.py`**（它又复用
#    `agent/vision/wall_data.py` 的 base64(float16)，一张 ≈ 2 KB）—— 同一个能力
#    只留一条实现，别在这里再写一份（T13-1 刚因为"两套接口"删过一次东西）。
#    **纯 Python**（不需要 numpy，开发机没装 numpy 也能跑单测）。
#    板端实测（RK3568, 满库 1000 条 × 768 维）: 逐条 `cosine()` = **0.83 s/次**
#    （其中 base64(float16) 解码 0.31 s）；**解码缓存 + 归一化退化成点积 = 稳态 0.31 s**
#    （15 次中位 0.310 s、最慢 0.328 s），冷启动第一次 1.30 s（建缓存）。
#    两者数学上等价 —— 见 `_dot` 的说明与那条钉住它的单测。
# ============================================================================

from __future__ import annotations

import json
import logging
import math
import os
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from agent.core.paths import resolve_config_path

from .game_anchors import cosine, decode, encode

__all__ = [
    "StudyAnchors", "StudyAnchorError", "normalize_classes",
    "cosine", "encode", "decode",
    "DEFAULT_ANCHOR_FILE", "DEFAULT_SHOT_DIR", "DEFAULT_MAX_PER_CLASS",
    "DEFAULT_CLASSES", "CATEGORY_STUDY", "CATEGORY_NOT_STUDY", "CATEGORIES",
]

_log = logging.getLogger(__name__)

#: 锚点索引文件（进 .gitignore）。
DEFAULT_ANCHOR_FILE = "config/study_anchors.jsonl"
#: 截图目录（进 .gitignore）。
DEFAULT_SHOT_DIR = "config/study_anchors"

#: 每个子标签最多留多少条锚点（超了丢**最旧**的）。你定的上限。
DEFAULT_MAX_PER_CLASS = 200

#: 两个大类。判定只关心"是不是学习"，子标签是给人看/给统计用的。
CATEGORY_STUDY = "study"
CATEGORY_NOT_STUDY = "not_study"
CATEGORIES = (CATEGORY_STUDY, CATEGORY_NOT_STUDY)

#: 默认的 子标签 -> 大类 映射（你定的五类；真源在 `config.yaml` 的 `study.classes.*`）。
#: 判据是"动漫/游戏 vs 真人/code/语言": 真人场景（`real`）= 学习, `anime` 场景 = 非学习。
DEFAULT_CLASSES: Dict[str, str] = {
    "code": CATEGORY_STUDY,
    "doc": CATEGORY_STUDY,
    "real": CATEGORY_STUDY,
    "anime": CATEGORY_NOT_STUDY,
    "game": CATEGORY_NOT_STUDY,
}

#: 大类名的大小写/别名归一（配置里写 `Study` / `NOT_STUDY` 都认）。
_CATEGORY_ALIASES = {
    "study": CATEGORY_STUDY,
    "not_study": CATEGORY_NOT_STUDY,
    "notstudy": CATEGORY_NOT_STUDY,
    "non_study": CATEGORY_NOT_STUDY,
    "非学习": CATEGORY_NOT_STUDY,
    "学习": CATEGORY_STUDY,
}


class StudyAnchorError(RuntimeError):
    """锚点库读不了 / 写不了 / 配置不合法（消息给人看）。"""


def _unit(values: Sequence[float]) -> List[float]:
    """向量 -> **单位**向量（全零/空 -> 原样返回，它匹配不上任何人）。"""
    total = 0.0
    for value in values:
        total += value * value
    if total <= 0.0:
        return [float(value) for value in values]
    norm = math.sqrt(total)
    return [float(value) / norm for value in values]


def _dot(unit_a: Sequence[float], unit_b: Sequence[float]) -> float:
    """两个**单位**向量的点积 —— 它就是余弦。

    @note 为什么这里有个"第二份相似度": 板端实测（RK3568, 满库 1000 条 × 768 维）——
          逐条调 `cosine()` 是 **0.83 s/次**（其中解码 0.31 s），而"解码缓存 + 两边先
          归一化 + 只算点积"是 **稳态 0.31 s**（15 次中位，冷启动第一次 1.3 s 建缓存）。
          两者数学上是一回事（实测最大差 1.1e-16，阈值一次只动 0.01）,
          并且有一条单测拿 `cosine()` 当基准钉住它别漂。
          ⚠ 余弦的**定义**仍然只有 `game_anchors.cosine` 一份 —— 这里只是"已经归一化了,
            所以省掉两次范数"的快速路径, 不是第二套相似度。
    """
    score = 0.0
    for left, right in zip(unit_a, unit_b):        # 实测显式 for 比 sum(map(...)) 快一倍
        score += left * right
    return score


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def resolve_anchor_file(configured: Optional[str] = None) -> str:
    """把配置里的锚点文件路径解析成绝对路径（相对路径按**仓库根**，与其它数据文件同款）。"""
    text = str(configured or "").strip() or DEFAULT_ANCHOR_FILE
    return resolve_config_path(configured, DEFAULT_ANCHOR_FILE, _repo_root())


def normalize_classes(raw: Optional[Mapping[str, Any]] = None) -> Dict[str, str]:
    """把配置里的 `study.classes.*` 归一成 `{子标签: 大类}`。

    @param raw None / 空 = 用 `DEFAULT_CLASSES`（五类）
    @raise StudyAnchorError 大类名不认识 / 子标签是空的
    @note ⚠ 只认得出的大类: 写错了要**当场报错**。一个拼错的大类会让那一类的锚点
          永远匹配不出结论（静默变笨），而"屏幕上是学习还是不是"正是这个功能本身。
    """
    if not raw:
        return dict(DEFAULT_CLASSES)
    if not isinstance(raw, Mapping):
        raise StudyAnchorError("study.classes 得是 {子标签: 大类} 的映射, 收到 %r" % (raw,))
    out: Dict[str, str] = {}
    for key, value in raw.items():
        name = str(key or "").strip()
        if not name:
            raise StudyAnchorError("study.classes 里有一个空的子标签名")
        category = _CATEGORY_ALIASES.get(str(value or "").strip().lower())
        if category is None:
            raise StudyAnchorError(
                "study.classes.%s 的大类 %r 不认识（只能是 %s）"
                % (name, value, " / ".join(CATEGORIES)))
        out[name] = category
    if not out:
        return dict(DEFAULT_CLASSES)
    return out


class StudyAnchors(object):
    """学习内容锚点库（内存里一份 + 落盘一份）。

    典型用法::

        anchors = StudyAnchors()
        anchors.load()
        hit = anchors.match(vector)          # -> {"cls","category","score","margin",...}
        anchors.add(cls="code", vector=vec, shot=jpeg, note="进程说是 pycharm64")

    @note `match()` 给的是**子标签**排序 + **大类**聚合。判定吃的是 **`relative`**
          （两个大类原型的余弦之差 —— T13-4 板端实测: 绝对余弦的两个大类会重叠,
          按绝对分位中点定阈值会判出 69% 的"判不出来"; 相对分口径 2 类准确率 95–97%）。
          绝对的那几个数（`category_score` / `category_margin`）留着**做诊断**。
    """

    def __init__(self, path: Optional[str] = None, *, classes: Optional[Mapping[str, Any]] = None,
                 shot_dir: Optional[str] = None, keep_shots: bool = True,
                 max_per_class: int = DEFAULT_MAX_PER_CLASS,
                 allow_unknown_classes: bool = False,
                 log: Optional[logging.Logger] = None) -> None:
        """
        @param classes         子标签 -> 大类 映射（None = 默认五类；配置在 `study.classes.*`）
        @param keep_shots      存锚点时要不要把截图存下来（默认存 —— 出问题要能回看）
        @param max_per_class   每个子标签的上限，超了丢最旧
        @param allow_unknown_classes 允许存"映射里没有的子标签"（默认 False: 拼错的子标签
                              要当场报错，否则那条锚点永远匹配不出结论）
        """
        self.path = resolve_anchor_file(path)
        self.shot_dir = str(shot_dir or os.path.join(_repo_root(), DEFAULT_SHOT_DIR))
        self.classes = normalize_classes(classes)
        self.keep_shots = bool(keep_shots)
        self.max_per_class = max(1, int(max_per_class or DEFAULT_MAX_PER_CLASS))
        self.allow_unknown_classes = bool(allow_unknown_classes)
        self.log = log or _log
        self._rows: List[Dict[str, Any]] = []
        self._prototypes: Optional[Dict[str, List[float]]] = None
        self._category_prototypes: Optional[Dict[str, List[float]]] = None
        self._units: Optional[List[Tuple[str, List[float]]]] = None

    def _invalidate(self) -> None:
        """库变了 -> 缓存（解码后的单位向量、类原型、大类原型）全作废。"""
        self._prototypes = None
        self._category_prototypes = None
        self._units = None

    # ------------------------------------------------------------ 读 ---
    def __len__(self) -> int:
        return len(self._rows)

    def class_names(self) -> List[str]:
        """现在**库里有锚点**的子标签（去重、排序）。"""
        return sorted({str(row.get("cls") or "") for row in self._rows if row.get("cls")})

    def configured_classes(self) -> List[str]:
        """配置认得的全部子标签（排序）。"""
        return sorted(self.classes)

    def rows(self) -> List[Dict[str, Any]]:
        """锚点行（**副本** —— 改它不影响库）。"""
        return [dict(row) for row in self._rows]

    def counts(self) -> Dict[str, int]:
        """每个子标签现在有几条锚点（按子标签名排序；空类不出现）。"""
        out: Dict[str, int] = {}
        for row in self._rows:
            name = str(row.get("cls") or "")
            if name:
                out[name] = out.get(name, 0) + 1
        return {key: out[key] for key in sorted(out)}

    def category_counts(self) -> Dict[str, int]:
        """每个大类现在有几条锚点（两个大类都给，没有就是 0）。"""
        out = {name: 0 for name in CATEGORIES}
        for name, count in self.counts().items():
            category = self.classes.get(name)
            if category:
                out[category] = out.get(category, 0) + count
        return out

    def category_of(self, cls: str) -> str:
        """子标签 -> 大类（不认识的子标签 -> `not_study` 之外的**空串**? 不: 抛错更吵）。

        @note 库里的子标签一定是映射里的（`add()` 拦过）；真遇到不认识的（比如手工
              改过 jsonl），返回空串并记一条 warning —— 让它**匹配不出结论**，
              而不是悄悄归到"学习"或"非学习"里去。
        """
        name = str(cls or "")
        category = self.classes.get(name)
        if category is None:
            self.log.warning("study_anchors: 子标签 %r 不在 study.classes 里（跳过）", name)
            return ""
        return category

    def load(self) -> int:
        """读索引文件（不在 = 空库，不是错误）。@return 读进来几条。"""
        self._rows = []
        self._invalidate()
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
                        self.log.warning("study_anchors: 第 %d 行不是 JSON（跳过）: %s",
                                         number, exc)
                        continue
                    if isinstance(row, Mapping) and row.get("cls") and row.get("vector"):
                        self._rows.append(dict(row))
        except OSError as exc:
            raise StudyAnchorError("锚点文件读不了（%s）: %s" % (self.path, exc)) from exc
        self.log.info("study_anchors: %d 个锚点 / %s", len(self._rows), self.counts())
        return len(self._rows)

    def vectors(self) -> List[Tuple[str, List[float]]]:
        """[(子标签, 向量)] —— 解不出来的锚点**跳过并如实记一条**（不静默）。"""
        out: List[Tuple[str, List[float]]] = []
        for row in self._rows:
            try:
                out.append((str(row["cls"]), decode(str(row["vector"]))))
            except Exception as exc:                       # noqa: BLE001
                self.log.warning("study_anchors: 一条锚点解不出来（跳过）: %s", exc)
        return out

    def unit_vectors(self) -> List[Tuple[str, List[float]]]:
        """[(子标签, **单位**向量)] —— 匹配真正用的那份（解码一次就缓存）。

        @note 板端实测: 1000 条锚点上, "每次 match 重新解码 0.31 s + 逐条 cosine 0.50 s"
              是 0.83 s; 缓存解码 + 归一化后退化成点积是**稳态 0.31 s**（冷启动第一次
              1.3 s 建缓存）。库变了（add / reset / prune / load）会作废缓存。
        """
        if self._units is None:
            self._units = [(cls, _unit(vec)) for cls, vec in self.vectors()]
        return [(cls, list(vec)) for cls, vec in self._units]

    def prototypes(self) -> Dict[str, List[float]]:
        """每个子标签的**类原型**（它所有锚点的**单位向量**逐维平均）—— 板端标定的第二种打法。

        @note 为什么平均的是单位向量而不是原始向量: SigLIP 的图像塔本来输出 L2 归一化向量,
              但 float16 落盘会带一点缩放；先归一化再平均, 得到的是"平均方向"
              （不受某条向量模长偏大的影响），也正好与匹配的快速路径一致。
        @note 算一次就缓存（库变了就作废）。
        """
        if self._prototypes is not None:
            return {key: list(value) for key, value in self._prototypes.items()}
        sums: Dict[str, List[float]] = {}
        counts: Dict[str, int] = {}
        for cls, vector in self.unit_vectors():
            bucket = sums.get(cls)
            if bucket is None:
                bucket = [0.0] * len(vector)
                sums[cls] = bucket
            if len(bucket) != len(vector):                 # 维度不一致的锚点不参与平均
                self.log.warning("study_anchors: %s 有一条锚点维度不对（不参与原型）", cls)
                continue
            for index, value in enumerate(vector):
                bucket[index] += value
            counts[cls] = counts.get(cls, 0) + 1
        out: Dict[str, List[float]] = {}
        for cls, bucket in sums.items():
            count = counts.get(cls) or 0
            if not count:
                continue
            out[cls] = _unit([value / count for value in bucket])
        self._prototypes = out
        return {key: list(value) for key, value in out.items()}

    def category_prototypes(self) -> Dict[str, List[float]]:
        """每个**大类**的原型（该大类所有锚点的单位向量平均方向）—— 判定的真正依据。

        @note 为什么判定不用"子标签里最像的那一条锚点"的**绝对**余弦（T13-4 板端实测）:
              39 张真截图上, study 的绝对分 5% 分位 0.796、not_study 的 95% 分位 0.906,
              而 study 最小 0.753 < not_study 最大 0.906 —— **两个大类的绝对分重叠**,
              按分位中点定阈值会让 **69%** 的帧"判不出来"。
              换成"两个大类各自的原型 + 相对分"（`match()["relative"]`）之后:
              2 类准确率 95–97%, 在 ±0.05 的没把握带下 误打扰 0 / 监督失效 1 / 判不出 8%。
        @note 算一次就缓存（库变了就作废）。
        """
        if self._category_prototypes is not None:
            return {key: list(value) for key, value in self._category_prototypes.items()}
        sums: Dict[str, List[float]] = {}
        counts: Dict[str, int] = {}
        for cls, vector in self.unit_vectors():
            category = self.classes.get(cls)
            if not category:
                continue                                    # 映射外的子标签不参与大类原型
            bucket = sums.get(category)
            if bucket is None:
                bucket = [0.0] * len(vector)
                sums[category] = bucket
            if len(bucket) != len(vector):
                self.log.warning("study_anchors: %s 有一条锚点维度不对（不参与大类原型）", cls)
                continue
            for index, value in enumerate(vector):
                bucket[index] += value
            counts[category] = counts.get(category, 0) + 1
        out: Dict[str, List[float]] = {}
        for category, bucket in sums.items():
            count = counts.get(category) or 0
            if not count:
                continue
            out[category] = _unit([value / count for value in bucket])
        self._category_prototypes = out
        return {key: list(value) for key, value in out.items()}

    def relative(self, vector: Sequence[float]) -> Optional[Dict[str, Any]]:
        """**两个大类原型的余弦之差** —— "更像学习还是更像非学习"。

        @return None = 有一边大类**一条锚点都没有**（原型无从谈起）;
                否则 `{"relative","study","not_study"}`（后两个是各自的余弦）
        @note 正数 = 更像 study。**两个大类都得有锚点才算得出来** —— 只有一个大类时
              它必然"更像自己", 那种结论没有信息量, 所以这里如实返回 None,
              由调用方（`study_watch`）走"进程名辅助"或判 unknown。
        """
        if not vector:
            return None
        prototypes = self.category_prototypes()
        study = prototypes.get(CATEGORY_STUDY)
        not_study = prototypes.get(CATEGORY_NOT_STUDY)
        if not study or not not_study:
            return None
        query = _unit([float(item) for item in vector])
        left = _dot(query, study)
        right = _dot(query, not_study)
        return {"relative": float(left - right), "study": float(left), "not_study": float(right)}

    # ------------------------------------------------------------ 比 ---
    def match(self, vector: Sequence[float], *, method: str = "anchor") -> Optional[Dict[str, Any]]:
        """这一帧最像哪个子标签、属于哪个大类。

        @param method "anchor" = 逐条锚点取最大（`DEFAULT`）; "prototype" = 与类原型比
        @return None = 库是空的 / 向量解不出来;
                否则::

                    {"cls","category","score","runner_up_cls","runner_up_score","margin",
                     "anchors",                      # 命中的那一类有几条锚点
                     "category_score","category_runner_up","category_runner_up_score",
                     "category_margin",              # 绝对口径（**诊断用**; 两个大类重叠, 别拿它判定）
                     "relative","study_cos","not_study_cos",   # 判定吃这三个（大类原型之差）
                     "counts","method"}

        @note `margin` = 第一名与**别的子标签**里最好那个的差（同类其他锚点不算对手）;
              `category_margin` 同理，但只在大类之间比（`study` vs `not_study`）。
        @note ⚠ `category_score`/`category_margin` 是**绝对**余弦，T13-4 实测两个大类会重叠，
              **不要**拿它跟绝对值阈值比（那会判出 69% 的"判不出来"）。判定的依据是
              `relative`（= `study_cos - not_study_cos`，由 `category_prototypes()` 算）。
              两个大类里只要有一边**一条锚点都没有**，`relative` 就是 None —— 那时
              调用方该走进程名辅助或判 unknown。
        """
        if not vector:
            return None
        wanted = str(method or "anchor").lower()
        samples: Iterable[Tuple[str, Sequence[float]]]
        if wanted == "prototype":
            samples = list(self.prototypes().items())
        else:
            samples = self.unit_vectors()
        query = _unit([float(item) for item in vector])
        best: Dict[str, float] = {}
        for cls, anchor in samples:
            score = _dot(query, anchor)
            if cls not in best or score > best[cls]:
                best[cls] = score
        if not best:
            return None
        ranked = sorted(best.items(), key=lambda item: -item[1])
        top_cls, top_score = ranked[0]
        runner = ranked[1][1] if len(ranked) > 1 else 0.0
        top_category = self.category_of(top_cls)

        # 大类聚合: 每个大类取它**最像的那一类**的分数（学习帧常常同时像 code 和 doc,
        # 那不是"分不开" —— 它们本来就同一个大类，只有跨大类才说明判定犹豫）。
        per_category: Dict[str, float] = {}
        for cls, score in best.items():
            category = self.category_of(cls)
            if not category:
                continue
            if category not in per_category or score > per_category[category]:
                per_category[category] = score
        cat_ranked = sorted(per_category.items(), key=lambda item: -item[1])
        cat_top, cat_score = cat_ranked[0] if cat_ranked else ("", 0.0)
        cat_runner = cat_ranked[1][1] if len(cat_ranked) > 1 else 0.0
        counts = self.counts()
        prototypes = self.category_prototypes()
        study_cos = not_study_cos = None
        relative = None
        if prototypes.get(CATEGORY_STUDY) and prototypes.get(CATEGORY_NOT_STUDY):
            study_cos = _dot(query, prototypes[CATEGORY_STUDY])
            not_study_cos = _dot(query, prototypes[CATEGORY_NOT_STUDY])
            relative = study_cos - not_study_cos
        return {
            "cls": top_cls,
            "category": top_category,
            "score": round(top_score, 4),
            "runner_up_cls": ranked[1][0] if len(ranked) > 1 else "",
            "runner_up_score": round(runner, 4),
            "margin": round(top_score - runner, 4),
            "anchors": counts.get(top_cls, 0),
            "category_score": round(cat_score, 4),
            "category_runner_up": cat_ranked[1][0] if len(cat_ranked) > 1 else "",
            "category_runner_up_score": round(cat_runner, 4),
            "category_margin": round(cat_score - cat_runner, 4),
            "relative": None if relative is None else round(relative, 4),
            "study_cos": None if study_cos is None else round(study_cos, 4),
            "not_study_cos": None if not_study_cos is None else round(not_study_cos, 4),
            "counts": counts,
            "method": "prototype" if wanted == "prototype" else "anchor",
        }

    # ------------------------------------------------------------ 写 ---
    def add(self, *, cls: str, vector: Sequence[float], shot: Optional[bytes] = None,
            note: str = "", source: str = "auto", when: Optional[float] = None) -> Dict[str, Any]:
        """加一个锚点（并落盘 + 存截图 + 按上限丢最旧）。

        @param cls    子标签（必须在 `study.classes` 里，除非开了 `allow_unknown_classes`）
        @param shot   截图原始字节（jpg/png）; None = 只记向量
        @return 这一行（`shot` 字段是**仓库相对路径**，便于人去找那张图）
        @raise StudyAnchorError 子标签不认识 / 向量是空的 / 写不了
        """
        name = str(cls or "").strip()
        if not name:
            raise StudyAnchorError("锚点得有个子标签（code / doc / real / anime / game）")
        if name not in self.classes and not self.allow_unknown_classes:
            raise StudyAnchorError(
                "子标签 %r 不在 study.classes 里（认得的: %s）—— 先在配置里给它一个"
                "大类（study 或 not_study），否则这条锚点永远匹配不出结论"
                % (name, " / ".join(self.configured_classes())))
        values = [float(item) for item in vector or ()]
        if not values:
            raise StudyAnchorError("锚点得有个向量")
        shot_rel = ""
        if shot and self.keep_shots:
            shot_rel = self._save_shot(name, shot, when=when)
        row = {"cls": name, "vector": encode(values), "shot": shot_rel,
               "source": str(source or "auto"), "note": str(note or ""),
               "created_at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                           time.localtime(when or time.time()))}
        previous = list(self._rows)
        self._rows.append(row)
        self._invalidate()
        dropped = self.prune(save=False)
        try:
            if dropped:
                # ⚠ 丢过最旧的就不能只追加: 那几条还在文件里, 下次 load() 又把上限撑破。
                #   整篇重写（原子写）才是"文件和内存一致"的那一步。
                self._rewrite()
            else:
                self._append(row)
        except (OSError, StudyAnchorError) as exc:
            # 落盘失败: 内存那份也退回去, 别留下"库里有、文件里没有"的不一致
            self._rows = previous
            self._invalidate()
            raise StudyAnchorError("锚点写不进去（%s）: %s" % (self.path, exc)) from exc
        self.log.info("study_anchors: 记了一个锚点 %s（%s, 现在 %d 个%s）", name, note or "-",
                      len(self._rows), "，丢了 %d 条最旧的" % dropped if dropped else "")
        return dict(row)

    def prune(self, cls: Optional[str] = None, *, keep: Optional[int] = None,
              save: bool = True) -> int:
        """把超上限的**最旧**锚点丢掉。@return 丢了几条。

        @param cls  None = 每个子标签都按自己的上限查
        @param keep 这一类的上限（None = `max_per_class`）
        @note 上限是**每类**的（不是总数）: 学习帧会源源不断自学习进来，
              没有这条就是"越用文件越大、匹配越慢"，最后一定在某天变成卡顿。
        """
        limit = max(1, int(keep if keep is not None else self.max_per_class))
        targets = [str(cls)] if cls else sorted({str(row.get("cls") or "") for row in self._rows})
        dropped = 0
        for name in targets:
            same = [row for row in self._rows if str(row.get("cls") or "") == name]
            if len(same) <= limit:
                continue
            cut = len(same) - limit
            dropped += cut
            for row in same[:cut]:                          # 最旧的先丢（插入顺序 = 时间顺序）
                self._rows.remove(row)
            self.log.info("study_anchors: %s 超过上限（%d > %d），丢了最旧的 %d 条",
                          name, len(same), limit, cut)
        if dropped:
            self._invalidate()
            if save:
                self._rewrite()
        return dropped

    def reset(self, cls: Optional[str] = None, *, save: bool = True) -> int:
        """清空锚点（`cls=None` = 全清，否则只清那一类）。@return 这条**内存里**清掉几条。

        @note 这是给人用的开关（`assistant study reset`）: 学歪了要能一键回到干净状态，
              而不是让人去手改 jsonl。
        @note ⚠ `save=True` 时**总是**把文件写成内存里这份 —— 哪怕这个实例还没 `load()`
              过（那时 `_rows` 是空的, 于是文件被清空）。为什么这么定: T13-4 标定脚本
              就踩过 —— `StudyAnchors(path).reset()` 之后又 `add()` 39 条, 结果**追加**
              在原来那 39 条后面, 库里变成 78 条（每条锚点重复一遍）。
              "清空"的语义就是"文件里也清空", 忘了 load 却以为清掉了才更糟。
        """
        if cls is None:
            removed = len(self._rows)
            self._rows = []
        else:
            name = str(cls)
            before = len(self._rows)
            self._rows = [row for row in self._rows if str(row.get("cls") or "") != name]
            removed = before - len(self._rows)
        self._invalidate()
        if save:
            self._rewrite()
        return removed

    def _append(self, row: Mapping[str, Any]) -> None:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(dict(row), ensure_ascii=False) + "\n")
        except OSError as exc:
            raise StudyAnchorError("锚点写不进去（%s）: %s" % (self.path, exc)) from exc

    def _rewrite(self) -> None:
        """把内存里这份**原子地**写回文件（丢最旧 / 清空之后用）。

        @note 走 `agent/config.py::write_text_atomic`（全仓唯一的原子写文本实现）——
              写一半掉电时文件要么是旧的完整内容、要么是新的完整内容。
        """
        from agent.config import write_text_atomic

        text = "".join(json.dumps(dict(row), ensure_ascii=False) + "\n" for row in self._rows)
        try:
            write_text_atomic(self.path, text)
        except OSError as exc:
            raise StudyAnchorError("锚点文件写不回去（%s）: %s" % (self.path, exc)) from exc

    def _save_shot(self, cls: str, shot: bytes, *, when: Optional[float] = None) -> str:
        folder = os.path.join(self.shot_dir, cls)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(when or time.time()))
        path = os.path.join(folder, "%s.jpg" % stamp)
        try:
            os.makedirs(folder, exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(shot)
        except OSError as exc:
            self.log.warning("study_anchors: 截图没存下来（忽略）: %s", exc)
            return ""
        try:
            return os.path.relpath(path, _repo_root())
        except ValueError:
            # 跨盘（Windows 上截图目录在别的盘）算不出相对路径 -> 如实记绝对路径
            return path

    # ------------------------------------------------------------ 杂 ---
    def snapshot(self) -> Dict[str, Any]:
        """给日志 / `assistant study status` 用的一眼可读状态。"""
        return {"anchors": len(self._rows), "counts": self.counts(),
                "categories": self.category_counts(), "classes": self.classes,
                "max_per_class": self.max_per_class, "file": self.path}
