# T15-2-11 首次刷板手册（K1 Mini：Ubuntu → 我们的 Buildroot 镜像）

> 状态：**准备就绪，等最后一步决定**（2026-09-29 22:30 写）。本文件是**执行时照着走**的稿子。
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

## 2. 两条刷机路线

### 路线 A：**板内本地升级**（不需要 USB 线、不需要按键）

板子自带了 `/usr/bin/updateEngine`（Rockchip 的 OTA 引擎）与 `/usr/bin/upgrade_tool`。
板子的 `/` 现在有 13 G 可用，放得下 759 MB 的镜像。

```bash
# ① 把镜像传到板子（PC 上执行；板子在 192.168.137.30）
scp <PC 上的 update.img> rk3568:/tmp/update.img
# ② 板上校验（必须与 §1 的 sha256 一致，不一致别继续）
ssh rk3568 'sha256sum /tmp/update.img'
# ③ 板上执行本地升级（先看清 updateEngine 的参数，各版本略有差别）
ssh rk3568 'updateEngine --help | head -30'
ssh rk3568 'updateEngine --image_url=/tmp/update.img --misc=update --save_dir=/tmp --reboot'
```

- 风险点：它是**在运行中的系统上写同一块 eMMC**，中途失败就得走路线 B 救砖。
  好处是**不用碰硬件**，也就不用你守在板子边上。
- 升级期间**别断电**；重启后板子应当进我们的 `assistant.target`。

### 路线 B：**USB loader 模式 + Windows 工具**（厂商标准路线，最稳）

SDK 里现成的工具：`$SDK/tools/windows/RKDevTool_Release_v3.37.zip`、
`upgrade_tool_v2.46.zip`、`DriverAssistant_v5.13.zip`（Windows）；
Linux 侧是 `rkbin/tools/upgrade_tool` 与 `tools/linux/Linux_Upgrade_Tool/`。

步骤（**需要你在板子边上**）：
1. Windows 上解压 `RKDevTool_Release_v3.37.zip`；若系统里没有 Rockusb 驱动，
   先装 `DriverAssistant_v5.13.zip` 里的驱动（要管理员权限）。
2. 板子**按住 RECOVERY 键**再上电/复位 → 工具里出现 **发现一个 LOADER/MASKROM 设备**。
3. `升级固件` 页 → `固件` 选 `update.img` → `升级`。
4. ⚠ WSL2 **不能直通 USB**（要 `usbipd-win`），所以这条路线在 Windows 侧做，
   不要在 WSL 里试。

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
