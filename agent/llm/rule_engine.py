# ============================================================================
#  agent/llm/rule_engine.py — 无 LLM 时的规则兜底
#
#  什么时候用它
#  ---------------------------------------------------------------------------
#  LLM 不可用时 (mode="disabled", 或 edge 模型还没装好) 系统不能变成哑巴:
#  至少要能回应问候、报时间、报状态、拒绝能力之外的请求。这就是 RuleEngine。
#
#  它**不是** LLM 的替代品
#  ---------------------------------------------------------------------------
#  规则引擎只会"匹配 -> 回固定话术", 不做理解、不做多轮推理。它存在的意义是
#  **明确地告诉用户"我现在没有大脑, 只能做这几件事"**, 而不是假装能聊。
#
#  匹配方式
#  ---------------------------------------------------------------------------
#  规则用**正则**匹配 (re.IGNORECASE)。用正则而不是关键字包含, 是因为:
#    · 关键字包含无法表达"以...开头/结尾", 容易误命中 ("你好吗" vs "你好")
#    · 中文没有词边界, \b 不好用, 正则反而更直白
#  规则按注册顺序匹配, **第一个命中即返回** —— 顺序就是优先级, 引入独立
#  优先级字段只会让"为什么这条没生效"更难查。
#
#  设计边界 (按约定不做的事)
#  ---------------------------------------------------------------------------
#  · **不做** prompt 模板管理 —— 话术直接写在 _BUILTIN_RULES 里
#  · **不做** token 计数 / 成本统计
#  · **不做** 真实的意图理解 —— 规则没命中就走默认话术
# ============================================================================

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Pattern, Sequence, Union

__all__ = ["Rule", "RuleEngine", "RuleHandler"]


#: 规则命中时的处理函数: (匹配对象或 None, 用户输入, context) -> str
RuleHandler = Callable[[Optional["re.Match"], str, Dict[str, Any]], str]


@dataclass
class Rule:
    """一条兜底规则。

    @param name    规则名 (诊断用; 命中了哪条一眼能看出来)
    @param pattern 正则 (大小写不敏感)。也接受多个正则, 任一命中即算命中。
    @param handler 生成回复的可调用对象; 返回 str
    """

    name: str
    pattern: Union[str, Sequence[str]]
    handler: RuleHandler

    #: 编译后的正则 (由 __post_init__ 填)
    _regexes: List[Pattern[str]] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        patterns = [self.pattern] if isinstance(self.pattern, str) else list(self.pattern)
        if not patterns:
            raise ValueError("rule %r must have at least one pattern" % self.name)
        for p in patterns:
            if not isinstance(p, str):
                raise TypeError("rule %r pattern must be a string, got %r" % (self.name, p))
            # 编译期就报错, 免得等到第一次匹配才发现正则写错
            self._regexes.append(re.compile(p, re.IGNORECASE))
        if not callable(self.handler):
            raise TypeError("rule %r handler must be callable" % self.name)

    def match(self, text: str) -> Optional["re.Match"]:
        """返回第一个命中的匹配对象; 都没命中返回 None。"""
        for rx in self._regexes:
            m = rx.search(text)
            if m is not None:
                return m
        return None


# ---------------------------------------------------------------------------
#  内置话术
# ---------------------------------------------------------------------------
def _say(text: str) -> RuleHandler:
    """常量话术的 handler 工厂。"""

    def handler(_m, _text, _ctx):
        return text

    return handler


def _state_of(context: Dict[str, Any]) -> Optional[str]:
    """从 context 里取当前状态名。

    同时接受 "state" (字符串或 State) 与 "state_machine" (有 current() 的对象),
    这样调用方不用为了给规则引擎用而特意转换一次。
    """
    state = context.get("state")
    if state is None:
        machine = context.get("state_machine")
        if machine is not None:
            try:
                state = machine.current()
            except Exception:  # noqa: BLE001
                return None
    if state is None:
        return None
    # State 是 Enum, 取 .value; 字符串直接用
    return getattr(state, "value", None) or (state if isinstance(state, str) else None)


#: 内置规则 (顺序即优先级)
_BUILTIN_RULES: List[Rule] = [
    Rule(
        "greeting",
        [r"^\s*(你好|您好|hi|hello|hey|早上好|晚上好)"],
        _say("你好，我是板端助手。当前处于简易模式（未接入大模型），可以问我时间或状态。"),
    ),
    Rule(
        "time",
        [r"几点", r"时间", r"\btime\b", r"现在.*(点|时候)"],
        lambda _m, _t, ctx: "现在是 %s。" % (
            (ctx.get("now") or datetime.now)().strftime("%Y-%m-%d %H:%M:%S")
        ),
    ),
    Rule(
        "state",
        [r"状态", r"在干什么", r"在做什么", r"\bstate\b", r"\bstatus\b"],
        lambda _m, _t, ctx: "当前状态：%s。" % (_state_of(ctx) or "未知"),
    ),
    Rule(
        "capability",
        [r"能做什么", r"会什么", r"有什么功能", r"你是谁", r"帮助", r"\bhelp\b"],
        _say(
            "简易模式下我只会回固定规则：问候、报时间、报状态。"
            "接入大模型后可以对话并调用工具。"
        ),
    ),
    Rule(
        "stop",
        [r"^\s*(停|别说了|闭嘴|stop|cancel)\s*[。.!！]?\s*$"],
        _say("好的，已停止。"),
    ),
    Rule(
        "farewell",
        [r"再见", r"拜拜", r"\bbye\b", r"晚安"],
        _say("再见。"),
    ),
]

#: 没命中任何规则时的默认话术。
#:
#: 刻意说清"我没听懂"而不是瞎编 —— 规则引擎不知道答案时说不知道, 比编一个更像样。
_DEFAULT_REPLY = "简易模式下我听不懂这句话。可以问我「现在几点」「当前状态」或「能做什么」。"


class RuleEngine:
    """按正则规则给回复。

    典型用法::

        engine = RuleEngine()
        await engine.respond("现在几点", {"state": "idle"})

        # 追加自定义规则 (插在最前面 = 优先级最高)
        engine.add_rule(Rule("lights", r"开灯", lambda m, t, c: "已开灯。"), prepend=True)
    """

    def __init__(self, rules: Optional[Sequence[Rule]] = None) -> None:
        """
        @param rules 自定义规则;**给定时完全替换内置规则**(而不是追加),
                     这样测试与定制场景的行为是可预期的。
                     想在保留内置规则的基础上加, 用 add_rule()。
        """
        self._rules: List[Rule] = list(rules) if rules is not None else list(_BUILTIN_RULES)
        self._last_rule: Optional[str] = None

    # ------------------------------------------------------------ 规则管理 --
    def add_rule(self, rule: Rule, prepend: bool = False) -> None:
        """追加规则。

        @param prepend True 时插到最前 (优先级最高)
        """
        if not isinstance(rule, Rule):
            raise TypeError("add_rule() expects a Rule, got %s" % type(rule).__name__)
        if prepend:
            self._rules.insert(0, rule)
        else:
            self._rules.append(rule)

    def remove_rule(self, name: str) -> bool:
        """按名字移除规则。返回是否真的删掉了一个。"""
        for i, rule in enumerate(self._rules):
            if rule.name == name:
                del self._rules[i]
                return True
        return False

    @property
    def rules(self) -> List[Rule]:
        """当前规则 (只读副本)。"""
        return list(self._rules)

    @property
    def last_rule(self) -> Optional[str]:
        """最近一次命中的规则名; 走默认话术时为 None。"""
        return self._last_rule

    # ------------------------------------------------------------ 主要接口 --
    async def respond(self, user_input: str, context: Optional[Dict[str, Any]] = None) -> str:
        """按规则生成回复。

        @param user_input 用户输入
        @param context    可选上下文; 认得的键:
                             state / state_machine  当前状态 (报状态用)
                             now                    取当前时间的可调用对象 (测试用)
        @return 回复文本;**永不抛异常** —— 规则出错时退化成默认话术
        """
        context = context or {}
        text = user_input if isinstance(user_input, str) else str(user_input)

        for rule in self._rules:
            try:
                matched = rule.match(text)
            except Exception:  # noqa: BLE001 - 单条规则的正则出问题, 不该拖垮整个引擎
                continue
            if matched is None:
                continue
            try:
                reply = rule.handler(matched, text, context)
            except Exception:  # noqa: BLE001 - handler 出错就换默认话术, 但记下规则名
                self._last_rule = rule.name
                return _DEFAULT_REPLY
            self._last_rule = rule.name
            return reply if isinstance(reply, str) else str(reply)

        self._last_rule = None
        return _DEFAULT_REPLY

    def __repr__(self) -> str:
        return "<RuleEngine rules=%d last=%s>" % (len(self._rules), self._last_rule)
