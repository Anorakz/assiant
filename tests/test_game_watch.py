#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`game_anchors.py` + `game_watch.py` + `pc_probe.py` 的单测 —— **全离线**。

注入的替身: 假的 SigLIP 编码器（想给什么向量就给什么）、假的 PC 探针（想说什么在跑就说）、
假的 meminfo / clock，锚点库与截图写到临时目录（不碰仓库里那份）。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.core.game_anchors import GameAnchors, cosine               # noqa: E402
from agent.core.game_watch import GameWatcher                        # noqa: E402
from agent.net.pc_probe import PcProbe, PcProbeError, normalize_process   # noqa: E402

DIM = 768        # 与 SigLIP 图像向量一致（锚点库复用 wall_data 的 base64(float16) 编解码）


def vector(*values):
    """造一个 768 维向量（前面按给定值, 其余补 0）。"""
    out = [float(v) for v in values][:DIM]
    return out + [0.0] * (DIM - len(out))


class FakeFrame(object):
    """假帧: 只要能被 `tobytes()` 存成截图就行（真帧是 numpy 数组）。"""

    def __init__(self, payload=b"fake-jpeg-bytes"):
        self.payload = payload

    def tobytes(self):
        return self.payload


class FakeModel(object):
    """假 SigLIP: `encode_image(frame)` 回一个固定向量。"""

    def __init__(self, vec, fail=False):
        self.vec = list(vec)
        self.fail = fail
        self.loaded = 0
        self.closed = 0
        self.seen = 0

    def load(self):
        self.loaded += 1
        return self

    def close(self):
        self.closed += 1

    def encode_image(self, frame):
        self.seen += 1
        if self.fail:
            raise RuntimeError("编码炸了")
        return list(self.vec)


class FakeProbe(object):
    def __init__(self, games=None, why="", ok=True):
        self._games = list(games or [])
        self._why = why
        self._ok = ok
        self.calls = 0

    def snapshot(self):
        self.calls += 1
        return {"ok": self._ok, "games": {name: ["x.exe"] for name in self._games},
                "why": self._why}


class AnchorCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="game-anchors-")
        self.path = os.path.join(self.dir, "game_anchors.jsonl")
        self.shots = os.path.join(self.dir, "shots")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def anchors(self, **kwargs):
        kwargs.setdefault("keep_shots", True)
        return GameAnchors(self.path, shot_dir=self.shots, **kwargs)

    def watcher(self, model_vec, *, probe=None, learn=True, score=0.82, margin=0.05,
                mem_mb=3000.0, interval=60.0, now=None):
        anchors = self.anchors()
        anchors.load()
        model = FakeModel(model_vec)
        watcher = GameWatcher(anchors, probe=probe, encoder_factory=lambda: model,
                              learn=learn, confident_score=score, confident_margin=margin,
                              meminfo=lambda: {"MemAvailable": mem_mb},
                              interval_s=interval, clock=(now or (lambda: 1000.0)))
        return watcher, anchors, model


class TestCosineAndAnchors(AnchorCase):

    def test_cosine(self):
        self.assertAlmostEqual(cosine([1.0, 0.0], [1.0, 0.0]), 1.0)
        self.assertAlmostEqual(cosine([1.0, 0.0], [0.0, 1.0]), 0.0)
        self.assertEqual(cosine([1.0], [1.0, 2.0]), 0.0)      # 维度不一样 = 不相似
        self.assertEqual(cosine([], [1.0]), 0.0)
        self.assertEqual(cosine([0.0, 0.0], [1.0, 1.0]), 0.0)

    def test_missing_file_is_an_empty_library(self):
        anchors = self.anchors()
        self.assertEqual(anchors.load(), 0)
        self.assertEqual(anchors.games(), [])
        self.assertIsNone(anchors.match(vector(1)))

    def test_add_writes_a_line_and_saves_the_shot(self):
        anchors = self.anchors()
        row = anchors.add(game="初雪樱", vector=vector(1, 1), shot=b"jpeg-bytes", note="进程说是它")
        self.assertEqual(row["game"], "初雪樱")
        self.assertTrue(row["shot"].endswith(".jpg"))
        # 索引里记的是"去哪儿找这张图"（仓库相对路径; 跨盘时如实记绝对路径）
        saved = row["shot"]
        if not os.path.isabs(saved):
            saved = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))), saved)
        self.assertTrue(os.path.exists(saved), saved)
        with open(self.path, encoding="utf-8") as handle:
            lines = [line for line in handle if line.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["game"], "初雪樱")

    def test_add_without_shot_still_records_the_vector(self):
        anchors = self.anchors()
        row = anchors.add(game="hoi4", vector=vector(1))
        self.assertEqual(row["shot"], "")
        self.assertEqual(len(anchors), 1)

    def test_roundtrip_and_match(self):
        anchors = self.anchors()
        anchors.add(game="hoi4", vector=vector(1, 0, 0))
        anchors.add(game="stellaris", vector=vector(0, 1, 0))
        again = self.anchors()
        again.load()
        self.assertEqual(sorted(again.games()), ["hoi4", "stellaris"])
        hit = again.match(vector(1, 0.1, 0))
        self.assertEqual(hit["game"], "hoi4")
        self.assertGreater(hit["score"], 0.9)
        self.assertLess(hit["margin"], 1.0)
        self.assertEqual(hit["anchors"], 1)

    def test_match_takes_the_best_anchor_of_each_game(self):
        anchors = self.anchors()
        anchors.add(game="hoi4", vector=vector(1, 0))
        anchors.add(game="hoi4", vector=vector(0, 1))         # 同游戏的第二条不构成"对手"
        hit = anchors.match(vector(0, 1))
        self.assertEqual(hit["game"], "hoi4")
        self.assertAlmostEqual(hit["margin"], 1.0, places=6)  # 没有别的游戏 -> runner_up = 0
        self.assertEqual(hit["anchors"], 2)

    def test_broken_lines_are_skipped_not_fatal(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("不是 JSON\n")
            handle.write(json.dumps({"game": "hoi4", "vector": "坏的base64!!"}) + "\n")
        anchors = self.anchors()
        anchors.load()
        self.assertEqual(len(anchors), 1)                     # 行还在
        self.assertEqual(anchors.vectors(), [])               # 但解不出来 -> 不进比对


class TestIdentify(AnchorCase):

    def test_process_only_bootstraps_an_anchor(self):
        watcher, anchors, _model = self.watcher(vector(1, 0), probe=FakeProbe(["hoi4"]))
        watcher.ensure_model("GAME")
        out = watcher.identify(FakeFrame())
        self.assertEqual(out["game"], "hoi4")
        self.assertEqual(out["source"], "process")
        self.assertTrue(out["learned"])
        self.assertEqual(len(anchors), 1)                     # 学了一个锚点（起步）

    def test_confident_anchor_agrees_with_process(self):
        watcher, anchors, _model = self.watcher(vector(1, 0), probe=FakeProbe(["hoi4"]))
        anchors.add(game="hoi4", vector=vector(1, 0))
        watcher.ensure_model("GAME")
        out = watcher.identify(FakeFrame())
        self.assertEqual(out["source"], "anchor")
        self.assertEqual(out["game"], "hoi4")
        self.assertFalse(out["learned"])                      # 一致就不用再学

    def test_anchor_wins_when_pc_has_nothing_recognised(self):
        watcher, anchors, _model = self.watcher(vector(1, 0), probe=FakeProbe([]))
        anchors.add(game="hoi4", vector=vector(1, 0))
        watcher.ensure_model("GAME")
        out = watcher.identify(FakeFrame())
        self.assertEqual(out["game"], "hoi4")
        self.assertEqual(out["source"], "anchor")
        self.assertIn("只有画面", out["note"])

    def test_mismatch_trusts_the_process_and_learns(self):
        """你定的纠错: 截图与进程内容不匹配时, 以进程为准并把这一帧更新成锚点。"""
        watcher, anchors, _model = self.watcher(vector(1, 0), probe=FakeProbe(["stellaris"]))
        anchors.add(game="hoi4", vector=vector(1, 0))         # 画面会说是 hoi4（0.99）
        watcher.ensure_model("GAME")
        out = watcher.identify(FakeFrame())
        self.assertEqual(out["game"], "stellaris")            # 进程说了算
        self.assertEqual(out["source"], "corrected")
        self.assertTrue(out["learned"])
        self.assertIn("以进程为准", out["note"])
        self.assertEqual(sorted(anchors.games()), ["hoi4", "stellaris"])

    def test_low_confidence_goes_to_the_process(self):
        watcher, anchors, _model = self.watcher(vector(0.6, 0.8), probe=FakeProbe(["hoi4"]))
        anchors.add(game="初雪樱", vector=vector(1, 0))       # 不算特别像
        watcher.ensure_model("GAME")
        out = watcher.identify(FakeFrame())
        self.assertEqual(out["game"], "hoi4")
        self.assertEqual(out["source"], "process")
        self.assertIn("没把握", out["note"])

    def test_narrow_margin_goes_to_the_process(self):
        # 两个游戏都很像 -> margin ≈ 0 -> 不采信画面
        watcher, anchors, _model = self.watcher(vector(1, 1), probe=FakeProbe(["hoi4"]))
        anchors.add(game="初雪樱", vector=vector(1, 0))
        anchors.add(game="甜蜜女友3", vector=vector(0, 1))
        watcher.ensure_model("GAME")
        out = watcher.identify(FakeFrame())
        self.assertEqual(out["game"], "hoi4")
        self.assertEqual(out["source"], "process")
        self.assertLess(out["margin"], 0.05)

    def test_nothing_recognised_does_nothing(self):
        watcher, anchors, _model = self.watcher(vector(1, 0), probe=FakeProbe([]))
        watcher.ensure_model("GAME")
        out = watcher.identify(FakeFrame())
        self.assertEqual(out["game"], "")
        self.assertFalse(out["learned"])
        self.assertEqual(len(anchors), 0)                     # 认不出就**不写锚点**
        self.assertIn("认不出", out["note"])

    def test_pc_unreachable_is_reported_not_guessed(self):
        watcher, anchors, _model = self.watcher(vector(1, 0),
                                                probe=FakeProbe([], ok=False, why="连不上 PC"))
        watcher.ensure_model("GAME")
        out = watcher.identify(FakeFrame())
        self.assertEqual(out["game"], "")
        self.assertIn("认不出", out["note"])

    def test_learn_can_be_switched_off(self):
        watcher, anchors, _model = self.watcher(vector(1, 0), probe=FakeProbe(["hoi4"]),
                                                learn=False)
        watcher.ensure_model("GAME")
        watcher.identify(FakeFrame())
        self.assertEqual(len(anchors), 0)

    def test_encoder_failure_is_not_fatal(self):
        watcher, anchors, model = self.watcher(vector(1, 0), probe=FakeProbe(["hoi4"]))
        model.fail = True
        watcher.ensure_model("GAME")
        out = watcher.identify(FakeFrame())
        self.assertEqual(out["game"], "hoi4")                 # 画面那条挂了, 进程那条还在
        self.assertEqual(out["source"], "process")


class TestTick(AnchorCase):

    def test_keyword_means_no_capture_at_all(self):
        watcher, _anchors, model = self.watcher(vector(1, 0), probe=FakeProbe(["hoi4"]))
        watcher.ensure_model("GAME")
        out = watcher.tick(FakeFrame(), state="GAME", has_keyword=True)
        self.assertIn("对话", out["skipped"])
        self.assertEqual(model.seen, 0)                       # **一帧都没编码**
        self.assertEqual(out["game"], "")

    def test_no_frame_skips(self):
        watcher, _anchors, _model = self.watcher(vector(1, 0), probe=FakeProbe(["hoi4"]))
        watcher.ensure_model("GAME")
        out = watcher.tick(None, state="GAME")
        self.assertIn("没拿到画面", out["skipped"])

    def test_interval_gating(self):
        now = [1000.0]
        watcher, _anchors, model = self.watcher(vector(1, 0), probe=FakeProbe(["hoi4"]),
                                                interval=60.0, now=lambda: now[0])
        watcher.ensure_model("GAME")
        self.assertEqual(watcher.tick(FakeFrame(), state="GAME")["game"], "hoi4")
        again = watcher.tick(FakeFrame(), state="GAME")
        self.assertIn("还没到间隔", again["skipped"])
        self.assertEqual(model.seen, 1)
        now[0] += 61.0
        self.assertEqual(watcher.tick(FakeFrame(), state="GAME")["game"], "hoi4")
        self.assertEqual(model.seen, 2)
        now[0] += 1.0
        self.assertEqual(watcher.tick(FakeFrame(), state="GAME", force=True)["game"], "hoi4")

    def test_model_residency_follows_the_state(self):
        watcher, _anchors, model = self.watcher(vector(1, 0), probe=FakeProbe(["hoi4"]))
        self.assertTrue(watcher.ensure_model("STUDY"))
        self.assertTrue(watcher.model_loaded)
        self.assertTrue(watcher.ensure_model("GAME"))
        self.assertTrue(watcher.model_loaded)
        self.assertEqual(model.loaded, 1)                     # 已经在内存里就不重复加载
        self.assertFalse(watcher.ensure_model("IDLE"))
        self.assertFalse(watcher.model_loaded)
        self.assertEqual(model.closed, 1)
        self.assertTrue(any("卸载" in note for note in watcher.notes()))

    def test_low_memory_refuses_to_load(self):
        watcher, _anchors, model = self.watcher(vector(1, 0), probe=FakeProbe(["hoi4"]),
                                                mem_mb=120.0)
        self.assertFalse(watcher.ensure_model("GAME"))
        self.assertEqual(model.loaded, 0)
        self.assertTrue(any("内存不够" in note for note in watcher.notes()))
        self.assertEqual(watcher.snapshot()["model"], "skipped-low-memory")

    def test_tick_without_model_is_honest(self):
        watcher, _anchors, _model = self.watcher(vector(1, 0), probe=FakeProbe(["hoi4"]),
                                                 mem_mb=50.0)
        out = watcher.tick(FakeFrame(), state="GAME")
        self.assertIn("模型不在内存里", out["skipped"])
        self.assertIn("内存不够", out["note"])

    def test_snapshot_and_last_game(self):
        watcher, anchors, _model = self.watcher(vector(1, 0), probe=FakeProbe(["hoi4"]))
        watcher.ensure_model("GAME")
        watcher.tick(FakeFrame(), state="GAME")
        snap = watcher.snapshot()
        self.assertEqual(snap["last_game"], "hoi4")
        self.assertTrue(snap["loaded"])
        self.assertEqual(snap["resident_states"], ["STUDY", "GAME"])
        self.assertEqual(snap["confident"], {"score": 0.82, "margin": 0.05})


class TestPcProbe(unittest.TestCase):

    def test_normalize_process(self):
        self.assertEqual(normalize_process("Hatsuyuki.exe"), "hatsuyuki")
        self.assertEqual(normalize_process("  Amakano3  "), "amakano3")
        self.assertEqual(normalize_process(None), "")

    def probe(self, output, code=0, error="", mapping=None):
        def runner(argv, timeout):
            if error:
                raise RuntimeError(error)
            return (code, output, "")

        return PcProbe(mapping=mapping or {"hatsuyuki": "初雪樱", "Amakano3": "甜蜜女友3"},
                       host="192.0.2.1", user="u", runner=runner)

    def test_names_are_read_and_normalised(self):
        probe = self.probe("explorer\nchrome\nHatsuyuki.exe\nHatsuyuki\n")
        self.assertEqual(probe.names(), ["explorer", "chrome", "hatsuyuki"])

    def test_mapping_turns_processes_into_games(self):
        probe = self.probe("Hatsuyuki.exe\nchrome\nAmakano3\n")
        self.assertEqual(probe.games(), {"初雪樱": ["hatsuyuki"], "甜蜜女友3": ["amakano3"]})
        self.assertEqual(probe.snapshot()["ok"], True)

    def test_unknown_processes_are_not_guessed(self):
        probe = self.probe("chrome\nexplorer\n")
        self.assertEqual(probe.games(), {})

    def test_multiple_games_are_flagged(self):
        probe = self.probe("hatsuyuki\namakano3\n")
        snapshot = probe.snapshot()
        self.assertEqual(sorted(snapshot["games"]), ["初雪樱", "甜蜜女友3"])
        self.assertIn("多个", snapshot["why"])

    def test_missing_host_is_honest(self):
        probe = PcProbe(runner=lambda argv, timeout: (0, "", ""))
        with self.assertRaises(PcProbeError) as caught:
            probe.names()
        self.assertIn("pc_host", str(caught.exception))

    def test_ssh_failure_is_honest(self):
        probe = self.probe("", error="ssh 超时")
        with self.assertRaises(PcProbeError) as caught:
            probe.names()
        self.assertIn("问不了 PC", str(caught.exception))
        self.assertFalse(probe.snapshot()["ok"])

    def test_command_failure_carries_the_reason(self):
        probe = self.probe("", code=1)
        with self.assertRaises(PcProbeError) as caught:
            probe.names()
        self.assertIn("退出码", str(caught.exception))

    def test_empty_output_is_honest(self):
        probe = self.probe("\n\n")
        with self.assertRaises(PcProbeError) as caught:
            probe.names()
        self.assertIn("没回", str(caught.exception))

    def test_from_config_uses_the_music_pc_settings(self):
        probe = PcProbe.from_config({"process_names": {"hoi4": "hoi4"}},
                                    music={"pc_host": "10.0.0.5", "pc_user": "me", "pc_port": 2222},
                                    runner=lambda argv, timeout: (0, "hoi4\n", ""))
        self.assertEqual(probe.host, "10.0.0.5")
        self.assertEqual(probe.port, 2222)
        self.assertEqual(probe.games(), {"hoi4": ["hoi4"]})
        argv = probe._ssh_argv(probe.command)
        self.assertIn("BatchMode=yes", argv)


if __name__ == "__main__":
    unittest.main(verbosity=2)
