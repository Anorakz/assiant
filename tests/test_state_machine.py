#!/usr/bin/env python3
"""
tests/test_state_machine.py — StateMachine 单测

运行:
    python tests/test_state_machine.py

覆盖:
  · 合法转换: SLEEP⇄IDLE、IDLE⇄STUDY、IDLE⇄GAME (以及表本身的对称性)
  · 非法转换: SLEEP→STUDY/GAME、STUDY→GAME、跨状态直跳 —— 返回 False 且状态不变
  · 自转换 (to == current) 返回 False
  · 无法识别的目标 / 非 State 类型 —— 返回 False, 不抛异常
  · 回调: 触发时机 (改完再触发)、入参 (old, new)、顺序、多个回调
  · 回调抛异常不影响状态机本身
  · 字符串目标 (IPC 场景) 能识别
  · is_connected: 注入检查 / native 缺席 / native 异常
"""

import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.core.state_machine import (  # noqa: E402
    INITIAL_STATE,
    LEGAL_TRANSITIONS,
    State,
    StateMachine,
)


class TestStateEnum(unittest.TestCase):
    def test_members_and_values(self):
        self.assertEqual(
            {s.name: s.value for s in State},
            {"SLEEP": "sleep", "IDLE": "idle", "STUDY": "study", "GAME": "game"},
        )

    def test_value_lookup_roundtrip(self):
        # IPC 传的是字符串, 所以 State("sleep") 必须能还原
        for s in State:
            self.assertIs(State(s.value), s)

    def test_initial_state_is_idle(self):
        self.assertIs(INITIAL_STATE, State.IDLE)


class TestTransitionTable(unittest.TestCase):
    """转换表本身的性质。"""

    def test_all_states_present(self):
        self.assertEqual(set(LEGAL_TRANSITIONS), set(State))

    def test_idle_is_the_hub(self):
        # 任何状态切换都必须经过 IDLE
        self.assertEqual(
            LEGAL_TRANSITIONS[State.IDLE],
            {State.SLEEP, State.STUDY, State.GAME},
        )
        for active in (State.SLEEP, State.STUDY, State.GAME):
            self.assertEqual(LEGAL_TRANSITIONS[active], {State.IDLE})

    def test_no_self_transition_listed(self):
        for src, dsts in LEGAL_TRANSITIONS.items():
            self.assertNotIn(src, dsts, "%s 不该在表里自转换" % src)

    def test_table_is_symmetric(self):
        # ⇄ 表示双向: 每条边反过来也必须在表里
        for src, dsts in LEGAL_TRANSITIONS.items():
            for dst in dsts:
                self.assertIn(src, LEGAL_TRANSITIONS[dst],
                              "%s->%s 有, 但 %s->%s 没有" % (src, dst, dst, src))

    def test_illegal_pairs_are_absent(self):
        for a, b in [(State.SLEEP, State.STUDY), (State.SLEEP, State.GAME),
                     (State.STUDY, State.GAME), (State.GAME, State.STUDY)]:
            self.assertNotIn(b, LEGAL_TRANSITIONS[a])


class TestLegalTransitions(unittest.TestCase):
    def setUp(self):
        self.sm = StateMachine()

    def test_starts_idle(self):
        self.assertIs(self.sm.current(), State.IDLE)

    def test_custom_initial_state(self):
        self.assertIs(StateMachine(initial=State.SLEEP).current(), State.SLEEP)

    def test_idle_to_each_active_state(self):
        for target in (State.SLEEP, State.STUDY, State.GAME):
            with self.subTest(target=target):
                sm = StateMachine()
                self.assertTrue(sm.transition(target, "test"))
                self.assertIs(sm.current(), target)

    def test_each_active_state_back_to_idle(self):
        for source in (State.SLEEP, State.STUDY, State.GAME):
            with self.subTest(source=source):
                sm = StateMachine(initial=source)
                self.assertTrue(sm.transition(State.IDLE, "back"))
                self.assertIs(sm.current(), State.IDLE)

    def test_full_round_trip_through_idle(self):
        # IDLE -> STUDY -> IDLE -> GAME -> IDLE -> SLEEP -> IDLE
        route = [
            (State.STUDY, True), (State.IDLE, True),
            (State.GAME, True), (State.IDLE, True),
            (State.SLEEP, True), (State.IDLE, True),
        ]
        for target, expected in route:
            with self.subTest(target=target):
                self.assertEqual(self.sm.transition(target, "route"), expected)
                self.assertIs(self.sm.current(), target)

    def test_accepts_string_target(self):
        # IPC / 配置里拿到的是字符串
        self.assertTrue(self.sm.transition("study", "from ipc"))
        self.assertIs(self.sm.current(), State.STUDY)
        self.assertTrue(self.sm.transition("idle", "from ipc"))
        self.assertIs(self.sm.current(), State.IDLE)

    def test_string_target_is_case_and_space_insensitive(self):
        self.assertTrue(self.sm.transition("  STUDY  ", "x"))
        self.assertIs(self.sm.current(), State.STUDY)

    def test_can_transition_probe(self):
        self.assertTrue(self.sm.can_transition(State.STUDY))
        self.assertFalse(self.sm.can_transition(State.IDLE), "自转换不算合法")
        self.assertFalse(self.sm.can_transition("banana"))
        # 探测不能有副作用
        self.assertIs(self.sm.current(), State.IDLE)

    def test_can_transition_after_moving(self):
        self.sm.transition(State.STUDY, "go")
        self.assertTrue(self.sm.can_transition(State.IDLE))
        self.assertFalse(self.sm.can_transition(State.GAME))
        self.assertFalse(self.sm.can_transition(State.SLEEP))


class TestIllegalTransitions(unittest.TestCase):
    """非法转换必须返回 False 且不改变状态、不触发回调。"""

    def test_sleep_to_active_states(self):
        for target in (State.STUDY, State.GAME):
            with self.subTest(target=target):
                sm = StateMachine(initial=State.SLEEP)
                self.assertFalse(sm.transition(target, "illegal"))
                self.assertIs(sm.current(), State.SLEEP, "状态不该变")

    def test_study_to_game_and_back(self):
        sm = StateMachine(initial=State.STUDY)
        self.assertFalse(sm.transition(State.GAME, "illegal"))
        self.assertIs(sm.current(), State.STUDY)

        sm = StateMachine(initial=State.GAME)
        self.assertFalse(sm.transition(State.STUDY, "illegal"))
        self.assertIs(sm.current(), State.GAME)

    def test_self_transition_returns_false(self):
        for s in State:
            with self.subTest(state=s):
                sm = StateMachine(initial=s)
                self.assertFalse(sm.transition(s, "same"))
                self.assertIs(sm.current(), s)

    def test_unknown_string_returns_false(self):
        sm = StateMachine()
        self.assertFalse(sm.transition("banana", "?"))
        self.assertIs(sm.current(), State.IDLE)

    def test_unknown_string_does_not_raise(self):
        sm = StateMachine()
        for bad in ("", "  ", "SLEEPING", "sle", "idle2"):
            with self.subTest(bad=bad):
                self.assertFalse(sm.transition(bad, "?"))

    def test_non_state_types_return_false(self):
        sm = StateMachine()
        for bad in (None, 0, 1, 1.5, [], {}, object(), State):
            with self.subTest(bad=bad):
                self.assertFalse(sm.transition(bad, "?"))
        self.assertIs(sm.current(), State.IDLE)

    def test_illegal_transition_does_not_fire_callback(self):
        sm = StateMachine(initial=State.SLEEP)
        seen = []
        sm.on_change(lambda old, new: seen.append((old, new)))

        self.assertFalse(sm.transition(State.GAME, "illegal"))
        self.assertEqual(seen, [], "非法转换不该触发回调")

    def test_illegal_transition_ignores_reason(self):
        sm = StateMachine(initial=State.SLEEP)
        sm.transition(State.GAME, "should be ignored")
        self.assertIsNone(sm.last_reason)


class TestCallbacks(unittest.TestCase):
    def test_callback_receives_old_and_new(self):
        sm = StateMachine()
        seen = []
        sm.on_change(lambda old, new: seen.append((old, new)))

        sm.transition(State.STUDY, "go")
        self.assertEqual(seen, [(State.IDLE, State.STUDY)])

    def test_callback_fires_after_state_is_committed(self):
        # 回调里 current() 必须是新状态, 否则 GUI 会显示旧状态
        sm = StateMachine()
        captured = []
        sm.on_change(lambda old, new: captured.append(sm.current()))

        sm.transition(State.GAME, "go")
        self.assertEqual(captured, [State.GAME])

    def test_multiple_callbacks_in_registration_order(self):
        sm = StateMachine()
        order = []
        sm.on_change(lambda o, n: order.append("first"))
        sm.on_change(lambda o, n: order.append("second"))
        sm.on_change(lambda o, n: order.append("third"))

        sm.transition(State.SLEEP, "go")
        self.assertEqual(order, ["first", "second", "third"])

    def test_callback_fires_for_every_transition(self):
        sm = StateMachine()
        seen = []
        sm.on_change(lambda old, new: seen.append((old.value, new.value)))

        sm.transition(State.STUDY, "a")
        sm.transition(State.IDLE, "b")
        sm.transition(State.GAME, "c")

        self.assertEqual(
            seen,
            [("idle", "study"), ("study", "idle"), ("idle", "game")],
        )

    def test_callback_count(self):
        sm = StateMachine()
        self.assertEqual(sm.callback_count, 0)
        sm.on_change(lambda o, n: None)
        sm.on_change(lambda o, n: None)
        self.assertEqual(sm.callback_count, 2)

    def test_remove_callback(self):
        sm = StateMachine()
        seen = []

        def cb(old, new):
            seen.append(new)

        sm.on_change(cb)
        self.assertTrue(sm.remove_callback(cb))
        sm.transition(State.STUDY, "go")
        self.assertEqual(seen, [])
        self.assertFalse(sm.remove_callback(cb), "重复注销返回 False")

    def test_non_callable_is_rejected(self):
        sm = StateMachine()
        for bad in (None, 1, "f", []):
            with self.subTest(bad=bad):
                with self.assertRaises(TypeError):
                    sm.on_change(bad)

    def test_callback_exception_does_not_break_transition(self):
        sm = StateMachine()

        def boom(old, new):
            raise RuntimeError("gui 挂了")

        sm.on_change(boom)
        self.assertTrue(sm.transition(State.STUDY, "go"), "回调炸了也要算转换成功")
        self.assertIs(sm.current(), State.STUDY)
        self.assertEqual(sm.callback_errors, 1)

    def test_callback_exception_does_not_block_later_callbacks(self):
        sm = StateMachine()
        seen = []
        sm.on_change(lambda o, n: (_ for _ in ()).throw(RuntimeError("x")))
        sm.on_change(lambda o, n: seen.append(n))

        sm.transition(State.GAME, "go")
        self.assertEqual(seen, [State.GAME], "前一个回调失败不该拦住后面的")

    def test_callback_registering_another_callback_is_safe(self):
        # 派发时遍历的是副本, 回调里注册不该影响本次派发
        sm = StateMachine()
        seen = []

        def adder(old, new):
            seen.append("adder")
            sm.on_change(lambda o, n: seen.append("added"))

        sm.on_change(adder)
        sm.transition(State.STUDY, "go")
        self.assertEqual(seen, ["adder"], "新注册的回调下次才生效")

        sm.transition(State.IDLE, "back")
        self.assertEqual(seen, ["adder", "adder", "added"])

    def test_last_reason_is_exposed(self):
        sm = StateMachine()
        self.assertIsNone(sm.last_reason)
        sm.transition(State.STUDY, "用户说开始学习")
        self.assertEqual(sm.last_reason, "用户说开始学习")
        sm.transition(State.IDLE, "结束")
        self.assertEqual(sm.last_reason, "结束")

    def test_reason_can_be_empty(self):
        sm = StateMachine()
        self.assertTrue(sm.transition(State.STUDY, ""))
        self.assertEqual(sm.last_reason, "")


class TestIsConnected(unittest.TestCase):
    def test_injected_check_true(self):
        sm = StateMachine(connected_check=lambda: True)
        self.assertTrue(sm.is_connected())

    def test_injected_check_false(self):
        sm = StateMachine(connected_check=lambda: False)
        self.assertFalse(sm.is_connected())

    def test_injected_check_called_each_time(self):
        # 连接状态会变, 不能缓存
        state = {"connected": False}
        sm = StateMachine(connected_check=lambda: state["connected"])
        self.assertFalse(sm.is_connected())
        state["connected"] = True
        self.assertTrue(sm.is_connected())

    def test_injected_check_exception_means_disconnected(self):
        def boom():
            raise RuntimeError("native 挂了")

        sm = StateMachine(connected_check=boom)
        self.assertFalse(sm.is_connected(), "探测失败应当当作未连接, 而不是抛出去")

    def test_truthy_return_is_coerced_to_bool(self):
        sm = StateMachine(connected_check=lambda: 1)
        self.assertIs(sm.is_connected(), True)

    def test_default_path_never_raises(self):
        """不注入 connected_check 时, is_connected() 必须总能给出一个 bool。

        结果取决于环境, 所以不断言具体值:
          · 宿主机上没有 agent_native -> False
          · 板端有 .so, 但没连流 -> 也是 False
          · 真连着流 -> True
        这里要守的是"查询接口不抛异常、且必定返回 bool"这条契约。
        """
        from agent.io import _native as native_mod

        native_mod.reset_native()  # 确保走"按需 import"那条分支
        result = StateMachine().is_connected()
        self.assertIsInstance(result, bool)

    def test_without_native_returns_false(self):
        # 明确模拟"native 装不上"的场景
        from agent.io import _native as native_mod

        class _NoNative:
            """get_native() 会 import agent_native; 用属性访问失败来模拟缺 .so。"""

            def __getattr__(self, name):
                raise ImportError("simulated missing agent_native")

        native_mod.set_native(_NoNative())
        try:
            self.assertIs(StateMachine().is_connected(), False)
        finally:
            native_mod.reset_native()

    def test_independent_of_state(self):
        # 连接与否和当前状态无关: 四个状态都能查, 值由数据源决定
        sm = StateMachine(connected_check=lambda: True)
        for target in (State.STUDY, State.IDLE, State.GAME, State.IDLE, State.SLEEP):
            with self.subTest(target=target):
                sm.transition(target, "x")
                self.assertTrue(sm.is_connected())

    def test_native_status_is_consulted_when_available(self):
        # 注入一个假 native, 确认走的是 moonlight.status()['connected']
        from agent.io import _native as native_mod

        class _FakeMoonlight:
            def __init__(self, connected):
                self._connected = connected

            def status(self):
                return {"connected": self._connected}

        class _FakeNative:
            def __init__(self, connected):
                self.moonlight = _FakeMoonlight(connected)

        native_mod.set_native(_FakeNative(True))
        try:
            self.assertTrue(StateMachine().is_connected())
            native_mod.set_native(_FakeNative(False))
            self.assertFalse(StateMachine().is_connected())
        finally:
            native_mod.reset_native()

    def test_native_status_missing_key_is_false(self):
        from agent.io import _native as native_mod

        class _FakeNative:
            class moonlight:  # noqa: N801
                @staticmethod
                def status():
                    return {}

        native_mod.set_native(_FakeNative())
        try:
            self.assertIs(StateMachine().is_connected(), False)
        finally:
            native_mod.reset_native()

    def test_native_status_raising_is_false(self):
        from agent.io import _native as native_mod

        class _FakeNative:
            class moonlight:  # noqa: N801
                @staticmethod
                def status():
                    raise RuntimeError("native 挂了")

        native_mod.set_native(_FakeNative())
        try:
            self.assertIs(StateMachine().is_connected(), False)
        finally:
            native_mod.reset_native()


class TestRepr(unittest.TestCase):
    def test_repr_contains_state(self):
        sm = StateMachine(connected_check=lambda: False)
        text = repr(sm)
        self.assertIn("idle", text)
        self.assertIn("connected=False", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
