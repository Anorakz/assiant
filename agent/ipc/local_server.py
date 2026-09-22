# ============================================================================
#  agent/ipc/local_server.py — Agent 侧的 Unix socket server
#
#  角色分工 (方向很重要)
#  ---------------------------------------------------------------------------
#      Agent (本文件, asyncio server)  ——  server, 主动 push 状态
#      GUI   (Qt5, 见 docs/ipc-protocol.md 第 8 节)  ——  client, 发命令
#
#      一个 socket **双向**通信:
#          Agent -> GUI   本文件 push(topic, data)
#          GUI   -> Agent 本文件 on_command(cb) 收到 cb(action, payload)
#
#  传输
#  ---------------------------------------------------------------------------
#      AF_UNIX / SOCK_STREAM, 默认 /tmp/agent.sock
#      换行分隔 JSON (NDJSON), UTF-8, 帧格式全部来自 protocol.py
#
#      走 Unix socket 而不是 TCP: 不占端口、不过网卡, 天然只限本机 ——
#      GUI 和 Agent 都在板端跑, 没有必要暴露到网络上。
#
#  三件容易被忽略、但必须有的事
#  ---------------------------------------------------------------------------
#  1) **读循环不能被慢 GUI 卡住**
#     push() 不直接 write+drain 到 socket, 而是投进每个连接自己的**有界队列**,
#     由一条独立的写任务去 write+drain。GUI 卡住时队列满了就丢最旧的一条并记
#     WARNING —— 状态是"快照"语义, 丢掉旧快照比卡死 Agent 好得多。
#     如果直接 await drain(), 一个不读数据的 GUI 就能把整个 Agent 拖停。
#
#  2) **一条坏消息不能断连接**
#     协议约定: 解析失败 -> 记日志 -> 丢弃该行 -> 连接继续用 (见 protocol.py)。
#     超长(一直不发换行)的行也按同样方式丢弃, 但丢掉的内容有字节上限, 免得
#     对端拿垃圾流把内存吃光。
#
#  3) **残留 socket 文件要能自动收拾**
#     进程被 kill -9 时 /tmp/agent.sock 会留下来, 下次 bind 直接 EADDRINUSE。
#     所以 bind 前先探测: 连得上 = 真有实例在跑 -> 报错(不抢别人的 socket);
#     连不上 = 上次异常退出的残留 -> 删掉重建。
#
#  平台
#  ---------------------------------------------------------------------------
#  ⚠ Windows 的 CPython **没有** socket.AF_UNIX, 也没有 asyncio.start_unix_server,
#    所以本模块只能在 Linux/macOS 上真正跑起来。UNIX_SOCKET_SUPPORTED 就是给
#    上层 (agent/ipc/__init__.py 的 build_ipc) 用来降级的。
#    本文件里 _ClientSession (收发逻辑) 不碰 AF_UNIX, 所以在 Windows 上也能测。
# ============================================================================

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import socket
import stat
from typing import Any, Callable, List, Optional

from .protocol import (
    COMMANDS,
    MAX_LINE_BYTES,
    MESSAGE_SEPARATOR,
    MODES,
    SOCKET_PATH,
    IpcProtocolError,
    decode,
    decode_command,
    encode,
)

__all__ = [
    "UNIX_SOCKET_SUPPORTED",
    "DEFAULT_BACKLOG",
    "DEFAULT_MODE",
    "DEFAULT_QUEUE_SIZE",
    "PROBE_TIMEOUT",
    "IpcServerError",
    "LocalServer",
    "NullServer",
    "mode_to_wire",
    "mode_from_wire",
]


# ---------------------------------------------------------------------------
#  平台能力
# ---------------------------------------------------------------------------
#: 当前平台是否支持 AF_UNIX 上的 asyncio server。
#: Windows 上是 False (socket.AF_UNIX 不存在) —— 上层据此降级, 不要在这里抛错。
UNIX_SOCKET_SUPPORTED = bool(
    hasattr(socket, "AF_UNIX") and hasattr(asyncio, "start_unix_server")
)

#: listen 队列长度。GUI 只有一个, 8 足够应付"重连瞬间挤了两次"。
DEFAULT_BACKLOG = 8

#: socket 文件权限。0600 = 只有属主能连 —— GUI 和 Agent 同用户跑, 够用且更紧。
#: (start_unix_server 建出来的默认是 0755, 受 umask 影响, 所以这里显式 chmod。)
DEFAULT_MODE = 0o600

#: 每个连接的发送队列长度。按"GUI 偶尔卡一下"来定: 128 条状态推送足够缓冲,
#: 又小到能立刻发现"这个 GUI 根本不读了"。
DEFAULT_QUEUE_SIZE = 128

#: 探测残留 socket 时 connect 的等待上限 (秒)。本机 Unix socket 要么立刻成功
#: 要么立刻 ECONNREFUSED, 半秒是纯粹的保险, 不至于让启动变慢。
PROBE_TIMEOUT = 0.5


class IpcServerError(RuntimeError):
    """IPC server 生命周期错误 (平台不支持 / 绑定失败 / 别人已经在监听)。

    继承 RuntimeError 而不是 ValueError: 这不是"参数写错了", 而是运行环境
    问题 —— 与 agent/llm/provider.py 的 LLMError 一致。
    """


# ---------------------------------------------------------------------------
#  模式名大小写转换 (协议要求, 且只在这一层做)
# ---------------------------------------------------------------------------
#  protocol.py 里的 MODES 是**全大写** ("SLEEP"/"IDLE"/"STUDY"/"GAME"),
#  而 state_machine.State.value 是**小写** ("sleep"/...)。两套不能混着传,
#  转换统一放在 ipc server 这一层 (protocol.py 的注释也是这么约定的)。
def mode_to_wire(mode: Any) -> str:
    """内部状态 -> IPC 上的模式名 (全大写)。

    @param mode State 枚举成员 ("study") / 字符串 ("study" / "STUDY")
    @return "SLEEP" / "IDLE" / "STUDY" / "GAME"
    @raise IpcProtocolError 不是已知模式
    """
    if mode is None:
        raise IpcProtocolError("mode must not be None")
    # State.STUDY -> "study"; 已经是 str 的话原样用
    text = getattr(mode, "value", mode)
    if not isinstance(text, str):
        raise IpcProtocolError(
            "mode must be a str or State, got %s" % type(mode).__name__
        )
    name = text.strip().upper()
    if name not in MODES:
        raise IpcProtocolError(
            "unknown mode %r (expected one of %s)" % (mode, "/".join(MODES))
        )
    return name


def mode_from_wire(text: Any) -> str:
    """IPC 上的模式名 -> 状态机的取值 (全小写)。

    @return "sleep" / "idle" / "study" / "game" (可直接喂给 StateMachine)
    @raise IpcProtocolError 不是已知模式
    """
    if not isinstance(text, str):
        raise IpcProtocolError(
            "mode must be a string, got %s" % type(text).__name__
        )
    name = text.strip().upper()
    if name not in MODES:
        raise IpcProtocolError(
            "unknown mode %r (expected one of %s)" % (text, "/".join(MODES))
        )
    return name.lower()


# ---------------------------------------------------------------------------
#  单个连接的收发
# ---------------------------------------------------------------------------
class _ClientSession:
    """一个 GUI 连接的收发逻辑。

    刻意**不碰** AF_UNIX / accept / bind: 只依赖 asyncio 的 reader/writer 接口,
    所以能在任何平台上用假的 stream 直接单测 (Windows 上唯一的测法)。

    职责:
        · 读循环: 按 \\n 切分 -> decode() -> on_message(topic, data)
        · 写队列: enqueue(payload) 入队, 独立的写任务 write+drain
        · 收尾: close() 幂等
    """

    #: 关连接时等发送缓冲冲刷的上限 (秒)。到点就 abort() 硬断 —— 见 close()。
    CLOSE_TIMEOUT = 1.0

    def __init__(
        self,
        reader: "asyncio.StreamReader",
        writer: "asyncio.StreamWriter",
        on_message: Callable[[str, dict], Any],
        on_drop: Optional[Callable[[str], None]] = None,
        max_line_bytes: int = MAX_LINE_BYTES,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        """
        @param on_message 解析出一条消息就调它 (可以是普通函数或协程函数);
                          它是**同步等待**的 —— 命令必须按顺序处理
        @param on_drop    丢弃一条坏消息时调它 (reason 字符串), 便于上层计数
        @param max_line_bytes 单条消息上限; 超了就丢弃该行
        @param queue_size 发送队列长度; 满了丢最旧的一条
        """
        self._reader = reader
        self._writer = writer
        self._on_message = on_message
        self._on_drop = on_drop
        self.max_line_bytes = max_line_bytes
        self.log = logger or logging.getLogger(__name__)

        self._out: "asyncio.Queue[bytes]" = asyncio.Queue(maxsize=queue_size)
        self._task: Optional["asyncio.Task[None]"] = None
        self._pump: Optional["asyncio.Task[None]"] = None
        self._closing = False
        self._closed = False

        #: 累计计数 (给上层看, 也给测试断言)
        self.received = 0
        self.dropped = 0

    # ---- 对外状态 ----
    @property
    def task(self) -> Optional["asyncio.Task[None]"]:
        """读循环所在的 task (stop() 用它打断)。run() 还没开始跑时是 None。"""
        return self._task

    @property
    def closing(self) -> bool:
        return self._closing

    def __repr__(self) -> str:
        return "<_ClientSession received=%d dropped=%d closing=%s>" % (
            self.received,
            self.dropped,
            self._closing,
        )

    # ---- 发送 ----
    def enqueue(self, payload: bytes) -> bool:
        """把一条已经编码好的消息排进发送队列。

        @return True = 排队成功; False = 队列满了, 丢了**最旧**的一条

        @note 这里绝不阻塞, 也不 await drain(): 调用方是 Agent 的业务代码
              (状态机/调度器), 它不能被一个不读数据的 GUI 拖住。
        @note 队列满时丢最旧而不是断开连接: 推送的是状态快照, 新的一定比旧的
              有用; 断开一个只是"卡了一下"的 GUI 太激进。
        """
        if self._closing:
            return False
        try:
            self._out.put_nowait(payload)
            return True
        except asyncio.QueueFull:
            pass

        # 丢最旧的一条腾位置。中间没有 await, 所以不会和写任务抢。
        with contextlib.suppress(asyncio.QueueEmpty):
            self._out.get_nowait()
        try:
            self._out.put_nowait(payload)
        except asyncio.QueueFull:      # 理论上到不了: 刚腾过位置
            return False
        return False

    async def _pump_loop(self) -> None:
        """把发送队列里的东西真正写出去 (每个连接一条)。"""
        try:
            while True:
                payload = await self._out.get()
                self._writer.write(payload)
                await self._writer.drain()
        except asyncio.CancelledError:
            raise
        except (ConnectionResetError, BrokenPipeError):
            pass                        # 对端没了, 读循环那边会收到 EOF
        except Exception as exc:        # noqa: BLE001 - 写失败不该冒泡到事件循环
            self.log.debug("ipc: 写连接失败: %r", exc, exc_info=True)

    # ---- 接收 ----
    async def run(self) -> None:
        """读循环: 一直读到 EOF / 出错 / close() 被调用。"""
        self._task = asyncio.current_task()
        self._pump = asyncio.create_task(self._pump_loop(), name="ipc-client-writer")
        try:
            while not self._closing:
                raw = await self._read_line()
                if raw is None:
                    return
                await self._handle_line(raw)
        except asyncio.CancelledError:
            raise
        except (ConnectionResetError, BrokenPipeError) as exc:
            self.log.debug("ipc: 连接被对端重置: %r", exc)
        except Exception as exc:        # noqa: BLE001 - 单条连接坏了不影响别人
            self.log.warning("ipc: 读取连接出错, 断开: %r", exc)
            self.log.debug("ipc: 读取连接 traceback", exc_info=True)
        finally:
            await self.close()

    async def _read_line(self) -> Optional[bytes]:
        """读一条以 \\n 结束的原始消息 (含换行原样返回)。

        @return 原始字节; EOF 返回 None
        @note 超长行在这里丢弃 + 记日志, 然后继续读下一条 —— 不断开连接。
        """
        while True:
            try:
                line = await self._reader.readuntil(MESSAGE_SEPARATOR)
            except asyncio.LimitOverrunError as exc:
                # 读到 limit 还没见到换行。先把这一行的残骸吃干净, 再继续。
                if not await self._discard_oversize(exc):
                    self.dropped += 1
                    self._note_drop("oversize without newline")
                    self.log.warning(
                        "ipc: 超长消息里找不到换行 (超过 %d 字节), 断开该连接",
                        self.max_line_bytes,
                    )
                    return None
                self.dropped += 1
                self._note_drop("line too long")
                continue
            except asyncio.IncompleteReadError as exc:
                if exc.partial:
                    # 连接关了但最后一条没写完 —— 丢掉的是一条残缺消息
                    self.dropped += 1
                    self._note_drop("truncated at EOF")
                    self.log.warning(
                        "ipc: 连接在消息中途关闭, 丢弃 %d 字节残缺数据",
                        len(exc.partial),
                    )
                return None

            if len(line) > self.max_line_bytes + len(MESSAGE_SEPARATOR):
                # readuntil 的 limit 已经挡了一层, 这里兜底, 保证喂给 decode()
                # 的一定在上限之内 (decode 自己也会再判一次)。
                self.dropped += 1
                self._note_drop("line too long")
                self.log.warning("ipc: 消息过长 (%d 字节), 已丢弃", len(line))
                continue
            return line

    async def _discard_oversize(self, first: "asyncio.LimitOverrunError") -> bool:
        """把一条超长消息的剩余部分读到换行为止。

        @return True = 找到换行, 这条消息丢掉了但连接还能用
                False = 丢掉的量超过预算(或直接 EOF), 连接应视为不可用
        """
        # 预算: 允许丢掉 8 条最大消息的量。正常超长行几千字节就完了;
        # 能超过这个数说明对端在灌垃圾, 不值得继续陪着读。
        budget = max(8 * self.max_line_bytes, 1 << 16)
        consumed = 0
        error = first
        while True:
            if error.consumed <= 0:
                # 防御: consumed 为 0 会导致 readexactly(0) 空转
                return False
            consumed += error.consumed
            if consumed > budget:
                return False
            try:
                await self._reader.readexactly(error.consumed)
            except asyncio.IncompleteReadError:
                return False
            try:
                await self._reader.readuntil(MESSAGE_SEPARATOR)
                return True
            except asyncio.LimitOverrunError as exc:
                error = exc
            except asyncio.IncompleteReadError:
                return False

    async def _handle_line(self, raw: bytes) -> None:
        """解一条**命令**并交给上层; 不合法就丢弃 (不断连接)。

        命令方向的信封是 {"action", "payload"} —— 以 GUI 的实际实现为准
        (Phase 6 决策 1)。Agent 发出去的是 {"topic","data","timestamp"},
        两个方向字段名不同是现状, 不是笔误。
        """
        try:
            action, payload = decode_command(raw)
        except IpcProtocolError as exc:
            self.dropped += 1
            self._note_drop(str(exc))
            self.log.warning("ipc: 丢弃一条非法命令: %s", exc)
            return

        self.received += 1
        await self._deliver(action, payload)

    async def _deliver(self, action: str, payload: dict) -> None:
        """调上层回调。异常只记日志 —— 一条消息处理失败不该断连接。"""
        try:
            result = self._on_message(action, payload)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:        # noqa: BLE001
            self.log.warning("ipc: 处理命令 %s 出错 (已忽略): %r", action, exc)
            self.log.debug("ipc: 处理命令 traceback", exc_info=True)

    def _note_drop(self, reason: str) -> None:
        if self._on_drop is not None:
            with contextlib.suppress(Exception):
                self._on_drop(reason)

    # ---- 收尾 ----
    def cancel(self) -> None:
        """打断读循环 (不从协程里调, 给 stop() 用)。"""
        task = self._task
        if task is not None and not task.done():
            task.cancel()

    async def close(self) -> None:
        """幂等收尾: 停写任务 -> 关 socket。重复调用安全。

        @note 关 socket 时**必须有超时**: StreamWriter.wait_closed() 要等发送缓冲
              冲刷完, 而对端不读数据时它永远刷不完。没有上限的话, 一个不读数据的
              GUI 能让 stop() 卡死 —— 这会拖住整个 Agent 的退出流程 (以及测试)。
              超时就 abort() 硬断: 反正是要断开的连接, 丢掉未发的数据是可以接受的。
        """
        if self._closed:
            return
        self._closed = True
        self._closing = True

        pump, self._pump = self._pump, None
        if pump is not None and not pump.done():
            pump.cancel()
        if pump is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pump

        writer = self._writer
        with contextlib.suppress(Exception):
            writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=self.CLOSE_TIMEOUT)
        except asyncio.CancelledError:
            self._abort_transport()
            raise
        except Exception as exc:        # noqa: BLE001 - 含 TimeoutError
            self.log.debug("ipc: 关闭连接超时/失败, 直接硬断: %r", exc)
            self._abort_transport()

        # 自己是读任务的 finally 里被调的时不取消自己; 从别处调时把读循环叫醒
        task = self._task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    def _abort_transport(self) -> None:
        """丢掉发送缓冲, 立刻断开 (close() 冲刷不动时的兜底)。"""
        transport = getattr(self._writer, "transport", None)
        abort = getattr(transport, "abort", None)
        if callable(abort):
            with contextlib.suppress(Exception):
                abort()


# ---------------------------------------------------------------------------
#  Server
# ---------------------------------------------------------------------------
class LocalServer:
    """Agent 侧的 Unix socket server (asyncio)。

    典型用法::

        server = LocalServer("/tmp/agent.sock")

        def on_cmd(action, payload):          # 协程函数也可以
            print(action, payload)

        server.on_command(on_cmd)
        await server.start()
        await server.push("status", {"mode": "STUDY", "connected": True})
        ...
        await server.stop()

    线程/事件循环: 只能在**创建它的那个事件循环**里用 (asyncio 的通用要求)。
    Agent 是单进程单循环, 不存在这个问题。
    """

    #: stop() 里等监听 socket 收尾的上限 (秒)。收尾卡住不该拖住进程退出。
    CLOSE_TIMEOUT = 2.0

    def __init__(
        self,
        path: str = SOCKET_PATH,
        *,
        backlog: int = DEFAULT_BACKLOG,
        mode: int = DEFAULT_MODE,
        max_line_bytes: int = MAX_LINE_BYTES,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        logger: Optional[logging.Logger] = None,
        on_client_connect: Optional[Callable[[], Any]] = None,
    ) -> None:
        """
        @param path           socket 文件路径 (默认 /tmp/agent.sock)
        @param backlog        listen 队列长度
        @param mode           建好 socket 后 chmod 的权限 (默认 0600)
        @param max_line_bytes 单条消息上限 (默认与 protocol.MAX_LINE_BYTES 一致)
        @param queue_size     每个连接的发送队列长度
        @param logger         注入 logger (测试用; 默认 agent.ipc.local_server)
        @param on_client_connect 一个新 GUI 连上时的回调 (普通函数或协程函数)。
                              用途: 补推"客户端连上时看不到、只有变化时才推"的那类状态
                              (T6 起用它补当前壁纸)。抛异常只记 WARNING, 连接保持。
        """
        self.path = path
        self.backlog = backlog
        self.mode = mode
        self.max_line_bytes = max_line_bytes
        self.queue_size = queue_size
        self.log = logger or logging.getLogger(__name__)
        self.on_client_connect = on_client_connect

        self._server: Optional["asyncio.AbstractServer"] = None
        self._sessions: List[_ClientSession] = []
        self._callbacks: List[Callable[[str, dict], Any]] = []

        #: 累计计数 (日志与排障用; 也方便测试断言)
        self.received = 0        # 解出来的合法消息条数
        self.dropped = 0         # 丢弃的非法/超长消息条数
        self.pushed = 0          # push() 成功排队的消息条数 (按客户端计)
        self.push_dropped = 0    # push() 因客户端消费不过来而丢掉的条数

    # ---- 只读状态 ----
    @property
    def is_running(self) -> bool:
        return self._server is not None

    @property
    def client_count(self) -> int:
        """当前连着的 GUI 数量 (正常是 0 或 1)。"""
        return len(self._sessions)

    def __repr__(self) -> str:
        return "<LocalServer path=%r running=%s clients=%d>" % (
            self.path,
            self.is_running,
            self.client_count,
        )

    # ---- 回调注册 ----
    def on_command(self, callback: Optional[Callable[[str, dict], Any]]):
        """注册命令处理函数: callback(action: str, payload: dict)。

        @param callback 普通函数或协程函数都可以; 传 None 表示清空已注册的
        @return callback 本身 (方便当装饰器/链式写)

        @note 支持注册多个, 按注册顺序**依次等待**调用。命令必须按顺序处理,
              所以这里是等到它返回再读下一条, 而不是丢成 task 并发跑 ——
              代价是某个回调卡住会卡住"这一个"连接的后续命令 (其他连接和
              push 都不受影响)。
        @note 回调抛异常只记一条 WARNING, 连接保持 (与坏消息同样的策略)。
        """
        if callback is None:
            self._callbacks = []
            return None
        if not callable(callback):
            raise IpcServerError(
                "on_command 需要可调用对象, 得到 %s" % type(callback).__name__
            )
        self._callbacks.append(callback)
        return callback

    # ---- 生命周期 ----
    async def start(self) -> None:
        """绑定并开始 accept。重复调用是幂等的 (不会重复 bind)。

        @raise IpcServerError 平台不支持 / 路径不合法 / 别人已经在监听
        """
        if self._server is not None:
            self.log.debug("ipc: server 已在运行, 忽略重复 start()")
            return

        if not UNIX_SOCKET_SUPPORTED:
            raise IpcServerError(
                "当前平台不支持 AF_UNIX (Windows 的 CPython 没有 socket.AF_UNIX), "
                "IPC server 只能在 Linux/macOS 上跑"
            )

        await self._prepare_socket_path()

        try:
            self._server = await asyncio.start_unix_server(
                self._on_client,
                path=self.path,
                # limit 是 StreamReader 的缓冲上限: 必须 >= 单条消息上限, 否则
                # 一条正常的 1 MiB 消息会被当成超长。+1 给换行留位置。
                limit=self.max_line_bytes + len(MESSAGE_SEPARATOR),
                backlog=self.backlog,
            )
        except OSError as exc:
            raise IpcServerError("绑定 %s 失败: %s" % (self.path, exc)) from exc

        self._chmod_socket()
        self.log.info(
            "IPC server 就绪: %s (权限 %s, 单条上限 %d 字节, 等 GUI 连接)",
            self.path,
            oct(self.mode),
            self.max_line_bytes,
        )

    async def stop(self) -> None:
        """停止监听、断开所有 GUI、删掉 socket 文件。幂等。

        @note **顺序很重要, 不能改**: 先 close() 停止 accept, 再收掉所有连接,
              最后才 await server.wait_closed()。原因是 Python 3.12 起
              Server.wait_closed() 会等**所有 handler 任务**结束, 而 handler 里的
              读循环要等到连接被关才会退出 —— 先等 wait_closed() 就是死锁。
              (3.12 之前它只等监听 socket, 所以在板端 3.8 上根本看不出问题,
               这个顺序在两边都对。)
        """
        server, self._server = self._server, None
        sessions, self._sessions = self._sessions, []

        if server is not None:
            server.close()               # 只停 accept, 已建立的连接不受影响

        # 先打断读循环, 再统一收尾 —— 这样不会留下 pending task 警告
        for session in sessions:
            session.cancel()
        tasks = [s.task for s in sessions if s.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if sessions:
            # close() 幂等: run() 的 finally 已经关过一遍也没关系
            await asyncio.gather(
                *(s.close() for s in sessions), return_exceptions=True
            )

        if server is not None:
            try:
                await asyncio.wait_for(
                    server.wait_closed(), timeout=self.CLOSE_TIMEOUT
                )
            except Exception as exc:    # noqa: BLE001 - 收尾失败不该抛给调用方
                self.log.debug("ipc: 等 server 关闭超时/失败: %r", exc)

        self._unlink_socket()
        if server is not None:
            self.log.info("IPC server 已停止: %s", self.path)

    async def push(self, topic: str, data: dict) -> int:
        """把一条消息推给**所有**连着的 GUI。

        @param topic TOPIC_* (send 方向: status/llm/wallpaper/music)
        @param data  JSON object; timestamp 自动补当前时间
        @return 排队成功的客户端数量 (0 = 当前没 GUI 连着, 不是错误)
        @raise IpcProtocolError topic/data 不合法 (这是调用方的 bug, 要能立刻发现)

        @note 编码一次, 广播给所有连接 (省 CPU, 也保证大家拿到的字节一致)。
        @note 不阻塞: 慢客户端由它自己的队列吸收, 队列满了丢最旧的一条。
        """
        # 先编码: 传错参数时要立刻抛错, 而不是等连接都断了才发现
        payload = encode(topic, data)

        targets = [s for s in self._sessions if not s.closing]
        if not targets:
            self.log.debug("ipc: 没有 GUI 连接, 消息 %s 直接丢弃", topic)
            return 0

        dropped = 0
        for session in targets:
            if not session.enqueue(payload):
                dropped += 1

        self.pushed += len(targets) - dropped
        self.push_dropped += dropped
        if dropped:
            # 一个卡住不读的 GUI 会让每次 push 都丢 —— 不能刷屏, 但也不能不报
            self.log.warning(
                "ipc: %d 个 GUI 消费不过来, 丢弃了最旧的消息 (累计丢 %d 条)",
                dropped,
                self.push_dropped,
            )
        return len(targets)

    # ---- 内部: accept 与分发 ----
    async def _on_client(
        self, reader: "asyncio.StreamReader", writer: "asyncio.StreamWriter"
    ) -> None:
        """一个 GUI 连进来了。"""
        session = _ClientSession(
            reader=reader,
            writer=writer,
            on_message=self._dispatch,
            on_drop=self._note_drop,
            max_line_bytes=self.max_line_bytes,
            queue_size=self.queue_size,
            logger=self.log,
        )
        self._sessions.append(session)
        self.log.info("ipc: GUI 已连接 (当前 %d 个)", len(self._sessions))
        await self._notify_client_connect()
        try:
            await session.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:        # noqa: BLE001
            self.log.warning("ipc: 连接处理出错, 断开: %r", exc)
            self.log.debug("ipc: 连接 traceback", exc_info=True)
        finally:
            with contextlib.suppress(ValueError):
                self._sessions.remove(session)
            await session.close()
            self.log.info("ipc: GUI 已断开 (剩余 %d 个)", len(self._sessions))

    async def _notify_client_connect(self) -> None:
        """叫一次 on_client_connect (有的话)。

        @note 这一下**在读到任何命令之前**发生 —— 客户端连上就能收到"当前状态"
              (T6: 当前壁纸), 不用先发一条查询。
        @note 回调抛异常只记 WARNING: 补推失败不该把刚建立的连接弄断。
        """
        callback = self.on_client_connect
        if callback is None:
            return
        try:
            result = callback()
            if inspect.isawaitable(result):
                await result
        except Exception as exc:        # noqa: BLE001 - 补推失败不是连接失败
            self.log.warning("ipc: on_client_connect 回调出错 (已忽略): %r", exc)
            self.log.debug("ipc: on_client_connect traceback", exc_info=True)

    async def _dispatch(self, topic: str, data: dict) -> None:
        """解出来的消息 -> 已注册的 on_command 回调。

        只有 protocol.COMMANDS 里的命令会被分发; 其他 topic 一律忽略 ——
        没有版本号时, 报错会让新旧版本根本没法共存 (向前兼容)。
        """
        self.received += 1

        if topic not in COMMANDS:
            self.log.warning(
                "ipc: 收到未知命令 %r, 已忽略 (两侧版本可能不一致)", topic
            )
            return

        if not self._callbacks:
            self.log.warning("ipc: 收到命令 %s 但没有注册处理函数, 已忽略", topic)
            return

        self.log.debug("ipc: 收到命令 %s %r", topic, data)
        for callback in list(self._callbacks):
            try:
                result = callback(topic, data)
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception as exc:    # noqa: BLE001
                self.log.warning(
                    "ipc: 处理命令 %s 的回调抛错 (已忽略): %r", topic, exc
                )
                self.log.debug("ipc: 命令回调 traceback", exc_info=True)

    def _note_drop(self, reason: str) -> None:
        self.dropped += 1
        self.log.debug("ipc: 丢弃消息 (%s), 累计 %d 条", reason, self.dropped)

    # ---- 内部: socket 文件 ----
    async def _prepare_socket_path(self) -> None:
        """保证 bind 之前路径是可用的 (建目录 / 清理上次的残留)。"""
        if not isinstance(self.path, str) or not self.path:
            raise IpcServerError("socket 路径必须是非空字符串, 得到 %r" % (self.path,))

        parent = os.path.dirname(os.path.abspath(self.path))
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as exc:
            raise IpcServerError("创建目录 %s 失败: %s" % (parent, exc)) from exc

        if not os.path.exists(self.path):
            return

        # 只删 socket。路径上是个普通文件/目录的话, 那是配置写错了,
        # 删掉它可能毁数据 —— 明确报错让人来处理。
        try:
            st = os.lstat(self.path)
        except OSError as exc:
            raise IpcServerError("读取 %s 状态失败: %s" % (self.path, exc)) from exc
        if not stat.S_ISSOCK(st.st_mode):
            raise IpcServerError(
                "%s 已存在且不是 socket 文件 (拒绝删除, 请先确认)" % self.path
            )

        if await self._probe_alive():
            raise IpcServerError(
                "另一个 agent 实例已经在监听 %s (同一个 socket 只能有一个 server)"
                % self.path
            )

        try:
            os.unlink(self.path)
        except OSError as exc:
            raise IpcServerError("删除残留 socket %s 失败: %s" % (self.path, exc)) from exc
        self.log.warning(
            "ipc: 发现上次留下的 socket 文件 %s 且无人监听, 已删除重建 "
            "(上次应该是异常退出)",
            self.path,
        )

    async def _probe_alive(self) -> bool:
        """连一下现有的 socket 文件, 判断是否真有 server 在监听。"""
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.setblocking(False)
        try:
            await asyncio.wait_for(
                asyncio.get_running_loop().sock_connect(probe, self.path),
                timeout=PROBE_TIMEOUT,
            )
            return True
        except (OSError, asyncio.TimeoutError):
            # ECONNREFUSED / ENOENT = 没人监听; 超时按"不可用"处理
            return False
        finally:
            probe.close()

    def _chmod_socket(self) -> None:
        try:
            os.chmod(self.path, self.mode)
        except OSError as exc:            # 文件系统不支持 chmod 也不该拦住启动
            self.log.warning(
                "ipc: chmod %s -> %s 失败 (权限可能比预期宽): %s",
                self.path,
                oct(self.mode),
                exc,
            )

    def _unlink_socket(self) -> None:
        try:
            if os.path.exists(self.path) and stat.S_ISSOCK(os.lstat(self.path).st_mode):
                os.unlink(self.path)
        except OSError as exc:
            self.log.debug("ipc: 删除 socket 文件 %s 失败: %r", self.path, exc)


# ---------------------------------------------------------------------------
#  降级实现 (平台不支持时顶替 LocalServer)
# ---------------------------------------------------------------------------
class NullServer:
    """不支持 AF_UNIX 的平台上的空 server。

    存在的意义: agent/main.py 的启动流程要求 ipc 组件有 start/stop/push,
    给一个"什么都不做但接口齐全"的对象, 启动就不会被记成组件失败, 日志里
    也只留一条说明原因的 WARNING —— 比抛异常或让 main.py 特判 Windows 干净。

    (Windows 上 Agent 本来就只是开发环境, 真机是 Linux 板子。)
    """

    def __init__(
        self,
        reason: str = "",
        logger: Optional[logging.Logger] = None,
        on_client_connect: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.reason = reason
        self.log = logger or logging.getLogger(__name__)
        self.path = SOCKET_PATH
        self.pushed = 0
        #: 与 LocalServer 同名字段: 空实现也是"接口齐全"的一部分（永远不会被调用,
        #  因为这里根本不会有客户端连上来）。
        self.on_client_connect = on_client_connect

    def __repr__(self) -> str:
        return "<NullServer (本平台不支持 AF_UNIX)>"

    @property
    def is_running(self) -> bool:
        return False

    @property
    def client_count(self) -> int:
        return 0

    def on_command(self, callback: Optional[Callable[[str, dict], Any]]):
        return callback

    async def start(self) -> None:
        self.log.warning("ipc: 跳过 Unix socket server —— %s", self.reason or "平台不支持")

    async def stop(self) -> None:
        return None

    async def push(self, topic: str, data: dict) -> int:
        self.pushed += 1
        self.log.debug("ipc: (空实现) 丢弃推送 %s", topic)
        return 0
