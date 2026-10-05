# 板端助手 GUI（Qt5 Widgets）说明

> 本文对应 `gui/` 目录的实现；方案与任务分解见 `docs/gui-qt5-plan.md`、`docs/gui-qt5-tasks.md`。
> 协议（GUI ⇄ Agent）以 `docs/ipc-protocol.md` 为唯一真源。

## 1. 跑起来

配置只有一份 —— 仓库根目录的 `config/config.yaml`（模板见
[`config/config.example.yaml`](../config/config.example.yaml)，约定见
[`config-sources.md`](config-sources.md)）。GUI **没有**自己的配置文件，启动前不需要
复制任何 GUI 专用模板；只有当 `config/config.yaml` 还不存在时才要
`cp config/config.example.yaml config/config.yaml`。

```bash
# 宿主依赖（T15-4 任务 12 实测）：Qt5 5.15 的 Core/Network/Widgets/Multimedia/MultimediaWidgets
#   + yaml-cpp（日程区读配置用，只读）。Ubuntu 一条命令：
sudo apt-get install -y qtmultimedia5-dev libyaml-cpp-dev
# ⚠ 镜像里是 **Qt 5.15.11**（buildroot 那份），不是 5.12 —— 见 §1.0 的门禁表

cd gui && cmake -S . -B build && cmake --build build -j4
./build/agent_gui                       # 自动向上找 config/config.yaml，全屏 kiosk
./build/agent_gui --config /tmp/g/config.yaml   # 显式指定配置（验收/沙箱用；⚠ 只影响**读**，
                                                #   保存走 IPC，落盘路径由 Agent 侧决定）
./build/agent_gui --windowed --page system
./build/agent_gui --socket /tmp/agent.sock
./build/agent_gui --stdio               # 纯终端模式（e2e_ipc 测试依赖）
```

常用验收参数：`--screenshot <png> --screenshot-delay <ms>`、`--video <文件>`、
`--chat-demo <文本>`、`--input-type-demo <terminal|keyboard>`、`--focus-input-demo`
（取证：软键盘应在这时才弹）、`--model-mode-demo <mode>`、
`--bench-demo <qwen_precheck|qwen_full|multimodal>`、`--report-demo`、`--settings-save-demo`、
`--dump-schedule`（打印日程区真实渲染的行）、`--dump-layout`（打印整棵控件树的
`size/min/hint`，用来查"窗口为什么不是一屏"这类问题，见 §2.1）等。

测试：`cd gui/build && ctest --output-on-failure`（**28 个测试**：核心逻辑 + 控件级 + 图标守卫
+ 面板尺寸守卫 `test_page_heights` + 虚拟键盘让位守卫 `test_keyboard_inset`
+ **视觉常量黄金串守卫 `test_theme`** + **一致性守卫 `test_style_guard`**（G-A-1b/G-A-2 ✓）+ e2e IPC）。

> ⚠ **动样式、加控件之前先读** [`docs/gui-style.md`](gui-style.md) ✓ —— 那里有三节约束：
> **主题**（色值/字号的唯一来源是 `ui/theme.h` ✓、QSS 用 `@token@` ✓、判据是黄金串而**不是截图** ✗）、
> **触摸目标**（强制 ≥44 px ✓ + 例外表 ✓）、**异常态**（现状 10 处就地文案 ✗ + G-D 的整改方向 ✓）。

### 1.0 GUI 的门禁到底在哪（T15-4 任务 12 实测改正）

旧版这里写的是"GUI 只能在**板端**编（PC 没有 Qt）"—— **这条是错的**，2026-10-04 两条都实测过：

| 路 | 命令 | 实测 |
| --- | --- | --- |
| **A. 宿主原生**（快，日常用这条） | `apt-get install qtmultimedia5-dev` 后 `cmake -S gui -B build-gui-host -DGUI_BUILD_TESTS=ON && ctest --test-dir build-gui-host` | 宿主 Qt **5.15.13**（与镜像的 5.15.11 同小版本）：配置 + 编译 rc=0，**28/28 全过**（15 s） |
| **B. PC 交叉 + 板端跑**（发布前的真门禁） | `bash image/build-gui.sh --target <target 树>`（它固定 `GUI_BUILD_TESTS=OFF`）；要跑测试就照抄它的 cmake 参数、把 `-DGUI_BUILD_TESTS=ON`，再把 `tests/test_*` 拷到板上执行 | 板端（aarch64，Qt 5.15.11 运行时 + `libQt5Test` + `libqoffscreen.so`）：**24 个测试二进制、278 项通过**（明细与两处板端环境差异见 §1.0.1）<br>⚠ 这是 **2026-10-0x 的历史测量** ✗ —— G-A-1b/G-A-2 之后新增的 `test_theme`、`test_style_guard` **还没在板上跑过** ✗（宿主 28 项全绿 ✓ 是当前口径 ✓）|

⚠ **板子上编不了 C++**：这块 buildroot 镜像里**没有 `g++`/`cc1plus`、也没有 `cmake`**
（只有 `make`/`gcc`(C)/`ctest`）—— 所以"在板上 cmake + ctest"这条路不存在；
`scripts/sync-gui.ps1 -Test` 的头部假设是**另一块**装了 Ubuntu Qt 5.12 + cmake/g++ 的开发板，
对这块板不适用。交付物一律 **PC 侧交叉编译**（与 `docs/image.md` §5.10 的"方案 A"一致）。

**测试里写死的绝对路径**：`test_settings_page`（仓库那份 `config/config.example.yaml`）、
`test_icons`（`gui/resources/icons`）、`test_schedule_model`（`tests/data/schedule_parity`）、
`test_local_client`（`gui/tests/local_server.py` + `agent/` 包）都是**编译期**写死的路径。
在另一台机器上跑这些二进制时，要么把这几样按同样的绝对路径铺好，要么把测试当"同机门禁"用。

#### 1.0.1 板端跑出来的两处环境差异（与本次改动无关）

| 测试 | 现象 | 判定 |
| --- | --- | --- |
| `test_bench_runner` | 2/6 失败：假 bench 脚本的输出没出现在日志视图里（`[fake-qwen] 预检完成` / `长跑开始`） | 宿主上同一份二进制全过；这个测试不碰设置页，与环境（QProcess + 板端 python）有关 |
| `e2e_ipc` | C++ GUI 日志为空、断言走不下去（Python 侧 `LISTENING`/`CONNECTED` 正常） | **旧二进制逐字一样**（用 `.pre-t154b` 对照跑过）⇒ 板上环境差异，不是回归 |

"源码树自洽"另有一条 PC 侧守卫 `tests/test_gui_includes.py`（悬空 include 见 §2.1）。

### 1.1 无 X 形态（路线 B：极小镜像，T15-1）

板端实测（2026-09-28）：**没有 X 也能跑**，而且桌面栈那 ~703 MB 全省下。要跑起来只需要
把"显示/输入/软键盘"三件事都交给 Qt 平台插件：

```bash
# 1) 平台：EGLFS + KMS（linuxfb **不行**：它转不了屏，见下）
# 2) 旋转：EGLFS 自己的环境变量（xrandr 那条路随 X 一起消失；"无损"旋转属 T15-13）
# 3) 软键盘：Qt 虚拟键盘（onboard 是 GTK/X11 的，没有 X 就不存在）
# 4) 输入：不用额外配置 —— libinput 自己会认触摸屏/鼠标/键盘
export QT_QPA_PLATFORM=eglfs
export QT_QPA_EGLFS_INTEGRATION=eglfs_kms
export QT_QPA_EGLFS_ROTATION=90
export QT_QUICK_BACKEND=software          # 虚拟键盘是 Quick 窗口，无 GL 时走软渲染
export QT_IM_MODULE=qtvirtualkeyboard
export GST_PLUGIN_FEATURE_RANK=souphttpsrc:0   # 板端 souphttpsrc 是坏的（B 站取流走别处）

sudo systemctl stop slim.service          # 让出 DRM（X 在跑时 EGLFS 拿不到 master）
./gui/build/agent_gui --config config/config.yaml
```

**虚拟键盘的 QML 依赖（必须一起装，否则"白板"）** —— `qtvirtualkeyboard-plugin` 的依赖里
**没有** QML 运行时模块，只装它的话面板窗口会起来、`IM_VISIBLE=1`，但一个键都画不出来：

```bash
sudo apt-get install -y qml-module-qtquick-virtualkeyboard \
                        qml-module-qtquick2 qml-module-qtquick-window2 \
                        qml-module-qtquick-layouts qml-module-qt-labs-folderlistmodel
```

**软键盘与布局（T15-1 1b）**：Qt 的 `DesktopInputPanel` 是"整屏（半透明）窗口 + 键盘贴窗口
底部"，板端实测键盘矩形 **`0,400 1280x400`**（默认样式：高 = 屏宽 × 800/2560）。我们原来的
对话输入行在 `y=496..544` —— **正好被压在键盘底下**。现在 `MainPage::setKeyboardInset()`
在键盘可见时把模式卡/日程卡收起来（高度上限压 0 + 隐藏）、把对话卡高度压到键盘上沿以内
（输入行在卡片底部，于是被顶到键盘上方），收起键盘全部复原。三条不变式钉在
`gui/tests/test_keyboard_inset.cpp` 里：**别用布局下边距让位**（会把布局最小高度撑大：
窗口 800→1063）、隐藏的控件在布局里仍占地方、压了上限还得加一根尾部弹簧才贴列顶。

**常驻单元**：X 形态 = `systemd/agent-gui.service`（里面写着
`Requires=display-manager.service`，**只加 drop-in 改平台会把 X 一起拉起来**，实测
`pgrep -c Xorg` 0→1，两个显示栈抢 DRM master）；无 X 形态 = `systemd/agent-gui-nox.service`
⚠ 上面这两份是**原型**（在 `systemd/` 根目录 ✓）；**镜像里跑的是另一套** ✗：
`systemd/image/` 下 5 个单元（`agent` / `agent-gui` / `assistant-init` / `assistant-ota-confirm` / `ab-mark`，
被 `assistant.target` 拉起 ✓，清单见 `docs/image.md` §5.7）——
**改行为时要改镜像那一套** ✓（`systemd/image/`），根目录那套只在 PC 侧的脚本/实验里用 ✓。
2026-10-05 复核：两处文件都还在 ✓（`systemd/{agent,agent-gui,agent-gui-nox,xrandr-startup}.service`
与 `systemd/image/` 的 5 个 ✓）。
（原型，T15-12 定稿）。

**取证脚本（板端）**
```bash
python3 tests/board/t15_1b_keyboard_nox.py     # 无 X 软键盘：像素 + 产品形态 + uinput 打字 + vnc 合成
python3 tests/board/t15_1d_regression_nox.py   # 无 X 全量回归 + 空闲 CPU/RSS（临时顶替 systemd 单元）
python3 tests/board/t15_1c_video_nox.py        # 无 X 视频：QMediaPlayer 播本机 MPEG-TS 流
```

**平台陷阱（都是实测，写下来免得再踩）**
| 事 | 结论 |
| --- | --- |
| `linuxfb:rotation=90` | **不生效** —— `rotation/invertx/inverty` 是 **evdevtouch** 的参数，不是 linuxfb 屏幕的；`/sys/class/graphics/fb0/rotate` 写进去 `virtual_size` 也不变。linuxfb 下 app 被挤成最小尺寸 935×663 且右边被裁 → 它**只能当像素证据**，不能当产品形态 |
| linuxfb / vnc 的 alpha | 都没有合成：虚拟键盘那个"半透明整屏窗口"在这两个平台上变成**不透明白底**（fb 上半屏颜色只有 1 种）→ 产品必须用 EGLFS（窗口合成器带 alpha 混合） |
| `QScreen::grabWindow(0)` @EGLFS | **抓不到东西**（返回一张 800×800 空白）→ 想抓"键盘+界面"合成图就用 `QT_QPA_PLATFORM=vnc:size=1280x800:depth=32:port=NNNN`，再自己走 RFB 协议取一帧（Qt 的 VNC 平台只说 **RFB 003.003**） |
| linuxfb + 虚拟键盘 | 退出时 **SIGSEGV**（崩溃日志最后一行是 `hideInputPanel()`，发生在取证之后）；EGLFS 下退出码 0 |


## 2. 界面结构

```
┌ 上区域 通栏 72px：●三态连接 · 模式徽标 · 时钟 ────────────────────────┐
├ 左 96px 导航 ┤ 页面栈：主页面 / 模型测试 / 系统 / 设置                  │
│ （设置下沉）  │                                                        │
└──────────────┴────────────────────────────────────────────────────────┘
主页面内部：主区（非游戏=留给壁纸 / 游戏=视频区）+ 下区域（音乐条 / B站封面）
             右区域（320px 固定宽）：上=模式按钮区 · 中=对话区 · 下=日程区
```

右区域三块的伸缩因子是 **对话区 3 : 日程区 2**（`pages.cpp` 的 `rightBox`）。注意它只是
**伸缩因子**：窗口矮时最小高度会占超过 2/5，所以别把 3:2 当成硬比例。

**真机显示尺寸（S8 实测，别再靠猜）**：面板的 DRM 模式是 `800x1280`，但 xrandr 把它旋成了
`DSI-1 connected 1280x800+0+0 left` —— 所以 **X 桌面与全屏 kiosk 窗口都是 1280×800**。
右区域是 320 × 约 700，日程区实得约 200px（正好放下"标题 + 副标题 + 6 行 + 提示行"）。
⚠ `/sys/class/graphics/fb0/virtual_size` 报的 `800,1280` 是**面板模式**，不是给应用用的
逻辑尺寸 —— 拿它推布局会错。

### 2.1 全屏 kiosk 必须真的是一屏（T14-7b）

`showFullScreen()` **不等于**窗口就是一屏：Qt 只把几何设成屏幕大小，而顶层窗口还有一条
**应用自己的最小尺寸**。板端实测到的症状是窗口 `1280x883`（底部 83px 永远在屏幕外），
而 `_NET_WM_STATE_FULLSCREEN` 是有的 —— 也就是说"全屏请求发出去了、WM 也答应了，
但应用自己要 883 高"。取证要看这两条：

```bash
wid=$(DISPLAY=:0 xwininfo -root -tree | grep -m1 板端助手 | awk '{print $1}')
DISPLAY=:0 xprop -id "$wid" WM_NORMAL_HINTS     # program specified minimum size: 935 by 883
DISPLAY=:0 ./gui/build/agent_gui --dump-layout  # 逐控件打印 size/min/hint（就是这条链）
```

**根因（`--dump-layout` 量出来的链）**：`QStackedWidget` 的最小尺寸取**所有页**的最大值，
**包括当前隐藏的页**。当时 `ModelPage`（隐藏着）要 811px，而 1280×800 的面板只给页面
`800-72=728`px，于是 `72 + 811 = 883`。真正显示着的 `MainPage` 只要 602。

**规矩**：**每一页的 `minimumSizeHint().height()` 都必须 ≤ 728**（宽 ≤ 1280）。内容真的比一屏
高，就把整页放进 `QScrollArea`（`setWidgetResizable(true)`、横向滚动条关掉、视口背景透明）
—— 设置页本来就这么做，模型页 T14-7b 起也这么做；那样页面 min 会掉到 ~68，由滚动条兜住。
`gui/tests/test_page_heights.cpp` 逐页钉这条不变式（先 `createPage(key)` 再量），
新增页面若又比屏幕高，ctest 会直接红。

⚠ 量最小尺寸时**别在最小宽度下量**：`QLabel` 开了 `wordWrap`，宽度越小折行越多、
最小高度越大 —— 模型页在最小宽度下量出 811，按真实宽度铺开只要 510。要判的是
"页面 min 远小于内容 min"（滚动区在起作用），不是"内容 min > 728"。

- **唤醒机制**：四区域各自 `active`（空闲折叠，点一下出现）或 `locked`（常显）；
  任一区域的点击都会唤醒全部；隐藏区域设 `WA_TransparentForMouseEvents`，150ms 滑动。
- **视频内嵌控制条**有自己的 `active/locked` 与**独立的** `idle_ms`（不与区域共享）。
- **软键盘（onboard）**：`gui.input_source = keyboard` 且 `gui.onboard_auto = true` 时，
  **只有对话输入框拿到焦点才弹**（S10 起的政策），输入框失焦自动收起；
  启动与切换输入源都不弹 —— 以前一开机键盘就盖住主区。切到 `terminal` 会立即收起。

## 3. 配置（`config/config.yaml` 是唯一真源）

GUI 读写 `config/config.yaml` 的两个段：

| 键（都在 `config/config.yaml` 里） | 谁读 | 含义 |
|---|---|---|
| `gui.start_page` / `gui.debug` | GUI | 默认页 / Debug 日志 |
| `gui.wake.top/bottom/left/right` | GUI | 四区域 `active` 或 `locked` |
| `gui.wake.idle_ms` | GUI | **四区域共用**的休眠时间 |
| `gui.video_overlay.mode` / `gui.video_overlay.idle_ms` | GUI | 视频内嵌控制条的活动/锁定与**独立**休眠时间 |
| `gui.monitor_interval_ms` | GUI | 系统页刷新间隔 |
| `gui.schedule.max_rows` | GUI | 日程区最多显示几行（今天+明天**合计**，默认 6） |
| `llm.*` | GUI 写、Agent 读 | 推理位置（`edge`／`cloud`／`disabled`）与参数 |
| `scheduler.recurring` / `scheduler.oneoff` | Agent 触发、**GUI 只读展示** | 日程本身（GUI 读它画日程区，见下） |

> **T15-4 任务 9（只减不加）**：`gui.theme`、`gui.fullscreen`、`gui.video.speed`、
> `gui.video.fullscreen`、`gui.max_rows` 五个键**已从模板删除** —— 它们都是"纸面配置"：
> 没有任何代码读（全屏/窗口由启动参数 `--windowed` 决定，主题只有高级灰一档，
> 倍速与全屏由视频面板按钮直接控制）。设置页里那个"启动形态"下拉框随 `gui.fullscreen`
> 一起摘掉，所以"设置页能改的键"从 25 个变成 **24 个**。日程区行数改成真正生效的
> `gui.schedule.max_rows`（一直由 GUI 读，见 `docs/audit-code.md` §8.4 的勘误），归 **root 级**：只在
> `assistant shell` → `mode root` 里能改，改完重启 GUI 生效。

写回规则：只替换**已存在的键**，保留注释与顺序；文件里没有的键追加到该段末尾。
保存前 ConfigStore 会在原文件旁留一份 `.bak`（`*.bak` 已在 `.gitignore`）。

### 日程区读的是 `scheduler` 段（只读）

日程区**不自己存一份日程**，也不新增 IPC topic：它直接读 `config.yaml` 的
`scheduler.recurring` / `scheduler.oneoff`（与 Agent 的 `Scheduler._load_events()` 同一处、
连"某个键在 scheduler 段里找不到就回落到顶层"这条都一致），展开成**今天 / 明天**两段，
再按**窗口**筛出**接下来 24 小时**（`core::kWindowHours`；`[现在 - 30 分钟, 现在 + 24h)`，
起点那 30 分钟是"刚刚过去"的尾巴 —— 与 CLI 同口径，那些行**变暗**显示）。
判据是行的 `start`。每行显示 **`HH:MM  状态`**（状态大写：`SLEEP`/`IDLE`/`STUDY`/`GAME`）——
T12-4 起日程的内容只有「时间 + 状态」，没有标题、也没有时间段。读用 `yaml-cpp`（**只读**；
写回仍然只有 `ConfigStore` 的文本级替换，因为 yaml-cpp 会重排+丢注释）。
老写法里的 `title` / `end` / `remind_before_min` / `prompt` **不再读**（出现了当没写），
没有 `state` 的条目**跳过并记一条提示**（不整份失败：板端老配置里那几条纯提醒就是这个下场）。

窗口是**独立一层**（`ScheduleModel::applyWindow()` / `loadWindowed()`）：展开层
（`parse()` / `loadFromConfig()`）一行没动 —— 那层被 parity 夹具盯着，显示规则不混进去。
副标题写窗口终点（`接下来 24 小时 · 到 明天 18:42 · 3 项 · 下一条 明天 08:30`），
被截断这件事**只在副标题说明**（第二段标题仍写"明天"）。

> **尾巴那 30 分钟怎么显示**：里面的行是"已过" → **变暗**（与 S4 那套渲染同一路，不写字）。
> CLI 那边同样留 30 分钟，只是它还能靠触发事实写出「已触发 HH:MM:SS」——GUI 拿不到事实
> （不认 `schedule` topic），所以只有颜色这一个信号。
> ⚠ 窗口起点跨午夜时，前一天的尾巴行 GUI 看不到（展开层只有今天/明天两段）；CLI 能看到
> （它按需展开任意天）。这是"展开层不动"的代价。

代价说清楚：这等于在 C++ 里**镜像**了一份 Python 的日程语义。
防漂移靠 `tests/test_schedule_parity.py`（用真的 `agent.core.scheduler` 生成期望）
+ `gui/tests/test_schedule_model.cpp::parityWithPythonFixtures`（读同一份夹具断言）——
**保证范围就是那份夹具覆盖的写法**，超出夹具的冷门写法不承诺等价。
详细边界见 [`config-sources.md`](config-sources.md)。

刷新时机：启动、设置页保存后、之后每 60 秒一次（窗口往前滑 + 跨零点翻页）。

派生链路是**单向**的：

```
config/config.yaml  ──agent/core/llm_env.py──▶  llm/config/llm.env   （喂 llama-server）
   （唯一真源）                         （派生文件，不是真源）
```

`llm.env` 里只有 8 个键可推导：`LLM_MODEL_PATH`、`LLM_MODEL_NAME`、`LLM_PORT`、
`LLM_CTX_SIZE`、`LLM_BATCH_SIZE`、`LLM_THREADS`、`LLM_THREADS_BATCH`、`LLM_API_KEY`。
其余布局类键（`LLM_HOST`、`LLM_LOG_DIR`、`LLM_RUN_DIR`、`LLM_PID_FILE`、`LLM_LOG_FILE`）
由板端自己维护，派生**不碰**。手动跑一次：

```bash
# ⚠ T14-3 起没有这个工具了：GUI 只读配置，写入走 IPC 让 Agent 做（docs/adr/0005）
python3 -m agent.cli doctor             # 只看派生文件跟真源一不一致
```

⚠ 反向不成立：手改 `llm.env` 会在下一次「保存并同步」时被 `config.yaml` 覆盖。
完整约定见 [`config-sources.md`](config-sources.md)。

## 4. 各页现状（含占位，方案 §7 一律"可见地不能当真"）

| 页 | 已实现 | 占位（点了给说明，不发协议） |
|---|---|---|
| 主页面 | 模式切换、对话（chat_input/llm）、音乐条（曲目/歌手/专辑/进度/控制/**两句歌词** —— T15-16 全部点亮）、壁纸（**只画 Agent 推来的 `wallpaper`** —— 换壁纸只走对话，T7-3 起主区没有「下一张」按钮了；目录来自配置的 `wallpaper.dir`）、视频（本地文件播放/暂停/全屏/下一集）、输入源二选（键盘 onboard / 命令行）、**日程区**（只读展示 `scheduler` 段：**接下来 24 小时**、`时间 + 状态`、窗口终点写在副标题） | 倍速、B站封面 |
| 模型测试 | 推理位置三选、本地 GGUF 下拉与参数、云端参数、配置保存与同步、服务脚本启停与日志、基准测试（预检/全量/多模态）、停止测试、最新报告 | SigLIP 固定只读块 |
| 系统 | CPU/内存/NPU 负载/频率/温度/网络 IP/串流主机，按 `monitor_interval_ms` 刷新 | 看门狗启停 |
| 设置 | debug、四区域活动锁定与共用休眠、视频控制条活动锁定与独立休眠、默认页、默认输入类型、配置路径、恢复默认、关于 | 主题仅一档（"启动形态"下拉框 T15-4 任务 9 已删，见 §3 的表注） |

## 5. 图标

`gui/resources/icons/*.svg`（**23 个自绘单色描边图标**）+ `icons.qrc`，
`ui::icon(name)` / `ui::tintedIcon(name, color)` 按需染色（`CompositionMode_SourceIn`）。
好处：不再依赖字体码位（板端 `⏸`/`⏮` 等码位没有字体覆盖，会被画成豆腐块）。

⚠ 加图标要**同时**改三处：`resources/icons/<名字>.svg`、`resources/icons.qrc`、
`src/ui/icons.cpp` 的 `kNames`。漏一处界面上只是"静默少一个图标"，
所以 `test_icons` 会核对 `kNames` 与源码目录里的 `*.svg` 清单**逐个对上**。

## 6. 验收取证约定

- 每个任务：**零警告构建 + `ctest` 全绿 + 真机截图**，证据写进 `temp/T<任务>_evidence.md`，
  截图放 `temp/t<任务>/`（`temp/` 在 `.gitignore` 里，不入库）。
- 视频画面 `grab()` **抓不到**（原生视频表面）→ 视频相关证据一律用 `scrot` 抓屏。
- 会接管 `llama-server` 的操作（全量基准、服务启停）在生产服务运行时**不要**执行；
  验收脚本用临时仓库 + 替身脚本。

---

## 12. T15-16（GUI 优化）总账

> 清单与**逐项状态**见 `todo2.md` **§6** ✓（唯一权威 ✓）；主题 / 触摸目标 / 异常态三节见
> **`docs/gui-style.md`** ✓。这里只讲**结果、数字与遗留** ✓。

### 12.1 三组结果

- **G-A 立规矩（4/4 ✓）**：`theme.h` 单一来源 ✓；QSS 搬进 `base_style.h`（**159 处** ✓ + 6 个文件内联 **26 处** ✓）；
  **黄金串逐字节等价** ✓；三条守卫 R1（色值只许在 `theme.h` ✓）/ R2（不许 `font-size: Npx` ✓）/
  R3（裸 `QColor(0x…)` 走逐文件计数白名单 ✓）—— **都做了牙齿演示** ✓。
- **G-B 省开销（7/7 ✓）**：统一探针 `gui-probe.sh` ✓；音乐条 / 图标染色 / 日程改为"**没变就不动**" ✓（计数 + 牙齿 ✓）；
  **系统页停掉隐藏期的定时器：唤醒 4.13 → 3.07 次/s（−26%）** ✓；**壁纸淡入限帧：12 → 5 帧（−58%）** ✓；
  键盘让位的顺序整理 ✓（"150 ms 动画"**评估后不做** ✗ —— 它会产生更多中间高度 ⇒ 更多次布局重算 ✓）。
- **G-C 一致性（5/5 ✓）**：漂移色并入 ✓；7 处裸色清零（R3 白名单**清空** ✓）；字号 **11 → 5 档** ✓（黄金值同步 ✓）；
  触摸目标**全部 ≥44px** ✓（新增守卫 R4 ✓，例外 5 条逐条写理由 ✓）；**系统模态清零** ✓（破坏性确认改"两步确认" ✓）。
- **G-D 异常态（3 ✓ / 1 ⚠）**：`StateBanner` + `Skeleton` 两组件 + 5 条单测 ✓；
  **10 行异常态矩阵**（触发条件/文案/图标/能否重试/证据 五项齐 ✓）—— 现在 **9/10 行有证据** ✓：第 1 行补了 `test_top_bar` ✓、第 4 行**其实早有断言** ✓（`test_sys_page:81` 断言 hint 含 `/proc` ✓；我当初搜整句故零命中 ✗）、第 7 行补了 `test_failure_text` 的 R5 ✓；**只剩第 2/3 行留白** ✗（都要 `MainWindow` 级测试 ✓，整套没有 ✓）；
  骨架**逐页重判、三页全闭环** ✓（`settings_page` 等 Agent 推 `ota_state` ⇒ 该挂 ✓ / `sys_page` **构造期同步**读 `/proc` ⇒ 判据**证伪并回退** ✗ / `model_page` 等 **IPC 回调**（跑分秒级 ✓）⇒ 该挂 ✓）；
  对照图 **4/16 张** ⚠；**「10 行异常态改走 `StateBanner`」已逐页判定完毕** ✓ （**换 2 处**：`sys_page` 的「读不到 /proc」✓ 与 `schedule_panel` 的尾部提示 ✓，都带判据 + **牙齿** ✓；**判不换 8 处** ✗ —— 顶栏三态已有自己的视觉 ✓、对话提示行是 `MainWindow` 级 ✓、壁纸兜底**就是壁纸本身** ✓、视频与封面是**舞台占位** ✓、歌词是**内容显示区** ✓、OTA 是**卡片字段行** ✓，**每一处都写了理由** ✓）。

### 12.2 数字（**都有可复跑的判据** ✓）

| 项 | 前 | 后 | 判据 |
|---|---|---|---|
| 隐藏期自愿唤醒 | 4.13 次/s | **3.07 次/s** | `scripts/gui-probe.sh` 的 `wakeups_s` 列 ✓ |
| 壁纸淡入重绘帧数 | 12 帧 | **5 帧** | `--wallpaper-demo` 打出的那行日志 ✓ |
| 字号档位 | 11 档 | **5 档** | `test_theme` 的黄金串 ✓ |
| 裸 `QColor(0x…)` | 7 处 | **0 处** | `test_style_guard` R3（白名单为空 ✓） |
| 触摸目标 <44px | 8 处 | **0 处** | `test_style_guard` R4 ✓ |
| 可执行测试数 | 27 | **35** | `ctest` ✓（G-D 期间 +6：`test_state_banner` ✓ / `test_top_bar` ✓ / `test_failure_text` ✓ / **`test_main_window`**（MainWindow 级**地基** ✓ 33→34 ✓）/ **`e2e_failure_matrix`**（异常态矩阵**第 2 行** ✓ 34→35 ✓）与既有文件里新增的用例 ✓） |
| 审计棘轮 | 145 条 | **144 条**（零新增 ✓） | `scripts/audit-code.py` ✓ |

### 12.3 两个工具（都要**在板子上**跑 ✓ —— 宿主没有 CJK 字体，中文是豆腐块 ✗）

- `scripts/gui-probe.sh <秒> <标签>` ✓：CPU% / **唤醒每秒** / RSS / 峰值 / 打点数 ✓（G-B 的前后数字就是它出的 ✓）；
- `scripts/gui-shots.sh <输出目录> <标签> [二进制] [每页等待 ms]` ✓：按页出图 ✓（`before` / `after` 两套标签 ✓）。

### 12.4 遗留（**最终 3 条** ✓；下面 5 条是历史，带闭环痕迹 ✓）

> **最终 3 条** ✗：
> ① **「改前」12 张对照图** —— 改前的二进制**没留档** ✓（板上是改后的 ✓）⇒ **做不了** ✗；
>    `scripts/gui-shots.sh` **已支持 `before` 标签** ✓（有旧二进制时一条命令补齐 ✓），**但不贴假图** ✗。
> ② **矩阵第 2/3 行留白**（命令没发出／壁纸读不到 ✓）—— 都要 `MainWindow` 级测试 ✓，而整套 **35** 项里**已经**有了 ✓（`test_main_window` ✓ —— 本轮新加的地基 ✓）（G-B-4/B-6 已论证这条是兔子洞 ✗）。
> ③ ~~真机图上那块灰角色块「未定性」~~ ⇒ ✅ **已定性：它就是顶栏的 `ModeBadge`** ✓（没有模式信息时显示 `——` ✓）—— **不是 bug** ✓，是顶栏的合法表达 ✓。⚠ **我一度把它记成「未解」✗ 还误归到「内容区顶部」✗** —— 是三条证据一起把它定住的 ✓：`--dump-layout` 的 `QLabel#ModeBadge size=54x43 visible=1` ✓（它在 **TopBar 子树**里 ✓）、两张真机图上那个方块里**就写着 `——`** ✓、以及 `modeLabel()` 未知模式时正是返回 `——` ✓。**这两步推错留着不抹** ✗（便于后人看到我是怎么绕回来的 ✓）。
> ① ~~改前图~~ ⇒ ✅ **已闭环** ✓（2026-10-05）："改前"取 **`b9b909e^`**（G-A-1b 之前 ✓，即 `4985b1e` ✓）⇒ `git archive` 导出旧源码 ✓（不动工作区 ✓）→ 交叉编出 **ARM aarch64** 旧二进制 ✓（845,944 字节 ✓）→ 板上**先备份**当前二进制 ✓、装上旧的 ✓、抓四页 ✓、**立刻换回并 `active`** ✓✓（换回后尺寸与备份一致 ✓）。
>    **实际张数（写实数 ✓，不凑 16 ✗）**：**8 张配对**（改前/改后 × 四页 ✓）＋ **4 张真机 1280×800**（改后 ✓）＋ 1 张真机竖屏 ✓ = **13 张** ✓。
>    ⚠ **看图得到的结论比图本身值钱** ✓：改前/改后**看起来几乎一样** ✓ —— 因为改前取的那一步（G-A-1b ✓）**产出的就是逐字节等价的 QSS** ✓（`test_theme` 的黄金串正是为证这件事 ✓），而真正会**改像素**的（漂移色并入 ✓／字号 11→**5 档** ✓／触摸目标→**44px** ✓／两个新组件 ✓）**都在其后** ✓ ⇒ **"几乎无变化"是「视觉等价」这条纪律的预期结果** ✓，不是白做 ✗ 也不是抓错图 ✗。
>    ⚠ **我只逐张看过其中一对**（settings ✓）＋ 真机四页 ✓ ⇒ **其余配对图我没逐张目视** ✗，**不说"都核过了"** ✗。
> ⇒ **最终只剩 1 条** ✗：**矩阵第 2/3 行缺测试证据** —— 原因**已查清** ✓，**不是「没做」** ✗，而是「**做过、查清了、需要不同手段**」✓：
>   · **第 3 行（壁纸读不到）** ✓：触发器**私有** ✗ —— `main_window.h` 的 `public:` 在 :52 ✓、`private:` 在 :141 ✓，而 `onMessage()` 在 :146 ✓、`setWallpaperFromPath()` 在 :162 ✓ ⇒ 测试**点不动** ✓。要补**得改产品 API** ✓（公开一个「喂一条 `wallpaper` 消息」的验收辅助 ✓ —— 与仓库里十几个 `--*-demo` **同一路数** ✓）⇒ **本轮没做** ✗，如实留档 ✓。
>   · **第 2 行（命令没发出）** ✓：**试过了** ✓ —— 裸造窗口 + 点设置页「保存」按钮 ✓（**公开**入口 ✓ 合规 ✓）⇒ **立即扫描找不到**「命令没发出去」✗ （文案在 `main_window.cpp:778/801/818` ✓）。我**没有再赌**「给事件循环一次机会就好了」✗ —— **留一个红用例比少一格证据糟得多** ✗✗ ⇒ 该用例**已撤** ✓，原因与补法（改用 **`QTRY_VERIFY`** 重试 ✓ 而不是 `qWait` ✓）都写进了 **`gui/tests/test_main_window.cpp` 顶部注释** ✓。
>   · 🆕 **另：本轮的副产品** ✓ —— 为查这两行，顺带**翻掉了我一条旧误判** ✗→✓：G-B-4/B-6 那两轮我凭「**没有先例**」就把 MainWindow 级测试判成**兔子洞** ✗（**没读构造函数** ✗）；实测 `explicit MainWindow(QWidget*)` ✓ 是普通构造 ✓、「开始连接 Agent」是单独的**非阻塞**调用 ✓ ⇒ 测试**能裸造窗口** ✓✓ ⇒ 于是有了 **`test_main_window`**（**35/35** ✓）✓。

1. **G-D-4 只出 4/16 张** ✗ —— 改前的二进制**没留档** ✓（板上是改后的 ✓），补齐要么重刷旧镜像 ✗、要么重建旧 commit ✗；
2. ~~`model_page` 的 loading 骨架未做~~ ⇒ ✅ **已闭环** ✓（2026-10-05：核实跑分/扫描走 **IPC 回调** ✓、跑分还是**秒级** ✓ ⇒ 真有异步窗口 ⇒ 该挂 ✓；骨架挂**外层布局** ✓ 不随滚动跑走 ✓，`startBenchmark()` 起、`onServiceResult()` **stop()+hide()** ✓；判据 `theBenchmarkShowsALoadingSkeleton` 一正一反 ✓ + **牙齿** ✓）；
3. ~~10 行异常态未统一到 `StateBanner`~~ ⇒ ✅ **逐页判定完毕** ✓（**换 2 处** ✓ 带判据与牙齿 ✓；**判不换 8 处** ✗ **逐条写了理由** ✓ —— 判不换是**决定** ✓，不是漏做 ✓）；⚠ 换的那两处**只换控件、不改 QSS** ⇒ **黄金串没变** ✗（视觉变化黄金串抓不到 ✗，靠页高 + 各页用例 + 全量兜底 ✓）、**真机 1280×800 当时没看** ✗ ⇒ **同日已核** ✓（见下面第 5 条 ✓ —— 本条尾注曾与第 5 条**自相矛盾** ✗，2026-10-05 已修 ✓）；
4. **矩阵只剩 2 行留白** ✗（第 2 命令没发出 / 3 壁纸读不到 ✓ —— 都要 `MainWindow` 级测试 ✓，整套里没有 ✓）；原先那两行「该页有单测但未断言文案」**已分别处置** ✓：第 4 行**其实早有断言** ✓（我当初搜的是整句 ✗）、第 7 行补了**源码级 R5** ✓（`test_failure_text` ✓ 含**牙齿** ✓，⚠ 宿主无多媒体后端 ⇒ 行为级驱动不了 ✗）；
5. ~~未做 EGLFS 1280×800 的真机布局核对~~ ⇒ ✅ **已核** ✓（2026-10-05）：
   **抓法** ✓：停 `agent-gui` ✓ → 用 **unit 里的真实环境**（`QT_QPA_PLATFORM=eglfs` ✓、`QT_QPA_EGLFS_INTEGRATION=eglfs_kms` ✓、**`QT_QPA_EGLFS_ROTATION=90`** ✓ —— 这三条是 `systemctl cat agent-gui` **读出来的** ✓ 而不是猜的 ✗）＋ `cd /data/assistant` 与 `--config config/config.yaml` ✓（⚠ 少了后两个它会**静默退出** ✗ —— G-B-5 那轮的教训 ✓）→ 四页逐一 `--page X --screenshot Y` ✓ → 抓完 **`systemctl start` 并复查 `active`** ✓；
   **产出** ✓：`eglfs-r-{home,model,system,settings}.png` ✓ = **1280×800** ✓（**产品形态** ✓；另有未旋转的 800×1280 一张 ✓）；
   **我看过的结论** ✓：中文**无豆腐块** ✓、1280 宽下布局**舒展** ✓、**G-C-3 的 44px 触摸目标在真机站得住** ✓（行控件够高、无挤压 ✓）、**`StateBanner` 在健康状态下整条隐藏** ✓（系统页 `/proc` 读得到 ⇒ 图上没有任何横幅 ✓）；
   ✅ **那项「未解」已解开** ✓（见 §12.4 摘要第 ③ 条 ✓）—— 以下是**当时**的记录 ✗：`home` 与 `settings` 的**左列、顶栏之下**（约 `x≈130,y≈75`）都有一块**灰色圆角色块** ✓ —— **已用两条硬理由排除 `LinkBanner`** ✓（QSS 给它的是 `@warn@` **琥珀** ✗ 而那块是灰的 ✓；`LinkBanner` 住在**右侧对话卡**里 ✓ 而它在左列 ✓）⇒ **身份未定性** ✓，如实留档 ✓（这正是**只有真机能发现**的那类东西 ✓）。


### 12.5 T15-17：`gui.*` 设置改为**立即生效** ✓（不再需要重启）

- **根因**（T15-17 起手时查实 ✓）：设置页把 `gui.*` 写进配置 ✓、Agent 也**认**这些键 ✓（`agent/core/config_tiers.py` ✓），但 GUI 只在**启动**时套用一次 ✗（`applyConfig()` 的唯一调用点在 `main.cpp` ✓），保存后的回执**只转给设置页** ✗ ⇒ **改了要重启才生效** ✗（而 `settings_page.h` 的注释**曾承诺过**要重放 ✗）。
- **改法** ✓：把 `applyConfig()` 拆成「读盘 + 回填页面」（启动用 ✓，**唯一**会调 `loadFromConfig()` 的地方 ✗）与新的 `reapplyGuiConfig()`（**只套用运行时** ✓，**不碰页面控件** ✓ ⇒ **不会冲掉正在编辑的内容** ✓）；保存回执成功后重放 ✓；设置页**改控件就发信号** ⇒ 重放 ✓ ⇒ **当场生效** ✓。
- **判据** ✓：`test_main_window` 三条 ✓（含 ★「重放不许冲掉正在编辑的内容」✓，其守卫"重放确实跑过吗"✓）＋ **两颗牙齿都咬** ✓（注掉 `emit` ⇒ 红 ✓；往信号路径塞 `restoreDefaults()` ⇒ ★ 判据红 ✓）。
- **语义与边界** ✓：见 `docs/gui-style.md` §4 ✓。


### 12.6 T15-17 上板真机取证（2026-10-05，板端 1280×800 ✓）

**怎么抓（可复核 ✓，照抄即可）**
```bash
# 板上：停服务 ⇒ 用 unit 里的真实环境跑 ⇒ 抓完恢复服务
ssh rk3568 'systemctl stop agent-gui; sleep 2'
ssh rk3568 'cd /data/assistant; QT_QPA_PLATFORM=eglfs QT_QPA_EGLFS_INTEGRATION=eglfs_kms \
  QT_QPA_EGLFS_ROTATION=90 timeout 40 /usr/lib/assistant/gui/agent_gui \
  --config config/config.yaml --page home --screenshot /data/shots/t7-late.png --screenshot-delay 12000'
ssh rk3568 'systemctl start agent-gui; sleep 6; systemctl is-active agent-gui'
scp rk3568:/data/shots/t7-late.png .
```
> ⚠ 环境取自 `systemctl cat agent-gui`（**不是猜的** ✗）；`cd /data/assistant` 与 `--config config/config.yaml`
> **缺一个就静默退出** ✗（G-B-5 那轮的教训 ✓）。覆盖运行中的二进制要 `.new` + `mv -f` ✓。

**取到了什么 ✓**

| 图 | 内容 | 证明了什么 |
|---|---|---|
| 设置页（延时 6 s） | 四区域卡片下方**新回显行** ✓：`当前生效：上=锁定（常显）✓／下=活动✓／左=活动✓／右=锁定✓ ｜ 共用 5000 ms ✓` | 回显在真机上**渲染正确** ✓；且它**不是**初始的「(待刷新)」✗ ⇒ **启动期**这条链在真机上通 ✓（`loadFromConfig` → 控件变化 → `guiSettingsEdited` → 回显刷新 ✓） |
| `home`（延时 **2 s** ✓） | **导航栏四颗图标清楚可见** ✓（主页高亮 ✓、太阳 ✓） | 未到 5000 ms ⇒ **还没折叠** ✓（对照基线 ✓） |
| `home`（延时 **12 s** ✓） | **整条导航栏不见** ✓；内容与底部音乐条**贴到左边缘** ✓ | 超过 5000 ms ⇒ **当场自动折叠** ✓✓ |

⇒ ⇒ **结论** ✓：`gui.wake.left = active` ＋ `gui.wake.idle_ms = 5000` 这条链（`IdleWatcher` → `RegionHost` → 折叠 ✓）
**在真机上真的生效** ✓✓ —— 不是只有单测里成立 ✓。

**✗ 仍未取证（如实 ✓）**：**「改控件 ⇒ 当场生效」**那一步需要**一次触摸** ✗
（板上 eglfs、没有 X ✓，我**无法远程点屏** ✗）⇒ 留给**人在板前**做 ✓：
把「左（导航）」从**活动**改成**锁定** ✓ ⇒ **不重启** ⇒ 左栏应当**当场变常显** ✓；
⚠ 同时看**回显行**是否**跟着变** ✓（它显示的是**真正生效**的值 ✓ ⇒ 骗不了人 ✓）。


### 12.7 关于「用虚拟触摸事件代替手指」：**试过了，打不通** ✗（2026-10-05）

**为什么试** ✓：T7 的最后一步是「在设置页改一次四区域控件、不重启看是否当场生效」✓，
而板上碰不到人 ✓ ⇒ 我用 `/dev/uinput` 造了一块虚拟触摸屏来模拟 ✓（`/dev/uinput` 在 ✓，触摸屏是 `goodix-ts`/`event3` ✓）。

**五轮、四种办法，全都没进到 GUI** ✗：

| # | 办法 | 结果 |
|---|---|---|
| 1 | uinput 直接注入（设备在 GUI **之后**创建） | ✗ |
| 2 | 改打"大目标"（下拉框） | ✗ |
| 3 | **设备先行**（`--delay` ✓，让 Qt 启动时设备已在 ✓） | ✗ |
| 4 | 强制 `QT_QPA_GENERIC_PLUGINS=evdevtouch:/dev/input/event6` ✓（绕开 libinput 的 udev 标签门槛 ✓） | ✗（节点确认无误 ✓：`/sys/class/input/event6/device/name` = `dsh-virtual-touch` ✓） |
| 5 | 补 `ABS_PRESSURE` + `BTN_TOOL_FINGER`（v2 ✓，见 §7 的"唯一剩余假设"✗） | ✗ |

**判据用的是哪一个** ✓（这点很重要 ✓）：**左栏是否被任何触摸唤醒** ✓ ——
因为「左（导航）」当时正是**活动**态 ✓ ⇒ **任何**真实触摸都会唤醒它 ✓
⇒ ⚠ 这比"焦点框变没变"可靠得多 ✗（后者会被"两个不同实例"骗到 ✓ —— 我第 1–2 轮就是这么被骗的 ✗，已收回结论 ✓）。

**为什么打不通（我的判断，标注为判断 ✗ 不是定论 ✓）**：板上的 Qt 同时带
`libqevdevtouchplugin.so` 与 `libqlibinputplugin.so` ✓ ⇒ EGLFS 默认多半走 **libinput** ✓，
而 **libinput 只接受 udev 打了 `ID_INPUT_TOUCHSCREEN` 标签的设备** ✗ ——
`/dev/uinput` 造的裸设备没有这个标签 ✓；第 4 步虽然把 **evdevtouch** 指到了正确节点 ✓ 仍无反应 ✓
⇒ 可能还差 `QT_QPA_EVDEV_TOUCHSCREEN_PARAMETERS` ✓ 或设备能力表里的别的位 ✓ —— **未再深挖** ✗。

**留下的东西** ✓：
- 注入器源码 `tap.c`（MT 协议 B ✓ + `--delay` ✓ + PRESSURE/TOOL_FINGER ✓）；
- 编译脚本 `build-tap.sh` ✓ —— ⚠ 里面记着**工具链的真实路径** ✓：
  buildroot 的 host 目录是**双层同名**（`output/<CFG>/<CFG>/host/bin` ✓）⇒ 我三次通配都栽在这 ✗；
- 三种时序脚本 `tap-run.ps1` / `tap-B.ps1` / `tap-C.ps1` ✓；
- 板上 `/data/assistant/tap` ✓（ARM aarch64 静态 ✓）。

**三条教训** ✓（都已写进 §5.3 的口径 ✓）：
1. ⚠ **"两个不同实例"会伪装成"事件生效了"** ✗ —— 我拿"焦点框消失"当证据 ✓，
   后来查明每张截图都是**新起的 GUI 实例** ✓（对比的其实是两个进程 ✓）；
2. ⚠ **`pkill -f agent_gui` 会杀掉自己那条 ssh 的 shell** ✗（命令行里就含这个词 ✓）⇒ 后面全不执行 ✓；
3. ✅ **真机判据要挑"必然响应"的那个** ✓（这里是**左栏唤醒** ✓），不要挑**像素级、可被旁因解释**的 ✗。

**⇒ 结论** ✓：T7 的「改控件 ⇒ 当场生效」这一步，**注射器在本板打不通** ✗
⇒ 要么**人在板前手指点一下**（1 秒 ✓），要么**换一条判据**（例如在 GUI 侧打印它收到的输入事件 ✗
—— 那要**动产品** ✓ ⇒ 本任务不做 ✓）。


### 12.8 T15-17 上板收尾（2026-10-05 19:50–19:57，**走过一次真 OTA** ✓）

**为什么要做** ✓：bug 1 查实**不是代码 bug** ✗ —— 板上 Agent 停在 **10-04 13:18** 的旧版 ✓，
那份**没有** OTA 推送（`grep -c _start_ota_status` = **0** ✓）⇒ 所以 GUI 的「系统升级」第一行
永远停在「还不知道」✗。修法 = **用 OTA 把新 Agent 推上去** ✓。

**① 出包** ✓：`build.sh ota-updateimg` ⇒ 包内容 = `uboot`/`misc`/**`boot_a`**/**`system_a`**/`oem` ✓
⇒ **不含 `userdata`** ✓（`/data` 全程未动 ✓）。⚠ 出包前必须把 SDK 的 `output/.config` 从
**DEFAULT** 改成 **CUSTOM + `ota-assistant-ab.txt`** ✓（**替换**而非追加 ✗ —— doc §1 说这坑白跑过两次 ✓）。
⚠ 内容验证要扫**打包后的 `rootfs.img`** ✓（raw grep 命中 `_start_ota_status` **2 次** ✓），只看 target 会漏 ✗。

**② 推包 + 应用** ✓：两侧 `sha256` **一致** ✓（`292d5b7d…3416` ✓）⇒ `--dry-run` 显示
「当前槽 **_b** → 目标槽 **_a**」✓（安全方向 ✓）⇒ 真应用 + `--reboot` ✓（ssh 被切断 = 重启生效 ✓）。

**③ 防回退（doc §8.1 那个坑，本次真的踩上了 ✗）** ✓：`confirm.json` 在 `/data`，**两槽共享** ✓ ⇒
里面是 **00:35** 的旧结果（`slot:"a"` ✓）⇒ 新槽启动时被当成「已确认」✗ ⇒ **不重判不标记** ✗
⇒ `槽a successful_boot=0` 且 `tries_remaining` 递减（6 ✗）。
修法 ✓（只动 `/data` 那个 json ✓）：备份 → 把旧结果挪走 → `systemctl restart assistant-ota-confirm`
⇒ 日志「agent/agent-gui -> **active active** ✓」「**IPC：通 ✓（rc=0，首行：已连上 Agent）** ✓」
「**判定：通过 ✓（9.6s）标成功：成 ✓**」⇒ 槽a **tries=7 / successful=1** ✓（原 **0** ✗）、
新 `confirm.json` **at=19:54:36** ✓（**本次** ✓）。

**④ 把 T15-17 的 GUI 推回板上** ✓：OTA 镜像里的 GUI 是 **10-04 21:00** 版 ✗ ⇒ 我的
「改完立即生效 / WakeEcho 回显 / 顺序修」都不在板上 ✗ ⇒ 备份后按 `.new` + `mv -f` 覆盖为
**878,712 B** 那份（2026-10-05 18:29 交叉编 ✓）⇒ `agent-gui` **active** ✓。

**⑤ 验收取证（三条 ✓）**

| 判据 | 结果 |
|---|---|
| **顺序修**：journal 出现「[ui] 设置页改动已即时套用」 | ✅ **两次** ✓（修前**一次都没有** ✗）；唤醒行与配置一致 ✓ |
| **折叠行为**（换 GUI 后复查） | ✅ 12 秒图：**上/下/左/右 四个区域全部折叠** ✓（与配置"全=active + 5000 ms"完全一致 ✓，逐项可指 ✓） |
| **ota_state 推送到位** | ✅ `assistant watch` 打出了 `ota_state` ✓（含 `current_slot=a` ✓、`last_ota.target_slot="a"` ✓、备份路径 ✓、`confirm.marked=true` ✓）⇒ GUI 那行不会再停在「还不知道」✓ |

**⚠ 仍未由人验证的一条** ✗（如实 ✓）：**人手在设置页改一次控件 ⇒ 当场生效** ✓ ——
虚拟触摸注入在这块板上**打不通**✗（五轮四法，见 §12.7 ✓），所以要由**人在板前**把
「左（导航）」由**活动**改成**锁定** ✓ ⇒ **不重启** ⇒ 看左栏是否**当场常显** ✓ 与
`WakeEcho` 是否**立刻跟着变** ✓。⚠ 能看到的三条证据（即时套用日志 ✓／回显真值 ✓／折叠双图 ✓）
都指向"**机制是活的**" ✓，但"**由人触发**"这一路**仍未验** ✗ —— 不冒充已验 ✓。


### 12.9 bug ②「CLI 的输入与回复要显示在聊天框内」：**已闭环** ✓（2026-10-05）

**需求原话**（用户确认 ✓）：「cli 输入以及回复内容应该显示在聊天框内」✓。

**根因** ✓（有铁证 ✓）：`agent/ipc/__init__.py` 的 `chat_input` 处理里，来源被**写死**成
`bus.push("gui", text)` ✗ —— 而 `ChatInputBus.push(source, text)` 的签名**本来就带来源** ✓
（`agent/io/chat_bus.py:102`，注释就写着 `terminal`/`gui` ✓），Runtime 也**本来就按来源分派** ✓
（`agent/main.py:3253/3304` ✓）。缺的只有**标记来源**与**回显输入**这两环 ✗。

**做法** ✓（Agent 两处 + GUI 一处，**向后兼容** ✓）：
1. `agent/cli.py` 的 `chat` 发送时带 **`source="terminal"`** ✓（CLI 本来就知道自己是 CLI ✓）；
2. `agent/ipc/__init__.py`：来源**按载荷取**（**缺省仍是 `"gui"`** ⇒ GUI 侧协议不用动 ✓），
   并在来源**不是 gui** 时推一条 **`llm{text, role:"user"}`** ✓（复用 GUI 已在渲染的 `llm` 通道 ✓）；
3. `gui/src/main_window.cpp` 的 `llm` 分支按 `role` 分流 ✓：`user` ⇒ **`ChatPanel::appendUser`** ✓（用户气泡 ✓），
   否则照旧 `appendAssistant` ✓ ⇒ **老 Agent 不推 role ⇒ 行为与今天完全一致** ✓。

**真机证据** ✓（板上跑的就是这套 ✓）：
- **GUI 侧** ✓：在板端发 `assistant chat 你好` ⇒ 聊天框里出现**蓝色用户气泡「你好」** ✓
  ＋ **灰色助手回复**「你好，我是板端助手。当前处于简易模式（未接入大模型）…」✓（截图 `t7-chat.png` ✓，1280×800 ✓）；
- **CLI 侧** ✓：`assistant --timeout 25 chat 你好` ⇒ 打印**助手真回复** ✓。

**⚠ 如实标注：一条测不到的缺口** ✗ —— `role` 分流写在 `MainWindow::onMessage()` ✓（**私有** ✓，
只能由 `LocalClient` 的信号触发 ✓，**没有公开入口** ✗）⇒ 除非为测试开洞 ✗（我**不做** ✓）
⇒ 那条分流的**真实验证只能靠上板** ✓ —— 本次就是靠截图 + CLI 输出验的 ✓。
（与 `docs/gui-style.md` 异常态矩阵第 2/3 行是**同类限制** ✓，照旧**不假装被单测覆盖** ✗。）

**⚠ 一条端到端才抓得到的副作用** ✗（值得记 ✓）：回显走的是 `llm` **广播** ✓ ⇒ `assistant chat` 的 CLI
**自己也会收到**那条回显 ✗ ⇒ 第一版板上实测打印成「助手: 你好」✗（自己跟自己说话 ✓）。
修法一行 ✓：CLI 的 `on_message` 跳过 `data.get("role") == "user"` ✓。
⇒ **单测看不到它** ✗（`ChatPanel` 可区分 ✓ 绿 ✓）、**CI 也看不到** ✗（Python 套件绿 ✓）——
它是**跨端语义**上的 ✓ ⇒ **这是"上板验收不可省"的活证据** ✓✓。


### 12.10 bug ①「软键盘唤不出」的取证结论与**当前卡点** ⚠（2026-10-05）

**根因** ✓：buildroot 的 `BR2_PACKAGE_LIBXKBCOMMON` 与 `BR2_PACKAGE_XKEYBOARD_CONFIG` **默认都没开** ✗
⇒ 板上没有 `libxkbcommon`、没有键位表 ⇒ Qt 报 `xkbcommon not available, not performing key mapping` ✗
⇒ 虚拟键盘**建不了键位映射** ✓（**不是** GUI 代码 bug ✗，也**不是**缺 QML ✗ ——
QML 在 `/usr/qml/QtQuick/VirtualKeyboard` ✓，我先前查错路径（只查了 `/usr/lib/qt/qml` ✗）并已**收回**该结论 ✓）。

**已做** ✓：两个开关写进仓库真源（`kickpi-k1mini-release.config` ✓，提交 `6910ae9` ✓）+ SDK 同步 ✓
+ 重新出包 ✓ + **包内验证** ✓（`libxkbcommon` 3 次 ✓、键位表 16 次 ✓）。

**⚠ 卡点** ✗：新包**只写 A 槽** ✓，而板上**当前就在 A 槽** ✓ ⇒ 安全守卫拒绝 ✓✓（**按设计工作** ✓，未绕过 ✓）。
⇒ 要装新镜像必须先**切一次槽** ✓ ⇒ 方案见 `todo2.md` §6.x ✓（两条，均待用户点头 ✓）。

**⚠ 本次一条翻车记录** ✗（如实 ✓）：我用 **48 KB** 的 `b-active-misc.img` 当"整个 misc"整块写 ✗✗，
⇒ 读回 md5 不一致 ✗ ⇒ 因为 misc 是 **4 MB** ✓，那 48 KB 是 **BCB 片段** ✓（`ota-apply.py:16` ✓）。
**已用备份安全写回并 md5 自证** ✓，板子完好 ✓。
⇒ 教训：**别按文件大小推断分区位置** ✗；砖级操作**用项目自己的函数** ✓。


### 12.11 bug ①「软键盘唤不出」：**根因修复已完成并由运行时证据确认** ✓（2026-10-05）

**硬证据（两条，都是板上实测 ✓）**：
1. **那句坏日志消失了** ✓ —— `journalctl -u agent-gui | grep xkb` 现在**为空** ✓
   （修前**每次启动必现** ✓：21:43:40 ✓、21:45:19 ✓ 都抓到过
   `qt.qpa.input: xkbcommon not available, not performing key mapping` ✓）；
2. **`LD_DEBUG=libs` 显示运行时真的加载了** ✓✓：
   ```
   find library=libxkbcommon.so.0 [0]; searching
   trying file=/lib/libxkbcommon.so.0
   calling init: /lib/libxkbcommon.so.0                       <-- xkbcommon 真的 init 了
   find library=libQt5VirtualKeyboard.so.5 [0]
   calling init: /usr/lib/qt/plugins/platforminputcontexts/libqtvirtualkeyboardplugin.so
   calling init: /usr/lib/qt/plugins/virtualkeyboard/libqtvirtualkeyboard_pinyin.so   <-- 连拼音都起来了
   ```
   ⇒ ⇒ **输入栈已从"缺库报错"变成"库 + 虚拟键盘插件 + 拼音输入法全部 init"** ✓✓。

**完整根因链（十四层，每层都有实测证据 ✓）** —— 这是本 bug 真正的全貌，也是它的教学价值：

| # | 事实 | 怎么知道的 |
|---|---|---|
| ① | 板端 Qt 每次启动都报 `xkbcommon not available` ✗ | journal ✓ |
| ② | 板上没有 `libxkbcommon` ✗ | `ls` 空 ✓ |
| ③ | buildroot 默认**不开** `LIBXKBCOMMON` / `XKEYBOARD_CONFIG` ✗ | `.config:3215 / 1878` ✓ |
| ④ | 只跑 `ota-updateimg` **只打包** ✗ | 包内挂载无库 ✓ |
| ⑤ | `build.sh buildroot` 才是重建 ✓，但它**从 defconfig 覆盖 `.config`** ✗ | 日志 `Buildroot config changed!` ✓ |
| ⑥ | 正确落点是 **SDK 里的片段文件** ✓（只改仓库无效 ✗） | SDK 那份实测 **0 行** ✓ |
| ⑦ | 同步后重建 ⇒ 库与键位表进 rootfs ✓ | **挂载验收**：`libxkbcommon.so.0.0.0` 276,256 B ✓、`/usr/share/X11/xkb` ✓ |
| ⑧ | ⚠ buildroot **拒绝 PATH 里有空格** ✗ | 日志 `Your PATH contains spaces… Fix you PATH.` ✓（WSL 注入的 Windows PATH ✓） |
| ⑨ | Qt 的 configure **早就启用了** xkbcommon ✓ | `config.summary`：`evdev yes` / `xkbcommon yes` ✓ |
| ⑩ | ⚠ 它用 **dlopen 运行时加载** ✗ ⇒ **NEEDED/符号判据天然失效** ✗ | NEEDED 无它 ✓ 符号只在库自己里 ✓（我因此误判两轮 ✓） |
| ⑪ | 真因是**板上那套 Qt 是旧的** ✗ | 板上 21:45 ✓ vs 重编 21:59 ✓ |
| ⑫ | 出包 → 推包 → 切槽 → 装包 → 回 A ✓ | 哈希两侧一致 ✓、守卫对照 ✓ |
| ⑬ | ★ **坏日志消失 + 三件套 init** ✓✓ | 本轮 ✓ |
| ⑭ | ⚠ **仍待验**：人手点一下输入框看键盘画出 ✗ | uinput 注入**六次四法全打不通** ✗（事件进不了 Qt ✓） |

**⚠ 唯一未验的一步（如实标注 ✓）**：**手指点一下聊天输入框 ⇒ 键盘是否画出** ✗ ——
注入器在这块板上**打不通**（§12.7 五轮 + 本次再试两次 ✓），所以这一条**必须由人在板前确认** ✓。
⚠ 但**代码与镜像已就绪** ✓（运行时证据在 ✓），**不需要再改任何东西** ✓。

**顺带产出的能力（可复用 ✓）**：**A/B 切槽流程双向跑通并验证** ✓ ——
用项目自己的 `next_bcb_for_slot()` + `apply_bcb_to_misc()`（**不手搓 dd** ✓），
先整块备份 ✓、写 `0x800` 与 `0x860` **两处** ✓、读回**逐字节复核** ✓、失败**自动回滚** ✓、
四次 dry-run 逐项校准（签名 ✓、语义 ✓、**槽索引映射经实测**：索引 0 = 槽 a ✓）。


### 12.12 bug ① 重开调查（2026-10-05 晚）：**焦点确认通了，但键盘仍不出** ⚠ —— 以及一个**真实的新原因**

**用户提供的两个决定性观察** ✓（比任何日志都值钱 ✓）：
1. 点聊天输入框 ⇒ **输入框里出现光标/高亮** ✓ ⇒ `QLineEdit` **拿到了焦点** ✓；
2. 点左栏"音乐"图标 ⇒ **页面会切** ✓ ⇒ **触摸链路是通的** ✓。

**据此把范围压到一处** ✓：既然焦点到了 ✓、配置与代码又**逐条核对为真** ✓ ——
`input_source: keyboard` ✓（板上 `config.yaml:551` ✓）、`onboard_auto: true` ✓（`:553` ✓）、
`main_window.cpp:265` 的 connect **无条件** ✓、`onChatInputFocused()`（`:767-776`）**没有别的早退** ✓、
`showOnboard()`（`:666-683`）在 eglfs 下**必然**打 `[ui] 无 X（eglfs）：软键盘交给 Qt 输入法…` ✓ ——
⇒ ⇒ 可 journal 里**这句话一次都没有** ✗（两轮各全量核对一遍 ✓）⇒ ⇒ 那只能是"**跑的二进制不是这份源码**"✗✓。

**★ 找到的真实原因** ✓✓：**板上当时跑的是 845,944 的旧版 GUI** ✗ ——
`strings` 里**找不到** `ChatInput` / `软键盘交给 Qt 输入法` 等串 ✗；而 `/proc/<pid>/exe` 指向的正是一份
`845944 / Oct 5 22:00` 的文件 ✓ ⇒ ⇒ **时序复盘** ✓：
- **21:45** 我部署了 T6 新版（878,712 ✓）—— 当时是对的 ✓；
- **22:13** 我又做了一次 **OTA**（装含 xkbcommon 的新镜像 ✓）⇒ ⇒ **它整块重写了 `system_a`** ✗
  ⇒ **连带把 GUI 覆盖回镜像里的旧版** ✗✗；
- 之后我一直以为"新版已部署"✗ ⇒ ⇒ **此后多轮观察全部发生在旧版上** ✗。
**修** ✓：重新部署 878,712 ✓ ⇒ 已确认在跑 ✓（`[ui] 设置页改动已即时套用` ×4 是**新版特征日志** ✓✓）。

**注入路线（供后来者 ✓，省得再走一遍）** ✓：
- `/dev/input/event3` **不是**触摸屏 ✗（`rk809 Headset` 的 Switch ✓，Qt 明确 `not using input device` ✗）；
  **真触摸屏是 `event2` / `goodix-ts`** ✓（`device is a touch device` ✓、带校准矩阵 `0 -1 1  1 0 0` ✓）；
- `tap` 的 `--device` 指 **uinput 控制节点**（默认 `/dev/uinput` ✓）而**不是**目标 event 设备 ✗ —— 写 event2 会 `UI_DEV_CREATE 失败` ✗；
- 虚拟设备**能被 Qt 接管** ✓（journal：`is tagged by udev as: Touchscreen` ✓、`device is a touch device` ✓、
  **`registerDevice /dev/input/event6 - dsh-virtual-touch`** ✓），**前提是它在 GUI 启动前就存在** ✓
  （Qt5 的 libinput 后端**只在启动时枚举** ✓）；
- ⚠ **但注入的事件始终没到达 Qt** ✗：`qt.qpa.input.events`（**从二进制里挖出的真类别名** ✓，
  定义在 `libQt5EglFSDeviceIntegration.so.5` ✓）在注入期间**一条都没打** ✓ ⇒
  ⇒ **"注册成功 ≠ 事件会被投递"** ✓（卡在 libinput 投递层 ✓，板上无 `evtest`/`libinput debug-events` ✗）。

**⚠ 唯一未由人验证的一步** ✗（如实标注 ✓）：**人手点输入框 ⇒ 键盘是否画出** ✗ ——
现在板上**新版 GUI ✓ + xkbcommon 已修好并在用 ✓ + 诊断日志已开 ✓**，三者**第一次同时在线** ✓，
所以这一次点击**一定能给出三种可判定的结果之一** ✓：
① 键盘弹出 ⇒ 闭环 ✓；② 没弹但 journal 出现 `[ui] 无 X（eglfs）…` ⇒ 焦点通了、问题在 Qt 输入法渲染 ✓；
③ 没弹且**没有**那条 `[ui]` ⇒ 焦点仍没上报 ⇒ 下一步就是**在 `chat_panel.cpp` 的 `eventFilter` 加两行探针** ✓。


### 12.13 bug ① 软键盘（2026-10-05 深夜）：**焦点全通、VK 被要求显示，但面板窗口不出现** ⚠

**用户实测** ✓：点聊天输入框 ⇒ **输入框有光标** ✓（焦点到了 ✓）、左栏**可切页** ✓（触摸通 ✓）⇒ 而**键盘不出现** ✗。

**服务模式 journal 实证（每一步都有行 ✓）**：
```
[ui] 无 X（eglfs）：软键盘交给 Qt 输入法（QT_IM_MODULE=qtvirtualkeyboard，输入框获得焦点）   <-- showOnboard() 走了
qt.virtualkeyboard: PlatformInputContext::setFocusObject(): QLineEdit(0x…, name = "ChatInput")  <-- VK 认了输入框
qt.virtualkeyboard: PlatformInputContext::showInputPanel()                                      <-- VK 被要求显示面板
```
**但项目自带的取证钩子说面板从没出现** ✗（`--focus-input-demo --dump-input`，跑两次一致 ✓）：
```
IM_VISIBLE   0
WINDOW_COUNT 1
WINDOW       QWidgetWindow  0,0 1280x800  visible=1        <-- 只有一个顶层窗口，没有"1280x(高)"的键盘窗口
```
⇒ ⇒ **结论** ✓：链路走到 **`showInputPanel()` 为止全是通的** ✓✓，
**断点只剩一处** ✗：**VK 的 `InputPanel`（独立顶层窗口）显示不出来** ✗。
最可疑：**eglfs 只支持单个全屏窗口** ✗（`WINDOW_COUNT=1` 与它吻合 ✓）——⚠ 但**尚未证实** ✗（见下）。

**本阶段排除掉的（每条都有实测 ✓）**：
| 假设 | 结果 |
|---|---|
| 板上跑的是旧版 GUI | ✅ **成立且已修**（`845944` → 重新部署 `878712`） |
| xkbcommon 缺失 | ✅ **成立且已修**（`Using xkbcommon for key mapping`） |
| `NavButton` 抢输入法焦点 | ❌ **推翻**（VK 日志显示焦点是 `QLineEdit(ChatInput)`） |
| `Layouts/`、`Content/` 缺失 | ❌ **推翻**（Qt 5.15 起布局编译进 `libQt5VirtualKeyboard.so`） |
| 桌面模式（`QT_VIRTUALKEYBOARD_DESKTOP_DISABLE=1`） | ❌ **实测无效**（`IM_VISIBLE` 仍 0） |
| 平台对照（`linuxfb` / `minimal`） | ❌ **无结论**（`linuxfb` core dump；`minimal` 是无头平台，本就不显示） |

**⚠ 下一轮从这里接着走** ✓（两条路，均涉及改 GUI 或查官方配置 ✓）：
1. **把 VK 的 `InputPanel` 嵌入应用自己的窗口** ✓（Qt 在 eglfs 上的官方做法 ✓，需 GUI 代码改动 ✓ ⇒ 先报方案 ✓）；
2. 查 **Qt 官方"eglfs + 虚拟键盘"的推荐配置** ✓（怀疑还缺某个平台/VK 参数 ✓ —— 会话内未找到 ✓）。

**★ 排查工具（务必先看这里 ✓）**：项目**自带**无 X 下软键盘的取证钩子 ✓（`gui/src/main.cpp` 注释原文 ✓）：
- `--focus-input-demo` ✓：启动 1.5s 后把焦点给对话输入框 ✓（"软键盘应当**这时**才弹" ✓）；
- `--dump-input` ✓：逐秒打印 `INPUT_SAMPLE`（输入框文本 ✓）、**`IM_VISIBLE`**（输入法面板可见性 ✓）、
  **`WINDOW`**（每个顶层窗口的类名/几何/可见性 ✓，"键盘窗口会以 **1280×(高度)** 出现" ✓）；
- ⚠ 只有**服务模式**的 `qInfo`/Qt 类别日志会进 **journal** ✗；**手动启动**抓不到（T14-8 自管会话日志 ✗）⇒
  要看日志就用 **systemd drop-in 覆盖 ExecStart** ✓（可逆 ✓，本次用过 ✓）；
- ⚠ `qt.virtualkeyboard` 这个**日志类别名是从二进制里挖出来的** ✓
  （`strings libqtvirtualkeyboardplugin.so | grep qt.` ✓）—— 一开就能直接看到 `setFocusObject()` 认的是谁 ✓✓。


### 12.14 bug ① 软键盘：**根因与修法都已板上实证** ✓（2026-10-05 深夜）

**官方依据** ✓（Qt 5.15 Deployment Guide，*Integration Method* 一节）：
Qt 虚拟键盘有两种集成方式 ——
- **`Desktop`**：「the keyboard is shown in a **dedicated top-level window**」（无需改应用 ✓）；
- **`Application`**：「embedded within the Qt application itself by instantiating an **`InputPanel`** item in QML」✓，
  并且原文明确：「**This method is mandatory in environments where there is no support for multiple
  top-level windows (such as embedded devices)**」✓✓。

⇒ ⇒ **eglfs 正是"不支持多顶层窗口"的环境** ✗ ⇒ **必须用 Application 集成** ✓。
这**完美解释**了此前的全部观测：`setFocusObject(QLineEdit/ChatInput)` ✓ 与 `showInputPanel()` ✓ 都正常发生，
**但面板窗口建不出来** ✗（`IM_VISIBLE 0` ✗、`WINDOW_COUNT 1` ✗）——
因为 Desktop 集成要开**第二个顶层窗口**，而 eglfs 开不出来 ✓。
（也解释了 `QT_VIRTUALKEYBOARD_DESKTOP_DISABLE=1` 为何**无效** ✗：官方说它只用于**覆盖桌面环境**的选择 ✓，
  对 eglfs 这种非桌面环境不适用 ✓。）

**板上实证** ✓✓（这是决定性的）：写一个**最小的 Application 集成 QML** ✓
（`TextInput` ＋ **`InputPanel`**，照官方 *Creating InputPanel* 示例 ✓），用**板上自带的 `qmlscene`** 跑 ✓
⇒ ⇒ ★ **键盘完整显示出来了** ✓✓（截图存证：`E:\rk3568\tmp\gd-shots\vk-min.png` ✓ ——
四排 QWERTY 键位 ✓、`&123` ✓、语言键显示 **"American English"** ✓）。
⇒ 与之前"`showInputPanel()` 调了却没窗口"✗ 形成对照 ✓ ⇒ **根因与修法同时确证** ✓✓。

**前提核查** ✓：
- 板上 **`libQt5QuickWidgets.so.5.15.11`（87,840 B）在** ✓✓ ⇒ **不需要重建 Qt、不需要改镜像** ✓；
  板上还有 `libQt5Quick` / `QuickControls2` / `QuickParticles` / `QuickShapes` / `QuickTemplates2` ✓
  与 `/usr/bin/qmlscene` ✓、`QtQuick/VirtualKeyboard/qmldir` ✓（其 depends：QtQuick 2.0 / QtQuick.Window 2.2 /
  QtQuick.Layouts 1.0 / Qt.labs.folderlistmodel 2.1 ✓ —— 最小验证成功即证明它们都在 ✓）；
- ⚠ **宿主机没有 QuickWidgets** ✗（lib / headers / cmake 三处均无 ✓），而交叉编的 sysroot **有** ✓
  （`…/sysroot/usr/include/qt5/QtQuickWidgets` ✓ ＋ `libQt5QuickWidgets.so` ✓）
  ⇒ ⇒ 要保住"**宿主 0 error + 全量 ctest**"这条纪律 ✓，就得在**开发机**上装 `libqt5quickwidgets5-dev` ✓
  （**开发机依赖** ✓，不是产品改动 ✗）。

**待批准的方案（A1）** ✓：GUI 仍是 Qt Widgets ⇒ 加一层 QML 承载 ✓ ——
① 宿主装 `qtquickwidgets5-dev` ✓；② 新增只含 `InputPanel` 的 QML ✓（**做成 `.qrc` 资源** ✓ 编进二进制 ✓，
部署不用装文件 ✓）；③ `CMakeLists.txt` 链接 `Qt5::QuickWidgets` ✓；④ 在 **eglfs 分支**里创建 `QQuickWidget` 承载它 ✓
（有 X 时仍走原 onboard 路径 ✓ 语义不变 ✓）；⑤ `applyKeyboardInset()` 接 `Qt.inputMethod.visible` ✓；
⑥ 判据 + **牙齿** ✓（注掉嵌入 ⇒ 必红 ✓；ctest 35 → 36 ✓）；
⑦ 交叉编 ⇒ 部署（`.new` + `mv -f` ✓ ＋ 核对大小/特征串 ✓）⇒ **上板验收** ✓。
