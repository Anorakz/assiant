# ============================================================================
#  tests/mocks/mock_agent_native.py — 替身 agent_native
#
#  用途
#  ---------------------------------------------------------------------------
#  agent_native 是交叉编译产物 (.cpython-38-aarch64-linux-gnu.so), 宿主机上没有。
#  这个替身把 native 暴露的**全部**接口都实现一遍, 让 agent/io 的单测不依赖 .so。
#
#  刻意与 native 保持一致的语义 (否则测试会给出假的安全感)
#  ---------------------------------------------------------------------------
#  · image_rb.read_latest()  在没有帧时返回 **None** (不是空数组)
#  · image_rb.read_latest()  **非破坏性**: 同一帧会被反复返回, 直到有新帧
#  · image_rb.read_by_timestamp(ts) 取"不超过 ts 的最大时间戳", 非破坏性
#  · host_input_rb.read_latest()  返回 dict 或 None
#  · host_input_rb.read_all()     取走全部未读事件 (会清空), FIFO
#  · send_* 只记录调用 (native 侧 stub 也会打日志), 供测试断言
#
#  不做的
#  ---------------------------------------------------------------------------
#  不模拟线程/SPSC 语义, 不模拟 pybind11 的类型转换严格性。类型转换的真实约束
#  由板端 test_binding_api.py 覆盖。
#
#  用法
#  ---------------------------------------------------------------------------
#      from tests.mocks.mock_agent_native import MockNative
#      native = MockNative()
#      native.push_image(frame_bytes, ts=1000)
#      native.push_host_event(type="key", key=ord("A"))
#      reader = ImageReader(native=native)
# ============================================================================

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
#  常量: 与 native/binding.cpp 里的取值保持一致
# ---------------------------------------------------------------------------
MODIFIER_NONE = 0x00
MODIFIER_SHIFT = 0x01
MODIFIER_CTRL = 0x02
MODIFIER_ALT = 0x04
MODIFIER_META = 0x08

BUTTON_LEFT = 0x01
BUTTON_MIDDLE = 0x02
BUTTON_RIGHT = 0x03

FRAME_WIDTH = 256
FRAME_HEIGHT = 256
FRAME_CHANNELS = 3
FRAME_BYTES = FRAME_WIDTH * FRAME_HEIGHT * FRAME_CHANNELS  # 196608

IMAGE_RING_CAPACITY = 300
HOST_INPUT_CAPACITY = 128

__version__ = "0.2.0-mock"


def _frame_stub(fill: int = 0) -> Any:
    """一帧 (256,256,3) uint8。有 numpy 就用 numpy, 没有就退化成 bytes。

    agent/io 只把返回值原样透传, 所以两种都能跑; 有 numpy 时更贴近真实。
    """
    try:
        import numpy as np
    except ImportError:
        return bytes([fill]) * FRAME_BYTES
    return np.full((FRAME_WIDTH, FRAME_HEIGHT, FRAME_CHANNELS), fill, dtype=np.uint8)


def _default_host_event(**overrides: Any) -> Dict[str, Any]:
    """一条 host_input_rb 事件的默认形态 (字段与 binding_utils.h 一致)。"""
    event = {
        "type": "key",
        "modifier": 0,
        "key": 0,
        "x": 0,
        "y": 0,
        "action": "press",
        "pressed": True,
        "timestamp_ns": time.time_ns(),
    }
    event.update(overrides)
    # 与 native 一致: action 与 pressed 是同一个信息的两种表示
    if "action" in overrides and "pressed" not in overrides:
        event["pressed"] = overrides["action"] == "press"
    return event


class _ImageRingBufferMock:
    def __init__(self, owner: "MockNative") -> None:
        self._owner = owner

    # -- native 接口 --
    def read_latest(self) -> Optional[Any]:
        self._owner.calls.append(("image_rb.read_latest",))
        if not self._owner.frames:
            return None
        return self._owner.frames[-1][1]

    def read_by_timestamp(self, ts: int) -> Optional[Any]:
        self._owner.calls.append(("image_rb.read_by_timestamp", ts))
        best = None
        for frame_ts, data in self._owner.frames:
            if frame_ts <= ts and (best is None or frame_ts > best[0]):
                best = (frame_ts, data)
        return None if best is None else best[1]

    def size(self) -> int:
        self._owner.calls.append(("image_rb.size",))
        return len(self._owner.frames)

    def capacity(self) -> int:
        self._owner.calls.append(("image_rb.capacity",))
        return IMAGE_RING_CAPACITY

    def overruns(self) -> int:
        self._owner.calls.append(("image_rb.overruns",))
        return self._owner.image_overruns


class _HostInputRingBufferMock:
    def __init__(self, owner: "MockNative") -> None:
        self._owner = owner

    def read_latest(self) -> Optional[Dict[str, Any]]:
        self._owner.calls.append(("host_input_rb.read_latest",))
        if not self._owner.host_events:
            return None
        # 与 native 一致: 取最新一条并丢弃比它更旧的
        event = self._owner.host_events[-1]
        self._owner.host_events = []
        return event

    def read_all(self) -> List[Dict[str, Any]]:
        self._owner.calls.append(("host_input_rb.read_all",))
        events = self._owner.host_events
        self._owner.host_events = []
        return events

    def size(self) -> int:
        self._owner.calls.append(("host_input_rb.size",))
        return len(self._owner.host_events)

    def capacity(self) -> int:
        self._owner.calls.append(("host_input_rb.capacity",))
        return HOST_INPUT_CAPACITY

    def overruns(self) -> int:
        self._owner.calls.append(("host_input_rb.overruns",))
        return self._owner.host_overruns


class _MoonlightMock:
    def __init__(self, owner: "MockNative") -> None:
        self._owner = owner
        self._state = "idle"
        self._error = ""

    # -- native 接口 --
    def start(self, host: str, app: str, w: int, h: int, fps: int) -> bool:
        self._owner.calls.append(("moonlight.start", host, app, w, h, fps))
        if self._owner.start_result:
            self._state = "streaming"
            self._error = ""
        else:
            self._state = "idle"
            self._error = self._owner.start_error
        return self._owner.start_result

    def stop(self) -> None:
        self._owner.calls.append(("moonlight.stop",))
        self._state = "idle"

    def status(self) -> Dict[str, Any]:
        self._owner.calls.append(("moonlight.status",))
        return {
            "state": self._state,
            "state_code": 0,
            "connected": self._state == "streaming",
            "error": self._error,
            "frames_pushed": len(self._owner.frames),
            "video_units_received": len(self._owner.frames),
            "image_frames_available": len(self._owner.frames),
            "image_frames_dropped": self._owner.image_overruns,
            "host_input_available": len(self._owner.host_events),
            "host_input_dropped": self._owner.host_overruns,
        }


class MockNative:
    """agent_native 的替身。字段/方法名与 native/binding.cpp 一一对应。"""

    def __init__(self) -> None:
        # ---- 可观测状态 ----
        self.calls: List[tuple] = []          # 所有被调用的 native 接口 (按序)
        self.frames: List[tuple] = []          # [(timestamp_ns, frame)]
        self.host_events: List[Dict[str, Any]] = []
        self.image_overruns = 0
        self.host_overruns = 0
        self.start_result = True               # moonlight.start 的返回值
        self.start_error = "mock start failed"

        # ---- 子模块 (与 native 同名) ----
        self.moonlight = _MoonlightMock(self)
        self.image_rb = _ImageRingBufferMock(self)
        self.host_input_rb = _HostInputRingBufferMock(self)

        # ---- 顶层常量 ----
        self.__version__ = __version__
        self.MODIFIER_NONE = MODIFIER_NONE
        self.MODIFIER_SHIFT = MODIFIER_SHIFT
        self.MODIFIER_CTRL = MODIFIER_CTRL
        self.MODIFIER_ALT = MODIFIER_ALT
        self.MODIFIER_META = MODIFIER_META
        self.BUTTON_LEFT = BUTTON_LEFT
        self.BUTTON_MIDDLE = BUTTON_MIDDLE
        self.BUTTON_RIGHT = BUTTON_RIGHT
        self.FRAME_WIDTH = FRAME_WIDTH
        self.FRAME_HEIGHT = FRAME_HEIGHT
        self.FRAME_CHANNELS = FRAME_CHANNELS
        self.IMAGE_RING_CAPACITY = IMAGE_RING_CAPACITY
        self.HOST_INPUT_CAPACITY = HOST_INPUT_CAPACITY

    # ------------------------------------------------------------ 顶层函数 --
    def ping(self) -> str:
        self.calls.append(("ping",))
        return "pong"

    def send_key(self, modifier: int, key: int, action: Any) -> None:
        self.calls.append(("send_key", modifier, key, action))

    def send_hotkey(self, keys: List[int]) -> None:
        self.calls.append(("send_hotkey", list(keys)))

    def send_mouse(self, x: int, y: int, action: Any) -> None:
        self.calls.append(("send_mouse", x, y, action))

    # ------------------------------------------------------- 测试辅助 API --
    def push_image(self, data: Any = None, ts: int = 0) -> Any:
        """往 image_rb 塞一帧。data=None 时用一帧按 ts 填充的图案。"""
        if data is None:
            data = _frame_stub(fill=ts % 256)
        self.frames.append((ts, data))
        return data

    def push_host_event(self, **overrides: Any) -> Dict[str, Any]:
        """往 host_input_rb 塞一条事件 (默认是一条按下 'A' 的 key 事件)。"""
        event = _default_host_event(**overrides)
        self.host_events.append(event)
        return event

    def calls_named(self, name: str) -> List[tuple]:
        """取出所有名字为 name 的调用记录。"""
        return [c for c in self.calls if c[0] == name]

    def last_call(self, name: str) -> Optional[tuple]:
        hits = self.calls_named(name)
        return hits[-1] if hits else None

    def reset(self) -> None:
        """清空所有记录与状态。"""
        self.calls.clear()
        self.frames.clear()
        self.host_events.clear()
        self.image_overruns = 0
        self.host_overruns = 0
        self.start_result = True
        self.moonlight = _MoonlightMock(self)
        self.image_rb = _ImageRingBufferMock(self)
        self.host_input_rb = _HostInputRingBufferMock(self)
