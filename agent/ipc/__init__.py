# ============================================================================
#  agent/ipc/__init__.py — IPC 层 (Agent ⇄ GUI)
#
#      协议:   protocol.py      编解码 + 常量     (线格式见 docs/ipc-protocol.md)
#      Server: local_server.py   Unix socket server (Agent 是 server, GUI 是 client)
#      Client: local_client.py   Python 测试客户端 (**不是**生产 GUI, 那个是 C++)
#      本文件: build_ipc()       main.py 的接入点 —— 把 server 和 bus 接起来
#
#  ⚠ 本层目前只做**收发**。Agent 主动推送 status/llm/wallpaper/music 还需要拿到
#    状态机与回复路径, 而 main.py 的约定是 factory(bus, config) —— 只给了 bus。
#    所以 build_ipc() 现在只接线 GUI -> Agent 这一个方向, 出方向留给下一步
#    (见 build_ipc 的注释)。没有假装推送, 也没有偷偷改 main.py 的接口约定。
# ============================================================================

import logging
from typing import Any, Dict, Optional

from .local_client import (
    CLIENT_SUPPORTED,
    IpcClientError,
    LocalClient,
)
from .local_server import (
    DEFAULT_BACKLOG,
    DEFAULT_MODE,
    DEFAULT_QUEUE_SIZE,
    PROBE_TIMEOUT,
    UNIX_SOCKET_SUPPORTED,
    IpcServerError,
    LocalServer,
    NullServer,
    mode_from_wire,
    mode_to_wire,
)
from .protocol import (
    ACTION_FIELD,
    COMMAND_CHAT_INPUT,
    COMMAND_NEXT_BILIBILI,
    COMMAND_NEXT_WALLPAPER,
    COMMAND_SWITCH_MODE,
    COMMANDS,
    ENCODING,
    MAX_LINE_BYTES,
    MESSAGE_SEPARATOR,
    MODE_GAME,
    MODE_IDLE,
    MODE_SLEEP,
    MODE_STUDY,
    MODES,
    PAYLOAD_FIELD,
    SOCKET_PATH,
    TOPIC_LLM,
    TOPIC_MUSIC,
    TOPIC_STATUS,
    TOPIC_WALLPAPER,
    TOPICS,
    InvalidMessageError,
    IpcProtocolError,
    MalformedJsonError,
    decode,
    decode_command,
    decode_full,
    encode,
    encode_command,
)

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
    # server
    "LocalServer",
    "NullServer",
    "IpcServerError",
    "UNIX_SOCKET_SUPPORTED",
    "DEFAULT_BACKLOG",
    "DEFAULT_MODE",
    "DEFAULT_QUEUE_SIZE",
    "PROBE_TIMEOUT",
    "mode_to_wire",
    "mode_from_wire",
    # client (只用于测试/联调; 生产 GUI 是 C++)
    "LocalClient",
    "IpcClientError",
    "CLIENT_SUPPORTED",
    # 接入点
    "build_ipc",
]

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  build_ipc: agent/main.py 的接入点
# ---------------------------------------------------------------------------
def build_ipc(bus: Any = None, config: Optional[Dict[str, Any]] = None) -> Any:
    """建好并接上 IPC server (main.py 约定: factory(bus, config))。

    @param bus    ChatInputBus; GUI 发来的 chat_input 会变成 bus 事件
                  (source="gui"), 和终端/主机键盘走完全相同的处理路径
    @param config 整个配置 dict; 读 ipc.socket_path
    @return LocalServer (不支持 AF_UNIX 的平台返回 NullServer)

    @note **出方向还没接线**: Agent -> GUI 的 status/llm/wallpaper/music 推送
          需要状态机与回复, 而这里只拿得到 bus。要做的话得让 main.py 把
          runtime 交出来 (或注册回调), 那是下一步的事 —— 这里不猜。
    """
    if not UNIX_SOCKET_SUPPORTED:
        return NullServer(
            reason="当前平台没有 AF_UNIX (Windows 的 CPython 不支持), "
                   "IPC 只能在板端 Linux 上运行"
        )

    section = config.get("ipc") if isinstance(config, dict) else None
    section = section if isinstance(section, dict) else {}

    path = section.get("socket_path") or SOCKET_PATH
    queue_size = _positive_int(section.get("queue_size"), DEFAULT_QUEUE_SIZE)

    server = LocalServer(path=path, queue_size=queue_size)
    server.on_command(_make_command_handler(bus))
    return server


def _make_command_handler(bus: Any):
    """把 GUI 命令翻译成 Agent 侧的动作。

    目前只有 chat_input 是通的 (它是唯一已经有完整下游的功能: bus -> LLM ->
    回复)。其余命令**明确记日志并忽略**, 而不是悄悄吞掉 —— 免得 GUI 那边
    以为生效了。
    """

    async def _on_command(action: str, payload: dict) -> None:
        if action == COMMAND_CHAT_INPUT:
            await _handle_chat_input(bus, payload)
            return

        if action == COMMAND_SWITCH_MODE:
            try:
                mode = mode_from_wire(payload.get("mode"))
            except IpcProtocolError as exc:
                _log.warning("ipc: switch_mode 的 mode 不合法, 已忽略: %s", exc)
                return
            _log.warning(
                "ipc: 收到 switch_mode(%s), 但状态机还没接进来 (下一步), 已忽略", mode
            )
            return

        if action in (COMMAND_NEXT_WALLPAPER, COMMAND_NEXT_BILIBILI):
            _log.warning("ipc: 命令 %s 的下游还没实现, 已忽略", action)
            return

        _log.warning("ipc: 命令 %s 没有处理分支, 已忽略", action)

    return _on_command


async def _handle_chat_input(bus: Any, payload: dict) -> None:
    """GUI 发来的聊天输入 -> bus (source="gui")。"""
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        _log.warning("ipc: chat_input 缺少非空 text 字段, 已忽略: %r", payload)
        return
    if bus is None:
        _log.warning("ipc: 收到 chat_input 但 bus 未就绪, 已忽略")
        return

    # 只取文本, 不透传整个 payload: bus 事件是给 LLM 看的, 多塞字段只会让它分心
    event = await bus.push("gui", text)
    _log.debug("ipc: chat_input 已入队: %r", event.get("text"))


def _positive_int(value: Any, default: int) -> int:
    """配置里的正整数; 不合法就退回默认值 (配置错了不该拦住启动)。"""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        if value is not None:
            _log.warning("ipc: queue_size=%r 不是正整数, 用默认值 %d", value, default)
        return default
    return value
