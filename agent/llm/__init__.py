# ============================================================================
#  agent/llm/__init__.py — LLM 层: 三模式分发 + 规则兜底
#
#      edge      板端 RKNN 0.6B 小模型 (⚠ 当前是 mock, 真实现待接)
#      cloud     OpenAI 兼容 API
#      disabled  不调模型, 走 RuleEngine 规则兜底
#
#  ⚠ 本包不在 import 时加载 openai SDK (宿主/板端都没装)。只有 cloud 模式
#    真正发起请求时才会 import, 缺包会给出带安装提示的 OpenAIClientError。
# ============================================================================

from .provider import (
    DEFAULT_MAX_TOOL_ROUNDS,
    DEFAULT_MODE,
    MODES,
    CloudBackend,
    EdgeBackend,
    LLMError,
    LLMProvider,
    OpenAIClientError,
)
from .rule_engine import Rule, RuleEngine

__all__ = [
    "LLMProvider",
    "RuleEngine",
    "Rule",
    "EdgeBackend",
    "CloudBackend",
    "LLMError",
    "OpenAIClientError",
    "MODES",
    "DEFAULT_MODE",
    "DEFAULT_MAX_TOOL_ROUNDS",
]
