# ============================================================================
#  agent/tools/wallpaper.py — "壁纸" 工具（Phase 7 T3；T7-3 能按内容挑；T8-5b 合并）
#
#  它做什么: 一个工具管壁纸的**全部**动作（`action` 决定干哪件）:
#
#      action="next"    往后走一格（三个的窗口: next 变当前, 再按画像补一个新的 next）
#      action="prev"    往回走一格（上一个）
#      action="repeat"  重推当前这张
#      action="pick"    按 `match=` 挑（按内容/按 IP 检索，最像的在最前）—— 会**立刻换**
#      action="least"   挑**用得最少**的一张（T8-6；等价于 pick + sort=used_asc）
#      action="most"    挑**用得最多**的一张（T8-6）
#      action="stage"   只把一张放进"下一个"（**不切屏**）—— 不带 match 就按画像重挑一张
#      action="tags"    只读: 库里有哪些标签 / 某个作品最像哪几张
#
#  ⚠ T8-6 板端实测（"挑一张我用得最少的壁纸"）: 0.6B **不会为了"用得最少"去设 `sort=`**
#    —— 它照样只填 `action="pick"` + 一个自己编的 `match="scene=landscape"`（那是词表里
#    的第一个标签）。所以把这件事做成一个**单字 action**: 枚举里一眼能看见, 还不用参数。
#    `sort=` 仍然保留（"在最像的那批里挑用得最少的"这种组合要靠它）。
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

__all__ = ["NAME", "ALLOWED_STATES", "ACTIONS", "SORTS", "DESCRIPTION", "SCHEMA",
           "VOCAB_TEXT_LIMIT", "normalize", "build"]

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
    "板子屏幕上的背景图：换、按内容挑、按用得多少挑、看标签。"
    "action=next/prev/repeat 翻页/重推；action=least/most 挑用得最少/最多的（不用 match）；"
    "action=stage 只预备下一个（不切屏, 按画像挑）；"
    "action=pick 要给 match（例如 \"scene=anime\"、\"anime\"、\"ip=EVA\"，"
    "多条用 / 隔开 = 任一命中）；action=tags 是只读清单"
    "（ip_query 只填作品名, 例如 EVA —— 不是问题/问句, 也别把作品名当标签答）。"
    "⚠ 只改显示, 不动任何文件; 推给界面后没有回执。"
)

#: `action` 的取值（schema 的 enum 与 handler 的分派都以它为准）
ACTIONS = ("next", "prev", "repeat", "pick", "least", "most", "stage", "tags")

#: T8-6: 按使用次数挑的两个 action -> 传给 Runtime 的 `sort`
_USAGE_ACTIONS = {"least": "used_asc", "most": "used_desc"}

#: 同一个意思的各种写法都归到 least / most（模型会写 least_used / used_asc / fewest…）
_ACTION_SYNONYMS = {
    "least": "least", "least_used": "least", "fewest": "least", "used_asc": "least",
    "most": "most", "most_used": "most", "used_desc": "most",
}

#: `sort` 的取值（T8-6: 按**使用次数**重排候选）
SORTS = ("used_asc", "used_desc")

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(ACTIONS),
            "description": "要做什么：next/prev 翻页 / repeat 重推 / least 用得最少 / "
                           "most 用得最多 / stage 只预备下一个（不切屏）/ "
                           "pick 按 match 挑 / tags 看标签（只读）",
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
        "sort": {
            "type": "string",
            "enum": list(SORTS),
            "description": "候选按使用次数再排一遍: used_asc 用得最少的先 / used_desc 用得最多的先"
                           "（只对挑图/翻页有意义; action=tags 不要给）",
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

#: 老版本（T7 时代）用过的参数名 `step` -> 现在的 `action`
#: ⚠ 来源: 板端实测模型偶尔会写老参数名/老写法; 1/-1/0 的语义与 next/prev/repeat 完全一致。
_STEP_ACTIONS = {"1": "next", "-1": "prev", "0": "repeat", 1: "next", -1: "prev", 0: "repeat"}

#: 空值写法 —— 模型有时候把"没给"写成这些字面量
_EMPTY_VALUES = ("", "none", "null", "nil", "n/a", "na", "-", "无", "空")


def normalize(args: Dict[str, Any]) -> Dict[str, Any]:
    """参数归一化（T8-5c）—— **按语义接受, 不按参数名挑刺**。

    规则（每条都写明来源; 只做等价改写, 不猜内容）:

    ============================================  ==========================================
    模型可能这样写                                 归一化成
    ============================================  ==========================================
    `action=" NEXT "` / `action="NEXT"`            `action="next"`（大小写+空白; 实测常见）
    `action="next"` + `match="scene=anime"`        原样保留（**等价合法** —— 就是"在最像的
                                                   几张里翻", 老版本 `next_wallpaper(step,
                                                   match)` 本来就长这样; 板端实测模型这么写过）
    `action="next"` + `ip_query="scene=anime"`     `match` 拿过来（`ip_query` 只有 `tags`
                                                   用得上; 板端实测模型把条件填进过它。
                                                   ⚠ 只覆盖 `next/prev/repeat`: `pick` 会
                                                   如实报错、`stage` 会当没给条件, 见
                                                   docs/tagging.md §7）
    `step=1 / -1 / 0`（老参数名）                  `action=next / prev / repeat`
    `action="least_used"` / `"used_asc"` / `"fewest"`  `action="least"`（T8-6: 同一个意思的各种写法;
                                                        板端实测模型不会为"用得最少"去设 `sort=`）
    `action="most_used"` / `"used_desc"`           `action="most"`
    `match=""` / `"none"` / `"null"` 这类空写法     删掉（当没给）
    `sort="USED_ASC"` / `sort=""`                  `"used_asc"` / 删掉
    `action="pick"` 且没给 match                   **不动**（让 handler 如实报"挑图要说明按什么挑"）
    `sort="used_asc"` 且没给 action                `action="least"`（`used_desc` -> `"most"`）
    ============================================  ==========================================

    @return 新的参数字典（不改入参）
    """
    out = dict(args)

    action = out.get("action")
    if isinstance(action, str):
        action = action.strip().lower()
        action = _ACTION_SYNONYMS.get(action, action)
    if action is None or action == "":
        # 老参数名 `step` -> action（只认语义完全一致的那三个）
        if "step" in out:
            action = _STEP_ACTIONS.get(out.get("step"))
            out.pop("step", None)
    if isinstance(action, str) and action:
        out["action"] = action

    for key in ("match", "ip_query", "sort"):
        value = out.get(key)
        if isinstance(value, str):
            value = value.strip()
            if value.lower() in _EMPTY_VALUES:
                out.pop(key, None)
            else:
                out[key] = value.lower() if key == "sort" else value
        elif value is None and key in out:
            out.pop(key)

    # 翻页类动作下, 把填错位置的 `ip_query` 当 match 用
    # ⚠ 只覆盖 `_STEPS`（next/prev/repeat）—— `pick`/`stage` 不在里面: `pick` 会如实报
    #   "挑图要说明按什么挑"（诚实, 用户能改）, `stage` 会当"没给条件"按画像挑一张。
    #   要放宽到"除 tags 以外"就是改工具行为, 单独做（T10-6 只记不改, 见 docs/tagging.md §7）。
    if out.get("action") in _STEPS and not out.get("match") and out.get("ip_query"):
        out["match"] = out["ip_query"]

    # 只给了 match/sort 却没给 action: 语义就是"挑一张"
    # ⚠ 只给 `sort` 时要落到 least/most 上 —— 落成 `pick` 会撞"pick 需要 match"那个错
    if not out.get("action"):
        if out.get("sort") in ("used_asc", "used_desc"):
            out["action"] = "least" if out["sort"] == "used_asc" else "most"
        elif out.get("match"):
            out["action"] = "pick"

    return out


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
                ip_query: Optional[str] = None, limit: int = 5,
                sort: Optional[str] = None) -> Dict[str, Any]:
        # ⚠ 归一化已经在路由层跑过（`Tool.normalize`, 见 `normalize()` 的规则表）——
        #   handler 这里只看规范形; 模型写的老写法/错位置参数都已经被改写了。
        choice = str(action or "").strip().lower()
        order = str(sort or "").strip().lower() or None
        if choice == "tags":
            # ⚠ T8-6 板端实测: "再挑一张我用得最少的壁纸" 被模型写成
            #   `action="tags" + sort="used_asc"` —— 而 `tags` 是**只读**的, 它不会换壁纸。
            #   按 tags 答会怎样: 安静地回一份标签清单, 模型接着说"已找到使用次数最少的壁纸,
            #   您可以在界面中看到它"（**谎报** —— 屏幕根本没换）。
            #   两种错都不选: ① 默默当 tags（等于骗用户）; ② 悄悄改成换图（把"读"当"写"猜）。
            #   如实报错 + 指出该用哪个 action（下一轮它有机会改, 不改也有 tell_user 兜底）。
            if order:
                return {"ok": False,
                        "tell_user": "看标签不会换壁纸；要挑\"用得最少的\"那张请用 action=\"least\"。",
                        "error": "action=tags 是只读的, 不接受 sort"
                                 "（要按用量挑图请用 action=\"least\"/\"most\"）"}
            return tags(ip_query, int(limit or 5))
        if choice == "pick":
            if not (match or "").strip():
                return {"ok": False, "tell_user": "挑图要说明按什么挑（例如 match=\"scene=anime\"）",
                        "error": "action=pick 需要 match（例如 \"scene=anime\" / \"ip=EVA\"）"}
            return advance(1, match, order)
        if choice in _USAGE_ACTIONS:                   # T8-6: least / most
            return advance(1, match or None, _USAGE_ACTIONS[choice])
        if choice == "stage":                          # T10-3: 只预备"下一个", 不切屏
            return advance(1, match or None, order, stage=True)
        if choice in _STEPS:
            return advance(_STEPS[choice], match or None, order)
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
        normalize=normalize,
    )
