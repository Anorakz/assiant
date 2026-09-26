#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`study_anchors.py` + `study_stats.py` 的单测 —— **全离线**。

`tests/test_game_watch.py` 的同款安排（那边一个文件管 game_anchors / game_watch / pc_probe）:
锚点与统计文件都写到临时目录，不碰仓库里那两份。

这里要守住的几条（都是设计里最容易悄悄坏掉的）:

  · **两个库是分开的**（游戏锚点 vs 学习锚点）—— 默认五类 + 大类映射来自配置;
  · **每类有上限，超了丢最旧**，而且**文件里也要丢**（只丢内存 = 下次重启又超）;
  · **拼错的子标签要当场报错**（不能静默攒一堆永远匹配不出的锚点）;
  · **统计全程有界**（直方图固定档数、明细/备注环形、计数器/阈值封顶）
    且**坏文件不能拖垮功能**（读不出来就从默认值起步）。
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.core import game_anchors                                     # noqa: E402
from agent.core import study_anchors as sa                              # noqa: E402
from agent.core import study_stats as ss                                # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIM = 768        # 与 SigLIP 图像向量一致（锚点复用 wall_data 的 base64(float16) 编解码）


def vector(*values):
    """造一个 768 维向量（前面按给定值，其余补 0）。"""
    out = [float(v) for v in values][:DIM]
    return out + [0.0] * (DIM - len(out))


def shot_path(row):
    """锚点行里 `shot` 字段 -> 真实路径（可能是仓库相对路径，也可能是绝对路径）。"""
    shot = str(row.get("shot") or "")
    return shot if os.path.isabs(shot) else os.path.join(REPO, shot)


class TempDirCase(unittest.TestCase):
    """所有用例共用: 临时目录 + 不碰仓库那两份文件的锚点库/统计。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="study-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.anchor_file = os.path.join(self.tmp, "study_anchors.jsonl")
        self.shot_dir = os.path.join(self.tmp, "shots")
        self.stats_file = os.path.join(self.tmp, "study_stats.json")

    def make_anchors(self, **kwargs):
        kwargs.setdefault("shot_dir", self.shot_dir)
        return sa.StudyAnchors(self.anchor_file, **kwargs)

    def make_stats(self, **kwargs):
        return ss.StudyStats(self.stats_file, **kwargs)

    def write_lines(self, rows):
        with open(self.anchor_file, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def read_lines(self):
        with open(self.anchor_file, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]


# ===========================================================================
#  类别映射
# ===========================================================================
class TestClassMap(TempDirCase):
    """子标签 -> 大类 的映射（配置在 `study.classes.*`）。"""

    def test_default_classes_are_the_five_we_agreed(self):
        anchors = self.make_anchors()
        self.assertEqual(sorted(anchors.classes), ["anime", "code", "doc", "game", "real"])
        self.assertEqual(anchors.classes["code"], sa.CATEGORY_STUDY)
        self.assertEqual(anchors.classes["doc"], sa.CATEGORY_STUDY)
        self.assertEqual(anchors.classes["real"], sa.CATEGORY_STUDY)
        self.assertEqual(anchors.classes["anime"], sa.CATEGORY_NOT_STUDY)
        self.assertEqual(anchors.classes["game"], sa.CATEGORY_NOT_STUDY)

    def test_config_mapping_overrides_the_default(self):
        anchors = self.make_anchors(classes={"doc": "study", "anime": "not_study"})
        self.assertEqual(sorted(anchors.classes), ["anime", "doc"])

    def test_category_names_are_normalised(self):
        anchors = self.make_anchors(classes={"doc": "Study", "anime": " NOT_STUDY "})
        self.assertEqual(anchors.classes, {"doc": "study", "anime": "not_study"})

    def test_unknown_category_is_refused_loudly(self):
        with self.assertRaises(sa.StudyAnchorError) as caught:
            sa.normalize_classes({"doc": "leisure"})
        self.assertIn("study.classes.doc", str(caught.exception))

    def test_empty_class_name_is_refused(self):
        with self.assertRaises(sa.StudyAnchorError):
            sa.normalize_classes({"  ": "study"})

    def test_non_mapping_is_refused(self):
        with self.assertRaises(sa.StudyAnchorError):
            sa.normalize_classes(["code", "doc"])

    def test_empty_mapping_means_defaults(self):
        self.assertEqual(sa.normalize_classes({}), sa.DEFAULT_CLASSES)

    def test_category_of_unknown_class_is_blank_not_a_guess(self):
        """库里出现映射外的子标签（手工改过 jsonl）-> 空串 + 一条 warning，**不猜大类**。"""
        anchors = self.make_anchors()
        with self.assertLogs("agent.core.study_anchors", level="WARNING"):
            self.assertEqual(anchors.category_of("mystery"), "")

    def test_category_counts_counts_both_categories(self):
        anchors = self.make_anchors()
        anchors.add(cls="code", vector=vector(1))
        anchors.add(cls="anime", vector=vector(0, 1))
        self.assertEqual(anchors.category_counts(), {"study": 1, "not_study": 1})


# ===========================================================================
#  复用（一个能力只有一条实现）
# ===========================================================================
class TestReuse(TempDirCase):
    """向量编解码 / 余弦都复用游戏锚点库那一份（T13-1 的教训）。"""

    def test_cosine_is_the_game_anchors_one(self):
        self.assertIs(sa.cosine, game_anchors.cosine)

    def test_encode_decode_are_the_game_anchors_ones(self):
        self.assertIs(sa.encode, game_anchors.encode)
        self.assertIs(sa.decode, game_anchors.decode)

    def test_vector_round_trip_survives_float16(self):
        anchors = self.make_anchors()
        anchors.add(cls="code", vector=vector(0.5, -0.25, 0.125))
        anchors.load()
        self.assertEqual(len(anchors), 1)
        cls, vec = anchors.vectors()[0]
        self.assertEqual(cls, "code")
        self.assertAlmostEqual(vec[0], 0.5, places=3)
        self.assertAlmostEqual(vec[1], -0.25, places=3)
        self.assertAlmostEqual(vec[2], 0.125, places=3)


# ===========================================================================
#  读 / 写 / 上限
# ===========================================================================
class TestLoad(TempDirCase):
    def test_missing_file_is_an_empty_library_not_an_error(self):
        anchors = self.make_anchors()
        self.assertEqual(anchors.load(), 0)
        self.assertEqual(len(anchors), 0)
        self.assertEqual(anchors.match(vector(1)), None)

    def test_broken_line_is_skipped_and_the_rest_still_loads(self):
        good = {"cls": "code", "vector": sa.encode(vector(1)), "shot": "",
                "source": "auto", "note": "", "created_at": "2026-01-01T00:00:00"}
        with open(self.anchor_file, "w", encoding="utf-8") as handle:
            handle.write("这不是 JSON\n")
            handle.write(json.dumps(good, ensure_ascii=False) + "\n")
            handle.write("\n")
            handle.write(json.dumps({"cls": "doc"}, ensure_ascii=False) + "\n")   # 没向量
        anchors = self.make_anchors()
        with self.assertLogs("agent.core.study_anchors", level="WARNING"):
            self.assertEqual(anchors.load(), 1)

    def test_unreadable_file_raises_a_readable_error(self):
        anchors = sa.StudyAnchors(self.tmp)          # 目录当文件读
        with self.assertRaises(sa.StudyAnchorError):
            anchors.load()

    def test_undecodable_vector_is_skipped_not_fatal(self):
        rows = [{"cls": "code", "vector": "!!!not-base64!!!", "shot": "", "source": "auto",
                 "note": "", "created_at": "2026-01-01T00:00:00"},
                {"cls": "doc", "vector": sa.encode(vector(1)), "shot": "", "source": "auto",
                 "note": "", "created_at": "2026-01-01T00:00:00"}]
        self.write_lines(rows)
        anchors = self.make_anchors()
        anchors.load()
        with self.assertLogs("agent.core.study_anchors", level="WARNING"):
            pairs = anchors.vectors()
        self.assertEqual([cls for cls, _ in pairs], ["doc"])

    def test_rows_are_copies(self):
        anchors = self.make_anchors()
        anchors.add(cls="code", vector=vector(1))
        rows = anchors.rows()
        rows[0]["cls"] = "anime"
        self.assertEqual(anchors.rows()[0]["cls"], "code")


class TestAdd(TempDirCase):
    def test_add_appends_one_line_and_reloads_the_same(self):
        anchors = self.make_anchors()
        anchors.add(cls="code", vector=vector(1), note="进程说是 pycharm64",
                    source="process", when=1700000000)
        rows = self.read_lines()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cls"], "code")
        self.assertEqual(rows[0]["note"], "进程说是 pycharm64")
        self.assertEqual(rows[0]["source"], "process")
        self.assertTrue(rows[0]["created_at"].startswith("2023-11-1"))

        other = self.make_anchors()
        self.assertEqual(other.load(), 1)
        self.assertEqual(other.counts(), {"code": 1})

    def test_unknown_class_is_refused_with_a_pointer_to_the_config(self):
        anchors = self.make_anchors()
        with self.assertRaises(sa.StudyAnchorError) as caught:
            anchors.add(cls="mystery", vector=vector(1))
        self.assertIn("study.classes", str(caught.exception))
        self.assertEqual(len(anchors), 0)

    def test_unknown_class_can_be_allowed_on_purpose(self):
        anchors = self.make_anchors(allow_unknown_classes=True)
        anchors.add(cls="mystery", vector=vector(1))
        self.assertEqual(anchors.counts(), {"mystery": 1})

    def test_empty_class_or_vector_is_refused(self):
        anchors = self.make_anchors()
        with self.assertRaises(sa.StudyAnchorError):
            anchors.add(cls="  ", vector=vector(1))
        with self.assertRaises(sa.StudyAnchorError):
            anchors.add(cls="code", vector=[])

    def test_write_failure_rolls_back_memory(self):
        """写不进去时内存里那份也要退回去（不能"库里有、文件里没有"）。"""
        anchors = sa.StudyAnchors(self.tmp, shot_dir=self.shot_dir)   # 目录当文件写
        with self.assertRaises(sa.StudyAnchorError):
            anchors.add(cls="code", vector=vector(1))
        self.assertEqual(len(anchors), 0)

    def test_counts_are_sorted_by_class(self):
        anchors = self.make_anchors()
        anchors.add(cls="real", vector=vector(1))
        anchors.add(cls="code", vector=vector(1))
        anchors.add(cls="code", vector=vector(0, 1))
        self.assertEqual(anchors.counts(), {"code": 2, "real": 1})
        self.assertEqual(anchors.class_names(), ["code", "real"])
        self.assertEqual(anchors.configured_classes(),
                         ["anime", "code", "doc", "game", "real"])


class TestCap(TempDirCase):
    """每类上限（你定的"别长成无限大"）—— 内存和**文件**都得丢。"""

    def test_oldest_is_dropped_and_the_file_shrinks_too(self):
        anchors = self.make_anchors(max_per_class=3)
        for index in range(5):
            anchors.add(cls="code", vector=vector(1, index), note="a%d" % index)
        self.assertEqual(anchors.counts(), {"code": 3})
        self.assertEqual([row["note"] for row in anchors.rows()], ["a2", "a3", "a4"])
        self.assertEqual(len(self.read_lines()), 3, "文件里也得丢掉最旧的，否则重启又超上限")

        reloaded = self.make_anchors(max_per_class=3)
        reloaded.load()
        self.assertEqual([row["note"] for row in reloaded.rows()], ["a2", "a3", "a4"])

    def test_cap_is_per_class_not_a_total(self):
        anchors = self.make_anchors(max_per_class=2)
        for index in range(3):
            anchors.add(cls="code", vector=vector(1, index), note="c%d" % index)
            anchors.add(cls="anime", vector=vector(0, 1, index), note="g%d" % index)
        self.assertEqual(anchors.counts(), {"anime": 2, "code": 2})
        self.assertEqual(len(self.read_lines()), 4)

    def test_prune_returns_how_many_were_dropped(self):
        anchors = self.make_anchors(max_per_class=10)
        for index in range(4):
            anchors.add(cls="code", vector=vector(1, index))
        self.assertEqual(anchors.prune("code", keep=2), 2)
        self.assertEqual(anchors.counts(), {"code": 2})

    def test_prune_with_nothing_to_do_touches_no_file(self):
        anchors = self.make_anchors(max_per_class=10)
        anchors.add(cls="code", vector=vector(1))
        before = os.path.getmtime(self.anchor_file)
        self.assertEqual(anchors.prune(), 0)
        self.assertEqual(os.path.getmtime(self.anchor_file), before)


class TestReset(TempDirCase):
    def test_reset_one_class_removes_only_that_class(self):
        anchors = self.make_anchors()
        anchors.add(cls="code", vector=vector(1))
        anchors.add(cls="doc", vector=vector(0, 1))
        anchors.add(cls="anime", vector=vector(0, 0, 1))
        self.assertEqual(anchors.reset("code"), 1)
        self.assertEqual(anchors.counts(), {"anime": 1, "doc": 1})
        self.assertEqual(sorted(row["cls"] for row in self.read_lines()), ["anime", "doc"])

    def test_reset_all_empties_the_file(self):
        anchors = self.make_anchors()
        anchors.add(cls="code", vector=vector(1))
        anchors.add(cls="anime", vector=vector(0, 1))
        self.assertEqual(anchors.reset(), 2)
        self.assertEqual(len(anchors), 0)
        self.assertEqual(self.read_lines(), [])

    def test_reset_of_an_absent_class_is_a_no_op(self):
        anchors = self.make_anchors()
        anchors.add(cls="code", vector=vector(1))
        self.assertEqual(anchors.reset("doc"), 0)
        self.assertEqual(len(self.read_lines()), 1)


# ===========================================================================
#  匹配
# ===========================================================================
class TestMatch(TempDirCase):
    def test_empty_library_gives_nothing(self):
        anchors = self.make_anchors()
        self.assertIsNone(anchors.match(vector(1)))
        self.assertIsNone(anchors.match([]))

    def test_the_nearest_anchor_wins_and_margin_is_across_classes(self):
        anchors = self.make_anchors()
        anchors.add(cls="code", vector=vector(1, 0))
        anchors.add(cls="code", vector=vector(1, 0.1))     # 同类不算对手
        anchors.add(cls="anime", vector=vector(0, 1))
        hit = anchors.match(vector(1, 0))
        self.assertEqual(hit["cls"], "code")
        self.assertGreater(hit["score"], 0.99)
        self.assertEqual(hit["runner_up_cls"], "anime")
        self.assertGreater(hit["margin"], 0.9)
        self.assertEqual(hit["category"], "study")
        self.assertEqual(hit["anchors"], 2)
        self.assertEqual(hit["method"], "anchor")

    def test_category_aggregation_ignores_same_category_siblings(self):
        """学习帧同时像 code 和 doc —— 那不是"分不开"，它们同一个大类。"""
        anchors = self.make_anchors()
        anchors.add(cls="code", vector=vector(1, 0))
        anchors.add(cls="doc", vector=vector(0.9, 0.1))
        anchors.add(cls="anime", vector=vector(0, 1))
        hit = anchors.match(vector(1, 0))
        self.assertEqual(hit["cls"], "code")
        self.assertEqual(hit["runner_up_cls"], "doc")
        self.assertLess(hit["margin"], 0.1)                # 子标签之间确实很近
        self.assertEqual(hit["category"], "study")
        self.assertEqual(hit["category_runner_up"], "not_study")
        self.assertGreater(hit["category_margin"], 0.5)    # 但大类分得很开

    def test_categories_are_read_from_the_configured_mapping(self):
        """把 anime 配成学习 -> 同一帧的大类结论就跟着变（映射是真源）。"""
        anchors = self.make_anchors(classes={"anime": "study"})
        anchors.add(cls="anime", vector=vector(0, 1))
        hit = anchors.match(vector(0, 1))
        self.assertEqual(hit["category"], "study")

    def test_prototype_method_uses_class_centroids(self):
        anchors = self.make_anchors()
        anchors.add(cls="code", vector=vector(1, 0))
        anchors.add(cls="code", vector=vector(0.8, 0.2))
        anchors.add(cls="anime", vector=vector(0, 1))
        hit = anchors.match(vector(0.9, 0.1), method="prototype")
        self.assertEqual(hit["method"], "prototype")
        self.assertEqual(hit["cls"], "code")
        self.assertEqual(hit["category"], "study")
        self.assertEqual(hit["anchors"], 2)

    def test_unknown_method_falls_back_to_per_anchor(self):
        anchors = self.make_anchors()
        anchors.add(cls="code", vector=vector(1))
        self.assertEqual(anchors.match(vector(1), method="???").get("method"), "anchor")

    def test_rows_outside_the_mapping_do_not_create_a_category(self):
        rows = [{"cls": "mystery", "vector": sa.encode(vector(1)), "shot": "", "source": "auto",
                 "note": "", "created_at": "2026-01-01T00:00:00"}]
        self.write_lines(rows)
        anchors = self.make_anchors()
        anchors.load()
        with self.assertLogs("agent.core.study_anchors", level="WARNING"):
            hit = anchors.match(vector(1))
        self.assertEqual(hit["cls"], "mystery")
        self.assertEqual(hit["category"], "")              # 不猜
        self.assertEqual(hit["category_score"], 0.0)

    def test_one_category_alone_has_zero_runner_up(self):
        anchors = self.make_anchors()
        anchors.add(cls="code", vector=vector(1))
        hit = anchors.match(vector(1))
        self.assertEqual(hit["category_runner_up"], "")
        self.assertEqual(hit["category_margin"], hit["category_score"])


class TestPrototypes(TempDirCase):
    def test_prototype_is_the_mean_of_its_anchors(self):
        anchors = self.make_anchors()
        anchors.add(cls="code", vector=vector(1, 0, 0))
        anchors.add(cls="code", vector=vector(0, 1, 0))
        proto = anchors.prototypes()["code"]
        self.assertAlmostEqual(proto[0], 0.5, places=3)
        self.assertAlmostEqual(proto[1], 0.5, places=3)
        self.assertAlmostEqual(proto[2], 0.0, places=3)

    def test_prototypes_are_cached_but_invalidated_by_add(self):
        anchors = self.make_anchors()
        anchors.add(cls="code", vector=vector(1, 0))
        first = anchors.prototypes()["code"]
        anchors.add(cls="code", vector=vector(0, 1))
        second = anchors.prototypes()["code"]
        self.assertAlmostEqual(first[0], 1.0, places=3)
        self.assertAlmostEqual(second[0], 0.5, places=3)

    def test_prototypes_are_copies(self):
        anchors = self.make_anchors()
        anchors.add(cls="code", vector=vector(1))
        got = anchors.prototypes()
        got["code"][0] = 999.0
        self.assertAlmostEqual(anchors.prototypes()["code"][0], 1.0, places=3)

    def test_empty_library_has_no_prototypes(self):
        self.assertEqual(self.make_anchors().prototypes(), {})


# ===========================================================================
#  截图
# ===========================================================================
class TestShots(TempDirCase):
    def test_shot_is_saved_under_the_class_folder(self):
        anchors = self.make_anchors()
        when = 1700000000
        row = anchors.add(cls="code", vector=vector(1), shot=b"fake-jpeg", when=when)
        path = shot_path(row)
        self.assertTrue(os.path.isfile(path), path)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(when))
        self.assertTrue(path.replace("\\", "/").endswith("shots/code/%s.jpg" % stamp), path)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"fake-jpeg")

    def test_keep_shots_false_records_only_the_vector(self):
        anchors = self.make_anchors(keep_shots=False)
        row = anchors.add(cls="code", vector=vector(1), shot=b"fake-jpeg")
        self.assertEqual(row["shot"], "")
        self.assertFalse(os.path.exists(self.shot_dir))

    def test_shot_write_failure_does_not_lose_the_anchor(self):
        """截图存不下来只该记一句 —— 锚点本身（向量）仍然要落盘。"""
        blocker = os.path.join(self.tmp, "blocked")
        with open(blocker, "w", encoding="utf-8") as handle:
            handle.write("我是文件，不是目录")
        anchors = self.make_anchors(shot_dir=blocker)
        with self.assertLogs("agent.core.study_anchors", level="WARNING"):
            row = anchors.add(cls="code", vector=vector(1), shot=b"fake-jpeg")
        self.assertEqual(row["shot"], "")
        self.assertEqual(len(self.read_lines()), 1)

    def test_snapshot_lists_what_the_library_holds(self):
        anchors = self.make_anchors()
        anchors.add(cls="code", vector=vector(1))
        anchors.add(cls="anime", vector=vector(0, 1))
        snap = anchors.snapshot()
        self.assertEqual(snap["anchors"], 2)
        self.assertEqual(snap["counts"], {"anime": 1, "code": 1})
        self.assertEqual(snap["categories"], {"study": 1, "not_study": 1})
        self.assertEqual(snap["max_per_class"], sa.DEFAULT_MAX_PER_CLASS)
        self.assertEqual(snap["file"], self.anchor_file)


# ===========================================================================
#  统计（有界 + 坏文件不拖垮功能）
# ===========================================================================
class TestStatsFile(TempDirCase):
    def test_defaults_are_empty_and_versioned(self):
        stats = self.make_stats()
        data = stats.to_dict()
        self.assertEqual(data["version"], ss.SCHEMA_VERSION)
        self.assertEqual(data["thresholds"], {})
        self.assertEqual(data["counters"], {})
        self.assertEqual(data["samples"], [])
        self.assertEqual(list(data["histogram"]), list(ss.CATEGORY_KEYS))
        self.assertEqual(len(data["histogram"]["study"]), ss.HISTOGRAM_BUCKETS)

    def test_save_and_load_round_trip(self):
        stats = self.make_stats()
        stats.set_threshold("confident_score", 0.8)
        stats.set_threshold("confident_margin", 0.03)
        stats.bump("frames", 3)
        stats.observe("study", 0.93)
        stats.record(cls="code", category="study", score=0.93, margin=0.05,
                     verdict="study", action="")
        stats.note("初值来自板端标定")
        stats.save()

        other = self.make_stats()
        other.load()
        self.assertEqual(other.threshold("confident_score"), 0.8)
        self.assertEqual(other.threshold("confident_margin"), 0.03)
        self.assertEqual(other.counter("frames"), 3)
        self.assertEqual(sum(other.histogram()["study"]), 1)
        self.assertEqual(other.last_sample()["cls"], "code")
        self.assertEqual(other.notes()[-1]["text"], "初值来自板端标定")

    def test_saved_file_is_readable_json_with_no_leftover_temp(self):
        stats = self.make_stats()
        stats.set_threshold("learn_score", 0.9)
        stats.save()
        with open(self.stats_file, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertIn("thresholds", data)
        leftovers = [name for name in os.listdir(self.tmp) if name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_missing_file_is_not_an_error(self):
        self.assertEqual(self.make_stats().load()["counters"], {})

    def test_broken_file_starts_from_defaults_instead_of_raising(self):
        with open(self.stats_file, "w", encoding="utf-8") as handle:
            handle.write("{这不是 JSON")
        stats = self.make_stats()
        with self.assertLogs("agent.core.study_stats", level="WARNING"):
            stats.load()
        self.assertEqual(stats.counters(), {})
        self.assertEqual(stats.thresholds(), {})

    def test_non_object_file_starts_from_defaults(self):
        with open(self.stats_file, "w", encoding="utf-8") as handle:
            handle.write("[1, 2, 3]")
        stats = self.make_stats()
        with self.assertLogs("agent.core.study_stats", level="WARNING"):
            stats.load()
        self.assertEqual(stats.counters(), {})

    def test_a_damaged_histogram_is_repaired_to_the_right_length(self):
        with open(self.stats_file, "w", encoding="utf-8") as handle:
            json.dump({"version": ss.SCHEMA_VERSION, "histogram": {"study": [1, "x", 3, 4]},
                       "thresholds": {"confident_score": "nope"}}, handle)
        stats = self.make_stats()
        with self.assertLogs("agent.core.study_stats", level="WARNING"):
            stats.load()
        self.assertEqual(len(stats.histogram()["study"]), ss.HISTOGRAM_BUCKETS)
        self.assertEqual(stats.histogram()["study"][:4], [1, 0, 3, 4])
        self.assertEqual(stats.thresholds(), {})

    def test_unknown_version_is_a_warning_not_a_failure(self):
        with open(self.stats_file, "w", encoding="utf-8") as handle:
            json.dump({"version": 99, "counters": {"frames": 2}}, handle)
        stats = self.make_stats()
        with self.assertLogs("agent.core.study_stats", level="WARNING"):
            stats.load()
        self.assertEqual(stats.counter("frames"), 2)

    def test_a_directory_instead_of_a_file_raises_on_save_only(self):
        stats = ss.StudyStats(self.tmp)
        stats.bump("frames")
        with self.assertRaises(OSError):
            stats.save()


class TestStatsThresholds(TempDirCase):
    def test_set_get_and_update(self):
        stats = self.make_stats()
        stats.set_threshold("confident_score", 0.8)
        stats.update_thresholds({"confident_margin": 0.03, "learn_score": 0.9})
        self.assertEqual(stats.threshold("confident_score"), 0.8)
        self.assertEqual(stats.threshold("confident_margin"), 0.03)
        self.assertEqual(stats.threshold("learn_score"), 0.9)
        self.assertIsNone(stats.threshold("nope"))
        self.assertEqual(stats.threshold("nope", 0.5), 0.5)

    def test_non_finite_or_non_numeric_values_are_refused(self):
        stats = self.make_stats()
        for bad in (float("nan"), float("inf"), "abc", None):
            with self.assertRaises(ValueError):
                stats.set_threshold("confident_score", bad)
        self.assertEqual(stats.thresholds(), {})

    def test_a_batch_is_all_or_nothing(self):
        """半套阈值比旧值更危险：一条不合法 -> 整批都不落。"""
        stats = self.make_stats()
        stats.set_threshold("confident_score", 0.8)
        with self.assertRaises(ValueError):
            stats.update_thresholds({"confident_margin": 0.03, "learn_score": "x"})
        self.assertEqual(stats.thresholds(), {"confident_score": 0.8})

    def test_threshold_count_is_capped(self):
        stats = self.make_stats(max_thresholds=2)
        stats.set_threshold("a", 0.1)
        stats.set_threshold("b", 0.2)
        with self.assertRaises(ValueError):
            stats.set_threshold("c", 0.3)

    def test_updating_an_existing_threshold_is_always_allowed(self):
        stats = self.make_stats(max_thresholds=1)
        stats.set_threshold("a", 0.1)
        stats.set_threshold("a", 0.2)
        self.assertEqual(stats.threshold("a"), 0.2)

    def test_reset_keeps_thresholds_by_default(self):
        stats = self.make_stats()
        stats.set_threshold("confident_score", 0.8)
        stats.bump("frames", 5)
        stats.reset()
        self.assertEqual(stats.threshold("confident_score"), 0.8)
        self.assertEqual(stats.counters(), {})

    def test_reset_can_drop_thresholds_too(self):
        stats = self.make_stats()
        stats.set_threshold("confident_score", 0.8)
        stats.reset(keep_thresholds=False)
        self.assertEqual(stats.thresholds(), {})


class TestStatsCounters(TempDirCase):
    def test_bump_accumulates_and_reports_the_new_value(self):
        stats = self.make_stats()
        self.assertEqual(stats.bump("frames"), 1)
        self.assertEqual(stats.bump("frames"), 2)
        self.assertEqual(stats.bump("reminded", 3), 3)
        self.assertEqual(stats.counter("nope"), 0)

    def test_a_typo_cannot_grow_the_file_forever(self):
        stats = self.make_stats(max_counters=2)
        stats.bump("frames")
        stats.bump("verdicts")
        with self.assertLogs("agent.core.study_stats", level="WARNING"):
            stats.bump("typo-name")
        self.assertEqual(sorted(stats.counters()), ["frames", "verdicts"])
        self.assertEqual(stats.counter("typo-name"), 0)

    def test_existing_counters_keep_counting_at_the_cap(self):
        stats = self.make_stats(max_counters=1)
        stats.bump("frames")
        self.assertEqual(stats.bump("frames", 2), 3)


class TestStatsHistogram(TempDirCase):
    def test_bucket_boundaries(self):
        self.assertEqual(ss.bucket_of(0.0), 0)
        self.assertEqual(ss.bucket_of(0.049), 0)
        self.assertEqual(ss.bucket_of(0.05), 1)
        self.assertEqual(ss.bucket_of(0.999), ss.HISTOGRAM_BUCKETS - 1)
        self.assertEqual(ss.bucket_of(1.0), ss.HISTOGRAM_BUCKETS - 1)
        self.assertEqual(ss.bucket_of(-1.0), 0)
        self.assertEqual(ss.bucket_of(float("nan")), 0)

    def test_observe_returns_the_bucket(self):
        stats = self.make_stats()
        self.assertEqual(stats.observe("study", 0.05), 1)
        self.assertEqual(stats.histogram()["study"][1], 1)
        self.assertEqual(sum(stats.histogram()["not_study"]), 0)

    def test_unknown_category_lands_in_the_unknown_bucket(self):
        stats = self.make_stats()
        with self.assertLogs("agent.core.study_stats", level="WARNING"):
            stats.observe("leisure", 0.5)
        self.assertEqual(sum(stats.histogram()["unknown"]), 1)

    def test_quantile_of_an_empty_category_is_none(self):
        self.assertIsNone(self.make_stats().quantile("study", 0.05))

    def test_quantile_reads_back_the_distribution(self):
        stats = self.make_stats()
        for _ in range(9):
            stats.observe("study", 0.82)          # 第 16 档 (0.80~0.85)
        stats.observe("study", 0.12)              # 第 2 档
        low = stats.quantile("study", 0.05)
        high = stats.quantile("study", 0.95)
        self.assertLess(low, high)
        self.assertAlmostEqual(high, 0.825, places=3)
        self.assertAlmostEqual(low, 0.125, places=3)


class TestStatsRingBuffers(TempDirCase):
    def test_notes_keep_only_the_newest(self):
        stats = self.make_stats(max_notes=3)
        for index in range(5):
            stats.note("第 %d 次调整" % index)
        self.assertEqual([item["text"] for item in stats.notes()],
                         ["第 2 次调整", "第 3 次调整", "第 4 次调整"])

    def test_samples_keep_only_the_newest(self):
        stats = self.make_stats(max_samples=2)
        for index in range(4):
            stats.record(cls="code", score=index / 10.0)
        self.assertEqual([item["score"] for item in stats.samples()], [0.2, 0.3])
        self.assertEqual(stats.last_sample()["score"], 0.3)

    def test_last_sample_is_none_when_nothing_was_recorded(self):
        self.assertIsNone(self.make_stats().last_sample())

    def test_notes_and_samples_survive_a_round_trip(self):
        stats = self.make_stats(max_samples=5)
        stats.record(cls="anime", category="not_study", score=0.7, margin=0.1,
                     verdict="not_study", action="remind", when=1700000000)
        stats.note("提醒了一次", when=1700000001)
        stats.save()

        other = self.make_stats()
        other.load()
        self.assertEqual(other.last_sample()["action"], "remind")
        self.assertEqual(other.notes()[0]["text"], "提醒了一次")

    def test_snapshot_shape(self):
        stats = self.make_stats()
        stats.bump("frames")
        snap = stats.snapshot()
        self.assertEqual(snap["file"], self.stats_file)
        self.assertEqual(snap["counters"], {"frames": 1})
        self.assertEqual(snap["samples"], 0)
        self.assertEqual(snap["notes"], 0)


# ===========================================================================
#  派生数据不入库
# ===========================================================================
class TestGitignore(TempDirCase):
    """这两份都是**运行期派生数据** —— 忘进 .gitignore 就会把板端的学习记录提交上去。"""

    def test_the_two_new_files_are_ignored(self):
        with open(os.path.join(REPO, ".gitignore"), encoding="utf-8") as handle:
            text = handle.read()
        for pattern in ("config/study_anchors.jsonl", "config/study_anchors/",
                        "config/study_stats.json"):
            self.assertIn(pattern, text, ".gitignore 里少了 %s" % pattern)


if __name__ == "__main__":
    unittest.main(verbosity=2)
