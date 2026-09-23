#!/usr/bin/env python3
"""
tests/test_ipc.py — LocalServer ⇄ LocalClient 端到端 (pytest + pytest-asyncio)

运行:
    pytest tests/test_ipc.py -v

为什么用 pytest 而不是本仓库其他测试用的 unittest:
    这个文件要验的是"两个 asyncio 对象隔着一条真 socket 说话", 每个用例都需要
    自己的事件循环 + 自己的临时目录。pytest-asyncio 的 async fixture 和 tmp_path
    正好就是干这个的, 用 unittest 写要自己搭一堆脚手架。

⚠ 这个 client 只是**测试替身**: 生产环境里 GUI 是 C++ (Qt5)。所以这里的用例
  只验协议行为, 不验性能, 也不碰 C++ 侧。

覆盖 (任务要求的 5 条):
  1. server 起 -> client 连 -> server push -> client 收到
  2. client 发命令 -> server 收到并回调
  3. 多个 client 同时连 -> push 广播给所有 client
  4. 非法 JSON 被 server 丢弃且不崩溃
  5. client 断开 -> server 不崩溃, 还能继续服务新连接

约定:
  · 每个用例一个独立的 socket 路径 (tmp_path fixture), 并行跑不会撞
  · 用例结束由 fixture 收尾: stop()/close() + 兜底删掉 socket 文件
  · 不做重连、不做压力测试 (任务明确排除)

平台: Windows 的 CPython 没有 AF_UNIX, 建不了 Unix socket —— 所有涉及 socket 的
      用例会 skip (非 socket 的用例照常跑)。真实验证在 WSL / 板端做。
"""

import asyncio
import contextlib
import logging
import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.ipc import (  # noqa: E402
    COMMAND_CHAT_INPUT,
    COMMAND_QUERY_SCHEDULE,
    TOPIC_STATUS,
    UNIX_SOCKET_SUPPORTED,
    IpcClientError,
    IpcProtocolError,
    LocalClient,
    LocalServer,
)

#: 所有需要真 socket 的用例都挂这个 —— Windows 上直接 skip, 不假装通过
needs_unix = pytest.mark.skipif(
    not UNIX_SOCKET_SUPPORTED,
    reason="本平台没有 AF_UNIX (Windows 的 CPython 不支持), 建不了 Unix socket",
)

# 用例里会**故意**灌坏数据, 这些 WARNING 是预期内的, 别刷屏
logging.getLogger("agent.ipc").setLevel(logging.CRITICAL)
logging.getLogger("agent.ipc.local_server").setLevel(logging.CRITICAL)
logging.getLogger("agent.ipc.local_client").setLevel(logging.CRITICAL)


# ===========================================================================
#  fixture / 工具
# ===========================================================================
@pytest.fixture
def sock_path(tmp_path):
    """每个用例一个独立的 socket 路径。

    tmp_path 是 pytest 给每个用例准备的唯一临时目录, 所以并行 (-n / xdist) 跑
    也不会两个用例抢同一个 socket 文件。
    """
    return str(tmp_path / "agent.sock")


@pytest_asyncio.fixture
async def server(sock_path):
    """起一个 LocalServer, 用例结束停掉并确保 socket 文件被清掉。"""
    srv = LocalServer(sock_path)
    await srv.start()
    try:
        yield srv
    finally:
        await srv.stop()
        # stop() 正常会删掉它; 这里兜一层, 保证任何失败路径下都不留垃圾文件
        if os.path.exists(sock_path):
            os.unlink(sock_path)


async def wait_for(predicate, timeout=2.0, what="条件"):
    """等某个状态成立 (等 accept / 等回调)。超时直接断言失败, 不会挂住。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        # 用真的 sleep 让出时间给 socket I/O (sleep(0) 会空转得很难看)
        await asyncio.sleep(0.005)
    raise AssertionError("%s 在 %.1fs 内没有成立" % (what, timeout))


@contextlib.asynccontextmanager
async def connected(sock_path, server=None, clients_after=1, **kwargs):
    """连上一个 LocalClient, 用完自动 close()。

    @param clients_after 连上之后 server 侧**应该**看到的连接数。必须显式给:
           accept 是异步的, 连上不等于 server 已经登记, 不等一下就 push 会推给
           空气 (真实 GUI 是先连上再等推送, 不存在这个问题)。
    """
    client = LocalClient(sock_path, **kwargs)
    await client.connect()
    if server is not None:
        await wait_for(
            lambda: server.client_count >= clients_after,
            what="server accept 到 %d 个连接" % clients_after,
        )
    try:
        yield client
    finally:
        await client.close()


def collect(client):
    """把 client 收到的消息塞进队列, 用例里 await queue.get() 取 (比 sleep 靠谱)。"""
    queue = asyncio.Queue()
    client.on_message(lambda topic, data: queue.put_nowait((topic, data)))
    return queue


# ===========================================================================
#  不需要 socket 的部分 (Windows 上也会跑)
# ===========================================================================
@pytest.mark.asyncio
async def test_send_command_before_connect_raises(sock_path):
    client = LocalClient(sock_path)
    with pytest.raises(IpcClientError, match="connect"):
        await client.send_command(COMMAND_CHAT_INPUT, {"text": "x"})


@pytest.mark.asyncio
async def test_close_without_connect_is_noop(sock_path):
    client = LocalClient(sock_path)
    await client.close()
    await client.close()          # 幂等
    assert client.is_connected is False
    assert client.sent == 0


@pytest.mark.asyncio
async def test_on_message_registers_and_clears(sock_path):
    client = LocalClient(sock_path)

    def handler(topic, data):
        return None

    assert client.on_message(handler) is handler
    assert client._callbacks == [handler]
    assert client.on_message(None) is None
    assert client._callbacks == []


@pytest.mark.asyncio
async def test_on_message_rejects_non_callable(sock_path):
    client = LocalClient(sock_path)
    with pytest.raises(IpcClientError):
        client.on_message("不是函数")


def test_defaults_and_repr(sock_path):
    client = LocalClient()
    assert client.path == "/tmp/agent.sock"
    assert LocalClient(sock_path).path == sock_path
    assert "connected=False" in repr(LocalClient(sock_path))
    assert client.is_connected is False
    assert (client.received, client.dropped, client.sent) == (0, 0, 0)


# ===========================================================================
#  1) server push -> client 收到
# ===========================================================================
@needs_unix
@pytest.mark.asyncio
async def test_server_push_reaches_client(server, sock_path):
    async with connected(sock_path, server, 1) as client:
        queue = collect(client)

        sent = {"mode": "STUDY", "connected": True}
        assert await server.push(TOPIC_STATUS, sent) == 1

        topic, data = await asyncio.wait_for(queue.get(), 2)
        assert topic == TOPIC_STATUS
        assert data == sent
        assert client.received == 1
        assert client.is_connected is True


@needs_unix
@pytest.mark.asyncio
async def test_push_without_client_returns_zero(server, sock_path):
    assert await server.push(TOPIC_STATUS, {"mode": "IDLE"}) == 0


@needs_unix
@pytest.mark.asyncio
async def test_push_keeps_order(server, sock_path):
    async with connected(sock_path, server, 1) as client:
        queue = collect(client)
        for i in range(5):
            await server.push(TOPIC_STATUS, {"n": i})

        got = []
        for _ in range(5):
            _, data = await asyncio.wait_for(queue.get(), 2)
            got.append(data["n"])
        assert got == [0, 1, 2, 3, 4]


@needs_unix
@pytest.mark.asyncio
async def test_unicode_survives_the_socket(server, sock_path):
    async with connected(sock_path, server, 1) as client:
        queue = collect(client)
        await server.push("llm", {"text": "你好, 世界 🌏"})
        _, data = await asyncio.wait_for(queue.get(), 2)
        assert data["text"] == "你好, 世界 🌏"


@needs_unix
@pytest.mark.asyncio
async def test_async_callback_is_awaited(server, sock_path):
    seen = []

    async def handler(topic, data):
        await asyncio.sleep(0)
        seen.append((topic, data))

    async with connected(sock_path, server, 1) as client:
        client.on_message(handler)
        await server.push(TOPIC_STATUS, {"mode": "IDLE"})
        await wait_for(lambda: seen, what="异步回调")
        assert seen[0][0] == TOPIC_STATUS


# ===========================================================================
#  2) client 发命令 -> server 收到并回调
# ===========================================================================
@needs_unix
@pytest.mark.asyncio
async def test_client_command_reaches_server(server, sock_path):
    seen = []
    server.on_command(lambda action, payload: seen.append((action, payload)))

    async with connected(sock_path, server, 1) as client:
        await client.send_command(COMMAND_CHAT_INPUT, {"text": "开灯"})

        await wait_for(lambda: seen, what="server 的命令回调")
        assert seen == [(COMMAND_CHAT_INPUT, {"text": "开灯"})]
        assert client.sent == 1
        assert server.received == 1


@needs_unix
@pytest.mark.asyncio
async def test_command_without_payload_becomes_empty_object(server, sock_path):
    seen = []
    server.on_command(lambda action, payload: seen.append((action, payload)))

    async with connected(sock_path, server, 1) as client:
        # payload 省略; 注意 action 必须是 protocol.COMMANDS 里的 ——
        # server 只分发已知命令 (未知 topic 一律忽略, 见下面那个用例)
        await client.send_command(COMMAND_QUERY_SCHEDULE)
        await wait_for(lambda: seen, what="无参数命令")
        assert seen == [(COMMAND_QUERY_SCHEDULE, {})]   # 协议要求 data 是 object


@needs_unix
@pytest.mark.asyncio
async def test_async_command_callback(server, sock_path):
    seen = []

    async def handler(action, payload):
        await asyncio.sleep(0)
        seen.append(action)

    server.on_command(handler)
    async with connected(sock_path, server, 1) as client:
        await client.send_command("switch_mode", {"value": "STUDY"})
        await wait_for(lambda: seen, what="异步命令回调")
        assert seen == ["switch_mode"]


@needs_unix
@pytest.mark.asyncio
async def test_send_command_rejects_bad_payload(server, sock_path):
    async with connected(sock_path, server, 1) as client:
        with pytest.raises(IpcProtocolError):
            await client.send_command(COMMAND_CHAT_INPUT, ["不是 object"])
        assert client.sent == 0, "编码失败就不该发出去"


# ===========================================================================
#  3) 多个 client -> 广播
# ===========================================================================
@needs_unix
@pytest.mark.asyncio
async def test_push_broadcasts_to_all_clients(server, sock_path):
    async with connected(sock_path, server, 1) as first:
        async with connected(sock_path, server, 2) as second:
            queue_a = collect(first)
            queue_b = collect(second)

            assert await server.push("music", {"title": "歌"}) == 2

            for queue in (queue_a, queue_b):
                topic, data = await asyncio.wait_for(queue.get(), 2)
                assert (topic, data) == ("music", {"title": "歌"})
            assert server.client_count == 2


@needs_unix
@pytest.mark.asyncio
async def test_commands_from_both_clients_arrive(server, sock_path):
    seen = []
    server.on_command(lambda action, payload: seen.append(payload["who"]))

    async with connected(sock_path, server, 1) as first:
        async with connected(sock_path, server, 2) as second:
            # 用真命令 + 额外字段带标识: 回调收到的是整份 payload
            await first.send_command(COMMAND_CHAT_INPUT, {"who": "first"})
            await second.send_command(COMMAND_CHAT_INPUT, {"who": "second"})

            await wait_for(lambda: len(seen) == 2, what="两个 client 的命令")
            assert sorted(seen) == ["first", "second"]


# ===========================================================================
#  4) 非法 JSON -> server 丢弃, 不崩溃
# ===========================================================================
@needs_unix
@pytest.mark.asyncio
async def test_invalid_json_is_dropped_and_server_survives(server, sock_path):
    seen = []
    server.on_command(lambda action, payload: seen.append(payload))

    async with connected(sock_path, server, 1) as client:
        # 故意绕过 client 的编码器, 直接往 socket 里灌坏数据 ——
        # 要验的正是"对端乱发时 server 不会崩、也不会把连接丢掉"
        client._writer.write(b"{not json}\n")
        client._writer.write(b"[]\n")            # 合法 JSON 但不是 object
        client._writer.write(b"\n")              # 空行
        await client._writer.drain()

        # 坏消息后面还能正常干活
        await client.send_command(COMMAND_CHAT_INPUT, {"text": "还在"})
        await wait_for(lambda: seen, what="坏消息之后的正常命令")

        assert seen == [{"text": "还在"}]
        assert server.dropped == 3
        assert server.client_count == 1, "坏消息不该把连接断掉"

        # server 仍然健康: 反向还能推
        queue = collect(client)
        assert await server.push(TOPIC_STATUS, {"mode": "IDLE"}) == 1
        topic, _ = await asyncio.wait_for(queue.get(), 2)
        assert topic == TOPIC_STATUS


@needs_unix
@pytest.mark.asyncio
async def test_unknown_command_does_not_crash_server(server, sock_path):
    seen = []
    server.on_command(lambda action, payload: seen.append(action))

    async with connected(sock_path, server, 1) as client:
        await client.send_command("no_such_command", {"x": 1})
        # 未知 topic 会被 server 忽略 (向前兼容), 不会回调、也不会断连接
        await asyncio.sleep(0.05)
        assert seen == []
        assert server.client_count == 1

        await client.send_command(COMMAND_CHAT_INPUT, {"text": "正常的"})
        await wait_for(lambda: seen, what="未知命令之后仍能正常工作")
        assert seen == [COMMAND_CHAT_INPUT]


# ===========================================================================
#  5) client 断开 -> server 不崩溃
# ===========================================================================
@needs_unix
@pytest.mark.asyncio
async def test_client_disconnect_does_not_break_server(server, sock_path):
    async with connected(sock_path, server, 1):
        pass                                   # 出 with 就 close() 了

    await wait_for(lambda: server.client_count == 0, what="server 感知到断开")
    assert server.is_running is True
    assert await server.push(TOPIC_STATUS, {"mode": "IDLE"}) == 0   # 没人连着, 不是错误

    # 断开之后还能接新连接, 并且照常推送
    async with connected(sock_path, server, 1) as again:
        queue = collect(again)
        assert await server.push(TOPIC_STATUS, {"mode": "GAME"}) == 1
        _, data = await asyncio.wait_for(queue.get(), 2)
        assert data == {"mode": "GAME"}


@needs_unix
@pytest.mark.asyncio
async def test_client_disconnect_mid_message_does_not_break_server(server, sock_path):
    client = LocalClient(sock_path)
    await client.connect()
    await wait_for(lambda: server.client_count == 1, what="accept")

    # 半条消息之后直接掐断 —— server 读到 EOF 上的残包, 只能丢弃, 不能崩
    client._writer.write(b'{"topic":"chat_input","data":{')
    await client._writer.drain()
    await client.close()

    await wait_for(lambda: server.client_count == 0, what="server 感知到断开")
    assert server.is_running is True
    assert server.dropped >= 1


# ===========================================================================
#  收尾: socket 文件必须被清掉
# ===========================================================================
@needs_unix
@pytest.mark.asyncio
async def test_stop_removes_socket_file(server, sock_path):
    assert os.path.exists(sock_path)
    await server.stop()
    assert not os.path.exists(sock_path)


@needs_unix
@pytest.mark.asyncio
async def test_connect_to_missing_socket_raises(tmp_path):
    client = LocalClient(str(tmp_path / "nothing-here.sock"))
    with pytest.raises(IpcClientError, match="失败"):
        await client.connect()
    assert client.is_connected is False


@needs_unix
@pytest.mark.asyncio
async def test_connect_twice_and_close_twice_are_idempotent(server, sock_path):
    client = LocalClient(sock_path)
    await client.connect()
    await client.connect()                     # 第二次应该什么都不做
    await wait_for(lambda: server.client_count == 1, what="accept")
    assert server.client_count == 1

    await client.close()
    await client.close()
    assert client.is_connected is False
    await wait_for(lambda: server.client_count == 0, what="server 感知到断开")


if __name__ == "__main__":
    # 让 `python tests/test_ipc.py` 也能跑 (和仓库其他测试文件保持一致的手感)
    sys.exit(pytest.main([__file__, "-v"]))
