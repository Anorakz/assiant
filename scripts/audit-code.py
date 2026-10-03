#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/audit-code.py —— 代码规范审计的**机械普查器**（T15-3，零依赖）

为什么要它（而不是"读一遍代码"）
---------------------------------------------------------------------------
仓库规模：Python 约 7 万行 + 原生 3.9 千行 + GUI 1.7 万行。靠眼睛"读一遍"只能得到
印象，得不到**可复现的数字**。这个脚本把"哪些是重复实现、哪些根本没被引用、哪些
写法违反我们自己的约定"变成可复算的清单；人只在清单上做判断（选哪份当唯一实现、
哪些直接删）。

它查三类问题
---------------------------------------------------------------------------
1. **重复实现**：函数体归一化（去掉 docstring、变量名/常量值/函数名抽象成占位符、
   保留属性名与关键字参数名）后取指纹 —— 指纹相同 = 同一段逻辑的多个副本。
   另报"**同名不同体**"，但**只在模块级函数之间**比（不同类上的同名方法是
   正常设计，不是重复）。
2. **疑似死代码**：① 从未被 import、也没在任何文本里被提到的模块；② 全仓库
   （含非 Python 文本：systemd 单元、脚本、配置）零引用的函数/类。引用计数分
   "生产引用 / 仅测试引用"两档 —— 只有测试用到的，生产上仍是死的（但会单独标出，
   因为它也可能是"框架回调/字符串注册"）。
3. **规范启发式**：裸 except、可变默认参数、`open()` 不带 encoding、`shell=True`、
   异步函数里的 `time.sleep`、库代码里的 `print`、超长函数、深嵌套、待办标记。

口径（T15-3 定稿）
---------------------------------------------------------------------------
· **只看我们自己的源码**：文件清单优先用 `git ls-files`（自动排除 submodule 内容
  与构建产物）；没有 git 时用目录白名单 + 跳过黑名单。
· **生产与测试分开报**：测试里的重复通常不值得收敛，不能把生产问题埋掉。
· 短函数（<3 条语句）不参与重复比较 —— 否则 `def get(self): return self._x`
  会刷屏。
· 死代码一律标"疑似"并给出引用计数：符合动态调用（getattr / 字符串注册 / 框架
  回调）的，必须人工确认后再删。

它自己也有测试（tests/test_audit_script.py）：喂一个含已知重复与死代码的样例，
断言它找得出来 —— 工具本身不可信，结论就没有意义。

用法
---------------------------------------------------------------------------
    python3 scripts/audit-code.py                  # 人读报告
    python3 scripts/audit-code.py --json out.json  # 机器可读（给文档/棘轮基线）
    python3 scripts/audit-code.py --quiet          # 只打汇总
    python3 scripts/audit-code.py --all            # 每节打印全部（默认只打前 N）
"""
from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict

# 目录白名单：只有这些是我们的源码（没有 git 时用）
INCLUDE_DIRS = ("agent", "gui", "native", "scripts", "image", "tests", "systemd", "config", "cmake")
# 黑名单（有 git 时用不到，兜底走目录扫描时用）
SKIP_DIRS = {
    ".git", ".github", ".pytest_cache", ".ruff_cache", ".vscode", "__pycache__",
    "build", "build-host", "build-rk3568", "build-rk3568-gui-payload",
    "build-rk3568-native-payload", "conda", "logs", "model", "temp",
    "third_party", "thirdparty", "external", "moonlight-common-c", "pybind11", "googletest",
}
TEXT_EXT = {".py", ".sh", ".bash", ".c", ".h", ".cc", ".cpp", ".hpp", ".qml", ".js",
            ".yaml", ".yml", ".json", ".md", ".txt", ".service", ".target", ".conf",
            ".rules", ".nmconnection", ".ps1", ".cmake", ".ini", ".cfg"}
# 哪些文件算"引用"的载体：**代码与配置**算，**文档/说明**不算。
#   为什么：文档里随手提一个函数名，不该把死代码"救活" —— 只在文档里出现的函数
#   仍然是死代码。而且文档改动频繁，算进去会让棘轮随每次写文档抖动
#   （实测：改一次 docs/audit-code.md 就能让棘轮多报 1 条 / 少报 7 条）。
REF_EXT = {".py", ".sh", ".bash", ".c", ".h", ".cc", ".cpp", ".hpp", ".qml", ".js",
           ".yaml", ".yml", ".json", ".service", ".target", ".conf", ".rules",
           ".nmconnection", ".ps1", ".cmake", ".ini", ".cfg"}
NATIVE_EXT = {".c", ".h", ".cc", ".cpp", ".hpp"}
MAX_FILE_BYTES = 2 * 1024 * 1024
LONG_FUNC_LINES = 120
DEEP_NEST = 6
MIN_STMTS_FOR_DUP = 3


class BodyNormalizer(ast.NodeTransformer):
    """把函数体里"与语义无关的细节"抽象掉，只留结构。

    去掉：docstring、变量名、函数名、常量字面量、参数名与默认值。
    保留：语句种类与顺序、运算符、**属性名**（`.read()` 与 `.write()` 不是一回事）、
          **关键字参数名**（`timeout=` 是语义）。
    """

    def visit_FunctionDef(self, node):                      # noqa: N802
        self.generic_visit(node)
        node.name = "_"
        node.args.args = [ast.arg(arg="_") for _ in node.args.args]
        node.args.kwonlyargs = [ast.arg(arg="_") for _ in node.args.kwonlyargs]
        node.args.defaults = []
        node.args.kw_defaults = []
        node.body = [n for n in node.body if not _is_docstring(n)]
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Name(self, node):                             # noqa: N802
        node.id = "_"
        return node

    def visit_Constant(self, node):                         # noqa: N802
        if isinstance(node.value, str):
            node.value = "_"
        elif isinstance(node.value, (int, float, complex)):
            node.value = 0
        return node


def _is_docstring(node: ast.stmt) -> bool:
    return (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str))


def git_files(root: str) -> list[str] | None:
    """（已不再用于主流程）用 git 拿文件清单。

    ⚠ 为什么不用它：`git ls-files` 只看**已跟踪**文件 —— 于是"新建但还没 add 的文件"
    不在清单里，棘轮结果会随 git 状态（未跟踪/已暂存/已提交）变化。实测就是它在
    同一棵树上给出 196 vs 190 两种结果。审计要的是**磁盘上的源码**，所以主流程
    一律用 walk_files（确定性强，新文件立刻纳入）。
    """
    try:
        out = subprocess.run(["git", "-C", root, "ls-files", "-z"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             check=True).stdout
    except Exception:                                        # noqa: BLE001
        return None
    files = [f for f in out.decode("utf-8", "replace").split("\0") if f]
    return files or None


def walk_files(root: str) -> list[str]:
    """没有 git 时的兜底扫描。

    ⚠ 对**任意目录**也要能用（工具自己的测试就是拿临时样例树跑的，实测这里踩过：
    样例树里没有 agent/gui/… 这些目录，白名单一卡就一个文件都扫不到）。
    所以：根目录下若有白名单目录就只扫它们；一个都没有就退回"扫全部，只跳黑名单"。
    """
    try:
        top = {d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))}
    except OSError:
        top = set()
    restrict = bool(top & set(INCLUDE_DIRS))

    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        if restrict:
            if rel_dir == ".":
                dirnames[:] = [d for d in dirnames if d in INCLUDE_DIRS]
            elif rel_dir.split("/")[0] not in INCLUDE_DIRS:
                continue
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in TEXT_EXT:
                files.append(os.path.join(rel_dir, fn) if rel_dir != "." else fn)
    return files


class FuncInfo:
    __slots__ = ("name", "file", "line", "fingerprint", "n_stmt", "is_test", "cls")

    def __init__(self, name, file, line, fingerprint, n_stmt, is_test, cls):
        self.name, self.file, self.line = name, file, line
        self.fingerprint, self.n_stmt, self.is_test, self.cls = fingerprint, n_stmt, is_test, cls


def fingerprint(node: ast.AST) -> str:
    """归一化后的结构指纹。

    ⚠ **不能用 `ast.unparse`**：那是 Python 3.9 才有的 API，而本仓库 CI 与**板端
    解释器都是 3.8**（CI 特意用 3.8 就是"与板端同版本"）。实测第一版用了它，CI 直接红。
    ⚠ **必须先 `deepcopy`**：`NodeTransformer` 是**就地修改** —— 直接归一化原节点会把
    函数名/参数名改成 `_`、把 docstring 删掉，后续分析（同名不同体、死代码）读到的
    就是被改坏的树（实测：去掉 unparse 后"零引用定义"直接从 10 变 0）。
    """
    copy = BodyNormalizer().visit(copy_module(node))
    return hashlib.sha1(ast.dump(copy).encode("utf-8")).hexdigest()[:16]


def copy_module(node: ast.AST) -> ast.AST:
    """深拷贝一个 AST 节点（3.8 兼容；不用 `ast.unparse` 那种 3.9+ 的绕法）。"""
    return copy.deepcopy(node)


class Corpus:
    """一次扫描的结果：文本、函数/类索引、以及**全局标识符索引**（避免 O(n²) 引用计数）。"""

    def __init__(self):
        self.text: dict[str, str] = {}
        self.py_files: list[str] = []
        self.funcs: list[FuncInfo] = []
        self.classes: list[tuple[str, str, int, bool]] = []
        self.parse_errors: list[tuple[str, str]] = []
        self.where: dict[str, list[tuple[str, int, bool]]] = defaultdict(list)   # 名字 -> 出现处
        self.imported: set[str] = set()

    def add_text(self, rel: str, text: str) -> None:
        self.text[rel] = text
        if os.path.splitext(rel)[1].lower() not in REF_EXT:
            return                      # 文档不算引用载体（见 REF_EXT 的说明）
        is_test = self.is_test(rel)
        for i, line in enumerate(text.splitlines(), 1):
            for name in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", line):
                self.where[name].append((rel, i, is_test))

    @staticmethod
    def is_test(rel: str) -> bool:
        base = os.path.basename(rel)
        return rel.startswith("tests/") or base.startswith("test_") or "/test_" in rel

    def refs(self, name: str, skip_file: str, skip_line: int) -> tuple[int, int]:
        """(测试之外的引用数, 总引用数)。"""
        non_test = total = 0
        for rel, line, is_test in self.where.get(name, ()):  # noqa: B007
            if rel == skip_file and line == skip_line:
                continue
            total += 1
            if not is_test:
                non_test += 1
        return non_test, total


def scan(root: str) -> Corpus:
    c = Corpus()
    # 一律用目录扫描（不用 git ls-files，理由见 git_files 的注释）
    files = walk_files(root)
    # submodule（moonlight-common-c / pybind11 / googletest …）不是我们的代码：
    # git ls-files 会把它们的内容也列出来，这里按 .gitmodules 里的路径前缀排掉。
    mods = submodule_prefixes(root)
    if mods:
        files = [f for f in files if not any(f == p or f.startswith(p + "/") for p in mods)]
    # ⚠ **不能审计自己的基线**：基线文件里存着形如 `unused_defs|a/b.py Handler.do_GET`
    #   的字符串，一旦被当源码扫进来，这些名字就"被引用"了 → 引用计数改变 →
    #   同一条命令在"有基线/没基线"两种情况下给出不同数字（实测 166 vs 195，
    #   棘轮因此永远报"新增"）。基线是**产物**，不是源码。
    files = [f for f in files if os.path.basename(f) != "audit-baseline.json"]
    for rel in files:
        ext = os.path.splitext(rel)[1].lower()
        if ext not in TEXT_EXT:
            continue
        p = os.path.join(root, rel)
        try:
            if os.path.getsize(p) > MAX_FILE_BYTES:
                continue
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        c.add_text(rel, text)
        if ext != ".py":
            continue
        c.py_files.append(rel)
        is_test = Corpus.is_test(rel)
        try:
            tree = ast.parse(text, filename=rel)
        except SyntaxError as e:
            c.parse_errors.append((rel, "line %s: %s" % (e.lineno, e.msg)))
            continue
        for m in re.finditer(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", text, re.M):
            c.imported.add((m.group(1) or m.group(2)).strip())
        # ⚠ 方法要带类名登记一次就够：先收类体里的方法节点，再在 walk 里跳过它们，
        #   否则每个方法会被登记两次（一次 `Class.m`、一次裸 `m`），
        #   报告里就会出现大量"函数与它自己重复"的假组（实测 430 组里绝大多数是这种）。
        method_nodes, method_cls = set(), {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                c.classes.append((node.name, rel, node.lineno, is_test))
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        method_nodes.add(id(sub))
                        method_cls[id(sub)] = node.name
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                body = [n for n in node.body if not _is_docstring(n)]
                # ⚠ 这里**不**按语句数过滤：短函数也要进索引，否则死代码检查会漏掉它们
                #   （实测踩过：样例里那个 1 条语句的函数根本没被登记）。语句数门槛
                #   只在"重复比较"那一步用（见 analyze().dups）。
                try:
                    fp = fingerprint(node)
                except Exception:                            # noqa: BLE001
                    continue
                c.funcs.append(FuncInfo(node.name, rel, node.lineno, fp, len(body), is_test,
                                        method_cls.get(id(node))))
    return c


def submodule_prefixes(root: str) -> list[str]:
    """读 .gitmodules，返回 submodule 的路径前缀。"""
    p = os.path.join(root, ".gitmodules")
    if not os.path.isfile(p):
        return []
    out = []
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(r"\s*path\s*=\s*(\S+)", line)
                if m:
                    out.append(m.group(1).strip())
    except OSError:
        return []
    return out


def analyze(c: Corpus):
    prod = [f for f in c.funcs if not f.is_test]
    tests = [f for f in c.funcs if f.is_test]

    def dups(items):
        # 语句数门槛只在这里用：太短的函数（`def get(self): return self._x`）当重复是噪音
        by_fp = defaultdict(list)
        for f in items:
            if f.n_stmt < MIN_STMTS_FOR_DUP:
                continue
            by_fp[f.fingerprint].append(f)
        return sorted((sorted("%s:%d %s%s" % (f.file, f.line, f.cls + "." if f.cls else "", f.name)
                              for f in g) for g in by_fp.values() if len(g) > 1),
                      key=len, reverse=True)

    # 同名不同体：只比**模块级**函数（不同类的同名方法是正常设计）
    by_name = defaultdict(list)
    for f in prod:
        if f.cls is None and not f.name.startswith("_"):
            by_name[f.name].append(f)
    # `main` 是入口样板（每个脚本各一个），不是"该收敛的重复"
    same_name = {n: sorted("%s:%d" % (f.file, f.line) for f in v)
                 for n, v in sorted(by_name.items())
                 if n != "main" and len(v) > 1 and len({f.fingerprint for f in v}) > 1}

    # 未引用模块：既没被 import，也没在任何文本里被提到（模块名或路径）
    unused_modules = []
    for rel in c.py_files:
        if rel.startswith(("scripts/", "tests/")) or os.path.basename(rel) == "__init__.py":
            continue
        mod = rel[:-3].replace("/", ".")
        if mod.endswith(".__init__"):
            mod = mod[: -len(".__init__")]
        top = mod.split(".")[0]
        if mod in c.imported or top in c.imported:
            continue
        stem = os.path.basename(rel)[:-3]
        mentioned = any(rel in t or mod in t or ("%s.py" % stem) in t
                        for r, t in c.text.items() if r != rel)
        if mentioned:
            continue
        if "__main__" in c.text.get(rel, ""):
            continue
        unused_modules.append(rel)

    # 无引用定义（生产代码里）
    unused_defs, tests_only = [], []
    for f in prod:
        if f.name.startswith("__") or f.name in ("main", "setup", "run", "app", "handler"):
            continue
        non_test, total = c.refs(f.name, f.file, f.line)
        label = "%s:%d %s%s" % (f.file, f.line, f.cls + "." if f.cls else "", f.name)
        if total == 0:
            unused_defs.append(label)
        elif non_test == 0:
            tests_only.append(label)
    return prod, tests, dups(prod), dups(tests), same_name, unused_modules, unused_defs, tests_only


def heuristics(c: Corpus):
    out = defaultdict(list)
    for rel, text in c.text.items():
        if not rel.endswith(".py"):
            continue
        try:
            tree = ast.parse(text, filename=rel)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and node.type is None:
                out["裸 except（吞掉一切异常）"].append("%s:%d" % (rel, node.lineno))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for d in list(node.args.defaults) + [d for d in node.args.kw_defaults if d]:
                    if isinstance(d, (ast.List, ast.Dict, ast.Set)):
                        out["可变默认参数"].append("%s:%d %s" % (rel, node.lineno, node.name))
                if isinstance(node, ast.AsyncFunctionDef):
                    for sub in ast.walk(node):
                        if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                                and sub.func.attr == "sleep" and isinstance(sub.func.value, ast.Name)
                                and sub.func.value.id == "time"):
                            out["异步函数里用 time.sleep（阻塞事件循环）"].append(
                                "%s:%d %s" % (rel, sub.lineno, node.name))
                span = getattr(node, "end_lineno", node.lineno) - node.lineno + 1
                if span >= LONG_FUNC_LINES:
                    out["超长函数（≥%d 行）" % LONG_FUNC_LINES].append(
                        "%s:%d %s（%d 行）" % (rel, node.lineno, node.name, span))
            if isinstance(node, ast.Call):
                fn = node.func
                if isinstance(fn, ast.Name) and fn.id == "open":
                    if not any(k.arg == "encoding" for k in node.keywords):
                        out["open() 没写 encoding（默认随 locale）"].append("%s:%d" % (rel, node.lineno))
                if isinstance(fn, ast.Attribute) and fn.attr in ("run", "call", "check_output", "Popen"):
                    if any(k.arg == "shell" and getattr(k.value, "value", False) is True
                           for k in node.keywords):
                        out["subprocess 用 shell=True"].append("%s:%d" % (rel, node.lineno))
                if isinstance(fn, ast.Name) and fn.id == "print":
                    if rel.startswith("agent/") and "if __name__" not in text:
                        out["库代码里用 print（绕过日志出口）"].append("%s:%d" % (rel, node.lineno))
        for i, line in enumerate(text.splitlines(), 1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if (len(line) - len(line.lstrip(" "))) // 4 >= DEEP_NEST and \
                    re.match(r"(if|for|while|try|with|elif|else)\b", s):
                out["深嵌套（≥%d 层）" % DEEP_NEST].append("%s:%d" % (rel, i))
        for m in re.finditer(r"\b(TODO|FIXME|HACK|XXX)\b", text):
            out["待办标记 %s" % m.group(1)].append("%s:%d" % (rel, text[: m.start()].count("\n") + 1))
    return {k: sorted(v) for k, v in sorted(out.items())}


def normalize_finding(s: str) -> str:
    """把发现里的行号抹掉，用于棘轮比较。

    为什么必须抹：`file:123 name` 里的行号会随任何无关改动漂移 —— 直接比字符串的话，
    在文件顶部加一行就会让几十条"新问题"蹦出来，棘轮立刻变成噪音源。
    """
    return re.sub(r":\d+", "", s)


def flatten(report: dict) -> set[str]:
    """把报告压成"可比较的发现集合"（供 --baseline 棘轮用）。"""
    out: set[str] = set()
    for key in ("duplicates_prod", "duplicates_tests", "unused_modules", "unused_defs",
                "defs_only_tests"):
        for item in report.get(key, []):
            if isinstance(item, list):
                out.add("%s|%s" % (key, " + ".join(sorted(normalize_finding(x) for x in item))))
            else:
                out.add("%s|%s" % (key, normalize_finding(item)))
    for name, items in report.get("same_name_prod", {}).items():
        out.add("same_name_prod|%s|%s" % (name, " + ".join(sorted(normalize_finding(x) for x in items))))
    for cat, items in report.get("heuristics", {}).items():
        for it in items:
            out.add("heuristic|%s|%s" % (cat, normalize_finding(it)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="代码规范审计的机械普查器（T15-3）")
    ap.add_argument("root", nargs="?", default=".")
    ap.add_argument("--json", dest="json_out", default="")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--top", type=int, default=10, help="每节最多打印多少条")
    ap.add_argument("--all", action="store_true", help="打印全部（等价 --top 很大）")
    ap.add_argument("--baseline", default="",
                    help="棘轮基线（audit-baseline.json）。给了就只报**新增**问题，"
                         "有新增则退出码 2 —— CI 用它拦住「边修边加」")
    ap.add_argument("--write-baseline", default="",
                    help="把当前发现写成基线文件（收敛之后刷新它）")
    args = ap.parse_args()
    root = os.path.abspath(args.root)

    c = scan(root)
    prod, tests, dup_prod, dup_tests, same_name, unused_modules, unused_defs, tests_only = analyze(c)
    heur = heuristics(c)
    py_lines = sum(len(c.text[f].splitlines()) for f in c.py_files)
    native = [f for f in c.text if os.path.splitext(f)[1].lower() in NATIVE_EXT]
    qml = [f for f in c.text if f.endswith(".qml")]

    report = {
        "files": {"py": len(c.py_files), "py_lines": py_lines,
                  "native": len(native), "qml": len(qml), "text_total": len(c.text)},
        "parse_errors": c.parse_errors,
        "duplicates_prod": dup_prod,
        "duplicates_tests": dup_tests,
        "same_name_prod": same_name,
        "unused_modules": unused_modules,
        "unused_defs": unused_defs,
        "defs_only_tests": tests_only,
        "heuristics": heur,
    }
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
    if args.write_baseline:
        with open(args.write_baseline, "w", encoding="utf-8") as f:
            json.dump({"findings": sorted(flatten(report))}, f,
                      ensure_ascii=False, indent=2, sort_keys=True)
        print("已写入棘轮基线 %s（%d 条发现）" % (args.write_baseline, len(flatten(report))))

    # ---- 棘轮：只拦"新增" ----
    if args.baseline:
        try:
            with open(args.baseline, "r", encoding="utf-8") as f:
                base = set(json.load(f).get("findings", []))
        except (OSError, ValueError) as e:
            print("!! 读基线失败: %s" % e, file=sys.stderr)
            return 2
        now = flatten(report)
        new = sorted(now - base)
        gone = sorted(base - now)
        print("== 棘轮（基线 %d 条，当前 %d 条）" % (len(base), len(now)))
        if gone:
            print("   已消掉 %d 条（好方向；收敛后记得用 --write-baseline 刷新基线）" % len(gone))
        if new:
            print("   !! 新增 %d 条：" % len(new))
            for x in new[:30]:
                print("      " + x)
            if len(new) > 30:
                print("      …还有 %d 条" % (len(new) - 30))
            return 2
        print("   没有新增 ✓")

    summary = ("Python %d 文件 / %d 行；原生 %d；QML %d ｜ 重复(生产) %d ／ 重复(测试) %d "
               "／ 同名多体 %d ／ 未引用模块 %d ／ 零引用定义 %d ／ 仅测试引用 %d ／ 启发式 %d"
               % (len(c.py_files), py_lines, len(native), len(qml), len(dup_prod), len(dup_tests),
                  len(same_name), len(unused_modules), len(unused_defs), len(tests_only),
                  sum(len(v) for v in heur.values())))
    if args.quiet:
        print(summary)
        return 0

    top = 10 ** 9 if args.all else args.top

    def show(title, items, fmt=str):
        print("\n== %s（%d）" % (title, len(items)))
        for it in items[:top]:
            print("   " + fmt(it))
        if len(items) > top:
            print("   …还有 %d 条（--all 看全部）" % (len(items) - top))

    print("== 规模")
    print("   Python %d 文件 / %d 行；原生 %d 文件；QML %d 文件；参与扫描的文本 %d"
          % (len(c.py_files), py_lines, len(native), len(qml), len(c.text)))
    if c.parse_errors:
        show("解析失败（语法错）", c.parse_errors, lambda t: "%s %s" % t)

    show("重复实现：生产代码（函数体指纹相同）", dup_prod, lambda g: " | ".join(g))
    show("重复实现：测试（通常不值得收敛）", dup_tests, lambda g: " | ".join(g))
    show("同名不同体：生产模块级函数", list(same_name.items()),
         lambda kv: "%s：%s" % (kv[0], " , ".join(kv[1])))
    show("疑似死代码：从未被 import / 未被任何文本提到", unused_modules)
    show("疑似死代码：全仓库零引用", unused_defs)
    show("仅测试引用（可能是框架回调/字符串注册，需人工确认）", tests_only)

    print("\n== 规范启发式")
    for k, v in heur.items():
        print("   %-36s %4d 条   例：%s" % (k, len(v), v[0] if v else ""))

    print("\n== 汇总")
    print("   " + summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
