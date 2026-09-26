# ============================================================================
#  agent/core/state_machine.py — 四个运行状态的转换
#
#  状态图 (docs/architecture.md 的状态层)
#  ---------------------------------------------------------------------------
#                    ┌─────────── SLEEP ⇄ IDLE ───────────┐
#                    │                                     │
#                    ▼                                     ▼
#                 STUDY ⇄ IDLE                        GAME ⇄ IDLE
#
#  合法转换表 (唯一事实来源: LEGAL_TRANSITIONS)
#      SLEEP ⇄ IDLE
#      IDLE  ⇄ STUDY
#      IDLE  ⇄ GAME
#      STUDY ⇄ IDLE        (与 IDLE ⇄ STUDY 是同一条边, 方向相反)
#      GAME  ⇄ IDLE
#
#  也就是说: **任何状态切换都必须经过 IDLE**。SLEEP 不能直接进 STUDY/GAME,
#  STUDY 也不能直接进 GAME。这不是限制, 是刻意的: IDLE 是唯一的"公共锚点",
#  两个活跃状态之间直接跳会让"休眠前保存了什么/游戏前加载了什么"失去唯一入口。
#
#  ⚠ T12-1: 规矩早就写在这张表里, 但**没人走多步** —— 调用方（日程/按钮）看到
#    GAME->SLEEP 非法就放弃了, 结果是"到点了没切过去"。现在多了 `transition_to()`:
#    它按这张表算路径（有直边一跳, 否则 经 IDLE 两跳）并**逐跳执行**, 每一跳都会
#    触发 `on_change` —— 于是"离开 GAME 要释放的东西"先放, 再轮到 IDLE 的。
#    `transition()`（单跳）原样保留。
#
#  设计边界 (按约定不做的事)
#  ---------------------------------------------------------------------------
#  · **不做** 状态持久化 —— 重启即回到初始状态
#  · **不做** 超时自动转换 —— 什么时候该睡/该学/该玩, 由 scheduler 决定
#  · **不做** 状态变更日志 —— 需要审计的调用方自己在回调里落盘
#  本模块只负责: 记住当前状态 + 判定转换是否合法 + 通知观察者。
#
#  非法转换
#  ---------------------------------------------------------------------------
#  transition() 对非法输入一律 **返回 False, 不抛异常**。理由: 状态转换的触发源
#  包括 IPC 消息和 LLM 输出, 让"说了个不存在的状态"直接把调用链炸掉, 不如返回
#  False 让调用方决定怎么处理 (通常就是忽略 + 记一条自己的日志)。
# ============================================================================

from __future__ import annotations

import sys
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set

__all__ = [
    "State",
    "StateMachine",
    "LEGAL_TRANSITIONS",
    "INITIAL_STATE",
]


class State(Enum):
    """四个运行状态。

    取值是小写字符串 (而不是 auto()), 因为 IPC 消息里传的就是这个字符串:
    序列化时直接用 .value, 反序列化时 State("sleep") 就能还原。
    """

    SLEEP = "sleep"
    IDLE = "idle"
    STUDY = "study"
    GAME = "game"


#: 初始状态。开机默认 IDLE —— 板子起来就在待命, 不是睡眠也不是某个活跃态。
INITIAL_STATE = State.IDLE

#: 合法转换表: from -> 允许到达的 to 集合
#:
#: 对称性检查 (有测试守着): 每条边都是双向的, 所以这张表反过来读也成立。
#: 加新状态时改这里一处即可, 判定/校验都从这里取。
LEGAL_TRANSITIONS: Dict[State, Set[State]] = {
    State.SLEEP: {State.IDLE},
    State.IDLE: {State.SLEEP, State.STUDY, State.GAME},
    State.STUDY: {State.IDLE},
    State.GAME: {State.IDLE},
}


class StateMachine:
    """持有当前状态, 校验并执行转换, 变更时通知回调。

    典型用法::

        sm = StateMachine()
        sm.on_change(lambda old, new: print(old, "->", new))
        sm.transition(State.STUDY, "用户说开始学习")
        sm.transition(State.GAME, "想打游戏")      # False: STUDY 不能直接进 GAME
    """

    def __init__(
        self,
        initial: State = INITIAL_STATE,
        connected_check: Optional[Callable[[], bool]] = None,
    ) -> None:
        """
        @param initial         初始状态 (默认 IDLE)
        @param connected_check 可选: 判断 moonlight 是否已连接的可调用对象。
                               传 None 时 is_connected() 去问 agent.io;
                               注入它是为了让本模块不依赖 native 也能测。
        """
        self._state: State = initial
        self._callbacks: List[Callable[[State, State], None]] = []
        self._connected_check = connected_check
        self._callback_errors = 0
        self._last_reason: Optional[str] = None

    # ------------------------------------------------------------ 查询 ---
    def current(self) -> State:
        """当前状态。"""
        return self._state

    def can_transition(self, to: State) -> bool:
        """判断能否转换到 to (不产生任何副作用)。

        供调用方在**动手之前**探测, 例如 GUI 里把不可选的目标灰掉。
        """
        target = self._coerce(to)
        if target is None or target is self._state:
            return False
        return target in LEGAL_TRANSITIONS.get(self._state, set())

    def is_connected(self) -> bool:
        """moonlight 是否已连接。

        与状态**无关**: 四个状态里任何一个都可能连着或没连着 (例如 IDLE 时
        还握着上一个会话, GAME 时可能刚掉线)。所以这里不去推断, 而是直接问
        数据源 —— 这正是把它单独列出来的原因。

        优先级:
          1. 构造时注入的 connected_check
          2. agent.io 的 native status (拿不到就当未连接)
        """
        if self._connected_check is not None:
            try:
                return bool(self._connected_check())
            except Exception:  # noqa: BLE001 - 探测失败等同于"没连上"
                return False

        return self._connected_from_native()

    @staticmethod
    def _connected_from_native() -> bool:
        """问 agent_native.moonlight.status()['connected']。

        native 缺席 (宿主机、或 .so 没部署) 时返回 False 而不是抛异常 ——
        is_connected() 是查询接口, 不该让调用方为"环境不完整"写 try/except。
        """
        try:
            from ..io._native import get_native

            status = get_native().moonlight.status()
            return bool(status.get("connected", False))
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------ 转换 ---
    def transition(self, to: State, reason: str) -> bool:
        """尝试转换到 to（**单跳**）。

        @param to     目标状态; 接受 State 成员, 也接受 "study" 这样的小写字符串
                      (IPC / 配置里拿到的是字符串)
        @param reason 转换原因, 透传给回调 (给 GUI 显示/IPC 广播用)
        @return 成功 True; 非法转换或已经在目标状态则 False (不抛异常)

        @note 回调在**状态已经改好之后**触发, 所以回调里 current() 拿到的
              一定是新状态。
        @note 想说"我要去 SLEEP"（不管现在在哪儿）请用 `transition_to()` —— 它会按
              **状态图的规矩**自己经 IDLE 中转, 而不是把非法的那一跳丢掉。
        """
        target = self._coerce(to)

        # 无法识别的目标 (例如 "banana")、或已经在目标状态 -> 否
        if target is None or target is self._state:
            return False
        if target not in LEGAL_TRANSITIONS.get(self._state, set()):
            return False

        previous = self._state
        self._state = target
        self._notify(previous, target, reason)
        return True

    def plan_path(self, to: State) -> List[State]:
        """算"从当前状态走到 to"的**跳法**（纯计算, 无副作用）。

        @return 途经的目标状态列表（不含当前状态）; **空列表 = 走不到**
        @note 有直边就一跳; 没有就 `当前 -> IDLE -> 目标` 两跳 —— 这正是状态图那条
              "任何切换都必须经过 IDLE" 的规矩落到代码里的样子。
        """
        target = self._coerce(to)
        if target is None or target is self._state:
            return []
        if target in LEGAL_TRANSITIONS.get(self._state, set()):
            return [target]
        via_idle = LEGAL_TRANSITIONS.get(self._state, set())
        if State.IDLE in via_idle and target in LEGAL_TRANSITIONS.get(State.IDLE, set()):
            return [State.IDLE, target]
        return []

    def transition_to(self, to: State, reason: str) -> Dict[str, Any]:
        """**按状态图的规矩走到 to**（必要时经 IDLE 中转）—— **一跳到一跳动**（T12-1）。

        @return {"ok": bool, "steps": [{"from","to"}, …], "why": str}

        ⚠ 为什么要有它（你定的规矩）: 从 GAME/STUDY 去 SLEEP **不是**一次转换, 而是
          `GAME -> IDLE -> SLEEP` 两次。逐跳执行意味着**每一跳都会触发 `on_change`**,
          于是"离开那个模式要释放的东西"按步发生 —— 释 GAME 的（视频/队列/SigLIP）先走,
          然后才轮到 IDLE 的, 最后进 SLEEP（停 llama-server）。如果一口跳到 SLEEP,
          GAME 那边开着的视频与常驻模型就没人放了。

        @note 每一跳仍然走 `transition()`（单跳校验 + 通知都在那儿）, 所以路径是算出来的
              也照样会被再校验一次 —— 表改了这里不会"偷偷跳过去", 而是返回 ok=False。
        @note 触发源: 日程到点(`Scheduler._apply_action`)、GUI/CLI 的 `switch_mode`、
              以后的工具。只想做"合法的单跳"就还用 `transition()`。
        """
        target = self._coerce(to)
        if target is None:
            return {"ok": False, "steps": [], "why": "不认识的模式 %r" % (to,)}
        if target is self._state:
            return {"ok": False, "steps": [], "why": "已经在这个模式（%s）" % target.value}

        hops = self.plan_path(target)
        if not hops:
            return {"ok": False, "steps": [],
                    "why": "状态图里从 %s 走不到 %s" % (self._state.value, target.value)}

        steps: List[Dict[str, str]] = []
        for hop in hops:
            previous = self._state
            if not self.transition(hop, reason):
                return {"ok": False, "steps": steps,
                        "why": "%s -> %s 被状态机拒了（路径是算出来的, 不该发生）"
                               % (previous.value, hop.value)}
            steps.append({"from": previous.value, "to": hop.value})
        return {"ok": True, "steps": steps}

    # ------------------------------------------------------------ 回调 ---
    def on_change(self, callback: Callable[[State, State], None]) -> None:
        """注册状态变更回调 callback(old_state, new_state)。

        可以注册多个, 按注册顺序依次调用。

        @note 回调**不接受 reason** —— 签名是 (old, new)。reason 通过
              last_reason 属性读取, 这样既保持签名简洁, 又不用为了
              "想知道原因" 而把签名改复杂。
        """
        if not callable(callback):
            raise TypeError("callback must be callable")
        self._callbacks.append(callback)

    def remove_callback(self, callback: Callable[[State, State], None]) -> bool:
        """注销回调。返回是否真的删掉了一个。

        不是签名要求的接口, 但没有它就没法在测试/热重载里干净地撤掉回调。
        """
        try:
            self._callbacks.remove(callback)
            return True
        except ValueError:
            return False

    @property
    def callback_count(self) -> int:
        """已注册的回调数量。"""
        return len(self._callbacks)

    @property
    def callback_errors(self) -> int:
        """回调抛异常的累计次数 (诊断用)。

        回调抛异常不会让 transition() 失败, 也不会影响后续回调 ——
        否则一个 GUI 通知失败会把状态机自己搞成半死不活。
        异常本身打到 stderr, 这个计数用来在测试/监控里断言。
        """
        return self._callback_errors

    @property
    def last_reason(self) -> Optional[str]:
        """最近一次成功转换的 reason; 从未转换过时为 None。"""
        return self._last_reason

    # ------------------------------------------------------------ 内部 ---
    def _notify(self, old: State, new: State, reason: str) -> None:
        self._last_reason = reason
        # 复制一份再遍历: 回调里注册/注销回调不该影响本次派发
        for callback in list(self._callbacks):
            try:
                callback(old, new)
            except Exception as exc:  # noqa: BLE001
                self._callback_errors += 1
                print(
                    "StateMachine: callback %r raised on %s->%s: %r"
                    % (getattr(callback, "__name__", callback), old.value, new.value, exc),
                    file=sys.stderr,
                )

    @staticmethod
    def _coerce(value) -> Optional[State]:
        """把入参归一成 State; 认不出来返回 None (而不是抛异常)。"""
        if isinstance(value, State):
            return value
        if isinstance(value, str):
            try:
                return State(value.strip().lower())
            except ValueError:
                return None
        return None

    def __repr__(self) -> str:
        return "<StateMachine %s connected=%s callbacks=%d>" % (
            self._state.value,
            self.is_connected(),
            len(self._callbacks),
        )
