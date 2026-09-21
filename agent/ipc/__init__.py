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
    "UNWIRED_COMMAND_NOTES",
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
    # switch_mode 被状态机拒绝时要**立刻**把真实状态推回去 (docs §4), 所以命令
    # 处理函数也需要一个"现在就推"的入口 —— 与出方向用同一个 dispatcher。
    dispatch = _make_dispatcher(server, _current_loop())
    server.on_command(_make_command_handler(bus, runtime=runtime, push=dispatch))
    if runtime is not None:
        _wire_outbound(server, runtime, dispatch=dispatch)
    return server


def _current_loop() -> Any:
    """当前事件循环; 没有就返回 None (同步脚本里直接调 build_ipc 的情况)。"""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _make_dispatcher(server: Any, loop: Any):
    """造一个"把一条推送投到事件循环上"的同步入口。

    状态变化回调是同步的、还可能从非事件循环线程进来 (调度器/原生回调), 而
    `push()` 是协程 —— 所以这里用 ``run_coroutine_threadsafe`` 投回去。
    server 没跑就静默跳过: `push()` 返回 0 ("当前没 GUI 连着") 本来就不是错误。
    """

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
            # 连事件循环都没有 —— 关掉协程免得报 "coroutine was never awaited"
            coro.close()
            _log.debug("ipc: 没有事件循环, 推送 %s 被丢弃", topic)

    return _dispatch


def _status_data(state: Any) -> Dict[str, Any]:
    """status 推送的负载。

    @note 只有这一处构造 status 载荷 —— 状态变化推送与"非法转换后把真实状态推回去"
          用的是同一份, 免得两处慢慢漂移。
    @note 线上取值是**大写** (SLEEP/IDLE/STUDY/GAME), 状态机内部是小写。
    """
    current = state.current()
    raw = getattr(current, "value", current)
    try:
        mode = mode_to_wire(raw)
    except IpcProtocolError:
        mode = str(raw)
        _log.warning("ipc: 状态 %r 无法转成线上取值, 原样推送", raw)

    connected = False
    is_connected = getattr(state, "is_connected", None)
    if callable(is_connected):
        try:
            connected = bool(is_connected())
        except Exception as exc:            # noqa: BLE001
            _log.debug("ipc: is_connected() 失败 (%r), 按未连接推送", exc)

    return {"mode": mode, "connected": connected}


def _wire_outbound(server: Any, runtime: Any, dispatch: Any = None) -> None:
    """把 Agent 的状态与回复推给 GUI (Phase 6 D4)。

    这是 D4 之前缺的那一半: 只有入方向 (命令), 出方向一个字都推不出去 ——
    `handle_event()` 算出来的回复被主循环直接丢掉, 状态变化也没人告诉 GUI。

    · mode 变化   -> ``status{mode, connected}``
    · 每条 LLM 回复 -> ``llm{text}``
    """
    if dispatch is None:
        dispatch = _make_dispatcher(server, _current_loop())

    state = getattr(runtime, "state", None)
    on_change = getattr(state, "on_change", None)
    if callable(on_change):
        def _status_on_change(old: Any, new: Any) -> None:
            # 回调触发时状态**已经**改好了 (state_machine 的约定), 所以直接读 current()
            dispatch(TOPIC_STATUS, _status_data(state))

        on_change(_status_on_change)
        _log.debug("ipc: 已接上状态推送 (state.on_change -> status)")

    if hasattr(runtime, "on_reply"):
        def _on_reply(text: str) -> None:
            dispatch(TOPIC_LLM, {"text": text})

        runtime.on_reply = _on_reply
        _log.debug("ipc: 已接上回复推送 (runtime.on_reply -> llm)")


#: 还没接下游的命令 -> 回给用户的那句话 (Phase 6 D7 / 决策 7)。
#: 下游属 Phase 7; 现在必须**回一句说明**, 否则 GUI 上点了完全没反应, 用起来像坏了。
#: 文案是给**用户**看的 (GUI 会把 llm 的 text 显示成助手气泡), 所以不要写成日志腔。
UNWIRED_COMMAND_NOTES: Dict[str, str] = {
    COMMAND_NEXT_WALLPAPER: "换壁纸的功能还没接入（Phase 7），这次点击先没有生效。",
    COMMAND_NEXT_BILIBILI: "B 站「下一集」还没接入（Phase 7），这次点击先没有生效。",
}


def _make_command_handler(bus: Any, runtime: Any = None, push: Any = None):
    """把 GUI 命令翻译成 Agent 侧的动作。

    @param runtime 给了就说明 switch_mode 能真的切状态 (Phase 6 D5: 之前只记一条
                   "状态机还没接进来")
    @param push    可选: "现在就推一条"的入口 —— 非法模式转换要把**真实**状态回给 GUI
                   (D5), 未接线的命令要回一句说明 (D7)
    """

    async def _on_command(action: str, payload: dict) -> None:
        if action == COMMAND_CHAT_INPUT:
            await _handle_chat_input(bus, payload)
            return

        if action == COMMAND_SWITCH_MODE:
            _handle_switch_mode(runtime, push, payload)
            return

        if action in UNWIRED_COMMAND_NOTES:
            # Phase 6 D7 (决策 7): 下游在 Phase 7, 但**必须回一句** —— 否则 GUI 上
            # 点了完全没反应, 用起来像坏了。回的是 llm (用户看得见的那条通道)。
            _log.warning("ipc: 命令 %s 的下游还没实现 (Phase 7), 已回推说明", action)
            if push is not None:
                push(TOPIC_LLM, {"text": UNWIRED_COMMAND_NOTES[action]})
            return

        _log.warning("ipc: 命令 %s 没有处理分支, 已忽略", action)

    return _on_command


def _handle_switch_mode(runtime: Any, push: Any, payload: dict) -> None:
    """switch_mode: ``{"value": "STUDY"}`` -> 状态机转换 (Phase 6 D5)。

    ⚠ 键是 **value** 不是 mode (D2): GUI 与 docs/ipc-protocol.md §4 都是
      ``{"value": "STUDY"}``; status **推送**里那个 "mode" 是另一个方向的字段。

    非法转换**不报协议错**: 状态机返回 False, 这里拒绝 + 记日志, 并把**当前真实
    状态**推回给 GUI —— 否则界面会停在它自己乐观切过去的那个状态上 (docs §4)。
    成功时不用手动推: 状态机的 on_change 回调已经负责推送 (D4)。
    """
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

    state = getattr(runtime, "state", None)
    if state is None:
        _log.warning(
            "ipc: 收到 switch_mode(%s) 但没有状态机 (build_ipc 没拿到 runtime), 已忽略",
            mode,
        )
        return

    # transition() 接受 "study" 这样的小写字符串, 并且非法/原地转换都只返回 False
    if state.transition(mode, "ipc: switch_mode"):
        _log.info("ipc: switch_mode -> %s", mode)
        return

    current = getattr(state.current(), "value", "?")
    _log.warning(
        "ipc: switch_mode(%s) 被状态机拒绝 (当前 %s), 把真实状态推回给 GUI",
        mode, current,
    )
    if push is not None:
        push(TOPIC_STATUS, _status_data(state))


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
