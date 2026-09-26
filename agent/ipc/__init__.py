# ============================================================================
#  agent/ipc/__init__.py — IPC 层 (Agent ⇄ GUI)
#
#      协议:   protocol.py      编解码 + 常量     (线格式见 docs/ipc-protocol.md)
#      Server: local_server.py   Unix socket server (Agent 是 server, GUI 是 client)
#      Client: local_client.py   Python 测试客户端 (**不是**生产 GUI, 那个是 C++)
#      本文件: build_ipc()       main.py 的接入点 —— 把 server 和 bus 接起来
#
#  ⚠ 出方向 (Agent -> GUI 的 status/llm/wallpaper/music/schedule) 需要知道状态机、
#    回复与调度器, 所以 build_ipc() 收一个可选的 runtime (Phase 6 D4):
#        build_ipc(bus, config)              只接入方向 (命令) —— 兼容旧签名
#        build_ipc(bus, config, runtime=rt)  再推 status (状态变化)、llm (回复)
#                                            与 schedule (日程真的触发, P 系列)
#    每样都是"有就接": runtime 少了 state / on_reply / scheduler 里任何一个,
#    只少推对应的那一类, 其余照常 —— 老 Runtime 与测试替身不用跟着改。
# ============================================================================

import asyncio
import logging
from datetime import datetime
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
    COMMAND_BILIBILI_PICK,
    COMMAND_BILIBILI_VIEWPORT,
    COMMAND_CHAT_INPUT,
    COMMAND_MUSIC_NEXT,
    COMMAND_MUSIC_PLAY_PAUSE,
    COMMAND_MUSIC_PREV,
    COMMAND_MUSIC_STOP,
    COMMAND_NEXT_BILIBILI,
    COMMAND_PREV_BILIBILI,
    COMMAND_QUERY_SCHEDULE,
    COMMAND_SWITCH_MODE,
    COMMAND_VIDEO_CONTROL,
    COMMAND_VIDEO_STATE,
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
    TOPIC_BILIBILI,
    TOPIC_LLM,
    TOPIC_MUSIC,
    TOPIC_SCHEDULE,
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
    "TOPIC_SCHEDULE",
    "TOPIC_BILIBILI",
    "TOPICS",
    # command (GUI -> Agent)
    "COMMAND_SWITCH_MODE",
    "COMMAND_CHAT_INPUT",
    "COMMAND_NEXT_BILIBILI",
    "COMMAND_PREV_BILIBILI",
    "COMMAND_BILIBILI_PICK",
    "COMMAND_BILIBILI_VIEWPORT",
    "COMMAND_VIDEO_STATE",
    "COMMAND_VIDEO_CONTROL",
    "COMMAND_QUERY_SCHEDULE",
    "COMMANDS",
    "UNWIRED_COMMAND_NOTES",
    "NO_SCHEDULER_NOTE",
    "NO_MUSIC_NOTE",
    # schedule.data.kind 的取值
    "SCHEDULE_KIND_STATE",
    "SCHEDULE_KIND_FIRED",
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


def _scheduler_of(runtime: Any) -> Any:
    """从 runtime 上取调度器 (取不到就 None)。

    @note 用 `recent_fired` 当能力探测: 只要它能报"触发过什么"就够本层用了 ——
          不去 isinstance(Scheduler), 那样测试替身与将来的实现都得跟着改。
    """
    scheduler = getattr(runtime, "scheduler", None) if runtime is not None else None
    return scheduler if hasattr(scheduler, "recent_fired") else None


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
    · 日程真的触发 -> ``schedule{kind:"fired", event}`` (P 系列; 没有调度器就跳过)
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

    # · 换壁纸 -> wallpaper{path, index}（T3）
    # 与 on_reply / scheduler.on_fire 同款"有就接": topic 名只在本层出现,
    # Runtime 不认识任何线格式字段（它只调 on_wallpaper(path, index)）。
    if hasattr(runtime, "on_wallpaper"):
        def _on_wallpaper(path: str, index: int) -> None:
            dispatch(TOPIC_WALLPAPER, {"path": path, "index": index})

        runtime.on_wallpaper = _on_wallpaper
        _log.debug("ipc: 已接上壁纸推送 (runtime.on_wallpaper -> wallpaper)")

    # · 新 GUI 连上 -> 补推当前壁纸（T6）
    # `wallpaper` 与 `status` 一样是"变化时才推"：客户端连上时不会自动收到一条,
    # 于是刚打开的界面主区是兜底底色。这里在**读到第一条命令之前**补一张当前壁纸。
    if hasattr(runtime, "push_current_wallpaper"):
        def _on_client_connect() -> None:
            pushed = runtime.push_current_wallpaper()
            _log.debug("ipc: 新客户端连上 -> 补推当前壁纸: %s", pushed)
            # T8-4: 音乐条同理 —— 连上就该看到"现在在放什么", 而不是空条
            current = getattr(runtime, "push_current_music", None)
            if callable(current):
                _log.debug("ipc: 新客户端连上 -> 补推当前音乐状态: %s", current())
            # T11-6: 预览图栏同理 —— 连上就该看到队列里有什么（空的话就是空的）
            bilibili = getattr(runtime, "push_current_bilibili", None)
            if callable(bilibili):
                _log.debug("ipc: 新客户端连上 -> 补推 B 站队列: %s", bilibili())

        server.on_client_connect = _on_client_connect
        _log.debug("ipc: 已接上'连上补推壁纸/音乐' (server.on_client_connect)")

    # · 换歌/进度 -> music{title, artist, album, position_s, duration_s, playing}
    # 与 on_wallpaper 同款"有就接": MusicPlayer 只交出快照 dict, 线格式字段由本层决定。
    if hasattr(runtime, "on_music"):
        def _on_music(snapshot: Dict[str, Any]) -> None:
            dispatch(TOPIC_MUSIC, dict(snapshot))

        runtime.on_music = _on_music
        _log.debug("ipc: 已接上音乐推送 (runtime.on_music -> music)")

    # · B 站队列/当前那条/要播的本地流 -> bilibili{queue, index, current, stream, …}（T11-6）
    # 与 on_music 同款"有就接"。⚠ 那个 `stream` 是**板端本地 FIFO 路径**（不是 URL）——
    # GUI 只是 `setSource(它)`; "怎么把流喂出来"是缓冲代理的事。
    if hasattr(runtime, "on_bilibili"):
        def _on_bilibili(snapshot: Dict[str, Any]) -> None:
            dispatch(TOPIC_BILIBILI, dict(snapshot))

        runtime.on_bilibili = _on_bilibili
        _log.debug("ipc: 已接上 B 站推送 (runtime.on_bilibili -> bilibili)")

    # · 日程触发 -> schedule{kind:"fired", event}
    # 与上面 on_reply 同款"有就接": runtime 没带调度器 (或它还没起来) 就跳过 ——
    # 老 Runtime 与测试替身不该因为少这一样而接不上其余推送。
    scheduler = _scheduler_of(runtime)
    if scheduler is not None:
        def _on_fire(fact: Dict[str, Any]) -> None:
            dispatch(TOPIC_SCHEDULE, {"kind": SCHEDULE_KIND_FIRED, "event": fact})

        scheduler.on_fire = _on_fire
        _log.debug("ipc: 已接上日程触发推送 (scheduler.on_fire -> schedule)")


#: 还没接下游的命令 -> 回给用户的那句话 (Phase 6 D7 / 决策 7)。
#: 文案是给**用户**看的 (GUI 会把 llm 的 text 显示成助手气泡), 所以不要写成日志腔。
#: ⚠ **T11-6 起这张表空了**: 最后一条 `next_bilibili` 也接上了（Runtime.bilibili_next）。
#:   表留着是因为"以后再有没接下游的命令"时还得有个地方写那句话, 而且
#:   `tests/test_ipc_local_server.py` 盯着"表空了就没有命令会落到那句说明"。
#:   老客户端发来的**未知**命令仍走下面"没有处理分支"那条: 只记 warning, 不会崩。
UNWIRED_COMMAND_NOTES: Dict[str, str] = {}

#: 收到音乐按钮但 Agent 侧没有音乐入口（`music.enabled=false`）时回的那句话 (给用户看)。
NO_MUSIC_NOTE = "音乐还没开：在 config.yaml 的 music 段里把 enabled 打开、填上 PC 地址。"

#: 收到 B 站按钮但 Agent 侧没有 B 站入口（`bilibili.enabled=false` / 没配好）时回的话。
NO_BILIBILI_NOTE = ("B 站视频还没开：在 config.yaml 的 bilibili 段里把 enabled 打开"
                    "（队列是空的，先在 GAME 模式里用对话点一个视频）。")


#: schedule.data.kind 的两个取值 (线格式见 docs/ipc-protocol.md §3)。
#: 放在这里而不是 protocol.py: 它们只在"构造 schedule 载荷"时用到, 没有第二处
#: 实现要对字面值 —— C++ 侧按"未知 topic 忽略"处理, 根本不认这个 topic。
SCHEDULE_KIND_STATE = "state"    #: 应答 query_schedule 的快照
SCHEDULE_KIND_FIRED = "fired"    #: 刚刚真的触发了一条

#: 收到 query_schedule 但 Agent 侧没有调度器时回的那句话。
#: 与 UNWIRED_COMMAND_NOTES 同款: 给**用户**看的 (会显示成助手气泡), 不能静默。
NO_SCHEDULER_NOTE = "现在还没有可查的日程触发记录：Agent 的调度器没起来（或没接进来）。"


def _schedule_state_data(scheduler: Any) -> Dict[str, Any]:
    """query_schedule 的应答负载 (kind="state", 线格式见 docs §3)。

    @note 与 _status_data 同样的用意: **只有这一处**构造 kind="state" 载荷; 实时那条
          kind="fired" 也只有 _wire_outbound 里那一处 —— 两处不会各长各的。
    @note limit 报的是**真实上限** (scheduler.history_limit), 不是这次带了几条 ——
          客户端拿它判断"是不是被截断了"。
    """
    return {
        "kind": SCHEDULE_KIND_STATE,
        "now": datetime.now().isoformat(timespec="seconds"),
        "limit": scheduler.history_limit,
        "fired": list(scheduler.recent_fired()),
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

        if action == COMMAND_QUERY_SCHEDULE:
            _handle_query_schedule(runtime, push)
            return

        if action in _MUSIC_ACTIONS:
            _handle_music(runtime, push, _MUSIC_ACTIONS[action])
            return

        if action in _BILIBILI_ACTIONS:
            _handle_bilibili(runtime, push, _BILIBILI_ACTIONS[action], payload)
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


#: 音乐命令 -> `Runtime.music_control()` 的动作名（T8-4）。
#: ⚠ 这里**只映射动作, 不决定放什么**: `next`/`prev` 走的是"chat 上次挑出来的候选顺序"
#:   （`MusicPlayer.step()`）; 语义说明见 protocol.py 里那三个常量的注释。
_MUSIC_ACTIONS: Dict[str, str] = {
    COMMAND_MUSIC_PLAY_PAUSE: "play_pause",
    COMMAND_MUSIC_NEXT: "next",
    COMMAND_MUSIC_PREV: "prev",
    COMMAND_MUSIC_STOP: "stop",
}


def _handle_music(runtime: Any, push: Any, action: str) -> None:
    """音乐按钮（播放/暂停、上一首、下一首、停止）—— **按钮不决定放什么, 对话才决定**。

    · 成功: 状态由 `Runtime` 推 `music{...}`（按钮的反馈就是音乐条自己变了）——
      **不往对话区写话**（与旧壁纸按钮同一条口径: 点一下多一条气泡太吵）。
    · 失败: 推一条 `llm` 说明（音乐没开 / PC 上没在放 / 还没有候选队列）——
      点了完全没反应最难查。
    """
    control = getattr(runtime, "music_control", None) if runtime is not None else None
    if not callable(control):
        _log.warning("ipc: 收到 %s 但 runtime 没有音乐入口, 回一句说明", action)
        if push is not None:
            push(TOPIC_LLM, {"text": NO_MUSIC_NOTE})
        return
    result = control(action) or {}
    if result.get("ok"):
        _log.debug("ipc: music %s -> ok", action)
        return
    reason = result.get("tell_user") or result.get("error") or "原因不明"
    _log.warning("ipc: music %s 失败: %s", action, reason)
    if push is not None:
        push(TOPIC_LLM, {"text": str(reason)})


#: B 站命令 -> `Runtime` 上的入口名（T11-6）。
#: ⚠ 与音乐同一条口径: 按钮**不决定放什么** —— 队列内容由**对话**（或画面认出的游戏）决定,
#:   按钮只在队列里走位/挑一条/回报进度。`video_state` 是**回报**, 不是请求。
_BILIBILI_ACTIONS: Dict[str, str] = {
    COMMAND_NEXT_BILIBILI: "next",
    COMMAND_PREV_BILIBILI: "prev",
    COMMAND_BILIBILI_PICK: "pick",
    COMMAND_BILIBILI_VIEWPORT: "viewport",
    COMMAND_VIDEO_STATE: "video_state",
    #: T11-10f: 让**播放器**播放/暂停 —— 载荷里的 `action`（play/pause/toggle）由
    #: `Runtime.bilibili_control` 自己解析（`toggle` 按缓冲的真值解析成 play/pause）。
    COMMAND_VIDEO_CONTROL: "control",
}


def _handle_bilibili(runtime: Any, push: Any, action: str, payload: dict) -> None:
    """B 站那几个 GUI 动作 —— **只走 Agent, 不经过 LLM**（你定的）。

    · `next` / `prev`: 在队列里走一格, 然后**开始放那一集**（用户按了就是"要看");
    · `pick`: 放队列里的第 index 条（用户点了预览图）—— **只有用户点才播**;
    · `viewport`: GUI 上报预览栏格数 -> 队列目标 = 3×它（**不播**）;
    · `video_state`: GUI 回报真实进度（**不播**）; `eof=true` -> 自动下一集;
    · `control`: 让**播放器**播放/暂停（T11-10f; 载荷 `{"action": play|pause|toggle}`）
      —— Agent 推一条 `bilibili{control{action,seq}}` 下去, **GUI 才是真按播放器的人**。

    失败一律推一条 `llm` 说明（点了没反应最难查）; 成功不写气泡（界面上已经变了）。
    """
    entry = getattr(runtime, "bilibili_control", None) if runtime is not None else None
    if not callable(entry):
        _log.warning("ipc: 收到 B 站命令 %s 但 runtime 没有入口, 回一句说明", action)
        if push is not None:
            push(TOPIC_LLM, {"text": NO_BILIBILI_NOTE})
        return
    try:
        result = entry(action, dict(payload or {})) or {}
    except Exception as exc:                              # noqa: BLE001 - 如实回一句
        _log.warning("ipc: B 站命令 %s 抛异常: %r", action, exc)
        if push is not None:
            push(TOPIC_LLM, {"text": "B 站那边出错了：%s" % exc})
        return
    if result.get("ok"):
        _log.debug("ipc: bilibili %s -> ok", action)
        return
    reason = result.get("tell_user") or result.get("error") or "原因不明"
    _log.warning("ipc: bilibili %s 失败: %s", action, reason)
    if push is not None:
        push(TOPIC_LLM, {"text": str(reason)})


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


def _handle_query_schedule(runtime: Any, push: Any) -> None:
    """query_schedule: 把"最近触发过哪些日程"回给客户端 (docs §4)。

    应答**就是**随后那条 ``schedule{kind:"state"}`` 推送 —— 与 switch_mode 的应答是
    随后那条 status 一样, **没有请求 id** (协议里根本没有关联字段)。
    没有调度器时回一条 llm 说明, 而不是静默: 点了没反应最难查。

    @note 这条查询**不改任何东西** (只读): 日程的增删改不在这条协议里 ——
          配置永远是唯一真源, 写配置是人在 PC 上做的事。
    """
    scheduler = _scheduler_of(runtime)
    if scheduler is None:
        _log.warning("ipc: 收到 query_schedule 但没有调度器, 回一句说明")
        if push is not None:
            push(TOPIC_LLM, {"text": NO_SCHEDULER_NOTE})
        return

    if push is None:
        _log.warning("ipc: 收到 query_schedule 但没有推送入口, 已忽略")
        return

    data = _schedule_state_data(scheduler)
    _log.debug("ipc: query_schedule -> schedule{kind=%s, %d 条}",
               data["kind"], len(data["fired"]))
    push(TOPIC_SCHEDULE, data)


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
