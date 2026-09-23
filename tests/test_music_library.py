#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_music_library.py — 本地音乐库（Phase 7 T8-3）

跑法:
    python tests/test_music_library.py

被测的是 `agent/media/music_library.py`：`config/music_library.jsonl`（**本地库**，
不再依赖云歌单）—— 一行一首歌: id + tags + 播放次数。**纯 Python**，开发机与板端
跑同一份断言。

覆盖:

  1) 记录 schema / tag 归一化（去空、去重、保持顺序；字符串也收）
  2) 读写: 文件不存在=空库 / 坏行跳过并记问题 / 原子写 + .bak / 路径解析（相对仓库根）
  3) `upsert_tracks` 合并: **按 id 幂等**（导两次结果一样）、**保留 plays**、tag 取并集
  4) chat 补充 tag: `add_tags` / `remove_tags`
  5) 计数: `bump_play` 加 1 并记时间
  6) 查询: `pick(tag=…, sort=…)`（听得最少优先是默认）、`tag_counts`、`summarise`
  7) 自动打标: `auto_tags`（genre/artist/lang/era）+ `guess_lang`（假名→jp、汉字→zh、
     英文→不猜）—— **只靠元数据**, 这一点是刻意的（我们没有音频特征）
"""

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

from agent.media import music_library as ml  # noqa: E402

logging.disable(logging.CRITICAL)


def track(track_id="1", name="JANE DOE", plays=0, tags=None, **kwargs):
    return ml.make_record(track_id, name, kwargs.pop("artists", "米津玄師"),
                          kwargs.pop("album", "JANE DOE"), kwargs.pop("duration_ms", 236007),
                          tags or {}, plays=plays, **kwargs)


# ===========================================================================
#  1) 记录与 tag
# ===========================================================================
class TestRecord(unittest.TestCase):
    def test_schema(self):
        item = track()
        self.assertEqual(item["version"], ml.RECORD_VERSION)
        for key in ("id", "name", "artists", "album", "duration_ms", "tags",
                    "plays", "last_played", "added_at", "source"):
            self.assertIn(key, item)
        self.assertEqual(item["plays"], 0)
        self.assertIsNone(item["last_played"], "没放过就是 None, 不是 0 时间")

    def test_id_is_a_string(self):
        self.assertEqual(track(track_id=2747166493)["id"], "2747166493")

    def test_tags_are_normalised(self):
        item = ml.make_record("1", "x", tags={"mood": ["calm", "calm", " "], "genre": "jpop",
                                              "": ["junk"], "bad": 5})
        self.assertEqual(item["tags"], {"mood": ["calm"], "genre": ["jpop"]},
                         "去重/去空/单字符串也收/没有值的轴丢掉")

    def test_tags_none_is_empty_dict(self):
        self.assertEqual(ml.make_record("1", "x")["tags"], {})


# ===========================================================================
#  2) 读写
# ===========================================================================
class TestReadWrite(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "music_library.jsonl")

    def test_missing_file_is_an_empty_library(self):
        loaded = ml.load(self.path)
        self.assertEqual(loaded, {"tracks": [], "problems": []})

    def test_round_trip(self):
        ml.write_tracks(self.path, [track("1", plays=2), track("2", name="晴天")])
        tracks, problems = ml.read_tracks(self.path)
        self.assertEqual(problems, [])
        self.assertEqual([t["id"] for t in tracks], ["1", "2"])
        self.assertEqual(tracks[0]["plays"], 2)

    def test_one_line_per_track(self):
        ml.write_tracks(self.path, [track("1"), track("2")])
        lines = [line for line in open(self.path, encoding="utf-8").read().splitlines() if line]
        self.assertEqual(len(lines), 2)
        for line in lines:
            json.loads(line)                             # 每行都是独立 JSON

    def test_bad_lines_are_skipped_with_a_problem(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write('{"id": "1", "name": "ok"}\n')
            handle.write("{ not json\n")
            handle.write('{"name": "no id"}\n')
            handle.write('{"id": "2", "name": "ok2"}\n')
        tracks, problems = ml.read_tracks(self.path)
        self.assertEqual([t["id"] for t in tracks], ["1", "2"])
        self.assertEqual(len(problems), 2, "坏行要**记下来**, 不是静默吞掉")

    def test_write_leaves_a_backup(self):
        ml.write_tracks(self.path, [track("1")])
        ml.write_tracks(self.path, [track("1"), track("2")])
        self.assertTrue(os.path.exists(self.path + ".bak"))
        backup = open(self.path + ".bak", encoding="utf-8").read()
        self.assertEqual(len(backup.strip().splitlines()), 1, "备份是上一版")

    def test_non_serialisable_record_is_refused(self):
        bad = track("1")
        bad["tags"] = {"mood": object()}
        with self.assertRaises(ml.MusicLibraryError):
            ml.write_tracks(self.path, [bad])

    def test_empty_library_writes_an_empty_file(self):
        ml.write_tracks(self.path, [])
        self.assertEqual(open(self.path, encoding="utf-8").read(), "")


class TestPathResolution(unittest.TestCase):
    def test_default_is_under_config(self):
        default = ml.default_library_file()
        self.assertTrue(default.endswith(os.path.join("config", "music_library.jsonl")))
        self.assertEqual(ml.resolve_library_file(None), default)
        self.assertEqual(ml.resolve_library_file(""), default)

    def test_relative_is_resolved_against_the_repo_root(self):
        self.assertEqual(ml.resolve_library_file("config/music_library.jsonl"),
                         ml.default_library_file())

    def test_absolute_is_used_as_is(self):
        absolute = os.path.join(tempfile.gettempdir(), "x.jsonl")
        self.assertEqual(ml.resolve_library_file(absolute), os.path.normpath(absolute))

    def test_no_mixed_separators(self):
        resolved = ml.resolve_library_file("config/music_library.jsonl")
        self.assertNotIn("/", resolved.replace(os.sep, ""))


# ===========================================================================
#  3) 合并
# ===========================================================================
class TestUpsert(unittest.TestCase):
    def test_new_tracks_are_added(self):
        merged, added, updated = ml.upsert_tracks([], [track("1"), track("2")])
        self.assertEqual((added, updated), (2, 0))
        self.assertEqual([t["id"] for t in merged], ["1", "2"])

    def test_importing_the_same_playlist_twice_is_idempotent(self):
        first, added, _ = ml.upsert_tracks([], [track("1"), track("2")])
        second, added2, updated2 = ml.upsert_tracks(first, [track("1"), track("2")])
        self.assertEqual((added2, updated2), (0, 0))
        self.assertEqual(len(second), 2, "不能重复添加")

    def test_plays_are_never_lost_on_reimport(self):
        existing = [ml.bump_play(ml.bump_play(track("1")))]        # plays=2
        merged, _added, _updated = ml.upsert_tracks(existing, [track("1")])
        self.assertEqual(merged[0]["plays"], 2, "重新导入歌单不能把听歌次数清零")

    def test_metadata_is_refreshed_but_tags_are_merged(self):
        existing = [ml.add_tags(track("1", name="旧名字", tags={"mood": ["calm"]}),
                                {"scene": ["anime"]})]
        merged, _a, updated = ml.upsert_tracks(
            existing, [ml.make_record("1", "新名字", tags={"mood": ["calm", "energetic"]})])
        self.assertEqual(updated, 1)
        self.assertEqual(merged[0]["name"], "新名字")
        self.assertEqual(sorted(merged[0]["tags"]), ["mood", "scene"])
        self.assertEqual(merged[0]["tags"]["mood"], ["calm", "energetic"])

    def test_order_is_stable_and_new_ones_go_last(self):
        existing, _a, _u = ml.upsert_tracks([], [track("1"), track("2")])
        merged, _a, _u = ml.upsert_tracks(existing, [track("3")])
        self.assertEqual([t["id"] for t in merged], ["1", "2", "3"])

    def test_records_without_id_are_skipped(self):
        merged, added, _u = ml.upsert_tracks([], [{"name": "no id"}])
        self.assertEqual((merged, added), ([], 0))


# ===========================================================================
#  4) chat 补充 / 删除 tag
# ===========================================================================
class TestTagEditing(unittest.TestCase):
    def test_add_tags_merges_and_does_not_mutate(self):
        original = track(tags={"mood": ["calm"]})
        updated = ml.add_tags(original, {"mood": ["calm", "energetic"], "scene": ["anime"]})
        self.assertEqual(original["tags"], {"mood": ["calm"]}, "不该改原对象")
        self.assertEqual(updated["tags"], {"mood": ["calm", "energetic"], "scene": ["anime"]})

    def test_add_tags_can_create_a_new_axis(self):
        updated = ml.add_tags(track(), {"weather": ["rainy"]})
        self.assertEqual(updated["tags"]["weather"], ["rainy"],
                         "自由轴: chat 想加什么轴都行")

    def test_remove_tags(self):
        updated = ml.remove_tags(track(tags={"mood": ["calm", "energetic"]}),
                                 {"mood": ["energetic"]})
        self.assertEqual(updated["tags"]["mood"], ["calm"])

    def test_removing_the_last_value_drops_the_axis(self):
        updated = ml.remove_tags(track(tags={"mood": ["calm"]}), {"mood": ["calm"]})
        self.assertEqual(updated["tags"], {})


# ===========================================================================
#  5) 计数
# ===========================================================================
class TestBumpPlay(unittest.TestCase):
    def test_bump_counts_and_stamps(self):
        counted = ml.bump_play(track(plays=3))
        self.assertEqual(counted["plays"], 4)
        self.assertTrue(counted["last_played"])

    def test_bump_does_not_mutate(self):
        original = track(plays=0)
        ml.bump_play(original)
        self.assertEqual(original["plays"], 0)

    def test_bump_accepts_an_explicit_time(self):
        self.assertEqual(ml.bump_play(track(), when="2026-09-23T20:00:00")["last_played"],
                         "2026-09-23T20:00:00")


# ===========================================================================
#  6) 查询（"下一首由 chat 决定"的原料）
# ===========================================================================
class TestPick(unittest.TestCase):
    def setUp(self):
        self.tracks = [
            track("1", name="A", plays=5, tags={"mood": ["energetic"]}),
            track("2", name="B", plays=0, tags={"mood": ["energetic", "calm"]}),
            track("3", name="C", plays=2, tags={"mood": ["calm"]}),
            track("4", name="D", plays=1, tags={"genre": ["jpop"]}),
        ]

    def test_default_sort_is_fewest_plays_first(self):
        picked = ml.pick(self.tracks)
        self.assertEqual([t["id"] for t in picked], ["2", "4", "3", "1"])

    def test_tag_filter(self):
        picked = ml.pick(self.tracks, tag="energetic")
        self.assertEqual([t["id"] for t in picked], ["2", "1"], "听过最少的先")
        self.assertEqual(picked[0]["matched"], ["mood=energetic"], "要说清命中哪个轴")

    def test_axis_filter(self):
        self.assertEqual([t["id"] for t in ml.pick(self.tracks, tag="calm", axis="mood")],
                         ["2", "3"])
        self.assertEqual(ml.pick(self.tracks, tag="calm", axis="genre"), [])

    def test_unknown_tag_gives_nothing(self):
        self.assertEqual(ml.pick(self.tracks, tag="nope"), [])

    def test_sorts(self):
        self.assertEqual([t["id"] for t in ml.pick(self.tracks, sort="plays_desc")],
                         ["1", "3", "4", "2"])
        self.assertEqual(len(ml.pick(self.tracks, sort="random", seed=7)), 4)
        self.assertEqual([t["id"] for t in ml.pick(self.tracks, sort="random", seed=7)],
                         [t["id"] for t in ml.pick(self.tracks, sort="random", seed=7)],
                         "同一个 seed 结果要一样（可复现）")

    def test_limit(self):
        self.assertEqual(len(ml.pick(self.tracks, limit=2)), 2)

    def test_pick_does_not_mutate_the_input(self):
        ml.pick(self.tracks, tag="energetic")
        self.assertNotIn("matched", self.tracks[0])


class TestCounts(unittest.TestCase):
    def test_tag_counts(self):
        tracks = [track("1", tags={"mood": ["calm"]}), track("2", tags={"mood": ["calm"]}),
                  track("3", tags={"mood": ["calm"], "genre": ["jpop"]})]
        counts = ml.tag_counts(tracks)
        self.assertEqual(counts["mood"], [{"tag": "calm", "count": 3}])
        self.assertEqual(counts["genre"], [{"tag": "jpop", "count": 1}])
        self.assertEqual(ml.tag_counts(tracks, axis="genre").keys(), {"genre"})

    def test_summarise(self):
        tracks = [ml.bump_play(track("1")), track("2")]
        summary = ml.summarise(tracks)
        self.assertEqual(summary["count"], 2)
        self.assertEqual(summary["total_plays"], 1)
        self.assertEqual(summary["never_played"], 1)

    def test_empty_library_summary(self):
        self.assertEqual(ml.summarise([]), {"count": 0, "total_plays": 0,
                                            "never_played": 0, "tags": {}})


# ===========================================================================
#  7) 自动打标（只靠元数据）
# ===========================================================================
class TestAutoTags(unittest.TestCase):
    def test_full_metadata(self):
        tags = ml.auto_tags("残酷な天使のテーゼ", "高橋洋子", "新世紀エヴァンゲリオン",
                            genre=["日语", "流行"], year=1995)
        self.assertEqual(tags["genre"], ["日语", "流行"])
        self.assertEqual(tags["artist"], ["高橋洋子"])
        self.assertEqual(tags["lang"], ["jp"])
        self.assertEqual(tags["era"], ["1990s"])

    def test_multiple_artists_are_split(self):
        tags = ml.auto_tags("x", "米津玄師/宇多田ヒカル、A")
        self.assertEqual(tags["artist"], ["米津玄師", "宇多田ヒカル", "A"])

    def test_nothing_known_gives_empty_tags(self):
        self.assertEqual(ml.auto_tags("JANE DOE", "", "", year=None),
                         {}, "英文歌名看不出语种 —— 不硬猜")

    def test_guess_lang(self):
        self.assertEqual(ml.guess_lang("残酷な天使のテーゼ"), "jp")
        self.assertEqual(ml.guess_lang("晴天"), "zh")
        self.assertIsNone(ml.guess_lang("JANE DOE"))
        self.assertIsNone(ml.guess_lang(""))

    def test_auto_axes_are_documented(self):
        # TAG_AXES_AUTO 是"哪些轴是自动打的"的唯一声明 —— 文档与工具都用它
        self.assertEqual(set(ml.TAG_AXES_AUTO), {"genre", "artist", "lang", "era"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
