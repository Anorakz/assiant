# ============================================================================
#  agent/ipc/__init__.py — IPC 层 (Agent ⇄ GUI)
#
#  内容
#  ---------------------------------------------------------------------------
#      protocol.py   协议: 编解码 + 常量 (消息里的 topic 表、命令表)
#      server.py     Agent 侧 IPC server: 监听 /tmp/agent.sock, 收命令 + push
#
#      文档: docs/ipc-protocol.md
#      传输: Unix domain socket /tmp/agent.sock, 换行分隔 JSON, UTF-8
#
#  ⚠ 这里**故意不导出** server.py 的 IPCServer / build_ipc
#  ---------------------------------------------------------------------------
#  agent/main.py 会 import 本包并 getattr 找 build_ipc()/IPCServer 自动接入。
#  但 main.py 的 hook 只传 (bus, config): switch_mode 需要状态机、status 需要
#  状态源, 这些都还没接。现在导出会让 main.py 起一个半残的 IPC (接受连接但
#  switch_mode 无效), 所以保持不导出 —— main.py 仍会记一条 warning 跳过, 这是
#  预期状态。等接线做完再把它加进 __all__。
#
#  单独起 IPC server (联调 / 手工调试):
#      python3 -m agent.ipc.server [--socket /tmp/agent.sock] [--control-stdin]
# ============================================================================

from .protocol import (
    COMMAND_CHAT_INPUT,
    COMMAND_NEXT_BILIBILI,
    COMMAND_NEXT_WALLPAPER,
    COMMAND_SWITCH_MODE,
    COMMANDS,
    ENCODING,
    MAX_LINE_BYTES,
    MESSAGE_SEPARATOR,
    MODE_GAME,
    MODE_IDLE,
    MODE_SLEEP,
    MODE_STUDY,
    MODES,
    SOCKET_PATH,
    TOPIC_LLM,
    TOPIC_MUSIC,
    TOPIC_STATUS,
    TOPIC_WALLPAPER,
    TOPICS,
    InvalidMessageError,
    IpcProtocolError,
    MalformedJsonError,
    decode,
    decode_full,
    encode,
)

__all__ = [
    "SOCKET_PATH",
    "MESSAGE_SEPARATOR",
    "ENCODING",
    "MAX_LINE_BYTES",
    "TOPIC_STATUS",
    "TOPIC_LLM",
    "TOPIC_WALLPAPER",
    "TOPIC_MUSIC",
    "TOPICS",
    "COMMAND_SWITCH_MODE",
    "COMMAND_NEXT_WALLPAPER",
    "COMMAND_CHAT_INPUT",
    "COMMAND_NEXT_BILIBILI",
    "COMMANDS",
    "MODE_SLEEP",
    "MODE_IDLE",
    "MODE_STUDY",
    "MODE_GAME",
    "MODES",
    "encode",
    "decode",
    "decode_full",
    "IpcProtocolError",
    "MalformedJsonError",
    "InvalidMessageError",
]
