# todo2 — T15 阶段：极小镜像 + 系统级优化（新任务清单）

> 这份是**新的任务清单**，接在 [`todo.md`](todo.md)（T1–T14 历史）后面。
> 背景：你 2026-09-27 定了**路线 B（重新构建系统镜像，这个软件作为唯一启动）**，
> 并给了 11 个优化方向。这份把它们排成**有依赖关系的顺序**，每项都带"出口条件"（可验收）。
>
> 铁律（延续 T14）：**先取证/量化 → 再动手 → 每项独立验收 → 每项留守卫**。
> 度量统一用 `scripts/monitor.sh`（CPU/内存/温度/RSS）、`scripts/bench.sh --baseline`（SigLIP/MPP/端到端）、
> `systemd-analyze`（启动）、以及那张**功能回归清单**（T14-7 自启与 1280×800、T14-9 WiFi+重连、
> T14-3 单写者、T14-8 崩溃日志、软键盘、B 站硬解、音乐条、日程）。

---

## 0. 已完成的前置（本次会话，留档）

| 项 | 结论 | 证据 |
| --- | --- | --- |
| 受控实验 E1a | **linuxfb 能画**（Qt 直接画 /dev/fb0） | fb 非黑像素 100%、1260 色 |
| 受控实验 E1b | **EGLFS + KMS 可用** | QPA 日志 `New DRM/KMS via GBM integration created` → `Using "/dev/dri/card0"` |
| 受控实验 E2a/E2b | **无 X 硬解 + 上屏可用** | `mppvideodec→fakesink` 与 `→videoconvert→kmssink` 都 EOS、退出码 0 |
| 受控实验 E3 | **软键盘有替代** | `qtvirtualkeyboard-plugin` 5.12.8 有包 |
| T15-0 | socket 被误删的隐患已硬化（新鲜度 + 连探两次 + 证据日志） | `db176bd`，板端 130 项 ipc 测试 OK |
| T15-1 切片1a | **我们的 GUI 在 EGLFS（无 X）下跑通**：`--screenshot` 出图、退出码 0；加 `QT_QPA_EGLFS_ROTATION=90` 后是 **1280×800 横屏**、逐项与 X 基线布局一致 | QPA 日志 `New DRM/KMS via GBM integration created` → `Creating GBM device for /dev/dri/card0`；截图 935×1280（未转）与 1280×800（转后）；唯一告警是 EGLFS 下的光标 `Failed to move cursor on screen DSI1: -14` |
| T15-1 切片1b 取证（旧） | 旧结论"Qt 虚拟键盘可用"**只看了一半**：`Loading extension "default"` 只证明插件加载，**QML 面板其实没起来** —— 真跑起来报 `InputPanel.qml: module "QtQuick" is not installed` | 已由下面的 1b 结论取代 |
| T15-1 切片1c 取证 | GStreamer 控制路径已验证（`mp4→qtdemux→h264parse→mppvideodec→videoconvert→kmssink` 退出码 0）；但**上一轮 `--video` 的截图无效** —— 视频面板**只在 GAME 模式**显示，HOME 模式主区是壁纸（截图里主区纯蓝、底部仍是「未播放」）| 1c 下一步：`--mode-demo GAME --video <mp4>` 在 EGLFS 下重跑；若 `QMediaPlayer` 在 EGLFS 渲染不出来，再实现"Agent 侧 ffmpeg→`kmssink` 直出"那条路（T15-8 一并做） |
| **T15-1 1b 结论（无 X 软键盘：弹出 + 画出来 + 能打字 + 不挡输入框，全绿）** | 复现脚本 `tests/board/t15_1b_keyboard_nox.py` 四段跑全 PASS：<br>· P1 **linuxfb 像素**：点焦点后 fb 底部条带强边界 0 → 500 条，存帧里是完整 QWERTY（`Q W E R T Y…`/Shift/&123/`American English`）<br>· P2 **EGLFS 产品形态**：屏 `0,0 1280x800`、主窗口没被改小、键盘窗口 `QtVirtualKeyboard::InputView` 可见、`IM_VISIBLE=1`、QML 缺模块 0 条、`[ui] 无 X（eglfs）：软键盘交给 Qt 输入法` 有<br>· P3 **能打字**：uinput 虚拟触摸屏点键盘 → `INPUT_SAMPLE` 逐秒 `"" → Mu, → Mu, i.`（峰值 7 字）<br>· P4 **vnc 抓帧**：键盘条带 `y=440..720`，与 Qt 自报键盘上沿 400 一致 | 键盘矩形 **`IM_RECT 0,400 1280x400`**（QVK 默认样式：高 = 屏宽 × 800/2560）；`INPUT_RECT` 修好后 **`961,331 82x48`**（下沿 379 < 键盘上沿 400）|
| **T15-1 1b 真卡点①：QVK 的 QML 依赖装不全就是"白板"** | `qtvirtualkeyboard-plugin` 的 **依赖里没有** QML 运行时模块 —— 只装它，面板窗口起来了、`IM_VISIBLE=1`，但**一个键都画不出来**（linuxfb 下整屏纯白）。缺的四个包（按报错逐个补）：`qml-module-qtquick2`、`qml-module-qtquick-window2`、`qml-module-qtquick-layouts`、`qml-module-qt-labs-folderlistmodel` | 报错原文：`InputPanel.qml:30 module "QtQuick" is not installed` → `components/Keyboard.qml:31/32/37 module "QtQuick.Layouts"/"QtQuick.Window"/"Qt.labs.folderlistmodel" is not installed`。**这四行就是 T15-2/T15-12 最小镜像的必装清单**（装完 0 条报错） |
| **T15-1 1b 真卡点②：键盘正好压住对话输入行（不挡布局差点没做到）** | 键盘占屏下半截 400px，而对话输入行在 `y=496..544` —— **整条被压住**。修法见 `MainPage::setKeyboardInset()`：打字时把模式卡/日程卡**压成 0 高并隐藏**、把对话卡高度**上限**压到键盘上沿以内（输入行在卡片底部，于是被顶到键盘上方），列尾加一根弹簧吸掉多余空间；收键盘全部复原 | 三条踩坑（都写进 `gui/tests/test_keyboard_inset.cpp` 当不变式）：<br>① **别用"给布局加下边距"让位** —— 布局最小高度 = 内容最小 + 下边距，板端窗口从 800 涨到 **1063**，输入行反而更往下掉；<br>② **隐藏的控件在布局里仍然占地方**（模式卡藏了、对话卡还停在 y=283），得把高度上限也压成 0；<br>③ 光压上限，QBoxLayout 会把多余空间往两头分（对话卡被摆到列中），**必须加一根 tail spring** 才贴列顶 |
| T15-1 1b 平台事实（选型依据） | · `linuxfb` **转不了屏**：`rotation/invertx/inverty` 是 **evdevtouch** 的参数，不是 linuxfb 屏幕的；`/sys/class/graphics/fb0/rotate` 写进去 `virtual_size` 也不变 → linuxfb 下 app 被挤成最小尺寸 935×663 且裁掉右半边，**只能当像素证据、不能当产品形态**<br>· `linuxfb`/`vnc` **没有 alpha 合成**：QVK 那个"半透明整屏窗口"在它们上面变成**不透明白底**（linuxfb 帧里上半屏颜色只有 1 种）→ 所以产品必须用 EGLFS（窗口合成器带 alpha 混合）<br>· EGLFS `QScreen::grabWindow(0)` **抓不到东西**（返回一张 800×800 空白）<br>· 板端 `gst-launch-1.0 rfbsrc` 一连就 `std::runtime_error` 崩（rc=-6）；自己写的最小 RFB 客户端要按 **RFB 003.003** 握手（Qt 的 vnc 平台只说 3.3：直接一个 u32 安全类型、没有 SecurityResult）<br>· `linuxfb` + QVK **退出时 SIGSEGV**（崩溃日志最后是 `hideInputPanel()`，发生在 dump 之后、不影响证据）；EGLFS 下退出码 0 | 见 P1/P4 日志与 `/tmp/t151b_P1_post.png`、`/tmp/t151b_P4_vnc.png` |
| **T15-1 1c 结论（无 X 视频路线通过）** | EGLFS + 无 X 下，`QMediaPlayer` **真的在播 Agent 那种本机 HTTP MPEG-TS 流**：`[video] 换源(URL)` → `state=1`，position **840ms → 15953ms** 实时推进，还回报 `video_state{playing:true}`；**截图里主区那片纯蓝就是视频帧**（测试片是纯色图样），地址栏/队列卡片都在位；**无任何多媒体错误** | 复现脚本 `tests/board/t15_1c_video_nox.py`（假 IPC 推送 + ffmpeg `-listen 1` 本机 TS 流，把 bilibili/LLM 的不确定性拿掉）。**结论：GUI 视频路径不需要改 `kmssink`**；真 B 站流只是把"流从哪来"换成 Agent 的真队列，播放这一层已经验过 → 真流联调并入 **T15-8** |
| T15-1 1c 真实卡点 | **`--video` 驱动不了 GAME 模式的面板**：那张截图里模式已切到「游戏」、主区是视频面板，但显示 **「视频源未接入」/「(还没有队列)」** —— 那个面板是 **B站队列**驱动的（`--video` 只改了"本地文件源"，没进队列） | 1c 正确做法二选一：(a) 走队列路径（Agent 侧把本地文件当"假队列"喂进去）(b) 用真 B 站流（需要真搜索 + Agent 的 stream）——(b) 本来就属于 T15-8 视频调优，建议合并 |
| **T15-1 1d 无 X 全量回归** | 复现脚本 `tests/board/t15_1d_regression_nox.py`：把 `agent-gui.service` **整份换成无 X 单元**（`systemd/agent-gui-nox.service`）→ 停 slim → 服务在 EGLFS 下起来。**16/17 项过**：`t14_3` 16 项、`t14_9` 32 项、`p8_coexist` 19 项全过；四页（home/model/system/settings）EGLFS 截图都是 1280×800 有内容、0 条错误行；`t14_8` 45 项在**清掉测试期遗留的会话文件后**也全过 | 唯一那项"失败"是 `t14_8` 的 B6 数 `logs/crash/gui-*.log` 个数：被 kill -9 过的测试进程留下 19 份"只有表头"的会话文件（**那是真的没干净退出**，产品会当历史报告留着），B6 的"停服务后应当为 0 / 起回来应当为 1"被这些历史干扰。单独验证过：清干净后无 X 形态下 `起=1 → 停=0 → 再起=1` ✓（脚本已把"先挪走历史"这一步固化，挪到 `/tmp/t151d_crashtrash`，**不删**）|
| **T15-1 1d 空闲数字（无 X，GUI 在跑）** | 整机忙 **25.4%**（4 核）；GUI CPU 均值 **1.0%** / 峰值 12.2%、RSS **99 MB**；Agent RSS **121 MB**；`free` used 1193 MB / available 2654 MB（有 X 时 used 1482 MB）。**凶手不是 GUI**：5 s 采样里 Agent 那个 python3 进程占 **85% CPU** | 热点线程 = **`VideoDec` 80.1% CPU、累计 8 分 12 秒**，来自 `native/third_party/moonlight-common-c/src/VideoStream.c:350`（Moonlight 串流解码线程）；进程里加载了 `libavcodec` + `librockchip_mpp`；日志证据 `sunshine: app=Desktop -> appid=881448767 sessionUrl=rtspenc://192.168.137.1:48010`。**即：设备在 STUDY 模式发呆时，Agent 仍常驻拉流+解码 PC 桌面，白烧一个核** → 交给 **T15-5 / T15-6 / T15-8**（"不串流时该断开 / 降帧 / 降分辨率"）|
| **T15-1 1d 卡点：X 形态的单元没法"只是换个平台"** | `systemd/agent-gui.service` 写着 `Requires=display-manager.service` + `ExecStartPre` 等 `/tmp/.X11-unix/X0` + `Environment=DISPLAY=:0`：**只加 `QT_QPA_PLATFORM=eglfs` 的 drop-in 会把 X 一起拉起来**（实测 `pgrep -c Xorg` 0→1），EGLFS 与 Xorg 抢 DRM master，`t14_8` 因此假失败；而 systemd 245 上 drop-in 也**删不掉** `Requires=`/`After=`（同 `xrandr-startup` 那份记录）| 所以整份替换：新增 `systemd/agent-gui-nox.service`（无 `display-manager` / 无 DISPLAY / 不等 X socket + 四个 Qt 环境变量），它同时是 **T15-12** 定稿的底稿 |

**结论：极小镜像可以不要 X**（省实测 703 MB 桌面栈 + 整条 X 依赖链）。

---

## 1. 顺序总览（为什么这么排）

```
T15-1 GUI 无X化(3切片)  ─┬─> T15-4 参数引出到 GUI/CLI ─┬─> T15-5 CPU 调度
        （定形态）        │                            ├─> T15-6 内存/碎片/压缩
T15-2 SDK 组件探测 ───────┘                            ├─> T15-7 低功耗/睡眠
  （能不能编出最小镜像）                                └─> T15-8 视频编解码调优
T15-3 代码规范审计（重复造轮子/死代码）─────> 与 T15-4 一起做
T15-9 以太网 / WiFi 分别测试与自动切换   （独立，可与 T15-5~8 并行）
T15-13 旋转的"无损"实现                  （独立，但要在 T15-1 之后定）
T15-10 GUI 美化                          （依赖 T15-1 定形态）
T15-11 开发镜像 / 发行镜像               （依赖 T15-2 的配方）
T15-12 最小 rootfs + 唯一启动目标         （依赖 T15-1/5/6/7）
T15-14 烧写 / 升级 / 无线 OTA            （依赖 T15-11/12）
T15-15 文档收口（docs/image.md、docs/power.md、docs/net.md 增补）
```

**三条硬依赖**：
1. **形态未定不要动资源优化** —— SIGLIP/LLM 的调度预算取决于"有没有 X、GUI 怎么渲染"；
2. **参数没引出不要做镜像** —— 镜像会把默认值固化，之后每改一个参数都得重打包；
3. **镜像未定不要做 OTA** —— OTA 的粒度取决于分区方案（A/B 还是单分区）。

---

## 2. 任务清单

### T15-1 GUI 无 X 化（进行中，切三片）
| 切片 | 内容 | 出口条件 |
| --- | --- | --- |
| **1a** ✅ | 平台选择与旋转：`QT_QPA_PLATFORM=eglfs`（linuxfb 只能当像素证据，它**转不了屏**）；旋转走 `QT_QPA_EGLFS_ROTATION`（内核 cmdline/DTB 留给 T15-13）；GUI 在无 X 下起来且**方向正确、1280×800 满屏** | 板端无 X 下 `--screenshot` 出的图与有 X 时布局一致（逐图对照）|
| **1b** ✅ | 软键盘替换：`onboard`（GTK/X11）→ **Qt VirtualKeyboard**；**另外补了"键盘不压住输入行"的让位逻辑**（`MainPage::setKeyboardInset`，见 §0 卡点②） | 无 X 下点输入框能弹出、能打字、不挡布局 —— 复现脚本 `tests/board/t15_1b_keyboard_nox.py` 四段全 PASS |
| **1c** ✅ | 视频：`QMediaPlayer` 在 EGLFS 下**不需要**改 kmssink（真播了本机 MPEG-TS 流，position 推到 15.9s）| 真 B 站流的联调并入 **T15-8** |
| 1d ✅ | 回归：无 X 形态下跑完整功能回归清单 + 空转 CPU 长采样 | `tests/board/t15_1d_regression_nox.py` 16/17 项过（唯一那项是 `t14_8` 数遗留会话文件，已查清并固化清理）；空闲数字 + 凶手线程已留档（见 §0）；`docs/gui.md` §1.1 已补无 X 形态（环境变量、**四个 QML 必装包**、键盘让位、无 X 单元、平台陷阱表）|

### T15-2 【路线 C】厂商 6.1 SDK + Buildroot 重建最小镜像（用户 2026-09-28 已批准任务列表）

**决策（用户拍板，详见 [docs/image.md](docs/image.md) §1）**：D1 走 **路线 C**（SDK + Buildroot 重建，不是裁剪现镜像）；
D2 允许 WSL 联网构建；D3 允许刷板（Maskrom 兜底）；D4 **开发镜像 + 发行镜像都要**；
D5 OTA **走无线**（→ 分区表必须现在定）；D6 睡眠**允许息屏**，唤醒要 **触摸 + CLI 双通道**。

**SDK 事实**（盘点见 [docs/image.md](docs/image.md) §3）：KICKPI 定制 Rockchip Linux 6.1 **V1.2.0**，内核 6.1.141、u-boot 2017.09、
buildroot 2024.02、Qt 5.15.11（联网拉）；**K1Mini 是一等目标**（`.chips/rk3566_rk3568/rockchip_rk3568_kickpi_k1Mini_{ubuntu,debian}_defconfig`）；
厂商 .deb 全集里有 **`libmali-bifrost-g52-g24p0-x11-wayland-gbm`**（解决 EGLFS 的 G52 缺口）；板端面板变体已核实 =
**`rk3568-kickpi-lcd-mipi0-10.1-800-1280-v2-k1Mini.dtsi`**。

| # | 任务 | 出口判据 |
| --- | --- | --- |
| 2-1 ✅ | 决策与配方文档定稿（`docs/image.md` §1–§4） | 文档能独立复述 D1–D6 |
| 2-2 ✅ | K1Mini 的 buildroot defconfig 落地（release 骨架 + **systemd** + Qt5 替 weston） | 配方在 `image/`（本仓库维护 + `image/install-into-sdk.sh` 注入）；SDK 里 `make O=output/rockchip_rk3568_kickpi_k1mini_release rockchip_rk3568_kickpi_k1mini_release_defconfig` **退出码 0**，`.config` 里 systemd / 串口 CLI / Qt Widgets+EGLFS / 虚拟键盘(en_US+zh_CN) / QML 运行时 / **G52+GBM** / RKNPU2 / MPP / NM+nmcli / CA 证书 / python3+numpy+pyyaml+opencv4 / yaml-cpp / libinput 逐项在位（行号见 `docs/image.md` §5.1）|
| 2-3 ✅ | 板级对齐（面板变体 / **触摸复位脚 PB5→PB6** / WiFi / 以太网 / PMIC / 容量） | 差异表 + 结论见 `docs/image.md` §6：**要改 2 处**（面板 include → `mipi0-10.1-800-1280-v2`；触摸复位 → PB6，厂商 pinctrl 与板端 live DT 都是 PB6，是那句属性写错）；改动落在 `image/kernel/` 三个文件，并用**内核真管线编出 dtb（退出码 0）**后反编译核对（触摸 PB6 ✓ / 面板 800×1280 ✓ / wifi rtl8822cs ✓ / PC7 使能 ✓ / 双 gmac 延迟 ✓）|
| 2-4 ✅ | **分区与 OTA 布局定稿**（A/B；模型 4.9 GB 落点） | 见 `docs/image.md` §7：**A/B 双 rootfs 3 GiB×2 + 共享 userdata（grow ≈22.8 GiB）**，模型/配置/日志都放 userdata（OTA 不必重下 4.9 GB）；机制用 SDK 原生 `RK_AB_UPDATE` + `ota-updateimg`，**回退靠 `misc` 元数据**；分区表 `image/device/.../parameter-assistant-ab.txt` 由 `image/check-parameter.py` + `tests/test_image_parameter.py`（8 项，含四种坏表）守住 |
| 2-5 ✅ | libmali G52(GBM) 进 buildroot | 见 `docs/image.md` §5.4。**blob 不在 SDK 的 `external/libmali` 里**（那里只有 RK3588 的 valhall-g610），它在 SDK 自带的厂商 deb 里 → 新增 `image/prepare-libmali.sh`：两级 sha256 校验后按 buildroot 期望的名字 `libmali-bifrost-g52-g24p0-x11-wayland-gbm.so` 落位（名字是**算**出来的：gpu / version / subversion(空) / platform 四段，platform 由已启用的 winsys 拼；再用厂商自带的 `grabber.sh` 与 `parse_name.sh` 互相印证）。`make rockchip-mali` **退出码 0**（22 分 46 秒，含首次**从零编的整条交叉工具链**），进镜像的 blob 与源 **逐字节相同**、`gbm_*` = **39**（≥30）、`BR2_PACKAGE_HAS_LIBGBM=y`、blob 的 **16 个 `DT_NEEDED` 缺失 0**，sysroot 里 `egl.pc`/`gbm.pc`/EGL·GLES 头齐全（2-6 的 Qt5 要用）。<br>⚠ **连带发现（改了一条路线 C 的细节）**：这份 blob 的 `DT_NEEDED` 硬依赖 `libX11 / libX11-xcb / libxcb(+dri2,dri3,xfixes,present) / libwayland-client / libwayland-server` → 无 X 的镜像**也必须**带这些**客户端**库（实测 **+3,760 KB**），所以片段里开了 XORG7 + WAYLAND（`HAS_X11`/`HAS_WAYLAND` 依赖它们，不然平台串会变）；**但 QPA 仍是 eglfs、`QT5BASE_XCB` 保持关**，没有 X server / 桌面。<br>⚠ **环境坑**：WSL 的 PATH 里带 Windows 条目（含空格）会让 buildroot 第一步 `dependencies.sh` 就死（`This doesn't work. Fix you PATH.`）→ 新增 `image/sdk-make.sh` 作为统一构建入口；`tests/test_image_recipe.py`（16 项）把这些不变量与"四条配方纪律"钉进 CI（另：把漏注册的 `tests/test_image_parameter.py` 一起补进测试清单，之前它根本没被 CI 跑到） |
| 2-6 ✅ | Qt5.15 + EGLFS + 虚拟键盘 + 四个 QML 模块 | 见 `docs/image.md` §5.5。`make qt5base qt5declarative qt5svg qt5quickcontrols2 qt5virtualkeyboard` **退出码 0**（**9 分 58 秒**），Qt 实测 **5.15.11**。<br>① **卡点（R2 的真身）**：这个 buildroot 的 Qt 走 `invent.kde.org` 的**按 commit 现生成**归档，而 KDE 会**重新打包** → sha256 与 buildroot 钉的不符，构建在下载校验就死。**先证明内容没问题**（包内 `.qmake.conf` = `MODULE_VERSION 5.15.11`；包内 LICENSE 三个文件的 sha256 与 buildroot 在同一个 `.hash` 里钉的**逐文件哈希完全一致**；同一 URL 两次下载字节相同），再由新增的 `image/prime-dl.sh` 从 buildroot 的 primary site 取**与 hash 一致的那份**（57,929,506 B）预置进 `dl/`（6 个包全部校验通过）。**没有改 buildroot 的 hash、没有改厂商树**。<br>② **验收**：`libqeglfs.so` 在位且 `DT_NEEDED` 里**直接有 `libmali.so.1`**（+ KMS/GBM 集成插件 `libqeglfs-kms-integration.so`）；`libqtvirtualkeyboardplugin.so` 在位；**四个 QML 模块**在 `usr/qml/{QtQuick.2, QtQuick/Window.2, QtQuick/Layouts, Qt/labs/folderlistmodel}`，各有 `qmldir`+`plugins.qmltypes`+插件 `.so`，**四个插件的 DT_NEEDED 缺失都是 0**。<br>③ **T15-1 1b 的卡点在镜像里被机器化复现**：`QtQuick/VirtualKeyboard/qmldir` 头上就写着 `depends QtQuick 2.0 / QtQuick.Window 2.2 / QtQuick.Layouts 1.0 / Qt.labs.folderlistmodel 2.1` —— 正是那四个。<br>④ 语言布局：`en_US` 经查是**合法**布局（`config.pri` 第 83/91 行有 `lang-en(_GB)?`/`lang-en(_US)?`），`zh_CN` 走拼音插件（`Pinyin`/`layouts/zh_CN/main.qml` 都在）→ 片段不用改。<br>⑤ 附带确认：`libQt5Quick` 里 56 个 software 符号 → 现有 `QT_QUICK_BACKEND=software` 仍可用（但 Mali GL 已通，不再是必需）。体积账（target 313 MB，其中 `usr/include/qt5` 32 MB 是厂商打包副作用）留给 2-9/2-12 |
| 2-7 ✅ | 我们的运行时与依赖闭环（python3/numpy/cv2/yaml、yaml-cpp、librknnrt、MPP+gst、NM、sshd、字体、llama-server+模型） | 见 `docs/image.md` §5.6。<br>① **落点（D7）**：代码进 rootfs（`/usr/lib/assistant/llm/{bin,scripts}`）、可写状态进 userdata（`/data/assistant/{config,llm,logs,run}` 与 `/data/model`）—— A/B OTA 换系统槽不会把派生配置/日志/PID 冲掉。为此给 `llm/scripts/*` 加了 `LLM_ENV_FILE`/`LLM_STATE_DIR` 两个覆盖点，并把**原先不在仓库里**的那 5 个脚本（板端手工投放）收进仓库（PID 身份判断改用 `/proc/<pid>/cmdline`，不依赖 procps）。<br>② **R8 依赖清单**：用 AST 扫出仓库真实的第三方 import 逐个定性（required 6 / optional 2 / host 5），机器化在 `image/check-runtime-deps.py` 的 `PY_MODULES`；新增 `tests/test_image_runtime.py`（7 项）反过来核对"有没有新 import 没定性"—— 它当场就抓出 3 个漏项。<br>③ **装进镜像**：新开 `bash`、`libcurl`+**curl 二进制**、`python-psutil`、`python-ruamel-yaml`；rknnlite 用 SDK 里 **cp311** 那份 wheel（**8 个编译扩展**，必须按镜像的 python 3.11.8 挑，SDK 自带 `packages.md5sum` 校验）；llama-server 由 `image/build-llama.sh` **交叉编译**（commit `b387ddfd8` = build 10677，参数与板端逐条对齐，只把 `GGML_NATIVE` 换成 `GGML_CPU_ARM_ARCH=armv8.2-a+dotprod+fp16`）；字体 `source-han-sans-cn` + DejaVu/Liberation/FontAwesome。<br>④ **验收（`image/check-runtime-deps.py`，结论"通过"）**：逐项在位**缺 0** + **DT_NEEDED 闭包：1970 个 ELF、缺失 0** + **真 chroot 冒烟全绿**：python 3.11.8、`numpy 1.25.0`+`cv2 4.9.0`、**`rknnlite` 真能 import**、bash、`curl 8.6.0`、`nmcli 1.44.2`、gst `mppvideodec`、思源黑体 CN、llama-server。⚠ 验收器自己被抓出过一次**假阴性**：第一版闭包把"可执行文件自己所在目录"也算进搜索路径，于是 llama-server 缺 `libllama-server-impl.so`（**交叉编译产物一开始没有 `$ORIGIN` rpath**）也被判成"解析得到"—— 是 chroot 冒烟那句 `cannot open shared object file` 暴露的；修法是检查器照 ld.so 真实语义（不搜自身目录）+ `build-llama.sh` 加 `$ORIGIN` RPATH（并让构建机绝对路径不漏进产物）+ `start.sh` 加 `LD_LIBRARY_PATH` 兜底。<br>⑤ 途中修掉三个"不修就编不出来/装不上"的问题：厂商 MPP 快照缺 `build/cmake/merge_objects.cmake`（补完还要删已 rsync 的构建目录）、厂商设的 GNU 镜像 USTC **403**（换清华）、**本树合并器的 `+=` 是替换不是追加**（差点丢掉厂商 overlay，改成写全量值）；另有一处注入路径写错一级（`board/` vs `buildroot/board/`）也由验收前的检查抓到。 |
| 2-8 ✅ | 只起我们的东西（`assistant.target` + agent + 无 X 的 GUI 单元） | 见 `docs/image.md` §5.7。**`default.target -> assistant.target`**（自己当开机目标，而不是往 multi-user 上挂 —— 挂它只能保证我们的起来、保证不了别人的没起来）；`assistant.target` 只带 `basic.target`（摘掉 graphical 之后必须自己接上系统d 基座）+ 我们的三个单元。<br>单元 4 个（`systemd/image/`）：`assistant.target` / `assistant-init.service`（首启用 `cp -n` 铺 userdata 的目录与默认配置**不软链**）/ `agent.service`（D7 环境变量：`AGENT_CONFIG_DIR`/`AGENT_LOG`/`AGENT_CRASH_DIR`/`LLM_ENV_FILE`/`LLM_STATE_DIR`）/ `agent-gui.service`（EGLFS + 虚拟键盘，镜像里不带 `-nox` 后缀）。<br>⚠ **NetworkManager 是唯一非我们的直接依赖**：实测 buildroot 的钩子只建 dbus 别名、`.wants` 一个没有、preset 是空的（vendor 靠 multi-user.target.wants 启它，而我们不进 multi-user 了），而 `network-online.target` 自己拉不起任何东西 → 必须显式 `Wants=`，并把 `NetworkManager-wait-online.service` 挂进 `network-online.target.wants/`。串口 CLI 不用 enable（`systemd-getty-generator` 按 cmdline 自动生成）。<br>验收 `image/check-assistant-target.py` **两种模式**（仓库/镜像）**通过**：`default.target -> assistant.target`、`.wants` 与闭包 = 我们的 4 个 + NetworkManager(2)，`graphical/display-manager/weston/slim/xorg/x11/bluetooth` 出现即失败。另加 `tests/test_image_target.py`（5 项）守住"板端单元与镜像单元除差异表外必须一致"。板上真跑 `list-dependencies` 留给 2-11 |
| 2-9 ✅ | 首次完整构建（不刷板） | 见 `docs/image.md` §5.8。入口 `wsl -u root bash image/build-image.sh <SDK>`（清洗 PATH → `python` 垫片 → 时钟看门狗 → `./build.sh <chip>:<defconfig>` lunch → `./build.sh all`），日志尾 `Running 99-all.sh - build_all succeeded`，会话 `output/sessions/2026-09-29_20-27-14/`。<br>**① 时长**：累计 **约 1 小时 40 分钟** —— 前一段 74 分钟（含**从零编交叉工具链**与 Qt）被用户要求的停机点打断，续跑增量 26 分钟收尾（第二次进入时 178 个包目录都还在）。<br>**② 体积**（`output/firmware/` 1.3 GB）：`update-…-buildroot-2026092920.img` **747,516,490 B（≈713 MiB）**、`rootfs.img` **679,477,248 B（≈648 MiB）**（进 `system_a/b` 各 3 GiB 槽，余量足）、`boot.img` 41,900,544 B、`oem.img` 12,582,912 B、`userdata.img` 8,388,608 B、`uboot.img` 4,194,304 B、`MiniLoaderAll.bin` 481,728 B、`parameter.txt` 579 B（**就是我们那份 `parameter-assistant-ab.txt`**）、`BR/target` 522 MB。<br>**③ 版本**：kernel **6.1.141**、u-boot **2017.09**、buildroot 2024.02（自建工具链 GCC **13.4.0**/glibc）、Qt **5.15.11**、python **3.11.8**、systemd 254.9、GStreamer 1.24.13、llama.cpp **b387ddfd8**、rknnlite 2.3.2、MPP 1.0.11、librknnrt 7,726,120 B、libmali 56,387,136 B。<br>**产物核对**：`default.target -> assistant.target`、`assistant.target.wants/{agent,agent-gui,assistant-init}.service`、四个单元在 `/usr/lib/systemd/system/`、`usr/lib/assistant/llm/bin/llama-server` 与 `llm/scripts/start.sh` 全在位；**没有 `recovery.img`**。<br>**六个坑**（前四落脚本、后两落配方）：① PATH 带 Windows 条目 ② `check-kernel.sh` 写死要 `python`（放垫片，不动系统）③ 宿主缺 `gettext`（`Your msgmerge is missing`）④ WSL `systemd-timesyncd` active 但没同步 → 步进调时钟 → `Clock skew detected … in the future`（已 mask + 看门狗）⑤ **厂商 `libxcrypt.mk` 缺 host 变体** → systemd 要 `host-libxcrypt` → `No rule to make target 'host-libxcrypt'`（仓库覆盖 `.mk`，补 `HOST_LIBXCRYPT_CONF_OPTS` + `$(eval $(host-autotools-package))`；target 侧本来就好的）⑥ **`RK_AB_UPDATE=y` 让 recovery 段算出 `rockchip_rk3566_rk3568_recovery`，SDK 里只有单芯片名** → `Can't find …_defconfig`；我们的分区表本来就没有 recovery 槽（回退靠 A/B + `misc`）→ 板级 defconfig 关掉，实测 `final.env` 里无 `RK_RECOVERY`、`build_all` 整段跳过、会话日志 0 次提到 recovery。<br>⚠ 两条 output 路径（§5.8 表）：单包构建在 `…/rockchip_rk3568_kickpi_k1mini_release/`，整机构建在**它下面再套一层**同名目录（另一棵全新树，`dl/` 共享）。未做：刷板（2-11）、模型投放 `/data/model`、dev 镜像（2-12）。<br>⚠ **刷板路径的坑（2-11 必踩）**：`output/firmware/` 里全是软链，且 AB 形态下 **`update.img -> update-ab.img` 是悬空的**（这个名字没人创建）；真镜像是 `$SDK/output/update-ab/Image/update.img`（747,516,490 B），或那条带版本号的软链。`firmware/parameter.txt` 已 diff 确认逐字节等于我们的 `parameter-assistant-ab.txt`。属主：`buildroot/output` 树下约 42.5 万个条目是 root（整机构建以 root 跑）→ 2-10 的 chroot 用 root 跑最省事，或以 anorak 跑前先 `chown -R` |
| 2-10 ✅ | 刷板前验证（chroot 跑 ctest + 仓库 Python 套件 + ldd/符号检查） | 见 `docs/image.md` §5.9。入口 `wsl -u root bash image/preflash-check.sh <SDK>`（WSL 的 `binfmt_misc` 里有 `qemu-aarch64`，aarch64 的 python3 能直接 chroot 真跑）。**结果**：逐项在位 **31 项缺 0**、DT_NEEDED 闭包 **2496 个 ELF 缺 0**、chroot 冒烟 **9/9**（`Python 3.11.8`/`numpy+cv2`/`rknnlite ok`/`bash`/`curl 8.6.0`/`nmcli 1.44.2`/`mppvideodec`/思源黑体/`llama-server`）、开机目标闭包 = 我们的 4 个 + NetworkManager(2) + `wifibt-init`。<br>**① 最大的收获是"前面几步验的是另一棵树"**：检查器写死单包构建那棵（§5.8 的两条路径），于是整机镜像里**根本没有 rknnlite**（`prepare-rknnlite.sh` 解到了单包树的 site-packages，还因旧戳打印"已经一致"）而构建全绿 —— 取证在会话日志 1081 行。修法：新增 `image/imagelib.py` 把选树收一处（`--target` > `IMG_TARGET` > 整机构建树 > 单包树，并打印选中哪棵）、两个检查器都支持 `--target`、post-build 传 `$TARGET_DIR`、`prepare-rknnlite.sh` 改从 target 树自己看 python 版本且装错树时大声警告。<br>**② 启动链里还有两个厂商服务**（2-8 那次在单包树上没看见）：`wifibt-init.service`（`sysinit.target.wants`，加载 rtl8822cs 固件）**必须留**→ 进白名单并写明理由；`usb-gadget.service`（`local-fs.target.wants`，`Type=simple` 常驻）**发行镜像已 mask**（`post-build.sh` 里 `ln -sfn /dev/null`，开发镜像可 `ASSISTANT_FLAVOR=dev` 保留）。检查器把 mask 当真语义（磁盘上真有 `/dev/null` 才算），且"策略要求 mask 的必须真被 mask"。<br>**③ 厂商自己的 bug**：`/etc/systemd/system/` 里有个目录名带空格的 `local-fs.target graphical.target.wants` → systemd 忽略 → 厂商以为 enable 了的 `async-commit.service` 从来没跑过（只记录）；`multi-user.target.wants` 里那几个（dhcpcd/input-event-daemon/irqbalance/log-guardian）我们根本不进 multi-user，不会被拉起。<br>**④ payload 9/9 未做**（agent 包 / gui 二进制 / `agent_native`（仓库现有的是 cp38，镜像 python 3.11 **必须重编**）/ 两个 example.yaml / 三个 `Documentation=` 指向的文件）→ **assistant.target 起得来但 agent/gui 会一直重启**，刷板前必须补（排在 2-11 之前，见下面的 2-10b）。<br>**只能板上测的**：DRM/KMS 真出图与旋转、触摸坐标、wlan0 真联网、RTC、NPU 真推理、硬解真解码、温度/功耗/启动耗时、A/B 与 OTA 回滚 |
| 2-10b | **payload 装进 rootfs**（2-10 发现的缺口；方案 A = 全部交叉编译，2026-09-29 已批准） | 出口：`preflash-check.sh` 退出码 0（payload 0/9 未做）+ 镜像重新打包。七个步骤见下（2-10b-1…7），每个完成后等你验收 |
| 2-10b-1 ✅ | payload 源清单 + `image/build-payload.sh` 骨架（`--dry-run`，不装任何东西） | 源→落点映射与检查器 `PAYLOAD` 表**机器互校**（`tests/test_image_payload.py` 13 项：清单↔检查器↔unit 三方，改一边忘另一边当场红）；`--dry-run` 三种走法实测：copy+gen → RC 0、全量 → RC 1 且明说"半成品"、不给 `--target` → RC 2 **拒绝猜树** |
| 2-10b-2 ✅ | python 侧进镜像（`agent/`、两个 `example.yaml`、`Readme.md`/`docs/*.md`、`/usr/bin/assistant` 包装、`/etc/profile.d/assistant.sh`） | 见 `docs/image.md` §5.10。装进整机 target：agent **66 个 .py / 0 个 pyc**、幂等（二次跑 7 跳过）、戳在 rootfs 之外、目录权限归一 0755/0644（DrvFS 会带 777）。<br>**① 抓到致命缺件**：镜像 python3 **没有 `_ssl`** → `import agent.cli` 在 `agent/net/sunshine_client.py:57` 断掉（`agent/core/__init__` 连锁 import）⇒ 板上 agent.service 会一直崩溃重启。厂商基座把 python3 扩展模块全关着 → 在 buildroot defconfig 末尾开 `PYTHON3_SSL`（带出 `_ssl`+`_hashlib`）与 `PYTHON3_READLINE`。修后 chroot：`ssl ok: OpenSSL 3.2.1 | hashlib ok`、**`agent import ok`**。<br>**② 两个构建机制坑**：vendor 的 `./build.sh <defconfig>`（lunch）**不重新生成** buildroot 的 `.config`（要用 `make O=... <cfg>_defconfig`）；buildroot 改了 `.config` **不会自动重建包**（`python3-rebuild` 又不重跑 configure，得 `python3-dirclean` 全重编）。<br>**③ 差点误判**：在配置注释里写了符号全名 → 合并器把**注释行**当成该符号的一次赋值（日志 `Previous value: # · <符号> …`），于是开关怎么改都不生效。已把这条硬纪律写成守卫（注释里一个符号全名都不许有） |
| 2-10b-3 ✅ | 修 `native/CMakeLists.txt` 的 python3.8 硬编码 + 交叉编译 cp311 扩展 | `image/build-native.sh`（`--target` 必给；从 target 反推 SDK；只认 ELF 机器类型**不认文件名**）。实测产物 **Machine: AArch64**、`NEEDED` 里 `libmoonlight-common-c.so` 与 `librockchip_mpp.so.1` 都在、chroot 里 `import agent_native` **RC=0** 且导出 `BUTTON_*/MODIFIER_*/FRAME_*`（mpp 那几行是 chroot 无设备树的正常噪声）。<br>三处板端假设被改掉：① python 头从 **sysroot 实际版本**探测（原写死 3.8，镜像里是 3.11）；② MPP 认**两种布局**（Ubuntu 的 `usr/lib/aarch64-linux-gnu` 与 buildroot 的扁平 `usr/lib`）；③ FFmpeg 从"缺就 FATAL"改成"缺就跳过"（**镜像里根本没有 FFmpeg**，板端有 dev 时行为不变）。<br>⚠ 自己踩的坑：pybind11 的 `EXT_SUFFIX` 按**宿主机**解释器算，交叉产物竟叫 `…cpython-311-x86_64…so`；第一版只校验名字（匹配 cp311 就放行）→ 差点把一个 x86_64 的 .so 装进 aarch64 镜像。现在判据是 `readelf -h` 的 Machine |
| 2-10b-4 ✅ | GUI 交叉编译（Qt5 Widgets+Multimedia、yaml-cpp、moonlight） | `image/build-gui.sh`（同 build-native 的纪律：`--target` 必给、**判据是 ELF 的 Machine=AArch64**、Release 后 strip）。**一次编过**：1,084,712 B → strip **821,368 B**；`NEEDED` 里 `libQt5{Widgets,Multimedia,MultimediaWidgets,Network,Gui,Core}` + `libyaml-cpp.so.0.8` **全在镜像里**（闭包 0 缺）；chroot 里 `agent_gui --help` RC=0、`--stdio` 能起来（真出图只能板上测）。前置条件都验过：`toolchainfile.cmake`、host `moc/rcc/uic`、sysroot 的 Qt5 cmake 配置与 `yaml-cpp` |
| 2-10b-5 ✅ | 串进 `post-build.sh`（`$TARGET_DIR`）+ 幂等内容哈希戳 + `ASSISTANT_FLAVOR` 预留 | `post-build.sh` 在 llama → rknnlite 之后调 `build-payload.sh --target "$TARGET_DIR" --src-root <SDK>/tools/assistant/payload-src`；`install-into-sdk.sh` 新增把 **agent/ + native/ + gui/ + 两个 shell 模板 + 清单** rsync 进 `payload-src/`（排除 `__pycache__`/`build*`/googletest/gui tests）。**验证方式是"先删后建"**：把 target 里的 payload 全删掉再整机构建，post-build 自己装回来了 ✓。`ASSISTANT_FLAVOR=dev` 的开关在 post-build 里（决定是否 mask usb-gadget） |
| 2-10b-6 ✅ | 重新打包镜像（`./build.sh all` 增量）+ 全量验证 | `preflight-check.sh` **退出码 0、payload 0/12**；`update-ab` **759,050,826 B**（比 2-9 的 747,516,490 多 11.5 MB：agent 1.6M + gui 0.8M + native/moonlight + python3 的 ssl/readline 模块）；target 531 MB；chroot 里 `agent.cli + agent.main + agent_native + ssl + cv2 + numpy + yaml + psutil + rknnlite` **全部 import ok**。<br>途中三个坑：① 清单里 `gen` 的源是仓库相对路径，SDK 里在 `tools/assistant/payload/` → 按四个候选位置找；② 清单有**两条** `native`（扩展 + moonlight），两条都跑会把落点覆盖成扩展本身（`undefined symbol: LiStartConnection`）→ 同一 `how` 只跑一次、其余只核对；③ 取 moonlight 库不能再 `find … -name | head -1`（抓到过同名错文件）→ 只从确定路径取**且**校验 `T LiStartConnection` |
| 2-10b-7 🔄 | 提交 + push + CI 绿 + 交你验收 | 本轮提交中；通过后进 2-11 首次刷板 |
| 2-11 🔄 | 首次刷板 + 板端基线（复跑 1b/1c/1d + T14 验收） | **准备完成，执行等你**（2026-09-29 22:38 你定：**下次专门做**、走**路线 B**=USB loader + Windows `RKDevTool`、**手上有厂商 Ubuntu 出厂镜像（可回退）**）。手册：`image/FLASH-RUNBOOK.md`（含 3 个你要做的手动动作、镜像 sha256、首次启动清单、板端基线要采的数字）。<br>**已就位**：镜像 `E:\rk3568\flash\update-assistant-20260929.img`（759,050,826 B，sha256 `cb02622b…0119` **已在 Windows 侧核对**）、raw 镜像与分区表、`RKDevTool_v3.37`、`DriverAssitant_v5.13`（厂商拼写少个 s）、命令行 `upgrade_tool_v2.46`、SD 卡备选 `SDDiskTool`。<br>**已备份**：板上 `config/`+`llm/`（`temp/board-state-20260929.tgz`，`config.yaml` md5 `5212393aea2147a34fa9a2b734683027` 不变）+ **板上 4.9 GB 模型**（`E:\rk3568\board-model-backup\model\`，18 文件 / 4.86 GB）。<br>**注意**：板上现在是厂商分区（`mmcblk0p1..p6`，28.9 G 的 `/`），刷机后**全部重排**；WSL 不能直通 USB，别在 WSL 里试路线 B；刷完首启要先 `mkfs.ext4 /dev/disk/by-partlabel/userdata` 并把模型投放到 `/data/model`。<br>出口（下次做完）：板子起得来 + `assistant.target` 自动拉起 + 1b/1c/1d/T14 复跑全绿 + 上面的数字齐备 |
| 2-12 | 开发镜像（同套 + sshd/调试工具/pytest/串口控制台） | `ssh` 能进、能在板上跑 pytest 与验收脚本 |

⚠ 构建两条风险先记在这：**WSL 只有 7 GB 内存**（并行构建封顶 `-j8` + ccache）、**Qt 源码 dl 里没有**（联网或预拉镜像）。

### T15-3 代码规范审计（重复造轮子 / 死代码 / 规范）
- ① **同一功能多处实现**：日程解析、config 读取/写入、CLI 与 GUI 各一套的路径解析、`agent/net/*` 与 `gui/src/*` 重叠逻辑；
- ② **死代码/退役残留**（T14 已清一批：`config_sync`、`next_wallpaper`…）；
- ③ 硬编码与魔法数（阈值/超时/周期/大小）统一进常量或配置；
- 出口：`docs/audit-code.md` + 一张"重复实现 → 收敛到哪一份"的表 + 收敛后的守卫测试。

### T15-4 可变参数引出（配置 → GUI/CLI）
- 做法：用脚本**机械枚举**"代码里可调但没进 `config.example.yaml`"的参数（超时、重试、周期、阈值、批大小、缓冲、音量、帧率、水位…），逐个决定：进 config / 只在 CLI / 保持内部；
- 出口：`config.example.yaml` 补齐 + GUI 设置页/CLI 能改（并遵守 T14 单写者：GUI 只发 `set_config`）+ `docs/config-sources.md` 同步。

### T15-5 CPU 调度优化
- 现状：4 核 @2.0 GHz governor=interactive（三处抢：`cpufrequtils`/`loadcpufreq`/内核默认）；
- 方向：governor 单点化（`schedutil`）；**进程/线程优先级**（GUI 渲染与输入优先、NPU/SigLIP 推理与 LLM 降级）；CPU 亲和把"重推理"与"GUI 主线程"分开；中断亲和；避免忙等/轮询过密（`monitor.sh` 里可见的利用率）；
- 出口：空闲 CPU%、交互延迟（点按到反馈）、推理 p50/p95 三项数据齐备且无回退。

### T15-6 内存：长期影响 / 碎片 / 压缩
- ① **长跑观察**：连续 6–24 h 采样 RSS（llama-server / Agent / GUI）判断有没有增长与台阶；
- ② **碎片**：`/proc/buddyinfo`、`slabtop`、malloc 行为（glibc arena 数 `MALLOC_ARENA_MAX`）；Python 侧对象池与 `cv2/numpy` 大缓冲复用；
- ③ **压缩/上限**：zram（zstd）1 G、`MemoryMax` 给三个单元、llama 量化/mmaps；
- 出口：长跑曲线图 + `docs/memory.md`（含"要不要 zram、给谁多大上限"的结论）。

### T15-7 睡眠模式与功耗
- 现状：SLEEP 模式只切状态，**没有真正降功耗**（llama-server 1.2 G 常驻、WiFi 常在、屏幕常亮）；
- 方向：进入 SLEEP 时（背光/Panel 关或降到最低、`wifi.powersave` 打开、llama-server 停、NPU/GPU devfreq 降、CPU idle 深睡、GUI 停止重绘/降帧）；唤醒路径要快；
- 出口：`docs/power.md` + 实测（温度/电流若有表、或至少 CPU 占用与唤醒延迟）+ "关屏能否被触摸唤醒"的验证。

### T15-8 视频编解码调优工具
- 现状：`mppvideodec` + 我们自己的缓冲策略（`bilibili_buffer`）；
- 方向：对照测试（硬解 vs 软解、不同码率/清晰度/缓冲窗口）、GStreamer pipeline 调优（`kmssink`/`glimagesink`、零拷贝）、把结论固化进 `scripts/bench.sh`；
- 出口：一张"参数 → 帧率/CPU/延迟"的表 + bench 新增一段。

### T15-9 以太网 / WiFi 分别测试与自动切换
- 现状：eth0/eth1 `unavailable`（驱动/PHY 没起），只有 wlan0；
- 方向：先把**以太网**弄活（驱动/PHY/DTB/NM 配置）→ 两条链路的独立测试（拔插/断开/恢复）→ **自动切换**（NM 路由度量 + 我们 LinkGuard 的链路健康，优先级与回切策略）；
- 出口：`docs/net.md` 增补"双链路优先级与切换" + 板端拔插实验记录。

### T15-10 GUI 页面美化
- 方向：视觉规范（间距/字号/圆角/色板统一进 QSS）、图标一致性、状态动画克制、弱网/无 Agent 等**异常态**的视觉、触摸目标尺寸 ≥ 44 px；
- 出口：改前改后截图对照（首页/模型/系统/设置四页）+ `docs/gui.md` 视觉小节。

### T15-11 开发镜像 vs 发行镜像
- 开发镜像：保留 g++/cmake/git/pytest/ssh（能改代码、跑验收）；
- 发行镜像：不含编译工具链与测试依赖，最小依赖集，只读根候选；
- 出口：两条线的**构建脚本 + 差异清单**（`scripts/image.sh --flavor dev|release`）+ 各自验收。

### T15-12 最小 rootfs + 唯一启动目标
- `default.target → assistant.target`（只 Wants：`assistant-agent`、`assistant-gui`、`NetworkManager`、`sshd`）；
- 桌面栈 0、常驻服务 ≤ 12、启动目标 **< 6 s**；
- 出口：全新镜像跑完功能回归 + `docs/image.md`（含"哪些服务、为什么"）。

### T15-13 旋转的"无损"实现
- 现状：xrandr（X 专属）→ 无 X 后要换；"无损"= 不二次缩放（面板原生 800×1280 → 逻辑 1280×800 只做 90° 旋转，不做 scale）；
- 候选：内核 cmdline `video=DSI-1:800x1280@60,rotate=90` / DTB panel 方向 / `QT_QPA_EGLFS_ROTATION`；
- 出口：逐像素验证（不糊、不缩）、触摸坐标一致、开机到首帧不闪。

### T15-14 烧写 / 升级 / 无线 OTA
- 烧写：`upgrade_tool`（板端已有）+ SD 卡启动路径；镜像产出 `.img` + SHA256；
- 升级：**A/B 分区**（推荐）或 recovery + 校验 + **失败自动回滚**；
- 无线 OTA：走 GitHub Release（我们已有 `release.yml`）或自建通道；**签名校验**；断网续传；
- 出口：一次完整演练（升上去 → 故意损坏 → 自动回滚）。

### T15-15 文档收口
- `docs/image.md`、`docs/power.md`、`docs/audit-code.md`、`docs/memory.md` + `docs/net.md`/`gui.md` 增补 + 本文件的完成记录。

---

## 3. 需要你定的事（这些不定，任务会返工）

| # | 问题 | 影响 |
| --- | --- | --- |
| 1 | 厂商 SDK（`rk356x_linux_sdk`）与 Buildroot/Yocto 覆盖层你手上有哪些？版本/路径 | T15-2 直接决定路线 B 的可行性 |
| 2 | `mppvideodec` 等 Rockchip gst 插件、libmali gbm、rknpu2 从哪来 | 同上（最大风险项） |
| 3 | 开发/发行镜像是否都要（T15-11） | 决定构建脚本的复杂度 |
| 4 | OTA 通道：GitHub Release / 自建服务器 / 只做本地升级 | 决定 T15-14 的形态 |
| 5 | 睡眠时"关屏"是否可接受（触摸唤醒 vs 按键唤醒） | 决定 T15-7 能做到多省电 |

---

## 4. 我的建议起点

**T15-1 切片 1a（EGLFS + 旋转）**先做完 —— 它是形态的锚点，后面的 CPU/内存/功耗/镜像全部依赖它。
做完 1a 我会把"无 X 下的实测数字（内存/CPU/启动）"给你，再一起决定 1b/1c 的优先级（键盘与视频）。
