#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_wall_data.py — 壁纸标签数据文件 + 词表 + `assistant tag` 的计划逻辑（T7-2）

跑法:
    python tests/test_wall_data.py

为什么这些能在**开发机**上跑（没有 numpy / 没有 NPU）
    这一层刻意做成纯 Python:
      · `wall_data.py` 用 `struct` 的 float16 打包向量（不 import numpy）
      · `tag_vocab.py` 只是数据与指纹
      · `assistant tag` 的 dry-run 只算"哪些图要打"，不读图片内容
    真正要 NPU 的只有 `tagger.py`（打标签那一刻），那部分在板端验收里跑。

覆盖
    · 词表: 三轴、英文标签唯一、指纹随内容变、配置追加式覆盖
    · 数据文件: 建记录 / 写读往返 / 原子写 + .bak / 坏行不让整个文件读不出来
    · 向量编解码: float16 base64 往返精度、维度校验、nan/inf 拒绝、坏 base64 拒绝
    · 增量计划: 新图 / 图变了 / 模型变了 / 词表变了 / 强制 / 孤儿行 / prune
    · 摘要: 每轴标签计数（给 `list_wallpaper_tags` 用）
"""

import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.vision import tag_vocab, wall_data  # noqa: E402


def _f32(value: float) -> float:
    """把这个 float64 舍入成 float32 能精确表示的值（模拟"来自 numpy float32"）。"""
    return struct.unpack("<f", struct.pack("<f", value))[0]


def make_dir(*names):
    root = tempfile.mkdtemp(prefix="walldata_")
    for name in names:
        with open(os.path.join(root, name), "wb") as handle:
            handle.write(b"\x89PNG\r\n\x1a\n" + name.encode("utf-8"))
    return root


def make_record(path, **over):
    base = dict(
        width=1280, height=800, size=42, sha256=wall_data.image_sha256(path),
        ms=1234, model_sha8="aaaa1111", vocab_sha8="bbbb2222",
        tags={"scene": [("landscape", 0.21), ("city", 0.08)],
              "tone": [("dark", 0.15)]},
        embedding=[0.0] * wall_data.EMBED_DIM,
    )
    base.update(over)
    return wall_data.make_record(path, **base)


# ------------------------------------------------------------------ 词表 ---
class TestVocab(unittest.TestCase):
    def test_three_axes_with_expected_names(self):
        self.assertEqual([axis.name for axis in tag_vocab.AXES], ["scene", "tone", "mood"])
        for axis in tag_vocab.AXES:
            self.assertTrue(axis.title, axis.name)
            self.assertGreaterEqual(len(axis.labels), 8, "%s 的候选太少" % axis.name)

    def test_labels_are_unique_and_ascii(self):
        for axis in tag_vocab.AXES:
            texts = axis.texts
            self.assertEqual(len(texts), len(set(texts)), "%s 有重复标签" % axis.name)
            for text in texts:
                self.assertTrue(text.isascii(), "%s 的标签必须是英文裸串: %r" % (axis.name, text))
                self.assertEqual(text, text.strip().lower())

    def test_every_label_has_a_chinese_name(self):
        for axis in tag_vocab.AXES:
            for label in axis.labels:
                self.assertTrue(label.zh and label.zh != label.en,
                                "%s/%s 缺中文名" % (axis.name, label.en))

    def test_fingerprint_changes_with_the_vocab(self):
        base = tag_vocab.vocab_sha8()
        self.assertEqual(base, tag_vocab.vocab_sha8(), "同一份词表必须得到同一指纹")
        grown = tag_vocab.with_overrides({"scene": ["cyberpunk"]})
        self.assertNotEqual(base, tag_vocab.vocab_sha8(grown), "加了标签指纹就该变")
        reordered = tag_vocab.with_overrides({"scene": []})
        self.assertEqual(base, tag_vocab.vocab_sha8(reordered), "空追加不该改指纹")

    def test_overrides_append_per_axis(self):
        axes = tag_vocab.with_overrides({"tone": ["sepia", "  ", "dark"]})
        tone = [axis for axis in axes if axis.name == "tone"][0]
        self.assertIn("sepia", tone.texts)
        self.assertEqual(tone.texts.count("dark"), 1, "重复标签不该加第二遍")
        self.assertEqual(len(axes), 3)
        self.assertNotIn("sepia", tag_vocab.axis_texts()["tone"], "默认词表不该被改")

    def test_unknown_axis_and_bad_overrides_are_ignored(self):
        self.assertEqual(len(tag_vocab.with_overrides({"nope": ["x"]})), 3)
        self.assertEqual(len(tag_vocab.with_overrides(None)), 3)
        self.assertEqual(len(tag_vocab.with_overrides("not a mapping")), 3)

    def test_label_zh_lookup(self):
        self.assertEqual(tag_vocab.label_zh("scene", "landscape"), "风景")
        self.assertEqual(tag_vocab.label_zh("scene", "nonexistent"), "nonexistent")
        self.assertEqual(tag_vocab.label_zh("nope", "landscape"), "landscape")


# -------------------------------------------------------------- 数据文件 ---
class TestEmbeddingCodec(unittest.TestCase):
    def test_round_trip_keeps_float16_precision(self):
        vector = [(i % 100) / 100.0 - 0.5 for i in range(wall_data.EMBED_DIM)]
        text = wall_data.encode_embedding(vector)
        self.assertIsInstance(text, str)
        back = wall_data.decode_embedding(text)
        self.assertEqual(len(back), wall_data.EMBED_DIM)
        for original, restored in zip(vector, back):
            self.assertAlmostEqual(original, restored, places=2)   # float16 精度

    def test_size_of_one_vector(self):
        text = wall_data.encode_embedding([0.0] * wall_data.EMBED_DIM)
        # 768 × 2 字节 → base64 ≈ 2048 字符（一张图约 2KB，40 张 ≈ 80KB）
        self.assertLessEqual(len(text), 2100)

    def test_rejects_wrong_dimension_and_non_finite(self):
        with self.assertRaises(wall_data.WallDataError):
            wall_data.encode_embedding([0.0] * 10)
        for bad in (float("nan"), float("inf")):
            vector = [0.0] * wall_data.EMBED_DIM
            vector[3] = bad
            with self.assertRaises(wall_data.WallDataError):
                wall_data.encode_embedding(vector)

    def test_rejects_bad_base64_and_wrong_length(self):
        with self.assertRaises(wall_data.WallDataError):
            wall_data.decode_embedding("not base64!!")
        with self.assertRaises(wall_data.WallDataError):
            wall_data.decode_embedding("")
        with self.assertRaises(wall_data.WallDataError):
            wall_data.decode_embedding("AAAA")            # 长度不对


class TestRecords(unittest.TestCase):
    def setUp(self):
        self.dir = make_dir("01_a.png", "02_b.png")
        self.data = os.path.join(tempfile.mkdtemp(prefix="walldata_out_"), "wall_data.jsonl")

    def test_make_record_shape(self):
        record = make_record(os.path.join(self.dir, "01_a.png"))
        self.assertEqual(record["version"], wall_data.RECORD_VERSION)
        for key in ("path", "w", "h", "bytes", "sha256", "tagged_at", "ms",
                    "model_sha8", "vocab_sha8", "tags", "embedding"):
            self.assertIn(key, record)
        self.assertEqual(record["tags"]["scene"][0], ["landscape", 0.21])

    def test_write_then_read_round_trip(self):
        record = make_record(os.path.join(self.dir, "01_a.png"))
        wall_data.write_records(self.data, [record])
        records, problems = wall_data.read_records(self.data)
        self.assertEqual(problems, [])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["path"], record["path"])
        self.assertEqual(records[0]["tags"], record["tags"])
        self.assertEqual(wall_data.decode_embedding(records[0]["embedding"]),
                         wall_data.decode_embedding(record["embedding"]))

    def test_missing_file_is_an_empty_table_not_an_error(self):
        records, problems = wall_data.read_records(os.path.join(self.dir, "nope.jsonl"))
        self.assertEqual((records, problems), ([], []))

    def test_bad_lines_do_not_hide_the_good_ones(self):
        good = make_record(os.path.join(self.dir, "01_a.png"))
        with open(self.data, "w", encoding="utf-8") as handle:
            handle.write("{not json}\n")
            handle.write(json.dumps(good, ensure_ascii=False) + "\n")
            handle.write('{"no_path": 1}\n')
            handle.write("\n")
        records, problems = wall_data.read_records(self.data)
        self.assertEqual(len(records), 1, "好行必须还在")
        self.assertEqual(len(problems), 2, "两个坏行各记一句")

    def test_backup_is_left_beside_the_file(self):
        record = make_record(os.path.join(self.dir, "01_a.png"))
        wall_data.write_records(self.data, [record])
        first = open(self.data, encoding="utf-8").read()
        wall_data.write_records(self.data, [record, make_record(os.path.join(self.dir, "02_b.png"))])
        backup = open(self.data + ".bak", encoding="utf-8").read()
        self.assertEqual(backup, first, ".bak 是**上一次**的内容")

    def test_atomic_write_leaves_no_temp_files(self):
        record = make_record(os.path.join(self.dir, "01_a.png"))
        wall_data.write_records(self.data, [record])
        leftovers = [name for name in os.listdir(os.path.dirname(self.data))
                     if name not in (os.path.basename(self.data),
                                     os.path.basename(self.data) + ".bak")]
        self.assertEqual(leftovers, [], "写完不该留下临时文件")

    def test_unserializable_record_is_refused(self):
        with self.assertRaises(wall_data.WallDataError):
            wall_data.write_records(self.data, [{"path": "/x", "bad": {1, 2, 3}}])


class TestPlan(unittest.TestCase):
    def setUp(self):
        self.dir = make_dir("01_a.png", "02_b.png", "03_c.png")
        self.a = os.path.join(self.dir, "01_a.png")
        self.b = os.path.join(self.dir, "02_b.png")
        self.c = os.path.join(self.dir, "03_c.png")
        self.images = [self.a, self.b, self.c]
        self.records = [make_record(self.a), make_record(self.b)]
        self.model8, self.vocab8 = "aaaa1111", "bbbb2222"

    def test_only_the_new_image_needs_tagging(self):
        plan = wall_data.tag_plan(self.records, self.images, self.model8, self.vocab8)
        self.assertEqual([item["path"] for item in plan["to_tag"]], [self.c])
        self.assertEqual(plan["to_tag"][0]["reason"], "新图")
        self.assertEqual(plan["fresh"], 2)
        self.assertEqual(plan["orphans"], [])

    def test_changed_image_is_detected(self):
        with open(self.a, "ab") as handle:
            handle.write(b"changed")
        plan = wall_data.tag_plan(self.records, self.images, self.model8, self.vocab8)
        self.assertEqual([item["reason"] for item in plan["to_tag"]], ["图变了", "新图"])

    def test_changed_model_or_vocab_invalidates_everything(self):
        for over in ({"model_sha8": "9999ffff"}, {"vocab_sha8": "9999ffff"}):
            plan = wall_data.tag_plan(self.records, self.images,
                                      over.get("model_sha8", self.model8),
                                      over.get("vocab_sha8", self.vocab8))
            reasons = {item["reason"] for item in plan["to_tag"]}
            self.assertTrue(reasons & {"模型变了", "词表变了"}, reasons)

    def test_force_retags_everything(self):
        plan = wall_data.tag_plan(self.records, self.images, self.model8, self.vocab8,
                                  force=True)
        self.assertEqual(len(plan["to_tag"]), 3)
        self.assertEqual(plan["fresh"], 0)
        # 已有的两张是"强制重打"；没打过的那张原因仍是"新图"（更准确）
        self.assertEqual([item["reason"] for item in plan["to_tag"]],
                         ["强制重打", "强制重打", "新图"])

    def test_old_format_version_is_retagged(self):
        records = [dict(self.records[0], version=0), self.records[1]]
        plan = wall_data.tag_plan(records, self.images, self.model8, self.vocab8)
        self.assertEqual(plan["to_tag"][0]["reason"], "格式变了")

    def test_orphan_rows_are_reported(self):
        records = self.records + [make_record(self.c)]
        plan = wall_data.tag_plan(records, [self.a, self.b], self.model8, self.vocab8)
        self.assertEqual(plan["orphans"], [self.c])

    def test_prune_keeps_existing_only(self):
        kept, dropped = wall_data.prune_records(self.records, [self.a])
        self.assertEqual([item["path"] for item in kept], [self.a])
        self.assertEqual(dropped, [self.b])


class TestSummarise(unittest.TestCase):
    def test_counts_labels_per_axis(self):
        root = make_dir("01_a.png", "02_b.png")
        records = [
            make_record(os.path.join(root, "01_a.png"),
                        tags={"scene": [("landscape", 0.2), ("city", 0.1)],
                              "tone": [("dark", 0.3)]}),
            make_record(os.path.join(root, "02_b.png"),
                        tags={"scene": [("landscape", 0.25)], "tone": [("bright", 0.2)]}),
        ]
        summary = wall_data.summarise(records, axes=("scene", "tone"))
        self.assertEqual(summary["count"], 2)
        self.assertEqual(summary["axes"]["scene"], {"landscape": 2, "city": 1})
        self.assertEqual(summary["axes"]["tone"], {"dark": 1, "bright": 1})

    def test_empty_records(self):
        summary = wall_data.summarise([])
        self.assertEqual(summary["count"], 0)
        self.assertEqual(summary["axes"], {})


class TestVocabRecord(unittest.TestCase):
    """数据文件**第一行**的词表向量缓存（T7-2 追加）。"""

    def setUp(self):
        self.dir = make_dir("01_a.png", "02_b.png")
        self.data = os.path.join(tempfile.mkdtemp(prefix="walldata_vocab_"), "wall_data.jsonl")
        self.axes = tag_vocab.AXES
        self.vocab8 = tag_vocab.vocab_sha8(self.axes)
        self.embeds = {axis.name: [[float(i + j) / 100.0 for j in range(wall_data.EMBED_DIM)]
                                   for i, _ in enumerate(axis.labels)]
                       for axis in self.axes}

    def _vocab(self, **over):
        kwargs = dict(model_sha8="aaaa1111", vocab_sha8=self.vocab8)
        kwargs.update(over)
        return wall_data.make_vocab_record(self.axes, self.embeds, **kwargs)

    def test_record_shape(self):
        record = self._vocab()
        self.assertEqual(record["kind"], "vocab")
        self.assertEqual(record["version"], wall_data.VOCAB_RECORD_VERSION)
        self.assertEqual(record["dim"], wall_data.EMBED_DIM)
        self.assertEqual(list(record["axes"]), [a.name for a in self.axes])
        self.assertEqual(record["axes"]["scene"], tag_vocab.SCENE.texts)
        self.assertEqual(len(record["embeds"]["mood"]), len(tag_vocab.MOOD.labels))

    def test_mismatched_label_and_vector_counts_are_refused(self):
        bad = dict(self.embeds)
        bad["scene"] = bad["scene"][:-1]                     # 少一条向量
        with self.assertRaises(wall_data.WallDataError):
            wall_data.make_vocab_record(self.axes, bad, "aaaa1111", self.vocab8)

    def test_written_first_and_read_back(self):
        record = make_record(os.path.join(self.dir, "01_a.png"))
        wall_data.write_records(self.data, [record], vocab=self._vocab())
        first = open(self.data, encoding="utf-8").readline()
        self.assertIn('"kind": "vocab"', first, "词表记录必须在第一行")

        loaded = wall_data.load(self.data)
        self.assertEqual(loaded["problems"], [])
        self.assertEqual(len(loaded["records"]), 1, "图片记录不该把词表头算进去")
        self.assertEqual(loaded["vocab"]["model_sha8"], "aaaa1111")

        # 旧的读法（只看图片记录）照样能用，且不把词表头当问题报出来
        records, problems = wall_data.read_records(self.data)
        self.assertEqual(len(records), 1)
        self.assertEqual(problems, [])

    def test_second_vocab_line_is_reported_and_first_wins(self):
        record = make_record(os.path.join(self.dir, "01_a.png"))
        lines = [json.dumps(self._vocab(), ensure_ascii=False),
                 json.dumps(self._vocab(model_sha8="bbbb2222"), ensure_ascii=False),
                 json.dumps(record, ensure_ascii=False)]
        with open(self.data, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        loaded = wall_data.load(self.data)
        self.assertEqual(loaded["vocab"]["model_sha8"], "aaaa1111", "第一条说了算")
        self.assertEqual(len(loaded["problems"]), 1)
        self.assertIn("第二条", loaded["problems"][0])

    def test_matches_only_with_same_model_and_vocab_and_labels(self):
        record = self._vocab()
        self.assertTrue(wall_data.vocab_matches(record, "aaaa1111", self.vocab8, self.axes))
        self.assertFalse(wall_data.vocab_matches(record, "9999ffff", self.vocab8, self.axes),
                         "换模型就该重编")
        self.assertFalse(wall_data.vocab_matches(record, "aaaa1111", "9999ffff", self.axes),
                         "换词表就该重编")
        self.assertFalse(wall_data.vocab_matches(dict(record, version=0),
                                                 "aaaa1111", self.vocab8, self.axes))
        self.assertFalse(wall_data.vocab_matches(dict(record, dim=512),
                                                 "aaaa1111", self.vocab8, self.axes))
        self.assertFalse(wall_data.vocab_matches(None, "aaaa1111", self.vocab8, self.axes))
        self.assertFalse(wall_data.vocab_matches({}, "aaaa1111", self.vocab8, self.axes))

    def test_matches_rejects_hand_edited_axis_labels(self):
        # 指纹只保证"词表内容没变"，但头记录被人手改过时这里要兜住
        record = self._vocab()
        record["axes"]["scene"] = ["landscape", "city"]      # 标签列表被改短
        self.assertFalse(wall_data.vocab_matches(record, "aaaa1111", self.vocab8, self.axes))

    def test_vectors_round_trip_bit_exact_for_float32_inputs(self):
        vectors = wall_data.vocab_vectors(self._vocab(), self.axes)
        self.assertEqual(list(vectors), [a.name for a in self.axes])
        label, vec = vectors["tone"][0]
        self.assertEqual(label, tag_vocab.TONE.texts[0])
        # 生产路径上向量来自 numpy float32，`float()` 出来的就是 float32 能精确表示的值 ——
        # 那种值打包再解开必须**逐位相同**（这样复用缓存与现场重编码的分数完全一致）
        self.assertEqual(vec, [_f32(v) for v in self.embeds["tone"][0]])

    def test_broken_embedding_gives_none_instead_of_raising(self):
        record = self._vocab()
        record["embeds"]["scene"][0] = "not base64!!"
        self.assertIsNone(wall_data.vocab_vectors(record, self.axes))

    def test_float32_keeps_far_more_precision_than_float16(self):
        # ⚠ 输入是 float64 的 python 浮点，所以 float32 打包**也会**把它舍入一点点；
        #   要点是误差量级: float32 ≈1e-8（生产上等于无损），float16 ≈1e-5（差 3 个量级）
        values = [0.123456789] * wall_data.EMBED_DIM
        exact = wall_data.decode_vector(
            wall_data.encode_vector(values, dtype="float32"), dtype="float32")
        lossy = wall_data.decode_vector(
            wall_data.encode_vector(values, dtype="float16"), dtype="float16")
        err32 = abs(exact[0] - values[0])
        err16 = abs(lossy[0] - values[0])
        self.assertLess(err32, 1e-7, "float32 误差应当可忽略")
        self.assertGreater(err16, err32 * 100, "float16 应当明显更粗")
        self.assertGreater(err16, 1e-6)
        # 再来一遍必须稳定（幂等）—— 幂等才谈得上"复用缓存得到同样的分数"
        self.assertEqual(exact, wall_data.decode_vector(
            wall_data.encode_vector(exact, dtype="float32"), dtype="float32"))

    def test_unknown_dtype_is_refused(self):
        with self.assertRaises(wall_data.WallDataError):
            wall_data.encode_vector([0.0] * wall_data.EMBED_DIM, dtype="float64")


class TestDataFilePath(unittest.TestCase):
    def test_default_is_under_config(self):
        self.assertEqual(wall_data.DEFAULT_DATA_FILE, "config/wall_data.jsonl")
        self.assertTrue(wall_data.default_data_file().endswith(
            os.path.join("config", "wall_data.jsonl")))

    def test_relative_paths_resolve_against_the_repo_root(self):
        resolved = wall_data.resolve_data_file("config/other.jsonl")
        self.assertTrue(os.path.isabs(resolved))
        self.assertEqual(resolved, os.path.normpath(os.path.join(
            wall_data.repo_root(), "config", "other.jsonl")))

    def test_absolute_and_blank_values(self):
        # ⚠ 用平台自己的绝对路径（`/tmp/x.jsonl` 在 Windows 上不是绝对路径）
        absolute = os.path.join(tempfile.gettempdir(), "x.jsonl")
        self.assertEqual(wall_data.resolve_data_file(absolute),
                         os.path.normpath(absolute))
        self.assertEqual(wall_data.resolve_data_file(""), wall_data.default_data_file())
        self.assertEqual(wall_data.resolve_data_file(None), wall_data.default_data_file())

    def test_separators_are_normalised(self):
        # 混着两种分隔符的路径打印出来很难看，resolve 后必须是平台规范形式
        resolved = wall_data.resolve_data_file("config/wall_data.jsonl")
        self.assertNotIn("/", os.path.basename(os.path.dirname(resolved)))
        self.assertTrue(resolved.endswith(os.path.join("config", "wall_data.jsonl")))


# ------------------------------------------------------------------ 使用次数 ---
class TestUsage(unittest.TestCase):
    """T8-6: `used` / `last_used` —— "这张壁纸显示过几次"。

    为什么要它: 板端可以挑"用得最少的"那张（`next_wallpaper(sort="used_asc")`），
    也让 `action="tags"` 能回报使用情况。写入者仍只有本模块（`write_records()`）。
    """

    def setUp(self):
        self.dir = make_dir("01_a.png", "02_b.png")
        self.a = os.path.join(self.dir, "01_a.png")
        self.b = os.path.join(self.dir, "02_b.png")
        self.data = os.path.join(self.dir, "wall_data.jsonl")

    def test_missing_fields_mean_zero(self):
        # 老数据文件（没有这两个字段）照样能用
        record = {"path": self.a}
        self.assertEqual(wall_data.usage_of(record), {"used": 0, "last_used": None})
        self.assertEqual(wall_data.usage_of(None), {"used": 0, "last_used": None})
        self.assertEqual(wall_data.usage_of({"used": "很多", "last_used": ""}),
                         {"used": 0, "last_used": None})

    def test_make_record_writes_them(self):
        record = make_record(self.a)
        self.assertEqual(record["used"], 0)
        self.assertIsNone(record["last_used"])
        self.assertEqual(make_record(self.a, used=3, last_used="2026-09-25T10:00:00")["used"], 3)

    def test_bump_returns_a_copy_and_sets_the_time(self):
        record = make_record(self.a)
        bumped = wall_data.bump_usage(record, when="2026-09-25T10:00:00")
        self.assertEqual(bumped["used"], 1)
        self.assertEqual(bumped["last_used"], "2026-09-25T10:00:00")
        self.assertEqual(record["used"], 0, "不该改入参（调用方拿的是副本）")
        self.assertEqual(wall_data.bump_usage(bumped, when="x")["used"], 2)

    def test_with_usage_carries_the_count_over(self):
        # ⚠ 重打标签时用它把计数带过去（assistant tag 已经这么做）
        old = wall_data.bump_usage(make_record(self.a), when="t")
        fresh = make_record(self.a)                       # 假装的"新打的那行"
        carried = wall_data.with_usage(fresh, wall_data.usage_of(old))
        self.assertEqual(carried["used"], 1)
        self.assertEqual(carried["last_used"], "t")
        self.assertEqual(fresh["used"], 0, "不改入参")

    def test_bump_in_file_writes_and_returns_the_count(self):
        wall_data.write_records(self.data, [make_record(self.a), make_record(self.b)])
        self.assertEqual(wall_data.bump_usage_in_file(self.data, self.a,
                                                      when="2026-09-25T11:00:00"), 1)
        self.assertEqual(wall_data.bump_usage_in_file(self.data, self.a, when="t2"), 2)
        records = wall_data.read_records(self.data)[0]
        by_path = {r["path"]: r for r in records}
        self.assertEqual(by_path[self.a]["used"], 2)
        self.assertEqual(by_path[self.a]["last_used"], "t2")
        self.assertEqual(by_path[self.b]["used"], 0, "别的图不该被动到")

    def test_bump_in_file_keeps_the_vocab_header(self):
        vocab = {"kind": "vocab", "version": 1, "axes": {}, "embeds": {}}
        wall_data.write_records(self.data, [make_record(self.a)], vocab=vocab)
        wall_data.bump_usage_in_file(self.data, self.a)
        loaded = wall_data.load(self.data)
        self.assertIsNotNone(loaded["vocab"], "整文件重写不能把词表头弄丢")
        self.assertEqual(loaded["records"][0]["used"], 1)

    def test_bump_in_file_ignores_an_unknown_path(self):
        wall_data.write_records(self.data, [make_record(self.a)])
        with open(self.data, "r", encoding="utf-8") as handle:
            before = handle.read()
        self.assertIsNone(wall_data.bump_usage_in_file(self.data, self.b))
        with open(self.data, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), before, "文件里没有这张 -> 一个字节都不写")

    def test_usage_summary_orders_and_counts(self):
        records = [make_record(self.a, used=5, last_used="t5"),
                   make_record(self.b)]
        summary = wall_data.usage_summary(records, top=1)
        self.assertEqual(summary["total_used"], 5)
        self.assertEqual(summary["with_usage"], 1)
        self.assertEqual(summary["never_used"], 1)
        self.assertEqual(summary["least_used"][0]["path"], self.b)
        self.assertEqual(summary["most_used"][0]["path"], self.a)

    def test_usage_survives_a_round_trip(self):
        wall_data.write_records(self.data, [make_record(self.a, used=7, last_used="t7")])
        record = wall_data.read_records(self.data)[0][0]
        self.assertEqual(wall_data.usage_of(record), {"used": 7, "last_used": "t7"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
