# ============================================================================
#  agent/main.py — 把各组件串起来, 启动 asyncio 事件循环
#
#  启动顺序 (按依赖关系, 关了再反着来)
#  ---------------------------------------------------------------------------
#      1. 日志         logs/agent.log — 最早, 后面出错才有地方看
#      2. config       读 config.yaml; 读不到就用模板, 都不行就退出
#      3. native       moonlight.start_with_session() —— **失败不致命**, 只记 warning
#      4. ChatInputBus 多源汇合点, 后面所有组件都挂在它上面
#      5. io 层        image_reader / input_sender
#      6. 音乐         连 PC 的 neteasecli + 本地库 (T8-4; music.enabled=false 则什么都不做)
#      7. StateMachine SLEEP/IDLE/STUDY/GAME
#      8. ToolRouter   注册工具 (agent/tools/)
#      9. llama-server 按配置管本机 llama-server 的启停 (T7-4; 不管就什么都不做)
#     10. LLMProvider  edge / cloud / disabled
#     11. Scheduler    日程 + 终端命令 (订阅 bus, 只看不取)
#     12. IPC          Unix socket, GUI 接入点
#     13. 终端输入     可选 (config: terminal.enabled)
#
#  ⚠ 音乐(6)必须在 ToolRouter(8) 之前: 音乐四个工具**建的时候**就要知道"音乐开没开",
#    没开就整个不装（`services[...] = None` 是在装配 services 那一刻求值的）。
#
#  stop 时**严格反向**: terminal -> ipc -> scheduler -> llm -> llama-server -> tools
#                        -> state -> 音乐 -> io -> bus -> native
#
#  异常处理: 单个组件失败不影响其他组件
#  ---------------------------------------------------------------------------
#  每个组件都用 _guarded() 单独 start/stop。失败只记 error 并继续 ——
#  板子上的现实是"能跑起来比跑得全更重要": 摄像头没插不该让命令监听也失效。
#  失败的组件记进 failures, 收尾时给一份汇总。
#
#  关于 IPC (第 10 步)
#  ---------------------------------------------------------------------------
#  IPC 已经实现: agent/ipc/local_server.py 的 LocalServer, 由 agent/ipc/__init__.py
#  的 build_ipc(bus, config, runtime) 导出。这里按"找接入点"的方式接 —— 找不到
#  build_ipc() 就记一条 warning 跳过, **不假装**起了一个 IPC。
#  这样 agent/ipc/ 换实现时不用改本文件。
#
#  不做的事 (按约定)
#  ---------------------------------------------------------------------------
#  · 不做 daemon 化 —— 交给 systemd (见 docs 里的 service 示例)
#  · 不做优雅重启 —— 退出就是退出
#  · 不做配置热重载 —— 改配置请重启进程
# ============================================================================

from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# 允许 `python agent/main.py` 直接跑 (此时包根不在 sys.path 上)
if __package__ in (None, ""):  # pragma: no cover - 只在直接执行时走
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.config import ConfigError, ConfigNotFoundError, config_path, load_config
from agent.core import Scheduler, State, StateMachine, ToolRouter
from agent.core import read_intents
from agent.io import (
    ChatInputBus,
    ImageReader,
    InputSender,
)
from agent.llm import DEFAULT_MODE, LLMProvider, LlamaService, RuleEngine
from agent.core.wallpaper import DEFAULT_WALLPAPER_DIR, WallpaperDeck, WallpaperError

__all__ = ["Runtime", "run", "main", "setup_logging", "DEFAULT_LOG_PATH"]

LOGGER_NAME = "agent"

#: 日志默认落盘位置 (相对仓库根)
DEFAULT_LOG_PATH = "logs/agent.log"

#: 主循环处理多少条消息后退出; None = 一直跑 (由信号/stop_event 结束)
#: 给冒烟测试用: AGENT_MAX_EVENTS=1 python3 agent/main.py --dry-run
_ENV_MAX_EVENTS = "AGENT_MAX_EVENTS"

#: 最多跑多少秒后退出; 0/未设 = 不限
_ENV_RUN_SECONDS = "AGENT_RUN_SECONDS"


# ---------------------------------------------------------------------------
#  日志
# ---------------------------------------------------------------------------
def setup_logging(
    log_path: Optional[str] = None,
    level: int = logging.INFO,
    console: bool = True,
) -> logging.Logger:
    """配置 agent 日志: 同时写 logs/agent.log 和标准输出。

    @param log_path 默认取 $AGENT_LOG 或 logs/agent.log; 传 "" 表示不落盘
    @param level    文件日志级别 (控制台固定 INFO, 免得 debug 刷屏)
    @return agent 根 logger

    @note 幂等: 重复调用不会挂上第二组 handler (否则日志会成倍重复)。
    """
    root = logging.getLogger(LOGGER_NAME)
    root.setLevel(logging.DEBUG)
    root.propagate = False

    if getattr(root, "_agent_configured", False):
        return root

    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if log_path is None:
        log_path = os.environ.get("AGENT_LOG", DEFAULT_LOG_PATH)

    if log_path:
        path = Path(log_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(path, encoding="utf-8")
            file_handler.setLevel(level)
            file_handler.setFormatter(fmt)
            root.addHandler(file_handler)
        except (OSError, ValueError) as exc:
            # 落盘失败不该让程序起不来; 控制台还有一份。
            # ValueError 也要接: 路径里有 NUL 之类会让 Path.mkdir 抛 ValueError,
            # 只 catch OSError 的话这种路径会直接把启动打断。
            print("agent: cannot open log file %s: %s" % (path, exc), file=sys.stderr)

    if console:
        stream = logging.StreamHandler(sys.stdout)
        stream.setLevel(logging.INFO)
        stream.setFormatter(fmt)
        root.addHandler(stream)

    root._agent_configured = True  # type: ignore[attr-defined]
    return root


def _silence_noisy_libraries() -> None:
    """把第三方库的 INFO 压到 WARNING。

    它们 (尤其 asyncio/http 客户端) 会在 INFO 上打一堆与业务无关的行, 把
    "日志无报错"这件事变得没法一眼看清。
    """
    for name in ("asyncio", "urllib3", "openai", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _call_ipc_factory(factory: Callable[..., Any],
                      bus: Any,
                      config: Any,
                      runtime: Any) -> Any:
    """调 IPC factory, 兼容只收 ``(bus, config)`` 的旧签名。

    Phase 6 D4 把约定扩成 ``(bus, config, runtime=None)``: runtime 是出方向推送
    (status / llm) 唯一的来源 —— 状态机与回复都在它身上。但**不强制**:
    只认旧签名的 factory 照旧能用, 只是没有出方向。

    @note 用签名探测而不是 try/except TypeError: 后者会把 factory **内部**真正的
          TypeError 也当成"签名不对", 然后再调一次 —— 那是重复副作用。
    """
    try:
        params = inspect.signature(factory).parameters
    except (TypeError, ValueError):        # 内建/奇怪的可调用对象: 按旧签名试
        params = {}

    accepts_runtime = "runtime" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )
    if accepts_runtime:
        return factory(bus, config, runtime=runtime)
    return factory(bus, config)


# ---------------------------------------------------------------------------
#  组件基类: 只记 start/stop, 失败不致命
# ---------------------------------------------------------------------------
class _Component:
    """一个可启停的组件。

    @param name  组件名 (日志与 failures 汇总用)
    @param start 可调用 (sync 或 async); 失败会被 _guarded 记下
    @param stop  可调用; 没给就只记一条 "nothing to stop"
    """

    def __init__(
        self,
        name: str,
        start: Optional[Callable[[], Any]] = None,
        stop: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.name = name
        self._start = start
        self._stop = stop
        self.started = False
        self.start_error: Optional[BaseException] = None

    async def do_start(self) -> None:
        if self._start is not None:
            result = self._start()
            if asyncio.iscoroutine(result):
                await result

    async def do_stop(self) -> None:
        if self._stop is not None:
            result = self._stop()
            if asyncio.iscoroutine(result):
                await result


# ---------------------------------------------------------------------------
#  挑图结果 -> 给 LLM 看的清单
# ---------------------------------------------------------------------------
def _name_and_score(result: Any, limit: int) -> List[Dict[str, Any]]:
    """把 MatchResult 的候选转成 [{name, path, score}, …]。

    @note **文件名与完整路径都给**: 回话里说给用户听的是文件名（`xx.png`）,
          排查/日志里要的是全路径; 让模型自己拼 basename 容易拼错。
    """
    scores = (getattr(result, "detail", None) or {}).get("scores") or {}
    out: List[Dict[str, Any]] = []
    for path in list(getattr(result, "pool", ()) or ())[:max(1, int(limit))]:
        item: Dict[str, Any] = {"name": os.path.basename(path), "path": path}
        if path in scores:
            item["score"] = scores[path]
        out.append(item)
    return out


# ---------------------------------------------------------------------------
#  Runtime
# ---------------------------------------------------------------------------
class Runtime:
    """持有全部组件, 负责按序起停与主循环。

    可以脱离进程入口单独使用/测试::

        rt = Runtime(config=cfg)          # 不读盘, 直接给配置
        await rt.start()
        await rt.serve()                  # 主循环
        await rt.stop()
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        config_path_used: Optional[Path] = None,
        log: Optional[logging.Logger] = None,
        stop_event: Optional[asyncio.Event] = None,
        max_events: Optional[int] = None,
        start_terminal: bool = True,
        start_native: bool = True,
    ) -> None:
        """
        @param config          配置 dict; None 时 load_config()
        @param stop_event      外部停止信号 (测试/信号处理用)
        @param max_events      主循环处理多少条消息后退出 (冒烟测试)
        @param start_terminal  是否把 stdin 作为输入源接进 bus
        @param start_native    是否连接串流主机 (握手 + moonlight.start_with_session; 测试里关掉)
        """
        self.log = log or logging.getLogger("%s.main" % LOGGER_NAME)
        self.config = config if config is not None else load_config("config")
        self.config_path_used = config_path_used

        self.stop_event = stop_event or asyncio.Event()
        self.max_events = max_events
        self._want_terminal = start_terminal
        self._want_native = start_native

        # ---- 组件 (start 时按顺序建) ----
        self.bus: Optional[ChatInputBus] = None
        self.state: Optional[StateMachine] = None
        self.tools: Optional[ToolRouter] = None
        self.llm: Optional[LLMProvider] = None
        #: 本机 llama-server 的启停（T7-4）—— **None = 不管**（mode 不是 edge 或开关没开）
        self.llm_service: Optional[LlamaService] = None
        self.scheduler: Optional[Scheduler] = None
        self.image_reader: Optional[ImageReader] = None
        self.input_sender: Optional[InputSender] = None
        self.ipc: Any = None

        #: 回复钩子: 每产生一条 LLM 回复就调一次 (IPC 层用它推 llm 消息)。
        #: 默认 None —— 没有 IPC 时行为与以前完全一样。
        #: Phase 6 D4: 之前 handle_event() 的回复被主循环直接丢掉, 推不出去。
        self.on_reply: Optional[Callable[[str], Any]] = None

        #: 壁纸钩子: 换了一张壁纸就调一次 (IPC 层用它推 wallpaper{path,index})。
        #: 与 on_reply 同款"有就接" —— 没有 IPC 时换壁纸照样能算, 只是推不出去。
        #: T3: topic 名只由 agent/ipc/ 知道, 这里不认识任何线格式字段。
        self.on_wallpaper: Optional[Callable[[str, int], Any]] = None

        #: 壁纸目录 + 游标 (T3 起)。在 _start_state_and_tools() 里按配置建。
        self.wallpaper: Optional[WallpaperDeck] = None

        #: 标签索引 (T7-3): 读 config/wall_data.jsonl 的**只读**索引, 挑图用。
        #: **懒建** —— 数据文件可能还不存在（没跑过 assistant tag）, 而且它只在
        #: 对话里挑图时才用得上, 不该拖慢启动（40 张的向量解码也就几十毫秒, 但
        #: "启动路径上多一个可能出错的文件读"不划算）。
        self._tag_index: Optional[Any] = None

        #: 上一次由 `match` 挑中的那张 ({"spec", "path"}) —— T7-4:
        #: 同一个条件再来一次就"往下翻一名"（再换一张同类的）; 换了条件就从第 1 名重挑。
        self._last_match_pick: Dict[str, str] = {}

        #: T8-6: 上一次**推到屏幕上**的那张壁纸 —— "换成另一张"才算一次使用,
        #: 重推当前这张 / GUI 重连补推同一张都不重复计数。
        self._last_shown_wallpaper: str = ""

        #: 音乐（T8-4）: `agent/core/music.py::MusicPlayer` + 在 PC 上跑的 neteasecli。
        #: **None = 没开音乐**（配置 music.enabled=false 或没配 pc_host）——
        #: 与其它工具"缺依赖就跳过"同一条口径。
        self.music: Optional[Any] = None
        #: music 推送钩子（与 on_wallpaper 同款"有就接"）—— IPC 层把它接到 topic `music`
        self.on_music: Optional[Callable[[Dict[str, Any]], Any]] = None
        self._music_task: Optional[asyncio.Task] = None
        #: 上次推过的 (title, playing)，用来决定"这次要不要推"（进度每次都推）
        self._last_music_push: Tuple[str, bool] = ("", False)

        self._components: List[_Component] = []
        self._terminal_task: Optional[asyncio.Task] = None
        self.failures: List[Tuple[str, str]] = []
        self.stats: Dict[str, Any] = {
            "events": 0,
            "replies": 0,
            "llm_errors": 0,
            "tool_calls": 0,
        }
        self._started_at: Optional[float] = None

    # ------------------------------------------------------------ 配置读取 --
    def _cfg(self, *keys: str, default: Any = None) -> Any:
        """按 "sunshine.host" 这样的路径取配置。"""
        node: Any = self.config
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    # ------------------------------------------------------------ 启动 ---
    #: 启动步骤 (顺序即依赖顺序; stop 时反向遍历 _components)
    _STEPS = (
        "_start_native",
        "_start_bus_and_io",
        # ⚠ 音乐在工具之前: `agent/tools/` 里的音乐四件套要**建的时候**就知道
        #   "音乐开没开"（没开就整个不装）—— 所以 `self.music` 必须先就位。
        "_start_music",
        "_start_state_and_tools",
        "_start_llm_service",
        "_start_llm",
        "_start_scheduler",
        "_start_ipc",
        "_start_terminal_input",
    )

    async def start(self) -> None:
        """按依赖顺序启动全部组件。组件失败不打断后续启动。

        @note 每个步骤包两层保护:
                · 内层 _guarded(): 组件自己的 start 失败 -> 记该组件的名字
                · 外层 _guard_step(): 步骤里**组件之外**的代码失败
                  (例如解析配置时 int("abc") 抛错) 也被挡住
              只有两层都在, "单组件失败不影响其他组件"才真的成立。
        """
        self._started_at = time.monotonic()
        self.log.info("=== agent 启动 ===")
        self._log_config_summary()

        # 外部停止信号: SIGINT / SIGTERM 都转成 stop_event
        self._install_signal_handlers()

        for step_name in self._STEPS:
            await self._guard_step(step_name)

        if self.failures:
            self.log.warning(
                "%d 个组件启动失败 (其余照常运行): %s",
                len(self.failures),
                ", ".join("%s(%s)" % (n, e) for n, e in self.failures),
            )
        else:
            self.log.info("全部组件启动完成")

    async def _guard_step(self, step_name: str) -> None:
        """跑一个启动步骤, 把该步骤抛出的任何异常收进 failures。"""
        step = getattr(self, step_name)
        try:
            await step()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            label = step_name[len("_start_"):] if step_name.startswith("_start_") else step_name
            # 一行说明放 WARNING (这是"日志无报错"该看到的形态), 完整 traceback 放
            # DEBUG —— 排查时开 debug 就有, 正常跑不会把日志刷得像崩了。
            self.log.warning("启动步骤 %s 失败, 继续启动其余组件: %r", label, exc)
            self.log.debug("启动步骤 %s traceback", label, exc_info=True)
            self.failures.append((label, "%s: %s" % (type(exc).__name__, exc)))

    def _log_config_summary(self) -> None:
        source = self.config_path_used.name if self.config_path_used else "(注入的配置)"
        self.log.info(
            "配置来源=%s  llm.mode=%s  sunshine=%s:%s  app=%s  schedule.interval_min=%s",
            source,
            self._cfg("llm", "mode", default=DEFAULT_MODE),
            self._cfg("sunshine", "host", default="(未配置)"),
            self._cfg("sunshine", "port", default="-"),
            self._cfg("sunshine", "app", default="-"),
            self._cfg("scheduler", "interval_min", default="1"),
        )

    # ---- 1) native ----
    async def _sunshine_handshake(self, host: str, app: str, width: int, height: int, fps: int):
        """Sunshine 的 HTTPS 握手 (47984 + 客户端证书), 返回 (ServerInfo, SessionStart)。

        为什么握手在这一层而不是 C++: /serverinfo /applist /launch 都在 TLS +
        客户端证书后面, 而 native 侧原来那个手写的裸 socket HTTP 客户端
        (moonlight_connection.cpp, 已删除) 支持不了 TLS —— 要支持就得给交叉编译
        再引一个 OpenSSL。Python 的 ssl 本来就在, 所以这边拿到 app_version 与
        sessionUrl0, 交给 native 的 start_with_session() 直接进 LiStartConnection
        (见 agent/net/sunshine_client.py)。

        全是阻塞调用 -> 丢到线程里跑, 不占事件循环。
        """
        from agent.io._native import get_native
        from agent.net import (
            DEFAULT_HTTPS_PORT,
            DEFAULT_TIMEOUT,
            DEFAULT_UNIQUE_ID,
            SunshineClient,
            stream_mode,
        )

        cert = self._cfg("sunshine", "cert")
        key = self._cfg("sunshine", "key")
        https_port = int(self._cfg("sunshine", "https_port", default=DEFAULT_HTTPS_PORT))
        unique_id = str(self._cfg("sunshine", "unique_id", default=DEFAULT_UNIQUE_ID))

        # Sunshine 的扩展参数由 moonlight-common-c 说了算, 不抄一份常量进来。
        # (host 构建没链接 moonlight, 会拿到空串 —— 那是正常的)
        extra = ""
        try:
            extra = get_native().moonlight.launch_url_query_parameters()
        except Exception as exc:  # noqa: BLE001 - 取不到就当没有扩展参数
            self.log.debug("native: 取不到 launch_url_query_parameters (%s)", exc)

        client = SunshineClient(
            host,
            https_port,
            cert=cert or None,
            key=key or None,
            unique_id=unique_id,
            timeout=DEFAULT_TIMEOUT,
            launch_extra_query=extra or "",
            logger=self.log,
        )

        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, client.server_info)
        self.log.info(
            "sunshine: %s appversion=%s PairStatus=%s",
            host, info.app_version, info.pair_status,
        )
        app_id = await loop.run_in_executor(None, client.resolve_app_id, app)
        start = await loop.run_in_executor(
            None, client.start_session, app_id, stream_mode(width, height, fps)
        )
        # 主机上挂着别的 Moonlight 客户端时走的是 /resume (共存), 这里记下来
        self.log.info(
            "sunshine: app=%s -> appid=%s (走 /%s) sessionUrl=%s",
            app, app_id, start.endpoint, start.session_url,
        )
        return info, start

    async def _start_native(self) -> None:
        host = self._cfg("sunshine", "host")
        app = self._cfg("sunshine", "app")

        if not self._want_native:
            self.log.info("native: 已按要求跳过 moonlight.start_with_session (测试模式)")
            return
        if not host or not app:
            self.log.warning(
                "native: 未配置 sunshine.host / sunshine.app, 跳过串流连接 "
                "(只跑本地输入与规则, 不影响启动)"
            )
            return

        width = int(self._cfg("sunshine", "width", default=1280))
        height = int(self._cfg("sunshine", "height", default=720))
        fps = int(self._cfg("sunshine", "fps", default=60))

        async def _start() -> None:
            from agent.io._native import get_native, run_native

            # 1) 握手 (HTTPS 47984 + 客户端证书) —— 失败会带着主机原话抛出来
            info, start = await self._sunshine_handshake(
                host, str(app), width, height, fps
            )

            # 2) 把握手结果交给 native, 由它进 LiStartConnection (不再发 HTTP)
            ok = await run_native(
                "input_sender",
                get_native().moonlight.start_with_session,
                host, str(app), width, height, fps,
                info.app_version, info.gfe_version, info.codec_mode_support,
                start.session_url,
            )
            if not ok:
                # 失败原因在 native 的 status().error 里。这里的措辞很关键:
                # 握手已经成功, 所以别再让人去查 host/app/证书。
                reason = ""
                try:
                    status = get_native().moonlight.status()
                    reason = status.get("error") or ""
                except Exception:  # noqa: BLE001
                    pass
                raise RuntimeError(
                    "moonlight.start_with_session(%s, %s) 失败: %s —— 握手本身是成功的 "
                    "(appversion=%s, sessionUrl=%s), 所以问题在 native 侧 "
                    "(解码器初始化 / 会话被占)"
                    % (host, app, reason or "未返回原因", info.app_version, start.session_url)
                )

        async def _stop() -> None:
            from agent.io._native import get_native, run_native

            await run_native("input_sender", get_native().moonlight.stop)

        # native 是"可选增强": 连不上照样跑本地规则与终端命令
        await self._guarded(
            _Component("native", _start, _stop),
            fatal=False,
        )

    # ---- 2) bus + io ----
    async def _start_bus_and_io(self) -> None:
        async def _start_bus() -> None:
            self.bus = ChatInputBus()
            self.log.info("ChatInputBus 就绪 (终端 / GUI)")

        await self._guarded(_Component("chat_bus", _start_bus))

        async def _start_io() -> None:
            # IO 组件各自有专属执行器 (见 io/_native.py)
            self.image_reader = ImageReader()
            self.input_sender = InputSender()
            self.log.info("io 层就绪 (image_reader / input_sender)")

        await self._guarded(_Component("io", _start_io))

    # ---- 3) state + tools ----
    async def _start_state_and_tools(self) -> None:
        async def _start_state() -> None:
            self.state = StateMachine()
            self.log.info("StateMachine 就绪 (初始状态 %s)", self.state.current().value)

        await self._guarded(_Component("state_machine", _start_state))

        async def _start_tools() -> None:
            # 壁纸目录 + 游标（T3）。它不是"需要启动/停止的组件"，就是一个纯对象，
            # 所以不占一个启动步骤；目录不存在也不在这里报错（真换的时候才报给用户）。
            self.wallpaper = WallpaperDeck(self._cfg("wallpaper", "dir",
                                                    default=DEFAULT_WALLPAPER_DIR))
            self.tools = ToolRouter(
                state_provider=self.state,
                # 工具要用的依赖：agent/tools/ 从 router.services 取（缺了就让那个工具
                # 自己跳过并记 warning，见 agent/tools/__init__.py 的约定）。
                # io 在步骤 2 就绪，tools 是步骤 3，所以这里一定拿得到。
                services={
                    "input_sender": self.input_sender,
                    "image_reader": self.image_reader,
                    "bus": self.bus,
                    "config": self.config,
                    # 壁纸: 换一张（T7-3 起**只有对话**这一条入口, 见 next_wallpaper()）
                    "next_wallpaper": self.next_wallpaper,
                    # 壁纸: 看看库里有什么标签 / 某个 IP 最像哪几张（只读）
                    "wallpaper_tags": self.wallpaper_tags,
                    # 音乐（T8-5b: 三个工具里的 `next_music` 用这几条入口）
                    # ⚠ **没开音乐就是 None** —— 那个工具会自己跳过
                    "music_list": self.music_candidates if self.music else None,
                    "music_search": self.music_search if self.music else None,
                    "music_state": self.music_state if self.music else None,
                    "music_enqueue": self.music_enqueue if self.music else None,
                    "music_queue_clear": self.music_queue_clear if self.music else None,
                    "music_queue_state": self.music_queue_state if self.music else None,
                    "music_tag": self.music_tag if self.music else None,
                    "music_control": self.music_control if self.music else None,
                },
            )
            registered = self._register_tools(self.tools)
            self.log.info("ToolRouter 就绪 (%d 个工具)", registered)
            self.log.info("壁纸目录 = %s (%d 张)", self.wallpaper.directory,
                          self.wallpaper.count())
            # 标签数据在不在，启动日志里就看得见 —— 排障时第一眼看这一行
            # （**只查文件在不在**, 不建索引: 建索引留给第一次真挑图时）
            from agent.vision import wall_data as _wall_data

            tag_file = _wall_data.resolve_data_file(
                self._cfg("wallpaper", "tagging", "data_file"))
            self.log.info("壁纸标签数据 = %s (%s)", tag_file,
                          "有" if os.path.exists(tag_file)
                          else "还没有 —— 挑图前先在板端跑 assistant tag --apply")

        await self._guarded(_Component("tool_router", _start_tools))

    # ------------------------------------------------------------ 壁纸 ---
    def tag_index(self, reload: bool = False) -> Any:
        """取标签索引（`agent/vision/tag_index.TagIndex`，**只读**，懒建 + 缓存）。

        @param reload True 时强制重读数据文件（`assistant tag` 刚跑完时有用）
        @note 数据文件读不了 / 不存在都**不抛**: 返回一个空索引, 让调用方给出
              "还没有标签数据, 先跑 assistant tag" 这种**能照做**的提示。
        """
        if self._tag_index is not None and not reload:
            return self._tag_index

        from agent.vision import wall_data
        from agent.vision.tag_index import TagIndex, TagIndexError

        path = wall_data.resolve_data_file(self._cfg("wallpaper", "tagging", "data_file"))
        try:
            index = TagIndex.from_file(path)
        except TagIndexError as exc:
            self.log.warning("wallpaper: 标签数据读不了 (%s) —— 挑图会按空库处理", exc)
            index = TagIndex(data_file=path)
        for problem in index.problems:
            self.log.warning("wallpaper: 标签数据有问题: %s", problem)
        self.log.info("wallpaper: 标签索引就绪 (%d 张, 轴=%s, 词表向量=%s)",
                      index.count(), ",".join(index.axes()) or "-",
                      index.has_vocab_vectors())
        self._tag_index = index
        return index

    def wallpaper_tags(self, ip_query: Optional[str] = None,
                       limit: int = 5) -> Dict[str, Any]:
        """库里有哪些标签 / 某个 IP 最像哪几张（`list_wallpaper_tags` 工具的入口）。

        @param ip_query IP 名字（配置里 `wallpaper.tagging.ip_presets` 的键）
        @param limit    最多回几条
        @return {"ok", ...} —— ok=False 时 error 是**给人看**的一句话
        @note **只读**: 不换壁纸、不写任何文件（换壁纸是 next_wallpaper 的事）。
        """
        index = self.tag_index()
        presets = self._cfg("wallpaper", "tagging", "ip_presets", default={}) or {}
        if not index.count():
            return {"ok": False, "data_file": index.data_file,
                    "tell_user": "还没有壁纸标签数据 —— 先在板端跑 assistant tag --apply。",
                    "error": "还没有壁纸标签数据（%s）—— 先在板端跑 "
                             "assistant tag --apply（要 NPU，40 张约 3 分钟）"
                             % index.data_file}

        from agent.vision.tag_index import preset_names

        if ip_query:
            result = index.match("ip=%s" % ip_query, presets=presets,
                                 wallpaper_dir=self._wallpaper_dir(), limit=limit)
            if not result.ok:
                # ⚠ 板端实测: 模型会把**问句**填进 ip_query（"这个作品最像哪几张"），
                #    失败后它还会把"可用的 IP 名"当成"壁纸标签"答给用户 —— 所以除了
                #    error, 再给一句现成的**给用户看的话**（provider 会追加进正文）
                return {"ok": False, "error": result.error,
                        "tell_user": "没找到这个作品的锚点：%s" % result.error,
                        "presets": preset_names(presets)}
            return {"ok": True, "kind": result.kind, "ip": str(ip_query),
                    "note": result.note, "count": len(result.pool),
                    "images": _name_and_score(result, limit),
                    "presets": preset_names(presets)}

        summary = index.summarise(top=max(1, int(limit)))
        summary["ok"] = True
        summary["ip_presets"] = preset_names(presets)
        summary["note"] = ("挑图用 next_wallpaper(match=\"scene=anime\") 这种写法；"
                           "IP 用 match=\"ip=EVA\"；"
                           "挑\"用得最少的\"用 sort=\"used_asc\"（T8-6）。"
                           "分数是余弦（不是概率）。")
        return summary

    def _wallpaper_dir(self) -> Optional[str]:
        return self.wallpaper.directory if self.wallpaper is not None else None

    def next_wallpaper(self, step: int = 1, match: Optional[str] = None,
                       sort: Optional[str] = None) -> Dict[str, Any]:
        """换一张壁纸并把结果推给 GUI（T3 起；T7-3 起支持按标签/IP 挑；T8-6 起能按使用次数排）。

        入口只有一条: **对话**里 LLM 调 `next_wallpaper` 工具（手动按钮与
        `next_wallpaper` IPC 命令在 T7-3 按需求**删掉了**, 见 agent/core/wallpaper.py 的模块头）。

        @param step  正数往后、负数往前、0 = 重推当前这张（都在候选列表里翻）
        @param match 挑图条件（None = 按文件名顺序翻全部）:
                     `"scene=anime"` 某轴某标签 / `"anime"` 只写标签名 / `"ip=EVA"` 锚点检索。
                     **候选按相关度排好**（最像的在前），所以 step=1 就是"最像的那张"。
        @param sort  T8-6: 候选**再按使用次数排一遍** —— `"used_asc"` 用得最少的在前
                     （"挑一张我最少看到的"）/ `"used_desc"` 用得最多的在前 / None = 不动。
                     有 match 时是"在最像的那批里挑用得最少的"。
                     ⚠ 排序后会**跳过屏幕上当前这张**再从第 1 名拿 —— 连说两次
                     "再挑一张用得最少的"是一张张往少走, 不会挑回同一张、也不会跳回用得多的一头。
        @return {"ok", "path", "index", "total", "pushed", "used", "match"?, "score"?, "error"}
                     `used` = **记完这一次之后**的总次数（"这张一共显示过几次"）。
        @note 目录不存在/没图片属于运行期问题: 这里报错, 不让工具在启动时消失。
              标签数据还没打（或 match 写错）同样只是**这次失败**, 不抛异常。
        @note **状态权限不在这里判**: 那张表挂在工具上（`allowed_states`）, 由
              `ToolRouter.execute()` 统一拦。本文件因此仍然**不认识任何具体工具**。
        @note 这个方法是**同步**的: 推送入口（IPC 的 dispatcher）本身就是同步且线程安全的,
              而工具 handler 可能在线程池里跑（ToolRouter 对同步 handler 就是这么做的）。
        """
        if self.wallpaper is None:
            return self._wallpaper_failure("壁纸还没初始化（Agent 的 tools 步骤没起来？）")

        pool = None
        picked: Dict[str, Any] = {}
        # 锚点（只在"按内容挑 / 按使用次数挑"时给）:
        #   None = 这次是新的挑选, 从第 1 名开始; 给路径 = 从它往后翻。
        # ⚠ 不给 match/sort 的普通"换一张"**不能**传 anchor（T8-6 顺手修的 T7-4 遗留 bug:
        #   传 None 等于"假装还没选过", 于是"换一张"每次都挑回第 1 张 —— 实测连叫 4 次
        #   都是 01_a.png。普通换图要走游标, 也就是 step() 自己的默认值）。
        anchor: Any = None
        if match:
            resolved = self._resolve_match(match)
            if not resolved.get("ok"):
                return resolved
            pool = resolved["pool"]
            picked = resolved
            # ⚠ 只有"上一张就是这个 match 挑的"才从它往后翻（"再换一张同类的"）;
            #   换了条件（或者压根是第一次）就从**第 1 名**开始 —— 否则用户说
            #   "换一张动漫的"，会从当前那张在候选里的名次往下走（实测踩到:
            #   当前那张排第 28，于是挑回来第 29 名，根本不是"最像的"）。
            previous = self._last_match_pick or {}
            if previous.get("spec") == match.strip() and previous.get("path") in pool:
                anchor = previous.get("path")

        # T8-6: 按使用次数重排候选（"挑用得最少的"）。⚠ 排完之后"上次挑的那张再往后翻"
        # 就不成立了（名次会随计数变），所以改成: **先把当前屏幕上这张从候选里去掉**,
        # 再从第 1 名拿。板端实测过两种错法:
        #   · 从"当前这张在排序里的名次"往后走 -> 第一次挑完它自己就跑到用得多的一头去了,
        #     第二次会跳到 **index=39/40**（用得最多的那一档）;
        #   · 不跳过当前这张 -> 用户说"再挑一张用得最少的"却拿到同一张。
        # 现在是"在**屏幕上没有**的那些里挑用得最少的", 连说几次会一张张往少走。
        if sort:
            pool, picked = self._sort_pool_by_usage(pool, match, sort)
            if pool is None:
                return picked                            # 失败（原因已在里面）
            current = self.wallpaper.current()
            if len(pool) > 1 and current in pool:
                pool = [path for path in pool if path != current]
            anchor = None

        try:
            if match or sort:
                index, path, total = self.wallpaper.step(step, pool=pool, anchor=anchor)
            else:                                   # 普通"换一张": 按游标一格一格走
                index, path, total = self.wallpaper.step(step)
        except WallpaperError as exc:
            self.log.warning("wallpaper: 换不了: %s", exc)
            return self._wallpaper_failure(str(exc))

        pushed = self._push_wallpaper(path, index)
        used = self.wallpaper_usage(path)
        self.log.info("wallpaper: %s (index=%d/%d%s%s, pushed=%s, used=%d)", path, index, total,
                      ", match=%s" % match if match else "",
                      ", sort=%s" % sort if sort else "", pushed, used)
        out: Dict[str, Any] = {"ok": True, "path": path, "index": index,
                               "total": total, "pushed": pushed, "used": used}
        if match:
            self._last_match_pick = {"spec": match.strip(), "path": path}
            out["match"] = {"spec": match, "kind": picked.get("kind"),
                            "note": picked.get("note"),
                            "candidates": len(pool or ()),
                            "rank": index + 1}
            score = (picked.get("scores") or {}).get(path)
            if score is not None:
                out["score"] = score
        if sort:
            out["sort"] = sort
        return out

    def _sort_pool_by_usage(self, pool: Optional[list], match: Optional[str],
                            sort: str) -> Any:
        """把候选按使用次数排一遍（T8-6）。

        @return (新候选, 说明 dict) —— 失败时返回 (None, 失败 dict)
        """
        order = str(sort or "").strip().lower()
        if order not in ("used_asc", "used_desc"):
            return None, self._wallpaper_failure("不认识的 sort %r（可用的: used_asc / used_desc）"
                                                 % sort)
        index = self.tag_index()
        if not index.count():
            return None, self._wallpaper_failure(
                "还没有壁纸标签数据（%s）—— 按使用次数挑也要先跑 assistant tag --apply"
                % index.data_file)
        ranked = index.rank_by_usage(ascending=(order == "used_asc"), pool=pool)
        paths = [item["path"] for item in ranked]
        if not paths:
            return None, self._wallpaper_failure("没有可挑的壁纸（候选是空的）")
        note = "%s：%d 张按使用次数排序（%s），最少的 %d 次、最多的 %d 次" % (
            "用得最少优先" if order == "used_asc" else "用得最多优先", len(paths),
            "升序" if order == "used_asc" else "降序",
            ranked[0]["used"], ranked[-1]["used"])
        return paths, {"kind": "usage", "note": note, "scores": {}}

    def wallpaper_usage(self, path: str) -> int:
        """某张壁纸被显示过几次（T8-6；读数据文件，缺字段 = 0）。"""
        try:
            return int(self.tag_index().usage_of(path).get("used") or 0)
        except Exception as exc:                     # noqa: BLE001 - 计数读不到不该影响换图
            self.log.warning("wallpaper: 读使用次数失败 (当成 0): %r", exc)
            return 0

    def _note_wallpaper_shown(self, path: str, count: bool = True) -> None:
        """屏幕上**换成了**这一张 -> 使用次数 +1（T8-6）。

        @param count False = 只是"同步显示"（开机后补推当前这张）, 记下是谁但**不计**
        ⚠ 判据是"换成了另一张"（path 跟上次推上去的不一样）—— 重推当前这张（step=0）、
          GUI 重连补推、同一次换图重复调用都**不算**新的使用。
        @note 计数写盘失败只记 warning: 一个统计数字不该让"换壁纸"这件事失败。
        """
        if not path or path == self._last_shown_wallpaper:
            return
        self._last_shown_wallpaper = path           # 屏幕上现在是这张（先记下, 免重复写盘）
        if not count:
            return
        from agent.vision import wall_data

        data_file = wall_data.resolve_data_file(self._cfg("wallpaper", "tagging", "data_file"))
        try:
            used = wall_data.bump_usage_in_file(data_file, path)
        except wall_data.WallDataError as exc:
            self.log.warning("wallpaper: 使用次数写盘失败 (已忽略): %s", exc)
            return
        if used is None:
            self.log.info("wallpaper: %s 不在标签数据里 —— 不计使用次数", path)
            return
        # 内存里的索引也要跟上, 否则同一次会话里再挑"用得最少的"会看到旧数字
        self._tag_index = None
        self.log.info("wallpaper: 使用次数 %s -> %d", os.path.basename(path), used)

    def _wallpaper_failure(self, reason: str, **extra: Any) -> Dict[str, Any]:
        """换壁纸失败的统一返回（T7-4 (c)）。

        @param reason 给人看的原因
        @return {"ok": False, "error": reason, "tell_user": "换壁纸没有成功：…", **extra}
        @note `tell_user` 是**工具给模型的一句现成话**（祈使句），`agent/llm/provider.py`
              还会把失败追加进最终正文 —— 因为板端实测 0.6B 会**谎报成功**
              （工具明明失败了, 它回"已更换为宁静的深色风景"）。用户看不到工具结果,
              所以这句必须由机制保证出现。
        """
        out: Dict[str, Any] = {"ok": False, "error": reason,
                               "tell_user": "换壁纸没有成功：%s" % reason,
                               "instruction": "请把这句如实告诉用户，不要说已经换好了。"}
        out.update(extra)
        return out

    def _resolve_match(self, match: str) -> Dict[str, Any]:
        """把 match 解析成候选路径（按相关度降序）。失败时返回 {"ok": False, "error"}。"""
        index = self.tag_index()
        if not index.count():
            return self._wallpaper_failure(
                "还没有壁纸标签数据（%s）—— 先在板端跑 assistant tag --apply，"
                "之后才能按内容挑图" % index.data_file,
                data_file=index.data_file)
        presets = self._cfg("wallpaper", "tagging", "ip_presets", default={}) or {}
        resolved = index.match(match, presets=presets, wallpaper_dir=self._wallpaper_dir())
        if not resolved.ok:
            self.log.warning("wallpaper: match=%r 用不了: %s", match, resolved.error)
            return self._wallpaper_failure(resolved.error)
        if not resolved.pool:
            return self._wallpaper_failure("没有符合条件的壁纸（%s）"
                                           % (resolved.note or match))
        return {"ok": True, "pool": resolved.pool, "kind": resolved.kind,
                "note": resolved.note, "scores": resolved.detail.get("scores") or {}}

    # ---- 3.6) 音乐 (T8-4) ----
    async def _start_music(self) -> None:
        """按配置接上"在 PC 上跑的 neteasecli" + 本地音乐库（`music.enabled`）。

        ⚠ 关掉时**什么都不做**（`self.music is None`）—— 工具会自己跳过, 与其它工具同款。
        ⚠ 轮询放在一个后台任务里: 每 `poll_interval_s` 问一次 PC 的真实进度, 推给 GUI。
          走 ssh 要 0.5–1.5 s, 所以丢线程池, 不卡事件循环。
        """

        async def _start() -> None:
            from agent.core.music import MusicPlayer
            from agent.media.music_library import resolve_library_file
            from agent.net.netease_cli import NeteaseCli

            cli = NeteaseCli.from_config(self.config, log=self.log)
            if cli is None:
                self.log.info("music: 没开（config 的 music.enabled/pc_host）—— 音乐工具不装")
                return
            library_file = resolve_library_file(self._cfg("music", "library_file"))
            self.music = MusicPlayer(
                cli, library_file,
                count_after_s=int(self._cfg("music", "count_after_s", default=30) or 30),
                log=self.log)
            self.log.info("music: 就绪 (PC=%s@%s, 本地库=%s)",
                          getattr(cli, "user", "?"), getattr(cli, "host", "?"), library_file)
            self._music_task = asyncio.ensure_future(self._music_loop())

        async def _stop() -> None:
            if self._music_task is not None:
                self._music_task.cancel()
                self._music_task = None

        await self._guarded(_Component("music", _start, _stop), fatal=False)

    async def _music_loop(self) -> None:
        """每 `poll_interval_s` 问一次 PC 的真实进度, 变化就推给 GUI。"""
        from agent.core.music import DEFAULT_POLL_INTERVAL_S

        interval = float(self._cfg("music", "poll_interval_s",
                                   default=DEFAULT_POLL_INTERVAL_S) or DEFAULT_POLL_INTERVAL_S)
        loop = asyncio.get_running_loop()
        while True:
            try:
                await asyncio.sleep(interval)
                snapshot = await loop.run_in_executor(None, self.music.refresh)
                self._push_music(snapshot)
            except asyncio.CancelledError:
                raise
            except Exception as exc:        # noqa: BLE001 - 轮询失败不该把任务搞死
                self.log.warning("music: 轮询失败 (已忽略): %r", exc)

    def _push_music(self, snapshot: Dict[str, Any]) -> bool:
        """把状态推给 GUI（**变化才推**: 曲目/播放状态一变必推, 播放中每次带新进度推）。"""
        if self.on_music is None:
            return False
        key = (str(snapshot.get("title") or ""), bool(snapshot.get("playing")))
        previous = self._last_music_push
        if key == previous and not key[1]:
            return False                       # 没在放、曲目也没变 -> 不重复推
        try:
            self.on_music(dict(snapshot))
            self._last_music_push = key
            return True
        except Exception as exc:                # noqa: BLE001 - 推送失败不该让轮询停
            self.log.warning("music: 推送失败 (已忽略): %r", exc)
            return False

    def push_current_music(self) -> bool:
        """GUI 刚连上时补推一次当前状态（与 `push_current_wallpaper` 同款）。"""
        if self.music is None:
            return False
        return self._push_music(self.music.snapshot())

    # ---- 音乐动作（GUI 命令与工具都走这里）----
    def music_state(self) -> Dict[str, Any]:
        """当前播放状态（**不碰 PC**: 缓存真值 + 本地外推）。"""
        if self.music is None:
            return {"ok": False, "error": "音乐没开（config.yaml 的 music.enabled）"}
        return self.music.snapshot()

    def music_control(self, action: str, **kwargs: Any) -> Dict[str, Any]:
        """播放控制（play_pause / pause / resume / stop / next / prev / seek / volume）。

        @return {"ok", ...} —— ok=False 时 error 是**给人看的**一句话
        """
        if self.music is None:
            return {"ok": False, "error": "音乐没开（config.yaml 的 music.enabled）"}
        from agent.core.music import MusicError

        try:
            if action in ("play_pause", "toggle"):
                out = self.music.toggle()
            elif action == "pause":
                out = self.music.pause()
            elif action == "resume":
                out = self.music.resume()
            elif action == "stop":
                out = self.music.stop()
            elif action == "next":
                out = self.music.step(1)
            elif action == "prev":
                out = self.music.step(-1)
            elif action == "seek":
                out = self.music.seek(float(kwargs.get("seconds") or 0),
                                      absolute=bool(kwargs.get("absolute")))
            elif action == "volume":
                out = self.music.set_volume(int(kwargs.get("level") or 0))
            else:
                return {"ok": False, "error": "不认识的动作 %r" % action}
        except (MusicError, Exception) as exc:  # noqa: BLE001 - 失败一律如实回话
            if isinstance(exc, MusicError):
                self.log.warning("music: %s 失败: %s", action, exc)
            else:
                self.log.warning("music: %s 出错: %r", action, exc)
            return {"ok": False, "error": str(exc), "tell_user": str(exc)}
        result = {"ok": True, "action": action}
        result.update(out)
        self._push_music(self.music.snapshot())
        return result

    def music_search(self, keyword: str, limit: int = 5,
                     kind: str = "track") -> Dict[str, Any]:
        """在 PC 上搜歌（返回候选 id/名字, **不播**）—— 给 chat 挑。"""
        if self.music is None:
            return {"ok": False, "error": "音乐没开（config.yaml 的 music.enabled）"}
        from agent.net.netease_cli import NeteaseCliError

        try:
            data = self.music.cli.search(kind, keyword, limit=int(limit))
        except NeteaseCliError as exc:
            return {"ok": False, "error": str(exc), "tell_user": str(exc)}
        tracks = data.get("tracks") or []
        return {"ok": True, "keyword": keyword, "count": len(tracks),
                "tracks": [{"id": str(t.get("id")), "name": t.get("name"),
                            "artists": ", ".join(str(a.get("name"))
                                                 for a in (t.get("artists") or [])
                                                 if isinstance(a, dict)),
                            "album": (t.get("album") or {}).get("name"),
                            "duration_ms": t.get("duration")}
                           for t in tracks if isinstance(t, dict)]}

    def music_enqueue(self, track_id: Any = None, keyword: Optional[str] = None,
                      tag: Optional[str] = None, sort: str = "plays_asc",
                      limit: int = 1, replace: bool = False) -> Dict[str, Any]:
        """把歌**加进环形队列**（chat 的两个决定之一；T8-5b）。

        四种给法（都给 `limit` 首，默认 1 首）::

            track_id=…              就这一首（一般来自 list/search 的清单）
            keyword=…               在 **PC 上搜**，取前 limit 首
            tag="mood=energetic"    在**本地库**按标签挑前 limit 首（统一语法，见 label_spec）
            （什么都不给）          本地库里按 `sort` 挑（默认 `plays_asc` = 听得最少的）

        @param replace True = 先清空队列再加（"换一批"一句话说完）
        @return {"ok", "queued": [id…], "started", "tracks", "queue"}
        @note **A1（你定的）**: 排完之后如果 PC 上什么都没在放 —— 顺手从新排的第一首起播
              （否则就是"chat 安排了队列, 用户什么都听不到"）。放不起来时**如实报错**
              （队列照旧留在那儿, 错误里说清是"已排进队列但 PC 没放起来"）。
              ⚠ 这一步要走 ssh + schtasks（板端实测 5~8 s）, 比默认工具超时（5 s）长 ——
                所以 `next_music` 这个工具自己声明了 `timeout_s`（见 tools/music.py）。
        @note `track_id` 会**去 PC 校验一次**（板端实测 0.6B 会编造 id）—— 问不到就如实报错。
        @note T8-7: 起播前会 `refresh()` 一次真值（见下）, 所以"PC 上已经在放"时**不会**
              把用户正在听的那首停掉重排。
        @note 这里只动**队列与播放**; 标签/播放次数不碰（那是 music_tag / 计次的事）。
        """
        if self.music is None:
            return {"ok": False, "error": "音乐没开（config.yaml 的 music.enabled）"}
        from agent.core import label_spec
        from agent.core.music import MusicError

        count = max(1, int(limit or 1))
        resolved: List[Dict[str, Any]] = []

        if track_id:
            resolved = [{"id": str(track_id), "meta": {}}]
        elif keyword:
            found = self.music_search(keyword, limit=count)
            if not found.get("ok"):
                return found
            tracks = (found.get("tracks") or [])[:count]
            if not tracks:
                return {"ok": False, "error": "PC 上没搜到 %r" % keyword,
                        "tell_user": "没搜到这首歌（换几个关键词试试）"}
            resolved = [{"id": str(t["id"]), "meta": t} for t in tracks]
        else:
            axis = None
            wanted: Any = None
            if tag:
                axis, values = label_spec.parse_one(tag)
                if axis == label_spec.EMPTY_AXIS:
                    axis = None                   # 只写了标签名 -> 所有轴里找
                if not values:
                    return {"ok": False, "error": "tag 是空的：要么别给, 要么写成 mood=energetic",
                            "tell_user": "标签写法不对（要么别给, 要么写成 mood=energetic）"}
                wanted = values[0] if len(values) == 1 else values
            picked = self.music.candidates(tag=wanted, axis=axis, sort=sort, limit=count)
            if not picked:
                return {"ok": False,
                        "error": ("本地库里没有%s的歌" % ("带 %s 标签" % tag if tag else "任何"))
                                 + " —— 用 keyword 去 PC 上搜一首，或先导入歌单",
                        "tell_user": "本地库里没有符合条件的歌（可以让我用别的关键词去搜）"}
            resolved = [{"id": str(t["id"]), "meta": t} for t in picked]

        queued: List[str] = []
        records: List[Dict[str, Any]] = []
        for index, item in enumerate(resolved):
            try:
                records.append(self.music.enqueue(item["id"], meta=item.get("meta") or {},
                                                  replace=bool(replace) and index == 0))
            except MusicError as exc:
                if queued:
                    break                          # 前面几首已经排进去了, 如实说清
                return {"ok": False, "error": str(exc), "tell_user": str(exc)}
            queued.append(str(item["id"]))

        # T8-7: 排之前**问一次 PC 的真值** —— `snapshot()` 是纯本地的（不碰 PC）,
        # 刚重启的 Agent 会以为"PC 上没在放", 于是把用户正在听的那首停掉从头起播。
        # 一次 ssh 换一句真话, 值（失败只记 warning: 问不到就按本地那份判）。
        self.music.refresh()
        will_start = bool(queued) and not bool(self.music.snapshot().get("playing"))
        started = False
        if will_start:
            try:
                self.music.play(queued[0])
                started = True
            except MusicError as exc:
                self._push_music(self.music.snapshot())
                return {"ok": False, "queued": queued, "started": False,
                        "queue": self.music.queue_state(),
                        "error": "已经排进队列, 但 PC 那边没放起来: %s" % exc,
                        "tell_user": "歌已经排进队列了，但 PC 上没放起来：%s" % exc}
        self._push_music(self.music.snapshot())
        return {"ok": True, "queued": queued, "started": started,
                "will_start": will_start, "queue": self.music.queue_state(),
                "tracks": [{"id": r.get("track_id") or r.get("id"), "name": r.get("name"),
                            "artists": r.get("artists"), "plays": r.get("plays"),
                            "tags": r.get("tags"), "matched": r.get("matched")}
                           for r in records]}

    def music_queue_clear(self) -> Dict[str, Any]:
        """清空环形队列（chat 的另一个决定）。**不停播放** —— 停/放是 GUI 按钮的事。"""
        if self.music is None:
            return {"ok": False, "error": "音乐没开（config.yaml 的 music.enabled）"}
        result = self.music.clear_queue()
        return {"ok": True, "cleared": result["cleared"], "queue": result["queue"]}

    def music_queue_state(self) -> Dict[str, Any]:
        """队列现状（**只给工具/日志看**；T8-5b 你定的: 不推给 GUI）。"""
        if self.music is None:
            return {"ok": False, "error": "音乐没开（config.yaml 的 music.enabled）"}
        state = self.music.queue_state()
        names = {str(t.get("id")): t.get("name") for t in self.music.tracks()}
        state["ok"] = True
        state["tracks"] = [{"id": track_id, "name": names.get(track_id)}
                           for track_id in state["ids"]]
        return state

    def music_candidates(self, tag: Optional[str] = None, axis: Optional[str] = None,
                         sort: str = "plays_asc", limit: int = 10) -> Dict[str, Any]:
        """从**本地库**挑候选（"下一首由 chat 决定"就看这个清单）。"""
        if self.music is None:
            return {"ok": False, "error": "音乐没开（config.yaml 的 music.enabled）"}
        picked = self.music.candidates(tag=tag, axis=axis, sort=sort, limit=limit)
        return {"ok": True, "count": len(picked), "sort": sort, "tag": tag,
                "tracks": [{"id": t.get("id"), "name": t.get("name"),
                            "artists": t.get("artists"), "plays": t.get("plays"),
                            "tags": t.get("tags"), "matched": t.get("matched")}
                           for t in picked],
                "summary": self.music.summary()}

    def music_tag(self, tags: Dict[str, Any], track_id: Optional[str] = None,
                  remove: bool = False) -> Dict[str, Any]:
        """chat 补充（或删掉）tag（不给 id 就作用于当前这首）。"""
        if self.music is None:
            return {"ok": False, "error": "音乐没开（config.yaml 的 music.enabled）"}
        from agent.core.music import MusicError

        try:
            record = self.music.tag_track(tags, track_id=track_id, remove=remove)
        except MusicError as exc:
            return {"ok": False, "error": str(exc), "tell_user": str(exc)}
        return {"ok": True, "track_id": record.get("id"), "tags": record.get("tags")}

    def push_current_wallpaper(self) -> bool:
        """把一个 GUI 刚连上来该看到的壁纸补推一次（T6）。

        @return 是否真的推了（目录用不了/没有图片/没有推送入口 -> False）
        @note 这是"**同步显示**", 不是"换一张": 所以**不受状态表限制** ——
              客户端连上时 Agent 可能正处在 SLEEP/GAME, 补一张当前壁纸不该被拒。
        @note 从来没选过任何一张时, 会**顺手选第一张**当初始画面（只动游标, 不做别的）。
        @note T8-6: 这一路**只在真换过图之后才计数**。开机/重启后 GUI 连上来的第一次
              补推不算一次使用（那张图本来就是屏幕上的, 不是用户换的）—— 否则每次
              重启 Agent 都会给同一张图白加一次。
        """
        if self.wallpaper is None:
            return False
        previous = self.wallpaper.current()        # None = 这次是"初始画面"
        snapshot = self.wallpaper.snapshot(initialise=True)
        if snapshot is None:
            self.log.debug("wallpaper: 没有可补推的壁纸（目录空的或读不了）")
            return False
        index, path, total = snapshot
        pushed = self._push_wallpaper(path, index, count=previous is not None)
        self.log.info("wallpaper: 给刚连上的 GUI 补推 %s (index=%d/%d, pushed=%s)",
                      path, index, total, pushed)
        return pushed

    def _push_wallpaper(self, path: str, index: int, count: bool = True) -> bool:
        """把一张壁纸交给 IPC 推出去（没有 IPC 时返回 False, 不抛）。

        @param count False = 只推**不计**（"同步显示当前这张", 不是"换成了这一张"）
        @note T8-6: 推出去之后**顺手记一次**（`_note_wallpaper_shown`）——
              判据是"换成了另一张", 所以重推/补推不会重复计数。
              推送**失败**（抛异常）就不记: 那张图并没真的上屏。计数写盘失败只记 warning。
        """
        pushed = False
        if self.on_wallpaper is None:
            self.log.info("wallpaper: 没有 IPC 推送入口（没接 GUI），只更新了游标")
        else:
            try:
                self.on_wallpaper(path, index)
                pushed = True
            except Exception as exc:        # noqa: BLE001 - 推送失败不该让"换壁纸"失败
                self.log.warning("wallpaper: 推送失败 (已忽略): %r", exc)
        if pushed or self.on_wallpaper is None:
            self._note_wallpaper_shown(path, count=count)
        return pushed

    def _register_tools(self, router: ToolRouter) -> int:
        """注册工具。

        工具在 `agent/tools/`（Phase 7 起有真工具了）：它提供 `build_tools(router)`
        或 `TOOLS`，本文件不用认识任何具体工具。工具自己从 `router.services` 取依赖。
        找不到模块/工厂时按"还没有工具"处理 —— 空的 ToolRouter 完全可用，
        不该因此起不来。
        """
        try:
            module = importlib.import_module("agent.tools")
        except ImportError as exc:
            self.log.warning(
                "tools: 导不进 agent/tools (%r), 工具列表为空 "
                "(LLM 仍可对话, 只是没有工具可调)",
                exc,
            )
            return 0

        factory = getattr(module, "build_tools", None) or getattr(module, "TOOLS", None)
        if factory is None:
            self.log.warning("tools: agent/tools/ 里没有 build_tools()/TOOLS, 工具列表为空")
            return 0

        try:
            items = factory(router) if callable(factory) else factory
        except Exception as exc:  # noqa: BLE001 - 工具注册失败不该拖垮启动
            self.log.error("tools: build_tools() 失败: %r", exc, exc_info=True)
            return 0

        count = 0
        for item in items or []:
            try:
                router.register(item)
                count += 1
            except Exception as exc:  # noqa: BLE001
                self.log.error("tools: 注册 %r 失败: %r", getattr(item, "name", item), exc)
        return count

    # ---- 3.5) 本机 llama-server 的启停 (T7-4) ----
    async def _start_llm_service(self) -> None:
        """按配置管本机 llama-server：Agent 启动 / 离开 SLEEP 起服务, 进入 SLEEP 停服务。

        ⚠ **默认不管**（`llm.manage_service` 缺省 false）: 管启停意味着"Agent 可能把你
          手动起的服务停掉", 这是明确的行为改变, 要人在 config.yaml 里打开。
        ⚠ 起服务的脚本是 `llm/scripts/start.sh`（nohup + 立刻返回）, 所以这一步**不等**
          模型加载完 —— 之后在后台探一次"到底能用了没", 只写日志（不阻塞启动）。
        ⚠ 脚本失败**不 fatal**: 服务没起来只该让 edge 降级（provider 本来就会记一条
          "LLM 降级为规则兜底"）, 不该让整个 Agent 起不来。
        """

        async def _start() -> None:
            service = LlamaService.from_config(self.config, log=self.log)
            if service is None:
                mode = self._cfg("llm", "mode", default="?")
                self.log.info("llm_service: 不管 llama-server 的启停"
                              "（mode=%s, manage_service=%s）",
                              mode, self._cfg("llm", "manage_service", default=False))
                return
            self.llm_service = service
            ok, message = service.start()
            if ok:
                self.log.info("llm_service: 已启动 llama-server (%s)", message)
            else:
                self.log.warning("llm_service: 启动 llama-server 没成功: %s", message)
            # 状态回调：进 SLEEP 停、离开 SLEEP 起
            if self.state is not None:
                self.state.on_change(self._on_state_change)
                self.log.info("llm_service: 已挂上 SLEEP 的启停回调")
            # 后台探活（只写日志: "什么时候真的能用了"对排障最有用）
            asyncio.get_running_loop().run_in_executor(None, self._log_when_ready)

        await self._guarded(_Component("llm_service", _start), fatal=False)

    def _log_when_ready(self) -> None:
        """（线程里跑）等到服务真的应答，记一行日志。"""
        service = self.llm_service
        if service is None:
            return
        ready, waited = service.wait_ready()
        if ready:
            self.log.info("llm_service: llama-server 就绪 (等了 %.1f s)", waited)
        else:
            self.log.warning("llm_service: 等了 %.1fs 还没就绪（模型没加载完 / key 不对?）"
                             "—— edge 模式下这会儿会降级到规则兜底", waited)

    def _on_state_change(self, old: State, new: State) -> None:
        """进入 SLEEP 停服务、离开 SLEEP 起回来（T7-4 的要求）。

        @note 只在 `llm_service` 存在时动（= mode=edge 且 manage_service 打开）
        @note 状态机要求**任何切换都经过 IDLE**, 所以"离开 SLEEP"就是 SLEEP→IDLE
        @note 这个回调在状态机调用栈里跑（同步）: 起停都是 subprocess, 所以丢到
              **后台线程**去做, 不卡状态切换与 GUI 的状态推送
        """
        service = self.llm_service
        if service is None:
            return
        if new is State.SLEEP and old is not State.SLEEP:
            self.log.info("llm_service: 进入 SLEEP -> 停 llama-server")
            self._run_service_op(service.stop, "停")
        elif old is State.SLEEP and new is not State.SLEEP:
            self.log.info("llm_service: 离开 SLEEP -> 起 llama-server")
            self._run_service_op(service.start, "起")

    def _run_service_op(self, operation: Callable[[], Tuple[bool, str]], what: str) -> None:
        """在后台线程里跑一次起/停（失败只记日志, 不抛给状态机）。"""

        def run() -> None:
            try:
                ok, message = operation()
            except Exception as exc:                  # noqa: BLE001
                self.log.warning("llm_service: %s llama-server 时出错: %r", what, exc)
                return
            level = logging.INFO if ok else logging.WARNING
            self.log.log(level, "llm_service: %s llama-server -> %s", what, message)
            if ok and what == "起":
                self._log_when_ready()

        threading.Thread(target=run, name="llm-service", daemon=True).start()

    # ---- 4) llm ----
    async def _start_llm(self) -> None:
        async def _start() -> None:
            mode = self._cfg("llm", "mode", default=None)
            self.llm = LLMProvider(
                mode=mode,
                tools=self.tools,
                config_loader=lambda: self.config,
                rule_engine=RuleEngine(),
            )
            for warning in self.llm.mode_errors:
                self.log.warning("llm: %s", warning)
            self.log.info("LLMProvider 就绪 (mode=%s)", self.llm.mode())
            if self.llm.mode() == "disabled":
                self.log.info("llm: 规则兜底模式 —— 只回问候/时间/状态, 接入模型请改 llm.mode")
            if self.llm.mode() == "edge":
                # 说清"连哪儿、哪个模型、参数是什么" —— 板端排障第一眼看的就是这一行
                self.log.info("llm: edge 后端 = %s", self.llm.edge_backend.describe())
                if not self.llm.edge_backend.is_ready():
                    self.log.warning(
                        "llm: edge 后端配置不完整或缺 openai SDK —— 请求会失败并退到规则兜底"
                    )

        await self._guarded(_Component("llm_provider", _start))

    # ---- 5) scheduler ----
    async def _start_scheduler(self) -> None:
        async def _start() -> None:
            self.scheduler = Scheduler(state=self.state, bus=self.bus, config=self.config,
                                       # R3: 一次性日程触发后从**这个文件**里删掉它
                                       # （开关默认关，见 config.example.yaml）
                                       config_path=self.config_path_used)
            await self.scheduler.start()
            self.log.info(
                "Scheduler 就绪 (%d 条日程, %d 条命令, 每 %g 分钟检查)",
                len(self.scheduler.events),
                len(self.scheduler.bindings),
                self.scheduler.interval_min,
            )
            if self.scheduler.remove_fired_oneoff:
                self.log.info("scheduler: 一次性日程触发后会自动从配置里移除（"
                              "remove_fired_oneoff=true, 文件 %s）",
                              self.config_path_used or "(未指定路径)")
            for warning in self.scheduler.warnings:
                self.log.warning("scheduler: %s", warning)

        async def _stop() -> None:
            if self.scheduler is not None:
                await self.scheduler.stop()

        await self._guarded(_Component("scheduler", _start, _stop))

    # ---- 6) ipc ----
    async def _start_ipc(self) -> None:
        """启动 IPC。agent/ipc/ 里没有接入点就跳过 —— 不假装提供, 也不因此失败。"""
        try:
            module = importlib.import_module("agent.ipc")
        except ImportError:
            self.log.warning(
                "ipc: agent/ipc/ 缺失, 跳过 (GUI 暂时连不上; 不影响其余功能)"
            )
            return

        factory = getattr(module, "build_ipc", None)
        if factory is None:
            self.log.warning(
                "ipc: agent/ipc/ 里没有 build_ipc(), 跳过 —— "
                "GUI 暂时连不上, 不影响其余功能"
            )
            return

        async def _start() -> None:
            self.ipc = _call_ipc_factory(factory, self.bus, self.config, self)
            start = getattr(self.ipc, "start", None)
            if start is None:
                raise RuntimeError("ipc 对象没有 start()")
            result = start()
            if asyncio.iscoroutine(result):
                await result

        async def _stop() -> None:
            stop = getattr(self.ipc, "stop", None)
            if stop is not None:
                result = stop()
                if asyncio.iscoroutine(result):
                    await result

        await self._guarded(_Component("ipc", _start, _stop), fatal=False)

    # ---- 7) 终端输入 (可选) ----
    async def _start_terminal_input(self) -> None:
        """把 stdin 接成第三个输入源。

        没有 GUI/IPC 时这是唯一能"手打字"的通道, 也是冒烟测试的入口。
        config: terminal.enabled (默认 true, 因为服务型进程常常没有 stdin;
        读不到就禁用, 不当成错误)。
        """
        if not self._want_terminal:
            return
        if not self._cfg("terminal", "enabled", default=True):
            self.log.info("terminal: 已在配置里禁用")
            return
        if not sys.stdin or not sys.stdin.isatty():
            self.log.info("terminal: 没有可交互的 stdin, 跳过 (systemd 下属正常)")
            return

        async def _start() -> None:
            self._terminal_task = asyncio.create_task(
                self._read_stdin(), name="terminal-input"
            )
            self.log.info("terminal 输入就绪 (直接敲字回车; Ctrl-D 结束)")

        async def _stop() -> None:
            task, self._terminal_task = self._terminal_task, None
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        await self._guarded(_Component("terminal_input", _start, _stop), fatal=False)

    async def _read_stdin(self) -> None:
        """用 add_reader 把 stdin 变成 awaitable, 不阻塞事件循环。

        (直接 run_in_executor(sys.stdin.readline) 会占住一个线程, 而且退出时
         那个阻塞的 readline 没法取消 —— add_reader 没有这个问题。)
        """
        loop = asyncio.get_running_loop()
        fd = sys.stdin.fileno()
        queue: asyncio.Queue = asyncio.Queue()

        def _on_readable() -> None:
            try:
                line = sys.stdin.readline()
            except Exception as exc:  # noqa: BLE001
                queue.put_nowait(exc)
                return
            queue.put_nowait(line)

        loop.add_reader(fd, _on_readable)
        try:
            while True:
                line = await queue.get()
                if isinstance(line, BaseException):
                    raise line
                if not line:      # EOF (Ctrl-D)
                    self.log.info("terminal: 输入结束 (EOF)")
                    return
                text = line.strip()
                if text:
                    await self.bus.push("terminal", text)
        finally:
            loop.remove_reader(fd)

    # ------------------------------------------------------------ 主循环 ---
    async def serve(self) -> None:
        """主循环: 从 bus 消费事件 → LLM 处理 → (工具由 LLMRouter 执行) → 回话。

        @note 终端命令由 Scheduler 自己旁观: 它用 subscribe() 看 bus 上的事件,
              不走这个循环。⚠ 但订阅**不消费**事件 —— 所以命中命令的那行文本
              仍然会到这里被送去 LLM (见 scheduler.listen_commands 的 @note)。
        """
        if self.bus is None:
            raise RuntimeError("bus 未就绪, 不能进入主循环")

        self.log.info("进入主循环, 等待输入 (Ctrl-C 退出)")

        while not self.stop_event.is_set():
            get_task = asyncio.create_task(self.bus.get())
            stop_task = asyncio.create_task(self.stop_event.wait())
            try:
                done, pending = await asyncio.wait(
                    {get_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                for task in (get_task, stop_task):
                    if not task.done():
                        task.cancel()
                # 让取消落地, 免得 "Task was destroyed but it is pending"
                await asyncio.gather(get_task, stop_task, return_exceptions=True)

            if stop_task in done:
                self.log.info("收到停止信号, 退出主循环")
                return
            if get_task not in done:
                continue

            try:
                event = get_task.result()
            except asyncio.CancelledError:
                return

            await self.handle_event(event)

            if self.max_events is not None and self.stats["events"] >= self.max_events:
                self.log.info("已达 max_events=%d, 主动退出主循环", self.max_events)
                return

    def wallpaper_snapshot(self) -> Dict[str, Any]:
        """当前壁纸的现状 —— **只读**（不动游标、不推送）。

        @return {"index", "path", "total"}; 没选过 / 目录用不了 -> {}（"不知道"就是不知道）
        @note T8-5c: 给"现在是什么壁纸"这类**只读问句直连**用（不走模型）。
        """
        if self.wallpaper is None:
            return {}
        snapshot = self.wallpaper.snapshot()
        if not snapshot:
            return {}
        index, path, total = snapshot
        return {"index": index, "path": path, "total": total}

    def read_services(self) -> Dict[str, Any]:
        """**只读问句直连**要用的入口（T8-5c）。

        @return 缺哪个键, 对应那类问句就**交回模型**（见 `agent/core/read_intents.py`）
        @note 这些入口与工具用的是**同一份数据**, 所以直连答的数字与工具回话一定一致。
        """
        return {
            "music_state": self.music_state if self.music else None,
            "music_list": self.music_candidates if self.music else None,
            "wallpaper_tags": self.wallpaper_tags,
            "wallpaper_state": self.wallpaper_snapshot,
        }

    async def _deliver_reply(self, reply: str) -> str:
        """把一条回复算进统计、记日志、交给 IPC 钩子（两条路共用: 直连 / 模型）。"""
        self.stats["replies"] += 1
        self.log.info("回复: %s", reply)

        # Phase 6 D4: 把回复交给 IPC (推 llm{text} 给 GUI)。
        # 钩子是"没人接就什么都不做" —— 没有 IPC / 没注册时行为与以前一致。
        # 钩子本身出错不能影响这条回复已经算成功这件事, 所以只记 warning。
        if reply and self.on_reply is not None:
            try:
                hooked = self.on_reply(reply)
                if inspect.isawaitable(hooked):
                    await hooked
            except Exception as exc:        # noqa: BLE001
                self.log.warning("回复钩子出错 (已忽略): %r", exc)
                self.log.debug("回复钩子 traceback", exc_info=True)
        return reply

    async def handle_event(self, event: Dict[str, Any]) -> Optional[str]:
        """处理一条 bus 事件并返回回复 (没回复则 None)。

        单独抽出来是为了能直接测"一条消息进来会怎样", 不用起整个进程。

        ⚠ T8-5c: **只读问句先走直连**（`agent/core/read_intents.py`）——
          "现在在放什么 / 库里有什么歌 / 壁纸有哪些标签 / 哪张壁纸用得最少"这类问题的
          答案只存在于设备状态里, 板端实测 0.6B 既不肯调 `status` 也编不出来,
          所以直接用真实数据拼一句话回, **一次模型推理都不花**。写意图照旧进模型。
        """
        source = event.get("source", "?")
        text = event.get("text", "")
        self.stats["events"] += 1
        self.log.info("[%s] %s", source, text)

        direct = read_intents.answer(text, self.read_services())
        if direct is not None:
            self.log.info("只读问句直连 (%s, 没走模型)", direct.intent)
            return await self._deliver_reply(direct.text)

        if self.llm is None:
            self.log.error("llm 未就绪, 无法处理这条输入")
            self.stats["llm_errors"] += 1
            return None

        context = {
            "state": self.state.current().value if self.state else "unknown",
            "source": source,
            "connected": self.state.is_connected() if self.state else False,
        }

        try:
            result = await self.llm.chat_with_tools(text, context)
        except Exception as exc:  # noqa: BLE001 - 单条消息失败不该停掉服务
            self.log.error("处理输入失败: %r", exc, exc_info=True)
            self.stats["llm_errors"] += 1
            return None

        for call in result.get("tool_calls") or []:
            self.stats["tool_calls"] += 1
            self.log.info(
                "工具 %s(%s) -> %s",
                call.get("name"),
                call.get("args"),
                "ok" if (call.get("result") or {}).get("ok") else "fail",
            )

        if not result.get("ok"):
            self.log.error("LLM 返回失败 (mode=%s): %s", result.get("mode"), result.get("error"))
            self.stats["llm_errors"] += 1
            return None

        # edge 后端挂了/没给出正文时 provider 会退回规则兜底: 答复照给, 但必须**说清**
        # 这不是模型答的 (也算一次模型失败)。
        if result.get("degraded"):
            self.log.warning("LLM 降级为规则兜底: %s", result["degraded"])
            self.stats["llm_errors"] += 1

        reply = result.get("text") or ""
        return await self._deliver_reply(reply)

    # ------------------------------------------------------------ 停止 ---
    async def stop(self) -> None:
        """严格按启动的**反向**顺序停止组件。"""
        self.stop_event.set()

        stopped = 0
        for component in reversed(self._components):
            if not component.started:
                continue
            try:
                await component.do_stop()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 收尾不能因为一个组件失败而中断
                self.log.error("停止组件 %s 失败: %r", component.name, exc, exc_info=True)
            else:
                stopped += 1
            finally:
                component.started = False

        uptime = 0.0 if self._started_at is None else time.monotonic() - self._started_at
        self.log.info(
            "=== agent 停止 === 已停 %d 个组件, 运行 %.1fs, "
            "处理 %d 条输入 / %d 条回复 / %d 次工具调用 / %d 次失败",
            stopped, uptime,
            self.stats["events"], self.stats["replies"],
            self.stats["tool_calls"], self.stats["llm_errors"],
        )
        if self.failures:
            self.log.warning("启动期失败组件: %s", self.failures)

    # ------------------------------------------------------------ 内部 ---
    async def _guarded(self, component: _Component, fatal: bool = False) -> bool:
        """启动一个组件; 失败只记 warning 并继续 (fatal=True 时记 error)。

        注册进 _components **总是**发生, 这样停止时能按相反顺序把已经起来的
        那些收干净。
        """
        self._components.append(component)
        try:
            await component.do_start()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            component.start_error = exc
            # 一行说明放 WARNING/ERROR (fatal 才是 ERROR), traceback 放 DEBUG。
            # 这样正常日志读起来是"某组件没起来, 其余照常", 而不是一坨栈。
            self.log.log(
                logging.ERROR if fatal else logging.WARNING,
                "组件 %s 启动失败, 继续启动其余组件: %r",
                component.name, exc,
            )
            self.log.debug("组件 %s traceback", component.name, exc_info=True)
            self.failures.append((component.name, "%s: %s" % (type(exc).__name__, exc)))
            return False
        else:
            component.started = True
            self.log.debug("组件 %s 已启动", component.name)
            return True

    def _install_signal_handlers(self) -> None:
        """SIGINT / SIGTERM -> stop_event (systemd stop 发的是 SIGTERM)。"""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._on_signal, sig)
            except (NotImplementedError, RuntimeError, ValueError):
                # 非 POSIX 主线程等情况下不可用; 退化成 KeyboardInterrupt
                self.log.debug("无法为 %s 注册信号处理器, 沿用默认行为", sig)

    def _on_signal(self, sig: int) -> None:
        self.log.info("收到信号 %s, 准备退出", signal.Signals(sig).name)
        self.stop_event.set()


# ---------------------------------------------------------------------------
#  进程入口
# ---------------------------------------------------------------------------
async def run(
    config: Optional[Dict[str, Any]] = None,
    log: Optional[logging.Logger] = None,
    max_events: Optional[int] = None,
    run_seconds: Optional[float] = None,
    start_terminal: bool = True,
    start_native: bool = True,
    stop_event: Optional[asyncio.Event] = None,
) -> int:
    """起一个 Runtime 跑到底, 返回进程退出码。

    @param max_events  处理 N 条输入后退出 (冒烟测试)
    @param run_seconds 跑 N 秒后退出 (冒烟测试; 与 max_events 可叠加, 谁先到算谁)
    @return 0 正常结束, 1 连配置都读不到
    """
    logger = log or logging.getLogger("%s.main" % LOGGER_NAME)

    # ---- 1) 加载 config ----
    config_path_used: Optional[Path] = None
    if config is None:
        try:
            config_path_used = config_path("config")
        except ConfigNotFoundError as exc:
            logger.error("找不到配置: %r", exc)
            return 1
        try:
            config = load_config("config")
        except ConfigError as exc:
            # 配置写错了不该带着半截状态往下跑
            logger.error("配置不合法: %r", exc)
            return 1
        logger.info("配置已加载: %s", config_path_used)

    runtime = Runtime(
        config=config,
        config_path_used=config_path_used,
        log=logger,
        stop_event=stop_event,
        max_events=max_events,
        start_terminal=start_terminal,
        start_native=start_native,
    )

    await runtime.start()

    # ---- 定时退出看门狗 (冒烟测试用) ----
    watchdog: Optional[asyncio.Task] = None
    if run_seconds is not None and run_seconds > 0:
        async def _watchdog() -> None:
            await asyncio.sleep(run_seconds)
            logger.info("已达 run_seconds=%.1f, 请求退出", run_seconds)
            runtime.stop_event.set()

        watchdog = asyncio.create_task(_watchdog(), name="run-seconds-watchdog")
        logger.info("将运行 %.1fs 后自动退出", run_seconds)

    try:
        await runtime.serve()
    except asyncio.CancelledError:
        logger.info("主循环被取消")
    finally:
        if watchdog is not None:
            watchdog.cancel()
            try:
                await watchdog
            except asyncio.CancelledError:
                pass
        await runtime.stop()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(
        prog="agent", description="RK3568 桌面助手 (板端主进程)"
    )
    parser.add_argument("--config", help="配置文件路径 (默认 config/config.yaml)")
    parser.add_argument("--log", default=None, help="日志文件路径 (默认 logs/agent.log)")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="不连串流也不读 stdin, 只验证组件能起来然后退出",
    )
    parser.add_argument(
        "--max-events", type=int, default=None,
        help="处理 N 条输入后退出 (冒烟测试用)",
    )
    parser.add_argument(
        "--check-config", action="store_true",
        help="只校验配置能读能解, 然后退出",
    )
    args = parser.parse_args(argv)

    # 环境变量兜底 (方便 systemd unit / 冒烟脚本)
    max_events = args.max_events
    if max_events is None and os.environ.get(_ENV_MAX_EVENTS):
        try:
            max_events = int(os.environ[_ENV_MAX_EVENTS])
        except ValueError:
            print("AGENT_MAX_EVENTS 不是整数, 忽略", file=sys.stderr)

    log = setup_logging(args.log)
    _silence_noisy_libraries()

    if args.config:
        os.environ.setdefault("AGENT_CONFIG_DIR", str(Path(args.config).resolve().parent))

    if args.check_config:
        try:
            load_config("config")
        except (ConfigError, ConfigNotFoundError) as exc:
            log.error("配置检查失败: %r", exc)
            return 1
        log.info("配置检查通过")
        return 0

    # "跑 N 秒就退" 给冒烟测试/CI 用: 能验证"起得来、不死、能干净退出"
    run_seconds: Optional[float] = None
    raw_seconds = os.environ.get(_ENV_RUN_SECONDS)
    if raw_seconds:
        try:
            run_seconds = float(raw_seconds)
        except ValueError:
            log.warning("%s 不是数字 (%r), 忽略", _ENV_RUN_SECONDS, raw_seconds)

    # dry-run: 不连串流、不读 stdin, 只验证组件装配
    start_native = not args.dry_run
    start_terminal = not args.dry_run

    try:
        return asyncio.run(
            run(
                log=log,
                max_events=max_events,
                run_seconds=run_seconds,
                start_terminal=start_terminal,
                start_native=start_native,
            )
        )
    except KeyboardInterrupt:
        log.info("被 Ctrl-C 中断")
        return 0


if __name__ == "__main__":
    sys.exit(main())
