# ============================================================================
#  agent/tools/wallpaper.py — "壁纸" 工具（Phase 7 T3；T7-3 能按内容挑；T8-5b 合并）
#
#  它做什么: 一个工具管壁纸的**全部**动作（`action` 决定干哪件）:
#
#      action="next"    往后翻一张（默认）
#      action="prev"    往前翻一张
#      action="repeat"  重推当前这张
#      action="pick"    按 `match=` 挑（按内容/按 IP 检索，最像的在最前）
#      action="tags"    只读: 库里有哪些标签 / 某个作品最像哪几张
#
#  为什么合并（T8-5b, 你定的"进一步抽象简化"）
#  ---------------------------------------------------------------------------
#  板端实测: 工具清单占第一轮 prompt 的 **90%**（6 个工具 1699 token, 而用户那句话只有
#  6 token）—— 上下文涨到第二轮 2131 > ctx 2048, 直接把一轮对话打崩。
#  合并后模型只看到 3 个工具（本工具 / `next_music` / `back_to_desktop`），
#  清单 token 掉一大截, 而且"翻页/挑图/看标签"不用再让模型先想清楚该叫哪个名字。
#
#  ⚠ T7-3 的入口没变: **手动换壁纸都删掉了**, 只有对话能换（见下面第 1 条）
#  ---------------------------------------------------------------------------
#      T3   GUI 主区「下一张」按钮 ┐
#           LLM 工具 next_wallpaper ├─▶ Runtime.next_wallpaper(step)
#      T6   + 同名 IPC 命令受状态权限表约束
#      T7-3 GUI 按钮、IPC 命令**删掉**；工具加上 `match=`
#                                      └─▶ Runtime.next_wallpaper(step, match)
#                                              └─ WallpaperDeck.step(step, pool)
#
#  ⚠ 让 LLM 知道的四件事（都写进 description）
#  ---------------------------------------------------------------------------
#    1. 只改**显示**, 不动任何文件（不是删除/移动壁纸）
#    2. action 的取值就是上面那五个（`action` 必填 —— 写错了会如实报错并列出合法的）
#    3. match 的写法与音乐的 tag **同一套语法**（`agent/core/label_spec.py`）:
#       轴=标签 / 只写标签 / ip=名字; 多条用 `/` = 任一命中
#    4. 推给 GUI 之后**没有回执**（GUI 是否真的画上去了, Agent 不知道）
#
#  ⚠ T7-4（b）: **当前词表写进 description**
#  ---------------------------------------------------------------------------
#  板端实测的失败模式: 0.6B 模型会**编造标签**（把 tone 的 `dark` 说成 `scene=darkness`），
#  于是第一次调用就撞一个"没有这条标签"的错, 白花一轮。词表是**配置决定的**
#  （`wallpaper.tagging.vocab` 可以按轴追加）, 模型看不见 —— 所以在这里把它写清楚:
#
#      可用标签: scene=landscape/city/…；tone=dark/…；mood=calm/…
#
#  写不下（词表被配置加得很长）就截断并指向本工具的 `action="tags"`。
#  拿不到词表（导入失败）时**什么都不写**（少一句话比写错一句好）；没有 config 时
#  用默认词表 —— 词表主要活在 `tag_vocab.py` 里, 配置只是按轴追加。
# ============================================================================

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Optional

from ..core.state_machine import State
from ..core.tool_router import Tool

__all__ = ["NAME", "ALLOWED_STATES", "ACTIONS", "DESCRIPTION", "SCHEMA",
           "VOCAB_TEXT_LIMIT", "build"]

_log = logging.getLogger(__name__)

NAME = "next_wallpaper"

#: 允许在哪些状态里换壁纸。
#: GAME 不在内: 主区那时是视频区, 换壁纸等于白换（SLEEP 要不要放行留给 T4 的状态表定）。
ALLOWED_STATES = (State.IDLE, State.STUDY)

#: 词表那一段最多写多少字符（超了就截断并让模型去查 list_wallpaper_tags）。
#: 为什么要有上限: 这段会跟着**每一次**请求送进上下文。
#: ⚠ T8-5 实测: 7 个工具的第一轮 prompt 里工具清单占 ~1700 token, 而**本工具一个人就
#:   392**（词表那一段是大头）—— 板端 ctx 4096 也是靠这个上限才留得住余量, 别再放开。
VOCAB_TEXT_LIMIT = 320

DESCRIPTION = (
    "板子屏幕上的背景图：换、按内容挑、看库里有什么标签。"
    "action=next/prev/repeat 翻页；action=pick 要给 match（例如 \"scene=anime\"、"
    "\"anime\"、\"ip=EVA\"，多条用 / 隔开 = 任一命中）；action=tags 是只读清单"
    "（ip_query 只填作品名, 例如 EVA —— 不是问题/问句, 也别把作品名当标签答）。"
    "⚠ 只改显示, 不动任何文件; 推给界面后没有回执。"
)

#: `action` 的取值（schema 的 enum 与 handler 的分派都以它为准）
ACTIONS = ("next", "prev", "repeat", "pick", "tags")

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(ACTIONS),
            "description": "要做什么：next 下一张 / prev 上一张 / repeat 重推当前这张 / "
                           "pick 按 match 挑 / tags 看有哪些标签（只读）",
        },
        "match": {
            "type": "string",
            "minLength": 1,
            "maxLength": 96,
            "description": "action=pick 的挑图条件: \"scene=anime\" / \"anime\" / \"ip=EVA\"; "
                           "多条标签用 / 或 , 隔开 = 任一命中",
        },
        "ip_query": {
            "type": "string",
            "minLength": 1,
            "maxLength": 32,
            "description": "action=tags 时只看某个作品：**只填作品名**（配置里的 IP 名字, "
                           "例如 EVA），不要填问句（\"这个作品最像哪几张\" 这种）",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 20,
            "description": "action=tags 最多回几条（默认 5）",
        },
    },
    "required": ["action"],
    "additionalProperties": False,
}


def vocab_hint(config: Optional[Mapping[str, Any]] = None) -> str:
    """拼一句"当前有哪些标签"（给 description 用）。

    @param config 已加载的配置（`services['config']`）—— 只有
                  `wallpaper.tagging.vocab`（按轴**追加**）会改词表
    @return 一句"可用标签: …"；**拿不到词表（导入失败）时返回 ""** —— 调用方就照旧不提标签
    @note 没有 config / config 形状不对 → 用**默认词表**（词表主要活在 `tag_vocab.py` 里,
          配置只是追加）—— 少一个信息来源, 不该变成少一段提示
    @note **不抛异常**: 这只是"给模型的一句提示", 拿不到不该让工具装不上
    """
    try:
        from ..vision import tag_vocab
    except Exception as exc:                      # noqa: BLE001
        _log.debug("tools: 拿不到 tag_vocab（%r），description 里不写词表", exc)
        return ""

    overrides: Mapping[str, Any] = {}
    node: Any = config
    for key in ("wallpaper", "tagging", "vocab"):
        node = node.get(key) if isinstance(node, Mapping) else None
        if node is None:
            break
    if isinstance(node, Mapping):
        overrides = node

    try:
        axes = tag_vocab.with_overrides(overrides)
    except Exception as exc:                      # noqa: BLE001
        _log.debug("tools: 词表叠加失败（%r），description 里不写词表", exc)
        return ""

    text = "可用标签: " + "；".join(
        "%s=%s" % (axis.name, "/".join(axis.texts)) for axis in axes
    )
    if len(text) > VOCAB_TEXT_LIMIT:
        text = "%s…（标签太多, 完整清单用 action=\"tags\" 查）" % text[:VOCAB_TEXT_LIMIT]
    return text


#: `action` -> `next_wallpaper(step=…)` 的步长（挑选那条单走）
_STEPS = {"next": 1, "prev": -1, "repeat": 0}


def build(services: Dict[str, Any]) -> Optional[Tool]:
    """造工具。缺 `next_wallpaper`（翻页/挑图）入口就返回 None（并说清为什么）。

    ⚠ 依赖的是**运行时那两个入口**（`agent/main.py::Runtime.next_wallpaper` /
      `Runtime.wallpaper_tags`), 不是壁纸目录: 目录不存在/是空的属于**运行期**问题,
      调的时候才知道（那时才该报错给用户看）。启动时目录还没建好, 不该让工具消失。
    @note `action="tags"` 要的是 `wallpaper_tags` —— 它不在时**整只工具不装**（而不是
          装一个少一个动作的版本）: 模型看到的 action 列表必须与真实可用的完全一致。
    """
    advance = (services or {}).get("next_wallpaper")
    tags = (services or {}).get("wallpaper_tags")
    if advance is None or tags is None:
        _log.warning("tools: next_wallpaper 需要 services['next_wallpaper'] 与 "
                     "services['wallpaper_tags']，装配里缺一个 —— 跳过")
        return None
    if not callable(advance) or not callable(tags):
        _log.warning("tools: services['next_wallpaper'/'wallpaper_tags'] 不是可调用的 —— 跳过")
        return None

    def handler(action: str = "", match: Optional[str] = None,
                ip_query: Optional[str] = None, limit: int = 5) -> Dict[str, Any]:
        choice = str(action or "").strip().lower()
        if choice == "tags":
            return tags(ip_query, int(limit or 5))
        # ⚠ T8-5b 板端实测: 模型会把挑图条件填进 `ip_query`（`action="next"` +
        #   `ip_query="scene=landscape"`）—— 那个参数只有 `action="tags"` 用得上。
        #   所以**翻页类动作**下把 ip_query 当 match 用（宽容一点, 少白丢一轮）。
        if choice in _STEPS and not (match or "").strip() and (ip_query or "").strip():
            match = ip_query
        if choice == "pick":
            if not (match or "").strip():
                return {"ok": False, "tell_user": "挑图要说明按什么挑（例如 match=\"scene=anime\"）",
                        "error": "action=pick 需要 match（例如 \"scene=anime\" / \"ip=EVA\"）"}
            return advance(1, match)
        if choice in _STEPS:
            # ⚠ T8-5b 板端实测: 模型常常写 `action="next"` **同时**给 `match` —— 那意思
            #   就是"在最像的那几张里翻"（老版本 next_wallpaper(step, match) 正是这样）。
            #   所以这里**接受** match: 给了就按 match 排出候选池, 再按 action 的步长翻。
            return advance(_STEPS[choice], match or None)
        return {"ok": False,
                "tell_user": "我看不懂这个壁纸动作（可用的是 %s）" % "/".join(ACTIONS),
                "error": "不认识的 action %r（可用的: %s）" % (action, "、".join(ACTIONS))}

    description = DESCRIPTION
    hint = vocab_hint((services or {}).get("config"))
    if hint:
        description = "%s %s" % (DESCRIPTION, hint)

    return Tool(
        name=NAME,
        description=description,
        schema=SCHEMA,
        handler=handler,
        allowed_states=set(ALLOWED_STATES),
    )
