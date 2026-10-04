# 无线 OTA（T15-14）

> 机制与**实测结论**在 [`image/FLASH-RUNBOOK.md`](../image/FLASH-RUNBOOK.md) §7（那份是"为什么"，
> 这份是"怎么用"）。两者冲突时以 §7 的实测为准，并回来修这份。

## 0. 一句话

**出包（宿主）→ 推包（网络）→ 应用（板端：写非当前槽 + 把 `misc` 指向它）→ 重启 → 自检确认。**
出问题就按 §7 的救砖配方把 `misc` 整块写回 —— **`/data`（模型/配置）全程不动** ✓。

## 1. 出包（WSL / 宿主侧）

```bash
# ① 让 OTA 包用"自己的清单"（默认那份含 userdata，会写掉 /data ✗✗）
#    ⚠ 落点是 SDK 的 output/.config，不是仓库里的 defconfig ✗；
#      而且 RK_OTA_PACKAGE_FILE_* 是个 choice —— 要用**替换**（把 _DEFAULT 设成 not set），
#      只追加会被后面的 =y 盖掉 ✗（这是 2026-10-04 白跑两次的坑）
# ② 清单文件本体放 <RK_CHIP_DIR>/（device/rockchip/.chip/），只去掉 userdata 那一行 ✓
cd <SDK>
./build.sh ota-updateimg          # 产物: output/firmware/update-*.img
```

**包的形状**（必须核对）：`boot_a` + `system_a`（**只写 A 槽** ⇒ 见 §3 的守卫 ✓）、
**不含 `userdata`** ✓；`afptool -pack` 不接受"只留三行"的清单 ✗（`parameter`/`bootloader`
这类标准条目必须在 ✓）。

## 2. 推包

- **ssh/scp 通道**（一直可用 ✓）：`scp update-*.img rk3568:/data/assistant/ota/`；
- **HTTP**：⚠ 服务若跑在 **WSL** 里，板子**到不了** ✗（WSL2 在 NAT 后面 ✗，实测 `Connection refused`）。
  要么把服务跑在 **Windows 侧**，要么加 `netsh portproxy` + 防火墙放行 ✗；
- 包落地后**先算哈希** ✓，后面 `apply` 要用它。

## 3. 应用（板端）

```bash
# 只看计划（一个字节都不写 ✓）—— 建议永远先跑这一条
ota-run.sh /data/assistant/ota/update-xxxx.img --sha256 <期望哈希> --dry-run

# 真落盘（会先备份 misc ✓）
ota-run.sh /data/assistant/ota/update-xxxx.img --sha256 <期望哈希> \
           --images /data/assistant/ota/Image --package-file /data/assistant/ota/package-file
# 想连着重启：再加 --reboot
```

`--images` 里要有 `boot.img` 与 `rootfs.img`（= `ota-updateimg` 产出的 `Image/` 目录内容 ✓，
2026-10-04 在板上真跑通的就是这两个文件 ✓）。

**安全规则（都在 `agent/core/ota.py` 里，有单测 ✓）**
1. 清单不合格（含 `userdata` / 缺 `boot_a`|`system_a`）→ 拒绝；
2. `sha256` 不符 → **一个字节都不写** ✓；
3. **包写 A 槽、当前就在 A 槽 → 拒绝** ✓（写当前槽 = 覆盖正在运行的系统）；
4. 写 `misc` 前**先整块备份** ✓；写完**自校验**（`parse_bcb`）✓；
5. `misc` 一律**整块写**（`0x800` 与 `0x860` 主备两份一起改 ✓）—— 只改主份出现过落 fastboot ✗。

`assistant ota status` 可以在板上**随时**看两槽状态（`priority/tries/successful` + 是否可引导 ✓）：

```
当前槽: _a
  槽a: priority=15 tries=7 successful=1 可引导  <- 当前
  槽b: priority=14 tries=7 successful=0 可引导
  last_boot=a
```

## 4. 重启后的"确认"（回退机制的半边）

新槽起来后要**把它标成成功**，否则 `tries_remaining` 会被一路扣到 0（§7.3 ✓）：

- 镜像里已有 `ab-mark.service`（开机后跑 `ab-mark.py`）✓ —— 它标**当前**槽 ✓；
- 判据（本项目的约定）：**Agent + GUI 都 active 且 IPC 通** ✓，然后才认为这次 OTA 成功；
- 手动核对：`assistant ota status` / `xxd -s 0x800 -l 32 /dev/mmcblk0p2` ✓。

## 5. 回退与救砖（都在 §7，不重复）

| 症状 | 先看 | 处理 |
|---|---|---|
| 起不来、串口 `No bootable slots` / `failed to get slot suffix` | SPL/U-Boot 那两行 `A/B-slot: …` | §5.2 或 §7.6：串口进 U-Boot → `ext4load` 备份 + `mmc write` 整块写回 ✓ |
| 只想换回旧槽 | `assistant ota status` | 把另一槽的优先级抬高（`ota.next_bcb_for_slot()` ✓）或写回 A-active 的 `misc` ✓ |
| 卡在 fastboot | 串口无输出 + 设备管理器 `USB download gadget` | 串口发 Ctrl+C 抢提示符 ✓ |

## 6. 已实测到什么程度（2026-10-04）

- ✅ **出包**：`./build.sh ota-updateimg` 出成品包，裁掉 `userdata`（`de907d6a…9d89`，1,042,039,370 B）✓；
- ✅ **写非当前槽**：`dd` `boot_a`(p3) + `system_a`(p6) + `misc`(p2, A-active) → 重启进 A 槽 ✓；
- ✅ **自检**：`agent`/`agent-gui`/`assistant-init` 全 active、IPC 通、tiers 133、`/data` 完好 ✓；
- ✅ **切槽**：抬优先级 A↔B 双向（串口见 `mmcblk0p6` / `mmcblk0p7` ✓）；
- ✅ **救砖**：`misc` 写坏后 `ext4load` 备份 + `mmc write` 整块写回，一次救回 ✓；
- ⚠️ **未直接观察**：某个槽**启动失败**导致的自动回退（用的是"抬优先级"等价路径 ✓）；
- ❌ **不可用**：`/usr/bin/rkupdate`（USB/NAND 时代工具，要 `CRKUsbComm`/`/dev/rkflash0` ✗）。
