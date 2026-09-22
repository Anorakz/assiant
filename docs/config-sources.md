# 配置来源：谁写、谁读、谁派生

> 归一化 D 系列定下的规则。要改这套关系，先改本文，再改代码。
> 部署侧规则见 [`deploy.md`](deploy.md)；GUI 的界面参数见 [`gui.md`](gui.md)。

## 1. 一句话

**`config/config.yaml` 是唯一的配置真源。** Agent 只读它；GUI 读它的 `gui:` 段、
读写它的 `llm:` 段、**只读**它的 `scheduler:` 段（画日程区，见 §5）；
喂 llama-server 的 `llm/config/llm.env` 是**派生**文件，不是真源。

```
                    ┌───────────────────────────┐
                    │  config/config.yaml       │   ← 唯一真源（板端本地，不入库）
                    │    llm:       推理位置与参数 │
                    │    gui:       界面参数      │
                    │    scheduler: 日程（只读展示）│
                    │    sunshine / ipc / …      │
                    └───────┬───────────┬───────┘
         只读（llm/scheduler）│           │  读写（gui: 读、llm: 读+写）
                    ┌───────▼───┐   ┌───▼────────────────────┐
                    │  Agent    │   │  GUI (Qt5, 板端)        │
                    │ agent/    │   │  gui/src/**             │
                    └───────────┘   └───┬────────────────────┘
                                        │ 派生（ConfigSyncer，8 个 LLM_* 键）
                                        ▼
                              llm/config/llm.env    ← 派生文件（板端本地，不入库）
                                        │
                                        ▼
                              llama-server 进程
```

## 2. 三份文件各是什么

| 文件 | 角色 | 进 git？ | 谁写 | 谁读 |
|---|---|---|---|---|
| `config/config.yaml` | **唯一真源** | 否（只提交 `config.example.yaml`） | 人 / GUI（设置页、模型测试页） | Agent、GUI |
| `llm/config/llm.env` | **派生**（喂 llama-server） | 否 | `ConfigSyncer`（GUI 保存时、或 `gui_config_sync` CLI） | llama-server 启动脚本 |
| `config/config.example.yaml` | 模板 | **是** | 人 | 人（`cp` 起步） |

`llm.env` 里可推导的只有 8 个键：`LLM_MODEL_PATH`、`LLM_MODEL_NAME`、`LLM_PORT`、
`LLM_CTX_SIZE`、`LLM_BATCH_SIZE`、`LLM_THREADS`、`LLM_THREADS_BATCH`、`LLM_API_KEY`
（映射表在 `gui/src/core/config_sync.cpp` 的 `envTargetKeys()`）。
其余布局类键 —— `LLM_HOST`、`LLM_LOG_DIR`、`LLM_RUN_DIR`、`LLM_PID_FILE`、`LLM_LOG_FILE`
—— 由板端自己维护，派生**不碰**。

## 3. 单向性（最容易踩的一条）

派生是**单向**的，所以：

- 手改 `llm/config/llm.env` **没用**：下一次「保存并同步」会按 `config.yaml` 把它覆盖回去。要改端口/线程/上下文，
  改 `config/config.yaml` 的 `llm:` 段。
- 反向读也不行：Agent **不许**读 `llm.env`。它是给 llama-server 用的进程环境，
  不是应用配置。
- 于是"改了不生效"只剩一种原因：改错了文件。先看真源。

## 4. Agent 只认一份配置

`agent/config.py` 加载的就是 `config/config.yaml`（`--config` 可覆盖路径），
没有第二个入口。`agent/` 里出现 GUI 专用配置名或 `llm.env` 就是回归 ——
`tests/test_config_source_guard.py` 会直接变红。

## 5. GUI 也读 `scheduler:` 段（只读，且只保证到夹具覆盖的范围）

GUI 的**日程区**显示的内容来自 `config.yaml` 的 `scheduler.recurring` / `scheduler.oneoff`
—— 与 Agent 的 `Scheduler._load_events()` **同一处**，连"某个键在 scheduler 段里找不到就
**逐键**回落到顶层"这条都一致。GUI 不写这一段，也不新增 IPC topic。

| | 谁 | 做什么 |
|---|---|---|
| 触发 | Agent（`agent/core/scheduler.py`） | 按 `window_min` / `late_grace_min` / `remind_before_min` 真正触发日程动作 |
| 展示 | GUI（`gui/src/core/schedule_model.cpp`） | 只读同一段，展开成"今天 / 明天"两段给日程区 |

**代价（写清楚，别当它是免费的）**：这等于在 C++ 里镜像了一份 Python 的日程语义
（`parse_clock` / `_weekday_index` / `date.fromisoformat` / `occurs_on`）。
镜像会漂，所以有两条守卫互为表里：

| 守卫 | 在哪 | 红了说明 |
|---|---|---|
| `tests/test_schedule_parity.py` | Python（三端都跑） | **Python 语义变了**：它用真的 `Scheduler` 重新生成期望，与入库的 `tests/data/schedule_parity/*.expect.json` 对不上 |
| `gui/tests/test_schedule_model.cpp::parityWithPythonFixtures` | C++（板端 ctest） | **C++ 镜像变了**：读同一份夹具 + 同一份期望，行级比对不一致 |

**保证边界**：只承诺"夹具覆盖到的写法两边等价"。夹具之外（锚点、显式 `!tag`、复杂 flow、
不同 Python 版本对日期宽容度不同的写法如 `20260920`）**不承诺**。
夹具里的写法是两边都能读的那一档；改动语义时要**同时**改 Python、C++ 镜像、本文件，
然后重新生成期望：

```bash
# ⚠ 在**板端**生成（python3.8 = Agent 真正跑的解释器；3.11+ 的 fromisoformat 更宽松）
ssh rk3568 'cd /home/kickpi/myproject/assitant && python3 tests/test_schedule_parity.py --write'
```

> ✅ **已删除（L1）**：仓库里那份 `config/schedule.example.yaml` 已经不在了 —— 它是历史遗留，
> **全仓没有任何代码读它**（`Scheduler._load_events()` 只从 `config.yaml` 的 scheduler 段/顶层找日程）。
> 里面唯一有价值的东西是**事件语法**，已并入 `config/config.example.yaml` 的 `scheduler:` 段注释，
> 且**只写真的会被读的键**（`title` / `days` / `start` / `end` / `date` / `remind_before_min` / `action`）。
> 它原有的 `timezone` / `defaults.remind_before_min` / `location` **从来没有读取者**，
> 所以没有替代物 —— 也不要再往那儿加。日程的**家**只有 `config.yaml` 的 `scheduler` 段。

## 6. 历史（为什么会变成这样）

归一化之前有三份配置：`config/config.yaml`（Agent）、GUI 自己那份界面配置、
`llm/config/llm.env`（那时还能手改）。GUI 的模型测试页一次写三份，Agent 只认一份，
所以"界面上改了参数，运行时不生效"。D 系列把界面参数并进 `config/config.yaml` 的
`gui:` 段、把 `llm.env` 降为纯派生产物，GUI 侧那份配置文件连同模板一起删除。

## 7. 常见操作

```bash
# 起一份自己的配置（新环境 / 沙箱验收）
cp config/config.example.yaml config/config.yaml

# 只预览"真源 → llm.env"会发生什么（默认 dry-run，不写文件）
gui/build/gui_config_sync

# 真写（留 .bak + 原子 rename）
gui/build/gui_config_sync --apply

# 指定别的路径（验收用临时仓库）
gui/build/gui_config_sync --config /tmp/g/config.yaml --env /tmp/g/llm.env --apply
```

GUI 保存配置时会写 `config/config.yaml`，并在它旁边留一份 `.bak`
（`*.bak` 已被 `.gitignore` 覆盖，不会污染 `git status`）。

**`config/config.yaml` 是板端本地文件**：`deploy.ps1` 只送已提交的内容，
`git archive` 里没有它，所以重新部署**不会**覆盖板端正在用的配置。
