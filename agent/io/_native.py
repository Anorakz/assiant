# ============================================================================
#  agent/io/_native.py — native (agent_native) 访问与"不阻塞 asyncio"的执行器
#
#  两个必须集中处理的问题
#  ---------------------------------------------------------------------------
#  1) **native 找不到时不要炸在 import 期**
#     agent_native 是交叉编译产物, 宿主机上没有。所以本包不在 import 时加载它,
#     而是"用到才找"(lazy), 找不到给出可操作的报错。这样:
#       · 单元测试可以注入 mock, 不需要真 .so
#       · 只 import agent.io 的模块不会因为缺 .so 而失败
#
#  2) **所有 native 调用都要跑在同一个线程上**
#     这不是性能问题, 是**正确性**问题。image_rb 是 SPSC
#     (单生产者单消费者)无锁环形缓冲, 约定"消费者永远是同一个线程"
#     (见 native/ring_buffer.h 的并发约定)。而 asyncio 默认的
#     loop.run_in_executor(None, ...) 用的是共享线程池 —— 同一个 reader 的两次
#     读取可能落在不同线程上, 那就直接违反了 SPSC 约定, 属于未定义行为。
#
#     所以这里给每个子系统一个**专属单线程执行器**: 该子系统的所有 native
#     调用都排到同一个线程上, 既满足 SPSC, 又不会阻塞事件循环。
#
#     (native 侧的读函数内部已经 py::gil_scoped_release 了, 所以读的时候
#      真正的 Python 解释器锁是放开的; 这里再包一层 executor 是为了不让
#      "取 GIL + 调 native" 这段卡住 asyncio 的事件循环。)
# ============================================================================

from __future__ import annotations

import atexit
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

__all__ = [
    "NATIVE_MODULE_NAME",
    "get_native",
    "set_native",
    "reset_native",
    "run_native",
    "executor_for",
    "reset_executors",
]

#: 交叉编译出来的扩展模块名 (native/binding.cpp 里的 PYBIND11_MODULE)
NATIVE_MODULE_NAME = "agent_native"

#: 注入的 native 模块 (测试用); None 表示"按需 import"
_injected_native: Any = None


# ---------------------------------------------------------------------------
#  native 模块解析
# ---------------------------------------------------------------------------
def set_native(module: Any) -> None:
    """注入一个 native 模块替身 (测试用)。

    传 None 等价于恢复"按需 import"。
    """
    global _injected_native
    _injected_native = module


def reset_native() -> None:
    """撤掉注入的替身, 恢复按需 import。"""
    global _injected_native
    _injected_native = None


def get_native() -> Any:
    """返回 native 模块。

    优先返回注入的替身; 否则 import agent_native。

    @raise ImportError 找不到 agent_native (附上排查提示)
    """
    if _injected_native is not None:
        return _injected_native

    try:
        import agent_native  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ImportError(
            "找不到 native 扩展 %r。\n"
            "  · 板端: 把 build-rk3568/native/agent_native*.so 放到 Python 能找到的目录\n"
            "  · 宿主机测试: 用 agent.io 的 set_native() 注入 mock\n"
            "    (见 tests/mocks/mock_agent_native.py)\n"
            "  原始错误: %s" % (NATIVE_MODULE_NAME, exc)
        ) from exc

    return agent_native


# ---------------------------------------------------------------------------
#  专属单线程执行器
# ---------------------------------------------------------------------------
# 每个子系统一个线程, 保证"同一 reader 的所有 native 调用同线程"(SPSC 要求),
# 同时把阻塞调用挪出事件循环。
_EXECUTORS = {}
_EXECUTORS_LOCK = threading.Lock()


def _shutdown_executors() -> None:
    """退出时收尾。

    cancel_futures=True 需要 Python 3.9; 板端是 3.8, 所以这里只做
    shutdown(wait=False): 不等待, 让还排着的任务随进程一起消失。
    (worker 是 daemon 线程, 不会拖住解释器退出。)
    """
    with _EXECUTORS_LOCK:
        executors = list(_EXECUTORS.values())
        _EXECUTORS.clear()
    for ex in executors:
        try:
            ex.shutdown(wait=False)
        except Exception:  # noqa: BLE001 - 退出路径不该再抛
            pass


atexit.register(_shutdown_executors)


def executor_for(name: str) -> ThreadPoolExecutor:
    """取 (必要时创建) 名为 name 的专属单线程执行器。

    @param name 子系统名, 例如 "image_rb" / "input_sender"
    """
    with _EXECUTORS_LOCK:
        ex = _EXECUTORS.get(name)
        if ex is None:
            ex = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="agent-io-%s" % name,
            )
            _EXECUTORS[name] = ex
        return ex


def reset_executors() -> None:
    """销毁所有执行器 (测试用, 避免用例间互相影响/泄漏线程)。"""
    _shutdown_executors()


async def run_native(subsystem: str, func, *args, **kwargs):
    """在 subsystem 的专属单线程执行器里调用 func(*args, **kwargs)。

    正常路径用 asyncio 的 run_in_executor(不阻塞事件循环); 没有运行中的事件
    循环时退回直接调用 —— 这样同步脚本里也能用同一套 API, 不必为了"能在
    REPL 里试一下"而开一个 loop。
    """
    import asyncio

    ex = executor_for(subsystem)
    loop = None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is None:
        # 没有事件循环: 直接同步执行 (仍然落在调用方线程, 调用方自己负责)
        return func(*args, **kwargs)

    def _call():
        return func(*args, **kwargs)

    return await loop.run_in_executor(ex, _call)
