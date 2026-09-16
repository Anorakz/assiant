# ============================================================================
#  agent/core/__init__.py — Agent Core: 状态层与调度层
#
#  当前只有状态机。scheduler / router / llm / vision 等后续加在这里。
#
#  ⚠ 本包不在 import 时加载 native 扩展 (agent_native 是交叉编译产物, 宿主机
#    上没有)。StateMachine.is_connected() 会按需去问 agent.io, 拿不到就报
#    "未连接", 不会因为缺 .so 而 import 失败。
# ============================================================================

from .state_machine import INITIAL_STATE, LEGAL_TRANSITIONS, State, StateMachine

__all__ = ["State", "StateMachine", "LEGAL_TRANSITIONS", "INITIAL_STATE"]
