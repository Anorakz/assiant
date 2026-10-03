# ============================================================================
#  agent/core/study_stats.py — **学习监督的运行统计**（Phase 13 T13-2）
#
#  它是什么: 一个**有界的** JSON 文件（config/study_stats.json），只干一件事 ——
#            把"这个功能跑成什么样了"记下来，好让阈值能**越用越准**（你定的"阈值不定死"）。
#
#  里面有什么（全部**有界**，见下面每条的上限）:
#      thresholds  当前阈值（分类门槛 / 余量门槛 / 学习门槛 …）—— 标定给初值,
#                  运行期由 `study_watch.py`（T13-3）一点点修正，改一次记一条理由
#      ewma        阈值自适应用到的指数滑动平均（T13-3）—— **它也得落盘**,
#                  否则 Agent 一重启就退回标定初值,"运行中不断优化"只优化到下次重启
#      counters    数出来的事: 看了多少帧 / 判了几次 study / 提醒了几次 / 弹了几次桌面 …
#      histogram   分数分布（每 0.05 一档）—— 自适应阈值要**看着分布**动，不看分布
#                  就等于拍脑袋; 三类各一份: study / not_study / unknown
#      samples     最近 N 次判定的明细（分数、余量、结论、动作），给人复核用
#      notes       最近 N 条"为什么动了阈值 / 出了什么事"
#
#  为什么单独一个模块、单独一个文件:
#      · 锚点库（`study_anchors.py`）是**几百 KB 的数据**，这份是**诊断与自适应状态**;
#        混在一个文件里，任何一次阈值微调都要重写全部锚点（还可能写坏）。
#      · 它必须**有界**: 一个跑几个月的板子不该把内存/磁盘吃光 —— 直方图固定档数、
#        明细与备注都是定长环形，计数器也封了上限（写错名字的键会被拒绝并记一条）。
#      · 坏文件**不能拖垮功能**: 读不出来就当没有（warning + 默认值），
#        统计是给人看的，不能因为它让"该提醒的时候没提醒"。
#
#  ⚠ 这里**只有存储，没有策略**。"什么时候动阈值、动多少、什么时候冻结"是
#    `study_watch.py`（T13-3）的事 —— 分开写才能单独测"存的对不对"。
#  ⚠ 写入路径只有这个模块；它已登记在 `tests/test_config_source_guard.py` 的
#    `ALLOWED_WRITERS` 里（写的是**派生数据**，配置真源仍然只有 config.yaml）。
# ============================================================================

from __future__ import annotations

import json
import logging
import math
import os
import time
from typing import Any, Deque, Dict, Iterable, List, Mapping, Optional, Sequence

from agent.core.paths import resolve_config_path

from collections import deque

__all__ = [
    "StudyStats", "DEFAULT_STATS_FILE", "SCHEMA_VERSION",
    "HISTOGRAM_BUCKETS", "MAX_NOTES", "MAX_SAMPLES", "MAX_COUNTERS", "MAX_THRESHOLDS",
    "MAX_EWMA", "CATEGORY_KEYS",
]

_log = logging.getLogger(__name__)

#: 统计文件（进 .gitignore）—— 运行期派生数据，换台机器就该从头攒。
DEFAULT_STATS_FILE = "config/study_stats.json"

#: 文件格式版本（以后改结构时用它判断旧文件能不能直接用）。
SCHEMA_VERSION = 1

#: 分数直方图分几档（20 档 = 每 0.05 一档，覆盖 0..1）。
HISTOGRAM_BUCKETS = 20

#: 备注与样本明细各留多少条（环形，超了丢最旧）。
MAX_NOTES = 50
MAX_SAMPLES = 200

#: 计数器的键上限（防止拼错的名字把文件撑成垃圾场）。
MAX_COUNTERS = 64

#: 阈值项上限（同上）。
MAX_THRESHOLDS = 16

#: 阈值自适应用的 EWMA 项上限（`study_watch.py` 在跑，只有几项）。
MAX_EWMA = 16

#: 直方图的三份: 真有标签的 study / not_study，以及"判不出来"的 unknown。
#: ⚠ `unknown` **完全中性**（你定的）: 它只是被记下来，不参与任何"往哪边动"的判断。
CATEGORY_KEYS = ("study", "not_study", "unknown")


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def resolve_stats_file(configured: Optional[str] = None) -> str:
    """把配置里的统计文件路径解析成绝对路径（相对路径按**仓库根**，与其它数据文件同款）。"""
    text = str(configured or "").strip() or DEFAULT_STATS_FILE
    return resolve_config_path(configured, DEFAULT_STATS_FILE, _repo_root())


def bucket_of(score: float, buckets: int = HISTOGRAM_BUCKETS) -> int:
    """分数 -> 直方图档号（0.0 进第 0 档，1.0 进最后一档，区间外的夹住）。"""
    count = max(1, int(buckets))
    try:
        value = float(score)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(value):
        return 0
    index = int(value * count)
    if index < 0:
        return 0
    if index >= count:
        return count - 1
    return index


class StudyStats(object):
    """学习监督的运行统计（内存一份 + JSON 落盘一份，全程有界）。

    典型用法::

        stats = StudyStats()
        stats.load()
        stats.bump("frames")
        stats.record(cls="code", category="study", score=0.93, margin=0.05, verdict="study")
        stats.note("把分类门槛从 0.80 提到 0.81（最近 40 次带标签样本里 study 的最低分是 0.82）")
        stats.save()
    """

    def __init__(self, path: Optional[str] = None, *, buckets: int = HISTOGRAM_BUCKETS,
                 max_notes: int = MAX_NOTES, max_samples: int = MAX_SAMPLES,
                 max_counters: int = MAX_COUNTERS, max_thresholds: int = MAX_THRESHOLDS,
                 max_ewma: int = MAX_EWMA,
                 clock: Any = time.time, log: Optional[logging.Logger] = None) -> None:
        self.path = resolve_stats_file(path)
        self.buckets = max(1, int(buckets or HISTOGRAM_BUCKETS))
        self.max_notes = max(1, int(max_notes or MAX_NOTES))
        self.max_samples = max(1, int(max_samples or MAX_SAMPLES))
        self.max_counters = max(1, int(max_counters or MAX_COUNTERS))
        self.max_thresholds = max(1, int(max_thresholds or MAX_THRESHOLDS))
        self.max_ewma = max(1, int(max_ewma or MAX_EWMA))
        self.clock = clock
        self.log = log or _log
        self._counters: Dict[str, int] = {}
        self._thresholds: Dict[str, float] = {}
        self._ewma: Dict[str, float] = {}
        self._histogram: Dict[str, List[int]] = {key: [0] * self.buckets for key in CATEGORY_KEYS}
        self._samples: Deque[Dict[str, Any]] = deque(maxlen=self.max_samples)
        self._notes: Deque[Dict[str, Any]] = deque(maxlen=self.max_notes)
        self.updated_at = 0.0

    # ------------------------------------------------------------ 读/写 ---
    def load(self) -> Dict[str, Any]:
        """读统计文件。**读不出来不是错误** —— 记一条 warning，从默认值起步。

        @return 这份统计的字典形式（`to_dict()`），方便调用方直接用
        @note 为什么要容忍坏文件: 它是诊断数据。为了"统计文件坏了"让监督功能起不来
              （或者抛异常把主循环打断）完全是本末倒置。
        """
        if not os.path.exists(self.path):
            return self.to_dict()
        try:
            with open(self.path, encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError) as exc:
            self.log.warning("study_stats: 统计文件读不出来（当成空的继续）: %s", exc)
            return self.to_dict()
        if not isinstance(raw, Mapping):
            self.log.warning("study_stats: 统计文件不是个对象（当成空的继续）")
            return self.to_dict()
        self.from_dict(raw)
        return self.to_dict()

    def save(self) -> None:
        """原子落盘（同目录临时文件 + `os.replace`，走全仓唯一的原子写实现）。

        @raise OSError 写盘失败 —— 原样抛出，由调用方决定是忽略还是说出来
        @note ⚠ 调用方（`study_watch.py`）应当**吞掉**这个异常: 统计写不进去
              不该让"该提醒的时候不提醒"。这里不吞，是因为"存的对不对"要被单测看见。
        """
        from agent.config import write_text_atomic

        self.updated_at = float(self.clock())
        text = json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        write_text_atomic(self.path, text)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": SCHEMA_VERSION,
            "updated_at": self.updated_at,
            "thresholds": dict(self._thresholds),
            "ewma": dict(self._ewma),
            "counters": dict(self._counters),
            "histogram": {key: list(value) for key, value in self._histogram.items()},
            "samples": [dict(item) for item in self._samples],
            "notes": [dict(item) for item in self._notes],
        }

    def from_dict(self, raw: Mapping[str, Any]) -> None:
        """把一份字典装回来（**不认识/不合法的部分跳过并记录**，不抛）。"""
        version = raw.get("version")
        if version not in (None, SCHEMA_VERSION):
            self.log.warning("study_stats: 文件版本 %r 与当前 %d 不同（只认识的字段照读）",
                             version, SCHEMA_VERSION)
        thresholds = raw.get("thresholds")
        if isinstance(thresholds, Mapping):
            self._thresholds = {}
            for name, value in thresholds.items():
                try:
                    self.set_threshold(str(name), value)
                except ValueError as exc:
                    self.log.warning("study_stats: 阈值 %r 不合法（跳过）: %s", name, exc)
        counters = raw.get("counters")
        if isinstance(counters, Mapping):
            self._counters = {}
            for name, value in counters.items():
                try:
                    self._counters[str(name)] = int(value)
                except (TypeError, ValueError):
                    self.log.warning("study_stats: 计数器 %r 不是整数（跳过）", name)
        ewma = raw.get("ewma")
        if isinstance(ewma, Mapping):
            self._ewma = {}
            for name, value in ewma.items():
                try:
                    self.set_ewma(str(name), value)
                except ValueError as exc:
                    self.log.warning("study_stats: EWMA %r 不合法（跳过）: %s", name, exc)
        histogram = raw.get("histogram")
        if isinstance(histogram, Mapping):
            for key in CATEGORY_KEYS:
                values = histogram.get(key)
                if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
                    continue
                self._histogram[key] = self._clean_bucket(values)
        for item in raw.get("samples") or ():
            if isinstance(item, Mapping):
                self._samples.append(dict(item))
        for item in raw.get("notes") or ():
            if isinstance(item, Mapping):
                self._notes.append(dict(item))
        try:
            self.updated_at = float(raw.get("updated_at") or 0.0)
        except (TypeError, ValueError):
            self.updated_at = 0.0

    def _clean_bucket(self, values: Iterable[Any]) -> List[int]:
        """把读到的一档计数修成**正好 `buckets` 个非负整数**（多了截、少了补 0）。"""
        out: List[int] = []
        for value in list(values)[: self.buckets]:
            try:
                out.append(max(0, int(value)))
            except (TypeError, ValueError):
                out.append(0)
        out.extend([0] * (self.buckets - len(out)))
        return out

    # ------------------------------------------------------------ 阈值 ---
    def thresholds(self) -> Dict[str, float]:
        return dict(self._thresholds)

    def threshold(self, name: str, default: Optional[float] = None) -> Optional[float]:
        value = self._thresholds.get(str(name))
        return default if value is None else float(value)

    def set_threshold(self, name: str, value: Any) -> float:
        """设一个阈值。@raise ValueError 名字是空的 / 值不是有限数 / 项数超上限。"""
        key = str(name or "").strip()
        if not key:
            raise ValueError("阈值得有个名字")
        if key not in self._thresholds and len(self._thresholds) >= self.max_thresholds:
            raise ValueError("阈值项太多了（上限 %d）—— 是不是名字拼错了？" % self.max_thresholds)
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError("阈值 %s 得是数字, 收到 %r" % (key, value))
        if not math.isfinite(number):
            raise ValueError("阈值 %s 得是有限数, 收到 %r" % (key, value))
        self._thresholds[key] = number
        return number
    def update_thresholds(self, values: Mapping[str, Any]) -> Dict[str, float]:
        """批量设阈值（**先全验一遍再落**：半套阈值比旧值更危险）。"""
        checked: Dict[str, float] = {}
        for name, value in (values or {}).items():
            key = str(name or "").strip()
            if not key:
                raise ValueError("阈值得有个名字")
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise ValueError("阈值 %s 得是数字, 收到 %r" % (key, value))
            if not math.isfinite(number):
                raise ValueError("阈值 %s 得是有限数, 收到 %r" % (key, value))
            checked[key] = number
        if len(self._thresholds) + len([k for k in checked if k not in self._thresholds]) \
                > self.max_thresholds:
            raise ValueError("阈值项太多了（上限 %d）" % self.max_thresholds)
        self._thresholds.update(checked)
        return dict(self._thresholds)

    # ------------------------------------------------------------ EWMA ---
    def ewma_all(self) -> Dict[str, float]:
        """阈值自适应用的指数滑动平均（`study_watch.py` 的"越用越准"就靠它）。"""
        return dict(self._ewma)

    def ewma(self, name: str, default: Optional[float] = None) -> Optional[float]:
        value = self._ewma.get(str(name))
        return default if value is None else float(value)

    def set_ewma(self, name: str, value: Any) -> float:
        """设一项 EWMA。@raise ValueError 名字是空的 / 值不是有限数 / 项数超上限。

        @note 为什么 EWMA 要**落盘**: 它是"学到了什么"的那部分（阈值该往哪边挪）。
              不落盘的话, Agent 一重启就退回到标定初值 —— 你定的"运行中不断优化"
              就永远只优化到下一次重启。
        """
        key = str(name or "").strip()
        if not key:
            raise ValueError("EWMA 得有个名字")
        if key not in self._ewma and len(self._ewma) >= self.max_ewma:
            raise ValueError("EWMA 项太多了（上限 %d）—— 是不是名字拼错了？" % self.max_ewma)
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError("EWMA %s 得是数字, 收到 %r" % (key, value))
        if not math.isfinite(number):
            raise ValueError("EWMA %s 得是有限数, 收到 %r" % (key, value))
        self._ewma[key] = number
        return number

    def push_ewma(self, name: str, value: float, *, alpha: float = 0.1) -> float:
        """把一个新观测揉进 EWMA: `new = (1-alpha)*old + alpha*value`（首样直接落）。

        @return 揉完之后的值
        """
        try:
            weight = min(1.0, max(0.0, float(alpha)))
        except (TypeError, ValueError):
            weight = 0.1
        old = self._ewma.get(str(name))
        number = float(value)
        if old is None:
            return self.set_ewma(name, number)
        return self.set_ewma(name, (1.0 - weight) * float(old) + weight * number)

    # ------------------------------------------------------------ 计数 ---
    def counters(self) -> Dict[str, int]:
        return dict(self._counters)

    def counter(self, name: str) -> int:
        return int(self._counters.get(str(name), 0))

    def bump(self, name: str, n: int = 1) -> int:
        """计数 +n。@return 加完之后的值（**新键超过上限时不加**，返回 0 并记一条）。"""
        key = str(name or "").strip()
        if not key:
            return 0
        if key not in self._counters and len(self._counters) >= self.max_counters:
            self.log.warning("study_stats: 计数器太多了（上限 %d），%r 没记上",
                             self.max_counters, key)
            return 0
        try:
            step = int(n)
        except (TypeError, ValueError):
            step = 1
        self._counters[key] = int(self._counters.get(key, 0)) + step
        return self._counters[key]

    # ------------------------------------------------------------ 分布 ---
    def histogram(self) -> Dict[str, List[int]]:
        """分数分布（三份各 `buckets` 档）。"""
        return {key: list(value) for key, value in self._histogram.items()}

    def observe(self, category: str, score: float) -> int:
        """把一次观察记进分布（类别不认识 -> 记进 unknown 并 warning）。

        @return 落进的档号
        """
        key = str(category or "").strip()
        if key not in CATEGORY_KEYS:
            self.log.warning("study_stats: 不认识的类别 %r（记进 unknown）", category)
            key = "unknown"
        index = bucket_of(score, self.buckets)
        self._histogram[key][index] += 1
        return index

    def quantile(self, category: str, q: float) -> Optional[float]:
        """从分布里估一个分位数（给自适应阈值用；样本为 0 -> None）。

        @note 用**档的下沿 + 半档**当代表值，够用：阈值每次只动 0.01（你定的护栏），
              不需要比 0.025 更细的分辨率。
        """
        key = str(category or "").strip()
        values = self._histogram.get(key)
        if not values:
            return None
        total = sum(values)
        if total <= 0:
            return None
        want = max(0.0, min(1.0, float(q))) * total
        seen = 0
        width = 1.0 / self.buckets
        for index, count in enumerate(values):
            seen += count
            if seen >= want:
                return round((index + 0.5) * width, 4)
        return round((len(values) - 0.5) * width, 4)

    # ------------------------------------------------------------ 明细 ---
    def record(self, *, cls: str = "", category: str = "", score: float = 0.0,
               margin: float = 0.0, verdict: str = "", action: str = "",
               note: str = "", when: Optional[float] = None,
               relative: Optional[float] = None, labeled: str = "",
               picture: str = "") -> Dict[str, Any]:
        """记一条判定明细（环形，只留最近 `max_samples` 条）。@return 这一条。

        @param relative 相对分（两个大类原型余弦之差; T13-4 起判定吃它）
        @param labeled  这一次的**真值大类**（进程名/人工给的; "" = 没有真值）
        @param picture  **画面单独**给出的结论（"" = 画面没把握）——
                        "带标签样本里有没有判反"要看它, 不能看最终结论
                       （走进程名那条路时最终结论**等于**真值, 看它永远看不出判反）
        """
        item = {"at": float(when if when is not None else self.clock()),
                "cls": str(cls or ""), "category": str(category or ""),
                "score": round(float(score or 0.0), 4), "margin": round(float(margin or 0.0), 4),
                "verdict": str(verdict or ""), "action": str(action or ""),
                "note": str(note or ""), "labeled": str(labeled or ""),
                "picture": str(picture or ""),
                "relative": None if relative is None else round(float(relative), 4)}
        self._samples.append(item)
        return dict(item)

    def samples(self) -> List[Dict[str, Any]]:
        return [dict(item) for item in self._samples]

    def last_sample(self) -> Optional[Dict[str, Any]]:
        return dict(self._samples[-1]) if self._samples else None

    def note(self, text: str, *, when: Optional[float] = None) -> Dict[str, Any]:
        """记一条备注（"为什么动了阈值"就写这儿；环形，只留最近 `max_notes` 条）。"""
        item = {"at": float(when if when is not None else self.clock()), "text": str(text or "")}
        self._notes.append(item)
        return dict(item)

    def notes(self) -> List[Dict[str, Any]]:
        return [dict(item) for item in self._notes]

    # ------------------------------------------------------------ 清空 ---
    def reset(self, *, keep_thresholds: bool = True) -> None:
        """清空统计。

        @param keep_thresholds 默认**留下阈值和 EWMA** —— 你只是"想重新数一遍"，
               不该顺手把学到的阈值也扔了；真要全清（回到标定初值）再传 False。
        """
        self._counters = {}
        self._histogram = {key: [0] * self.buckets for key in CATEGORY_KEYS}
        self._samples.clear()
        self._notes.clear()
        if not keep_thresholds:
            self._thresholds = {}
            self._ewma = {}

    def snapshot(self) -> Dict[str, Any]:
        """给日志 / `assistant study status` 用的一眼可读状态。"""
        return {"file": self.path, "thresholds": self.thresholds(), "ewma": self.ewma_all(),
                "counters": self.counters(), "histogram": self.histogram(),
                "samples": len(self._samples), "notes": len(self._notes),
                "updated_at": self.updated_at}
