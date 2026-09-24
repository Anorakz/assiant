# ============================================================================
#  agent/media/music_library.py — 本地音乐库（Phase 7 T8-3）
#
#  这是什么（按你的要求: **不再依赖云歌单**）
#  ---------------------------------------------------------------------------
#  `config/music_library.jsonl` —— 一行一首歌, **纯本地**:
#
#      {"id":"2747166493","name":"JANE DOE","artists":"米津玄師, 宇多田ヒカル",
#       "album":"JANE DOE","duration_ms":236007,
#       "tags":{"genre":["日语","流行"],"artist":["米津玄師"],"lang":["jp"],
#               "mood":["energetic"],"scene":["anime"]},
#       "plays":3,"last_played":"2026-09-23T18:31:00","added_at":"…",
#       "source":"playlist:8503898111"}
#
#  云歌单只用来**导入一次 id**（"颂你乐" 18 首是第一批）; 之后挑歌只看这个文件。
#
#  两类 tag（你定的"元数据自动 + chat 补充"）
#  ---------------------------------------------------------------------------
#  · **自动**（导入时, 只靠元数据 —— 我们没有音频特征, 这一点必须诚实）:
#      genre   云歌单自己的标签（"日语"/"流行"…）
#      artist  歌手名（逐条）
#      lang    从歌名/专辑名**猜**的语种（假名→jp、汉字→zh、否则 en）—— 标着 "猜的"
#      era     专辑发行年（拿得到才写）
#  · **chat 补充**: 你说"这首很燃" → `add_tags(track, {"mood": ["energetic"]})`
#    自由轴（mood/scene/style… 想加什么轴都行, 与壁纸的三轴不是一套东西）
#
#  播放次数
#  ---------------------------------------------------------------------------
#  只有**真的听了 30 s**才 +1（你定的阈值; 见 `agent/core/music.py` 的会话逻辑）——
#  避免"点开就切"把次数刷上去。次数是"下一首由 chat 决定"的主要依据
#  （`pick(sort="plays_asc")` = 听得最少的先）。
#
#  边界
#  ---------------------------------------------------------------------------
#  · **纯 Python**（不 import numpy / 不联网）, 开发机与板端跑同一份
#  · 本模块是 `config/music_library.jsonl` 的**唯一写者**（写盘走
#    `agent/config.py::write_text_atomic` + `.bak`）
#  · 它是**板端本地数据**（你听过的歌单）, 所以进 `.gitignore`
# ============================================================================

from __future__ import annotations

import json
import logging
import os
import random
import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "MusicLibraryError",
    "RECORD_VERSION",
    "DEFAULT_LIBRARY_FILE",
    "TAG_AXES_AUTO",
    "repo_root",
    "default_library_file",
    "resolve_library_file",
    "make_record",
    "load",
    "read_tracks",
    "write_tracks",
    "upsert_tracks",
    "add_tags",
    "remove_tags",
    "bump_play",
    "pick",
    "tag_counts",
    "summarise",
    "guess_lang",
    "auto_tags",
]

_log = logging.getLogger(__name__)

#: 记录格式版本（结构变了就 +1）
RECORD_VERSION = 1

#: 默认库文件（相对仓库根）
DEFAULT_LIBRARY_FILE = "config/music_library.jsonl"

#: 导入时**自动**打的轴（chat 补充的轴不在此列 —— 那些是自由轴）
TAG_AXES_AUTO: Tuple[str, ...] = ("genre", "artist", "lang", "era")

_NEWLINE = "\n"

#: 假名（平假名/片假名）—— 判"日语歌"用
_KANA_RE = re.compile(r"[\u3040-\u30ff]")
_HAN_RE = re.compile(r"[\u4e00-\u9fff]")


class MusicLibraryError(ValueError):
    """库文件读不了/写不了/格式不对。

    继承 ValueError，与 `agent/vision/wall_data.py::WallDataError` 同款约定。
    """


# ---------------------------------------------------------------------------
#  路径
# ---------------------------------------------------------------------------
def repo_root() -> str:
    """仓库根目录（本文件在 agent/media/ 下，往上两级）。"""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def default_library_file() -> str:
    return os.path.normpath(os.path.join(repo_root(), DEFAULT_LIBRARY_FILE))


def resolve_library_file(configured: Optional[str] = None) -> str:
    """把配置里的 `music.library_file` 解析成绝对路径（相对路径按**仓库根**）。

    @note 一律过 `normpath`：`os.path.join` 遇到带 `/` 的相对路径会混出
          `…/assitant\\config/music_library.jsonl` 这种两种分隔符都在的串。
    """
    text = configured.strip() if isinstance(configured, str) else ""
    if not text:
        return default_library_file()
    if os.path.isabs(text):
        return os.path.normpath(text)
    return os.path.normpath(os.path.join(repo_root(), text))


# ---------------------------------------------------------------------------
#  记录
# ---------------------------------------------------------------------------
def make_record(track_id: Any, name: str, artists: str = "", album: str = "",
                duration_ms: int = 0, tags: Optional[Mapping[str, Any]] = None,
                plays: int = 0, last_played: Optional[str] = None,
                source: str = "", added_at: Optional[str] = None) -> Dict[str, Any]:
    """造一行记录（**schema 只在这里定义一次**）。"""
    return {
        "version": RECORD_VERSION,
        "id": str(track_id),
        "name": str(name or ""),
        "artists": str(artists or ""),
        "album": str(album or ""),
        "duration_ms": int(duration_ms or 0),
        "tags": _clean_tags(tags),
        "plays": int(plays or 0),
        "last_played": last_played,
        "added_at": added_at or datetime.now().isoformat(timespec="seconds"),
        "source": str(source or ""),
    }


def _clean_tags(tags: Optional[Mapping[str, Any]]) -> Dict[str, List[str]]:
    """归一化 tag：{轴: [值…]}，去掉空轴/重复值，保持书写顺序。"""
    out: Dict[str, List[str]] = {}
    if not isinstance(tags, Mapping):
        return out
    for axis, values in tags.items():
        name = str(axis).strip()
        if not name:
            continue
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, (list, tuple, set)):
            continue
        cleaned: List[str] = []
        for value in values:
            text = str(value).strip()
            if text and text not in cleaned:
                cleaned.append(text)
        if cleaned:
            out[name] = cleaned
    return out


# ---------------------------------------------------------------------------
#  读 / 写
# ---------------------------------------------------------------------------
def load(path: str) -> Dict[str, Any]:
    """读库文件。

    @return {"tracks": [...], "problems": [...]}
    @note 文件不存在 = 空库（不是错误）；**坏行跳过并记一句**，不让一行坏数据
          把整个库读不出来（与 wall_data.jsonl 同款口径）。
    """
    if not os.path.exists(path):
        return {"tracks": [], "problems": []}
    tracks: List[Dict[str, Any]] = []
    problems: List[str] = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, 1):
                text = line.strip()
                if not text:
                    continue
                try:
                    item = json.loads(text)
                except ValueError as exc:
                    problems.append("第 %d 行不是合法 JSON: %s" % (lineno, exc))
                    continue
                if not isinstance(item, dict) or not str(item.get("id") or ""):
                    problems.append("第 %d 行缺 id 字段，已跳过" % lineno)
                    continue
                tracks.append(item)
    except OSError as exc:
        raise MusicLibraryError("读不了音乐库 %s: %s" % (path, exc))
    return {"tracks": tracks, "problems": problems}


def read_tracks(path: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    loaded = load(path)
    return loaded["tracks"], loaded["problems"]


def write_tracks(path: str, tracks: Sequence[Mapping[str, Any]],
                 backup: bool = True) -> str:
    """整体重写库文件（**原子写 + .bak**）。@return 写进去的文本。"""
    from ..config import write_text_atomic

    lines: List[str] = []
    for track in tracks:
        try:
            lines.append(json.dumps(track, ensure_ascii=False, sort_keys=False))
        except (TypeError, ValueError) as exc:
            raise MusicLibraryError("记录无法序列化: %s (%r)" % (exc, track.get("id")))
    text = _NEWLINE.join(lines) + (_NEWLINE if lines else "")
    if backup and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                old = handle.read()
            with open(path + ".bak", "w", encoding="utf-8") as handle:
                handle.write(old)
        except OSError as exc:                       # 备份失败不该阻止写入
            _log.warning("music_library: 留备份失败 (已忽略): %r", exc)
    try:
        write_text_atomic(path, text, encoding="utf-8")
    except OSError as exc:
        raise MusicLibraryError("写不了音乐库 %s: %s" % (path, exc))
    return text


# ---------------------------------------------------------------------------
#  合并 / 打标 / 计数
# ---------------------------------------------------------------------------
def upsert_tracks(existing: Sequence[Mapping[str, Any]],
                  incoming: Sequence[Mapping[str, Any]]) -> Tuple[List[Dict[str, Any]], int, int]:
    """把导入的歌并进库里（**按 id**）。

    @return (合并后的列表, 新增数, 更新数)
    @note **已有的 plays 一定保留**：导入只是"补元数据", 不能把听歌次数清掉。
    @note 已在本地的歌**不重复添加**（幂等：同一个歌单导两次结果一样）。
    """
    by_id: Dict[str, Dict[str, Any]] = {str(t.get("id")): dict(t) for t in existing}
    order: List[str] = [str(t.get("id")) for t in existing]
    added = updated = 0
    for item in incoming:
        track_id = str(item.get("id") or "")
        if not track_id:
            continue
        if track_id not in by_id:
            by_id[track_id] = dict(item)
            order.append(track_id)
            added += 1
            continue
        current = by_id[track_id]
        merged = dict(current)
        for key in ("name", "artists", "album", "duration_ms", "source"):
            if item.get(key):
                merged[key] = item[key]
        merged["plays"] = int(current.get("plays") or 0)          # ← 次数不丢
        merged["last_played"] = current.get("last_played")
        merged["added_at"] = current.get("added_at") or item.get("added_at")
        merged["tags"] = _merge_tags(current.get("tags"), item.get("tags"))
        if merged != current:
            updated += 1
        by_id[track_id] = merged
    return [by_id[key] for key in order], added, updated


def _merge_tags(old: Optional[Mapping[str, Any]],
                new: Optional[Mapping[str, Any]]) -> Dict[str, List[str]]:
    """合并两边的 tag（按轴取并集，保留原有顺序）。"""
    out = _clean_tags(old)
    for axis, values in _clean_tags(new).items():
        bucket = out.setdefault(axis, [])
        for value in values:
            if value not in bucket:
                bucket.append(value)
    return out


def add_tags(track: Mapping[str, Any], tags: Mapping[str, Any]) -> Dict[str, Any]:
    """给一首歌**补充** tag（chat 那条路）—— 返回新记录（不改原对象）。"""
    out = dict(track)
    out["tags"] = _merge_tags(track.get("tags"), tags)
    return out


def remove_tags(track: Mapping[str, Any], tags: Mapping[str, Any]) -> Dict[str, Any]:
    """删掉某些 tag 值（chat 说错了要能改回来）。"""
    out = dict(track)
    current = _clean_tags(track.get("tags"))
    for axis, values in _clean_tags(tags).items():
        drop = {str(v) for v in values}
        kept = [v for v in current.get(axis, []) if v not in drop]
        if kept:
            current[axis] = kept
        else:
            current.pop(axis, None)
    out["tags"] = current
    return out


def bump_play(track: Mapping[str, Any], when: Optional[str] = None) -> Dict[str, Any]:
    """播放次数 +1（**由 `agent/core/music.py` 在"真的听了 30 s"之后调**）。"""
    out = dict(track)
    out["plays"] = int(track.get("plays") or 0) + 1
    out["last_played"] = when or datetime.now().isoformat(timespec="seconds")
    return out


# ---------------------------------------------------------------------------
#  查询（"下一首由 chat 决定"就靠这几个）
# ---------------------------------------------------------------------------
def pick(tracks: Sequence[Mapping[str, Any]], tag: Optional[Any] = None,
         sort: str = "plays_asc", limit: int = 10,
         axis: Optional[str] = None, seed: Optional[int] = None) -> List[Dict[str, Any]]:
    """按 tag 过滤 + 按次数排序，给 chat 一份候选清单。

    @param tag  只要带这个标签的歌（`"energetic"`）；**也可以给一串**（`["energetic","calm"]`
                = 任一命中 —— T8-5b 起统一语法允许 `mood=energetic/calm`）；None = 全部
    @param axis 限定在某个轴上找（`"mood"`）；None = 所有轴
    @param sort `plays_asc`（听得最少的先, 默认）/ `plays_desc` / `recent` /
                `oldest` / `added` / `random`
    @return 记录列表（带上 `matched` 说明命中的是哪个轴/标签）
    """
    wanted = [str(x).strip() for x in (
        tag if isinstance(tag, (list, tuple, set)) else [tag]) if str(x or "").strip()]
    hits: List[Dict[str, Any]] = []
    for track in tracks:
        tags = _clean_tags(track.get("tags"))
        if wanted:
            matched = [(name, value) for name, values in tags.items()
                       if (axis is None or name == axis)
                       for value in wanted if value in values]
            if not matched:
                continue
            item = dict(track)
            item["matched"] = ["%s=%s" % pair for pair in matched]
            hits.append(item)
        else:
            hits.append(dict(track))

    if sort == "plays_desc":
        hits.sort(key=lambda t: (-int(t.get("plays") or 0), str(t.get("id"))))
    elif sort == "recent":
        hits.sort(key=lambda t: (str(t.get("last_played") or ""), int(t.get("plays") or 0)),
                  reverse=True)
    elif sort == "oldest":
        hits.sort(key=lambda t: (str(t.get("last_played") or "9"), int(t.get("plays") or 0)))
    elif sort == "added":
        hits.sort(key=lambda t: str(t.get("added_at") or ""), reverse=True)
    elif sort == "random":
        rng = random.Random(seed)
        rng.shuffle(hits)
    else:                                            # 默认: 听得最少的先
        hits.sort(key=lambda t: (int(t.get("plays") or 0), str(t.get("id"))))
    return hits[:max(1, int(limit))] if limit else hits


def tag_counts(tracks: Sequence[Mapping[str, Any]],
               axis: Optional[str] = None) -> Dict[str, List[Dict[str, Any]]]:
    """每个轴上各标签有几首（给 chat 看"库里有什么"）。"""
    out: Dict[str, Dict[str, int]] = {}
    for track in tracks:
        for name, values in _clean_tags(track.get("tags")).items():
            if axis and name != axis:
                continue
            bucket = out.setdefault(name, {})
            for value in values:
                bucket[value] = bucket.get(value, 0) + 1
    return {name: [{"tag": value, "count": count}
                   for value, count in sorted(bucket.items(), key=lambda kv: (-kv[1], kv[0]))]
            for name, bucket in out.items()}


def summarise(tracks: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """库的摘要（工具回话用）。"""
    total_plays = sum(int(t.get("plays") or 0) for t in tracks)
    never = [t for t in tracks if not int(t.get("plays") or 0)]
    return {
        "count": len(tracks),
        "total_plays": total_plays,
        "never_played": len(never),
        "tags": tag_counts(tracks),
    }


# ---------------------------------------------------------------------------
#  导入时的自动打标（**只靠元数据**, 不含听感）
# ---------------------------------------------------------------------------
def guess_lang(*texts: str) -> Optional[str]:
    """从歌名/专辑名猜语种（**猜的**, 只为给 chat 一点线索）。

    @return "jp" / "zh" / None（看不出就不写, 不硬猜）
    @note 有假名 → jp；纯汉字 → zh；都没有 → None（英文歌与符号名分不清, 不猜）
    """
    joined = " ".join(str(t or "") for t in texts)
    if _KANA_RE.search(joined):
        return "jp"
    if _HAN_RE.search(joined):
        return "zh"
    return None


def auto_tags(name: str = "", artists: str = "", album: str = "",
              genre: Iterable[str] = (), year: Optional[int] = None) -> Dict[str, List[str]]:
    """导入时自动打的 tag（元数据那一路）。

    @param genre 云歌单自己的标签（"日语"/"流行"…）
    @param year  专辑发行年（拿得到才给, 用来做 `era`）
    """
    tags: Dict[str, List[str]] = {}
    if genre:
        tags["genre"] = [str(g) for g in genre if str(g).strip()]
    if artists:
        tags["artist"] = [part.strip() for part in re.split(r"[/,、]", str(artists)) if part.strip()]
    lang = guess_lang(name, album)
    if lang:
        tags["lang"] = [lang]
    if year:
        tags["era"] = ["%ds" % (int(year) // 10 * 10)]
    return tags
