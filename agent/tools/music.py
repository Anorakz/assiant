# ============================================================================
#  agent/tools/music.py — "音乐" 工具（Phase 7 T8-5b，合并原来的音乐四件套）
#
#  它做什么: 一个工具管音乐的全部动作（`action` 决定干哪件）:
#
#      action="enqueue"      把歌**加进环形队列**（chat 的两个决定之一）
#      action="clear_queue"  清空队列（chat 的另一个决定）
#      action="list"         只读: 本地库里有什么（id / 名字 / 播放次数 / 标签）
#      action="search"       只读: 去 **PC 上搜**（只回候选，不放歌）
#      action="status"       只读: 现在在放什么 + 队列里有几首
#      action="tag"          给某一首（或当前这首）加/删标签
#      action="volume"       调音量（"小声点"）
#
#  ⚠ 分工（T8-5b 你定的）——这张表决定"谁决定什么"
#  ---------------------------------------------------------------------------
#      chat（本工具）: 队列**内容** —— 加一首 / 清空
#      GUI（四个按钮）: 播放 / 暂停 / 上一首 / 下一首（走 IPC, 不经过本工具）
#      Runtime:         队列是**环形**的（到尾回第一首）, 曲终自动下一首
#
#  所以这里**没有** play/pause/next/prev/stop —— 那是界面的活。唯一例外是 `volume`
#  （"小声点"是调节, 不是换曲）; `seek` 这次没做（省两个参数）。
#
#  为什么合并（T8-5b, 你定的"进一步抽象简化"）
#  ---------------------------------------------------------------------------
#  板端实测: 4 个音乐工具吃掉 1088 token 的工具清单, 而"放一首听得最少的歌"要三轮模型
#  （先 list 再 play 再回话）, 一轮对话 3~10 分钟。合并后:
#      · 模型只看到 3 个工具（next_wallpaper / next_music / back_to_desktop）
#      · "放一首听得最少的" = 一句话: action="enqueue"（不给条件 = 默认听得最少的）
#      · 标签写法与壁纸的 match **同一套语法**（agent/core/label_spec.py）
#
#  ⚠ 让 LLM 知道的四件事（都写进 description）
#  ---------------------------------------------------------------------------
#    1. 声音从 **PC** 出; 本工具只安排队列/登记, **不动任何文件**
#    2. 播放次数**只在真的听了 30 秒**之后 +1（点开就切不算）
#    3. 标签只有两路来源: 元数据自动 + 你说过的（**我们没有音频特征** ——
#       别把"听起来像"当成能做的事）
#    4. 失败一律如实报（PC 没开机 / 公钥没配 / 登录态失效 / mpv 不在 PATH）
# ============================================================================

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from ..core import label_spec
from ..core.state_machine import State
from ..core.tool_router import Tool

__all__ = ["NAME", "ALLOWED_STATES", "ACTIONS", "SORTS", "TIMEOUT_S", "DESCRIPTION",
           "SCHEMA", "normalize", "build"]

_log = logging.getLogger(__name__)

NAME = "next_music"

#: 与壁纸那两个工具同一套状态（睡觉时不该出声, GAME 的主区是视频区）
ALLOWED_STATES = (State.IDLE, State.STUDY)

#: `action` 的取值
ACTIONS = ("enqueue", "clear_queue", "list", "search", "status", "tag", "volume")

#: `sort` 的取值（与 `agent/media/music_library.py::pick()` 一一对应）
SORTS = ("plays_asc", "plays_desc", "recent", "oldest", "added", "random")

#: 这个工具自己的超时（秒）——比路由默认的 5 s 长得多。
#: 为什么: `enqueue` 在"PC 上没在放"时会**同步起播**（A1）, 那一步要走 ssh + schtasks,
#: 板端实测 5~8 s; 用默认 5 s 会把它误判成 `timed out`（T8-5b 板端实测踩到:
#: "工具 next_music(...) -> fail / timed out after 5s", 而歌其实已经在放）。
TIMEOUT_S = 25.0

DESCRIPTION = (
    "音乐（声音从 PC 出）。action=enqueue 把歌排进播放队列（不给条件就是库里听得最少的"
    "那首；也可以给 track_id / keyword 去 PC 搜 / tag 按标签挑；replace=true 先清空队列）；"
    "action=clear_queue 清空队列；list 看本地库清单（只读）；search 去 PC 上搜（只读、不放）；"
    "status 看在放什么；tag 给某首加/删标签；volume 调音量。"
    "播放/暂停/上一首/下一首由界面按钮决定，不在这里。"
    "⚠ 播放次数只在真听了 30 秒后才 +1；标签只有'元数据自动 + 你说过的'两路（没有音频特征）。"
)

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(ACTIONS),
            "description": "要做什么：enqueue 排进队列 / clear_queue 清空队列 / list 看清单 / "
                           "search 搜（只读）/ status 看在放什么 / tag 打标签 / volume 调音量",
        },
        "track_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 32,
            "description": "歌曲 id（来自 list / search 回过的清单）",
        },
        "keyword": {
            "type": "string",
            "minLength": 1,
            "maxLength": 64,
            "description": "enqueue: 去 PC 上搜并排前 limit 首; search: 只回候选不放歌",
        },
        "tag": {
            "type": "string",
            "minLength": 1,
            "maxLength": 96,
            "description": "按标签挑/筛: \"mood=energetic\" / \"energetic\"（所有轴）; "
                           "多条用 / 隔开 = 任一命中（与壁纸 match 同一套语法）",
        },
        "sort": {
            "type": "string",
            "enum": list(SORTS),
            "description": "list/enqueue 的排序；默认 plays_asc（听得最少的先）",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 20,
            "description": "enqueue 排几首（默认 1）；list/search 最多回几条（默认 5/10）",
        },
        "replace": {
            "type": "boolean",
            "description": "enqueue 时先清空队列再加（\"换一批\"）",
        },
        "set_tag": {
            "type": "string",
            "minLength": 1,
            "maxLength": 96,
            "description": "action=tag 要写的标签，必须带轴: \"mood=燃\" "
                           "（多轴用 ; 隔开: \"mood=燃; style=rock\"）",
        },
        "remove": {
            "type": "boolean",
            "description": "action=tag 时 true=删掉这些标签（默认 false=添加）",
        },
        "level": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
            "description": "action=volume 的音量百分比",
        },
    },
    "required": ["action"],
    "additionalProperties": False,
}


def _parsed_tag(value: Optional[str]) -> Any:
    """把 `tag=` 解析成 `(axis, tag)`（统一语法）—— 多条值时给列表（任一命中）。

    @return (axis 或 None, 标签 str 或 list)。解析不出东西时返回 `(None, "")`
    """
    axis, values = label_spec.parse_one(value or "")
    if axis == label_spec.EMPTY_AXIS:
        axis = None
    if not values:
        return None, ""
    return axis, (values[0] if len(values) == 1 else values)


#: 老工具名/同义写法 -> 现在的 action（`play_music` 是 T8-5 那版的工具名,
#: 语义与现在的 `enqueue` 完全一样; 板端实测模型偶尔会用老名字）
_ACTION_SYNONYMS = {
    "play": "enqueue",
    "play_music": "enqueue",
    "add": "enqueue",
    "push": "enqueue",
    "queue": "enqueue",
    "clear": "clear_queue",
    "list_music_library": "list",
    "search_music": "search",
}

#: 空值写法 —— 模型有时候把"没给"写成这些字面量（板端实测见过 `track_id="none"`）
_EMPTY_VALUES = ("", "none", "null", "nil", "n/a", "na", "-", "无", "空")

#: 哪些字段出现就意味着"这是一条 enqueue"（action 缺失时用来推断）
_ENQUEUE_HINTS = ("track_id", "keyword", "tag", "sort", "limit", "replace")


def _clean_text(value: Any) -> Optional[str]:
    """字符串字段的清洗: 去空白; 空值字面量 -> None（= 没给）。"""
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in _EMPTY_VALUES:
        return None
    return text


def normalize(args: Dict[str, Any]) -> Dict[str, Any]:
    """参数归一化（T8-5c）—— **按语义接受, 不按参数名挑刺**。

    规则（每条都写明来源; 只做等价改写, 不猜内容）:

    ================================================  ==========================================
    模型可能这样写                                     归一化成
    ================================================  ==========================================
    `action=" ENQUEUE "` / 大写                      小写 + 去空白
    `action="play"` / `"play_music"` / `"add"`        `action="enqueue"`（老工具名/同义; 板端
                                                     实测模型会用老名字）
    `action="clear"`                                  `action="clear_queue"`
    没给 `action`, 但给了 `track_id`/`keyword`/`tag`/  `action="enqueue"`（语义就是"放一首"）
      `sort`/`limit`/`replace`
    没给 `action`, 但给了 `set_tag`                   `action="tag"`；给了 `level` -> `volume`
    `track_id="none"` / `""` / `null`（板端实测）      删掉（当没给 -> 走"按条件挑"）
    `keyword` / `tag` / `set_tag` 的空写法             删掉
    `action="tag"` 却把标签放进了 `tag`（不是 `set_tag`）`set_tag` 拿过来, 删掉 `tag`
    `limit="3"` / `level="30"`（数字写成字符串）       转成 int（schema 会拒字符串）
    ================================================  ==========================================

    @return 新的参数字典（不改入参）
    """
    out = dict(args)

    action = out.get("action")
    if isinstance(action, str):
        action = action.strip().lower()
    if isinstance(action, str) and action:
        action = _ACTION_SYNONYMS.get(action, action)

    for key in ("track_id", "keyword", "tag", "set_tag"):
        if key in out:
            cleaned = _clean_text(out.get(key))
            if cleaned is None:
                out.pop(key, None)
            else:
                out[key] = cleaned
    for key in ("quality", "sort"):
        if isinstance(out.get(key), str):
            out[key] = out[key].strip()

    if not action:
        if "set_tag" in out:
            action = "tag"
        elif "level" in out:
            action = "volume"
        elif any(key in out for key in _ENQUEUE_HINTS):
            action = "enqueue"
    if isinstance(action, str) and action:
        out["action"] = action

    for key in ("limit", "level"):
        value = out.get(key)
        if isinstance(value, str):
            try:
                out[key] = int(float(value))
            except ValueError:
                out.pop(key, None)      # 转不了就当没给（handler 会用它自己的默认 / 如实报错）
        elif isinstance(value, bool):   # True/False 不是数字
            out.pop(key, None)

    # `action="tag"` 时模型容易把要写的标签塞进 `tag`（那是筛选用的）—— 等价改写到 set_tag
    if out.get("action") == "tag" and not out.get("set_tag") and out.get("tag"):
        out["set_tag"] = out.pop("tag")

    return out


def build(services: Dict[str, Any]) -> Optional[Tool]:
    """造工具。音乐没开（缺入口）就返回 None（并说清为什么）。

    ⚠ 要求**全部入口都在**（`music.enabled=false` 时 Runtime 给的就是一整组 None）:
      少一个动作会让"模型看到的 action 列表"与真实可用的不一致 —— 那比少一个工具更坏。
    """
    needed = ("music_enqueue", "music_queue_clear", "music_list", "music_search",
              "music_state", "music_queue_state", "music_tag", "music_control")
    services = services or {}
    missing = [name for name in needed if not callable(services.get(name))]
    if missing:
        _log.warning("tools: next_music 需要 services 里的 %s，装配里缺 %s —— 跳过",
                     "/".join(needed), "、".join(missing) or "（音乐没开？）")
        return None

    def _need(name: str, **kwargs: Any) -> Dict[str, Any]:
        return {"ok": False,
                "tell_user": "这条指令少了 %s" % name,
                "error": "%s 需要 %s" % (kwargs.get("action"), name)}

    def handler(action: str = "", track_id: Optional[str] = None,
                keyword: Optional[str] = None, tag: Optional[str] = None,
                sort: str = "plays_asc", limit: int = 0, replace: Optional[bool] = None,
                set_tag: Optional[str] = None, remove: Optional[bool] = None,
                level: Optional[int] = None) -> Dict[str, Any]:
        # ⚠ 归一化已经在路由层跑过（`Tool.normalize`, 规则表见 `normalize()`）——
        #   这里只看规范形: 老 action 名 / 空值字面量 / 字符串数字都已经被改写。
        choice = str(action or "").strip().lower()

        if choice == "enqueue":
            axis, wanted = _parsed_tag(tag)
            if tag and not wanted:
                return {"ok": False, "tell_user": "标签写法不对（要么别给, 要么写成 mood=energetic）",
                        "error": "tag 解析不出标签: %r" % tag}
            return services["music_enqueue"](
                track_id=track_id, keyword=keyword, tag=tag, sort=sort,
                limit=int(limit or 1), replace=bool(replace))

        if choice == "clear_queue":
            return services["music_queue_clear"]()

        if choice == "list":
            axis, wanted = _parsed_tag(tag)
            if tag and not wanted:
                return {"ok": False, "tell_user": "标签写法不对（要么别给, 要么写成 mood=energetic）",
                        "error": "tag 解析不出标签: %r" % tag}
            return services["music_list"](tag=wanted or None, axis=axis, sort=sort,
                                          limit=int(limit or 10))

        if choice == "search":
            if not keyword:
                return _need("keyword（搜什么）", action="action=search")
            return services["music_search"](keyword, limit=int(limit or 10))

        if choice == "status":
            state = dict(services["music_state"]())
            queue = services["music_queue_state"]()
            state["queue"] = {"size": queue.get("size"), "index": queue.get("index"),
                              "tracks": queue.get("tracks")}
            return state

        if choice == "tag":
            tags = label_spec.parse(set_tag)
            if not tags:
                return _need("set_tag（要写的标签, 例如 mood=燃）", action="action=tag")
            bare = tags.pop(label_spec.EMPTY_AXIS, [])
            if bare:
                return {"ok": False,
                        "tell_user": "打标签要写清是哪一轴（例如 mood=燃）",
                        "error": "set_tag 里的 %s 没写轴 —— 请写成 轴=标签" % "/".join(bare)}
            return services["music_tag"](tags, track_id=track_id, remove=bool(remove))

        if choice == "volume":
            if level is None:
                return _need("level（0..100）", action="action=volume")
            return services["music_control"]("volume", level=int(level))

        return {"ok": False,
                "tell_user": "我看不懂这个音乐动作（可用的是 %s）" % "/".join(ACTIONS),
                "error": "不认识的 action %r（可用的: %s）" % (action, "、".join(ACTIONS))}

    return Tool(
        name=NAME,
        description=DESCRIPTION,
        schema=SCHEMA,
        handler=handler,
        allowed_states=set(ALLOWED_STATES),
        timeout_s=TIMEOUT_S,
        normalize=normalize,
    )
