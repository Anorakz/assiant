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

测试：`cd gui/build && ctest --output-on-failure`（**25 个测试**：核心逻辑 + 控件级 + 图标守卫
+ 面板尺寸守卫 `test_page_heights` + 虚拟键盘让位守卫 `test_keyboard_inset` + e2e IPC）。

### 1.0 GUI 的门禁到底在哪（T15-4 任务 12 实测改正）

旧版这里写的是"GUI 只能在**板端**编（PC 没有 Qt）"—— **这条是错的**，2026-10-04 两条都实测过：

| 路 | 命令 | 实测 |
| --- | --- | --- |
| **A. 宿主原生**（快，日常用这条） | `apt-get install qtmultimedia5-dev` 后 `cmake -S gui -B build-gui-host -DGUI_BUILD_TESTS=ON && ctest --test-dir build-gui-host` | 宿主 Qt **5.15.13**（与镜像的 5.15.11 同小版本）：配置 + 编译 rc=0，**25/25 全过**（15 s） |
| **B. PC 交叉 + 板端跑**（发布前的真门禁） | `bash image/build-gui.sh --target <target 树>`（它固定 `GUI_BUILD_TESTS=OFF`）；要跑测试就照抄它的 cmake 参数、把 `-DGUI_BUILD_TESTS=ON`，再把 `tests/test_*` 拷到板上执行 | 板端（aarch64，Qt 5.15.11 运行时 + `libQt5Test` + `libqoffscreen.so`）：**24 个测试二进制、278 项通过**（明细与两处板端环境差异见 §1.0.1） |

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
