#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""image/check-parameter.py — 分区表守卫（T15-2-4）

分区表（Rockchip parameter 文件的 `CMDLINE: mtdparts=...`）写错一个十六进制数字，
后果是**刷机之后分区重叠 / rootfs 被覆盖**，而且要到板子上才看得出来。所以这里用
纯算术把它守住（不依赖 SDK、不依赖板子，CI 里就能跑）：

  1. 格式：每个条目必须是 `[size]@offset(name[:grow])`，offset/size 是 0x 开头的十六进制；
  2. **连续性**：每段的 offset 必须正好等于上一段的结束（不留缝、不重叠）；
  3. **可增长段**：`grow` 只能有一个，且必须是最后一段，并且它的起点仍在设备容量内；
  4. **A/B 成对**：出现 `_a` 就必须有同名 `_b`（Rockchip 的 A/B 更新按这个判据找槽位）；
  5. **容量**：所有分区（含 grow 的起点）必须落在 eMMC 容量之内；
  6. **最小尺寸**（可选用例表）：`--need system_a=1048576` 这种，检查每段够不够装东西
     （rootfs 镜像、FIT、模型放 userdata 都是"够不够"的问题）。

跑法::

    python image/check-parameter.py image/device/rockchip/.chips/rk3566_rk3568/parameter-assistant-ab.txt
    python image/check-parameter.py <file> --device-sectors 61071360 --need system_a=1572864
"""
from __future__ import annotations

import re
import sys

SECTOR = 512
#: KICKPI K1 Mini 的 eMMC：/sys/block/mmcblk0/size = 61071360 个 512B 扇区（29.1 GiB）
DEFAULT_DEVICE_SECTORS = 61071360

ENTRY_RE = re.compile(r"^(?P<size>[0-9a-fA-Fx]+|-)#?")


def human(sectors: int) -> str:
    if sectors * SECTOR < 1024 ** 3:
        return "%.1f MiB" % (sectors * SECTOR / (1024 ** 2))
    return "%.2f GiB" % (sectors * SECTOR / (1024 ** 3))


def parse_entries(cmdline: str):
    """把 `:a@b(c),d@e(f:grow)` 解析成 [(name, offset, size|None, grow)]。"""
    body = cmdline.split("mtdparts=", 1)[1]
    body = body.split(":", 1)[1] if body.startswith(":") else body
    out = []
    for raw in body.split(","):
        raw = raw.strip()
        m = re.match(r"^(?P<size>0x[0-9a-fA-F]+|-)(?:@(?P<off>0x[0-9a-fA-F]+))?"
                     r"\((?P<name>[^()]+)\)$", raw)
        if not m:
            raise ValueError("条目格式不对: %r" % raw)
        name = m.group("name")
        grow = name.endswith(":grow")
        if grow:
            name = name[: -len(":grow")]
        size = None if m.group("size") == "-" else int(m.group("size"), 16)
        off = int(m.group("off"), 16) if m.group("off") else None
        out.append((name, off, size, grow))
    return out


def check(path: str, device_sectors: int, needs: dict):
    text = open(path, encoding="utf-8").read().splitlines()
    cmdline = None
    for line in text:
        if line.startswith("CMDLINE:"):
            cmdline = line
    problems = []
    if cmdline is None:
        print("[FAIL] %s: 没有 CMDLINE 行" % path)
        return 1
    if "mtdparts=" not in cmdline:
        print("[FAIL] %s: CMDLINE 里没有 mtdparts=" % path)
        return 1

    entries = parse_entries(cmdline)
    print("== %s（设备 %d 扇区 = %s）" % (path, device_sectors, human(device_sectors)))
    print("  %-10s %12s %12s %12s  %s" % ("分区", "起始(扇区)", "大小(扇区)", "大小", "备注"))

    cursor = None
    grow_count = 0
    for idx, (name, off, size, grow) in enumerate(entries):
        if off is None:
            problems.append("%s 没有 @offset" % name)
            continue
        if cursor is not None and off != cursor:
            problems.append("%s 起始 0x%x != 上一段结束 0x%x（有缝或重叠）"
                            % (name, off, cursor))
        if grow:
            grow_count += 1
            if idx != len(entries) - 1:
                problems.append("%s 是 grow 但不是最后一段" % name)
            if off >= device_sectors:
                problems.append("%s 起点 0x%x 超出设备容量" % (name, off))
            cursor = device_sectors
            note = "可增长（剩余 %s）" % human(device_sectors - off)
        else:
            if size is None:
                problems.append("%s 没有大小又不是 grow" % name)
                continue
            if off + size > device_sectors:
                problems.append("%s 结束 0x%x 超出设备容量" % (name, off + size))
            cursor = off + size
            note = ""
        if name in needs and not grow and size is not None:
            if size < needs[name]:
                problems.append("%s 只有 %d 扇区（%s），需求 %d（%s）"
                                % (name, size, human(size), needs[name], human(needs[name])))
            else:
                note = (note + " " if note else "") + ">=需求 %s" % human(needs[name])
        print("  %-10s %12d %12s %12s  %s"
              % (name, off, "grow" if grow else "0x%x" % size,
                 "" if grow else human(size), note))

    names = [e[0] for e in entries]
    for name in names:
        if name.endswith("_a"):
            if name[:-2] + "_b" not in names:
                problems.append("有 %s 但没有 %s（A/B 必须成对）" % (name, name[:-2] + "_b"))
    if not grow_count:
        problems.append("没有可增长分区（userdata）—— 模型/日志放哪儿？")

    used = cursor or 0
    print("  -> 固定分区结束 0x%x（%s），userdata 可增长到设备末尾" % (used, human(used)))

    if problems:
        print("[FAIL] 分区表有问题：")
        for p in problems:
            print("   - %s" % p)
        return 1
    print("[OK] 分区表自检通过（连续、A/B 成对、容量与最小尺寸都满足）")
    return 0


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    path = argv[1]
    device = DEFAULT_DEVICE_SECTORS
    needs = {}
    i = 2
    while i < len(argv):
        if argv[i] == "--device-sectors":
            device = int(argv[i + 1])
            i += 2
        elif argv[i] == "--need":
            key, value = argv[i + 1].split("=", 1)
            needs[key] = int(value)
            i += 2
        else:
            print("不认识的参数: %s" % argv[i])
            return 2
    return check(path, device, needs)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
