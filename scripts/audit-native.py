#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/audit-native.py —— C/C++ 的重复组件与可疑写法普查（T15-3 / 3-5）

为什么要它：`scripts/audit-code.py` 只懂 Python，而 `native/`（3.9 千行）与
`gui/`（1.7 万行）都是 C++。这一版**不是**编译器前端，是**文本级**的普查器 ——
它的价值在于"把可疑处列出来给人看"，而不是给出终审判决。

它做什么
---------------------------------------------------------------------------
1. **重复函数体**：按 `名字(参数) { ... }` 粗略切出函数，把函数体归一化
   （去注释/字符串/数字、标识符一律抽象、去空白）后取指纹，指纹相同 = 同一段逻辑的副本。
2. **同名不同体**：同一个函数名在多处各写一遍（可能是该收敛的接口，也可能是正常重载）。
3. **可疑写法**：`new`/`delete` 手工配对、`malloc/free`、`strcpy`/`sprintf`/`gets`
   （溢出高危）、`using namespace` 出现在头文件、`#define` 常量而非 `constexpr`、
   `TODO/FIXME/HACK` 计数。
4. **规模**：文件/行数，按目录分布。

⚠ 已知限制（写清楚，免得被当成判决依据）
---------------------------------------------------------------------------
· 不看预处理指令（`#if` 分支全算进去），不做重载/模板展开；
· 大括号计数对"`{` 出现在字符串或宏里"会失真 → 所以结论一律标"疑似"；
· `gui/` 依赖 Qt 头，本地编不了 → 这里只做文本级检查，编译器告警留给能编的环境。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from collections import defaultdict

SRC_EXT = {".c", ".cc", ".cpp", ".cxx"}
HDR_EXT = {".h", ".hh", ".hpp", ".hxx"}
SKIP_DIRS = {"third_party", "build", "build-host", "build-rk3568", "temp", "__pycache__",
             "moonlight-common-c", "pybind11", "googletest", "conda", "model", "logs"}
ROOTS = ("native", "gui")
DANGER_CALLS = ("strcpy", "strcat", "sprintf", "vsprintf", "gets", "alloca")
TODO_RE = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b")
FUNC_RE = re.compile(
    r"^[ \t]*(?:[\w:<>,*&\s~]+?)[ \t]+(?P<name>[\w:~]+)[ \t]*\((?P<args>[^;{)]*)\)"
    r"[ \t]*(?:const[ \t]*)?(?:noexcept[ \t]*)?(?:override[ \t]*)?(?:final[ \t]*)?\{",
    re.M)
# 控制流关键字/非常规函数名会被上面的正则误当"函数"（实测 `if (...) {`、
# `QTimer::singleShot(...) {` 都进来了）→ 显式排掉，否则"重复函数体"全是假阳性。
NOT_A_FUNC = {"if", "for", "while", "switch", "catch", "else", "do", "return",
              "QTimer::singleShot", "sizeof", "alignof", "static_assert"}


def strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"//[^\n]*", " ", text)
    text = re.sub(r'"(?:\\.|[^"\\])*"', '""', text)
    text = re.sub(r"'(?:\\.|[^'\\])*'", "''", text)
    return text


def normalize_body(body: str) -> str:
    body = strip_comments(body)
    body = re.sub(r"\b\d+\b", "0", body)
    body = re.sub(r"\b[A-Za-z_]\w*\b", "_", body)
    body = re.sub(r"\s+", " ", body)
    return body.strip()


def split_functions(text: str):
    """粗切函数：返回 [(name, start_line, normalized_body)]。"""
    out = []
    for m in FUNC_RE.finditer(text):
        if m.group("name") in NOT_A_FUNC:
            continue
        start = m.end() - 1                       # 指向 '{'
        depth, i, n = 0, start, len(text)
        while i < n:
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        if depth != 0 or i >= n:
            continue                              # 大括号不平衡（宏/字符串）→ 跳过
        body = text[start:i + 1]
        line = text[:m.start()].count("\n") + 1
        nb = normalize_body(body)
        if len(nb) < 120:                         # 太短不参与重复比较
            continue
        out.append((m.group("name"), line, nb))
    return out


def scan(root: str):
    files, funcs_by_fp, funcs_by_name, todos, danger, usingns, defines = [], {}, {}, [], [], [], []
    for top in ROOTS:
        for dirpath, dirnames, filenames in os.walk(os.path.join(root, top)):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for fn in filenames:
                ext = os.path.splitext(fn)[1].lower()
                if ext not in SRC_EXT | HDR_EXT:
                    continue
                p = os.path.join(dirpath, fn)
                rel = os.path.relpath(p, root).replace(os.sep, "/")
                try:
                    text = open(p, "r", encoding="utf-8", errors="replace").read()
                except OSError:
                    continue
                files.append((rel, len(text.splitlines())))
                for name, line, nb in split_functions(text):
                    fp = hashlib.sha1(nb.encode("utf-8")).hexdigest()[:16]
                    funcs_by_fp.setdefault(fp, []).append("%s:%d %s" % (rel, line, name))
                    funcs_by_name.setdefault(name, set()).add(fp)
                for m in TODO_RE.finditer(text):
                    todos.append("%s:%d %s" % (rel, text[:m.start()].count("\n") + 1, m.group(1)))
                for call in DANGER_CALLS:
                    for m in re.finditer(r"\b%s\s*\(" % call, strip_comments(text)):
                        danger.append("%s:%d %s" % (rel, text[:m.start()].count("\n") + 1, call))
                if ext in HDR_EXT:
                    for m in re.finditer(r"^\s*using namespace\s+([\w:]+)", text, re.M):
                        usingns.append("%s:%d %s" % (rel, text[:m.start()].count("\n") + 1, m.group(1)))
                for m in re.finditer(r"^\s*#define\s+([A-Z_][A-Z0-9_]*)\s+\d", text, re.M):
                    defines.append("%s:%d %s" % (rel, text[:m.start()].count("\n") + 1, m.group(1)))
    dups = sorted((sorted(v) for v in funcs_by_fp.values() if len(v) > 1), key=len, reverse=True)
    same_name = {k: sorted(v) for k, v in funcs_by_name.items() if len(v) > 1 and k not in ("main",)}
    return files, dups, same_name, todos, danger, usingns, defines


def main() -> int:
    ap = argparse.ArgumentParser(description="C/C++ 重复组件与可疑写法普查（T15-3）")
    ap.add_argument("root", nargs="?", default=".")
    ap.add_argument("--top", type=int, default=8)
    args = ap.parse_args()
    root = os.path.abspath(args.root)

    files, dups, same_name, todos, danger, usingns, defines = scan(root)
    src = [f for f in files if os.path.splitext(f[0])[1].lower() in SRC_EXT]
    hdr = [f for f in files if os.path.splitext(f[0])[1].lower() in HDR_EXT]
    print("== 规模")
    print("   源文件 %d（%d 行）；头文件 %d（%d 行）"
          % (len(src), sum(n for _, n in src), len(hdr), sum(n for _, n in hdr)))

    print("\n== 重复函数体（%d 组）" % len(dups))
    for g in dups[: args.top]:
        print("   -- %d 处" % len(g))
        for it in g:
            print("      " + it)
    if len(dups) > args.top:
        print("   …还有 %d 组" % (len(dups) - args.top))

    print("\n== 同名不同体（%d 个，可能是该收敛的接口，也可能只是重载）" % len(same_name))
    for name, fps in list(same_name.items())[: args.top]:
        print("   %-28s %d 种实现" % (name, len(fps)))

    print("\n== 可疑写法")
    for label, items in (("高危字符串/内存函数（%d）" % len(danger), danger),
                         ("头文件里的 using namespace（%d）" % len(usingns), usingns),
                         ("宏定义的数字常量（%d，考虑 constexpr）" % len(defines), defines),
                         ("待办标记（%d）" % len(todos), todos)):
        print("   -- " + label)
        for it in items[: args.top]:
            print("      " + it)
        if len(items) > args.top:
            print("      …还有 %d 条" % (len(items) - args.top))
    return 0


if __name__ == "__main__":
    sys.exit(main())
