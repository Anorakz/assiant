#!/usr/bin/env python3
"""tests/test_lyrics.py — LRC 解析（T15-16-1）

覆盖:
  · **真夹具**（`tests/data/lyrics/qingtian.*`，2026-10-04 从 PC 上真拉的那首剪出来的）：
    元信息行保留 / 间奏空行不产出行 / 译文全空 -> has_tr False / 行数与首末时间戳
  · 译文合并（同一批时间戳，缺译文的那行 `tr` 为空）+ `has_tr`
  · offset：LRC 自己的 `[offset:±ms]` 与配置键 `music.lyric_offset_ms` **同号**
    （**正值 = 歌词提前**），并且夹到 >= 0
  · 宽容性：一行多时间戳 / 重复去重 / 乱序 -> 升序 / 坏行跳过计数 / 任何输入都不抛
  · 载荷形状：每行恰好 `{t, text, tr}`，类型对（GUI 侧按这个形状解析）

运行:
    python3 tests/test_lyrics.py
"""
import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.core import lyrics as lyr  # noqa: E402

FIXTURES = _PROJECT_ROOT / "tests" / "data" / "lyrics"


def _read(name):
    with open(FIXTURES / name, encoding="utf-8") as handle:
        return handle.read()


#: 合成的"有译文"一对（时间戳一一对应；中间那行**故意**不给译文）
LRC_TR = "[00:01.00]第一句\n[00:03.50]第二句\n[00:06.00]第三句\n"
TLYRIC = "[00:01.00]First line\n[00:03.50]\n[00:06.00]Third line\n"


class TestRealFixture(unittest.TestCase):
    """真夹具：形状、行数、元信息、间奏、译文全空。"""

    def setUp(self):
        self.parsed = lyr.parse_lrc(_read("qingtian.lrc"), _read("qingtian.tlyric"))

    def test_line_count_matches_the_non_interlude_lines(self):
        # 夹具 31 行，其中 `[02:21.989]` 是只有时间戳的间奏行 -> 30 行
        self.assertEqual(len(self.parsed["lines"]), 30)
        self.assertTrue(self.parsed["has_lyric"])
        self.assertEqual(self.parsed["skipped"], 0)
        self.assertEqual(self.parsed["reason"], "")

    def test_metadata_lines_are_kept(self):
        """元信息行（作词/作曲/编曲）按用户决定**保留**。"""
        first_three = [row["text"] for row in self.parsed["lines"][:3]]
        self.assertEqual(first_three, ["作词 : 周杰伦", "作曲 : 周杰伦", "编曲 : 周杰伦"])
        self.assertEqual(self.parsed["lines"][0]["t"], 0.0)

    def test_first_real_lyric_line_is_the_fourth(self):
        self.assertEqual(self.parsed["lines"][3]["t"], 28.95)
        self.assertEqual(self.parsed["lines"][3]["text"], "故事的小黄花")

    def test_interlude_line_produces_no_row(self):
        """`[02:21.989]` 只有时间戳 -> 不产出行（"上一行继续显示"由取行逻辑得到）。"""
        stamps = [row["t"] for row in self.parsed["lines"]]
        self.assertNotIn(141.989, stamps)
        # 前后两行都在，说明只丢了那一行、没连着丢
        self.assertIn(132.909, stamps)
        self.assertIn(154.66, stamps)

    def test_lines_are_sorted_by_time(self):
        stamps = [row["t"] for row in self.parsed["lines"]]
        self.assertEqual(stamps, sorted(stamps))

    def test_all_empty_translations_mean_no_translation(self):
        self.assertFalse(self.parsed["has_tr"])
        self.assertTrue(all(row["tr"] == "" for row in self.parsed["lines"]))


class TestTranslation(unittest.TestCase):
    """译文的合并与 `has_tr`。"""

    def test_translation_is_merged_by_timestamp(self):
        parsed = lyr.parse_lrc(LRC_TR, TLYRIC)
        self.assertEqual([row["tr"] for row in parsed["lines"]],
                         ["First line", "", "Third line"])
        self.assertTrue(parsed["has_tr"])

    def test_missing_translation_leaves_the_original_untouched(self):
        """没译文的那行 `tr` 为空 -> 显示侧自然回退到原文（用户决定①）。"""
        parsed = lyr.parse_lrc(LRC_TR, TLYRIC)
        middle = parsed["lines"][1]
        self.assertEqual(middle["tr"], "")
        self.assertEqual(middle["text"], "第二句")

    def test_no_tlyric_at_all_is_not_an_error(self):
        parsed = lyr.parse_lrc(LRC_TR)
        self.assertTrue(parsed["has_lyric"])
        self.assertFalse(parsed["has_tr"])
        self.assertEqual([row["tr"] for row in parsed["lines"]], ["", "", ""])

    def test_translation_with_a_different_length_still_merges(self):
        parsed = lyr.parse_lrc(LRC_TR, "[00:03.50]只有第二句的译文\n")
        self.assertEqual([row["tr"] for row in parsed["lines"]], ["", "只有第二句的译文", ""])


class TestOffsetSign(unittest.TestCase):
    """`[offset:±ms]` 与 `offset_ms` 参数：**正值 = 歌词提前**。"""

    LRC = "[00:10.00]甲\n"
    TAGGED_PLUS = "[offset:+500]\n" + LRC
    TAGGED_MINUS = "[offset:-500]\n" + LRC

    def test_tag_with_positive_offset_shifts_lines_earlier(self):
        self.assertEqual(lyr.parse_lrc(self.TAGGED_PLUS)["lines"][0]["t"], 9.5)

    def test_tag_with_negative_offset_shifts_lines_later(self):
        self.assertEqual(lyr.parse_lrc(self.TAGGED_MINUS)["lines"][0]["t"], 10.5)

    def test_param_and_tag_share_the_same_sign(self):
        self.assertEqual(lyr.parse_lrc(self.LRC, offset_ms=500)["lines"][0]["t"], 9.5)
        self.assertEqual(lyr.parse_lrc(self.LRC, offset_ms=-500)["lines"][0]["t"], 10.5)

    def test_param_and_tag_add_up(self):
        self.assertEqual(lyr.parse_lrc(self.TAGGED_PLUS, offset_ms=500)["lines"][0]["t"], 9.0)

    def test_zero_offset_keeps_the_original_timestamp(self):
        self.assertEqual(lyr.parse_lrc(self.LRC, offset_ms=0)["lines"][0]["t"], 10.0)

    def test_shifted_times_are_clamped_at_zero(self):
        self.assertEqual(lyr.parse_lrc(self.LRC, offset_ms=60000)["lines"][0]["t"], 0.0)

    def test_a_broken_offset_value_is_ignored(self):
        """`[offset:abc]` 看不懂就当他没有（不抛、也不乱移）。"""
        self.assertEqual(lyr.parse_lrc("[offset:abc]\n" + self.LRC)["lines"][0]["t"], 10.0)


class TestTolerantParsing(unittest.TestCase):
    """宽容性：坏行、多时间戳、去重、乱序、极端值。"""

    def test_multiple_timestamps_on_one_line_produce_several_rows(self):
        parsed = lyr.parse_lrc("[00:01.00][00:05.00]同一句\n")
        self.assertEqual([(row["t"], row["text"]) for row in parsed["lines"]],
                         [(1.0, "同一句"), (5.0, "同一句")])

    def test_identical_rows_are_deduplicated(self):
        parsed = lyr.parse_lrc("[00:01.00]甲\n[00:01.00]甲\n")
        self.assertEqual(len(parsed["lines"]), 1)

    def test_same_timestamp_with_different_text_keeps_both(self):
        parsed = lyr.parse_lrc("[00:01.00]甲\n[00:01.00]乙\n")
        self.assertEqual([row["text"] for row in parsed["lines"]], ["甲", "乙"])

    def test_out_of_order_input_is_sorted(self):
        parsed = lyr.parse_lrc("[00:05.00]后\n[00:01.00]前\n")
        self.assertEqual([row["text"] for row in parsed["lines"]], ["前", "后"])

    def test_two_and_three_digit_milliseconds_both_parse(self):
        self.assertEqual(lyr.parse_lrc("[00:01.05]甲\n")["lines"][0]["t"], 1.05)
        self.assertEqual(lyr.parse_lrc("[00:01.050]甲\n")["lines"][0]["t"], 1.05)
        # 毫秒用冒号分隔的写法也吃
        self.assertEqual(lyr.parse_lrc("[00:01:05]甲\n")["lines"][0]["t"], 1.05)

    def test_minutes_can_exceed_sixty(self):
        self.assertEqual(lyr.parse_lrc("[100:00.00]甲\n")["lines"][0]["t"], 6000.0)

    def test_whitespace_around_the_text_is_trimmed(self):
        self.assertEqual(lyr.parse_lrc("[00:01.00]   两边有空格   \n")["lines"][0]["text"],
                         "两边有空格")

    def test_text_without_timestamp_is_counted_as_skipped(self):
        parsed = lyr.parse_lrc("这不是歌词\n[ti:晴天]\n[00:01.00]甲\n")
        self.assertEqual(parsed["skipped"], 1)
        self.assertEqual(len(parsed["lines"]), 1)

    def test_broken_lines_do_not_raise(self):
        for text in ("随便一段话", "[00:", "[[00:01.00]]", "\x00\x01\xff", "[]"):
            parsed = lyr.parse_lrc(text)          # 不抛就算过
            self.assertIn("lines", parsed)

    def test_no_lyric_at_all(self):
        for text in (None, "", "   \n\n"):
            parsed = lyr.parse_lrc(text)
            self.assertFalse(parsed["has_lyric"])
            self.assertEqual(parsed["lines"], [])
            self.assertEqual(parsed["reason"], lyr.REASON_NO_LRC)

    def test_text_but_no_timeline_gets_its_own_reason(self):
        parsed = lyr.parse_lrc("整首歌词都没有时间戳\n第二行\n")
        self.assertFalse(parsed["has_lyric"])
        self.assertEqual(parsed["reason"], lyr.REASON_NO_TIMELINE)
        self.assertEqual(parsed["skipped"], 2)


class TestPayloadShape(unittest.TestCase):
    """载荷形状（GUI 侧按这个解析；改形状要连着改 C++ 与文档）。"""

    def setUp(self):
        self.parsed = lyr.parse_lrc(LRC_TR, TLYRIC, offset_ms=250)

    def test_every_row_has_exactly_the_three_fields(self):
        for row in self.parsed["lines"]:
            self.assertEqual(set(row), {"t", "text", "tr"})

    def test_field_types(self):
        row = self.parsed["lines"][0]
        self.assertIsInstance(row["t"], float)
        self.assertIsInstance(row["text"], str)
        self.assertIsInstance(row["tr"], str)

    def test_result_keys_are_stable(self):
        self.assertEqual(set(self.parsed),
                         {"lines", "has_lyric", "has_tr", "skipped", "reason"})

    def test_payload_is_json_friendly(self):
        import json

        json.dumps(self.parsed)                  # 推给 GUI 的那份必须能直接编码


if __name__ == "__main__":
    unittest.main(verbosity=2)
