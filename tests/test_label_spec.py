#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_label_spec.py — 统一标签语法（Phase 7 T8-5b）

跑法:
    python tests/test_label_spec.py

为什么单独一个文件
    `agent/core/label_spec.py` 是**壁纸 match= 与音乐 tag= 的唯一一份解析**。
    它被两边共用，所以边界必须钉死：拆键、拆值、多轴用 `;`、轴名小写而值保持原样、
    空输入是空（不是 `{"": []}`）。壁纸那边的老行为（分隔符集合）在这里也一起钉住 ——
    因为 `agent/vision/tag_index.py` 的两个内部函数现在只是转发到它。
"""

import logging
import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.core import label_spec  # noqa: E402

logging.disable(logging.CRITICAL)


class TestSplitKey(unittest.TestCase):
    def test_axis_and_value(self):
        self.assertEqual(label_spec.split_key("scene=anime"), ("scene", "anime"))

    def test_colon_also_works(self):
        self.assertEqual(label_spec.split_key("ip: EVA"), ("ip", "EVA"))

    def test_axis_is_lowercased_value_is_not(self):
        # 轴名本来就是小写英文；标签值可能是中文/大写，**不许动**
        self.assertEqual(label_spec.split_key("Scene=Anime"), ("scene", "Anime"))

    def test_whitespace_is_trimmed(self):
        self.assertEqual(label_spec.split_key("  mood = 燃 "), ("mood", "燃"))

    def test_bare_label_has_no_axis(self):
        self.assertEqual(label_spec.split_key("anime"), (None, "anime"))

    def test_empty_key_means_bare_label(self):
        # "=anime" 是手滑，不该报错，按"只写了标签名"处理
        self.assertEqual(label_spec.split_key("=anime"), (None, "anime"))

    def test_empty_input(self):
        self.assertEqual(label_spec.split_key(""), (None, ""))
        self.assertEqual(label_spec.split_key(None), (None, ""))

    def test_first_separator_wins(self):
        self.assertEqual(label_spec.split_key("a=b=c"), ("a", "b=c"))


class TestSplitValues(unittest.TestCase):
    def test_slash(self):
        self.assertEqual(label_spec.split_values("anime/landscape"), ["anime", "landscape"])

    def test_every_separator_the_board_has_seen(self):
        # 板端实测: 模型用斜杠、逗号、空格、顿号把一串标签拼起来
        for text in ("a/b", "a,b", "a b", "a、b", "a，b", "a;b", "a；b", "a / b"):
            self.assertEqual(label_spec.split_values(text), ["a", "b"], text)

    def test_order_kept_and_duplicates_dropped(self):
        self.assertEqual(label_spec.split_values("b/a/b"), ["b", "a"])

    def test_empty(self):
        self.assertEqual(label_spec.split_values(""), [])
        self.assertEqual(label_spec.split_values("   "), [])
        self.assertEqual(label_spec.split_values(None), [])


class TestParse(unittest.TestCase):
    def test_single_axis(self):
        self.assertEqual(label_spec.parse("scene=anime"), {"scene": ["anime"]})

    def test_multi_value_same_axis(self):
        self.assertEqual(label_spec.parse("scene=anime/landscape"),
                         {"scene": ["anime", "landscape"]})

    def test_bare_label_lands_in_the_empty_axis(self):
        self.assertEqual(label_spec.parse("energetic"), {label_spec.EMPTY_AXIS: ["energetic"]})

    def test_multiple_axes_use_semicolon(self):
        self.assertEqual(label_spec.parse("mood=燃; style=rock"),
                         {"mood": ["燃"], "style": ["rock"]})

    def test_full_width_semicolon_too(self):
        self.assertEqual(label_spec.parse("mood=燃；style=rock"),
                         {"mood": ["燃"], "style": ["rock"]})

    def test_trailing_separator_is_ignored(self):
        self.assertEqual(label_spec.parse("mood=燃;"), {"mood": ["燃"]})

    def test_empty_input_is_an_empty_dict(self):
        # ⚠ 不是 {"": []} —— 调用方用 `if not parsed` 判空
        self.assertEqual(label_spec.parse(""), {})
        self.assertEqual(label_spec.parse("   "), {})
        self.assertEqual(label_spec.parse(None), {})

    def test_ip_key_is_preserved(self):
        self.assertEqual(label_spec.parse("ip=EVA"), {"ip": ["EVA"]})

    def test_dedup_across_segments(self):
        self.assertEqual(label_spec.parse("mood=燃; mood=燃"), {"mood": ["燃"]})


class TestParseOne(unittest.TestCase):
    def test_takes_the_first_segment(self):
        self.assertEqual(label_spec.parse_one("scene=anime/landscape; mood=燃"),
                         ("scene", ["anime", "landscape"]))

    def test_bare_label(self):
        self.assertEqual(label_spec.parse_one("anime"), (None, ["anime"]))

    def test_bare_multi_value(self):
        self.assertEqual(label_spec.parse_one("a b"), (None, ["a", "b"]))

    def test_empty(self):
        self.assertEqual(label_spec.parse_one(""), (None, []))
        self.assertEqual(label_spec.parse_one(None), (None, []))


class TestFormatTags(unittest.TestCase):
    def test_round_trip(self):
        tags = {"mood": ["燃"], "style": ["rock"]}
        self.assertEqual(label_spec.format_tags(tags), "mood=燃; style=rock")
        self.assertEqual(label_spec.parse(label_spec.format_tags(tags)), tags)

    def test_multi_value(self):
        self.assertEqual(label_spec.format_tags({"scene": ["anime", "landscape"]}),
                         "scene=anime/landscape")

    def test_empty_axis_writes_the_bare_value(self):
        self.assertEqual(label_spec.format_tags({label_spec.EMPTY_AXIS: ["燃"]}), "燃")

    def test_empty_things(self):
        self.assertEqual(label_spec.format_tags({}), "")
        self.assertEqual(label_spec.format_tags(None), "")
        self.assertEqual(label_spec.format_tags({"mood": []}), "")


class TestIsQualified(unittest.TestCase):
    def test_with_axis(self):
        self.assertTrue(label_spec.is_qualified("mood=燃"))

    def test_without_axis(self):
        self.assertFalse(label_spec.is_qualified("燃"))
        self.assertFalse(label_spec.is_qualified(""))


class TestTagIndexStillUsesTheSameGrammar(unittest.TestCase):
    """壁纸那两个内部函数现在只是转发 —— 转发坏了这里立刻红。"""

    def test_split_spec_is_the_shared_one(self):
        from agent.vision import tag_index

        self.assertEqual(tag_index._split_spec("scene=anime"),
                         label_spec.split_key("scene=anime"))

    def test_pick_labels_splits_like_the_shared_one(self):
        from agent.vision import tag_index

        known = ["anime", "landscape"]
        self.assertEqual(tag_index._pick_labels("anime/landscape/nope", known),
                         (["anime", "landscape"], ["nope"]))
        self.assertEqual(label_spec.split_values("anime/landscape/nope"),
                         ["anime", "landscape", "nope"])

    def test_the_old_regex_is_gone(self):
        # 两套分隔符迟早会漂移 —— 老的本地正则不许再留一份
        from agent.vision import tag_index

        self.assertFalse(hasattr(tag_index, "_LABEL_SPLIT_RE"),
                         "分隔符只在 label_spec 里定义一份")


if __name__ == "__main__":
    unittest.main(verbosity=2)
