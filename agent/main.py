# ============================================================================
#  agent/main.py — 把各组件串起来, 启动 asyncio 事件循环
#
#  启动顺序 (按依赖关系, 关了再反着来)
#  ---------------------------------------------------------------------------
#      1. 日志         logs/agent.log — 最早, 后面出错才有地方看
#      2. config       读 config.yaml; 读不到就用模板, 都不行就退出
#      3. native       moonlight.start_with_session() —— **失败不致命**, 只记 warning
#      4. ChatInputBus 三源汇合点, 后面所有组件都挂在它上面
#      5. io 层        host_input_reader(轮询→bus)
#      6. StateMachine SLEEP/IDLE/STUDY/GAME
#      7. ToolRouter   注册工具 (agent/tools/ 还没写 -> 空路由)
#      8. LLMProvider  edge / cloud / disabled
#      9. Scheduler    日程 + 快捷键 (订阅 bus, 只看不取)
#     10. IPC          当前没实现 -> 跳过并记一条日志
#     11. 终端输入     可选 (config: terminal.enabled)
#
#  stop 时**严格反向**: terminal -> ipc -> scheduler -> llm -> tools -> state
#                        -> io -> bus -> native
#
#  异常处理: 单个组件失败不影响其他组件
#  ---------------------------------------------------------------------------
#  每个组件都用 _guarded() 单独 start/stop。失败只记 error 并继续 ——
#  板子上的现实是"能跑起来比跑得全更重要": 摄像头没插不该让快捷键也失效。
#  失败的组件记进 failures, 收尾时给一份汇总。
#
#  关于 IPC (第 10 步)
#  ---------------------------------------------------------------------------
#  协议已定 (docs/ipc-protocol.md + agent/ipc/protocol.py), 但**收发还没写**
#  (server / client 在 todo.md 里)。这里**不假装**起了一个 IPC: 找不到
#  build_ipc() / IPCServer 就记一条 warning 跳过。将来把 server 放进 agent/ipc/
#  并导出 build_ipc(bus, config) 即可自动接入, 不用改本文件。
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
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# 允许 `python agent/main.py` 直接跑 (此时包根不在 sys.path 上)
if __package__ in (None, ""):  # pragma: no cover - 只在直接执行时走
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.config import ConfigError, ConfigNotFoundError, config_path, load_config
from agent.core import Scheduler, State, StateMachine, ToolRouter
from agent.io import (
    ChatInputBus,
    HostInputReader,
    ImageReader,
    InputSender,
    event_to_text,
)
from agent.llm import DEFAULT_MODE, LLMProvider, RuleEngine

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
        self.scheduler: Optional[Scheduler] = None
        self.image_reader: Optional[ImageReader] = None
        self.host_input_reader: Optional[HostInputReader] = None
        self.input_sender: Optional[InputSender] = None
        self.ipc: Any = None

        #: 回复钩子: 每产生一条 LLM 回复就调一次 (IPC 层用它推 llm 消息)。
        #: 默认 None —— 没有 IPC 时行为与以前完全一样。
        #: Phase 6 D4: 之前 handle_event() 的回复被主循环直接丢掉, 推不出去。
        self.on_reply: Optional[Callable[[str], Any]] = None

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
        "_start_state_and_tools",
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

        # native 是"可选增强": 连不上照样跑本地规则与快捷键
        await self._guarded(
            _Component("native", _start, _stop),
            fatal=False,
        )

    # ---- 2) bus + io ----
    async def _start_bus_and_io(self) -> None:
        async def _start_bus() -> None:
            self.bus = ChatInputBus()
            self.log.info("ChatInputBus 就绪 (三源: 终端 / GUI / 主机键盘)")

        await self._guarded(_Component("chat_bus", _start_bus))

        async def _start_io() -> None:
            # 三个 IO 组件共用同一个 native, 但各自有专属执行器 (见 io/_native.py)
            self.image_reader = ImageReader()
            self.input_sender = InputSender()
            self.host_input_reader = HostInputReader()
            await self.host_input_reader.start_polling(
                self.bus,
                interval_ms=int(self._cfg("scheduler", "host_input_interval_ms", default=50)),
            )
            self.log.info("io 层就绪 (image_reader / input_sender / host_input_reader 轮询中)")

        async def _stop_io() -> None:
            if self.host_input_reader is not None:
                await self.host_input_reader.stop()

        await self._guarded(_Component("io", _start_io, _stop_io))

    # ---- 3) state + tools ----
    async def _start_state_and_tools(self) -> None:
        async def _start_state() -> None:
            self.state = StateMachine()
            self.log.info("StateMachine 就绪 (初始状态 %s)", self.state.current().value)

        await self._guarded(_Component("state_machine", _start_state))

        async def _start_tools() -> None:
            self.tools = ToolRouter(state_provider=self.state)
            registered = self._register_tools(self.tools)
            self.log.info("ToolRouter 就绪 (%d 个工具)", registered)

        await self._guarded(_Component("tool_router", _start_tools))

    def _register_tools(self, router: ToolRouter) -> int:
        """注册工具。

        agent/tools/ 还没实现, 所以这里找不到模块就当作"还没有工具" —— 空的
        ToolRouter 是完全可用的 (LLM 只是没有工具可调), 不该因此起不来。
        想加工具时按下面的约定提供工厂即可, 本文件不用改。
        """
        try:
            module = importlib.import_module("agent.tools")
        except ImportError:
            self.log.warning(
                "tools: agent/tools/ 还没有实现, 工具列表为空 "
                "(LLM 仍可对话, 只是没有工具可调)"
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

        await self._guarded(_Component("llm_provider", _start))

    # ---- 5) scheduler ----
    async def _start_scheduler(self) -> None:
        async def _start() -> None:
            self.scheduler = Scheduler(state=self.state, bus=self.bus, config=self.config)
            await self.scheduler.start()
            self.log.info(
                "Scheduler 就绪 (%d 条日程, %d 个快捷键, 每 %g 分钟检查)",
                len(self.scheduler.events),
                len(self.scheduler.bindings),
                self.scheduler.interval_min,
            )
            for warning in self.scheduler.warnings:
                self.log.warning("scheduler: %s", warning)

        async def _stop() -> None:
            if self.scheduler is not None:
                await self.scheduler.stop()

        await self._guarded(_Component("scheduler", _start, _stop))

    # ---- 6) ipc ----
    async def _start_ipc(self) -> None:
        """启动 IPC。只有协议、没有 server 就跳过 —— 不假装提供, 也不因此失败。"""
        try:
            module = importlib.import_module("agent.ipc")
        except ImportError:
            self.log.warning(
                "ipc: agent/ipc/ 缺失, 跳过 (GUI 暂时连不上; 不影响其余功能)"
            )
            return

        factory = getattr(module, "build_ipc", None) or getattr(module, "IPCServer", None)
        if factory is None:
            # 当前就是这个状态: agent/ipc/ 里只有 protocol.py (协议已定, 收发还没写)
            self.log.warning(
                "ipc: agent/ipc/ 里只有协议 (protocol.py), 还没有 build_ipc()/IPCServer, "
                "跳过 —— GUI 暂时连不上, 不影响其余功能"
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

        @note 快捷键由 Scheduler 自己消费它需要的那部分 —— 它用 subscribe()
              旁听 host_keyboard, 不走这个循环 (见 scheduler 的注释)。
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

    async def handle_event(self, event: Dict[str, Any]) -> Optional[str]:
        """处理一条 bus 事件并返回回复 (没回复则 None)。

        单独抽出来是为了能直接测"一条消息进来会怎样", 不用起整个进程。
        """
        source = event.get("source", "?")
        text = event.get("text", "")
        self.stats["events"] += 1
        self.log.info("[%s] %s", source, text)

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

        reply = result.get("text") or ""
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
