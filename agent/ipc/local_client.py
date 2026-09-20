# ============================================================================
#  agent/ipc/local_client.py — Agent ⇄ GUI 协议的 Python 客户端
#
#  ⚠ 这个 client **只用于测试/联调**, 生产环境的 GUI 是 C++ (Qt5, 见
#    docs/ipc-protocol.md 第 8 节)。所以它的目标不是性能, 而是"够用 + 读得懂":
#    没有连接池、没有批量发送、没有零拷贝 —— 就是一个小而直白的 asyncio client。
#
#  用途
#  ---------------------------------------------------------------------------
#  · 单测/联调时顶替 C++ GUI: `await client.connect()` -> `push` 收到的消息,
#    `send_command()` 发命令。
#  · 排障: 板端跑起来后想手动确认 Agent 在推什么, 写三行 Python 就能看。
#
#  它和 server 的关系 (方向和职责都别搞反)
#  ---------------------------------------------------------------------------
#      Agent  LocalServer   监听, 主动 push(topic, data)
#      GUI    LocalClient   连接, 收 on_message(topic, data), 发 send_command()
#
#  刻意**不做**的事 (任务约定)
#  ---------------------------------------------------------------------------
#  · 不自动重连: 断了就是断了, connect() 抛错。测试里要的就是"立刻知道断了",
#    自动重连会让失败的用例变得难查。
#  · 不做发送队列/背压: send_command() 直接 write+drain。调用方是测试代码,
#    没有"被慢 GUI 拖死"的场景。
#  · 不做心跳/保活: 本地 Unix socket 没有中间设备会静默掐链路。
#
#  平台
#  ---------------------------------------------------------------------------
#  ⚠ 和 server 一样: Windows 的 CPython 没有 asyncio.open_unix_connection,
#    只能在 Linux/macOS 上真正连。CLIENT_SUPPORTED 是给调用方判断用的。
# ============================================================================

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from typing import Any, Callable, List, Optional

from .protocol import (
    MAX_LINE_BYTES,
    MESSAGE_SEPARATOR,
    SOCKET_PATH,
    IpcProtocolError,
    decode,
    encode,
)

__all__ = [
    "CLIENT_SUPPORTED",
    "IpcClientError",
    "LocalClient",
]


#: 当前平台是否支持 asyncio 的 Unix socket 客户端。
#: Windows 上是 False (没有 asyncio.open_unix_connection)。
CLIENT_SUPPORTED = bool(hasattr(asyncio, "open_unix_connection"))


class IpcClientError(RuntimeError):
    """IPC client 的错误 (平台不支持 / 连不上 / 没 connect 就发)。

    继承 RuntimeError 而不是 ValueError: 和 IpcServerError 对称 —— 这些是运行
    环境/调用时序问题, 不是"参数写错了"。
    """


class LocalClient:
    """连到 Agent 那条 Unix socket 的最小客户端。

    典型用法::

        client = LocalClient("/tmp/agent.sock")
        client.on_message(lambda topic, data: print(topic, data))
        await client.connect()
        await client.send_command("chat_input", {"text": "你好"})
        ...
        await client.close()

    收到消息靠 on_message 注册的回调; 发命令用 send_command()。
    所有回调都在**同一个事件循环**里被调用 (就是 connect 时那个)。
    """

    #: close() 时等发送缓冲冲刷的上限 (秒)。和 server 侧同理: 冲刷不动就硬断,
    #: 不能让 close() 卡住 (对端不读数据时 wait_closed() 会一直等)。
    CLOSE_TIMEOUT = 1.0

    def __init__(
        self,
        path: str = SOCKET_PATH,
        *,
        max_line_bytes: int = MAX_LINE_BYTES,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        """
        @param path           Agent 监听的 socket 路径
        @param max_line_bytes 单条消息上限, 与 server 侧保持一致
        @param logger         注入 logger (测试用)
        """
        self.path = path
        self.max_line_bytes = max_line_bytes
        self.log = logger or logging.getLogger(__name__)

        self._reader: Optional["asyncio.StreamReader"] = None
        self._writer: Optional["asyncio.StreamWriter"] = None
        self._task: Optional["asyncio.Task[None]"] = None
        self._callbacks: List[Callable[[str, dict], Any]] = []
        self._connected = False
        self._closing = False        # 读循环该不该停
        self._closed = False         # close() 是否已经跑过

        #: 累计计数 (排障与测试断言用)
        self.received = 0        # 收到的合法消息条数
        self.dropped = 0         # 丢弃的非法消息条数
        self.sent = 0            # 成功发出的命令条数

    # ---- 只读状态 ----
    @property
    def is_connected(self) -> bool:
        """连接是否还活着 (对端断开后, 读循环退出会把它置回 False)。"""
        return self._connected

    def __repr__(self) -> str:
        return "<LocalClient path=%r connected=%s sent=%d received=%d>" % (
            self.path,
            self._connected,
            self.sent,
            self.received,
        )

    # ---- 回调注册 ----
    def on_message(self, callback: Optional[Callable[[str, dict], Any]]):
        """注册收消息的回调: callback(topic: str, data: dict)。

        @param callback 普通函数或协程函数都行; 传 None 表示清空已注册的
        @return callback 本身 (方便链式/当装饰器用)

        @note 支持多个, 按注册顺序依次等待。回调抛异常只记 WARNING, 连接保持
              —— 和 server 侧同一套策略。
        """
        if callback is None:
            self._callbacks = []
            return None
        if not callable(callback):
            raise IpcClientError(
                "on_message 需要可调用对象, 得到 %s" % type(callback).__name__
            )
        self._callbacks.append(callback)
        return callback

    # ---- 连接 ----
    async def connect(self) -> None:
        """连上 Agent 并开始收消息。重复调用是幂等的。

        @raise IpcClientError 平台不支持 / 连不上 (文件不存在、没人监听……)
        """
        if self._connected:
            self.log.debug("ipc client: 已经连上了, 忽略重复 connect()")
            return

        if not CLIENT_SUPPORTED:
            raise IpcClientError(
                "当前平台不支持 AF_UNIX (Windows 的 CPython 没有 "
                "asyncio.open_unix_connection), IPC client 只能在 Linux/macOS 上连"
            )

        try:
            reader, writer = await asyncio.open_unix_connection(
                path=self.path,
                # 和 server 侧同样的理由: 默认 limit 只有 64 KiB, 而协议单条上限
                # 是 1 MiB, 不显式放大就会把正常的大消息当成超长。
                limit=self.max_line_bytes + len(MESSAGE_SEPARATOR),
            )
        except (OSError, ValueError) as exc:
            # 文件不存在 / ECONNREFUSED / 权限不够都在这里
            raise IpcClientError("连接 %s 失败: %s" % (self.path, exc)) from exc

        self._reader = reader
        self._writer = writer
        self._connected = True
        self._closing = False
        self._closed = False         # 允许 close() 之后手动再 connect()
        self._task = asyncio.create_task(self._read_loop(), name="ipc-client-read")
        self.log.info("ipc client 已连接: %s", self.path)

    async def close(self) -> None:
        """断开并收尾。幂等, 没连过也能调。

        @note 本 client 不做重连: close() 之后再想用就重新 connect()。
        """
        if self._closed:
            return
        self._closed = True
        self._closing = True
        self._connected = False

        task, self._task = self._task, None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

        writer, self._writer = self._writer, None
        self._reader = None
        if writer is None:
            return

        with contextlib.suppress(Exception):
            writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=self.CLOSE_TIMEOUT)
        except asyncio.CancelledError:
            self._abort_transport(writer)
            raise
        except Exception as exc:        # noqa: BLE001 - 含 TimeoutError
            self.log.debug("ipc client: 关闭连接超时/失败, 直接硬断: %r", exc)
            self._abort_transport(writer)

        self.log.info("ipc client 已断开: %s", self.path)

    # ---- 发命令 ----
    async def send_command(self, action: str, payload: Optional[dict] = None) -> None:
        """发一条命令给 Agent: {"topic": action, "data": payload}。

        @param action  命令名 (COMMAND_*; 不做白名单校验 —— 这是测试工具,
                       发个未知命令去看看 Agent 怎么处理也是常见需求)
        @param payload 参数; None 当成 {} (协议要求 data 必须是 object)
        @raise IpcClientError  还没 connect() (或已经断了)
        @raise IpcProtocolError action/payload 不合法 (调用方的 bug, 立刻报)

        @note 直接 write + drain, 没有发送队列: 调用方是测试代码, 不需要背压。
        """
        if not self._connected or self._writer is None:
            raise IpcClientError(
                "还没 connect() 就 send_command() (本 client 不做自动重连)"
            )

        line = encode(action, {} if payload is None else payload)
        self._writer.write(line)
        await self._writer.drain()
        self.sent += 1
        self.log.debug("ipc client: 已发送 %s %r", action, payload)

    # ---- 内部: 收 ----
    async def _read_loop(self) -> None:
        """一直读到对端断开 / 出错 / close()。"""
        try:
            while not self._closing:
                line = await self._read_line()
                if line is None:
                    break
                await self._handle_line(line)
        except asyncio.CancelledError:
            raise
        except (ConnectionResetError, BrokenPipeError) as exc:
            self.log.debug("ipc client: 连接被对端重置: %r", exc)
        except Exception as exc:        # noqa: BLE001
            self.log.warning("ipc client: 读连接出错: %r", exc)
            self.log.debug("ipc client: 读连接 traceback", exc_info=True)
        else:
            self.log.info("ipc client: 对端已断开 (%s)", self.path)
        finally:
            self._connected = False

    async def _read_line(self) -> Optional[bytes]:
        """读一条以 \\n 结束的原始消息。

        @return 原始字节 (含换行); EOF 返回 None
        @raise IpcClientError 对端发了超过 max_line_bytes 都没换行的东西
        """
        try:
            return await self._reader.readuntil(MESSAGE_SEPARATOR)
        except asyncio.IncompleteReadError as exc:
            # 对端关了。partial 非空说明最后一条是残的 —— 测试工具不值得像 server
            # 那样写一套"丢掉残行再继续"的逻辑, 记一条日志就够。
            if exc.partial:
                self.dropped += 1
                self.log.warning(
                    "ipc client: 对端在消息中途断开, 丢弃 %d 字节残缺数据",
                    len(exc.partial),
                )
            return None
        except asyncio.LimitOverrunError as exc:
            self.dropped += 1
            raise IpcClientError(
                "对端发来的消息超过 %d 字节仍无换行, 判定为协议违规并断开"
                % self.max_line_bytes
            ) from exc

    async def _handle_line(self, raw: bytes) -> None:
        """解一条消息交给回调; 不合法就丢弃 (连接继续用)。"""
        try:
            topic, data = decode(raw)
        except IpcProtocolError as exc:
            self.dropped += 1
            self.log.warning("ipc client: 丢弃一条非法消息: %s", exc)
            return
        self.received += 1
        await self._deliver(topic, data)

    async def _deliver(self, topic: str, data: dict) -> None:
        """调已注册的回调。

        @note 和 server 侧一致: **按顺序 await**。消息顺序是测试要看的东西,
              挂成 task 并发跑会让"先推的先到"变成随机。回调抛异常只记 WARNING
              (连接保持)。
        """
        for callback in list(self._callbacks):
            try:
                result = callback(topic, data)
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception as exc:    # noqa: BLE001
                self.log.warning(
                    "ipc client: 处理 %s 的回调抛错 (已忽略): %r", topic, exc
                )
                self.log.debug("ipc client: 回调 traceback", exc_info=True)

    @staticmethod
    def _abort_transport(writer: "asyncio.StreamWriter") -> None:
        """丢掉发送缓冲, 立刻断开 (close() 冲刷不动时的兜底)。"""
        transport = getattr(writer, "transport", None)
        abort = getattr(transport, "abort", None)
        if callable(abort):
            with contextlib.suppress(Exception):
                abort()
