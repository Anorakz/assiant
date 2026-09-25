#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent/core/user_profile.py — **用户画像内核**（Phase 7 T9-2）

它算什么（三件, 前两件是**权重**, 第三件**调模型**）
    1. **壁纸 IP 喜好**（权重）: 用户换过的图 × `wallpaper.tagging.ip_presets` 的锚点
       （纯 CPU 余弦, `TagIndex.match("ip=…")`）+ 纯对话里提到这个 IP 的次数
    2. **歌手喜好**（权重）: `music_library.jsonl` 的 `plays` × `tags.artist` +
       纯对话里提到这个歌手的次数
    3. **当前心情**: 把"纯对话片段 + **当时场景**（idle/study/game/sleep + 时间）"交给模型,
       只许回封闭词表里的一个词; 解析不出就 `unknown`（**不编**）

权重怎么算（每个数都留分子分母, 能复核）
    权重 = 0.6×用量占比 + 0.3×提及占比 + 0.1×近期占比      （IP）
    权重 = 0.7×播放占比 + 0.3×提及占比                      （歌手）
    占比 = 该项 / 所有候选的和（都不是绝对值, 是"谁相对更重"）; 近期 = 30 天内线性衰减。

**负反馈 = 直接清零**（你 T9 定的, 且**源文件也清 0**）
    用户说"不想听 X 了 / 别看 X 了 / 听腻了 / 换掉 X"（负向词 + 实体名同时出现才算）→
      · 画像里 X 的权重与全部分量归 0, 并记 `muted_at` + 原话;
      · **源文件也清 0**: 那些图的 `used`/`last_used`、那些歌的 `plays`/`last_played`
        （走 `wall_data.clear_usage_in_file()` / `music_library.clear_plays()`, 唯一的两个写者）。
      ⚠ 清零之后它们会重新变成"用得/听得最少的" → **下次"挑最少的"先把它们挑出来**。
        这是你明确的设计意图（"清零 = 把偏好归零、重新进候选"）, 不是副作用。
      ⚠ 识别不出实体名时**什么都不做**, 只记一条 `unmatched`（宁可不猜）。

落盘
    `config/user_profile.jsonl`: **一次构建一行**（原子写; 本模块是唯一写者）。
    全局的"不想看/听"名单是**上一行里记着的** `muted` —— 每次构建都先算、再套用,
    否则源文件里新长出来的用量会把它顶回来。

它**不做**什么
    · 不决定"什么时候构建"（那是 T9-3: 纯对话攒到 2000 字, Agent 触发）;
    · 不把画像用在挑图/挑歌上（你定的"暂不应用"）;
    · 不把画像塞进每轮上下文（模型看不到它）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "PROFILE_VERSION",
    "DEFAULT_PROFILE_FILE",
    "DEFAULT_IP_FILE",
    "DEFAULT_TRIGGER_CHARS",
    "DEFAULT_TRIGGER_TURNS",
    "DEFAULT_RETRY_S",
    "MOOD_LABELS",
    "MOOD_ZH",
    "MOOD_UNKNOWN",
    "MOOD_TO_TAGS",
    "NEGATIVE_CUES",
    "IP_WEIGHTS",
    "ARTIST_WEIGHTS",
    "RANK_WALLPAPER_WEIGHTS",
    "RANK_TRACK_WEIGHTS",
    "DEFAULT_SIMILAR_TAGS",
    "RECENCY_DAYS",
    "Feedback",
    "ProfileError",
    "Sources",
    "resolve_profile_file",
    "count_hits",
    "mood_tags",
    "profile_basis",
    "rank_wallpapers",
    "rank_tracks",
    "similar_tracks",
    "detect_negative_feedback",
    "wallpaper_weights",
    "artist_weights",
    "mood_prompt",
    "parse_mood",
    "ask_mood",
    "build_profile",
    "append_record",
    "read_records",
    "latest_record",
    "muted_from_records",
]

PROFILE_VERSION = 1

#: 画像文件（相对于**仓库根**解析, 与其它本地数据同款）
DEFAULT_PROFILE_FILE = "config/user_profile.jsonl"

#: `assistant tag` 那份壁纸数据（IP 喜好的"用量"和"锚点"都从它来）
DEFAULT_IP_FILE = "config/wall_data.jsonl"

#: 心情的**封闭词表**（英文 key + 中文; 模型只许回这里面之一）
MOOD_LABELS: Tuple[str, ...] = ("focused", "relaxed", "happy", "tired", "stressed", "calm")
MOOD_ZH: Dict[str, str] = {
    "focused": "专注", "relaxed": "放松", "happy": "开心",
    "tired": "疲惫", "stressed": "烦躁", "calm": "平静",
    "unknown": "说不准",
}
MOOD_UNKNOWN = "unknown"

#: 负向词（**必须和实体名同时出现**才算一条负反馈 —— 只看到"不想听"就去猜是谁, 会猜错）
NEGATIVE_CUES: Tuple[str, ...] = (
    "不想听", "不想看", "不爱听", "不爱看", "别放", "别再放", "别给我放", "别给我看",
    "不要放", "不要看", "别听", "别看", "别再听", "别再给",
    "听腻", "看腻", "腻了", "讨厌", "不喜欢", "换掉", "受不了",
    "no more", "dislike", "tired of",
)

#: 权重配方（改这两行就等于改口径 —— 文档 docs/profile.md 里有同一张表）
IP_WEIGHTS = {"usage": 0.6, "mention": 0.3, "recency": 0.1}
ARTIST_WEIGHTS = {"plays": 0.7, "mention": 0.3}

#: "近期"的窗口（天）: 30 天前的算 0 分, 越新越接近 1
RECENCY_DAYS = 30.0

#: 触发线（T9-3）: **纯对话**攒到这么多字就构建一次（你定的 2000）；
#: `trigger_turns` 只是兜底（有人一直发很短的话时, 别永远攒不满）。
#: ⚠ 触发者**只有 Agent 一个**（`Runtime._profile_tick`）—— 模型看不到、也调不到它。
DEFAULT_TRIGGER_CHARS = 2000
DEFAULT_TRIGGER_TURNS = 12

#: 构建失败后多久内不再重试（免得每轮都去问一次模型）
DEFAULT_RETRY_S = 60.0


class ProfileError(ValueError):
    """画像文件读不了/写不了, 或配置里的路径不对。"""


# ---------------------------------------------------------------------------
#  路径
# ---------------------------------------------------------------------------
def _repo_root() -> str:
    from ..config import config_dir

    return str(config_dir().parent)


def resolve_profile_file(configured: Optional[str] = None) -> str:
    """把配置里的 `profile.file` 解析成绝对路径（相对路径按**仓库根**, 与其它数据同款）。"""
    text = configured.strip() if isinstance(configured, str) else ""
    if not text:
        return os.path.normpath(os.path.join(_repo_root(), DEFAULT_PROFILE_FILE))
    if os.path.isabs(text):
        return os.path.normpath(text)
    return os.path.normpath(os.path.join(_repo_root(), text))


# ---------------------------------------------------------------------------
#  负反馈
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Feedback:
    """一条识别出来的负反馈（"不想听 X 了"）。"""

    axis: str                 # "ip" | "artist"
    name: str                 # 实体名（IP 名 / 歌手名）
    text: str                 # 原话
    ts: float = 0.0
    matched: str = ""         # 原话里那一次写法（大小写可能不同）

    def to_dict(self) -> Dict[str, Any]:
        return {"axis": self.axis, "name": self.name, "text": self.text,
                "ts": self.ts, "matched": self.matched}


def detect_negative_feedback(entries: Sequence[Any], known_ips: Sequence[str],
                             known_artists: Sequence[str],
                             known_tracks: Sequence[str] = ()) -> List[Feedback]:
    """从纯对话里挑出"不想听/看某个实体"的话。

    @param entries `ChatMemory.entries()`（只要 `role == "user"` 的会被看）
    @param known_tracks 歌名与 id（两边都认 —— 用户一般说歌名, 偶尔念 id）
    @return 识别出来的负反馈（**实体名对不上就一条都不产生** —— 宁可不猜）
    @note 判据是"负向词 + 实体名同时出现"。同一句话里同时骂两个也会各记一条。
    """
    lookup: List[Tuple[str, str]] = []
    for name in known_ips or ():
        if str(name).strip():
            lookup.append(("ip", str(name).strip()))
    for name in known_artists or ():
        if str(name).strip():
            lookup.append(("artist", str(name).strip()))
    for name in known_tracks or ():
        if str(name).strip():
            lookup.append(("track", str(name).strip()))

    found: List[Feedback] = []
    for entry in entries or ():
        if getattr(entry, "role", "") != "user":
            continue
        text = str(getattr(entry, "text", "") or "")
        lowered = text.lower()
        if not any(cue.lower() in lowered for cue in NEGATIVE_CUES):
            continue
        for axis, name in lookup:
            if name.lower() in lowered:
                found.append(Feedback(axis=axis, name=name, text=text,
                                      ts=float(getattr(entry, "ts", 0.0) or 0.0),
                                      matched=name))
    return found


def muted_from_records(records: Sequence[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """把历史记录里的"不想看/听"名单汇总出来（**最新的那次说了算**）。

    @return {"ip": {名字: {...}}, "artist": {...}, "track": {id: {...}}}
    @note 顺序: 老 -> 新, 后面的覆盖前面的（用户后来又想听了就再构建一次即可）。
    """
    muted: Dict[str, Dict[str, Any]] = {"ip": {}, "artist": {}, "track": {}}
    for record in records or ():
        blob = record.get("muted") if isinstance(record, Mapping) else None
        if not isinstance(blob, Mapping):
            continue
        for axis in ("ip", "artist", "track"):
            for item in blob.get(axis) or ():
                if isinstance(item, Mapping) and str(item.get("name") or ""):
                    muted[axis][str(item["name"])] = dict(item)
    return muted


# ---------------------------------------------------------------------------
#  权重
# ---------------------------------------------------------------------------
def _share(value: float, total: float) -> float:
    return round(float(value) / float(total), 4) if total else 0.0


def _age_days(stamp: Any, now: datetime) -> Optional[float]:
    """ISO 时间戳 -> 多少天前（解析不了返回 None）。"""
    text = str(stamp or "").strip()
    if not text:
        return None
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    return max(0.0, (now - when).total_seconds() / 86400.0)


def _recency(stamp: Any, now: datetime) -> float:
    """30 天内线性衰减到 1（越新越接近 1）, 更早/没有 = 0。"""
    age = _age_days(stamp, now)
    if age is None:
        return 0.0
    return max(0.0, 1.0 - age / RECENCY_DAYS)


def wallpaper_weights(usages: Mapping[str, int], hits: Mapping[str, Sequence[str]],
                      mentions: Mapping[str, int], lasts: Mapping[str, Any],
                      now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """算各 IP 的权重（**纯函数** —— 输入是"已经查好的数", 方便单测与复核）。

    @param usages   {IP 名: 用户换过的图里"像该 IP"的张数}（= 命中张数）
    @param hits     {IP 名: [那些图的路径]}（只进记录, 不进公式）
    @param mentions {IP 名: 对话里提到它的次数}
    @param lasts    {IP 名: 最近一次用到的时间戳}
    @return [{name, weight, parts:{hits, mentions, recency, usage_share, mention_share,
             recency_share}, last_used, images}]（按权重降序; 权重都是 0 时按名字排）
    """
    now = now or datetime.now()
    names = sorted(set(list(usages) + list(mentions)))
    if not names:
        return []
    total_hits = float(sum(max(0, int(usages.get(n) or 0)) for n in names))
    total_mentions = float(sum(max(0, int(mentions.get(n) or 0)) for n in names))
    recency_values = {n: _recency(lasts.get(n), now) for n in names}
    total_recency = float(sum(recency_values.values()))

    out: List[Dict[str, Any]] = []
    for name in names:
        usage_share = _share(usages.get(name) or 0, total_hits)
        mention_share = _share(mentions.get(name) or 0, total_mentions)
        recency_share = _share(recency_values[name], total_recency)
        weight = (IP_WEIGHTS["usage"] * usage_share
                  + IP_WEIGHTS["mention"] * mention_share
                  + IP_WEIGHTS["recency"] * recency_share)
        out.append({
            "name": name,
            "weight": round(weight, 4),
            "parts": {"hits": int(usages.get(name) or 0),
                      "mentions": int(mentions.get(name) or 0),
                      "recency": round(recency_values[name], 4),
                      "usage_share": usage_share,
                      "mention_share": mention_share,
                      "recency_share": recency_share},
            "last_used": lasts.get(name),
            "images": sorted(str(p) for p in (hits.get(name) or ())),
        })
    out.sort(key=lambda item: (-item["weight"], item["name"]))
    return out


def artist_weights(plays: Mapping[str, int], mentions: Mapping[str, int],
                   lasts: Optional[Mapping[str, Any]] = None,
                   tracks: Optional[Mapping[str, Sequence[str]]] = None) -> List[Dict[str, Any]]:
    """算各歌手的权重（纯函数, 同 `wallpaper_weights`）。"""
    names = sorted(set(list(plays) + list(mentions)))
    if not names:
        return []
    total_plays = float(sum(max(0, int(plays.get(n) or 0)) for n in names))
    total_mentions = float(sum(max(0, int(mentions.get(n) or 0)) for n in names))
    out: List[Dict[str, Any]] = []
    for name in names:
        play_share = _share(plays.get(name) or 0, total_plays)
        mention_share = _share(mentions.get(name) or 0, total_mentions)
        weight = ARTIST_WEIGHTS["plays"] * play_share + ARTIST_WEIGHTS["mention"] * mention_share
        out.append({
            "name": name,
            "weight": round(weight, 4),
            "parts": {"plays": int(plays.get(name) or 0),
                      "mentions": int(mentions.get(name) or 0),
                      "plays_share": play_share,
                      "mention_share": mention_share},
            "last_played": (lasts or {}).get(name),
            "tracks": sorted(str(item) for item in (tracks or {}).get(name) or ()),
        })
    out.sort(key=lambda item: (-item["weight"], item["name"]))
    return out


def mention_counts(entries: Sequence[Any], names: Sequence[str],
                   roles: Sequence[str] = ("user",)) -> Dict[str, int]:
    """这些名字在纯对话里被提到几次（默认只数**用户**说的）。"""
    counts = {str(name): 0 for name in names}
    for entry in entries or ():
        if roles and getattr(entry, "role", "") not in roles:
            continue
        text = str(getattr(entry, "text", "") or "").lower()
        for name in counts:
            if name and name.lower() in text:
                counts[name] += 1
    return counts


# ---------------------------------------------------------------------------
#  心情（**唯一一次模型调用**）
# ---------------------------------------------------------------------------
def mood_prompt(excerpt: str, states: Sequence[str], clock: Optional[str] = None,
                reason: str = "") -> str:
    """拼"判心情"的那个提示词。

    @param excerpt 纯对话片段（`ChatMemory.excerpt()`, 已经带 `[study]` 这种场景标记）
    @param states  这段对话里出现过的模式（**场景**依据之一）
    @param clock   现在几点（`HH:MM`; 夜里/清晨也是场景）
    @param reason  这次为什么构建（攒满/等等）, 只影响提示词里的那句话
    """
    scope = "/".join([str(item) for item in states if str(item)]) or "未知"
    return (
        "根据下面这段对话和它发生的场景, 判断**用户当前的心情**。\n"
        "只允许从这 %d 个词里选一个: %s\n"
        "（中文对应: %s）\n"
        "场景: 板子在 %s 模式; 现在 %s。%s\n"
        "对话:\n%s\n"
        "只输出那一个英文词, 不要解释、不要标点。判不出来就输出 unknown。"
        % (len(MOOD_LABELS), " / ".join(MOOD_LABELS),
           " / ".join("%s=%s" % (k, MOOD_ZH[k]) for k in MOOD_LABELS),
           scope, clock or "时间未知", reason or "", excerpt or "(还没说什么)")
    )


def parse_mood(reply: str) -> Optional[str]:
    """把模型的回答解析成词表里的一个词（**认不出就 None, 不硬套**）。

    @note 容忍大小写/标点/中英文（"疲惫"/"tired."/"TIRED"都可以）; 一句话里出现
          两个不同的词 -> 也返回 None（说明它没照规矩来, 那就不采信）。
    """
    text = str(reply or "").strip().lower()
    if not text:
        return None
    found = []
    for label in MOOD_LABELS:
        if label in text or MOOD_ZH[label] in str(reply or ""):
            found.append(label)
    if MOOD_UNKNOWN in text:
        return MOOD_UNKNOWN
    if len(found) == 1:
        return found[0]
    return None


async def ask_mood(llm: Any, prompt: str) -> Dict[str, Any]:
    """问一次模型（**唯一的模型调用**）。

    @return {"label", "raw", "ok", "error"?, "ms"} —— 失败/解析不出都给 `unknown`
    @note 不抛异常: 判心情失败不该让整次构建失败（调用方照样落一行, 记下 raw）。
    """
    import time

    started = time.time()
    try:
        raw = await llm.chat(prompt)
    except Exception as exc:                # noqa: BLE001 - 单次模型失败不该炸构建
        return {"label": MOOD_UNKNOWN, "raw": "", "ok": False,
                "error": "%s: %s" % (type(exc).__name__, exc),
                "ms": int((time.time() - started) * 1000)}
    label = parse_mood(raw)
    # ⚠ 模型回 "unknown" 是**合法回答**（它老实说判不出来）, 但那不是"判出来了":
    #   `ok` 只认词表里那六个词 —— 否则记录上会出现 "ok=True, label=unknown"（板端实测）。
    return {"label": label or MOOD_UNKNOWN, "raw": str(raw or "").strip(),
            "ok": bool(label) and label != MOOD_UNKNOWN,
            "ms": int((time.time() - started) * 1000)}


# ---------------------------------------------------------------------------
#  构建（把上面几块拼起来 + 负反馈清零）
# ---------------------------------------------------------------------------
def count_hits(ranking: Sequence[str], used_paths: Sequence[str],
               top_k: Optional[int] = None) -> List[str]:
    """用过的图里, 有几张排在该 IP 的**前 K**（K 默认 = 用过的张数, 最少 3）。

    ⚠ 为什么不是"在不在候选里": `TagIndex.match("ip=…")` 给的是**全库按相似度排序**
      （不是筛过的集合 —— 40 张库就是 40 个候选）。所以"像这个 IP"只能是"排在前 K"
      这件事。K 取"用户用过的张数"意味着: 在 40 张里最像它的那几张里, 用户用过几张。
    @return 命中的路径（按相似度从高到低）
    """
    order = [str(item) for item in (ranking or ())]
    used = {str(item) for item in (used_paths or ())}
    if not order or not used:
        return []
    k = int(top_k) if top_k else max(3, len(used))
    k = max(1, min(k, len(order)))
    return [path for path in order[:k] if path in used]


@dataclass
class Sources:
    """**外部依赖的口子**（真实实现在 `repo_sources()`; 单测给假的就行）。"""

    list_walls: Callable[[], List[Dict[str, Any]]] = field(default=lambda: [])
    match_ip: Callable[[str], List[str]] = field(default=lambda _name: [])
    list_tracks: Callable[[], List[Dict[str, Any]]] = field(default=lambda: [])
    clear_wall_usage: Callable[[Sequence[str]], int] = field(default=lambda _paths: 0)
    clear_plays: Callable[[Sequence[str]], int] = field(default=lambda _ids: 0)
    ip_names: Sequence[str] = ()
    profile_file: str = ""
    wall_file: str = ""
    library_file: str = ""
    wall_dir: str = ""


def repo_sources(config: Optional[Mapping[str, Any]] = None,
                 profile_file: Optional[str] = None) -> Sources:
    """真实依赖: 读 `wall_data.jsonl` / `music_library.jsonl`, 锚点走 `TagIndex`。"""
    from ..media import music_library
    from ..vision import wall_data
    from ..vision.tag_index import TagIndex

    node: Any = config or {}
    for key in ("wallpaper", "tagging"):
        node = node.get(key) if isinstance(node, Mapping) else None
        if node is None:
            break
    tagging = node if isinstance(node, Mapping) else {}
    ip_presets = tagging.get("ip_presets") or {}
    wall_file = wall_data.resolve_data_file(tagging.get("data_file"))
    # ⚠ 锚点是**文件名**（相对 wallpaper.dir）—— 不把目录传下去, `match("ip=…")` 一个候选
    #   都给不出来（**而且是静默的**）。板端 T9-4 实测踩到: IP 权重一直是 0。
    wall_node = (config or {}).get("wallpaper") if isinstance(config, Mapping) else None
    wall_dir = ""
    if isinstance(wall_node, Mapping) and wall_node.get("dir"):
        wall_dir = str(wall_node["dir"])
    if not wall_dir:
        from .wallpaper import DEFAULT_WALLPAPER_DIR

        wall_dir = DEFAULT_WALLPAPER_DIR
    lib_node: Any = (config or {}).get("music") if isinstance(config, Mapping) else None
    library_file = music_library.resolve_library_file(
        (lib_node or {}).get("library_file") if isinstance(lib_node, Mapping) else None)
    out_file = resolve_profile_file(profile_file)

    def list_walls() -> List[Dict[str, Any]]:
        try:
            records, _problems = wall_data.read_records(wall_file)
        except wall_data.WallDataError as exc:
            raise ProfileError("壁纸数据读不了: %s" % exc)
        return [dict(item) for item in records]

    cached: Dict[str, Any] = {}

    def _index() -> Any:
        """标签索引只建一次（一个 IP 建一次等于把 234KB 读十遍）。"""
        if "index" not in cached:
            try:
                cached["index"] = TagIndex.from_file(wall_file)
            except Exception as exc:             # noqa: BLE001 - 索引坏了就当没锚点
                raise ProfileError("壁纸标签索引读不了: %s" % exc)
        return cached["index"]

    def match_ip(name: str) -> List[str]:
        """该 IP 的原型向量检索 —— **返回全库按相似度排好的路径**（不是筛过的集合）。"""
        index = _index()
        if not index.count():
            return []
        result = index.match("ip=%s" % name, presets=ip_presets, wallpaper_dir=wall_dir)
        if not result.ok:
            raise ProfileError("ip=%s 检索不了: %s" % (name, result.error))
        return [str(path) for path in result.pool or ()]

    def list_tracks() -> List[Dict[str, Any]]:
        tracks, _problems = music_library.read_tracks(library_file)
        return [dict(item) for item in tracks]

    def clear_wall_usage(paths: Sequence[str]) -> int:
        return wall_data.clear_usage_in_file(wall_file, list(paths))

    def clear_plays(ids: Sequence[str]) -> int:
        return music_library.clear_plays(library_file, list(ids))

    return Sources(list_walls=list_walls, match_ip=match_ip, list_tracks=list_tracks,
                   clear_wall_usage=clear_wall_usage, clear_plays=clear_plays,
                   ip_names=sorted(str(k) for k in ip_presets),
                   profile_file=out_file, wall_file=wall_file, library_file=library_file,
                   wall_dir=wall_dir)


#: 心情 -> **内容标签**（壁纸与音乐共用同一套 mood 词表, 见 tag_vocab.MOOD）。
#: 心情是"人现在的状态", 标签是"东西给人的感觉" —— 这张表就是两者之间的桥:
#: 累了/烦躁 -> 要安静的; 开心 -> 来点活力/可爱的; 专注 -> 安静但别太软。
#: ⚠ 只映射到词表里真有的那 8 个标签; `unknown`（判不出来）**不给任何偏好**。
MOOD_TO_TAGS: Dict[str, Tuple[str, ...]] = {
    "focused": ("calm", "minimal")[:1],      # 专注: 安静（minimal 在 scene 轴上, 不混）
    "relaxed": ("cozy", "calm"),
    "happy": ("energetic", "cute"),
    "tired": ("calm", "cozy"),
    "stressed": ("calm",),
    "calm": ("calm",),
}

#: 排序配方（与"画像怎么算"那两个配方分开: 这两个是"画像怎么用"）。
#: **主信号（IP/歌手）为主, 心情是加分项, "没怎么用过"破平局** —— 这样解释起来简单:
#: 用户明确喜欢的东西优先, 心情只在差不多的时候起作用（0.3 会让"心情命中"几乎追平
#: 一个满分的 IP 匹配, 那就说不清谁说了算了）。
RANK_WALLPAPER_WEIGHTS = {"ip": 0.7, "mood": 0.2, "fresh": 0.1}
RANK_TRACK_WEIGHTS = {"artist": 0.7, "mood": 0.2, "fresh": 0.1}

#: "类似"的判据（T10 你定的 A1）: **同歌手** 永远算; 另外这些 tag 轴共有也算。
#: ⚠ 默认**不含 genre**: 板端库里 `genre` 是"日语/流行"这种大口袋, 一句"不想听"
#:   会把整个库都清掉（比"去掉类似的"过头太多）。要开就改配置。
DEFAULT_SIMILAR_TAGS: Tuple[str, ...] = ("mood",)


def mood_tags(label: Optional[str]) -> Tuple[str, ...]:
    """画像里的心情 -> 内容标签（认不出/unknown -> 空, **不给偏好**）。"""
    return tuple(MOOD_TO_TAGS.get(str(label or "").strip().lower(), ()))


def profile_basis(profile: Optional[Mapping[str, Any]]) -> Dict[str, bool]:
    """这份画像**能不能拿来挑东西**（T10-3/T10-4 靠它决定"用画像"还是"回退老规则"）。

    @return {"ip": 有 IP 权重, "artist": 有歌手权重, "mood": 心情已知,
             "wallpaper": 壁纸可用, "music": 音乐可用}
    @note **薄样本不算可用**: `thin` 里点了名的那些轴一律当没有（宁可回退, 不装）。
    """
    data = profile if isinstance(profile, Mapping) else {}
    thin = {str(item) for item in (data.get("thin") or ())}
    walls = [(data.get("walls") or {}).get("ip") or []]
    artists = [(data.get("music") or {}).get("artist") or []]
    has_ip = any(float(row.get("weight") or 0) > 0 for row in walls[0]) and "壁纸用量" not in thin
    has_artist = (any(float(row.get("weight") or 0) > 0 for row in artists[0])
                  and "音乐库" not in thin)
    label = str((data.get("mood") or {}).get("label") or MOOD_UNKNOWN)
    has_mood = bool(mood_tags(label))
    return {"ip": bool(has_ip), "artist": bool(has_artist), "mood": has_mood,
            "wallpaper": bool(has_ip or has_mood), "music": bool(has_artist or has_mood)}


def _freshness(count: Any) -> float:
    """"没怎么看过/听过"的加分: 1/(1+次数)（0 次 = 1.0）。"""
    try:
        value = max(0, int(count or 0))
    except (TypeError, ValueError):
        value = 0
    return 1.0 / (1.0 + value)


def rank_wallpapers(profile: Optional[Mapping[str, Any]], candidates: Sequence[str], *,
                    ip_similarity: Optional[Callable[[str, str], float]] = None,
                    usage: Optional[Callable[[str], int]] = None,
                    mood_of: Optional[Callable[[str], Sequence[str]]] = None,
                    limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """按画像把候选壁纸排一遍（**纯函数**: 外部的相似度/用量/标签都从参数进来）。

    @param profile 一条画像记录（`build_profile()` 的产物; None/空 = 没有画像）
    @param candidates 候选路径（一般就是目录里那些图）
    @param ip_similarity `(IP 名, 路径) -> 0..1`（调用方用标签索引的向量算; 测试给假的）
    @param usage `路径 -> 用过几次`（默认都算 0）
    @param mood_of `路径 -> 它的 mood 标签`（默认空）
    @return [{path, score, parts:{ip,mood,fresh}, why:[…]}] 按分数降序
    @note 分数是**相对**的（画像权重本身是占比）, 只用于排序 —— 不要当概率。
    """
    data = profile if isinstance(profile, Mapping) else {}
    basis = profile_basis(data)
    ip_rows = [row for row in ((data.get("walls") or {}).get("ip") or [])
               if float(row.get("weight") or 0) > 0]
    tags = mood_tags((data.get("mood") or {}).get("label")) if basis["mood"] else ()

    out: List[Dict[str, Any]] = []
    for path in candidates or ():
        text = str(path)
        why: List[str] = []
        best_ip, best_name = 0.0, ""
        if basis["ip"] and ip_similarity is not None:
            for row in ip_rows:
                try:
                    similarity = float(ip_similarity(str(row.get("name")), text) or 0.0)
                except Exception:                     # noqa: BLE001 - 相似度算不出来就当 0
                    similarity = 0.0
                score = float(row.get("weight") or 0) * max(0.0, min(1.0, similarity))
                if score > best_ip:
                    best_ip, best_name = score, str(row.get("name"))
        mood_hit = 0.0
        if tags and mood_of is not None:
            try:
                labels = {str(item) for item in (mood_of(text) or ())}
            except Exception:                         # noqa: BLE001
                labels = set()
            hit = labels & set(tags)
            if hit:
                mood_hit = 1.0
                why.append("命中 %s" % "、".join(sorted(hit)))
        used = 0
        if usage is not None:
            try:
                used = max(0, int(usage(text) or 0))
            except Exception:                         # noqa: BLE001
                used = 0
        fresh = _freshness(used)
        score = (RANK_WALLPAPER_WEIGHTS["ip"] * best_ip
                 + RANK_WALLPAPER_WEIGHTS["mood"] * mood_hit
                 + RANK_WALLPAPER_WEIGHTS["fresh"] * fresh)
        if best_ip > 0:
            why.insert(0, "像 %s（%.2f）" % (best_name, best_ip))
        if used == 0:
            why.append("还没看过")
        elif used <= 2:
            why.append("只看过 %d 次" % used)
        if not (basis["ip"] or basis["mood"]):
            # 画像给不出偏好时**要说清是回退**（调用方据此决定要不要走老规则, 日志里也一眼看得出来）
            why.insert(0, "画像里没有可用的偏好（回退到看得最少的）")
        if not why:
            why.append("画像里没有可用的偏好（回退）")
        out.append({"path": text, "score": round(score, 4),
                    "parts": {"ip": round(best_ip, 4), "mood": mood_hit,
                              "fresh": round(fresh, 4), "used": used},
                    "why": why})
    out.sort(key=lambda item: (-item["score"], item["path"]))
    return out[:int(limit)] if limit else out


def rank_tracks(profile: Optional[Mapping[str, Any]], tracks: Sequence[Mapping[str, Any]], *,
                exclude: Sequence[str] = (), muted_artists: Sequence[str] = (),
                muted_tracks: Sequence[str] = (),
                limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """按画像把候选歌排一遍（纯函数, 同 `rank_wallpapers`）。

    @param exclude 不要的 id（已经在队列里的 / 正在放的）
    @param muted_artists / muted_tracks 画像里"不想听"的名单（歌手名 / 歌名或 id）
    @return [{id, name, score, parts:{artist,mood,fresh,plays}, why:[…]}] 降序
    """
    data = profile if isinstance(profile, Mapping) else {}
    basis = profile_basis(data)
    artist_rows = {str(row.get("name")): float(row.get("weight") or 0)
                   for row in ((data.get("music") or {}).get("artist") or [])
                   if float(row.get("weight") or 0) > 0}
    tags = mood_tags((data.get("mood") or {}).get("label")) if basis["mood"] else ()
    skip = {str(item) for item in (exclude or ())}
    skip_artists = {str(item) for item in (muted_artists or ())}
    skip_tracks = {str(item) for item in (muted_tracks or ())}

    out: List[Dict[str, Any]] = []
    for track in tracks or ():
        if not isinstance(track, Mapping):
            continue
        track_id = str(track.get("id") or "")
        name = str(track.get("name") or track_id)
        if not track_id or track_id in skip:
            continue
        if track_id in skip_tracks or name in skip_tracks:
            continue
        artists = _artists_of(track)
        if any(artist in skip_artists for artist in artists):
            continue
        best_artist, best_name = 0.0, ""
        if basis["artist"]:
            for artist in artists:
                weight = float(artist_rows.get(artist) or 0.0)
                if weight > best_artist:
                    best_artist, best_name = weight, artist
        why: List[str] = []
        mood_hit = 0.0
        if tags:
            values = {str(item) for item in ((track.get("tags") or {}).get("mood") or ())}
            hit = values & set(tags)
            if hit:
                mood_hit = 1.0
                why.append("命中 %s" % "、".join(sorted(hit)))
        try:
            plays = max(0, int(track.get("plays") or 0))
        except (TypeError, ValueError):
            plays = 0
        fresh = _freshness(plays)
        score = (RANK_TRACK_WEIGHTS["artist"] * best_artist
                 + RANK_TRACK_WEIGHTS["mood"] * mood_hit
                 + RANK_TRACK_WEIGHTS["fresh"] * fresh)
        if best_artist > 0:
            why.insert(0, "喜欢 %s（%.2f）" % (best_name, best_artist))
        if plays == 0:
            why.append("还没听过")
        elif plays <= 2:
            why.append("只听过 %d 次" % plays)
        if not (basis["artist"] or basis["mood"]):
            why.insert(0, "画像里没有可用的偏好（回退到听得最少的）")
        if not why:
            why.append("画像里没有可用的偏好（回退）")
        out.append({"id": track_id, "name": name, "score": round(score, 4),
                    "parts": {"artist": round(best_artist, 4), "mood": mood_hit,
                              "fresh": round(fresh, 4), "plays": plays},
                    "why": why})
    out.sort(key=lambda item: (-item["score"], item["id"]))
    return out[:int(limit)] if limit else out


def similar_tracks(tracks: Sequence[Mapping[str, Any]], target: Mapping[str, Any],
                   tags: Sequence[str] = DEFAULT_SIMILAR_TAGS) -> List[str]:
    """"差不多是同一类"的歌（T10 你定的 A1: **同歌手** + 共有所配置的 tag）。

    @param target 被点名的那首（本地库记录）
    @return 这些歌的 id（**含被点名的那首**）; 认不出目标就返回空
    @note 判据是"或": 同歌手 **或** 共有一个 `tags` 轴上的值。
    @note ⚠ 默认只看 `mood` 轴, **不看 genre** —— 板端库里 genre 是"日语/流行"这种大口袋,
          一句"不想听"会把整个库清空（比"去掉类似的"过头太多）。
    """
    if not isinstance(target, Mapping) or not str(target.get("id") or ""):
        return []
    want_id = str(target.get("id"))
    want_artists = set(_artists_of(target))
    axes = [str(axis) for axis in (tags or ()) if str(axis)]
    want_tags = {str(value) for axis in axes
                 for value in ((target.get("tags") or {}).get(axis) or ())}
    picked: List[str] = []
    for track in tracks or ():
        if not isinstance(track, Mapping) or not str(track.get("id") or ""):
            continue
        track_id = str(track.get("id"))
        if track_id == want_id:
            picked.append(track_id)
            continue
        same_artist = bool(want_artists & set(_artists_of(track)))
        shared = {str(value) for axis in axes
                  for value in ((track.get("tags") or {}).get(axis) or ())} & want_tags
        if same_artist or shared:
            picked.append(track_id)
    return picked


def _artists_of(track: Mapping[str, Any]) -> List[str]:
    tags = track.get("tags") or {}
    values = tags.get("artist") if isinstance(tags, Mapping) else None
    if isinstance(values, str):
        values = [values]
    out = [str(item).strip() for item in (values or []) if str(item).strip()]
    if not out and str(track.get("artists") or "").strip():
        out = [part.strip() for part in str(track["artists"]).replace("/", ",").split(",")
               if part.strip()]
    return out


def _aware(moment: Optional[datetime]) -> datetime:
    """统一成 aware（跟数据文件里的 `datetime.now().isoformat()` 同一种本地时间）。"""
    return moment or datetime.now()


async def build_profile(entries: Sequence[Any], *, config: Optional[Mapping[str, Any]] = None,
                        sources: Optional[Sources] = None, llm: Any = None,
                        now: Optional[datetime] = None, reason: str = "chat_chars",
                        trigger: Optional[Mapping[str, Any]] = None,
                        clock: Optional[str] = None,
                        history: Optional[Sequence[Mapping[str, Any]]] = None) -> Dict[str, Any]:
    """构建一条画像记录（**不落盘** —— 落盘是 `append_record()` 的事, 好测）。

    @param entries 纯对话（`ChatMemory.entries()`）
    @param history 已有的画像记录（用来继承"不想看/听"名单, 只读最后一条也行）
    @param llm     判心情用的 provider（None = 不判, 记 unknown; 单测可以不传）
    @return 一条记录（见模块头; `walls.ip` / `music.artist` / `mood` 三块）
    """
    src = sources or repo_sources(config)
    moment = _aware(now)
    # ⚠ `entries` 经常直接是 `ChatMemory`（它有 `entries()` 也有 `__iter__`）——
    #   这里两种都收（集成时才发现 `list(ChatMemory)` 曾经是空的, 见 T9-3 的接线测试）。
    if hasattr(entries, "entries"):
        items = list(entries.entries())
    else:
        items = list(entries or ())
    previous = list(history or ())

    walls = src.list_walls()
    tracks = src.list_tracks()

    # ---- 1) 壁纸 IP 权重 ----
    hit_counts: Dict[str, int] = {}
    hit_paths: Dict[str, List[str]] = {}
    lasts: Dict[str, Any] = {}
    anchor_problems: Dict[str, str] = {}
    used_by_path = {str(record.get("path")): record for record in walls}
    used_paths = [path for path, record in used_by_path.items()
                  if int(record.get("used") or 0) > 0]
    for name in src.ip_names:
        try:
            ranking = list(src.match_ip(name))
        except ProfileError as exc:
            # ⚠ **不许静默**: 锚点/目录配错时如实记下来（否则画像会一直显示 IP 权重 0）
            anchor_problems[name] = str(exc)
            ranking = []
        hits = count_hits(ranking, used_paths)
        hit_counts[name] = len(hits)
        hit_paths[name] = hits
        stamps = [used_by_path[path].get("last_used") for path in hits
                  if used_by_path[path].get("last_used")]
        if stamps:
            lasts[name] = max(str(item) for item in stamps)
    wall_mentions = mention_counts(items, list(src.ip_names))
    wall_rows = wallpaper_weights(hit_counts, hit_paths, wall_mentions, lasts, now=moment)

    # ---- 2) 歌手权重 ----
    plays: Dict[str, int] = {}
    per_artist_tracks: Dict[str, List[str]] = {}
    artist_last: Dict[str, Any] = {}
    for track in tracks:
        for artist in _artists_of(track):
            plays[artist] = plays.get(artist, 0) + max(0, int(track.get("plays") or 0))
            per_artist_tracks.setdefault(artist, []).append(str(track.get("id")))
            stamp = track.get("last_played")
            if stamp and str(stamp) > str(artist_last.get(artist) or ""):
                artist_last[artist] = stamp
    artist_names = sorted(plays)
    artist_mentions = mention_counts(items, artist_names)
    artist_rows = artist_weights(plays, artist_mentions, artist_last, per_artist_tracks)

    # ---- 3) 负反馈: 清零（画像 + **源文件**）----
    known_tracks = sorted(({str(track.get("name") or "") for track in tracks}
                           | {str(track.get("id") or "") for track in tracks}) - {""})
    feedback = detect_negative_feedback(items, src.ip_names, artist_names, known_tracks)
    muted = muted_from_records(previous)
    cleared: Dict[str, Any] = {"wall_images": 0, "tracks": 0, "track_ids": []}
    unmatched: List[Dict[str, Any]] = []
    feedback_done: List[Dict[str, Any]] = []

    def _zero_tracks(ids: Sequence[str], text: str, axis: str) -> None:
        """把这几首按 T9 的口径处理: 清库里的 plays + 进 muted（给 T10-5 清队列用）。"""
        wanted = [str(item) for item in ids if str(item)]
        if not wanted:
            return
        cleared["tracks"] += int(src.clear_plays(wanted) or 0)
        cleared["track_ids"] = sorted(set(cleared["track_ids"]) | set(wanted))
        at = moment.isoformat(timespec="seconds")
        for track_id in wanted:
            entry = {"axis": "track", "name": track_id, "at": at, "text": text,
                     "by": axis}
            muted["track"][track_id] = entry

    for item in feedback:
        axis, name = item.axis, item.name
        if axis == "track":
            # 点名一首歌 -> **类似的全部**（同歌手 + 共有 mood）一起清（你定的第 5 条）
            target = next((track for track in tracks
                           if str(track.get("id")) == name
                           or str(track.get("name") or "") == name), None)
            if target is None:
                unmatched.append(item.to_dict())
                continue
            ids = similar_tracks(tracks, target)
            _zero_tracks(ids, item.text, "track")
            done = item.to_dict()
            done["affected"] = list(ids)
            feedback_done.append(done)
            continue
        row = next((r for r in (wall_rows if axis == "ip" else artist_rows)
                    if r["name"] == name), None)
        if row is None:
            unmatched.append(item.to_dict())
            continue
        if axis == "ip":
            images = [str(p) for p in row.get("images") or ()]
            cleared["wall_images"] += int(src.clear_wall_usage(images) or 0)
            row["parts"] = {key: 0 for key in row["parts"]}
            row["images"] = []
            row["last_used"] = None
            affected: List[str] = []
        else:
            affected = [str(x) for x in row.get("tracks") or ()]
            _zero_tracks(affected, item.text, "artist")
            row["parts"] = {key: 0 for key in row["parts"]}
            row["tracks"] = []
            row["last_played"] = None
        row["weight"] = 0.0
        row["muted_at"] = moment.isoformat(timespec="seconds")
        row["muted_text"] = item.text
        muted[axis][name] = {"axis": axis, "name": name, "at": row["muted_at"],
                             "text": item.text}
        done = item.to_dict()
        done["affected"] = affected
        feedback_done.append(done)

    # 继承下来的 muted: 每次重建都要**重新套用**（否则源文件里的老用量会把它顶回来）
    for axis, rows in (("ip", wall_rows), ("artist", artist_rows)):
        for row in rows:
            item = muted.get(axis, {}).get(row["name"])
            if not item:
                continue
            row["weight"] = 0.0
            row["parts"] = {key: 0 for key in row["parts"]}
            row["muted_at"] = item.get("at")
            row["muted_text"] = item.get("text")
    for rows in (wall_rows, artist_rows):
        rows.sort(key=lambda item: (-item["weight"], item["name"]))

    # ---- 4) 心情（唯一一次模型调用）----
    states = sorted({getattr(entry, "state", "") for entry in items
                     if getattr(entry, "state", "")})
    if entries is not None and hasattr(entries, "excerpt"):
        excerpt = entries.excerpt(max_chars=800)
    elif items:
        excerpt = "\n".join(
            "%s%s: %s" % ("[%s] " % getattr(e, "state", "") if getattr(e, "state", "") else "",
                          "用户" if getattr(e, "role", "") == "user" else "助手",
                          getattr(e, "text", "")) for e in items[-8:])
    else:
        excerpt = ""
    if llm is None:
        mood = {"label": MOOD_UNKNOWN, "raw": "", "ok": False,
                "error": "这次没有模型可用（没接 LLM）", "ms": 0,
                "prompt_chars": 0}
    else:
        prompt = mood_prompt(excerpt, states, clock=clock, reason=reason)
        mood = await ask_mood(llm, prompt)
        mood["prompt_chars"] = len(prompt)

    samples = {
        "wall_tagged": len(walls),
        "wall_images_used": sum(1 for r in walls if int(r.get("used") or 0) > 0),
        "tracks": len(tracks),
        "plays": sum(max(0, int(t.get("plays") or 0)) for t in tracks),
        "chat_turns": sum(1 for e in items if getattr(e, "role", "") == "user"),
        "chat_chars": sum(len(str(getattr(e, "text", "") or "")) for e in items),
    }
    thin = [key for key, value in (("壁纸用量", samples["wall_images_used"]),
                                  ("音乐库", samples["tracks"]),
                                  ("对话", samples["chat_turns"])) if value < 3]

    return {
        "version": PROFILE_VERSION,
        "built_at": moment.isoformat(timespec="seconds"),
        "trigger": dict(trigger or {"reason": reason}),
        "samples": samples,
        "walls": {"ip": wall_rows, "anchor_problems": anchor_problems},
        "music": {"artist": artist_rows},
        "mood": {"label": mood.get("label") or MOOD_UNKNOWN,
                 "zh": MOOD_ZH.get(mood.get("label") or MOOD_UNKNOWN, ""),
                 "ok": bool(mood.get("ok")),
                 "confidence": ("low" if (not mood.get("ok") or thin) else
                                "medium" if samples["chat_turns"] < 8 else "high"),
                 # ⚠ 这里**不存对话原文**（你定的"纯对话记忆不落盘"）—— 只存长度,
                 #   要复核就去看日志里那段 excerpt。唯一留下的原话是 `muted_text`
                 #   （一个破坏性动作的凭据, 必须能追溯）。
                 "excerpt_chars": len(excerpt),
                 "raw": mood.get("raw", ""),
                 "error": mood.get("error"),
                 "ms": mood.get("ms", 0),
                 "prompt_chars": mood.get("prompt_chars", 0)},
        "feedback": feedback_done,
        "unmatched_feedback": unmatched,
        "muted": {"ip": list(muted["ip"].values()), "artist": list(muted["artist"].values()),
                  "track": list(muted["track"].values())},
        "cleared": cleared,
        "thin": thin,
    }


# ---------------------------------------------------------------------------
#  落盘（**本模块是 `config/user_profile.jsonl` 的唯一写者**）
# ---------------------------------------------------------------------------
def read_records(path: str) -> List[Dict[str, Any]]:
    """读所有记录（坏行跳过并记一句, 不让一行坏掉整个文件）。"""
    if not os.path.exists(path):
        return []
    out: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, 1):
                text = line.strip()
                if not text:
                    continue
                try:
                    item = json.loads(text)
                except ValueError:
                    continue
                if isinstance(item, dict):
                    out.append(item)
    except OSError as exc:
        raise ProfileError("画像文件读不了 %s: %s" % (path, exc))
    return out


def append_record(path: str, record: Mapping[str, Any]) -> str:
    """追加一行（**读全量 + 原子写** —— 走 `agent/config.py::write_text_atomic`）。

    @return 写进去的整段文本
    @raise ProfileError 写不了
    """
    from ..config import write_text_atomic

    existing = read_records(path)
    lines = [json.dumps(item, ensure_ascii=False, sort_keys=False) for item in existing]
    try:
        lines.append(json.dumps(dict(record), ensure_ascii=False, sort_keys=False))
    except (TypeError, ValueError) as exc:
        raise ProfileError("画像记录无法序列化: %s" % exc)
    text = "\n".join(lines) + "\n"
    try:
        write_text_atomic(path, text, encoding="utf-8")
    except OSError as exc:
        raise ProfileError("画像文件写不了 %s: %s" % (path, exc))
    return text


def latest_record(path: str) -> Optional[Dict[str, Any]]:
    """最后一次构建的那行（没有就是 None）。"""
    records = read_records(path)
    return records[-1] if records else None
