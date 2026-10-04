"""无线 OTA 的纯逻辑（T15-14）。

设计原则：**不发明新机制**。这里落的每一条规则，都是 2026-10-04 在真板上逐项实测过的：

* 槽守卫：包固定写 A ⇒ **当前就在 A 槽时必须拒绝**（`RefusesToWriteCurrentSlot`）；
* 校验：`sha256` 不符**一个字节都不写**；
* `misc` 的 A/B 元数据在 `0x800` 与 `0x860` **主备两份**，改动一律**整块写**
  （实测：只改主份会出现中间态，且那次把板子搞成了 `No bootable slots`）；
* 元数据是 32 字节：magic ``\\0AB0`` + 版本 1.0 + 两槽各 4 字节（prio/tries/succ/rsv）
  + `last_boot` + CRC32(前 28 字节)**大端**放在末尾（与 `image/make-misc-img.py` 同构）。

这个模块**只做纯计算**（不碰块设备、不发网络、不 import 重物），所以能直接单测。
真正落盘/重启由板端入口脚本负责（`image/ota/ota-run.sh` 那条路）。
"""

from __future__ import annotations

import hashlib
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
#  A/B 元数据（misc 的 0x800 / 0x860）
# ---------------------------------------------------------------------------

MISC_METADATA_OFFSETS: Tuple[int, ...] = (0x800, 0x860)   # 主份 / 备份（实测）
METADATA_SIZE = 32
AVB_MAGIC = b"\x00AB0"
AVB_MAJOR = 1
AVB_MINOR = 0
MAX_TRIES = 7
DEFAULT_SLOT_A_PRIORITY = 15          # 出厂 misc 是 A 优先（实测：写它起来的是 A 槽）
DEFAULT_SLOT_B_PRIORITY = 14


def crc32_ieee(data: bytes) -> int:
    """标准 CRC-32（与 u-boot 的 crc32()、zlib 同值）。

    @note 不用 zlib：板端精简环境可能没有这个模块（T15-2-11 实测过）。
    """
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xEDB88320 if crc & 1 else 0)
    return crc ^ 0xFFFFFFFF


def build_bcb(slot_priorities: Sequence[int] = (DEFAULT_SLOT_A_PRIORITY,
                                                DEFAULT_SLOT_B_PRIORITY),
              tries: Sequence[int] = (MAX_TRIES, MAX_TRIES),
              successful: Sequence[int] = (0, 0),
              last_boot: int = 0) -> bytes:
    """拼出 32 字节的 `bootloader_control`（含大端 CRC）。

    @param slot_priorities 两槽优先级；**谁高引导器选谁**（实测：A=14/B=15 就从 B 起）
    @param tries           两槽剩余尝试次数（每次启动尝试会减一，成功标记后补满）
    @param successful      两槽是否已确认成功（1 = 不再扣 tries）
    @param last_boot       上次启动的槽（0=A, 1=B）
    """
    if len(slot_priorities) != 2 or len(tries) != 2 or len(successful) != 2:
        raise ValueError("两槽的三个字段都必须恰好 2 个值")
    meta = bytearray(METADATA_SIZE)
    meta[0:4] = AVB_MAGIC
    meta[4], meta[5] = AVB_MAJOR, AVB_MINOR
    for index in range(2):
        base = 8 + index * 4
        meta[base] = int(slot_priorities[index]) & 0xFF
        meta[base + 1] = int(tries[index]) & 0xFF
        meta[base + 2] = int(successful[index]) & 0xFF
        meta[base + 3] = 0
    meta[16] = int(last_boot) & 0xFF
    meta[28:32] = crc32_ieee(bytes(meta[0:28])).to_bytes(4, "big")
    return bytes(meta)


def parse_bcb(raw: bytes) -> Dict[str, Any]:
    """解析 32 字节元数据（**校验通过才返回**，否则 `ok=False` + 原因）。"""
    out: Dict[str, Any] = {"ok": False, "reason": "", "slots": [], "last_boot": None,
                           "crc_ok": False}
    if len(raw) < METADATA_SIZE:
        out["reason"] = "元数据不足 32 字节"
        return out
    if raw[0:4] != AVB_MAGIC:
        out["reason"] = "magic 不是 \\0AB0"
        return out
    want = int.from_bytes(raw[28:32], "big")
    out["crc_ok"] = want == crc32_ieee(raw[0:28])
    if not out["crc_ok"]:
        out["reason"] = "CRC 不符"
        return out
    for index in range(2):
        base = 8 + index * 4
        out["slots"].append({"priority": raw[base], "tries_remaining": raw[base + 1],
                             "successful_boot": raw[base + 2]})
    out["last_boot"] = raw[16]
    out["ok"] = True
    out["reason"] = ""
    return out


def apply_bcb_to_misc(image: bytes, bcb: bytes) -> bytes:
    """把 32 字节元数据**写进两份副本**（`0x800` 与 `0x860`），其余字节原样。

    @note 这就是"整块写"的根据：我们从出厂 misc（48 KB）改出目标镜像，再整块 dd 下去。
    """
    if len(image) < max(MISC_METADATA_OFFSETS) + METADATA_SIZE:
        raise ValueError("misc 镜像太小，装不下 0x860 处的副本")
    out = bytearray(image)
    for offset in MISC_METADATA_OFFSETS:
        out[offset:offset + METADATA_SIZE] = bcb
    return bytes(out)


# ---------------------------------------------------------------------------
#  包与槽
# ---------------------------------------------------------------------------

SLOT_NAMES = ("a", "b")
#: 包里的分区名 → 目标分区。实测：`ota-updateimg` 出的包**固定写 A 槽**（`boot_a`/`system_a`）。
PACKAGE_TARGET_SLOT = "a"
DEFAULT_PARTITION_DEV = {
    ("a", "boot"): "/dev/disk/by-partlabel/boot_a",
    ("b", "boot"): "/dev/disk/by-partlabel/boot_b",
    ("a", "system"): "/dev/disk/by-partlabel/system_a",
    ("b", "system"): "/dev/disk/by-partlabel/system_b",
    "misc": "/dev/disk/by-partlabel/misc",
}
FORBIDDEN_PACKAGE_ENTRIES = ("userdata",)   # 含它就会写掉 /data（实测：默认清单里就有）


def parse_package_file(text: str) -> List[Dict[str, str]]:
    """解析 afptool 的 `package-file`（`NAME<TAB>PATH`，`#` 开头是注释）。"""
    entries: List[Dict[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        entries.append({"name": parts[0], "path": parts[1]})
    return entries


def check_package(entries: Sequence[Dict[str, str]]) -> List[str]:
    """检查包清单；@return 问题列表（空 = 通过）。

    @note 重点两条：**不许含 `userdata`**（会写掉 /data）、**必须含 boot_a 与 system_a**
          （这个包固定写 A 槽 —— 也就决定了"当前在 A 槽时必须拒绝"）。
    """
    problems: List[str] = []
    names = [entry.get("name", "") for entry in entries]
    for bad in FORBIDDEN_PACKAGE_ENTRIES:
        if bad in names:
            problems.append("包里含 %s：它会写掉 /data（出包时要裁掉）" % bad)
    for need in ("boot_a", "system_a"):
        if need not in names:
            problems.append("包里缺 %s" % need)
    return problems


def sha256_file(path: str, chunk: int = 1 << 20) -> str:
    """算文件的 sha256（分块读，别把 1 GB 一次性读进内存）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
#  计划与守卫
# ---------------------------------------------------------------------------

class OtaError(Exception):
    """OTA 的可预期失败（调用方按人话把 `str(exc)` 打给用户）。"""


def current_slot_from_cmdline(cmdline: str, want_prefix: str = "android_slotsufix=") -> str:
    """从 `/proc/cmdline` 取当前槽；@return "a"/"b"；取不到抛 `OtaError`。

    @note 实测：cmdline 里是 `android_slotsufix=_a`（拼写是厂商的 `slotsufix` ✓）。
    """
    for token in cmdline.split():
        if token.startswith(want_prefix):
            suffix = token[len(want_prefix):].lstrip("_")
            if suffix in SLOT_NAMES:
                return suffix
            raise OtaError("槽后缀看不懂: %r" % token)
    raise OtaError("cmdline 里没有 %s（槽元数据可能坏了）" % want_prefix)


def plan_apply(current_slot: str, package_slot: str = PACKAGE_TARGET_SLOT,
               allow_same_slot: bool = False) -> Dict[str, Any]:
    """决定"写哪个槽、写哪些分区"；该拒绝就抛 `OtaError`。

    @param current_slot    当前运行槽（`current_slot_from_cmdline` 取的）
    @param package_slot    包的目标槽（默认 A —— SDK 的 ab-ota 包固定写 A）
    @param allow_same_slot 是否允许写当前槽（**默认 False**；它是 root 级开关，见 config_tiers）
    @return {"target_slot","writes":[(role, device)],"backup_misc":True}
    """
    if current_slot not in SLOT_NAMES:
        raise OtaError("当前槽非法: %r" % current_slot)
    if package_slot not in SLOT_NAMES:
        raise OtaError("包的目标槽非法: %r" % package_slot)
    if package_slot == current_slot and not allow_same_slot:
        raise OtaError("包写的是当前槽（%s）—— 拒绝执行：写当前槽 = 正在运行的系统被覆盖"
                       % current_slot)
    writes = [(role, DEFAULT_PARTITION_DEV[(package_slot, role)])
              for role in ("boot", "system")]
    return {"target_slot": package_slot,
            "writes": writes,
            "misc": DEFAULT_PARTITION_DEV["misc"],
            "backup_misc": True,          # 写 misc 前先备份（实测：靠它救回过板子）
            "reboot_required": True}


def next_bcb_for_slot(target_slot: str) -> bytes:
    """把 `target_slot` 抬成优先（另一槽降一档），其余与出厂一致。"""
    if target_slot not in SLOT_NAMES:
        raise OtaError("目标槽非法: %r" % target_slot)
    if target_slot == "a":
        priorities = (DEFAULT_SLOT_A_PRIORITY, DEFAULT_SLOT_B_PRIORITY)
        last_boot = 0
    else:
        priorities = (DEFAULT_SLOT_B_PRIORITY, DEFAULT_SLOT_A_PRIORITY)
        last_boot = 0
    return build_bcb(slot_priorities=priorities, last_boot=last_boot)


def verify_package(path: str, expect_sha256: Optional[str] = None) -> str:
    """校验包文件；@return 实际 sha256；不符抛 `OtaError`。

    @note **不符就一个字节都不写**：调用方必须先过这一关再谈写盘。
    """
    if not os.path.isfile(path):
        raise OtaError("包不存在: %s" % path)
    size = os.path.getsize(path)
    if size <= 0:
        raise OtaError("包是空文件: %s" % path)
    actual = sha256_file(path)
    if expect_sha256 and actual.lower() != expect_sha256.lower():
        raise OtaError("sha256 不符（期望 %s，实际 %s）—— 不写任何东西"
                       % (expect_sha256, actual))
    return actual


# ---------------------------------------------------------------------------
#  生产编排层（`assistant ota apply` 直接调它；板端入口只负责落盘/重启）
# ---------------------------------------------------------------------------

def apply_package(package_path: str,
                  current_slot: str,
                  package_file_text: Optional[str] = None,
                  expect_sha256: Optional[str] = None,
                  allow_same_slot: bool = False) -> Dict[str, Any]:
    """把"一次 OTA 要做哪些事"算清楚；任一条不满足就抛 `OtaError`（**先拒绝，再谈写盘**）。

    顺序即安全边界：清单检查 → sha256 校验 → 槽守卫 → 生成写盘计划与目标 BCB。
    这个函数**不写任何东西**（纯计算 ✓），落盘由板端入口按计划执行。

    @param package_path    包文件（`ota-updateimg` 出的 `update-*.img`）
    @param current_slot    当前槽（`current_slot_from_cmdline` 取的）
    @param package_file_text 包清单文本（不给就跳过清单检查，例如拿不到 afptool 时）
    @param expect_sha256   期望哈希（给了就必须相符）
    @param allow_same_slot 是否允许写当前槽（root 级开关，默认 False）
    @return {"target_slot","writes","misc","bcb","sha256","size","backup_misc","reboot_required"}
    """
    if package_file_text:
        problems = check_package(parse_package_file(package_file_text))
        if problems:
            raise OtaError("包清单不合格：" + "；".join(problems))

    digest = verify_package(package_path, expect_sha256)
    plan = plan_apply(current_slot, PACKAGE_TARGET_SLOT, allow_same_slot=allow_same_slot)
    out = dict(plan)
    out["bcb"] = next_bcb_for_slot(plan["target_slot"])
    out["sha256"] = digest
    out["size"] = os.path.getsize(package_path)
    return out
