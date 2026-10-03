#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`study_watch.py` 的单测 —— **全离线**（假帧 / 假 SigLIP / 假进程探针 / 假时钟）。

`tests/test_game_watch.py` 的同款安排: 锚点与统计都写到临时目录，不碰仓库那两份。

这里钉住的都是"设计里最容易悄悄坏掉"的几条:

  · 提醒**不算**一次不通过（3 次不通过不含提醒）; 提醒后 5 分钟一查;
  · `unknown` **完全中性** —— 不提醒、不计数、不推进也不重置那条升级链;
  · **没画面/没模型是"跳过"**，不是 unknown（否则会白白把判不出来的比例抬高）;
  · 画面与进程名**对不上 -> unknown**（不硬猜、也不学锚点）;
  · 判定吃的是**相对分**（两个大类原型的余弦之差）与**没把握带**: 带内 -> unknown
    （T13-4 板端标定定的口径; 绝对余弦的两个大类会重叠, 不能用）;
  · 没把握带一次只动 0.01、样本不够不动、到界就停、每次调整都有理由;
  · 弹回桌面后 5 分钟内又判成学习 -> 记"可能误判" + 那个类停学。
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.core import study_watch as sw                                # noqa: E402
from agent.core.study_anchors import StudyAnchors                       # noqa: E402
from agent.core.study_stats import StudyStats                           # noqa: E402

DIM = 768
MIN = 60.0
STUDY_VEC = [1.0, 0.0, 0.0] + [0.0] * (DIM - 3)
NOT_STUDY_VEC = [0.0, 1.0, 0.0] + [0.0] * (DIM - 3)
FAR_VEC = [0.0, 0.0, 1.0] + [0.0] * (DIM - 3)
#: 相对分 ≈ +0.036 —— **在 0.05 的没把握带以内**（用来验"带内 -> unknown"）。
INSIDE_BAND_VEC = [1.0, 0.95, 0.0] + [0.0] * (DIM - 3)


def vector(*values):
    out = [float(v) for v in values][:DIM]
    return out + [0.0] * (DIM - len(out))


class FakeClock(object):
    def __init__(self, start=1700000000.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)
        return self.now


class FakeFrame(object):
    def __init__(self, payload=b"fake-jpeg"):
        self.payload = payload

    def tobytes(self):
        return self.payload


class ArrayLike(object):
    """只有 `max()` 的假数组。

    ⚠ 开发机上**没有 numpy**, 而 `_is_blank()` 只用到 `frame.max()` —— 所以用这个替身
      就能在两边都测到"全黑帧 = 跳过"这条, 不必等板端。
    """

    def __init__(self, peak):
        self.peak = peak

    def max(self):
        return self.peak

    def tobytes(self):
        return b"fake-array"


class FakeModel(object):
    """假 SigLIP: 想给什么向量就给什么（`fail=True` 模拟编码炸了）。"""

    def __init__(self, vec=None, fail=False):
        self.vec = list(vec or STUDY_VEC)
        self.fail = fail
        self.seen = 0

    def encode_image(self, frame):
        self.seen += 1
        if self.fail:
            raise RuntimeError("编码炸了")
        return list(self.vec)


class FakeProbe(object):
    """假进程探针: `games` 就是 `PcProbe.snapshot()` 里那张 {映射值: [进程名]}。"""

    def __init__(self, games=None, ok=True, why=""):
        self.games = dict(games or {})
        self.ok = ok
        self.why = why
        self.calls = 0

    def snapshot(self):
        self.calls += 1
        return {"ok": self.ok, "games": dict(self.games), "why": self.why}


class WatchCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="study-watch-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.anchor_file = os.path.join(self.tmp, "study_anchors.jsonl")
        self.stats_file = os.path.join(self.tmp, "study_stats.json")
        self.shot_dir = os.path.join(self.tmp, "shots")
        self.clock = FakeClock()

    def make_anchors(self, **kwargs):
        kwargs.setdefault("shot_dir", self.shot_dir)
        return StudyAnchors(self.anchor_file, **kwargs)

    def make_stats(self, **kwargs):
        return StudyStats(self.stats_file, **kwargs)

    def make_watcher(self, *, model=None, probe=None, anchors=None, stats=None,
                     with_stats=True, **kwargs):
        self.anchors = anchors if anchors is not None else self.make_anchors()
        self.stats = stats if stats is not None else (self.make_stats() if with_stats else None)
        self.model = model
        watcher = sw.StudyWatcher(self.anchors, stats=self.stats, probe=probe,
                                  encoder=model, clock=self.clock, **kwargs)
        watcher.seed_thresholds()
        return watcher

    def add(self, cls, vec):
        return self.anchors.add(cls=cls, vector=list(vec), when=self.clock.now)

    def seed_both(self):
        """两个大类各放一条锚点。

        ⚠ 判定要用**两个大类原型**的相对分, 所以**两边都得有锚点**才算得出来
          （只有一边时 `relative is None` -> 画面没把握 -> 走进程名辅助或 unknown）。
          标定给的那 39 条本来两边都有; 这是单测里的最小起点。
        """
        self.add("code", STUDY_VEC)
        self.add("anime", NOT_STUDY_VEC)


# ===========================================================================
#  判定: 锚点 / 进程 / 冲突 / 中性
# ===========================================================================
class TestVerdict(WatchCase):
    def test_empty_library_and_no_process_is_unknown(self):
        watcher = self.make_watcher(model=FakeModel(STUDY_VEC))
        out = watcher.verdict(FakeFrame())
        self.assertEqual(out["verdict"], sw.VERDICT_UNKNOWN)
        self.assertEqual(out["source"], "none")
        self.assertIn("锚点库是空的", out["note"])

    def test_confident_picture_decides_alone(self):
        """两个大类都有锚点 -> 相对分说得很清楚 -> 画面自己就能定。"""
        watcher = self.make_watcher(model=FakeModel(STUDY_VEC))
        self.seed_both()
        out = watcher.verdict(FakeFrame())
        self.assertEqual(out["verdict"], sw.VERDICT_STUDY)
        self.assertEqual(out["source"], "anchor")
        self.assertEqual(out["cls"], "code")
        self.assertTrue(out["confident"])
        self.assertAlmostEqual(out["relative"], 1.0, places=3)

    def test_not_study_comes_from_the_not_study_classes(self):
        watcher = self.make_watcher(model=FakeModel(NOT_STUDY_VEC))
        self.seed_both()
        out = watcher.verdict(FakeFrame())
        self.assertEqual(out["verdict"], sw.VERDICT_NOT_STUDY)
        self.assertEqual(out["cls"], "anime")
        self.assertEqual(out["category"], "not_study")
        self.assertAlmostEqual(out["relative"], -1.0, places=3)

    def test_a_frame_inside_the_band_is_unknown(self):
        """**带内 -> unknown**（中性）: 画面像 study 但只多 0.036 —— 不到 0.05 不算数。"""
        watcher = self.make_watcher(model=FakeModel(INSIDE_BAND_VEC))
        self.seed_both()
        out = watcher.verdict(FakeFrame())
        self.assertEqual(out["verdict"], sw.VERDICT_UNKNOWN)
        self.assertEqual(out["source"], "none")
        self.assertFalse(out["confident"])
        self.assertGreater(out["relative"], 0.0)
        self.assertLess(out["relative"], 0.05)

    def test_a_wider_band_makes_the_same_frame_unknown(self):
        """带是可配的: 同一帧在 0.01 的带下能判, 在 0.05 的带下判不出来。"""
        tight = self.make_watcher(model=FakeModel(INSIDE_BAND_VEC), relative_band=0.01)
        self.seed_both()
        self.assertEqual(tight.verdict(FakeFrame())["verdict"], sw.VERDICT_STUDY)

    def test_only_one_category_seeded_means_the_picture_cannot_decide(self):
        """只有一边的锚点 -> 相对分算不出来（不硬猜成"更像自己"）。"""
        watcher = self.make_watcher(model=FakeModel(STUDY_VEC))
        self.add("code", STUDY_VEC)
        out = watcher.verdict(FakeFrame())
        self.assertIsNone(out["relative"])
        self.assertEqual(out["verdict"], sw.VERDICT_UNKNOWN)
        self.assertIn("缺一个大类", out["note"])

    def test_process_is_used_only_when_the_picture_is_unsure(self):
        watcher = self.make_watcher(model=FakeModel(FAR_VEC),
                                    probe=FakeProbe({"code": ["pycharm64"]}))
        self.seed_both()
        out = watcher.verdict(FakeFrame())
        self.assertEqual(out["verdict"], sw.VERDICT_STUDY)
        self.assertEqual(out["source"], "process")
        self.assertIn("辅助", out["note"])

    def test_confident_picture_with_agreeing_process_stays_anchor(self):
        watcher = self.make_watcher(model=FakeModel(STUDY_VEC),
                                    probe=FakeProbe({"code": ["pycharm64"]}))
        self.seed_both()
        out = watcher.verdict(FakeFrame())
        self.assertEqual(out["verdict"], sw.VERDICT_STUDY)
        self.assertEqual(out["source"], "anchor")
        self.assertIn("一致", out["note"])

    def test_conflict_is_unknown_not_a_guess(self):
        """画面说"学习"，进程名说"游戏" —— 不硬猜，判不出来。"""
        watcher = self.make_watcher(model=FakeModel(STUDY_VEC),
                                    probe=FakeProbe({"game": ["steam"]}))
        self.seed_both()
        out = watcher.verdict(FakeFrame())
        self.assertEqual(out["verdict"], sw.VERDICT_UNKNOWN)
        self.assertEqual(out["source"], "conflict")
        self.assertIn("对不上", out["note"])

    def test_process_sees_both_kinds_and_disqualifies_itself(self):
        watcher = self.make_watcher(model=FakeModel(FAR_VEC),
                                    probe=FakeProbe({"code": ["pycharm64"],
                                                     "game": ["steam"]}))
        out = watcher.verdict(FakeFrame())
        self.assertEqual(out["verdict"], sw.VERDICT_UNKNOWN)
        self.assertIn("矛盾", out["process"]["why"])

    def test_unmapped_processes_are_ignored(self):
        """表里没有的进程名（浏览器那种）-> 忽略, 不猜。"""
        watcher = self.make_watcher(model=FakeModel(FAR_VEC),
                                    probe=FakeProbe({"chrome": ["chrome.exe"]}))
        with self.assertLogs("agent.core.study_watch", level="WARNING"):
            out = watcher.verdict(FakeFrame())
        self.assertEqual(out["verdict"], sw.VERDICT_UNKNOWN)
        self.assertEqual(out["process"]["category"], "")

    def test_a_broken_probe_is_not_fatal(self):
        watcher = self.make_watcher(model=FakeModel(FAR_VEC),
                                    probe=FakeProbe(ok=False, why="ssh 不通"))
        out = watcher.verdict(FakeFrame())
        self.assertEqual(out["verdict"], sw.VERDICT_UNKNOWN)
        self.assertIn("ssh 不通", out["process"]["why"])

    def test_a_probe_that_raises_is_not_fatal(self):
        class Boom(object):
            def snapshot(self):
                raise RuntimeError("探针炸了")

        watcher = self.make_watcher(model=FakeModel(FAR_VEC), probe=Boom())
        out = watcher.verdict(FakeFrame())
        self.assertEqual(out["verdict"], sw.VERDICT_UNKNOWN)
        self.assertIn("探针炸了", out["process"]["why"])

    def test_encoder_is_resolved_lazily_so_the_shared_model_can_be_handed_over(self):
        """encoder 给一个零参可调用 -> 每次现取（Runtime 把认游戏那份常驻模型接过来）。"""
        model = FakeModel(STUDY_VEC)
        calls = []

        def getter():
            calls.append(1)
            return model

        watcher = self.make_watcher(model=getter)
        self.seed_both()
        self.assertEqual(watcher.verdict(FakeFrame())["verdict"], sw.VERDICT_STUDY)
        self.assertEqual(len(calls), 1)

    def test_a_getter_that_fails_only_costs_this_round(self):
        def getter():
            raise RuntimeError("模型还没加载")

        watcher = self.make_watcher(model=getter)
        out = watcher.verdict(FakeFrame())
        self.assertEqual(out["verdict"], sw.VERDICT_UNKNOWN)
        self.assertTrue(any("取 SigLIP 失败" in note for note in watcher.notes()))


# ===========================================================================
#  升级链: 提醒 -> 5 分钟复查 -> 3 次 -> 返回桌面 -> 冷却
# ===========================================================================
class TestEscalation(WatchCase):
    def make_not_study(self, **kwargs):
        watcher = self.make_watcher(model=FakeModel(NOT_STUDY_VEC), **kwargs)
        self.seed_both()                                    # 判定要两边都有锚点
        return watcher

    def test_first_time_only_reminds_and_does_not_count(self):
        watcher = self.make_not_study()
        out = watcher.tick(FakeFrame())
        self.assertEqual(out["action"], sw.ACTION_REMIND)
        self.assertEqual(out["text"], sw.REMIND_TEXT)
        self.assertEqual(out["failures"], 0)
        self.assertEqual(self.stats.counter("reminded"), 1)
        self.assertEqual(self.stats.counter("back_to_desktop"), 0)

    def test_reminder_is_not_one_of_the_three_failures(self):
        watcher = self.make_not_study()
        watcher.tick(FakeFrame())                       # 提醒（第 0 次）
        for expected in (1, 2):
            self.clock.advance(5 * MIN)
            out = watcher.tick(FakeFrame())
            self.assertEqual(out["action"], "", "第 %d 次还不该弹桌面" % expected)
            self.assertEqual(out["failures"], expected)
        self.clock.advance(5 * MIN)
        out = watcher.tick(FakeFrame())                 # 第 3 次
        self.assertEqual(out["action"], sw.ACTION_BACK_TO_DESKTOP)
        self.assertEqual(self.stats.counter("back_to_desktop"), 1)

    def test_recheck_is_five_minutes(self):
        watcher = self.make_not_study()
        watcher.tick(FakeFrame())
        self.clock.advance(4 * MIN)
        out = watcher.tick(FakeFrame())
        self.assertEqual(out["action"], "")
        self.assertIn("还没到点", out["skipped"])
        self.clock.advance(1 * MIN)
        self.assertEqual(watcher.tick(FakeFrame())["failures"], 1)

    def test_a_study_verdict_resets_the_chain_and_waits_30_minutes(self):
        watcher = self.make_not_study()
        self.add("code", STUDY_VEC)
        watcher.tick(FakeFrame())                       # 不像学习 -> 提醒
        self.clock.advance(5 * MIN)
        self.assertEqual(watcher.tick(FakeFrame())["failures"], 1)
        self.model.vec = list(STUDY_VEC)
        self.clock.advance(5 * MIN)
        out = watcher.tick(FakeFrame())
        self.assertEqual(out["verdict"], sw.VERDICT_STUDY)
        self.assertEqual(out["failures"], 0)
        self.assertAlmostEqual(out["next_in_s"], 30 * MIN, delta=1)
        self.clock.advance(29 * MIN)
        self.assertIn("还没到点", watcher.tick(FakeFrame())["skipped"])

    def test_cooldown_after_the_bounce_keeps_quiet(self):
        watcher = self.make_not_study()
        watcher.tick(FakeFrame())
        for _ in range(3):
            self.clock.advance(5 * MIN)
            watcher.tick(FakeFrame())
        self.clock.advance(1 * MIN)
        out = watcher.tick(FakeFrame())
        self.assertEqual(out["action"], "", "冷却里只看不动")
        self.assertEqual(out["text"], "")
        self.assertIn("冷却", out["note"])
        self.assertGreater(out["cooldown_left_s"], 28 * MIN)
        self.assertEqual(self.stats.counter("reminded"), 1, "冷却里不该再提醒")
        self.assertEqual(self.stats.counter("back_to_desktop"), 1)

    def test_the_cooldown_still_looks_but_takes_no_action(self):
        """冷却管的是**动作**不是**观察** —— 不看就发现不了"可能误判"。"""
        watcher = self.make_not_study()
        watcher.tick(FakeFrame())
        for _ in range(3):
            self.clock.advance(5 * MIN)
            watcher.tick(FakeFrame())
        frames = self.stats.counter("frames")
        self.clock.advance(1 * MIN)
        watcher.tick(FakeFrame())
        self.assertEqual(self.stats.counter("frames"), frames + 1, "冷却里也要看（只看不动）")

    def test_after_the_cooldown_a_fresh_cycle_starts_with_a_reminder(self):
        watcher = self.make_not_study()
        watcher.tick(FakeFrame())
        for _ in range(3):
            self.clock.advance(5 * MIN)
            watcher.tick(FakeFrame())
        self.clock.advance(30 * MIN)
        out = watcher.tick(FakeFrame())
        self.assertEqual(out["action"], sw.ACTION_REMIND)
        self.assertEqual(self.stats.counter("reminded"), 2)

    def test_back_to_desktop_does_not_add_a_second_bubble(self):
        """返回桌面那一下**没有**第二句话（你定的: 同一件事不吵两次）。"""
        watcher = self.make_not_study()
        watcher.tick(FakeFrame())
        for _ in range(3):
            self.clock.advance(5 * MIN)
            out = watcher.tick(FakeFrame())
        self.assertEqual(out["action"], sw.ACTION_BACK_TO_DESKTOP)
        self.assertEqual(out["text"], "")

    def test_only_study_state_is_supervised(self):
        watcher = self.make_not_study()
        out = watcher.tick(FakeFrame(), state="IDLE")
        self.assertIn("只在 STUDY", out["skipped"])
        self.assertEqual(self.stats.counter("frames"), 0)

    def test_dialogue_keyword_skips_the_frame(self):
        watcher = self.make_not_study()
        out = watcher.tick(FakeFrame(), has_keyword=True)
        self.assertIn("对话", out["skipped"])
        self.assertEqual(self.stats.counter("frames"), 0)
        self.assertEqual(out["action"], "")

    def test_skip_on_keyword_can_be_turned_off(self):
        watcher = self.make_not_study(skip_on_keyword=False)
        self.assertEqual(watcher.tick(FakeFrame(), has_keyword=True)["action"],
                         sw.ACTION_REMIND)

    def test_force_bypasses_the_interval(self):
        watcher = self.make_not_study()
        watcher.tick(FakeFrame())
        self.clock.advance(1 * MIN)
        self.assertIn("还没到点", watcher.tick(FakeFrame())["skipped"])
        self.assertEqual(watcher.tick(FakeFrame(), force=True)["failures"], 1)

    def test_per_study_reset_starts_a_clean_cycle(self):
        watcher = self.make_not_study()
        watcher.tick(FakeFrame())
        self.clock.advance(5 * MIN)
        watcher.tick(FakeFrame())
        watcher.reset_cycle()
        snap = watcher.snapshot()
        self.assertEqual(snap["failures"], 0)
        self.assertFalse(snap["reminded"])
        self.assertEqual(snap["cooldown_left_s"], 0.0)
        out = watcher.tick(FakeFrame())                 # 重置后立刻能判
        self.assertEqual(out["action"], sw.ACTION_REMIND)


# ===========================================================================
#  unknown 完全中性
# ===========================================================================
class TestUnknownIsNeutral(WatchCase):
    def make_unknown(self, **kwargs):
        """一个"判不出来"的组合: 库里有锚点, 但这一帧离谁都远, 进程名也没结论。"""
        watcher = self.make_watcher(model=FakeModel(FAR_VEC), **kwargs)
        self.add("code", STUDY_VEC)
        self.add("anime", NOT_STUDY_VEC)
        return watcher

    def test_unknown_takes_no_action_and_touches_no_counter(self):
        watcher = self.make_unknown()
        out = watcher.tick(FakeFrame())
        self.assertEqual(out["verdict"], sw.VERDICT_UNKNOWN)
        self.assertEqual(out["action"], "")
        self.assertEqual(out["text"], "")
        self.assertEqual(out["failures"], 0)
        self.assertEqual(self.stats.counter("reminded"), 0)
        self.assertEqual(self.stats.counter("back_to_desktop"), 0)
        self.assertIn("中性", out["note"])

    def test_unknown_neither_advances_nor_resets_the_chain(self):
        watcher = self.make_unknown()
        self.model.vec = list(NOT_STUDY_VEC)
        watcher.tick(FakeFrame())                       # 不像学习 -> 提醒
        self.clock.advance(5 * MIN)
        self.assertEqual(watcher.tick(FakeFrame())["failures"], 1)
        self.model.vec = list(FAR_VEC)
        self.clock.advance(5 * MIN)
        out = watcher.tick(FakeFrame())
        self.assertEqual(out["verdict"], sw.VERDICT_UNKNOWN)
        self.assertEqual(out["failures"], 1, "unknown 不该把已攒的失败次数抹掉")
        self.model.vec = list(NOT_STUDY_VEC)
        self.clock.advance(5 * MIN)
        self.assertEqual(watcher.tick(FakeFrame())["failures"], 2, "unknown 也不该推进")

    def test_unknown_never_feeds_the_learning(self):
        watcher = self.make_unknown()
        for _ in range(5):
            self.clock.advance(30 * MIN)
            watcher.tick(FakeFrame())
        self.assertIsNone(self.stats.ewma("score_study"))
        self.assertIsNone(self.stats.ewma("score_not_study"))
        self.assertEqual(self.stats.counter("labeled"), 0)
        self.assertEqual(sum(self.stats.histogram()["unknown"]) > 0, True,
                         "unknown 的分数只进 unknown 那份分布")

    def test_missing_frame_is_a_skip_not_an_unknown_verdict(self):
        """没画面时**不要**记成 unknown —— 那会把'判不出来'的比例抬高, 门槛被白白放松。"""
        watcher = self.make_unknown()
        out = watcher.tick(None)
        self.assertIn("没拿到这一帧的向量", out["skipped"])
        self.assertEqual(self.stats.counter("verdict_unknown"), 0)
        self.assertEqual(self.stats.counter("frames"), 0)
        self.assertEqual(sum(self.stats.histogram()["unknown"]), 0)

    def test_encoder_failure_is_a_skip_too(self):
        watcher = self.make_unknown()
        watcher._encoder = FakeModel(fail=True)
        out = watcher.tick(FakeFrame())
        self.assertIn("编码失败", out["skipped"])
        self.assertEqual(self.stats.counter("verdicts"), 0)

    def test_an_all_black_frame_is_a_skip_not_a_verdict(self):
        """T13-6 验收实测: 串流刚起来会给一帧全黑图, 它会被判成"学习"（相对分 +0.068）。

        那不是内容, 是"没看到" —— 所以当跳过（既不推进升级链, 也不污染统计）。
        """
        watcher = self.make_watcher(model=FakeModel(STUDY_VEC))
        self.seed_both()
        out = watcher.tick(ArrayLike(0))                    # 全黑（max=0）
        self.assertIn("全是黑的", out["skipped"])
        self.assertEqual(self.stats.counter("frames"), 0)
        self.assertEqual(self.stats.counter("reminded"), 0)

    def test_a_dark_but_real_frame_is_still_judged(self):
        """深色主题/暗画面的**真实内容**不该被当成黑帧（阈值取得很窄: ≤ 8）。"""
        watcher = self.make_watcher(model=FakeModel(STUDY_VEC))
        self.seed_both()
        out = watcher.tick(ArrayLike(200))                  # 暗但有内容
        self.assertEqual(out["skipped"], "")
        self.assertEqual(self.stats.counter("frames"), 1)

    def test_the_decision_carries_the_relative_score(self):
        """⚠ T13-6 验收抓到的漏项: `tick()` 的返回里原来**没有** `relative` ——
        循环那行日志（"相对分 %s"）一直在打 None。"""
        watcher = self.make_watcher(model=FakeModel(STUDY_VEC))
        self.seed_both()
        out = watcher.tick(FakeFrame())
        self.assertAlmostEqual(out["relative"], 1.0, places=3)
        self.assertEqual(out["band"], 0.05)
        self.assertEqual(out["picture"], sw.VERDICT_STUDY)
        self.assertAlmostEqual(out["study_cos"], 1.0, places=3)


# ===========================================================================
#  学锚点
# ===========================================================================
class TestLearning(WatchCase):
    def test_cold_start_learns_from_the_process_name(self):
        watcher = self.make_watcher(model=FakeModel(STUDY_VEC),
                                    probe=FakeProbe({"code": ["pycharm64"]}))
        out = watcher.tick(FakeFrame())
        self.assertEqual(out["verdict"], sw.VERDICT_STUDY)
        self.assertTrue(out["learned"])
        self.assertEqual(self.anchors.counts(), {"code": 1})
        self.assertEqual(self.stats.counter("learned"), 1)

    def test_a_wrong_picture_gets_corrected_by_the_label(self):
        watcher = self.make_watcher(model=FakeModel(NOT_STUDY_VEC),
                                    probe=FakeProbe({"code": ["pycharm64"]}))
        self.add("doc", STUDY_VEC)                      # 库里有学习类, 但这一帧不像
        out = watcher.tick(FakeFrame())
        self.assertEqual(out["source"], "process")
        self.assertTrue(out["learned"])
        self.assertEqual(sorted(self.anchors.counts()), ["code", "doc"])

    def test_agreement_does_not_grow_the_library(self):
        watcher = self.make_watcher(model=FakeModel(STUDY_VEC),
                                    probe=FakeProbe({"code": ["pycharm64"]}))
        self.seed_both()                                    # 画面有把握 + 进程名一致
        out = watcher.tick(FakeFrame())
        self.assertEqual(out["source"], "anchor")
        self.assertFalse(out["learned"])
        self.assertEqual(self.anchors.counts(), {"anime": 1, "code": 1})

    def test_conflict_learns_nothing(self):
        """冲突 = unknown = 完全中性: 连锚点也不学（两边都不信）。"""
        watcher = self.make_watcher(model=FakeModel(STUDY_VEC),
                                    probe=FakeProbe({"game": ["steam"]}))
        self.seed_both()                                    # 画面说学习, 进程名说游戏
        out = watcher.tick(FakeFrame())
        self.assertEqual(out["verdict"], sw.VERDICT_UNKNOWN)
        self.assertFalse(out["learned"])
        self.assertEqual(self.anchors.counts(), {"anime": 1, "code": 1})

    def test_learning_respects_the_per_class_cap(self):
        watcher = self.make_watcher(model=FakeModel(NOT_STUDY_VEC),
                                    probe=FakeProbe({"code": ["pycharm64"]}),
                                    anchors=self.make_anchors(max_per_class=2))
        self.add("code", STUDY_VEC)                     # 已有 1 条
        for _ in range(3):
            self.clock.advance(30 * MIN)
            watcher.tick(FakeFrame())
        self.assertEqual(self.anchors.counts()["code"], 2)

    def test_learn_can_be_switched_off(self):
        watcher = self.make_watcher(model=FakeModel(STUDY_VEC),
                                    probe=FakeProbe({"code": ["pycharm64"]}),
                                    learn=False)
        out = watcher.tick(FakeFrame())
        self.assertFalse(out["learned"])
        self.assertEqual(len(self.anchors), 0)

    def test_manual_label_adds_an_anchor_and_clears_the_pause(self):
        watcher = self.make_watcher(model=FakeModel(STUDY_VEC))
        watcher._paused["code"] = self.clock.now + 10 * MIN
        out = watcher.label("code", frame=FakeFrame())
        self.assertTrue(out["learned"])
        self.assertEqual(out["category"], "study")
        self.assertEqual(self.anchors.counts(), {"code": 1})
        self.assertEqual(watcher.paused_classes(), {})

    def test_manual_label_refuses_an_unknown_class(self):
        watcher = self.make_watcher(model=FakeModel(STUDY_VEC))
        with self.assertRaises(sw.StudyWatchError):
            watcher.label("mystery", frame=FakeFrame())

    def test_manual_label_without_a_vector_says_so(self):
        watcher = self.make_watcher(model=FakeModel(fail=True))
        out = watcher.label("code", frame=FakeFrame())
        self.assertFalse(out["learned"])
        self.assertIn("没拿到向量", out["note"])


# ===========================================================================
#  可能误判 -> 停学
# ===========================================================================
class TestMisjudgement(WatchCase):
    def bounce(self, watcher):
        """走完一整条升级链, 拿到那次 back_to_desktop。"""
        self.model.vec = list(NOT_STUDY_VEC)
        watcher.tick(FakeFrame())
        for _ in range(3):
            self.clock.advance(5 * MIN)
            out = watcher.tick(FakeFrame())
        return out

    def make_watcher_with_both(self, **kwargs):
        watcher = self.make_watcher(model=FakeModel(NOT_STUDY_VEC), **kwargs)
        self.add("code", STUDY_VEC)
        self.add("anime", NOT_STUDY_VEC)
        return watcher

    def test_reappearing_as_study_is_flagged_as_a_misjudgement(self):
        watcher = self.make_watcher_with_both()
        out = self.bounce(watcher)
        self.assertEqual(out["action"], sw.ACTION_BACK_TO_DESKTOP)
        self.clock.advance(2 * MIN)
        self.model.vec = list(STUDY_VEC)
        out = watcher.tick(FakeFrame())
        self.assertEqual(out["verdict"], sw.VERDICT_STUDY)
        self.assertIn("可能误判", out["note"])
        self.assertEqual(self.stats.counter("misjudged"), 1)

    def test_the_blamed_class_stops_learning_for_a_while(self):
        """弹回桌面后 5 分钟内又判成学习 -> 那个类**停学**（别把一次误判学成锚点）。"""
        probe = FakeProbe({"anime": ["potplayer"]})      # 弹的时候进程名跟画面一致
        watcher = self.make_watcher_with_both(probe=probe)
        self.bounce(watcher)
        self.clock.advance(2 * MIN)
        probe.games = {"code": ["pycharm64"]}            # 人家回到 PyCharm 了
        self.model.vec = list(STUDY_VEC)
        out = watcher.tick(FakeFrame())
        self.assertEqual(out["verdict"], sw.VERDICT_STUDY)
        self.assertIn("可能误判", out["note"])
        self.assertIn("code", watcher.paused_classes())

        before = self.anchors.counts()["code"]
        self.model.vec = list(FAR_VEC)                   # 画面判错了, 进程名给了标签
        watcher.tick(FakeFrame(), force=True)
        self.assertEqual(self.anchors.counts()["code"], before,
                         "停学期内不该再学这个类")

    def test_the_pause_expires(self):
        probe = FakeProbe({"anime": ["potplayer"]})
        watcher = self.make_watcher_with_both(probe=probe)
        self.bounce(watcher)
        self.clock.advance(2 * MIN)
        probe.games = {"code": ["pycharm64"]}
        self.model.vec = list(STUDY_VEC)
        watcher.tick(FakeFrame())
        self.assertIn("code", watcher.paused_classes())
        self.clock.advance(31 * MIN)
        self.assertEqual(watcher.paused_classes(), {})
        before = self.anchors.counts().get("code", 0)
        self.model.vec = list(FAR_VEC)
        watcher.tick(FakeFrame(), force=True)
        self.assertGreater(self.anchors.counts().get("code", 0), before)

    def test_a_study_verdict_long_after_the_bounce_is_not_a_misjudgement(self):
        watcher = self.make_watcher_with_both()
        self.bounce(watcher)
        self.clock.advance(30 * MIN)                    # 冷却过了
        self.model.vec = list(STUDY_VEC)
        out = watcher.tick(FakeFrame())
        self.assertEqual(out["verdict"], sw.VERDICT_STUDY)
        self.assertNotIn("可能误判", out["note"])


# ===========================================================================
#  没把握带的自适应（护栏）
# ===========================================================================
class TestAdaptation(WatchCase):
    def feed(self, watcher, *, label, relative, times):
        """喂带标签样本: 相对分 `relative`（正 = 像学习）。"""
        for _ in range(times):
            watcher.learn_score(relative, label=label, margin=0.10)

    def test_nothing_moves_before_both_classes_have_enough_samples(self):
        watcher = self.make_watcher(with_stats=True, min_labeled=20)
        self.feed(watcher, label="study", relative=0.20, times=19)
        self.feed(watcher, label="not_study", relative=-0.20, times=19)
        self.assertFalse(watcher.adapt()["moved"])
        self.assertAlmostEqual(watcher.relative_band, 0.05, places=6)

    def test_it_moves_toward_the_overlap_midpoint_one_step_at_a_time(self):
        watcher = self.make_watcher(min_labeled=20)
        self.feed(watcher, label="study", relative=0.10, times=20)
        self.feed(watcher, label="not_study", relative=-0.10, times=20)
        # 两类的中点 = 0 -> 带该往**窄**里挪一步（起点 0.05 -> 0.04）
        self.assertTrue(watcher.adapt()["moved"])
        self.assertAlmostEqual(watcher.relative_band, 0.04, places=6)

    def test_a_wider_overlap_widens_the_band(self):
        """两类重叠得多（study 只 +0.10、not_study 也 +0.10）-> 带该放宽。"""
        watcher = self.make_watcher(min_labeled=20)
        self.feed(watcher, label="study", relative=0.20, times=20)
        self.feed(watcher, label="not_study", relative=0.10, times=20)
        watcher.adapt()
        self.assertAlmostEqual(watcher.relative_band, 0.06, places=6)

    def test_reasons_are_written_down(self):
        watcher = self.make_watcher(min_labeled=20)
        self.feed(watcher, label="study", relative=0.10, times=20)
        self.feed(watcher, label="not_study", relative=-0.10, times=20)
        watcher.adapt()
        notes = [item["text"] for item in self.stats.notes()]
        self.assertTrue(any("没把握带从 0.050 挪到 0.040" in text for text in notes), notes)
        self.assertTrue(any("一次只动 0.01" in text for text in notes), notes)

    def test_the_bound_is_respected(self):
        watcher = self.make_watcher(min_labeled=20, band_bounds=(0.03, 0.20))
        self.feed(watcher, label="study", relative=0.0, times=20)
        self.feed(watcher, label="not_study", relative=0.0, times=20)
        for _ in range(5):
            watcher.adapt()
        self.assertAlmostEqual(watcher.relative_band, 0.03, places=6)

    def test_a_high_unknown_rate_narrows_the_band(self):
        """判不出来的比例超过目标 -> 带太宽, 收窄 0.01。"""
        watcher = self.make_watcher(model=FakeModel(FAR_VEC), min_labeled=2,
                                    target_unknown_rate=0.3)
        self.seed_both()
        for _ in range(4):
            self.clock.advance(31 * MIN)
            watcher.tick(FakeFrame())
        self.assertLess(watcher.relative_band, 0.05)
        self.assertGreaterEqual(watcher.relative_band, 0.01)
        self.assertTrue(any("判不出来的比例太高" in item["text"]
                            for item in self.stats.notes()))

    def test_narrowing_stops_at_the_bound(self):
        watcher = self.make_watcher(model=FakeModel(FAR_VEC), min_labeled=2,
                                    target_unknown_rate=0.0,
                                    band_bounds=(0.03, 0.20))
        self.seed_both()
        for _ in range(6):
            self.clock.advance(31 * MIN)
            watcher.tick(FakeFrame())
        self.assertAlmostEqual(watcher.relative_band, 0.03, places=6)

    def test_a_reversed_label_widens_the_band(self):
        """带标签样本里出现"画面判反了" -> 宁可判不出来, 把带放宽一步。

        这里走的是**真路径**: 画面说 study、进程名说 not_study（冲突 -> unknown）——
        冲突虽然不动作, 但那条样本记着"真值 not_study / 画面 study", 正是判反。
        """
        watcher = self.make_watcher(model=FakeModel(STUDY_VEC), min_labeled=2,
                                    probe=FakeProbe({"anime": ["potplayer"]}))
        self.seed_both()
        self.clock.advance(MIN)
        out = watcher.tick(FakeFrame())
        self.assertEqual(out["verdict"], sw.VERDICT_UNKNOWN)        # 冲突 -> 中性
        sample = self.stats.last_sample()
        self.assertEqual(sample["labeled"], sw.VERDICT_NOT_STUDY)   # 真值（大类）
        self.assertEqual(sample["picture"], sw.VERDICT_STUDY)       # 画面判反了
        watcher.adapt()
        self.assertGreater(watcher.relative_band, 0.05)
        self.assertTrue(any("反向错误" in item["text"] for item in self.stats.notes()))

    def test_freeze_stops_everything_and_unfreeze_resumes(self):
        watcher = self.make_watcher(min_labeled=20)
        self.feed(watcher, label="study", relative=0.10, times=20)
        self.feed(watcher, label="not_study", relative=-0.10, times=20)
        watcher.freeze()
        self.assertFalse(watcher.adapt()["moved"])
        self.assertAlmostEqual(watcher.relative_band, 0.05, places=6)
        watcher.unfreeze()
        self.assertTrue(watcher.adapt()["moved"])

    def test_reset_learning_goes_back_to_the_configured_starting_point(self):
        watcher = self.make_watcher(min_labeled=20)
        self.feed(watcher, label="study", relative=0.10, times=20)
        self.feed(watcher, label="not_study", relative=-0.10, times=20)
        watcher.adapt()
        self.assertAlmostEqual(watcher.relative_band, 0.04, places=6)
        watcher.reset_learning()
        self.assertAlmostEqual(watcher.relative_band, 0.05, places=6)
        self.assertIsNone(self.stats.ewma("relative_study"))

    def test_the_band_and_ewma_survive_a_restart(self):
        watcher = self.make_watcher(min_labeled=20)
        self.feed(watcher, label="study", relative=0.10, times=20)
        self.feed(watcher, label="not_study", relative=-0.10, times=20)
        watcher.adapt()
        watcher.stats.save()

        restarted = self.make_stats()
        restarted.load()
        again = self.make_watcher(stats=restarted)
        self.assertAlmostEqual(again.relative_band, 0.04, places=6)
        self.assertIsNotNone(restarted.ewma("relative_study"))

    def test_unknown_labels_are_refused(self):
        watcher = self.make_watcher()
        out = watcher.learn_score(0.5, label="unknown")
        self.assertFalse(out["learned"])
        self.assertIsNone(self.stats.ewma("relative_unknown"))

    def test_without_stats_everything_still_works(self):
        watcher = self.make_watcher(with_stats=False, model=FakeModel(NOT_STUDY_VEC))
        self.seed_both()
        out = watcher.tick(FakeFrame())
        self.assertEqual(out["action"], sw.ACTION_REMIND)
        self.assertFalse(watcher.adapt()["moved"])
        self.assertAlmostEqual(watcher.relative_band, 0.05, places=6)

    def test_a_stats_file_that_cannot_be_written_does_not_break_supervision(self):
        stats = StudyStats(self.tmp)                    # 目录当文件 -> 写不下去
        watcher = self.make_watcher(stats=stats, model=FakeModel(NOT_STUDY_VEC))
        self.seed_both()
        out = watcher.tick(FakeFrame())
        self.assertEqual(out["action"], sw.ACTION_REMIND)
        self.assertTrue(any("统计没写下去" in note for note in watcher.notes()))

    def test_bounds_that_are_written_backwards_are_refused(self):
        with self.assertRaises(sw.StudyWatchError):
            self.make_watcher(band_bounds=(0.20, 0.01))


# ===========================================================================
#  统计与状态
# ===========================================================================
class TestStats(WatchCase):
    def test_counters_and_samples_are_recorded(self):
        watcher = self.make_watcher(model=FakeModel(NOT_STUDY_VEC))
        self.seed_both()
        watcher.tick(FakeFrame())
        self.assertEqual(self.stats.counter("frames"), 1)
        self.assertEqual(self.stats.counter("verdicts"), 1)
        self.assertEqual(self.stats.counter("verdict_not_study"), 1)
        self.assertEqual(self.stats.counter("source_anchor"), 1)
        sample = self.stats.last_sample()
        self.assertEqual(sample["action"], sw.ACTION_REMIND)
        self.assertEqual(sample["cls"], "anime")
        self.assertAlmostEqual(sample["relative"], -1.0, places=3)
        self.assertEqual(sample["picture"], sw.VERDICT_NOT_STUDY)

    def test_process_labels_feed_the_distribution(self):
        watcher = self.make_watcher(model=FakeModel(FAR_VEC),
                                    probe=FakeProbe({"code": ["pycharm64"]}))
        self.seed_both()                                    # 相对分算得出来才算"带标签样本"
        watcher.tick(FakeFrame())
        self.assertEqual(self.stats.counter("labeled_study"), 1)
        self.assertEqual(sum(self.stats.histogram()["study"]), 2,
                         "相对分与绝对余量各记一份分布")

    def test_snapshot_carries_what_status_needs(self):
        watcher = self.make_watcher(model=FakeModel(STUDY_VEC),
                                    probe=FakeProbe({"code": ["pycharm64"]}))
        self.seed_both()
        watcher.tick(FakeFrame())
        snap = watcher.snapshot()
        self.assertEqual(snap["anchors"], 2)
        self.assertEqual(snap["counts"], {"anime": 1, "code": 1})
        self.assertEqual(snap["categories"], {"study": 1, "not_study": 1})
        self.assertEqual(snap["thresholds"]["relative_band"], 0.05)
        self.assertEqual(snap["thresholds"]["band_bounds"], [0.01, 0.20])
        self.assertEqual(snap["intervals_min"], {"focus": 30.0, "recheck": 5.0, "cooldown": 30.0,
                                                 "cooldown_probe": 1.0})
        self.assertEqual(snap["max_failures"], 3)
        self.assertTrue(snap["adapt"])
        self.assertEqual(snap["last"]["verdict"], sw.VERDICT_STUDY)

    def test_notes_can_be_drained(self):
        watcher = self.make_watcher(with_stats=False)
        watcher.freeze()
        self.assertTrue(watcher.notes())
        self.assertEqual(watcher.notes(), [])

    def test_the_bubble_text_is_exactly_the_agreed_sentence(self):
        self.assertEqual(sw.REMIND_TEXT, "现在是学习时间")

    def test_default_process_map_covers_what_we_agreed(self):
        mapping = sw.DEFAULT_PROCESS_MAP
        for name in ("pycharm64", "windowsterminal", "obsidian", "zotero",
                     "steam", "yuanshen", "potplayer"):
            self.assertIn(name, mapping)
        for browser in ("chrome", "msedge", "firefox", "mpv"):
            self.assertNotIn(browser, mapping, "%s 故意不映射（猜错就是一次误提醒）" % browser)


if __name__ == "__main__":
    unittest.main(verbosity=2)
