#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_systemd_units.py — systemd 单元守卫（T14-7）

跑法:
    python tests/test_systemd_units.py

为什么要有这个守卫
    T14-7 的第一次交付在板端**看起来完全正常**：`systemctl enable` 成功、手动
    `systemctl start` 也成功、单元文件 `systemd-analyze verify` 不报错 —— 但重启之后
    `agent-gui.service` 是 `inactive (dead)`、`MainPID=0`、journal 里连一条
    "Starting ..." 都没有。两个原因都不是"服务起不来"，而是**单元文件写错**：

      1) **排序成环**：GUI 写了 `After=xrandr-startup.service`，而板端那个 vendor 单元
         自己 `After=graphical.target` 又 `WantedBy=graphical.target`（自相矛盾）。
         systemd 破环的手段是**删掉环里的启动作业** —— 于是表现为"没启动"而不是"失败"。
      2) **`StartLimitIntervalSec` 写在 `[Service]`**：systemd 245（Ubuntu 20.04）里它
         属于 `[Unit]`，写错只会得到一条 `Unknown key name ... ignoring` 警告 ——
         以为配了重启限流，其实没有。

    这两类错误的共同点是：**静默**。不会让任何测试变红、不会让 `verify` 报错、只会让
    开机的行为与你以为的不一样。所以这里把"单元文件的形状"变成跑得出来的检查。

检查什么（都是机械可判的）
  1) 三个单元文件都在、都是 **UTF-8 + LF + 末尾换行**（板端 `while read` 与 `cp` 都吃过 CRLF 的亏）；
  2) **`[Service]` 里不许出现只属于 `[Unit]` 的键**（StartLimit* / After / Before / Wants / …）；
  3) **不许有"WantedBy=T 又 After=T"**（xrandr 那个 bug 的通用形式：自相矛盾的双向排序）；
  4) **把三个单元放到一起做一次排序环检测**（显式 After/Before + 目标层的隐式排序），
     并额外钉住 `multi-user.target` 在 `graphical.target` 之前这条 systemd 不变式；
  5) **GUI 单元必须等 X、也要等转屏**（`After=display-manager.service` +
     `After=xrandr-startup.service` + `ExecStartPre` 里轮询 `/tmp/.X11-unix/X0`）；
  6) **GUI 单元不许再写 `PartOf=graphical.target`**（T14-7 取证后去掉的那一行：
     DM 停/重启由 `Requires=display-manager.service` 带，PartOf 只多一层成环风险）；
  7) `docs/deploy.md` 的安装片段**必须点名三个单元文件**（只装两个就会成环，
     这正是当时漏掉的那一步）。

不检查（刻意的）
  · 不检查板端**实际**的开机行为 —— 那只能在板端重启一次看（`journalctl -b | grep -i
    'ordering cycle'` 必须为空）。这个文件只保证"送去板端的那份文件不是已知的坏形状"。
  · 不检查 systemd 版本差异的其它细节；只钉 T14-7 踩到的那几条。
"""

import re
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
SYSTEMD_DIR = _PROJECT_ROOT / "systemd"
DEPLOY_DOC = _PROJECT_ROOT / "docs" / "deploy.md"

AGENT = "agent.service"
GUI = "agent-gui.service"
XRANDR = "xrandr-startup.service"
UNIT_FILES = [AGENT, GUI, XRANDR]

#: 只允许出现在 `[Unit]` 的键（写进 `[Service]` 会被静默忽略 —— systemd 245 实测）。
#: 来源: systemd.unit(5) 的 `[Unit]` 段 + T14-7 板端 journal 里的那条 warning。
UNIT_ONLY_KEYS = {
    "Description", "Documentation", "After", "Before", "Wants", "Requires",
    "Requisite", "BindsTo", "PartOf", "Conflicts", "WantedBy", "RequiredBy",
    "StartLimitIntervalSec", "StartLimitBurst", "StartLimitAction",
    "ConditionPathExists", "OnFailure",
}

#: systemd 不变式：graphical.target 想要（且排在）multi-user.target 之后。
#: 板端 `systemctl show graphical.target -p After` 实测里有 multi-user.target。
KNOWN_TARGET_ORDER = [("multi-user.target", "graphical.target")]


# ---------------------------------------------------------------------------
#  极简 unit 解析（够用就好：真正要判的是键在哪一段、值里排了谁）
# ---------------------------------------------------------------------------
_SECTION_RE = re.compile(r"^\[([A-Za-z]+)\]\s*$")


def parse_unit(text):
    """`{段名: [(键, 值), ...]}` —— 注释与空行丢掉，保留出现顺序。"""
    sections = {}
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        match = _SECTION_RE.match(line)
        if match:
            current = match.group(1)
            sections.setdefault(current, [])
            continue
        if current is None or "=" not in line:
            continue
        key, _, value = line.partition("=")
        sections[current].append((key.strip(), value.strip()))
    return sections


def raw_values(sections, section, key):
    """某个键的**原始**值（不切词）—— `ExecStart=` 这类值里有空格，切开就没法比。"""
    return [value for name, value in sections.get(section, []) if name == key]


def values(sections, section, key):
    """某个键的所有值，按空白切成多个目标（`After=` / `WantedBy=` 用这个）。"""
    out = []
    for value in raw_values(sections, section, key):
        out.extend(value.split())
    return out


def misplaced_unit_keys(sections):
    """出现在 `[Service]` 里的 `[Unit]` 专属键（应为空）。"""
    return sorted({name for name, _value in sections.get("Service", [])
                   if name in UNIT_ONLY_KEYS})


def back_edges_to_wanted_target(sections):
    """`WantedBy=T` 又 `After=T` 的目标（自相矛盾的双向排序，应为空）。"""
    wanted = set(values(sections, "Install", "WantedBy"))
    after = set(values(sections, "Unit", "After"))
    return sorted(wanted & after)


def ordering_edges(units):
    """把单元集合变成"谁必须在谁之前"的有向边。

    方向 = 时间先后：`A -> B` 表示 A 要先起。
      · `WantedBy=T`：systemd 会补一条隐式 `Before=T`，即 U -> T
        （板端实测：`graphical.target` 的 `After=` 里就是它想要的那些单元）；
      · `After=B`：B -> A；`Before=B`：A -> B。
    """
    edges = set()
    for name, sections in units.items():
        for target in values(sections, "Install", "WantedBy"):
            edges.add((name, target))
        for target in values(sections, "Unit", "After"):
            edges.add((target, name))
        for target in values(sections, "Unit", "Before"):
            edges.add((name, target))
    edges.update(KNOWN_TARGET_ORDER)
    return edges


def find_ordering_cycles(units):
    """返回找到的环（每个环是节点列表）。DFS 三色标记。"""
    edges = ordering_edges(units)
    graph = {}
    for src, dst in edges:
        graph.setdefault(src, set()).add(dst)
        graph.setdefault(dst, set())

    cycles = []
    WHITE, GREY, BLACK = 0, 1, 2
    color = {node: WHITE for node in graph}
    stack = []

    def visit(node):
        color[node] = GREY
        stack.append(node)
        for nxt in sorted(graph[node]):
            if color[nxt] == GREY:                 # 回到栈上 = 有环
                cycles.append(stack[stack.index(nxt):] + [nxt])
            elif color[nxt] == WHITE:
                visit(nxt)
        stack.pop()
        color[node] = BLACK

    for node in sorted(graph):
        if color[node] == WHITE:
            visit(node)
    return cycles


def load_units():
    """`{文件名: 解析结果}`；文件缺失就跳过（另一条测试会点名）。"""
    units = {}
    for name in UNIT_FILES:
        path = SYSTEMD_DIR / name
        if path.is_file():
            units[name] = parse_unit(path.read_text(encoding="utf-8"))
    return units


class TestUnitFilesShape(unittest.TestCase):
    """文件存在、行尾、以及"键写在正确的段里"。"""

    def test_all_three_units_exist(self):
        missing = [name for name in UNIT_FILES if not (SYSTEMD_DIR / name).is_file()]
        self.assertFalse(missing, "缺单元文件: %s" % "、".join(missing))

    def test_units_are_utf8_lf_with_final_newline(self):
        for name in UNIT_FILES:
            path = SYSTEMD_DIR / name
            raw = path.read_bytes()
            self.assertNotIn(b"\r", raw, "%s 里有 CRLF（板端会读到带 \\r 的值）" % name)
            self.assertTrue(raw.endswith(b"\n"), "%s 末尾没有换行" % name)
            raw.decode("utf-8")                        # 解不开就抛异常

    def test_unit_only_keys_are_not_in_the_service_section(self):
        bad = []
        for name, sections in load_units().items():
            found = misplaced_unit_keys(sections)
            if found:
                bad.append("%s 的 [Service] 里有 %s" % (name, "、".join(found)))
        self.assertFalse(
            bad,
            "只属于 [Unit] 的键写进了 [Service]，systemd 会 `Unknown key name ... ignoring` "
            "静默丢掉（T14-7 实测过 StartLimit*）:\n  %s" % "\n  ".join(bad))

    def test_the_misplaced_key_check_has_teeth(self):
        """反空转: 把当时那份坏写法喂进来，必须被抓到。"""
        broken = parse_unit(
            "[Unit]\nDescription=x\n\n[Service]\nExecStart=/bin/true\n"
            "StartLimitIntervalSec=60\nStartLimitBurst=5\n")
        self.assertEqual(misplaced_unit_keys(broken),
                         ["StartLimitBurst", "StartLimitIntervalSec"])


class TestNoOrderingCycle(unittest.TestCase):
    """排序：自相矛盾的边、以及把三个单元放一起的整体环检测。"""

    def test_no_wantedby_after_same_target(self):
        bad = []
        for name, sections in load_units().items():
            for target in back_edges_to_wanted_target(sections):
                bad.append("%s: WantedBy=%s 又 After=%s" % (name, target, target))
        self.assertFalse(
            bad,
            "自相矛盾的双向排序（WantedBy=T 的单元被隐式排到 T 之前，就不能再 After=T）:\n  %s"
            % "\n  ".join(bad))

    def test_back_edge_check_has_teeth(self):
        """反空转: 板端 vendor 版 xrandr 单元就是这个形状 —— 必须被抓到。"""
        vendor = parse_unit(
            "[Unit]\nDescription=Start xrandr script on boot\nAfter=graphical.target\n\n"
            "[Service]\nType=oneshot\nExecStart=/usr/bin/xrandr\n\n"
            "[Install]\nWantedBy=graphical.target\n")
        self.assertEqual(back_edges_to_wanted_target(vendor), ["graphical.target"])

    def test_repository_units_have_no_ordering_cycle(self):
        cycles = find_ordering_cycles(load_units())
        pretty = [" -> ".join(cycle) for cycle in cycles]
        self.assertFalse(
            cycles,
            "仓库里的单元合起来会成环（systemd 会删掉启动作业，表现为开机不启动）:\n  %s"
            % "\n  ".join(pretty))

    def test_cycle_detector_has_teeth(self):
        """反空转: 当时的真实坏组合（vendor xrandr + 我们原来的 agent-gui）必须被检出。"""
        broken = {
            GUI: parse_unit(
                "[Unit]\nAfter=display-manager.service xrandr-startup.service agent.service\n"
                "Wants=agent.service\nRequires=display-manager.service\n"
                "PartOf=graphical.target\n\n[Service]\nExecStart=/bin/true\n\n"
                "[Install]\nWantedBy=graphical.target\n"),
            XRANDR: parse_unit(
                "[Unit]\nAfter=graphical.target\n\n[Service]\nType=oneshot\nExecStart=/bin/true\n\n"
                "[Install]\nWantedBy=graphical.target\n"),
        }
        cycles = find_ordering_cycles(broken)
        self.assertTrue(cycles, "环检测没牙：当时那个真实的环没被检出来")
        joined = [" -> ".join(cycle) for cycle in cycles]
        self.assertTrue(
            any(GUI in cycle and XRANDR in cycle for cycle in joined),
            "检出的环里没有 GUI+xrandr: %r" % (joined,))

    def test_graphical_target_ordering_assumption_is_still_true(self):
        """环检测依赖"multi-user 在 graphical 之前"这条不变式，别让它悄悄失效。"""
        self.assertIn(("multi-user.target", "graphical.target"), KNOWN_TARGET_ORDER)


class TestGuiUnitWiring(unittest.TestCase):
    """GUI 单元：等 X、等转屏、root、自愈、别再写 PartOf。"""

    def setUp(self):
        path = SYSTEMD_DIR / GUI
        self.assertTrue(path.is_file(), "%s 不见了" % GUI)
        self.sections = parse_unit(path.read_text(encoding="utf-8"))

    def test_waits_for_display_manager_and_rotation(self):
        after = set(values(self.sections, "Unit", "After"))
        for target in ("display-manager.service", "xrandr-startup.service"):
            self.assertIn(target, after,
                          "GUI 必须 %s 之后才起（不然第一帧是没转屏的竖屏）" % target)

    def test_waits_for_the_x_socket_before_exec(self):
        """开机时 X 可能还没在听（实测差约 1 秒），要有轮询等 socket 的 ExecStartPre。"""
        pres = raw_values(self.sections, "Service", "ExecStartPre")
        self.assertTrue(pres, "GUI 单元没有 ExecStartPre")
        self.assertTrue(any("/tmp/.X11-unix/X0" in pre for pre in pres),
                        "ExecStartPre 里没有等 /tmp/.X11-unix/X0 的轮询: %r" % (pres,))

    def test_runs_as_root_with_display_and_restarts(self):
        self.assertEqual(values(self.sections, "Service", "User"), ["root"],
                         "GUI 必须 root：IPC socket 是 0600（桌面自启的 kickpi 连不上）")
        self.assertIn("DISPLAY=:0", values(self.sections, "Service", "Environment"),
                      "GUI 要画到 :0 就得带 DISPLAY=:0")
        self.assertEqual(values(self.sections, "Service", "Restart"), ["always"],
                         "kiosk 正常退出也该回来")

    def test_does_not_use_partof_graphical_target(self):
        self.assertNotIn("PartOf", [name for name, _v in self.sections.get("Unit", [])],
                         "PartOf=graphical.target 在 T14-7 取证后故意去掉的："
                         "DM 停/重启由 Requires=display-manager.service 带，PartOf 只多一层成环风险")

    def test_is_enabled_by_graphical_target(self):
        self.assertEqual(values(self.sections, "Install", "WantedBy"), ["graphical.target"])


class TestAgentUnitWiring(unittest.TestCase):
    """Agent 单元：root、能自愈、给足优雅退出时间。"""

    def setUp(self):
        path = SYSTEMD_DIR / AGENT
        self.assertTrue(path.is_file(), "%s 不见了" % AGENT)
        self.sections = parse_unit(path.read_text(encoding="utf-8"))

    def test_runs_as_root_and_restarts_on_failure(self):
        self.assertEqual(values(self.sections, "Service", "User"), ["root"])
        self.assertEqual(values(self.sections, "Service", "Restart"), ["on-failure"])

    def test_creates_the_log_directory_first(self):
        """`append:` 不会自己建目录 —— 少了这一条，首次开机就是干净地失败。"""
        pres = " ".join(raw_values(self.sections, "Service", "ExecStartPre"))
        self.assertIn("mkdir -p", pres)
        self.assertIn("logs", pres)

    def test_enabled_by_multi_user_target(self):
        self.assertEqual(values(self.sections, "Install", "WantedBy"), ["multi-user.target"])


class TestXrandrUnitIsFixed(unittest.TestCase):
    """板端 vendor 单元必须被替换成修好的那份。"""

    def setUp(self):
        path = SYSTEMD_DIR / XRANDR
        self.assertTrue(path.is_file(), "%s 不见了（它必须在仓库里，才能整份替换板端）" % XRANDR)
        self.sections = parse_unit(path.read_text(encoding="utf-8"))

    def test_is_not_ordered_after_graphical_target(self):
        after = set(values(self.sections, "Unit", "After"))
        self.assertNotIn("graphical.target", after,
                         "`After=graphical.target` 就是那个环的坏边（T14-7 取证）")

    def test_is_ordered_after_the_display_manager(self):
        self.assertIn("display-manager.service",
                      set(values(self.sections, "Unit", "After")),
                      "转屏要 X 起来之后做，所以排 display-manager.service 之后")

    def test_waits_until_x_is_reachable(self):
        """只写 After=display-manager 会太早：实测 `Can't open display :0`，单元 failed。"""
        pres = raw_values(self.sections, "Service", "ExecStartPre")
        self.assertTrue(any("xrandr --query" in pre for pre in pres),
                        "要轮询 `xrandr --query` 等 X 真能连上: %r" % (pres,))

    def test_rotates_as_root_without_xauthority(self):
        self.assertEqual(values(self.sections, "Service", "User"), ["root"],
                         "kickpi 的 .Xauthority 在板端根本不存在；root 不需要它（实测）")
        self.assertEqual(raw_values(self.sections, "Service", "ExecStart"),
                         ["/usr/bin/xrandr --output DSI-1 --rotate left"])


class TestDeployDocListsEveryUnit(unittest.TestCase):
    """安装片段必须点名三个文件 —— 只装两个正是当时成环的原因。"""

    def test_deploy_doc_mentions_all_three_units(self):
        self.assertTrue(DEPLOY_DOC.is_file(), "docs/deploy.md 不见了")
        text = DEPLOY_DOC.read_text(encoding="utf-8")
        missing = [name for name in UNIT_FILES if name not in text]
        self.assertFalse(missing,
                         "docs/deploy.md 没提到 %s —— 装的时候会漏" % "、".join(missing))

    def test_deploy_doc_has_the_cycle_selfcheck(self):
        """文档要给出"怎么自证没成环"的那条命令，否则下次还是靠猜。"""
        text = DEPLOY_DOC.read_text(encoding="utf-8")
        self.assertIn("ordering cycle", text, "docs/deploy.md 少了开机自检（grep ordering cycle）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
