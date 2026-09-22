# ============================================================================
#  agent/llm/__init__.py — LLM 层: 三模式分发 + 规则兜底
#
#      edge      板端本地模型: llama.cpp GGUF, 经本机 llama-server (T2 起是真的,
#                不再是 mock); 与 cloud 共用同一套 OpenAI 兼容客户端与工具循环
#      cloud     OpenAI 兼容 API
#      disabled  不调模型, 走 RuleEngine 规则兜底
#
#  ⚠ 本包不在 import 时加载 openai SDK (宿主/板端都可能没装)。只有 edge / cloud
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
