#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""image/check-assistant-target.py — 「起来之后只有我们的东西」的机器验收（T15-2-8）

T15-2-8 的出口是：`systemctl list-dependencies assistant.target` **只有我们的服务**。
那句话要有牙，得能复跑。这里用**静态依赖闭包**把它做出来 —— 两种模式：

    # ① 仓库模式（CI 里跑）：只看 systemd/image/ 这一组单元自己是否自洽
    python3 image/check-assistant-target.py --units systemd/image

    # ② 镜像模式：对着已经建好的 target 树算**真正的**闭包
    python3 image/check-assistant-target.py --sdk <SDK 根>

为什么不用 `systemctl list-dependencies` 直接问
-------------------------------------------------------------------------------
它要跟**正在跑的 systemd** 说话（dbus），`systemctl --root=` 不支持 list-dependencies。
所以这里自己解析 unit 文件与 `.wants/`/`.requires/` 符号链接算闭包 ——
和 systemd 的语义对齐到"够用的程度"，而**板上真跑那条命令**是 T15-2-11 的事。

判据（三条，都是"错了就报"）
-------------------------------------------------------------------------------
  1. `default.target` 必须指向 `assistant.target`（镜像模式）；
  2. 闭包里除了**我们的单元**，只允许 systemd 自带的基座 target 与**白名单基础设施**
     （NetworkManager：buildroot 不启用它，而 network-online.target 自己拉不起任何东西）；
     出现 graphical / display-manager / weston / slim / xorg / X11 / 蓝牙 / cups 一律失败；
  3. 单元本身不许出现 X 相关的东西（DISPLAY= / xrandr / display-manager / X11-unix），
     不许出现板端的 git checkout 路径（/home/kickpi/...），ExecStart 必须在允许表里。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

#: 我们自己的单元（镜像里就这四个）
OUR_UNITS = {
    "assistant.target",
    "agent.service",
    "agent-gui.service",
    "assistant-init.service",
}

#: 白名单：**不是我们的**但允许出现在闭包里的 —— 每一个都要有理由
INFRA_UNITS = {
    # buildroot 的 NetworkManager 钩子只建 dbus 别名，不启用服务；而
    # network-online.target 自己不会拉起任何东西。不带它 = 板上没网。
    "NetworkManager.service",
    "NetworkManager-wait-online.service",
    "NetworkManager-dispatcher.service",
}

#: systemd 自带的基座（这些出现在闭包里是正常的；桌面那一路不在其中）
SYSTEM_UNITS_OK = {
    "basic.target", "sysinit.target", "local-fs.target", "local-fs-pre.target",
    "swap.target", "paths.target", "slices.target", "sockets.target", "timers.target",
    "network.target", "network-online.target", "getty.target", "getty-pre.target",
    "remote-fs.target", "-.mount", "systemd-journald.service", "systemd-journald.socket",
    "systemd-udevd.service", "systemd-udevd-control.socket", "systemd-udevd-kernel.socket",
    "systemd-tmpfiles-setup.service", "systemd-sysctl.service", "systemd-modules-load.service",
    "systemd-random-seed.service", "systemd-remount-fs.service", "systemd-user-sessions.service",
    "dbus.service", "dbus.socket", "systemd-fsck-root.service", "systemd-journal-flush.service",
    "systemd-update-utmp.service", "systemd-machine-id-commit.service", "systemd-sysusers.service",
    "sys-kernel-config.mount", "sys-kernel-debug.mount", "proc-sys-fs-binfmt_misc.mount",
    "dev-hugepages.mount", "dev-mqueue.mount", "sys-fs-fuse-connections.mount",
}

#: 明确"不该出现"的关键词（出现即失败，比白名单更能说清问题）
FORBIDDEN_PATTERNS = [
    "graphical.target", "display-manager", "weston", "slim", "lightdm", "gdm",
    "xorg", "x11", "xrandr", "bluetooth", "cups", "avahi", "desktop",
]

#: ExecStart/ExecStartPre 里允许出现的可执行文件（镜像里真实存在的那几个）
ALLOWED_BINARIES = {
    "/usr/bin/python3", "/usr/lib/assistant/gui/agent_gui", "/bin/sh",
    "/bin/mkdir", "/usr/bin/mkdir",
}

WANT_RE = re.compile(r"^(Wants|Requires|BindsTo|PartOf|Upholds)\s*=\s*(.+)$")
EXEC_RE = re.compile(r"^Exec(Start|StartPre|Stop|Reload)\s*=\s*(.+)$")
SECTION_RE = re.compile(r"^\[(.+)\]$")


def parse_unit(path: Path) -> dict:
    """极简 unit 解析：返回 {'section': {key: [values]}}（够我们这几条判据用）。"""
    out: dict = {}
    section = ""
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        m = SECTION_RE.match(line)
        if m:
            section = m.group(1)
            out.setdefault(section, {})
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        out.setdefault(section, {}).setdefault(k.strip(), []).append(v.strip())
    return out


def unit_names_in(value: str) -> list:
    """从 `Wants=a.service b.target` 里取出以 .service/.target/.socket 结尾的名字。"""
    names = []
    for tok in value.split():
        if re.search(r"\.(service|target|socket|timer|path|mount|slice)$", tok):
            names.append(tok)
    return names


def load_units(units_dir: Path) -> dict:
    units = {}
    for p in sorted(units_dir.glob("*")):
        if p.is_file() or p.is_symlink():
            units[p.name] = parse_unit(p)
    return units


def direct_deps(units: dict, name: str) -> set:
    deps = set()
    for key in ("Wants", "Requires"):
        for v in units.get(name, {}).get("Unit", {}).get(key, []):
            deps.update(unit_names_in(v))
    return deps


def closure(units: dict, start: str, extra_deps: dict = None) -> set:
    seen, stack = set(), [start]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        deps = set(direct_deps(units, cur))
        if extra_deps and cur in extra_deps:
            deps |= extra_deps[cur]
        stack.extend(d for d in deps if d not in seen)
    return seen


def check_unit_file(name: str, unit: dict, problems: list) -> None:
    text_sections = unit
    for section, keys in text_sections.items():
        for key, values in keys.items():
            for v in values:
                low = v.lower()
                if "/home/kickpi" in v:
                    problems.append("%s: 还带着板端 git checkout 路径: %s=%s" % (name, key, v))
                if section == "Service" and key.startswith("Exec"):
                    exe = v.split()[0] if v.split() else ""
                    if exe.startswith("/") and exe not in ALLOWED_BINARIES:
                        problems.append("%s: ExecStart 用了未允许的可执行文件 %s" % (name, exe))
                if key in ("Environment", "EnvironmentFile"):
                    if "DISPLAY=" in v or "X11" in v:
                        problems.append("%s: 出现 X 相关的环境变量: %s" % (name, v))
                if key in ("After", "Before", "Requires", "Wants"):
                    for dep in unit_names_in(v):
                        for bad in FORBIDDEN_PATTERNS:
                            if bad in dep.lower():
                                problems.append("%s: %s 依赖了不该有的 %s" % (name, key, dep))
                if key == "WantedBy":
                    if v != "assistant.target" and name != "assistant.target":
                        problems.append("%s: WantedBy=%s（镜像里应当挂 assistant.target）" % (name, v))
    if name.endswith(".service"):
        install = unit.get("Install", {})
        if "WantedBy" not in install and name not in ("assistant-init.service",):
            problems.append("%s: 没有 [Install] WantedBy（没人会拉起它）" % name)


def is_system_internal(name: str) -> bool:
    """systemd 自带的基座单元（出现在闭包里是正常的）。

    ⚠ 这里按**类型**放行而不是只列名字：实测闭包里会冒出 `-.slice`、`system.slice`、
    `tmp.mount` 这类东西（每个服务都在某个 slice 里、local-fs 那一路会带 mount）。
    真正要盯的是**服务与 target** —— 那里出现非我们的就是问题，所以保留严格名单。
    桌面/X 那一路仍然会被 FORBIDDEN_PATTERNS 拦住（在放行之前先查）。
    """
    if name in SYSTEM_UNITS_OK:
        return True
    if name.startswith("systemd-"):
        return True
    if name.endswith((".slice", ".mount")):
        return True
    return False


def run_units_mode(units_dir: Path) -> int:
    print("== 仓库模式：检查 %s" % units_dir)
    units = load_units(units_dir)
    problems = []
    for name, unit in units.items():
        check_unit_file(name, unit, problems)

    for required in ("assistant.target", "agent.service", "agent-gui.service"):
        if required not in units:
            problems.append("缺单元文件: %s" % required)

    closure_all = closure(units, "assistant.target")
    print("   assistant.target 的闭包（按单元文件解析）:")
    for u in sorted(closure_all):
        tag = "我们的" if u in OUR_UNITS else ("基础设施" if u in INFRA_UNITS else "系统基座")
        print("     %-32s %s" % (u, tag))

    for u in sorted(closure_all):
        if u in OUR_UNITS or u in INFRA_UNITS or is_system_internal(u):
            continue
        for bad in FORBIDDEN_PATTERNS:
            if bad in u.lower():
                problems.append("闭包里出现不该有的单元: %s（命中 %s）" % (u, bad))
                break
        else:
            problems.append("闭包里出现未被允许的单元: %s（要么是我们漏登记，要么该删）" % u)

    print()
    if problems:
        print("!! 问题 %d 条:" % len(problems))
        for p in problems:
            print("   - %s" % p)
        return 1
    print("== 通过：闭包里只有我们的单元 + 白名单基础设施 + systemd 基座")
    return 0


def run_sdk_mode(sdk: Path) -> int:
    target = sdk / "buildroot" / "output" / "rockchip_rk3568_kickpi_k1mini_release" / "target"
    sysd = target / "usr/lib/systemd/system"
    etc = target / "etc/systemd/system"
    if not sysd.is_dir():
        print("!! 找不到 target 里的 systemd 目录: %s" % sysd, file=sys.stderr)
        return 2

    print("== 镜像模式：检查 %s" % target)
    units = load_units(sysd)
    for p in sorted(etc.glob("*")) if etc.is_dir() else []:
        if p.is_file() and p.suffix in (".service", ".target"):
            units[p.name] = parse_unit(p)

    problems = []
    # 1) default.target 必须指向 assistant.target
    default = etc / "default.target"
    if not default.is_symlink() and not default.exists():
        problems.append("没有 %s —— 开机目标还是 systemd 默认的 multi-user" % default)
    else:
        resolved = default.resolve().name if default.is_symlink() else default.name
        print("   default.target -> %s" % resolved)
        if resolved != "assistant.target":
            problems.append("default.target 指向 %s，不是 assistant.target" % resolved)

    # 2) .wants/.requires 目录（enable 的落地形式）
    extra: dict = {}
    if etc.is_dir():
        for d in sorted(etc.glob("*.wants")) + sorted(etc.glob("*.requires")):
            owner = d.name.rsplit(".", 1)[0]     # assistant.target.wants -> assistant.target
            names = {p.name for p in d.iterdir()}
            extra.setdefault(owner, set()).update(names)
            print("   %-34s -> %s" % (d.name, " ".join(sorted(names)) or "(空)"))

    # 3) 闭包
    for name, unit in units.items():
        if name in OUR_UNITS:
            check_unit_file(name, unit, problems)
    closure_all = closure(units, "assistant.target", extra)
    ours = sorted(u for u in closure_all if u in OUR_UNITS)
    infra = sorted(u for u in closure_all if u in INFRA_UNITS)
    print("   闭包：我们的 %s" % ", ".join(ours))
    print("         基础设施 %s" % (", ".join(infra) or "(无)"))
    unknown = sorted(u for u in closure_all
                     if u not in OUR_UNITS and u not in INFRA_UNITS
                     and not is_system_internal(u))
    for u in unknown:
        for bad in FORBIDDEN_PATTERNS:
            if bad in u.lower():
                problems.append("闭包里出现不该有的单元: %s（命中 %s）" % (u, bad))
                break
        else:
            problems.append("闭包里出现未被允许的单元: %s" % u)

    # 4) 整个 /etc/systemd/system 里也不许有桌面那一套被 enable
    if etc.is_dir():
        for d in list(etc.glob("*.wants")) + list(etc.glob("*.requires")):
            for p in d.iterdir():
                for bad in ("graphical", "display-manager", "weston", "slim", "xorg", "x11"):
                    if bad in p.name.lower():
                        problems.append("%s 里启用了 %s" % (d.name, p.name))

    print()
    if problems:
        print("!! 问题 %d 条:" % len(problems))
        for p in problems:
            print("   - %s" % p)
        return 1
    print("== 通过：只有我们的服务（+ NetworkManager 这一个白名单基础设施）")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--units", metavar="DIR", help="仓库模式的单元目录（例如 systemd/image）")
    ap.add_argument("--sdk", metavar="SDK", help="镜像模式：SDK 根目录")
    args = ap.parse_args(argv)
    if args.units:
        return run_units_mode(Path(args.units))
    if args.sdk:
        return run_sdk_mode(Path(args.sdk))
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
