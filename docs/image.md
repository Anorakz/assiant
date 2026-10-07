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

> ⚠ **T15-2-10 追记（这条很重要）**：上面那次验收是对着**单包构建**那棵树做的
> （那时整机构建还没跑，两棵树的区别见 §5.8）。对着**整机构建**那棵树重跑之后，
> 闭包里多出两个厂商服务 —— `wifibt-init.service`（厂商挂在 `sysinit.target.wants/`，
> 加载 rtl8822cs 固件，**必须留**）与 `usb-gadget.service`（挂在 `local-fs.target.wants/`，
> `Type=simple` 常驻，发行镜像**已 mask**）。也就是说"只有我们的东西"这句话，
> 在整机镜像上的准确形态是：**我们的 4 个 + NetworkManager(2) + wifibt-init**，
> 加上被 mask 的 usb-gadget。详见 §5.9。

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

**② 体积**（表观合计 **≈1.39 GiB**；`du -sh` 会显示约 1.3 GiB，因为 rootfs 是稀疏文件，见下）：

| 产物 | 大小 | 说明 |
| --- | --- | --- |
| `update-…-buildroot-2026092920.img` | **747,516,490 B ≈ 713 MiB** | 整套烧写镜像（软链到 `output/update-ab/Image/update.img`） |
| `rootfs.img` → `images/rootfs.ext2` | **679,477,248 B ≈ 648 MiB** | 装进 `system_a`/`system_b`（各 3 GiB 槽，余量充足） |
| `boot.img` | 41,900,544 B ≈ 40 MiB | FIT：kernel + dtb + resource |
| `oem.img` | 12,582,912 B ≈ 12 MiB | |
| `userdata.img`（首启镜像） | 8,388,608 B ≈ 8 MiB | 真实 userdata 是 22.8 GiB 的 grow 分区 |
| `uboot.img` / `MiniLoaderAll.bin` | 4,194,304 B / 481,728 B | loader 与 u-boot |
| `parameter.txt` | 579 B | **就是我们那份 `parameter-assistant-ab.txt`** ✓ |
| `BR/target`（根文件系统树） | 522 MB | 字体 70 MB、`usr/include/qt5` 32 MB 都在里面（账见 §5.5/§5.7） |

> ⚠ `output/firmware/` 里**全是软链**，而且 AB 形态下 **`update.img` 是悬空的**。
> 实拍（`ls -la output/firmware/`）：
>
> ```
> update.img                                                    -> update-ab.img        ← 悬空！firmware/ 里没有这个名字
> update-rk3568-kickpi-k1Mini-assistant-buildroot-2026092920.img -> ../update-ab/Image/update.img   ← 真的这份
> rootfs.img      -> ../../buildroot/output/<整机构建那棵树>/images/rootfs.ext2
> boot.img        -> ../../kernel-6.1/boot.img
> parameter.txt   -> ../../device/rockchip/.chips/rk3566_rk3568/parameter-assistant-ab.txt
> ```
>
> 也就是：vendor 的 `mk-firmware.sh` 在 AB 分支里把真镜像放进了
> `output/update-ab/Image/update.img`（747,516,490 B）并另建了一个带版本号的软链，
> 但**同时也留了一个老式 `update.img -> update-ab.img`**，而这个名字在 AB 形态下没人创建。
> **烧写要用**：`output/update-ab/Image/update.img`（或那条带版本号的软链），
> 不是 `firmware/update.img` —— 用后者会以"文件不存在"告终（T15-2-11 会用到）。
>
> 体积口径：上表是**表观大小**（`stat -Lc %s`）。`rootfs.ext2` 是**稀疏文件**
> （表观 648 MiB / 实占 529 MB，1,081,648 个 512 B 块），所以 `du -sh` 会给出
> 大约 **1.3 GiB** 这样偏小的"实占"数字 —— 两者都对，别拿它们互相校对。

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

### 5.9 刷板前验证（T15-2-10）：一条命令，三张表

#### 一条命令

```bash
wsl -u root bash image/preflash-check.sh <SDK>
```

root 是给 chroot 冒烟用的：WSL 的 `binfmt_misc` 里已经注册了 `qemu-aarch64`
（`qemu-user-static` 8.2.2），所以 **aarch64 的 `python3` 能直接在 chroot 里跑真 import**，
不需要 `qemu-aarch64-static` 手动塞进树里。脚本按优先级选树（`--target` > `IMG_TARGET` >
整机构建树 > 单包树），并把"我在看哪棵树"打印出来。退出码：0 全过、1 有缺件、3 系统层过了但 payload 没装。

#### 这一轮真正的收获：**前面几步的验收，验的是另一棵树**

§5.8 记了"两条 output 路径"，但两个检查器（以及 post-build 调的那个脚本）当初都按**单包**
那棵写死了路径。于是 T15-2-9 整机构建成功之后，所谓"刷板前验证"其实在看另一棵树 ——
**实测后果**：`prepare-rknnlite.sh` 把 wheel 解进了单包那棵的 site-packages，
而整机镜像里**根本没有 `rknnlite`**，构建却全绿（它还因为那棵树里留着 T15-2-7 的旧戳，
打印了一句"已经一致"就退出了）。取证就在会话日志第 1081 行：

```
site-packages: .../buildroot/output/rockchip_rk3568_kickpi_k1mini_release/target/usr/lib/python3.11/site-packages
                                                                        ↑ 少了一层，是单包那棵
= 已经一致：... 里已经是 rknn_toolkit_lite2-2.3.2-cp311-...whl
```

修法三条，都落进了配方：

1. **选树收在一处**：`image/imagelib.py`（两个检查器都 `import` 它），
   优先级 `--target` > `IMG_TARGET` > 整机构建树 > 单包树；打印选中哪棵。
2. **post-build 把 `$TARGET_DIR` 传给 `prepare-rknnlite.sh`**，脚本加 `--target`；
   它的 python 版本也改成**从 target 树自己看**（`usr/lib/python3.x`），
   不再从 `$OUT/build/python3-*` 推 —— 那个路径本身就是"算错树"的来源。
   不给 `--target` 且只找到单包树时，它会**大声警告**而不是安静地装错地方。
3. **新入口 `image/preflash-check.sh`**：一条命令跑完全部，且把结果分成
   "缺件（必须修）/ payload（已计划未做）/ 只能板上测" 三类。

#### 结果（2026-09-29，对着整机构建树）

| 段 | 结果 |
| --- | --- |
| 逐项在位 | **31 项，缺 0**（二进制/库/gst 插件/QML/字体/python 模块/llama 运行时；`rknnlite` 修好后由缺变有） |
| DT_NEEDED 闭包 | **2496 个 ELF，缺失 0**（交叉版 ldd，照 ld.so 真实搜索顺序） |
| chroot 冒烟 | **9/9 全过**：`Python 3.11.8`、`numpy 1.25.0 + cv2 4.9.0`、`rknnlite ok`、`bash ok`、`curl 8.6.0`、`nmcli 1.44.2`、`mppvideodec` 起得来、`fc-list :lang=zh` 出思源黑体、`llama-server` 跑得起来 |
| 开机目标闭包 | **我们的 4 个 + NetworkManager(2) + `wifibt-init`**；`usb-gadget` 被 mask（见 F3） |
| **payload** | **9/9 未做** —— 见 F5 |

#### 四个发现（F1/F2 已修，F3 已处理，F4 只记录）

**F1 `rknnlite` 装错树**（上面那段）：已修 + 已对现有 target 补装，chroot 里
`from rknnlite.api import RKNNLite` 真的通过了。

**F2 检查器看错树**（meta bug，比 F1 更值钱）：已修，见上。

**F3 我们的启动链里还挂着两个厂商服务**（2-8 那次是在单包树上验的，所以没看见）：

| 服务 | 厂商挂在哪 | 判断 |
| --- | --- | --- |
| `wifibt-init.service` | `sysinit.target.wants/`（`WantedBy=sysinit.target`，oneshot） | **留**：`/usr/bin/wifibt-init.sh start` 负责加载 wlan0(rtl8822cs) 的固件/模块，屏蔽了连设备都没有，`network-online` 永远等不到 → 进白名单（带理由） |
| `usb-gadget.service` | `local-fs.target.wants/`（`WantedBy=local-fs.target`，`Type=simple` 常驻） | **发行镜像 mask**：adb/rndis 那一类调试与传输通道，每次都起，与"只起我们的东西"矛盾 → `post-build.sh` 里 `ln -sfn /dev/null /etc/systemd/system/usb-gadget.service`；开发镜像（2-12）可 `ASSISTANT_FLAVOR=dev` 保留 |

检查器现在把 mask 当**真语义**处理：磁盘上真有 `/dev/null` 才算"起不来"，
而且**策略表里要求 mask 的必须真被 mask**（否则报"post-build 的 MASK_UNITS 没落？"）。

> 顺带看清了厂商自己的一处 bug：`/etc/systemd/system/` 里有个目录叫
> **`local-fs.target graphical.target.wants`**（名字里带空格）。systemd 不认这种目录名，
> 所以厂商以为自己 enable 了的 `async-commit.service`（`Enable ASYNC_COMMIT for Rockchip BSP kernel`）
> **实际从来没跑过**。不是我们的问题，但板端基线时别以为它在跑（检查器会打印这条提示）。
> 另外 `multi-user.target.wants/` 里那几个（`dhcpcd`/`input-event-daemon`/`irqbalance`/`log-guardian`）
> 在我们这种形态下**不会被拉起** —— 我们根本不进 `multi-user.target`。

**F4** 即上面那条目录名带空格的厂商 bug（只记录，不阻塞）。

**F5 payload 9/9 未做**（这是刷板前**必须**解决的那件事）：

| 类别 | 缺的东西 | 说明 |
| --- | --- | --- |
| agent | `usr/lib/assistant/agent/{main.py,__init__.py,...}` | `agent.service` 的 `ExecStart` 是 `python3 -m agent.main`（`PYTHONPATH=/usr/lib/assistant`） |
| gui | `usr/lib/assistant/gui/agent_gui` | `agent-gui.service` 的 `ExecStart`；**要用 buildroot 的 Qt 5.15.11 交叉编译** |
| native | `usr/lib/assistant/agent_native.cpython-311-aarch64-linux-gnu.so` | pybind11 扩展。⚠ 仓库里现有那份是 **cp38**（板端 Ubuntu 的 python），镜像 python 是 **3.11**，**必须重编** |
| config | `config/{config,user_profile}.example.yaml` | `assistant-init.service` 首启 `cp -n` 到 `/data/assistant/config/` |
| doc | `Readme.md`、`docs/{gui,image}.md` | 三个 unit 的 `Documentation=` 指向它们 |

也就是说：**当前这套镜像刷上去，系统层是好的（能起、有网、字体/EGLFS/硬解/NPU 运行时都在），
但 `assistant.target` 拉起来的 agent/gui 会一直重启**（`No module named agent` /
`No such file or directory`）。所以 2-11 之前需要先补 payload（方案与排期见任务表）。

#### 只能板上测的（构建机给不出证据，别当成"没做"）

DRM/KMS 真出图（Mali G52 的 EGLFS、旋转 90°、开机到首帧）· 触摸（gt9xx、旋转后坐标）·
wlan0 真联网与 `network-online` 真等到 · RTC（hym8563 @i2c5 0x51、掉电走时）·
NPU 真推理（`RKNNLite.init_runtime()`）· 硬解真解码（帧率与 CPU，不是 `gst-inspect` 能看就行）·
温度/功耗/启动耗时（`systemd-analyze`、`/sys/class/thermal`）· A/B 槽与 OTA 回滚。

#### 交给 T15-2-11 的

- 现有 `update.img` 是**修复前**的（缺 rknnlite、未 mask usb-gadget）→ 刷板前要重新打包，
  顺便把 payload 决定一起落进去（只重跑 `./build.sh all` 的打包段即可，增量很快）。
- 刷写用的镜像路径见 §5.8 的提醒（`output/update-ab/Image/update.img`）。

> **关于原始出口里那句"chroot 跑 ctest + 仓库 Python 套件"**：镜像里**没有** ctest，
> 也不该有 —— 发行镜像不带编译工具与测试框架。我们那套测试是**构建机/CI 的产物**：
> host ctest 与 `scripts/test-python.sh` 每次提交都在 CI 里跑（本轮新增/加固：
> `test_image_recipe` 27 项、`test_image_runtime` 19 项、`test_image_target` 10 项）。
> 板上要跑测试的那一套属于开发镜像（2-12，带 pytest）。chroot 在这里能给的证据是
> "**镜像自带的运行时真能跑**"，已经在上面那张冒烟表里逐条列出来了。

### 5.10 把 payload 装进 rootfs（T15-2-10b，方案 A = 全部交叉编译）

#### 为什么单独一个清单文件

payload = "unit 文件**已经指向**、但当时还没做"的那批东西（§5.9 的 F5）：agent 包、
GUI 二进制、pybind11 扩展、默认配置、unit 的 `Documentation=` 文档、CLI 入口。
它最容易出的错不是"装不进去"，而是**清单与真实意图漂**：改了路径、加了文件、
unit 里换了 `ExecStart`，而装的那份没跟上 —— 构建全绿，板子上服务起不来。

所以 `image/payload.manifest` 是唯一来源表（`how|src|dest`），三方互校钉进 CI：

```
systemd/image/*.service 里引用的路径   ←→   check-runtime-deps.py 的 PAYLOAD 表
                                          ←→   payload.manifest（谁把它装出来）
```

`tests/test_image_payload.py`（13 项）把这三方两两对上：**"检查器盯着但没人装"**、
**"装了但没人盯"**、unit 里的 `ExecStart`/`Documentation`/`assistant-init` 的 `cp` 源
都必须在 PAYLOAD 里出现。改一边忘另一边，CI 当场红。

#### 三条做法（都是这次踩出来的）

1. **只认调用方给的 target 树**：`build-payload.sh` 没有 `--target`（也没有 buildroot
   导出的 `TARGET_DIR`）时**直接拒绝**，绝不自己猜 —— 猜错的后果 §5.9 已经演示过一次
   （rknnlite 装进了另一棵树，整机镜像里没有它而构建全绿）。
2. **幂等戳放在 rootfs 之外**：`<target 的兄弟目录>/.assistant-payload-stamps/`。
   第一版把戳写进落点目录（`usr/lib/assistant/agent/.payload-stamp`），那等于**往镜像里
   塞构建垃圾**，而且下次算内容哈希时又会把它算进去。
3. **权限归一**：本仓库在 Windows 盘上（WSL 的 DrvFs），源文件权限是 **777**，
   `tar` 照抄进 rootfs 就是"镜像里全是 777"。所以目录复制后统一
   `目录 0755 / 文件 0644 / *.sh 0755`。

#### 入口与 shell 环境

`/usr/bin/assistant`（包装 `python3 -m agent.cli`）与 `/etc/profile.d/assistant.sh`
是**给交互式 shell 用的**：两个 unit 里的 `Environment=` 只作用于服务进程，
串口/ssh 登进去的 shell 不继承它们，于是要么 `No module named agent`、要么读不到配置。
两者设的是**同一套 D7 变量**（`PYTHONPATH`/`AGENT_CONFIG_DIR`/`AGENT_LOG`/
`AGENT_CRASH_DIR`/`LLM_ENV_FILE`/`LLM_STATE_DIR`），`tests/test_image_payload.py`
会与 `agent.service` 里的取值逐条对照，防止两边漂。T15-7 的 "cli 唤醒" 也会用这个入口。

#### 这一轮抓到的两个坑

**① 镜像里的 python3 没有 `_ssl` —— agent 根本起不来。** chroot 实测：

```
import agent.cli → agent/core/__init__ → ... → agent/net/sunshine_client.py:57
                 → import ssl → ModuleNotFoundError: No module named '_ssl'
```

`agent/core/__init__` 会连锁 import 到它 ⇒ 板上 `agent.service` 会**一直崩溃重启**。
厂商基座把 python3 的扩展模块全关着；我们在 buildroot defconfig 末尾打开
`BR2_PACKAGE_PYTHON3_SSL`（一次带出 `_ssl` + `_hashlib`）与 `..._READLINE`（串口 CLI 好使）。
守卫在 `tests/test_image_recipe.py::TestPythonModulesTheAgentNeeds`（必须 `=y`、
必须在所有 `#include` **之后**、注释里必须写理由）。

**② vendor 的 lunch（`./build.sh <defconfig>`）不会重新生成 buildroot 的 `.config`。**
这是"改了配置却没生效"的真凶，也是差点让我误判成"片段合并器不认 python3 子选项"的地方：

```
./build.sh rk3566_rk3568:<cfg>_defconfig              → .config 纹丝不动（时间戳不变）
cd buildroot && make O=output/<cfg> <cfg>_defconfig   → 立刻 PYTHON3_SSL=y
```

已配置过的树上，lunch 只打印 `Running within sudo(root) environment!` 就结束了；
**整机构建（`./build.sh all` → `mk-buildroot.sh`）走的是后者那条路**，所以 2-9 的
`.config` 是新的。→ 以后验证配置改动用 `make O=... <cfg>_defconfig`，别用 lunch。

#### 第四个坑：改配置**不等于**重建（三层机制，少一层就白改）

```
./build.sh rk3566_rk3568:<cfg>_defconfig              # lunch：已配置过就什么都不做
cd buildroot && make O=output/<cfg> <cfg>_defconfig   # 这才重新生成 .config
make O=... python3-rebuild                            # 又**不重跑 configure**（DISABLED_EXTENSIONS 照旧）
make O=... python3-dirclean python3                   # 只有 dirclean 才重新 configure → _ssl 才编出来
```

三条都实测过：跳过任何一条，`.config` 里那个开关是 `y` 而 `lib-dynload/_ssl*.so` 依旧不存在。
（另外：单独 `make python3` 会在收尾的 `check-bin-arch` 上报三个**厂商二进制**的架构警告
`architecture for /usr/bin/input-event-daemon is "ARM"` —— 与我们的改动无关，
整机构建路径（`./build.sh all`）不报，镜像正常。）

#### 状态

| 步骤 | 状态 |
| --- | --- |
| 2-10b-1 清单 + 骨架 | ✅ `--dry-run` 三种走法（copy+gen → 0；全量 → 1 且明说"半成品"；无 `--target` → 2 拒绝猜树） |
| 2-10b-2 python 侧 | ✅ 装进整机 target（66 个 .py、0 个 pyc、幂等、戳在 rootfs 之外、权限归一）；`_ssl`/`_hashlib`/`readline` 修好 |
| 2-10b-3 native（cp311） | ✅ `Machine: AArch64`；moonlight 库**定义了** `LiStartConnection`；chroot `import agent_native` RC=0 |
| 2-10b-4 GUI | ✅ Qt5 Widgets 交叉编译（1,084,712 B → strip **821,368 B**）、`Machine: AArch64`、`NEEDED` 里的 Qt5/yaml-cpp 全在镜像里；`agent_gui --help` chroot RC=0 |
| 2-10b-5 串进 post-build | ✅ `post-build.sh` 在 llama → rknnlite 之后调 `build-payload.sh --target "$TARGET_DIR" --src-root …/payload-src`；**删掉 target 里的 payload 再整机构建，它自己装回来了**（证明真的走通了，不是"我手动拷的"） |
| 2-10b-6 重新打包 + 全量验证 | ✅ `preflash-check.sh` **RC=0（payload 0/12）**；`update-ab` **759,050,826 B**；target 531 MB；chroot 里 `agent.cli + agent.main + agent_native + ssl + cv2 + numpy + yaml + psutil + rknnlite` **全部 import ok** |

#### 途中另外三个坑（都在 payload 串线时暴露）

1. **模板路径要认两种布局**：清单里的 `gen` 源写成仓库相对路径
   （`image/payload/assistant`），而 post-build 的世界里它在 `<SDK>/tools/assistant/payload/`。
   整机构建当场断在 `!! 清单里的模板不存在：image/payload/assistant`。
   现在按四个候选位置依次找，找不到才报错（并把试过的路径全打出来）。
2. **同一种 `how` 只许跑一次**：清单里有**两条** `native`（扩展 + 它的 moonlight 库），
   第一版两条都去调 `build-native.sh` → 第二条把 moonlight 的落点**覆盖成了扩展自己**
   → chroot 里 `import agent_native` 报 `undefined symbol: LiStartConnection`。
   现在第一条真跑、后面的只核对在位；`build-payload.sh` 里用 `native_done`/`gui_done` 记住。
3. **取依赖库要认"符号"而不是"文件名"**：`find … -name 'libmoonlight-common-c.so*' | head -1`
   在构建目录里抓到过同名但不是那个库的文件。现在只从确定路径
   `$BUILD_DIR/moonlight-common-c/libmoonlight-common-c.so` 取，**并且**
   `nm -D --defined-only | grep ' T LiStartConnection'` 验过才装。

> 这三条与 §5.9 的 F1/F2 是同一类错误：**"构建成功"与"镜像里真的是那样"之间隔着好几层**，
> 每层都得拿产物本身（ELF 头、导出符号、chroot 里的真 import）说话。

---

### 5.11 开发镜像（T15-2-12）：同一套配方 + 第二棵树

**目标**（用户 2026-10-02 批准的清单）：发行镜像保持最小；另出一个开发镜像，
板上带编译链与排障工具，**能在板上跑 pytest 与验收脚本**。

**怎么建**（两条命令，都在 SDK 之外/仓库里执行）：

```bash
# ① 造开发树：硬链接复制第二份 SDK（实测 64 秒，实际只多占 582 MB）
bash image/make-dev-sdk.sh <发行树 SDK 根目录>
# ② 按 dev 形态构建（板级配置 + ASSISTANT_FLAVOR=dev 都会自动切）
wsl -u root bash image/build-image.sh --flavor dev <开发树 SDK 根目录>
```

**为什么是"第二棵树"而不是"两个输出目录"**：SDK 把输出目录写死在
`device/rockchip/common/scripts/build.sh`（`export RK_OUTDIR="$RK_SDK_DIR/output"`，
而且**不止一处**）。实测把它改成可由环境变量指定后，`output-dev/` 确实建起来了、
release 的 `output/` 也没被动，**但 lunch 仍把配置写进 `output/.config`** ——
要继续就得在 vendor 脚本上打多处补丁，改动面越大越难维护。所以选"同一套配方 +
第二棵树"：`cp -al` 让开发树**继承**发行树已编好的产物，首编只编 dev 新增的包。

| 项 | 值（2026-10-03 实测） |
| --- | --- |
| `cp -al` 造树 | **64 秒**，实际多占 **582 MB** |
| 首编（含 ccache 触发的工具链重编） | **63.5 分钟** |
| 之后改一行再重建（ccache 生效） | **3 分 12 秒** |
| 镜像 / target 体积 | dev **1003 MB** / 759 MB；release 737 MB / 532 MB |
| ccache 缓存 | 782 MB（`~/.buildroot-ccache`，只给开发树开） |

**结构：dev = release + 一个"只加不改"的片段**

```
buildroot:  configs/rockchip_rk3568_kickpi_k1mini_dev_defconfig
              ├─ #include 发行那份 defconfig（原样引入，拿到全部基础）
              └─ #include products/kickpi-k1mini-dev-assistant.config（只加不改）
板级:       .chips/rk3566_rk3568/rockchip_rk3568_kickpi_k1mini_dev_defconfig
              = 发行那份，**只差 RK_BUILDROOT_BASE_CFG 一行**（守卫测试钉住）
```

这样"两镜像行为一致"是**结构性**保证的，不靠人同步两份配置；dev 多出来的东西
一律不进发行镜像。

**dev 清单（都验证过是 aarch64 ELF）**

编译链 `gcc / ld / make / ctest / pkgconf`；调试排障 `gdb / gdbserver / strace /
top / pgrep / pkill / vim`；网络 `tcpdump / iperf3`；版本控制与测试 `git /
python-pytest / python3-zlib`；外加 `ASSISTANT_FLAVOR=dev` 保留的 `usb-gadget`
（adb/rndis）。发行镜像里**没有**这些（实测不串味）。

**已知差异（用户 2026-10-02 选"方案 A"接受，不算失败）**

| 项 | 实测 | 为什么不补 |
| --- | --- | --- |
| 板端 **C++** | target 里只有 `cc1`，**没有 `cc1plus`、没有 `g++`**；`gcc -x c++` 直接失败 | 要改 buildroot 的 gcc 包行为并**重编板端 GCC**（30–60 分钟）+ 长期维护；而我们所有交付物本来就是 **PC 侧交叉编译** |
| 板端 **cmake 驱动** | 只装了 `ctest`(8.9 MB) 与 `share/cmake-3.28`，**没有 `cmake`** | 厂商那份 buildroot 的 cmake 包只把 ctest 装进 target（包目录里还有厂商自己的 patch） |

**板上验收**（出口判据就是它）：

```bash
scp image/dev-image-acceptance.sh rk3568:/tmp/
ssh rk3568 "sed -i 's/\r$//' /tmp/dev-image-acceptance.sh; sh /tmp/dev-image-acceptance.sh"
```

真编 C 程序、真跑 pytest、gdb 下断点求值、strace 抓 write、板上 make 构建，
逐项验 15 个工具 + zlib。**2026-10-03 实测：通过 23 项 / 失败 0 项 / 已知差异 3 项。**
脚本是 POSIX sh（板上可能没 bash），且有失败项时退出码非 0。

⚠ **脚本里跑被测程序必须限时+限量**（`timeout 5` + `head -c 200`）：板上 gcc 曾
链接出入口错误的程序（见 §5.12 末），一跑就狂输出，把 `$( )` 缓冲撑到 3.8 GB，
整个脚本被 OOM 杀掉。验收工具不该因为被测程序发疯而把板子搞死。

### 5.12 刷机自带可引导的 A/B 元数据（T15-2-12 修 2）

**两次砖的根因**（2026-10-02，b8 一次、"黑屏+WiFi 连不上"一次）：刷完镜像后板子
黑屏、掉 fastboot，串口 `No bootable slots found.`；读 `misc@0x800` 发现两个槽都
不可引导、且 **`tries=0`** —— 只要这次启动失败一次，两个槽立刻全死；而
`ab-mark.service`（开机标记成功）要等系统起来才能跑 → **死锁，只能串口救**。

**机制**（读 SDK 源码）：`mk-updateimg.sh` 的 `gen_package_file()` 按分区表逐个生成
条目，而且 **`[ -r "$IMAGE" ]` 存在才收进包** —— 所以 `Image/` 里没有 `misc.img`
时，刷机**根本不写 misc**，元数据一直是上一次的残留（而残留可能已经是死的）。

**修法**（三件事，全在配方里，不改 SDK）：

1. 两份板级 defconfig 开 `RK_MISC=y` + `RK_MISC_CUSTOM=y`
   + `RK_MISC_IMG="misc-assistant-ab.img"`；
2. 新增 `image/make-misc-img.py`：生成 48 KB 镜像，0x800 处是 AVB 默认状态
   （槽A prio15/tries7、槽B prio14/tries7 → **两个槽都可引导**；CRC32 大端）；
3. `install-into-sdk.sh` 把它生成到 `<SDK>/device/rockchip/.chips/rk3566_rk3568/`，
   SDK 的 `mk-misc.sh`（`build-hooks/08-misc.sh`）会取它。

**验证**（2026-10-03）：

```
打包日志：misc,Add file: ./misc.img ... flash_address=0x00006000
package-file：misc	misc.img
刷完板端 misc@0x800：00 41 42 30 01 00 00 00  0f 07 01 00 0e 07 00 00 ...
                    magic ✓ ver1.0            槽A prio15 tries7 succ1 ✓  槽B prio14 tries7 ✓
```

槽A 的 `successful=1` 是**起来之后 ab-mark 自动标的** —— 整条链闭合：
**刷机写入可引导元数据 → 启动 → 标记成功 → 再也不会被扣死**。
（现场急救流程仍保留在 `image/FLASH-RUNBOOK.md` §5.2。）

**同一轮里修掉的板上 gcc 问题（修 1，两层）**

| 层 | 现象 | 修法 |
| --- | --- | --- |
| 1 | 开了板上 gcc 后 `libgcc_s` **不会进 target** → `ld: cannot find -lgcc_s` | post-build 从交叉 sysroot 补 `libgcc_s.so{,.1}` |
| 2 | buildroot 的 strip **连 target 里的 `.o` 一起剥了符号** → `crt1.o` 只剩 944 B、**没有 `_start`**（sysroot 里 2416 B 有）→ 链接出的程序入口错误：`ld: warning: cannot find entry symbol _start; defaulting to ...4003c0` | 从 sysroot 补 `crt1.o / Scrt1.o / crti.o / crtn.o / libc_nonshared.a / libc.so / libm.so`（**大小不符即替换**）+ 交叉 `ranlib` 重建索引 |

第 2 层的危害是实测出来的：那种二进制跑起来乱来，把验收脚本的 shell 内存撑到
3.8 GB 被 OOM 杀掉（`dmesg: Killed process (sh) anon-rss:3840984kB`）。
补完后板上闭环通过：`gcc 编译并运行成功：hello from board gcc`。

### 5.13 ★★ 标准镜像（2026-10-07）：带上 dmabuf 零拷贝视频修复 + 刷板前验收全过

**为什么重建**：GUI 的视频播放这一轮被修好并**由用户目视确认**（详见
[dmabuf-zero-copy-findings.md](dmabuf-zero-copy-findings.md) §7.9/§7.10）：零拷贝路
**25 帧/s**、`vqueue:src` 归零、CPU 从 112.7% 降到 14.7~16.8%，并且修掉了"切视频偶发重启"
（真因是 `gst_message_type` 在这个 GStreamer 构建里**没有导出**，而 `pumpBus()` 无条件调它 ✓）。
镜像里的 GUI 是**从本仓库 `gui/` 交叉编译**出来的（§5.10 的 payload），所以要让这套修复
进镜像，只需**重新注入源码 + 重建**。

**怎么重建（两条命令，全程约 12 分钟增量）**：

```bash
# ① 把仓库源码重新注入 SDK（rsync payload-src；源是唯一来源 ✓）
wsl -u root bash image/install-into-sdk.sh <SDK>
# ② 整机构建（post-build 会重新交叉编译 GUI 并装进 rootfs，再打包 update.img）
wsl -u root bash image/build-image.sh <SDK>          # 用 root（见 §5.8 的三条约束）
# 完事把属主改回来，否则非 root 的 chroot 活会碰壁 ✓
chown -R anorak:anorak <SDK>/output <SDK>/buildroot/output
```

**这次的标准件（指纹，可逐条核对 ✓）**：

| 项 | 值 |
| --- | --- |
| 构建会话 | `<SDK>/output/sessions/2026-10-07_12-16-04/`（12:16 → 12:28） |
| **`update.img`** | `output/update-ab/Image/update.img`，**816,595,530 B**，sha256 `5c597ae8554d7de35be625af3a84d329c03d1dae943f97144a72926092a8d81f`（12:28:29） |
| 同内容软链 | `output/firmware/update-rk3568-kickpi-k1Mini-assistant-buildroot-2026100712.img` → 上面那份 ✓ |
| `rootfs.img` | 736,100,352 B（上一版 692,060,160 B ⇒ **+44 MB**，与 GUI 变大一致 ✓） |
| `boot.img` | 54,307,328 B ｜ target 树 569 MB |
| **GUI（target 里那份）** | `usr/lib/assistant/gui/agent_gui`，**944,264 B**，md5 **`a986143fa6f1ea10a0ce4cbbf3d3e0da`**（上一版 `6c620a10…` 821,368 B） |
| 注入的 GUI 源码 | `payload-src/gui/src/ui/gst_video_widget.cpp` = `2b1d7c31…`、`video_panel.cpp` = `2868f255…`（**与仓库逐字节相同** ✓） |

**"镜像里那份 GUI 就是验好的那份"怎么证的**（不靠嘴说 ✓）：
1. 用**镜像自己的脚本** `image/build-gui.sh`（同 sysroot / 同 flags / 同 strip ✓）编一遍 ⇒ 得到
   `a986143fa6f1ea10a0ce4cbbf3d3e0da` ✓（即"标准镜像里应有的 md5"）；
2. 把这份**部署到板上真跑**：日志 `[video] 走零拷贝主路径` ✓、绘制 **25.1 帧/s** ✓、
   `vqueue 进程数 = 0` ✓、`agent_gui` CPU 16.3% ✓、**换流 5 次 0 崩** ✓；
3. 整机构建完成后核对 target 里那份 md5 = **`a986143f…`** ✓✓ ⇒ **同一份二进制** ✓。

> ⚠ 顺带记一条**构建可复现性**的坑：同一份源码，用 `image/build-gui.sh`（release 树 + `CMAKE_SYSROOT`）
> 与 §5.10 之前那份临时脚本（**dev 树** sysroot）编出来**大小相同、字节不同** ✗（`.text` 就不同）。
> 功能同源、板上都能跑，但**"验过的二进制"与"进镜像的二进制"必须用同一条构建路径** ✓ ——
> 所以这次专门用镜像脚本重编 + 重新上板验证 ✓。

**刷板前验收（`image/preflash-check.sh`）：== 通过 ✓**

| 段 | 结果 |
| --- | --- |
| 逐项在位 | 缺 **0**（含 `rknnlite`、字体、gst 插件、QML、llama 运行时） |
| DT_NEEDED 闭包 | **2680 个 ELF，缺失 0** |
| chroot 冒烟 | **9/9**：`Python 3.11.8`、`numpy 1.25.0 + cv2 4.9.0`、`rknnlite ok`、`bash`、`curl 8.6.0`、`nmcli 1.44.2`、`mppvideodec`、思源黑体、`llama-server` |
| payload | **0/12 未做**（agent 包 / GUI / native 扩展 + moonlight 库 / 两份默认配置 / 三个文档 / CLI 入口 / profile） |
| 开机目标闭包 | 我们的 **6 个**（`agent-gui` `agent` `assistant-init` `assistant-ota-confirm` `assistant.target` `sshd`）+ 白名单基础设施 5 个；`usb-gadget` 已 mask |

**这一轮顺手修掉的两个"检查器自己坏了"**（都不是视频改动引入的 ✗，但会让验收永远红 ✗）：

| # | 现象 | 根因 | 修法 |
| --- | --- | --- | --- |
| 1 | `preflash-check.sh` 第 1 步就 `NameError: name 'site_packages' is not defined` ✗ | T15-3（3-6a，提交 `59bab14`）把 `site_packages` **收敛进 `imagelib`**，但 `check-runtime-deps.py:171` 还是裸调 ✗ | 改成 `imagelib.site_packages(target)` ✓ |
| 2 | `sshd.service` 报 4 条：三个可执行文件未允许 ＋ `WantedBy=multi-user.target` ✗ | `sshd` 是 T15-2-11 之后**故意**登记进 `OUR_UNITS` 的（板端唯一交互/关机通道 ✓），但检查器的可执行文件表与 `WantedBy` 判据没跟上 ✗ | 白名单补 `/usr/sbin/sshd`、`/usr/bin/ssh-keygen`、`/bin/kill` ✓；新增**逐单元例外表** `WANTEDBY_ALLOWED_EXTRA`（上游 openssh 单元就写 `multi-user.target`，我们是靠 post-build 软链挂到 `assistant.target` ✓），两条都写了理由 ✓ |

改完后 `python3 -m pytest tests/test_image_target.py tests/test_image_runtime.py tests/test_image_payload.py -q`
⇒ **48 passed** ✓（这两个检查器在 CI 里有守卫 ✓）。

### 5.13.1 ⚠ 上面那版是**废的**（2026-10-07 当晚发现并重做）—— 事故与三个缺口

§5.13 记的那份 `816,595,530 B` 镜像**带进了 weston** ✗（用户实测：界面起不来、偶尔闪一张图 ✗）。
一晚查下来是**三类问题**，都已修好并重新出镜像：

| # | 问题 | 症状（板端实测） | 根因 | 修法 |
| --- | --- | --- | --- | --- |
| 1 | **陈旧 / 厂商 overlay 的 weston** ✗ | `Could not queue DRM page flip on screen DSI1 (Device or resource busy)` ✗；`weston.service`（`WantedBy=sysinit.target`）开机极早抢走 DRM ⇒ 我们的 GUI 画不上去 | ① buildroot **不会删**上一次 weston 还开着时装进 `target/` 的 30 个文件 ✗（`.config` 里 `WESTON is not set` 也拦不住 ✗）；② 厂商 overlay 每次构建都拷 `etc/xdg/weston/*` ✗（日志 `>>> Copying board/rockchip/common/overlays/10-weston`）| `post-build.sh` 里一律删干净 ✓ ＋ `check-runtime-deps.py` 新增 **`FORBIDDEN_IN_ROOTFS`** 用产物兜底 ✓ |
| 2 | **镜像里没有 `openai` SDK** ✗ | `LLM 降级为规则兜底: OpenAIClientError: 需要 openai SDK` ✗ ⇒ 对话 ⇒ 工具调用 ⇒ `bilibili_search` ⇒ 队列 ⇒ 视频**整条链断** ✗（界面上像"变笨了"） | §5.6 那张表把 openai 标成"optional／镜像里没有（懒加载）"✗ —— 但它挡的是**主功能** ✓；板端 Ubuntu 期那份是装在 `/data` 的 `openai 3.24.0`（docs/perf-cpu-mem.md ✓）| 新增 `image/prepare-openai.sh`（钉 `openai==3.24.0` ＋ `image/openai-wheels.lock` 逐轮子 sha256 校验 ✓，按 `prepare-rknnlite.sh` 的套路解进 site-packages ✓）；检查器把 openai/httpx2/pydantic/jiter **改判 required** ✓ ＋ chroot 里真 `import openai` ✓ |
| 3 | **python 缺 `zlib`** ✗ ＋ **gst 缺 `tsdemux`** ✗ | ① `httpx2/_decoders.py: import zlib → ModuleNotFoundError` ✗（⇒ openai 也起不来 ✓）；② 播放器 `No decoder available for type 'video/mpegts…'` ✗ ⇒ 面板"视频源未接入" ✗ | ① 厂商基座把 python3 扩展模块全关着 ✗（同 §5.10 的 `_ssl`/`readline` ✓）；② 片段里 `BR2_PACKAGE_GST1_PLUGINS_BAD_PLUGIN_MPEGTSDEMUX=y` ✓ **但包没重编** ✗ ⇒ 插件没进镜像 ✗（**同一个"改子选项 ≠ 重编包"的坑** ✓）| ① defconfig 加 `BR2_PACKAGE_PYTHON3_ZLIB=y` ✓（在所有 `#include` 之后 ✓；CI 守卫 `TestPythonModulesTheAgentNeeds` 同步 ✓）；② `rm -rf build/gst1-plugins-*` 强制重编 ✓（⚠ **`gst1-plugins-rockchip` 也要一起** —— 它就是 `mppvideodec` 的家 ✓，漏了就"插件齐了但硬解没了" ✗）|

**另有两条只踩一次就够了的教训** ✓：
- **强制重编 python3 会清掉 site-packages 里别人的文件** ✗（`numpy`/`rknnlite`/`cv2` 都被清空 ✗，而"已装"的戳还在 ⇒ 不会自动补 ✗）⇒ 必须把它们**一起**强制重装 ✓。**检查器的 chroot 冒烟正是判据** ✓（"在位"只看路径存在 ✗ ⇒ 空目录能蒙过去 ✗）。
- **掉 fastboot 的现场救援**（这次真用了 ✓）：`misc` 两个槽被判死后，从 u-boot 清零 misc ⇒ SPL 自己重写默认元数据 ✓（runbook §5.2 option 2 ✓）。串口是唯一通道（fastboot 在串口上**完全静默** ✗，但 Windows 设备里会出现 `USB download gadget VID_18D1&PID_4D00` ✓）。

### 5.13.2 ✅ 新的标准镜像（2026-10-07 晚，这一版是**真的**）

| 项 | 值 |
| --- | --- |
| 构建会话 | `<SDK>/output/sessions/2026-10-07_21-10-40/` |
| `rootfs.ext2` | **761,266,176 B**，sha256 `4c9a91dc9756e3c3b68e9b21d9f90ef156e2130163938e89aa2e14b36509b1ea` |
| `boot.img` | 54,307,328 B，sha256 `fa229ab9427bd4ab6c8ccae6abf714be41f52a5e29c57eb057f11006302f8e54` |
| target 里的 GUI | md5 **`a986143fa6f1ea10a0ce4cbbf3d3e0da`**（与 §7.10 在板上验过的那份**逐字节一致** ✓）|
| gst 插件 | **54 个** ✓，含 `libgstmpegtsdemux.so`（`tsdemux` ✓）与 `libgstrockchipmpp.so`（`mppvideodec` ✓）|
| python | 3.11.8 ＋ numpy 1.25.0 / cv2 4.9.0 / yaml / psutil / ruamel / rknnlite ＋ **openai 3.24.0（+ httpx2 / pydantic / jiter）** ✓ ＋ `zlib` 扩展 ✓ |
| weston | 文件 **0** ✓／进程 0 ✓／`weston.service` 不存在 ✓ |
| `preflash-check.sh` | **结论：通过**（在位缺 0、依赖缺 0、冒烟失败 0、payload 未做 0/12）✓；开机目标 全过 ✓ |

### 5.13.3 上板方式（A/B，不动 `/data` 的那条路 ✓）

板子跑在一个槽上时，把新镜像写**另一个**槽 ✓（`dd` 到 `system_a|b` ＋ `boot_a|b` ＋ 整块写 `misc` ✓），
写完**读回校验 sha256** ✓ 再重启 ✓ —— 全程不需要 USB/按键，也不碰 `/data` ✓。

```bash
# 槽与标签（别靠 p6/p7 猜 ✓）：boot_a→p3  boot_b→p4  system_a→p6  system_b→p7  misc→p2  userdata→p9
dd if=rootfs.ext2 of=/dev/mmcblk0p6 bs=1M  count=<MiB>   conv=fsync   # system_a
dd if=boot.img    of=/dev/mmcblk0p3 bs=512 count=106069  conv=fsync   # boot_a
dd if=misc-X.img  of=/dev/mmcblk0p2 bs=512 count=96      conv=fsync   # 48 KB 整块（主+备都在内 ✓）
# misc 用 agent/core/ota.py 的 build_bcb/apply_bcb_to_misc 生成 ✓（唯一实现，别手抄 CRC ✗）：
#   目标槽 prio15/tries7/succ0（**未确认** ⇒ 起不来会按 tries 扣完自动回退 ✓）
#   另一槽 prio14/tries7/succ1（保持"已确认"，当回退 ✓）
```

### 5.13.4 换槽后的最终实测（2026-10-07 21:2x，板上 ✓）

```
槽 = _b（root=…54aa = system_b）✓   GUI md5 = a986143f… ✓   weston 文件/进程 = 0/0 ✓
服务全 active ✓（assistant.target/agent/agent-gui/assistant-init/sshd）
misc: B **prio15/tries7/succ1** ✓ ⇒ assistant-ota-confirm 已把新槽标成功 ✓
/data: 22.7G 用 12.4G、模型 4.9G ✓、config.yaml ✓、bilibili_cookie.json ✓
对话: 「罗刹海市已成功搜索到…」✓（模型答的 ✓）｜agent: 搜「罗刹海市」-> 成功（18 条）✓
      ⇒ 队列填上 ⇒ `可以播了` ✓（等 10 秒）
★ 零拷贝视频: caps 640x360 ✓｜绘制 **25.0 帧/s** ✓｜画帧间隔 40.0ms（最大 47~57ms）✓
             ｜零拷贝帧 == 已画帧 ✓｜**vqueue = 0** ✓｜**page flip = 0** ✓｜**NRestarts = 0** ✓
```

⇒ **标准镜像 = 这一版** ✓（含 §7.10 的零拷贝修复 ＋ 无桌面 ＋ 对话/工具调用可用 ✓）。
**仍未做**：整包 USB 重刷 ✗（`update.img` 那条会重排分区、清 `/data` ✗）—— 需要时按 `FLASH-RUNBOOK.md` 走 ✓。

### 5.13.5 ⚠ 刷完机/换槽之后**必做的两件小事**（2026-10-07 实测踩到）

这套镜像**故意不把密钥放进 rootfs**（用户 2026-10-07 决定：不做持久化，走"刷完手动恢复"✓）⇒
**每次换槽/刷机后**下面两处都会失效 ✗：

```bash
# ① PC 侧：sshd 主机密钥换了 ⇒ 清掉旧指纹（否则 ssh rk3568 报 REMOTE HOST IDENTIFICATION HAS CHANGED）
ssh-keygen -R 192.168.137.30          # PC 上跑；之后第一次连接会重新登记 ✓

# ② 板端密钥恢复（PC 侧不用再动 —— 装的还是同一把公钥 ✓）
cp /data/assistant/keys/id_ed25519* /root/.ssh/ && chmod 600 /root/.ssh/id_ed25519
#    没有备份时：板端 ssh-keygen 生成，再按 docs/music.md §3.2.1 把公钥放到 PC 的
#    C:\ProgramData\ssh\administrators_authorized_keys（管理员 PowerShell ✓ 纯 ASCII 脚本 ✓）
```

**别小看 ② 的后果** ✗：缺了板端客户端密钥，agent 会**每 3 秒**重试一次连 PC 并刷 WARNING ✗
（实测一晚刷到 **30755 条** ✓、负载抬到 1.2 ✗），**视频会偶发几秒级卡顿** ✗
（实测最大画帧间隔 3.8~7.6 秒 ✗；洪流停掉后立刻回到 **25fps / 40ms** ✓✓）。
完整步骤与验证命令见 `docs/music.md` **§3.2.1** ✓。

### 5.13.6 Moonlight（板子）↔ Sunshine（PC）连上：**只要拷预配对凭据**（Sunshine 侧不用动）

**2026-10-07 实测跑通** ✓。要点是「**配对早就做过了**」✓（见 `todo.md:105-121` 的 B2 条目）：

- Sunshine 授权名单（`D:\tool\sunshine\config\sunshine_state.json`）里 **`agent-native` 在列且 enabled** ✓，
  证书长度 **1208** = `E:\rk3568\local\creds\client.pem` ✓ —— 配对当时用的是 **PIN 5678** ✓
  （`docs/sunshine-pairing-findings.md:107`、`scripts/pair-run.sh` 的默认值 ✓）。
- 所以**不需要再配对、也不需要动 Sunshine** ✓；新镜像里缺的只是那对凭据文件 ✗
  （旧 Ubuntu 把它们放在 `/home/kickpi/myproject/assitant/creds/` ✗，而 `/data` 侧只有 `assistant-init` 建的空目录 ✓）。

```bash
# ① 拷凭据（PC → 板子；配置里 sunshine.cert/key 本来就是这两个路径 ✓）
scp E:/rk3568/local/creds/client.pem E:/rk3568/local/creds/client.key rk3568:/data/assistant/creds/
ssh rk3568 "chmod 600 /data/assistant/creds/client.key; chmod 644 /data/assistant/creds/client.pem"

# ② **重启 agent**（native 只在启动时握手一次 ✓，原因见下的 bug）
ssh rk3568 "systemctl restart agent"

# ③ 判据（agent 日志）
#    sunshine: 192.168.137.1 appversion=7.1.431.-1 PairStatus=**1**
#    sunshine: app=Desktop -> appid=881448767 (走 /resume) sessionUrl=rtspenc://192.168.137.1:48010
#    PC 侧 D:\tool\sunshine\config\sunshine.log：CLIENT CONNECTED + Creating encoder [hevc_nvenc] ✓
```

**⚠ 这条路上有个真 bug（待办，本轮未修）** ✗：`native` **只在 agent 启动时**握手一次 ✓，
而开机时 agent 起得比网络早 ✗ ⇒ 每次开机日志里都是

```
SunshineError: 连接 192.168.137.1:47984 失败: [Errno 101] Network is unreachable ✗
```

⇒ **失败后不重试** ✗ ⇒ 重启板子后 moonlight 一直是断的 ✓，得手动 `systemctl restart agent` 才连上 ✓。
修法（二选一，都还没做）：① `native` 失败后按退避重试（像 music/LLM 那几条一样 ✓）；
② `agent.service` 依赖 `network-online.target`，并把握手推迟到网络真就绪（板端 WiFi 要 ~11 s ✓，
而 agent 开机 ~2 s 就握手 ✗）。

**排查时别被这个骗了** ✗：`netstat` 里 `192.168.137.1:48010 ← 板子` 全是 **TIME_WAIT** 是**正常的** ✓ ——
48010 是 RTSP **握手**（短连接 ✓），真正的视频/音频走 **UDP 47998~48000** ✓。
判"是否真在串流"要看板端线程（`VideoRecv` / `VideoDec` / `mpp_dec_parser` / `mpp_dec_hal` / `ReqIdrFrame` ✓）
与 PC 侧 Sunshine 日志的 `CLIENT CONNECTED` ✓。

> 附带发现：`study` 那一路现在报 `game_watch: SigLIP 加载失败: 缺少 tokenizers 库` ✗
> （镜像里 `tokenizers` 仍是 optional ✓，与 `openai` 那次同源 ✓）—— 想让「认游戏/学习监督」吃上真 moonlight 帧，
> 得照 `prepare-openai.sh` 的套路把 `tokenizers==0.20.3` 也注入 ✓（还没做 ✗）。

**⚠ 还没做的：刷板**（`T15-2-11` 的正文 ✗）。这套 `update.img` 会**重排 eMMC 分区**
（板上现有数据全丢 ✗，第一次还要 `mkfs.ext4` userdata ＋ 投放 4.9 GB 模型 ✓），
所以不在"标准化"里顺手做 ✗ —— 步骤见 `image/FLASH-RUNBOOK.md`，
烧写用的路径是 **`output/update-ab/Image/update.img`**（或那条带版本号的软链 ✓），
**不要**用 `output/firmware/update.img`（AB 形态下它是**悬空软链** ✗）。



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

## 9. 附：盘点用到的命令（可复现）

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
