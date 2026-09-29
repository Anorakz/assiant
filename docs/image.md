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
| 工具链（buildroot 侧） | ⚠ **buildroot 会自己编一套 glibc 工具链**：`BR2_TOOLCHAIN_BUILDROOT=y` / `BR2_TOOLCHAIN_EXTERNAL` 未开 / GCC **13.4.0** / `BR2_KERNEL_HEADERS_AS_KERNEL`（→ 6.1）/ `BR2_GCC_TARGET_CPU="cortex-a55"`。SDK 里那份 `prebuilts/gcc/linux-x86/aarch64/gcc-arm-10.3-2021.07-…` 只给**内核/u-boot** 用。⇒ 首次构建里有一段 40 分钟量级的"编工具链"（gcc-initial → glibc 头 → gcc-final → glibc），之后是缓存；`BR2_CCACHE` 目前**没开**（R3，2-9 决定） |
| 下载缓存 | `buildroot/dl` 58 项，**无 Qt/mali** → Qt 必须联网；另有 `linux-kernel-6.1/rootfs/buildroot-dl-rk-linux6.1-20260801.tar.gz`（272 MB，历史缓存） |
| ⚠ PATH 陷阱 | WSL 默认把 **Windows 的 PATH 接到 Linux PATH 后面**，里面有 `/mnt/c/Program Files/...` 这种**带空格**的条目 → buildroot 第一步 `support/dependencies/dependencies.sh` 直接判死：`This doesn't work. Fix you PATH.`（`dependencies.mk:27`，退出码 2）。看上去像 buildroot 坏了，其实与环境有关。**构建一律走 `image/sdk-make.sh`**，它把带 `/mnt/` 或带空格的条目剔掉再 exec make（`tests/test_image_recipe.py` 用假 `make` 把这件事钉住了） |

---

## 4. 缺口、风险与应对

| # | 缺口 / 风险 | 应对 | 归属任务 |
| --- | --- | --- | --- |
| R1 ✅ | `external/libmali` **没有 G52**（只有 RK3588 的 valhall-g610） | 已闭环（§5.4）：`image/prepare-libmali.sh` 从 SDK 自带的厂商 deb 里抽 `.so`，按 buildroot 期望的文件名落位，并校验两级 sha256 + `SONAME` + `gbm_*` 符号数 | T15-2-5 ✅ |
| R2 ✅ | Qt 5.15.11 源码要从 `invent.kde.org` 按 commit 拉（`dl` 里没有） | 已闭环（§5.5）：KDE 会**重新打包**同一 commit 的归档 → 与 buildroot 钉的 sha256 不符，构建在下载校验就死。先证明包内 LICENSE 逐文件哈希与 buildroot 所钉一致（内容没问题），再由 `image/prime-dl.sh` 从 buildroot 的 primary site 取**与 hash 一致的那份**预置进 `dl/`。**没有改 buildroot 的 hash，也没改厂商树** | T15-2-6 ✅ |
| R3 | WSL 只有 7 GB 内存，24 核全开必 OOM | 构建封顶 `-j8` + `BR2_CCACHE`；必要时夜里串行 | T15-2-9 |
| R4 | **触摸复位脚不一致**（板端 PB6 vs SDK dtsi PB5） | 用设备树 + pinctrl 逐项核对；拿不准就先按板端实测值改 dts 再刷 | T15-2-3 |
| R5 | 版本漂移：Qt **5.12→5.15**、Python **3.8→3.11**、libmali **g2p0→g24p0**、MPP 打包名 1.5.0 vs 源码 CHANGELOG 1.0.11 | 镜像出来后**在板上**复跑 T15-1 的三套脚本（1b/1c/1d）+ T14 验收；不通过就回退到对应版本。⚠ libmali 这条**已经确定会漂**：镜像装的是 SDK 的 **g24p0**，而板端 Ubuntu 现在是 **g2p0**（§5.4），所以 2-11 必须专门验 GPU/EGLFS | T15-2-11 |
| R9 | 路线 C 说"没有 X"，但 Mali blob 的 `DT_NEEDED` 硬依赖 `libX11 / libxcb / libwayland-*` → 镜像里必然带 X/Wayland 的**客户端库**（§5.4） | 已确认无解（blob 只有合集版、也只有 G52 这一份）：接受，但**不开 X server、不开桌面、Qt 只跑 EGLFS**（`QT5BASE_XCB` 保持关）。实测代价 **3,760 KB**（§5.4）；真要去掉得向厂商索取 GBM-only blob | T15-2-5 ✅ |
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
| `image/install-into-sdk.sh` | —— | 注入脚本（打印 `新增/覆盖/已一致`，重复执行幂等；最后会调 `prepare-libmali.sh`） |
| `image/prepare-libmali.sh` | `external/libmali/lib/aarch64-linux-gnu/` | 从 SDK 自带的厂商 deb 里抽出 G52 的 `libmali.so.1.9.0`，按 buildroot 期望的文件名落位并做指纹/符号校验（T15-2-5，见 §5.4） |
| `image/sdk-make.sh` | —— | **构建入口**：剔掉 PATH 里带空格的 Windows 条目后 `exec make`（见 §3.5 的 PATH 陷阱） |
| `image/prime-dl.sh` | `<SDK>/buildroot/dl/<包名>/` | 把 Qt 源码包**原始的、与 buildroot 钉的 sha256 一致的那份**从 primary site 预置进下载缓存（T15-2-6，见 §5.5） |

```bash
# 只配置（快，用来验证配方本身 —— T15-2-2 的验收就是这条）
bash image/sdk-make.sh <SDK> rockchip_rk3568_kickpi_k1mini_release_defconfig
# 单包构建（例如只验 Mali —— T15-2-5 的验收）
bash image/sdk-make.sh <SDK> rockchip-mali
# 整机构建（内核 + u-boot + rootfs + 镜像，会联网拉 Qt 等源码 —— T15-2-9）
cd <SDK> && ./build.sh rk3566_rk3568:rockchip_rk3568_kickpi_k1mini_release_defconfig
```

> `sdk-make.sh` 里的 `make O=output/<cfg> -j8` 可以照抄手打，但**必须先把 PATH 里的
> Windows 条目去掉**，否则连 `make` 第一步都过不去（§3.5）。整机构建的 `./build.sh`
> 也要注意同一个坑。

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

- **G52 的 Mali 用户态 .so 不在 SDK 的 `external/libmali` 里**：那里只有 valhall-g610
  （RK3588）。G52 的 blob 厂商**放在 SDK 里**了，只是放在 Ubuntu rootfs 那套 deb 里，
  而不是 buildroot 会去找的 `lib/` 下 → **T15-2-5 已闭环**，做法与文件名推导见 §5.4。
- **Qt 源码不在 `dl` 缓存里**（要联网拉 `invent.kde.org`）→ 见 R2。
- 我们的运行时文件（Agent/GUI 二进制、unit、模型 4.9 GB）还没进镜像 →
  **T15-2-7 / 2-8**（片段里给 overlay 留了位置，故意先不开）。

### 5.4 G52 的 Mali blob：从哪来、叫什么名字、为什么名字是关键（T15-2-5）

#### 来源与指纹

| 项 | 值 |
| --- | --- |
| 载体 | SDK 自带 `<SDK>/ubuntu/packages/arm64/libmali/libmali-bifrost-g52-g24p0-x11-wayland-gbm_1.9-1_arm64.deb`（15,901,824 B） |
| deb 的 sha256 | `c8707755f9e3734c051cd6b71b63214290e70cc653eea7de799e013fe67098de` |
| 里面的本体 | `usr/lib/aarch64-linux-gnu/libmali.so.1.9.0`，**56,387,136 B**，`SONAME=libmali.so.1` |
| 本体的 sha256 | `e208194f2ec03a35f15fce3141ccfa2fac954feebe59c368fb6e9b82f32f8bdf` |
| 导出符号 | `gbm_*` **39**、`egl*` 121、`gl*` 654、`cl*` 152、`vk_*` **0** |
| 落位 | `<SDK>/external/libmali/lib/aarch64-linux-gnu/libmali-bifrost-g52-g24p0-x11-wayland-gbm.so` |
| 谁做的 | `image/prepare-libmali.sh`（幂等；deb 与 .so **两级 sha256 都对得上**才动手，另外自检 SONAME 与 `gbm_*` 数量） |

**blob 不进本仓库**（56 MB，git 里不合适）：它从 SDK 自带的那个 deb 里现取，
所以"配方"里只留它的**指纹与期望文件名**，而不是二进制本体。

那个 deb 里另有 6 个 stub（`mali/lib{EGL,GLESv1_CM,GLESv2,MaliOpenCL,gbm,wayland-egl}.so*`）、
`libmali-hook.so.1.9.0`、`mali.pc`、`/etc/ld.so.conf.d/00-aarch64-mali.conf`、
`/etc/OpenCL/vendors/mali.icd` —— 那是 Ubuntu deb 那套的交付方式，buildroot 这一侧
**不需要**（meson 会自己生成 wrapper 与 pkgconfig，见 §5.1）。

#### 文件名是怎么"算"出来的（不是猜的）

`buildroot/package/rockchip/rockchip-mali/rockchip-mali.mk` 把
`-Dgpu -Dversion -Dsubversion -Dplatform` 交给 `external/libmali/meson.build`，
meson 再调 `scripts/grabber.sh`，后者在 `optimize_<O>/<arch>*/` 下按
`.*libmali-<gpu>-<version>[-<subversion>]-<platform>.so` 去 `find`。各段的实际取值：

| 段 | 值 | 出处（都能在树的原文里点到） |
| --- | --- | --- |
| gpu | `bifrost-g52` | `rockchip-mali/Config.in` 第 55 行（我们的片段选了 BIFROST_G52） |
| version | `g24p0` | 同文件第 65 行 |
| subversion | **空** | 同文件第 68–71 行：只有 px3se / utgard-400 有默认值。`grabber.sh` 里 `${4:-none}` 会把空当 `none` 处理 |
| optimize | `O3` → 目录 `optimize_3`（本树里就是 `lib` 的软链） | 同文件第 75 行默认值 + `grabber.sh` 第 25–29 行 |
| platform | `x11-wayland-gbm` | `rockchip-mali.mk` 第 42–86 行按**已启用的 winsys** 拼：HAS_X11→x11、HAS_WAYLAND→wayland、HAS_GBM→gbm（HAS_OPENCL 关着才会多一个 nocl） |

⇒ **`libmali-bifrost-g52-g24p0-x11-wayland-gbm.so`**，与厂商自己的命名一致
（那个 deb 就叫这个名字，`lib/` 下已有的 `libmali-valhall-g610-g24p0-x11-wayland-gbm.so`
是同一规格）。脚本最后用厂商自带的两个脚本互相印证：
`grabber.sh` 必须**找得到**这个文件，`parse_name.sh --format` 必须把这个文件名**反解回原名**
（后者证明我们不是"凑了一个能匹配的名字"，而是厂商的规范格式）。

`tests/test_image_recipe.py` 又把这条**跨文件不变量**钉进 CI：片段里的 winsys 选择
→ 算出的平台串 → `prepare-libmali.sh` 里的 `PLATFORM`/`GPU`/`VERSION`，三者必须一致。
改了片段却没同步脚本 = 只有真构建才会炸的 `ERROR: Failed to find matched library`。

#### ⚠ 由此确定的一件事：这个 blob **硬依赖 X11/Wayland 的客户端库**

`readelf -d` 的 `DT_NEEDED`（**整库加载级**：ld.so 载入 libmali.so 时这些必须都在，
否则 EGLFS 连 EGL 都拿不到）：

```
libdrm.so.2  libwayland-client.so.0  libwayland-server.so.0
libX11.so.6  libX11-xcb.so.1  libxcb.so.1
libxcb-dri2.so.0  libxcb-dri3.so.0  libxcb-xfixes.so.0  libxcb-present.so.0
libstdc++.so.6  libm.so.6  libpthread.so.0  libdl.so.2  libc.so.6  libgcc_s.so.1
```

所以"没有 X"的镜像里**必须**有 X11/Wayland 的**客户端库** → 片段里开 `XORG7` + `WAYLAND`
（`HAS_X11` 依赖前者、`HAS_WAYLAND` 依赖后者；**依赖不满足时 kconfig 会让这两个符号直接消失**，
平台串会悄悄变成别的值，blob 就找不到了）。meson.build 也会按 blob 里的
`libxcb.so` / `libwayland-client.so` 两个字符串判定 has_x11 / has_wayland，再
`dependency()` 这些包 —— 不启用连 meson 配置阶段都过不去。

**这不是"要装 X/桌面"**：没有 X server、没有桌面，`QT5BASE_XCB` 保持关、QPA 仍是 eglfs
（`tests/test_image_recipe.py` 把这两条也钉住了）。真要去掉只能是向厂商索取 GBM-only 的 blob。

#### 构建实测（2026-09-28，`make rockchip-mali`）

命令：`bash image/sdk-make.sh <SDK> rockchip-mali` → **退出码 0**，耗时 **22 分 46 秒**
（19:24:03 → 19:46:49）。这段时间里还**从零编了一整条交叉工具链**（见 §3.5），
属首次构建的一次性成本，后面都是缓存。

| 验收项 | 实测 |
| --- | --- |
| `make rockchip-mali` | 退出码 **0**；日志末尾 `fixup_dummy.sh lib optimize_3/aarch64-linux-gnu/libmali-bifrost-g52-g24p0-x11-wayland-gbm.so` |
| meson 到底用了哪个 blob | `Building for aarch64\|bifrost-g52\|g24p0\|\|x11-wayland-gbm\|O3`、`Source libraries: ['optimize_3/aarch64-linux-gnu/libmali-bifrost-g52-g24p0-x11-wayland-gbm.so']`、`Using … with x11 wayland gbm` —— **决定性证据** |
| 进镜像的 blob | `target/usr/lib/libmali.so.1.9.0` = **56,387,136 B**，sha256 `e208194f…`（与源 blob **逐字节相同**） |
| `gbm_*` 符号 | **39**（要求 ≥ 30） |
| SONAME / 软链 | `libmali.so` → `libmali.so.1` → `libmali.so.1.9.0`，`SONAME=libmali.so.1` |
| blob 的 `DT_NEEDED` | **16/16 在镜像里找得到**（缺失 0） |
| 提供的虚拟包 | `BR2_PACKAGE_HAS_LIBGBM=y`、`BR2_PACKAGE_HAS_LIBEGL=y` |
| wrapper / pkgconfig（T15-2-6 的 Qt5 要用） | sysroot 里 `libEGL.so.1`/`libgbm.so.1`/`libGLESv2.so.2`/`libGLESv1_CM.so.1`/`libOpenCL.so.1` 都指到 `libmali.so.1`；`egl.pc`/`gbm.pc`/`glesv2.pc`/`glesv1_cm.pc`/`OpenCL.pc`/`mali.pc` 在位；头 `EGL/ GLES/ GLES2/ GLES3/ KHR/` + `gbm.h` 在位 |
| meson 的依赖检查 | `Dependency libdrm found: YES 2.4.124`、`Dependency x11 found: YES 1.8.7` —— 不开 XORG7/WAYLAND 就卡在这里 |

`egl.pc` 自己就把这件事写明白了（`Requires: libdrm, wayland-client, wayland-server, x11, xcb,
x11-xcb, xcb-dri2`）—— 这几条**不是我们想要，是 blob 要**。

副作用（都记在案，不值得为它去改厂商包）：

- **X11/Wayland 客户端库占 rootfs 3,760 KB**（按实际文件去重后统计）：`libX11.so.6.4.0` 1,457,464 B、
  `libxml2.so.2.12.5` 1,523,952 B（被 wayland 选中）、`libxcb.so.1.1.0` 220,688 B、
  `libexpat.so.1.9.1` 176,304 B、`libwayland-server.so.0.23.1` 112,952 B、
  `libdrm.so.2.124.0` 111,912 B、`libwayland-client.so.0.23.1` 86,576 B 等。
- **开发文件也进了 rootfs**：`usr/include/{EGL,GLES,GLES2,GLES3,CL,KHR}` + `gbm.h` + 6 个 `.pc`，
  合计 **276 KB**。成因是厂商 meson.build 把 headers/pkgconfig 无条件装进 prefix，而
  buildroot 的 `INSTALL_STAGING` 会让 install 跑两遍（一遍 staging、一遍 target）——
  这是**厂商包的行为，不是我们加的**。release 镜像要不要剔掉，等 2-9/2-12 一起看体积时再定。
- 跑完之后 SDK 那棵树的 `git status` 会多出 8 项：4 个配置/分区表、3 个 `-assistant` 内核
  dts/dtsi、1 个 blob —— 正好等于"我们注入了什么"的清单（注入不写进厂商仓库）。

### 5.5 Qt 5.15.11：源码从哪来、为什么会被 hash 校验拦下、四个 QML 模块装在哪（T15-2-6）

#### 来源，以及"KDE 重新打包"这件事

这个 buildroot 的 Qt5 **不走官方发行 tarball**，而是 `invent.kde.org` 的**按 commit 现生成**归档：

```
QT5_SITE       = https://invent.kde.org/qt/qt
QT5BASE_SITE   = $(QT5_SITE)/qtbase/-/archive/$(QT5BASE_VERSION)
QT5BASE_SOURCE = qtbase-$(QT5BASE_VERSION).tar.bz2      # VERSION 是 40 位 commit
```

`make qt5base` 直接死在**下载校验**（不是编译）：

```
ERROR: qtbase-da6e958...tar.bz2 has wrong sha256 hash:
ERROR: expected: 935d01f5c34903ad9e979431cec7a8a59332ed3fc539e639f5ba87e8d6989b9d
ERROR: got     : 3067c4d84ba9927bfe65bf606c17af082199e0a3b22781fbf9bc6c6bc3de26dd
ERROR: Incomplete download, or man-in-the-middle (MITM) attack
```

**先证明那包内容没问题**（不然就不该往下走）：

| 证据 | 结果 |
| --- | --- |
| 同一 URL 连下两次 | 都是 57,931,736 B，sha256 **两次完全相同** → 不是传输损坏 |
| 包内 `.qmake.conf` | `MODULE_VERSION = 5.15.11` —— 就是这个版本 |
| 包内 `LICENSE.LGPLv3` / `LICENSE.GPL2` / `LICENSE.FDL` | sha256 与 buildroot 在**同一个 `.hash` 文件里钉的逐文件哈希完全一致** |

⇒ 同一个源码树，只是 KDE 换了外层打包方式（两份差 2,230 B）。

**修法：不改 hash，改"从哪拿"。** buildroot 的 primary site 上放着**与 hash 一致的那份**
（`sources.buildroot.net/<包名>/<源文件名>`，57,929,506 B，sha256 正是 `935d01f5…`）。
新增的 `image/prime-dl.sh` 把**已启用的** Qt 包逐个按 buildroot 钉的 sha256 校验后预置进 `dl/`：

```
qt5base / qt5declarative / qt5svg / qt5quickcontrols2 / qt5virtualkeyboard / qt5multimedia
→ 6 个全部"校验通过"（共 ~105 MB）
```

预置之后 buildroot 不会再 fallback 到 invent.kde.org 那份重打包归档。两个细节值得记：

- 脚本必须**展开 `.mk` 里的变量**才认得出这是 KDE 的归档（`_SITE` 写的是 `$(QT5_SITE)/…`）。
  第一版直接对文本匹配 `invent.kde.org` → 6 个包被**静默全跳过**（不报错、什么都不做），
  所以 `tests/test_image_recipe.py` 用一棵假 SDK 树把这个失败方式钉死了。
- 判断"哪些 Qt 包启用"优先读 `output/<cfg>/.config`；还没有 `.config` 时回落到我们自己的
  products 片段（配方里写了什么就是什么），所以注入之后、defconfig 之前也能先预置。

#### 验收实测（2026-09-28）

```
bash image/sdk-make.sh <SDK> qt5base qt5declarative qt5svg qt5quickcontrols2 qt5virtualkeyboard
→ 退出码 0，用时 9 分 58 秒（20:15:34 → 20:25:32）
```

| 验收项 | 实测 |
| --- | --- |
| Qt 版本 | `libQt5Core.so.5` 里就是 `Qt 5.15.11` |
| GUI 要用的库 | Core / Gui / Widgets / Network / Xml / DBus / Qml / Quick / Svg / QuickControls2 / QuickTemplates2 / VirtualKeyboard 全在 |
| **`libqeglfs.so`** | `target/usr/lib/qt/plugins/platforms/libqeglfs.so`（同目录还有 `libqvnc.so` / `libqminimal.so` / `libqoffscreen.so`） |
| EGLFS 的 KMS/GBM 通路 | `plugins/egldeviceintegrations/libqeglfs-kms-integration.so`（200,720 B）与 `…-kms-egldevice-integration.so`；`libqeglfs.so` 的 `DT_NEEDED` 里**直接有 `libmali.so.1`**（还有 `libdrm`、`libinput`、`libmtdev`） |
| **`libqtvirtualkeyboardplugin.so`** | `target/usr/lib/qt/plugins/platforminputcontexts/`（56,440 B），`DT_NEEDED` 全解（缺失 0） |
| 四个 QML 运行时模块 | `usr/qml/QtQuick.2/`、`usr/qml/QtQuick/Window.2/`、`usr/qml/QtQuick/Layouts/`、`usr/qml/Qt/labs/folderlistmodel/`，每个都有 `qmldir` + `plugins.qmltypes` + 插件 `.so`；四个插件的 `DT_NEEDED` **缺失都是 0** |
| 虚拟键盘的 QML | `usr/qml/QtQuick/VirtualKeyboard/`（+ `Styles/`、`Settings/`），qmldir 头上写着 `depends QtQuick 2.0 / QtQuick.Window 2.2 / QtQuick.Layouts 1.0 / Qt.labs.folderlistmodel 2.1` |
| 语言布局 | 片段写的是 `en_US zh_CN`。configure 行 `CONFIG+="lang-en_US lang-zh_CN"`；`src/virtualkeyboard/config.pri` 第 83/91 行确实有 `lang-en(_GB)?` 与 `lang-en(_US)?` → **en_US 是合法布局**（不用改）。编进去的 qrc 里 `layouts/en_US/*.fallback` 5 条；`zh_CN` 走拼音插件（`layouts/zh_CN/{main.qml,symbols.qml,…}`），`libQt5VirtualKeyboard.so.5` 里有 `Pinyin` |
| 软件渲染后端 | `libQt5Quick.so.5` 里有 56 个 software 符号（`QSGSoftwareRenderer*`）→ 现有的 `QT_QUICK_BACKEND=software` **仍然可用**（不过 Mali 的 GL 通路已经通了，它不再是必需项） |

#### QML 模块装在 `/usr/qml`（T15-1 1b 的卡点，在镜像里的机器化复现）

Qt 的 QML 模块在 **`/usr/qml`**（不是 `/usr/lib/qt/qml`；`/usr/lib/qt/` 下只有 `plugins/`），
全树共 **29 个 `qmldir`**。而 `QtQuick/VirtualKeyboard/qmldir` 自己就把 T15-1 1b 那四个依赖写在头上：

```
depends QtQuick 2.0
depends QtQuick.Window 2.2
depends QtQuick.Layouts 1.0
depends Qt.labs.folderlistmodel 2.1
```

—— 这正是"Ubuntu 上只装 `qtvirtualkeyboard-plugin` 就是白板"的原因（它不拉这四个），
也正是我们验收必须**逐个盯住这四个目录**的原因。现在四个都在，且依赖可解。

#### 体积（release 镜像的账，留到 2-9/2-12 一起算）

| 项 | 大小 |
| --- | --- |
| `usr/qml` | 11 MB |
| Qt5 运行库（`libQt5*.so.5.15.11`） | 48 MB |
| `usr/lib/qt/plugins` | 5.5 MB |
| **`usr/include/qt5`（在 target 里）** | **32 MB** |
| `usr/bin/qml*`（qml / qmlscene / qmlpreview / qmltime / qmltestrunner） | 388 KB |
| target 合计 | 313 MB |
| 下载缓存 `dl/`（含 Qt 六个包 ~105 MB） | 424 MB |

`usr/include/qt5` 的 32 MB、测试用二进制、以及 `QtTest` / `Qt/test` 这些模块，都是厂商打包行为的
副产品（`INSTALL_STAGING` 的副作用），**不是我们加的**；release 镜像要不要剔掉，统一放到体积账里决定。

### 5.6 我们的运行时与依赖闭环（T15-2-7）

#### 决策 D7：代码进 rootfs，可写状态进 userdata

A/B OTA 换的是**系统槽**。凡是"运行期会长出来"的东西都不能放在系统槽里 ——
换了槽就没了（用户设的模型参数被冲回默认、日志丢光、PID 文件残留）。所以分两处：

| 位置 | 放什么 | 为什么 |
| --- | --- | --- |
| **rootfs** `/usr/lib/assistant/` | `llm/bin/`（交叉编译的 llama-server + 8 个 ggml/llama 库）、`llm/scripts/`（起停脚本）、（2-8 的）agent / GUI / systemd 单元 | 属于"OS 的一部分"：跟着 A/B 槽一起升级 |
| **userdata** `/data/` | `assistant/config/`（`config.yaml`）、`assistant/llm/config/llm.env`（**派生文件**）、`assistant/llm/{run,logs}`、`assistant/{logs,run}`、`model/`（4.9 GB 模型） | 运行期状态与大数据：OTA 不该碰，也不该每次重下 |

落地靠三个小东西：

1. **`llm/scripts/*` 加两个位置覆盖点**（`LLM_ENV_FILE` / `LLM_STATE_DIR`）。原脚本把
   `config/llm.env`、`run/`、`logs/` 都算在 `llm/` 自己目录下；镜像里代码在 rootfs、状态在
   `/data`，没有这两个变量就必然写进系统槽。**不设它们时行为与板端那版完全一致**，所以板端现有那套不受影响。
2. **`llm/scripts/*` 收进仓库**：它们原先**不在仓库里**（板端手工投放，`git ls-files llm` = 0），
   也就是说只看仓库复现不出板端在跑的那套脚本。T15-2-7 把它们导进来，并改了一处实现：
   PID 身份判断从 `ps -p PID -o args=` 改成读 `/proc/<pid>/cmdline`（镜像里没有 procps，
   busybox 的 `ps` 对 `-o args=` 支持不保证）。来源与改动写在 `llm/README.md`。
3. **`/data` 挂载点与 fstab 用 post-build 追加**，**不能**放进 rootfs overlay：
   overlay 会**整份替换** `/etc/fstab`，而 buildroot 生成的那份里有根文件系统那行
   （`/dev/root / auto rw 0 1`），替换掉等于把根挂载项弄丢。

> ⚠ **踩到的坑：本树片段合并器的 `+=` 是"替换"不是"追加"。** 我先按厂商 chip 片段的写法用了
> `BR2_ROOTFS_OVERLAY+="board/.../rootfs-overlay"`，生成的 `.config` 里**只剩我们这一项** ——
> 厂商的 `board/rockchip/common/base board/rockchip/rk3566_rk3568/fs-overlay/` 被整条丢掉
> （base overlay 里有东西，丢了会直接坏根文件系统）。所以这两个符号在片段里**写全量值**
> （把厂商原有项原样抄在前面再加我们的），并加进 `tests/test_image_recipe.py` 的不变量里。

#### R8：Python 依赖清单（用 AST 扫出来的，不是猜的）

仓库里没有依赖声明文件，所以 T15-2-7 用 AST 扫了 `agent/ scripts/ tests/ gui/tools/` 的**真实 import**，
逐个定性。这张表就是 R8 的答案，也机器化在 `image/check-runtime-deps.py` 的 `PY_MODULES` 里：

| 模块 | 定性 | 镜像里的落点 | 说明 |
| --- | --- | --- | --- |
| `numpy` | required | `python-numpy`（1.25.0） | 视觉/帧管线的点采样（15 个文件在用） |
| `cv2` | required | `opencv4` + `LIB_PYTHON`（4.9.0） | 视觉/帧管线（6 个文件） |
| `yaml` | required | `python-pyyaml`（6.0.1） | 配置读写（9 个文件） |
| `psutil` | required | `python-psutil` | `rknnlite` 的 `Requires-Dist` |
| `ruamel.yaml` | required | `python-ruamel-yaml` | 同上 |
| `rknnlite` | required | `image/prepare-rknnlite.sh`（SDK 的 cp311 wheel） | SigLIP 走 NPU 的入口 |
| `openai` | optional | **镜像里没有**（`provider.py` 懒加载） | cloud 模式的可选 SDK |
| `tokenizers` | optional | **镜像里没有**（`tokenizer.py` 懒加载） | SigLIP 分词的可选后端 |
| `cryptography` | host | 只在 `scripts/pair_analyze.py` | 开发机分析工具 |
| `pytest` / `pytest_asyncio` | host | 只在 `tests/` | 测试框架 |
| `agent_native` | host | pybind11 扩展，镜像侧还没做 | 本地原生扩展 |

#### 镜像里新增/确认的组件

- **新开的包**：`bash`（`llm/scripts/*.sh` 是 bash 脚本，ash 跑不了）、`libcurl` + **curl 二进制**
  （`status.sh` 的健康检查；只开 libcurl 只给库、不给命令行工具，两个符号是分开的）、
  `python-psutil`、`python-ruamel-yaml`。
- **rknnlite**：SDK 里 `external/rknn-toolkit2/rknn-toolkit-lite2/packages/` 同时提供
  cp37/38/39/310/**311**/312 的 aarch64 wheel。这份 wheel 里**有 8 个编译扩展**
  （`rknn_runtime.cpython-311-aarch64-linux-gnu.so` 等），**必须按镜像的 python 版本挑** ——
  本树 python 是 **3.11.8**，正好有 cp311 那份；脚本按编译出来的 python 版本挑，对不上就报错退出，
  并且用 SDK 自带的 `packages.md5sum` 校验 wheel 指纹。
- **llama.cpp**：`image/build-llama.sh` **交叉编译**（用户 2026-09-28 批准的做法 A），
  钉板端验证过的 commit `b387ddfd8`（= build 10677），参数与板端逐条对齐
  （`BUILD_SHARED_LIBS` / `Release -O3` / `GGML_OPENMP` / `GGML_CPU_REPACK` / `LLAMA_CURL=OFF`），
  只有一项必须改：交叉编译不能 `GGML_NATIVE=ON`，改成 `GGML_CPU_ARM_ARCH=armv8.2-a+dotprod+fp16`。
  **仓库不放 16 MB 构建产物、也不用板端那份二进制。**
  源码**多源 + 逐源 sha256 校验**：实测 github 直连拿到过一个**只有 15.5 MB、缺
  `tools/server/server.cpp`** 的残档，而国内两个反代拿到的是完整的 37,067,727 B 且彼此逐字节相同。
- **字体**：`source-han-sans-cn`（**包名是 `source-han-sans-cn`，不是 `source-han-sans`**）+
  DejaVu + Liberation + FontAwesome。⚠ **Source Han Sans CN 装了 7 个字重，共 70 MB** —— 体积账。
- **MPP + gst**：`rockchip-mpp`（+ `rockchip-rga`）与 `gstreamer1-rockchip`，
  产出 `libgstrockchipmpp.so`（`mppvideodec` 那条硬解路，T15-5/6 要靠它把 80% 的 CPU 解码降下来）。

#### 验收：`image/check-runtime-deps.py`（就是"逐项在位 + ldd 无缺失"的机器化）

```bash
# 真 chroot 冒烟需要 root：sudo 或 wsl -u root 跑
python3 <SDK>/tools/assistant/check-runtime-deps.py <SDK>
```

它做三件事：① 逐项在位（MANIFEST）；② **DT_NEEDED 闭包** —— 按 ld.so 的真实搜索顺序
（它自己的 RPATH/RUNPATH，展开 `$ORIGIN` → target 的 lib 目录；**不含"可执行文件自己所在目录"**，
ld.so 并不搜它）解析每个 ELF 的依赖，覆盖**库与插件**（`ldd` 一次只能看一个文件，交叉场景下也跑不起来）；
③ chroot 冒烟（权限不足时如实标"跳过"，不假装通过）。

#### 验收实测（2026-09-28）

```
== 结论：通过（在位缺 0、依赖缺 0、冒烟失败 0）
```

| 段 | 实测 |
| --- | --- |
| 逐项在位 | **缺 0**（MANIFEST 全绿：二进制 / 库 / gst 插件 / QML / 字体 / python 模块 / llama 运行时） |
| **DT_NEEDED 闭包** | 检查 **1970 个 ELF**，**缺失 0** |
| chroot: python | `Python 3.11.8` |
| chroot: 视觉库 | `numpy 1.25.0` + `cv2 4.9.0`（真 import，不是看目录在不在） |
| chroot: NPU API | `from rknnlite.api import RKNNLite` → **ok**（cp311 wheel 在 3.11.8 上真能 import） |
| chroot: shell 工具 | `bash ok`、`curl 8.6.0 (aarch64-buildroot-linux-gnu) libcurl/8.6.0 OpenSSL/3.2.1`、`nmcli tool, version 1.44.2` |
| chroot: 硬解 | `gst-inspect-1.0 mppvideodec` 能起来（输出里那条 `mpp_soc: open /proc/device-tree/compatible error` 是 chroot 里没有设备树，板上不会有） |
| chroot: 中文 | `fc-list :lang=zh` → `Source Han Sans CN,思源黑体 CN` |
| chroot: LLM | `llama-server` 跑得起来（`version: 0.3.0-dev … built with GNU 13.4.0 for Linux aarch64`） |

> ⚠ **llama-server 的 `--version` 版本串不能当版本依据**：我们是**从 tarball 构建**（没有 `.git`），
> llama.cpp 的 cmake 会去 `git rev-parse` 外层目录，于是抓到了 **SDK 内核仓库的 commit**
> （`544b6630a`，那其实是内核的 HEAD），build 号也退化成默认值 103。
> **版本以 `.llama-source-commit`（`b387ddfd8`）与源码 tarball 的 sha256 为准。**

> ⚠ **验收器自己被抓出过一次假阴性**（值得记下来）：第一版的闭包检查把"可执行文件自己所在目录"
> 也算进搜索路径，于是 `llama-server` 缺 `libllama-server-impl.so`（**交叉编译产物一开始没有
> `$ORIGIN` rpath**）也被判成"解析得到"—— 是 chroot 冒烟那句
> `cannot open shared object file` 把它暴露出来的。两处都修了：
> 检查器不再搜可执行文件自己的目录（照 ld.so 的真实语义），`build-llama.sh` 加上
> `CMAKE_BUILD_RPATH/INSTALL_RPATH='$ORIGIN'` + `CMAKE_BUILD_WITH_INSTALL_RPATH=ON`
> （后者是为了不让**构建机的绝对路径**漏进产物），另外 `llm/scripts/start.sh` 再加一条
> `LD_LIBRARY_PATH` 兜底。修完 llama 三个主要产物 / 库的 RPATH 都只剩 `$ORIGIN`。

#### 途中修掉的三个"不修就编不出来/装不上"的问题

1. **厂商 MPP 快照缺件**：`external/mpp` 是 1.0.11（2025-09-10）的新源码，`CMakeLists.txt:42`
   **无条件** `include(.../build/cmake/merge_objects.cmake)`，而那个文件**整个 SDK 里都没有**
   （不是 .gitignore 吞的，是真没进快照）→ `make rockchip-mpp` 在 configure 就死
   （`Unknown CMake command "merge_objects"`）。上游 `develop`/`master` 上那份接口一致
   （2037 B / sha256 `4411cd9a…`），`image/prepare-mpp.sh` 按字节校验后补上。
   ⚠ 补完还要**删掉已经 rsync 过的 `output/<cfg>/build/rockchip-mpp-develop/`**，
   否则 buildroot 不会重新同步（`.stamp_rsynced` 还在）。
2. **厂商设的 GNU 镜像 403**：`BR2_GNU_MIRROR="https://mirrors.ustc.edu.cn/gnu"` 现在对本机
   **403 Forbidden**（http 那条直接超时）→ `bash` 的依赖 `readline` 拉不下来，构建停在下载阶段。
   实测 ftp.gnu.org / 清华 / 阿里 / 南大都 200，片段里改成清华源。
3. 上面那条 `+=` 的坑（差点丢掉厂商 overlay）。

#### 明确交给后面几步的事

- **2-8**：`assistant.target` + agent/GUI 的 unit；unit 里要把
  `LLM_ENV_FILE=/data/assistant/llm/config/llm.env`、`LLM_STATE_DIR=/data/assistant/llm` 传下去；
  agent/GUI 的 payload（代码与二进制）进 rootfs 也还没做。
- **2-9**：整机构建会**通过 post-build 自动带上** llama-server 与 rknnlite
  （`BR2_ROOTFS_POST_BUILD_SCRIPT` 里追加了我们的钩子）。
- **2-11**：首次刷机后 `userdata` 是**未格式化**的（要 `mkfs.ext4` 一次），
  4.9 GB 模型要投放到 `/data/model/`；`llm.env` 的 `LLM_MODEL_PATH` 指过去。
- **体积**：target 现在 **553 MB**（字体 70 MB、`usr/include/qt5` 32 MB、llama 约 16 MB）。

### 5.7 开机目标：起来之后只有我们的东西（T15-2-8）

#### 为什么自己写一个 target，而不是往 multi-user 上挂

挂 `multi-user.target` 只能保证"**我们的**起来了"，**保证不了"别人的没起来"** ——
厂商 base overlay 与各个包都可能往 multi-user / graphical 上挂东西（桌面、显示管理器、调试服务）。
所以镜像把 `default.target` **直接指到 `assistant.target`**：

```
/etc/systemd/system/default.target -> /usr/lib/systemd/system/assistant.target
```

`assistant.target` 里只有两条支撑：

| 依赖 | 为什么 |
| --- | --- |
| `Requires/After=basic.target` | **摘掉 graphical 之后必须自己接上系统d 的基座**（sysinit/udev/dbus 那一路都在 basic 下面）。删了它系统就起不来 —— 这是"精简"与"搞瘫"之间的那根线 |
| `Wants=assistant-init.service agent.service agent-gui.service` | 我们自己的三个（显式列出，静态可查；post-build 同时建 `.wants/` 符号链接，两条路 systemd 取并集） |
| `Wants/After=NetworkManager.service` | ⚠ **唯一一个不是我们的**直接依赖，理由见下 |

#### 单元（4 个，都在 `systemd/image/`）

| 单元 | 干什么 | 关键点 |
| --- | --- | --- |
| `assistant.target` | 开机目标 | 见上；`AllowIsolate=yes`（排查时能手动切） |
| `assistant-init.service` | **首启**把 userdata 上的目录与默认配置铺出来 | oneshot + `RequiresMountsFor=/data`；只复制（`cp -n`）**不软链** —— `config.yaml` 是用户可写的真源，软链会让写入落到 rootfs，正是 D7 要避免的 |
| `agent.service` | Agent 常驻 | root；`AGENT_CONFIG_DIR=/data/assistant/config`（顺带把派生 `llm.env` 定到 `/data/assistant/llm/config/llm.env`）、`AGENT_LOG`/`AGENT_CRASH_DIR` 也在 userdata、`PYTHONPATH=/usr/lib/assistant` |
| `agent-gui.service` | GUI 常驻（无 X） | EGLFS + 虚拟键盘那一组环境变量（T15-1 板端实测的那套），旋转走 `QT_QPA_EGLFS_ROTATION`；镜像里没有第二种形态，所以**不带 `-nox` 后缀** |

#### ⚠ NetworkManager 为什么必须显式要

实测：buildroot 的 `NETWORK_MANAGER_INSTALL_INIT_SYSTEMD` **只建了一个 dbus 别名**
（`/etc/systemd/system/dbus-org.freedesktop.NetworkManager.service`），
`etc/systemd/system/` 里**一个 `.wants` 都没有**；而它的 preset 文件是空的。
也就是说 vendor 那边是靠 **multi-user.target.wants** 启的 —— 而我们**已经不进 multi-user 了**。
更麻烦的是 `network-online.target` **自己不会拉起任何东西**（它只是"等别人把网弄好"）。
所以：

- `assistant.target` 里显式 `Wants=NetworkManager.service`；
- 另外把 `NetworkManager-wait-online.service` 挂进 `network-online.target.wants/`，
  否则 `agent.service` 的 `After=network-online.target` 是个**空等**。

不带它们的后果很具体：Agent 的 ssh / 云端 LLM / GUI 的 WiFi 卡片**全都没有网**。
`image/check-assistant-target.py` 把 NetworkManager（两个单元）列进**白名单**，
除此之外闭包里出现任何非我们的单元都算失败。

> 串口 CLI（D6 的"CLI 也能唤醒"）**不需要**额外 enable：`systemd-getty-generator`
> 会按内核 cmdline 里的 `console=ttyFIQ0` 自动生成 `serial-getty@ttyFIQ0.service`。

#### 单元有**两套**，而且不许悄悄漂

板端形态在 `systemd/`（指向 git 工作区 `/home/kickpi/myproject/assitant`），
镜像形态在 `systemd/image/`（指向 `/usr/lib/assistant` 与 `/data`）。
同一服务的行为必须一致 —— `tests/test_image_target.py` 会逐键比对，
**只允许差异表里那几项**（Description/Documentation、WorkingDirectory、ExecStart*、
StandardOutput/Error、Environment、WantedBy、After）。改一边忘另一边会当场红。

#### 验收：`image/check-assistant-target.py`（两种模式）

```bash
python3 image/check-assistant-target.py --units systemd/image   # 仓库模式（CI 里跑）
python3 image/check-assistant-target.py --sdk <SDK>             # 镜像模式（对着 target 树）
```

`systemctl list-dependencies` **没法离线用**（它要跟正在跑的 systemd 说话，
`systemctl --root=` 不支持子命令），所以这里**自己解析 unit 文件与 `.wants/`/`.requires/` 符号链接**
算闭包。镜像模式实测：

```
   default.target -> assistant.target
   assistant.target.wants      -> agent-gui.service agent.service assistant-init.service
   network-online.target.wants -> NetworkManager-wait-online.service
   闭包：我们的 agent-gui.service, agent.service, assistant-init.service, assistant.target
         基础设施 NetworkManager-wait-online.service, NetworkManager.service
== 通过：只有我们的服务（+ NetworkManager 这一个白名单基础设施）
```

判据三条：① `default.target` 必须指向 `assistant.target`；② 闭包里除 systemd 基座
（`basic/sysinit/local-fs/getty` 这些 target、`systemd-*`、`*.slice`、`*.mount`）之外，
只允许我们的单元与白名单基础设施；`graphical/display-manager/weston/slim/xorg/x11/bluetooth/cups/avahi`
**出现即失败**（单元文件里出现 X 相关字样、或还留着 `/home/kickpi/...` 路径也算失败）；
③ 单元自身的 `ExecStart` 必须在允许的可执行文件表里。

> ⚠ **静态闭包 ≠ 板上真跑**：这里证明的是"配置上没有别的东西被拉起来"。
> 板上真跑 `systemctl list-dependencies assistant.target` 与 `systemd-analyze` 是 **T15-2-11** 的事。

### 5.8 首次完整构建（T15-2-9）

#### 怎么跑

```bash
wsl -u root bash image/build-image.sh <SDK 根目录>
```

它做四件事：清洗 PATH → 放 `python` 垫片 → 起时钟看门狗 → `./build.sh <chip>:<defconfig>`（lunch）再 `./build.sh all`。
三条约束都写在脚本头部，都踩过：

- **必须 root**：`build.sh` 有一段**无条件**的 sudo 密码缓存（`sudo -n true` 失败就 `read -s`），
  非交互后台跑会卡死。root 跑完记得把 `$SDK/output`、`$SDK/buildroot/output` 属主改回来。
- **不要 export 任何 `RK_*`**：`build.sh` 会把"与 `initial.env` 不一致的 `RK_*`"当成*自定义环境*，
  弹一句 `Press enter to continue.` —— 非交互直接失败（实测：`RK_SESSION=...` 就是这么挂的）。
- **两条 output 路径别搞混**（这条最容易让人以为"白编了"）：

| 用途 | buildroot output 目录 |
| --- | --- |
| 我们自己的**单包**构建（`image/sdk-make.sh`，T15-2-5/6/7 用它） | `buildroot/output/rockchip_rk3568_kickpi_k1mini_release/` |
| vendor **整机构建**（`./build.sh all`，由 `mk-buildroot.sh` 第 12 行算出） | `.../rockchip_rk3568_kickpi_k1mini_release/rockchip_rk3568_kickpi_k1mini_release/` |

也就是整机构建用的是**另一棵全新树**（交叉工具链都重编一遍）。`buildroot/dl` 是共享的，
所以我们预置的 6 个 Qt 源码包两边都能用。

#### 三组数字（2026-09-28/29 实跑）

**① 时长**：**累计约 1 小时 40 分钟**。其中 22:49–00:03 那一段约 74 分钟（含**从零编交叉工具链**与 Qt），
中间按用户要求的停机点中断，之后 20:03–20:29 增量续跑约 26 分钟收尾。
（增量是真的增量：第二次进入时 178 个包目录都还在，只补了剩下的包与打包。）

**② 体积**（`$SDK/output/firmware/` 合计 1.3 GB）：

| 产物 | 大小 | 说明 |
| --- | --- | --- |
| `update-…-buildroot-2026092920.img`（= `update.img`） | **747,516,490 B ≈ 713 MiB** | 整套烧写镜像（A/B 形态，另存为 `update-ab.img`） |
| `rootfs.img` → `images/rootfs.ext2` | **679,477,248 B ≈ 648 MiB** | 装进 `system_a`/`system_b`（各 3 GiB 槽，余量充足） |
| `boot.img` | 41,900,544 B ≈ 40 MiB | FIT：kernel + dtb + resource |
| `oem.img` | 12,582,912 B ≈ 12 MiB | |
| `userdata.img`（首启镜像） | 8,388,608 B ≈ 8 MiB | 真实 userdata 是 22.8 GiB 的 grow 分区 |
| `uboot.img` / `MiniLoaderAll.bin` | 4,194,304 B / 481,728 B | loader 与 u-boot |
| `parameter.txt` | 579 B | **就是我们那份 `parameter-assistant-ab.txt`** ✓ |
| `BR/target`（根文件系统树） | 522 MB | 字体 70 MB、`usr/include/qt5` 32 MB 都在里面（账见 §5.5/§5.7） |

**③ 组件版本**：

| 组件 | 版本 |
| --- | --- |
| kernel | **6.1.141** |
| u-boot | **2017.09** |
| buildroot | 2024.02（自建交叉工具链 GCC **13.4.0**、glibc） |
| Qt5 | **5.15.11** |
| python3 | **3.11.8** |
| systemd | 254.9 |
| GStreamer | 1.24.13 |
| llama.cpp | **b387ddfd8**（= build 10677，交叉编译产物带 `$ORIGIN` RPATH） |
| rknnlite | rknn-toolkit-lite2 2.3.2（cp311 wheel） |
| MPP（源码） | 1.0.11（2025-09-10） |
| librknnrt / libmali | 7,726,120 B / 56,387,136 B（G52 g24p0） |

#### 产物核对（我们那套东西确实进了最终镜像）

| 检查 | 结果 |
| --- | --- |
| `etc/systemd/system/default.target` | → `/usr/lib/systemd/system/assistant.target` ✓ |
| `assistant.target.wants/` | `agent.service`、`agent-gui.service`、`assistant-init.service` ✓ |
| 我们的单元 | 四个都在 `/usr/lib/systemd/system/` ✓ |
| llama 运行时 | `usr/lib/assistant/llm/bin/llama-server`（post-build 里交叉编译出来的）✓ |
| llm 脚本 | `usr/lib/assistant/llm/scripts/start.sh` 等 ✓ |
| **recovery** | **没有 `recovery.img`** ✓（我们关掉了，见下） |

#### 路上挡道的六个坑（前四个落成脚本，后两个落成配方）

1. **PATH 里带 Windows 条目** → buildroot 依赖自检直接退出（`build-image.sh` 清洗）。
2. **`python` 这个名字缺失** → `check-kernel.sh` 写死要 `python`（Debian 习惯用 `python-is-python3`），
   WSL 只有 `python3` → 内核还没开始编就报 `Your python is missing`；脚本放 `python -> python3` 垫片，不动系统。
3. **宿主缺 `gettext`** → `Your msgmerge is missing`；已 `apt-get install gettext libssl-dev libelf-dev`。
4. **WSL 时钟偏移**：`systemd-timesyncd` 处于 active 但 unsynchronized，会**步进调时钟**，
   于是刚写出的文件 mtime "落在未来"，make 报 `Clock skew detected … 0.2s in the future`
   （打在 systemd、host-wayland 的 configure 上）。
   → 已 `stop + mask systemd-timesyncd`，并在构建入口加了每 60 秒夹一次未来时间戳的看门狗。
5. **厂商快照的 `libxcrypt.mk` 少了 host 变体** → `systemd` 的 `HOST_SYSTEMD_DEPENDENCIES` 要 `host-libxcrypt`，
   buildroot 只有包定义了 host 变体才会生成那个目标 → 整机构建死在
   `No rule to make target 'host-libxcrypt'`。
   注意 **target 侧没问题**（`BR2_PACKAGE_LIBXCRYPT=y` 开着、源码也下好了），缺的只是 host 变体。
   修法：`image/buildroot/package/libxcrypt/libxcrypt.mk`（我们仓库维护）**覆盖**厂商那份，
   照上游补上 `HOST_LIBXCRYPT_CONF_OPTS` 与 `$(eval $(host-autotools-package))`。
6. **recovery 段算出来的 defconfig 名不存在**：`Config.in.recovery` 里
   `RK_RECOVERY_BASE_CFG` 的第一条默认值是 `"" if RK_AB_UPDATE` —— 我们 `RK_AB_UPDATE=y` 先命中它，
   于是 `RK_RECOVERY_CFG = rockchip_${RK_CHIP_FAMILY}_recovery` = **rockchip_rk3566_rk3568_recovery**，
   而 SDK 里只有按**单芯片**命名的 `rockchip_rk3566_recovery` / `rockchip_rk3568_recovery` → 必然失败：
   `Makefile:1056: *** "Can't find rockchip_rk3566_rk3568_recovery_defconfig".  Stop.`
   （那句 `default RK_CHIP if RK_CHIP_FAMILY = "rk3566_rk3568"` 永远轮不到。）
   而且我们的分区表里**本来就没有 recovery 槽**（回退靠 A/B + `misc`）→ 在板级 defconfig 里关掉：
   `# RK_RECOVERY is not set`。实测有效：构建后 `output/final.env` 里**没有** `RK_RECOVERY` 这一项，
   `build_all` 里 `[ -z "$RK_RECOVERY" ] || mk-recovery.sh` 整段跳过，会话日志里 0 次提到 recovery。
   （`output/.config` 文本里那行仍显示 `y` —— 那是 lunch 的落盘形式，**以 `final.env` 与实际行为为准**。）

#### 交给后面几步的

- **T15-2-11 首次刷板**：这套 `update.img` 会**重排 eMMC 分区**（板上数据全部丢失）；
  `userdata` 首次要先格 ext4；4.9 GB 模型投放到 `/data/model/`。
- **T15-2-12 开发镜像**：同一套配方 + sshd/调试工具/pytest/串口控制台。
- 属主：root 跑过之后执行 `chown -R anorak:anorak $SDK/output $SDK/buildroot/output`
  （否则非 root 的 T15-2-10 chroot 测试会碰壁）。

---

## 6. 板级对齐（T15-2-3，已编译验证）

**方法**：以**板端实际运行**的设备树为准（`dtc -I fs /sys/firmware/devicetree/base` 导出 6065 行，
来自 5.10 厂商镜像 —— 触摸/显示/WiFi 都是好的），逐项对 SDK 的 K1Mini dts 链
（`rk3568-kickpi-k1Mini.dtsi` → `rk3568-kickpi-evb.dtsi` + eth/wifibt/相机/USB/IR/SATA/PCIe/40pin）；
然后把我们的 dts 用**内核自己的构建管线**编成 dtb，再反编译核对取值。

| 项 | 板端（live DT / 运行时） | SDK 的 K1Mini 链 | 结论 |
| --- | --- | --- | --- |
| 板名 | compatible `rockchip,rk3568-kickpi-k1a`，model "K1A Board" | `rockchip,rk3568-kickpi-k1Mini`，model "K1Mini Board" | 厂商旧镜像的命名差异，不影响 |
| 显示通路 | dsi0(`fe060000`)/`panel@0`，800×1280 @ 68 MHz，init sequence 与 **v2** 面板逐字节一致 | 默认只 include **HDMI**，MIPI 面板那几个 include 全注释 | **要改**：换成 `rk3568-kickpi-lcd-mipi0-10.1-800-1280-v2-k1Mini.dtsi` |
| 背光 / 面板电源 | `backlight-dsi0`（pwm4）+ `vcc3v3-lcd0-n`（**gpio0 PC7** 使能） | 同（都在 LCD 文件里） | ✅ 一致 |
| 触摸 | goodix gt9xx @ i2c5(`fe5a0000`) 0x5d，800×1280，IRQ **gpio3 PA3 低有效**，复位 **gpio0 PB6** | IRQ 一致；**复位属性写 PB5**，但同一文件里的 pinctrl 写的是 PB6 | **要改**：复位脚改 **PB6**（用覆盖文件） |
| WiFi / BT | `wifi_chip_type = "rtl8822cs"` | `rk3568-kickpi-wifibt.dtsi` 里正是 **rtl8822cs**（它覆盖了 evb.dtsi 的 ap6398s） | ✅ 一致（⚠ 差点误判：只看 evb.dtsi 会以为不匹配） |
| 以太网 | 两个 gmac 都 rgmii：`fe010000` tx 0x2f / rx 0x39 / reset gpio3 PA7；`fe2a0000` tx 0x21 / rx 0x3c / reset gpio2 PD3 | gmac0 = 0x21/0x3c + gpio2 PD3；gmac1 = 0x2f/0x39 + gpio3 PA7 | ✅ 逐项一致 |
| PMIC / 音频 | rk809（codec rk817）、hp-det gpio1 PD3 | evb.dtsi 里 rk809 + K1Mini 里 hp-det gpio1 PD3 | ✅ 一致 |
| RTC | 双 RTC：rk809 内置 + **hym8563 @ i2c5 0x51** | K1Mini dtsi 里 hym8563 @ i2c5 0x51 | ✅ 一致 |
| 内存 / 存储 | **4 GB**（MemTotal 3.99 GB）；**32 GB eMMC** | 由 DDR 初始化 + 分区表决定 | 归 **T15-2-4**（分区）与**首次刷机**核验 |
| 相机 / USB / SATA / PCIe / IR / 40pin | live DT 里都有对应节点 | K1Mini dtsi 全部 include | ✅ 同族（未逐条比电路细节） |

### 6.1 我们改了什么（3 个文件，全部 `-assistant` 后缀，不动厂商同名文件）

| 文件（本仓库） | 改动 |
| --- | --- |
| `image/kernel/rk3568-kickpi-k1Mini-assistant.dtsi` | 厂商 `rk3568-kickpi-k1Mini.dtsi` 的副本，**只两处**：① LCD include 换成 v2 MIPI 面板 ② 末尾 include 我们的覆盖文件 |
| `image/kernel/rk3568-kickpi-assistant-overrides.dtsi` | 触摸复位脚 `RK_PB5` → **`RK_PB6`**（三条证据写在文件里：live DT 引脚 14、厂商 pinctrl 也是 PB6、IRQ 两侧一致） |
| `image/kernel/rk3568-kickpi-k1Mini-assistant.dts` | 顶层：我们的 dtsi + `rk3568-linux.dtsi` |

板级 defconfig 里已经指向它：`RK_KERNEL_DTS_NAME="rk3568-kickpi-k1Mini-assistant"`。

### 6.2 编译验证（内核真管线）

```bash
cd <SDK>/kernel-6.1
export PATH=<SDK>/prebuilts/gcc/linux-x86/aarch64/gcc-arm-10.3-2021.07-x86_64-aarch64-none-linux-gnu/bin:$PATH
make ARCH=arm64 CROSS_COMPILE=aarch64-none-linux-gnu- rockchip_linux_defconfig
make ARCH=arm64 CROSS_COMPILE=aarch64-none-linux-gnu- -j8 rockchip/rk3568-kickpi-k1Mini-assistant.dtb
# → DTC arch/arm64/boot/dts/rockchip/rk3568-kickpi-k1Mini-assistant.dtb   （退出码 0）
```

反编译这份 dtb 逐项核对（这才是板子将来真正启动用的东西）：

| 项 | dtb 里的值 | 与板端一致？ |
| --- | --- | --- |
| 触摸复位脚 | `goodix_rst_gpio = <gpio0 14 0>` = **PB6** | ✅（live 也是引脚 14） |
| 触摸 IRQ | `= <gpio3 3 8>` = PA3 + 低有效 | ✅（引脚号/触发方式一致；phandle 数字两次构不同，不可比） |
| 面板 | `hactive = 800` / `vactive = 1280` | ✅ |
| 面板电源 | `vcc3v3-lcd0-n` 带 `gpio = <gpio0 0x17>` = PC7 使能 | ✅ |
| WiFi | `wifi_chip_type = "rtl8822cs"` | ✅ |
| 以太网 | 两组 tx/rx delay = 0x2f/0x39 与 0x21/0x3c | ✅ |

⚠ **一次误判记录**（留着防复发）：我先把厂商 LCD 文件里自带的 `vcc3v3-lcd0-n` 当成"与 evb.dtsi 撞名"，
改成 `&vcc3v3_lcd0_n` 引用式覆盖 → 内核管线直接报
`Error: ...v2-k1Mini-assistant.dtsi:56 ... Label or path vcc3v3_lcd0_n not found`。
真相：evb.dtsi 约 285 行起有一段 **`/* ... */` 块注释**，把 `vcc5v0_host`、`vcc5v0_otg`、
`vcc3v3_lcd0_n`、`vcc3v3_lcd1_n` 全注释掉了 —— 所以那两个稳压器**只有** LCD 文件在定义，
厂商写法本来就是对的。已撤回，改用厂商原文件。
（教训：`grep` 只看"文件里有没有"不够，得看**预处理之后**还在不在；判 dts 冲突要么用内核管线，
要么至少 `cpp` 一遍。）

---

## 7. 分区与 OTA 布局（T15-2-4 定稿）

**为什么选 A/B**（D5 定了无线 OTA）：这份 SDK **原生支持 A/B 更新** ——
`RK_AB_UPDATE`（`device/rockchip/common/configs/Config.in.update`）+
`mk-updateimg.sh` 的 `build_ota_updateimg`（日志会打 "Making A/B update image for OTA..."）+
分区模板 `parameter-buildroot-fit-ab.txt`。A/B 最大好处是**回退是机制自带的**：
新槽起不来就回旧槽，不需要 recovery 分区、不需要人插手；代价是 rootfs 存两份
（3 GiB × 2，29 GiB eMMC 完全够）。

**我们的表**：`image/device/rockchip/.chips/rk3566_rk3568/parameter-assistant-ab.txt`
（板级 defconfig 里 `RK_PARAMETER="parameter-assistant-ab.txt"` + `RK_AB_UPDATE=y`）。

| 分区 | 起始（扇区） | 大小 | 作用 |
| --- | --- | --- | --- |
| uboot | 0x4000 | 4 MiB | 引导链（loader/trust 按 SDK 约定） |
| misc | 0x6000 | 4 MiB | **A/B 引导元数据**（U-Boot `CONFIG_ANDROID_AB` 用它记"起哪个槽 / 试过几次"） |
| boot_a / boot_b | 0x8000 / 0x28000 | 32 MiB ×2 | FIT（内核 + dtb + resource），**每槽一份** |
| backup | 0x48000 | 32 MiB | SDK 约定保留 |
| system_a / system_b | 0x58000 / 0x658000 | **3 GiB ×2** | 我们的 rootfs（名字沿用 Rockchip A/B 约定） |
| oem | 0xc58000 | 128 MiB | SDK 约定保留 |
| userdata | 0xc98000 | **grow ≈ 22.8 GiB** | **模型（4.9 GB）/ 日志 / 运行时配置**，两个槽**共用** |

固定分区到 0xc98000（13,205,504 扇区）结束，`userdata` 吃掉剩下的 ≈22.8 GiB
（eMMC 实测 61,071,360 扇区 = 29.12 GiB）。

**模型放 userdata 是这里最关键的取舍**：4.9 GB GGUF **不进 rootfs** ——
① 进 rootfs 就要 A/B 各存一份（≈10 GB）；
② 每次 OTA 都得重下 4.9 GB（无线 OTA 最慢的一环）。
放共享 `userdata` 之后：OTA 只写"另一个 system 槽 + 对应 boot 槽"，模型/配置/日志原地不动。
运行时的 `/data` 挂载与应用读写路径在 **T15-2-7 / 2-8** 落地。

**无线 OTA 流程（机制定稿；实现归 T15-14）**

1. 云端出包：`./build.sh ota-updateimg`（SDK 的 A/B OTA 包；可 `edit-ota-package-file` 改清单）；
2. 板端下到 `userdata` → **校验**（哈希/签名）→ 写**非当前槽**（`system_b` + `boot_b`）；
3. 在 `misc` 里把引导目标指向新槽，并标记"未确认成功"；
4. 重启 → U-Boot 起新槽；
5. 新系统自检（Agent/GUI 都起来）通过才"确认成功"；
   不确认就按 `misc` 的计数回退到旧槽 —— **这就是回退路径**。

**守卫**：`image/check-parameter.py` 是纯算术校验器（连续性 / A-B 成对 / 容量 / 最小尺寸），
`tests/test_image_parameter.py` 把它钉进 CI（8 项：我们的真表必须过，四种坏表必须被抓）。
跑法：`python image/check-parameter.py image/device/rockchip/.chips/rk3566_rk3568/parameter-assistant-ab.txt`

⚠ **首次刷机会重新分区**（板上现有的 p1…p6 会被换掉）→ 板上的数据会没有；
刷机与回退步骤归 **T15-2-11 / T15-14**。另有三条接线要在首次构建/刷机时核实：
① 每个槽的 FIT 自带指向本槽 rootfs 的 `root=`（`PARTLABEL=system_a|system_b` 或对应 uuid）；
② U-Boot 的 `CONFIG_ANDROID_AB` 与 `misc` 元数据真的开着；
③ SDK 能正确消费我们的表（`rk_partition_parse_names` 应看到 9 段且 A/B 成对）。

---

## 8. 任务列表（T15-2，已批准 2026-09-28）

顺序即依赖顺序；每条做完等验收。

| # | 任务 | 出口 |
| --- | --- | --- |
| 2-1 | 决策与配方文档定稿（本文档 §1–§4） | 文档能独立复述 D1–D6 |
| 2-2 ✅ | K1Mini 的 buildroot defconfig 落地（release 骨架 + **systemd** + Qt5 替 weston） | `make <defconfig>` 通过（退出码 0）、`.config` 里 systemd 与 Qt/EGLFS/虚拟键盘/G52/GBM/NPU/MPP/NM/Python 逐项在位（见 §5.1） |
| 2-3 ✅ | 板级对齐（面板变体 / 触摸复位脚 / WiFi / 以太网 / PMIC / 容量） | 差异表 + "要不要改 dts"结论，逐项带证据（见 §6）：**要改 2 处** —— 面板 include 换成 v2 MIPI、触摸复位脚 PB5→**PB6**；改动落在 `image/kernel/` 的 3 个文件里，并用**内核真管线编出 dtb（退出码 0）**再反编译逐项核对 |
| 2-4 ✅ | 分区与 OTA 布局定稿（A/B；模型 4.9 GB 落点） | 见 §7：**A/B 双 rootfs（3 GiB×2）+ 共享 userdata（grow ≈22.8 GiB，模型/配置/日志都在这）**；机制用 SDK 原生的 `RK_AB_UPDATE` + `ota-updateimg`，回退靠 `misc` 元数据；分区表由 `image/check-parameter.py` + `tests/test_image_parameter.py`（8 项）守住 |
| 2-5 ✅ | libmali G52(GBM) 进 buildroot | 见 §5.4：`image/prepare-libmali.sh` 把厂商 deb 里的 G52 blob 按 buildroot 期望的名字落位；**`make rockchip-mali` 退出码 0（22 分 46 秒，含首次整条交叉工具链）**，进镜像的 blob 与源 blob **逐字节相同**、`gbm_*` **39**（≥30）、`BR2_PACKAGE_HAS_LIBGBM=y`、blob 的 **16 个 `DT_NEEDED` 缺失 0**，sysroot 里 `egl.pc`/`gbm.pc`/EGL·GLES 头齐全（Qt5 要用） |
| 2-6 ✅ | Qt5.15 + EGLFS + 虚拟键盘 + 四个 QML 模块 | 见 §5.5：`make` 退出码 0（9 分 58 秒）；**`libqeglfs.so`**（`DT_NEEDED` 里直接有 `libmali.so.1`，KMS/GBM 集成插件在位）与 **`libqtvirtualkeyboardplugin.so`** 在位；**四个 QML 模块** `usr/qml/{QtQuick.2, QtQuick/Window.2, QtQuick/Layouts, Qt/labs/folderlistmodel}` 各有 `qmldir`+插件且依赖缺失 0；`en_US`+`zh_CN`(拼音) 布局确认编入；Qt = 5.15.11 |
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
