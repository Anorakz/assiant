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
# 板端依赖：Qt 5.12（本来就有）+ yaml-cpp（日程区读配置用，只读）
sudo apt-get install -y libyaml-cpp-dev

cd gui && cmake -S . -B build && cmake --build build -j4
./build/agent_gui                       # 自动向上找 config/config.yaml，全屏 kiosk
./build/agent_gui --config /tmp/g/config.yaml   # 显式指定配置（验收/沙箱用）
./build/agent_gui --windowed --page system
./build/agent_gui --socket /tmp/agent.sock
./build/agent_gui --stdio               # 纯终端模式（e2e_ipc 测试依赖）
```

常用验收参数：`--screenshot <png> --screenshot-delay <ms>`、`--video <文件>`、
`--chat-demo <文本>`、`--input-type-demo <terminal|keyboard>`、`--focus-input-demo`
（取证：软键盘应在这时才弹）、`--model-mode-demo <mode>`、
`--bench-demo <qwen_precheck|qwen_full|multimodal>`、`--report-demo`、`--settings-save-demo`、
`--dump-schedule`（打印日程区真实渲染的行）等。

测试：`cd gui/build && ctest --output-on-failure`（**19 个测试**：核心逻辑 + 控件级 + 图标守卫 + e2e IPC）。

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
`DSI-1 connected 1280x800+0+0 left` —— 所以 **X 桌面与全屏 kiosk 窗口都是 1280×800**
（`xwininfo` 实测窗口 `1280x800+0+0`）。右区域是 320 × 约 700，日程区实得约 200px
（正好放下"标题 + 副标题 + 6 行 + 提示行"）。
⚠ `/sys/class/graphics/fb0/virtual_size` 报的 `800,1280` 是**面板模式**，不是给应用用的
逻辑尺寸 —— 拿它推布局会错。

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
| `gui.theme` | GUI | 高级灰（当前仅一档） |
| `gui.fullscreen` / `gui.start_page` / `gui.debug` | GUI | 启动形态 / 默认页 / Debug 日志 |
| `gui.wake.top/bottom/left/right` | GUI | 四区域 `active` 或 `locked` |
| `gui.wake.idle_ms` | GUI | **四区域共用**的休眠时间 |
| `gui.video_overlay.mode` / `gui.video_overlay.idle_ms` | GUI | 视频内嵌控制条的活动/锁定与**独立**休眠时间 |
| `gui.chat_channel` / `gui.input_source` / `gui.onboard_auto` | GUI | 对话通道 / 输入源（`keyboard`｜`terminal`）/ 是否真控 onboard（**点输入框才弹**，失焦收起） |
| `gui.monitor_interval_ms` | GUI | 系统页刷新间隔 |
| `gui.schedule.max_rows` | GUI | 日程区最多显示几行（今天+明天**合计**，默认 6） |
| `llm.*` | GUI 写、Agent 读 | 推理位置（`edge`／`cloud`／`disabled`）与参数 |
| `scheduler.recurring` / `scheduler.oneoff` | Agent 触发、**GUI 只读展示** | 日程本身（GUI 读它画日程区，见下） |

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
config/config.yaml  ──ConfigSyncer──▶  llm/config/llm.env      （喂 llama-server）
   （唯一真源）                         （派生文件，不是真源）
```

`llm.env` 里只有 8 个键可推导：`LLM_MODEL_PATH`、`LLM_MODEL_NAME`、`LLM_PORT`、
`LLM_CTX_SIZE`、`LLM_BATCH_SIZE`、`LLM_THREADS`、`LLM_THREADS_BATCH`、`LLM_API_KEY`。
其余布局类键（`LLM_HOST`、`LLM_LOG_DIR`、`LLM_RUN_DIR`、`LLM_PID_FILE`、`LLM_LOG_FILE`）
由板端自己维护，派生**不碰**。手动跑一次：

```bash
./build/gui_config_sync                # 只打印将要发生的 diff（默认 dry-run）
./build/gui_config_sync --apply        # 真写 llm/config/llm.env（留 .bak + 原子 rename）
```

⚠ 反向不成立：手改 `llm.env` 会在下一次「保存并同步」时被 `config.yaml` 覆盖。
完整约定见 [`config-sources.md`](config-sources.md)。

## 4. 各页现状（含占位，方案 §7 一律"可见地不能当真"）

| 页 | 已实现 | 占位（点了给说明，不发协议） |
|---|---|---|
| 主页面 | 模式切换、对话（chat_input/llm）、音乐条、壁纸（**只画 Agent 推来的 `wallpaper`** —— 换壁纸只走对话，T7-3 起主区没有「下一张」按钮了；目录来自配置的 `wallpaper.dir`）、视频（本地文件播放/暂停/全屏/下一集）、输入源二选（键盘 onboard / 命令行）、**日程区**（只读展示 `scheduler` 段：**接下来 24 小时**、`时间 + 状态`、窗口终点写在副标题） | 歌词、歌手、专辑、进度、上一集、倍速、B站封面 |
| 模型测试 | 推理位置三选、本地 GGUF 下拉与参数、云端参数、配置保存与同步、服务脚本启停与日志、基准测试（预检/全量/多模态）、停止测试、最新报告 | SigLIP 固定只读块 |
| 系统 | CPU/内存/NPU 负载/频率/温度/网络 IP/串流主机，按 `monitor_interval_ms` 刷新 | 看门狗启停 |
| 设置 | debug、四区域活动锁定与共用休眠、视频控制条活动锁定与独立休眠、启动形态、默认页、默认输入类型、配置路径、恢复默认、关于 | 主题仅一档 |

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
