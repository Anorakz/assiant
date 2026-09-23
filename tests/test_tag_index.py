#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_tag_index.py — 标签索引 + 锚点检索（Phase 7 T7-3）

跑法:
    python tests/test_tag_index.py

被测的是 `agent/vision/tag_index.py` —— Agent **挑图**那一层。它**纯 Python**
（不 import numpy、不碰 NPU、不碰真实模型），所以这个文件在开发机与板端跑的是
同一份代码、同一批断言。

覆盖:

  1) 读数据文件: 空文件/文件不存在/坏向量各自怎么办（坏的那张降级成"只按标签用"）
  2) 统计: 每轴各标签几张（`label_counts`）/ 词表里的标签名（`labels`）
  3) 排序: 按**词表向量**算余弦（不受打标签时 top-k 截断的影响）
  4) IP 锚点: 平均向量当原型 / 锚点没打过标签要如实报出来 / 名字大小写不敏感 /
     `ip_presets` 的三种写法（{anchors: […]}/[…]／字符串）/ 相对文件名按壁纸目录解析
  5) `match` 语法: `轴=标签` / 只写标签名 / `ip=名字` / 分隔符与大小写 /
     写错要**如实报错**（不认识的轴、不认识的标签、没锚点的 IP）
  6) 余弦与归一化这两个小工具（含"零向量不该炸"）

⚠ 不测"分数准不准"（那是板端 `tests/board/tag_quality.py` 与人工真值的事）。
"""

import json
import logging
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.vision import tag_vocab, wall_data  # noqa: E402
from agent.vision.tag_index import (  # noqa: E402
    DEFAULT_LIMIT,
    IP_KEY,
    MatchResult,
    TagIndex,
    TagIndexError,
    _anchors_of,
    _split_spec,
    cosine,
    normalise,
    preset_names,
)

logging.disable(logging.CRITICAL)          # "坏向量降级"那条 warning 别刷屏

DIM = wall_data.EMBED_DIM


# ---------------------------------------------------------------------------
#  造数据: 两个轴、各 3 条标签, 向量是手工放的"单点"（好算、好验）
# ---------------------------------------------------------------------------
class _Axis:
    """最小轴对象（TagIndex 只用到 .name 与 .texts）。"""

    def __init__(self, name, labels):
        self.name = name
        self.title = name
        self.note = ""
        self.labels = list(labels)

    @property
    def texts(self):
        return [str(x) for x in self.labels]


AXES = (_Axis("scene", ["anime", "landscape", "city"]),
        _Axis("tone", ["dark", "bright", "pastel"]))


def vector_at(index, scale=1.0, noise=0.0):
    """造一个 768 维向量: 第 index 维是 scale, 其余是 noise。

    @note 用"单点"而不是随机数: 这样余弦可以手算（两个不同 index 的向量正交,
          同一个 index 的向量余弦 = 1）, 断言能写死。
    """
    out = [0.0] * DIM
    out[index % DIM] = float(scale)
    if noise:
        out[(index + 1) % DIM] = float(noise)
    return out


def label_vectors():
    """{轴: [向量, …]} —— 顺序与 `axis.texts` 一致（`make_vocab_record` 要的形状）。"""
    table = {}
    position = 0
    for axis in AXES:
        table[axis.name] = []
        for _ in axis.texts:
            table[axis.name].append(vector_at(position))
            position += 1
    return table


LABEL_VECTORS = label_vectors()


def label_vector(axis_name, label):
    """按轴+标签名取向量（测试里读起来比下标清楚）。"""
    for axis in AXES:
        if axis.name == axis_name:
            return LABEL_VECTORS[axis_name][axis.texts.index(label)]
    raise KeyError(axis_name)


def build_index(tmpdir, images, with_vocab=True, model_sha8="m1", vocab_sha8="v1"):
    """造一个数据文件并读成 TagIndex。

    @param images {文件名: (标签, {轴: [(标签, 分数)]}, 向量 | None)}
    @return (TagIndex, 数据文件路径, 目录)
    """
    directory = os.path.join(tmpdir, "wallpapers")
    os.makedirs(directory, exist_ok=True)
    records = []
    for position, (name, (top_label, tags, vector)) in enumerate(images.items()):
        path = os.path.join(directory, name)
        with open(path, "wb") as handle:
            handle.write(b"not an image, just a file")
        record = wall_data.make_record(
            path, 1280, 800, 1234, "sha-%s" % name, 100 + position,
            model_sha8, vocab_sha8, tags, vector if vector is not None else [0.0] * DIM)
        if vector is None:
            record.pop("embedding")            # 模拟"这张图的向量坏了/没存"
        records.append(record)

    vocab = None
    if with_vocab:
        vocab = wall_data.make_vocab_record(AXES, LABEL_VECTORS, model_sha8, vocab_sha8)
    data_file = os.path.join(tmpdir, "wall_data.jsonl")
    wall_data.write_records(data_file, records, vocab=vocab, backup=False)
    return TagIndex.from_file(data_file), data_file, directory


def sample_images():
    """三张图: anime 那张最像 anime, landscape 那张最像 landscape。"""
    return {
        "01_anime.png": ("anime", {"scene": [["anime", 1.0], ["city", 0.1]],
                                   "tone": [["dark", 0.4]]},
                         label_vector("scene", "anime")),
        "02_land.png": ("landscape", {"scene": [["landscape", 0.9]],
                                      "tone": [["bright", 0.8]]},
                        label_vector("scene", "landscape")),
        "03_mixed.png": ("city", {"scene": [["city", 0.5]],
                                  "tone": [["pastel", 0.3]]},
                         vector_at(0, scale=0.6, noise=0.8)),   # 与 anime 有点像
    }


# ===========================================================================
#  1) 读文件
# ===========================================================================
class TestLoading(unittest.TestCase):
    def test_missing_file_is_an_empty_index(self):
        index = TagIndex.from_file(os.path.join(tempfile.mkdtemp(), "nope.jsonl"))
        self.assertEqual(index.count(), 0)
        self.assertEqual(index.axes(), [])
        self.assertEqual(index.match("scene=anime").pool, [])

    def test_axes_and_count_come_from_the_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            index, _, _ = build_index(tmp, sample_images())
            self.assertEqual(index.count(), 3)
            self.assertEqual(index.axes(), ["scene", "tone"])
            self.assertEqual(len(index.paths()), 3)

    def test_a_broken_embedding_only_loses_the_vector(self):
        images = sample_images()
        images["04_broken.png"] = ("anime", {"scene": [["anime", 0.7]]}, None)
        with tempfile.TemporaryDirectory() as tmp:
            index, _, directory = build_index(tmp, images)
            broken = os.path.join(directory, "04_broken.png")
            self.assertFalse(index.has_vector(broken))
            self.assertEqual(index.count(), 4, "向量坏了不代表这一行不算数")
            # 其它三张的向量照旧可用
            self.assertEqual(len([p for p in index.paths() if index.has_vector(p)]), 3)

    def test_empty_file_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "empty.jsonl")
            open(path, "w").close()
            index = TagIndex.from_file(path)
            self.assertEqual(index.count(), 0)

    def test_tags_of_reads_back_the_stored_pairs(self):
        with tempfile.TemporaryDirectory() as tmp:
            index, _, directory = build_index(tmp, sample_images())
            tags = index.tags_of(os.path.join(directory, "01_anime.png"))
            self.assertEqual(tags["scene"][0], ["anime", 1.0])
            self.assertEqual(tags["tone"][0], ["dark", 0.4])

    def test_repr_never_raises(self):
        self.assertIn("TagIndex", repr(TagIndex()))

    def test_error_is_a_value_error(self):
        self.assertTrue(issubclass(TagIndexError, ValueError))


# ===========================================================================
#  2) 统计
# ===========================================================================
class TestCounts(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.index, self.data_file, self.directory = build_index(self._tmp.name,
                                                                 sample_images())

    def tearDown(self):
        self._tmp.cleanup()

    def test_label_counts_per_axis(self):
        # top_k=3: 01_anime 的 scene 存了两条（anime 1.0 / city 0.1），所以 city 是 2
        counts = self.index.label_counts()
        self.assertEqual({item["label"]: item["count"] for item in counts["scene"]},
                         {"anime": 1, "city": 2, "landscape": 1})
        self.assertEqual({item["label"]: item["count"] for item in counts["tone"]},
                         {"dark": 1, "bright": 1, "pastel": 1})

    def test_label_counts_can_look_at_top_one_only(self):
        counts = self.index.label_counts(top_k=1)
        self.assertEqual({item["label"]: item["count"] for item in counts["scene"]},
                         {"anime": 1, "landscape": 1, "city": 1},
                         "只看第一名时每张图只算一次")

    def test_label_counts_respects_top(self):
        counts = self.index.label_counts(top=1)
        for items in counts.values():
            self.assertLessEqual(len(items), 1)

    def test_labels_come_from_the_vocab_not_only_the_records(self):
        # 词表里有的标签即使一张图都没打上, 也该列出来（"你说这个词我不认识"时才列得准）
        self.assertEqual(self.index.labels("scene"), ["anime", "landscape", "city"])

    def test_labels_fall_back_to_the_records_without_a_vocab_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            index, _, _ = build_index(tmp, sample_images(), with_vocab=False)
            self.assertEqual(sorted(index.labels("tone")), ["bright", "dark", "pastel"])
            self.assertFalse(index.has_vocab_vectors())

    def test_summarise_shape(self):
        summary = self.index.summarise(top=2)
        self.assertEqual(summary["count"], 3)
        self.assertEqual(summary["with_vectors"], 3)
        self.assertTrue(summary["vocab_vectors"])
        self.assertEqual(summary["problems"], [])


# ===========================================================================
#  3) 排序
# ===========================================================================
class TestRanking(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.index, _, self.directory = build_index(self._tmp.name, sample_images())

    def tearDown(self):
        self._tmp.cleanup()

    def test_rank_by_label_puts_the_closest_first(self):
        hits = self.index.rank_by_label("scene", "anime")
        self.assertEqual(os.path.basename(hits[0]["path"]), "01_anime.png")
        self.assertAlmostEqual(hits[0]["score"], 1.0, places=6)

    def test_rank_by_label_uses_the_vocab_vector_not_the_stored_topk(self):
        # 02_land 记录里**没有** anime 这条标签（top-k 只存了 landscape）,
        # 但它的向量与 anime 正交 —— 靠词表向量才能算出"0 分"并让它出现在列表里
        hits = self.index.rank_by_label("scene", "anime")
        names = [os.path.basename(hit["path"]) for hit in hits]
        self.assertIn("02_land.png", names, "有词表向量时不该被 top-k 截断影响")

    def test_rank_by_label_without_a_vocab_header_only_knows_stored_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            index, _, directory = build_index(tmp, sample_images(), with_vocab=False)
            hits = index.rank_by_label("scene", "anime")
            self.assertEqual([os.path.basename(h["path"]) for h in hits], ["01_anime.png"])

    def test_rank_by_vector_orders_by_cosine(self):
        hits = self.index.rank_by_vector(vector_at(0, scale=1.0))
        self.assertEqual(os.path.basename(hits[0]["path"]), "01_anime.png")
        self.assertGreater(hits[0]["score"], hits[1]["score"])

    def test_rank_by_vector_respects_limit_and_exclude(self):
        first = self.index.paths()[0]
        hits = self.index.rank_by_vector(vector_at(0), limit=1, exclude=[first])
        self.assertEqual(len(hits), 1)
        self.assertNotEqual(hits[0]["path"], first)

    def test_hits_carry_the_stored_tags(self):
        hits = self.index.rank_by_label("scene", "anime")
        self.assertEqual(hits[0]["tags"]["scene"][0][0], "anime")


# ===========================================================================
#  4) IP 锚点
# ===========================================================================
class TestAnchorPrototype(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.index, _, self.directory = build_index(self._tmp.name, sample_images())

    def tearDown(self):
        self._tmp.cleanup()

    def test_prototype_is_the_normalised_mean(self):
        a = os.path.join(self.directory, "01_anime.png")
        b = os.path.join(self.directory, "03_mixed.png")
        prototype, used, missing = self.index.anchor_prototype([a, b])
        self.assertEqual(sorted(used), sorted([a, b]))
        self.assertEqual(missing, [])
        self.assertAlmostEqual(math.sqrt(sum(x * x for x in prototype)), 1.0, places=6,
                               msg="原型要先归一化, 否则余弦会被张数放大")

    def test_anchors_without_vectors_are_reported_not_swallowed(self):
        images = sample_images()
        images["04_broken.png"] = ("anime", {"scene": [["anime", 0.7]]}, None)
        with tempfile.TemporaryDirectory() as tmp:
            index, _, directory = build_index(tmp, images)
            good = os.path.join(directory, "01_anime.png")
            broken = os.path.join(directory, "04_broken.png")
            prototype, used, missing = index.anchor_prototype([good, broken])
            self.assertEqual(used, [good])
            self.assertEqual(missing, [broken])

    def test_no_usable_anchor_gives_none(self):
        prototype, used, missing = self.index.anchor_prototype(
            [os.path.join(self.directory, "nope.png")])
        self.assertIsNone(prototype)
        self.assertEqual(used, [])
        self.assertEqual(len(missing), 1)


# ===========================================================================
#  5) match 语法
# ===========================================================================
class TestMatch(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.index, _, self.directory = build_index(self._tmp.name, sample_images())
        self.presets = {
            "EVA": {"query": "Evangelion",
                    "anchors": ["01_anime.png", "03_mixed.png"]},
            "Nier": ["02_land.png"],
            "Empty": [],
        }

    def tearDown(self):
        self._tmp.cleanup()

    # ---- 三条正常路径 ----
    def test_axis_label(self):
        result = self.index.match("scene=anime")
        self.assertTrue(result.ok)
        self.assertEqual(result.kind, "axis")
        self.assertEqual(os.path.basename(result.pool[0]), "01_anime.png")
        self.assertIn("scene=anime", result.note)
        self.assertIn("没标定过", result.note,
                      "措辞不能让人把排序当成'命中 N 张'")

    def test_bare_label_searches_every_axis(self):
        result = self.index.match("anime")
        self.assertTrue(result.ok)
        self.assertEqual(result.kind, "label")
        self.assertEqual(result.detail["axes"], ["scene"])
        self.assertEqual(os.path.basename(result.pool[0]), "01_anime.png")

    def test_ip_uses_the_anchors(self):
        result = self.index.match("ip=EVA", presets=self.presets,
                                  wallpaper_dir=self.directory)
        self.assertTrue(result.ok)
        self.assertEqual(result.kind, "ip")
        self.assertEqual(os.path.basename(result.pool[0]), "01_anime.png",
                         "两个锚点都指向 anime 那一带")
        self.assertEqual(result.detail["ip"], "EVA")

    # ---- 语法细节 ----
    def test_separators_and_case(self):
        for spec in ("scene=anime", "scene:anime", "scene: anime", "SCENE=anime"):
            with self.subTest(spec=spec):
                self.assertTrue(self.index.match(spec).ok, spec)

    def test_ip_key_is_recognised_in_any_case(self):
        result = self.index.match("IP=eva", presets=self.presets,
                                  wallpaper_dir=self.directory)
        self.assertTrue(result.ok, "LLM 很容易把 EVA 写成 eva")
        self.assertEqual(result.detail["ip"], "EVA", "回话里要用配置里的原始名字")

    def test_a_missing_key_is_treated_as_a_bare_label(self):
        # "=anime" 这种手滑不该报错
        self.assertTrue(self.index.match("=anime").ok)

    def test_empty_spec_says_what_to_write(self):
        result = self.index.match("   ")
        self.assertFalse(result.ok)
        self.assertIn("scene=anime", result.error)

    # ---- 如实报错 ----
    def test_unknown_axis_lists_the_real_ones(self):
        result = self.index.match("weather=sunny")
        self.assertFalse(result.ok)
        self.assertIn("weather", result.error)
        self.assertIn("scene", result.error)
        self.assertIn("ip=", result.error, "要顺手说 IP 怎么写")

    def test_unknown_label_lists_the_vocabulary(self):
        result = self.index.match("scene=cyberpunk")
        self.assertFalse(result.ok)
        self.assertIn("cyberpunk", result.error)
        self.assertIn("anime", result.error)

    def test_unknown_bare_label_lists_everything(self):
        result = self.index.match("mecha")
        self.assertFalse(result.ok)
        self.assertIn("mecha", result.error)
        self.assertIn("pastel", result.error)

    def test_ip_without_presets_says_where_to_configure(self):
        result = self.index.match("ip=EVA", presets={}, wallpaper_dir=self.directory)
        self.assertFalse(result.ok)
        self.assertIn("ip_presets", result.error)

    def test_unknown_ip_lists_the_known_names(self):
        result = self.index.match("ip=ZZZ", presets=self.presets,
                                  wallpaper_dir=self.directory)
        self.assertFalse(result.ok)
        self.assertIn("ZZZ", result.error)
        self.assertIn("EVA", result.error)

    def test_ip_without_anchors_says_so(self):
        result = self.index.match("ip=Empty", presets=self.presets,
                                  wallpaper_dir=self.directory)
        self.assertFalse(result.ok)
        self.assertIn("anchors", result.error)

    def test_ip_whose_anchors_are_untagged_says_what_to_run(self):
        result = self.index.match("ip=Ghost", presets={"Ghost": ["99_gone.png"]},
                                  wallpaper_dir=self.directory)
        self.assertFalse(result.ok)
        self.assertIn("assistant tag", result.error)

    def test_partially_tagged_anchors_still_work_but_say_what_was_skipped(self):
        presets = {"Mixed": {"anchors": ["01_anime.png", "99_gone.png"]}}
        result = self.index.match("ip=Mixed", presets=presets,
                                  wallpaper_dir=self.directory)
        self.assertTrue(result.ok)
        self.assertEqual(len(result.detail["anchors"]), 1)
        self.assertEqual(len(result.detail["missing"]), 1)
        self.assertIn("没算进去", result.note)

    def test_limit_is_honoured(self):
        result = self.index.match("anime", limit=1)
        self.assertEqual(len(result.pool), 1)

    def test_scores_are_reported_for_the_top_hits(self):
        result = self.index.match("scene=anime", limit=2)
        self.assertEqual(len(result.detail["scores"]), 2)
        self.assertAlmostEqual(result.detail["top"][0]["score"], 1.0, places=6)


class TestSpecSplitting(unittest.TestCase):
    """`_split_spec` / `_anchors_of` / `preset_names` 这几个零件。"""

    def test_split(self):
        self.assertEqual(_split_spec("scene=anime"), ("scene", "anime"))
        self.assertEqual(_split_spec("ip: EVA"), ("ip", "EVA"))
        self.assertEqual(_split_spec("anime"), (None, "anime"))
        self.assertEqual(_split_spec("=anime"), (None, "anime"))
        self.assertEqual(_split_spec("  SCENE = anime "), ("scene", "anime"))

    def test_anchor_forms(self):
        self.assertEqual(_anchors_of({"anchors": ["a.png"]}), ["a.png"])
        self.assertEqual(_anchors_of({"anchors": "a.png"}), ["a.png"])
        self.assertEqual(_anchors_of(["a.png", "b.png"]), ["a.png", "b.png"])
        self.assertEqual(_anchors_of("a.png"), ["a.png"])
        self.assertEqual(_anchors_of({"query": "x"}), [])
        self.assertEqual(_anchors_of(None), [])

    def test_preset_names_are_sorted(self):
        self.assertEqual(preset_names({"Nier": {}, "EVA": {}}), ["EVA", "Nier"])
        self.assertEqual(preset_names(None), [])

    def test_ip_key_constant(self):
        self.assertEqual(IP_KEY, "ip")
        self.assertGreaterEqual(DEFAULT_LIMIT, 1)


# ===========================================================================
#  6) 小工具
# ===========================================================================
class TestVectorMath(unittest.TestCase):
    def test_cosine_of_a_vector_with_itself_is_one(self):
        vector = vector_at(0, scale=3.0)          # 模长不为 1 也算得对
        self.assertAlmostEqual(cosine(vector, vector), 1.0, places=9)

    def test_orthogonal_vectors_are_zero(self):
        self.assertAlmostEqual(cosine(vector_at(0), vector_at(1)), 0.0, places=9)

    def test_a_zero_vector_is_zero_not_a_crash(self):
        self.assertEqual(cosine([0.0] * DIM, vector_at(0)), 0.0)
        self.assertEqual(normalise([0.0] * DIM), [0.0] * DIM)

    def test_normalise_gives_unit_length(self):
        unit = normalise(vector_at(0, scale=5.0))
        self.assertAlmostEqual(math.sqrt(sum(x * x for x in unit)), 1.0, places=9)

    def test_length_mismatch_does_not_raise(self):
        # 数据坏了不该让挑图崩（按短的算）
        self.assertIsInstance(cosine([1.0, 0.0], [1.0]), float)


# ===========================================================================
#  7) MatchResult
# ===========================================================================
class TestMatchResult(unittest.TestCase):
    def test_ok_means_no_error(self):
        self.assertTrue(MatchResult(pool=["a"], kind="axis").ok)
        self.assertFalse(MatchResult(error="x").ok)

    def test_pool_is_copied(self):
        pool = ["a"]
        result = MatchResult(pool=pool)
        pool.append("b")
        self.assertEqual(result.pool, ["a"], "别让调用方改到内部列表")

    def test_repr_is_readable(self):
        self.assertIn("axis", repr(MatchResult(pool=["a"], kind="axis")))
        self.assertIn("error", repr(MatchResult(error="炸了")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
