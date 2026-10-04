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

## 7. 部署与切槽的四个坑（2026-10-04 真跑时全踩过一遍）

| 坑 | 现象 | 规矩 |
|---|---|---|
| **`/tmp` 重启即清空** ✗ | 放 `/tmp` 的脚本/镜像重启后消失 → 自动化链**静默空转** ✗ | 跨重启的东西**一律放 `/data`** ✓；每步前确认脚本还在 ✓ |
| **A/B 各有自己的 rootfs** ✗ | 把 `agent/core/ota.py` 装进 `/usr/lib/assistant/…` 后切槽 ⇒ `ImportError` ✗ | 共享依赖放 `/data` + 显式 `PYTHONPATH=/data/assistant/ota/bin` ✓ |
| **`ab-mark.py --slot x` ≠ 切槽** ✗ | 在 A 槽钦点 B（prio15/tries7/succ1）⇒ 下次启动被引导器**清零**、板子仍起 A ✗ | 切槽只能用**整块写 `misc`**（两槽都合法、目标槽优先级更高 ✓） |
| **多份 `misc` 备份是生命线** ✓ | 今天靠它救回过一次砖 ✓ | 每次写 `misc` 前先整块备份到 `/data` ✓（`ota-apply.py` 已内置 ✓） |

**板端实测的推荐调用**（真跑通过 ✓，`OTA-RC=0`）：

```bash
# 共享依赖树（跨槽、跨重启都在 ✓）
/data/assistant/ota/bin/{ota-apply.py, agent/…}
PYTHONPATH=/data/assistant/ota/bin python3 /data/assistant/ota/bin/ota-apply.py \
    /data/assistant/ota/ota-assistant-ab.img \
    --sha256 <期望哈希> --images /data/assistant/ota/images
```
`--images` 里放 `boot.img` 与 `rootfs.img`（= `ota-updateimg` 产出的 `Image/` 内容 ✓）。

## 8. 三个"真金白银"的坑（2026-10-05，三次真 OTA 才全踩出来）

这一节全是**板端实测**逼出来的，不是推演 ✓ —— 每一条都让"升级成功"这件事**静默失效**过 ✗。

### 8.1 幂等判据**必须认槽**（最隐蔽 ✗✗）

`confirm.json` 放在 `/data/assistant/ota/`，而 **`/data` 是两个槽共享的** ✗ ⇒
"A 槽那次成功留下的 `ok:true`" 会被 **B 槽启动时**当成"已经确认过了" ✓ ⇒
**根本不判定、也不标记** ✗ ⇒ `successful_boot` 一直是 0、`tries_remaining` 一路递减 ✗。

板端日志实锤（它就是被这句话骗过去的）：
```
ota-confirm[465]: /data/assistant/ota/confirm.json 里已经是 ok ✓（幂等，不再重判）
```
**规矩** ✓：结果里记 `slot` ✓；判断"是否已确认"要求 **`slot` 等于当前槽** ✓
（当前槽从 `/proc/cmdline` 的 `android_slotsufix=` 取 ✓）；**没记 `slot` 的旧结果一律不算** ✓。

### 8.2 `--timeout` 是**全局选项**，必须写在子命令之前

```bash
assistant status --timeout 8     # ✗ argparse 直接报错退出 ⇒ 判据永远 False ⇒ 槽永远标不上
assistant --timeout 8 status     # ✓
```
**连带教训** ✓：判"IPC 通不通"**不能只看输出里有没有 `agent.sock`** ✗ ——
usage/报错文本里也可能出现 ✓（**假通** ✗）。现在判据是 **`rc == 0` 且出现"已连上"** ✓，
并且**把 rc 打进日志** ✓（这次排障绕远就是因为日志里没有 rc ✗）。

### 8.3 `ab-mark` 的**启用者**是 `assistant.target` 的 `Wants=` ✗（不是 `.wants/` 软链）

`ab-mark.service` 原来是"每次开机无条件把当前槽标成功" ✓ —— 这会把 **A/B 的失败回退路径废掉** ✗
（新槽只要起得来就算成功，`tries` 再也不递减 ⇒ 永远不会回退 ✓）。改的时候踩过两次空：

| 只改这里 | 结果 |
|---|---|
| 清 `post-build.sh` 的挂载清单 / `rm -rf assistant.target.wants` | ✗ **没用** —— target-finalize 阶段 `systemctl preset-all` 又按 `[Install]` 建回来（实测时间戳：post-build 23:40 清过，**板子上它 23:41** 又出现）|
| 去掉单元自己的 `[Install] WantedBy=` | ✗ **还是没用** —— `systemd/image/assistant.target` 里那行 **`Wants=ab-mark.service`** 才是真正的启用者 |

**正确的分工** ✓（现在的状态）：
- **判定**由 `assistant-ota-confirm.service` 做（`agent` 与 `agent-gui` 都 active **且 IPC 通** ✓）；
- 判过了它**按脚本路径**直接调 `/usr/lib/assistant/ab-mark.py` ✓（**不走 systemd** ✓）；
- `ab-mark.service` **不再自动启动** ✗（`assistant.target` 的 `Wants=` 里已换成确认单元 ✓，
  单元去掉了 `[Install]` ✓，`image/check-assistant-target.py` 给它记了**带理由的工具类例外** ✓）；
- 手动排障照样能 `systemctl start ab-mark.service` ✓。

**验收判据**（照这个查，别只看构建成功 ✗）：
```bash
assistant ota status                     # 槽A: priority=15 tries=7 successful=1 可引导 <- 当前
cat /data/assistant/ota/confirm.json     # ok:true + slot 是当前槽 + at 是**本次**
systemctl is-active ab-mark.service      # 期望 inactive（它不再自动跑 ✓）
ls /etc/systemd/system/assistant.target.wants/ | grep -c ab-mark   # 期望 0
```
⚠ 还有一条**核验纪律** ✓：要确认"镜像里到底是什么"，得看**打包后的** `rootfs.ext2`
（`debugfs -R "cat /usr/lib/systemd/system/assistant.target" rootfs.ext2` ✓）——
只看构建用的 target 目录**会漏** ✗（那条旧软链就藏在打包后的镜像里 ✓）。

