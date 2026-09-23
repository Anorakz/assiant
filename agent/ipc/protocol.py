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
    "TOPIC_SCHEDULE",
    "TOPICS",
    # command (GUI -> Agent)
    "COMMAND_SWITCH_MODE",
    "COMMAND_CHAT_INPUT",
    "COMMAND_NEXT_BILIBILI",
    "COMMAND_QUERY_SCHEDULE",
    "COMMAND_MUSIC_PLAY_PAUSE",
    "COMMAND_MUSIC_NEXT",
    "COMMAND_MUSIC_PREV",
    "COMMAND_MUSIC_STOP",
    "COMMANDS",
    # 命令方向的信封字段名 (GUI 实际实现为准)
    "ACTION_FIELD",
    "PAYLOAD_FIELD",
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
    "encode_command",
    "decode_command",
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

#: 日程**触发事实** (P 系列)。data.kind 二取一:
#:     "state"  应答 query_schedule 的快照 (data.fired = 事实数组)
#:     "fired"  刚刚真的触发了一条 (data.event = 那一条事实)
#: ⚠ 它传的不是"日程表" (那个在 config 里), 而是**运行中 Agent 真发生过的事**:
#:   进程重启即清零。详见 docs/ipc-protocol.md §3。
TOPIC_SCHEDULE = "schedule"

#: 全部 topic (Agent -> GUI)
TOPICS = (TOPIC_STATUS, TOPIC_LLM, TOPIC_WALLPAPER, TOPIC_MUSIC, TOPIC_SCHEDULE)


# ---------------------------------------------------------------------------
#  Command: GUI -> Agent
# ---------------------------------------------------------------------------
COMMAND_SWITCH_MODE = "switch_mode"
COMMAND_CHAT_INPUT = "chat_input"
COMMAND_NEXT_BILIBILI = "next_bilibili"

#: ⚠ T7-3 删掉了 `next_wallpaper` 命令（T3 加的）: 换壁纸**只走对话**
#: （LLM 工具 `next_wallpaper`, 见 agent/tools/wallpaper.py）。手动按钮"只能按文件名
#: 翻下一张"，而标签化之后"换成什么样"该由自然语言说 —— 见 docs/ipc-protocol.md §4。
#: GUI 侧同步删掉了主区那个「下一张」按钮与 `--next-wallpaper-demo`。
#: 线格式上这只是一个"不再有人发的命令名" —— 老客户端发过来会被当成未知命令忽略（回一句说明）。

#: 问一句"你最近触发过哪些日程" (payload 必须是 {})。
#: 应答**就是**随后那条 topic=TOPIC_SCHEDULE / kind="state" 的推送 —— 与 switch_mode
#: 的应答是随后那条 status 一样, **没有请求 id** (协议没有版本号与关联字段)。
COMMAND_QUERY_SCHEDULE = "query_schedule"

# ---- 音乐（T8-4）----
#: ⚠ 这三个按钮**不决定放什么**: "下一首放哪首"由**对话**决定（chat 挑候选 → play）。
#: 这里的语义分别是:
#:   `music_play_pause` 暂停/继续**当前这首**（无参）;
#:   `music_next` / `music_prev` 在**chat 上次挑出来的候选顺序**里前后走一格;
#:   还没有候选队列时回一句说明（"先从对话里挑一次歌"）—— 不假装换了一首。
COMMAND_MUSIC_PLAY_PAUSE = "music_play_pause"
COMMAND_MUSIC_NEXT = "music_next"
COMMAND_MUSIC_PREV = "music_prev"
COMMAND_MUSIC_STOP = "music_stop"

#: 全部 command (GUI -> Agent)
COMMANDS = (
    COMMAND_SWITCH_MODE,
    COMMAND_CHAT_INPUT,
    COMMAND_NEXT_BILIBILI,
    COMMAND_QUERY_SCHEDULE,
    COMMAND_MUSIC_PLAY_PAUSE,
    COMMAND_MUSIC_NEXT,
    COMMAND_MUSIC_PREV,
    COMMAND_MUSIC_STOP,
)

# 命令方向的信封字段名。**以 GUI 的实际实现为准** (Phase 6 决策 1):
#     gui/src/services/local_client.cpp 的 sendCommand() 发的是
#         {"action": "chat_input", "payload": {"text": "..."}} + '\n'
# 而 Agent -> GUI 的推送仍是 {"topic", "data", "timestamp"} (GUI 也是按那个解的)。
# 两个方向字段名不同, 是照实实现的现状, 不是笔误 —— 见 docs/ipc-protocol.md §2/§4。
#: 命令名所在的字段
ACTION_FIELD = "action"
#: 命令参数所在的字段 (必须是 JSON object; 没有参数就给 {})
PAYLOAD_FIELD = "payload"


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


def encode_command(action: str, payload: Optional[Dict[str, Any]] = None) -> bytes:
    """把一条 GUI -> Agent 的命令编码成**可以直接写进 socket 的完整字节**。

    信封就是 GUI 那一种: ``{"action": ..., "payload": {...}}`` —— **没有 timestamp**,
    因为 GUI 不发它。这里不"顺手"补一个: 多一个字段就多一处两边可能不一致的地方,
    而这一层的意义正是让两侧发的东西一模一样。

    @param action  命令名 (COMMAND_*); 非空字符串
    @param payload JSON object (dict); None 视为 {}
    @return UTF-8 字节, 以 b"\\n" 结尾
    @raise InvalidMessageError action/payload 不合法, 或 payload 无法 JSON 序列化
    """
    if not isinstance(action, str) or not action.strip():
        raise InvalidMessageError("action must be a non-empty string, got %r" % (action,))

    body = {} if payload is None else payload
    if not isinstance(body, dict):
        raise InvalidMessageError(
            "payload must be a JSON object (dict), got %s" % type(body).__name__
        )

    envelope = {ACTION_FIELD: action, PAYLOAD_FIELD: body}

    try:
        text = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise InvalidMessageError("payload is not JSON-serializable: %r" % (exc,)) from exc

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
    payload = _parse_object(line)

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


def decode_command(
    line: Union[bytes, bytearray, memoryview, str],
) -> Tuple[str, dict]:
    """解一条 GUI -> Agent 的命令, 返回 (action, payload)。

    与 decode_full() 的区别就是信封: 命令方向是 ``{"action", "payload"}``,
    **没有 timestamp** —— 以 GUI 的实际实现为准 (Phase 6 决策 1)。

    @raise MalformedJsonError   不是合法 JSON / 不是 UTF-8
    @raise InvalidMessageError  缺 action/payload, 或 payload 不是 object
    @raise IpcProtocolError     超过 MAX_LINE_BYTES

    @note 收到 topic 形态的命令 (老格式 / 发错了方向) 会在这里报"缺 action",
          接收方的标准动作是 log + 丢弃该行, 连接继续用 —— 不会静默当成成功。
    """
    payload = _parse_object(line)

    action = payload.get(ACTION_FIELD)
    if not isinstance(action, str) or not action.strip():
        raise InvalidMessageError("missing or invalid 'action' field: %r" % (action,))

    if PAYLOAD_FIELD not in payload:
        # 与 'data' 同样的取舍: 没有参数也必须显式给 {}, 缺字段要吵出来
        raise InvalidMessageError(
            "missing 'payload' field (use {} for no arguments)"
        )

    body = payload[PAYLOAD_FIELD]
    if not isinstance(body, dict):
        raise InvalidMessageError(
            "'payload' must be a JSON object, got %s" % type(body).__name__
        )

    return action, body


# ---------------------------------------------------------------------------
#  内部
# ---------------------------------------------------------------------------
def _parse_object(line: Union[bytes, bytearray, memoryview, str]) -> dict:
    """线 -> JSON object (dict)。推送与命令共用的前半段 (长度/UTF-8/JSON/object)。

    两种信封的差别只在字段名与必填项, 所以校验前半段只有这一份 —— 两处各写一遍
    迟早会漂移。
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

    return payload


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
