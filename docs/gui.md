# 板端助手 GUI（Qt5 Widgets）说明

> 本文对应 `gui/` 目录的实现；方案与任务分解见 `docs/gui-qt5-plan.md`、`docs/gui-qt5-tasks.md`。
> 协议（GUI ⇄ Agent）以 `docs/ipc-protocol.md` 为唯一真源。

## 1. 跑起来

首次运行：仓库只带 `gui/config/gui.yaml.example`，先把模板复制成实际配置
（运行期页面会写这份文件，所以它不入库）：

```bash
cp gui/config/gui.yaml.example gui/config/gui.yaml
```

```bash
cd gui && cmake -S . -B build && cmake --build build -j4
./build/agent_gui                       # 读 gui/config/gui.yaml，全屏 kiosk
./build/agent_gui --windowed --page system
./build/agent_gui --socket /tmp/agent.sock
./build/agent_gui --stdio               # 纯终端模式（e2e_ipc 测试依赖）
```

常用验收参数：`--screenshot <png> --screenshot-delay <ms>`、`--video <文件>`、
`--chat-demo <文本>`、`--input-type-demo <terminal|keyboard>`、`--model-mode-demo <mode>`、
`--bench-demo <qwen_precheck|qwen_full|multimodal>`、`--report-demo`、`--settings-save-demo` 等。

测试：`cd gui/build && ctest --output-on-failure`（**16 个测试**：核心逻辑 + 控件级 + e2e IPC）。

## 2. 界面结构

```
┌ 上区域 通栏 72px：●三态连接 · 模式徽标 · 时钟 ────────────────────────┐
├ 左 96px 导航 ┤ 页面栈：主页面 / 模型测试 / 系统 / 设置                  │
│ （设置下沉）  │                                                        │
└──────────────┴────────────────────────────────────────────────────────┘
主页面内部：主区（非游戏=留给壁纸 / 游戏=视频区）+ 下区域（音乐条 / B站封面）
             右区域：上=模式按钮区，下=对话区（含输入行 + 输入源小按钮）
```

- **唤醒机制**：四区域各自 `active`（空闲折叠，点一下出现）或 `locked`（常显）；
  任一区域的点击都会唤醒全部；隐藏区域设 `WA_TransparentForMouseEvents`，150ms 滑动。
- **视频内嵌控制条**有自己的 `active/locked` 与**独立的** `idle_ms`（不与区域共享）。

## 3. 配置（`gui/config/gui.yaml` 是唯一真源）

| 键 | 含义 |
|---|---|
| `theme` | 高级灰（当前仅一档） |
| `fullscreen` / `start_page` / `debug` | 启动形态 / 默认页 / Debug 日志 |
| `wake.top/bottom/left/right` | 四区域 `active` 或 `locked` |
| `wake.idle_ms` | **四区域共用**的休眠时间 |
| `video_overlay.mode` / `video_overlay.idle_ms` | 视频内嵌控制条的活动/锁定与**独立**休眠时间 |
| `chat_channel` / `input_source` / `onboard_auto` | 对话通道 / 输入源（`keyboard`｜`terminal`）/ 是否真控 onboard |
| `monitor_interval_ms` | 系统页刷新间隔 |
| `llm.*` | 推理位置（local/cloud/disabled）与参数；由模型测试页写入并同步 |

同步链路：模型测试页 → `gui.yaml` → `ConfigSyncer` → `llm/config/llm.env` 与 `config/config.yaml`
（文本级键替换，保留注释与顺序）。设置页/输入源只写 `gui.yaml`。

## 4. 各页现状（含占位，方案 §7 一律"可见地不能当真"）

| 页 | 已实现 | 占位（点了给说明，不发协议） |
|---|---|---|
| 主页面 | 模式切换、对话（chat_input/llm）、音乐条、壁纸（wallpaper + 下一张）、视频（本地文件播放/暂停/全屏/下一集）、输入源二选（键盘 onboard / 命令行） | 歌词、歌手、专辑、进度、上一集、倍速、B站封面 |
| 模型测试 | 推理位置三选、本地 GGUF 下拉与参数、云端参数、配置保存与同步、服务脚本启停与日志、基准测试（预检/全量/多模态）、停止测试、最新报告 | SigLIP 固定只读块 |
| 系统 | CPU/内存/NPU 负载/频率/温度/网络 IP/串流主机，按 `monitor_interval_ms` 刷新 | 看门狗启停 |
| 设置 | debug、四区域活动锁定与共用休眠、视频控制条活动锁定与独立休眠、启动形态、默认页、默认输入类型、配置路径、恢复默认、关于 | 主题仅一档 |

## 5. 图标

`gui/resources/icons/*.svg`（**22 个自绘单色描边图标**）+ `icons.qrc`，
`ui::icon(name)` / `ui::tintedIcon(name, color)` 按需染色（`CompositionMode_SourceIn`）。
好处：不再依赖字体码位（板端 `⏸`/`⏮` 等码位没有字体覆盖，会被画成豆腐块）。

## 6. 验收取证约定

- 每个任务：**零警告构建 + `ctest` 全绿 + 真机截图**，证据写进 `temp/T<任务>_evidence.md`，
  截图放 `temp/t<任务>/`（`temp/` 在 `.gitignore` 里，不入库）。
- 视频画面 `grab()` **抓不到**（原生视频表面）→ 视频相关证据一律用 `scrot` 抓屏。
- 会接管 `llama-server` 的操作（全量基准、服务启停）在生产服务运行时**不要**执行；
  验收脚本用临时仓库 + 替身脚本。
