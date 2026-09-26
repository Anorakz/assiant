#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
#  tests/test_bilibili_config.py — `bilibili:` 段的**模板守卫**（Phase 7 T11-8）
#
#  为什么值得单写一个文件：`config/config.example.yaml` 是所有人 `cp` 起步的那份模板，
#  而**配置写错不报错、只会静默变笨**（agent/config.py 不做 schema 校验）。T11 这一段
#  有 6 个子键组、十几个键，写错一个（例如 `intial_s`）不会有人发现 —— 所以这里把
#  "模板里的键**正是**代码会读的那些"钉成机械可查的事实。
#
#  这个文件做三件事（全程离线，不碰网络、不碰真数据）：
#    1. 模板里的 `bilibili:` 段能被**真构造器**吃下去（不是"看着像"）;
#    2. 段里的每个键都在"有读取者的键"白名单里（多一个/少一个都要红）;
#    3. 模板里写的默认值与代码里的默认值**一致**（改了一边忘了另一边会红）。
# ============================================================================
import os
import sys
import unittest

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.core.bilibili import DEFAULT_QUEUE_MAX, DEFAULT_VIEWPORT, BilibiliQueue  # noqa: E402
from agent.core.bilibili_buffer import (                                          # noqa: E402
    DEFAULT_INITIAL_S,
    DEFAULT_MAX_S,
    DEFAULT_MEM_WATERMARK_MB,
    BilibiliBuffer,
)
from agent.core.game_anchors import DEFAULT_ANCHOR_FILE, GameAnchors               # noqa: E402
from agent.core.game_watch import (                                               # noqa: E402
    DEFAULT_CONFIDENT_MARGIN,
    DEFAULT_CONFIDENT_SCORE,
    DEFAULT_INTERVAL_S,
)
from agent.net.bilibili_api import (                                              # noqa: E402
    DEFAULT_COOKIE_FILE,
    DEFAULT_TIMEOUT_S,
    BilibiliApi,
)
from agent.net.pc_probe import PcProbe                                            # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE = os.path.join(PROJECT_ROOT, "config", "config.example.yaml")

#: `bilibili:` 段里**所有有读取者的键**（真源是各个模块 + docs/bilibili.md §8 的表）。
#: ⚠ 加键时**三处一起改**: 代码读它的地方 / config.example.yaml / 这个白名单。
KNOWN_KEYS = {
    "top": {"enabled", "cookie_file", "timeout_s", "queue", "buffer", "game_watch"},
    "queue": {"viewport_fallback", "max"},
    "buffer": {"dir", "initial_s", "max_s", "mem_watermark_mb"},
    "game_watch": {"enabled", "interval_s", "confident_score", "confident_margin",
                   "anchor_file", "process_names", "mem_watermark_mb"},
}


class _FakeTransport(object):
    """不让构造器碰网络（这个文件只验配置，不验 HTTP）。"""

    def get(self, url, headers=None):        # noqa: D401 - 替身
        raise AssertionError("这个用例不该发请求: %s" % url)

    def cookie_header(self):
        return ""


def example_section():
    """读模板里的 `bilibili:` 段（模板不在 = 跳过，交给别的守卫去喊）。"""
    with open(EXAMPLE, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    section = (data or {}).get("bilibili")
    return section if isinstance(section, dict) else None


class TestBilibiliExampleSection(unittest.TestCase):
    def setUp(self):
        self.section = example_section()
        if self.section is None:
            self.skipTest("config/config.example.yaml 里没有 bilibili: 段")

    # ---------------------------------------------------------- 1) 能构造 ---
    def test_the_example_section_builds_the_real_objects(self):
        section = self.section
        api = BilibiliApi.from_config(section, transport=_FakeTransport())
        self.assertIsNotNone(api, "enabled 是 true，构造器不该返回 None")
        # 相对路径按仓库根解析 -> 模板里那句 config/bilibili_cookie.json 落到仓库里
        self.assertTrue(api.cookie_file.endswith(os.path.join("config", "bilibili_cookie.json")),
                        api.cookie_file)

        queue_cfg = section["queue"]
        queue = BilibiliQueue(api, viewport=int(queue_cfg["viewport_fallback"]),
                              queue_max=int(queue_cfg["max"]))
        self.assertEqual(queue.target(), 3 * int(queue_cfg["viewport_fallback"]))

        watch = section["game_watch"]
        anchors = GameAnchors(watch["anchor_file"])
        self.assertTrue(anchors.path.endswith("game_anchors.jsonl"), anchors.path)
        probe = PcProbe.from_config(watch, music={})
        self.assertTrue(probe.mapping, "process_names 是空的？那认游戏只能靠画面")
        buffer = BilibiliBuffer(api, fifo_dir=section["buffer"]["dir"],
                                initial_s=section["buffer"]["initial_s"],
                                max_s=section["buffer"]["max_s"],
                                mem_watermark_mb=section["buffer"]["mem_watermark_mb"])
        self.assertEqual(buffer.initial_s, float(section["buffer"]["initial_s"]))
        self.assertEqual(buffer.max_s, float(section["buffer"]["max_s"]))

    # ------------------------------------------------------- 2) 键不写错 ---
    def test_every_key_in_the_example_is_one_the_code_reads(self):
        self.assertEqual(set(self.section) - KNOWN_KEYS["top"], set(),
                         "bilibili: 段里有代码不读的键（拼错了？还是忘了加读取者）")
        for name in ("queue", "buffer", "game_watch"):
            sub = self.section[name]
            self.assertIsInstance(sub, dict)
            self.assertEqual(set(sub) - KNOWN_KEYS[name], set(),
                             "bilibili.%s 里有代码不读的键" % name)

    def test_the_keys_the_code_needs_are_all_in_the_example(self):
        # 反过来也要查: 少了键会静默走默认值（不报错），模板就"缺了一半"
        self.assertEqual(KNOWN_KEYS["top"] - set(self.section), set())
        for name in ("queue", "buffer", "game_watch"):
            self.assertEqual(KNOWN_KEYS[name] - set(self.section[name]), set(),
                             "bilibili.%s 缺键" % name)

    # --------------------------------------------------- 3) 默认值对齐 ---
    def test_the_example_defaults_match_the_code(self):
        section = self.section
        self.assertEqual(int(section["queue"]["viewport_fallback"]), DEFAULT_VIEWPORT)
        self.assertEqual(int(section["queue"]["max"]), DEFAULT_QUEUE_MAX)
        self.assertEqual(float(section["buffer"]["initial_s"]), DEFAULT_INITIAL_S)
        self.assertEqual(float(section["buffer"]["max_s"]), DEFAULT_MAX_S)
        self.assertEqual(float(section["buffer"]["mem_watermark_mb"]), DEFAULT_MEM_WATERMARK_MB)
        self.assertEqual(float(section["game_watch"]["interval_s"]), DEFAULT_INTERVAL_S)
        self.assertEqual(float(section["game_watch"]["confident_score"]), DEFAULT_CONFIDENT_SCORE)
        self.assertEqual(float(section["game_watch"]["confident_margin"]), DEFAULT_CONFIDENT_MARGIN)
        self.assertEqual(float(section["game_watch"]["mem_watermark_mb"]), DEFAULT_MEM_WATERMARK_MB)
        self.assertEqual(float(section["timeout_s"]), DEFAULT_TIMEOUT_S)
        # 默认路径也要对得上（写错了会去读别的文件，而且**不报错**）
        self.assertEqual(section["cookie_file"], DEFAULT_COOKIE_FILE)
        self.assertEqual(section["game_watch"]["anchor_file"], DEFAULT_ANCHOR_FILE)

    # ------------------------------------------------- 4) 老板给的映射 ---
    def test_the_process_map_covers_the_names_the_boss_gave(self):
        """你给的三个 exe -> 游戏名必须在模板里（照抄就能用）。"""
        mapping = PcProbe.from_config(self.section["game_watch"], music={}).mapping
        self.assertEqual(mapping.get("white album memories like falling snow"), "white album")
        self.assertEqual(mapping.get("hatsuyuki"), "初雪樱")
        self.assertEqual(mapping.get("amakano3"), "甜蜜女友3")


if __name__ == "__main__":
    unittest.main(verbosity=2)
