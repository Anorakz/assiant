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
  2) **环形队列**（T8-5b）: 内容由 chat 决定（`enqueue` / `clear_queue`）,
     **走位由 GUI 决定**（`step(±1)`）; 到尾回第一首、到首回最后一首;
     **曲终自动下一首**（队列空才停下）
  3) 进度: `snapshot()` 用"上次真值 + 本地外推"（播放中会走、暂停不动、不超过时长）
  4) 队列: `enqueue()` 不碰 PC、`clear_queue()` 不停播放、`start_from_queue()` 给 GUI 的播放按钮
  5) 入库: 播一首库里没有的歌 -> 自动按元数据打 tag 加进去; 已存在的**不动 plays/tags**
  6) chat 补 tag: `tag_track()` 默认作用于当前这首；库里没有就报错
  7) PC 那边起不来: `PlayerFailure` 被翻成 `MusicError`（消息原样带上, 给人看）
  8) 没在放时的控制: `pause()` 如实报错; `toggle()`/`resume()` 从队列起播（队列空才报错）
"""

import asyncio
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.core.music import (  # noqa: E402
    DEFAULT_LYRIC_BACKOFF_S, MusicError, MusicPlayer)
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
        #: T15-16: `track lyric` 的返回（形状与真 neteasecli 一致）+ 失败开关
        self.fail_lyric = None
        self.lyric_payload = {"lrc": "[00:01.00]第一句\n[00:03.00]第二句\n",
                              "tlyric": "[00:01.00]First\n[00:03.00]\n",
                              "hasLyric": True, "hasTranslation": True}

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

    def lyric(self, track_id):
        self.calls.append(("lyric", str(track_id)))
        if self.fail_lyric:
            raise self.fail_lyric
        return dict(self.lyric_payload)

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
    def test_without_a_queue_it_stops_at_the_end(self):
        player, cli, _path = make_player()
        player.play("1", meta={"name": "A", "duration_ms": 200000})
        cli.state.update({"playing": True, "paused": False, "position": 199.0, "duration": 200.0})
        snapshot = player.refresh()
        self.assertFalse(snapshot["playing"], "没有队列 -> 到末尾就停")
        self.assertEqual([c[0] for c in cli.calls].count("play"), 1, "不许自动播下一首")

    def test_with_a_queue_it_advances_to_the_next_track(self):
        player, cli, _path = make_player()
        player.enqueue("1", meta={"name": "A", "duration_ms": 200000})
        player.play("1", queue=["1", "2"])
        player.ensure_track("2", meta={"name": "B", "duration_ms": 100000})
        cli.state.update({"playing": True, "paused": False, "position": 199.0, "duration": 200.0})
        player.refresh()
        self.assertEqual(player.current_id, "2", "曲终 -> 环形队列里的下一首")
        self.assertEqual(cli.calls[-1], ("play", "2"))

    def test_it_wraps_from_the_last_track_back_to_the_first(self):
        player, cli, _path = make_player()
        player.play("1", queue=["1", "2"], meta={"name": "A", "duration_ms": 100000})
        player.ensure_track("2", meta={"name": "B", "duration_ms": 100000})
        player.step(1)                                  # 光标在 "2"（最后一首）
        cli.state.update({"playing": True, "paused": False, "position": 99.0, "duration": 100.0})
        player.refresh()
        self.assertEqual(player.current_id, "1", "最后一首放完 -> 回到第一首（环形）")
        self.assertEqual(player.queue_state()["index"], 0)

    def test_a_queue_of_one_replays_itself(self):
        player, cli, _path = make_player()
        player.play("1", queue=["1"], meta={"name": "A", "duration_ms": 100000})
        cli.state.update({"playing": True, "paused": False, "position": 99.0, "duration": 100.0})
        player.refresh()
        self.assertEqual(player.current_id, "1", "只有一首时=重放这一首")
        self.assertEqual([c[0] for c in cli.calls].count("play"), 2)

    def test_playback_that_never_started_does_not_chain_through_the_queue(self):
        """PC 上根本没放起来（duration 一直 0）时不许顺着队列一路切下去。"""
        player, cli, _path = make_player()
        player.enqueue("1", meta={"name": "A"})
        player.enqueue("2", meta={"name": "B"})
        player.play("1", queue=["1", "2"])              # 会话起点 = 现在
        before = [c[0] for c in cli.calls].count("play")
        cli.state = {"playing": False, "paused": False, "position": 0.0, "duration": 0.0}
        player.refresh()                                # 立刻又读到 duration=0
        self.assertEqual([c[0] for c in cli.calls].count("play"), before,
                         "会话还没放够 _MIN_ADVANCE_S, 不许自动切")
        self.assertFalse(player.snapshot()["playing"])

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
                    "playing", "plays", "tags",
                    "lyric_ok", "lyric_lines", "lyric_rev", "lyric_reason"):
            self.assertIn(key, snapshot)
        self.assertEqual(snapshot["title"], "A")
        self.assertAlmostEqual(snapshot["duration_s"], 236.0, places=1)


# ===========================================================================
#  3b) 歌词（T15-16）
# ===========================================================================
class TestLyrics(unittest.TestCase):
    """歌词: **换歌才拉** / 缓存 / 失败退避 / 载荷字段 / 版本号只在真变时动。

    ⚠ 这一层是"数据"侧: 解析规则在 `tests/test_lyrics.py`, 显示口径在 C++ 侧。
    """

    def setUp(self):
        self.player, self.cli, _path = make_player()
        self.clock = Clock()
        self.player._clock = self.clock

    def lyric_calls(self):
        return [call for call in self.cli.calls if call[0] == "lyric"]

    def test_fetches_once_for_the_current_track(self):
        self.player.play("1", meta={"name": "A"})
        first = self.player.ensure_lyric()
        self.assertTrue(first["fetched"])
        self.assertTrue(first["ok"])
        self.assertEqual(len(self.lyric_calls()), 1)
        again = self.player.ensure_lyric()
        self.assertFalse(again["fetched"], "同一首不该再走一趟 ssh")
        self.assertEqual(len(self.lyric_calls()), 1)

    def test_snapshot_carries_the_lines_and_the_translation(self):
        self.player.play("1", meta={"name": "A"})
        self.player.ensure_lyric()
        snapshot = self.player.snapshot()
        self.assertTrue(snapshot["lyric_ok"])
        self.assertEqual([row["text"] for row in snapshot["lyric_lines"]],
                         ["第一句", "第二句"])
        self.assertEqual(snapshot["lyric_lines"][0]["tr"], "First")
        self.assertEqual(snapshot["lyric_lines"][1]["tr"], "", "没译文的那行留空 = 显示原文")
        self.assertEqual(snapshot["lyric_rev"], self.player._lyric["rev"])

    def test_changing_track_drops_the_previous_lyrics_immediately(self):
        """换歌那一瞬（新歌词还没到）**不许**继续报上一首的歌词。"""
        self.player.play("1", meta={"name": "A"})
        self.player.ensure_lyric()
        self.player.play("2", meta={"name": "B"})
        snapshot = self.player.snapshot()
        self.assertFalse(snapshot["lyric_ok"])
        self.assertEqual(snapshot["lyric_lines"], [])
        self.assertEqual(snapshot["lyric_reason"], "", "空 reason = 界面知道是'还没取到'")

    def test_a_new_track_is_fetched_again(self):
        self.player.play("1", meta={"name": "A"})
        self.player.ensure_lyric()
        self.player.play("2", meta={"name": "B"})
        self.player.ensure_lyric()
        self.assertEqual([call[1] for call in self.lyric_calls()], ["1", "2"])

    def test_failure_is_reported_and_backed_off(self):
        self.cli.fail_lyric = PlayerFailure("ssh 超时")
        self.player.play("1", meta={"name": "A"})
        first = self.player.ensure_lyric()
        self.assertTrue(first["fetched"], "试过了（只是没成）")
        self.assertFalse(first["ok"])
        self.assertIn("歌词取不到", self.player.snapshot()["lyric_reason"])

        self.clock.tick(60.0)
        self.assertFalse(self.player.ensure_lyric()["fetched"], "退避窗口里不重试")
        self.assertEqual(len(self.lyric_calls()), 1)

        self.clock.tick(DEFAULT_LYRIC_BACKOFF_S)
        self.cli.fail_lyric = None                     # PC 回来了
        self.assertTrue(self.player.ensure_lyric()["fetched"], "退避到点后自己再试")
        self.assertEqual(len(self.lyric_calls()), 2)
        self.assertTrue(self.player.snapshot()["lyric_ok"])

    def test_a_pure_instrumental_says_so(self):
        self.cli.lyric_payload = {"lrc": "", "tlyric": "", "hasLyric": False}
        self.player.play("1", meta={"name": "A"})
        self.player.ensure_lyric()
        snapshot = self.player.snapshot()
        self.assertFalse(snapshot["lyric_ok"])
        self.assertEqual(snapshot["lyric_reason"], "没有歌词")

    def test_revision_only_moves_when_the_content_changes(self):
        """`lyric_rev` 是推送去重键的一部分 —— 每轮都变就等于每轮都推。"""
        self.player.play("1", meta={"name": "A"})
        self.player.ensure_lyric()
        rev = self.player.snapshot()["lyric_rev"]
        self.player.ensure_lyric()
        self.player.ensure_lyric()
        self.assertEqual(self.player.snapshot()["lyric_rev"], rev)

    def test_no_track_means_no_ssh(self):
        result = self.player.ensure_lyric()
        self.assertFalse(result["fetched"])
        self.assertEqual(self.lyric_calls(), [])
        self.assertFalse(self.player.snapshot()["lyric_ok"])

    def test_the_constructor_offset_moves_the_lines(self):
        player, _cli, _path = make_player(lyric_offset_ms=500)
        player.play("1", meta={"name": "A"})
        player.ensure_lyric()
        self.assertEqual([row["t"] for row in player.snapshot()["lyric_lines"]], [0.5, 2.5])

    def test_force_refetches_even_in_the_backoff_window(self):
        self.cli.fail_lyric = PlayerFailure("ssh 超时")
        self.player.play("1", meta={"name": "A"})
        self.player.ensure_lyric()
        self.assertTrue(self.player.ensure_lyric(force=True)["fetched"])
        self.assertEqual(len(self.lyric_calls()), 2)


# ===========================================================================
#  4) 队列
# ===========================================================================
class TestQueue(unittest.TestCase):
    """环形队列（T8-5b）: **内容**由 chat 决定（enqueue / clear_queue）, **走位**由 GUI 决定。"""

    def test_enqueue_appends_and_does_not_touch_the_pc(self):
        player, cli, _path = make_player()
        first = player.enqueue("1", meta={"name": "A"})
        self.assertTrue(first["added"])
        self.assertEqual(first["queue"]["size"], 1)
        self.assertEqual(first["queue"]["index"], 0, "第一首进队 -> 光标落在它身上")
        second = player.enqueue("2", meta={"name": "B"})
        self.assertTrue(second["added"])
        self.assertEqual(second["queue"]["ids"], ["1", "2"])
        self.assertEqual(second["queue"]["index"], 0, "进队列不改光标")
        self.assertEqual([c[0] for c in cli.calls].count("play"), 0, "进队列**不放**歌")

    def test_enqueue_the_same_song_twice_does_not_duplicate(self):
        player, cli, _path = make_player()
        player.enqueue("1", meta={"name": "A"})
        again = player.enqueue("1", meta={"name": "A"})
        self.assertFalse(again["added"])
        self.assertEqual(again["queue"]["ids"], ["1"])

    def test_replace_clears_the_queue_first(self):
        player, cli, _path = make_player()
        player.enqueue("1", meta={"name": "A"})
        player.enqueue("2", meta={"name": "B"})
        replaced = player.enqueue("3", meta={"name": "C"}, replace=True)
        self.assertTrue(replaced["replaced"])
        self.assertEqual(replaced["queue"]["ids"], ["3"])
        self.assertEqual(replaced["queue"]["index"], 0)

    def test_clear_queue_reports_and_keeps_playing(self):
        player, cli, _path = make_player()
        player.play("1", queue=["1", "2"], meta={"name": "A"})
        result = player.clear_queue()
        self.assertEqual(result["cleared"], 2)
        self.assertEqual(result["queue"]["size"], 0)
        self.assertEqual(result["queue"]["index"], -1)
        self.assertEqual([c[0] for c in cli.calls].count("stop"), 0,
                         "清空队列**不停播放** —— 停/放是 GUI 按钮的事")
        self.assertTrue(player.snapshot()["playing"])

    def test_queue_state_shape(self):
        player, cli, _path = make_player()
        player.play("1", queue=["1", "2"], meta={"name": "A"})
        state = player.queue_state()
        self.assertEqual(state, {"size": 2, "index": 0, "current": "1", "ids": ["1", "2"]})

    def test_start_from_queue_plays_the_cursor(self):
        player, cli, _path = make_player()
        player.enqueue("1", meta={"name": "A"})
        player.enqueue("2", meta={"name": "B"})
        player.step(1)                                  # 光标到 "2"
        record = player.start_from_queue()
        self.assertEqual(player.current_id, "2")
        self.assertEqual(record["name"], "B")

    def test_start_from_queue_without_a_queue_says_so(self):
        player, cli, _path = make_player()
        with self.assertRaises(MusicError) as ctx:
            player.start_from_queue()
        self.assertIn("队列是空的", str(ctx.exception))
        self.assertIn("对话", str(ctx.exception), "要告诉人怎么才能有队列")

    def test_step_walks_the_queue_the_chat_gave(self):
        player, cli, _path = make_player()
        player.play("1", queue=["1", "2", "3"], meta={"name": "A"})
        player.step(1)
        self.assertEqual(player.current_id, "2")
        player.step(1)
        self.assertEqual(player.current_id, "3")

    def test_stepping_past_the_end_wraps_around(self):
        player, cli, _path = make_player()
        player.play("1", queue=["1", "2"], meta={"name": "A"})
        player.step(1)
        player.step(1)                                  # 末尾再往后 -> 回到第一首
        self.assertEqual(player.current_id, "1")
        self.assertEqual(player.queue_state()["index"], 0)

    def test_stepping_before_the_start_wraps_to_the_last(self):
        player, cli, _path = make_player()
        player.play("1", queue=["1", "2"], meta={"name": "A"})
        player.step(-1)
        self.assertEqual(player.current_id, "2", "第一首往前 -> 最后一首")

    def test_a_single_track_queue_replays_itself(self):
        player, cli, _path = make_player()
        player.play("1", queue=["1"], meta={"name": "A"})
        player.step(1)
        self.assertEqual(player.current_id, "1")
        self.assertEqual([c[0] for c in cli.calls].count("play"), 2, "环形: 一首就重放它")

    def test_enqueue_refuses_an_id_the_pc_does_not_know(self):
        """T8-5b 板端实测: 0.6B 会**编造 id**（写过 `track1`）—— 库里没有就当场问 PC,
        问不到就报错, 不许安静地排进队列。"""
        class NoDetail(FakeCli):
            def track_detail(self, track_id):
                self.calls.append(("detail", str(track_id)))
                raise NeteaseCliError("PC 那边网络请求失败: Unknown error (code: 400)")

        cli = NoDetail()
        player, _cli, _path = make_player(cli=cli)
        with self.assertRaises(MusicError) as ctx:
            player.enqueue("track1")
        self.assertIn("track1", str(ctx.exception))
        self.assertIn("search", str(ctx.exception), "要告诉模型 id 从哪儿来")
        self.assertEqual(player.queue_state()["size"], 0, "没通过校验就别排进队列")
        self.assertEqual(player.tracks(), [], "也别写进本地库")

    def test_enqueue_accepts_an_id_the_pc_knows(self):
        player, cli, _path = make_player()
        cli.detail["9"] = {"name": "残酷な天使のテーゼ", "duration": 240000,
                           "artists": [{"name": "高橋洋子"}],
                           "album": {"name": "新世紀エヴァンゲリオン"}}
        result = player.enqueue("9")
        self.assertEqual(result["name"], "残酷な天使のテーゼ", "问到的详情要用来入库")
        self.assertEqual(result["queue"]["ids"], ["9"])
        self.assertEqual(player.tracks()[0]["artists"], "高橋洋子")

    def test_enqueue_does_not_ask_the_pc_for_a_track_already_in_the_library(self):
        player, cli, _path = make_player()
        player.enqueue("1", meta={"name": "A"})
        before = [c for c in cli.calls if c[0] == "detail"]
        player.enqueue("1")                                   # 已在库里
        self.assertEqual([c for c in cli.calls if c[0] == "detail"], before,
                         "库里有的歌不该再去问 PC")

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
        self.assertIn("队列是空的", str(ctx.exception))
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

    def test_toggle_with_an_empty_queue_is_reported(self):
        player, cli, _path = make_player()
        with self.assertRaises(MusicError) as ctx:
            player.toggle()
        self.assertIn("队列是空的", str(ctx.exception))

    def test_toggle_starts_from_the_queue_when_nothing_is_playing(self):
        player, cli, _path = make_player()
        player.enqueue("1", meta={"name": "A", "duration_ms": 100000})
        result = player.toggle()                       # GUI 的"播放"按钮
        self.assertTrue(result["started"])
        self.assertFalse(result["paused"])
        self.assertEqual(player.current_id, "1")
        self.assertEqual(cli.calls[-1], ("play", "1"))

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

    def test_two_zero_reads_with_an_empty_queue_report_it(self):
        player, cli, _path = make_player()
        player.play("1", meta={"name": "A", "duration_ms": 100000})
        cli.state = {"playing": False, "paused": False, "position": 0.0, "duration": 0.0}
        with self.assertRaises(MusicError) as ctx:
            player.toggle()
        self.assertIn("队列是空的", str(ctx.exception))


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

    def _runtime(self, tmp, cli=None, enabled=True, lyric_offset_ms=0):
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
                              "count_after_s": 30, "poll_interval_s": 0.05,
                              "lyric_offset_ms": lyric_offset_ms}},
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

    async def test_enqueue_by_keyword_queues_and_starts(self):
        tmp = tempfile.mkdtemp(prefix="music-rt-")
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        runtime = self._runtime(tmp)
        pushed = []
        runtime.on_music = lambda snapshot: pushed.append(snapshot)
        await runtime._start_music()
        try:
            result = runtime.music_enqueue(keyword="JANE DOE")
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["queued"], ["2747166493"],
                             "keyword 默认只排 1 首（要几首就写 limit）")
            self.assertTrue(result["started"], "A1: 什么都没在放 -> 顺手起播")
            self.assertEqual(result["tracks"][0]["name"], "JANE DOE", "用搜索结果里的名字入库")
            self.assertEqual(result["tracks"][0]["id"], "2747166493",
                             "回话里的 id 必须是真 id（T8-7 修: 以前这里是 null，"
                             "模型拿到 null 没法接着用它）")
            self.assertEqual(result["queue"]["size"], 1)
            self.assertTrue(pushed, "起播后要推一条 music")
            self.assertIn("title", pushed[-1])

            state = runtime.music_state()
            self.assertEqual(state["track_id"], "2747166493")

            paused = runtime.music_control("play_pause")
            self.assertTrue(paused["ok"], paused)
            self.assertTrue(paused["paused"])
        finally:
            await runtime.stop()

    async def test_enqueue_does_not_interrupt_what_is_already_playing(self):
        """T8-7: 排歌之前先问一次 PC 的**真值**（`snapshot()` 是纯本地的, 不碰 PC）。

        修前: 刚重启的 Agent（本地那份 playing=False）会把用户正在听的那首停掉从头起播。
        """
        tmp = tempfile.mkdtemp(prefix="music-rt-")
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        runtime = self._runtime(tmp)
        await runtime._start_music()
        try:
            self.cli.state = {"playing": True, "paused": False, "position": 30.0,
                              "duration": 236.0}          # PC 上已经在放
            result = runtime.music_enqueue(track_id="2747166493")
            self.assertTrue(result["ok"], result)
            self.assertFalse(result["will_start"], "PC 上已经在放 -> 不该起播")
            self.assertFalse(result["started"])
            self.assertNotIn(("play", "2747166493"), self.cli.calls,
                             "不该去动用户正在听的那首")
        finally:
            await runtime.stop()

    async def test_enqueue_still_starts_when_something_was_playing_before_we_looked(self):
        """反面: PC 上确实没在放（status duration=0）-> 照旧顺手起播。"""
        tmp = tempfile.mkdtemp(prefix="music-rt-")
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        runtime = self._runtime(tmp)
        await runtime._start_music()
        try:
            self.cli.state = {"playing": False, "paused": False, "position": 0.0,
                              "duration": 0.0}
            result = runtime.music_enqueue(track_id="2747166493")
            self.assertTrue(result["started"], result)
            self.assertIn(("play", "2747166493"), self.cli.calls)
        finally:
            await runtime.stop()

    async def test_enqueue_picks_from_the_library_by_tag_and_sort(self):
        tmp = tempfile.mkdtemp(prefix="music-rt-")
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        runtime = self._runtime(tmp)
        await runtime._start_music()
        try:
            lib.write_tracks(os.path.join(tmp, "music_library.jsonl"), [
                lib.make_record("1", "A", plays=9, tags={"mood": ["energetic"]}),
                lib.make_record("2", "B", plays=0, tags={"mood": ["energetic"]}),
                lib.make_record("3", "C", plays=1, tags={"mood": ["calm"]}),
            ])
            result = runtime.music_enqueue(tag="mood=energetic")     # 统一语法
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["queued"], ["2"], "听得最少的先（默认 plays_asc）")

            more = runtime.music_enqueue(tag="mood=energetic", limit=2, replace=True)
            self.assertEqual(more["queued"], ["2", "1"], "limit=2 + replace 先清空")
            self.assertEqual(more["queue"]["ids"], ["2", "1"])
            self.assertFalse(more["started"],
                             "A1 只在'什么都没在放'时顺手起播 —— 换队列不打断正在放的歌")

            state = runtime.music_queue_state()
            self.assertEqual([t["name"] for t in state["tracks"]], ["B", "A"])

            cleared = runtime.music_queue_clear()
            self.assertEqual(cleared["cleared"], 2)
            self.assertEqual(cleared["queue"]["size"], 0)
        finally:
            await runtime.stop()

    async def test_enqueue_with_an_empty_library_says_what_to_do(self):
        tmp = tempfile.mkdtemp(prefix="music-rt-")
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        runtime = self._runtime(tmp)
        await runtime._start_music()
        try:
            result = runtime.music_enqueue()          # 库里什么都没有
            self.assertFalse(result["ok"])
            self.assertIn("keyword", result["error"] + result["tell_user"])
        finally:
            await runtime.stop()

    async def test_enqueue_is_honest_when_the_pc_cannot_start(self):
        tmp = tempfile.mkdtemp(prefix="music-rt-")
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        cli = FakeCli()
        cli.fail_play = PlayerFailure("PC 上没人登录")
        runtime = self._runtime(tmp, cli=cli)
        await runtime._start_music()
        try:
            result = runtime.music_enqueue(keyword="JANE DOE")
            self.assertFalse(result["ok"])
            self.assertIn("没人登录", result["error"])
            self.assertIn("排进队列", result["error"], "要说清队列其实已经安排了")
            self.assertEqual(result["queued"], ["2747166493"])
        finally:
            await runtime.stop()

    async def test_enqueue_refuses_a_hallucinated_id(self):
        """板端实测（T8-5b）: 模型把 `track1` 当 id 填进来 —— 要当场诚实回绝。"""
        tmp = tempfile.mkdtemp(prefix="music-rt-")
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        cli = FakeCli()
        cli.detail = {}                        # detail 默认返回 T<id>（非空）—— 这里让它失败
        original = cli.track_detail

        def failing(track_id):
            raise PlayerFailure("PC 那边网络请求失败: Unknown error (code: 400)")

        cli.track_detail = failing
        runtime = self._runtime(tmp, cli=cli)
        await runtime._start_music()
        try:
            result = runtime.music_enqueue(track_id="track1")
            self.assertFalse(result["ok"])
            self.assertIn("track1", result["error"])
            self.assertIn("search", result["error"] + result["tell_user"])
            self.assertEqual(runtime.music_queue_state()["size"], 0)
        finally:
            cli.track_detail = original
            await runtime.stop()

    async def test_control_failure_is_honest(self):
        tmp = tempfile.mkdtemp(prefix="music-rt-")
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        runtime = self._runtime(tmp)
        await runtime._start_music()
        try:
            result = runtime.music_control("next")     # 还没队列
            self.assertFalse(result["ok"])
            self.assertIn("队列是空的", result["error"])
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

    async def test_a_lyric_arriving_while_paused_still_pushes(self):
        """T15-16: 暂停时曲目与播放状态都没变 —— 只有歌词变了也必须推出去。

        （这正是 `_push_music()` 去重键要带 `lyric_rev` 的原因。）
        """
        tmp = tempfile.mkdtemp(prefix="music-rt-")
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        runtime = self._runtime(tmp)
        pushed = []
        runtime.on_music = lambda snapshot: pushed.append(snapshot)
        await runtime._start_music()
        try:
            runtime._push_music({"title": "A", "playing": False, "lyric_rev": 0})
            self.assertEqual(len(pushed), 1)
            runtime._push_music({"title": "A", "playing": False, "lyric_rev": 0})
            self.assertEqual(len(pushed), 1, "一模一样的快照不重复推")
            runtime._push_music({"title": "A", "playing": False, "lyric_rev": 1})
            self.assertEqual(len(pushed), 2, "只有歌词变了 -> 也要推")
        finally:
            await runtime.stop()

    async def test_reconnecting_gui_gets_the_current_state_even_when_paused(self):
        """T15-16 板端真歌验收抓到的真 bug：暂停中补推会被"变化才推"的去重**吞掉**。

        当时的现场：PC 上 mpv 是 paused，`push_current_music()` 复用 `_push_music()` 的去重键
        （和上一次一模一样）-> 补推被丢弃 -> 新连上的 GUI 显示"未播放"。
        """
        tmp = tempfile.mkdtemp(prefix="music-rt-")
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        runtime = self._runtime(tmp)
        pushed = []
        runtime.on_music = lambda snapshot: pushed.append(snapshot)
        await runtime._start_music()
        try:
            # 停掉轮询：不然它每 0.05 s 也会推一条，计数就不可确定（这条测的是补推本身）
            if runtime._music_task is not None:
                runtime._music_task.cancel()
                runtime._music_task = None
            runtime.music.play("536622304", meta={"name": "Lemon"})
            # 摆成板端当时的现场：有曲目、但 PC 上 mpv 是 **paused**
            runtime.music._truth = {"position": 8.5, "duration": 256.0, "playing": False}
            # 先推一条（键 = 标题/playing/lyric_rev）
            runtime._push_music(runtime.music.snapshot())
            self.assertEqual(len(pushed), 1)
            self.assertFalse(pushed[0]["playing"])
            # 此刻 GUI 重连 -> 补推必须**照推**（force），哪怕键一模一样
            self.assertTrue(runtime.push_current_music(), "补推不能被去重吞掉")
            self.assertEqual(len(pushed), 2)
            self.assertEqual(pushed[-1]["title"], "Lemon")
            self.assertFalse(pushed[-1]["playing"])
        finally:
            await runtime.stop()

    async def test_the_offset_key_reaches_the_player(self):
        tmp = tempfile.mkdtemp(prefix="music-rt-")
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        runtime = self._runtime(tmp, lyric_offset_ms=300)
        await runtime._start_music()
        try:
            self.assertEqual(runtime.music.lyric_offset_ms, 300)
        finally:
            await runtime.stop()

    async def test_music_state_for_the_model_has_no_lyric_table(self):
        """T15-16: `music_state()` 会进工具结果（喂模型）—— 上百行歌词不该跟着进去。"""
        tmp = tempfile.mkdtemp(prefix="music-rt-")
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        runtime = self._runtime(tmp)
        await runtime._start_music()
        try:
            runtime.music.play("1", meta={"name": "A"})
            runtime.music.ensure_lyric()
            self.assertTrue(runtime.music.snapshot()["lyric_ok"], "推送那条路有歌词")
            self.assertTrue(runtime.music.snapshot()["lyric_lines"])
            state = runtime.music_state()
            self.assertNotIn("lyric_lines", state)
            self.assertIn("lyric_ok", state, "有没有歌词这个结论留着（不占地方）")
            self.assertIn("lyric_reason", state)
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

    # ---- T10-4: 轮询里自动补歌 ----

    async def test_autofill_fills_the_queue_from_the_library(self):
        tmp = tempfile.mkdtemp(prefix="music-rt-")
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        runtime = self._runtime(tmp)
        await runtime._start_music()
        try:
            lib.write_tracks(os.path.join(tmp, "music_library.jsonl"),
                             [lib.make_record(str(i), "T%d" % i, plays=i) for i in range(3)])
            runtime.music.set_target(3)
            result = runtime._music_autofill()
            self.assertEqual(result["from_library"], 3)
            self.assertEqual(len(runtime.music.queue_ids()), 3)
            self.assertIsNone(runtime._music_autofill(), "够了就别再补")
        finally:
            await runtime.stop()

    async def test_autofill_searches_by_the_profile_artist_when_short(self):
        tmp = tempfile.mkdtemp(prefix="music-rt-")
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        runtime = self._runtime(tmp)
        await runtime._start_music()
        try:
            lib.write_tracks(os.path.join(tmp, "music_library.jsonl"),
                             [lib.make_record("1", "唯一", artists="米津玄師", plays=2)])
            # 画像文件: 只有歌手权重（补歌就靠它去搜）
            runtime.profile_file = os.path.join(tmp, "user_profile.jsonl")
            with open(runtime.profile_file, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"version": 1, "built_at": "t",
                                         "music": {"artist": [{"name": "米津玄師",
                                                               "weight": 0.6}]},
                                         "walls": {"ip": []}, "mood": {"label": "calm"},
                                         "thin": [], "muted": {"ip": [], "artist": [],
                                                               "track": []}},
                                        ensure_ascii=False) + "\n")
            runtime.music.set_target(5)
            result = runtime._music_autofill()
            self.assertEqual(result["from_library"], 1)
            self.assertEqual(result["searched"], ["米津玄師"], "本地库不够 -> 按歌手去搜")
            self.assertIn(("search", "track", "米津玄師"), self.cli.calls)
            self.assertIn("2747166493", runtime.music.queue_ids(), "搜到的真 id 进队了")
            self.assertNotIn(("play", "2747166493"), self.cli.calls, "补歌不播")
        finally:
            await runtime.stop()

    async def test_autofill_does_nothing_when_disabled(self):
        tmp = tempfile.mkdtemp(prefix="music-rt-")
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        runtime = self._runtime(tmp)
        runtime.config["music"]["autofill"] = {"enabled": False, "target": 5}
        await runtime._start_music()
        try:
            self.assertEqual(runtime.music.target(), 0, "关掉就是目标 0")
            self.assertIsNone(runtime._music_autofill())
            self.assertEqual(runtime.music.queue_ids(), [])
        finally:
            await runtime.stop()

    async def test_the_poll_loop_calls_the_autofill(self):
        tmp = tempfile.mkdtemp(prefix="music-rt-")
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        runtime = self._runtime(tmp)
        await runtime._start_music()
        # ⚠ 先停掉 `_start_music` 起的那个后台轮询 —— 否则会有**两个** loop 同时补歌
        runtime._music_task.cancel()
        runtime._music_task = None
        calls = []
        runtime._music_autofill = lambda: calls.append("autofill")
        original = asyncio.sleep

        async def one_tick(_seconds):
            if calls:
                raise asyncio.CancelledError()
            await original(0)

        asyncio.sleep = one_tick
        try:
            with self.assertRaises(asyncio.CancelledError):
                await runtime._music_loop()
        finally:
            asyncio.sleep = original
            await runtime.stop()
        self.assertEqual(calls, ["autofill"], "每次轮询都要看一眼队列够不够")


class TestAutofill(unittest.TestCase):
    """T10-4: 队列补到目标长度 —— 先吃本地库, 不够**按歌手**去 PC 搜。"""

    def _player(self, tracks, cli=None, target=30):
        player, cli, path = make_player(cli=cli)
        lib.write_tracks(path, tracks)
        player.set_target(target)
        return player, cli, path

    def _by_artist(self, artist, count, start=900):
        return {"tracks": [{"id": str(start + i), "name": "%s-%d" % (artist, i),
                            "artists": [{"name": artist}],
                            "album": {"name": "A"}, "duration": 200000}
                           for i in range(count)]}

    def test_it_fills_from_the_library_first(self):
        tracks = [lib.make_record(str(i), "T%d" % i, plays=i) for i in range(5)]
        player, cli, _path = self._player(tracks, target=5)
        result = player.refill()
        self.assertEqual(result["from_library"], 5)
        self.assertEqual(result["from_search"], 0)
        self.assertEqual(result["searched"], [], "本地库够 -> 不出去搜")
        self.assertEqual(len(player.queue_ids()), 5)
        self.assertEqual(result["short_by"], 0)

    def test_songs_already_in_the_queue_are_not_added_twice(self):
        tracks = [lib.make_record(str(i), "T%d" % i) for i in range(3)]
        player, _cli, _path = self._player(tracks, target=3)
        player.enqueue("1", verify=False)
        result = player.refill()
        self.assertEqual(player.queue_ids().count("1"), 1)
        self.assertEqual(len(player.queue_ids()), 3, "补到目标就不再补")
        self.assertEqual(result["from_library"], 2)

    def test_it_searches_by_the_profile_artists_when_the_library_is_short(self):
        tracks = [lib.make_record("1", "唯一", artists="米津玄師", plays=3)]
        player, cli, _path = self._player(tracks, target=30)
        cli.search = lambda kind, keyword, limit=20, offset=0: self._by_artist(keyword, 4)
        profile = {"music": {"artist": [{"name": "米津玄師", "weight": 0.6}]},
                   "mood": {"label": "calm"}, "thin": []}
        result = player.refill(profile, search_per_cycle=3, search_limit=4)
        self.assertEqual(result["from_library"], 1)
        self.assertEqual(result["from_search"], 4, "本地库那首 + 搜到的 4 首")
        self.assertEqual(result["searched"], ["米津玄師"])
        self.assertEqual(len(player.queue_ids()), 5)
        self.assertEqual(result["short_by"], 25, "目标 30 —— 搜完这一轮还差 25")

    def test_only_tracks_by_that_artist_are_kept(self):
        player, cli, _path = self._player([])
        cli.search = lambda kind, keyword, limit=20, offset=0: {
            "tracks": [{"id": "1", "name": "对", "artists": [{"name": keyword}]},
                       {"id": "2", "name": "不对", "artists": [{"name": "别人"}]}]}
        profile = {"music": {"artist": [{"name": "米津玄師", "weight": 0.6}]}}
        player.refill(profile)
        self.assertEqual(player.queue_ids(), ["1"], "只收 artists 命中的")

    def test_the_search_bound_and_backoff_are_respected(self):
        player, cli, _path = self._player([])
        calls = []

        def search(kind, keyword, limit=20, offset=0):
            calls.append(keyword)
            raise NeteaseCliError("连不上 PC")

        cli.search = search
        profile = {"music": {"artist": [{"name": "A", "weight": 3}, {"name": "B", "weight": 2},
                                       {"name": "C", "weight": 1}]}}
        first = player.refill(profile, search_per_cycle=2, search_backoff_s=300, now=1000.0)
        self.assertEqual(calls, ["A", "B"], "一个 tick 最多搜 2 个")
        self.assertEqual(sorted(first["skipped"]), ["A", "B"])
        player.refill(profile, search_per_cycle=3, search_backoff_s=300, now=1100.0)
        self.assertEqual(calls, ["A", "B", "C"], "退避中的 A/B 不再试, 换 C")
        player.refill(profile, search_per_cycle=3, search_backoff_s=300, now=1400.0)
        self.assertEqual(calls[-1], "C", "C 也进了退避（1400-1000=400 > 300 -> A 可以再试）")

    def test_muted_artists_and_tracks_are_left_alone(self):
        tracks = [lib.make_record("1", "不想听", artists="A"),
                  lib.make_record("2", "可以", artists="B")]
        player, cli, _path = self._player(tracks, target=2)
        cli.search = lambda kind, keyword, limit=20, offset=0: self._by_artist(keyword, 2)
        profile = {"music": {"artist": [{"name": "A", "weight": 0.9},
                                        {"name": "B", "weight": 0.1}]}}
        result = player.refill(profile, muted_artists=["A"], muted_tracks=["1"])
        self.assertEqual(result["from_library"], 1)
        self.assertEqual(player.queue_ids()[0], "2")
        self.assertNotIn("A", result["searched"], "不想听的歌手不搜")

    def test_target_zero_means_no_autofill(self):
        player, _cli, _path = self._player([lib.make_record("1", "T")], target=0)
        result = player.refill()
        self.assertEqual(result["filled"], [])
        self.assertIn("没开自动补歌", result["why"][0])

    def test_it_says_why_it_could_not_fill(self):
        player, _cli, _path = self._player([lib.make_record("1", "唯一")], target=30)
        result = player.refill()                       # 没有画像 -> 不搜
        self.assertEqual(result["short_by"], 29)
        self.assertTrue(any("画像里还没有歌手偏好" in note for note in result["why"]))

    def test_a_search_that_returns_nothing_by_that_artist_is_not_fatal(self):
        player, cli, _path = self._player([])
        cli.search = lambda kind, keyword, limit=20, offset=0: {
            "tracks": [{"id": "9", "name": "别人的", "artists": [{"name": "别人"}]}]}
        profile = {"music": {"artist": [{"name": "米津玄師", "weight": 0.6}]}}
        result = player.refill(profile)
        self.assertEqual(result["from_search"], 0)
        self.assertEqual(player.queue_ids(), [])


class TestQueueRemoval(unittest.TestCase):
    """T10 第 5 条: "不想听" -> 从播放队列里去掉（不动本地库）。"""

    def test_it_removes_only_the_named_songs(self):
        player, _cli, _path = make_player()
        for track_id in ("1", "2", "3"):
            player.enqueue(track_id, verify=False)
        result = player.remove(["2"])
        self.assertEqual(result["removed"], ["2"])
        self.assertEqual(player.queue_ids(), ["1", "3"])
        self.assertFalse(result["current_removed"])

    def test_removing_the_current_one_moves_the_cursor_back_one(self):
        """正在放的那首被去掉 -> 光标退一格, 调用方 `step(1)` 就是"跳到下一首"。"""
        player, _cli, _path = make_player()
        for track_id in ("1", "2", "3"):
            player.enqueue(track_id, verify=False)
        player.current_id = "2"
        player._queue_index = 1
        result = player.remove(["2"])
        self.assertTrue(result["current_removed"])
        player.step(1)
        self.assertEqual(player.current_id, "3", "跳到被去掉那首后面那首")

    def test_removing_everything_leaves_an_empty_queue(self):
        player, _cli, _path = make_player()
        player.enqueue("1", verify=False)
        result = player.remove(["1", "9"])
        self.assertEqual(result["removed"], ["1"])
        self.assertEqual(player.queue_ids(), [])
        self.assertEqual(player.queue_state()["index"], -1)

    def test_removing_something_that_is_not_there_is_a_no_op(self):
        player, _cli, _path = make_player()
        player.enqueue("1", verify=False)
        result = player.remove(["7"])
        self.assertEqual(result["removed"], [])
        self.assertEqual(player.queue_ids(), ["1"])


class TestProfileEffectsOnTheQueue(unittest.IsolatedAsyncioTestCase):
    """T10-5: 画像构建完的队列动作 —— 负反馈清队列 / **只有心情变了才重置**。"""

    def _runtime(self, tmp):
        import logging as _logging

        from agent.main import Runtime

        runtime = Runtime(config={"music": {"enabled": True, "pc_host": "192.168.137.1",
                                            "pc_user": "Anorak",
                                            "library_file": os.path.join(tmp, "music_library.jsonl"),
                                            "count_after_s": 30, "poll_interval_s": 0.05}},
                          start_native=False, start_terminal=False,
                          log=_logging.getLogger("test.music.effects"))
        return runtime

    async def _with_queue(self, tmp, ids=("1", "2", "3")):
        self.cli = FakeCli()
        from agent.net import netease_cli

        original = netease_cli.NeteaseCli.from_config
        netease_cli.NeteaseCli.from_config = classmethod(
            lambda cls, config=None, log=None, runner=None: self.cli)
        self.addCleanup(setattr, netease_cli.NeteaseCli, "from_config", original)
        lib.write_tracks(os.path.join(tmp, "music_library.jsonl"),
                         [lib.make_record(i, "T%s" % i, artists="A") for i in ids])
        runtime = self._runtime(tmp)
        await runtime._start_music()
        runtime._music_task.cancel()
        runtime._music_task = None
        for track_id in ids:
            runtime.music.enqueue(track_id, verify=False)
        return runtime

    async def test_negative_feedback_clears_them_from_the_queue_and_refills(self):
        tmp = tempfile.mkdtemp(prefix="music-eff-")
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        runtime = await self._with_queue(tmp, ids=("1", "2", "3"))
        refills = []
        runtime._music_autofill = lambda: refills.append("refill")
        try:
            runtime._apply_profile_effects({"cleared": {"track_ids": ["2"]}})
            self.assertEqual(runtime.music.queue_ids(), ["1", "3"])
            self.assertEqual(refills, ["refill"], "去掉之后要**立刻**补队列")
        finally:
            await runtime.stop()

    async def test_removing_what_is_playing_skips_to_the_next(self):
        tmp = tempfile.mkdtemp(prefix="music-eff-")
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        runtime = await self._with_queue(tmp, ids=("1", "2", "3"))
        runtime._music_autofill = lambda: None
        try:
            runtime.music.current_id = "2"
            runtime.music._queue_index = 1
            runtime._apply_profile_effects({"cleared": {"track_ids": ["2"]}})
            self.assertEqual(self.cli.calls[-1], ("play", "3"),
                             "正在放的那首被点名 -> 跳到它后面那首")
        finally:
            await runtime.stop()

    async def test_a_mood_change_resets_the_queue_but_keeps_what_is_playing(self):
        tmp = tempfile.mkdtemp(prefix="music-eff-")
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        runtime = await self._with_queue(tmp, ids=("1", "2", "3"))
        refills = []
        runtime._music_autofill = lambda: refills.append("refill")
        try:
            runtime.music.current_id = "2"
            result = runtime._reset_queue_on_mood_change(
                {"mood": {"label": "tired"}}, {"mood": {"label": "happy"}})
            self.assertEqual((result["from"], result["to"]), ("happy", "tired"))
            self.assertEqual(result["kept"], "2")
            self.assertEqual(runtime.music.queue_ids(), ["2"], "只留正在放的那首")
            self.assertEqual(refills, ["refill"], "重置完立刻按新心情补")
        finally:
            await runtime.stop()

    async def test_the_same_mood_does_not_reset_anything(self):
        tmp = tempfile.mkdtemp(prefix="music-eff-")
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        runtime = await self._with_queue(tmp, ids=("1", "2", "3"))
        runtime._music_autofill = lambda: None
        try:
            self.assertIsNone(runtime._reset_queue_on_mood_change(
                {"mood": {"label": "calm"}}, {"mood": {"label": "calm"}}))
            self.assertEqual(runtime.music.queue_ids(), ["1", "2", "3"], "没变就别动队列")
        finally:
            await runtime.stop()

    async def test_an_unknown_mood_is_not_a_change(self):
        tmp = tempfile.mkdtemp(prefix="music-eff-")
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        runtime = await self._with_queue(tmp, ids=("1", "2"))
        runtime._music_autofill = lambda: None
        try:
            for old, new in (("calm", "unknown"), ("unknown", "calm"), ("", "calm"),
                             ("calm", "")):
                with self.subTest(old=old, new=new):
                    self.assertIsNone(runtime._reset_queue_on_mood_change(
                        {"mood": {"label": new}}, {"mood": {"label": old}}),
                        "unknown 不算心情变化（模型偶尔答不出, 不该清队列）")
            self.assertEqual(runtime.music.queue_ids(), ["1", "2"])
        finally:
            await runtime.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
