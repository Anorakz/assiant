# ============================================================================
#  agent/core/tool_router.py — 工具注册 / 权限控制 / 执行调度
#
#  职责 (就这三件)
#  ---------------------------------------------------------------------------
#      注册    register(tool)          把工具登记进路由表
#      暴露    list_tools()            给 LLM 的 [{name, description, parameters}]
#      调度    await execute(name, args)  校验 -> 执行 -> 统一结果
#
#  一次 execute 的完整流程
#  ---------------------------------------------------------------------------
#      1. 工具存在?            否则 {"ok": False, "error": "unknown tool: x"}
#      2. **当前状态允许?**     否则 {"ok": False, "error": "tool 'x' not allowed in state 'sleep'"}
#      3. **参数符合 schema?**  否则 {"ok": False, "error": "invalid arguments: $...."}
#      4. 执行, 带**超时保护**  超时 -> {"ok": False, "error": "timeout after 5.0s"}
#      5. 异常捕获              任何异常 -> {"ok": False, "error": "RuntimeError: ..."}
#   成功一律 {"ok": True, "result": <handler 的返回值>}
#
#  设计边界 (按约定不做的事)
#  ---------------------------------------------------------------------------
#  · **不实现**任何具体工具 —— 工具在 agent/tools/, 本模块只认接口
#  · **不做**工具编排 —— 谁调谁、按什么顺序, 由 LLM 自己决定
#  · **不做**调用日志落盘 —— 需要审计的调用方自己包一层 handler
#
#  参数校验为什么自己写而不用 jsonschema
#  ---------------------------------------------------------------------------
#  实测宿主机 / WSL / 板端 (Ubuntu 20.04 + Python 3.8) **都没有** jsonschema,
#  也不想为了一个字段校验往板端塞一个依赖。所以这里实现 JSON Schema 的一个
#  明确子集 (见下面 SUPPORTED_KEYWORDS), 够覆盖工具的入参描述。
#  ⚠ 子集之外的关键字 (format / allOf / $ref / patternProperties ...) 会被
#    **静默忽略** —— 也就是说 schema 里写了它们不会报错, 但也不会生效。
#    用 validate_schema(..., permissive=False) 可以把这种情况变成报错。
# ============================================================================

from __future__ import annotations

import asyncio
import functools
import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from .state_machine import State, StateMachine

__all__ = [
    "Tool",
    "ToolRouter",
    "SchemaError",
    "SUPPORTED_KEYWORDS",
    "DEFAULT_TIMEOUT_S",
    "validate_args",
    "validate_schema",
]


#: 执行超时默认值 (秒)
DEFAULT_TIMEOUT_S = 5.0

#: 本模块实现的 JSON Schema 关键字子集
SUPPORTED_KEYWORDS: Set[str] = {
    "type", "properties", "required", "enum", "items",
    "additionalProperties", "minimum", "maximum", "minLength", "maxLength",
    "description", "title", "default",
}

#: 容器结构关键字: 不参与校验, 只是描述
_ANNOTATION_KEYWORDS: Set[str] = {"description", "title", "default"}

_TYPE_NAMES = ("object", "array", "string", "integer", "number", "boolean", "null")


class SchemaError(ValueError):
    """schema 本身不合法, 或参数不符合 schema。

    继承 ValueError, 与其他模块的错误约定一致 (既能 except SchemaError 精确捕获,
    也能 except ValueError 统一处理)。
    """


# ---------------------------------------------------------------------------
#  类型判定
# ---------------------------------------------------------------------------
def _is_type(value: Any, expected: str) -> bool:
    """按 JSON Schema 的语义判断 value 是否属于 expected 类型。

    ⚠ 关键细节: Python 里 bool 是 int 的子类, 但 JSON 里 boolean 与 integer 是
    **不同类型**。不特判的话 `{"type": "integer"}` 会放过 True —— 那正是最想
    拦住的输入 (True 拿去当坐标/数量会静默变成 1)。
    """
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "null":
        return value is None
    return False


def _type_label(value: Any) -> str:
    """给错误信息用的类型名 (与 JSON Schema 的叫法对齐)。"""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if value is None:
        return "null"
    return type(value).__name__


# ---------------------------------------------------------------------------
#  schema 结构校验
# ---------------------------------------------------------------------------
def validate_schema(schema: Any, path: str = "$", permissive: bool = True) -> None:
    """检查 schema 本身写得对不对 (不是检查数据)。

    @param permissive True (默认): 子集之外的关键字忽略, 不报错
                      False: 遇到不支持的关键字就报 SchemaError, 用来发现
                             "以为生效其实没生效" 的 schema
    @raise SchemaError
    """
    if not isinstance(schema, dict):
        raise SchemaError("%s: schema must be an object, got %s"
                          % (path, _type_label(schema)))

    if "type" in schema:
        t = schema["type"]
        if isinstance(t, list):
            bad = [x for x in t if x not in _TYPE_NAMES]
            if bad:
                raise SchemaError("%s.type: unknown type(s) %s" % (path, bad))
        elif t not in _TYPE_NAMES:
            raise SchemaError("%s.type: unknown type %r" % (path, t))

    if "required" in schema:
        req = schema["required"]
        if not isinstance(req, list) or not all(isinstance(x, str) for x in req):
            raise SchemaError("%s.required: must be a list of strings" % path)

    if "properties" in schema:
        props = schema["properties"]
        if not isinstance(props, dict):
            raise SchemaError("%s.properties: must be an object" % path)
        for key, sub in props.items():
            validate_schema(sub, "%s.properties.%s" % (path, key), permissive)

    if "items" in schema:
        validate_schema(schema["items"], "%s.items" % path, permissive)

    if "additionalProperties" in schema:
        ap = schema["additionalProperties"]
        if isinstance(ap, dict):
            validate_schema(ap, "%s.additionalProperties" % path, permissive)
        elif not isinstance(ap, bool):
            raise SchemaError("%s.additionalProperties: must be bool or object" % path)

    if "enum" in schema and not isinstance(schema["enum"], list):
        raise SchemaError("%s.enum: must be a list" % path)

    for key in ("minimum", "maximum", "minLength", "maxLength"):
        if key in schema and not isinstance(schema[key], (int, float)):
            raise SchemaError("%s.%s: must be a number" % (path, key))

    if not permissive:
        unsupported = sorted(
            k for k in schema
            if k not in SUPPORTED_KEYWORDS and k not in _ANNOTATION_KEYWORDS
        )
        if unsupported:
            raise SchemaError(
                "%s: unsupported schema keyword(s) %s (支持的关键字: %s)"
                % (path, unsupported, ", ".join(sorted(SUPPORTED_KEYWORDS)))
            )


# ---------------------------------------------------------------------------
#  参数校验
# ---------------------------------------------------------------------------
def validate_args(schema: Dict[str, Any], args: Any, path: str = "$") -> None:
    """按 schema 校验 args。

    @raise SchemaError 参数不符合 schema (错误信息带出错路径)
    """
    if not isinstance(schema, dict):
        raise SchemaError("%s: schema must be an object" % path)

    # ---- type ----
    if "type" in schema:
        expected = schema["type"]
        names = expected if isinstance(expected, list) else [expected]
        if not any(_is_type(args, n) for n in names):
            raise SchemaError(
                "%s: expected %s, got %s"
                % (path, " or ".join(names), _type_label(args))
            )

    # ---- enum ----
    if "enum" in schema and args not in schema["enum"]:
        raise SchemaError(
            "%s: %r is not one of %s" % (path, args, schema["enum"])
        )

    # ---- 数值/长度边界 ----
    if isinstance(args, (int, float)) and not isinstance(args, bool):
        if "minimum" in schema and args < schema["minimum"]:
            raise SchemaError("%s: %r < minimum %r" % (path, args, schema["minimum"]))
        if "maximum" in schema and args > schema["maximum"]:
            raise SchemaError("%s: %r > maximum %r" % (path, args, schema["maximum"]))

    if isinstance(args, str):
        if "minLength" in schema and len(args) < schema["minLength"]:
            raise SchemaError("%s: length %d < minLength %d"
                              % (path, len(args), schema["minLength"]))
        if "maxLength" in schema and len(args) > schema["maxLength"]:
            raise SchemaError("%s: length %d > maxLength %d"
                              % (path, len(args), schema["maxLength"]))

    # ---- object ----
    if isinstance(args, dict):
        for key in schema.get("required", []):
            if key not in args:
                raise SchemaError("%s: missing required property %r" % (path, key))

        props = schema.get("properties", {})
        for key, sub in props.items():
            if key in args:
                validate_args(sub, args[key], "%s.%s" % (path, key))

        # additionalProperties=False 时, 未声明的字段一律拒绝。默认 (不写) 是放行,
        # 与 JSON Schema 一致 —— 由各工具自己决定要不要收紧。
        if schema.get("additionalProperties") is False:
            extra = sorted(set(args) - set(props))
            if extra:
                raise SchemaError("%s: unexpected propert%s %s"
                                  % (path, "y" if len(extra) == 1 else "ies", extra))
        elif isinstance(schema.get("additionalProperties"), dict):
            sub = schema["additionalProperties"]
            for key in set(args) - set(props):
                validate_args(sub, args[key], "%s.%s" % (path, key))

    # ---- array ----
    if isinstance(args, list) and "items" in schema:
        item_schema = schema["items"]
        for i, item in enumerate(args):
            validate_args(item_schema, item, "%s[%d]" % (path, i))


# ---------------------------------------------------------------------------
#  Tool
# ---------------------------------------------------------------------------
@dataclass
class Tool:
    """一个可被 LLM 调用的工具。

    @param name           唯一名字 (LLM 用它来调用)
    @param description    给 LLM 看的说明
    @param schema         参数的 JSON Schema (见 SUPPORTED_KEYWORDS 子集)
    @param handler        实际执行体; 同步或 async 都可以
    @param allowed_states 允许在哪些状态下调用 (空集合 = 任何状态都不允许)
    """

    name: str
    description: str
    schema: Dict[str, Any]
    handler: Callable[..., Any]
    allowed_states: Set[State] = field(default_factory=set)

    def __post_init__(self) -> None:
        # 允许传 list/tuple/frozenset; 统一成 set 便于查找
        if not isinstance(self.allowed_states, set):
            self.allowed_states = set(self.allowed_states)
        # 允许传字符串 "idle" / "study"
        self.allowed_states = {
            s if isinstance(s, State) else State(str(s).strip().lower())
            for s in self.allowed_states
        }

    @property
    def is_async(self) -> bool:
        return inspect.iscoroutinefunction(self.handler)

    def to_llm_dict(self) -> Dict[str, Any]:
        """给 LLM 的形态 (OpenAI tools 风格)。"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.schema,
        }


# ---------------------------------------------------------------------------
#  ToolRouter
# ---------------------------------------------------------------------------
class ToolRouter:
    """工具注册表 + 权限闸门 + 执行调度。

    典型用法::

        sm = StateMachine()
        router = ToolRouter(state_provider=sm)

        router.register(Tool(
            name="screenshot",
            description="截取主机当前画面",
            schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=do_screenshot,
            allowed_states={State.GAME, State.STUDY},
        ))

        tools = router.list_tools()          # 丢给 LLM
        result = await router.execute("screenshot", {})
    """

    def __init__(
        self,
        state_provider: Optional[StateMachine] = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        permissive_schema: bool = True,
    ) -> None:
        """
        @param state_provider  提供 current() 的状态机; None 时自建一个
                               (接口只依赖 current(), 所以任何有这个方法的对象都行)
        @param timeout_s       单次执行超时 (秒), 默认 5.0
        @param permissive_schema 注册时是否放行 schema 子集之外的关键字
        """
        self._state_provider = state_provider if state_provider is not None else StateMachine()
        self._timeout_s = float(timeout_s)
        self._permissive_schema = bool(permissive_schema)
        self._tools: Dict[str, Tool] = {}

    # ------------------------------------------------------------ 注册 ---
    def register(self, tool: Tool) -> None:
        """登记一个工具。

        @raise TypeError   tool 不是 Tool, 或 handler 不可调用
        @raise SchemaError schema 结构不合法
        @raise ValueError  名字为空, 或与已注册工具重名
        """
        if not isinstance(tool, Tool):
            raise TypeError("register() expects a Tool, got %s" % type(tool).__name__)
        if not tool.name or not str(tool.name).strip():
            raise ValueError("tool name must not be empty")
        if not callable(tool.handler):
            raise TypeError("tool %r handler is not callable" % tool.name)
        if tool.name in self._tools:
            raise ValueError("tool %r is already registered" % tool.name)

        # 注册时就校验 schema 结构: 写错的 schema 越早暴露越好, 不要等第一次调用
        validate_schema(tool.schema, "$", permissive=self._permissive_schema)

        self._tools[tool.name] = tool

    def unregister(self, name: str) -> bool:
        """注销工具。返回是否真的删掉了一个。"""
        return self._tools.pop(name, None) is not None

    # ------------------------------------------------------------ 查询 ---
    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def names(self) -> List[str]:
        return sorted(self._tools)

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def list_tools(self) -> List[Dict[str, Any]]:
        """给 LLM 的工具清单: [{name, description, parameters}, ...]。

        按名字排序, 保证同样的注册集合每次顺序一致 —— 方便缓存 prompt 与比对测试。
        """
        return [self._tools[name].to_llm_dict() for name in self.names()]

    def allowed_tools(self) -> List[Dict[str, Any]]:
        """只列出**当前状态**下可用的工具。

        不算签名要求的接口, 但给 LLM 看的清单按状态过滤是权限控制的一部分:
        不可用的工具根本不该出现在候选里。
        """
        current = self.current_state()
        return [
            self._tools[name].to_llm_dict()
            for name in self.names()
            if current in self._tools[name].allowed_states
        ]

    def current_state(self) -> Optional[State]:
        """当前状态 (来自 state_provider); 拿不到时返回 None。

        provider 没实现 current() 或它抛异常时返回 None —— 权限检查会因此
        判定"任何工具都不可用"(fail closed), 而不是放行。
        """
        provider = self._state_provider
        if provider is None:
            return None
        try:
            state = provider.current()
        except Exception:  # noqa: BLE001
            return None
        return state if isinstance(state, State) else None

    def is_allowed(self, name: str) -> bool:
        """当前状态下 name 是否允许调用 (不存在或不允许都返回 False)。"""
        tool = self._tools.get(name)
        if tool is None:
            return False
        state = self.current_state()
        return state is not None and state in tool.allowed_states

    # ------------------------------------------------------------ 执行 ---
    async def execute(self, name: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """校验并执行工具。

        @param name 工具名
        @param args 参数 dict (None 视为 {})
        @return 成功 {"ok": True, "result": ...}
                失败 {"ok": False, "error": "<原因>"}
                永不抛异常 —— 调用方 (LLM 循环) 只需要看 ok。
        """
        if args is None:
            args = {}

        # 1) 工具存在?
        tool = self._tools.get(name)
        if tool is None:
            return {"ok": False, "error": "unknown tool: %s" % name}

        # 2) 状态允许? (fail closed: 不知道当前状态就不放行)
        state = self.current_state()
        if state is None:
            return {
                "ok": False,
                "error": "cannot determine current state; refusing to run tool %r" % name,
            }
        if state not in tool.allowed_states:
            return {
                "ok": False,
                "error": "tool %r not allowed in state %r (allowed: %s)"
                % (name, state.value, self._states_label(tool)),
            }

        # 3) 参数符合 schema?
        try:
            validate_args(tool.schema, args)
        except SchemaError as exc:
            return {"ok": False, "error": "invalid arguments: %s" % exc}

        # 4/5) 执行 + 超时 + 异常
        return await self._run(tool, args)

    async def _run(self, tool: Tool, args: Dict[str, Any]) -> Dict[str, Any]:
        """真正执行 handler, 带超时与异常兜底。"""
        try:
            if tool.is_async:
                result = await asyncio.wait_for(
                    tool.handler(**args), timeout=self._timeout_s
                )
            else:
                # 同步 handler 丢到线程池, 别卡住事件循环 —— 工具里可能有
                # 文件/网络/刷屏之类的阻塞调用。
                loop = asyncio.get_running_loop()
                call = functools.partial(tool.handler, **args)
                result = await asyncio.wait_for(
                    loop.run_in_executor(None, call), timeout=self._timeout_s
                )
        except asyncio.TimeoutError:
            # ⚠ 同步 handler 的超时只能"放弃等待", 线程本身还会跑完 (Python 没法
            #    强杀线程)。所以超时后那个工具可能仍在后台动, 调用方要自己考虑
            #    幂等/清理。异步 handler 则是真的被 cancel 掉。
            return {
                "ok": False,
                "error": "tool %r timed out after %.3gs" % (tool.name, self._timeout_s),
            }
        except asyncio.CancelledError:
            # 调用方被取消: 不能吞, 否则上层取消逻辑会失灵
            raise
        except (KeyboardInterrupt, SystemExit):
            # 进程级信号也不该被当成"某个工具失败了"
            raise
        except BaseException as exc:  # noqa: BLE001
            # 兜到这里的是 Exception + 其余非常规 BaseException。
            # 用 BaseException 而不是 Exception: 工具里偶尔会 raise
            # BaseException 子类 (或某些库在 C 层抛出的非常规异常), 让它们
            # 违反"execute 永不抛"的契约会很难查。真正的进程级控制流
            # (中断/退出/取消) 已在上面单独放行。
            return {
                "ok": False,
                "error": "%s: %s" % (type(exc).__name__, exc),
            }

        return {"ok": True, "result": result}

    # ------------------------------------------------------------ 内部 ---
    @staticmethod
    def _states_label(tool: Tool) -> str:
        if not tool.allowed_states:
            return "(none)"
        return ", ".join(sorted(s.value for s in tool.allowed_states))

    def __repr__(self) -> str:
        return "<ToolRouter tools=%d state=%s timeout=%.1fs>" % (
            len(self._tools),
            self.current_state().value if self.current_state() else "?",
            self._timeout_s,
        )
