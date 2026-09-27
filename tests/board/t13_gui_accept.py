#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T13-10 门禁实测: **设置页三张卡片的板端 GUI 验收**（真 Qt5 / 真控件 / 真保存）。

验的是"真 GUI 真的把这几段按你点的值写进真源了，而且**只动它该动的键**"。四块:

  A. **前置**: 仓库根 / 真配置 / `config.example.yaml` / `agent_gui` 二进制都在;
     板端 `ctest` 全绿（GUI 单测 23 个测试目标）。
  B. **副本**上跑"有区分度"的一组值（`--settings-cards-demo`）—— 证明"控件 -> 文件"这条路通了,
     而不是把模板默认值抄了一遍:
       · `study:` / `bilibili:` / `profile:` 三段一次长出来;
       · 值 == 卡片上那组（逐键比对）;
       · **GUI 自己读回来**（`--settings-dump-cards`）与文件里的值一致;
       · 逐行 opcode 比对: 改动的行只能是白名单里的键; **新增**的行只能落在"新建的那几段"里;
         **一行都不许删**（结构级内容因此逐字节不动）;
       · `.bak` == 保存前原文; 没有 `.tmp` 残留。
  C. **真配置**（要 `--apply-live`）: 跑 `--settings-final-demo`（三段 = 模板默认值 + 学习监督开启）:
       · 三段都在、值与 GUI 读回来的一致;
       · 逐行 opcode 比对同上; `.bak` == 保存前原文;
       · **再跑一次** -> 文件内容不变（幂等: 值没变就不写、也不留新 `.bak`）;
       · 不变量: 派生数据（`study_anchors.jsonl` / `study_stats.json`）与
         **凭据文件**（`bilibili_cookie.json`）md5 未变; `git status` 干净; 没留进程。
  D. **截图取证**: 页顶 / 学习监督 / 游戏检测 / 画像压缩 四张 PNG（`--settings-scroll-demo`）。

跑法（板端, 仓库根）::

    python3 tests/board/t13_gui_accept.py                  # A/B/D（**不写真配置**）
    python3 tests/board/t13_gui_accept.py --apply-live      # 连 C 一起（真在真源上点保存）
    python3 tests/board/t13_gui_accept.py --dump-cards      # 只打印 GUI 读到的卡片值
    python3 tests/board/t13_gui_accept.py --json            # 末尾多打一段机器可读的汇总

⚠ 这是**验收脚本**, 不是生产路径: 它只驱动设置页的控件与"保存"按钮, **不手写 YAML**。
   C 段会在真源旁边留一份 `.bak`; 想回滚就 `cp config/config.yaml.bak config/config.yaml`。
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GUI = os.path.join(REPO, "gui", "build", "agent_gui")
CONFIG = os.path.join(REPO, "config", "config.yaml")
TEMPLATE = os.path.join(REPO, "config", "config.example.yaml")

#: 卡片上"有区分度"的那组值（B 段; 与 `gui/src/main.cpp` 的 `--settings-cards-demo` 同源）。
CARDS_DEMO = {
    "study.enabled": "true",
    "study.focus_interval_min": "25",
    "study.recheck_interval_min": "6",
    "study.max_failures": "4",
    "study.cooldown_min": "20",
    "study.cooldown_probe_min": "2",
    "study.relative_band": "0.06",
    "bilibili.game_watch.enabled": "false",
    "bilibili.game_watch.interval_s": "50",
    "bilibili.game_watch.confident_score": "0.88",
    "profile.enabled": "false",
    "profile.trigger_chars": "1500",
    "profile.trigger_turns": "8",
}

#: 收尾（C 段）: 三段落在模板默认值上 + 学习监督开启 —— 你 2026-09-27 定的最终状态。
FINAL_VALUES = {
    "study.enabled": "true",
    "study.focus_interval_min": "30",
    "study.recheck_interval_min": "5",
    "study.max_failures": "3",
    "study.cooldown_min": "30",
    "study.cooldown_probe_min": "1",
    "study.relative_band": "0.05",
    "bilibili.game_watch.enabled": "true",
    "bilibili.game_watch.interval_s": "60",
    "bilibili.game_watch.confident_score": "0.82",
    "profile.enabled": "true",
    "profile.trigger_chars": "2000",
    "profile.trigger_turns": "12",
}

#: 设置页**准许动**的键（与 `gui/tests/test_settings_page.cpp::isWhitelistedKey` 同一张名单）。
WHITELIST_PREFIXES = ("gui.", "llm.")
WHITELIST_EXACT = set(FINAL_VALUES) | {
    "bilibili.cookie_file",
}

#: 三段（"新键只能落在这里面，而且要那一整段本来就是新加的"）。
CARD_SECTIONS = ("study", "bilibili", "profile")

#: 跑完必须一个字节没变的文件（真配置**不**在内: C 段就是要改它，但它留 `.bak`）。
GUARDED_FILES = [
    "config/study_anchors.jsonl",
    "config/study_stats.json",
    "config/bilibili_cookie.json",
]

_KEY_RE = re.compile(r"^(\s*)([^:#]+?)\s*:(.*)$")

_PASSED = []
_FAILED = []
_SCREENSHOTS = []


# ---------------------------------------------------------------------------
#  小工具（与 t13_accept.py 同款）
# ---------------------------------------------------------------------------
def check(name, ok, detail=""):
    (_PASSED if ok else _FAILED).append(name)
    print("  %s %s%s" % ("✔" if ok else "✘", name, ("  —— %s" % detail) if detail else ""))
    return bool(ok)


def note(text):
    print("     %s" % text)


def md5(path):
    if not os.path.exists(path):
        return "(不在)"
    with open(path, "rb") as handle:
        return hashlib.md5(handle.read()).hexdigest()


def read_text(path):
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return handle.read()


def run(cmd, timeout=300, env_extra=None, cwd=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(cmd, cwd=cwd or REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          universal_newlines=True, timeout=timeout, env=env)
    return proc.returncode, (proc.stdout or "")


def gui_env():
    """板端经 SSH 跑时没有 DISPLAY -> 用 offscreen；有显示就照常用。"""
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return {}
    return {"QT_QPA_PLATFORM": "offscreen"}


def run_gui(args, config, timeout=120):
    cmd = [GUI, "--config", config] + list(args)
    return run(cmd, timeout=timeout, env_extra=gui_env())


def cookie_values():
    """凭据文件里的值（坏文件/不在 -> 空字典; 只用来做"不回显"的检查）。"""
    path = os.path.join(REPO, "config", "bilibili_cookie.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def dump_cards(config):
    """GUI 自己读回来的卡片值（`CARD\\t键=值`）。"""
    rc, out = run_gui(["--settings-dump-cards"], config)
    values = {}
    for line in out.splitlines():
        if not line.startswith("CARD\t"):
            continue
        body = line[len("CARD\t"):]
        if "=" not in body:
            continue
        key, value = body.split("=", 1)
        values[key.strip()] = value.strip()
    return rc, values, out


# ---------------------------------------------------------------------------
#  YAML 文本视图（够用即可: 只认 `键: 值` 与缩进栈; 与 GUI 那份解析同口径）
# ---------------------------------------------------------------------------
def _strip_comment(value):
    in_single = in_double = False
    for index, char in enumerate(value):
        if in_double and char == "\\":
            continue
        if char == '"' and not in_single:
            in_double = not in_double
        elif char == "'" and not in_double:
            in_single = not in_single
        elif char == "#" and not in_single and not in_double and index > 0 \
                and value[index - 1] == " ":
            return value[:index]
    return value


def parse_scalars(text):
    """`{点号路径: 裸值}`（映射条目里的点也算路径的一部分 —— 只用来比对我们那几个键）。"""
    out = {}
    stack = []
    for raw in text.split("\n"):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("-"):
            continue
        match = _KEY_RE.match(raw)
        if not match:
            continue
        indent = len(match.group(1))
        key = match.group(2).strip()
        value = _strip_comment(match.group(3)).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        while stack and stack[-1][0] >= indent:
            stack.pop()
        path = ".".join([item[1] for item in stack] + [key])
        if value == "":
            stack.append((indent, key))
        else:
            out[path] = value
    return out


def top_level_keys(lines):
    out = []
    for line in lines:
        match = _KEY_RE.match(line)
        if match and len(match.group(1)) == 0:
            out.append(match.group(2).strip())
    return out


def line_keys(lines):
    """每行的点号键路径（注释/空行 -> None）—— 靠缩进往上找父容器。"""
    mapping = {}
    for index, line in enumerate(lines):
        match = _KEY_RE.match(line)
        if not match or line.strip().startswith("#"):
            mapping[index] = None
            continue
        indent = len(match.group(1))
        parts = [match.group(2).strip()]
        want = indent - 2
        for back in range(index - 1, -1, -1):
            if want < 0:
                break
            other = _KEY_RE.match(lines[back])
            if not other or lines[back].strip().startswith("#"):
                continue
            other_indent = len(other.group(1))
            if other_indent == want:
                parts.insert(0, other.group(2).strip())
                want -= 2
            elif other_indent < want:
                break
        mapping[index] = ".".join(parts)
    return mapping


def is_whitelisted(key):
    if key is None:
        return True                      # 空行/注释行: 跟着它所在的那次改动一起算
    if key.startswith(WHITELIST_PREFIXES):
        return True
    return key in WHITELIST_EXACT


def diff_report(before_text, after_text):
    """逐行 opcode 比对 -> (改了哪些键, 新加了哪些键, 被删的行, 问题描述列表)。"""
    before_lines = before_text.split("\n")
    after_lines = after_text.split("\n")
    before_keys = line_keys(before_lines)
    after_keys = line_keys(after_lines)
    before_sections = set(top_level_keys(before_lines))
    after_sections = set(top_level_keys(after_lines))
    new_sections = after_sections - before_sections

    changed, added, removed, problems = [], [], [], []
    matcher = difflib.SequenceMatcher(None, before_lines, after_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "delete" or (tag == "replace" and (i2 - i1) > (j2 - j1)):
            removed.extend(before_lines[i1:i2])
            problems.append("删了行: %r" % (before_lines[i1:i2][:2],))
            continue
        if tag == "replace":
            for index in range(i1, i2):
                key = before_keys.get(index)
                changed.append(key)
                if not is_whitelisted(key):
                    problems.append("改了白名单之外的键: %s" % key)
            continue
        # insert
        for index in range(j1, j2):
            key = after_keys.get(index)
            if key is None:
                continue                  # 插入块里的空行/说明注释
            section = key.split(".")[0]
            if is_whitelisted(key):
                added.append(key)
                continue
            if section in new_sections and section in CARD_SECTIONS:
                added.append(key)         # 新建段的其它默认键（classes / process_names …）
                continue
            problems.append("加了白名单之外的键: %s（段 %s 不是新建的）" % (key, section))
    return changed, added, removed, problems, sorted(new_sections)


# ---------------------------------------------------------------------------
#  A. 前置
# ---------------------------------------------------------------------------
def part_a(args):
    print("\n== A. 前置（仓库 / 二进制 / 模板 / ctest）")
    check("仓库根看起来对（config/ 与 gui/ 都在）",
          os.path.isdir(os.path.join(REPO, "config")) and os.path.isdir(os.path.join(REPO, "gui")))
    check("真配置在: config/config.yaml", os.path.isfile(CONFIG), CONFIG)
    check("模板在: config/config.example.yaml", os.path.isfile(TEMPLATE), TEMPLATE)
    check("GUI 二进制在: gui/build/agent_gui（要先 cmake --build）", os.path.isfile(GUI), GUI)
    check("GUI 有 T13-10 的取证开关（--settings-dump-cards）",
          "--settings-dump-cards" in run([GUI, "--help"], timeout=60, env_extra=gui_env())[1])
    if args.no_ctest:
        note("⊘ 板端 ctest（--no-ctest 跳过）")
    else:
        # ⚠ 要在构建目录里跑（板端 cmake 3.16 没有 `ctest --test-dir`）
        rc, out = run(["ctest", "--output-on-failure"], timeout=900,
                      env_extra={"QT_QPA_PLATFORM": "offscreen"},
                      cwd=os.path.join(REPO, "gui", "build"))
        tail = [line for line in out.splitlines() if "tests passed" in line]
        check("板端 ctest 全绿（gui/build）", rc == 0 and "100% tests passed" in out,
              tail[-1] if tail else out.strip()[-120:])
    return os.path.isfile(GUI) and os.path.isfile(CONFIG) and os.path.isfile(TEMPLATE)


# ---------------------------------------------------------------------------
#  B. 副本上的"控件 -> 文件"
# ---------------------------------------------------------------------------
def part_b(tmp):
    print("\n== B. 副本: 卡片的值真的写进文件了（且只动该动的键）")
    work = os.path.join(tmp, "copy")
    os.makedirs(work, exist_ok=True)
    copy_config = os.path.join(work, "config.yaml")
    shutil.copy2(CONFIG, copy_config)
    shutil.copy2(TEMPLATE, os.path.join(work, "config.example.yaml"))
    before_text = read_text(copy_config)
    before_sections = set(top_level_keys(before_text.split("\n")))
    note("副本保存前已有段: %s" % (", ".join(sorted(before_sections)) or "(空)"))

    rc, out = run_gui(["--windowed", "--page", "settings", "--settings-cards-demo",
                       "--screenshot", os.path.join(work, "cards.png"),
                       "--screenshot-delay", "3000"], copy_config, timeout=180)
    check("副本: 走真实「保存」按钮跑完（退出码 0）", rc == 0, out.strip()[-100:])
    check("副本: 日志里有“配置已保存”", "配置已保存" in out)
    after_text = read_text(copy_config)

    after_sections = set(top_level_keys(after_text.split("\n")))
    for section in CARD_SECTIONS:
        check("副本: `%s:` 段在（%s）" % (section, "本次新建" if section not in before_sections
                                         else "保存前就有"),
              section in after_sections)

    rc, dumped, dump_out = dump_cards(copy_config)
    check("副本: GUI 读回来的值可用（--settings-dump-cards）", rc == 0 and bool(dumped),
          dump_out.strip()[-100:])
    for key, want in CARDS_DEMO.items():
        got = dumped.get(key)
        same = got is not None and abs(float(got) - float(want)) < 1e-9
        check("副本: GUI 读回 %s == %s（区分度那组）" % (key, want), same, "读回 %s" % got)

    parsed = parse_scalars(after_text)
    mismatched = []
    for key, want in CARDS_DEMO.items():
        got = parsed.get(key)
        if got is None or abs(float(got) - float(want)) > 1e-9:
            mismatched.append("%s=%s(要 %s)" % (key, got, want))
    check("副本: 文件里的值 == 卡片上那组（14 个键）", not mismatched, "；".join(mismatched))
    check("副本: GUI 读回的值 == 文件里的值",
          all(abs(float(dumped[key]) - float(parsed[key])) < 1e-9
              for key in CARDS_DEMO if key in dumped and key in parsed))

    changed, added, removed, problems, new_sections = diff_report(before_text, after_text)
    check("副本: 改动的行都是白名单键（%d 个键）" % len([k for k in changed if k]), not problems,
          "；".join(problems[:4]))
    check("副本: 没有删行（结构级内容因此逐字节不动）", not removed)
    note("副本新建的段: %s；改动的键: %s"
         % (", ".join(new_sections) or "无", ", ".join(sorted(set(k for k in changed if k)))))

    check("副本: `.bak` == 保存前原文",
          os.path.isfile(copy_config + ".bak") and read_text(copy_config + ".bak") == before_text)
    leftovers = [name for name in os.listdir(work) if name.endswith(".tmp")]
    check("副本: 没留下 .tmp", not leftovers, "；".join(leftovers))
    return copy_config


# ---------------------------------------------------------------------------
#  C. 真配置
# ---------------------------------------------------------------------------
def part_c(args):
    print("\n== C. 真配置: 三段落盘 + 学习监督开启（--apply-live）")
    live_before_text = read_text(CONFIG)
    before_md5 = md5(CONFIG)
    before_sections = set(top_level_keys(live_before_text.split("\n")))
    note("真配置 md5（保存前）: %s" % before_md5)
    note("真配置保存前已有段: %s" % (", ".join(sorted(before_sections)) or "(空)"))

    rc, out = run_gui(["--windowed", "--page", "settings", "--settings-final-demo",
                       "--screenshot", os.path.join(args.tmp, "live_saved.png"),
                       "--screenshot-delay", "3000"], CONFIG, timeout=180)
    check("真配置: 走真实「保存」按钮跑完（退出码 0）", rc == 0, out.strip()[-100:])
    check("真配置: 日志里有“配置已保存”", "配置已保存" in out)

    after_text = read_text(CONFIG)
    after_sections = set(top_level_keys(after_text.split("\n")))
    for section in CARD_SECTIONS:
        check("真配置: `%s:` 段在（%s）" % (section, "本次新建" if section not in before_sections
                                          else "保存前就有"),
              section in after_sections)
    check("真配置: 真的变了（md5 %s -> %s）" % (before_md5, md5(CONFIG)), md5(CONFIG) != before_md5)

    rc, dumped, dump_out = dump_cards(CONFIG)
    check("真配置: GUI 读回来可用", rc == 0 and bool(dumped), dump_out.strip()[-100:])
    bad = []
    for key, want in FINAL_VALUES.items():
        got = dumped.get(key)
        if got is None or abs(float(got) - float(want)) > 1e-9:
            bad.append("%s=%s(要 %s)" % (key, got, want))
    check("真配置: 卡片值 == 约定值（三段默认 + 学习监督开）", not bad, "；".join(bad))

    parsed = parse_scalars(after_text)
    bad = []
    for key, want in FINAL_VALUES.items():
        got = parsed.get(key)
        if got is None or abs(float(got) - float(want)) > 1e-9:
            bad.append("%s=%s(要 %s)" % (key, got, want))
    check("真配置: 文件里的值 == 约定值", not bad, "；".join(bad))

    changed, added, removed, problems, new_sections = diff_report(live_before_text, after_text)
    check("真配置: 改动的行都是白名单键", not problems, "；".join(problems[:4]))
    check("真配置: 没有删行", not removed)
    note("真配置改动的键: %s" % (", ".join(sorted(set(k for k in changed if k))) or "(无)"))
    note("真配置新增的键: %d 个（都落在新建的段 %s 里）" % (len(added),
                                                        ", ".join(new_sections) or "无"))

    bak = CONFIG + ".bak"
    check("真配置: `.bak` == 保存前原文（回滚材料）",
          os.path.isfile(bak) and read_text(bak) == live_before_text, bak)

    # 幂等: 值都没变 -> 不写文件、也不留新 .bak
    tmp_bak = os.path.join(args.tmp, "live.bak.copy")
    if os.path.isfile(bak):
        shutil.copy2(bak, tmp_bak)
    after_first = read_text(CONFIG)
    rc, out = run_gui(["--windowed", "--page", "settings", "--settings-final-demo",
                       "--screenshot", os.path.join(args.tmp, "live_again.png"),
                       "--screenshot-delay", "3000"], CONFIG, timeout=180)
    check("真配置: 再保存一次什么都不变（幂等）", rc == 0 and read_text(CONFIG) == after_first)
    if os.path.isfile(tmp_bak) and os.path.isfile(bak):
        check("真配置: 第二次没覆盖 .bak（值没变就不写）",
              read_text(bak) == read_text(tmp_bak))
    return changed


# ---------------------------------------------------------------------------
#  D. 截图取证 + 汇总
# ---------------------------------------------------------------------------
def part_d(tmp):
    print("\n== D. 截图取证（页顶 / 学习监督 / 游戏检测 / 画像压缩）")
    shots = [("01_top", 0), ("02_study", 520), ("03_game", 1080), ("04_profile", 1620)]
    for name, offset in shots:
        path = os.path.join(tmp, "%s.png" % name)
        rc, out = run_gui(["--windowed", "--page", "settings",
                           "--settings-scroll-demo", str(offset),
                           "--screenshot", path, "--screenshot-delay", "2600"], CONFIG,
                          timeout=180)
        size = os.path.getsize(path) if os.path.isfile(path) else 0
        check("截图 %s.png（滚动 %d px, %d 字节）" % (name, offset, size),
              rc == 0 and size > 10 * 1024, out.strip()[-80:])
        _SCREENSHOTS.append(path)
    return _SCREENSHOTS


def part_e(args):
    print("\n== E. 不变量（派生数据 / 凭据 / 仓库 / 进程）")
    for name in GUARDED_FILES:
        path = os.path.join(REPO, name)
        digest = args.guarded.get(name, "(不在)")
        check("%s 一个字节没动" % name, md5(path) == digest, md5(path))
    rc, out = run(["git", "status", "--porcelain"], timeout=30)
    check("板端 git status 干净（真配置与派生数据都被忽略）", rc == 0 and not out.strip(),
          out.strip()[:100])
    leftovers = []
    for name in ("agent_gui", "moonlight", "llama-server"):
        rc, out = run(["pgrep", "-a", name], timeout=10)
        if rc == 0 and out.strip():
            leftovers.append("%s: %s" % (name, out.strip().splitlines()[0][:60]))
    check("没留下 agent_gui / moonlight / llama-server 进程", not leftovers, "；".join(leftovers))

    # 凭据只在界面上以**掩码**出现（真值不许进 dump/截图）
    secret = cookie_values().get("SESSDATA")
    rc, dumped, dump_out = dump_cards(CONFIG)
    if secret:
        check("凭据不回显（dump 里没有 SESSDATA 原值）", secret not in dump_out)
        check("凭据以掩码出现（dump 里有 …）",
              "…" in dumped.get("bilibili.credentials", ""), dumped.get("bilibili.credentials", ""))
    else:
        note("⊘ 凭据文件里没有 SESSDATA（匿名），跳过掩码检查")


# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply-live", action="store_true",
                        help="在**真** config/config.yaml 上点保存（会留 .bak）")
    parser.add_argument("--no-ctest", action="store_true", help="跳过板端 ctest")
    parser.add_argument("--dump-cards", action="store_true", help="只打印 GUI 读到的卡片值")
    parser.add_argument("--json", action="store_true", help="末尾多打一段机器可读的汇总")
    args = parser.parse_args()

    if args.dump_cards:
        rc, dumped, out = dump_cards(CONFIG)
        if rc != 0 or not dumped:
            print(out)
            return 2
        for key in sorted(dumped):
            print("%-40s %s" % (key, dumped[key]))
        return 0

    args.tmp = tempfile.mkdtemp(prefix="t13-gui-")
    args.guarded = {name: md5(os.path.join(REPO, name)) for name in GUARDED_FILES}
    print("== 学习内容监督 · 设置页三张卡片的板端 GUI 验收（T13-10）")
    print("== 仓库: %s" % REPO)
    print("== 临时目录: %s" % args.tmp)
    print("== 真配置: %s（%s）" % (CONFIG, "本段会写真源" if args.apply_live else "只读，不写"))

    ok = part_a(args)
    if not ok:
        print("\n== 结果: 前置不满足，后面不跑")
        return 2

    if args.apply_live:
        part_b(args.tmp)
        part_c(args)
    else:
        note("⊘ B/C 段需要 --apply-live（只跑 A/D 与汇总）")
        note("（不加 --apply-live 时连副本都不试 —— 免得给你一种“验过了”的错觉）")

    part_e(args)
    part_d(args.tmp)

    print("\n== 结果: %s（%d 项通过%s）"
          % ("全部通过" if not _FAILED else "失败 %d 项" % len(_FAILED), len(_PASSED),
             (", 失败: " + "、".join(_FAILED)) if _FAILED else ""))
    if _SCREENSHOTS:
        print("== 截图: %s" % "、".join(_SCREENSHOTS))
    if args.json:
        print(json.dumps({"passed": _PASSED, "failed": _FAILED, "screenshots": _SCREENSHOTS,
                          "config_md5": md5(CONFIG), "live": bool(args.apply_live)},
                         ensure_ascii=False, indent=2))
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
