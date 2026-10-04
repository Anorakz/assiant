#!/usr/bin/env python3
"""
tests/test_ipc_protocol.py — agent/ipc/protocol.py 单测

运行:
    python tests/test_ipc_protocol.py

这一套的重点是**把线格式钉死**: C++ 侧要照着 docs/ipc-protocol.md 实现,
所以这里断言的是"确切的字节", 而不是"能来回解就行"。任何无意的格式变化
(分隔符、字段顺序、转义方式) 都会被这里拦住。

覆盖:
  常量        topic / command / mode 的取值与文档一致
  encode      信封字段、紧凑分隔符、结尾换行、UTF-8、data 必须是 object、
              timestamp 自动/显式/非法
  decode      结尾 \n 可有可无、容忍 \r\n、str/bytes 输入
  命令信封    encode_command/decode_command: {"action","payload"} **无 timestamp**,
              字段顺序与 GUI 一致, 拒收 topic 形态的老格式 (Phase 6 D1)
  字节级契约  与文档 §5 的 hex 逐字节一致
  错误        JSON 非法 / 非 UTF-8 / 顶层非 object / 缺字段 / data 非 object /
              timestamp 非法 / 超长
  向前兼容    未知字段忽略; 未知 topic 不报错 (由接收方决定忽略)
  日程事实    schedule topic / query_schedule command 的形状能 round-trip (P 系列)
  与文档一致  §3/§4 表里列出的每个 topic/command 都能 round-trip
"""

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.core.music import MusicPlayer  # noqa: E402
from agent.ipc import protocol as p  # noqa: E402


# ===========================================================================
#  常量
# ===========================================================================
class TestConstants(unittest.TestCase):
    def test_topics(self):
        self.assertEqual(p.TOPIC_STATUS, "status")
        self.assertEqual(p.TOPIC_LLM, "llm")
        self.assertEqual(p.TOPIC_WALLPAPER, "wallpaper")
        self.assertEqual(p.TOPIC_MUSIC, "music")
        self.assertEqual(p.TOPIC_SCHEDULE, "schedule")
        self.assertEqual(p.TOPIC_BILIBILI, "bilibili")
        # T14-2: 一次 set_config 的回执（GUI 靠它知道"写进去没有"）
        self.assertEqual(p.TOPIC_CONFIG_RESULT, "config_result")
        # T14-3: 一次 llm_service（启停本机 llama-server）的回执
        self.assertEqual(p.TOPIC_SERVICE_RESULT, "service_result")
        # T14-9: 本机 WiFi 链路（kind=status|scan|ack）
        self.assertEqual(p.TOPIC_WIFI, "wifi")
        self.assertEqual(p.TOPICS,
                         ("status", "llm", "wallpaper", "music", "schedule", "bilibili",
                          "config_result", "service_result", "wifi"))

    def test_commands(self):
        self.assertEqual(p.COMMAND_SWITCH_MODE, "switch_mode")
        self.assertEqual(p.COMMAND_CHAT_INPUT, "chat_input")
        self.assertEqual(p.COMMAND_QUERY_SCHEDULE, "query_schedule")
        # T14-2: 让 Agent 改配置真源（GUI 不再自己写, 见 docs/adr/0005）
        self.assertEqual(p.COMMAND_SET_CONFIG, "set_config")
        # T14-3: 让 Agent 跑 llm/scripts/*.sh（模型页那两颗按钮）
        self.assertEqual(p.COMMAND_LLM_SERVICE, "llm_service")
        # T14-9: 让 Agent 操作 wlan0（nmcli）—— 名字不能也叫 "wifi"（topic 已占用）
        self.assertEqual(p.COMMAND_WIFI, "wifi_control")
        self.assertNotEqual(p.COMMAND_WIFI, p.TOPIC_WIFI)
        self.assertEqual(p.COMMANDS,
                         ("switch_mode", "chat_input",
                          "next_bilibili", "prev_bilibili", "bilibili_pick",
                          "bilibili_viewport", "video_state", "video_control", "query_schedule",
                          "music_play_pause", "music_next", "music_prev", "music_stop",
                          "set_config", "llm_service", "wifi_control"))

    def test_bilibili_commands(self):
        # T11-6: B 站那几个按钮/回报。⚠ 与音乐同一条口径: 它们**不决定放什么**
        # （队列内容由对话或画面认出的游戏决定）; `video_state` 是 GUI 的**回报**。
        self.assertEqual(p.COMMAND_NEXT_BILIBILI, "next_bilibili")
        self.assertEqual(p.COMMAND_PREV_BILIBILI, "prev_bilibili")
        self.assertEqual(p.COMMAND_BILIBILI_PICK, "bilibili_pick")
        self.assertEqual(p.COMMAND_BILIBILI_VIEWPORT, "bilibili_viewport")
        self.assertEqual(p.COMMAND_VIDEO_STATE, "video_state")
        for name in (p.COMMAND_NEXT_BILIBILI, p.COMMAND_PREV_BILIBILI,
                     p.COMMAND_BILIBILI_PICK, p.COMMAND_BILIBILI_VIEWPORT,
                     p.COMMAND_VIDEO_STATE):
            self.assertIn(name, p.COMMANDS)

    def test_video_control_is_a_new_command(self):
        """T11-10f: 让**播放器**播放/暂停 —— 与音乐的 `music_play_pause` 不同, 这条是**新增**的。

        为什么必须新增: GUI 那颗播放/暂停按钮是"本地点", 协议里原来没有任何一条能让
        Agent/CLI 去按它; 而 `video_state` 是 GUI 往上的**回报**, 不能兼职当命令。
        """
        self.assertEqual(p.COMMAND_VIDEO_CONTROL, "video_control")
        self.assertIn(p.COMMAND_VIDEO_CONTROL, p.COMMANDS)
        self.assertNotEqual(p.COMMAND_VIDEO_CONTROL, p.COMMAND_VIDEO_STATE)

    def test_music_commands(self):
        # T8-4: 音乐按钮。⚠ 它们**不决定放什么**（那是对话的事）—— 只做"暂停/继续"和
        # "在 chat 挑出来的候选顺序里前后走"（语义见 protocol.py 那几个常量的注释）。
        self.assertEqual(p.COMMAND_MUSIC_PLAY_PAUSE, "music_play_pause")
        self.assertEqual(p.COMMAND_MUSIC_NEXT, "music_next")
        self.assertEqual(p.COMMAND_MUSIC_PREV, "music_prev")
        self.assertEqual(p.COMMAND_MUSIC_STOP, "music_stop")
        for name in (p.COMMAND_MUSIC_PLAY_PAUSE, p.COMMAND_MUSIC_NEXT,
                     p.COMMAND_MUSIC_PREV, p.COMMAND_MUSIC_STOP):
            self.assertIn(name, p.COMMANDS)

    def test_the_manual_wallpaper_command_is_gone(self):
        # T7-3: 换壁纸只走对话, 这条命令连同 GUI 的「下一张」按钮一起删了
        self.assertFalse(hasattr(p, "COMMAND_NEXT_WALLPAPER"))
        self.assertNotIn("next_wallpaper", p.COMMANDS)

    def test_topics_and_commands_do_not_overlap(self):
        # 同一条连接双向都用 topic 字段, 名字重了就没法区分方向
        self.assertEqual(set(p.TOPICS) & set(p.COMMANDS), set())

    def test_modes_are_uppercase(self):
        self.assertEqual(p.MODES, ("SLEEP", "IDLE", "STUDY", "GAME"))
        for mode in p.MODES:
            self.assertEqual(mode, mode.upper())

    def test_transport_constants(self):
        self.assertEqual(p.SOCKET_PATH, "/tmp/agent.sock")
        self.assertEqual(p.MESSAGE_SEPARATOR, b"\n")
        self.assertEqual(p.ENCODING, "utf-8")
        self.assertEqual(p.MAX_LINE_BYTES, 1 << 20)


# ===========================================================================
#  encode
# ===========================================================================
class TestEncode(unittest.TestCase):
    def test_envelope_fields(self):
        raw = p.encode(p.TOPIC_STATUS, {"mode": "STUDY", "connected": True},
                       timestamp=1234567890.123)
        payload = json.loads(raw.decode("utf-8"))
        self.assertEqual(set(payload), {"topic", "data", "timestamp"})
        self.assertEqual(payload["topic"], "status")
        self.assertEqual(payload["data"], {"mode": "STUDY", "connected": True})
        self.assertEqual(payload["timestamp"], 1234567890.123)

    def test_ends_with_newline(self):
        raw = p.encode(p.TOPIC_LLM, {"text": "hi"}, timestamp=1.0)
        self.assertTrue(raw.endswith(b"\n"))
        # 只有结尾一个换行
        self.assertEqual(raw.count(b"\n"), 1)

    def test_compact_separators(self):
        # 紧凑格式与 Qt 的 QJsonDocument::Compact 一致, 且没有多余空格
        text = p.encode(p.TOPIC_LLM, {"text": "hi"}, timestamp=1.0).decode()
        self.assertEqual(text, '{"topic":"llm","data":{"text":"hi"},"timestamp":1.0}\n')
        self.assertNotIn(": ", text)
        self.assertNotIn(", ", text)

    def test_key_order_is_topic_data_timestamp(self):
        # 顺序不是协议要求 (JSON object 无序), 但固定下来便于比对与阅读
        text = p.encode(p.TOPIC_MUSIC, {"title": "x"}, timestamp=1.0).decode()
        self.assertTrue(text.index('"topic"') < text.index('"data"'))
        self.assertTrue(text.index('"data"') < text.index('"timestamp"'))

    def test_timestamp_defaults_to_now(self):
        import time
        before = time.time()
        raw = p.encode(p.TOPIC_STATUS, {})
        after = time.time()
        _, _, ts = p.decode_full(raw)
        self.assertGreaterEqual(ts, before)
        self.assertLessEqual(ts, after)

    def test_timestamp_is_float(self):
        raw = p.encode(p.TOPIC_STATUS, {}, timestamp=1234567890)
        payload = json.loads(raw.decode())
        self.assertIsInstance(payload["timestamp"], float)

    def test_explicit_int_timestamp_accepted(self):
        _, _, ts = p.decode_full(p.encode(p.TOPIC_STATUS, {}, timestamp=1))
        self.assertEqual(ts, 1.0)

    def test_unicode_is_raw_utf8(self):
        raw = p.encode(p.TOPIC_LLM, {"text": "已经切换到学习模式。"}, timestamp=1.0)
        self.assertIn("已经切换到学习模式。".encode("utf-8"), raw,
                      "应当以原样 UTF-8 出现 (ensure_ascii=False), 而不是 \\uXXXX")
        self.assertNotIn(b"\\u", raw)

    def test_unicode_roundtrip(self):
        _, data = p.decode(p.encode(p.TOPIC_LLM, {"text": "你好 🎮"}, timestamp=1.0))
        self.assertEqual(data["text"], "你好 🎮")

    # ------------------------------------------------------- data 校验 ---
    def test_data_must_be_object(self):
        for bad in ([1, 2], "text", 3, 1.5, None, True):
            with self.subTest(bad=bad):
                with self.assertRaises(p.InvalidMessageError):
                    p.encode(p.TOPIC_LLM, bad)

    def test_empty_data_object_is_fine(self):
        _, data = p.decode(p.encode(p.COMMAND_QUERY_SCHEDULE, {}))
        self.assertEqual(data, {})

    def test_nested_data_allowed(self):
        data = {"path": "/x.jpg", "index": 3, "meta": {"w": 100, "h": 50},
                "tags": ["a", "b"]}
        _, got = p.decode(p.encode(p.TOPIC_WALLPAPER, data))
        self.assertEqual(got, data)

    def test_non_serializable_data_raises_protocol_error(self):
        # 给个明确的协议错误, 而不是裸 TypeError
        with self.assertRaises(p.InvalidMessageError):
            p.encode(p.TOPIC_LLM, {"bad": {1, 2, 3}})      # set 不能序列化
        with self.assertRaises(p.InvalidMessageError):
            p.encode(p.TOPIC_LLM, {"bad": b"bytes"})

    # ------------------------------------------------------- topic 校验 ---
    def test_topic_must_be_non_empty_string(self):
        for bad in ("", "   ", None, 123, [], {}):
            with self.subTest(bad=bad):
                with self.assertRaises(p.InvalidMessageError):
                    p.encode(bad, {})

    def test_unknown_topic_is_allowed_on_encode(self):
        # 发送方可以发协议外的新 topic (向前兼容靠接收方忽略)
        raw = p.encode("future_topic", {"x": 1}, timestamp=1.0)
        self.assertEqual(p.decode(raw)[0], "future_topic")

    # --------------------------------------------------- timestamp 校验 ---
    def test_bad_timestamp_rejected(self):
        # None 表示"用当前时间", 所以不在非法之列
        for bad in ("123", "abc", [], {}):
            with self.subTest(bad=bad):
                with self.assertRaises(p.InvalidMessageError):
                    p.encode(p.TOPIC_STATUS, {}, timestamp=bad)

    def test_bool_timestamp_rejected(self):
        # bool 是 int 的子类, 不特判就会被当成 1/0
        with self.assertRaises(p.InvalidMessageError):
            p.encode(p.TOPIC_STATUS, {}, timestamp=True)


# ===========================================================================
#  decode
# ===========================================================================
class TestDecode(unittest.TestCase):
    def _line(self, **overrides):
        payload = {"topic": "status", "data": {"mode": "IDLE"}, "timestamp": 1.5}
        payload.update(overrides)
        return json.dumps(payload).encode("utf-8")

    def test_basic(self):
        topic, data = p.decode(self._line())
        self.assertEqual(topic, "status")
        self.assertEqual(data, {"mode": "IDLE"})

    def test_decode_full_returns_timestamp(self):
        topic, data, ts = p.decode_full(self._line())
        self.assertEqual(ts, 1.5)

    def test_trailing_newline_optional(self):
        raw = self._line()
        self.assertEqual(p.decode(raw), p.decode(raw + b"\n"))

    def test_crlf_tolerated(self):
        self.assertEqual(p.decode(self._line() + b"\r\n"), p.decode(self._line()))

    def test_str_input_accepted(self):
        self.assertEqual(p.decode(self._line().decode("utf-8")),
                         p.decode(self._line()))

    def test_bytearray_and_memoryview_accepted(self):
        raw = self._line()
        self.assertEqual(p.decode(bytearray(raw)), p.decode(raw))
        self.assertEqual(p.decode(memoryview(raw)), p.decode(raw))

    def test_extra_whitespace_inside_json_is_fine(self):
        raw = b'{ "topic" : "status" , "data" : { } , "timestamp" : 1.0 }'
        self.assertEqual(p.decode(raw), ("status", {}))

    def test_field_order_does_not_matter(self):
        raw = b'{"timestamp":1.0,"data":{"a":1},"topic":"llm"}'
        self.assertEqual(p.decode(raw), ("llm", {"a": 1}))

    def test_unknown_fields_ignored(self):
        raw = b'{"topic":"llm","data":{"text":"x"},"timestamp":1.0,"future":"whatever"}'
        self.assertEqual(p.decode(raw), ("llm", {"text": "x"}))

    def test_unknown_topic_is_not_an_error(self):
        # 由接收方决定忽略; 协议层不报错
        raw = b'{"topic":"brand_new","data":{},"timestamp":1.0}'
        self.assertEqual(p.decode(raw)[0], "brand_new")

    def test_int_timestamp_accepted(self):
        raw = b'{"topic":"llm","data":{},"timestamp":1700000000}'
        self.assertEqual(p.decode_full(raw)[2], 1700000000.0)

    # --------------------------------------------------------- 错误 ---
    def test_invalid_json(self):
        for bad in (b"{not json", b'{"topic":}', b"", b"   ", b"\n", b"null"):
            with self.subTest(bad=bad):
                with self.assertRaises(p.IpcProtocolError):
                    p.decode(bad)

    def test_top_level_not_object(self):
        for bad in (b"[1,2,3]", b'"a string"', b"42", b"true"):
            with self.subTest(bad=bad):
                with self.assertRaises(p.InvalidMessageError):
                    p.decode(bad)

    def test_not_utf8(self):
        with self.assertRaises(p.MalformedJsonError):
            p.decode(b'{"topic":"\xff\xfe","data":{},"timestamp":1.0}')

    def test_missing_topic(self):
        with self.assertRaises(p.InvalidMessageError):
            p.decode(b'{"data":{},"timestamp":1.0}')

    def test_topic_wrong_type(self):
        for bad in (b'{"topic":123,"data":{},"timestamp":1.0}',
                    b'{"topic":"","data":{},"timestamp":1.0}',
                    b'{"topic":null,"data":{},"timestamp":1.0}'):
            with self.subTest(bad=bad):
                with self.assertRaises(p.InvalidMessageError):
                    p.decode(bad)

    def test_missing_data_is_rejected(self):
        # 没有参数也必须给 {} —— 明确比"缺了就当空"更好查
        with self.assertRaises(p.InvalidMessageError):
            p.decode(b'{"topic":"next_wallpaper","timestamp":1.0}')

    def test_data_wrong_type(self):
        for bad in (b'{"topic":"llm","data":"text","timestamp":1.0}',
                    b'{"topic":"llm","data":[1,2],"timestamp":1.0}',
                    b'{"topic":"llm","data":3,"timestamp":1.0}',
                    b'{"topic":"llm","data":null,"timestamp":1.0}'):
            with self.subTest(bad=bad):
                with self.assertRaises(p.InvalidMessageError):
                    p.decode(bad)

    def test_missing_timestamp(self):
        with self.assertRaises(p.InvalidMessageError):
            p.decode(b'{"topic":"llm","data":{}}')

    def test_timestamp_wrong_type(self):
        for bad in (b'{"topic":"llm","data":{},"timestamp":"1.0"}',
                    b'{"topic":"llm","data":{},"timestamp":null}',
                    b'{"topic":"llm","data":{},"timestamp":true}'):
            with self.subTest(bad=bad):
                with self.assertRaises(p.InvalidMessageError):
                    p.decode(bad)

    def test_oversize_line_rejected(self):
        big = b'{"topic":"llm","data":{"text":"' + b"x" * (p.MAX_LINE_BYTES + 10) + b'"}}'
        with self.assertRaises(p.IpcProtocolError) as ctx:
            p.decode(big)
        self.assertIn("too large", str(ctx.exception))

    def test_just_under_limit_is_accepted(self):
        # 边界: 恰好等于上限可以通过
        filler = b"x" * (p.MAX_LINE_BYTES - 60)
        raw = b'{"topic":"llm","data":{"t":"' + filler + b'"},"timestamp":1.0}'
        if len(raw) <= p.MAX_LINE_BYTES:
            self.assertEqual(p.decode(raw)[0], "llm")

    def test_non_bytes_input_rejected(self):
        for bad in (123, None, [], {}):
            with self.subTest(bad=bad):
                with self.assertRaises(p.IpcProtocolError):
                    p.decode(bad)

    def test_error_hierarchy(self):
        # 调用方既能 except IpcProtocolError, 也能 except ValueError
        self.assertTrue(issubclass(p.IpcProtocolError, ValueError))
        self.assertTrue(issubclass(p.MalformedJsonError, p.IpcProtocolError))
        self.assertTrue(issubclass(p.InvalidMessageError, p.IpcProtocolError))
        with self.assertRaises(ValueError):
            p.decode(b"{bad")


# ===========================================================================
#  字节级契约 (C++ 侧要照这个实现)
# ===========================================================================
class TestWireContract(unittest.TestCase):
    def test_documented_status_bytes(self):
        """与 docs/ipc-protocol.md §5 的 hex 逐字节一致。"""
        expected_hex = (
            "7b 22 74 6f 70 69 63 22 3a 22 73 74 61 74 75 73 22 2c 22 64 61 74 61 22 3a "
            "7b 22 6d 6f 64 65 22 3a 22 53 54 55 44 59 22 2c 22 63 6f 6e 6e 65 63 74 65 "
            "64 22 3a 74 72 75 65 7d 2c 22 74 69 6d 65 73 74 61 6d 70 22 3a 31 32 33 34 "
            "35 36 37 38 39 30 2e 31 32 33 7d 0a"
        )
        expected = bytes(int(x, 16) for x in expected_hex.split())
        actual = p.encode(p.TOPIC_STATUS,
                          {"mode": p.MODE_STUDY, "connected": True},
                          timestamp=1234567890.123)
        self.assertEqual(actual, expected)

    def test_documented_examples_roundtrip(self):
        """文档 §3/§4 里给的每条示例都要能解出来。"""
        examples = [
            (p.TOPIC_STATUS, {"mode": "STUDY", "connected": True}),
            (p.TOPIC_LLM, {"text": "已经切换到学习模式。"}),
            (p.TOPIC_WALLPAPER, {"path": "/home/kickpi/wallpapers/04.jpg", "index": 3}),
            (p.TOPIC_MUSIC, {"track_id": "186016", "title": "晴天", "artist": "周杰伦",
                             "album": "叶惠美", "position_s": 70.3, "duration_s": 269.0,
                             "playing": True, "plays": 12, "tags": {"mood": ["calm"]},
                             "lyric_ok": True,
                             "lyric_lines": [{"t": 0.0, "text": "作词 : 周杰伦", "tr": ""},
                                             {"t": 28.95, "text": "故事的小黄花", "tr": ""}],
                             "lyric_rev": 1, "lyric_reason": ""}),
            (p.TOPIC_SCHEDULE, {
                "kind": "fired",
                "event": {"state": "study", "date": "2026-09-22",
                          "scheduled_at": "2026-09-22T13:00",
                          "fired_at": "2026-09-22T13:00:03",
                          "actions": [{"type": "state", "state": "study", "ok": True,
                                       "steps": [{"from": "idle", "to": "study"}],
                                       "why": "日程: 13:00 → study",
                                       "current": "study"}]},
            }),
            (p.TOPIC_SCHEDULE, {"kind": "state", "now": "2026-09-22T13:05:00",
                                "limit": 50, "fired": []}),
            (p.COMMAND_SWITCH_MODE, {"value": "GAME"}),
            (p.COMMAND_CHAT_INPUT, {"text": "帮我看看现在几点了"}),
            (p.COMMAND_NEXT_BILIBILI, {}),
            (p.COMMAND_QUERY_SCHEDULE, {}),
        ]
        for topic, data in examples:
            with self.subTest(topic=topic):
                self.assertEqual(p.decode(p.encode(topic, data, timestamp=1.0)),
                                 (topic, data))

    def test_every_documented_topic_and_command_is_covered(self):
        # 防止"文档加了条目但测试漏了"（9 个 topic + 16 条命令 = 25）
        documented = set(p.TOPICS) | set(p.COMMANDS)
        self.assertEqual(len(documented), 25)

class TestTheMusicRowMatchesTheCode(unittest.TestCase):
    """§3 的 `music` 行必须列全 `MusicPlayer.snapshot()` 真正推的字段（T15-16）。

    ⚠ 为什么值得一条机械守卫: 这份文档自称"唯一真源"，但在 T15-16 之前它只写了
      `title`/`playing` —— `artist`/`album`/`position_s`/`plays`/`tags` 代码早就在推了。
      人写文档会漏，所以让测试来对账（两个方向都查：文档多写 / 代码多推）。
    """

    DOC = _PROJECT_ROOT / "docs" / "ipc-protocol.md"

    def documented_fields(self):
        """§3 表里 `music` 那几行（含 `| |` 续行）第 2 列的反引号字段名。"""
        rows = []
        for line in self.DOC.read_text(encoding="utf-8").splitlines():
            if re.match(r"\|\s*`music`\s*\|", line):
                rows.append(line)
                continue
            if rows and line.startswith("| |"):
                rows.append(line)
                continue
            if rows:
                break
        fields = set()
        for row in rows:
            cells = row.split("|")
            if len(cells) > 2:
                fields.update(re.findall(r"`([a-z_]+)`", cells[2]))
        return fields

    def code_fields(self):
        """`snapshot()` 的键（它**不碰 PC**，所以 cli 给 None 就行）。"""
        with tempfile.TemporaryDirectory() as folder:
            player = MusicPlayer(None, os.path.join(folder, "music_library.jsonl"))
            return set(player.snapshot())

    def test_the_two_sides_are_the_same_set(self):
        documented = self.documented_fields()
        code = self.code_fields()
        self.assertEqual(
            documented, code,
            "§3 的 music 行与 MusicPlayer.snapshot() 对不上：\n"
            "  · 文档写了、代码没推: %s\n"
            "  · 代码推了、文档没写: %s"
            % (sorted(documented - code) or "无", sorted(code - documented) or "无"))

    def test_the_comparison_is_not_vacuous(self):
        """反空转: 提取规则坏了（正则/行首匹配变了）不能变成"两边都空 -> 绿"。"""
        documented = self.documented_fields()
        self.assertGreaterEqual(len(documented), 13, "只从文档里认出 %d 个字段" % len(documented))
        for must in ("title", "playing", "position_s", "lyric_ok", "lyric_lines",
                     "lyric_rev", "lyric_reason"):
            self.assertIn(must, documented, "提取结果里少了 %s" % must)
        self.assertGreaterEqual(len(self.code_fields()), 13)


class TestLineFormat(unittest.TestCase):
    """NDJSON 行格式: 一条消息一行、多条能按换行切（原"字节级契约"那组的尾巴）。"""

    def test_one_message_is_exactly_one_line(self):
        # NDJSON 的前提: 消息里不能出现裸换行
        for text in ("line1\nline2", "tab\there", "quote\"inside", "back\\slash",
                     "中文\n换行"):
            with self.subTest(text=text):
                raw = p.encode(p.TOPIC_LLM, {"text": text}, timestamp=1.0)
                self.assertEqual(raw.count(b"\n"), 1, "换行必须被转义, 只留结尾分隔符")
                self.assertTrue(raw.endswith(b"\n"))
                self.assertEqual(p.decode(raw)[1]["text"], text)

    def test_multiple_messages_can_be_split_by_newline(self):
        stream = (p.encode(p.TOPIC_LLM, {"text": "a"}, timestamp=1.0)
                  + p.encode(p.TOPIC_MUSIC, {"title": "b"}, timestamp=2.0))
        lines = [ln for ln in stream.split(b"\n") if ln]
        self.assertEqual(len(lines), 2)
        self.assertEqual(p.decode(lines[0])[0], "llm")
        self.assertEqual(p.decode(lines[1])[0], "music")


# ===========================================================================
#  命令方向的信封 (Phase 6 D1: 以 GUI 的实际实现为准)
# ===========================================================================
class TestCommandEnvelope(unittest.TestCase):
    """GUI -> Agent 的命令是 {"action","payload"}, **没有 timestamp**。

    C++ 侧 (gui/src/services/local_client.cpp 的 sendCommand) 就是这么发的, 所以这里
    同样断言**确切的字节** —— 两侧必须逐字节一致, 否则联调时"测试全绿、真 GUI 一来
    就哑"。D1 之前 Agent 只认 {"topic","data","timestamp"}, 真 GUI 的命令会被当坏行丢掉。
    """

    def test_documented_byte_example_is_exact(self):
        """docs/ipc-protocol.md §5 里那段命令方向的十六进制, 必须与代码一致。

        文档里贴了 52 字节的逐字节示例 (含中文原样 UTF-8 e4 bd a0 e5 a5 bd);
        这里把它钉住 —— 否则改一次格式, 文档里的 hex 就变成骗人的。
        """
        raw = p.encode_command(p.COMMAND_CHAT_INPUT, {"text": "你好"})
        self.assertEqual(len(raw), 52, "文档 §5 写的是 52 字节")
        self.assertEqual(raw,
                         b'{"action":"chat_input","payload":{"text":"\xe4\xbd\xa0\xe5\xa5\xbd"}}\n')

    def test_field_name_constants(self):
        self.assertEqual(p.ACTION_FIELD, "action")
        self.assertEqual(p.PAYLOAD_FIELD, "payload")

    def test_exact_bytes_match_the_gui(self):
        raw = p.encode_command(p.COMMAND_CHAT_INPUT, {"text": "hi"})
        self.assertEqual(raw, b'{"action":"chat_input","payload":{"text":"hi"}}\n')

    def test_field_order_is_action_then_payload(self):
        # Qt 的 QJsonObject 按插入顺序序列化, GUI 先插 action 再插 payload
        text = p.encode_command(p.COMMAND_CHAT_INPUT, {"text": "x"}).decode()
        self.assertLess(text.index('"action"'), text.index('"payload"'))

    def test_there_is_no_timestamp_field(self):
        payload = json.loads(p.encode_command(p.COMMAND_CHAT_INPUT, {}).decode())
        self.assertEqual(set(payload), {"action", "payload"})

    def test_none_payload_becomes_empty_object(self):
        self.assertEqual(p.encode_command(p.COMMAND_QUERY_SCHEDULE),
                         b'{"action":"query_schedule","payload":{}}\n')

    def test_round_trip_every_documented_command(self):
        for action in p.COMMANDS:
            with self.subTest(action=action):
                # 只有 chat_input 有参数, 其余都必须吃 {}
                payload = {"text": "你好"} if action == p.COMMAND_CHAT_INPUT else {}
                self.assertEqual(p.decode_command(p.encode_command(action, payload)),
                                 (action, payload))

    def test_unicode_and_newlines_are_escaped(self):
        raw = p.encode_command(p.COMMAND_CHAT_INPUT, {"text": "你好\n换行"})
        self.assertEqual(raw.count(b"\n"), 1, "换行必须转义, 只留结尾分隔符")
        self.assertIn("你好".encode("utf-8"), raw)
        self.assertEqual(p.decode_command(raw)[1]["text"], "你好\n换行")

    def test_decode_tolerates_separator_crlf_and_str_input(self):
        body = '{"action":"chat_input","payload":{"text":"x"}}'
        for line in (body, body + "\n", body + "\r\n"):
            with self.subTest(line=repr(line)):
                self.assertEqual(p.decode_command(line), ("chat_input", {"text": "x"}))

    def test_topic_shaped_command_is_rejected(self):
        # D1 的分水岭: 命令方向只认一种格式。两种都收 = "文档说 A、代码也收 B",
        # 正是归一化要收敛掉的东西。
        raw = p.encode(p.COMMAND_CHAT_INPUT, {"text": "x"}, timestamp=1.0)
        with self.assertRaises(p.InvalidMessageError) as ctx:
            p.decode_command(raw)
        self.assertIn("action", str(ctx.exception))

    def test_decode_rejects_bad_shapes(self):
        for bad in (b'{"payload":{}}',                        # 缺 action
                    b'{"action":"","payload":{}}',             # action 为空
                    b'{"action":"   ","payload":{}}',          # action 全空白
                    b'{"action":123,"payload":{}}',            # action 非字符串
                    b'{"action":"chat_input"}',                # 缺 payload
                    b'{"action":"chat_input","payload":[]}',   # payload 非 object
                    b'{"action":"chat_input","payload":null}'):
            with self.subTest(bad=bad):
                with self.assertRaises(p.InvalidMessageError):
                    p.decode_command(bad)

    def test_decode_rejects_malformed_json_and_non_object(self):
        for bad in (b"{not json", b"", b"null", b"[1,2]"):
            with self.subTest(bad=bad):
                with self.assertRaises(p.IpcProtocolError):
                    p.decode_command(bad)

    def test_encode_rejects_bad_action(self):
        for action in ("", "   ", None, 123):
            with self.subTest(action=repr(action)):
                with self.assertRaises(p.InvalidMessageError):
                    p.encode_command(action, {})

    def test_encode_rejects_non_object_payload(self):
        for payload in ([], "x", 1, True):
            with self.subTest(payload=repr(payload)):
                with self.assertRaises(p.InvalidMessageError):
                    p.encode_command(p.COMMAND_CHAT_INPUT, payload)

    def test_encode_rejects_unserializable_payload(self):
        with self.assertRaises(p.InvalidMessageError):
            p.encode_command(p.COMMAND_CHAT_INPUT, {"bad": {1, 2, 3}})

    def test_encode_rejects_oversize_line(self):
        raw = p.encode_command(p.COMMAND_CHAT_INPUT,
                               {"text": "x" * (p.MAX_LINE_BYTES + 10)})
        with self.assertRaises(p.IpcProtocolError):
            p.decode_command(raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
