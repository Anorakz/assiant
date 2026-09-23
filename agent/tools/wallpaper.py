# ============================================================================
#  agent/tools/wallpaper.py — "换壁纸" 工具（Phase 7 T3；T7-3 起能按内容挑）
#
#  它做什么: 让板子屏幕上的背景图换一张 —— 默认按文件名翻下一页；给了 `match`
#            就**先按内容筛**（标签 / IP），再在筛出来的候选里翻（相关度最高的在前）。
#            最后把结果推给 GUI（topic `wallpaper`, 线格式见 docs/ipc-protocol.md §3）。
#
#  ⚠ T7-3 的入口变化: **手动换壁纸都删掉了**
#  ---------------------------------------------------------------------------
#      T3   GUI 主区「下一张」按钮 ┐
#           LLM 工具 next_wallpaper ├─▶ Runtime.next_wallpaper(step)
#      T6   + 同名 IPC 命令受状态权限表约束
#      T7-3 GUI 按钮、IPC 命令**删掉**；工具加上 `match=`
#                                      └─▶ Runtime.next_wallpaper(step, match)
#                                              └─ WallpaperDeck.step(step, pool)
#  也就是说: 现在**只有对话**能换壁纸（"换一张安静的深色风景"），
#  因为"换成什么样"这件事只有自然语言说得清，按钮只能按文件名翻。
#
#  ⚠ 让 LLM 知道的三件事（都写进 description）
#  ---------------------------------------------------------------------------
#    1. 只改**显示**, 不动任何文件（不是删除/移动壁纸）
#    2. match 的三条写法（轴=标签 / 只写标签 / ip=名字）—— 写错了会**如实报错**,
#       不会"随便换一张糊弄过去"
#    3. 推给 GUI 之后**没有回执**（GUI 是否真的画上去了, Agent 不知道）
#
#  ⚠ T7-4（b）: **当前词表写进 description**
#  ---------------------------------------------------------------------------
#  板端实测的失败模式: 0.6B 模型会**编造标签**（把 tone 的 `dark` 说成 `scene=darkness`），
#  于是第一次调用就撞一个"没有这条标签"的错, 白花一轮。词表是**配置决定的**
#  （`wallpaper.tagging.vocab` 可以按轴追加）, 模型看不见 —— 所以在这里把它写清楚:
#
#      可用标签: scene=landscape/city/…；tone=dark/…；mood=calm/…
#
#  写不下（词表被配置加得很长）就截断并指向 `list_wallpaper_tags`。
#  拿不到词表（导入失败）时**什么都不写**（少一句话比写错一句好）；没有 config 时
#  用默认词表 —— 词表主要活在 `tag_vocab.py` 里, 配置只是按轴追加。
# ============================================================================

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Optional

from ..core.state_machine import State
from ..core.tool_router import Tool

__all__ = ["NAME", "ALLOWED_STATES", "DESCRIPTION", "SCHEMA", "VOCAB_TEXT_LIMIT", "build"]

_log = logging.getLogger(__name__)

NAME = "next_wallpaper"

#: 允许在哪些状态里换壁纸。
#: GAME 不在内: 主区那时是视频区, 换壁纸等于白换（SLEEP 要不要放行留给 T4 的状态表定）。
ALLOWED_STATES = (State.IDLE, State.STUDY)

#: 词表那一段最多写多少字符（超了就截断并让模型去查 list_wallpaper_tags）。
#: 为什么要有上限: 这段会跟着**每一次**请求送进上下文（板端 ctx 只有 2048）。
VOCAB_TEXT_LIMIT = 320

DESCRIPTION = (
    "把板子屏幕上的背景图换成另一张。"
    "step=1 往后翻、-1 往前翻、0 重推当前这张。"
    "想按内容挑就给 match：\"scene=anime\"（场景轴上的某条标签）、"
    "\"anime\"（只写标签名，各轴里找）、\"ip=EVA\"（某个作品，按锚点图检索）。"
    "不知道库里有什么标签时，先用 list_wallpaper_tags 看一眼。"
    "⚠ 只改显示, 不动任何文件; 推给界面后没有回执。"
)

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "step": {
            "type": "integer",
            "minimum": -50,
            "maximum": 50,
            "description": "在候选里往前翻几张（负数=往回翻, 0=重推当前这张）; 默认 1",
        },
        "match": {
            "type": "string",
            "minLength": 1,
            "maxLength": 64,
            "description": "挑图条件（不写=按文件名翻下一页）: "
                           "\"scene=anime\" / \"anime\" / \"ip=EVA\"",
        },
    },
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
        text = "%s…（标签太多, 完整清单用 list_wallpaper_tags 查）" % text[:VOCAB_TEXT_LIMIT]
    return text


def build(services: Dict[str, Any]) -> Optional[Tool]:
    """造工具。缺 `next_wallpaper` 入口就返回 None（并说清为什么）。

    ⚠ 依赖的是**运行时那个入口**（`agent/main.py::Runtime.next_wallpaper`), 不是壁纸目录:
      目录不存在/是空的属于**运行期**问题, 调的时候才知道（那时才该报错给用户看）。
      启动时目录还没建好, 不该让这个工具消失。
    """
    advance = (services or {}).get("next_wallpaper")
    if advance is None:
        _log.warning("tools: next_wallpaper 需要 services['next_wallpaper']，但装配里没有 —— 跳过")
        return None
    if not callable(advance):
        _log.warning("tools: services['next_wallpaper'] 不是可调用的 —— 跳过 next_wallpaper")
        return None

    def handler(step: int = 1, match: Optional[str] = None) -> Dict[str, Any]:
        return advance(step, match)

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
