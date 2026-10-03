# ============================================================================
#  agent/core/settings_config.py — **设置项的文本级写入**（Phase 13 T13-8）
#
#  干什么: 把"某个已知设置项"改成新值，**只动那一行**，其余字节原样保留
#          （注释、空行、顺序、CRLF 全留着），并在原文件旁留一份 `.bak`。
#
#  为什么还是**文本级**（而不是 yaml.safe_dump 重写）
#  ---------------------------------------------------------------------------
#    · `config/config.yaml` 的**注释就是各字段的事实约定来源** —— GUI 的 ConfigStore
#      与 `schedule_config.py` 都是文本级改它；整体重排 + 丢注释不可接受。
#    · 所以这里只做三件事:
#        ① 找到那个键所在的行 -> 只换行尾的值（**保留原来的行尾注释**）;
#        ② 键不在、但**段在** -> 按模板的注释与缩进把这一行插进段的末尾;
#        ③ **段都没有** -> 把模板里那一整段（含说明注释）追加到文件末尾。
#
#  真源是谁: `config/config.example.yaml`（模板）
#  ---------------------------------------------------------------------------
#    · **能改哪些键 = 模板里有那些键**: 不在模板里的路径一律拒绝（这样"键清单"
#      只有一处维护 —— 加一个新设置项就是往模板里加一行, 不用改这个模块）。
#    · 只有**标量**能改: 映射（`study.classes` / `bilibili.game_watch.process_names`）
#      与序列（`scheduler.oneoff`）拒绝 —— 它们是结构，文本级改它们风险太大，
#      报个理由让人去手改（与 `schedule_config` 对 flow 风格的态度一致）。
#    · 值的**类型跟着模板走**: 模板里是 `false` 就只能给布尔、是 `0.05` 就只能给数字、
#      是字符串就给字符串。理由: 类型写错在 YAML 里**不报错**（`relative_band: "0.05"`
#      是字符串），只会让 Agent 读出来变成另一个东西。
#
#  谁用它: `assistant set`（CLI）与 `assistant study freeze/unfreeze`（T13-8）。
#          GUI 那份 C++ ConfigStore 是**同一套语义的另一份实现**（GUI 在板端编译,
#          与 Python 不同进程、不同语言, 没法共用代码）—— 两边的约定必须一致:
#          点号路径 / 只动目标行 / `.bak` / 缺段按模板新建。
#
#  ⚠ 本模块**不校验语义**（`--focus-interval-min` 是不是负数、路径存不存在）：
#    它只保证"写进去是一行类型正确的 YAML"。语义校验在各功能的构造函数里
#    （例如 `StudyWatcher._bounds` 会拒绝写反的界）。
# ============================================================================

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from agent.config import config_dir, write_text_atomic

__all__ = [
    "SettingsConfigError",
    "BACKUP_SUFFIX",
    "TEMPLATE_NAME",
    "TARGET_NAME",
    "known_paths",
    "template_value",
    "read_value",
    "plan_changes",
    "apply_changes",
    "render_scalar",
]

#: 备份后缀（与 GUI 的 ConfigStore / `schedule_config` 同一约定；`*.bak` 已被 .gitignore 覆盖）
BACKUP_SUFFIX = ".bak"

#: 模板（键清单与插段用的注释都从它来）与真源。
TEMPLATE_NAME = "config.example.yaml"
TARGET_NAME = "config.yaml"

#: `key:` 行（缩进 + 键 + 冒号后的原始文本）
_KEY_RE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:(.*)$")
#: 一行里"值 + 行尾注释"的分界（注释前必须有空白，与 YAML 一致）
_INLINE_COMMENT_RE = re.compile(r"\s+#")
#: 段与段之间的**分隔横幅**（`# ----------`）—— 新建整段时看到它就停。
_BANNER_RE = re.compile(r"^#\s*-{3,}")
#: 可以裸着写进 YAML 的字符串（其余一律加双引号 —— 保险，且两边解析器都认）
_PLAIN_RE = re.compile(r"^[A-Za-z0-9_./\-+]+$")
#: YAML 1.1 里有特殊含义的裸词（写成裸词会被解析成布尔/空/数字）
_RESERVED_PLAIN = {
    "true", "false", "yes", "no", "on", "off", "null", "none", "y", "n", "~",
    "nan", "inf", "-inf",
}


class SettingsConfigError(ValueError):
    """不认识的设置项 / 值类型不对 / 文件形状不支持（继承 ValueError，与其它模块一致）。"""


# ---------------------------------------------------------------------------
#  文本工具（与 schedule_config 同一套口径：逐字节读、保留行尾）
# ---------------------------------------------------------------------------
def _read_text(path: Path) -> str:
    """**逐字节**读（不做换行翻译）—— 否则"读出来再写回去"会把 CRLF 悄悄改成 LF。"""
    with open(str(path), "r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _ending_of(line: str) -> str:
    return "\r\n" if line.endswith("\r\n") else "\n"


def _dominant_ending(text: str) -> str:
    match = re.search(r"\r\n|\n", text)
    return match.group(0) if match else "\n"


def _strip_body(line: str) -> str:
    """去掉行尾（`\\n` / `\\r\\n`），留着内容。"""
    return line[:-2] if line.endswith("\r\n") else (line[:-1] if line.endswith("\n") else line)


def _split_value(raw: str) -> Tuple[str, str]:
    """把 `0.05   # 注释` 拆成（`0.05`, `   # 注释`）。没有注释时第二段是空串。"""
    match = _INLINE_COMMENT_RE.search(raw)
    if not match:
        return raw, ""
    return raw[:match.start()], raw[match.start():]


def _bare(raw: str) -> str:
    """去掉引号与首尾空白（只看值那一半）。"""
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1]
    return text.strip()


# ---------------------------------------------------------------------------
#  模板: 键清单 + 插段用的原文
# ---------------------------------------------------------------------------
class _Template(object):
    """模板的文本视图: 每个**叶子键**在模板里的 (行号, 缩进, 值, 注释, 所属段)。"""

    def __init__(self, text: str):
        self.text = text
        self.lines = text.splitlines(keepends=True)
        self.leaves: Dict[str, Dict[str, Any]] = {}
        self.segments: Dict[str, Dict[str, Any]] = {}
        #: 键名里带点的映射条目（例如 `study.process_names.code.exe`）—— 不可寻址, 只记一笔
        self.dotted: List[str] = []
        self._parse()

    def _comment_run(self, pending: List[int]) -> List[int]:
        """`pending` 里**紧贴在上面**的那一段注释（**遇到空行就停**）。

        @note 为什么不是"所有攒下来的行": 上一个段尾巴上的注释也攒在 `pending` 里
              （T13-8 本地冒烟抓到 —— 新建 `study:` 段时把 `bilibili:` 段最后那两句
              说明也搬过来了）。段与段之间**有且只有一个空行**，所以"往回走，遇到空行停"
              就是那条分界。
        """
        run: List[int] = []
        for index in reversed(pending):
            if _strip_body(self.lines[index]).strip().startswith("#"):
                run.append(index)
                continue
            break
        return sorted(run)

    def _parse(self) -> None:
        stack: List[Tuple[int, str]] = []                # [(缩进, 键)] —— 顶层段的路径
        pending_comments: List[int] = []                 # 紧贴在某个键上面的注释/空行
        for index, line in enumerate(self.lines):
            body = _strip_body(line)
            stripped = body.strip()
            if not stripped or stripped.startswith("#"):
                pending_comments.append(index)
                continue
            match = _KEY_RE.match(body)
            if not match:
                pending_comments = []
                continue
            indent = len(match.group(1))
            key = match.group(2)
            raw_value = match.group(3)
            # 弹出比当前缩进更深的层级
            while stack and stack[-1][0] >= indent:
                stack.pop()
            path_parts = [item[1] for item in stack] + [key]
            path = ".".join(path_parts)
            value, comment = _split_value(raw_value)
            value = value.strip()
            run = self._comment_run(pending_comments)
            if indent == 0:
                self.segments[path] = {"line": index, "comments": run}
            if value == "":
                # 空的键 = 下面是嵌套块（段 / 映射 / 序列）
                stack.append((indent, key))
                pending_comments = []
                continue
            if "." in key:
                # ⚠ 键名里带点的**映射条目**（例如 `study.process_names` 下的 `code.exe`）
                #   没法用点号路径寻址（`a.code.exe` 会被拆成四段）—— 不进键清单,
                #   也不给改（它是映射的一部分, 结构级的东西本来就该手改）。
                self.dotted.append(path)
                pending_comments = []
                continue
            self.leaves[path] = {"line": index, "indent": indent, "value": value,
                                 "comment": comment, "comments": run,
                                 "segment": path_parts[0]}
            pending_comments = []

    def segment_span(self, path: str) -> Tuple[int, int]:
        """段（顶层键）在模板里的 `[起, 止)` —— 起含它自己的横幅, 止到下一个段的横幅。"""
        if path not in self.segments:
            raise SettingsConfigError("模板里没有 %s 这一段" % path)
        banner = self.segments[path]["comments"]
        start = min(banner) if banner else self.segments[path]["line"]
        end = len(self.lines)
        for index in range(self.segments[path]["line"] + 1, len(self.lines)):
            body = _strip_body(self.lines[index])
            if not body.strip() or body.startswith((" ", "\t")):
                continue                                 # 空行/缩进行 = 段内
            if body.lstrip().startswith("#") and not _BANNER_RE.match(body.lstrip()):
                continue                                 # 段内的顶层说明注释
            end = index
            break
        return start, end


def _template_path(name: Optional[str] = None) -> Path:
    return Path(name) if name else (config_dir() / TEMPLATE_NAME)


def _target_path(name: Optional[str] = None) -> Path:
    return Path(name) if name else (config_dir() / TARGET_NAME)


def _load_template(template: Optional[str] = None) -> _Template:
    path = _template_path(template)
    if not path.is_file():
        raise SettingsConfigError("读不到模板 %s（键清单以它为准）" % path)
    return _Template(_read_text(path))


def known_paths(template: Optional[str] = None) -> List[str]:
    """模板里所有**可设置的标量键**（点号路径，排序）。"""
    return sorted(_load_template(template).leaves)


def template_value(path: str, template: Optional[str] = None) -> str:
    """模板里这个键的值（裸文本）。"""
    return str(_load_template(template).leaves[path]["value"])


# ---------------------------------------------------------------------------
#  类型与渲染
# ---------------------------------------------------------------------------
def _kind_of(raw: str) -> str:
    """模板里的值是哪种类型: bool / int / float / str。"""
    text = _bare(raw)
    if text.lower() in ("true", "false"):
        return "bool"
    try:
        int(text)
        return "int"
    except ValueError:
        pass
    try:
        float(text)
        return "float"
    except ValueError:
        return "str"


def render_scalar(value: Any, *, like: str) -> str:
    """按模板的类型把值渲染成 YAML 标量。

    @param like 模板里那个值的裸文本（`false` / `0.05` / `config/study_stats.json`）
    @raise SettingsConfigError 类型不对（字符串给了数字、数字给了文字……）
    """
    kind = _kind_of(str(like))
    text = "" if value is None else str(value)
    if "\n" in text or "\r" in text:
        raise SettingsConfigError("值不能换行（一行一个键，这是文本级写入的前提）")
    if kind == "bool":
        low = text.strip().lower()
        if low in ("true", "1", "yes", "on"):
            return "true"
        if low in ("false", "0", "no", "off"):
            return "false"
        raise SettingsConfigError("这个键是布尔值（模板里是 %s），给不了 %r" % (like, value))
    if kind in ("int", "float"):
        try:
            number = float(text)
        except ValueError:
            raise SettingsConfigError("这个键是数字（模板里是 %s），给不了 %r" % (like, value))
        if number != number or number in (float("inf"), float("-inf")):
            raise SettingsConfigError("数字得是有限值, 收到 %r" % (value,))
        if kind == "int" and number != int(number):
            raise SettingsConfigError("这个键是整数（模板里是 %s），给不了小数 %r"
                                      % (like, value))
        if kind == "int":
            return str(int(number))
        rendered = ("%.6f" % number).rstrip("0").rstrip(".")
        return rendered or "0"
    # 字符串
    stripped = text.strip()
    if stripped and _PLAIN_RE.match(stripped) and stripped.lower() not in _RESERVED_PLAIN:
        return stripped
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return '"%s"' % escaped


# ---------------------------------------------------------------------------
#  读 / 计划 / 写
# ---------------------------------------------------------------------------
def read_value(path: str, *, target: Optional[str] = None) -> Optional[str]:
    """从**目标文件**里读一个键的裸值（读不到返回 None）。"""
    file = _target_path(target)
    if not file.is_file():
        return None
    lines = _read_text(file).splitlines()
    located = _locate(lines, path)
    if located is None:
        return None
    _index, _indent, raw, _comment = located
    return _bare(raw)


def _locate(lines: Sequence[str], path: str) -> Optional[Tuple[int, int, str, str]]:
    """在文本里找 `a.b.c` 那一行（按缩进逐层下去，不做 YAML 解析）。

    @return (行号, 缩进, **值的原文**（含它前面的空白）, 行尾注释) 或 None
    @note 值那一半特意**不 strip** —— 替换时靠它保住原来的对齐空白与行尾注释。
    """
    parts = [item for item in str(path).split(".") if item]
    if not parts:
        return None
    index, indent = 0, 0
    for depth, key in enumerate(parts):
        found = None
        while index < len(lines):
            body = _strip_body(lines[index])
            match = _KEY_RE.match(body)
            if match:
                this_indent = len(match.group(1))
                if this_indent < indent:
                    return None                              # 缩进退回去了 -> 这条路径不存在
                if this_indent == indent and match.group(2) == key:
                    found = (index, match, body)
                    break
            index += 1
        if found is None:
            return None
        line_index, match, body = found
        if depth == len(parts) - 1:
            colon = body.index(":", len(match.group(1)) + len(match.group(2)))
            value_part, comment = _split_value(body[colon + 1:])
            return (line_index, len(match.group(1)), value_part, comment)
        if match.group(3).strip():
            return None                                      # 半路遇到标量 -> 不是这条路径
        indent += 2
        index += 1
    return None


def _new_line(line: str, value_part: str, comment: str, rendered: str) -> str:
    """把这一行里那个键的值换成 `rendered`（**保留缩进/对齐空白/行尾注释**）。

    @param value_part `_locate` 给的那一段原文（含它前面的空白, **不 strip**）
    @return **不带行尾**的新行体（行尾由计划里的 `ending` 统一补 —— 早先这里也补了一次,
            结果每写一次就多出一个空行; T13-8 本地冒烟抓到的）
    """
    body = _strip_body(line)
    match = _KEY_RE.match(body)
    if match is None:
        raise SettingsConfigError("这一行不是 `键: 值`: %r" % body)
    colon = body.index(":", len(match.group(1)) + len(match.group(2)))
    lead = value_part[:len(value_part) - len(value_part.lstrip())] or " "
    return body[:colon + 1] + lead + rendered + comment


def _leaf_line_parts(line: str) -> Tuple[str, str]:
    """一行 `键: 值  # 注释` -> (值的原文, 行尾注释)。"""
    body = _strip_body(line)
    match = _KEY_RE.match(body)
    if match is None:
        raise SettingsConfigError("这一行不是 `键: 值`: %r" % body)
    colon = body.index(":", len(match.group(1)) + len(match.group(2)))
    return _split_value(body[colon + 1:])


def _block_with_value(template: "_Template", start: int, end: int, leaf_line: int,
                      rendered: str) -> List[str]:
    """模板 `lines[start:end]` -> 插入用的行（**把目标键那一行的值换成新值**）。

    ⚠ 不换的话就是把模板的默认值抄进去 —— "缺段就新建"时用户要的那个值根本没生效
      （T13-8 的单测抓到的: `set study --relative-band 0.07` 新建出来的段里还是 0.05）。

    @return **不带行尾**的行体（行尾由调用方按目标文件的风格统一补 —— 早先这里连着行尾
            一起返回、调用方又补了一次, 结果插进去的每一行后面都多一个空行）
    """
    lines = [_strip_body(line) for line in template.lines[start:end]]
    offset = leaf_line - start
    if 0 <= offset < len(lines):
        value_part, comment = _leaf_line_parts(lines[offset])
        lines[offset] = _new_line(lines[offset], value_part, comment, rendered)
    return lines


def _plan_one(lines: List[str], path: str, value: Any, template: _Template,
              ending: str) -> Dict[str, Any]:
    """把一次改动算成"新的行列表 + 说明"（**不改文件**）。"""
    entry = template.leaves.get(path)
    if entry is None:
        if path in template.segments:
            raise SettingsConfigError(
                "%s 是**一整段**（不是单个设置项）—— 段里的键一个个来（能改的键 = 模板里"
                "有的标量键: %s …）" % (path, "、".join(sorted(template.leaves)[:4])))
        raise SettingsConfigError("不认识的设置项 %s（能改的键 = 模板 %s 里有的键）"
                                  % (path, TEMPLATE_NAME))
    rendered = render_scalar(value, like=str(entry["value"]))
    located = _locate(lines, path)
    if located is not None:
        index, _indent, value_part, comment = located
        return {"path": path, "action": "set", "line": index + 1,
                "old": _bare(value_part), "new": _bare(rendered),
                "body": _new_line(lines[index], value_part, comment, rendered),
                "ending": _ending_of(lines[index])}
    # 键不在: 段在就插进段尾; 段也不在就把模板那一整段（含说明注释）追加到文件末尾
    segment = str(entry.get("segment") or path.split(".")[0])
    indent = " " * int(entry["indent"])
    if segment in template.segments and _locate(lines, segment) is not None:
        start = min(template.leaves[path]["comments"] or [int(entry["line"])])
        block = _block_with_value(template, start, int(entry["line"]) + 1,
                                 int(entry["line"]), rendered)
        insert = [indent + line.strip() + ending for line in block]
        at = _segment_end(lines, segment)
        return {"path": path, "action": "add-key", "line": at + 1, "old": None,
                "new": _bare(rendered), "insert_at": at, "insert": insert}
    if segment not in template.segments:
        raise SettingsConfigError("模板里没有 %s 这一段，没法新建" % segment)
    start, end = template.segment_span(segment)
    block = _block_with_value(template, start, end, int(entry["line"]), rendered)
    insert = [line + ending for line in block]
    if lines and _strip_body(lines[-1]).strip():
        insert = [ending] + insert                             # 与已有内容隔一个空行
    return {"path": path, "action": "add-segment", "line": len(lines) + 1, "old": None,
            "new": _bare(rendered), "insert_at": len(lines), "insert": insert,
            "segment": segment}


def _segment_end(lines: Sequence[str], segment: str) -> int:
    """段（顶层键）的最后一行之后（下一个顶层键之前）。"""
    start = None
    for index, line in enumerate(lines):
        match = _KEY_RE.match(_strip_body(line))
        if match and len(match.group(1)) == 0 and match.group(2) == segment:
            start = index
            break
    if start is None:
        return len(lines)
    for index in range(start + 1, len(lines)):
        match = _KEY_RE.match(_strip_body(lines[index]))
        if match and len(match.group(1)) == 0:
            return index
    return len(lines)


def _apply_plan(working: List[str], plan: Mapping[str, Any]) -> None:
    """把一条计划落进"工作副本"。"""
    if plan["action"] == "set":
        working[plan["line"] - 1] = plan["body"] + plan["ending"]
    else:
        working[plan["insert_at"]:plan["insert_at"]] = list(plan["insert"])


def plan_changes(changes: Mapping[str, Any], *, target: Optional[str] = None,
                 template: Optional[str] = None) -> List[Dict[str, Any]]:
    """算出这批改动会怎么动文件（**不写盘**）。@return 每条改动的说明。

    @raise SettingsConfigError 键不认识 / 类型不对 / 文件不在（是模板）
    """
    file = _target_path(target)
    if not file.is_file():
        raise SettingsConfigError("真源不在: %s（先 cp 模板起步）" % file)
    if file.name.endswith(".example.yaml"):
        raise SettingsConfigError("%s 是模板, 不是真源 —— 改它没意义" % file)
    view = _load_template(template)
    ending = _dominant_ending(_read_text(file))
    working = _read_text(file).splitlines(keepends=True)
    plans: List[Dict[str, Any]] = []
    for path, value in (changes or {}).items():
        plan = _plan_one(working, path, value, view, ending)
        plans.append({key: item for key, item in plan.items() if key != "body"})
        _apply_plan(working, plan)
    return plans


def apply_changes(changes: Mapping[str, Any], *, target: Optional[str] = None,
                  template: Optional[str] = None) -> Dict[str, Any]:
    """按计划真写（原文件旁留 `.bak` + **原子写**）。

    @return `{"plans","backup","changed","path"}`（`changed` = 真的改了值/加了键的条数）
    @note 一条都没改（值本来就是这样）**不写文件** —— 免得白留一份 `.bak`。
    """
    file = _target_path(target)
    plans = plan_changes(changes, target=target, template=template)
    changed = [plan for plan in plans if plan["old"] != plan["new"]]
    if not changed:
        return {"plans": plans, "backup": "", "changed": 0, "path": str(file)}
    view = _load_template(template)
    text = _read_text(file)
    ending = _dominant_ending(text)
    working = text.splitlines(keepends=True)
    for path, value in (changes or {}).items():
        _apply_plan(working, _plan_one(working, path, value, view, ending))
    backup = str(file) + BACKUP_SUFFIX
    try:
        shutil.copy2(str(file), backup)
        write_text_atomic(file, "".join(working))
    except OSError as exc:
        raise SettingsConfigError("写不进去（%s）: %s" % (file, exc)) from exc
    return {"plans": plans, "backup": backup, "changed": len(changed), "path": str(file)}
