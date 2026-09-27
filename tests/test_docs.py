#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_docs.py — 文档守卫：相对链接有效 + 过时声明不得回归

跑法:
    python tests/test_docs.py

检查两件事:

  1) **相对链接必须指向存在的文件**
     `Readme.md` 与 `docs/**/*.md` 里的 `[文本](目标)`，目标是相对路径时，
     必须能相对该 md 所在目录解析到真实文件/目录。
     外链 (`http://` `https://` `mailto:`) 与纯锚点 (`#标题`) 不检查。

  2) **过时声明不得出现**（STALE_CLAIMS 黑名单，大小写不敏感）
     这些是"文档说 A、代码做 B"里已经被清掉的说法。每发现一种新的漂移，
     就往 STALE_CLAIMS 里加一条 —— 这样它就不会再回来。

     条目可以带第三个元素 `exempt`：**行内命中这个正则就不算过时声明**。
     给的是"这句话在讲它已经不在了"的写法 —— 删掉一个东西之后，文档里
     那句"X 已在 T<编号> 删除"是**现状**而不是漂移，没有这个口子就只能
     二选一（要么不记，要么把守卫关掉）。

为什么单独有这个文件
    文档漂移不会让任何测试变红，所以它总是最后才被发现的（Phase 6 就吃过一次：
    `ipc-protocol.md` 说命令用 topic 信封、GUI 实际发 action 信封，两边都"有文档"
    却互相矛盾）。这个守卫把"改代码时顺手改文档"从自觉变成跑得出来的约束。

注意
    扫描范围是**现状类文档**（Readme + docs/）。`todo.md` 是历史记录，里面出现旧词
    （例如 Phase 8 计划里提到的技术选项）是合理的，不在扫描范围内。

    还有一类要排除：**本机独有的文档**。板端 `docs/` 下留着两份旧 GUI 方案
    （它们在板端的 `.git/info/exclude` 里，不属于任何分支）。它们是历史材料，
    说"gui.yaml 是真源"完全正常，不该让守卫变红。判据用 `git check-ignore`：
    被本机忽略的文件不算仓库的文档。
"""

import re
import subprocess
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
#  扫描范围
# ---------------------------------------------------------------------------
#: 现状类文档 (相对仓库根)。todo.md 是历史记录, 故意不在内。
DOC_FILES = ["Readme.md"]
DOC_GLOBS = ["docs/*.md", "docs/**/*.md"]

# ---------------------------------------------------------------------------
#  过时声明黑名单: (正则, 为什么[, 免检正则])
# ---------------------------------------------------------------------------
#: 每条都对应一次真实的漂移。加新条目时写清"为什么它过时了"。
#: 可选的第三项 = 免检正则: 该行命中它就不算过时声明（用于"它已经删了"这种现状陈述）。
STALE_CLAIMS = [
    (r"ZeroMQ",
     "IPC 早就改成同机 Unix domain socket (/tmp/agent.sock) 了, 没有消息队列"),
    (r"\bzmq\b",
     "同上: 没有 zmq 客户端/服务端"),
    (r"PySide6",
     "GUI 是板端的 Qt5 C++ 程序 (gui/src), 不是 PC 上的 PySide6"),
    (r"\b5555\b|\b5556\b",
     "ZeroMQ 时代的端口, 现在这条链路上没有 TCP 端口"),
    (r"/opt/agent",
     "板端仓库路径是 /home/kickpi/myproject/assitant"),
    (r"test-board\.ps1",
     "板端套件由 scripts/run-board-tests.ps1 驱动 (它跑的是共享的 test-python.sh)"),
    (r"实现未做",
     "IPC 的 server/client 都已经实现并在跑"),
    (r"gui\.yaml|gui/config/",
     "GUI 已经没有自己的配置文件了: 界面参数并进 config/config.yaml 的 gui: 段, "
     "gui/config/ 目录连同模板一起删除 (归一化 D 系列)"),
    (r"同步到[^\n]{0,40}llm\.env",
     "llm/config/llm.env 是**派生**文件, 不是被同步的真源 —— "
     "方向只有 config.yaml → llm.env 一个"),
    (r"--today\b|--tomorrow\b",
     "R1 起 CLI 的 schedule 只有 --hours 窗口, 没有'整天'开关了 (--today/--tomorrow 已删)"),
    (r"(换壁纸[^\n]{0,20}(还没接|未接)|next_wallpaper`?\s*/\s*`?next_bilibili[^\n]{0,24}(还没接|Phase 7))",
     "T3 起换壁纸真的有下游了: 命令与 LLM 工具都走 Runtime.next_wallpaper() —— "
     "只有 next_bilibili 还是'还没接入'"),
    (r"list_wallpaper_tags|list_music_library|play_music|control_music|tag_music",
     "T8-5b 把七个工具合并成三个（next_wallpaper / next_music / back_to_desktop）: "
     "旧工具名已经是**历史**, 现状类文档里出现就是漂移 —— 现在写 "
     "next_wallpaper(action=\"tags\") / next_music(action=\"enqueue\")"),
    (r"日程提醒",
     "T12-4 起日程到点**不往对话里发消息**: 它只切状态 + 推一行展示文本"
     "（`日程到点：切到 STUDY`）, 模型看不到。没有「日程提醒：<标题>」这种推送了"),
    (r"kind=fired title=",
     "T12-4 起日程事实里是 `state`（没有 `title` 了）: `assistant watch` 打的那行是 "
     "`kind=fired state=study date=… scheduled_at=… fired_at=…`"),
    (r"SigLIPEncoder",
     "T13-1 删掉了那个空接口: 视觉层只有 agent/vision/siglip/（真 RKNN 双塔）一条路。"
     "现状类文档里再出现 SigLIPEncoder 就是漂移 —— 实时帧要 embedding 就用 "
     "SiglipModel.from_config(cfg[\"vision\"]) + encode_image(frame)",
     r"已删|删掉|删除|移除|不在了|废弃"),
]


def _entry_hits(entry, line):
    """这一行是否命中**这一条**过时声明（带免检词的行一律不算）。

    抽成函数是为了能对本机制本身写测试 —— 一个写得太宽的免检正则会让条目
    静默失效（守卫永远绿），那比没有守卫更糟。
    """
    pattern = entry[0]
    exempt = entry[2] if len(entry) > 2 else None
    if exempt and re.search(exempt, line, re.IGNORECASE):
        return False
    return bool(re.search(pattern, line, re.IGNORECASE))


def _locally_ignored(paths):
    """返回其中被**本机** git 忽略的那些（`.gitignore` + `.git/info/exclude`）。

    git 不在 / 不是仓库时返回空集合 —— 那就退化成"全都扫"，与过去的行为一致。
    """
    if not paths:
        return set()
    try:
        proc = subprocess.run(
            ["git", "check-ignore", "--stdin"],
            cwd=str(_PROJECT_ROOT),
            input="\n".join(str(p) for p in paths),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            universal_newlines=True,
        )
    except (OSError, ValueError):
        return set()
    if proc.returncode not in (0, 1):        # 128 = 不是 git 仓库
        return set()
    return {Path(line.strip()).resolve() for line in proc.stdout.splitlines() if line.strip()}


def _doc_paths():
    """收集要扫描的 md 文件 (存在才收, 排序稳定)。本机忽略的不算。"""
    out = []
    for rel in DOC_FILES:
        p = _PROJECT_ROOT / rel
        if p.is_file():
            out.append(p)
    for pattern in DOC_GLOBS:
        out.extend(p for p in _PROJECT_ROOT.glob(pattern) if p.is_file())
    # 去重 + 稳定顺序
    paths = sorted(set(out))
    ignored = _locally_ignored(paths)
    return [p for p in paths if p.resolve() not in ignored]


#: markdown 链接: [文本](目标)。目标里不含 ')' 就够用了 (本仓库没有带括号的路径)。
_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")

#: 不检查的目标: 外链 / 纯锚点 / 空
_SKIP_PREFIX_RE = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.\-]*:|#|$)")

#: **ADR 不参与"过时声明"检查**（链接仍然要有效）。
#:
#: 为什么: ADR 的工作就是记"当时为什么没选另一条路" —— 里面**必然**出现 ZMQ / PySide6
#: 这些被否决的东西（`docs/adr/0001-ipc-unix-socket.md` 的"替代方案与代价"整节就是讲它们）。
#: 那不是在声称"现状还是 ZMQ"，恰恰相反。所以判据用**路径**，而不是"这一行怎么措辞"——
#: 后者要靠给每条黑名单加免检正则，越加越松，最后等于把守卫关了。
#:
#: 这个口子不会变成后门: `docs/adr/` 下的文件另有一条守卫（`tests/test_adr.py`）
#: 强制它长得像 ADR（编号命名 / 必需小节 / 索引登记），不能拿来塞普通文档。
ADR_PREFIX = "docs/adr/"


class TestDocLinksExist(unittest.TestCase):
    """相对链接必须能解析到存在的文件。"""

    def test_relative_links_resolve(self):
        broken = []
        for path in _doc_paths():
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), 1):
                for target in _LINK_RE.findall(line):
                    target = target.strip().strip("<>")
                    if _SKIP_PREFIX_RE.match(target):
                        continue          # http(s): / mailto: / #anchor
                    # 去掉锚点与可选标题: "a.md#x" / 'a.md "标题"'
                    rel = target.split("#", 1)[0].split(None, 1)[0].strip()
                    if not rel:
                        continue
                    resolved = (path.parent / rel).resolve()
                    if not resolved.exists():
                        broken.append("%s:%d  ->  %s" % (
                            path.relative_to(_PROJECT_ROOT).as_posix(), lineno, target))

        if broken:
            self.fail("有 %d 个相对链接指向不存在的文件:\n  %s"
                      % (len(broken), "\n  ".join(broken)))


class TestNoStaleClaims(unittest.TestCase):
    """已被清掉的旧说法不得回来（ADR 除外，见 ADR_PREFIX 的说明）。"""

    def test_stale_claims_are_gone(self):
        found = []
        for path in _doc_paths():
            rel = path.relative_to(_PROJECT_ROOT).as_posix()
            if rel.startswith(ADR_PREFIX):
                continue          # ADR 记的就是"被否决的方案"，见 ADR_PREFIX 的说明
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), 1):
                for entry in STALE_CLAIMS:
                    if _entry_hits(entry, line):
                        found.append("%s:%d  命中 /%s/\n      %s\n      原因: %s"
                                     % (rel, lineno, entry[0], line.strip(), entry[1]))

        if found:
            self.fail("文档里出现了 %d 处过时声明 (改掉, 或如果是合理例外就调整"
                      " STALE_CLAIMS 并说明原因):\n  %s"
                      % (len(found), "\n  ".join(found)))

    def test_adr_exemption_is_scoped_to_adr_files(self):
        """反空转: ADR 那条免检只按**路径**生效 —— 别的地方说"现状还是 ZMQ"照样要被抓。

        四步把机制钉住: ① 黑名单对 zmq 仍有牙; ② 路径判据只为 `docs/adr/` 开头放行;
        ③ ADR 里**确实**有这些词（否则这条豁免没有存在理由）; ④ ADR 确实在扫描范围内
        （否则豁免是多余的）。
        """
        entry = next(e for e in STALE_CLAIMS if e[0] == r"\bzmq\b")
        self.assertTrue(_entry_hits(entry, "IPC 还是 zmq 的 pub_bind（现状）"),
                        "黑名单对 zmq 没牙了")

        self.assertTrue("docs/adr/0001-ipc-unix-socket.md".startswith(ADR_PREFIX))
        self.assertFalse("docs/architecture.md".startswith(ADR_PREFIX))

        adr_paths = sorted((_PROJECT_ROOT / ADR_PREFIX).glob("*.md"))
        self.assertTrue(adr_paths, "docs/adr/ 下没有文件 —— 免检规则成了空话")
        text = "".join(p.read_text(encoding="utf-8") for p in adr_paths)
        for probe in ("zmq", "PySide6"):
            self.assertIn(probe, text, "ADR 里没有 %r —— 那这条豁免就没有存在理由，删掉它" % probe)

        scanned = {p.relative_to(_PROJECT_ROOT).as_posix() for p in _doc_paths()}
        self.assertTrue(any(rel.startswith(ADR_PREFIX) for rel in scanned),
                        "ADR 根本没被扫描 —— 那这个豁免是多余的")

    def test_exempt_marker_only_waives_marked_lines(self):
        """免检正则的语义: 只有**写了免检词**的行被放过, 别的行照样命中。"""
        fake = (r"FooBar", "假条目: 只用来验机制", r"已删|删掉|删除")
        self.assertTrue(_entry_hits(fake, "实时帧那条路还是 FooBar (⚠ 仍是 mock)"))
        self.assertFalse(_entry_hits(fake, "FooBar 已在 T13-1 删除"))

    def test_the_siglip_entry_actually_has_teeth(self):
        """真实条目自己也要有牙: 一条"它还在"的描述必须被抓到。"""
        entry = next(e for e in STALE_CLAIMS if e[0] == r"SigLIPEncoder")
        self.assertTrue(_entry_hits(entry, "实时帧那条路是 SigLIPEncoder (⚠ 仍是 mock)"))
        self.assertFalse(_entry_hits(entry, "SigLIPEncoder 已在 T13-1 删除"))

    def test_the_scan_actually_covers_something(self):
        """防止正则写错导致"零命中"这种假绿灯。"""
        paths = _doc_paths()
        self.assertGreaterEqual(len(paths), 5, "扫描到的 md 太少: %r" % (paths,))
        names = {p.name for p in paths}
        for must in ("Readme.md", "ipc-protocol.md", "architecture.md", "config-sources.md"):
            self.assertIn(must, names, "扫描范围漏了 %s" % must)


class TestLinksAreActuallyExtracted(unittest.TestCase):
    """同理: 证明链接提取真的抓到了东西（否则 test_relative_links_resolve 永远绿）。"""

    def test_found_some_links(self):
        total = 0
        for path in _doc_paths():
            total += len(_LINK_RE.findall(path.read_text(encoding="utf-8")))
        self.assertGreater(total, 0, "一个 markdown 链接都没提取到, 正则或文档有问题")


if __name__ == "__main__":
    unittest.main(verbosity=2)
