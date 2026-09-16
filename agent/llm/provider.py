# ============================================================================
#  agent/llm/provider.py — 三种模式的统一出口: edge / cloud / disabled
#
#  模式
#  ---------------------------------------------------------------------------
#      edge      板端 RKNN 加载的 0.6B 小模型 (⚠ 当前是 mock, 见 EdgeBackend)
#      cloud     OpenAI 兼容 API (openai SDK)
#      disabled  不调模型, 走 RuleEngine 规则兜底
#
#  模式从 config 读 (llm.mode), 运行时可变: provider.set_mode("cloud")。
#  也可以 set_mode(None) 让下次读盘重新取 —— 用于"配置改了想生效"。
#
#  两条入口的区别 (很重要)
#  ---------------------------------------------------------------------------
#      chat()              -> str            纯文本回复, 失败**抛出**异常
#      chat_with_tools()   -> dict           LLM 循环入口, 失败**不抛**, 返回 ok=False
#
#  为什么这样分: chat() 是给人看的简单接口, 调用方 (终端/GUI) 需要知道"模型挂了"
#  才能自己决定怎么提示; 而 chat_with_tools() 是给自动循环用的, 它必须能继续跑,
#  所以把错误收进返回值, 由 LLM 循环决定"换个说法再试"还是"报给用户"。
#
#  设计边界 (按约定不做的事)
#  ---------------------------------------------------------------------------
#  · **不做**真实 edge 推理 —— EdgeBackend 只回固定/可注入的文本
#  · **不做** prompt 模板管理 —— 系统提示直接写在 _SYSTEM_PROMPT
#  · **不做** token 计数 / 成本统计
#
#  依赖
#  ---------------------------------------------------------------------------
#  openai SDK **只在 cloud 模式真正请求时**才 import (板上/开发机都没装)。
#  这样 disabled / edge 模式完全不需要它, 也不会因为缺包而 import 失败。
# ============================================================================

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Dict, List, Optional

from ..core.tool_router import ToolRouter
from .rule_engine import RuleEngine

__all__ = [
    "LLMProvider",
    "LLMError",
    "OpenAIClientError",
    "EdgeBackend",
    "CloudBackend",
    "MODES",
    "DEFAULT_MODE",
]

#: 支持的三种模式
MODES = ("edge", "cloud", "disabled")

#: 默认模式: disabled。
#: 刻意不默认 edge/cloud —— 默认值不该在用户没配置的时候就去调模型/发网络请求。
DEFAULT_MODE = "disabled"

#: 系统提示 (按约定写死在代码里, 不做模板管理)
_SYSTEM_PROMPT = (
    "你是运行在 RK3568 开发板上的桌面助手。回答简洁、直接, 默认用中文。"
    "需要操作主机时调用提供的工具, 不要凭空描述结果。"
)

#: 工具循环的最大轮数。防止模型反复调用工具把一次请求拖成无限循环。
DEFAULT_MAX_TOOL_ROUNDS = 4


class LLMError(RuntimeError):
    """LLM 调用层的错误 (配置缺失、后端不可用……)。"""


class OpenAIClientError(LLMError):
    """cloud 模式缺少 openai SDK 或客户端不可用。"""


# ---------------------------------------------------------------------------
#  edge 后端 (mock)
# ---------------------------------------------------------------------------
class EdgeBackend:
    """板端 RKNN 小模型的替身。

    ⚠ 现在**不做真实推理**: respond() 返回注入的 reply, 或者一个说明性的固定文本。
    真实实现要接 agent_native / RKNN runtime, 那是后续任务。

    保留这一层的意义: provider 的模式分发、工具循环、错误处理现在就能定死并单测,
    之后把 respond() 换成真实现即可, 不用回头改接口。
    """

    def __init__(
        self,
        model_path: Optional[str] = None,
        reply: Optional[str] = None,
        responder: Optional[Callable[[str, Dict[str, Any]], str]] = None,
    ) -> None:
        """
        @param model_path 模型文件路径 (仅记录, mock 不加载)
        @param reply      固定回复; None 时用 _MOCK_REPLY
        @param responder  自定义可调用 (user_input, context) -> str; 优先于 reply
        """
        self.model_path = model_path
        self._reply = reply
        self._responder = responder
        self.calls: List[Dict[str, Any]] = []

    _MOCK_REPLY = "[edge-mock] 板端小模型尚未接入，这是占位回复。"

    def is_ready(self) -> bool:
        """mock 恒为 False: 明确表示"真实现还没有", 免得被误当成可用。"""
        return False

    def respond(self, user_input: str, context: Dict[str, Any]) -> str:
        self.calls.append({"user_input": user_input, "context": dict(context)})
        if self._responder is not None:
            return self._responder(user_input, context)
        if self._reply is not None:
            return self._reply
        return self._MOCK_REPLY


# ---------------------------------------------------------------------------
#  cloud 后端
# ---------------------------------------------------------------------------
class CloudBackend:
    """OpenAI 兼容 API 的客户端持有者。

    把"从哪儿拿 client"抽出来, 是为了让测试注入假 client ——
    这样工具循环的真实逻辑能被完整测到, 而不用真的联网。
    """

    def __init__(
        self,
        client: Any = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: str = "gpt-4o-mini",
        timeout_s: float = 30.0,
    ) -> None:
        self._client = client
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.timeout_s = timeout_s

    @property
    def client(self) -> Any:
        """返回 OpenAI 客户端; 没有就按需创建。

        @raise OpenAIClientError 缺 openai SDK (附安装提示)
        """
        if self._client is None:
            self._client = self._make_client()
        return self._client

    def _make_client(self) -> Any:
        try:
            import openai  # type: ignore[import-not-found]
        except ImportError as exc:
            raise OpenAIClientError(
                "cloud 模式需要 openai SDK。安装: pip3 install openai\n"
                "(开发机上装好后即可; 板端若不用 cloud 模式则不需要)\n"
                "原始错误: %s" % exc
            ) from exc

        if not self.api_key:
            raise OpenAIClientError("cloud 模式缺少 api_key (配置 llm.api_key)")

        kwargs: Dict[str, Any] = {"api_key": self.api_key, "timeout": self.timeout_s}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return openai.OpenAI(**kwargs)


# ---------------------------------------------------------------------------
#  provider
# ---------------------------------------------------------------------------
class LLMProvider:
    """按 mode 分发到 edge / cloud / disabled。

    典型用法::

        provider = LLMProvider(mode="disabled", tools=router)
        await provider.chat("现在几点", {"state": "idle"})     # 走规则兜底

        provider.set_mode("cloud")                            # 运行时切换
        await provider.chat("你好", {})

    配置读取: mode 为 None 时用 _read_mode_from_config();
    找不到配置就退回 DEFAULT_MODE ("disabled")。
    """

    def __init__(
        self,
        mode: str,
        tools: Optional[ToolRouter] = None,
        config_loader: Optional[Callable[[], Dict[str, Any]]] = None,
        edge_backend: Optional[EdgeBackend] = None,
        cloud_backend: Optional[CloudBackend] = None,
        rule_engine: Optional[RuleEngine] = None,
        max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS,
    ) -> None:
        """
        @param mode          "edge" / "cloud" / "disabled"; 传 None 表示"去配置里读"
        @param tools         工具路由 (chat_with_tools 要用); 可为 None
        @param config_loader 返回配置 dict 的可调用对象 (默认读 agent.config)
        @param edge_backend / cloud_backend / rule_engine  可注入的替身
        """
        self._tools = tools
        self._config_loader = config_loader
        self._edge = edge_backend if edge_backend is not None else EdgeBackend()
        self._cloud = cloud_backend
        self._rules = rule_engine if rule_engine is not None else RuleEngine()
        self._max_tool_rounds = max(1, int(max_tool_rounds))

        self._mode: str = ""
        self._mode_errors: List[str] = []
        # mode=None 时立刻去配置里读一次, 这样构造完就有确定模式
        self._mode = self._normalize_mode(mode) if mode is not None else self._read_mode_from_config()

    # ------------------------------------------------------------ 模式 ---
    def mode(self) -> str:
        """当前模式。"""
        return self._mode

    def set_mode(self, mode: Optional[str]) -> str:
        """运行时切换模式; 传 None 表示"下次重新从配置读"。

        @return 生效后的模式
        @note 非法的模式名**不抛异常**, 而是退回 disabled (规则兜底) ——
              配置写错时系统应该降级可用, 而不是直接起不来。
              具体错误记在 mode_errors 里。
        """
        self._mode = (
            self._read_mode_from_config() if mode is None else self._normalize_mode(mode)
        )
        return self._mode

    @property
    def mode_errors(self) -> List[str]:
        """模式解析过程中遇到的问题 (非法模式名、配置读取失败……)。"""
        return list(self._mode_errors)

    def _normalize_mode(self, mode: Any) -> str:
        name = mode.strip().lower() if isinstance(mode, str) else ""
        if name in MODES:
            return name
        self._mode_errors.append(
            "unknown llm mode %r; falling back to %r (valid: %s)"
            % (mode, DEFAULT_MODE, ", ".join(MODES))
        )
        return DEFAULT_MODE

    def _read_mode_from_config(self) -> str:
        """从配置里读 llm.mode。任何失败都退回 DEFAULT_MODE。"""
        loader = self._config_loader
        if loader is None:
            return DEFAULT_MODE
        try:
            data = loader()
        except Exception as exc:  # noqa: BLE001 - 配置读不到不该让 provider 起不来
            self._mode_errors.append("failed to load llm config: %r" % (exc,))
            return DEFAULT_MODE

        value = None
        if isinstance(data, dict):
            llm = data.get("llm")
            if isinstance(llm, dict):
                value = llm.get("mode")

        if value is None:
            return DEFAULT_MODE
        return self._normalize_mode(value)

    # ------------------------------------------------------------ chat ---
    async def chat(self, user_input: str, context: Optional[Dict[str, Any]] = None) -> str:
        """取一段文本回复。

        @return 回复文本
        @raise LLMError cloud 模式配置/SDK 有问题, 或后端调用失败
        """
        return await self._complete(user_input, context or {})

    async def _complete(self, user_input: str, context: Dict[str, Any]) -> str:
        mode = self._mode
        if mode == "disabled":
            return await self._rules.respond(user_input, context)
        if mode == "edge":
            return self._edge.respond(user_input, context)
        if mode == "cloud":
            return await self._cloud_chat(user_input, context)
        # 理论上到不了这里 (_normalize_mode 已兜底), 保底再兜一次
        return await self._rules.respond(user_input, context)

    async def _cloud_chat(self, user_input: str, context: Dict[str, Any]) -> str:
        """cloud 模式的纯文本调用 (不带工具)。"""
        backend = self._require_cloud()
        loop = asyncio.get_running_loop()
        messages = _build_messages(user_input, context)

        def _call():
            return backend.client.chat.completions.create(
                model=backend.model, messages=messages
            )

        response = await loop.run_in_executor(None, _call)
        return _first_text(response) or ""

    def _require_cloud(self) -> CloudBackend:
        if self._cloud is None:
            raise OpenAIClientError(
                "cloud 模式未配置客户端 (构造时传 cloud_backend, 或确保 llm.api_key 已设置)"
            )
        return self._cloud

    # --------------------------------------------------- chat_with_tools ---
    async def chat_with_tools(
        self, user_input: str, context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """LLM 循环入口: 回复 + 调用过的工具。

        @return  {"ok": bool, "text": str, "tool_calls": [{"name","args","result"}],
                  "error": str|None, "mode": str}
                 **永不抛异常** —— 错误收进 ok/error, 让上层循环能继续跑。
        """
        context = context or {}
        mode = self._mode

        # disabled 不需要工具: 规则引擎不会调工具, 也不该假装调了
        if mode == "disabled":
            try:
                text = await self._rules.respond(user_input, context)
            except Exception as exc:  # noqa: BLE001
                return _result(False, "", [], "%s: %s" % (type(exc).__name__, exc), mode)
            return _result(True, text, [], None, mode)

        if mode == "edge":
            # mock 阶段 edge 也不走工具循环 (真实现要自己做 function-call 解析)
            try:
                text = self._edge.respond(user_input, context)
            except Exception as exc:  # noqa: BLE001
                return _result(False, "", [], "%s: %s" % (type(exc).__name__, exc), mode)
            return _result(True, text, [], None, mode)

        return await self._cloud_chat_with_tools(user_input, context)

    async def _cloud_chat_with_tools(
        self, user_input: str, context: Dict[str, Any]
    ) -> Dict[str, Any]:
        try:
            backend = self._require_cloud()
        except LLMError as exc:
            return _result(False, "", [], str(exc), self._mode)

        messages = _build_messages(user_input, context)
        tool_schemas = self._tools.list_tools() if self._tools is not None else []
        tool_calls: List[Dict[str, Any]] = []
        loop = asyncio.get_running_loop()

        for _round in range(self._max_tool_rounds):
            try:
                response = await loop.run_in_executor(
                    None,
                    lambda: _cloud_request(backend, messages, tool_schemas),
                )
            except Exception as exc:  # noqa: BLE001 - 网络/鉴权错误都收进返回值
                return _result(
                    False, "", tool_calls, "%s: %s" % (type(exc).__name__, exc), self._mode
                )

            text = _first_text(response)
            requested = _requested_tool_calls(response)

            if not requested:
                # 没有工具调用 -> 这一轮就是最终回复
                return _result(True, text or "", tool_calls, None, self._mode)

            # 把模型的"我要调工具"这条也加进历史, 否则下一轮上下文不完整
            messages.append(_assistant_tool_message(response))

            for call in requested:
                name = call.get("name", "")
                args = call.get("args") or {}
                result = await self._invoke_tool(name, args)
                tool_calls.append({"name": name, "args": args, "result": result})
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "content": _json_dumps(result),
                    }
                )

        # 轮数用尽: 明确告知, 而不是假装正常结束
        return _result(
            False,
            text or "",
            tool_calls,
            "tool loop exceeded %d rounds" % self._max_tool_rounds,
            self._mode,
        )

    async def _invoke_tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """执行工具; 没有工具路由时返回明确的错误而不是崩掉。"""
        if self._tools is None:
            return {"ok": False, "error": "no tool router configured"}
        return await self._tools.execute(name, args)

    # ------------------------------------------------------------ 诊断 ---
    @property
    def tools(self) -> Optional[ToolRouter]:
        return self._tools

    @property
    def rule_engine(self) -> RuleEngine:
        return self._rules

    @property
    def edge_backend(self) -> EdgeBackend:
        return self._edge

    def __repr__(self) -> str:
        return "<LLMProvider mode=%s tools=%s>" % (
            self._mode,
            len(self._tools) if self._tools is not None else 0,
        )


# ---------------------------------------------------------------------------
#  辅助
# ---------------------------------------------------------------------------
def _result(
    ok: bool, text: str, tool_calls: List[Dict[str, Any]], error: Optional[str], mode: str
) -> Dict[str, Any]:
    return {
        "ok": ok,
        "text": text,
        "tool_calls": tool_calls,
        "error": error,
        "mode": mode,
    }


def _build_messages(user_input: str, context: Dict[str, Any]) -> List[Dict[str, Any]]:
    """组装 messages。

    context 非空时塞进 system 消息 —— 不做模板管理, 就是一句 JSON。
    """
    messages: List[Dict[str, Any]] = [{"role": "system", "content": _SYSTEM_PROMPT}]

    # 只放能序列化的上下文; 塞不进去的键丢掉而不是让整个请求失败
    safe_context = {}
    for key, value in (context or {}).items():
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            continue
        safe_context[key] = value

    if safe_context:
        messages.append(
            {
                "role": "system",
                "content": "上下文: " + json.dumps(safe_context, ensure_ascii=False),
            }
        )

    messages.append({"role": "user", "content": user_input})
    return messages


def _cloud_request(backend: CloudBackend, messages: List[Dict[str, Any]], tool_schemas: List[Dict[str, Any]]):
    """一次 OpenAI 调用 (同步, 由调用方放进线程池)。"""
    kwargs: Dict[str, Any] = {"model": backend.model, "messages": messages}
    if tool_schemas:
        kwargs["tools"] = [{"type": "function", "function": s} for s in tool_schemas]
        kwargs["tool_choice"] = "auto"
    return backend.client.chat.completions.create(**kwargs)


def _first_text(response: Any) -> Optional[str]:
    """从 OpenAI 响应里取第一段文本。"""
    choices = getattr(response, "choices", None)
    if not choices:
        return None
    message = getattr(choices[0], "message", None)
    if message is None:
        return None
    content = getattr(message, "content", None)
    return content if isinstance(content, str) else None


def _requested_tool_calls(response: Any) -> List[Dict[str, Any]]:
    """从 OpenAI 响应里取工具调用请求。

    @return [{"id", "name", "args"}]; args 解析失败时给 {} (不带崩整个循环)
    """
    choices = getattr(response, "choices", None)
    if not choices:
        return []
    message = getattr(choices[0], "message", None)
    calls = getattr(message, "tool_calls", None) if message is not None else None
    if not calls:
        return []

    out: List[Dict[str, Any]] = []
    for call in calls:
        fn = getattr(call, "function", None)
        raw_args = getattr(fn, "arguments", None) if fn is not None else None
        try:
            args = json.loads(raw_args) if raw_args else {}
        except (TypeError, ValueError):
            args = {}
        if not isinstance(args, dict):
            args = {}
        out.append(
            {
                "id": getattr(call, "id", ""),
                "name": getattr(fn, "name", "") if fn is not None else "",
                "args": args,
            }
        )
    return out


def _assistant_tool_message(response: Any) -> Dict[str, Any]:
    """把"模型要求调工具"这一轮还原成 messages 里的一条 assistant 消息。"""
    choices = getattr(response, "choices", None)
    message = getattr(choices[0], "message", None) if choices else None
    calls = getattr(message, "tool_calls", None) if message is not None else None

    serialized = []
    for call in calls or []:
        fn = getattr(call, "function", None)
        serialized.append(
            {
                "id": getattr(call, "id", ""),
                "type": "function",
                "function": {
                    "name": getattr(fn, "name", "") if fn is not None else "",
                    "arguments": getattr(fn, "arguments", "") if fn is not None else "{}",
                },
            }
        )
    return {
        "role": "assistant",
        "content": getattr(message, "content", None),
        "tool_calls": serialized,
    }


def _json_dumps(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return json.dumps({"ok": False, "error": "tool result is not JSON-serializable"})
