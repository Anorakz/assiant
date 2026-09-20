# ============================================================================
#  tests/test_sunshine_client.py — Sunshine HTTPS 握手客户端
#
#  全部无网: 解析 / 拼串是纯函数; 网络错误用 mock 掉的 urlopen 构造。
#  真机通路由"PC 本机连 127.0.0.1:47984"和板端的 B3 验证, 不在单测里做。
# ============================================================================

import io
import socket
import sys
import unittest
from pathlib import Path
from unittest import mock
from urllib import error as urlerror

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.net import (  # noqa: E402
    DEFAULT_HTTPS_PORT,
    DEFAULT_UNIQUE_ID,
    SunshineClient,
    SunshineConfigError,
    SunshineError,
    build_launch_query,
    is_already_running,
    parse_app_list,
    parse_launch,
    parse_server_info,
    status_code_of,
    stream_mode,
    surround_audio_info,
)

# ---------------------------------------------------------------------------
#  固定响应样本 (形态照 Sunshine 实际输出; <App> 整个打在一行是真实行为)
# ---------------------------------------------------------------------------

SERVERINFO_OK = """<root status_code="200">
  <hostname>DESKTOP-RK3568</hostname>
  <appversion>7.1.431.-1</appversion>
  <GfeVersion>3.23.0.74</GfeVersion>
  <uniqueid>%s</uniqueid>
  <ServerCodecModeSupport>3841</ServerCodecModeSupport>
  <PairStatus>1</PairStatus>
</root>""" % DEFAULT_UNIQUE_ID

SERVERINFO_UNPAIRED = '<root status_code="200"><appversion>7.1.431.-1</appversion><PairStatus>0</PairStatus></root>'

APPLIST_OK = """<root status_code="200">
<App><AppTitle>Desktop</AppTitle><ID>1</ID></App>
<App><AppTitle>Steam Big Picture</AppTitle><ID>2</ID></App>
</root>"""

APPLIST_EMPTY = '<root status_code="200"></root>'

LAUNCH_OK = """<root status_code="200">
<sessionUrl0>rtsp://192.168.137.1:48010</sessionUrl0>
<gamesession>1234</gamesession>
</root>"""

LAUNCH_BUSY = '<root status_code="503" status_message="Failed to start application: no active session"/>'

#: 下面两条是**真机抓到的原文** (PC -> 本机 Sunshine, 主机上另有一个 Moonlight
#: 客户端正在串流), 不是编出来的样本。
LAUNCH_ALREADY_RUNNING = '<root status_code="400" status_message="An app is already running on this host"/>'
RESUME_OK = ('<?xml version="1.0" encoding="utf-8"?>\n'
             '<root status_code="200"><sessionUrl0>rtsp://127.0.0.1:48010</sessionUrl0>'
             '<resume>1</resume></root>')


class _FakeResponse:
    """urlopen 的替身: 只要能用 with 包起来并且 read() 出字节。"""

    def __init__(self, body):
        self._body = body if isinstance(body, bytes) else body.encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestSurroundAudioInfo(unittest.TestCase):
    def test_stereo_matches_the_curl_reference(self):
        # verify-authorized.sh 里写死的字面量就是 196610; 两边必须一致,
        # 否则 Python 这一侧发出去的启动参数与"验证过的通路"不是一回事。
        self.assertEqual(surround_audio_info(), 196610)

    def test_matches_the_limelight_macro_formula(self):
        # Limelight.h: SURROUNDAUDIOINFO_FROM_AUDIO_CONFIGURATION(x) = mask<<16 | count
        self.assertEqual(surround_audio_info(2, 0x3), (0x3 << 16) | 2)
        self.assertEqual(surround_audio_info(6, 0x3F), (0x3F << 16) | 6)

    def test_rejects_bad_channels(self):
        for args in ((0, 0x3), (2, 0), (-1, 0x3)):
            with self.assertRaises(SunshineConfigError):
                surround_audio_info(*args)


class TestStreamMode(unittest.TestCase):
    def test_builds_the_expected_shape(self):
        self.assertEqual(stream_mode(1280, 720, 60), "1280x720x60")

    def test_rejects_non_positive_and_bool(self):
        for args in ((0, 720, 60), (1280, 0, 60), (1280, 720, 0), (True, 720, 60)):
            with self.assertRaises(SunshineConfigError):
                stream_mode(*args)


class TestParseServerInfo(unittest.TestCase):
    def test_reads_all_fields(self):
        info = parse_server_info(SERVERINFO_OK)
        self.assertEqual(info.hostname, "DESKTOP-RK3568")
        self.assertEqual(info.app_version, "7.1.431.-1")
        self.assertEqual(info.gfe_version, "3.23.0.74")
        self.assertEqual(info.unique_id, DEFAULT_UNIQUE_ID)
        self.assertEqual(info.codec_mode_support, 3841)
        self.assertEqual(info.pair_status, 1)
        self.assertEqual(info.status_code, 200)
        self.assertTrue(info.paired)

    def test_unpaired_is_not_paired(self):
        info = parse_server_info(SERVERINFO_UNPAIRED)
        self.assertEqual(info.pair_status, 0)
        self.assertFalse(info.paired)

    def test_missing_fields_degrade_instead_of_raising(self):
        info = parse_server_info("<root status_code=\"200\"></root>")
        self.assertEqual(info.app_version, "")
        self.assertEqual(info.codec_mode_support, 0)
        self.assertEqual(info.pair_status, -1)  # -1 = 没说, 不等于"未配对"

    def test_repr_does_not_dump_the_whole_xml(self):
        self.assertNotIn("<root", repr(parse_server_info(SERVERINFO_OK)))


class TestStatusCode(unittest.TestCase):
    def test_reads_the_attribute(self):
        self.assertEqual(status_code_of('<root status_code="200">'), 200)
        self.assertEqual(status_code_of(LAUNCH_BUSY), 503)

    def test_garbage_is_minus_one(self):
        self.assertEqual(status_code_of("<root>"), -1)
        self.assertEqual(status_code_of('<root status_code="abc">'), -1)


class TestParseAppList(unittest.TestCase):
    def test_one_line_app_blocks_are_parsed(self):
        # Sunshine 把整个 <App> 打在一行, 不能要求标签之间有换行
        apps = parse_app_list(APPLIST_OK)
        self.assertEqual([(a.app_id, a.title) for a in apps],
                         [("1", "Desktop"), ("2", "Steam Big Picture")])

    def test_empty_list(self):
        self.assertEqual(parse_app_list(APPLIST_EMPTY), [])

    def test_entry_without_id_is_skipped(self):
        xml = "<root><App><AppTitle>NoId</AppTitle></App><App><ID>9</ID></App></root>"
        apps = parse_app_list(xml)
        self.assertEqual([a.app_id for a in apps], ["9"])


class TestParseLaunch(unittest.TestCase):
    def test_success(self):
        result = parse_launch(LAUNCH_OK)
        self.assertEqual(result.session_url, "rtsp://192.168.137.1:48010")
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.ok)

    def test_status_message_is_an_attribute(self):
        # 这是 Sunshine 特有的形态: 用 <status_message> 元素是取不到的
        result = parse_launch(LAUNCH_BUSY)
        self.assertIn("no active session", result.status_message)
        self.assertFalse(result.ok)

    def test_status_message_element_form_is_not_mistaken_for_the_attribute(self):
        xml = '<root status_code="503"><status_message>boom</status_message></root>'
        self.assertEqual(parse_launch(xml).status_message, "")

    def test_no_session_url_is_not_ok(self):
        self.assertFalse(parse_launch('<root status_code="200"></root>').ok)


class TestBuildLaunchQuery(unittest.TestCase):
    #: verify-authorized.sh 里那一串参数, 顺序也照它
    REFERENCE_KEYS = [
        "uniqueid", "appid", "mode", "rikey", "rikeyid",
        "localAudioPlayMode", "surroundAudioInfo", "gcmap", "hdrMode", "sops",
    ]

    def test_matches_the_curl_reference(self):
        query = build_launch_query(DEFAULT_UNIQUE_ID, "1", "1280x720x60")
        keys = [part.split("=", 1)[0] for part in query.split("&")]
        self.assertEqual(keys, self.REFERENCE_KEYS)
        self.assertIn("surroundAudioInfo=196610", query)
        self.assertIn("rikey=%s" % ("0" * 32), query)
        self.assertIn("mode=1280x720x60", query)

    def test_appends_the_native_extension_parameters(self):
        # LiGetLaunchUrlQueryParameters() 现在返回 "&corever=1"
        with_extra = build_launch_query(DEFAULT_UNIQUE_ID, "1", "1280x720x60", "&corever=1")
        self.assertTrue(with_extra.endswith("&corever=1"))
        self.assertIn("sops=0&corever=1", with_extra)

    def test_extra_without_leading_ampersand_is_still_joined(self):
        query = build_launch_query(DEFAULT_UNIQUE_ID, "1", "1280x720x60", "corever=1")
        self.assertIn("sops=0&corever=1", query)

    def test_empty_extra_leaves_no_trailing_ampersand(self):
        self.assertFalse(build_launch_query(DEFAULT_UNIQUE_ID, "1", "1280x720x60").endswith("&"))

    def test_required_arguments_are_checked(self):
        for args in (("", "1", "1280x720x60"), (DEFAULT_UNIQUE_ID, "", "1280x720x60"),
                     (DEFAULT_UNIQUE_ID, "1", "")):
            with self.assertRaises(SunshineConfigError):
                build_launch_query(*args)


class TestClientConstruction(unittest.TestCase):
    def test_defaults_and_repr(self):
        client = SunshineClient("192.168.137.1")
        self.assertEqual(client.https_port, DEFAULT_HTTPS_PORT)
        self.assertEqual(client.unique_id, DEFAULT_UNIQUE_ID)
        self.assertIn("192.168.137.1", repr(client))

    def test_rejects_bad_host_and_port(self):
        for kwargs in ({"host": ""}, {"host": "  "}):
            with self.assertRaises(SunshineConfigError):
                SunshineClient(**kwargs)
        for port in (0, 65536, -1, "47984"):
            with self.assertRaises(SunshineConfigError):
                SunshineClient("h", https_port=port)

    def test_rejects_bad_timeout(self):
        with self.assertRaises(SunshineConfigError):
            SunshineClient("h", timeout=0)

    def test_cert_and_key_must_come_together(self):
        with self.assertRaises(SunshineConfigError) as ctx:
            SunshineClient("h", cert="client.pem")
        self.assertIn("成对", str(ctx.exception))

    def test_missing_cert_file_is_reported_with_its_path(self):
        missing = str(_PROJECT_ROOT / "creds" / "definitely-not-here.pem")
        with self.assertRaises(SunshineConfigError) as ctx:
            SunshineClient("h", cert=missing, key=missing)
        self.assertIn("definitely-not-here.pem", str(ctx.exception))

    def test_broken_cert_file_fails_at_construction_not_at_handshake(self):
        # 证书坏了应该在启动时就报出来, 而不是等握手才报
        broken = _PROJECT_ROOT / "tests" / "_broken_cert_for_test.pem"
        broken.write_text("this is not a certificate\n", encoding="utf-8")
        try:
            with self.assertRaises(SunshineConfigError) as ctx:
                SunshineClient("h", cert=str(broken), key=str(broken))
            self.assertIn("证书加载失败", str(ctx.exception))
        finally:
            broken.unlink()

    def test_client_without_cert_is_allowed(self):
        # Sunshine 需要客户端证书, 但"没给证书"是调用方的事; 这里只保证不炸
        self.assertIsNotNone(SunshineClient("h")._context)


class TestClientNetworkErrors(unittest.TestCase):
    """错误信息必须可操作 —— 这一层出问题时人只看到一行日志。"""

    def _client(self):
        return SunshineClient("192.168.137.1", timeout=1.0)

    def _raising(self, exc):
        return mock.patch("agent.net.sunshine_client.urlrequest.urlopen", side_effect=exc)

    def test_401_mentions_authorisation_and_the_host_message(self):
        body = '<root status_code="401" status_message="Invalid uniqueid"/>'
        exc = urlerror.HTTPError("https://h/serverinfo", 401, "Unauthorized", {},
                                 io.BytesIO(body.encode("utf-8")))
        with self._raising(exc):
            with self.assertRaises(SunshineError) as ctx:
                self._client().server_info()
        text = str(ctx.exception)
        self.assertIn("401", text)
        self.assertIn("授权", text)
        self.assertIn("Invalid uniqueid", text)

    def test_404_hints_at_the_wrong_port(self):
        exc = urlerror.HTTPError("https://h/serverinfo", 404, "Not Found", {},
                                 io.BytesIO(b"nope"))
        with self._raising(exc):
            with self.assertRaises(SunshineError) as ctx:
                self._client().server_info()
        self.assertIn("47989", str(ctx.exception))

    def test_connection_refused_points_at_sunshine(self):
        reason = urlerror.URLError(ConnectionRefusedError(111, "Connection refused"))
        with self._raising(reason):
            with self.assertRaises(SunshineError) as ctx:
                self._client().server_info()
        text = str(ctx.exception)
        self.assertIn("连接被拒", text)
        self.assertIn("47984", text)

    def test_dns_failure_is_distinguished(self):
        reason = urlerror.URLError(socket.gaierror(-2, "Name or service not known"))
        with self._raising(reason):
            with self.assertRaises(SunshineError) as ctx:
                self._client().server_info()
        self.assertIn("解析失败", str(ctx.exception))

    def test_timeout_is_reported_with_the_timeout_value(self):
        with self._raising(socket.timeout("timed out")):
            with self.assertRaises(SunshineError) as ctx:
                self._client().server_info()
        self.assertIn("超时", str(ctx.exception))

    def test_successful_get_returns_the_body(self):
        with mock.patch("agent.net.sunshine_client.urlrequest.urlopen",
                        return_value=_FakeResponse(SERVERINFO_OK)):
            info = self._client().server_info()
        self.assertEqual(info.app_version, "7.1.431.-1")

    def test_serverinfo_without_appversion_is_actionable(self):
        # 没有 appversion 就没法启动会话 —— 要在握手这一步就说清楚
        with mock.patch("agent.net.sunshine_client.urlrequest.urlopen",
                        return_value=_FakeResponse('<root status_code="200"></root>')):
            with self.assertRaises(SunshineError) as ctx:
                self._client().server_info()
        self.assertIn("appversion", str(ctx.exception))


class TestClientHighLevel(unittest.TestCase):
    """用假的 _get 驱动上层逻辑, 不碰网络。"""

    def _client(self, body):
        client = SunshineClient("192.168.137.1")
        client._get = mock.Mock(return_value=body)
        return client

    def test_app_list(self):
        apps = self._client(APPLIST_OK).app_list()
        self.assertEqual([a.app_id for a in apps], ["1", "2"])

    def test_empty_app_list_is_an_error_not_an_empty_result(self):
        with self.assertRaises(SunshineError):
            self._client(APPLIST_EMPTY).app_list()

    def test_resolve_by_title_ignoring_case(self):
        self.assertEqual(self._client(APPLIST_OK).resolve_app_id("desktop"), "1")
        self.assertEqual(self._client(APPLIST_OK).resolve_app_id("Steam Big Picture"), "2")

    def test_resolve_by_exact_appid(self):
        self.assertEqual(self._client(APPLIST_OK).resolve_app_id("2"), "2")

    def test_unknown_app_lists_what_the_host_has(self):
        with self.assertRaises(SunshineError) as ctx:
            self._client(APPLIST_OK).resolve_app_id("Firefox")
        text = str(ctx.exception)
        self.assertIn("Firefox", text)
        self.assertIn("Desktop", text)  # 列出可用项, 而不是静默换了别的应用

    def test_launch_returns_the_session_url(self):
        result = self._client(LAUNCH_OK).launch("1", "1280x720x60")
        self.assertEqual(result.session_url, "rtsp://192.168.137.1:48010")
        self.assertTrue(result.ok)

    def test_launch_failure_carries_the_host_reason(self):
        with self.assertRaises(SunshineError) as ctx:
            self._client(LAUNCH_BUSY).launch("1", "1280x720x60")
        text = str(ctx.exception)
        self.assertIn("503", text)
        self.assertIn("no active session", text)

    def test_launch_query_carries_the_unique_id_and_mode(self):
        client = self._client(LAUNCH_OK)
        client.launch("7", "1920x1080x60")
        path = client._get.call_args[0][0]
        self.assertTrue(path.startswith("/launch?"))
        self.assertIn("uniqueid=%s" % DEFAULT_UNIQUE_ID, path)
        self.assertIn("appid=7", path)
        self.assertIn("mode=1920x1080x60", path)


class TestIsAlreadyRunning(unittest.TestCase):
    """判定"主机上已有应用在跑" —— 是它就得退到 /resume, 而不是报错。"""

    def test_real_sunshine_message_is_detected(self):
        self.assertTrue(is_already_running(400, "An app is already running on this host"))

    def test_detected_from_the_body_when_status_message_is_missing(self):
        self.assertTrue(is_already_running(-1, "", LAUNCH_ALREADY_RUNNING))

    def test_bare_400_counts_as_a_conflict(self):
        # 文案变了也不漏判; 万一 400 其实是别的原因, /resume 会再失败一次并把
        # 双方原话都带出来, 不会把真问题吞掉
        self.assertTrue(is_already_running(400, ""))

    def test_genuine_failures_are_not_conflicts(self):
        self.assertFalse(is_already_running(503, "Failed to start application: no active session"))
        self.assertFalse(is_already_running(401, "Invalid uniqueid"))
        self.assertFalse(is_already_running(200, ""))
        self.assertFalse(is_already_running(-1, ""))

    def test_resume_success_body_is_not_a_conflict(self):
        self.assertFalse(is_already_running(200, parse_launch(RESUME_OK).status_message, RESUME_OK))


class TestStartSession(unittest.TestCase):
    """start_session(): 先 /launch, 主机忙就退到 /resume (与别的客户端共存)。"""

    def _client(self, bodies):
        """按调用顺序吐出给定响应体 (第 N 次调用用第 N 条)。"""
        client = SunshineClient("127.0.0.1")
        client._get = mock.Mock(side_effect=list(bodies))
        return client

    def test_idle_host_uses_launch_and_only_launch(self):
        client = self._client([LAUNCH_OK])
        start = client.start_session("1", "1280x720x60")
        self.assertEqual(start.endpoint, "launch")
        self.assertEqual(start.session_url, "rtsp://192.168.137.1:48010")
        self.assertEqual(client._get.call_count, 1)
        self.assertIn("/launch?", client._get.call_args[0][0])

    def test_busy_host_falls_back_to_resume(self):
        client = self._client([LAUNCH_ALREADY_RUNNING, RESUME_OK])
        start = client.start_session("881448767", "1280x720x60")
        self.assertEqual(start.endpoint, "resume")
        self.assertEqual(start.session_url, "rtsp://127.0.0.1:48010")
        paths = [call[0][0] for call in client._get.call_args_list]
        self.assertTrue(paths[0].startswith("/launch?"))
        self.assertTrue(paths[1].startswith("/resume?"))
        self.assertIn("appid=881448767", paths[1])

    def test_genuine_launch_failure_never_touches_resume(self):
        client = self._client([LAUNCH_BUSY])
        with self.assertRaises(SunshineError) as ctx:
            client.start_session("1", "1280x720x60")
        self.assertIn("no active session", str(ctx.exception))
        # 关键: 不能因为 launch 失败就去动别人的会话
        self.assertEqual(client._get.call_count, 1)

    def test_both_failing_reports_both_reasons(self):
        resumed = '<root status_code="503" status_message="resume refused"/>'
        client = self._client([LAUNCH_ALREADY_RUNNING, resumed])
        with self.assertRaises(SunshineError) as ctx:
            client.start_session("1", "1280x720x60")
        text = str(ctx.exception)
        self.assertIn("already running", text)
        self.assertIn("resume refused", text)

    def test_repr_shows_which_endpoint_was_used(self):
        start = self._client([LAUNCH_ALREADY_RUNNING, RESUME_OK]).start_session("1", "1280x720x60")
        self.assertIn("resume", repr(start))

    def test_resume_returns_the_session_url(self):
        client = self._client([RESUME_OK])
        result = client.resume("881448767", "1280x720x60")
        self.assertTrue(result.ok)
        self.assertEqual(result.session_url, "rtsp://127.0.0.1:48010")
        self.assertIn("/resume?", client._get.call_args[0][0])

    def test_resume_failure_is_reported_against_resume(self):
        client = self._client([LAUNCH_BUSY])
        with self.assertRaises(SunshineError) as ctx:
            client.resume("1", "1280x720x60")
        self.assertIn("/resume", str(ctx.exception))

    def test_launch_still_raises_on_a_busy_host(self):
        # 公开的 launch() 保持"一根筋": 主机忙就是失败, 共存交给 start_session()
        with self.assertRaises(SunshineError) as ctx:
            self._client([LAUNCH_ALREADY_RUNNING]).launch("1", "1280x720x60")
        self.assertIn("already running", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
