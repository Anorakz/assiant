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
| T15-1 待办 | 1b 软键盘换 Qt VirtualKeyboard（onboard 是 X11 的）；1c 视频改 GStreamer+kmssink（`QMediaPlayer` 在无 X 下未验）；1a 的**永久**旋转方案进 T15-13（cmdline/DTB vs EGLFS 旋转的开销对比） | — |

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
| **1a** | 平台选择与旋转：`QT_QPA_PLATFORM=eglfs`（回退 linuxfb）；旋转从 xrandr 改到 **EGLFS/内核 cmdline**；GUI 在无 X 下能起来且**方向正确、1280×800 满屏** | 板端无 X 下 `--screenshot` 出的图与有 X 时布局一致（逐图对照） |
| **1b** | 软键盘替换：`onboard`（GTK/X11）→ **Qt VirtualKeyboard** 或自绘 | 无 X 下点输入框能弹出、能打字、不挡布局 |
| **1c** | 视频：`QMediaPlayer` → **GStreamer + kmssink**（E2b 已验证可行） | 无 X 下 B 站视频硬解满帧播放、音画同步、暂停/进度可控 |
| 1d | 回归：无 X 形态下跑完整功能回归清单 | 全绿 + `docs/gui.md` 更新（含新环境变量） |

### T15-2 SDK 组件探测 → 最小镜像可行性配方
- 探测：厂商 SDK（`rk356x_linux_sdk`）在不在、Buildroot/Yocto 版本、覆盖层里有什么；
- **逐项确认来源**：`qt5base(+eglfs,widgets,svg)`、`libmali` 的 **gbm flavor**、**`mppvideodec`（Rockchip gst 插件）**、`rknpu2(librknnrt.so)`、`python3 + numpy/cv2`、`yaml-cpp`、`NetworkManager|wpa_supplicant`、`sshd`、字体/时区；
- 出口：一份**能编出最小 rootfs 的配方**（包清单 + 每个包从哪来 + 谁负责交叉编译），并给出体积/启动的**预估**。

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
