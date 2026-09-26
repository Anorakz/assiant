# ============================================================================
#  agent/core/schedule_config.py — 日程的**文本级**增删（R3 的删除 + T12-5 的新增）
#
#  为什么是**文本级**手术（而不是 yaml.dump / save_config）
#  ---------------------------------------------------------------------------
#    · `config/config.yaml` 的注释就是各字段的事实约定来源（`config.example.yaml`
#      里写明了这件事），GUI 也用**文本级替换**写它的 `gui:`/`llm:` 段 ——
#      整体重排 + 丢注释是不可接受的。
#    · 所以这里只做两件事：在 `recurring:` / `oneoff:` 序列里找出**那一条**的行区间
#      删掉，或者在序列尾部**添**一条渲染好的条目 —— **其余字节原样保留**（测试逐字节比对）。
#
#  匹配靠注入的 predicate（不是在这里解析日程语义）
#  ---------------------------------------------------------------------------
#    `matches(fields)` 收到的是从**文件**里抽出来的裸标量
#    `{"state": ..., "start": ..., "days": ..., "date": ...}`（已去引号、去行尾注释），
#    自己判断"是不是我要删的那条"。这样"什么算同一条"（state + start + date/days 怎么归一化）
#    留在懂日程语义的那一层（Scheduler 用 parse_clock / date.fromisoformat），本模块只懂文本
#    —— 也避免了 `scheduler.py <-> schedule_config.py` 的循环依赖。
#
#  渲染这一半的边界（T12-5）
#  ---------------------------------------------------------------------------
#    · 本模块**不校验日程语义**（state 认不认得、这条合不合法）：那是 `ScheduleEvent` 的事。
#      这里只保证"渲染出来是一段语法正确的 YAML，而且缩进/引号风格与模板一致"。
#    · `start` 一律加引号（`"09:30"`）、`date` 一律加引号（`"2026-09-22"`）：
#      不加引号的 `9:30` 在 YAML 1.1 里是**六十进制整数**、`2026-09-22` 是 **date 类型**，
#      加了引号两边（PyYAML / yaml-cpp）看到的都一定是字符串。
#    · `days` 空列表 = 不写这个键（读的一侧"缺 days 就是每天"，见 ScheduleEvent.from_config）
#
#  明确**不做**的事（都给出理由、原样返回不改文件）
#  ---------------------------------------------------------------------------
#    · flow 风格（`oneoff: [{state: ..., date: ...}]`）：一行里塞多个条目，文本级改它
#      风险太大 —— 报个理由让人去手改
#    · 找不到匹配 -> 也算"没删"（可能已经删过了，或者配置被人改过），不猜
#    · **紧贴在被删条目前面**的注释与空行**留着**（只删属于那条的行）：宁可留一行悬空
#      注释，也不删掉可能是在描述下一条的注释 —— 注释在这个仓库里是信息，不是装饰
#    · 新增**只往序列尾部加**（不插到中间、不排序）：文件里的顺序是人写的，改它得由人。
# ============================================================================

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from agent.config import write_text_atomic

__all__ = [
    "ScheduleConfigError",
    "BACKUP_SUFFIX",
    "ENTRY_KEYS",
    "strip_scalar",
    "entry_key_of",
    "render_entry_lines",
    "find_entry",
    "find_oneoff_block",
    "add_entry",
    "remove_entry",
    "remove_oneoff_entry",
    "add_entry_in_file",
    "remove_entry_in_file",
    "remove_oneoff_from_file",
]

#: 日程所在的两个序列。顺序就是查找/报告顺序（recurring 在前）。
ENTRY_KEYS = ("recurring", "oneoff")

#: 备份后缀：与 GUI 的 ConfigStore 同一约定（`config/config.yaml.bak`；已被 .gitignore 覆盖）
BACKUP_SUFFIX = ".bak"

#: `key:` 所在的行（用来找 `oneoff:` 序列）
_KEY_RE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:(.*)$")
#: 条目块里的 `state:` / `date:` / `start:` / `days:`（含 `- state:` 这种首行写法）
_FIELD_RE = re.compile(r"^\s*(?:-\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*:(.*)$")
#: 一条序列项的开头：`  - state: ...`
_ITEM_RE = re.compile(r"^(\s*)-\s*(.*)$")
#: 渲染时允许直接落在文本里的标量（state / days 的项）：只认最朴素的形状，
#: 免得"用户给的值"变成配置里的结构（注入 YAML 的另一半 —— 那一半是语义校验的事）
_PLAIN_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
#: 渲染 date 时的形状（语义上合不合法由 `date.fromisoformat` 判，这里只管形状）
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ScheduleConfigError(ValueError):
    """文本级改配置时不认识的形状（继承 ValueError，与其它模块一致）。"""


# ---------------------------------------------------------------------------
#  文本工具
# ---------------------------------------------------------------------------
def strip_scalar(raw: str) -> str:
    """把 `"09:30"` / `'09:30'` / `09:30  # 注释` 归一成裸文本。

    @note 只处理最常见的三件事：去引号、去行尾注释、去首尾空白。更花的写法（锚点、
          显式 tag）不在保证范围内 —— 那种条目匹配不上就不会被删，方向是安全的。
    """
    text = raw.strip()
    quote: Optional[str] = None
    for index, ch in enumerate(text):
        if quote is not None:
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            continue
        if ch == "#" and (index == 0 or text[index - 1].isspace()):
            text = text[:index]
            break
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1].strip()
    return text


def _split_lines(text: str) -> List[str]:
    """按行切开并**保留行尾**（CRLF 要原样留下来，写完还是 CRLF）。"""
    return text.splitlines(keepends=True)


def _read_text(path: Path) -> str:
    """读配置文件：**逐字节**读进来，不做换行翻译。

    ⚠ `Path.read_text()` 走的是文本模式（`newline=None`），在 POSIX 上会把 CRLF 翻成 LF ——
      于是"读出来再写回去"就把整个文件的换行悄悄改成 LF 了（T12-5 板端实测抓到）。
      这个模块的全部承诺就是"除了目标那几行，其余**字节**不变"，所以两头都不能做翻译。
    """
    with open(str(path), "r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _ending_of(line: str) -> str:
    """这一行用的是哪种行尾（新加的行跟着它走）。"""
    return "\r\n" if line.endswith("\r\n") else "\n"


def _dominant_ending(text: str) -> str:
    """整份文本的行尾风格（新加"一整段"时用；一行都没有就 LF）。"""
    match = re.search(r"\r\n|\n", text)
    return match.group(0) if match else "\n"


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_blank(line: str) -> bool:
    return line.strip() == ""


def _is_comment(line: str) -> bool:
    return line.strip().startswith("#")


def _key_on(line: str, key: str) -> Optional[re.Match]:
    """这一行是不是 `key:`（返回 match，取 group(1)=缩进 group(3)=值）。"""
    if _is_comment(line):
        return None
    match = _KEY_RE.match(line)
    if match and match.group(2) == key:
        return match
    return None


# ---------------------------------------------------------------------------
#  找条目
# ---------------------------------------------------------------------------
def _sequence_items(lines: List[str], key_index: int, key_indent: int) -> List[Tuple[int, int]]:
    """`key:` 之后，属于这个序列的每个条目的行区间 `[start, end)`。

    @note ⚠ `end` 是**缩进回退那一行**（不是文件末尾）：T12-5 之前这里是 `len(lines)`，
          于是"序列后面还有别的段"时（板端真配置的 `oneoff:` 后面就是 `gui:`）区间会一路
          吃到文件尾 —— 删一条 oneoff 会把后面的段一起删掉。R3 默认关着，所以一直没露头；
          T12-5 的"加一条"直接踩在上面（新条目被加到了文件末尾），才把它翻出来。
    """
    items: List[Tuple[int, int]] = []
    current: Optional[int] = None
    stop = len(lines)

    for index in range(key_index + 1, len(lines)):
        line = lines[index]
        if _is_blank(line):
            continue                    # 空行不切条目（收尾按下一条目/缩进回退算）
        if _indent_of(line) <= key_indent:
            stop = index                 # 缩进回退 = 序列结束（注释也算：那是别的段的）
            break
        if _ITEM_RE.match(line) and not _is_comment(line):
            if current is not None:
                items.append((current, index))
            current = index
    if current is not None:
        items.append((current, stop))

    # 尾部的空行/纯注释行不算条目内容 —— 留着它们，免得删掉本该属于下一条的注释
    trimmed: List[Tuple[int, int]] = []
    for start, end in items:
        while end - 1 > start and (_is_blank(lines[end - 1]) or _is_comment(lines[end - 1])):
            end -= 1
        trimmed.append((start, end))
    return trimmed


def _fields_of(lines: List[str], start: int, end: int) -> Dict[str, str]:
    """条目块里抽 `state` / `start` / `days` / `date`（每个键取第一次出现）。

    @note T12-4: 日程 = **时间 + 状态**，所以匹配的键从 `title`/`date`/`start` 换成了
          `state`/`start`（+ `date`）；T12-5 又加上 `days` —— 要删一条**每周**的日程时
          得能分清"同样 09:00 但星期不同的两条"。这里仍旧只抽**裸标量** —— 语义归一
          （大小写、时间写法、星期写法）在 `scheduler.entry_matcher` 里做。
    """
    fields: Dict[str, str] = {}
    for index in range(start, end):
        line = lines[index]
        if _is_comment(line):
            continue
        match = _FIELD_RE.match(line)
        if not match:
            continue
        key = match.group(1)
        if key in ("state", "start", "days", "date") and key not in fields:
            fields[key] = strip_scalar(match.group(2))
    return fields


def _iter_blocks(text: str, keys: Sequence[str] = ENTRY_KEYS
                 ) -> Iterator[Tuple[str, int, int, int, Dict[str, str]]]:
    """遍历 `keys` 里每个序列的条目（按**文件里的先后**）。

    @return (序列名, 键所在行号, 条目起点, 条目终点, 字段)
    @raise ScheduleConfigError 某个键用了 flow 风格（不碰那种写法）
    """
    lines = _split_lines(text)
    for index, line in enumerate(lines):
        if _is_comment(line):
            continue
        match = _KEY_RE.match(line)
        if match is None:
            continue
        key = match.group(2)
        if key not in keys:
            continue
        key_indent = len(match.group(1))
        value = strip_scalar(match.group(3))
        if value:
            if value in ("[]", "{}"):
                continue                # 空序列：没东西可删
            raise ScheduleConfigError(
                "%s 用了 flow 风格（%s），文本级改它风险太大 —— 请手改" % (key, value[:60])
            )
        for start, end in _sequence_items(lines, index, key_indent):
            yield key, index, start, end, _fields_of(lines, start, end)


def _iter_oneoff_blocks(text: str) -> Iterator[Tuple[int, int, int, Dict[str, str]]]:
    """只遍历 `oneoff:` 的条目（R3 的删除只动 oneoff，语义在调用方）。"""
    for _key, key_index, start, end, fields in _iter_blocks(text, ("oneoff",)):
        yield key_index, start, end, fields


def find_entry(text: str, matches: Callable[[Dict[str, str]], bool],
               keys: Sequence[str] = ENTRY_KEYS
               ) -> Tuple[Optional[Dict[str, Any]], str]:
    """找第一条让 `matches(fields)` 说"是它"的日程（默认 `recurring` 与 `oneoff` 都找）。

    @param keys 只在哪些序列里找（`add_entry` 的查重只查**目标那一个**序列——
                别人写的另一段是 flow 风格不该拦住这次添加）
    @return (条目, 原因)。找到时条目是 `{"key", "key_index", "fields", "span"}`、
            原因是空串；没找到时条目是 None、原因说明为什么（写法不支持 / 没有匹配）。
    """
    try:
        for key, key_index, start, end, fields in _iter_blocks(text, keys):
            if matches(fields):
                return {"key": key, "key_index": key_index, "fields": fields,
                        "span": (start, end)}, ""
    except ScheduleConfigError as exc:
        return None, str(exc)
    return None, "配置里没有匹配的那条（可能已经删过了，或者被人改过）"


def find_oneoff_block(text: str, matches: Callable[[Dict[str, str]], bool]
                      ) -> Tuple[Optional[Tuple[int, int]], str]:
    """找第一条让 `matches(fields)` 说"是它"的 oneoff 条目。

    @return (区间, 原因)。找到时原因是空串。
    """
    try:
        for _key_index, start, end, fields in _iter_oneoff_blocks(text):
            if matches(fields):
                return (start, end), ""
    except ScheduleConfigError as exc:
        return None, str(exc)
    return None, "配置里没有匹配的那条（可能已经删过了，或者被人改过）"


# ---------------------------------------------------------------------------
#  删条目
# ---------------------------------------------------------------------------
def _remove_span(text: str, key: str, key_index: int, span: Tuple[int, int]) -> str:
    """删掉区间，其余**逐字节**保留；序列空了就把 `key:` 补成 `[]`。"""
    lines = _split_lines(text)
    start, end = span
    remaining = lines[:start] + lines[end:]

    match = _key_on(remaining[key_index], key) if key_index < len(remaining) else None
    if match is None:
        return "".join(remaining)       # 键那行也被删了？（不该发生）原样返回
    key_indent = len(match.group(1))
    if _sequence_items(remaining, key_index, key_indent):
        return "".join(remaining)       # 还有别的条目：一个字节都不动

    ending = _ending_of(remaining[key_index])
    remaining[key_index] = "%s%s: []%s" % (match.group(1), key, ending)
    return "".join(remaining)


def remove_entry(text: str, matches: Callable[[Dict[str, str]], bool]
                 ) -> Tuple[str, str, Optional[Dict[str, Any]]]:
    """从**文本**里删掉匹配的那条日程（`recurring` 与 `oneoff` 都找）。

    @return (新文本, 原因, 找到的那条)。原因是空串 = 真的删了；否则文本原样返回、
            原因说明为什么。找到的那条与 `find_entry` 同形（没找到就是 None）——
            调用方要靠它说清"删掉的是哪个序列里的哪一条"。
    """
    found, why = find_entry(text, matches)
    if found is None:
        return text, why, None
    return _remove_span(text, found["key"], found["key_index"], found["span"]), "", found


def remove_oneoff_entry(text: str, matches: Callable[[Dict[str, str]], bool]
                        ) -> Tuple[str, str]:
    """从**文本**里删掉匹配的那条 oneoff（R3 用：只动 oneoff，recurring 一条都不动）。

    @return (新文本, 原因)。原因是空串 = 真的删了；否则文本原样返回、原因说明为什么。
    """
    try:
        for key_index, start, end, fields in _iter_oneoff_blocks(text):
            if matches(fields):
                return _remove_span(text, "oneoff", key_index, (start, end)), ""
    except ScheduleConfigError as exc:
        return text, str(exc)
    return text, "配置里没有匹配的那条（可能已经删过了，或者被人改过）"


# ---------------------------------------------------------------------------
#  加条目
# ---------------------------------------------------------------------------
def entry_key_of(values: Dict[str, Any]) -> str:
    """这条日程该写进哪个序列：给了 `date` 就是 `oneoff`，否则 `recurring`。

    ⚠ 与 `ScheduleEvent.from_config` 同一条判据（"有 date 就是 oneoff"）——
      两边不一致的话，写进去的条目会被读成另一种，等于写坏配置。
    """
    return "oneoff" if values.get("date") else "recurring"


def _render_clock(raw: Any) -> str:
    """把 `start` 归一成 `HH:MM`（只管形状，不管它是不是"能读懂的时间"）。

    @raise ScheduleConfigError 形状不对 / 时或分越界
    """
    text = str(raw if raw is not None else "").strip().replace("：", ":")
    if ":" in text:
        parts = text.split(":")
        if len(parts) != 2:
            raise ScheduleConfigError("start 要写成 HH:MM（得到 %r）" % (raw,))
        hh, mm = parts[0].strip(), parts[1].strip()
    elif len(text) == 4 and text.isdigit():
        hh, mm = text[:2], text[2:]
    else:
        raise ScheduleConfigError("start 要写成 HH:MM（得到 %r）" % (raw,))
    if not (hh.isdigit() and mm.isdigit()):
        raise ScheduleConfigError("start 要写成 HH:MM（得到 %r）" % (raw,))
    hour, minute = int(hh), int(mm)
    if not (0 <= hour <= 23) or not (0 <= minute <= 59):
        raise ScheduleConfigError("start 超出 0..23:0..59（得到 %r）" % (raw,))
    return "%02d:%02d" % (hour, minute)


def _plain_token(raw: Any, what: str) -> str:
    """渲染进文本的裸标量：只放行最朴素的形状（不给注入 YAML 的机会）。"""
    text = str(raw if raw is not None else "").strip()
    if not text or not _PLAIN_TOKEN_RE.match(text):
        raise ScheduleConfigError("%s 只能是普通标识符（得到 %r）" % (what, raw))
    return text


def render_entry_lines(values: Dict[str, Any], item_indent: int) -> List[str]:
    """把一条日程渲染成**不带行尾**的若干行（第一条含 `- `）。

    @param values `{"state": str, "start": str, "days": [..] | "date": str}`
                  （顺序无所谓；`date` 与 `days` 不能同时给）
    @param item_indent 条目缩进（`- ` 那个减号所在的列）
    @raise ScheduleConfigError 形状不对（缺 state/start、date 与 days 同时给…）
    @note 不校验**语义**（state 认不认得、日期存不存在）—— 那是 `ScheduleEvent` 的事。
    """
    state = _plain_token(values.get("state"), "state")
    start = _render_clock(values.get("start"))
    days = values.get("days")
    on = values.get("date")
    if days and on:
        raise ScheduleConfigError("date 与 days 不能同时给（这条是 %r / %r）" % (on, days))

    pad = " " * item_indent
    field_pad = " " * (item_indent + 2)
    lines = ["%s- state: %s" % (pad, state)]
    if on:
        text = str(on).strip()
        if not _DATE_RE.match(text):
            raise ScheduleConfigError("date 要写成 YYYY-MM-DD（得到 %r）" % (on,))
        lines.append('%sdate: "%s"' % (field_pad, text))
    elif days:
        tokens = [_plain_token(item, "days 里的项") for item in days]
        if tokens:
            lines.append("%sdays: [%s]" % (field_pad, ", ".join(tokens)))
    lines.append('%sstart: "%s"' % (field_pad, start))
    return lines


def _find_section(lines: List[str]) -> Optional[Tuple[int, int]]:
    """找 `scheduler:` 那一行（值必须是空的，`scheduler: {}` 这种不给改）。"""
    for index, line in enumerate(lines):
        if _is_comment(line) or _is_blank(line):
            continue
        match = _KEY_RE.match(line)
        if match and match.group(2) == "scheduler":
            return index, len(match.group(1))
    return None


def _find_key_line(lines: List[str], key: str) -> Optional[int]:
    """找 `key:` 那一行（先看 `scheduler:` 段里，再退回**顶层**）—— 与读的一侧同一条规矩。

    @note 顶层那一遍只在**缩进 0** 上找：`agent.config` 读的是顶层键，别处缩进的同名键
          根本不是日程所在的地方 —— 写进去等于"报告成功但什么都没发生"。
    """
    section = _find_section(lines)
    if section is not None:
        index, section_indent = section
        for candidate in range(index + 1, len(lines)):
            line = lines[candidate]
            if _is_comment(line) or _is_blank(line):
                continue
            if _indent_of(line) <= section_indent:
                break
            match = _KEY_RE.match(line)
            if match and match.group(2) == key:
                return candidate
    for candidate, line in enumerate(lines):
        if _is_comment(line) or _indent_of(line) != 0:
            continue
        match = _KEY_RE.match(line)
        if match and match.group(2) == key:
            return candidate
    return None


def _section_end(lines: List[str], section_index: int, section_indent: int) -> int:
    """`scheduler:` 段的收尾行号（段里最后一个非空非注释行的下一行）。"""
    end = section_index + 1
    for index in range(section_index + 1, len(lines)):
        line = lines[index]
        if _is_blank(line) or _is_comment(line):
            continue
        if _indent_of(line) <= section_indent:
            break
        end = index + 1
    return end


def add_entry(text: str, values: Dict[str, Any],
              matches: Optional[Callable[[Dict[str, str]], bool]] = None
              ) -> Tuple[str, str]:
    """往**文本**里加一条日程（`recurring` / `oneoff` 由有没有 `date` 决定）。

    @param values  `{"state", "start", "days"|"date"}`（见 `render_entry_lines`）
    @param matches 可选：判断"已经有一条一样的了吗"的 predicate（`scheduler.entry_matcher`）。
                   给了就先查重 —— 命中就**一个字节都不改**，原因写「已经有一条一样的了」。
    @return (新文本, 原因)。原因是空串 = 真的加了；否则文本原样返回、原因说明为什么。

    @note 插入位置：序列**尾部**（文件里的顺序是人写的，这里不排序）；
          键不存在就在现有 `scheduler:` 段尾补一个；连 `scheduler:` 都没有才在文件末尾补一整段。
    @note 写进去的缩进跟着**文件里已有的条目**走；`key: []` 会被重写成块状写法。
    @note ⚠ **绝不新建第二个 `scheduler:` 段**：YAML 里同名键后者胜，那会把
          `interval_min` / `commands` / `remove_fired_oneoff` 整段悄悄丢掉。
    @raise ScheduleConfigError `values` 渲染不出来（缺 state/start、date 与 days 同时给…）
           —— 那是**调用方**给错了，不是文件的问题，所以直接抛，不混进"原因"里。
    """
    key = entry_key_of(values)
    # 先把"渲染得出来吗"问清楚：渲染失败不该以"文件里正好没有这个键"的形式表现出来
    render_entry_lines(values, 0)                # 缩进这里无所谓，只为校验形状

    if matches is not None:
        existing, why = find_entry(text, matches, keys=(key,))
        if existing is not None:
            return text, "已经有一条一样的了（%s 里那条 %s）" % (
                existing["key"], existing["fields"].get("start"))
        if why and "flow 风格" in why:
            return text, why

    lines = _split_lines(text)
    key_index = _find_key_line(lines, key)

    if key_index is None:
        return _add_missing_key(text, key, values)

    key_match = _KEY_RE.match(lines[key_index])
    assert key_match is not None
    key_indent = len(key_match.group(1))
    value = strip_scalar(key_match.group(3))
    if value and value not in ("[]", "{}"):
        return text, ("%s 用了 flow 风格（%s），文本级加条目得先把它改成块状写法 —— 请手改"
                      % (key, value[:60]))

    existing_items = _sequence_items(lines, key_index, key_indent)
    if existing_items:
        # 已有条目：插在**最后一条之后**（一行都不替换）
        item_indent = _indent_of(lines[existing_items[0][0]])
        at = existing_items[-1][1]
        head: List[str] = []
        replaced = 0
    else:
        # 空序列：把 `key: []`（或 `key:`）那一行换成 `key:`，条目跟在后面
        item_indent = key_indent + 2
        at = key_index
        head = ["%s%s:%s" % (key_match.group(1), key, _ending_of(lines[key_index]))]
        replaced = 1

    ending = _ending_of(lines[key_index])
    body = [line + ending for line in render_entry_lines(values, item_indent)]
    return "".join(lines[:at] + head + body + lines[at + replaced:]), ""


def _add_missing_key(text: str, key: str, values: Dict[str, Any]) -> Tuple[str, str]:
    """文件里没有这个键：塞进现有 `scheduler:` 段的尾部；连段都没有才补一整段。

    @return (新文本, 原因)；原因非空 = 文件里那段的写法不支持（flow 风格），原样返回。
    """
    ending = _dominant_ending(text)
    lines = _split_lines(text)
    section = _find_section(lines)

    if section is None:
        # 连 `scheduler:` 都没有：末尾补一整段（缩进照模板：段 0 / 键 2 / 条目 4）
        out = list(lines)
        if out and not _is_blank(out[-1]):
            out.append(ending)          # 段与段之间空一行（模板就是这个风格）
        if out and not out[-1].endswith(("\n", "\r")):
            out.append(ending)          # 原文件末尾没有换行：先补上
        out.append("scheduler:" + ending)
        out.append("  %s:" % key + ending)
        out.extend(line + ending for line in render_entry_lines(values, 4))
        return "".join(out), ""

    index, section_indent = section
    section_match = _KEY_RE.match(lines[index])
    assert section_match is not None
    if strip_scalar(section_match.group(3)):
        return text, "scheduler 用了 flow 风格，文本级加条目得先把它改成块状写法 —— 请手改"

    key_indent = section_indent + 2
    at = _section_end(lines, index, section_indent)
    head = [" " * key_indent + key + ":" + ending]
    body = [line + ending for line in render_entry_lines(values, key_indent + 2)]
    return "".join(lines[:at] + head + body + lines[at:]), ""


# ---------------------------------------------------------------------------
#  落盘（留 `.bak` + 原子写）
# ---------------------------------------------------------------------------
def _read_write(config_file: Any, transform: Callable[[str], Tuple[str, str]]
                ) -> Tuple[bool, str]:
    """读文件 -> `transform` 出 (新文本, 原因) -> 原因空才写（`.bak` + 原子替换）。

    @return (写了吗, 原因)
    @raise OSError 读/写盘失败 —— 原样抛出，调用方（工具 / CLI / Scheduler）自己决定怎么报
    """
    path = Path(config_file)
    original = _read_text(path)
    new_text, why = transform(original)
    if why:
        return False, why
    write_text_atomic(str(path) + BACKUP_SUFFIX, original)   # 覆盖上一份 .bak
    write_text_atomic(path, new_text)
    return True, ""


def add_entry_in_file(config_file: Any, values: Dict[str, Any],
                      matches: Optional[Callable[[Dict[str, str]], bool]] = None
                      ) -> Tuple[bool, str]:
    """把一条日程加到**文件**里（留 `.bak` + 原子写）。

    @return (写了吗, 原因)。False 时原因说清是"已经有一条一样的"还是"写法不支持"。
    @note 与 `remove_oneoff_from_file` 同一套落盘纪律：写前重新读一遍，在这份刚读到的
          文本上做手术 —— 所以"配置在半路被人或 GUI 改过"不会被覆盖掉。
    """
    return _read_write(config_file, lambda text: add_entry(text, values, matches))


def remove_entry_in_file(config_file: Any, matches: Callable[[Dict[str, str]], bool]
                         ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """从**文件**里删掉匹配的那条日程（留 `.bak` + 原子写）。

    @return (删了吗, 原因, 找到的那条)。没匹配上/写法不支持时 `(False, 原因, None)`。
    """
    path = Path(config_file)
    original = _read_text(path)
    new_text, why, found = remove_entry(original, matches)
    if why:
        return False, why, found
    write_text_atomic(str(path) + BACKUP_SUFFIX, original)
    write_text_atomic(path, new_text)
    return True, "", found


def remove_oneoff_from_file(config_file: Any, matches: Callable[[Dict[str, str]], bool]) -> bool:
    """把匹配的那条 oneoff 从**文件**里删掉（留 `.bak` + 原子写）。

    @param config_file 配置文件路径
    @param matches     `matches(fields) -> bool`；见 `Scheduler.oneoff_matcher()`
    @return True = 删了并落盘；False = 没删（没匹配上 / 写法不支持）
    @raise OSError 读/写盘失败 —— 原样抛出，调用方（Scheduler / CLI）自己决定怎么报

    @note 两个调用方：Agent 触发后删自己刚触发的那条（`remove_fired_oneoff` 开关），
          以及 CLI 的 `assistant cleanup --apply`（清理**已经过去**的一次性日程）。
          两者走的是**同一套**文本级删除，只有"谁的 predicate"不同。
    @note ⚠ 只动 `oneoff`（R3 的规矩）：recurring 删了明天就不响了，一条都不许碰。
          工具 `set_schedule` 的"删"走的是上面那条**通用**的 `remove_entry_in_file`。
    @note 写前**重新读一遍**，匹配就在这份刚读到的文本上做 —— 所以"配置在半路被人或 GUI
          改过"不会误删（对不上就不删）。
    @note 读与 `os.replace` 之间还剩毫秒级窗口（另一个写入者可能正好插进来）。那点风险用
          `.bak` 兜着，不引入锁：同一时刻两个写入者的概率极低，锁的复杂度不划算。
    """
    wrote, _why = _read_write(config_file,
                              lambda text: remove_oneoff_entry(text, matches))
    return wrote
