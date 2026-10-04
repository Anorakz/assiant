#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ab-mark.py — 把**当前** A/B 槽标记为"启动成功"（T15-2-11 救砖后补的关键一步）

## 为什么必须要有它

板端实测（2026-10-02）：刷完 b8 后板子起不来，串口里是

    U-Boot SPL ... No bootable slots found, use lastboot.
    U-Boot 2017.09 ... PartType: EFI
    No bootable slots found.
    rk_avb_append_part_slot: failed to get slot suffix !
    FIT: No boot partition
    ... Android boot failed, error -1.
    Enter fastboot...OK

根因**不是镜像**，是 **A/B 元数据被烧光**：
  · SPL 与 u-boot 每次启动都会把当前槽的 `tries_remaining` **减 1**
    （`common/spl/spl_ab.c`、`lib/avb/rk_avb_user/rk_ab_ops_user.c`）；
  · 而整个镜像里**没有任何**把 `successful_boot` 置 1 的东西
    （`abctl`/`bootctl` 都不存在），也没有用户态工具；
  · 于是一路扣到 0，可引导判定（见下）把两个槽都判死 → 不进系统。
    Android 那边这一步是 userspace 做的（boot control HAL）。我们缺，所以补在这里。

## 格式（全部来自 SDK 源码，不是猜的）

`u-boot/include/android_avb/avb_ab_flow.h`：

    #define AVB_AB_MAGIC "\\0AB0"        // 4 字节：00 41 42 30
    #define AVB_AB_MAJOR_VERSION 1
    #define AVB_AB_MAX_PRIORITY 15
    #define AVB_AB_MAX_TRIES_REMAINING 7

    typedef struct AvbABSlotData {   // packed，4 字节
        uint8_t priority;
        uint8_t tries_remaining;
        uint8_t successful_boot;
        uint8_t reserved[1];
    } AvbABSlotData;

    typedef struct AvbABData {       // packed，32 字节
        uint8_t magic[4];            // +0
        uint8_t version_major;       // +4
        uint8_t version_minor;       // +5
        uint8_t reserved1[2];        // +6
        AvbABSlotData slots[2];      // +8   （每槽 4 字节）
        uint8_t last_boot;           // +16
        uint8_t reserved2[11];       // +17
        uint32_t crc32;              // +28  **大端**存储
    } AvbABData;

存放位置：`misc` 的 **0x800**（`spl_ab.h` 的 `AB_METADATA_OFFSET = 4` 扇区；
`common/spl/spl_ab.c` 用 `part_info.start + AB_METADATA_OFFSET` 读写 ✓）。

CRC：覆盖前 28 字节，**big-endian**（`rk_ab_ops_user.c` 的
`avb_ab_data_verify_and_byteswap()`：`dest->crc32 = be32toh(dest->crc32)` 再与
`crc32(0, dest, sizeof(AvbABData) - sizeof(uint32_t))` 比较 ✓）。

可引导判定：`priority > 0 && (successful_boot || tries_remaining > 0)`
→ 置 `successful_boot = 1` 之后，这个槽**再也不会被扣死**。

## 踩过的坑（别再走一遍）

一开始我按 u-boot 里的 `android_bootloader_control`（magic `BCAB`、CRC 小端）写，
结果 SPL 直接：
    Magic is incorrect. / Error validating A/B metadata from disk. Resetting and writing new ...
—— 因为 SPL 与 u-boot（这份 SDK）用的是 **AVB 格式**，不是那个 Android 结构。
（`android_ab.c` 那套在本 SDK 里不是生效路径。）

## 用法

    ab-mark.py [--misc /dev/disk/by-partlabel/misc] [--slot a|b] [--dry-run]

默认从 `/proc/cmdline` 取当前槽（厂商拼写是 `android_slotsufix=_a|_b`）。
幂等：已经是 successful_boot=1 就只打日志。
"""
import argparse
import os
import struct
import sys

# ⚠ T15-14：CRC 与槽解析**只有一份实现**了（原来这里各有一份，审计棘轮的
#   `duplicates_prod` / `same_name_prod` 就是冲这个来的）。板上两者同处
#   `PYTHONPATH=/usr/lib/assistant`（本文件在 `/usr/lib/assistant/ab-mark.py`，
#   `agent/` 也在那儿），仓库里则要往上找到仓库根。
_HERE = os.path.dirname(os.path.abspath(__file__))
for _cand in (_HERE, os.path.dirname(os.path.dirname(_HERE))):
    if os.path.isdir(os.path.join(_cand, "agent", "core")):
        if _cand not in sys.path:
            sys.path.insert(0, _cand)
        break
from agent.core.ota import crc32_ieee, parse_bcb  # noqa: E402  （不在 zlib 上：板端没有 zlib 模块）

MISC_OFFSET = 0x800          # 4 扇区（spl_ab.h: AB_METADATA_OFFSET）
AVB_MAGIC = b"\x00AB0"       # AVB_AB_MAGIC
AVB_MAJOR, AVB_MINOR = 1, 0
MAX_PRIORITY = 15
MAX_TRIES = 7
SLOT_OFF = 8                 # slots[] 在结构里的偏移
SLOT_SIZE = 4


def slot_from_cmdline_or_empty() -> str:
    """从 `/proc/cmdline` 取当前槽（拼写是厂商的 `android_slotsufix=_a|_b`）。

    @note 解析逻辑复用 `agent.core.ota`（**函数名不再与它同名** —— 审计棘轮的
          `same_name_prod` 就是冲这个来的 ✓）；这里保留"取不到就返回空串"的老行为，
          脚本要靠空串打印那句人话提示 ✓。
    """
    from agent.core.ota import OtaError, current_slot_from_cmdline as _parse

    try:
        with open("/proc/cmdline", encoding="utf-8") as f:
            cmdline = f.read()
    except OSError:
        return ""
    try:
        return _parse(cmdline)
    except OtaError:
        for token in cmdline.split():            # 兼容 androidboot.slot_suffix= 这种写法
            if token.startswith("androidboot.slot_suffix="):
                return token.split("=", 1)[1].strip().lstrip("_")
        return ""


def decode_slot(buf: bytes, idx: int):
    off = SLOT_OFF + idx * SLOT_SIZE
    return {"priority": buf[off], "tries_remaining": buf[off + 1], "successful_boot": buf[off + 2]}


def encode_slot(buf: bytearray, idx: int, priority: int, tries: int, successful: int):
    off = SLOT_OFF + idx * SLOT_SIZE
    buf[off] = priority & 0xFF
    buf[off + 1] = tries & 0xFF
    buf[off + 2] = successful & 0x01
    buf[off + 3] = 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--misc", default="/dev/disk/by-partlabel/misc")
    ap.add_argument("--slot", default="", help="a 或 b；默认从 /proc/cmdline 取")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    slot_name = args.slot or slot_from_cmdline_or_empty()
    if slot_name not in ("a", "b"):
        print("!! 拿不到当前槽（cmdline 无 android_slotsufix=_a/_b，也没给 --slot）")
        return 2
    idx = 0 if slot_name == "a" else 1

    with open(args.misc, "rb") as f:
        f.seek(MISC_OFFSET)
        bcb = bytearray(f.read(32))
    if len(bcb) != 32:
        print("!! 读元数据失败")
        return 2

    magic_ok = bytes(bcb[0:4]) == AVB_MAGIC
    before = [decode_slot(bcb, i) for i in range(2)]
    print("当前槽 = _%s (index %d)" % (slot_name, idx))
    print("元数据现状: magic=%r version=%d.%d last_boot=%d"
          % (bytes(bcb[0:4]), bcb[4], bcb[5], bcb[16]))
    print("  槽0(_a): %s" % before[0])
    print("  槽1(_b): %s" % before[1])

    if not magic_ok:
        print("=> magic 不是 AVB 的 \\0AB0（可能被置零或被别的东西写过）→ 按默认重建")
        bcb[0:4] = AVB_MAGIC
        bcb[4], bcb[5] = AVB_MAJOR, AVB_MINOR
        bcb[6:8] = b"\x00\x00"
        encode_slot(bcb, 0, MAX_PRIORITY, MAX_TRIES, 0)
        encode_slot(bcb, 1, MAX_PRIORITY - 1, MAX_TRIES, 0)
        bcb[17:28] = b"\x00" * 11

    cur = decode_slot(bcb, idx)
    if cur["successful_boot"] == 1:
        changed = False
        print("=> 当前槽已经是 successful_boot=1（幂等，不写盘）")
    else:
        changed = True
        encode_slot(bcb, idx, max(cur["priority"], MAX_PRIORITY), MAX_TRIES, 1)
    bcb[16] = idx                       # last_boot = 当前槽

    crc = crc32_ieee(bytes(bcb[0:28]))
    struct.pack_into(">I", bcb, 28, crc)   # **大端**（be32toh 那一步）

    # ⚠ T15-14：写完**用 `agent.core.ota.parse_bcb` 自校验一遍**（顺带把 CRC 也验了 ✓）。
    #   以前这里是本地 `decode_slot` 各读一遍、CRC 只由调用方口头确认 —— 现在这一步
    #   既是"打印写入后的状态"，也是"证明刚才那 32 字节是**格式合法**的" ✓。
    checked = parse_bcb(bytes(bcb))
    if not checked["ok"]:
        print("!! 写入后的元数据自校验失败: %s" % checked["reason"])
        return 3
    after = checked["slots"]
    print("写入后: 槽0(_a)=%s  槽1(_b)=%s  last_boot=%s  crc32=0x%08x(BE) 自校验=ok"
          % (after[0], after[1], checked["last_boot"], crc))

    if args.dry_run:
        print("（--dry-run，不落盘）")
        return 0

    if changed or not magic_ok:
        with open(args.misc, "r+b") as f:
            f.seek(MISC_OFFSET)
            f.write(bytes(bcb))
            f.flush()
            os.fsync(f.fileno())
        print("已写回 %s@0x%x" % (args.misc, MISC_OFFSET))
    return 0


if __name__ == "__main__":
    sys.exit(main())
