# ============================================================================
#  agent/core/game_watch.py — **游戏观察器**（Phase 7 T11-4）
#
#  它干什么: 每隔一段时间拿一帧屏幕画面, 认出"现在在玩什么游戏", 交给上层去搜 B 站队列。
#            **它不是 LLM 工具**（你定的）—— 是 Agent 在 GAME 状态里的一个循环, 模型看不见它。
#
#  两条路 + 谁说了算（T11-0 标定 + 你的"纠错机制"）:
#      ① **画面锚点**（SigLIP 图像向量 + 余弦）—— 只采信**很有把握**的那些:
#         `top1 ≥ 0.82` 且 `top1 - 第二名 ≥ 0.05`（实测 13 张样本里 6 张达得到、零误判）。
#      ② **PC 进程名**（硬证据; 窗口标题拿不到, 见 pc_probe）—— 只在①没把握时用。
#      融合规则:
#        · ①有把握 且 与②一致        -> 用①（`source="anchor"`）
#        · ①有把握 但 与②**不一致**   -> **以②为准**, 并**把这一帧登记成②那个游戏的锚点**
#                                       （自学习 = 你定的"纠错机制"）`source="corrected"`
#        · ①没把握 或 库是空的        -> 用②（`source="process"`）, 同样学一个锚点（起步就靠它）
#        · ②也认不出（没跑认识的游戏 / ssh 不通）-> **什么都不做**, 如实记一句
#
#  什么时候**不**看画面（你定的）:
#      · **有对话关键词时完全不抓帧**（对话优先）;
#      · 没帧（串流没在跑）-> 跳过;
#      · 距上次不足 `interval_s`（默认 60 s）-> 跳过。
#
#  模型常驻策略（你定的）: **STUDY 与 GAME 都常驻**; 离开这两个状态就 `close()` 卸载。
#      加载前还会看一眼内存水位（SigLIP 实测常驻 923 MB）—— 不够就**不加载**并如实记一句。
#
#  ⚠ 本模块**不碰 IPC、不碰队列、不抓帧**: 帧由调用方给（`ImageReader.read_latest()`）,
#    认出游戏之后要不要更新队列是 Runtime 的事。这样它可以完全离线单测。
# ============================================================================

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

__all__ = ["GameWatcher", "DEFAULT_INTERVAL_S", "DEFAULT_CONFIDENT_SCORE",
           "DEFAULT_CONFIDENT_MARGIN", "DEFAULT_MEM_WATERMARK_MB", "RESIDENT_STATES"]

_log = logging.getLogger(__name__)

#: 多久看一次（你定的"每间隔一段时间"）。
DEFAULT_INTERVAL_S = 60.0
#: 画面采信门槛（T11-0 标定: 13 张样本里 6 张达到、零误判）。
DEFAULT_CONFIDENT_SCORE = 0.82
#: 与第二名的差距门槛（同分就说明分不开 —— 交给进程裁决）。
DEFAULT_CONFIDENT_MARGIN = 0.05
#: 加载/保持 SigLIP 的内存水位（它常驻 923 MB, 别把板子挤爆）。
DEFAULT_MEM_WATERMARK_MB = 400.0
#: 哪些状态要**常驻**模型（你定的: STUDY 与 GAME）。
RESIDENT_STATES = ("STUDY", "GAME")


class GameWatcher(object):
    """画面 + 进程 双路认游戏（含自学习锚点、模型常驻策略）。

    典型用法（Runtime 里）::

        watcher = GameWatcher(anchors, probe=probe)
        watcher.ensure_model(state)                 # STUDY/GAME 常驻, 其它状态卸载
        decision = watcher.tick(frame, state=state, has_keyword=bool(queue.keyword))
        if decision.get("game") and decision["game"] != last_game:
            queue.search(decision["game"], source="screen")
    """

    def __init__(self, anchors: Any, *, probe: Any = None,
                 encoder_factory: Optional[Callable[[], Any]] = None,
                 interval_s: float = DEFAULT_INTERVAL_S,
                 confident_score: float = DEFAULT_CONFIDENT_SCORE,
                 confident_margin: float = DEFAULT_CONFIDENT_MARGIN,
                 mem_watermark_mb: float = DEFAULT_MEM_WATERMARK_MB,
                 resident_states: Sequence[str] = RESIDENT_STATES,
                 meminfo: Optional[Callable[[], Mapping[str, float]]] = None,
                 learn: bool = True, clock: Callable[[], float] = time.time,
                 log: Optional[logging.Logger] = None) -> None:
        """
        @param anchors         `agent/core/game_anchors.py::GameAnchors`
        @param probe           `agent/net/pc_probe.py::PcProbe`（None = 只有画面那条路）
        @param encoder_factory 造 SigLIP 模型（默认 `SiglipModel.from_config(vision)`）; 单测注入假的
        @param learn           认出来之后要不要学一个锚点（默认学 —— 你定的纠错/自学习）
        """
        self.anchors = anchors
        self.probe = probe
        self.interval_s = max(1.0, float(interval_s or DEFAULT_INTERVAL_S))
        self.confident_score = float(confident_score or DEFAULT_CONFIDENT_SCORE)
        self.confident_margin = float(confident_margin or DEFAULT_CONFIDENT_MARGIN)
        self.mem_watermark_mb = float(mem_watermark_mb or DEFAULT_MEM_WATERMARK_MB)
        self.resident_states = tuple(str(item).upper() for item in resident_states)
        self.learn = bool(learn)
        self.clock = clock
        self.log = log or _log
        self._encoder_factory = encoder_factory
        self._meminfo = meminfo
        self._model: Any = None
        self._model_state = "unloaded"
        self.last_at = 0.0
        self.last_game = ""
        self.last_decision: Dict[str, Any] = {}
        self._notes: List[str] = []

    # ------------------------------------------------------- 模型生命周期 ---
    @property
    def model_loaded(self) -> bool:
        return self._model is not None

    def encoder(self) -> Any:
        """把**当前这一个** SigLIP **借出去**（T13-5: 学习监督用同一个模型）。

        @return 模型对象（没加载 / 被内存水位拦下 -> None）; 调用方**不许** `close()` 它
        @note 为什么不各加载一份: SigLIP 常驻 923 MB, 板子一共 3.9 GB —— 两个观察器
              各持一份就是 1.8 GB, 直接把自己撑死。所以学习监督那边拿到的是
              `lambda: game_watcher.encoder()`（"一个能力只有一条实现"）。
        """
        return self._model

    def ensure_model(self, state: Any) -> bool:
        """按状态加载/卸载模型（**STUDY/GAME 常驻, 其它状态卸载**）。

        @return 现在模型在不在内存里
        @note 加载前看内存水位: 不够就**不加载**, 并记一句"为什么"（不静默）。
        """
        wanted = str(state or "").upper() in self.resident_states
        if not wanted:
            if self._model is not None:
                self.unload()
                self._notes.append("离开 %s，把 SigLIP 卸载了（释放约 900 MB）"
                                   % "/".join(self.resident_states))
            return False
        if self._model is not None:
            return True
        available = self._available_mb()
        if available is not None and available < self.mem_watermark_mb:
            self._notes.append("内存不够（剩 %.0f MB < 水位 %.0f MB），这次不加载 SigLIP"
                               % (available, self.mem_watermark_mb))
            self._model_state = "skipped-low-memory"
            return False
        try:
            self._model = self._make_encoder()
            self._model.load() if hasattr(self._model, "load") else None
            self._model_state = "loaded"
            self.log.info("game_watch: SigLIP 已加载（%s 状态常驻）", state)
            return True
        except Exception as exc:                          # noqa: BLE001 - 加载失败不该炸上层
            self._model = None
            self._model_state = "error"
            self._notes.append("SigLIP 加载失败（%s）—— 这次只能靠进程那条路" % exc)
            self.log.warning("game_watch: SigLIP 加载失败: %r", exc)
            return False

    def unload(self) -> None:
        """卸载模型（`close()` 实测能把常驻内存从 923 MB 放回 ~110 MB）。"""
        model, self._model = self._model, None
        self._model_state = "unloaded"
        if model is None:
            return
        closer = getattr(model, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception as exc:                      # noqa: BLE001
                self.log.debug("game_watch: 卸载模型时出错（忽略）: %r", exc)

    # ------------------------------------------------------------ 认 ---
    def encode_frame(self, frame: Any) -> Optional[List[float]]:
        """一帧 -> 图像向量（没模型/编码失败返回 None）。"""
        if self._model is None or frame is None:
            return None
        try:
            vector = self._model.encode_image(frame)
        except Exception as exc:                          # noqa: BLE001
            self.log.warning("game_watch: 这一帧编码失败（跳过）: %r", exc)
            return None
        try:
            return [float(item) for item in list(vector)]
        except TypeError:
            return None

    def identify(self, frame: Any) -> Dict[str, Any]:
        """双路判定（**只看一帧**, 不管间隔/关键词/状态）。

        @return `{"game","source","score","margin","process_games","note","learned"}`
                `game` 为空串 = 认不出（此时**什么都没做**）。
        """
        out: Dict[str, Any] = {"game": "", "source": "", "score": 0.0, "margin": 0.0,
                               "process_games": [], "note": "", "learned": False}
        vector = self.encode_frame(frame)
        anchor_hit: Optional[Dict[str, Any]] = None
        if vector:
            anchor_hit = self.anchors.match(vector)
        confident = bool(anchor_hit and anchor_hit["score"] >= self.confident_score
                         and anchor_hit["margin"] >= self.confident_margin)
        out["score"] = float((anchor_hit or {}).get("score") or 0.0)
        out["margin"] = float((anchor_hit or {}).get("margin") or 0.0)

        process_games: List[str] = []
        process_note = ""
        if self.probe is not None:
            snapshot = self.probe.snapshot()
            process_games = sorted(snapshot.get("games") or {})
            process_note = str(snapshot.get("why") or "")
            if not snapshot.get("ok"):
                process_note = str(snapshot.get("why") or "问不到 PC")
        out["process_games"] = process_games

        def learn(game: str, note: str) -> None:
            if not self.learn or not vector:
                return
            shot = getattr(frame, "tobytes", None)
            try:
                self.anchors.add(game=game, vector=vector,
                                 shot=shot() if callable(shot) else None,
                                 source="auto", note=note, when=self.clock())
                out["learned"] = True
            except Exception as exc:                      # noqa: BLE001 - 学不成不该影响识别
                self._notes.append("锚点没记下来（%s）" % exc)
                self.log.warning("game_watch: 锚点没记下来: %r", exc)

        anchor_game = str((anchor_hit or {}).get("game") or "")
        if process_games:
            # 多个游戏在跑时: 优先取与画面一致的那个（画面给了个"哪个更像"的排序）
            pick = process_games[0]
            if anchor_game and anchor_game in process_games:
                pick = anchor_game
            elif len(process_games) > 1:
                out["note"] = process_note or "PC 上开着多个认识的游戏"
            if confident and anchor_game == pick:
                out.update({"game": pick, "source": "anchor"})
                out["note"] = out["note"] or "画面与进程一致"
                return out
            out.update({"game": pick, "source": "corrected" if confident else "process"})
            if confident and anchor_game and anchor_game != pick:
                out["note"] = ("画面说是 %s（%.3f），进程说是 %s —— 以进程为准，"
                               "并把这一帧学成 %s 的锚点"
                               % (anchor_game, out["score"], pick, pick))
            elif not anchor_hit:
                out["note"] = out["note"] or "锚点库里没有它 —— 记一个锚点（起步阶段）"
            else:
                out["note"] = out["note"] or ("画面没把握（%.3f/余量 %.3f）—— 进程说了算"
                                              % (out["score"], out["margin"]))
            learn(pick, "进程说是 %s" % pick)
            return out
        if confident:
            out.update({"game": anchor_game, "source": "anchor",
                        "note": "只有画面认出来了（PC 上没看到认识的游戏进程）"})
            return out
        if anchor_hit:
            out["note"] = ("认不出：画面最像 %s 但没把握（%.3f/余量 %.3f），PC 上也没有认识的游戏"
                           % (anchor_game, out["score"], out["margin"]))
        else:
            out["note"] = ("认不出：%s" % (process_note or "锚点库是空的，PC 上也没看到认识的游戏"))
        return out

    def tick(self, frame: Any, *, state: Any = "GAME", has_keyword: bool = False,
             force: bool = False) -> Dict[str, Any]:
        """到点了就认一次（含全部"什么时候不看画面"的规矩）。

        @param has_keyword 队列当前是**对话关键词**驱动的 -> 你定的"有对话就不抓帧"
        @return `{"skipped","game","source","score","margin","note","learned","at"}`
        """
        now = float(self.clock())
        out: Dict[str, Any] = {"skipped": "", "game": "", "source": "", "score": 0.0,
                               "margin": 0.0, "note": "", "learned": False, "at": now}
        if has_keyword:
            out["skipped"] = "队列是对话指定的关键词 —— 不抓帧（你定的：有对话就不看画面）"
            return out
        if not force and (now - self.last_at) < self.interval_s:
            out["skipped"] = "还没到间隔（%.0f s / %.0f s）" % (now - self.last_at,
                                                                self.interval_s)
            return out
        if frame is None:
            out["skipped"] = "没拿到画面（串流没在跑？）—— 跳过"
            return out
        if not self.ensure_model(state):
            out["skipped"] = "模型不在内存里（记忆里那句原因）—— 跳过"
            out["note"] = (self._notes[-1] if self._notes else "")
            return out
        self.last_at = now
        decision = self.identify(frame)
        decision["at"] = now
        decision["skipped"] = ""
        self.last_decision = dict(decision)
        if decision.get("game"):
            self.last_game = str(decision["game"])
        return decision

    # ------------------------------------------------------------ 杂 ---
    def notes(self) -> List[str]:
        """攒下来的"要如实说的话"（加载被跳过、卸载、锚点没记下来…）。取走即清空。"""
        out = list(self._notes)
        self._notes = []
        return out

    def snapshot(self) -> Dict[str, Any]:
        """给日志/状态用（谁在常驻、上次认出了什么）。"""
        return {"model": self._model_state, "loaded": self.model_loaded,
                "anchors": len(self.anchors), "games": self.anchors.games()[:8],
                "last_game": self.last_game, "last_at": self.last_at,
                "interval_s": self.interval_s,
                "confident": {"score": self.confident_score, "margin": self.confident_margin},
                "resident_states": list(self.resident_states)}

    def _make_encoder(self) -> Any:
        if self._encoder_factory is not None:
            return self._encoder_factory()
        from agent.config import load_config
        from agent.vision.siglip import SiglipModel

        return SiglipModel.from_config((load_config("config") or {}).get("vision"))

    def _available_mb(self) -> Optional[float]:
        if self._meminfo is not None:
            info = self._meminfo() or {}
        else:
            info = {}
            try:
                with open("/proc/meminfo", encoding="utf-8") as handle:
                    for line in handle:
                        if line.startswith("MemAvailable:"):
                            info = {"MemAvailable": int(line.split()[1]) / 1024.0}
                            break
            except OSError:
                return None
        try:
            value = float(info.get("MemAvailable") or 0.0)
        except (TypeError, ValueError):
            return None
        return value or None
