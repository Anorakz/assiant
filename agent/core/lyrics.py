# ============================================================================
#  agent/core/lyrics.py — LRC 解析（T15-16-1）
#
#  数据从哪来
#  ---------------------------------------------------------------------------
#  板端 ssh 到 PC 跑 `neteasecli --json track lyric <id>`，回来是
#      {"id":"186016","lrc":"[00:28.950]故事的小黄花\n…","tlyric":"…",
#       "hasLyric":true,"hasTranslation":true}
#  本模块只做**一件事**：把这两段 LRC 文本解析成"时间轴 + 每行的原文/译文"，
#  好让 `music` 推送带着它下去、GUI 侧只按时间取行（不再解析文本）。
#
#  三条刻意的决定（对应 T15-16 方案，2026-10-04 用户拍板）
#  ---------------------------------------------------------------------------
#   · **原文与译文都留着**（`text` / `tr`）。"优先中文/意译、没有翻译时显示原文"
#     这条口径由**显示侧**执行（C++ 一行：`tr 非空取 tr，否则取 text`）——
#     这样将来想加"看原文"的开关不用再回 PC 拉一次。
#   · **只有时间戳的行不产出行**（间奏：`[02:21.989]`）。"上一行继续显示"由取行
#     逻辑天然得到，不需要往时间轴里插哨兵行。
#   · **offset 的符号只定义一次**：`正值 = 歌词提前`（边界时间**减掉**它）。
#     本模块自己的 `[offset:±ms]` 标签与配置键 `music.lyric_offset_ms` 同号。
#
#  真数据里踩到的形状（2026-10-04 实测，夹具见 tests/data/lyrics/）
#  ---------------------------------------------------------------------------
#   · `[mm:ss.xx]` 与 `[mm:ss.xxx]` 都有（毫秒 2 位或 3 位）；也吃 `[m:ss]`
#   · 一行**多个时间戳**（`[00:01.00][00:05.00]同一句`）—— 公益 LRC 常见
#   · 开头 10 秒是**元信息行**（`[00:00.000] 作词 : 周杰伦`）：按用户决定
#     **保留**（它就是这首歌前 10 秒屏幕上该出现的东西）
#   · `tlyric` 与原文**同一批时间戳**；没有译文的那几行是空文本
#   · 坏行（没时间戳 / 半截标签 / 乱码）跳过并计数，**绝不抛异常**
# ============================================================================
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["parse_lrc", "REASON_NO_LRC", "REASON_NO_TIMELINE"]

#: `[mm:ss]` / `[mm:ss.xx]` / `[mm:ss.xxx]` / `[mm:ss:xx]`（毫秒分隔两种都吃）
_TIME_RE = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
#: 元信息标签：`[ti:…]` `[ar:…]` `[offset:+500]`。
#: ⚠ 冒号前必须是**字母**（`[00:28.950]` 不能被当成标签 —— 这是最容易踩的一脚）
_TAG_RE = re.compile(r"\[([A-Za-z#]+):([^\]]*)\]")

#: 没有歌词（netease 对纯音乐给的就是空 `lrc`）
REASON_NO_LRC = "没有歌词"
#: 有文本但解析不出任何带时间戳的行
REASON_NO_TIMELINE = "歌词里没有可用的时间戳"


def parse_lrc(lrc: Optional[str], tlyric: Optional[str] = None,
              offset_ms: Any = 0) -> Dict[str, Any]:
    """把两段 LRC 文本解析成"时间轴 + 原文/译文"。

    @param lrc 原文（neteasecli 的 `data["lrc"]`）；空/None = 没有歌词
    @param tlyric 译文（`data["tlyric"]`，与原文同一批时间戳）；可不给
    @param offset_ms 全局对轴补偿（配置键 `music.lyric_offset_ms`）。
                     **正值 = 歌词提前**，与 LRC 自己的 `[offset:]` 标签同号
    @return `{"lines": [{"t": float, "text": str, "tr": str}, …],
              "has_lyric": bool, "has_tr": bool, "skipped": int, "reason": str}`
            · `lines` 按 `t` 升序，`t` 已夹到 >= 0.0 且保留 3 位小数
            · `reason` 只在 `has_lyric` 为假时非空（给人看的一句话）
    @note 任何输入都不抛：看不懂的行跳过并计入 `skipped`。
    """
    entries, lrc_offset, skipped = _parse_lrc_side(lrc)
    translations, _, tr_skipped = _parse_lrc_side(tlyric)
    skipped += tr_skipped

    shift_ms = _tag_int(offset_ms) or 0
    shift_ms += lrc_offset or 0

    # 译文按**原始**时间戳建索引（两边的 t 在移位之前是同一批）
    by_ms = {}                                     # type: Dict[int, str]
    for stamp, text in translations:
        by_ms.setdefault(_stamp_ms(stamp), text)

    lines = []                                     # type: List[Dict[str, Any]]
    seen = set()                                   # type: set
    for stamp, text in entries:
        pair = (stamp, text)
        if pair in seen:                           # 多时间戳抄重了的情况
            continue
        seen.add(pair)
        lines.append({
            "t": _moved_seconds(stamp, shift_ms),
            "text": text,
            "tr": by_ms.get(_stamp_ms(stamp), ""),
        })
    lines.sort(key=lambda row: row["t"])

    has_lyric = bool(lines)
    return {
        "lines": lines,
        "has_lyric": has_lyric,
        "has_tr": any(row["tr"] for row in lines),
        "skipped": skipped,
        "reason": "" if has_lyric else _no_lines_reason(lrc),
    }


def _parse_lrc_side(text: Optional[str]) -> Tuple[List[Tuple[float, str]], Optional[int], int]:
    """解析一份 LRC 文本。

    @return `([(原始时间戳, 文本), …], offset 标签的毫秒数或 None, 跳过的行数)`
    @note 返回的是**未移位**的时间戳 —— 译文索引要按原始值对齐。
    """
    entries = []                                   # type: List[Tuple[float, str]]
    offset = None                                  # type: Optional[int]
    skipped = 0
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if _TAG_RE.search(line):
            for name, value in _TAG_RE.findall(line):
                if name.lower() == "offset":
                    offset = _tag_int(value)
            line = _TAG_RE.sub("", line).strip()
            if not line:                           # 纯标签行（`[ti:…]`）不是歌词
                continue
        stamps = _TIME_RE.findall(line)
        if not stamps:
            skipped += 1
            continue
        body = _TIME_RE.sub("", line).strip()
        if not body:                               # 间奏：只有时间戳 -> 不产出行
            continue
        for minute, second, frac in stamps:
            entries.append((_stamp_seconds(minute, second, frac), body))
    return entries, offset, skipped


def _stamp_seconds(minute: str, second: str, frac: Optional[str]) -> float:
    """`(分, 秒, 毫秒串)` -> 秒（`[01:00.189]` -> 60.189）。"""
    value = int(minute) * 60 + int(second)
    if frac:
        value += int(frac) / (10 ** len(frac))
    return round(value, 3)


def _stamp_ms(seconds: float) -> int:
    """秒 -> 毫秒（译文对齐用；两边都从同一批文本解析出来，不会差）。"""
    return int(round(seconds * 1000))


def _moved_seconds(seconds: float, shift_ms: int) -> float:
    """把边界时间移位：**正值 = 提前**（减掉），并夹到 >= 0。"""
    return round(max(0.0, seconds - shift_ms / 1000.0), 3)


def _tag_int(value: Any) -> Optional[int]:
    """`"+500"` / `500` / `-500` -> 500 / 500 / -500；看不懂给 None。"""
    text = str(value if value is not None else "").strip().lstrip("+")
    try:
        return int(float(text))
    except ValueError:
        return None


def _no_lines_reason(lrc: Optional[str]) -> str:
    """一行都没解析出来时的说明。"""
    return REASON_NO_LRC if not (lrc or "").strip() else REASON_NO_TIMELINE
