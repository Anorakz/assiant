# 配置来源：谁写、谁读、谁派生

> 归一化 D 系列定下的规则。要改这套关系，先改本文，再改代码。
> 部署侧规则见 [`deploy.md`](deploy.md)；GUI 的界面参数见 [`gui.md`](gui.md)。

## 1. 一句话

**`config/config.yaml` 是唯一的配置真源。** Agent 读它，并且**只在一件事上写它**：
`remove_fired_oneoff` 打开时，把**已经触发过的一次性日程**从 `oneoff:` 里删掉（§3.1）；
GUI 读它的 `gui:` 段、读写它的 `llm:` 段、**只读**它的 `scheduler:` 段（画日程区，见 §5）；
喂 llama-server 的 `llm/config/llm.env` 是**派生**文件，不是真源。

```
                    ┌───────────────────────────┐
                    │  config/config.yaml       │   ← 唯一真源（板端本地，不入库）
                    │    llm:       推理位置与参数 │
                    │    vision:    离线打标签用的  │
                    │               SigLIP 模型路径 │
                    │    gui:       界面参数      │
                    │    wallpaper: 壁纸目录      │
                    │    scheduler: 日程（谁读都行 │
                    │               写只允许删已触发的 oneoff）│
                    │    sunshine / ipc / …      │
                    └───────┬───────────┬───────┘
     只读 / 删已触发的 oneoff │           │  读写（gui: 读、llm: 读+写）
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
| `config/config.yaml` | **唯一真源** | 否（只提交 `config.example.yaml`） | 人 / GUI（设置页、模型测试页）/ **Agent（只在"删掉已触发的一次性日程"这一件事上，见 §3.1）** | Agent、GUI |
| `config/wall_data.jsonl` | **派生数据**（第一行 = 标签向量缓存，其后一行一张图: 标签 + 图像向量 + 使用次数，Phase 7 T7-2 / T8-6） | 否（`.gitignore` 里单列一行） | `agent/vision/wall_data.py`（唯一写者）: `assistant tag --apply` 打标签，以及**运行期换壁纸时给那一张 `used` +1** | Agent（挑图/检索/按用量挑）、`assistant tag` 自己（算增量 + 复用词表向量） |
| `config/music_library.jsonl` | **本地数据**（一行一首歌: id + tags + 播放次数，Phase 7 T8-3） | 否（`.gitignore` 里单列一行） | `agent/media/music_library.py`（唯一写者）: `assistant music` 导入/打标、运行期"听满 30 秒计一次"、以及**T10-4 自动补歌把搜到的歌登记进库**（库会因此长大，见 [`music.md`](music.md) §4.4） | Agent（挑歌/**补歌**）、`assistant music` |
| `config/user_profile.jsonl` | **本地数据**（一次构建一行: IP/歌手**权重** + 心情 + 清零记录，Phase 7 T9-2） | 否（`.gitignore` 里单列一行） | `agent/core/user_profile.py`（唯一写者）: Agent 在"纯对话攒到 2000 字"时构建（T9-3） | Agent 自己（**T10 起真在消费**: 挑下一个壁纸 / 补歌 / 判负反馈 / 心情变了重置队列，见 [`profile.md`](profile.md) §8）；**模型看不到**（不是工具） |
| `config/netease_cookie.json` | **T8-1 的保险条目**（板端**不放** cookie；登录态住在 PC 上 neteasecli 自己的 store） | 否（`.gitignore` 里单列一行） | 谁都不写（T8-7 核对过: 代码里没有任何地方读它） | — |
| `config/bilibili_cookie.json` | **凭据**（B 站 `SESSDATA`，Phase 7 T11）：有它 DASH 才能到 **1080P**，没有就只有单文件的 360P~720P | 否（`.gitignore` 里单列一行） | **人**（手写；键名 `SESSDATA`，`SEESSDATA` 这种笔误代码会认下来并提醒） | `agent/net/bilibili_api.py`（**只读**，空/缺 = 匿名） |
| `config/game_anchors.jsonl` | **派生数据**（一行一锚点: 游戏名 + 768 维 float16 向量 + 截图路径 + 来源 + 时间，T11-4） | 否（`.gitignore` 里单列一行；截图目录 `config/game_anchors/` 一并忽略） | `agent/core/game_anchors.py`（唯一写者）: 画面与 PC 进程**不一致时把那一帧登记成该游戏的锚点**（自纠错） | `agent/core/game_watch.py`（识别时算余弦） |
| `llm/config/llm.env` | **派生**（喂 llama-server） | 否 | `ConfigSyncer`（GUI 保存时、或 `gui_config_sync` CLI） | llama-server 启动脚本 |
| `config/config.example.yaml` | 模板 | **是** | 人 | 人（`cp` 起步） |

> ⚠ `config/` 下现在有**三类**东西：**真源**（`config.yaml`，人/GUI 写）、**派生/本地数据**
> （`wall_data.jsonl`、`music_library.jsonl`、`user_profile.jsonl`、`game_anchors.jsonl`，机器写）、
> **凭据**（`netease_cookie.json` 那个空保险 + T11 起真的要用的 `bilibili_cookie.json`）。
> 别因为"都在 config 目录里"就以为都能手改 —— 手改 `wall_data.jsonl` **基本没意义**
> （下次打标签或换壁纸会覆盖那一行；只有 `used`/`last_used` 是运行期真的会被改的字段，
> 想清零就直接删文件重打标签，见 [`tagging.md`](tagging.md) §6.1）；
> `music_library.jsonl` 手改**有意义**（它就是"我的本地歌单"，格式见 [`music.md`](music.md)）。
> `game_anchors.jsonl` **可以补**，但别手写：一行里的向量是 **768 维 float16 的 base64**
> （`game + vector + shot + source + when`），照格式**用脚本**加（T11-0 标定时就是这么填的）；
> 运行期 Agent 也会自己往里加（画面与进程不一致时），见 [`bilibili.md`](bilibili.md) §3）。

`llm.env` 里可推导的只有 8 个键：`LLM_MODEL_PATH`、`LLM_MODEL_NAME`、`LLM_PORT`、
`LLM_CTX_SIZE`、`LLM_BATCH_SIZE`、`LLM_THREADS`、`LLM_THREADS_BATCH`、`LLM_API_KEY`
（映射表在 `gui/src/core/config_sync.cpp` 的 `envTargetKeys()`）。
其余布局类键 —— `LLM_HOST`、`LLM_LOG_DIR`、`LLM_RUN_DIR`、`LLM_PID_FILE`、`LLM_LOG_FILE`
—— 由板端自己维护，派生**不碰**。

### 2.1 `llm:` 段不只是"给 llama-server 用的"（T2 起）

`edge` 模式接上真模型之后，Agent **自己也读**这一段里的几个键 —— 它要去连本机的
llama-server。这张表是"哪个键有第二个读者"的唯一说明（改键名时两边都要改）：

| 键 | Agent（`agent/llm/provider.py::EdgeBackend.from_config`）怎么用 | llama-server（派生 `llm.env`） |
| --- | --- | --- |
| `port` | `base_url = http://127.0.0.1:<port>/v1` | `LLM_PORT` |
| `model_name` | 请求里的 `model` | `LLM_MODEL_NAME` |
| `local_api_key` | 请求带的 key（**不是**云端的 `api_key`） | `LLM_API_KEY` |
| `max_tokens` | 每次请求的 `max_tokens` | 不用 |
| `temperature` | 每次请求的 `temperature` | 不用 |
| `timeout_s` | 单次请求超时（edge 与 cloud 共用） | 不用 |
| `model_path` | **只打进日志**，不读文件 | `LLM_MODEL_PATH` |
| `ctx_size` / `batch_size` / `threads` / `threads_batch` | 不读 | 对应 4 个 `LLM_*` |

读不到的键各自退回默认值（`port=9000`、`model_name=qwen3-0.6b`），**不抛异常** ——
配置写漏了不该让 Agent 起不来。⚠ 但"能发请求"≠"llama-server 活着"：
`EdgeBackend.is_ready()` 只查配置与 SDK，**不联网**；真活着的证据是一次成功的请求。

**T7-4 新增的 `manage_service`（谁**写**这个进程的生命周期）**：

| 键 | 谁读 | 行为 |
| --- | --- | --- |
| `manage_service` | `agent/llm/service.py::LlamaService.from_config`（`agent/main.py` 装配） | `false`（默认）= Agent **不碰** llama-server 进程；`true` 且 `mode=edge` 时：Agent 启动 / 离开 SLEEP → `llm/scripts/start.sh`，进入 SLEEP → `llm/scripts/stop.sh`（Agent 退出**不停**） |

⚠ 这条改的是**进程**, 不是配置：Agent 只会去调 `llm/scripts/` 里那两个脚本
（与 GUI 的「启动服务」按钮同一条路），脚本本身仍由人/GUI 用同一份 PID 文件与日志管理。
`mode` 不是 edge 时这个开关无效。

### 2.2 `vision:` 段只给**离线**打标签用（T7-1）

| 键 | 谁读 | 说明 |
| --- | --- | --- |
| `model_path` | `agent/vision/siglip/`（经 `assistant tag` / 检索） | SigLIP 双塔 rknn；**Agent 启动路径不加载它** |
| `tokenizer_path` | 同上 | `tokenizer.json`（板端需 `tokenizers==0.20.3`） |
| `runtime_lib` | 同上（只记进日志/报错） | 实测需 librknnrt ≥ 1.6.0（model version 6） |
| `verbose` / `warmup_runs` | 同上 | rknn 日志开关 / 首帧预热次数 |

⚠ **模型契约不在这一段里**：256×256、nhwc、uint8 原始 0-255、64 token、pad=1、768 维、
L2 归一化、只能按余弦 —— 全部写死在 `agent/vision/siglip/config.py` 的常量里。
那些值改错**不报错、只会静默变笨**，所以刻意不给配置入口（见 `architecture.md` §4.3）。

### 2.3 `profile:` 段给"用户画像"用（T9）

| 键 | 谁读 | 说明 |
| --- | --- | --- |
| `enabled` | `agent/main.py::_start_profile` | 关掉就什么都不做（与其它子系统的口径一致） |
| `trigger_chars` | 同上（默认 2000） | **自上次构建以来**纯对话攒到这么多字就构建一次 |
| `trigger_turns` | 同上（默认 12） | 兜底：短消息太多时按轮数触发 |
| `file` | `agent/core/user_profile.py` | 画像落盘路径，默认 `config/user_profile.jsonl`（相对路径按仓库根） |

⚠ 这一段的**消费者只有 Agent 自己**：模型看不到画像（它不是工具），GUI 也不读它。
**T10 起 Agent 会消费画像**（挑"下一个"壁纸 / 补歌 / 判负反馈 / 心情变了重置队列）——
但那是**读画像文件**，不是读这一段配置：这三个消费者**不从这里拿任何键**，
权重配方与心情→标签的映射都在 `agent/core/user_profile.py` 的常量里
（消费者清单见 [`profile.md`](profile.md) §8）。
⚠ `profile.md` 里有三路口径（权重配方 / 两层清零 / 心情那次模型调用）的完整说明。

⚠ **壁纸那三格窗口（`prev`/`current`/`next`）没有配置键**（T10-3 定的）：窗口是
**进程内状态**，不落盘、可配的只有"排队时用什么画像权重"（写死在 `user_profile.py`）。
同理，**音乐队列的目标长度在 `music.autofill` 段**（那张表在 [`music.md`](music.md) §5），
不在 `profile:` 里 —— 画像只说"喜欢谁"，"补到几首"是音乐自己的配置。

### 2.4 `bilibili:` 段给 B 站视频用（T11）

| 键 | 谁读 | 说明 |
| --- | --- | --- |
| `enabled` | `agent/net/bilibili_api.py::from_config`（`agent/main.py::_start_bilibili` 装配） | `false` → 工具整个不装、队列/缓冲/观察器都不起 |
| `cookie_file` | 同上 | **凭据**（`config/bilibili_cookie.json`，相对路径按仓库根）；空/缺 = 匿名 360P |
| `timeout_s` | 同上 | 单次 HTTP 超时 |
| `queue.viewport_fallback` / `queue.max` | `agent/main.py::_start_bilibili` → `BilibiliQueue` | 预览栏还没上报格数时的兜底 / 窗口硬上限（目标 = 3×格数） |
| `buffer.dir` / `initial_s` / `max_s` / `mem_watermark_mb` | `agent/core/bilibili_buffer.py` | FIFO 目录 / 起播门槛 / 暂停时的封顶 / 内存水位 |
| `game_watch.enabled` / `interval_s` / `confident_score` / `confident_margin` | `agent/core/game_watch.py` | 双路识别：多久认一次、"有把握"的门槛 |
| `game_watch.anchor_file` | `agent/core/game_anchors.py` | 锚点索引（**派生数据**，见 §2 表） |
| `game_watch.process_names` | `agent/net/pc_probe.py` | **进程名 → 游戏名**的映射；ssh 参数复用 `music:` 段（同一台 PC） |
| `game_watch.mem_watermark_mb` | `agent/core/game_watch.py` | 加载/保持 SigLIP 前的 `MemAvailable` 检查 |

⚠ 这一段的消费者**只有 Agent**：GUI 不读它（界面上的东西都是 Agent 推过去的 topic），
模型也读不到它。逐键注释与实测数字见 [`bilibili.md`](bilibili.md)（§4 缓冲、§6 清晰度、§8 配置）。
⚠ **它一个字节都不写**：队列与缓冲都是进程内状态（重启即空），只有**锚点库**会追加写
（见 §3.1 的写入者名单）。

## 3. 单向性（最容易踩的一条）

派生是**单向**的，所以：

- 手改 `llm/config/llm.env` **没用**：下一次「保存并同步」会按 `config.yaml` 把它覆盖回去。要改端口/线程/上下文，
  改 `config/config.yaml` 的 `llm:` 段。
- 反向读也不行：Agent **不许**读 `llm.env`。它是给 llama-server 用的进程环境，
  不是应用配置。
- 于是"改了不生效"只剩一种原因：改错了文件。先看真源。

### 3.1 Agent 唯一被允许的写：删掉已触发的一次性日程（R3）

`scheduler.remove_fired_oneoff: true` 时（**默认 false**），一条 **oneoff** 触发之后，
Agent 会把它**从那一条所在的 `oneoff:` 序列里删掉**。规则与边界（都在代码与单测里钉着）：

| 项 | 做法 | 为什么 |
| --- | --- | --- |
| 删哪些 | **只删 oneoff**；`recurring` 一条不动 | recurring 删了明天就不响了 |
| 怎么写 | **文本级**：只删属于那条的行区间，其余**逐字节**不变（`agent/core/schedule_config.py`） | 本文件的注释就是各字段的事实约定来源，整体重排 + 丢注释不可接受 |
| 原子性 | `agent/config.py::write_text_atomic()`（同目录临时文件 + `os.replace`）—— **全仓唯一实现**，`save_config()` 也走它 | "临时文件必须与目标同文件系统"这条细节只写一遍 |
| 备份 | 原文件旁留 `config.yaml.bak`（覆盖上一份），与 GUI 的 ConfigStore 同一约定 | 出问题能回退 |
| 核对 | 写前**重新读盘**，`title` + `date` + `start` 三者都要对上，对不上就**不删** | 配置可能被人/GUI 改过，宁可不动也不误删 |
| 失败 | **只记 WARNING**（写不进配置绝不影响这次触发） | 只读挂载 / 权限 / 磁盘满都不该让日程失效 |
| 认不出的写法 | flow 风格（`oneoff: [{...}]`）**给理由、不改文件** | 一行里塞多个条目，文本级改它风险太大 |
| 注释 | 紧贴**下一条**的注释与空行**留着**（只删属于那条的行） | 宁可留一行悬空注释，也不误删可能描述下一条的注释 |

**谁在用这套机制**（两个调用方，同一份实现）：

| 调用方 | 什么时候 | 谁触发 |
| --- | --- | --- |
| Agent（`scheduler.remove_fired_oneoff`） | 一条 oneoff **触发之后**立刻删 | 自动（开关默认关） |
| CLI（`assistant cleanup --apply`） | 清理 `date < 今天` 的 oneoff（已经过去、不会再触发） | 人显式敲（默认 dry-run） |

> ⚠ CLI 的"默认只读"立场因此有一个**显式例外** —— 见 [`cli.md`](cli.md) §4 的 `cleanup` 一节。
> 它不另写一份删除逻辑，走的就是这里这一套。

两条**如实写下的限制**：

1. **竞争窗口**：读盘到 `os.replace` 之间有毫秒级窗口，另一个写入者（GUI 的设置页/模型页）
   可能正好插进来 —— 最后写入者赢。用 `.bak` 兜着，**不引入锁**（同刻两个写入者的概率极低，
   锁的复杂度不划算）。
2. **触发与写回之间进程崩了**：那条不会被删，下次启动若还在同一时间窗内会**再触发一次** ——
   这与 `agent/core/scheduler.py` 模块头既有的约定一致（"重启后同一时间窗内会再触发一次"），
   不是新问题。

> ⚠ 打开它意味着 **Agent 成为 `config.yaml` 的第二个写入者**。默认关就是这个原因：
> 这是"程序自动改真源"的行为，该由人显式决定。
> **守卫**：`tests/test_config_source_guard.py::TestWhoWritesTheConfig` —— `agent/` 里出现写入原语的
> 只能是这六个，且**每个都必须是"派生数据/本地数据"，不能是真源**：
> `agent/config.py`（唯一的原子写实现）、`agent/core/schedule_config.py`（唯一允许改真源的调用方）、
> `agent/vision/wall_data.py`、`agent/media/music_library.py`、`agent/core/user_profile.py`、
> `agent/core/game_anchors.py`（T11-4 的锚点库，**追加写**）。
> ⚠ **T11-8 补的漏项**：这条守卫原来只认"整篇重写"那几种原语，**追加写（`open(path, "a")`）扫不到** ——
> 于是 `game_anchors.py` 明明在写 `config/` 却不在名单上。现在追加写也算，六个写入者都在名单里。

## 4. Agent 只认一份配置

`agent/config.py` 加载的就是 `config/config.yaml`（`--config` 可覆盖路径），
没有第二个入口。`agent/` 里出现 GUI 专用配置名或 `llm.env` 就是回归 ——
`tests/test_config_source_guard.py` 会直接变红。

## 5. GUI 也读 `scheduler:` 段（只读，且只保证到夹具覆盖的范围）

GUI 的**日程区**显示的内容来自 `config.yaml` 的 `scheduler.recurring` / `scheduler.oneoff`
—— 与 Agent 的 `Scheduler._load_events()` **同一处**，连"某个键在 scheduler 段里找不到就
**逐键**回落到顶层"这条都一致。GUI 不写这一段（删已触发 oneoff 的是 Agent，见 §3.1），
也不新增 IPC topic。

| | 谁 | 做什么 |
|---|---|---|
| 触发 | Agent（`agent/core/scheduler.py`） | 按 `window_min` / `late_grace_min` / `remind_before_min` 真正触发日程动作；开关打开时删掉已触发的那条 oneoff |
| 展示 | GUI（`gui/src/core/schedule_model.cpp`） | 只读同一段，展开成"今天 / 明天"两段，再按**窗口**（`[现在, 现在+24h)`）筛出"接下来 24 小时"给日程区 |

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
