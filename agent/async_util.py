# -*- coding: utf-8 -*-
"""agent/async_util.py —— 订阅者协程的 done 回调（T15-3 / 3-6 第 7 项）

`agent/core/scheduler.py`（日程触发的命令动作）与 `agent/io/chat_bus.py`（订阅者协程）
各自 `add_done_callback(...)` 一个"吃掉异常"的回调，两份实现**逐字相同**，只差打印前缀
与后半句。收敛到这里。

⚠ 为什么放在**顶层**、而不是 `core/` 或 `io/`：这两个包今天互不 import
（`agent/io/*` 一个 `agent.core` 都没有），放任何一边都会新增一条跨层依赖；放顶层
就是两边都 import 一个**中立的兄弟模块**。而且 `agent/__init__.py` 是**零副作用**的
（它自己的注释明说不做任何 I/O），所以 io 侧 import 它不会把整个 core 包拉进来。

⚠ 为什么仍然用 `print` 而不是日志（**别顺手改掉**）: 见 `docs/audit-code.md` §9.2 ——
   这两处是**后台订阅协程**的报错出口，出事时日志系统本身可能正在初始化/重入，
   走 print 更稳。这次收敛只动"谁写这段逻辑"，不动"写去哪儿"。
"""
from __future__ import annotations

import asyncio


def log_task_exception(task: asyncio.Task, prefix: str, what: str) -> None:
    """吃掉协程的异常并打印（没有它会有 "Task exception was never retrieved"）。

    @param task   `add_done_callback` 交进来的那个 task
    @param prefix 谁的协程（`Scheduler` / `ChatInputBus`）
    @param what   什么事（`background action` / `subscriber task`）
    @note 输出 = `<prefix>: <what> raised: <repr(异常)>` —— 与原来两处那句**逐字相同**
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        print("%s: %s raised: %r" % (prefix, what, exc))
