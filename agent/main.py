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
from agent.core.chat_memory import ChatMemory
from agent.core.user_profile import (
    DEFAULT_RETRY_S as DEFAULT_PROFILE_RETRY_S,
    DEFAULT_TRIGGER_CHARS,
    DEFAULT_TRIGGER_TURNS,
    ProfileError,
)
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

        #: 纯对话记忆（T9-1）: 用户跟助手说过的那几句话（**只在这块内存里, 不落盘**）。
        #: 记的是"谁说的/说了什么/什么时候/当时哪个模式" —— T9-2 判心情的语料与场景都在里面。
        #: ⚠ 它不是模型的上下文（板端 LLM 每轮无状态, 见 agent/core/chat_memory.py 模块头）。
        self.chat_memory = ChatMemory()

        #: 用户画像（T9-2 内核 + T9-3 触发）: 纯对话攒到触发线就**由 Agent 自己**构建一次。
        #: ⚠ **不是工具**: 不进 `TOOL_MODULES`、模型看不到也调不到; 触发只有"攒够字数"一条路。
        self.profile_file: str = ""
        self._profile_enabled = False
        self._profile_trigger_chars = DEFAULT_TRIGGER_CHARS
        self._profile_trigger_turns = DEFAULT_TRIGGER_TURNS
        self._profile_task: Optional[Any] = None      # 后台构建任务（一次只跑一个）
        self._profile_failed_at = 0.0                 # 上次失败的时间（退避用）

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
        #: 自动补歌（T10-4）的设置（`music.autofill`；`None` 只是没读配置时的占位）
        self._autofill: Dict[str, Any] = {}
        #: music 推送钩子（与 on_wallpaper 同款"有就接"）—— IPC 层把它接到 topic `music`
        self.on_music: Optional[Callable[[Dict[str, Any]], Any]] = None
        self._music_task: Optional[asyncio.Task] = None
        #: 上次推过的 (title, playing)，用来决定"这次要不要推"（进度每次都推）
        self._last_music_push: Tuple[str, bool] = ("", False)

        #: B 站视频（T11-6）: 队列 + 游戏观察器 + 缓冲代理。
        #: **None = 没开**（`bilibili.enabled=false`）—— 与音乐同一条口径。
        #: ⚠ 队列内容只有两个来源: **对话关键词**（工具 `bilibili_search`）与
        #:   **画面认出的游戏**（`_game_watch` 在 GAME 模式里的循环）; 播放只由 **GUI 操作**触发
        #:   （你定的"等 GUI 操作才开始播放, 不提前缓存"）。
        self.bilibili: Optional[Any] = None
        self._bilibili_api: Optional[Any] = None
        self._bilibili_anchors: Optional[Any] = None
        self._game_watch: Optional[Any] = None
        self._buffer: Optional[Any] = None            # 当前那条的缓冲代理（换条就换一个）
        self._bilibili_cfg: Dict[str, Any] = {}
        self._bilibili_task: Optional[asyncio.Task] = None
        self._last_bilibili_game: str = ""             # 上次认出并搜过的游戏（只在**变了**时重搜）
        self._bilibili_stream: str = ""                # 现在这一条的本地 FIFO 路径
        self._bilibili_quality: str = ""               # 这一条的清晰度（给人看的名字）
        #: bilibili 推送钩子（与 on_music 同款"有就接"）—— IPC 层把它接到 topic `bilibili`
        self.on_bilibili: Optional[Callable[[Dict[str, Any]], Any]] = None

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
        # ⚠ B 站也在工具**之前**: `bilibili_search` 那条入口要建工具时就位
        #   （没开就整个不装 —— 与音乐同一条口径）。
        "_start_bilibili",
        "_start_state_and_tools",
        "_start_llm_service",
        "_start_llm",
        # ⚠ 画像在 LLM 之后: 判心情那一步要问模型（构建是后台任务, 步骤本身只是读配置）
        "_start_profile",
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
                    # B 站（T11-5/T11-6）: 工具只会"把关键词交给队列"这一件事。
                    # ⚠ **没开就是 None** —— 那个工具会自己跳过（与音乐同款）。
                    "bilibili_search": self.bilibili_search if self.bilibili else None,
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
                       sort: Optional[str] = None,
                       stage: bool = False) -> Dict[str, Any]:
        """换一张壁纸并把结果推给 GUI（T3 起；T7-3 按标签/IP 挑；T8-6 按用量挑；T10-3 走三格窗口）。

        入口只有一条: **对话**里 LLM 调 `next_wallpaper` 工具（手动按钮与
        `next_wallpaper` IPC 命令在 T7-3 按需求**删掉了**, 见 agent/core/wallpaper.py 的模块头）。

        **窗口怎么走（T10-3, 你定的）**::

            prev  ←  current  ←  next            （三格, prev 只留 1 张）
            action=next   往后走: next 变 current, 原来那张进 prev, **再按画像塞一个新的 next**
            action=prev   往回走: 对调; prev 空着就退回"按文件名往前翻一张"
            action=pick   替换 next（按 match/sort）-> **推进一格** -> 再塞一个新的 next
            action=stage  只把一张放进 next（**不切屏**）: 不带 match = 按画像重挑一张
                                             带 match = 把指定条件里最好的那张放进去

        @param step  正数往后、负数往前、0 = 重推当前这张（老口径, 现在映射到窗口的走法）
        @param match 挑图条件（None = 按画像挑, 画像不可用就按"用得最少的"）:
                     `"scene=anime"` 某轴某标签 / `"anime"` 只写标签名 / `"ip=EVA"` 锚点检索。
        @param sort  T8-6: `"used_asc"` 用得最少的在前 / `"used_desc"` 用得最多的在前 / None = 不动。
        @param stage True = **只更新"下一个"**, 不切屏、不推送、不计使用次数。
        @return {"ok", "path", "index", "total", "pushed", "used", "window", "why", …}
                     `used` = **记完这一次之后**的总次数; `staged=True` 时 `pushed` 恒为 False。
        @note 目录不存在/没图片属于运行期问题: 这里报错, 不让工具在启动时消失。
              标签数据还没打（或 match 写错）同样只是**这次失败**, 不抛异常。
        @note **状态权限不在这里判**: 那张表挂在工具上（`allowed_states`）, 由
              `ToolRouter.execute()` 统一拦。本文件因此仍然**不认识任何具体工具**。
        @note 这个方法是**同步**的: 推送入口（IPC 的 dispatcher）本身就是同步且线程安全的,
              而工具 handler 可能在线程池里跑（ToolRouter 对同步 handler 就是这么做的）。
        """
        if self.wallpaper is None:
            return self._wallpaper_failure("壁纸还没初始化（Agent 的 tools 步骤没起来？）")

        # ---- ① stage: 只改"下一个", 不切屏（你定的第 3 条①③）----
        if stage:
            chosen = self._choose_next(match=match, sort=sort, avoid_next=True)
            if not chosen.get("ok"):
                return self._wallpaper_failure(chosen.get("error") or "挑不出下一张")
            self.wallpaper.set_next(chosen["path"])
            self.log.info("wallpaper: 预备下一张 = %s（%s）", os.path.basename(chosen["path"]),
                          chosen.get("why_text") or "")
            out: Dict[str, Any] = {"ok": True, "staged": True, "path": chosen["path"],
                                   "pushed": False, "window": self._window_names(),
                                   "why": chosen.get("why") or []}
            if match or sort:
                out["basis"] = chosen.get("basis")
            return out

        # ---- ② 带条件的换图（pick / least / most）: 替换 next -> 推进 -> 再塞一个 ----
        if match or sort:
            chosen = self._choose_next(match=match, sort=sort)
            if not chosen.get("ok"):
                return self._wallpaper_failure(chosen.get("error") or "挑不出下一张")
            self.wallpaper.set_next(chosen["path"])
            try:
                _index, path, _total = self.wallpaper.advance()
            except WallpaperError as exc:
                self.log.warning("wallpaper: 换不了: %s", exc)
                return self._wallpaper_failure(str(exc))
            pushed = self._push_wallpaper(path, chosen.get("rank_index", 0))
            used = self.wallpaper_usage(path)
            self._refill_next()
            self.log.info("wallpaper: %s（按条件 %s%s, 候选 %d 张, 第 %d 名, pushed=%s, used=%d）",
                          path, match or "-", ", sort=%s" % sort if sort else "",
                          len(chosen.get("pool") or ()), chosen.get("rank", 1), pushed, used)
            result: Dict[str, Any] = {
                "ok": True, "path": path, "index": chosen.get("rank_index", 0),
                "total": len(chosen.get("pool") or ()), "pushed": pushed, "used": used,
                "window": self._window_names(), "why": chosen.get("why") or []}
            if match:
                self._last_match_pick = {"spec": match.strip(), "path": path}
                result["match"] = {"spec": match, "kind": chosen.get("kind"),
                                   "note": chosen.get("note"),
                                   "candidates": len(chosen.get("pool") or ()),
                                   "rank": chosen.get("rank", 1)}
                score = (chosen.get("scores") or {}).get(path)
                if score is not None:
                    result["score"] = score
            if sort:
                result["sort"] = sort
            return result

        # ---- ③ 普通"换一张": 往后走窗口 / 往回走 / 重推当前 ----
        try:
            if step > 0:
                if not self.wallpaper.window()["ready"]:
                    chosen = self._choose_next()
                    if chosen.get("ok"):
                        self.wallpaper.set_next(chosen["path"])
                    else:
                        self.log.debug("wallpaper: 画像/用量都挑不出下一张(%s), 按文件名翻",
                                       chosen.get("error"))
                if self.wallpaper.window()["ready"]:
                    index, path, total = self.wallpaper.advance()
                else:
                    index, path, total = self.wallpaper.step(1)
            elif step < 0:
                index, path, total = self.wallpaper.back()
            else:
                index, path, total = self.wallpaper.step(0)
        except WallpaperError as exc:
            self.log.warning("wallpaper: 换不了: %s", exc)
            return self._wallpaper_failure(str(exc))

        pushed = self._push_wallpaper(path, index)
        used = self.wallpaper_usage(path)
        staged = self._refill_next()
        self.log.info("wallpaper: %s (index=%d/%d, step=%d, pushed=%s, used=%d, window=%s)",
                      path, index, total, step, pushed, used, self._window_names())
        out: Dict[str, Any] = {"ok": True, "path": path, "index": index, "total": total,
                               "pushed": pushed, "used": used,
                               "window": self._window_names()}
        if step > 0 and staged:
            out["next"] = staged
        return out

    def _window_names(self) -> Dict[str, Optional[str]]:
        """窗口三格的文件名（日志/工具回话里用短名, 省上下文）。"""
        window = self.wallpaper.window() if self.wallpaper is not None else {}
        return {key: (os.path.basename(value) if value else None)
                for key, value in (("prev", window.get("prev")),
                                   ("current", window.get("current")),
                                   ("next", window.get("next")))}

    def _choose_next(self, match: Optional[str] = None, sort: Optional[str] = None,
                     avoid_next: bool = False) -> Dict[str, Any]:
        """挑"下一个"那张（**不改窗口**）：有条件按条件, 没条件按画像, 画像不行按用量。

        @return {"ok", "path", "why": […], "why_text", "basis": "condition|profile|usage",
                 "pool"?, "rank"?, "rank_index"?, "kind"?, "note"?, "scores"?}
        @note 一定**跳过窗口里的 prev 与 current**（"下一张"不该是屏幕上这张或刚看过的那张）;
              `avoid_next=True` 时连当前那个 next 也跳过（stage = 换掉它）。
        """
        if self.wallpaper is None:
            return {"ok": False, "error": "壁纸还没初始化"}
        try:
            images = self.wallpaper.scan()
        except WallpaperError as exc:
            return {"ok": False, "error": str(exc)}
        if not images:
            return {"ok": False, "error": "壁纸目录里没有图片"}

        window = self.wallpaper.window()
        skip = {path for path in (window.get("prev"), window.get("current")) if path}
        if avoid_next and window.get("next"):
            skip.add(window["next"])
        available = [path for path in images if path not in skip] or list(images)

        if match or sort:
            pool: Optional[list] = None
            picked: Dict[str, Any] = {}
            if match:
                resolved = self._resolve_match(match)
                if not resolved.get("ok"):
                    return resolved          # 已经带 tell_user（别在外面再包一层）
                pool = list(resolved["pool"])
                picked = resolved
            if sort:
                pool, picked = self._sort_pool_by_usage(pool, match, sort)
                if pool is None:
                    return {"ok": False, "error": (picked or {}).get("error") or "排不了序"}
            candidates = [path for path in (pool or []) if path in set(available)]
            if not candidates:
                return {"ok": False,
                        "error": "符合条件的图都在窗口里了（就剩刚看过的那几张）—— 换个条件试试"}
            chosen = candidates[0]
            rank = (pool or []).index(chosen) + 1
            return {"ok": True, "path": chosen, "basis": "condition", "pool": pool,
                    "rank": rank, "rank_index": rank - 1, "kind": picked.get("kind"),
                    "note": picked.get("note"), "scores": picked.get("scores") or {},
                    "why": ["按条件挑：%s" % (match or sort or "")],
                    "why_text": "%s -> 第 %d 名" % (match or sort or "", rank)}

        ranked = self._rank_next_by_profile(available)
        if ranked:
            top = ranked[0]
            return {"ok": True, "path": top["path"], "basis": "profile",
                    "why": top.get("why") or [], "why_text": "；".join(top.get("why") or [])}
        # ⚠ 画像不可用时**回退到老行为**（按文件名顺序往后）, 不是"用得最少的" ——
        #   用户说"下一张"时, 可预期的顺序比一个悄悄换掉的排序规则重要;
        #   "挑用得最少的"仍然是显式动作（sort="used_asc"）。
        #   （available 已经按文件名排好、并去掉了 prev/current, 所以第一个就是"往后下一张"。）
        if available:
            return {"ok": True, "path": available[0], "basis": "order",
                    "why": ["画像还没有可用的偏好 —— 按文件名往后拿（老行为）"],
                    "why_text": "回退: 按文件名"}
        return {"ok": False, "error": "挑不出下一张（标签数据也用不上）"}

    def _rank_next_by_profile(self, candidates: Sequence[str]) -> List[Dict[str, Any]]:
        """按用户画像排一遍候选（**画像不可用就返回空** —— 让调用方回退）。

        @note 相似度是**名次折算**的（`1 - rank/N`）: 锚点检索本来就给全库排序,
                  再逐张算余弦等于把同一件事算两遍; 名次对排序是等价的。
        """
        from agent.core import user_profile

        if not self.profile_file:
            return []
        try:
            record = user_profile.latest_record(self.profile_file)
        except ProfileError as exc:
            self.log.warning("wallpaper: 画像读不了（当成没有）: %s", exc)
            return []
        if not user_profile.profile_basis(record).get("wallpaper"):
            return []
        try:
            index = self.tag_index()
        except Exception as exc:                     # noqa: BLE001
            self.log.warning("wallpaper: 标签索引用不了（当成没有）: %r", exc)
            return []
        presets = self._cfg("wallpaper", "tagging", "ip_presets", default={}) or {}
        pools: Dict[str, List[str]] = {}

        def similarity(name: str, path: str) -> float:
            if name not in pools:
                try:
                    found = index.match("ip=%s" % name, presets=presets,
                                        wallpaper_dir=self._wallpaper_dir())
                    pools[name] = [str(item) for item in (found.pool if found.ok else [])]
                except Exception:                    # noqa: BLE001
                    pools[name] = []
            pool = pools[name]
            if not pool or path not in pool:
                return 0.0
            return 1.0 - pool.index(path) / float(len(pool))

        def mood_of(path: str) -> List[str]:
            tags = index.tags_of(path) or {}
            return [str(pair[0]) for pair in (tags.get("mood") or []) if pair]

        return user_profile.rank_wallpapers(
            record, candidates, ip_similarity=similarity,
            usage=lambda path: int(index.usage_of(path).get("used") or 0),
            mood_of=mood_of)

    def _refill_next(self) -> Optional[str]:
        """推进/换图之后**再塞一个 next**（T10-3；失败只记 debug, 下次推进时还会再试）。"""
        if self.wallpaper is None:
            return None
        if self.wallpaper.window()["ready"]:
            return self.wallpaper.window()["next"]
        chosen = self._choose_next()
        if not chosen.get("ok"):
            self.log.debug("wallpaper: 没准备出下一张 (%s)", chosen.get("error"))
            return None
        self.wallpaper.set_next(chosen["path"])
        self.log.info("wallpaper: 下一个 = %s（%s）", os.path.basename(chosen["path"]),
                      chosen.get("why_text") or "")
        return chosen["path"]

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
            # T10-4: 自动补歌（先吃本地库, 不够**按歌手**去 PC 搜）—— 目标长度 30（可配）
            autofill = self._cfg("music", "autofill", default={}) or {}
            self._autofill = {
                "enabled": bool(autofill.get("enabled", True)),
                "target": int(autofill.get("target", 30) or 30),
                "by_artist": bool(autofill.get("by_artist", True)),
                "search_per_cycle": int(autofill.get("search_per_cycle", 3) or 3),
                "search_limit": int(autofill.get("search_limit", 10) or 10),
                "search_backoff_s": float(autofill.get("search_backoff_s", 300) or 300),
            }
            target = self._autofill["target"] if self._autofill["enabled"] else 0
            self.music.set_target(target)
            self.log.info("music: 就绪 (PC=%s@%s, 本地库=%s, 自动补歌=%s)",
                          getattr(cli, "user", "?"), getattr(cli, "host", "?"), library_file,
                          "目标 %d 首" % target if target else "关")
            self._music_task = asyncio.ensure_future(self._music_loop())

        async def _stop() -> None:
            if self._music_task is not None:
                self._music_task.cancel()
                self._music_task = None

        await self._guarded(_Component("music", _start, _stop), fatal=False)

    # ---- 3.7) B 站视频 (T11-6) ----
    async def _start_bilibili(self) -> None:
        """按配置接上 B 站视频: 网络层 + 队列 + 锚点库 + 游戏观察器 + GAME 里的观察循环。

        ⚠ **工具只有一条入口**（`bilibili_search`: 把对话关键词交给队列）——
          队列的另一个来源是**画面认出的游戏**（本方法起的循环）, 播放**只由 GUI 操作**触发。
        @note 关掉时什么都不做（`bilibili.enabled=false`）—— 那个工具也会自己跳过。
        """
        from agent.core.bilibili import BilibiliQueue
        from agent.core.game_anchors import GameAnchors
        from agent.core.game_watch import GameWatcher
        from agent.net.bilibili_api import BilibiliApi

        async def _start() -> None:
            section = self._cfg("bilibili", default={}) or {}
            api = BilibiliApi.from_config(section, log=self.log)
            if api is None:
                self.log.info("bilibili: 没开（config 的 bilibili.enabled）—— 工具不装")
                return
            self._bilibili_cfg = dict(section)
            queue_cfg = section.get("queue") or {}
            viewport = int(queue_cfg.get("viewport_fallback", 6) or 6)
            self.bilibili = BilibiliQueue(
                api, viewport=viewport, queue_max=int(queue_cfg.get("max", 60) or 60),
                log=self.log)
            self._bilibili_api = api
            watch = section.get("game_watch") or {}
            self._bilibili_anchors = GameAnchors(watch.get("anchor_file"), log=self.log)
            try:
                self._bilibili_anchors.load()
            except Exception as exc:                      # noqa: BLE001 - 锚点坏了不该拦住看视频
                self.log.warning("bilibili: 锚点库读不了（当成空的）: %s", exc)
            probe = None
            if bool(watch.get("enabled", True)):
                from agent.net.pc_probe import PcProbe

                probe = PcProbe.from_config(watch, music=self._cfg("music", default={}),
                                            log=self.log)
            buffer_cfg = section.get("buffer") or {}
            self._game_watch = GameWatcher(
                self._bilibili_anchors, probe=probe,
                interval_s=float(watch.get("interval_s", 60) or 60),
                confident_score=float(watch.get("confident_score", 0.82) or 0.82),
                confident_margin=float(watch.get("confident_margin", 0.05) or 0.05),
                mem_watermark_mb=float(buffer_cfg.get("mem_watermark_mb", 400) or 400),
                log=self.log)
            self._bilibili_task = asyncio.ensure_future(self._game_watch_loop())
            # ⚠ 起播前先问一次 cookie 有效性（失效的话第一句提示就要说实话, 见方法注释）
            self._check_bilibili_cookie()
            self.log.info(
                "bilibili: 就绪（预览栏兜底 %d 格 -> 目标 %d 条; 观察器 %s, 锚点 %d 个/%s）",
                viewport, self.bilibili.target(),
                "开" if probe is not None else "关（只看画面）",
                len(self._bilibili_anchors),
                "、".join(self._bilibili_anchors.games()[:4]) or "还没有")

        async def _stop() -> None:
            task, self._bilibili_task = self._bilibili_task, None
            if task is not None:
                task.cancel()
            self._stop_buffer()
            if self._game_watch is not None:
                self._game_watch.unload()

        await self._guarded(_Component("bilibili", _start, _stop), fatal=False)

    # ---- 3.8) 用户画像 (T9-3) ----
    async def _start_profile(self) -> None:
        """按配置接上"用户画像"（T9-2 的内核 + T9-3 的触发）。

        ⚠ **它不是工具**: 不进 `TOOL_MODULES`、模型看不到也调不到; 触发只有一条路 ——
          `_profile_tick()` 发现"纯对话攒到触发线"就后台构建一次（你 T9 定的）。
        @note 关掉时什么都不做（`profile.enabled=false`）。
        """
        from agent.core import user_profile

        async def _start() -> None:
            self._profile_enabled = bool(self._cfg("profile", "enabled", default=True))
            self.profile_file = user_profile.resolve_profile_file(self._cfg("profile", "file"))
            self._profile_trigger_chars = max(1, int(
                self._cfg("profile", "trigger_chars", default=DEFAULT_TRIGGER_CHARS)
                or DEFAULT_TRIGGER_CHARS))
            self._profile_trigger_turns = max(1, int(
                self._cfg("profile", "trigger_turns", default=DEFAULT_TRIGGER_TURNS)
                or DEFAULT_TRIGGER_TURNS))
            self.log.info("用户画像: %s (纯对话攒到 %d 字触发一遍; 文件 %s)",
                          "开" if self._profile_enabled else "关",
                          self._profile_trigger_chars, self.profile_file)

        async def _stop() -> None:
            task = self._profile_task
            self._profile_task = None
            if task is not None and not task.done():
                task.cancel()

        await self._guarded(_Component("profile", _start, _stop), fatal=False)

    def _profile_tick(self) -> None:
        """每轮答完检查一次: **纯对话攒够了**就起一个后台构建（只有 Agent 会触发）。

        @note 三条"不做": 没开不做 / 已有构建在跑不做 / 刚失败过（60 秒内）不做。
        @note 构建本身在后台任务里（要问一次模型, 板端可能几十秒）—— 不堵住对话。
        """
        if not self._profile_enabled or self._profile_task is not None:
            return
        chars, turns = self.chat_memory.pending_chars(), self.chat_memory.pending_turns()
        hit_chars = chars >= self._profile_trigger_chars
        hit_turns = turns >= self._profile_trigger_turns
        if not (hit_chars or hit_turns):
            return
        if self._profile_failed_at and (time.time() - self._profile_failed_at) < DEFAULT_PROFILE_RETRY_S:
            return
        trigger = {"reason": "chat_chars" if hit_chars else "chat_turns",
                   "chars": chars, "turns": turns,
                   "threshold": {"chars": self._profile_trigger_chars,
                                 "turns": self._profile_trigger_turns}}
        self.log.info("用户画像: 自上次构建以来攒了 %d 字 / %d 轮 -> 构建一次", chars, turns)
        self._profile_task = asyncio.ensure_future(self._build_profile_task(trigger))

    async def _build_profile_task(self, trigger: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """后台构建一次画像: 读本地数据 + 问一次模型（判心情）-> 落盘 -> 结算纯对话。

        @note 失败**不结算**（下次再试）也不抛: 画像失败绝不能影响对话。
        @note "结算" = `ChatMemory.settle()` 只留最新几条（新来的那几句自动留着, 因为
              它留的是**最新的** N 条）。
        """
        from agent.core import user_profile

        started = time.time()
        try:
            history = user_profile.read_records(self.profile_file)
            previous = history[-1] if history else None      # 上一条（判"心情变了"用）
            record = await user_profile.build_profile(
                self.chat_memory, config=self.config, llm=self.llm, trigger=trigger,
                clock=time.strftime("%H:%M"), history=history)
            user_profile.append_record(self.profile_file, record)
            self._profile_failed_at = 0.0
            dropped = self.chat_memory.settle()
            # T10-3: 画像变了 -> 立刻按新画像重挑壁纸的"下一个"（你定的第 3 条②）
            try:
                self._refill_next()
            except Exception as exc:        # noqa: BLE001 - 挑图失败不该让画像构建失败
                self.log.warning("用户画像: 重挑壁纸的\"下一个\"失败 (已忽略): %r", exc)
            # T10-5: 负反馈 -> 从播放队列里去掉类似的全部; 只有**心情变化**才重置队列
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, self._apply_profile_effects, record, previous)
            except Exception as exc:        # noqa: BLE001 - 队列动作失败不该让画像构建失败
                self.log.warning("用户画像: 队列动作失败 (已忽略): %r", exc)
            walls = record["walls"]["ip"][:2]
            artists = record["music"]["artist"][:2]
            self.log.info(
                "用户画像: 构建完成 (%.1fs) 心情=%s/%s 置信=%s; IP=%s; 歌手=%s; "
                "清零 图%d/歌%d; 结算掉 %d 条",
                time.time() - started, record["mood"]["label"], record["mood"]["zh"],
                record["mood"]["confidence"],
                "、".join("%s %.2f" % (row["name"], row["weight"]) for row in walls) or "-",
                "、".join("%s %.2f" % (row["name"], row["weight"]) for row in artists) or "-",
                record["cleared"]["wall_images"], record["cleared"]["tracks"], dropped)
            return record
        except Exception as exc:        # noqa: BLE001 - 画像失败只记 warning
            self._profile_failed_at = time.time()
            self.log.warning("用户画像: 构建失败 (已忽略, %.0f 秒后再试): %r",
                             DEFAULT_PROFILE_RETRY_S, exc)
            return None
        finally:
            self._profile_task = None

    def _apply_profile_effects(self, record: Dict[str, Any],
                               previous: Optional[Mapping[str, Any]] = None) -> None:
        """画像构建完的**队列动作**（T10-5, 同步, 由调用方丢线程池）:

        1. 负反馈点名了歌/歌手 -> 把**类似的全部**从播放队列里去掉; 正在放的那首被去掉时
           跳下一首（用户说"不想听"却继续放它, 就是骗人）;
        2. **只有心情变了才重置队列**（你定的第 6 条）: 保留正在放的那首, 其余清掉重补。
        @note 两个动作之后都会**立刻补队列**（不等下一个轮询周期）。
        """
        if self.music is None:
            return
        ids = [str(item) for item in (record.get("cleared") or {}).get("track_ids") or ()]
        removed = self._remove_from_playback(ids) if ids else None
        if (removed or {}).get("removed"):
            self._music_autofill()
        self._reset_queue_on_mood_change(record, previous)

    def _remove_from_playback(self, track_ids: Sequence[str]) -> Optional[Dict[str, Any]]:
        """从播放队列里去掉这些歌（T10-5 第 5 条）。@return `music.remove()` 的结果。"""
        if self.music is None or not track_ids:
            return None
        try:
            result = self.music.remove(track_ids)
        except MusicError as exc:
            self.log.warning("music: 从队列里去掉失败 (已忽略): %s", exc)
            return None
        if not result["removed"]:
            self.log.info("music: 负反馈点名了 %d 首, 但队列里没有它们", len(track_ids))
            return result
        self._push_music(self.music.snapshot())
        if result["current_removed"]:
            try:
                self.music.step(1)                 # 跳到被去掉那首后面那首（环形）
            except MusicError as exc:
                self.log.warning("music: 想跳下一首但失败了: %s", exc)
            self._push_music(self.music.snapshot())
        self.log.info("music: 不想听的 %d 首已从队列里去掉（正在放的那首%s被去掉）",
                      len(result["removed"]), "" if result["current_removed"] else "没")
        return result

    def _reset_queue_on_mood_change(self, record: Dict[str, Any],
                                    previous: Optional[Mapping[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """**只有心情变了才重置队列**（你定的第 6 条）。

        @return `{"from","to","cleared","kept"}`；没重置就是 None
        @note "变了" = **两边都是已知心情**且不同 —— `unknown` 不算变化
              （模型偶尔答不出, 不该因此把队列清掉）。
        @note 重置时**保留正在放的那首**在队首（不掐正在听的）, 其余清掉再按新心情补满。
        """
        if self.music is None:
            return None
        new_label = str((record.get("mood") or {}).get("label") or "")
        old_label = str(((previous or {}).get("mood") or {}).get("label") or "")
        if not new_label or not old_label:
            return None
        if new_label in ("unknown", old_label) or old_label == "unknown":
            return None
        kept = self.music.current_id
        cleared = self.music.clear_queue()["cleared"]
        if kept:
            try:
                self.music.enqueue(kept, verify=False)
            except MusicError as exc:               # noqa: BLE001
                self.log.warning("music: 重置队列时没能留住正在放的那首: %s", exc)
        self._push_music(self.music.snapshot())
        self.log.info("music: 心情从 %s 变成 %s -> 重置队列（清掉 %d 首, 留住正在放的 %s）",
                      old_label, new_label, cleared, kept or "-")
        self._music_autofill()
        return {"from": old_label, "to": new_label, "cleared": cleared, "kept": kept}

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
                # T10-4: 队列短于目标就补（先本地库、不够按歌手去 PC 搜）——
                # 走线程池: 搜那一段是 ssh, 不能堵事件循环。
                await loop.run_in_executor(None, self._music_autofill)
            except asyncio.CancelledError:
                raise
            except Exception as exc:        # noqa: BLE001 - 轮询失败不该把任务搞死
                self.log.warning("music: 轮询失败 (已忽略): %r", exc)

    def _music_autofill(self) -> Optional[Dict[str, Any]]:
        """把队列补到目标长度（**同步**, 由轮询丢线程池跑）。

        @return `music.refill()` 的结果；没做事就是 None
        @note 只在"短于目标"时动手; 补不上时**同一条原因只记一次**（别每 3 秒刷一遍）。
        """
        from agent.core import user_profile

        if self.music is None or not getattr(self, "_autofill", {}).get("enabled"):
            return None
        target = self.music.target()
        if target <= 0 or len(self.music.queue_ids()) >= target:
            return None

        record = None
        muted_artists: List[str] = []
        muted_tracks: List[str] = []
        if self.profile_file:
            try:
                records = user_profile.read_records(self.profile_file)
                record = records[-1] if records else None
                muted = user_profile.muted_from_records(records)
                muted_artists = [str(item.get("name")) for item in muted["artist"].values()]
                muted_tracks = [str(item.get("name")) for item in muted["track"].values()]
            except ProfileError as exc:
                self.log.warning("music: 画像读不了（补歌就没偏好可用）: %s", exc)
        try:
            result = self.music.refill(
                record, muted_artists=muted_artists, muted_tracks=muted_tracks,
                by_artist=self._autofill["by_artist"],
                search_per_cycle=self._autofill["search_per_cycle"],
                search_limit=self._autofill["search_limit"],
                search_backoff_s=self._autofill["search_backoff_s"])
        except Exception as exc:        # noqa: BLE001 - 补歌失败不该把轮询搞死
            self.log.warning("music: 自动补歌失败 (已忽略): %r", exc)
            return None
        note = "；".join(result.get("why") or [])
        if note and note != getattr(self, "_autofill_note", ""):
            self.log.info("music: 自动补歌: %s", note)
        self._autofill_note = note
        return result

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

    # ---- B 站视频（T11-6: 队列 / 观察器 / 缓冲; GUI 命令与工具都走这里）----
    def bilibili_state(self) -> Dict[str, Any]:
        """队列 + 当前那条 + 缓冲现状（**只读**, 给工具/日志/GUI 用）。

        @note ⚠ T11-7 补的一个**真缺口**: T11-6 起这里只带 `current`, **没带 `queue`** ——
              于是 GUI 的预览栏**一条都画不出来**（协议文档 §3 的表里 `queue` 一直是有的,
              是实现的漏项）。现在把窗口里的每一条都放进去。
        """
        if self.bilibili is None:
            return {"ok": False, "error": "B 站视频没开（config.yaml 的 bilibili.enabled）"}
        out = dict(self.bilibili.state())
        out["queue"] = self.bilibili.items        # 预览栏要的那一串（窗口 = 3×格数）
        out["ok"] = True
        out["stream"] = self._bilibili_stream
        out["quality"] = self._bilibili_quality
        buffer = self._buffer.snapshot() if self._buffer is not None else None
        out["buffer"] = buffer
        out["ready"] = bool(buffer and buffer.get("ready"))
        return out

    def bilibili_search(self, keyword: str) -> Dict[str, Any]:
        """**对话那条路**（工具 `bilibili_search` 的入口）: 关键词 -> 重建队列并推给 GUI。

        @return 给模型看的形状: {"ok","count","index","current","keyword","why"}
        @note 这里**只搜不播** —— 播不播由用户在预览栏里点（你定的）。
        """
        if self.bilibili is None:
            return {"ok": False, "tell_user": "B 站视频还没开：在 config.yaml 的 bilibili 段"
                                             "里把 enabled 打开。",
                    "error": "bilibili 没开"}
        result = self.bilibili.search(keyword, source="dialogue")
        self._push_bilibili()
        current = result.get("current") or {}
        self.log.info("bilibili: 搜「%s」-> %s（%d 条, 目标 %d）",
                      keyword, "成功" if result["ok"] else "失败", result["count"],
                      self.bilibili.target())
        out = {"ok": bool(result["ok"]), "count": result["count"],
               "index": result["index"], "keyword": result["keyword"],
               "why": result.get("why") or "",
               "current": {"bvid": current.get("bvid"), "title": current.get("title"),
                           "author": current.get("author")} if current else None}
        if not result["ok"]:
            out["error"] = result.get("why") or "搜索失败"
            out["tell_user"] = ("没搜到能用的视频：%s" % (result.get("why") or "")
                                ) if result.get("why") else "搜索失败了，过一会再试"
        return out

    def bilibili_control(self, action: str, payload: Optional[Dict[str, Any]] = None
                         ) -> Dict[str, Any]:
        """**GUI 那条路**（IPC 命令的入口）—— 只有它会让视频**真的开始放**。

        @param action next / prev / pick / viewport / video_state
        @return {"ok", "tell_user"?} —— ok=False 时 tell_user 是给用户的一句话
        @note `viewport` / `video_state` **不播**（前者是格数上报, 后者是进度回报）。
        """
        if self.bilibili is None:
            return {"ok": False, "error": "bilibili 没开",
                    "tell_user": "B 站视频还没开：在 config.yaml 的 bilibili 段里把 enabled 打开。"}
        data = dict(payload or {})
        if action == "viewport":
            visible = self.bilibili.set_viewport(data.get("visible"))
            self._push_bilibili()
            return {"ok": True, "viewport": visible, "target": self.bilibili.target()}
        if action == "video_state":
            return self._bilibili_video_state(data)
        if action in ("next", "prev"):
            moved = self.bilibili.move(1 if action == "next" else -1)
            if not moved["ok"]:
                return {"ok": False, "error": moved["why"], "tell_user": moved["why"]}
            return self._bilibili_play()
        if action == "pick":
            picked = self.bilibili.pick(data.get("index"))
            if not picked["ok"]:
                return {"ok": False, "error": picked["why"], "tell_user": picked["why"]}
            return self._bilibili_play()
        if action == "clear":
            cleared = self.bilibili.clear()
            self._stop_buffer()
            self._push_bilibili()
            return {"ok": True, "why": cleared["why"]}
        return {"ok": False, "error": "不认识的 B 站动作 %r" % action}

    def _bilibili_play(self) -> Dict[str, Any]:
        """给当前那条**取流 + 起缓冲**（只有 GUI 操作才会调到它）。

        @note **不阻塞事件循环**: `start()` 只是起 ffmpeg 与两个线程, "攒够 15 s"
          在后台线程里等（`on_ready` 回调把 FIFO 路径推给 GUI）。
        """
        if self.bilibili is None or self._bilibili_api is None:
            return {"ok": False, "error": "bilibili 没开"}
        item = self.bilibili.current()
        if not item:
            return {"ok": False, "tell_user": "队列是空的 —— 在 GAME 模式里跟我说一个视频名，"
                                             "或者点一下预览图"}
        from agent.core.bilibili_buffer import BilibiliBuffer, BufferError

        self._stop_buffer()
        buffer_cfg = self._bilibili_cfg.get("buffer") or {}
        buffer = BilibiliBuffer(self._bilibili_api,
                                fifo_dir=buffer_cfg.get("dir"),
                                initial_s=buffer_cfg.get("initial_s", 15),
                                max_s=buffer_cfg.get("max_s", 60),
                                mem_watermark_mb=buffer_cfg.get("mem_watermark_mb", 400),
                                log=self.log)
        buffer.on_ready = self._on_bilibili_ready
        try:
            started = buffer.start(item)
        except BufferError as exc:
            self.log.warning("bilibili: 起不了缓冲: %s", exc)
            return {"ok": False, "error": str(exc), "tell_user": "这条放不了：%s" % exc}
        self._buffer = buffer
        self._bilibili_stream = ""
        self._bilibili_quality = str((started or {}).get("quality") or "")
        # 后台等门槛（**不堵事件循环**）: 到点了 on_ready 会推路径; 等不到就按现有缓冲起播
        threading.Thread(target=self._wait_bilibili_ready, args=(buffer,),
                         name="bilibili-gate", daemon=True).start()
        self._push_bilibili()
        self.log.info("bilibili: 开始缓冲 %s（%s, 门槛 %.0f s）", item.get("bvid"),
                      started.get("state"), buffer.initial_s)
        return {"ok": True, "bvid": item.get("bvid"), "title": item.get("title"),
                "state": started.get("state")}

    def _wait_bilibili_ready(self, buffer: Any) -> None:
        """等"攒够 15 s"（后台线程）; 等不到就**按现有缓冲起播**并如实说一句。"""
        try:
            if buffer.wait_ready(buffer.initial_s * 2 + 5.0):
                return
            if self._buffer is not buffer:
                return                                    # 已经换条/停了
            buffer.open_gate()
            self._say("B 站这条网络有点慢：没攒够 %.0f 秒就先起播了（缓冲 %.1f 秒）"
                      % (buffer.initial_s, buffer.buffered_s()))
        except Exception as exc:                          # noqa: BLE001 - 后台线程不能炸
            self.log.warning("bilibili: 等门槛时出错（忽略）: %r", exc)

    def _on_bilibili_ready(self, path: str) -> None:
        """缓冲就绪（**从缓冲线程调进来**）-> 把 FIFO 路径推给 GUI, 并提醒清晰度。

        @note 这条路只在**用户操作之后**才会发生（你定的"等 GUI 操作才开始播放"）。
        @note 清晰度提示**只走聊天气泡**（你定的"清晰度只使用 LLM 对话框, 不更新 GUI"）。
        """
        if self._buffer is None or self._buffer.path != path:
            return
        self._bilibili_stream = path
        snapshot = self._buffer.snapshot()
        self._bilibili_quality = str(snapshot.get("quality") or self._bilibili_quality)
        self._push_bilibili()
        self.log.info("bilibili: 可以播了 %s（%s, 缓冲 %.1f s）", path,
                      self._bilibili_quality or "清晰度未知", snapshot.get("buffered_s") or 0)
        note = self._bilibili_quality_note()
        if note:
            self._say(note)

    def _check_bilibili_cookie(self) -> None:
        """起播前把 cookie 的**有效性**问清楚（T11-9 板端验收抓到的漏项）。

        ⚠ 为什么必须在**起播前**问：实测（T11-9）—— cookie 失效时 B 站的 playurl
          **不报错**，它就把你当匿名（给 480P/720P），于是"cookie 过期"这件事
          只能靠 `nav`（`login()`）看出来。不问的话，用户看到的是
          "这条 B 站只给到 720P，和登不登录无关" —— 那是**甩锅给视频**。
        只问一次（进程启动 / 状态重进时），结果记在 `api.auth_note` 上。
        """
        api = self._bilibili_api
        if api is None or not api.cookie_present:
            return
        try:
            info = api.login(refresh=True)
        except Exception as exc:                          # noqa: BLE001 - 问不到不该拦住启动
            self.log.warning("bilibili: 登录态没问出来（忽略）: %r", exc)
            return
        if info.get("logged_in"):
            self.log.info("bilibili: cookie 有效（高清走 DASH）")
            return
        api.auth_note = str(info.get("why") or "cookie 失效了")
        self.log.warning("bilibili: %s", api.auth_note)

    def _bilibili_quality_note(self) -> str:
        """这一条清晰度的**如实提醒**（要说的才说, 1080P 及以上不念叨）。

        ⚠ T11-9 板端验收抓到的顺序问题: **cookie 失效**那条话必须排在最前面 ——
          失效时 `playurl` 会退回匿名单文件（视频照样能放），如果先按"清晰度 < 80"去解释，
          用户看到的是"这条视频就这样"，而真相是"cookie 过期了、配上就能 1080P"。
        """
        if self._buffer is None:
            return ""
        api = self._bilibili_api
        auth_note = str(getattr(api, "auth_note", "") or "")
        if auth_note:
            return "（cookie 失效了：%s）" % auth_note
        stream = getattr(self._buffer, "stream", {}) or {}
        quality = int(stream.get("quality") or 0)
        if quality >= 80:
            return ""
        label = str(stream.get("quality_label") or "未知清晰度")
        cookie = bool(api is not None and api.cookie_present)
        if not cookie:
            return ("（没配 config/bilibili_cookie.json，这条只给到 %s；要高清得在板端配上 "
                    "SESSDATA）" % label)
        return "（这条 B 站只给到 %s，和登不登录无关）" % label

    def _bilibili_video_state(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """GUI 回报的真实进度（**不播**）: 暂停时就多预取, `eof` 时自动下一集。"""
        if self._buffer is not None:
            self._buffer.set_playing(bool(payload.get("playing", True)))
        if not payload.get("eof"):
            return {"ok": True, "position_s": payload.get("position_s")}
        moved = self.bilibili.move(1) if self.bilibili is not None else {"ok": False,
                                                                       "why": "队列是空的"}
        if not moved.get("ok"):
            self._stop_buffer()
            self._bilibili_stream = ""
            self._push_bilibili()
            self.log.info("bilibili: 放完了，后面没有了（%s）", moved.get("why"))
            return {"ok": True, "eof": True, "note": moved.get("why") or ""}
        self.log.info("bilibili: 放完了 -> 自动下一集 %s",
                      (self.bilibili.current() or {}).get("bvid"))
        played = self._bilibili_play()
        played["eof"] = True
        return played

    def _stop_buffer(self) -> None:
        """停掉当前缓冲（换条/清空/退出时都走它; 幂等）。"""
        buffer, self._buffer = self._buffer, None
        self._bilibili_stream = ""
        if buffer is None:
            return
        try:
            buffer.stop()
        except Exception as exc:                          # noqa: BLE001
            self.log.warning("bilibili: 停缓冲时出错（忽略）: %r", exc)

    def push_current_bilibili(self) -> bool:
        """GUI 刚连上时补推一次队列（与 `push_current_music` 同款）。"""
        if self.bilibili is None:
            return False
        return self._push_bilibili()

    def _push_bilibili(self, **extra: Any) -> bool:
        """把队列现状推给 GUI（topic `bilibili` 的负载只在这一处构造）。"""
        if self.on_bilibili is None:
            return False
        payload = self.bilibili_state()
        payload.update(extra)
        try:
            self.on_bilibili(payload)
            return True
        except Exception as exc:                          # noqa: BLE001 - 推送失败不该影响队列
            self.log.warning("bilibili: 推送失败 (已忽略): %r", exc)
            return False

    def _say(self, text: str) -> None:
        """往对话区说一句（走 `on_reply`; 没有 IPC 时就只记日志）。"""
        if not text:
            return
        if self.on_reply is None:
            self.log.info("bilibili: %s", text)
            return
        try:
            self.on_reply(text)
        except Exception as exc:                          # noqa: BLE001
            self.log.warning("bilibili: 气泡推不出去 (已忽略): %r", exc)

    async def _game_watch_loop(self) -> None:
        """**GAME 模式里的循环**（你定的: 画面捕获不走 LLM 工具, 是 Agent 自己的循环）。

        · 只在 **GAME** 里抓帧认游戏（别的模式画面里没有游戏）;
        · **有对话关键词时完全不抓帧**（你定的: 有对话就不看画面）;
        · 认出的游戏**变了**才重搜队列（否则会把你正在看的列表刷掉）;
        · 模型按状态常驻/卸载（STUDY/GAME 常驻 —— 你定的）。
        """
        while True:
            try:
                await asyncio.sleep(self._GAME_WATCH_TICK_S)
                state = self.state.current().value if self.state is not None else ""
                if self._game_watch is not None:
                    self._game_watch.ensure_model(state)          # 常驻/卸载
                if state != "game" or self._game_watch is None or self.bilibili is None:
                    continue
                has_keyword = bool(self.bilibili.keyword) and self.bilibili.source == "dialogue"
                if has_keyword:
                    continue                                     # 有对话关键词 -> 一帧都不抓
                frame = None
                if self.image_reader is not None:
                    frame = await self.image_reader.read_latest()
                decision = self._game_watch.tick(frame, state=state, has_keyword=False)
                for note in self._game_watch.notes():
                    self.log.info("bilibili: %s", note)
                if decision.get("skipped"):
                    self.log.debug("bilibili: 观察器跳过（%s）", decision["skipped"])
                    continue
                game = str(decision.get("game") or "")
                self.log.info("bilibili: 画面认游戏 -> %s（%s, %.3f/余量 %.3f）%s",
                              game or "认不出", decision.get("source") or "-",
                              decision.get("score") or 0.0, decision.get("margin") or 0.0,
                              ("；" + decision["note"]) if decision.get("note") else "")
                if game and game != self._last_bilibili_game:
                    self._last_bilibili_game = game
                    result = self.bilibili.search(game, source="screen")
                    self._push_bilibili()
                    self.log.info("bilibili: 按画面搜「%s」-> %s（%d 条）", game,
                                  "成功" if result["ok"] else "失败", result["count"])
                    if not result["ok"] and result.get("why"):
                        self._say("想给你找「%s」的视频，但没搜到：%s" % (game, result["why"]))
            except asyncio.CancelledError:
                raise
            except Exception as exc:                          # noqa: BLE001 - 轮询不能死
                self.log.warning("bilibili: 观察循环出错（已忽略）: %r", exc)
                await asyncio.sleep(5.0)

    #: 观察循环的轮询间隔（真正"该不该看"由 GameWatcher 按 60 s 判; 这里只是心跳）
    _GAME_WATCH_TICK_S = 2.0

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

    def _state_text(self) -> str:
        """当前模式（"idle"/"study"…；状态机没起来就是 "unknown"）。"""
        return self.state.current().value if self.state else "unknown"

    async def _deliver_reply(self, reply: str, source: Optional[str] = None) -> str:
        """把一条回复算进统计、记日志、交给 IPC 钩子（两条路共用: 直连 / 模型）。

        @param source 这条回复是**对谁说的**（T9-1: 只有 gui/terminal 的对话进纯对话记忆；
                      不给就是"不知道来源", 不记记忆)
        """
        self.stats["replies"] += 1
        self.log.info("回复: %s", reply)
        if source is not None:
            self.chat_memory.add_reply(reply, source=source, state=self._state_text())

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

        ⚠ T9-3: 每轮答完都走一次 `_profile_tick()`（**放在 finally 里**, 成功/直连/失败
          三条路都算一轮对话）: 纯对话攒到触发线就在后台构建一次用户画像。
        """
        try:
            return await self._handle_event(event)
        finally:
            self._profile_tick()

    async def _handle_event(self, event: Dict[str, Any]) -> Optional[str]:
        """`handle_event` 的正身（记记忆 -> 直连或模型 -> 回复 -> 计统计）。"""
        source = event.get("source", "?")
        text = event.get("text", "")
        self.stats["events"] += 1
        self.log.info("[%s] %s", source, text)
        # T9-1: 纯对话记忆（只收 gui/terminal；记下**当时**的模式给 T9-2 判心情用）
        self.chat_memory.add_user(text, source=source, state=self._state_text())

        direct = read_intents.answer(text, self.read_services())
        if direct is not None:
            self.log.info("只读问句直连 (%s, 没走模型)", direct.intent)
            return await self._deliver_reply(direct.text, source=source)

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
        return await self._deliver_reply(reply, source=source)

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
