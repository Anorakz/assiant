#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`agent/core/bilibili.py`（队列内核）的单测 —— **离线**：注入假 api，不碰网络。

钉住的是这套规矩（T11-0/D4 定的）:
  · **缓存**抓过的页不丢（走回头路要能走回去）;**窗口** = 缓存里的一个切片,
    目标 **3 × 预览栏格数 N**, 当前条前面留 N 条;
  · 往哪边走就往哪边补页（next 抓下一页 / prev 抓上一页并前插）;
  · 边界按 `num_pages`/`page=1` 判 —— **永远不请求越界页**（实测越界页会回重复内容）;
  · 缓存内按 bvid 去重; 取页失败**能动的先动**, 动不了才如实说。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.core.bilibili import (                                    # noqa: E402
    DEFAULT_QUEUE_MAX,
    DEFAULT_VIEWPORT,
    BilibiliQueue,
)
from agent.net.bilibili_api import BilibiliRiskError                 # noqa: E402


class FakeApi(object):
    """假 B 站。`pages` = {页号: 条数 或 [bvid…]}（没写的页默认 `page_size` 条）。"""

    def __init__(self, pages=None, num_pages=50, page_size=20, error=None):
        self.pages = dict(pages or {})
        self.num_pages = int(num_pages)
        self.page_size = int(page_size)
        self.error = error
        self.calls = []

    def search(self, keyword, page=1, order="totalrank"):
        self.calls.append(int(page))
        if self.error is not None:
            raise self.error
        if page < 1:
            raise AssertionError("不该请求第 %d 页" % page)
        if page > self.num_pages:
            raise AssertionError("不该请求越界页 %d（num_pages=%d）" % (page, self.num_pages))
        spec = self.pages.get(page, self.page_size)
        bvids = (["BV%d-%02d" % (page, index) for index in range(spec)]
                 if isinstance(spec, int) else list(spec))
        return {"keyword": keyword, "page": page, "order": order,
                "num_pages": self.num_pages, "num_results": self.num_pages * self.page_size,
                "items": [{"bvid": bvid, "title": "标题 %s" % bvid, "author": "UP",
                           "play": 1000, "like": 10, "duration_s": 200,
                           "duration_text": "3:20",
                           "cover": "https://i.example/%s.jpg" % bvid,
                           "url": "https://www.bilibili.com/video/%s" % bvid}
                          for bvid in bvids]}

    def pages_requested(self):
        return list(self.calls)


def make(viewport=6, queue_max=None, **kwargs):
    api = FakeApi(**kwargs)
    return BilibiliQueue(api, viewport=viewport, queue_max=queue_max), api


class TestWindowSize(unittest.TestCase):

    def test_front_page_is_the_default_viewport(self):
        queue, _api = make(viewport=None)
        self.assertEqual(queue.viewport, DEFAULT_VIEWPORT)
        self.assertEqual(queue.target(), 3 * DEFAULT_VIEWPORT)

    def test_target_is_three_times_the_viewport(self):
        queue, _api = make(viewport=6)
        self.assertEqual(queue.target(), 18)
        queue.search("x")
        self.assertEqual(len(queue), 18)              # 窗口就是 18 条
        self.assertEqual(queue.index, 0)
        self.assertEqual(queue.items[0]["bvid"], "BV1-00")

    def test_viewport_from_gui_resizes_the_window(self):
        queue, _api = make(viewport=6)
        queue.search("x")
        self.assertEqual(queue.set_viewport(3), 3)
        self.assertEqual(queue.target(), 9)
        self.assertEqual(len(queue), 9)
        queue.set_viewport(8)
        self.assertEqual(queue.target(), 24)
        self.assertEqual(len(queue), 24)

    def test_queue_max_caps_the_window(self):
        queue, _api = make(viewport=20, queue_max=25)
        self.assertEqual(queue.target(), 25)
        queue.search("x")
        self.assertEqual(len(queue), 25)

    def test_viewport_is_clamped(self):
        queue, _api = make(viewport=6)
        self.assertEqual(queue.set_viewport(0), 1)
        self.assertEqual(queue.set_viewport(999), 20)
        self.assertEqual(queue.set_viewport("乱写"), DEFAULT_VIEWPORT)


class TestWalking(unittest.TestCase):

    def test_next_walks_forward(self):
        queue, _api = make(viewport=2)
        queue.search("x")
        for _ in range(3):
            queue.next()
        self.assertEqual(queue.current()["bvid"], "BV1-03")
        self.assertEqual(queue.index, 2)              # 前面恒留 N=2 条

    def test_next_fetches_the_next_page_when_the_cache_runs_out(self):
        queue, api = make(viewport=2, page_size=6, num_pages=5)
        queue.search("x")
        self.assertEqual(api.pages_requested(), [1])
        for _ in range(5):
            queue.next()                              # 走到第 6 条（缓存尾）
        self.assertEqual(queue.current()["bvid"], "BV1-05")
        queue.next()                                  # 后面没了 -> 抓第 2 页
        self.assertEqual(queue.current()["bvid"], "BV2-00")
        self.assertIn(2, api.pages_requested())

    def test_next_stops_honestly_at_the_last_page(self):
        queue, api = make(viewport=1, pages={1: 2}, num_pages=1)
        queue.search("x")
        self.assertTrue(queue.next()["ok"])
        result = queue.next()
        self.assertFalse(result["ok"])
        self.assertIn("后面没有了", result["why"])
        self.assertEqual(api.pages_requested(), [1])   # 越界页一次都没请求

    def test_prev_is_honest_before_the_first_page(self):
        queue, api = make(viewport=6, num_pages=3)
        queue.search("x")
        result = queue.prev()
        self.assertFalse(result["ok"])
        self.assertIn("前面没有了", result["why"])
        self.assertEqual(api.pages_requested(), [1])

    def test_walking_back_returns_the_same_videos_in_reverse(self):
        queue, api = make(viewport=2, page_size=6, num_pages=5)
        queue.search("x")
        forward = [queue.current()["bvid"]]
        for _ in range(14):
            queue.next()
            forward.append(queue.current()["bvid"])
        self.assertIn(2, api.pages_requested())
        self.assertIn(3, api.pages_requested())
        backward = [queue.current()["bvid"]]
        for _ in range(14):
            queue.prev()
            backward.append(queue.current()["bvid"])
        self.assertEqual(forward, list(reversed(backward)))
        self.assertEqual(api.pages_requested().count(0), 0)     # 永不请求第 0 页

    def test_walking_back_uses_the_cache_not_the_network(self):
        queue, api = make(viewport=2, page_size=6, num_pages=5)
        queue.search("x")
        for _ in range(8):
            queue.next()
        before = api.pages_requested()
        for _ in range(6):
            queue.prev()
        self.assertEqual(api.pages_requested(), before)         # 走回头路只读缓存

    def test_empty_queue_is_honest(self):
        queue, _api = make()
        self.assertFalse(queue.next()["ok"])
        self.assertIn("空", queue.next()["why"])
        self.assertFalse(queue.move("乱写")["ok"])

    def test_step_zero_does_not_move(self):
        queue, _api = make(viewport=2)
        queue.search("x")
        queue.next()
        current = queue.current()["bvid"]
        self.assertTrue(queue.move(0)["ok"])
        self.assertEqual(queue.current()["bvid"], current)


class TestWindowDiscipline(unittest.TestCase):

    def test_window_is_a_slice_starting_n_before_the_current(self):
        queue, _api = make(viewport=3, page_size=60, num_pages=5)
        queue.search("x")
        for _ in range(3):
            queue.next()                                # 先离开开头（开头那几条前面没东西）
        for _ in range(10):
            self.assertEqual(len(queue), 9)              # 3N
            self.assertEqual(queue.index, 3)             # 前面恒留 N
            self.assertEqual(queue.items[queue.index]["bvid"], queue.current()["bvid"])
            queue.next()

    def test_window_shrinks_honestly_near_the_start_and_end(self):
        queue, _api = make(viewport=2, pages={1: 4}, num_pages=1)
        queue.search("x")
        self.assertEqual(len(queue), 4)                         # 总共就 4 条
        self.assertIn("只凑到 4 条", queue.why)
        queue.next()
        queue.next()
        self.assertEqual(queue.index, 2)
        self.assertEqual(len(queue), 4)

    def test_pick_recenters_the_window(self):
        queue, _api = make(viewport=2, page_size=20)
        queue.search("x")
        queue.pick(5)
        self.assertEqual(queue.current()["bvid"], "BV1-05")
        self.assertEqual(queue.index, 2)                        # 前面留 N
        self.assertEqual(len(queue), 6)

    def test_pick_is_clamped_to_the_window(self):
        queue, _api = make(viewport=2)
        queue.search("x")
        visible = queue.items
        queue.pick(999)
        self.assertEqual(queue.current()["bvid"], visible[-1]["bvid"])
        visible = queue.items
        queue.pick(-5)
        self.assertEqual(queue.current()["bvid"], visible[0]["bvid"])

    def test_dedup_inside_the_cache(self):
        pages = {1: ["BV-a", "BV-b", "BV-c"], 2: ["BV-c", "BV-d", "BV-e"]}
        queue, _api = make(viewport=2, pages=pages, num_pages=2)
        queue.search("x")
        for _ in range(2):
            queue.next()
        cached = [item["bvid"] for item in queue.items]
        self.assertEqual(len(cached), len(set(cached)))
        self.assertIn("BV-d", cached)


class TestSourcesAndErrors(unittest.TestCase):

    def test_search_records_source_and_keyword(self):
        queue, api = make()
        queue.search("luna say maybe", source="dialogue")
        self.assertEqual(queue.keyword, "luna say maybe")
        self.assertEqual(queue.source, "dialogue")
        self.assertEqual(api.pages_requested(), [1])
        self.assertEqual(queue.state()["cached"], 20)

    def test_screen_source(self):
        queue, _api = make()
        queue.search("stellaris", source="screen")
        self.assertEqual(queue.source, "screen")

    def test_clear_drops_everything(self):
        queue, _api = make()
        queue.search("x")
        result = queue.clear()
        self.assertTrue(result["ok"])
        self.assertEqual(len(queue), 0)
        self.assertEqual(queue.cached_count(), 0)
        self.assertEqual(queue.keyword, "")
        self.assertEqual(queue.source, "")
        self.assertIsNone(queue.current())

    def test_empty_keyword_is_refused(self):
        queue, api = make()
        self.assertFalse(queue.search("   ")["ok"])
        self.assertEqual(api.pages_requested(), [])

    def test_no_results_is_honest_not_an_error(self):
        queue, _api = make(pages={1: 0}, num_pages=50)
        result = queue.search("不存在的关键词")
        self.assertFalse(result["ok"])
        self.assertIn("一条都没搜到", result["why"])

    def test_api_error_is_reported_and_walking_still_works(self):
        queue, api = make(viewport=2, page_size=4, num_pages=9)
        queue.search("x")
        api.error = BilibiliRiskError("被 B 站风控了（HTTP 412）—— 等几分钟再试")
        result = None
        for _ in range(8):
            result = queue.next()                   # 缓存里还有 -> 照走
        self.assertFalse(result["ok"])
        self.assertIn("风控", result["why"])
        self.assertLessEqual(len(queue), queue.target())

    def test_more_forces_another_page(self):
        queue, api = make(viewport=1)
        queue.search("x")
        self.assertEqual(api.pages_requested(), [1])
        result = queue.more()
        self.assertTrue(result["ok"])
        self.assertEqual(api.pages_requested(), [1, 2])

    def test_more_at_the_end_is_honest(self):
        queue, _api = make(viewport=1, pages={1: 3}, num_pages=1)
        queue.search("x")
        result = queue.more()
        self.assertFalse(result["ok"])
        self.assertIn("后面没有了", result["why"])

    def test_search_failure_says_so(self):
        queue, _api = make(error=BilibiliRiskError("被 B 站风控了（code=-352）"))
        result = queue.search("x")
        self.assertFalse(result["ok"])
        self.assertIn("风控", result["why"])


class TestStateAndCopies(unittest.TestCase):

    def test_state_snapshot(self):
        queue, _api = make(viewport=2, page_size=6, num_pages=4)
        queue.search("x")
        queue.next()
        state = queue.state()
        self.assertEqual(state["keyword"], "x")
        self.assertEqual(state["source"], "dialogue")
        self.assertEqual(state["viewport"], 2)
        self.assertEqual(state["target"], 6)
        self.assertEqual(state["count"], len(queue))
        self.assertEqual(state["index"], queue.index)
        self.assertEqual(state["current"]["bvid"], queue.current()["bvid"])
        self.assertEqual(state["first_page"], 1)
        self.assertEqual(state["last_page"], 2)      # 往前走会把后面那页补上
        self.assertEqual(state["num_pages"], 4)
        self.assertFalse(state["at_head"])
        self.assertFalse(state["at_tail"])

    def test_at_head_and_tail_flags(self):
        queue, _api = make(viewport=2, pages={1: 3}, num_pages=1)
        queue.search("x")
        self.assertTrue(queue.state()["at_head"])
        self.assertFalse(queue.state()["at_tail"])
        queue.pick(2)
        self.assertTrue(queue.state()["at_tail"])

    def test_items_are_copies(self):
        queue, _api = make()
        queue.search("x")
        items = queue.items
        items[0]["bvid"] = "改坏了"
        self.assertNotEqual(queue.items[0]["bvid"], "改坏了")

    def test_cached_count_grows_as_we_walk(self):
        queue, _api = make(viewport=2, page_size=6, num_pages=5)
        queue.search("x")
        self.assertEqual(queue.cached_count(), 6)
        for _ in range(6):
            queue.next()
        self.assertGreater(queue.cached_count(), 6)

    def test_queue_max_default(self):
        queue, _api = make(viewport=None)
        self.assertEqual(queue.queue_max, DEFAULT_QUEUE_MAX)
        self.assertLessEqual(queue.target(), DEFAULT_QUEUE_MAX)


if __name__ == "__main__":
    unittest.main(verbosity=2)
