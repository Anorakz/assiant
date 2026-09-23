#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_music_player.py — 播放内核（Phase 7 T8-4）

跑法:
    python tests/test_music_player.py

被测的是 `agent/core/music.py::MusicPlayer`：把"PC 上的 neteasecli"、"本地库"、
"推给 GUI 的状态"接起来。PC 用一个**替身 cli**（不 ssh），本地库用临时文件。

覆盖（都是"你定的那三条"相关的）:

  1) 播放/计次: **只有听到 30 s 才 +1**；同一会话只计一次；暂停期间不计（position 不走）
  2) **曲终停下**: position 到 duration 就置 playing=False（**不自动续**——下一首由 chat 决定）
  3) 进度: `snapshot()` 用"上次真值 + 本地外推"（播放中会走、暂停不动、不超过时长）
  4) 队列: `play(queue=…)` 记下 chat 挑的候选顺序, `step(±1)` 在队列里走；没队列就如实报错
  5) 入库: 播一首库里没有的歌 -> 自动按元数据打 tag 加进去; 已存在的**不动 plays/tags**
  6) chat 补 tag: `tag_track()` 默认作用于当前这首；库里没有就报错
  7) PC 那边起不来: `PlayerFailure` 被翻成 `MusicError`（消息原样带上, 给人看）
  8) 没在放时的控制（toggle/pause/resume）如实报错
"""

import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.core.music import MusicError, MusicPlayer  # noqa: E402
from agent.media import music_library as lib  # noqa: E402
from agent.net.netease_cli import NeteaseCliError, PlayerFailure  # noqa: E402

logging.disable(logging.CRITICAL)


class FakeCli(object):
    """替身 neteasecli: 状态由测试摆布, 记下每次调用。"""

    #: 真 `NeteaseCli` 上有这两个（Runtime 的日志会用到）
    user = "Anorak"
    host = "192.168.137.1"

    def __init__(self):
        self.state = {"playing": False, "paused": False, "position": 0.0, "duration": 0.0}
        self.calls = []
        self.fail_play = None
        self.detail = {}

    # --- 内核用到的四个动作 ---
    def status(self):
        self.calls.append(("status",))
        return dict(self.state)

    def play(self, track_id, quality=None):
        self.calls.append(("play", str(track_id)))
        if self.fail_play:
            raise self.fail_play
        self.state = {"playing": True, "paused": False, "position": 0.0,
                      "duration": float(self.detail.get(str(track_id), {}).get("duration", 236000)) / 1000.0}
        return {"message": "Now playing"}

    def pause(self):
        self.calls.append(("pause",))
        self.state["paused"] = not self.state.get("paused")
        self.state["playing"] = not self.state["paused"]
        return {"message": "Paused" if self.state["paused"] else "Resumed"}

    def stop(self):
        self.calls.append(("stop",))
        self.state = {"playing": False, "paused": False, "position": 0.0, "duration": 0.0}
        return {"message": "Stopped"}

    def seek(self, seconds, absolute=False):
        self.calls.append(("seek", seconds, absolute))
        self.state["position"] = float(seconds)
        return {"message": "Seeked"}

    def set_volume(self, level):
        self.calls.append(("volume", level))
        return {"message": "Volume: %d%%" % level}

    def track_detail(self, track_id):
        self.calls.append(("detail", str(track_id)))
        return self.detail.get(str(track_id), {"name": "T%s" % track_id, "duration": 200000})

    def search(self, kind, keyword, limit=20, offset=0):
        self.calls.append(("search", kind, keyword))
        return {"tracks": [{"id": "2747166493", "name": "JANE DOE",
                            "artists": [{"name": "米津玄師"}], "album": {"name": "JANE DOE"},
                            "duration": 236007},
                           {"id": "2", "name": "另一个", "artists": [], "album": {},
                            "duration": 100000}]}


def make_player(**kwargs):
    tmp = tempfile.mkdtemp(prefix="music-")
    path = os.path.join(tmp, "music_library.jsonl")
    cli = kwargs.pop("cli", None) or FakeCli()
    player = MusicPlayer(cli=cli, library_file=path, **kwargs)
    return player, cli, path


class Clock(object):
    """可控时钟: 把 monotonic 换成自己走的秒数。"""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def tick(self, seconds):
        self.now += seconds


# ===========================================================================
#  1) 计次
# ===========================================================================
class TestPlayCount(unittest.TestCase):
    def setUp(self):
        self.player, self.cli, self.path = make_player(count_after_s=30)
        self.clock = Clock()
        self.player._clock = self.clock          # 注入时钟（生产用 time.monotonic）

    def test_counts_only_after_the_threshold(self):
        self.player.play("1", meta={"name": "A", "duration_ms": 200000})
        self.cli.state.update({"playing": True, "paused": False, "position": 10.0, "duration": 200.0})
        self.player.refresh()
        self.assertEqual(self.player.tracks()[0]["plays"], 0, "10 s 不算一次")

        self.cli.state["position"] = 29.9
        self.player.refresh()
        self.assertEqual(self.player.tracks()[0]["plays"], 0, "阈值前一点都不算")

        self.cli.state["position"] = 30.0
        self.player.refresh()
        self.assertEqual(self.player.tracks()[0]["plays"], 1, "到 30 s 记一次")

    def test_only_once_per_session(self):
        self.player.play("1", meta={"name": "A", "duration_ms": 200000})
        for position in (30.0, 60.0, 90.0):
            self.cli.state.update({"playing": True, "paused": False,
                                   "position": position, "duration": 200.0})
            self.player.refresh()
        self.assertEqual(self.player.tracks()[0]["plays"], 1, "一次播放会话只计一次")

    def test_a_new_play_session_counts_again(self):
        self.player.play("1", meta={"name": "A", "duration_ms": 200000})
        self.cli.state.update({"playing": True, "position": 31.0, "duration": 200.0})
        self.player.refresh()
        self.player.play("1", meta={"name": "A", "duration_ms": 200000})
        self.cli.state.update({"playing": True, "position": 31.0, "duration": 200.0})
        self.player.refresh()
        self.assertEqual(self.player.tracks()[0]["plays"], 2)

    def test_status_happens_before_the_threshold_is_not_counted(self):
        self.player.play("1", meta={"name": "A", "duration_ms": 200000})
        self.cli.state.update({"playing": True, "paused": True, "position": 12.0, "duration": 200.0})
        self.player.refresh()
        self.assertEqual(self.player.tracks()[0]["plays"], 0)


# ===========================================================================
#  2) 曲终停下
# ===========================================================================
class TestEndOfTrack(unittest.TestCase):
    def test_it_stops_instead_of_auto_advancing(self):
        player, cli, _path = make_player()
        player.play("1", meta={"name": "A", "duration_ms": 200000})
        cli.state.update({"playing": True, "paused": False, "position": 199.0, "duration": 200.0})
        snapshot = player.refresh()
        self.assertFalse(snapshot["playing"], "到末尾就停（下一首由 chat 决定）")
        self.assertEqual([c[0] for c in cli.calls].count("play"), 1, "不许自动播下一首")

    def test_when_the_player_is_gone_it_is_also_ended(self):
        player, cli, _path = make_player()
        player.play("1", meta={"name": "A", "duration_ms": 200000})
        cli.state = {"playing": True, "paused": False, "position": 0.0, "duration": 0.0}
        self.assertFalse(player.refresh()["playing"], "duration=0 = 没有播放器在放")


# ===========================================================================
#  3) 进度外推
# ===========================================================================
class TestSnapshot(unittest.TestCase):
    def test_position_advances_between_polls(self):
        player, cli, _path = make_player()
        player.play("1", meta={"name": "A", "duration_ms": 200000})
        cli.state.update({"playing": True, "paused": False, "position": 10.0, "duration": 200.0})
        player.refresh()
        first = player.snapshot()
        second = player.snapshot(now=player._truth_at + 5.0)
        self.assertAlmostEqual(second["position_s"] - first["position_s"], 5.0, places=1)

    def test_paused_position_does_not_move(self):
        player, cli, _path = make_player()
        player.play("1", meta={"name": "A", "duration_ms": 200000})
        cli.state.update({"playing": False, "paused": True, "position": 42.0, "duration": 200.0})
        player.refresh()
        self.assertAlmostEqual(player.snapshot(now=player._truth_at + 30.0)["position_s"],
                               42.0, places=1)

    def test_position_never_exceeds_duration(self):
        player, cli, _path = make_player()
        player.play("1", meta={"name": "A", "duration_ms": 200000})
        cli.state.update({"playing": True, "paused": False, "position": 199.0, "duration": 200.0})
        player.refresh()
        self.assertLessEqual(player.snapshot(now=player._truth_at + 100.0)["position_s"], 200.0)

    def test_snapshot_shape(self):
        player, cli, _path = make_player()
        player.play("1", meta={"name": "A", "artists": "米津玄師", "album": "X",
                               "duration_ms": 236007})
        snapshot = player.snapshot()
        for key in ("track_id", "title", "artist", "album", "position_s", "duration_s",
                    "playing", "plays", "tags"):
            self.assertIn(key, snapshot)
        self.assertEqual(snapshot["title"], "A")
        self.assertAlmostEqual(snapshot["duration_s"], 236.0, places=1)


# ===========================================================================
#  4) 队列
# ===========================================================================
class TestQueue(unittest.TestCase):
    def test_step_walks_the_queue_the_chat_gave(self):
        player, cli, _path = make_player()
        player.play("1", queue=["1", "2", "3"], meta={"name": "A"})
        player.step(1)
        self.assertEqual(player.current_id, "2")
        player.step(1)
        self.assertEqual(player.current_id, "3")

    def test_stepping_past_the_end_is_reported(self):
        player, cli, _path = make_player()
        player.play("1", queue=["1", "2"], meta={"name": "A"})
        player.step(1)
        with self.assertRaises(MusicError) as ctx:
            player.step(1)
        self.assertIn("末尾", str(ctx.exception))

    def test_backwards(self):
        player, cli, _path = make_player()
        player.play("1", queue=["1", "2"], meta={"name": "A"})
        player.step(1)
        player.step(-1)
        self.assertEqual(player.current_id, "1")

    def test_without_a_queue_it_says_so(self):
        player, cli, _path = make_player()
        player.play("1", meta={"name": "A"})
        with self.assertRaises(MusicError) as ctx:
            player.step(1)
        self.assertIn("候选队列", str(ctx.exception))
        self.assertIn("对话", str(ctx.exception), "要告诉人怎么才能有队列")


# ===========================================================================
#  5) 入库
# ===========================================================================
class TestEnsureTrack(unittest.TestCase):
    def test_a_new_track_is_added_with_auto_tags(self):
        player, cli, _path = make_player()
        cli.detail["9"] = {"name": "残酷な天使のテーゼ", "duration": 240000,
                           "artists": [{"name": "高橋洋子"}], "album": {"name": "新世紀エヴァンゲリオン"}}
        record = player.ensure_track("9", genre=["日语"])
        self.assertEqual(record["name"], "残酷な天使のテーゼ")
        self.assertEqual(record["artists"], "高橋洋子")
        self.assertEqual(record["tags"]["lang"], ["jp"])
        self.assertEqual(record["tags"]["genre"], ["日语"])
        self.assertEqual(record["plays"], 0)

    def test_an_existing_track_is_returned_untouched(self):
        player, cli, _path = make_player()
        player.play("1", meta={"name": "A"})
        player.tag_track({"mood": ["calm"]}, track_id="1")
        before = player.tracks()[0]
        again = player.ensure_track("1", meta={"name": "别的名字"})
        self.assertEqual(again["name"], "A", "已有的不动")
        self.assertEqual(again["tags"], before["tags"])
        self.assertEqual(again["plays"], before["plays"])

    def test_detail_failure_still_stores_the_id(self):
        class Broken(FakeCli):
            def track_detail(self, track_id):
                raise NeteaseCliError("PC 关机了")

        player, _cli, _path = make_player(cli=Broken())
        record = player.ensure_track("7")
        self.assertEqual(record["id"], "7")
        self.assertEqual(record["name"], "7", "拿不到详情也不丢这首歌")


# ===========================================================================
#  6) chat 补 tag
# ===========================================================================
class TestTagging(unittest.TestCase):
    def test_tag_the_current_track(self):
        player, cli, _path = make_player()
        player.play("1", meta={"name": "A"})
        updated = player.tag_track({"mood": ["energetic"]})
        self.assertEqual(updated["tags"]["mood"], ["energetic"])
        self.assertEqual(player.snapshot()["tags"]["mood"], ["energetic"])

    def test_remove_a_tag(self):
        player, cli, _path = make_player()
        player.play("1", meta={"name": "A"})
        player.tag_track({"mood": ["energetic", "calm"]})
        updated = player.tag_track({"mood": ["calm"]}, remove=True)
        self.assertEqual(updated["tags"]["mood"], ["energetic"])

    def test_tagging_nothing_is_reported(self):
        player, cli, _path = make_player()
        with self.assertRaises(MusicError):
            player.tag_track({"mood": ["calm"]})

    def test_tagging_an_unknown_track_is_reported(self):
        player, cli, _path = make_player()
        with self.assertRaises(MusicError) as ctx:
            player.tag_track({"mood": ["calm"]}, track_id="404")
        self.assertIn("404", str(ctx.exception))


# ===========================================================================
#  7/8) 失败与"没在放"
# ===========================================================================
class TestFailures(unittest.TestCase):
    def test_play_failure_becomes_a_music_error(self):
        cli = FakeCli()
        cli.fail_play = PlayerFailure("PC 上没有开始播放（PC 上没人登录）")
        player, _cli, _path = make_player(cli=cli)
        with self.assertRaises(MusicError) as ctx:
            player.play("1", meta={"name": "A"})
        self.assertIn("没人登录", str(ctx.exception))

    def test_toggle_without_playback_is_reported(self):
        player, cli, _path = make_player()
        with self.assertRaises(MusicError) as ctx:
            player.toggle()
        self.assertIn("没有在放", str(ctx.exception))

    def test_pause_and_resume(self):
        player, cli, _path = make_player()
        player.play("1", meta={"name": "A", "duration_ms": 100000})
        self.assertEqual(player.pause()["paused"], True)
        self.assertTrue(cli.state["paused"])
        self.assertEqual(player.resume()["paused"], False)

    def test_stop_clears_the_session(self):
        player, cli, _path = make_player()
        player.play("1", meta={"name": "A"})
        result = player.stop()
        self.assertEqual(result["track"], "1")
        self.assertFalse(player.snapshot()["playing"])
        self.assertEqual(cli.calls[-1][0], "stop")

    def test_seek_and_volume_are_forwarded(self):
        player, cli, _path = make_player()
        player.play("1", meta={"name": "A", "duration_ms": 100000})
        player.seek(30, absolute=True)
        player.set_volume(60)
        self.assertIn(("seek", 30, True), cli.calls)
        self.assertIn(("volume", 60), cli.calls)

    def test_status_reads_zero_once_right_after_play_are_retried(self):
        """板端实测: 刚 play 完问一次 status 可能瞬时读到 duration=0（neteasecli 的状态
        还没写好）—— 那时"暂停/继续"会误报"没有在放的歌"。这里钉住"会重试一次"。"""

        class Flaky(FakeCli):
            def __init__(self):
                FakeCli.__init__(self)
                self.zero_once = False

            def status(self):
                self.calls.append(("status",))
                if self.zero_once:
                    self.zero_once = False
                    return {"playing": True, "paused": False, "position": 0.0, "duration": 0.0}
                return dict(self.state)

        cli = Flaky()
        player, _cli, _path = make_player(cli=cli)
        player.play("1", meta={"name": "A", "duration_ms": 100000})
        cli.state.update({"playing": True, "paused": False, "position": 5.0, "duration": 100.0})
        cli.zero_once = True
        self.assertTrue(player.toggle()["paused"], "重试之后应当真的暂停, 而不是误报")

    def test_two_zero_reads_still_report_nothing_playing(self):
        player, cli, _path = make_player()
        player.play("1", meta={"name": "A", "duration_ms": 100000})
        cli.state = {"playing": False, "paused": False, "position": 0.0, "duration": 0.0}
        with self.assertRaises(MusicError) as ctx:
            player.toggle()
        self.assertIn("没有在放", str(ctx.exception))


# ===========================================================================
#  候选（"下一首由 chat 决定"的原料）
# ===========================================================================
class TestCandidates(unittest.TestCase):
    def test_candidates_come_from_the_library(self):
        player, cli, path = make_player()
        lib.write_tracks(path, [
            lib.make_record("1", "A", plays=5, tags={"mood": ["energetic"]}),
            lib.make_record("2", "B", plays=0, tags={"mood": ["energetic"]}),
            lib.make_record("3", "C", plays=2, tags={"mood": ["calm"]}),
        ])
        picked = player.candidates(tag="energetic")
        self.assertEqual([t["id"] for t in picked], ["2", "1"], "听得最少的先")

    def test_summary(self):
        player, cli, path = make_player()
        lib.write_tracks(path, [lib.make_record("1", "A", plays=1)])
        self.assertEqual(player.summary()["count"], 1)
        self.assertEqual(player.summary()["total_plays"], 1)


class TestRuntimeWiring(unittest.IsolatedAsyncioTestCase):
    """`Runtime` 的音乐接线（T8-4）: 建 player、轮询推送、命令入口、失败如实。"""

    def _runtime(self, tmp, cli=None, enabled=True):
        import agent.main as main_module
        from agent.net import netease_cli

        self.cli = cli or FakeCli()
        self._original = netease_cli.NeteaseCli.from_config
        netease_cli.NeteaseCli.from_config = classmethod(
            lambda cls, config=None, log=None, runner=None: (
                self.cli if (config or {}).get("music", {}).get("enabled") else None))
        self.addCleanup(setattr, netease_cli.NeteaseCli, "from_config", self._original)

        runtime = main_module.Runtime(
            config={"music": {"enabled": enabled, "pc_host": "192.168.137.1",
                              "pc_user": "Anorak",
                              "library_file": os.path.join(tmp, "music_library.jsonl"),
                              "count_after_s": 30, "poll_interval_s": 0.05}},
            start_native=False, start_terminal=False,
            log=logging.getLogger("test.music"))
        return runtime

    async def test_start_builds_the_player_and_the_poll_task(self):
        tmp = tempfile.mkdtemp(prefix="music-rt-")
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        runtime = self._runtime(tmp)
        await runtime._start_music()
        try:
            self.assertIsNotNone(runtime.music, "music.enabled=true 时应当建出播放器")
            self.assertIsNotNone(runtime._music_task)
        finally:
            await runtime.stop()

    async def test_disabled_means_no_player_and_no_task(self):
        tmp = tempfile.mkdtemp(prefix="music-rt-")
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        runtime = self._runtime(tmp, enabled=False)
        await runtime._start_music()
        self.assertIsNone(runtime.music)
        self.assertIsNone(runtime._music_task)

    async def test_state_and_control_go_through_the_hook(self):
        tmp = tempfile.mkdtemp(prefix="music-rt-")
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        runtime = self._runtime(tmp)
        pushed = []
        runtime.on_music = lambda snapshot: pushed.append(snapshot)
        await runtime._start_music()
        try:
            played = runtime.music_play(keyword="JANE DOE")
            self.assertTrue(played["ok"], played)
            self.assertEqual(played["name"], "JANE DOE", "用搜索结果里的名字入库")
            self.assertEqual(played["track_id"], "2747166493")
            self.assertEqual(played["queue"], ["2747166493", "2"],
                             "搜出来的候选顺序记成队列（上一首/下一首在它里面走）")
            self.assertTrue(pushed, "播放后要推一条 music")
            self.assertIn("title", pushed[-1])

            state = runtime.music_state()
            self.assertEqual(state["track_id"], "2747166493")

            paused = runtime.music_control("play_pause")
            self.assertTrue(paused["ok"], paused)
            self.assertTrue(paused["paused"])
        finally:
            await runtime.stop()

    async def test_control_failure_is_honest(self):
        tmp = tempfile.mkdtemp(prefix="music-rt-")
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        runtime = self._runtime(tmp)
        await runtime._start_music()
        try:
            result = runtime.music_control("next")     # 还没队列
            self.assertFalse(result["ok"])
            self.assertIn("候选队列", result["error"])
            self.assertEqual(result["tell_user"], result["error"])
        finally:
            await runtime.stop()

    async def test_unknown_action_is_refused(self):
        tmp = tempfile.mkdtemp(prefix="music-rt-")
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        runtime = self._runtime(tmp)
        await runtime._start_music()
        try:
            self.assertFalse(runtime.music_control("dance")["ok"])
        finally:
            await runtime.stop()

    async def test_candidates_come_from_the_library(self):
        tmp = tempfile.mkdtemp(prefix="music-rt-")
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        runtime = self._runtime(tmp)
        await runtime._start_music()
        try:
            lib.write_tracks(os.path.join(tmp, "music_library.jsonl"), [
                lib.make_record("1", "A", plays=2, tags={"mood": ["calm"]}),
                lib.make_record("2", "B", plays=0, tags={"mood": ["calm"]}),
            ])
            found = runtime.music_candidates(tag="calm")
            self.assertTrue(found["ok"])
            self.assertEqual([t["id"] for t in found["tracks"]], ["2", "1"])
            self.assertEqual(found["summary"]["count"], 2)
        finally:
            await runtime.stop()

    async def test_tagging_without_playback_is_honest(self):
        tmp = tempfile.mkdtemp(prefix="music-rt-")
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        runtime = self._runtime(tmp)
        await runtime._start_music()
        try:
            result = runtime.music_tag({"mood": ["calm"]})
            self.assertFalse(result["ok"])
            self.assertIn("没有在放", result["error"])
        finally:
            await runtime.stop()

    async def test_push_only_when_something_changed(self):
        tmp = tempfile.mkdtemp(prefix="music-rt-")
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        runtime = self._runtime(tmp)
        pushed = []
        runtime.on_music = lambda snapshot: pushed.append(snapshot)
        await runtime._start_music()
        try:
            runtime._push_music({"title": "", "playing": False})
            self.assertEqual(pushed, [], "没在放、也没曲目 -> 不推")
            runtime._push_music({"title": "A", "playing": True})
            self.assertEqual(len(pushed), 1)
        finally:
            await runtime.stop()

    async def test_search_returns_candidates_without_playing(self):
        tmp = tempfile.mkdtemp(prefix="music-rt-")
        self.addCleanup(__import__("shutil").rmtree, tmp, True)

        class Searcher(FakeCli):
            def search(self, kind, keyword, limit=20, offset=0):
                self.calls.append(("search", kind, keyword))
                return {"tracks": [{"id": "1", "name": "A",
                                    "artists": [{"name": "X"}], "album": {"name": "Z"},
                                    "duration": 1000}]}

        runtime = self._runtime(tmp, cli=Searcher())
        await runtime._start_music()
        try:
            found = runtime.music_search("A")
            self.assertTrue(found["ok"])
            self.assertEqual(found["tracks"][0]["artists"], "X")
            self.assertNotIn(("play", "1"), self.cli.calls, "搜歌不播")
        finally:
            await runtime.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
