# ============================================================================
#  agent/ipc/__init__.py — IPC 层 (Agent ⇄ GUI)
#
#      协议:   protocol.py      编解码 + 常量     (线格式见 docs/ipc-protocol.md)
#      Server: local_server.py   Unix socket server (Agent 是 server, GUI 是 client)
#      Client: local_client.py   Python 测试客户端 (**不是**生产 GUI, 那个是 C++)
#      本文件: build_ipc()       main.py 的接入点 —— 把 server 和 bus 接起来
#
#  ⚠ 出方向 (Agent -> GUI 的 status/llm/wallpaper/music) 需要知道状态机与回复,
#    所以 build_ipc() 收一个可选的 runtime (Phase 6 D4):
#        build_ipc(bus, config)              只接入方向 (命令) —— 兼容旧签名
#        build_ipc(bus, config, runtime=rt)  再推 status (状态变化) 与 llm (回复)
# ============================================================================

import asyncio
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
def build_ipc(bus: Any = None,
              config: Optional[Dict[str, Any]] = None,
              runtime: Any = None) -> Any:
    """建好并接上 IPC server (main.py 约定: factory(bus, config[, runtime]))。

    @param bus     ChatInputBus; GUI 发来的 chat_input 会变成 bus 事件
                   (source="gui"), 和终端/主机键盘走完全相同的处理路径
    @param config  整个配置 dict; 读 ipc.socket_path
    @param runtime Runtime (可选)。给了就接上**出方向**推送 (Phase 6 D4):
                     · runtime.state.on_change  -> push status{mode, connected}
                     · runtime.on_reply        -> push llm{text}
                   不给就只有入方向 (命令), 与 D4 之前完全一样。
    @return LocalServer (不支持 AF_UNIX 的平台返回 NullServer)
    """
    if not UNIX_SOCKET_SUPPORTED:
        server = NullServer(
            reason="当前平台没有 AF_UNIX (Windows 的 CPython 不支持), "
                   "IPC 只能在板端 Linux 上运行"
        )
        if runtime is not None:
            _wire_outbound(server, runtime)
        return server

    section = config.get("ipc") if isinstance(config, dict) else None
    section = section if isinstance(section, dict) else {}

    path = section.get("socket_path") or SOCKET_PATH
    queue_size = _positive_int(section.get("queue_size"), DEFAULT_QUEUE_SIZE)

    server = LocalServer(path=path, queue_size=queue_size)
    server.on_command(_make_command_handler(bus))
    if runtime is not None:
        _wire_outbound(server, runtime)
    return server


def _wire_outbound(server: Any, runtime: Any) -> None:
    """把 Agent 的状态与回复推给 GUI (Phase 6 D4)。

    这是 D4 之前缺的那一半: 只有入方向 (命令), 出方向一个字都推不出去 ——
    `handle_event()` 算出来的回复被主循环直接丢掉, 状态变化也没人告诉 GUI。

    · mode 变化   -> ``status{mode, connected}``
    · 每条 LLM 回复 -> ``llm{text}``

    @note 状态变化回调是**同步**的, 而且可能从非事件循环线程进来 (调度器/原生回调),
          而 `push()` 是协程 —— 所以建好时抓住 loop, 用
          ``run_coroutine_threadsafe`` 投回去。
    @note 回调在 server 没跑或没有 GUI 连着时静默跳过: `push()` 返回 0 本来就不是
          错误 ("当前没 GUI 连着")。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    def _dispatch(topic: str, data: Dict[str, Any]) -> None:
        if not getattr(server, "is_running", False):
            return
        coro = server.push(topic, data)
        if loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, loop)
            return
        try:
            asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            # 连事件循环都没有 (同步脚本里直接调 build_ipc) —— 关掉协程免得
            # 报 "coroutine was never awaited"
            coro.close()
            _log.debug("ipc: 没有事件循环, 推送 %s 被丢弃", topic)

    state = getattr(runtime, "state", None)
    on_change = getattr(state, "on_change", None)
    if callable(on_change):
        def _status_on_change(old: Any, new: Any) -> None:
            mode = getattr(new, "value", new)
            try:
                wire_mode = mode_to_wire(mode)
            except IpcProtocolError:
                # 状态机的取值域变了而这里没跟上时, 别把推送整个搞崩
                wire_mode = str(mode)
                _log.warning("ipc: 状态 %r 无法转成线上取值, 原样推送", mode)
            connected = False
            is_connected = getattr(state, "is_connected", None)
            if callable(is_connected):
                try:
                    connected = bool(is_connected())
                except Exception as exc:        # noqa: BLE001
                    _log.debug("ipc: is_connected() 失败 (%r), 按未连接推送", exc)
            _dispatch(TOPIC_STATUS, {"mode": wire_mode, "connected": connected})

        on_change(_status_on_change)
        _log.debug("ipc: 已接上状态推送 (state.on_change -> status)")

    if hasattr(runtime, "on_reply"):
        def _on_reply(text: str) -> None:
            _dispatch(TOPIC_LLM, {"text": text})

        runtime.on_reply = _on_reply
        _log.debug("ipc: 已接上回复推送 (runtime.on_reply -> llm)")


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
            # ⚠ 键是 **value** 不是 mode (Phase 6 D2): GUI 与 docs/ipc-protocol.md §4
            #   都是 {"value": "STUDY"}; 而 status 推送里那个 "mode" 是**另一个方向**
            #   的字段, 别把两者混起来。
            if "value" not in payload:
                _log.warning(
                    "ipc: switch_mode 的 payload 缺少 value 字段 (得到 %r), 已忽略",
                    payload,
                )
                return
            try:
                mode = mode_from_wire(payload.get("value"))
            except IpcProtocolError as exc:
                _log.warning("ipc: switch_mode 的 value 不合法, 已忽略: %s", exc)
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
