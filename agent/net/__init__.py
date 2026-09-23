# ============================================================================
#  agent/net/__init__.py — 对外网络客户端
#
#  职责
#  ---------------------------------------------------------------------------
#      SunshineClient   Sunshine (GameStream) 的 HTTPS 握手:
#                       /serverinfo /applist /launch → app_version + sessionUrl0
#      NeteaseCli       **在 PC 上**跑第三方 neteasecli（T8）:
#                       ssh → `neteasecli --json …` → 解析 JSON（播放/进度/歌词/搜索）
#
#  为什么握手在这一层而不在 native: 47984 是 TLS + 客户端证书, 而 C++ 侧不想为它
#  引入 OpenSSL。细节见 sunshine_client.py 的文件头。
#
#  ⚠ 本包 import 时不联网、也不 import native; 只有真正调用方法才发请求/起进程。
# ============================================================================

from .netease_cli import (
    DEFAULT_BINARY,
    DEFAULT_TIMEOUT_S,
    AuthFailure,
    ConnectionFailure,
    NeteaseCli,
    NeteaseCliError,
    PlayerFailure,
    ssh_available,
)
from .sunshine_client import (
    ALREADY_RUNNING_CODES,
    ALREADY_RUNNING_HINTS,
    DEFAULT_HTTP_PORT,
    DEFAULT_HTTPS_PORT,
    DEFAULT_TIMEOUT,
    DEFAULT_UNIQUE_ID,
    STEREO_CHANNEL_COUNT,
    STEREO_CHANNEL_MASK,
    AppEntry,
    LaunchResult,
    ServerInfo,
    SessionStart,
    SunshineClient,
    SunshineConfigError,
    SunshineError,
    build_launch_query,
    is_already_running,
    parse_app_list,
    parse_launch,
    parse_server_info,
    status_code_of,
    stream_mode,
    surround_audio_info,
)

__all__ = [
    # 主要接口
    "SunshineClient",
    "ServerInfo",
    "AppEntry",
    "LaunchResult",
    "SessionStart",
    # neteasecli（T8: 在 PC 上跑, 板端只说 ssh）
    "NeteaseCli",
    "NeteaseCliError",
    "ConnectionFailure",
    "AuthFailure",
    "PlayerFailure",
    "DEFAULT_BINARY",
    "DEFAULT_TIMEOUT_S",
    "ssh_available",
    # 异常
    "SunshineError",
    "SunshineConfigError",
    # 纯函数 (解析 / 拼串 / 判定, 无网可测)
    "parse_server_info",
    "parse_app_list",
    "parse_launch",
    "status_code_of",
    "build_launch_query",
    "stream_mode",
    "surround_audio_info",
    "is_already_running",
    # 常量
    "ALREADY_RUNNING_CODES",
    "ALREADY_RUNNING_HINTS",
    "DEFAULT_HTTPS_PORT",
    "DEFAULT_HTTP_PORT",
    "DEFAULT_TIMEOUT",
    "DEFAULT_UNIQUE_ID",
    "STEREO_CHANNEL_COUNT",
    "STEREO_CHANNEL_MASK",
]
