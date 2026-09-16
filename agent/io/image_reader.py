# ============================================================================
#  agent/io/image_reader.py — image_rb 的 asyncio 包装
#
#  对应的 native 接口 (native/binding.cpp)
#  ---------------------------------------------------------------------------
#      agent_native.image_rb.read_latest()             -> numpy(256,256,3) uint8 | None
#      agent_native.image_rb.read_by_timestamp(ts)     -> 同上
#      agent_native.image_rb.size() / capacity() / overruns()
#
#  ⚠ 为什么必须固定一个线程
#  ---------------------------------------------------------------------------
#  ImageRingBuffer 是 SPSC 无锁结构, 消费者必须是**同一个线程**。
#  所以这里的三次 native 调用全部走 "image_rb" 这一个专属单线程执行器
#  (见 _native.run_native), 而不是 asyncio 默认的共享线程池。
#
#  语义说明
#  ---------------------------------------------------------------------------
#  · read_latest() 在还没有帧时返回 None (不抛异常, 也不返回空数组)
#  · native 侧的 read_latest 是**非破坏性**的: 同一个最新帧会被反复返回,
#    直到有新帧进来。想"只要新帧"要由调用方自己比对 (native 有 read_by_timestamp
#    可以配合做这件事), 本层不做去重 —— 去重需要策略, 而策略属于上层。
#  · read_by_timestamp 取"时间戳最接近且不超过 ts"的帧, 不推进游标,
#    所以同一个 ts 重复调用结果一致。
# ============================================================================

from __future__ import annotations

from typing import Any, Optional

from ._native import get_native, run_native

__all__ = ["ImageReader"]

#: 归属的子系统名 (决定用哪个专属执行器)
_SUBSYS = "image_rb"


class ImageReader:
    """把 image_rb 的阻塞读取包成 awaitable。

    典型用法::

        reader = ImageReader()
        frame = await reader.read_latest()      # numpy (256,256,3) uint8 或 None
    """

    def __init__(self, native: Any = None) -> None:
        """
        @param native 注入的 native 模块 (测试用); None = 按需 import agent_native
        """
        self._native = native

    # ------------------------------------------------------------ 内部 ---
    def _rb(self) -> Any:
        native = self._native if self._native is not None else get_native()
        return native.image_rb

    # ------------------------------------------------------------ 读取 ---
    async def read_latest(self) -> Optional[Any]:
        """读最近一帧。

        @return numpy ndarray, shape (256, 256, 3), dtype uint8; 还没有帧时 None
        """
        rb = self._rb()
        return await run_native(_SUBSYS, rb.read_latest)

    async def read_by_timestamp(self, ts: int) -> Optional[Any]:
        """读"时间戳最接近且不超过 ts"的一帧 (非破坏性)。

        @param ts 纳秒时间戳 (与 image_rb 的 timestamp_ns 同一时基)
        @return numpy ndarray 或 None (没有满足条件的帧)
        """
        rb = self._rb()
        return await run_native(_SUBSYS, rb.read_by_timestamp, ts)

    # ------------------------------------------------------------ 诊断 ---
    async def size(self) -> int:
        """当前可读帧数。"""
        rb = self._rb()
        return await run_native(_SUBSYS, rb.size)

    async def capacity(self) -> int:
        """环形缓冲固定容量。"""
        rb = self._rb()
        return await run_native(_SUBSYS, rb.capacity)

    async def overruns(self) -> int:
        """因覆盖而丢掉的未读帧数累计值。"""
        rb = self._rb()
        return await run_native(_SUBSYS, rb.overruns)
