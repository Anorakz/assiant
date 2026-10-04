#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ota-apply.py — 板端 OTA 落盘入口（T15-14）

**分工**：`agent/core/ota.py` 负责"算"（清单检查 / sha256 / 槽守卫 / 写盘计划 / 目标 BCB），
本文件只负责"做"（备份 → `dd` → 整块写 misc → 可选重启）。这样：
  · 算的那半纯函数化、可单测 ✓；
  · 做的那半是 OS 级动作，出问题也只影响**非当前槽** ✓。

## 安全顺序（一步都不许颠倒）

1. **算计划**（`ota.apply_package`）：清单不合格 / sha256 不符 / 目标是当前槽 → **直接拒绝** ✓；
2. **备份 misc**：`/data/assistant/ota/misc-backup-<时间>.img`（4 MB ✓）——
   今天就是靠它把板子从"元数据坏了"救回来的 ✓；
3. **写非当前槽**：`dd` 到 `boot_a|b`、`system_a|b`（`conv=fsync` ✓）；
4. **整块写 misc**：读出当前 48 KB → 放进目标 BCB（**主备两份一起改** ✓）→ 整块写回 ✓
   （⚠ 只改 `0x800` 那 32 字节出现过落 fastboot 的中间态 ✗）；
5. `sync` → 可选 `reboot` ✓。

## 用法（板上）

    ota-apply.py <包文件> --sha256 <期望哈希> [--package-file <清单>]
                 [--misc /dev/disk/by-partlabel/misc] [--dry-run] [--reboot]

退出码：0 成功；2 用法/读取问题；3 环境不对；4 被安全规则拒绝；5 写盘失败。
"""
import argparse
import json
import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
for _cand in (_HERE, "/usr/lib/assistant", os.path.dirname(os.path.dirname(_HERE))):
    if os.path.isdir(os.path.join(_cand, "agent", "core")):
        if _cand not in sys.path:
            sys.path.insert(0, _cand)
        break

from agent.core import ota  # noqa: E402

STATE_DIR = "/data/assistant/ota"
MISC_BLOCK_BYTES = 48 * 1024        # SDK 一直用 48 KB（老 Windows 工具的上限 ✓）

#: 计划里的角色 → 分区镜像文件名（与 `ota-updateimg` 产出的 `Image/` 目录一致 ✓，
#: 也就是 2026-10-04 在板上真跑通的那两个文件 ✓）。
IMAGE_FILES = {"boot": "boot.img", "system": "rootfs.img"}


def log(text: str) -> None:
    print("   %s" % text, flush=True)


def save_state(payload: dict) -> None:
    """把"走到哪一步"落盘（断电重启后能看出上次写到哪 ✓）。"""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        path = os.path.join(STATE_DIR, "state.json")
        payload = dict(payload)
        payload["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
    except OSError as exc:                       # 状态写不了不该让 OTA 失败
        log("（状态没存下：%s）" % exc)


def read_cmdline_slot() -> str:
    with open("/proc/cmdline", encoding="utf-8") as handle:
        return ota.current_slot_from_cmdline(handle.read())


def backup_misc(device: str) -> str:
    path = os.path.join(STATE_DIR, "misc-backup-%s.img" % time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(STATE_DIR, exist_ok=True)
    subprocess.run(["dd", "if=%s" % device, "of=%s" % path, "bs=512", "conv=fsync"],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log("已备份 misc → %s（%d B）" % (path, os.path.getsize(path)))
    return path


def dd_into(image: str, device: str) -> None:
    log("dd %s → %s" % (image, device))
    subprocess.run(["dd", "if=%s" % image, "of=%s" % device, "bs=4M", "conv=fsync"],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def write_bcb(device: str, bcb: bytes) -> None:
    """读整块 → 放进 BCB → 整块写回（**两份副本一起改** ✓）。"""
    with open(device, "rb") as handle:
        image = bytearray(handle.read(MISC_BLOCK_BYTES))
    fixed = ota.apply_bcb_to_misc(bytes(image), bcb)
    with open(device, "r+b") as handle:
        handle.seek(0)
        handle.write(fixed)
        handle.flush()
        os.fsync(handle.fileno())
    got = ota.parse_bcb(fixed[ota.MISC_METADATA_OFFSETS[0]:][:ota.METADATA_SIZE])
    log("misc 已整块写回（两份副本），自校验=%s 槽A=%s 槽B=%s"
        % ("ok" if got["ok"] else "!! " + got["reason"],
           got["slots"][0] if got["ok"] else "?",
           got["slots"][1] if got["ok"] else "?"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("package")
    ap.add_argument("--sha256", default="")
    ap.add_argument("--package-file", default="")
    ap.add_argument("--misc", default=ota.DEFAULT_PARTITION_DEV["misc"])
    ap.add_argument("--images", default="",
                    help="放分区镜像的目录（要有 boot.img 与 rootfs.img ✓）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reboot", action="store_true")
    args = ap.parse_args()

    manifest_text = None
    if args.package_file:
        with open(args.package_file, encoding="utf-8") as handle:
            manifest_text = handle.read()

    print("== 1. 算计划（清单 / sha256 / 槽守卫）")
    if not args.dry_run and not os.path.isdir(args.images):
        print("!! 要落盘就必须给 --images（放 boot.img 与 rootfs.img 的目录）；"
              "只想看计划请加 --dry-run")
        return 2
    try:
        plan = ota.apply_package(args.package, read_cmdline_slot(),
                                 package_file_text=manifest_text,
                                 expect_sha256=args.sha256 or None)
    except (ota.OtaError, OSError) as exc:
        print("!! 拒绝执行：%s" % exc)
        return 4
    log("当前槽 _%s → 目标槽 _%s" % (read_cmdline_slot(), plan["target_slot"]))
    log("包 %s（%d B）sha256=%s" % (args.package, plan["size"], plan["sha256"]))
    for role, device in plan["writes"]:
        log("将写 %-6s → %s" % (role, device))
    log("将写 misc   → %s（先备份 ✓）" % plan["misc"])
    if args.dry_run:
        print("（--dry-run：到这里为止，一个字节都没写 ✓）")
        return 0

    print("== 2. 备份 misc")
    save_state({"step": "backup", "plan_size": plan["size"]})
    try:
        backup = backup_misc(plan["misc"])
    except (OSError, subprocess.CalledProcessError) as exc:
        print("!! 备份失败，**不再往下走**：%s" % exc)
        return 5

    print("== 3. 写非当前槽")
    save_state({"step": "write", "backup": backup, "sha256": plan["sha256"]})
    try:
        for role, device in plan["writes"]:
            name = IMAGE_FILES.get(role, "")
            image = os.path.join(args.images, name) if name else ""
            if not image or not os.path.isfile(image):
                print("!! 缺分区镜像 %s（--images 指到 ota-updateimg 产出的 Image/ 目录 ✓）"
                      % (image or role))
                return 5
            log("用 %s（%d B）" % (image, os.path.getsize(image)))
            dd_into(image, device)
    except (OSError, subprocess.CalledProcessError) as exc:
        print("!! 写盘失败：%s（misc 还没动，可用 %s 恢复 ✓）" % (exc, backup))
        return 5

    print("== 4. 写 misc 指向新槽")
    try:
        write_bcb(plan["misc"], plan["bcb"])
    except (OSError, ValueError) as exc:
        print("!! 写 misc 失败：%s —— 串口进 U-Boot，用 %s 整块写回即可 ✓" % (exc, backup))
        return 5
    subprocess.run(["sync"], check=False)

    save_state({"step": "done", "backup": backup, "target_slot": plan["target_slot"]})
    print("== 5. 完成：%s" % ("重启生效" if args.reboot else "重启后生效（reboot 自己来 ✓）"))
    if args.reboot:
        subprocess.run(["reboot"], check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
