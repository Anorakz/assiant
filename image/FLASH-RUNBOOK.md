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
