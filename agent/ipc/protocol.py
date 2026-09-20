# ============================================================================
#  agent/ipc/protocol.py — Agent ⇄ GUI 的 IPC 协议 (编解码 + 常量)
#
#  线格式 (与 docs/ipc-protocol.md 一一对应)
#  ---------------------------------------------------------------------------
#      Unix domain socket  /tmp/agent.sock
#      换行分隔 JSON (NDJSON): 每条消息是一个 JSON object, 以 \n 结束
#      编码 UTF-8
#
#      消息 = {"topic": str, "data": object, "timestamp": float}
#
#  为什么能"两侧无歧义解析"
#  ---------------------------------------------------------------------------
#  · **消息里不可能出现裸换行**: JSON 编码器会把字符串里的 \n 转义成 \\n
#    (Python json.dumps 如此, Qt QJsonDocument::toJson 也如此), 所以按 \n
#    切分永远是安全的, 不需要长度前缀, 也不需要转义层。
#  · **字段顺序无关**: JSON object 无序, 两侧都必须按 key 取, 不能依赖顺序。
#  · **未知字段忽略**: 这是没有版本号时唯一的向前兼容手段 —— 收到不认识的 key
#    直接忽略, 不要报错。
#  · **data 必须是 object**: 数组/标量一律判为非法消息 (见下面 _require_object)。
#
#  本模块只做编解码与常量, **不含 server / client** (按约定不做)。
#  真正的 socket 收发放在后续的 ipc server 里, 它负责: 按行读 → decode →
#  分发; 解析失败 → 记日志并丢弃该行, **不断开连接**。
# ============================================================================

from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional, Tuple, Union

__all__ = [
    # 传输层常量
    "SOCKET_PATH",
    "MESSAGE_SEPARATOR",
    "ENCODING",
    "MAX_LINE_BYTES",
    # topic (Agent -> GUI)
    "TOPIC_STATUS",
    "TOPIC_LLM",
    "TOPIC_WALLPAPER",
    "TOPIC_MUSIC",
    "TOPICS",
    # command (GUI -> Agent)
    "COMMAND_SWITCH_MODE",
    "COMMAND_NEXT_WALLPAPER",
    "COMMAND_CHAT_INPUT",
    "COMMAND_NEXT_BILIBILI",
    "COMMANDS",
    # 取值域
    "MODE_SLEEP",
    "MODE_IDLE",
    "MODE_STUDY",
    "MODE_GAME",
    "MODES",
    # 编解码
    "encode",
    "decode",
    "decode_full",
    # 错误
    "IpcProtocolError",
    "MalformedJsonError",
    "InvalidMessageError",
]


# ---------------------------------------------------------------------------
#  传输层
# ---------------------------------------------------------------------------
#: Unix domain socket 路径 (Agent 监听, GUI 连接)
SOCKET_PATH = "/tmp/agent.sock"

#: 消息分隔符 —— 每条消息以它结束
MESSAGE_SEPARATOR = b"\n"

#: 线上编码。两侧都必须按 UTF-8 处理 (中文文本会直接出现在 payload 里)
ENCODING = "utf-8"

#: 单行最大字节数。NDJSON 没有长度前缀, 所以必须有个上限, 否则对端一直不发
#: 换行就能把内存吃光。超限的行按"非法消息"处理: 丢弃 + 记日志, 不断开连接。
MAX_LINE_BYTES = 1 << 20          # 1 MiB


# ---------------------------------------------------------------------------
#  Topic: Agent -> GUI
# ---------------------------------------------------------------------------
TOPIC_STATUS = "status"
TOPIC_LLM = "llm"
TOPIC_WALLPAPER = "wallpaper"
TOPIC_MUSIC = "music"

#: 全部 topic (Agent -> GUI)
TOPICS = (TOPIC_STATUS, TOPIC_LLM, TOPIC_WALLPAPER, TOPIC_MUSIC)


# ---------------------------------------------------------------------------
#  Command: GUI -> Agent
# ---------------------------------------------------------------------------
COMMAND_SWITCH_MODE = "switch_mode"
COMMAND_NEXT_WALLPAPER = "next_wallpaper"
COMMAND_CHAT_INPUT = "chat_input"
COMMAND_NEXT_BILIBILI = "next_bilibili"

#: 全部 command (GUI -> Agent)
COMMANDS = (
    COMMAND_SWITCH_MODE,
    COMMAND_NEXT_WALLPAPER,
    COMMAND_CHAT_INPUT,
    COMMAND_NEXT_BILIBILI,
)


# ---------------------------------------------------------------------------
#  取值域
# ---------------------------------------------------------------------------
#: status.mode / switch_mode.value 的合法取值 —— **全大写**。
#:
#: ⚠ 注意与 agent/core/state_machine.py 的 State 区分: 那边的 .value 是**小写**
#:   ("sleep"/"idle"/"study"/"game"), 因为它是内部表示与配置文件里的写法。
#:   IPC 上用大写是协议约定 (GUI 直接拿去显示/比较)。转换发生在 ipc server 那一层,
#:   不要把两套大小写混着传。
MODE_SLEEP = "SLEEP"
MODE_IDLE = "IDLE"
MODE_STUDY = "STUDY"
MODE_GAME = "GAME"

#: 全部模式 (顺序与状态机一致)
MODES = (MODE_SLEEP, MODE_IDLE, MODE_STUDY, MODE_GAME)


# ---------------------------------------------------------------------------
#  错误
# ---------------------------------------------------------------------------
class IpcProtocolError(ValueError):
    """IPC 协议层错误 (编解码不合法)。

    继承 ValueError: 与其他模块一致, 调用方既能 except IpcProtocolError 精确捕获,
    也能 except ValueError 统一处理。

    接收方应当**捕获它 -> 记日志 -> 丢弃该行**, 并且**不断开连接** ——
    一条坏消息不代表对端坏了。
    """


class MalformedJsonError(IpcProtocolError):
    """不是合法 JSON (或编码不是 UTF-8)。"""


class InvalidMessageError(IpcProtocolError):
    """是合法 JSON, 但不是合法消息 (缺字段/类型不对/data 不是 object……)。"""


# ---------------------------------------------------------------------------
#  编码: Python -> 线
# ---------------------------------------------------------------------------
def encode(
    topic: str,
    data: Dict[str, Any],
    timestamp: Optional[float] = None,
) -> bytes:
    """把一条消息编码成**可以直接写进 socket 的完整字节** (含结尾换行)。

    @param topic     消息名 (TOPIC_* 或 COMMAND_*); 非空字符串
    @param data      JSON object (dict); **不能**是 list/str/int 等标量
    @param timestamp Unix epoch 秒 (浮点)。None 时取 time.time();
                     显式传入主要是为了测试可复现
    @return UTF-8 字节, 以 b"\\n" 结尾
    @raise IpcProtocolError topic/data/timestamp 不合法, 或 data 无法 JSON 序列化

    @note 用紧凑分隔符 ("," ":") 且 ensure_ascii=False:
          前者与 Qt 的 QJsonDocument::Compact 输出风格一致 (省字节),
          后者让中文原样以 UTF-8 出现, 便于两边用 tcpdump/日志直接看。
    """
    if not isinstance(topic, str) or not topic.strip():
        raise InvalidMessageError("topic must be a non-empty string, got %r" % (topic,))

    if not isinstance(data, dict):
        # 这是协议硬约束: data 只允许 object。放行标量会让两侧的取值代码
        # 到处写类型分支, 而且 JSON 里 array/object 的边界最容易两边理解不一致。
        raise InvalidMessageError(
            "data must be a JSON object (dict), got %s" % type(data).__name__
        )

    if timestamp is None:
        timestamp = time.time()
    elif isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        raise InvalidMessageError(
            "timestamp must be a number (Unix epoch seconds), got %s"
            % type(timestamp).__name__
        )

    envelope = {"topic": topic, "data": data, "timestamp": float(timestamp)}

    try:
        text = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        # 例如 data 里有 set / bytes / 自定义对象 —— 明确说是负载不能序列化,
        # 而不是把一个裸 TypeError 抛给调用方
        raise InvalidMessageError("data is not JSON-serializable: %r" % (exc,)) from exc

    return text.encode(ENCODING) + MESSAGE_SEPARATOR


# ---------------------------------------------------------------------------
#  解码: 线 -> Python
# ---------------------------------------------------------------------------
def decode(line: Union[bytes, bytearray, memoryview, str]) -> Tuple[str, dict]:
    """解一条消息, 返回 (topic, data)。

    这是**签名要求的简版**: 它丢掉 timestamp。需要时间戳的调用方用 decode_full()。

    @param line 一条消息的字节 (结尾的 \\n 可有可无; 也容忍 \\r\\n)
    @return (topic, data)
    @raise MalformedJsonError   不是合法 JSON / 不是 UTF-8
    @raise InvalidMessageError  结构不合法 (缺字段、类型不对、data 不是 object)
    @raise IpcProtocolError     超过 MAX_LINE_BYTES

    @note 接收方的标准动作是 catch -> log -> 丢弃该行, 连接继续用。
    """
    topic, data, _ = decode_full(line)
    return topic, data


def decode_full(
    line: Union[bytes, bytearray, memoryview, str],
) -> Tuple[str, dict, float]:
    """同 decode(), 但把 timestamp 也返回。

    @return (topic, data, timestamp)
    """
    raw = _to_bytes(line)

    # 结尾换行可有可无: 按行读的实现通常会把它去掉, 直接喂整段的会留着。
    # 只剥结尾, 不剥开头 —— 前面的空白属于消息内容的一部分 (会让 JSON 解析失败,
    # 那是应该报错的)。
    raw = raw.rstrip(b"\r\n")

    if not raw:
        raise InvalidMessageError("empty message")

    if len(raw) > MAX_LINE_BYTES:
        raise IpcProtocolError(
            "message too large: %d bytes > %d" % (len(raw), MAX_LINE_BYTES)
        )

    try:
        text = raw.decode(ENCODING)
    except UnicodeDecodeError as exc:
        raise MalformedJsonError("message is not valid %s: %r" % (ENCODING, exc)) from exc

    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise MalformedJsonError("invalid JSON: %r" % (exc,)) from exc

    if not isinstance(payload, dict):
        raise InvalidMessageError(
            "message must be a JSON object, got %s" % type(payload).__name__
        )

    # 未知字段直接忽略 (没有版本号时唯一的向前兼容手段)
    topic = payload.get("topic")
    if not isinstance(topic, str) or not topic.strip():
        raise InvalidMessageError("missing or invalid 'topic' field: %r" % (topic,))

    if "data" not in payload:
        # 即使没有参数也必须给 {} —— 明确比"缺了就当空"更好查
        raise InvalidMessageError("missing 'data' field (use {} for no arguments)")

    data = payload["data"]
    if not isinstance(data, dict):
        raise InvalidMessageError(
            "'data' must be a JSON object, got %s" % type(data).__name__
        )

    timestamp = payload.get("timestamp")
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        raise InvalidMessageError(
            "missing or invalid 'timestamp' (Unix epoch seconds): %r" % (timestamp,)
        )

    return topic, data, float(timestamp)


# ---------------------------------------------------------------------------
#  内部
# ---------------------------------------------------------------------------
def _to_bytes(line: Union[bytes, bytearray, memoryview, str]) -> bytes:
    """归一成 bytes。str 按 UTF-8 编码 (方便测试里直接写字符串)。"""
    if isinstance(line, bytes):
        return line
    if isinstance(line, (bytearray, memoryview)):
        return bytes(line)
    if isinstance(line, str):
        return line.encode(ENCODING)
    raise InvalidMessageError(
        "message must be bytes or str, got %s" % type(line).__name__
    )
