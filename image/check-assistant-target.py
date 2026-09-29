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
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import imagelib  # noqa: E402  （同目录的工具模块：两棵树只差一层同名目录，选树收在一处）

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
    # 厂商的 WiFi/BT 固件初始化（`/usr/bin/wifibt-init.sh start`，oneshot，
    # `WantedBy=sysinit.target`）。**必须留着**：wlan0(rtl8822cs) 的固件/模块是它加载的，
    # 没有它 NetworkManager 连设备都没有，network-online 永远等不到。
    # —— T15-2-10 才发现它在我们的启动链里：2-8 那次检查看的是**单包构建**那棵树
    # （那时还没装到这一步），整机构建的树里它在 `sysinit.target.wants/`。
    "wifibt-init.service",
}

#: 被我们**主动 mask** 掉的厂商服务（`/etc/systemd/system/<unit> -> /dev/null`）。
#: 出现在这里 = post-build 明确不要它；检查器看到 mask 就当它不存在（systemd 的语义也是
#: "屏蔽"，即使别的 target 还 Want 它也起不来）。每一个都要有理由。
MASKED_BY_US = {
    # USB gadget（adb/rndis 那一类调试与传输通道）：`Type=simple` 常驻，
    # `WantedBy=local-fs.target` —— 不屏蔽的话**每次开机都会起**，与
    # "只起我们的东西"（T15-2-8 的出口）矛盾。发行镜像屏蔽，开发镜像保留（T15-2-12）。
    "usb-gadget.service",
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


def is_masked(etc_dir: Path, name: str) -> bool:
    """`/etc/systemd/system/<unit> -> /dev/null` = systemd 的"屏蔽"。

    ⚠ 不能用 `Path.readlink()`：那是 Python **3.9+** 才有的，CI 跑 3.8。
    """
    p = etc_dir / name
    try:
        return p.is_symlink() and os.readlink(str(p)) == "/dev/null"
    except OSError:
        return False


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


def run_sdk_mode(sdk: Path, target: Path = None) -> int:
    if target is None:
        target, why = imagelib.resolve_target(sdk)
        if target is None:
            print("!! %s" % why, file=sys.stderr)
            return 2
    else:
        why = "--target 指定"
    sysd = target / "usr/lib/systemd/system"
    etc = target / "etc/systemd/system"
    if not sysd.is_dir():
        print("!! 找不到 target 里的 systemd 目录: %s" % sysd, file=sys.stderr)
        return 2

    print("== 镜像模式：检查 %s" % target)
    print("   （%s）" % why)
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
    masked = []
    if etc.is_dir():
        for d in sorted(etc.glob("*.wants")) + sorted(etc.glob("*.requires")):
            owner = d.name.rsplit(".", 1)[0]     # assistant.target.wants -> assistant.target
            names = set()
            for p in d.iterdir():
                if is_masked(etc, p.name):
                    masked.append("%s（%s 里挂过，已被我们 mask）" % (p.name, d.name))
                    continue
                names.add(p.name)
            extra.setdefault(owner, set()).update(names)
            print("   %-34s -> %s" % (d.name, " ".join(sorted(names)) or "(空)"))
    if masked:
        print("   被我们 mask 掉（/dev/null，systemd 语义上起不来，因此不算进闭包）:")
        for m in masked:
            print("     - %s" % m)

    # 3) 闭包
    for name, unit in units.items():
        if name in OUR_UNITS:
            check_unit_file(name, unit, problems)
    closure_all = closure(units, "assistant.target", extra)
    # mask 掉的单元即使被 Wants= 直接引用也不会起来，所以不算进闭包（但要说出来）。
    # ⚠ 判据是**磁盘上真有那个 /dev/null 符号链接**，不是 MASKED_BY_US 这张策略表 ——
    #   否则"策略要求 mask、但 post-build 没落"就会被自己人掩盖过去。
    really_masked = {u for u in closure_all if is_masked(etc, u)}
    for m in sorted(really_masked):
        print("   （闭包里点到了被 mask 的 %s —— 按 systemd 语义不会起来，已忽略）" % m)
    closure_all = closure_all - really_masked
    # 策略表里要求 mask 的，必须真的被 mask 了（少了这一步，策略只是文档）
    for u in sorted(MASKED_BY_US):
        if (sysd / u).exists() and not is_masked(etc, u):
            problems.append("按策略要 mask 的 %s 没有被 mask（post-build 的 MASK_UNITS 没落？）" % u)
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
            # 厂商自己的 enable 目录名里带空格（实测：`local-fs.target graphical.target.wants`）
            # → systemd **不认这种目录名**，里面的单元永远不会被拉起。
            # 不是我们的问题，但板端基线时别以为它跑了（T15-2-10 记录）。
            if " " in d.name:
                print("   ! 目录名不合规（含空格），systemd 会忽略: %s（里面: %s）"
                      % (d.name, " ".join(sorted(p.name for p in d.iterdir())) or "(空)"))
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
    print("== 通过：闭包里只有我们的 %d 个单元 + 白名单基础设施 %s"
          % (len(ours), ", ".join(infra) or "(无)"))
    if masked:
        print("   （另有 %d 个厂商常驻服务被我们 mask 掉：%s）"
              % (len(masked), ", ".join(m.split("（")[0] for m in masked)))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--units", metavar="DIR", help="仓库模式的单元目录（例如 systemd/image）")
    ap.add_argument("--sdk", metavar="SDK", help="镜像模式：SDK 根目录")
    ap.add_argument("--target", metavar="DIR",
                    help="镜像模式：直接指定 target 树（不给则优先选整机构建那棵，"
                         "见 image/imagelib.py）")
    args = ap.parse_args(argv)
    if args.units:
        return run_units_mode(Path(args.units))
    if args.sdk:
        return run_sdk_mode(Path(args.sdk), Path(args.target) if args.target else None)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
