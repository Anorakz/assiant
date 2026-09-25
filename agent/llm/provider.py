# ============================================================================
#  agent/llm/provider.py — 三种模式的统一出口: edge / cloud / disabled
#
#  模式
#  ---------------------------------------------------------------------------
#      edge      板端本地模型: llama.cpp 的 GGUF, 经**本机 llama-server** 的
#                OpenAI 兼容接口访问 (与 cloud 同一套客户端, 只是连 127.0.0.1)
#      cloud     远端 OpenAI 兼容 API (openai SDK)
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
#  edge 与 cloud **共用同一个工具循环** (T2): 两边都是 OpenAI 兼容接口, 差别只有
#  "连哪儿 + 每次请求带什么参数", 所以循环只写一份 (_chat_with_tools)。
#
#  edge 挂了怎么办 (T2 的决定, 要能一眼看出不是模型答的)
#  ---------------------------------------------------------------------------
#  · chat()           照抛 —— 调用方要能知道模型挂了
#  · chat_with_tools()**退回规则兜底**, 但结果里带 `degraded` 说明原因, 且 main.py
#    会记一条 warning 并计入 llm_errors。不假装模型答了。
#
#  设计边界 (按约定不做的事)
#  ---------------------------------------------------------------------------
#  · **不加载 GGUF 文件** —— 真正加载模型的是 llama-server 进程, Agent 只是它的客户端
#  · **不做** prompt 模板管理 —— 系统提示直接写在 _SYSTEM_PROMPT (+ Qwen3 的 /no_think)
#  · **不做** token 计数 / 成本统计
#
#  依赖
#  ---------------------------------------------------------------------------
#  openai SDK **只在真正要发请求时**才 import。所以 disabled 模式完全不需要它;
#  edge / cloud 缺 SDK 时报的是 OpenAIClientError 并给出安装提示, 而不是 import 崩。
# ============================================================================

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Dict, List, Mapping, Optional

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

#: 模式别名 —— 规范名之外的常见写法, 静默归一而不是报"非法模式"。
#: "board" 尤其重要: 板端本地模型口语上就叫 board 模型, 而规范名是 edge。
_MODE_ALIASES = {
    "board": "edge",
    "local": "edge",
    "onboard": "edge",
    "off": "disabled",
    "none": "disabled",
    "nollm": "disabled",
}

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

#: Qwen3 的"软开关": 加在 system 提示末尾就关掉思考模式。
#: ⚠ 为什么非要它: 板端实测 (Qwen3-0.6B-Q4_K_M + llama-server build 10677) 不带这个时,
#:    普通提问会把 token 预算全烧在思考上 —— finish_reason="length"、content 是空字符串、
#:    正文一个字都没有。0.6B 上思考内容并没有换来更好的答复, 所以 edge 默认关掉。
_NO_THINK_SUFFIX = "/no_think"

#: edge 降级时加在答复正文前面的那句。GUI 只显示正文, 所以"这不是模型答的"
#: 必须写在正文里, 不能只写在日志/结果字段里。
_DEGRADED_PREFIX = "（板端模型没有响应，这条是规则兜底）"

#: edge 后端的默认值: llama-server 就在本机, 端口来自 llm.port。
DEFAULT_EDGE_PORT = 9000
DEFAULT_EDGE_MODEL = "qwen3-0.6b"
#: llama-server 没配 --api-key 时 openai SDK 也要求一个非空 key, 给个占位。
DEFAULT_EDGE_API_KEY = "sk-no-key-required"


class LLMError(RuntimeError):
    """LLM 调用层的错误 (配置缺失、后端不可用……)。"""


class OpenAIClientError(LLMError):
    """cloud 模式缺少 openai SDK 或客户端不可用。"""


# ---------------------------------------------------------------------------
#  OpenAI 兼容后端: edge 与 cloud 的共同底座
# ---------------------------------------------------------------------------
class _OpenAICompatibleBackend:
    """一个 OpenAI 兼容 HTTP 服务的客户端持有者。

    edge (本机 llama-server) 与 cloud (远端 API) 只差三件事: 连哪儿、默认参数、
    缺 SDK/缺 key 时的提示。请求怎么发 ("tools" 怎么摆、额外参数怎么带) 是一样的,
    所以放在这里一份 —— 工具循环也就不必为两边各写一遍。

    测试注入 `client=` 就能把真实逻辑完整测到而不联网 (tests/test_llm.py 的假替身)。
    """

    def __init__(
        self,
        client: Any = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: str = "",
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

        @raise OpenAIClientError 缺 openai SDK / 缺 api_key
        """
        if self._client is None:
            self._client = self._make_client()
        return self._client

    # -- 子类要改的两处信号 ------------------------------------------------
    def _missing_sdk_message(self, exc: Exception) -> str:
        return ("需要 openai SDK。安装: pip3 install openai\n原始错误: %s" % exc)

    def _make_client(self) -> Any:
        try:
            import openai  # type: ignore[import-not-found]
        except ImportError as exc:
            raise OpenAIClientError(self._missing_sdk_message(exc)) from exc

        if not self.api_key:
            raise OpenAIClientError(self._missing_api_key_message())

        kwargs: Dict[str, Any] = {"api_key": self.api_key, "timeout": self.timeout_s}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return openai.OpenAI(**kwargs)

    def _missing_api_key_message(self) -> str:
        return "缺少 api_key"

    def extra_request_kwargs(self) -> Dict[str, Any]:
        """每次请求额外要带的参数 (子类覆盖)。"""
        return {}

    # -- 发一次请求 (同步, 由调用方丢线程池) --------------------------------
    def request_kwargs(
        self, messages: List[Dict[str, Any]], tool_schemas: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {"model": self.model, "messages": messages}
        if tool_schemas:
            kwargs["tools"] = [{"type": "function", "function": s} for s in tool_schemas]
            kwargs["tool_choice"] = "auto"
        kwargs.update(self.extra_request_kwargs())
        return kwargs

    def create(
        self, messages: List[Dict[str, Any]], tool_schemas: Optional[List[Dict[str, Any]]] = None
    ) -> Any:
        """一次 chat.completions.create。工具循环与纯文本调用都走这里。"""
        return self.client.chat.completions.create(
            **self.request_kwargs(messages, tool_schemas)
        )


# ---------------------------------------------------------------------------
#  edge 后端 (本机 llama-server)
# ---------------------------------------------------------------------------
class EdgeBackend(_OpenAICompatibleBackend):
    """板端本地模型 —— **llama.cpp 的 GGUF, 由 llama-server 进程加载**。

    Agent 自己不读 GGUF、不跑推理, 只是 llama-server 的 OpenAI 兼容客户端
    (默认 `http://127.0.0.1:9000/v1`)。T2 之前这里是个回固定文本的 mock。

    三个键从 config 的 `llm:` 段来 (见 `from_config`):
    `port` -> base_url, `model_name` -> model, `local_api_key` -> api_key。
    ⚠ 用的是 llama-server **自己的** key, 不是云端的 `llm.api_key`。
    """

    def __init__(
        self,
        client: Any = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout_s: float = 30.0,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        model_path: Optional[str] = None,
        no_think: bool = True,
    ) -> None:
        """
        @param max_tokens  None = 不带这个参数 (由 llama-server 自己决定)
        @param model_path  只记录: GGUF 路径**由 llama-server 加载**, Agent 不读它
        @param no_think    是否在 system 提示末尾加 /no_think (Qwen3 关思考)
        """
        super().__init__(
            client=client,
            api_key=api_key if api_key is not None else DEFAULT_EDGE_API_KEY,
            base_url=base_url if base_url is not None else _edge_base_url(DEFAULT_EDGE_PORT),
            model=model if model is not None else DEFAULT_EDGE_MODEL,
            timeout_s=timeout_s,
        )
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.model_path = model_path
        self.no_think = bool(no_think)

    # ------------------------------------------------------------ 配置 ---
    @classmethod
    def from_config(cls, llm_cfg: Optional[Dict[str, Any]]) -> "EdgeBackend":
        """从 config 的 `llm:` 段造后端。**任何键读不到都退回默认值, 不抛异常**。

        读的键: port / model_name / local_api_key / timeout_s / max_tokens /
                temperature / model_path —— 全是 config.example.yaml 里已有的。
        """
        cfg = llm_cfg if isinstance(llm_cfg, dict) else {}

        def _int(key: str) -> Optional[int]:
            raw = cfg.get(key)
            if isinstance(raw, bool) or raw is None:
                return None
            try:
                return int(str(raw).strip())
            except (TypeError, ValueError):
                return None

        def _float(key: str) -> Optional[float]:
            raw = cfg.get(key)
            if isinstance(raw, bool) or raw is None:
                return None
            try:
                return float(str(raw).strip())
            except (TypeError, ValueError):
                return None

        def _text(key: str) -> Optional[str]:
            raw = cfg.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
            return None

        port = _int("port")
        base_url = _text("edge_base_url")
        if base_url is None:
            base_url = _edge_base_url(port if port is not None else DEFAULT_EDGE_PORT)

        return cls(
            base_url=base_url,
            model=_text("model_name"),
            api_key=_text("local_api_key"),
            timeout_s=_float("timeout_s") if _float("timeout_s") is not None else 30.0,
            max_tokens=_int("max_tokens"),
            temperature=_float("temperature"),
            model_path=_text("model_path"),
        )

    # ------------------------------------------------------------ 请求 ---
    def extra_request_kwargs(self) -> Dict[str, Any]:
        """把 llm.max_tokens / temperature 真正带上 (T2 之前这两个键没人读)。

        ⚠ T8-5c-4: `no_think` 打开时**每一次请求**都带
        `chat_template_kwargs={"enable_thinking": false}` —— 这是 llama.cpp 的模板开关
        （模型模板里有 `{%- if enable_thinking is defined and enable_thinking is false %}`,
        为假时直接吐一个空的思考块）。为什么不能只靠 system/user 里那句 `/no_think`:

          · 软开关是**模型学过的**：只在第一轮稳, 工具结果那一轮里 user 消息已经被推到
            历史深处（最后一条是 `tool`）, 不能保证还压得住;
          · 模板参数是**模板自己保证**的, 与第几轮、消息形状都无关。

        ⚠ 必须走 `extra_body=`（板端实测踩到）: openai SDK **不接受未知的顶层关键字** ——
        直接传 `chat_template_kwargs=` 会 `TypeError: create() got an unexpected keyword
        argument`, 然后整轮降级成规则兜底。`extra_body` 是 SDK 官方的"塞进请求体"通道。
        """
        kwargs: Dict[str, Any] = {}
        if self.max_tokens is not None:
            kwargs["max_tokens"] = int(self.max_tokens)
        if self.temperature is not None:
            kwargs["temperature"] = float(self.temperature)
        if self.no_think:
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
        return kwargs

    # ------------------------------------------------------------ 诊断 ---
    def is_ready(self) -> bool:
        """配置齐不齐 + openai SDK 在不在。

        ⚠ **不发网络请求**, 所以 True 只代表"能发得出请求", 不代表 llama-server 活着。
        真正"活着"的证据是一次成功的请求 (见 docs/llm.md 的板端验证一节)。
        """
        if self._client is not None:
            return True
        if not (self.base_url and self.model):
            return False
        try:
            import openai  # type: ignore[import-not-found]  # noqa: F401
        except ImportError:
            return False
        return True

    def describe(self) -> str:
        return "llama-server %s model=%s max_tokens=%s temperature=%s no_think=%s" % (
            self.base_url,
            self.model,
            self.max_tokens,
            self.temperature,
            self.no_think,
        )

    def __repr__(self) -> str:
        return "<EdgeBackend %s>" % self.describe()


# ---------------------------------------------------------------------------
#  cloud 后端
# ---------------------------------------------------------------------------
class CloudBackend(_OpenAICompatibleBackend):
    """远端 OpenAI 兼容 API 的客户端持有者。

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
        super().__init__(
            client=client, api_key=api_key, base_url=base_url, model=model, timeout_s=timeout_s
        )

    def _missing_sdk_message(self, exc: Exception) -> str:
        return (
            "cloud 模式需要 openai SDK。安装: pip3 install openai\n"
            "(开发机上装好后即可; 板端若不用 cloud 模式则不需要)\n"
            "原始错误: %s" % exc
        )

    def _missing_api_key_message(self) -> str:
        return "cloud 模式缺少 api_key (配置 llm.api_key)"


def _edge_base_url(port: Any) -> str:
    """llm.port -> 本机 OpenAI 兼容地址。"""
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = DEFAULT_EDGE_PORT
    return "http://127.0.0.1:%d/v1" % port


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
        @note edge_backend 不传时**按需**从 config 的 llm: 段造 (见 _require_edge) ——
              造后端本身不联网, 也不 import openai。
        """
        self._tools = tools
        self._config_loader = config_loader
        self._edge = edge_backend
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
        name = _MODE_ALIASES.get(name, name)
        if name in MODES:
            return name
        self._mode_errors.append(
            "unknown llm mode %r; falling back to %r (valid: %s)"
            % (mode, DEFAULT_MODE, ", ".join(MODES))
        )
        return DEFAULT_MODE

    def _load_llm_config(self) -> Optional[Dict[str, Any]]:
        """读 config 的 `llm:` 段; 读不到返回 None (原因记进 mode_errors)。

        只读一次真源里的这一段, 模式与 edge 参数都从这里取。
        """
        loader = self._config_loader
        if loader is None:
            return None
        try:
            data = loader()
        except Exception as exc:  # noqa: BLE001 - 配置读不到不该让 provider 起不来
            self._mode_errors.append("failed to load llm config: %r" % (exc,))
            return None
        if not isinstance(data, dict):
            return None
        llm = data.get("llm")
        return llm if isinstance(llm, dict) else None

    def _read_mode_from_config(self) -> str:
        """从配置里读 llm.mode。任何失败都退回 DEFAULT_MODE。"""
        llm = self._load_llm_config()
        value = llm.get("mode") if llm else None
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
            return await self._plain_chat(self._require_edge(), user_input, context)
        if mode == "cloud":
            return await self._plain_chat(self._require_cloud(), user_input, context)
        # 理论上到不了这里 (_normalize_mode 已兜底), 保底再兜一次
        return await self._rules.respond(user_input, context)

    async def _plain_chat(
        self,
        backend: "_OpenAICompatibleBackend",
        user_input: str,
        context: Dict[str, Any],
    ) -> str:
        """纯文本一次调用 (不带工具)。edge 与 cloud 共用。

        @note **失败照抛** (chat 的约定: 调用方要能知道模型挂了); 空正文返回 ""。
        """
        loop = asyncio.get_running_loop()
        messages = _build_messages(user_input, context, no_think=_no_think(backend))
        response = await loop.run_in_executor(
            None, lambda: backend.create(messages, [])
        )
        return _first_text(response) or ""

    def _require_cloud(self) -> CloudBackend:
        if self._cloud is None:
            raise OpenAIClientError(
                "cloud 模式未配置客户端 (构造时传 cloud_backend, 或确保 llm.api_key 已设置)"
            )
        return self._cloud

    def _require_edge(self) -> EdgeBackend:
        """取 edge 后端; 没注入就按 config 造一个 (造的时候不联网、不 import openai)。"""
        if self._edge is None:
            self._edge = EdgeBackend.from_config(self._load_llm_config())
        return self._edge

    # --------------------------------------------------- chat_with_tools ---
    async def chat_with_tools(
        self, user_input: str, context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """LLM 循环入口: 回复 + 调用过的工具。

        @return  {"ok": bool, "text": str, "tool_calls": [{"name","args","result"}],
                  "error": str|None, "mode": str, "degraded": str|None}
                 **永不抛异常** —— 错误收进 ok/error, 让上层循环能继续跑。
                 `degraded` 非空 = 这条不是模型答的, 是规则兜底 (原因在里面)。
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

        # edge 与 cloud 走**同一个循环** (T2): 都是 OpenAI 兼容接口 + function call
        if mode == "edge":
            return await self._chat_with_tools(self._require_edge(), user_input, context)

        try:
            backend = self._require_cloud()
        except LLMError as exc:
            return _result(False, "", [], str(exc), mode)
        return await self._chat_with_tools(backend, user_input, context)

    async def _chat_with_tools(
        self,
        backend: "_OpenAICompatibleBackend",
        user_input: str,
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """工具循环 (edge / cloud 共用)。

        与 `_plain_chat` 的区别不止"带工具": 这里**不抛** —— 后端挂了要么降级兜底
        (edge), 要么把原因收进 ok=False (cloud)。
        """
        messages = _build_messages(user_input, context, no_think=_no_think(backend))
        tool_schemas = _advertised_tools(self._tools)
        tool_calls: List[Dict[str, Any]] = []
        loop = asyncio.get_running_loop()

        for _round in range(self._max_tool_rounds):
            try:
                response = await loop.run_in_executor(
                    None, lambda: backend.create(messages, tool_schemas)
                )
            except Exception as exc:  # noqa: BLE001 - 网络/鉴权错误都收进返回值
                return await self._backend_failed(exc, tool_calls, user_input, context)

            text = _first_text(response)
            requested = _requested_tool_calls(response)

            if not requested:
                if text:
                    # 没有工具调用 -> 这一轮就是最终回复
                    return _result(True, text, tool_calls, None, self._mode)
                return await self._no_text(response, tool_calls, user_input, context)

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

    # ------------------------------------------------------- 后端失败 ---
    async def _backend_failed(
        self,
        exc: Exception,
        tool_calls: List[Dict[str, Any]],
        user_input: str,
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """后端调用失败。

        · edge: **退回规则兜底** (板端模型可能只是没起来, 不该连"几点了"都答不了),
                但结果里带 `degraded` 说清这不是模型答的。
        · cloud: 保持原样 —— ok=False + 原因, 上层 (main.handle_event) 会记 error 不回话。
        """
        reason = "%s: %s" % (type(exc).__name__, exc)
        if self._mode != "edge":
            return _result(False, "", tool_calls, reason, self._mode)
        return await self._degrade(reason, tool_calls, user_input, context)

    async def _no_text(
        self,
        response: Any,
        tool_calls: List[Dict[str, Any]],
        user_input: str,
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """模型这一轮既没要求调工具、也没有正文。

        edge 上这不是罕见情况: Qwen3-0.6B 把 token 预算烧在思考上时, 正文就是空的
        (finish_reason="length")。**不能当成"模型说了空话"** —— 那会看起来像正常答复。
        """
        reason = _no_text_reason(response)
        if self._mode != "edge":
            # cloud 保持既有行为 (返回空串 + ok=True); 理由见模块头的边界说明
            return _result(True, "", tool_calls, None, self._mode)
        return await self._degrade(reason, tool_calls, user_input, context)

    async def _degrade(
        self,
        reason: str,
        tool_calls: List[Dict[str, Any]],
        user_input: str,
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """规则兜底, 并**明确标出这是降级**。

        ⚠ 为什么答复正文里也要带一句: GUI 只显示 `text`, 看不到日志里的 warning。
        只有结果字典里的 `degraded` 的话, 界面上那条兜底回复看起来跟模型答的一样。
        """
        try:
            text = await self._rules.respond(user_input, context)
        except Exception as exc:  # noqa: BLE001
            return _result(
                False,
                "",
                tool_calls,
                "%s; 规则兜底也失败: %s: %s" % (reason, type(exc).__name__, exc),
                self._mode,
            )
        return _result(
            True, _DEGRADED_PREFIX + text, tool_calls, None, self._mode, degraded=reason
        )

    # ------------------------------------------------------------ 诊断 ---
    @property
    def tools(self) -> Optional[ToolRouter]:
        return self._tools

    @property
    def rule_engine(self) -> RuleEngine:
        return self._rules

    @property
    def edge_backend(self) -> EdgeBackend:
        """当前 edge 后端 (没注入就按 config 造一个)。"""
        return self._require_edge()

    def __repr__(self) -> str:
        return "<LLMProvider mode=%s tools=%s>" % (
            self._mode,
            len(self._tools) if self._tools is not None else 0,
        )


# ---------------------------------------------------------------------------
#  辅助
# ---------------------------------------------------------------------------
def _result(
    ok: bool,
    text: str,
    tool_calls: List[Dict[str, Any]],
    error: Optional[str],
    mode: str,
    degraded: Optional[str] = None,
) -> Dict[str, Any]:
    """chat_with_tools 的结果 (形状固定 —— 见 chat_with_tools 的 @return)。

    `degraded` 非空 = 这条不是模型答的: 后端挂了/没给出正文, 已退回规则兜底。
    `tool_failures` = 本次**失败过**的工具（T7-4）: 那几句已经追加在 `text` 末尾,
    这里再单独留一份给测试/上层查。
    """
    failures = _tool_failure_notes(tool_calls)
    if failures:
        text = ("%s\n\n%s" % (text, "\n".join(failures))) if text else "\n".join(failures)
    return {
        "ok": ok,
        "text": text,
        "tool_calls": tool_calls,
        "error": error,
        "mode": mode,
        "degraded": degraded,
        "tool_failures": failures,
    }


#: 工具失败时追加在正文前的那句（给**用户**看的, 所以是中文整句）
_TOOL_FAILED_PREFIX = "⚠ "


def _tool_failure_notes(tool_calls: Optional[List[Dict[str, Any]]]) -> List[str]:
    """把"失败过的工具"变成几句**必须让用户看到**的话（T7-4 (c)）。

    为什么要在**这里**做（而不是只靠工具结果里那句提示）: 板端实测 0.6B 会
    **谎报成功** —— 工具明明返回 `{"ok": false, "error": "scene 轴上没有 darkness…"}`,
    它回给用户的却是"已更换为宁静的深色风景"。工具结果只有模型看得见,
    用户看不到; 所以失败必须在**最终正文**里出现, 不能指望模型转述。

    @return ["⚠ 换壁纸没有成功：…", …]（去重、保持调用顺序）; 没有失败就是 []
    """
    notes: List[str] = []
    seen = set()
    for call in tool_calls or ():
        if not isinstance(call, Mapping):
            continue
        line = _tool_failure_note(call)
        if not line or line in seen:
            continue
        seen.add(line)
        notes.append(line)
    return notes


def _tool_failure_note(call: Mapping[str, Any]) -> str:
    """一次工具调用 -> "一句给人看的失败说明"；没失败就返回 ""。

    两种失败都要认:
      · 路由层的失败 —— `{"ok": false, "error": "…"}`（未知工具 / 状态不允许 /
        参数不合法 / 超时）
      · handler 自己的失败 —— `{"ok": true, "result": {"ok": false, …}}`
        （例如"还没有标签数据, 先跑 assistant tag"）; 这时优先用 `tell_user`
        —— 那是工具**已经写好的整句**（"换壁纸没有成功：…"），别再套一层前缀
    """
    if not isinstance(call, Mapping):
        return ""
    name = str(call.get("name") or "工具")
    result = call.get("result")
    if not isinstance(result, Mapping):
        return ""
    if result.get("ok") is False:
        return "%s%s 没有成功：%s" % (_TOOL_FAILED_PREFIX, name,
                                     result.get("error") or "原因不明")
    payload = result.get("result")
    if isinstance(payload, Mapping) and payload.get("ok") is False:
        tell_user = payload.get("tell_user")
        if tell_user:
            return "%s%s" % (_TOOL_FAILED_PREFIX, tell_user)
        return "%s%s 没有成功：%s" % (_TOOL_FAILED_PREFIX, name,
                                     payload.get("error") or "原因不明")
    return ""


def _no_think(backend: Any) -> bool:
    """这个后端要不要关思考。只有 EdgeBackend 有这个开关 (Qwen3 专用)。"""
    return bool(getattr(backend, "no_think", False))


def _advertised_tools(tools: Optional[ToolRouter]) -> List[Dict[str, Any]]:
    """丢给模型的工具清单 —— **按当前状态过滤**（T4）。

    为什么不是 `list_tools()`: 权限控制的另一半是"不可用的工具根本不该出现在候选里"
    （见 `ToolRouter.allowed_tools()` 的说明）。不过滤的话, SLEEP/GAME 下模型仍会看到
    `back_to_desktop` 并尝试调用, 然后吃一个"不允许"的拒绝 —— 白花一轮, 答复还容易
    变成"我做不到"。执行期的 fail-closed 校验照旧（这里是**少给**, 不是**放宽**）。
    """
    if tools is None:
        return []
    allowed = getattr(tools, "allowed_tools", None)
    if callable(allowed):
        return allowed()
    return tools.list_tools()


def _build_messages(
    user_input: str, context: Dict[str, Any], no_think: bool = False
) -> List[Dict[str, Any]]:
    """组装 messages。

    context 非空时塞进 system 消息 —— 不做模板管理, 就是一句 JSON。

    `no_think=True`（edge 默认）时在**两处**写 Qwen3 的软开关 `/no_think`:
    **system 末尾** + **最后一条 user 消息末尾**。

    ⚠ 为什么两处都写（T8-5b 板端实测）: 只写在 system 里时, 模型**时不时还是会想**
    （6 条提示词里有 1 条产出了 339 字的 `reasoning_content`, 那一轮要 211 s）;
    把同样的 `/no_think` 再放到 user 消息末尾后, 同一批 6 条**思考全为 0 字**、
    单轮从 11~211 s 收到 13~54 s, 工具调用成功率也从 1/6 升到 4/6。
    注意这不是"模板在帮我们"—— 模型的 chat 模板里**没有** `/no_think` 的处理
    （只有 `enable_thinking` 变量）, 起作用的是模型自己学过的软开关,
    所以位置越靠近生成点越可靠。
    """
    system = _SYSTEM_PROMPT
    if no_think:
        system = system + " " + _NO_THINK_SUFFIX
    messages: List[Dict[str, Any]] = [{"role": "system", "content": system}]

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

    if no_think:
        user_input = "%s %s" % (user_input, _NO_THINK_SUFFIX)
    messages.append({"role": "user", "content": user_input})
    return messages


def _finish_reason(response: Any) -> Optional[str]:
    choices = getattr(response, "choices", None)
    if not choices:
        return None
    return getattr(choices[0], "finish_reason", None)


def _reasoning_text(response: Any) -> Optional[str]:
    """llama-server 把 Qwen3 的思考内容放在 message.reasoning_content 里。"""
    choices = getattr(response, "choices", None)
    message = getattr(choices[0], "message", None) if choices else None
    value = getattr(message, "reasoning_content", None) if message is not None else None
    return value if isinstance(value, str) and value else None


def _no_text_reason(response: Any) -> str:
    """"模型没给正文"的具体原因 —— 要能直接看出是不是思考把预算烧完了。"""
    reason = _finish_reason(response)
    thinking = _reasoning_text(response)
    if thinking and reason == "length":
        return (
            "模型只产出了 %d 字的思考内容、正文为空 (finish_reason=length) —— "
            "token 预算被思考吃掉了" % len(thinking)
        )
    if thinking:
        return "模型只产出了思考内容、正文为空 (finish_reason=%r)" % (reason,)
    return "模型没有给出正文 (finish_reason=%r)" % (reason,)


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
