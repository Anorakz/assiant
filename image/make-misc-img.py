#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make-misc-img.py — 生成"随镜像一起烧进去"的 misc 镜像（T15-2-12 修 2）

## 为什么必须要有它（今天的两次事故）

板端实测（2026-10-02，两次）：
  刷完镜像后板子黑屏、掉 fastboot，串口里是 `No bootable slots found.`
  读 `misc@0x800` 发现 A/B 元数据是 **两个槽都不可引导**：

      magic "\0AB0" ✓ | 槽A: priority=0  tries=0  successful=0   <- 判死
                        | 槽B: priority=1  tries=0  successful=0   <- 也不可引导

  关键在于 **tries=0**：只要这次启动失败一次，两个槽立刻全死。而 `ab-mark.service`
  （我们上轮加的"开机标记成功"）要等系统起来才能跑 —— 于是成了死锁：**刷完就再也
  起不来，只能串口救**。

## 机制（读 SDK 源码得到的，不是猜的）

  · `gen_package_file()`（device/rockchip/common/scripts/mk-updateimg.sh）按**分区表**
    逐个生成条目，而且 **`[ -r "$IMAGE" ]` 存在才收进包** ——
    所以 `Image/` 里没有 misc.img 时，刷机**根本不写 misc**，元数据就是上一次的残留。
  · SDK **自带** misc 目标：`build-hooks/08-misc.sh` / `scripts/mk-misc.sh`，
    开 `RK_MISC=y` 后由 `mk-all.sh` 调用；选了 `RK_MISC_CUSTOM` 就取
    `<RK_CHIP_DIR>/<RK_MISC_IMG>`（默认 misc.img）。
  · `08-misc.sh` 会先把 misc.img 截成 48 KB（注释：老 Windows 工具不接受 > 64K），
    CUSTOM 模式再用我们的文件软链覆盖它 → **我们的文件就是最终内容**。

  所以修法是三件事：① 两份板级 defconfig 开 `RK_MISC=y` + `RK_MISC_CUSTOM=y`
  + `RK_MISC_IMG="misc-assistant-ab.img"`；② 把本脚本生成的镜像放进 chip 目录；
  ③ 构建时 `mk-misc.sh` 取它 → package-file 自动多出 `misc  misc.img`。

## 写进去的初始状态

按 AVB 的默认（`avb_ab_data_init`）：**两个槽都可引导**，A 优先 →
无论 SPL 挑哪个槽，都能起来；`ab-mark` 起来后再把当前槽标成 successful。

    槽A: priority=15  tries=7  successful=0
    槽B: priority=14  tries=7  successful=0
    last_boot = 0

格式（`u-boot/include/android_avb/avb_ab_flow.h`）：magic "\0AB0"、32 字节、
CRC32 覆盖前 28 字节且**大端**存储、位置 misc 0x800（`spl_ab.h`: AB_METADATA_OFFSET=4 扇区）。

## 用法

    make-misc-img.py <输出路径>            # 默认 48 KB
    make-misc-img.py <输出路径> --size 4M  # 要整分区大小时
"""
import argparse
import pathlib
import sys

# ⚠ T15-14：格式与 CRC **只有一份实现**（`agent/core/ota.py`）。这里原来自己抄了一份
#   `crc32_ieee` —— 审计棘轮的 `duplicates_prod` 就是冲它来的 ✓。板端与宿主都从同一个
#   模块取，避免"两处格式各自漂移"（今天就吃过"只改主份"的亏 ✓）。
_HERE = pathlib.Path(__file__).resolve().parent
for _cand in (_HERE.parent, _HERE):            # 仓库根 / 本目录
    if (_cand / "agent" / "core").is_dir():
        if str(_cand) not in sys.path:
            sys.path.insert(0, str(_cand))
        break
from agent.core.ota import (  # noqa: E402
    AVB_MAJOR,
    AVB_MAGIC,
    AVB_MINOR,
    METADATA_SIZE,
    MISC_METADATA_OFFSETS,
    apply_bcb_to_misc,
    build_bcb,
    crc32_ieee,
)

MISC_OFFSET = MISC_METADATA_OFFSETS[0]      # 0x800（主份；备份见 MISC_METADATA_OFFSETS）
SLOT0_PRIORITY, SLOT1_PRIORITY = 15, 14     # avb_ab_data_init 的默认（可被外部改写）
MAX_TRIES = 7
DEFAULT_SIZE = 48 * 1024                     # 与 SDK 的 48 KB 一致（老工具的上限）


def build_metadata() -> bytes:
    """按本脚本的"出厂形态"拼 32 字节元数据（A 优先、两槽都可引导）。

    @note 复用 `agent.core.ota.build_bcb()` —— 优先级取本模块的 `SLOT0/SLOT1_PRIORITY`
          （外部脚本会改写它们来造演练镜像 ✓，所以这里必须现读、不能内联常量 ✓）。
    """
    return build_bcb(slot_priorities=(SLOT0_PRIORITY, SLOT1_PRIORITY),
                     tries=(MAX_TRIES, MAX_TRIES),
                     successful=(0, 0),
                     last_boot=0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--size", default="48K", help="镜像大小，默认 48K（支持 K/M 后缀）")
    args = ap.parse_args()

    spec = args.size.strip().upper()
    if spec.endswith("K"):
        size = int(spec[:-1]) * 1024
    elif spec.endswith("M"):
        size = int(spec[:-1]) * 1024 * 1024
    else:
        size = int(spec)
    if size < MISC_OFFSET + 32:
        print("!! 尺寸太小，装不下 0x800 处的元数据", file=sys.stderr)
        return 2

    raw = bytearray(size)
    # 整块写：**主备两份副本一起改**（`0x800` 与 `0x860`）—— 复用 agent/core/ota.py 的实现 ✓
    img = bytearray(apply_bcb_to_misc(bytes(raw), build_metadata()))

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(bytes(img))

    for offset in MISC_METADATA_OFFSETS:
        md = img[offset:offset + METADATA_SIZE]
        print("   副本@0x%x: magic=%r ver=%d.%d 槽A(prio=%d tries=%d succ=%d) "
              "槽B(prio=%d tries=%d succ=%d) last_boot=%d crc=%s"
              % (offset, md[0:4], md[4], md[5], md[8], md[9], md[10], md[12], md[13],
                 md[14], md[16], md[28:32].hex()))
    md = bytes(img[MISC_OFFSET:MISC_OFFSET + METADATA_SIZE])
    print("已生成 %s（%d 字节）" % (out, size))
    print("   自检 CRC: %s" % ("ok" if int.from_bytes(md[28:32], "big") == crc32_ieee(md[0:28]) else "!! 不符"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
