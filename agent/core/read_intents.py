#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent/core/read_intents.py — **只读问句直连**（Phase 7 T8-5c）

为什么要有它
    板端实测（T8-5b 那批 6 条提示词）: 0.6B **不肯为"现在在放什么"调 `status`**
    —— 它把问题原样复读一遍（"现在在放什么"）。这是模型的能力边界, 不是接线问题:
    这类问句的答案**只存在于设备状态里**（在放哪首 / 库里有什么 / 壁纸有哪些标签），
    模型既看不到也编不出来。

    所以定成一条分工（你 T8-5c 拍的板）::

        只读问句  ->  **直连**: 规则命中就拿 Runtime 的真实数据拼一句话, **不进模型**
        写意图    ->  照旧进模型（它负责决定"做什么", 以及怎么把失败说清楚）

    ⚠ 直连的答案与工具用的是**同一份数据**（同一个 `Runtime.music_state()` /
      `music_candidates()` / `wallpaper_tags()`），所以两边数字一定一致。

边界（刻意的）
    · **只认"读状态"** —— 不含问候/时间/闲聊（那些在 edge 模式下仍交给模型, 只有
      没有模型时由 `agent/llm/rule_engine.py` 兜底）。理由: 直连回答是**固定话术**,
      用在"报数据"上没损失, 用在闲聊上会把对话变僵。
    · **不认识就返回 None**（交回模型）—— 宁可多花一次推理, 也不猜。
    · **依赖缺了也返回 None**（音乐没开 / 标签数据没打）—— 直连不假装知道。
    · 正则按顺序匹配, **第一个命中即返回**（与 RuleEngine 同一条约定: 顺序即优先级）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Pattern, Sequence, Union

__all__ = ["ReadIntent", "ReadAnswer", "INTENTS", "answer", "looks_like_read"]

_log = logging.getLogger(__name__)

#: 直连回答的处理函数: (services, 匹配对象) -> 一句话; 拿不到数据就返回 None
AnswerHandler = Callable[[Mapping[str, Any], Optional["re.Match"]], Optional[str]]


@dataclass
class ReadAnswer:
    """一次直连回答。

    @param intent 命中的意图名（日志与测试要看"是哪条规则答的"）
    @param text   给用户看的那句话
    """

    intent: str
    text: str


@dataclass
class ReadIntent:
    """一条"只读问句 -> 直连回答"的规则。

    @param name    规则名（诊断用）
    @param pattern 正则（大小写不敏感）; 也可以给多个, 任一命中即算
    @param handler 拼回答; **拿不到数据必须返回 None**（那就交回模型）
    @param needs   需要的 services 键（缺一个就不算命中 —— 免得答出"音乐没开"这种
                   本来该由工具/模型说清的话）
    """

    name: str
    pattern: Union[str, Sequence[str]]
    handler: AnswerHandler
    needs: Sequence[str] = ()

    #: 编译后的正则（__post_init__ 填）
    _regexes: List[Pattern[str]] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        patterns = [self.pattern] if isinstance(self.pattern, str) else list(self.pattern)
        self._regexes = [re.compile(p, re.IGNORECASE) for p in patterns]

    def match(self, text: str) -> Optional["re.Match"]:
        for regex in self._regexes:
            found = regex.search(text or "")
            if found:
                return found
        return None


# ---------------------------------------------------------------------------
#  话术零件（都从**真实数据**拼, 不做任何猜测）
# ---------------------------------------------------------------------------
def _clock(seconds: Any) -> str:
    """秒 -> `m:ss`（报进度用; 数值不对就当 0）。"""
    try:
        total = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return "0:00"
    return "%d:%02d" % (total // 60, total % 60)


def _failure_text(payload: Any) -> Optional[str]:
    """入口自己报了 `ok=False` -> 把它那句**能照做**的话原样用（读失败也是答案）。

    @note 例如"音乐没开（config.yaml 的 music.enabled）"、"还没有壁纸标签数据 —— 先跑
          assistant tag --apply"。这类话**不该**丢回模型去猜（它只会编）。
    """
    if not isinstance(payload, Mapping) or payload.get("ok") is not False:
        return None
    return str(payload.get("tell_user") or payload.get("error") or "") or None


def _status_text(services: Mapping[str, Any], _match: Optional["re.Match"]) -> Optional[str]:
    state = services["music_state"]()
    problem = _failure_text(state)
    if problem:
        return "现在报不出播放状态：%s" % problem
    if not isinstance(state, Mapping):
        return None
    if not state.get("track_id"):
        return "现在没在放歌。想听什么就说一句「放一首听得最少的」。"
    progress = "%s/%s" % (_clock(state.get("position_s")), _clock(state.get("duration_s")))
    playing = "在放" if state.get("playing") else "停着"
    who = state.get("artist") or "未知歌手"
    plays = state.get("plays")
    tail = "（已经算过一次播放）" if plays else ""
    return "现在%s《%s》—— %s，%s。%s" % (playing, state.get("title") or state.get("track_id"),
                                        who, progress, tail)


def _library_text(services: Mapping[str, Any], match: Optional["re.Match"]) -> Optional[str]:
    found = services["music_list"](tag=None, sort="plays_asc", limit=3)
    problem = _failure_text(found)
    if problem:
        return "现在报不出本地库：%s" % problem
    if not isinstance(found, Mapping):
        return None
    summary = found.get("summary") or {}
    tracks = found.get("tracks") or []
    if not summary.get("count"):
        return "本地音乐库还是空的 —— 可以让我用「搜一首…」去 PC 上找，或先导入歌单。"
    names = "、".join("《%s》（%s 次）" % (t.get("name"), t.get("plays") or 0)
                     for t in tracks if t.get("name"))
    text = "本地库里有 %d 首歌，一共听了 %d 次。" % (summary.get("count"),
                                              summary.get("total_plays") or 0)
    if names:
        text += "听得最少的三首：%s。" % names
    return text


def _wallpaper_tags_text(services: Mapping[str, Any],
                         _match: Optional["re.Match"]) -> Optional[str]:
    summary = services["wallpaper_tags"]()
    problem = _failure_text(summary)
    if problem:
        return "现在报不出壁纸标签：%s" % problem
    if not isinstance(summary, Mapping):
        return None
    axes = summary.get("axes") or {}
    parts = []
    for axis, items in axes.items():
        labels = "、".join("%s(%d)" % (item.get("label"), item.get("count"))
                           for item in (items or [])[:5] if item.get("label"))
        if labels:
            parts.append("%s: %s" % (axis, labels))
    if not parts:
        return None
    return "壁纸库有 %d 张，标签是 —— %s。（想换就说「换一张 scene=xxx 的壁纸」）" % (
        summary.get("count") or 0, "；".join(parts))


#: T8-6: "哪张壁纸用得最少"那三条规则的**护栏**（整句范围内生效）:
#:   · 整句必须有一个**疑问词**（哪/什么/啥/几/多少/吗/？）;
#:   · 整句**不能**出现"换一张/放一首/挑一张"这类**动作**（写意图不许被截胡）。
#: 教训来自 T8-5c: 一条只读规则写松了就会把"现在在放什么"这种音乐问句抢走;
#: 宁可漏答（交回模型）, 也不抢写意图 —— 用户说"换一张用得最少的壁纸"是要**换图**。
_USAGE_GUARD = (r"^(?!.*(换一|换个|换张|来一|来首|放首|放一|给我|挑一|清空))"
                r"(?=.*(哪|什么|啥|几|多少|吗|？|\?|how|what|which))[\s\S]*?")


def _wallpaper_usage_text(services: Mapping[str, Any],
                          _match: Optional["re.Match"]) -> Optional[str]:
    """"哪张壁纸我用得最少"（T8-6）—— 也是**只读**，同样不该过模型。"""
    summary = services["wallpaper_tags"]()
    problem = _failure_text(summary)
    if problem:
        return "现在报不出壁纸用量：%s" % problem
    if not isinstance(summary, Mapping):
        return None
    usage = summary.get("usage")
    if not isinstance(usage, Mapping):
        return None

    def names(items, limit, only_used=False):
        picked = []
        for item in items or ():
            if not item.get("name"):
                continue
            used = item.get("used") or 0
            if only_used and used <= 0:
                continue
            picked.append("%s（%d 次）" % (item.get("name"), used))
            if len(picked) >= limit:
                break
        return "、".join(picked)

    least = names(usage.get("least_used"), 3)
    if not least:
        return None
    total = usage.get("total_used") or 0
    never = usage.get("never_used") or 0
    if not total:
        # ⚠ 全是 0 时**别报"用得最多的是谁"** —— 那只是按文件名排出来的先后, 不是真的用过
        #   （板端第一次问就撞上: "用得最多的是 wallhaven-…（0 次）" 看着像在胡说）
        return ("壁纸还一次都没换过（%d 张, 每张都是 0 次）—— 换几张之后我再告诉你哪张最少。"
                % never)
    text = "壁纸一共换过 %d 次，还有 %d 张没被换到过。用得最少的是 %s。" % (total, never, least)
    most = names(usage.get("most_used"), 1, only_used=True)
    if most:
        text += "用得最多的是 %s。" % most
    return text + "（想换就说「换一张用得最少的壁纸」）"


def _wallpaper_now_text(services: Mapping[str, Any],
                        _match: Optional["re.Match"]) -> Optional[str]:
    snapshot = services["wallpaper_state"]()
    if not isinstance(snapshot, Mapping) or not snapshot.get("path"):
        return None
    import os

    # ⚠ 报给用户的是**从 1 开始**的序号（游标本身是 0 基, 见 WallpaperDeck.snapshot）
    index = snapshot.get("index")
    shown = (int(index) + 1) if isinstance(index, int) else "?"
    return "现在显示的是第 %s/%s 张：%s。" % (shown, snapshot.get("total"),
                                          os.path.basename(str(snapshot["path"])))

#: 规则表（顺序即优先级）。⚠ 只读问句; 写意图**不要**往里加。
INTENTS: List[ReadIntent] = [
    ReadIntent(
        "music_status",
        # "现在在放什么" / "在放哪首歌" / "放的是什么" / "听的是啥" / "当前播放"
        [r"(现在|当前|此刻)?\s*(在)?\s*(放|听|播)的?\s*(是)?\s*(什么|啥|哪(一)?首|哪首歌)",
         r"(当前|现在)?\s*(播放|歌曲|曲目)\s*(是什么|是啥|状态)?\s*[?？]?$",
         r"what.{0,12}playing"],
        _status_text,
        needs=("music_state",),
    ),
    ReadIntent(
        "music_library",
        # "库里有什么歌" / "本地库有哪些音乐" / "歌单里都有啥"
        [r"(本地)?(音乐)?(库|歌单)(里|中)?\s*(都)?\s*(有|存)(什么|哪些|啥)",
         r"(有|存)了?\s*(什么|哪些|几首)\s*(歌|音乐)",
         r"what.{0,12}(songs|music).{0,12}(library|have)"],
        _library_text,
        needs=("music_list",),
    ),
    ReadIntent(
        "wallpaper_usage",                     # T8-6
        # "哪张壁纸我用得最少" / "我用得最多的壁纸是哪张" / "壁纸都用过几次"
        [_USAGE_GUARD + r"(壁纸|图片).{0,8}(用|看|换)(得|过|的)?.{0,6}(最少|最多|几次|多少次|几回)",
         _USAGE_GUARD + r"(壁纸|图片).{0,10}(没|不)(怎么|太|常)?\s*(用|看|换)",
         _USAGE_GUARD + r"(最|最少|最多)\s*(用|看|换)(过|得)?\s*(的)?\s*(壁纸|图片)",
         _USAGE_GUARD + r"(用|看|换)(得|过)?\s*(最少|最多).{0,6}(壁纸|图片)",
         _USAGE_GUARD + r"wallpaper.{0,20}(usage|how many times)",
         _USAGE_GUARD + r"(how many times|usage).{0,30}wallpaper"],
        _wallpaper_usage_text,
        needs=("wallpaper_tags",),
    ),
    ReadIntent(
        "wallpaper_tags",
        # "有哪些壁纸标签" / "壁纸标签有哪些" / "壁纸库里有什么风格"
        [r"(壁纸|图片).{0,6}(标签|风格|分类).{0,6}(有|是)?\s*(什么|哪些|啥)",
         r"(有|存在)\s*(什么|哪些|啥)\s*(壁纸)?(标签|风格)",
         r"wallpaper.{0,20}tags?\b"],
        _wallpaper_tags_text,
        needs=("wallpaper_tags",),
    ),
    ReadIntent(
        "wallpaper_now",
        # ⚠ 必须**锚住名词/动词**（壁纸/图、"显示的是"）—— 早先写成 "现在.*(是|放).*(什么|哪张)"
        #   会误命中"现在在放什么"（音乐问句被壁纸规则截胡, 板端口径就乱了）
        [r"(现在|当前).{0,4}(是|显示)(的)?\s*(哪张|哪一幅|什么)\s*(壁纸|图片|图)",
         r"(现在|当前)?\s*显示(的|出来)?\s*是\s*(哪张|哪一幅|什么)",
         r"(第几|第几张)\s*(壁纸|图)"],
        _wallpaper_now_text,
        needs=("wallpaper_state",),
    ),
]


def answer(text: str, services: Optional[Mapping[str, Any]] = None) -> Optional[ReadAnswer]:
    """把一句**只读问句**直连回答掉。

    @param services 只读入口（`Runtime` 装配时给的那几个）; 缺某个键 = 那条规则不算命中
    @return 命中并拿到数据 -> `ReadAnswer`; 否则 None（交给模型）
    @note handler 抛异常 / 返回空 → 记 warning 并**当没命中**（直连不该把一轮对话搞崩）
    """
    services = services or {}
    for intent in INTENTS:
        if any(not callable(services.get(key)) for key in intent.needs):
            continue
        found = intent.match(text or "")
        if not found:
            continue
        try:
            reply = intent.handler(services, found)
        except Exception as exc:  # noqa: BLE001 - 直连失败就退回模型
            _log.warning("read_intents: %s 拼回答失败 (交回模型): %r", intent.name, exc)
            return None
        if reply:
            return ReadAnswer(intent=intent.name, text=str(reply))
        # 命中但拿不到数据（例如库是空的）: **交回模型**, 别在这里硬说
        return None
    return None


def looks_like_read(text: str, services: Optional[Mapping[str, Any]] = None) -> bool:
    """只做匹配、不调 handler（给测试/日志用）。"""
    services = services or {}
    for intent in INTENTS:
        if any(not callable(services.get(key)) for key in intent.needs):
            continue
        if intent.match(text or ""):
            return True
    return False
