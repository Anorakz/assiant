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
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from agent.config import read_config_file
from agent.core import config_tiers, llm_env, settings_config, settings_credentials

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
    COMMAND_LLM_SERVICE,
    COMMAND_WIFI,
    COMMAND_MUSIC_NEXT,
    COMMAND_MUSIC_PLAY_PAUSE,
    COMMAND_MUSIC_PREV,
    COMMAND_MUSIC_STOP,
    COMMAND_NEXT_BILIBILI,
    COMMAND_PREV_BILIBILI,
    COMMAND_QUERY_SCHEDULE,
    COMMAND_SET_CONFIG,
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
    TOPIC_CONFIG_RESULT,
    TOPIC_LLM,
    TOPIC_MUSIC,
    TOPIC_SCHEDULE,
    TOPIC_SERVICE_RESULT,
    TOPIC_WIFI,
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
    "TOPIC_CONFIG_RESULT",
    "TOPIC_SERVICE_RESULT",
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
    "COMMAND_SET_CONFIG",
    "COMMAND_LLM_SERVICE",
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

    # ★ T15-2-10e（2026-10-07 板端实测）：给 Runtime 一个"**主动推一次 status**"的钩子。
    #   为什么需要：`status.connected`（= moonlight 串流主机是否连上）在 Runtime 那边是
    #   **懒查询**（`state_machine.is_connected()` 去问 native），连接/断开**没有事件** ✗ ⇒
    #   只靠上面那条 `state.on_change` 会漏掉它：实测 moonlight 21:52 连上了，GUI 一直显示
    #   "重连中（主机未就绪）"，直到用户切一次模式才补上 ✗。
    if state is not None:
        def _on_status_push(_snapshot: Any = None) -> None:
            """Runtime 调它 = 把**当前**状态推一条给 GUI（线格式仍由本层决定 ✓）。"""
            dispatch(TOPIC_STATUS, _status_data(state))

        runtime.on_status = _on_status_push
        _log.debug("ipc: 已接上 status 主动补推钩子 (runtime.on_status -> status)")

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
            # T15-14-a: OTA/槽状态同理 —— 而且**必须走 force 补推**：
            #   否则"状态没变"会被变化去重吞掉（T15-16 在音乐条上踩过同一个 bug ✗）。
            ota_push = getattr(runtime, "push_current_ota", None)
            if callable(ota_push):
                _log.debug("ipc: 新客户端连上 -> 补推 OTA 状态: %s", ota_push())
            # ★ T15-2-10e: status 也必须补推 ✗ —— 否则刚连上的界面显示的是**旧**状态
            #   （板端实测：GUI 一直"重连中（主机未就绪）"，而 moonlight 其实早连上了 ✓）。
            #   注意 status 是"变化才推"、且 connected 是懒查询 ⇒ 这里必须无条件推一条 ✓。
            status_push = getattr(runtime, "on_status", None)
            if callable(status_push):
                status_push()
                _log.debug("ipc: 新客户端连上 -> 补推 status ✓")

        server.on_client_connect = _on_client_connect
        _log.debug("ipc: 已接上'连上补推壁纸/音乐' (server.on_client_connect)")

    # · 换歌/进度 -> music{title, artist, album, position_s, duration_s, playing}
    # 与 on_wallpaper 同款"有就接": MusicPlayer 只交出快照 dict, 线格式字段由本层决定。
    if hasattr(runtime, "on_music"):
        def _on_music(snapshot: Dict[str, Any]) -> None:
            dispatch(TOPIC_MUSIC, dict(snapshot))

        runtime.on_music = _on_music
        _log.debug("ipc: 已接上音乐推送 (runtime.on_music -> music)")

    # · OTA/槽状态 -> ota_state{ok, current_slot, slots, last_boot, last_ota, confirm}（T15-14-a）
    # 与 on_music 同款"有就接"。GUI 只**展示**它（升级本身是 root 级命令行动作，
    # 界面上不提供"开始升级"按钮 ✓ —— 危险动作不该藏在设置页里）。
    if hasattr(runtime, "on_ota"):
        def _on_ota(state: Dict[str, Any]) -> None:
            dispatch("ota_state", dict(state))

        runtime.on_ota = _on_ota
        _log.debug("ipc: 已接上 OTA 推送 (runtime.on_ota -> ota_state)")

    # · B 站队列/当前那条/要播的本地流 -> bilibili{queue, index, current, stream, …}（T11-6）
    # 与 on_music 同款"有就接"。⚠ 那个 `stream` 是**板端本地 FIFO 路径**（不是 URL）——
    # GUI 只是 `setSource(它)`; "怎么把流喂出来"是缓冲代理的事。
    if hasattr(runtime, "on_bilibili"):
        def _on_bilibili(snapshot: Dict[str, Any]) -> None:
            dispatch(TOPIC_BILIBILI, dict(snapshot))

        runtime.on_bilibili = _on_bilibili
        _log.debug("ipc: 已接上 B 站推送 (runtime.on_bilibili -> bilibili)")

    # · 日程触发 -> schedule{kind:"fired", event} + **一句展示**
    # 与上面 on_reply 同款"有就接": runtime 没带调度器 (或它还没起来) 就跳过 ——
    # 老 Runtime 与测试替身不该因为少这一样而接不上其余推送。
    scheduler = _scheduler_of(runtime)
    if scheduler is not None:
        def _on_fire(fact: Dict[str, Any]) -> None:
            dispatch(TOPIC_SCHEDULE, {"kind": SCHEDULE_KIND_FIRED, "event": fact})
            # T12-4: 日程到点**只切状态**, 不再往 bus 里推文本（不叫 LLM）。给用户看的话
            # 在这里推成 `llm` **展示** —— 与"助手气泡"同一个通道, 但它不进模型输入。
            text = _schedule_fired_text(fact)
            if text:
                dispatch(TOPIC_LLM, {"text": text})

        scheduler.on_fire = _on_fire
        _log.debug("ipc: 已接上日程触发推送 (scheduler.on_fire -> schedule + llm)")


def _schedule_fired_text(fact: Dict[str, Any]) -> str:
    """日程触发后给用户看的那一句（**纯格式化**, 单测钉它）。

    @return 形如 `日程到点：切到 STUDY`；切不过去时如实说清楚（`actions` 里有 ok/why）
    @note 时刻取 `scheduled_at`（"这条日程是几点"）；它可能比 `fired_at` 早几秒 ——
          报"日程的时刻"比报"我们什么时候才发现"更有用。
    """
    state = str(fact.get("state") or "").strip().upper()
    if not state:
        return ""
    clock = str(fact.get("scheduled_at") or "")
    _, sep, time_part = clock.partition("T")
    if sep and len(time_part) >= 5:
        clock = time_part[:5]
    else:
        clock = ""
    where = "（%s）" % clock if clock else ""

    failed = ""
    for action in fact.get("actions") or []:
        if isinstance(action, dict) and action.get("type") == "state" and not action.get("ok"):
            failed = str(action.get("why") or "状态机没让切")
            break
    if failed:
        return "日程到点：想切到 %s，但没切过去 —— %s" % (state, failed)
    return "日程到点：切到 %s%s" % (state, where)


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


def _handle_llm_service(runtime: Any, payload: dict, push: Any) -> None:
    """`llm_service`：让 **Agent** 跑 `llm/scripts/start.sh` / `stop.sh`（T14-3）。

    模型页那两颗按钮原来自己 `QProcess` 跑脚本；T14 起 GUI 不做系统动作
    （不跑脚本、不写文件），只发命令 —— Agent 是板端那个有权限的进程。

    ⚠ 与 `LlamaService.from_config()` 的取舍**不同**: 那个方法只在
      `mode=edge 且 manage_service=true` 时返回对象（"Agent 该不该自己管别人的进程"）。
      这里是**用户明确点了按钮**，所以照做；只在"没有 edge 那套脚本"时如实报失败。
    """
    from agent.llm.service import LlamaService       # 局部导入: 避免 ipc 层与 llm 层互相牵

    request_id = payload.get("id")
    if not isinstance(request_id, str):
        request_id = ""
    action = payload.get("action")

    reply: Dict[str, Any] = {"id": request_id, "ok": False, "action": action, "message": ""}

    def _finish() -> None:
        if push is not None:
            push(TOPIC_SERVICE_RESULT, reply)

    if action not in ("start", "stop", "status"):
        reply["message"] = "action 只能是 start / stop / status（收到 %r）" % (action,)
        _log.warning("ipc: llm_service 载荷不合法: %r", action)
        _finish()
        return

    service = getattr(runtime, "llm_service", None) if runtime is not None else None
    if service is None:
        # 没接服务对象（manage_service 没开 / 测试替身）就现建一个 —— 只是跑脚本，
        # 端口/密钥只影响探活，不影响启停。
        try:
            service = LlamaService(log=_log)
        except Exception as exc:                     # noqa: BLE001
            reply["message"] = "起不了 LlamaService: %s" % exc
            _log.warning("ipc: llm_service 建不出 LlamaService: %s", exc)
            _finish()
            return

    ok, message = service.start() if action == "start" else service.stop()
    reply["ok"] = bool(ok)
    reply["message"] = str(message)
    _log.info("ipc: llm_service %s -> %s (%s)", action, "ok" if ok else "失败", message)
    _finish()


def _wifi_work(runtime: Any, payload: dict) -> List[Tuple[str, Dict[str, Any]]]:
    """`wifi` 命令的**纯计算**部分：返回要推的 `[(kind, data)]`。

    ⚠ 刻意不碰 `push`：它会在线程池里跑（nmcli 扫描要几秒，不能卡住事件循环），
      而 IPC 的推送只能由事件循环线程做（见 `_handle_wifi`）。

    payload: `{action, id?, ssid?, password?, autoconnect?}`
    ⚠ 密码只在这里出现一次，**不写日志、不写 config.yaml**（`Wifi.connect` 负责
      "进 stdin / 进 0600 keyfile"）。日志里只记 `action` 与 SSID。
    """
    from agent.net.wifi import Wifi, WifiError      # 局部导入: ipc 层不往外扩依赖

    request_id = payload.get("id")
    if not isinstance(request_id, str):
        request_id = ""
    action = payload.get("action")
    ssid = payload.get("ssid")
    password = payload.get("password")
    if password is not None and not isinstance(password, str):
        password = None
    events: List[Tuple[str, Dict[str, Any]]] = []

    def _ack(ok: bool, message: str, **extra: Any) -> None:
        reply: Dict[str, Any] = {"id": request_id, "ok": ok, "action": action,
                                 "message": message}
        reply.update(extra)
        events.append(("ack", reply))

    if not isinstance(action, str) or action not in (
            "status", "scan", "connect", "forget", "autoconnect", "reconnect"):
        _ack(False, "action 只能是 status / scan / connect / forget / autoconnect / "
                    "reconnect（收到 %r）" % (action,))
        return events

    wifi = getattr(runtime, "wifi", None) if runtime is not None else None
    if wifi is None:
        try:
            wifi = Wifi(log=_log)
        except Exception as exc:                       # noqa: BLE001
            _ack(False, "起不了 Wifi 封装: %s" % exc)
            return events

    def _status_payload() -> Dict[str, Any]:
        payload_out = wifi.status().to_dict()
        guard = getattr(runtime, "link_guard", None) if runtime is not None else None
        if guard is not None and hasattr(guard, "status_dict"):
            payload_out["guard"] = guard.status_dict()
        return payload_out

    try:
        if action == "status":
            events.append(("status", _status_payload()))
        elif action == "scan":
            points = [point.to_dict() for point in wifi.scan()]
            events.append(("scan", {"points": points, "count": len(points)}))
        elif action == "connect":
            if not isinstance(ssid, str) or not ssid:
                _ack(False, "connect 需要 ssid")
                return events
            autoconnect = payload.get("autoconnect")
            autoconnect = True if autoconnect is None else bool(autoconnect)
            result = wifi.connect(ssid, password, autoconnect=autoconnect)
            _log.info("ipc: wifi connect %r -> %s", ssid, result.get("profile"))
            _ack(True, "已连接 %s（%s）" % (ssid, result.get("ip") or "无 IP"),
                 ssid=ssid, profile=result.get("profile"),
                 autoconnect=bool(result.get("autoconnect")))
        elif action == "forget":
            if not isinstance(ssid, str) or not ssid:
                _ack(False, "forget 需要 ssid")
                return events
            result = wifi.forget(ssid)
            _log.info("ipc: wifi forget %r（was_active=%s）", ssid, result.get("was_active"))
            message = "已忘记 %s" % ssid
            if result.get("was_active"):
                message += "（它正在用 —— 链路会断）"
            _ack(True, message, ssid=ssid, was_active=bool(result.get("was_active")))
        elif action == "autoconnect":
            if not isinstance(ssid, str) or not ssid:
                _ack(False, "autoconnect 需要 ssid")
                return events
            on = bool(payload.get("autoconnect", True))
            result = wifi.set_autoconnect(ssid, on)
            _ack(True, "%s 自动连接已%s" % (ssid, "打开" if on else "关闭"),
                 ssid=ssid, autoconnect=bool(result.get("autoconnect")))
        else:                                          # reconnect
            guard = getattr(runtime, "link_guard", None) if runtime is not None else None
            if guard is not None and hasattr(guard, "probe"):
                health = guard.probe()                 # 里面会按需重连（含日志）
                _ack(bool(health.ok), "链路体检：%s" % ("正常" if health.ok else "仍不通"),
                     health=health.to_dict())
            else:
                wifi.reconnect()
                health = wifi.health()
                _ack(bool(health.ok), "已尝试重连：%s" % ("通了" if health.ok else "仍不通"),
                     health=health.to_dict())
    except WifiError as exc:
        _log.warning("ipc: wifi %s 失败: %s", action, exc)
        _ack(False, str(exc))
    except Exception as exc:                           # noqa: BLE001
        _log.warning("ipc: wifi %s 意外失败: %r", action, exc)
        _ack(False, "WiFi 操作失败: %s" % exc)
    return events


async def _handle_wifi(runtime: Any, payload: dict, push: Any) -> None:
    """`wifi` 命令（T14-9）：**计算放线程池、推送回事件循环**。

    nmcli 的扫描要几秒（`--rescan yes`），直接同步跑会把推送循环卡住（扫描时 GUI
    还在等 status）。而 IPC 的 `push` 只能在事件循环线程调用 —— 所以这里分成两步。
    """
    import asyncio

    loop = asyncio.get_running_loop()
    events = await loop.run_in_executor(None, _wifi_work, runtime, payload)
    for kind, data in events:
        if push is not None:
            push(TOPIC_WIFI, dict(data, kind=kind))


def _handle_set_config(runtime: Any, payload: dict, push: Any) -> None:
    """`set_config`：**Agent 是配置真源唯一的写入者**（T14-2，见 docs/adr/0005）。

    顺序刻意是"先全部校验、再动第一份文件"：
      ① 载荷形状 -> ② 凭据纯校验（不写）-> ③ 配置**算计划**（不写；不认识的键 /
      类型错 / 结构级键 / **root 级键**都在这一步被拒）-> ④ 写配置
      （`settings_config.apply_changes`：只动目标行 + `.bak` + 原子写）->
      ⑤ 写凭据（另一个文件）-> ⑥ 派生 `llm.env`。
    ①②③ 任何一步失败都**一个字节都不写**，回执 ok=false + 原话。

    ⚠ **权限层（T15-4）**：GUI/IPC 这条路**永远**按 user 权限走
      （`config_tiers.plan_changes/apply_changes` 的 `allow_root` 默认 False）——
      也就是说 root 级设置项（`study.adapt`、`bilibili.buffer.*` …）**界面改不了**，
      要在板端 root 控制台里 `assistant shell` → `mode root` 才改得动。

    回执走 `TOPIC_CONFIG_RESULT`（带 payload 里的 id）—— GUI 靠它知道"写进去没有"，
    所以**每一条路径都必须 push**（包括失败），否则界面只能等到超时。

    @note 这里做的是**小文件**同步 I/O（几 KB），与其它命令处理器一样同步执行；
          不搬进 executor：一次几百微秒，换来的是"顺序一眼可见"。
    """
    request_id = payload.get("id")
    if not isinstance(request_id, str):
        request_id = ""

    reply: Dict[str, Any] = {
        "id": request_id,
        "ok": False,
        "changed": 0,
        "backup": "",
        "path": "",
        "error": "",
        "llm_env": {"ok": True, "changed": 0, "path": "", "error": ""},
    }

    def _finish() -> None:
        if push is not None:
            push(TOPIC_CONFIG_RESULT, reply)

    keys = payload.get("keys")
    if keys is None:
        keys = {}
    if not isinstance(keys, dict) or any(not isinstance(k, str) or not k.strip()
                                         or not isinstance(v, str) for k, v in keys.items()):
        reply["error"] = "keys 必须是 {点号路径: 字符串}（值一律按字符串传，类型由 Agent 校验）"
        _log.warning("ipc: set_config 载荷不合法（keys）")
        _finish()
        return

    credentials = payload.get("credentials")
    if credentials is None:
        credentials = {}
    if not isinstance(credentials, dict):
        reply["error"] = "credentials 必须是 {键: 字符串}"
        _log.warning("ipc: set_config 载荷不合法（credentials）")
        _finish()
        return

    config_file = getattr(runtime, "config_path_used", None) if runtime is not None else None
    if not config_file:
        reply["error"] = ("Agent 不知道自己用的 config.yaml 是哪一份（启动时没带 --config？）"
                          "—— 为了不改错文件，这次拒绝写入")
        _log.warning("ipc: set_config 被拒：runtime 没有 config_path_used")
        _finish()
        return
    reply["path"] = str(config_file)
    if not os.path.isfile(str(config_file)):
        reply["error"] = "真源不在: %s" % config_file
        _log.warning("ipc: set_config 被拒：真源不在 %s", config_file)
        _finish()
        return

    # ①② 校验（不写任何东西）
    try:
        cleaned_credentials = settings_credentials.clean_values(credentials)
    except settings_credentials.CredentialsError as exc:
        reply["error"] = "凭据不合法：%s" % exc
        _log.warning("ipc: set_config 凭据被拒：%s", exc)
        _finish()
        return

    if keys:
        try:
            config_tiers.plan_changes_for_tier(keys, target=str(config_file))
        except (settings_config.SettingsConfigError, config_tiers.ConfigTierError) as exc:
            reply["error"] = str(exc)
            _log.warning("ipc: set_config 被拒（没写任何文件）：%s", exc)
            _finish()
            return

    # ③ 写配置真源
    if keys:
        try:
            result = config_tiers.apply_changes_for_tier(keys, target=str(config_file))
        except (settings_config.SettingsConfigError, config_tiers.ConfigTierError) as exc:
            reply["error"] = "写 config.yaml 失败：%s" % exc
            _log.warning("ipc: set_config 写入失败：%s", exc)
            _finish()
            return
        reply["changed"] = int(result.get("changed", 0))
        reply["backup"] = str(result.get("backup", ""))
    reply["ok"] = True

    _finish_set_config(config_file, cleaned_credentials, reply, _finish)


def _finish_set_config(config_file: Any, cleaned_credentials: Dict[str, str],
                       reply: Dict[str, Any], finish: Any) -> None:
    """④⑤ 收尾：写凭据（另一个文件）+ 派生 `llm.env` + 回执。

    @note 从 `_handle_set_config` 里抽出来（T15-4 任务 3）：那个函数本来就在"超长函数
          120 行"的线附近, 加几行说明就过线了 —— 抽出来两边都清楚。
    @note 这一步失败时"配置**已经**写成功了"，所以原话必须说清那个中间态，
          而且**不能**因此把 `ok` 留在 true（凭据没写进去 = 这次请求没全成）。
    """
    if cleaned_credentials:
        try:
            config_data = read_config_file(str(config_file))
        except Exception as exc:                     # noqa: BLE001 - ConfigError 及 YAML 错
            reply["ok"] = False
            reply["error"] = "配置已写，但读不回真源（凭据没写）：%s" % exc
            _log.warning("ipc: set_config 凭据那步失败：%s", exc)
            finish()
            return
        try:
            settings_credentials.write_cookie(config_data, cleaned_credentials)
        except (settings_credentials.CredentialsError, OSError) as exc:
            reply["ok"] = False
            reply["error"] = "配置已写，但凭据写入失败：%s" % exc
            _log.warning("ipc: set_config 凭据写入失败：%s", exc)
            finish()
            return

    # ⑤ 派生 llm.env（同一个回执里报成败 —— 真源已经写成功了，不让它变 ok=false）
    try:
        env_result = llm_env.sync_from_config(config_path=str(config_file))
        reply["llm_env"] = {"ok": True, "changed": int(env_result.get("changed", 0)),
                            "path": str(env_result.get("path", "")), "error": ""}
    except llm_env.LlmEnvError as exc:
        reply["llm_env"] = {"ok": False, "changed": 0, "path": "", "error": str(exc)}
        _log.warning("ipc: set_config 派生 llm.env 失败：%s", exc)

    _log.info("ipc: set_config id=%s 改了 %d 行 (llm.env %s)",
              reply["id"] or "-", reply["changed"],
              "已同步" if reply["llm_env"]["ok"] else "没同步")
    finish()


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
            await _handle_chat_input(bus, payload, push)
            return

        if action == COMMAND_SWITCH_MODE:
            _handle_switch_mode(runtime, push, payload)
            return

        if action == COMMAND_QUERY_SCHEDULE:
            _handle_query_schedule(runtime, push)
            return

        if action == COMMAND_SET_CONFIG:
            _handle_set_config(runtime, payload, push)
            return

        if action == COMMAND_LLM_SERVICE:
            _handle_llm_service(runtime, payload, push)
            return

        if action == COMMAND_WIFI:
            await _handle_wifi(runtime, payload, push)
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

    # ⚠ T12-1: 走 `transition_to()` —— **按状态图的规矩**（跨模式时经 IDLE 两跳）。
    #   以前只调单跳 `transition()`, 于是一按"睡眠"（当前 GAME/STUDY）就被拒, 而且
    #   GAME 那边开着的视频/常驻模型没人放。现在每一跳都会触发释放。
    result = state.transition_to(mode, "ipc: switch_mode")
    if result.get("ok"):
        _log.info("ipc: switch_mode -> %s（%s）", mode,
                  " -> ".join(step["to"] for step in result.get("steps") or []) or mode)
        return

    current = getattr(state.current(), "value", "?")
    _log.warning(
        "ipc: switch_mode(%s) 被拒（当前 %s, %s）, 把真实状态推回给 GUI",
        mode, current, result.get("why") or "原因不明",
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


async def _handle_chat_input(bus: Any, payload: dict, push: Any = None) -> None:
    """GUI 发来的聊天输入 -> bus (source="gui")。"""
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        _log.warning("ipc: chat_input 缺少非空 text 字段, 已忽略: %r", payload)
        return
    if bus is None:
        _log.warning("ipc: 收到 chat_input 但 bus 未就绪, 已忽略")
        return

    # 只取文本, 不透传整个 payload: bus 事件是给 LLM 看的, 多塞字段只会让它分心
    # T15-17 / bug②：**来源如实取** ✓（缺省 "gui" ✓ ⇒ GUI 侧不用改协议 ✓）。
    source = str(payload.get("source") or "gui")
    event = await bus.push(source, text)
    # T15-17 / bug②：**非 GUI 来源的输入要回显给 GUI** ✓ ——
    #   复用它已经在渲染的 `llm` 通道 ✓，加 `role="user"` 标成"用户气泡" ✓
    #   （老客户端忽略未知字段不会坏 ✓）。
    if source != "gui" and push is not None:
        push(TOPIC_LLM, {"text": text, "role": "user"})
        _log.debug("ipc: 非 gui 来源的输入已回显给 GUI: source=%s", source)
    _log.debug("ipc: chat_input 已入队: %r", event.get("text"))


def _positive_int(value: Any, default: int) -> int:
    """配置里的正整数; 不合法就退回默认值 (配置错了不该拦住启动)。"""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        if value is not None:
            _log.warning("ipc: queue_size=%r 不是正整数, 用默认值 %d", value, default)
        return default
    return value
