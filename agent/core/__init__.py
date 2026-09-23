# ============================================================================
#  agent/core/__init__.py — Agent Core: 状态层、工具路由、调度层
#
#  当前有状态机与工具路由。scheduler / router / llm / vision 等后续加在这里。
#
#  ⚠ 本包不在 import 时加载 native 扩展 (agent_native 是交叉编译产物, 宿主机
#    上没有)。StateMachine.is_connected() 会按需去问 agent.io, 拿不到就报
#    "未连接", 不会因为缺 .so 而 import 失败。
# ============================================================================

from .state_machine import INITIAL_STATE, LEGAL_TRANSITIONS, State, StateMachine
from .music import DEFAULT_POLL_INTERVAL_S, MusicError, MusicPlayer
from .scheduler import (
    DEFAULT_HISTORY_LIMIT,
    DEFAULT_INTERVAL_MIN,
    DEFAULT_WINDOW_MIN,
    TERMINAL_SOURCE,
    CommandBinding,
    ScheduleEvent,
    Scheduler,
    SchedulerError,
    normalize_command,
    parse_clock,
    parse_command_config,
)
from .tool_router import (
    DEFAULT_TIMEOUT_S,
    SUPPORTED_KEYWORDS,
    SchemaError,
    Tool,
    ToolRouter,
    validate_args,
    validate_schema,
)

__all__ = [
    # 状态层
    "State",
    "StateMachine",
    "LEGAL_TRANSITIONS",
    "INITIAL_STATE",
    # 调度层
    "Scheduler",
    "ScheduleEvent",
    "CommandBinding",
    "SchedulerError",
    "TERMINAL_SOURCE",
    "parse_clock",
    "normalize_command",
    "parse_command_config",
    "DEFAULT_INTERVAL_MIN",
    "DEFAULT_WINDOW_MIN",
    "DEFAULT_HISTORY_LIMIT",
    # 工具路由
    "Tool",
    "ToolRouter",
    "SchemaError",
    "validate_args",
    "validate_schema",
    "SUPPORTED_KEYWORDS",
    "DEFAULT_TIMEOUT_S",
    # 播放内核（音乐, T8-4）
    "MusicPlayer",
    "MusicError",
    "DEFAULT_POLL_INTERVAL_S",
]
