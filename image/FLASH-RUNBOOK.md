# T15-2-11 首次刷板手册（K1 Mini：Ubuntu → 我们的 Buildroot 镜像）

> 状态：**已执行完成**（首刷 2026-09-29，救砖与修复到 2026-10-02；见
> `image/CHECKPOINT-T15-2-11.md`）。本文件保留为**照着手册**：
> 刷机步骤 + **起不来时怎么救**（§7 是 10-02 那次真踩过的两种砖）。
> 相关记录：`docs/image.md` §5.8（两条 output 路径 / `update.img` 是悬空软链）、
> §5.9（刷板前验证）、§5.10（payload）；任务表见 `todo2.md` 的 2-11。

## 0. 先说清楚风险（刷之前必须知道）

| 事项 | 说明 |
| --- | --- |
| **eMMC 会被重排分区** | 新表（`parameter-assistant-ab.txt`）是 A/B：`uboot / misc / boot_a / boot_b / backup / system_a / system_b / oem / userdata:grow`。板上现在那份 Ubuntu 的分区（`mmcblk0p1..p6`，最后一个是 28.9 G 的 `/`）**一个都留不下** |
| **板上数据全丢** | 包括 `/home/kickpi/myproject/assitant`（git 工作区）、`/home/kickpi/model`（**4.9 GB 模型**）、`game_samples`、`wallpapers` 等 |
| **已备份的** | `config/`（含 `config.yaml`，md5 `5212393aea2147a34fa9a2b734683027`）+ `llm/`（含 `llm.env`）→ 已拉到 PC：`temp/board-state-20260929.tgz`（6.9 MB） |
| **未备份的** | `/home/kickpi/model` 4.9 GB、`wallpapers/`、`game_samples/`、`project/`、`test/`。**要么现在拷到 PC，要么刷完重新投放** |
| **可回退性** | ⚠ **刷成 Buildroot 之后，想回到厂商 Ubuntu 需要厂商那份 `update.img`**。KICKPI 通常会放出厂镜像；如果你手上没有，这次就是**单程票**（我们的镜像是可复现的，但厂商那套不一定拿得回来） |

## 1. 要烧的东西

```bash
SDK=/home/anorak/rk3568_buildroot/linux-kernel-6.1/rk-linux6.1-2026060914/rk-linux6.1-2026060914
```

| 项 | 值 |
| --- | --- |
| 镜像 | `$SDK/output/update-ab/Image/update.img` |
| 大小 | **759,050,826 B** |
| sha256 | **`cb02622b4f2fd50e1e7f6ad79d7e08bdea635a54142f55713c88fbbc4b4a0119`** |
| ⚠ 别拿这个 | `$SDK/output/firmware/update.img`（AB 形态下它是**悬空软链**，指向不存在的 `update-ab.img`） |
| 分区表 | `$SDK/output/firmware/parameter.txt`（= 我们的 `parameter-assistant-ab.txt`，T15-2-4 校验过） |

## 2. 刷机路线：**已选定 B（USB loader + Windows RKDevTool）**

> 2026-09-29 22:38 你的决定：**下次专门做**、走 **路线 B**、**手上有厂商 Ubuntu 出厂镜像**
> （所以这次刷板**可逆**）。下面是执行稿。

### 2.1 我这边已经就位的东西（下次不用再准备）

| 东西 | 路径 |
| --- | --- |
| 镜像（Windows 侧，**sha256 已核对**） | `E:\rk3568\flash\update-assistant-20260929.img` = 759,050,826 B / `cb02622b…4b4a0119` |
| 分区镜像（按分区单独烧用） | `E:\rk3568\flash\update-assistant-20260929.raw.img` |
| 分区表 | `E:\rk3568\flash\parameter-assistant-ab.txt` |
| RKDevTool v3.37 | `E:\rk3568\flash-tools\RKDevTool_Release_v3.37\RKDevTool_v3.37_for_window\RKDevTool.exe` |
| Rockusb 驱动（没装过才需要） | `E:\rk3568\flash-tools\DriverAssistant\DriverAssitant_v5.13\DriverInstall.exe` |
| 命令行版（可选） | `E:\rk3568\flash-tools\upgrade_tool_v2.46\` 下的 `upgrade_tool.exe` |
| 备选（SD 卡启动） | SDK `tools/windows/SDDiskTool_v1.78.zip` |
| 备份：板上配置与 llm | `temp/board-state-20260929.tgz`（6.9 MB，含 `config.yaml` md5 `5212393a…`、`llm.env`） |
| 备份：板上 4.9 GB 模型 | `E:\rk3568\board-model-backup\model\`（18 个文件 / **4.86 GB**） |

### 2.2 下次执行（**只有 3 个手动动作要你做**）

1. **装驱动**（只需一次）：管理员运行 `DriverInstall.exe`。之前刷过这台板子的话，多半已装好。
2. **插 USB 线**（板子的 USB OTG/下载口 ↔ PC）。
3. **按住 RECOVERY 键**上电或复位，直到 RKDevTool 底部出现 **"发现一个 LOADER 设备"**（或 MASKROM），松手。

然后：`升级固件 页 → 固件(F) 选 E:\rk3568\flash\update-assistant-20260929.img → 升级`，
等 100%（**期间绝不能断电/拔线**），板子自己重启进我们的 `assistant.target`。

> ⚠ **不要在 WSL 里试 USB 路线**：WSL2 默认不直通 USB（要 `usbipd-win`），一定在 Windows 侧。

### 2.3 路线 A（板内 `updateEngine`）——本次不采用，留档

板子自带 `/usr/bin/updateEngine`，可以 `scp` 镜像上去后
`updateEngine --image_url=/tmp/update.img --misc=update --save_dir=/tmp --reboot`，
不用插线不用按键。**没采用**：它是在运行中的系统上写同一块 eMMC，中途失败要回路线 B 救砖；
既然出厂镜像在手、也不赶时间，路线 B 更干净。

## 3. 刷完之后的首次启动（2-11 的正文）

```bash
# ① 起来了吗（新镜像 rootfs 里没有 sshd 的 enable，先看串口/网络）
ping 192.168.137.30
ssh root@192.168.137.30 'systemctl is-active assistant.target agent.service agent-gui.service assistant-init.service'

# ② 串口控制台：systemd-getty-generator 会按 cmdline 自动起 serial-getty（不用 enable）

# ③ userdata 首次要先格式化（新分区刚建出来是空的）
mkfs.ext4 -L userdata /dev/disk/by-partlabel/userdata
mount -a && df -h /data

# ④ 模型投放（4.9 GB）→ /data/model，并让 llm.env 的 LLM_MODEL_PATH 指过去
#    默认配置由 assistant-init.service 从 /usr/lib/assistant/config/*.example.yaml 铺到
#    /data/assistant/config/（首启那一次）
```

### 板端基线要采的数字（2-11 的出口）

| 项 | 怎么采 |
| --- | --- |
| 启动耗时 | `systemd-analyze`、`systemd-analyze blame \| head` |
| 起来的东西是不是只有我们的 | `systemctl list-dependencies assistant.target`（对着 §5.9 的静态闭包核对） |
| EGLFS 真出图 / 旋转 | 看屏；`QT_QPA_EGLFS_INTEGRATION=eglfs_kms` 是否走通（失败会打 kms/egl 错） |
| 触摸 | `evtest`/`libinput debug-events`；旋转 90° 后坐标是否对得上 |
| 网络 | `nmcli device status`、`ping` 外网、`systemctl status NetworkManager-wait-online` |
| RTC | `hwclock -r`、`date`、掉电走时（hym8563 @i2c5 0x51） |
| NPU | `python3 -c "from rknnlite.api import RKNNLite; ..."` 真做一次 `init_runtime()` |
| 硬解 | `gst-launch-1.0 ... ! mppvideodec` 真解码 + `top` 看 CPU（对照板端基线的 80%） |
| Agent/GUI | `journalctl -u agent -u agent-gui`、`assistant status`（CLI 入口）、GUI 首帧 |
| 温度 | `/sys/class/thermal/thermal_zone*/temp` |

### 基线要复跑的既有验收（1b/1c/1d + T14）

- 1b：EGLFS + 旋转 + 虚拟键盘（QML 四模块）
- 1c：触摸/输入
- 1d：视频（Moonlight/B 站）那条链
- T14：Agent+GUI 的功能验收（按 `todo.md`/`docs/` 里那套）

## 4. 万一刷坏了

- **进不了系统** → 路线 B 的 loader 模式还能用（Rockusb 在 u-boot/loader 里，不依赖 rootfs）。
- **要回 Ubuntu** → 需要厂商 KICKPI 的出厂 `update.img`（**先确认手上有**）。
- 我们的镜像可复现：`wsl -u root bash image/build-image.sh <SDK>` + `image/preflash-check.sh <SDK>` 退出码 0。

## 5. 起不来时的两种真砖（2026-10-02 各踩过一次，按症状对号入座）

### 5.1 `FIT: No boot partition` / u-boot 找不到 boot 分区 → u-boot 缺 A/B 支持

串口表现（掉到 `=>` 提示符）：
```
PartType: EFI
FIT: No boot partition
android_image_load_by_partname: Can't find part: boot
```
而 `=> part list mmc 0` 里 GPT 完全正确（`boot_a/boot_b/system_a/system_b…`）。
根因：`RK_AB_UPDATE=y` **传不到 u-boot**，它按非 A/B 找 `boot`。
已修：`image/uboot/rk3568-assistant-ab.config`（开 `CONFIG_ANDROID_AB`）+
板级 defconfig 的 `RK_UBOOT_CFG_FRAGMENTS`。**别再删这两处。**

### 5.2 `No bootable slots found` → A/B 元数据不可引导（会掉进 fastboot）

串口表现：
```
U-Boot SPL ... No bootable slots found, use lastboot.
U-Boot ... No bootable slots found.
rk_avb_append_part_slot: failed to get slot suffix !
FIT: No boot partition
... Android boot failed, error -1.
Enter fastboot...OK
```
**两个独立成因，都已经修掉**（2026-10-02 各踩一次）：

1. **反复开机把计数扣光**：SPL 与 u-boot 每次启动都把当前槽 `tries_remaining`
   减 1（`common/spl/spl_ab.c`、`lib/avb/rk_avb_user/rk_ab_ops_user.c`），而镜像里
   没有东西置 `successful_boot` → 扣到 0 两个槽都判死。
   **已修**：`ab-mark.service`（开机早期把当前槽按 **AVB 格式**写成 `successful_boot=1`）。
2. **刷机根本不写 misc，元数据是上一次的残留** —— 而残留里 `tries=0`：
   只要这次启动失败一次，两个槽立刻全死；而 `ab-mark` 要等系统起来才能跑 →
   **死锁：刷完就再也起不来**（这次"黑屏 + WiFi 连不上"就是这个）。
   机制：`mk-updateimg.sh` 的 `gen_package_file()` 按分区表生成条目，**镜像文件
   存在才收进包** → `Image/` 里没有 `misc.img` 时刷机不写 `misc`。
   **已修**：板级 defconfig 开 `RK_MISC=y` + `RK_MISC_CUSTOM=y`
   + `RK_MISC_IMG="misc-assistant-ab.img"`，镜像由 `image/make-misc-img.py` 生成
   （48 KB，0x800 处是 AVB 默认状态：**两个槽都可引导**）→ `package-file` 里
   自动出现 `misc  misc.img` → **刷完就是可引导的**。
   ⚠ 判断有没有生效：`grep -w misc output/update-ab/Image/package-file` 应有输出。

**现场急救（不用重刷、不用拆机，5 分钟内）**：两种做法二选一 ——
1. 让板子停在 u-boot 提示符（`temp/serial-ctrlc.ps1` **持续发 Ctrl+C**；
   这块板的 u-boot 只认 Ctrl+C，窗口只有一两秒，手动敲基本来不及），
   然后**写回可引导的元数据**（推荐：保留槽位信息，不破坏 last_boot）：
   ```
   mmc dev 0
   mmc read 0x10000000 0x6004 1
   mw.l 0x10000008 0x0000070f      # 槽A: prio15 tries7 succ0
   mw.l 0x1000000c 0x0001070f      # 槽B: prio15 tries7 succ1
   mw.b 0x10000010 0x01            # last_boot = B
   mw.l 0x1000001c 0x37d933d1      # CRC32（大端在内存里是反的）
   md.b 0x10000000 0x20            # 先核对
   mmc write 0x10000000 0x6004 1
   reset
   ```
   ⚠ 上面那串是**这一份元数据**的固定值；改了槽位/计数就要自己重算 CRC
   （`image/make-misc-img.py` 里有现成的 `crc32_ieee()`）。
2. 或者干脆把 `misc` 置零（= 首刷时的空状态，SPL 会自己重写默认元数据）：
   ```
   part list mmc 0                 # 确认 misc 的起止 LBA（我们的是 0x6000–0x7fff）
   mw.b 0x10000000 0 0x400000
   mmc write 0x10000000 0x6000 0x2000
   reset
   ```
3. SPL 检测到元数据无效会**自己重写默认值**（日志：`Magic is incorrect. /
   Error validating A/B metadata from disk. Resetting and writing new A/B metadata
   to disk.`）→ 正常启动。
   ⚠ 如果只想标记成功而不清零，可以用镜像里的工具：
   `python3 /usr/lib/assistant/ab-mark.py`（幂等，会打印前后状态）—— 但这要
   系统已经能起来；起不来时只能用上面 u-boot 里的两条路。

### 5.3 串口**一个字符都没有**（连 SPL 都没打印）

先排除时序：抓取窗口要**在断电之前**就开着。还要分清"真的静默"和"已经停在
fastboot"：后者串口不会有新输出，但 **Windows 设备管理器里会出现
`USB download gadget`**（VID_18D1&PID_4D00）—— 看到它基本就是 §5.2 那种情况，
先在串口里发几次 Ctrl+C 抢提示符（fastboot 循环里 Ctrl+C 能退出来，不必断电），
再按 §5.2 修元数据。

若确实是完全静默 → loader 模式（按住 RECOVERY 上电，RKDevTool 能看到 LOADER）
重刷整包即可。

---

## 6. 2026-10-04 实测补记（A/B 切槽 / 刷分区 vs 整包 / host key）

### 6.1 `misc` 是这套 A/B 的**单点**
- U-Boot 环境里的 `bootargs` **本身不带槽信息**（实测只有
  `storagemedia=emmc androidboot.storagemedia=emmc androidboot.mode=normal`）；
  **槽后缀与 `root=PARTUUID=…` 是 U-Boot 每次从 `misc` 的 BCB 现拼进 cmdline 的**。
  ⇒ `misc` 一坏必然 `No bootable slots found` + `FIT: No bootpartition` + 掉 fastboot。
- 症状对照：正常启动的 cmdline 里有 `android_slotsufix=_b root=PARTUUID=…54aa`；
  坏了就只剩 6.1 说的那半段。急救按 §5.2。

### 6.2 切槽 = 改 `misc` 里两个槽的**优先级**
- 出厂那份 `misc-assistant-ab.img` 是 **A prio=15 / B prio=14** ⇒ **默认起 A 槽**
  （2026-10-04 实测：写它进板子后 cmdline 就是 `android_slotsufix=_a`）。
- 切到 B：把 B 抬到 15、A 降到 14（`image/make-misc-img.py` 的 `SLOT0_PRIORITY` /
  `SLOT1_PRIORITY` 是**模块常量**，不是命令行参数 —— 改常量再调 `build_metadata()`）
  → 写进 `/dev/mmcblk0p2` → 重启即起 B 槽（实测 ✓，CRC 自检 ok）。
- **板端从运行中的系统**写 BCB 的一行式（将来 OTA「指向新槽」就是这一步）：
  ```
  dd if=<misc.img> of=/dev/mmcblk0p2 bs=512 conv=fsync && sync && reboot
  ```
- 别用 `new-misc` 之外的东西试：写坏 `misc` 的表现**和"完全刷坏"一模一样**，
  别误判成镜像问题（这次就绕过一圈）。

### 6.3 刷写方式：**整包必然带 userdata**
- `update-ab.img` 的 `package-file` 含 `userdata userdata.img` ⇒ **整包刷会写掉 `/data`**
  （4.9 GB 模型、全部配置、日志都在那儿）。
- 想保住 `/data` 只有两条路：**① 分区刷**（只勾 `boot_a|b` / `system_a|b` / `misc`…，
  **不勾 `userdata`**）；**② 先删 userdata 再整包刷**。
- 分区刷的一个事实：`system` 那份镜像只有 **925 MiB**（`rootfs.ext2`）而槽是 3 GiB ——
  实测**没问题**（10-03 与 10-04 两版都是这么刷上来的），不必凑满槽。

### 6.4 新 rootfs 会换 **sshd host key**
- 刷完新镜像后 PC 侧第一次 ssh 会报 `WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!`
  ⇒ **正常现象**（新 rootfs 重新生成密钥），不是中间人：
  `ssh-keygen -R <板子IP>` 再连即可。它同时也是"确实起了新镜像"的旁证。


