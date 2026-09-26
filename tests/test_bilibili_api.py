#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`agent/net/bilibili_api.py` 的单测（**全程离线**：注入假 transport，不碰网络）。

假 transport 是这一层的"网"，所以这些用例同时钉住两件事:
  1. 我们**怎么发请求**（URL 参数、UA/Referer/Cookie 头、首页 cookie 引导）；
  2. 我们**怎么读响应**（字段规范化、清晰度阶梯、错误 -> 能照做的话）。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.net import bilibili_api as api_mod                       # noqa: E402
from agent.net.bilibili_api import (                                 # noqa: E402
    DEFAULT_UA,
    BilibiliApi,
    BilibiliError,
    BilibiliNetworkError,
    BilibiliNotFound,
    BilibiliPayloadError,
    BilibiliRiskError,
    clean_title,
    load_cookie_file,
    parse_duration_text,
    quality_label,
)

HOME_HTML = "<!DOCTYPE html><html><body>ok</body></html>"

#: 不存在的 cookie 文件（保证"匿名"这条路径不依赖开发机上的真 cookie）
NO_COOKIE_FILE = os.path.join(tempfile.gettempdir(), "bili-no-such-cookie.json")


class FakeTransport(object):
    """假网络：按 URL 里的特征串给响应；顺手记下所有请求（含头）。

    @note **按特征串从长到短匹配**：`fnval=16` 里包含 `fnval=1`，按书写顺序匹配会张冠李戴
          （第一版就这么错了：DASH 请求被"单文件"那条规则接走）。
    """

    def __init__(self, routes, jar="buvid3=FAKE-BUVID; b_nut=1"):
        self.routes = sorted(list(routes), key=lambda pair: -len(pair[0]))
        self.calls = []
        self._jar = jar

    def get(self, url, headers=None, timeout_s=None):
        self.calls.append({"url": url, "headers": dict(headers or {})})
        for needle, result in self.routes:
            if needle in url:
                if isinstance(result, Exception):
                    raise result
                return result
        raise AssertionError("假 transport 没准备这个 URL: %s" % url)

    def cookie_header(self):
        return self._jar

    def urls(self):
        return [call["url"] for call in self.calls]

    def headers_of(self, needle):
        for call in self.calls:
            if needle in call["url"]:
                return call["headers"]
        return {}


def fake(**kwargs):
    """造一个客户端（默认**匿名且强制没有 cookie 文件**、假网络）。

    ⚠ 必须显式给 cookie_file：不给就走默认路径 `config/bilibili_cookie.json`，
      而开发机上**真有一份**（老板的 SESSDATA）→ 用例会跟着本机文件变（第一次跑就踩到了）。
    """
    kwargs.setdefault("cookie_file", NO_COOKIE_FILE)
    return BilibiliApi(transport=FakeTransport(kwargs.pop("routes")), **kwargs)


def fake_with_cookie(routes, **kwargs):
    """造一个**带 SESSDATA** 的客户端（cookie 写进临时文件，不碰仓库里那份）。"""
    path = os.path.join(tempfile.mkdtemp(prefix="bili-"), "c.json")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write('{"SESSDATA": "MY-SESSION"}')
    return BilibiliApi(cookie_file=path, transport=FakeTransport(routes), **kwargs)


def search_payload(items, num_pages=50, num_results=1000):
    import json

    return json.dumps({"code": 0, "message": "OK", "data": {
        "numPages": num_pages, "numResults": num_results, "result": items}})


PLAIN_PAYLOAD = """
{"code":0,"message":"OK","data":{"quality":64,"accept_quality":[64,16],
 "durl":[{"order":1,"length":265904,"size":70276253,
          "url":"https://upos.example/x.mp4?e=1"}]}}
"""

DASH_PAYLOAD = """
{"code":0,"message":"OK","data":{"quality":80,"accept_quality":[112,80,64,16],
 "dash":{"video":[{"id":32,"bandwidth":400000,"baseUrl":"https://upos.example/v32.m4s"},
                  {"id":80,"bandwidth":1201000,"baseUrl":"https://upos.example/v80.m4s"}],
         "audio":[{"bandwidth":66000,"baseUrl":"https://upos.example/a64.m4s"},
                  {"bandwidth":101000,"baseUrl":"https://upos.example/a128.m4s"}]}}}
"""

VIEW_PAYLOAD = """
{"code":0,"message":"OK","data":{"bvid":"BV1xx411c7mD","aid":123,"cid":456,
 "title":"标题<em class=\\"keyword\\">高亮</em>","pic":"//i0.example/cover.jpg",
 "duration":265,"owner":{"name":"UP主"},"pages":[{"cid":456},{"cid":457}]}}
"""


class TestPureHelpers(unittest.TestCase):

    def test_clean_title_strips_highlight_and_entities(self):
        # 接口把关键词包在 <em class="keyword">…</em> 里（json 解完就是这个样子，实测）
        self.assertEqual(clean_title("【中字】<em class=\"keyword\">Luna</em> say&amp;maybe"),
                         "【中字】Luna say&maybe")
        self.assertEqual(clean_title("  <em>初雪樱</em> 实况  "), "初雪樱 实况")
        self.assertEqual(clean_title(None), "")

    def test_parse_duration_text(self):
        self.assertEqual(parse_duration_text("3:4"), 184)
        self.assertEqual(parse_duration_text("233:45"), 14025)   # 分可以超过 60（实测见过）
        self.assertEqual(parse_duration_text("1:02:03"), 3723)
        self.assertEqual(parse_duration_text(""), 0)
        self.assertEqual(parse_duration_text("abc"), 0)
        self.assertEqual(parse_duration_text("1:2:3:4"), 0)

    def test_quality_label(self):
        self.assertEqual(quality_label(16), "360P")
        self.assertEqual(quality_label(64), "720P")
        self.assertEqual(quality_label(80), "1080P")
        self.assertEqual(quality_label("80"), "1080P")
        self.assertEqual(quality_label(9999), "未知清晰度(码 9999)")
        self.assertEqual(quality_label(None), "未知清晰度")


class TestCookieFile(unittest.TestCase):

    def setUp(self):
        import tempfile

        self.dir = tempfile.mkdtemp(prefix="bili-cookie-")

    def write(self, text, name="c.json"):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_missing_file_is_anonymous_not_an_error(self):
        self.assertEqual(load_cookie_file(os.path.join(self.dir, "nope.json")), ({}, []))
        self.assertEqual(load_cookie_file(None), ({}, []))

    def test_reads_sessdata(self):
        cookies, notes = load_cookie_file(self.write('{"SESSDATA": "abc"}'))
        self.assertEqual(cookies, {"SESSDATA": "abc"})
        self.assertEqual(notes, [])

    def test_typo_seessdata_is_accepted_with_a_note(self):
        cookies, notes = load_cookie_file(self.write('{"SEESSDATA": "abc"}'))
        self.assertEqual(cookies, {"SESSDATA": "abc"})
        self.assertEqual(len(notes), 1)
        self.assertIn("SEESSDATA", notes[0])
        self.assertIn("SESSDATA", notes[0])

    def test_broken_and_empty_files_degrade_to_anonymous(self):
        cookies, notes = load_cookie_file(self.write("{not json"))
        self.assertEqual(cookies, {})
        self.assertEqual(len(notes), 1)
        cookies, notes = load_cookie_file(self.write("[1,2,3]"))
        self.assertEqual(cookies, {})
        self.assertEqual(len(notes), 1)
        cookies, notes = load_cookie_file(self.write('{"SESSDATA": "   "}'))
        self.assertEqual(cookies, {})

    def test_file_without_sessdata_says_so(self):
        cookies, notes = load_cookie_file(self.write('{"bili_jct": "x"}'))
        self.assertEqual(cookies, {"bili_jct": "x"})
        self.assertTrue(any("没有 SESSDATA" in note for note in notes))

    def test_client_warns_on_typo(self):
        import logging

        records = []

        class Grab(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        logger = logging.getLogger("test-bili-cookie")
        logger.addHandler(Grab())
        logger.setLevel(logging.WARNING)
        BilibiliApi(cookie_file=self.write('{"SEESSDATA": "abc"}'),
                    transport=FakeTransport([]), log=logger)
        self.assertTrue(any("SEESSDATA" in message for message in records))


class TestRequestShape(unittest.TestCase):

    def test_bootstrap_hits_home_once(self):
        client = fake(routes=[(api_mod.HOME_URL, (200, HOME_HTML)),
                              ("search/type", (200, search_payload([])))])
        client.search("x")
        client.search("y")
        self.assertEqual(len([u for u in client._transport.urls() if u == api_mod.HOME_URL]), 1)

    def test_search_forwards_keyword_order_page_and_headers(self):
        client = fake(routes=[(api_mod.HOME_URL, (200, HOME_HTML)),
                              ("search/type", (200, search_payload([])))])
        client.search("luna say maybe", page=3, order="click")
        url = [u for u in client._transport.urls() if "search/type" in u][0]
        self.assertIn("keyword=luna%20say%20maybe", url)
        self.assertIn("order=click", url)
        self.assertIn("page=3", url)
        headers = client._transport.headers_of("search/type")
        self.assertEqual(headers["User-Agent"], DEFAULT_UA)
        self.assertIn("bilibili.com", headers["Referer"])
        self.assertIn("buvid3=FAKE-BUVID", headers["Cookie"])

    def test_unknown_order_falls_back_to_totalrank(self):
        client = fake(routes=[(api_mod.HOME_URL, (200, HOME_HTML)),
                              ("search/type", (200, search_payload([])))])
        client.search("x", order="乱写")
        self.assertIn("order=totalrank", client._transport.urls()[1])

    def test_cookie_file_is_sent_when_present(self):
        client = fake_with_cookie([(api_mod.HOME_URL, (200, HOME_HTML)),
                                   ("search/type", (200, search_payload([])))])
        client.search("x")
        self.assertIn("SESSDATA=MY-SESSION", client._transport.headers_of("search/type")["Cookie"])

    def test_media_headers_and_ffmpeg_args_carry_referer(self):
        client = fake(routes=[])
        headers = client.media_headers("BV1xx411c7mD")
        self.assertEqual(headers["Referer"], "https://www.bilibili.com/video/BV1xx411c7mD")
        args = client.ffmpeg_input_args("BV1xx411c7mD", "https://upos.example/x.mp4")
        self.assertEqual(args[0], "-user_agent")
        self.assertEqual(args[1], DEFAULT_UA)
        self.assertEqual(args[2], "-headers")
        self.assertEqual(args[3], "Referer: https://www.bilibili.com/video/BV1xx411c7mD\r\n")
        self.assertEqual(args[4], "-i")
        self.assertEqual(args[5], "https://upos.example/x.mp4")

    def test_from_config_can_disable_and_resolves_relative_path(self):
        self.assertIsNone(BilibiliApi.from_config({"enabled": False},
                                                 transport=FakeTransport([])))
        client = BilibiliApi.from_config({"cookie_file": "config/bilibili_cookie.json"},
                                        transport=FakeTransport([]))
        self.assertTrue(os.path.isabs(client.cookie_file))
        self.assertTrue(client.cookie_file.endswith(os.path.join("config", "bilibili_cookie.json")))


class TestSearch(unittest.TestCase):

    def items(self):
        return [{"type": "video", "bvid": "BV1", "title": "<em class=\"keyword\">初雪樱</em> OP",
                 "author": "UP1", "play": 12345, "like": 67, "duration": "3:4",
                 "pic": "//i0.example/a.jpg", "pubdate": 1700000000},
                {"type": "video", "bvid": "", "title": "没有 bvid 的行"},
                {"type": "video", "bvid": "BV2", "title": "第二条", "author": "UP2",
                 "play": "999", "duration": "233:45", "pic": "https://i1.example/b.jpg"}]

    def client(self):
        return fake(routes=[(api_mod.HOME_URL, (200, HOME_HTML)),
                            ("search/type", (200, search_payload(self.items())))])

    def test_normalises_items(self):
        out = self.client().search("初雪樱")
        self.assertEqual(out["num_pages"], 50)
        self.assertEqual(out["num_results"], 1000)
        self.assertEqual(len(out["items"]), 2)                 # 没 bvid 的那行被丢掉
        first = out["items"][0]
        self.assertEqual(first["bvid"], "BV1")
        self.assertEqual(first["title"], "初雪樱 OP")
        self.assertEqual(first["cover"], "https://i0.example/a.jpg")
        self.assertEqual(first["url"], "https://www.bilibili.com/video/BV1")
        self.assertEqual(first["duration_s"], 184)
        self.assertEqual(first["play"], 12345)
        second = out["items"][1]
        self.assertEqual(second["play"], 999)                   # 字符串数字也认
        self.assertEqual(second["cover"], "https://i1.example/b.jpg")
        self.assertEqual(second["duration_s"], 14025)

    def test_limit_truncates(self):
        out = self.client().search("x", limit=1)
        self.assertEqual(len(out["items"]), 1)

    def test_empty_keyword_is_refused(self):
        with self.assertRaises(BilibiliPayloadError):
            self.client().search("   ")


class TestErrors(unittest.TestCase):

    def client_with(self, route):
        return fake(routes=[(api_mod.HOME_URL, (200, HOME_HTML)), ("search/type", route)])

    def test_http_412_is_risk(self):
        with self.assertRaises(BilibiliRiskError) as caught:
            self.client_with((412, "<html>出错啦</html>")).search("x")
        self.assertIn("风控", str(caught.exception))
        self.assertIn("等", str(caught.exception))

    def test_code_minus_352_is_risk(self):
        body = '{"code":-352,"message":"风控校验失败","data":null}'
        with self.assertRaises(BilibiliRiskError):
            self.client_with((200, body)).search("x")

    def test_code_minus_404_is_not_found(self):
        body = '{"code":-404,"message":"啥都木有","data":null}'
        with self.assertRaises(BilibiliNotFound):
            self.client_with((200, body)).search("x")

    def test_non_json_is_payload_error(self):
        with self.assertRaises(BilibiliPayloadError):
            self.client_with((200, "<html>不是 JSON</html>")).search("x")

    def test_http_500_is_network_error(self):
        with self.assertRaises(BilibiliNetworkError):
            self.client_with((500, "boom")).search("x")

    def test_transport_failure_propagates(self):
        client = fake(routes=[(api_mod.HOME_URL, (200, HOME_HTML)),
                              ("search/type", BilibiliNetworkError("连不上 B 站（超时）"))])
        with self.assertRaises(BilibiliNetworkError):
            client.search("x")


class TestViewAndPlayurl(unittest.TestCase):

    def client(self, playurl_payload, **kwargs):
        return fake(routes=[(api_mod.HOME_URL, (200, HOME_HTML)),
                            ("web-interface/view", (200, VIEW_PAYLOAD)),
                            ("fnval=1", (200, playurl_payload)),
                            ("player/playurl", (200, playurl_payload))], **kwargs)

    def test_view(self):
        info = self.client(PLAIN_PAYLOAD).view("BV1xx411c7mD")
        self.assertEqual(info["cid"], 456)
        self.assertEqual(info["duration_s"], 265)
        self.assertEqual(info["page_count"], 2)
        self.assertEqual(info["owner"], "UP主")
        self.assertEqual(info["cover"], "https://i0.example/cover.jpg")
        self.assertEqual(info["title"], "标题高亮")

    def test_view_without_bvid(self):
        with self.assertRaises(BilibiliPayloadError):
            self.client(PLAIN_PAYLOAD).view("")

    def test_playurl_plain(self):
        stream = self.client(PLAIN_PAYLOAD).playurl("BV1xx411c7mD")
        self.assertEqual(stream["kind"], "plain")
        self.assertEqual(stream["quality"], 64)
        self.assertEqual(stream["quality_label"], "720P")
        self.assertEqual(stream["url"], "https://upos.example/x.mp4?e=1")
        self.assertEqual(stream["size"], 70276253)
        # 码率 = size*8/length（实测这条 ≈ 2114 kbps）
        self.assertAlmostEqual(stream["bps"], 70276253 * 8 / 265.904, delta=1000)
        self.assertEqual(stream["accept_quality"], [64, 16])

    def test_anonymous_only_asks_for_the_single_file(self):
        """没 cookie 时**不该**去问 DASH（实测匿名 DASH 只给 480P，白跑一次）。"""
        client = self.client(PLAIN_PAYLOAD)
        client.playurl("BV1xx411c7mD", cid=456)
        urls = [u for u in client._transport.urls() if "player/playurl" in u]
        self.assertEqual(len(urls), 1)
        self.assertIn("fnval=1", urls[0])

    def test_playurl_without_durl_and_without_cookie_is_honest(self):
        body = '{"code":0,"message":"OK","data":{"quality":64,"durl":[]}}'
        with self.assertRaises(BilibiliError) as caught:
            self.client(body).playurl("BV1xx411c7mD", cid=456)
        self.assertIn("cookie", str(caught.exception).lower())

    def test_dash_wins_when_it_is_clearer(self):
        """实测：`luna say maybe` 单文件只给 720P，而 DASH+登录给 1080P → 要挑 DASH。"""
        client = fake_with_cookie([(api_mod.HOME_URL, (200, HOME_HTML)),
                                   ("fnval=1", (200, PLAIN_PAYLOAD)),
                                   ("fnval=16", (200, DASH_PAYLOAD))])
        stream = client.playurl("BV1xx411c7mD", cid=456)
        self.assertEqual(stream["kind"], "dash")
        self.assertEqual(stream["quality_label"], "1080P")
        self.assertEqual(stream["video"]["id"], 80)
        self.assertEqual(stream["audio"]["bps"], 101000)
        self.assertEqual(stream["bps"], 1201000 + 101000)

    def test_single_file_wins_when_it_is_already_clearer(self):
        """实测：`Barricades` 匿名单文件就给 1080P，而 DASH 匿名只给 720P → 用单文件（省合流）。"""
        plain_1080 = ('{"code":0,"message":"OK","data":{"quality":80,"accept_quality":[80,16],'
                      '"durl":[{"length":100000,"size":10000000,'
                      '"url":"https://upos.example/1080.mp4"}]}}')
        client = fake_with_cookie([(api_mod.HOME_URL, (200, HOME_HTML)),
                                   ("fnval=1", (200, plain_1080)),
                                   ("fnval=16", (200, DASH_PAYLOAD))])
        stream = client.playurl("BV1xx411c7mD", cid=456)
        self.assertEqual(stream["kind"], "plain")
        self.assertEqual(stream["quality_label"], "1080P")

    def test_prefer_dash_forces_dash(self):
        client = fake_with_cookie([(api_mod.HOME_URL, (200, HOME_HTML)),
                                   ("fnval=1", (200, PLAIN_PAYLOAD)),
                                   ("fnval=16", (200, DASH_PAYLOAD))])
        self.assertEqual(client.playurl("BV1xx411c7mD", cid=456, prefer="dash")["kind"], "dash")

    def test_playurl_without_any_stream_is_honest(self):
        body = '{"code":0,"message":"OK","data":{"dash":{"video":[],"audio":[]}}}'
        client = self.client(body)
        with self.assertRaises(BilibiliError) as caught:
            client.playurl("BV1xx411c7mD", cid=456, prefer="dash")
        self.assertIn("版权", str(caught.exception))


class TestLogin(unittest.TestCase):

    def test_logged_in(self):
        client = fake(routes=[(api_mod.HOME_URL, (200, HOME_HTML)),
                              ("web-interface/nav", (200, '{"code":0,"data":{"isLogin":true}}'))])
        self.assertTrue(client.login()["logged_in"])

    def test_anonymous(self):
        client = fake(routes=[(api_mod.HOME_URL, (200, HOME_HTML)),
                              ("web-interface/nav", (200, '{"code":-101,"message":"账号未登录"}'))])
        out = client.login()
        self.assertFalse(out["logged_in"])
        self.assertIn("SESSDATA", out["why"])

    def test_network_problem_does_not_raise(self):
        client = fake(routes=[(api_mod.HOME_URL, (200, HOME_HTML)),
                              ("web-interface/nav", BilibiliNetworkError("连不上"))])
        out = client.login()
        self.assertFalse(out["logged_in"])
        self.assertTrue(out["why"])

    def test_result_is_cached(self):
        client = fake(routes=[(api_mod.HOME_URL, (200, HOME_HTML)),
                              ("web-interface/nav", (200, '{"code":0,"data":{"isLogin":true}}'))])
        client.login()
        client.login()
        self.assertEqual(len([u for u in client._transport.urls() if "nav" in u]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
