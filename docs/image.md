# 系统镜像（路线 C：厂商 6.1 SDK + Buildroot 重建最小镜像）

> 这份文档是**镜像这件事的唯一真源**：决策、现状基线、SDK 盘点、缺口与风险、构建/刷机配方。
> 它不是"设计稿"—— 每一条都要求能追到证据（命令、路径、数字、文件）。改这份文档 = 改镜像方案，
> 所以每次改动都要说清"为什么"。
>
> 相关：[todo2.md](../todo2.md)（T15 任务顺序与验收口径）、[gui.md](gui.md) §1.1（无 X 形态）、
> [deploy.md](deploy.md)（当前部署方式）、[bench.md](bench.md)（性能口径）。

---

## 1. 决策记录（2026-09-28，用户拍板）

| # | 决策 | 影响 / 为什么记下来 |
| --- | --- | --- |
| D1 | **走路线 C：用厂商 Rockchip Linux 6.1 SDK + Buildroot 重建最小镜像** | 不是"在现有 Ubuntu 20.04 上裁剪"（那是路线 D，留作回退）。目标是**没有 X、没有桌面**，我们的 Agent + GUI 是唯一被拉起的东西 |
| D2 | **允许在 WSL 里联网构建** | Buildroot 的 `dl` 缓存里没有 Qt 源码 → 构建期要能拉到 `invent.kde.org` 的 Qt 5.15.11；Ubuntu 底板还要拉 base rootfs（走 C 则不需要） |
| D3 | **允许刷板**（eMMC；Maskrom 兜底） | 首次刷机优先用软件方式 `reboot loader`；进不去再请用户按 Maskrom 键 |
| D4 | **开发镜像 + 发行镜像都要** | 两套 defconfig：dev 带 sshd/调试工具/串口控制台，release 只留运行时 + 我们的 target。对应 T15-11 |
| D5 | **OTA 走无线** | ⚠ 这决定**分区表必须现在就定**（A/B 或 recovery+双 rootfs），不能等刷完再改 —— 见 T15-2-4 |
| D6 | **睡眠允许关闭显示**，但唤醒必须 **触摸 + CLI 双通道** | 息屏不等于停服务：触摸要能唤醒，CLI（串口/ssh/命令）也要能唤醒。归 T15-7，镜像侧要保证触摸设备与 CLI 通路都活着 |

---

## 2. 板端现状基线（要被替换掉的那一套）

### 2.1 硬件与系统

| 项 | 实测值 | 证据 |
| --- | --- | --- |
| 板子 | **KICKPI K1 Mini**（RK3568）。⚠ 板端设备树里的 model 字符串写的是 `Rockchip RK3568 KICKPI K1A Board` —— 那是**厂商旧镜像的命名**，硬件按 K1Mini 走 | `cat /proc/device-tree/model`；`/proc/device-tree/compatible` = `rockchip,rk3568-kickpi-k1a rockchip,rk3568` |
| 系统 | Ubuntu **20.04.6 LTS (focal)** | `/etc/os-release` |
| 内核 | **5.10.160** `#96 SMP ... aarch64` | `uname -a` |
| 存储 | eMMC **29.1 GB**，根分区 `/dev/mmcblk0p6` ext4 28.9 GB（已用 15 GB） | `lsblk`；`df -h /` |
| 已装包 | **1268** 个 dpkg 包 | `dpkg -l \| grep -c ^ii` |
| 启动参数 | `storagemedia=emmc androidboot.mode=normal rw rootwait earlycon=uart8250,mmio32,0xfe660000 console=ttyFIQ0 root=PARTUUID=614e0000-0000` | `/proc/cmdline` |

### 2.2 分区（厂商 Ubuntu 镜像的布局）

```
mmcblk0   29.1G
├─p1  4M     ├─p2  4M     ├─p3 64M    ├─p4 128M   ├─p5 32M    └─p6 28.9G (/) 
mmcblk0boot0 / mmcblk0boot1 : 4M each
```

> 重建镜像时这套分区表要**重画**：D5 选了无线 OTA，所以要预留第二份 rootfs（或 recovery 分区）。
> 见 T15-2-4。

### 2.3 启动与资源基线（改造前，用来对照"到底快了多少/省了多少"）

| 指标 | 实测 | 证据 |
| --- | --- | --- |
| 启动总时长 | **13.914 s** = 内核 3.167 s + 用户态 10.746 s；`graphical.target` 在用户态 10.689 s 到达 | `systemd-analyze` |
| 最大单项 | **`NetworkManager-wait-online.service` 7.421 s**（几乎占用户态 70%） | `systemd-analyze blame` |
| 其它大头 | usbdevice 2.557 s、dev-mmcblk0p6.device 2.194 s、e2scrub_reap 1.396 s、xrandr-startup 1.134 s、udisks2 1.068 s、systemd-resolved 0.986 s、blueman-mechanism 0.955 s、NetworkManager 0.904 s、loadcpufreq 0.901 s | 同上 |
| 内存 | `free -m`：total 3901，used **1471**（有 X）/ **1195**（无 X）、available 2654；桌面栈本体 ≈ **703 MB** | `free -m`；T15-1 1d 脚本 |
| 空闲 CPU（无 X，GUI 在跑） | 整机忙 **25.4%**；其中 **GUI 1.0~1.1% CPU / 99 MB RSS**；Agent RSS 121 MB | `tests/board/t15_1d_regression_nox.py` |
| ⚠ 空闲 CPU 的凶手 | Agent 进程里 **`VideoDec` 线程 80.1% CPU（累计 8 分 12 秒）** —— Moonlight 串流解码线程，STUDY 模式也在拉流解码 PC 桌面 | 线程来自 `native/third_party/moonlight-common-c/src/VideoStream.c:350`；日志 `sunshine: app=Desktop ... sessionUrl=rtspenc://192.168.137.1:48010`。**归 T15-5/T15-6/T15-8** |

### 2.4 组件清单与来源（镜像必须复现的最小集合）

| 组件 | 板端版本 | 来源（现状） | 备注 |
| --- | --- | --- | --- |
| Qt5 | **5.12.8** | Ubuntu focal 官方包（qtbase5-dev / qtmultimedia5-dev / qtdeclarative5 / qt5-qml-module-* ） | GUI 依赖 Widgets/Network/Multimedia |
| Qt 虚拟键盘 | `qtvirtualkeyboard-plugin 5.12.8+dfsg-0ubuntu1` | focal 官方包 | ⚠ 还必须补 **四个 QML 运行时模块**：`qml-module-qtquick2`、`-qtquick-window2`、`-qtquick-layouts`、`-qt-labs-folderlistmodel`（缺了键盘是"白板"，见 [gui.md](gui.md) §1.1） |
| Mali 用户态 | **`libmali-bifrost-g52-g2p0-x11 1.9-1`**（`libmali.so.1.9.0`，45 MB） | 厂商 .deb | 名字带 `x11`，但**实际导出 35 个 `gbm_*` 符号** → EGLFS/KMS 可用（T15-1 1a/1b 实证） |
| MPP | `librockchip-mpp1 1.5.0-1` + `librockchip-vpu0 1.5.0-1` + `rockchip-mpp-demos` | 厂商 .deb | 视频硬解 |
| GStreamer Rockchip | `gstreamer1.0-rockchip1 1.14-4` | 厂商 .deb | `mppvideodec` / `kmssink` 在这 |
| RGA / ISP | `librga2 2.2.0-1` / `camera-engine-rkaiq 5.0x1.2-rc2` | 厂商 .deb | 2D 加速 / 3A |
| NPU 运行时 | `/usr/lib/librknnrt.so` **7,726,232 字节**（2026-09-18 手工替换，非 dpkg；原版备份 `librknnrt.so.1.4.0.bak`） | 手工投放 | ⚠ 与 SDK `external/rknpu2/runtime/Linux/librknn_api/aarch64/librknnrt.so` **大小完全一致** → 来源就是那套 |
| Python | **3.8.10**；PyYAML 5.3.1（apt）；**numpy 1.24.4 + opencv-python 4.8.0.74（pip）** | apt + pip | ⚠ 仓库里**没有** Python 依赖声明文件；`agent/` 里 `import yaml / cv2 / numpy` 都是懒加载（T15-2-7 要补声明） |
| yaml-cpp | focal 包 | apt | GUI 日程区**只读** config.yaml 用 |
| 网络管理 | NetworkManager **1.22.10**（`nmcli`）、sshd | apt | GUI 第 4 张卡片与 T15-9 都依赖 nmcli |
| LLM | `llm/bin/llama-server`（目录 17 MB）+ 模型 **`/home/kickpi/model` 4.9 GB** | 手工投放 | 镜像里要决定模型放 rootfs 还是独立分区（T15-2-7） |

### 2.5 已确认的板级事实（从板端**实际运行**的设备树反查，不是猜）

板端导出：`tar czhf /tmp/live-dt.tar.gz -C /sys/firmware devicetree/base` → PC 上 `dtc -I fs -O dts`，
得到 6065 行的 `live-k1a.dts`（**K1Mini 硬件的事实来源**）。

| 项 | 板端实测 | SDK 的 K1Mini dts | 结论 |
| --- | --- | --- | --- |
| 显示通路 | `dsi@fe060000` → `panel@0`（**MIPI0/DSI0**），`hactive 800 / vactive 1280`，`clock-frequency 68 MHz` | `rk3568-kickpi-k1Mini.dtsi` 默认只 include **HDMI**，MIPI 面板那几个 include 全是注释 | 必须**打开**面板 include |
| 面板型号 | init sequence 开头 `E0 00 / E1 93 / E2 65 / E3 F8 / 80 03 / E0 01` | 与 **`rk3568-kickpi-lcd-mipi0-10.1-800-1280-v2-k1Mini.dtsi`** 逐字节一致（8 寸那款是 `FF 98 81 03`，不是我们） | ✅ 变体 = **mipi0 / 10.1" / 800×1280 / v2** |
| 触摸 | `gt9xx@5d`，`compatible = "goodix,gt9xx"`，`gtp_resolution_x/y = 800/1280`，IRQ = `gpio3 PA3`（低有效） | 同为 goodix gt9xx，IRQ 也是 `&gpio3 RK_PA3` | IRQ 一致 ✅ |
| 触摸复位脚 | `goodix_rst_gpio = <0x3e 0x0e 0x00>` → 引脚号 **14（PB6）** | `goodix_rst_gpio = <&gpio0 RK_PB5 ...>` → **PB5** | ⚠ **不一致，待核对**（T15-2-3 第一件事） |

---

## 3. SDK 盘点（Rockchip Linux 6.1，KICKPI 定制版）

### 3.1 SDK 本体

| 项 | 值 |
| --- | --- |
| 位置（WSL） | `/home/anorak/rk3568_buildroot/linux-kernel-6.1/rk-linux6.1-2026060914/rk-linux6.1-2026060914` |
| 版本 | Rockchip Linux 6.1 SDK **V1.2.0（20250620）**，KICKPI 定制（`docs/cn/RK3566_RK3568/RK3566_RK3568_Linux6.1_SDK_Note.md`） |
| 仓库 | 私有 fork `oranth_sdk/rockchip/rk-linux6.1.git`，`master`，HEAD `544b6630a`，**工作树干净**；234,142 个跟踪文件（**整份 SDK**，不是"内核仓库套壳"，无 `.repo`） |
| 内核 | **6.1.141**（完整源码，152 个 rk3568 dts） |
| u-boot | **2017.09**（有 `rk3568_defconfig`；⚠ **没有 KICKPI 的 u-boot 配置**） |
| rkbin | `rk3568_bl31_v1.46.elf`、`rk3568_bl32_v2.16.bin`、`rk3568_ddr_1056MHz_v1.25.bin` |
| 交叉工具链 | `prebuilts/gcc/linux-x86/{arm,aarch64}`（只有工具链，**没有**预编译库/镜像） |
| 构建产物 | **什么都没编过**：无 `output/`、无 `rockdev/`、无 `buildroot/output`、无 `*.img` |

### 3.2 板级配置：K1Mini 是一等目标 ✅

板级 defconfig 不在 `device/rockchip/rk3568/`（这一代 SDK 改成 `.chips`），而在
`device/rockchip/.chips/rk3566_rk3568/`：

| 文件 | 内容 |
| --- | --- |
| `rockchip_rk3568_kickpi_k1Mini_ubuntu_defconfig` | `RK_KERNEL_DTS_NAME="rk3568-kickpi-k1Mini-linux"`、`RK_USE_FIT_IMG=y`、`RK_PARAMETER="parameter-buildroot-fit.txt"`、`RK_ROOTFS_SYSTEM=ubuntu` |
| `rockchip_rk3568_kickpi_k1Mini_debian_defconfig` | 同上但 `RK_ROOTFS_SYSTEM=debian` |
| `rockchip_rk3566_kickpi_k11c_{ubuntu,debian}_defconfig` | 另一块板（RK3566） |
| `boot.its` / `boot4recovery.its` / `zboot.its` / `parameter-buildroot-fit*.txt` | FIT 与分区模板 |

KICKPI 自己的 `batch_build.sh` **当前就在编 K1Mini 的 debian12 + ubuntu2404**（k3b/k11c/k8d 被注释掉）。
内核 dts `Makefile` 里没有 kickpi 的 dtb 目标 —— 这没问题，`device/rockchip/common/scripts/mk-kernel.sh`
是**按名字显式构建** `$RK_KERNEL_DTS_NAME.img` 的（但别指望裸 `make dtbs` 能出 kickpi 的 dtb）。

### 3.3 根文件系统四条路

| 路径 | 版本 | 评价 |
| --- | --- | --- |
| `buildroot/` | **2024.02** | **路线 C 选它**：能做最小；代价是要自己写 Qt5/VK 配置、外部供给 G52 libmali |
| `ubuntu/` | **24.04 noble**（`mk-rootfs-noble.sh`，从 cdimage 拉 base） | 与现镜像（20.04）不同代；保留 apt/NM/systemd，省事但"最小"程度差 |
| `debian/` | **12 bookworm** | 同上 |
| `yocto/` | poky + meta-rockchip | 最重，不选 |

### 3.4 关键组件在 SDK 里能不能拿到

| 组件 | buildroot 包? | 厂商 .deb? | 本地源码/二进制? | 结论 |
| --- | --- | --- | --- | --- |
| Qt5 | ✅ `package/qt5/*`（39 个模块，含 **qt5base / qt5declarative / qt5multimedia / qt5virtualkeyboard**），**5.15.11** | — | ❌ 源码不随树（`dl` 里也没有）→ 构建时从 `invent.kde.org/qt` 按 commit 拉 | 要写 Qt 配置片段；首次构建需联网 |
| Qt EGLFS | ✅ `qt5base` 支持 `-eglfs`、`EGLFS_DEVICE_INTEGRATION=eglfs_mali\|eglfs_kms\|eglfs_viv`、`-gbm`（需 `HAS_LIBGBM`） | — | — | ✅ 可开 |
| **G52 Mali** | ⚠ 包在（`rockchip-mali`，可请求 `BIFROST_G52`），但 **`external/libmali` 只有 valhall-g610**（RK3588），没有 G52 | ✅ **`ubuntu/packages/arm64/libmali/libmali-bifrost-g52-g24p0-x11-wayland-gbm_1.9-1_arm64.deb`** | — | **用 .deb 抽 .so 做本地包**（T15-2-5），不靠 grabber 联网 |
| MPP / gst-rockchip | ✅ `package/rockchip/rockchip-mpp`、`gstreamer1-rockchip`；源码在 `external/mpp` | ✅ `mpp/librockchip-mpp1_1.5.0-1`（**与板端同版本**） | ✅ `external/mpp`（CHANGELOG 1.0.11 / 2025-09-10） | ✅ 可编 |
| RKNPU2 | ✅ `package/rockchip/rknpu2` | ✅ `rknpu2/` | ✅ 预编译 `external/rknpu2/.../librknnrt.so`（**与板端同尺寸 7,726,232**） | ✅ 直接带 |
| RGA / RKAIQ | ✅ `rockchip-rga` / `camera-engine-rkaiq` | ✅ | ✅ | ✅ |
| WiFi/BT | ✅ `rkwifibt` + `wpa_supplicant` | ✅ | ✅ | ✅ |
| 网络管理 | ✅ **`package/network-manager`** | — | — | ✅ `nmcli` 保得住 |
| Python | ✅ python3 **3.11.8**、`python-numpy`、`python-pyyaml`、`opencv4` | — | — | ⚠ 与板端 **3.8** 不同代，要复验 |
| 中文字体/时区 | ✅ `font/chinese.config`、`dejavu`、`tzdata` | — | — | ✅ |
| systemd | ✅ 支持（`BR2_INIT_SYSTEMD`），但**默认是 busybox init**，只有 `products/electric.config` 用了 | — | — | ⚠ 我们的单元是 systemd 的 → **镜像里要显式开 systemd** |

### 3.5 构建环境（WSL Ubuntu-24.04）

| 项 | 值 |
| --- | --- |
| 磁盘 | `/` 1007 GB，**751 GB 可用** ✅ |
| CPU / 内存 | **24 核 / 7 GB**（⚠ 内存偏小：并行构建 Qt 要封顶 `-j8` + 开 ccache） |
| 工具链 | gcc / make / python3 / git / rsync / bc / cpio / unzip / wget / **dtc** 全在 ✅ |
| 下载缓存 | `buildroot/dl` 58 项，**无 Qt/mali** → Qt 必须联网；另有 `linux-kernel-6.1/rootfs/buildroot-dl-rk-linux6.1-20260801.tar.gz`（272 MB，历史缓存） |

---

## 4. 缺口、风险与应对

| # | 缺口 / 风险 | 应对 | 归属任务 |
| --- | --- | --- | --- |
| R1 | `external/libmali` **没有 G52**（只有 RK3588 的 valhall-g610） | 从 SDK 自带 `libmali-bifrost-g52-g24p0-x11-wayland-gbm_1.9-1_arm64.deb` 抽 `.so` 做成本地 buildroot 包；验收看 `nm -D` 的 `gbm_*` 数量 | T15-2-5 |
| R2 | Qt 5.15.11 源码要从 `invent.kde.org` 按 commit 拉（`dl` 里没有） | 允许联网；若慢/断则设 `BR2_PRIMARY_SITE` 镜像或把源码包先放进 `dl/` | T15-2-6 |
| R3 | WSL 只有 7 GB 内存，24 核全开必 OOM | 构建封顶 `-j8` + `BR2_CCACHE`；必要时夜里串行 | T15-2-9 |
| R4 | **触摸复位脚不一致**（板端 PB6 vs SDK dtsi PB5） | 用设备树 + pinctrl 逐项核对；拿不准就先按板端实测值改 dts 再刷 | T15-2-3 |
| R5 | 版本漂移：Qt **5.12→5.15**、Python **3.8→3.11**、libmali **g2p0→g24p0**、MPP 打包名 1.5.0 vs 源码 CHANGELOG 1.0.11 | 镜像出来后**在板上**复跑 T15-1 的三套脚本（1b/1c/1d）+ T14 验收；不通过就回退到对应版本 | T15-2-11 |
| R6 | **无线 OTA（D5）要求分区表现在就定** | 在 `parameter-*.txt` 上定 A/B 或 recovery 方案，并把"写谁/怎么回退"写进文档 | T15-2-4 |
| R7 | 首次刷板可能需要物理 Maskrom | 先试 `reboot loader`（软件进 rockusb）；失败再请用户按键 | T15-2-11 |
| R8 | 仓库**没有** Python 依赖声明，pip 装的 numpy/opencv 版本只在板端 | 生成并提交依赖清单（版本钉住），镜像按它装 | T15-2-7 |

---

## 5. 配方（T15-2-2 起）

配方由**本仓库**维护，用注入脚本装进厂商 SDK —— 厂商树只被注入、不被改：

```bash
# 幂等注入（可先 --dry-run 预演）
bash image/install-into-sdk.sh /home/anorak/rk3568_buildroot/linux-kernel-6.1/rk-linux6.1-2026060914/rk-linux6.1-2026060914
```

| 本仓库文件 | 注入到 SDK | 作用 |
| --- | --- | --- |
| `image/buildroot/configs/rockchip_rk3568_kickpi_k1mini_release_defconfig` | `buildroot/configs/` | buildroot defconfig（片段式）：base + chip + 中文字体/区域 + wireless + mpp + gst(video/audio) + npu2 + mali + 我们的 products 片段 |
| `image/buildroot/configs/rockchip/products/kickpi-k1mini-release.config` | `buildroot/configs/rockchip/products/` | 我们自己的片段：systemd + Qt5(EGLFS/虚拟键盘) + GPU 型号 + 输入 + NM + Python/OpenCV/yaml-cpp |
| `image/device/rockchip/.chips/rk3566_rk3568/rockchip_rk3568_kickpi_k1mini_release_defconfig` | 同名路径 | SDK 板级 defconfig：dts `rk3568-kickpi-k1Mini-linux`、FIT、`parameter-buildroot-fit.txt`、rootfs=buildroot |
| `image/install-into-sdk.sh` | —— | 注入脚本（打印 `新增/覆盖/已一致`，重复执行幂等） |

```bash
# 只配置（快，用来验证配方本身 —— T15-2-2 的验收就是这条）
cd <SDK>/buildroot
make O=output/rockchip_rk3568_kickpi_k1mini_release \
     rockchip_rk3568_kickpi_k1mini_release_defconfig
# 整机构建（内核 + u-boot + rootfs + 镜像，会联网拉 Qt 等源码 —— T15-2-9）
cd <SDK>
./build.sh rk3566_rk3568:rockchip_rk3568_kickpi_k1mini_release_defconfig
```

### 5.1 配置验收（2026-09-28 实测，只到"配置通过"，还没构建）

`make <defconfig>` 退出码 0，生成的
`buildroot/output/rockchip_rk3568_kickpi_k1mini_release/.config` 里逐项核对：

| 项 | 实测行 |
| --- | --- |
| systemd 作为 init | `BR2_INIT_SYSTEMD=y`（503） |
| 串口 CLI（D6 的"CLI 唤醒"通路） | `BR2_TARGET_SERIAL_SHELL_GETTY=y`（523）+ `BR2_TARGET_GENERIC_GETTY_PORT="ttyFIQ0"`（528） |
| 主机名/时区 | `=assistant`（495）/ `="Asia/Shanghai"`（545） |
| Qt Widgets + EGLFS | `QT5BASE_WIDGETS=y`（1521）/ `QT5BASE_EGLFS=y`（1535）/ `QT5BASE_DEFAULT_QPA="eglfs"` |
| 虚拟键盘 + 布局 | `QT5VIRTUALKEYBOARD=y`（1577）+ `_LANGUAGE_LAYOUTS="en_US zh_CN"`（1578） |
| QML 运行时（防"白板"） | `QT5DECLARATIVE=y` / `_QUICK=y` / `QT5QUICKCONTROLS2=y`（1566） |
| 视频 | `QT5MULTIMEDIA=y`（1563） |
| G52 + GBM | `ROCKCHIP_MALI_BIFROST_G52=y`（656）/ `_HAS_GBM=y`（664）/ `PROVIDES_LIBGBM="rockchip-mali"`（670）→ Qt 会拿到 `-gbm` |
| NPU / MPP | `RKNPU2=y`（639）/ `ROCKCHIP_MPP=y`（673） |
| 网络 | `NETWORK_MANAGER_CLI=y`（3704）/ `CA_CERTIFICATES=y`（2549）/ `OPENSSH_SERVER=y`（3731）/ `# BR2_PACKAGE_DROPBEAR is not set`（3587） |
| 运行时 | `PYTHON_NUMPY=y`（2199）/ `PYTHON_PYYAML=y`（2293）/ `OPENCV4_LIB_PYTHON=y`（2816）/ `YAML_CPP=y`（3016） |
| 触摸/输入 | `LIBINPUT=y`（2913）（+ `LIBEVDEV` / `MTDEV`） |

### 5.2 写配方的四条纪律（都是踩出来的）

1. **`#include` 行不能带尾注释** —— SDK 给 buildroot 打的片段合并器是 sed 实现的，会把注释
   当路径去 sed 读，直接 `The merge file 'configs/rockchip/#' does not exist. Exit.`。
2. **普通配置行也别带尾注释** —— 注释文本会被并进"值"里（实测 `CA_CERTIFICATES` 的"新值"
   就带上了注释）。
3. **注释里出现"符号全名 + 等号"这种写法会被当成配置解析** —— 实测它把一句注释当成了
   `CA_CERTIFICATES` 的新值。所以注释里提符号要省掉 `BR2_` 前缀、也不写等号。
4. **写的符号必须真的存在于本树**，否则 kconfig **静默丢掉**（"看起来写了、其实没生效"）：
   串口那条就吃过一次 —— 本树里必须先开 `BR2_TARGET_SERIAL_SHELL_GETTY`，它才会
   select `BR2_TARGET_GENERIC_GETTY`，只写后者无效。

### 5.3 已知缺口（配方"配置对"≠"能编出来"）

- **G52 的 Mali 用户态 .so 不在 SDK 里**：`external/libmali` 只有 valhall-g610。
  配置能选 G52，但构建时会找不到 `.so` → **T15-2-5** 用厂商 deb
  `ubuntu/packages/arm64/libmali/libmali-bifrost-g52-g24p0-x11-wayland-gbm_1.9-1_arm64.deb`
  里的二进制补上。
- **Qt 源码不在 `dl` 缓存里**（要联网拉 `invent.kde.org`）→ 见 R2。
- 我们的运行时文件（Agent/GUI 二进制、unit、模型 4.9 GB）还没进镜像 →
  **T15-2-7 / 2-8**（片段里给 overlay 留了位置，故意先不开）。

---

## 6. 任务列表（T15-2，已批准 2026-09-28）

顺序即依赖顺序；每条做完等验收。

| # | 任务 | 出口 |
| --- | --- | --- |
| 2-1 | 决策与配方文档定稿（本文档 §1–§4） | 文档能独立复述 D1–D6 |
| 2-2 ✅ | K1Mini 的 buildroot defconfig 落地（release 骨架 + **systemd** + Qt5 替 weston） | `make <defconfig>` 通过（退出码 0）、`.config` 里 systemd 与 Qt/EGLFS/虚拟键盘/G52/GBM/NPU/MPP/NM/Python 逐项在位（见 §5.1） |
| 2-3 | 板级对齐（面板变体已定 / 触摸复位脚 / WiFi / 以太网 / PMIC / 容量） | 差异表 + "要不要改 dts"结论，逐项带证据 |
| 2-4 | 分区与 OTA 布局定稿（A/B 或 recovery；模型 4.9 GB 落点） | 分区表可落地 + 回退路径明确 |
| 2-5 | libmali G52(GBM) 进 buildroot | `gbm_*` ≥ 30，`BR2_PACKAGE_HAS_LIBGBM=y` |
| 2-6 | Qt5.15 + EGLFS + 虚拟键盘 + 四个 QML 模块 | 四目录 + `libqeglfs.so`/`libqtvirtualkeyboardplugin.so` 在位 |
| 2-7 | 我们的运行时与依赖闭环（python3/numpy/cv2/yaml、yaml-cpp、librknnrt、MPP+gst、NM、sshd、字体、llama-server+模型） | chroot 内逐项在位 + `ldd` 无缺失 |
| 2-8 | 只起我们的东西（`assistant.target` + agent + 无 X 的 GUI 单元） | `systemctl list-dependencies assistant.target` 只有我们的服务 |
| 2-9 | 首次完整构建（不刷板） | 构建成功 + 时长/体积/组件版本三组数字 |
| 2-10 | 刷板前验证（chroot 跑 ctest + 仓库 Python 套件 + ldd/符号检查） | 测试结果表（区分缺包 vs 只能板上测） |
| 2-11 | 首次刷板 + 板端基线（复跑 1b/1c/1d + T14 验收） | 板子起得来 + target 自动拉起 + 全绿 + 数字 |
| 2-12 | 开发镜像（同套 + sshd/调试工具/pytest/串口控制台） | `ssh` 能进、能在板上跑 pytest 与验收脚本 |

---

## 6. 附：盘点用到的命令（可复现）

```bash
# 板端：现状基线
uname -a; cat /etc/os-release; lsblk; cat /proc/cmdline
systemd-analyze; systemd-analyze blame | head -12
dpkg -l | grep -c ^ii
dpkg -l | grep -i -e mali -e rockchip -e rknn -e rknnrt
python3 -m pip list | grep -i -e numpy -e opencv -e yaml

# 板端：把**实际运行**的设备树导出来（镜像 dts 的事实来源）
tar czhf /tmp/live-dt.tar.gz -C /sys/firmware devicetree/base
# PC 侧（WSL 里有 dtc）：
dtc -I fs -O dts /tmp/livedt/devicetree/base > live-k1a.dts

# SDK 侧：板级配置与组件
cd <SDK>
cat device/rockchip/.chips/rk3566_rk3568/rockchip_rk3568_kickpi_k1Mini_ubuntu_defconfig
cat buildroot/configs/rockchip_rk3568_defconfig     # 片段式：#include "chips/rk3566_rk3568_aarch64.config" ...
ls ubuntu/packages/arm64/libmali | grep -i g52      # G52 的 gbm flavor 在这里
ls external/rknpu2/runtime/Linux/librknn_api/aarch64/
```
