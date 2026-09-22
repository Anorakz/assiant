# ============================================================================
#  agent/core/schedule_config.py — 删掉"已经触发过的一次性日程"（R3）
#
#  为什么是**文本级**手术（而不是 yaml.dump / save_config）
#  ---------------------------------------------------------------------------
#    · `config/config.yaml` 的注释就是各字段的事实约定来源（`config.example.yaml`
#      里写明了这件事），GUI 也用**文本级替换**写它的 `gui:`/`llm:` 段 ——
#      整体重排 + 丢注释是不可接受的。
#    · 所以这里只做一件事：在 `oneoff:` 序列里找出**那一条**的行区间，删掉那些行，
#      **其余字节原样保留**（测试逐字节比对）。
#
#  匹配靠注入的 predicate（不是在这里解析日程语义）
#  ---------------------------------------------------------------------------
#    `matches(fields)` 收到的是从**文件**里抽出来的裸标量
#    `{"title": ..., "date": ..., "start": ...}`（已去引号、去行尾注释），自己判断
#    "是不是我要删的那条"。这样"什么算同一条"（title + date + start 怎么归一化）留在
#    懂日程语义的那一层（Scheduler 用 parse_clock / date.fromisoformat），本模块只懂文本
#    —— 也避免了 `scheduler.py <-> schedule_config.py` 的循环依赖。
#
#  明确**不做**的事（都给出理由、原样返回不改文件）
#  ---------------------------------------------------------------------------
#    · flow 风格（`oneoff: [{title: ..., date: ...}]`）：一行里塞多个条目，文本级改它
#      风险太大 —— 报个理由让人去手改
#    · 找不到匹配 -> 也算"没删"（可能已经删过了，或者配置被人改过），不猜
#    · **紧贴在被删条目前面**的注释与空行**留着**（只删属于那条的行）：宁可留一行悬空
#      注释，也不删掉可能是在描述下一条的注释 —— 注释在这个仓库里是信息，不是装饰
# ============================================================================

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from agent.config import write_text_atomic

__all__ = [
    "ScheduleConfigError",
    "BACKUP_SUFFIX",
    "strip_scalar",
    "find_oneoff_block",
    "remove_oneoff_entry",
    "remove_fired_oneoff",
]

#: 备份后缀：与 GUI 的 ConfigStore 同一约定（`config/config.yaml.bak`；已被 .gitignore 覆盖）
BACKUP_SUFFIX = ".bak"

#: `key:` 所在的行（用来找 `oneoff:` 序列）
_KEY_RE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:(.*)$")
#: 条目块里的 `title:` / `date:` / `start:`（含 `- title:` 这种首行写法）
_FIELD_RE = re.compile(r"^\s*(?:-\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*:(.*)$")
#: 一条序列项的开头：`  - title: ...`
_ITEM_RE = re.compile(r"^(\s*)-\s*(.*)$")


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
    """`oneoff:` 之后，属于这个序列的每个条目的行区间 `[start, end)`。"""
    items: List[Tuple[int, int]] = []
    current: Optional[int] = None

    for index in range(key_index + 1, len(lines)):
        line = lines[index]
        if _is_blank(line):
            continue                    # 空行不切条目（收尾按下一条目/缩进回退算）
        if _indent_of(line) <= key_indent:
            break                       # 缩进回退 = 序列结束（注释也算：那是别的段的）
        if _ITEM_RE.match(line) and not _is_comment(line):
            if current is not None:
                items.append((current, index))
            current = index
    if current is not None:
        items.append((current, len(lines)))

    # 尾部的空行/纯注释行不算条目内容 —— 留着它们，免得删掉本该属于下一条的注释
    trimmed: List[Tuple[int, int]] = []
    for start, end in items:
        while end - 1 > start and (_is_blank(lines[end - 1]) or _is_comment(lines[end - 1])):
            end -= 1
        trimmed.append((start, end))
    return trimmed


def _fields_of(lines: List[str], start: int, end: int) -> Dict[str, str]:
    """条目块里抽 `title` / `date` / `start`（每个键取第一次出现）。"""
    fields: Dict[str, str] = {}
    for index in range(start, end):
        line = lines[index]
        if _is_comment(line):
            continue
        match = _FIELD_RE.match(line)
        if not match:
            continue
        key = match.group(1)
        if key in ("title", "date", "start") and key not in fields:
            fields[key] = strip_scalar(match.group(2))
    return fields


def _iter_oneoff_blocks(text: str) -> Iterator[Tuple[int, int, int, Dict[str, str]]]:
    """遍历所有 `oneoff:` 序列里的条目。

    @return (键所在行号, 条目起点, 条目终点, 字段)
    @raise ScheduleConfigError `oneoff:` 用了 flow 风格（不碰那种写法）
    """
    lines = _split_lines(text)
    for index, line in enumerate(lines):
        match = _key_on(line, "oneoff")
        if match is None:
            continue
        key_indent = len(match.group(1))
        value = strip_scalar(match.group(3))
        if value:
            if value in ("[]", "{}"):
                continue                # 空序列：没东西可删
            raise ScheduleConfigError(
                "oneoff 用了 flow 风格（%s），文本级改它风险太大 —— 请手改" % value[:60]
            )
        for start, end in _sequence_items(lines, index, key_indent):
            yield index, start, end, _fields_of(lines, start, end)


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
def _remove_span(text: str, key_index: int, span: Tuple[int, int]) -> str:
    """删掉区间，其余**逐字节**保留；序列空了就把 `oneoff:` 补成 `[]`。"""
    lines = _split_lines(text)
    start, end = span
    remaining = lines[:start] + lines[end:]

    match = _key_on(remaining[key_index], "oneoff") if key_index < len(remaining) else None
    if match is None:
        return "".join(remaining)       # 键那行也被删了？（不该发生）原样返回
    key_indent = len(match.group(1))
    if _sequence_items(remaining, key_index, key_indent):
        return "".join(remaining)       # 还有别的条目：一个字节都不动

    ending = "\r\n" if remaining[key_index].endswith("\r\n") else "\n"
    remaining[key_index] = "%soneoff: []%s" % (match.group(1), ending)
    return "".join(remaining)


def remove_oneoff_entry(text: str, matches: Callable[[Dict[str, str]], bool]
                        ) -> Tuple[str, str]:
    """从**文本**里删掉匹配的那条 oneoff。

    @return (新文本, 原因)。原因是空串 = 真的删了；否则文本原样返回、原因说明为什么。
    """
    try:
        for key_index, start, end, fields in _iter_oneoff_blocks(text):
            if matches(fields):
                return _remove_span(text, key_index, (start, end)), ""
    except ScheduleConfigError as exc:
        return text, str(exc)
    return text, "配置里没有匹配的那条（可能已经删过了，或者被人改过）"


def remove_fired_oneoff(config_file: Any, matches: Callable[[Dict[str, str]], bool]) -> bool:
    """把匹配的那条一次性日程从**文件**里删掉（留 `.bak` + 原子写）。

    @return True = 删了并落盘；False = 没删（没匹配上 / 写法不支持）
    @raise OSError 读/写盘失败 —— 原样抛出，调用方（Scheduler）记 WARNING 就好

    @note 写前**重新读一遍**，匹配就在这份刚读到的文本上做 —— 所以"配置在半路被人或 GUI
          改过"不会误删（对不上就不删）。
    @note 读与 `os.replace` 之间还剩毫秒级窗口（另一个写入者可能正好插进来）。那点风险用
          `.bak` 兜着，不引入锁：同一时刻两个写入者的概率极低，锁的复杂度不划算。
    """
    path = Path(config_file)
    original = path.read_text(encoding="utf-8")
    new_text, why = remove_oneoff_entry(original, matches)
    if why:
        return False
    write_text_atomic(str(path) + BACKUP_SUFFIX, original)   # 覆盖上一份 .bak
    write_text_atomic(path, new_text)
    return True
