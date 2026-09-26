# ============================================================================
#  agent/core/study_watch.py — **学习内容监督**（Phase 13 T13-3）
#
#  它干什么: 每隔一段时间看一眼**串流屏幕**（`ImageReader.read_latest()` 的 256×256 帧），
#            判断"现在屏幕上是不是学习内容"。不是 -> 气泡提醒一次; 提醒之后还是不是 ->
#            每 5 分钟复查, 连续 3 次不通过 -> **返回桌面**(Win+D), 然后冷却 30 分钟。
#
#  判定**不进 LLM**（你定的）: 模型不参与判决。判据是**图像锚点余弦**
#      （`study_anchors.py`，与认游戏同源）; PC 进程名只在画面没把握时当**辅助**。
#
#  三条结论（第三态是一等公民）:
#      study      像学习（真人场景 / 代码 / 文档）
#      not_study  不像学习（动漫 / 游戏场景）
#      unknown    **判不出来 —— 完全中性**（你定的）: 不提醒、不动计数器、不弹桌面、
#                 不算"通过"也不算"不通过"（既不推进也不重置那条升级链）
#
#  为什么"冲突"要判成 unknown 而不是像认游戏那样"以进程为准"（想清楚再改）:
#      认错游戏只是搜错视频; 而这里判错一个方向的代价是**打扰人**（弹提醒/弹桌面）,
#      另一个方向的代价是**监督静默失效**。两个方向都不该靠单边证据硬猜,
#      所以"画面有把握但和进程名对不上" -> 判不出来, 记一条证据, 等下一轮。
#
#  升级链（你定的: 3 次不通过**不包含**提醒）:
#      不像学习 ──▶ 气泡「现在是学习时间」（计 0 次） ──5 分钟──▶ 第 1 次不通过
#              ──5 分钟──▶ 第 2 次 ──5 分钟──▶ 第 3 次 ──▶ 返回桌面 + 冷却 30 分钟
#      任一时刻判成 study -> 计数清零, 下一次 30 分钟后; unknown -> 什么都不动。
#  ⚠ 冷却管的是**动作**（不再提醒、不再弹桌面），**不是观察**: 冷却期里照样看
#    （`cooldown_probe_min`，默认 1 分钟一次），因为"弹回桌面之后 5 分钟内又变回学习"
#    这条**只有看着才知道** —— 不看的话这个信号永远发不出来。冷却期里一旦判成学习,
#    冷却立刻结束（人家已经回到学习内容了）。
#  ⚠ 一个**假定**（不对就改）: 队列是**对话关键词**驱动时这一轮不抓帧（沿用认游戏
#    那条你定的规矩）。要改成"照看"就传 `skip_on_keyword=False`。
#
#  阈值**不定死**（你定的"运行过程中不断优化"）—— 护栏全部写死在代码里:
#      · 输入**只有带标签的样本**: PC 进程名 / 人工 `assistant study label`;
#      · EWMA `0.9 * 旧 + 0.1 * 新`（score 与 margin 各一类一份）;
#      · **样本不够不动**: 两个大类都攒到 `min_labeled`(默认 20) 个才允许挪;
#      · **一次只动 0.01**, 方向 = 两类 EWMA 的中点, 再用分布兜一层
#        （不低于 study 的 5% 分位 - 0.02、不高于 not_study 的 95% 分位 + 0.02）;
#      · **界**: score ∈ [0.55, 0.95], margin ∈ [0.01, 0.20] —— 到底了就停;
#      · **无标签**时只看"判不出来的比例": 超过 `target_unknown_rate`(0.30) 就把门槛
#        放松 0.01（两侧都放），说明当前门槛太严;
#      · `unknown` **从不学习**（它连"往哪边挪"都不知道）;
#      · 每次调整都写一条**人能看懂的**理由进 `study_stats.notes()`（"为什么动了"）;
#      · `freeze()` / `reset()` 是人的开关（CLI `assistant study freeze/reset`）;
#      · 弹回桌面之后 5 分钟内又判成学习 -> 记"**可能误判**" + 给那个类**停学** 30 分钟
#        （别把一次误判立刻学成锚点, 越学越歪）。
#
#  ⚠ 本模块**不碰 IPC、不碰状态机、不抓帧**: 帧由调用方给, 动作（气泡/返回桌面）由
#    调用方执行 —— `tick()` 只回一个 `action`（"remind" / "back_to_desktop"）。
#    这样它能完全离线单测; Runtime 的接法见 T13-5。
#
#  ⚠ 已知边界（别在文档里吹过头）:
#    · 只在 **STUDY** 状态且**串流在跑**时有效;
#    · 进程名那条路**没有前台信息**（Windows 的 sshd 在 session 0, 窗口标题恒空）,
#      所以它只是辅助: 开着浏览器查资料 + 挂着游戏, 进程名照样说"游戏在跑";
#    · 256×256 的帧**看不清文字**, 只判"像不像学习", 判不出"在学哪一科/有没有走神"。
# ============================================================================

from __future__ import annotations

import logging
import math
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .study_anchors import StudyAnchors, StudyAnchorError

__all__ = [
    "StudyWatcher", "StudyWatchError",
    "REMIND_TEXT", "VERDICT_STUDY", "VERDICT_NOT_STUDY", "VERDICT_UNKNOWN",
    "ACTION_REMIND", "ACTION_BACK_TO_DESKTOP",
    "DEFAULT_PROCESS_MAP", "DEFAULT_FOCUS_INTERVAL_MIN", "DEFAULT_RECHECK_INTERVAL_MIN",
    "DEFAULT_MAX_FAILURES", "DEFAULT_COOLDOWN_MIN", "DEFAULT_CONFIDENT_SCORE",
    "DEFAULT_CONFIDENT_MARGIN", "DEFAULT_LEARN_SCORE", "DEFAULT_TARGET_UNKNOWN_RATE",
    "DEFAULT_MIN_LABELED", "DEFAULT_STEP", "DEFAULT_SCORE_BOUNDS", "DEFAULT_MARGIN_BOUNDS",
    "DEFAULT_REAPPEAR_WINDOW_MIN", "DEFAULT_PAUSE_MIN", "DEFAULT_EWMA_ALPHA", "STUDY_STATE",
]

_log = logging.getLogger(__name__)

#: 气泡原话（你定的那一句; 返回桌面**不再**补第二句 —— 同一件事不吵两次）。
REMIND_TEXT = "现在是学习时间"

#: 三个结论。
VERDICT_STUDY = "study"
VERDICT_NOT_STUDY = "not_study"
VERDICT_UNKNOWN = "unknown"

#: 两个动作（由调用方执行）。
ACTION_REMIND = "remind"
ACTION_BACK_TO_DESKTOP = "back_to_desktop"

#: 只在这个状态里监督（你定的: 学习时间才有学习监督）。
STUDY_STATE = "STUDY"

#: 间隔/次数/冷却（都能在配置里改; 这几个是**你定的**默认值）。
DEFAULT_FOCUS_INTERVAL_MIN = 30.0        # 判成学习之后, 下次多久再看
DEFAULT_RECHECK_INTERVAL_MIN = 5.0       # 提醒之后, 每 5 分钟复查
DEFAULT_MAX_FAILURES = 3                 # 连续几次不通过就弹回桌面（**不含**那次提醒）
DEFAULT_COOLDOWN_MIN = 30.0              # 弹回桌面之后, 多久不再管（**只看不动**）
#: 冷却期里多久看一眼（比 5 分钟短 —— 否则"弹回桌面之后 5 分钟内又变回学习"
#: 这条**永远发现不了**: 5 分钟才看一次, 刚好卡在窗口边界上）。
DEFAULT_COOLDOWN_PROBE_MIN = 1.0

#: 画面采信门槛的**初值**（T13-4 板端标定会给真值; 这里只是没标定时的兜底,
#: 而且运行期会被"带标签样本"慢慢修正 —— 见模块头的护栏）。
DEFAULT_CONFIDENT_SCORE = 0.80
DEFAULT_CONFIDENT_MARGIN = 0.03
#: 学锚点的门槛: 只学"画面与标签不一致"的那些帧（一致就不必再攒）。
DEFAULT_LEARN_SCORE = 0.90

#: 自适应（你定的"不定死"）。
DEFAULT_TARGET_UNKNOWN_RATE = 0.30       # 判不出来的比例超过它就放松门槛
DEFAULT_MIN_LABELED = 20                 # 两个大类各攒够多少个带标签样本才允许挪
DEFAULT_STEP = 0.01                      # 一次只动这么多
DEFAULT_SCORE_BOUNDS = (0.55, 0.95)
DEFAULT_MARGIN_BOUNDS = (0.01, 0.20)
DEFAULT_EWMA_ALPHA = 0.1                 # 0.9 * 旧 + 0.1 * 新

#: 弹回桌面之后多久内又判成学习 = "可能误判"; 那个类停学多久。
DEFAULT_REAPPEAR_WINDOW_MIN = 5.0
DEFAULT_PAUSE_MIN = 30.0

#: 默认的"进程名 -> 子标签"表。**子标签**（不是大类）: 有了子标签既能推出大类,
#: 又能在"画面判错"时把这一帧学成**具体那一类**的锚点。
#: 值是 `study` / `not_study`（大类）也认 —— 但那样只能当**带标签信号**, 学不了锚点。
#: ⚠ 浏览器（chrome/msedge/firefox）与我们的音乐播放器（mpv）**故意不映射**:
#:   它们既可能在学习也可能不是, 猜错会直接变成一次误提醒。
DEFAULT_PROCESS_MAP: Dict[str, str] = {
    # —— 学习: 代码 / 文档 / 阅读 ——
    "code": "code", "pycharm64": "code", "idea64": "code", "clion64": "code",
    "goland64": "code", "sublime_text": "code", "windowsterminal": "code",
    "devenv": "code", "vim": "code", "nvim": "code",
    "obsidian": "doc", "typora": "doc", "notion": "doc", "zotero": "doc",
    "anki": "doc", "sumatrapdf": "doc", "acrobat": "doc", "winword": "doc",
    "powerpnt": "doc", "excel": "doc", "wps": "doc", "foxitreader": "doc",
    # —— 非学习: 游戏 / 动漫 ——
    "steam": "game", "steamwebhelper": "game", "epicgameslauncher": "game",
    "genshinimpact": "game", "yuanshen": "game", "hoi4": "game", "stellaris": "game",
    "hatsuyuki": "game", "galgame": "game",
    "potplayer": "anime", "vlc": "anime", "douyin": "anime", "bilibili": "anime",
}


class StudyWatchError(RuntimeError):
    """监督器自己的配置错误（消息给人看）。"""


class StudyWatcher(object):
    """学习内容监督（判定 + 升级链 + 阈值自适应）。**不碰 IPC / 状态机 / 抓帧**。

    典型用法（Runtime 里, T13-5）::

        watcher = StudyWatcher(anchors, stats=stats, probe=study_probe,
                               encoder=lambda: game_watcher.model)
        watcher.reset_cycle()                        # 进 STUDY
        decision = watcher.tick(frame, state=state, has_keyword=...)
        if decision["action"] == "remind":           # 气泡
            say(decision["text"])
        elif decision["action"] == "back_to_desktop":  # Win+D（不再补一句话）
            sender.show_desktop()
    """

    def __init__(self, anchors: StudyAnchors, *, stats: Any = None, probe: Any = None,
                 encoder: Any = None, process_map: Optional[Mapping[str, str]] = None,
                 focus_interval_min: float = DEFAULT_FOCUS_INTERVAL_MIN,
                 recheck_interval_min: float = DEFAULT_RECHECK_INTERVAL_MIN,
                 max_failures: int = DEFAULT_MAX_FAILURES,
                 cooldown_min: float = DEFAULT_COOLDOWN_MIN,
                 cooldown_probe_min: float = DEFAULT_COOLDOWN_PROBE_MIN,
                 confident_score: float = DEFAULT_CONFIDENT_SCORE,
                 confident_margin: float = DEFAULT_CONFIDENT_MARGIN,
                 learn_score: float = DEFAULT_LEARN_SCORE,
                 target_unknown_rate: float = DEFAULT_TARGET_UNKNOWN_RATE,
                 min_labeled: int = DEFAULT_MIN_LABELED,
                 step: float = DEFAULT_STEP,
                 score_bounds: Sequence[float] = DEFAULT_SCORE_BOUNDS,
                 margin_bounds: Sequence[float] = DEFAULT_MARGIN_BOUNDS,
                 ewma_alpha: float = DEFAULT_EWMA_ALPHA,
                 reappear_window_min: float = DEFAULT_REAPPEAR_WINDOW_MIN,
                 pause_min: float = DEFAULT_PAUSE_MIN,
                 skip_on_keyword: bool = True, learn: bool = True, adapt: bool = True,
                 clock: Callable[[], float] = time.time,
                 log: Optional[logging.Logger] = None) -> None:
        """
        @param anchors     `agent/core/study_anchors.py::StudyAnchors`
        @param stats       `agent/core/study_stats.py::StudyStats`（None = 不落统计/不自适应）
        @param probe       问 PC 进程的那份实现（`PcProbe`，映射表换成 `process_map`）;
                           None = 只有画面那条路
        @param encoder     造 SigLIP 的那个对象（有 `encode_image`）; 也可以给一个
                           **零参可调用**（每次现取 —— Runtime 用它把认游戏那份常驻模型
                           接过来, "一个能力只有一条实现"）
        @param process_map {进程名: 子标签}（None = `DEFAULT_PROCESS_MAP`）
        @param skip_on_keyword 队列是对话指定的关键词时不抓帧（沿用认游戏那条规矩）
        @param adapt       允许阈值在运行期自己修正（`freeze()` 关掉）
        @param learn       允许把"判错的帧"学成锚点
        """
        self.anchors = anchors
        self.stats = stats
        self.probe = probe
        self._encoder = encoder
        self.process_map = {str(key).strip().lower(): str(value).strip()
                            for key, value in dict(process_map if process_map is not None
                                                   else DEFAULT_PROCESS_MAP).items()
                            if str(key).strip()}
        self.focus_interval_s = max(60.0, float(focus_interval_min or 0) * 60.0)
        self.recheck_interval_s = max(60.0, float(recheck_interval_min or 0) * 60.0)
        self.max_failures = max(1, int(max_failures or DEFAULT_MAX_FAILURES))
        self.cooldown_s = max(0.0, float(cooldown_min or 0) * 60.0)
        self.cooldown_probe_s = max(10.0, float(cooldown_probe_min or 0) * 60.0)
        self._confident_score0 = float(confident_score)
        self._confident_margin0 = float(confident_margin)
        self.learn_threshold = float(learn_score or DEFAULT_LEARN_SCORE)
        self.target_unknown_rate = float(target_unknown_rate or 0.0)
        self.min_labeled = max(1, int(min_labeled or DEFAULT_MIN_LABELED))
        self.step = abs(float(step or DEFAULT_STEP))
        self.score_bounds = self._bounds(score_bounds, DEFAULT_SCORE_BOUNDS, "score_bounds")
        self.margin_bounds = self._bounds(margin_bounds, DEFAULT_MARGIN_BOUNDS, "margin_bounds")
        self.ewma_alpha = min(1.0, max(0.0, float(ewma_alpha or DEFAULT_EWMA_ALPHA)))
        self.reappear_window_s = max(0.0, float(reappear_window_min or 0) * 60.0)
        self.pause_s = max(0.0, float(pause_min or 0) * 60.0)
        self.skip_on_keyword = bool(skip_on_keyword)
        self.learn = bool(learn)
        self.adapt_enabled = bool(adapt)
        self.clock = clock
        self.log = log or _log
        self._next_at = 0.0                 # 0 = "下一次 tick 就判"
        self._failures = 0
        self._reminded = False
        self._cooldown_until = 0.0
        self._last_bounce_at = 0.0
        self._paused: Dict[str, float] = {}
        self._last_decision: Dict[str, Any] = {}
        self._notes: List[str] = []

    # ------------------------------------------------------------ 配置 ---
    @staticmethod
    def _bounds(values: Sequence[float], default: Tuple[float, float],
                name: str) -> Tuple[float, float]:
        try:
            low, high = float(values[0]), float(values[1])
        except (TypeError, ValueError, IndexError):
            raise StudyWatchError("%s 得是两个数 (低, 高)" % name)
        if low > high:
            raise StudyWatchError("%s 写反了: %s > %s" % (name, low, high))
        return (low, high)

    @property
    def confident_score(self) -> float:
        """当前分类门槛（stats 里有就用学到的那个 —— 它才是"运行期不断优化"的结果）。"""
        return self._threshold("confident_score", self._confident_score0)

    @property
    def confident_margin(self) -> float:
        return self._threshold("confident_margin", self._confident_margin0)

    def _threshold(self, name: str, default: float) -> float:
        if self.stats is not None:
            value = self.stats.threshold(name)
            if value is not None:
                return float(value)
        return float(default)

    def seed_thresholds(self) -> Dict[str, float]:
        """把配置里的初值写进 stats（**只在还没有的时候**）。

        @note 运行期学到的值不能被配置覆盖 —— 你改配置是"换初值", 不是"清空学习";
              真要清空有 `reset_learning()`。
        """
        if self.stats is None:
            return {}
        for name, value in (("confident_score", self._confident_score0),
                            ("confident_margin", self._confident_margin0),
                            ("learn_score", self.learn_threshold)):
            if self.stats.threshold(name) is None:
                self.stats.set_threshold(name, value)
        return self.stats.thresholds()

    # ------------------------------------------------------- 模型/编码 ---
    def _get_encoder(self) -> Any:
        target = self._encoder
        if callable(target) and not hasattr(target, "encode_image"):
            try:
                return target()
            except Exception as exc:                       # noqa: BLE001
                self._notes.append("取 SigLIP 失败（%s）—— 这轮只能靠进程那条路" % exc)
                return None
        return target

    def encode(self, frame: Any) -> Optional[List[float]]:
        """一帧 -> 图像向量（没有模型 / 编码失败 -> None, 并如实记一句）。"""
        if frame is None:
            return None
        model = self._get_encoder()
        if model is None:
            self._notes.append("没有可用的 SigLIP —— 这次判不出来（中性）")
            return None
        try:
            vector = model.encode_image(frame)
        except Exception as exc:                           # noqa: BLE001
            self.log.warning("study_watch: 这一帧编码失败（跳过）: %r", exc)
            self._notes.append("这一帧编码失败（%s）—— 判不出来（中性）" % exc)
            return None
        try:
            return [float(item) for item in list(vector)]
        except TypeError:
            return None

    # ------------------------------------------------------------ 证据 ---
    def process_evidence(self) -> Dict[str, Any]:
        """问 PC 进程名那条路（**辅助证据**）。

        @return `{"ok","labels","categories","labeled","category","why"}` ——
                `labels` = {子标签: [进程名]}, `categories` = {大类: [子标签或大类]},
                `labeled` = 唯一那个子标签（学锚点要用）, `category` = 推出来的大类
                （两个大类都有 -> 空串 = 自己就矛盾, 不作数）。
        """
        out: Dict[str, Any] = {"ok": False, "labels": {}, "categories": {},
                               "labeled": "", "category": "", "why": ""}
        if self.probe is None:
            out["why"] = "没接进程探针"
            return out
        try:
            snapshot = self.probe.snapshot()
        except Exception as exc:                           # noqa: BLE001 - 探针炸了不该影响判定
            out["why"] = "问 PC 出错（%s）" % exc
            return out
        if not isinstance(snapshot, Mapping) or not snapshot.get("ok"):
            out["why"] = str((snapshot or {}).get("why") or "问不到 PC") \
                if isinstance(snapshot, Mapping) else "问不到 PC"
            return out
        out["ok"] = True
        out["why"] = str(snapshot.get("why") or "")
        for value, names in dict(snapshot.get("games") or {}).items():
            key = str(value)
            category = self._category_of_label(key)
            if category:
                out["categories"].setdefault(category, []).append(key)
            if key in self.anchors.classes:
                out["labels"].setdefault(key, list(names or []))
        categories = sorted(out["categories"])
        if len(categories) == 1:
            out["category"] = categories[0]
        elif len(categories) > 1:
            out["why"] = (out["why"] or "") + \
                "（PC 上同时开着学习类和非学习类进程：%s —— 进程这条路自己就矛盾, 不作数）" \
                % "、".join(categories)
            out["category"] = ""
        if len(out["labels"]) == 1:
            out["labeled"] = next(iter(out["labels"]))
        elif len(out["labels"]) > 1:
            out["labeled"] = ""                            # 多个子标签: 不猜哪个是前台
        return out

    def _category_of_label(self, value: str) -> str:
        """一个映射值 -> 大类（子标签查配置; 直接写 `study`/`not_study` 也认）。"""
        text = str(value or "").strip()
        if not text:
            return ""
        category = self.anchors.classes.get(text)
        if category:
            return category
        if text in (VERDICT_STUDY, VERDICT_NOT_STUDY):
            return text
        self.log.warning("study_watch: process_names 里的 %r 既不是子标签也不是大类（忽略）",
                         text)
        return ""

    # ------------------------------------------------------------ 判定 ---
    def verdict(self, frame: Any = None, *, vector: Optional[Sequence[float]] = None,
                now: Optional[float] = None) -> Dict[str, Any]:
        """**只看这一帧**（不管间隔/状态/升级链）: 得出 study / not_study / unknown。

        @return `{"verdict","source","confident","cls","category","score","margin",
                  "labeled","process","anchors","note","at"}`
        """
        moment = float(self.clock() if now is None else now)
        out: Dict[str, Any] = {"verdict": VERDICT_UNKNOWN, "source": "", "confident": False,
                               "cls": "", "category": "", "score": 0.0, "margin": 0.0,
                               "labeled": "", "process": {}, "anchors": len(self.anchors),
                               "note": "", "at": moment}
        image = list(vector) if vector is not None else self.encode(frame)
        hit = self.anchors.match(image) if image else None
        confident = bool(hit and float(hit.get("category_score") or 0.0) >= self.confident_score
                         and float(hit.get("category_margin") or 0.0) >= self.confident_margin)
        out["confident"] = confident
        if hit:
            out.update({"cls": str(hit.get("cls") or ""),
                        "category": str(hit.get("category") or ""),
                        "score": float(hit.get("category_score") or 0.0),
                        "margin": float(hit.get("category_margin") or 0.0)})

        evidence = self.process_evidence()
        out["process"] = {"ok": evidence["ok"], "why": evidence["why"],
                          "labels": evidence["labels"], "category": evidence["category"]}
        out["labeled"] = evidence["labeled"]

        if confident and evidence["category"]:
            if evidence["category"] == out["category"]:
                out.update({"verdict": out["category"], "source": "anchor",
                            "note": "画面有把握，进程名也一致（%s）"
                                    % "、".join(sorted(evidence["labels"]) or
                                                [evidence["category"]])})
            else:
                out.update({"verdict": VERDICT_UNKNOWN, "source": "conflict",
                            "note": "画面说是「%s」（%.3f/余量 %.3f），进程名说是「%s」—— "
                                    "两路对不上，判不出来（中性，不打扰你）"
                                    % (out["category"], out["score"], out["margin"],
                                       evidence["category"])})
            return out
        if confident:
            out.update({"verdict": out["category"], "source": "anchor",
                        "note": "画面有把握（%s %.3f/余量 %.3f）%s"
                                % (out["category"], out["score"], out["margin"],
                                   "；" + evidence["why"] if evidence["why"] else "")})
            return out
        if evidence["category"]:
            out.update({"verdict": evidence["category"], "source": "process",
                        "note": "画面没把握（%s %.3f/余量 %.3f），按进程名算 —— 它只是辅助"
                                "（没有前台信息）" % (out["cls"] or "?", out["score"],
                                                     out["margin"])})
            return out
        if not len(self.anchors):
            out["note"] = ("锚点库是空的，进程名也没有认识的 —— 判不出来（中性）; "
                           "先跑标定或 `assistant study label`")
        else:
            out["note"] = ("判不出来：画面最像「%s」（%s %.3f/余量 %.3f），进程名那条路也没有"
                           "结论%s" % (out["cls"] or "?", out["category"] or "?", out["score"],
                                       out["margin"],
                                       "（%s）" % evidence["why"] if evidence["why"] else ""))
        out["source"] = "none"
        return out

    # ------------------------------------------------------------ 升级链 ---
    def tick(self, frame: Any = None, *, vector: Optional[Sequence[float]] = None,
             state: Any = STUDY_STATE, has_keyword: bool = False, force: bool = False,
             now: Optional[float] = None) -> Dict[str, Any]:
        """到点了就看一眼，并按升级链决定要不要提醒 / 弹回桌面。

        @param has_keyword 队列当前是**对话关键词**驱动的 -> 不抓帧（对话优先）
        @param force 无视间隔（单测/人工 `assistant study check` 用）
        @return `{"skipped","verdict","source","action","text","cls","category","score",
                  "margin","failures","next_in_s","cooldown_left_s","learned","adapted",
                  "note","at"}`
        """
        moment = float(self.clock() if now is None else now)
        out: Dict[str, Any] = {"skipped": "", "verdict": VERDICT_UNKNOWN, "source": "",
                               "action": "", "text": "", "cls": "", "category": "",
                               "score": 0.0, "margin": 0.0, "failures": self._failures,
                               "next_in_s": 0.0, "cooldown_left_s": 0.0,
                               "learned": False, "adapted": False, "note": "", "at": moment}
        if str(state or "").upper() != STUDY_STATE:
            out["skipped"] = "只在 %s 里监督（现在是 %s）" % (STUDY_STATE, state)
            return out
        if self.skip_on_keyword and has_keyword:
            out["skipped"] = "队列是对话指定的关键词 —— 不抓帧（对话优先）"
            return out
        # ⚠ 冷却管的是**动作**（不提醒、不弹桌面），不是**观察**:
        #   不看了的话,"弹回桌面之后 5 分钟里又变回学习 -> 可能误判"这条**永远发现不了**。
        #   所以冷却期里照看（间隔更短: `cooldown_probe_min`），只是什么都不做。
        in_cooldown = moment < self._cooldown_until
        out["cooldown_left_s"] = max(0.0, self._cooldown_until - moment) if in_cooldown else 0.0
        if not force and moment < self._next_at:
            out["next_in_s"] = self._next_at - moment
            out["skipped"] = "还没到点（还有 %.0f 分钟）" % (out["next_in_s"] / 60.0)
            if in_cooldown:
                out["skipped"] += "；顺便说一句: 冷却还剩 %.0f 分钟" \
                                  % (out["cooldown_left_s"] / 60.0)
            return out

        # ⚠ 先拿到向量再判: "没画面 / 模型没在 / 编码失败"是**跳过**, 不是 unknown ——
        #   把它算成 unknown 会白白抬高"判不出来的比例", 然后把门槛放松掉。
        image = list(vector) if vector is not None else self.encode(frame)
        if image is None:
            out["skipped"] = ("没拿到这一帧的向量（没画面 / SigLIP 没在 / 编码失败）"
                              "—— 跳过（不算一次判定）")
            return out

        result = self.verdict(frame, vector=image, now=moment)
        out.update({"verdict": result["verdict"], "source": result["source"],
                    "cls": result["cls"], "category": result["category"],
                    "score": result["score"], "margin": result["margin"],
                    "note": result["note"]})
        learned = self._learn_from(result, frame=frame, vector=image, now=moment)
        out["learned"] = learned
        self._count(result)
        if result["source"] == "process":
            # 进程名给的**是标签也是分数**（唯一能自动喂自适应的真值来源）
            self.learn_score(result["score"], label=result["verdict"],
                             margin=result["margin"], when=moment)

        verdict = result["verdict"]
        if verdict == VERDICT_NOT_STUDY:
            if in_cooldown:
                self._next_at = min(self._cooldown_until, moment + self.cooldown_probe_s)
                out["note"] = ("还在冷却里（还剩 %.0f 分钟）—— 只看不动，不再打扰你"
                               % (out["cooldown_left_s"] / 60.0))
            elif not self._reminded:
                # 第一次: 提醒（**不算一次不通过** —— 你定的"3 次不包含提醒"）
                self._reminded = True
                self._failures = 0
                self._next_at = moment + self.recheck_interval_s
                out.update({"action": ACTION_REMIND, "text": REMIND_TEXT,
                            "note": "不像学习 —— 提醒一次（这次不算数，%d 分钟后再看）"
                                    % int(self.recheck_interval_s / 60)})
                self._bump("reminded")
            else:
                self._failures += 1
                if self._failures >= self.max_failures:
                    self._cooldown_until = moment + self.cooldown_s
                    self._last_bounce_at = moment
                    self._failures = 0
                    self._reminded = False
                    self._next_at = moment + self.cooldown_probe_s
                    out.update({"action": ACTION_BACK_TO_DESKTOP,
                                "note": "提醒之后连续 %d 次都不像学习 —— 返回桌面，冷却 %.0f 分钟"
                                        % (self.max_failures, self.cooldown_s / 60.0)})
                    self._bump("back_to_desktop")
                else:
                    self._next_at = moment + self.recheck_interval_s
                    out["note"] = "还是不像学习（第 %d / %d 次）—— %d 分钟后再看" \
                                  % (self._failures, self.max_failures,
                                     int(self.recheck_interval_s / 60))
            out["failures"] = self._failures
        elif verdict == VERDICT_STUDY:
            if self._last_bounce_at and (moment - self._last_bounce_at) <= self.reappear_window_s:
                blame = result["labeled"] or result["cls"] or result["category"]
                if blame:
                    self._paused[str(blame)] = moment + self.pause_s
                out["note"] = ("**可能误判**：%.0f 分钟前刚弹回桌面，现在又判成学习"
                               "（%s）—— 先给「%s」停学 %.0f 分钟，别把误判学成锚点"
                               % ((moment - self._last_bounce_at) / 60.0, result["cls"] or "?",
                                  blame or result["cls"] or "?", self.pause_s / 60.0))
                self._bump("misjudged")
            if in_cooldown:
                self._cooldown_until = 0.0              # 人家已经回到学习内容了, 冷却到此为止
                out["note"] = (out["note"] + "；" if out["note"] else "") + \
                              "已经回到学习内容 —— 冷却提前结束"
                out["cooldown_left_s"] = 0.0
            self._failures = 0
            self._reminded = False
            self._next_at = moment + self.focus_interval_s
        else:
            # unknown: **完全中性** —— 不动计数器、不动动作、不重置那条链
            if in_cooldown:
                self._next_at = min(self._cooldown_until, moment + self.cooldown_probe_s)
            else:
                self._next_at = moment + (self.recheck_interval_s if self._reminded
                                          else self.focus_interval_s)
            out["note"] = result["note"] + "（中性：不算通过也不算不通过）"
        out["failures"] = self._failures
        out["next_in_s"] = max(0.0, self._next_at - moment)
        out["cooldown_left_s"] = max(0.0, self._cooldown_until - moment)
        if self.adapt_enabled:
            # 两条输入都在这儿: 带标签样本（`learn_score` 喂的 EWMA/分布）
            # 与"判不出来的比例"（太高说明门槛太严 -> 放松）。
            out["adapted"] = self.adapt(now=moment)["moved"]
        self._record_sample(result, out, moment)
        self._save()
        self._last_decision = dict(out)
        return out

    # ------------------------------------------------------- 学习与自适应 ---
    def label(self, cls: str, *, frame: Any = None, vector: Optional[Sequence[float]] = None,
              note: str = "", when: Optional[float] = None) -> Dict[str, Any]:
        """人工给一帧打标签（`assistant study label` 那条路，T13-8）。

        @return `{"cls","category","learned","anchors","row","note"}`

        @note 人工标签**只纠锚点、不喂阈值自适应**: 它没有"这一帧当时有多像"的那个分数,
              硬塞一个假分数会把 EWMA 带偏。分数分布那条路靠进程名（它自带分数）。
        """
        name = str(cls or "").strip()
        if not name:
            raise StudyWatchError("打标签得给个子标签（%s）"
                                  % " / ".join(self.anchors.configured_classes()))
        image = list(vector) if vector is not None else self.encode(frame)
        if not image:
            return {"cls": name, "category": "", "learned": False, "anchors": len(self.anchors),
                    "row": None, "note": "没拿到向量（没有帧 / 没有模型）—— 没学"}
        moment = float(self.clock() if when is None else when)
        try:
            row = self.anchors.add(cls=name, vector=image, shot=self._shot(frame),
                                   source="manual", note=note or "人工打标签", when=moment)
        except (StudyAnchorError, OSError) as exc:
            raise StudyWatchError("这一帧没学进去（%s）" % exc) from exc
        self._paused.pop(name, None)                        # 人工纠正 = 解除这个类的停学
        self._bump("labeled")
        self._bump("labeled_manual")
        self._save()
        return {"cls": name, "category": self.anchors.classes.get(name, ""),
                "learned": True, "anchors": len(self.anchors), "row": row,
                "note": "学成「%s」的锚点了" % name}

    def learn_score(self, score: float, *, label: str,
                    margin: Optional[float] = None,
                    when: Optional[float] = None) -> Dict[str, Any]:
        """喂一个**带标签**样本（阈值自适应的唯一输入）。

        @param label 大类（`study` / `not_study`）; `unknown` **从不学习**（你定的）
        """
        key = str(label or "").strip()
        if key not in (VERDICT_STUDY, VERDICT_NOT_STUDY):
            return {"learned": False, "why": "unknown 从不学习（你定的）"}
        if self.stats is None:
            return {"learned": False, "why": "没有 stats（自适应无处可存）"}
        self.stats.push_ewma("score_" + key, float(score), alpha=self.ewma_alpha)
        self.stats.observe(key, float(score))
        if margin is not None:
            self.stats.push_ewma("margin_" + key, float(margin), alpha=self.ewma_alpha)
        self.stats.bump("labeled_" + key)
        self.stats.bump("labeled")
        return {"learned": True, "label": key,
                "ewma": self.stats.ewma("score_" + key)}

    def adapt(self, *, now: Optional[float] = None) -> Dict[str, Any]:
        """按护栏挪一次阈值（**能不动就不动**）。@return `{"moved","why"}`。"""
        out: Dict[str, Any] = {"moved": False, "why": []}
        if not self.adapt_enabled or self.stats is None:
            return out
        moved = False
        for name, bounds, ewma_key in (("confident_score", self.score_bounds, "score"),
                                       ("confident_margin", self.margin_bounds, "margin")):
            current = self._threshold(name, self._confident_score0 if name == "confident_score"
                                      else self._confident_margin0)
            target = self._target(name, ewma_key)
            if target is None:
                continue
            new_value = self._step_toward(current, target, bounds)
            if abs(new_value - current) < 1e-9:
                continue
            self.stats.set_threshold(name, new_value)
            reason = ("把 %s 从 %.3f 挪到 %.3f：%s（带标签样本 study %d / not_study %d，"
                      "一次只动 %.2f，界 [%.2f, %.2f]）"
                      % (self._name_cn(name), current, new_value, self._target_why(name),
                         self.stats.counter("labeled_study"),
                         self.stats.counter("labeled_not_study"), self.step,
                         bounds[0], bounds[1]))
            self.stats.note(reason)
            self._bump("adapt_up" if new_value > current else "adapt_down")
            out["why"].append(reason)
            moved = True

        relax = self._relax_reason()
        if relax:
            changes = []
            for name, bounds in (("confident_score", self.score_bounds),
                                 ("confident_margin", self.margin_bounds)):
                current = self._threshold(name, self._confident_score0
                                          if name == "confident_score"
                                          else self._confident_margin0)
                new_value = max(bounds[0], round(current - self.step, 4))
                if abs(new_value - current) >= 1e-9:
                    self.stats.set_threshold(name, new_value)
                    changes.append("%s %.3f -> %.3f" % (self._name_cn(name), current, new_value))
            if changes:
                reason = "判不出来的比例太高（%s），放松门槛：%s" % (relax, "；".join(changes))
                self.stats.note(reason)
                out["why"].append(reason)
                self._bump("adapt_relax")
                moved = True
        out["moved"] = moved
        return out

    def _name_cn(self, name: str) -> str:
        return {"confident_score": "分类门槛", "confident_margin": "余量门槛",
                "learn_score": "学习门槛"}.get(name, name)

    def _target(self, name: str, ewma_key: str) -> Optional[float]:
        """目标值 = 两类 EWMA 的中点, 再用分布兜一层（返回 None = 样本不够, 别动）。"""
        n_study = self.stats.counter("labeled_" + VERDICT_STUDY)
        n_not = self.stats.counter("labeled_" + VERDICT_NOT_STUDY)
        if min(n_study, n_not) < self.min_labeled:
            return None
        left = self.stats.ewma("%s_%s" % (ewma_key, VERDICT_STUDY))
        right = self.stats.ewma("%s_%s" % (ewma_key, VERDICT_NOT_STUDY))
        if left is None or right is None:
            return None
        middle = (float(left) + float(right)) / 2.0
        low = self.stats.quantile(VERDICT_STUDY, 0.05)
        high = self.stats.quantile(VERDICT_NOT_STUDY, 0.95)
        if low is not None:
            middle = max(middle, float(low) - 0.02)         # 别把真学习帧误伤
        if high is not None:
            middle = min(max(middle, 0.0), float(high) + 0.02)
        return middle

    def _target_why(self, name: str) -> str:
        ewma_key = "margin" if name == "confident_margin" else "score"
        left = self.stats.ewma("%s_%s" % (ewma_key, VERDICT_STUDY))
        right = self.stats.ewma("%s_%s" % (ewma_key, VERDICT_NOT_STUDY))
        return ("两类 %s 的 EWMA 中点是 %.3f（study %.3f / not_study %.3f）"
                % ("余量" if ewma_key == "margin" else "分数",
                   ((left or 0.0) + (right or 0.0)) / 2.0, left or 0.0, right or 0.0))

    def _step_toward(self, current: float, target: float, bounds: Tuple[float, float]) -> float:
        """朝 target 挪**最多一步**（你定的 0.01），并夹在界内。"""
        if target > current + 1e-9:
            new_value = current + self.step
        elif target < current - 1e-9:
            new_value = current - self.step
        else:
            return current
        return round(min(bounds[1], max(bounds[0], new_value)), 4)

    def _relax_reason(self) -> str:
        """判不出来的比例是否过高（过高 -> 门槛太严, 放松一点）。@return "" = 不用动。"""
        total = self.stats.counter("verdicts")
        unknown = self.stats.counter("verdict_unknown")
        if total < self.min_labeled or not total:
            return ""
        rate = unknown / float(total)
        if rate <= self.target_unknown_rate:
            return ""
        current = self._threshold("confident_score", self._confident_score0)
        if current <= self.score_bounds[0] + 1e-9:
            return ""                                       # 已经到底了, 别再放松
        return ("%d/%d = %.0f%% > 目标 %.0f%%" % (unknown, total, rate * 100,
                                                  self.target_unknown_rate * 100))

    # ------------------------------------------------------- 学锚点 ---
    def _learn_from(self, result: Mapping[str, Any], *, frame: Any = None,
                    vector: Optional[Sequence[float]] = None, now: float) -> bool:
        """把"画面判错的那一帧"学成标签说的那一类（你定的纠错机制）。"""
        if not self.learn:
            return False
        if str(result.get("verdict") or "") == VERDICT_UNKNOWN:
            # 判不出来就**什么都不学**（你定的"unknown 完全中性"）: 冲突时两面都不信,
            # 库里空着也只是"还没见过", 不该拿一帧没根据的画面当锚点。
            return False
        cls = str(result.get("labeled") or "")
        if not cls or cls not in self.anchors.classes:
            return False                                    # 没有子标签就学不了锚点
        if self._is_paused(cls, now):
            self._notes.append("「%s」正在停学（上次可能误判），这一帧不学" % cls)
            return False
        hit_cls = str(result.get("cls") or "")
        hit_category = str(result.get("category") or "")
        if result.get("confident") and hit_category == result.get("verdict") and hit_cls == cls:
            return False                                    # 画面本来就说对了, 不必再攒
        image = list(vector) if vector is not None else None
        if image is None:
            image = self.encode(frame)
        if not image:
            return False
        try:
            self.anchors.add(cls=cls, vector=image, shot=self._shot(frame), source="auto",
                             note="画面说是 %s、标签说是 %s" % (hit_cls or "?", cls),
                             when=now)
        except (StudyAnchorError, OSError) as exc:
            self._notes.append("锚点没记下来（%s）" % exc)
            self.log.warning("study_watch: 锚点没记下来: %r", exc)
            return False
        self._bump("learned")
        return True

    def _shot(self, frame: Any) -> Optional[bytes]:
        getter = getattr(frame, "tobytes", None)
        if callable(getter):
            try:
                return getter()
            except Exception:                               # noqa: BLE001
                return None
        return None

    def _is_paused(self, cls: str, now: float) -> bool:
        until = self._paused.get(str(cls))
        if not until:
            return False
        if now >= until:
            self._paused.pop(str(cls), None)
            return False
        return True

    def paused_classes(self, *, now: Optional[float] = None) -> Dict[str, float]:
        """现在停学的子标签 -> 还剩多少秒（顺手清掉过期的）。"""
        moment = float(self.clock() if now is None else now)
        for name in list(self._paused):
            self._is_paused(name, moment)
        return {name: until - moment for name, until in sorted(self._paused.items())}

    # ------------------------------------------------------------ 计数 ---
    def _count(self, result: Mapping[str, Any]) -> None:
        if self.stats is None:
            return
        self._bump("frames")
        self._bump("verdicts")
        verdict = str(result.get("verdict") or VERDICT_UNKNOWN)
        self._bump("verdict_" + verdict)
        self._bump("source_" + str(result.get("source") or "none"))
        if verdict == VERDICT_UNKNOWN:
            # 判不出来的那些帧的分数分布（它**不参与**阈值计算, 只用来回答"卡在哪儿"）
            self.stats.observe("unknown", float(result.get("score") or 0.0))

    def _record_sample(self, result: Mapping[str, Any], out: Mapping[str, Any],
                       moment: float) -> None:
        if self.stats is None:
            return
        self.stats.record(cls=str(result.get("cls") or ""),
                          category=str(result.get("category") or ""),
                          score=float(result.get("score") or 0.0),
                          margin=float(result.get("margin") or 0.0),
                          verdict=str(out.get("verdict") or ""),
                          action=str(out.get("action") or ""),
                          note=str(out.get("note") or ""), when=moment)

    def _bump(self, name: str, n: int = 1) -> None:
        if self.stats is not None:
            self.stats.bump(name, n)

    def _save(self) -> None:
        """落盘统计。**写不进去也不能打断监督**（它是诊断数据）。"""
        if self.stats is None:
            return
        try:
            self.stats.save()
        except OSError as exc:
            self.log.warning("study_watch: 统计没写下去（忽略）: %s", exc)
            self._notes.append("统计没写下去（%s）—— 不影响判定" % exc)

    # ------------------------------------------------------------ 开关 ---
    def reset_cycle(self) -> None:
        """**每次进 STUDY 调一次**（你定的"每个 STUDY 周期重新开始"）。

        @note 清的是**这一轮**（失败计数/提醒过没有/冷却/停学），**不清**学到的阈值与锚点;
              "刚弹过桌面"那个时间戳特意留着 —— 它是"可能误判"的判据。
        @note 重置后 `_next_at = 0` = **下一次 tick 立刻判一眼**（进 STUDY 就该知道现状）。
        """
        self._failures = 0
        self._reminded = False
        self._cooldown_until = 0.0
        self._paused = {}
        self._next_at = 0.0
        self._notes.append("进入 %s —— 这一轮监督从头开始（学到的阈值/锚点都留着）"
                           % STUDY_STATE)

    def freeze(self) -> None:
        """冻住阈值（不再自己修正）—— 排除法定位问题 / 你想手动定死时用。"""
        self.adapt_enabled = False
        self._notes.append("阈值自适应已冻结（判定照旧，只是不再自己挪门槛）")

    def unfreeze(self) -> None:
        self.adapt_enabled = True
        self._notes.append("阈值自适应已解冻")

    def adapt_on(self, enabled: bool = True) -> None:
        """`adapt(False)` = `freeze()`（配置开关那条路）。"""
        if enabled:
            self.unfreeze()
        else:
            self.freeze()

    def reset_learning(self) -> Dict[str, Any]:
        """把**学到的**清掉, 回到配置里的初值（锚点不动 —— 那是另一个开关）。"""
        if self.stats is not None:
            self.stats.reset(keep_thresholds=False)
            self.seed_thresholds()
            self.stats.note("阈值与 EWMA 已清空, 回到配置初值（分类 %.3f / 余量 %.3f）"
                            % (self._confident_score0, self._confident_margin0))
            self._save()
        self.reset_cycle()
        return {"confident_score": self.confident_score,
                "confident_margin": self.confident_margin}

    def notes(self) -> List[str]:
        """攒下来的"要如实说的话"。取走即清空。"""
        out = list(self._notes)
        self._notes = []
        return out

    # ------------------------------------------------------------ 状态 ---
    def snapshot(self) -> Dict[str, Any]:
        """给日志 / `assistant study status` 用。"""
        return {"anchors": len(self.anchors), "counts": self.anchors.counts(),
                "categories": self.anchors.category_counts(),
                "thresholds": {"confident_score": round(self.confident_score, 4),
                               "confident_margin": round(self.confident_margin, 4),
                               "learn_score": self.learn_threshold,
                               "score_bounds": list(self.score_bounds),
                               "margin_bounds": list(self.margin_bounds)},
                "intervals_min": {"focus": self.focus_interval_s / 60.0,
                                  "recheck": self.recheck_interval_s / 60.0,
                                  "cooldown": self.cooldown_s / 60.0,
                                  "cooldown_probe": self.cooldown_probe_s / 60.0},
                "max_failures": self.max_failures, "failures": self._failures,
                "reminded": self._reminded, "cooldown_left_s":
                    max(0.0, self._cooldown_until - float(self.clock())),
                "paused": self.paused_classes(), "adapt": self.adapt_enabled,
                "learn": self.learn, "process_map": len(self.process_map),
                "last": dict(self._last_decision)}
