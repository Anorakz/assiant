# ============================================================================
#  agent/ipc/server.py — Agent 侧 IPC server (Unix domain socket)
#
#  职责 (只管收发, 不管业务)
#  ---------------------------------------------------------------------------
#      · 监听 /tmp/agent.sock, 接受 GUI 连接
#      · 收: 按 '\n' 切行 -> decode_command() -> on_command(action, payload)
#      · 发: push(topic, data) 给所有已连接客户端 (用 protocol.encode 编码)
#
#  线格式 (两个方向不一样, 见下)
#  ---------------------------------------------------------------------------
#      GUI -> Agent  {"action": str, "payload": object}
#          阶段 2 任务约定的 LocalClient.sendCommand 格式。
#          ⚠ 这与 docs/ipc-protocol.md §4 的 {topic,data,timestamp} 信封不同;
#            等两边统一后, decode_command() 应换成 protocol.decode()。
#
#      Agent -> GUI  {"topic": str, "data": object, "timestamp": number}
#          protocol.encode(), 与 docs 一致 —— LocalClient 只认这一种。
#
#  错误处理 (协议 §6: 一条坏消息只影响它自己)
#  ---------------------------------------------------------------------------
#      非法 JSON / 非 object / 缺 action / payload 不是 object / 超长行
#          -> 丢弃该行 + 记 warning, 不断开连接
#      对端关闭 (EOF) -> 关掉这条连接, 继续 accept 下一个
#
#  与 agent/main.py 的关系
#  ---------------------------------------------------------------------------
#  main.py 会在 agent.ipc 里找 build_ipc()/IPCServer 自动接入。**本文件故意不从
#  agent/ipc/__init__.py 导出它们** —— main.py 的 hook 只传 (bus, config), 而
#  switch_mode 需要状态机、status 需要状态源, 接线是后续任务; 现在导出会让
#  main.py 起一个半残的 IPC。所以本任务只提供"单独起 IPC server"的能力 (联调用)。
#
#  单独运行 (联调 / 手工调试)
#  ---------------------------------------------------------------------------
#      python3 -m agent.ipc.server [--socket /tmp/agent.sock] [--control-stdin]
#
#      --control-stdin 从 stdin 读控制命令 (联调脚本靠它触发 push):
#          push status [MODE]   push 一条 status (可顺带切 mode)
#          push llm <文本>        push 一条 llm
#          quit                  退出
# ============================================================================
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Set, Tuple

# 允许 `python3 agent/ipc/server.py` 直接跑 (此时包根不在 sys.path 上)
if __package__ in (None, ""):  # pragma: no cover - 只在直接执行时走
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent.ipc.protocol import (
    ENCODING,
    MAX_LINE_BYTES,
    MODES,
    MODE_STUDY,
    SOCKET_PATH,
    TOPIC_LLM,
    TOPIC_STATUS,
    InvalidMessageError,
    IpcProtocolError,
    MalformedJsonError,
    encode,
)

__all__ = ["IPCServer", "decode_command", "CommandHandler"]

LOGGER_NAME = "agent.ipc.server"

#: on_command 回调: (action, payload) -> None。允许是 sync 或 async 函数。
CommandHandler = Callable[[str, Dict[str, Any]], Any]


# ---------------------------------------------------------------------------
#  解码: GUI -> Agent 的命令
# ---------------------------------------------------------------------------
def decode_command(line: Any) -> Tuple[str, Dict[str, Any]]:
    """解一条 GUI 发来的命令, 返回 (action, payload)。

    @param line 一行字节 (结尾的 \\n 可有可无, 也容忍 \\r\\n)
    @raise MalformedJsonError   不是合法 JSON / 不是 UTF-8
    @raise InvalidMessageError  结构不合法 (不是 object / 缺 action / payload 不是 object)
    @raise IpcProtocolError     超过 MAX_LINE_BYTES

    @note 调用方的标准动作是 catch -> 记日志 -> 丢弃该行, 连接继续用。
    @note 线格式与 docs/ipc-protocol.md §4 的信封不同, 见模块头的说明。
    """
    if isinstance(line, str):
        raw = line.encode(ENCODING)
    elif isinstance(line, (bytes, bytearray, memoryview)):
        raw = bytes(line)
    else:
        raise InvalidMessageError("line must be bytes or str, got %s" % type(line).__name__)

    # 只剥结尾: 行首空白属于内容, 会让 JSON 解析失败 (那是应该报错的)
    raw = raw.rstrip(b"\r\n")

    if not raw:
        raise InvalidMessageError("empty command")

    if len(raw) > MAX_LINE_BYTES:
        raise IpcProtocolError(
            "command too large: %d bytes > %d" % (len(raw), MAX_LINE_BYTES)
        )

    try:
        text = raw.decode(ENCODING)
    except UnicodeDecodeError as exc:
        raise MalformedJsonError("command is not valid %s: %r" % (ENCODING, exc)) from exc

    try:
        envelope = json.loads(text)
    except ValueError as exc:
        raise MalformedJsonError("invalid JSON: %r" % (exc,)) from exc

    if not isinstance(envelope, dict):
        raise InvalidMessageError(
            "command must be a JSON object, got %s" % type(envelope).__name__
        )

    # 未知字段直接忽略 (协议没有版本号, 加字段靠忽略)
    action = envelope.get("action")
    if not isinstance(action, str) or not action.strip():
        raise InvalidMessageError("missing or invalid 'action' field: %r" % (action,))

    if "payload" not in envelope:
        raise InvalidMessageError("missing 'payload' field (use {} for no arguments)")
    payload = envelope["payload"]
    if not isinstance(payload, dict):
        raise InvalidMessageError(
            "'payload' must be a JSON object, got %s" % type(payload).__name__
        )

    return action, payload


# ---------------------------------------------------------------------------
#  Server
# ---------------------------------------------------------------------------
class IPCServer:
    """Agent 侧 IPC server。

    只管 socket 收发与协议编解码; 命令的业务语义由 on_command 注入 ——
    这样它既能被联调脚本单独跑, 也能被将来的 Runtime 接上状态机。
    """

    def __init__(
        self,
        socket_path: str = SOCKET_PATH,
        on_command: Optional[CommandHandler] = None,
        log: Optional[logging.Logger] = None,
    ) -> None:
        """
        @param socket_path  监听路径 (默认 /tmp/agent.sock)
        @param on_command   (action, payload) 回调; None 表示只记日志
        @param log          日志器; None 时用 agent.ipc.server
        """
        self.socket_path = socket_path
        self.on_command = on_command
        self.log = log or logging.getLogger(LOGGER_NAME)

        self._server: Optional[asyncio.AbstractServer] = None
        self._clients: Set[asyncio.StreamWriter] = set()
        self._send_lock = asyncio.Lock()
        self.stats = {"connections": 0, "commands": 0, "dropped": 0, "pushed": 0, "push_failed": 0}

    # ------------------------------------------------------------ 生命周期 --
    @property
    def client_count(self) -> int:
        """当前连着的 GUI 数量 (没有就是 0, push 会静默丢掉)。"""
        return len(self._clients)

    async def start(self) -> None:
        """bind + listen (不阻塞; accept 由事件循环驱动)。"""
        if self._server is not None:
            raise RuntimeError("IPC server 已经启动过了")

        # 崩溃残留的 socket 文件会挡住 bind。这个路径归 Agent 管
        # (协议 §1: Agent 退出时自己删), 所以这里直接清掉。
        if os.path.exists(self.socket_path):
            self.log.debug("清理残留的 socket 文件: %s", self.socket_path)
            os.unlink(self.socket_path)

        self._server = await asyncio.start_unix_server(
            self._handle_client, path=self.socket_path
        )
        # 同机 socket 靠文件权限隔离 (协议 §9): 只有本用户能连
        os.chmod(self.socket_path, 0o600)
        self.log.info("IPC 监听 %s", self.socket_path)

    async def stop(self) -> None:
        """关监听 -> 关所有连接 -> 删 socket 文件。可重复调用。"""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        for writer in list(self._clients):
            writer.close()
        self._clients.clear()

        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

        self.log.info("IPC 已停止 (收 %d 条命令, 丢 %d 条, push %d 条)",
                      self.stats["commands"], self.stats["dropped"], self.stats["pushed"])

    # ------------------------------------------------------------ 发送 --
    async def push(self, topic: str, data: Dict[str, Any]) -> int:
        """给所有已连接客户端发一条消息。

        @return 成功发出的连接数 (没人连着就是 0 —— 不缓存, 发的时候没有就是没有)
        @raise IpcProtocolError topic/data 不合法 (调用方写错了, 不该吞)
        """
        line = encode(topic, data)   # 结尾自带 \n
        sent = 0
        dead = []

        async with self._send_lock:
            for writer in list(self._clients):
                try:
                    writer.write(line)
                    await writer.drain()
                    sent += 1
                except (ConnectionError, OSError) as exc:
                    self.log.warning("push 到某条连接失败: %r", exc)
                    dead.append(writer)
            for writer in dead:
                self._clients.discard(writer)

        self.stats["pushed"] += sent
        self.stats["push_failed"] += len(dead)
        return sent

    # ------------------------------------------------------------ 接收 --
    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """一条 GUI 连接的生命周期 (按行读 -> 分发)。"""
        self._clients.add(writer)
        self.stats["connections"] += 1
        self.log.info("GUI 已连接 (当前 %d 个)", len(self._clients))

        buf = b""
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:          # EOF: 对端断开
                    break
                buf += chunk

                # 一直不发换行就会把内存吃光; 超过上限的"半行"直接丢
                if len(buf) > MAX_LINE_BYTES and b"\n" not in buf:
                    self.log.warning("丢弃超长未结束行: %d 字节", len(buf))
                    buf = b""
                    continue

                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    await self._dispatch(line)
        except (ConnectionError, OSError) as exc:
            self.log.warning("连接异常: %r", exc)
        finally:
            self._clients.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError, AttributeError):
                pass
            self.log.info("GUI 已断开 (当前 %d 个)", len(self._clients))

    async def _dispatch(self, line: bytes) -> None:
        """解一条命令并交给 on_command。坏行只丢自己。"""
        try:
            action, payload = decode_command(line)
        except IpcProtocolError as exc:
            self.stats["dropped"] += 1
            self.log.warning("丢弃非法命令: %r (%s)", line[:200], exc)
            return

        self.stats["commands"] += 1
        self.log.info("收到命令 action=%s payload=%s", action, payload)

        if self.on_command is None:
            return
        try:
            result = self.on_command(action, payload)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:  # noqa: BLE001 - 单条命令处理失败不该断开连接
            self.log.error("处理命令 %s 失败: %r", action, exc, exc_info=True)


# ---------------------------------------------------------------------------
#  单独运行 (联调 / 手工调试)
# ---------------------------------------------------------------------------
def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


async def _control_loop(
    server: IPCServer, stop_event: asyncio.Event, mode: str
) -> None:
    """从 stdin 读控制命令 (供联调脚本触发 push)。

    stdin 用后台线程读 (阻塞 IO 不适合直接塞进事件循环), 读到一行就
    call_soon_threadsafe 丢回事件循环处理。
    """
    queue: "asyncio.Queue[Optional[str]]" = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def _reader() -> None:
        try:
            for raw in sys.stdin:
                loop.call_soon_threadsafe(queue.put_nowait, raw)
        except (OSError, ValueError):  # pragma: no cover - 管道被关
            pass
        loop.call_soon_threadsafe(queue.put_nowait, None)   # EOF

    threading.Thread(target=_reader, name="ipc-control-stdin", daemon=True).start()

    while True:
        raw = await queue.get()
        if raw is None:
            print("[ipc] 控制通道 EOF (stdin 已关闭)", flush=True)
            return

        parts = raw.strip().split(None, 2)
        if not parts:
            continue
        cmd = parts[0].lower()

        if cmd == "push":
            target = parts[1].lower() if len(parts) > 1 else "status"
            if target == "status":
                if len(parts) > 2 and parts[2].upper() in MODES:
                    mode = parts[2].upper()
                n = await server.push(
                    TOPIC_STATUS, {"mode": mode, "connected": server.client_count > 0}
                )
                print("[ipc] push status(mode=%s) -> %d 个客户端" % (mode, n), flush=True)
            elif target == "llm":
                text = parts[2] if len(parts) > 2 else ""
                n = await server.push(TOPIC_LLM, {"text": text})
                print("[ipc] push llm -> %d 个客户端" % n, flush=True)
            else:
                print("[ipc] 不认识的 push 目标: %r" % target, flush=True)
        elif cmd in ("quit", "exit"):
            print("[ipc] 收到 quit, 退出", flush=True)
            stop_event.set()
            return
        else:
            print("[ipc] 不认识的控制命令: %r" % raw.strip(), flush=True)


async def _run(args: argparse.Namespace) -> int:
    server = IPCServer(socket_path=args.socket)
    await server.start()

    # 联调脚本靠这一行同步 (不要改格式)
    print("[ipc] LISTENING %s" % args.socket, flush=True)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):  # pragma: no cover
            pass

    control: Optional[asyncio.Task] = None
    if args.control_stdin:
        control = asyncio.create_task(
            _control_loop(server, stop_event, args.mode), name="ipc-control"
        )

    await stop_event.wait()

    if control is not None:
        control.cancel()
        try:
            await control
        except asyncio.CancelledError:
            pass

    await server.stop()
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent.ipc.server",
        description="Agent 侧 IPC server (Unix domain socket, 只起 IPC 不起其他组件)",
    )
    parser.add_argument("--socket", default=SOCKET_PATH,
                        help="监听路径 (默认 %s)" % SOCKET_PATH)
    parser.add_argument("--mode", default=MODE_STUDY, choices=list(MODES),
                        help="debug 模式下 status 推的 mode (默认 %s)" % MODE_STUDY)
    parser.add_argument("--control-stdin", action="store_true",
                        help="从 stdin 读控制命令: push status [MODE] / push llm <文本> / quit")
    parser.add_argument("--log-level", default="INFO",
                        help="日志级别 (默认 INFO)")
    args = parser.parse_args(argv)

    _setup_logging(args.log_level)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:  # pragma: no cover
        return 0


if __name__ == "__main__":
    sys.exit(main())
