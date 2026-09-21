# ============================================================================
#  agent/io/__init__.py — I/O 层: 输入汇聚 + 图像读取 + 输入发送
#
#  职责
#  ---------------------------------------------------------------------------
#  把 native (agent_native) 的阻塞接口包成 asyncio 友好的 awaitable:
#
#      ChatInputBus      多源(终端/GUI)统一事件流
#      ImageReader       image_rb 读取 (numpy 帧)
#      InputSender       发按键/组合键/鼠标给主机
#
#  ⚠ 本包不在 import 时加载 native 扩展: agent_native 是交叉编译产物, 宿主机上
#    没有。缺 .so 时只有真正调用 native 的那一刻才会报错, 且错误信息带排查提示。
#    测试用 _native.set_native(mock) 注入替身。
# ============================================================================

from ._native import get_native, reset_executors, reset_native, set_native
from .chat_bus import EVENT_FIELDS, ChatInputBus
from .image_reader import ImageReader
from .input_sender import InputSender, resolve_key, resolve_modifier

__all__ = [
    # 主要接口
    "ChatInputBus",
    "ImageReader",
    "InputSender",
    # 辅助
    "EVENT_FIELDS",
    "resolve_key",
    "resolve_modifier",
    # native 注入 (测试)
    "get_native",
    "set_native",
    "reset_native",
    "reset_executors",
]
