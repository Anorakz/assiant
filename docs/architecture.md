# 架构：双机开发（Windows PC ⇄ RK3568 板端）

> 本文描述**当前的真实实现**，不是最初的设计稿。
> 判断可信度的标准：与代码冲突时以代码为准，并回来改本文。
> 归一化进行中的条目见 `todo.md` 的「项目归一化处理」。

---

## 1. 总体拓扑

```
┌────────────────── Windows PC（开发机）───────────────────┐
│  DSH 工作区（pc-dev；rk3568-dev 经 SSH 透明访问板端）      │
│  · native/   C++ / pybind11 源码                          │
│  · agent/    Python Agent Core 源码                       │
│  · gui/      Qt5 GUI 源码（在板端编译，见 §5）             │
│  · cmake/toolchain.cmake + sysroot → aarch64 交叉编译      │
│  · 跑 host 单测（ctest）与 Python 单测（PC / WSL）         │
│  ★ 唯一提交方：commit / push / deploy 都在这里             │
└──────────────────────────┬────────────────────────────────┘
                           │ ① SSH + scp    部署与远程执行
                           │ ② Moonlight    视频流下行 + 输入上行
┌──────────────────────────▼────────────────────────────────┐
│                RK3568（Ubuntu 20.04.6 / aarch64）          │
│  agent_native.cpython-38-aarch64-linux-gnu.so ← pybind11   │
│  Python Agent Core（asyncio）                              │
│   · Image RingBuffer  ← 视频帧（唯一一条接收通路）          │
│   · send_key / send_mouse → moonlight-common-c 直调        │
│   · Chat Input Bus（asyncio.Queue）                        │
│  Qt5 C++ GUI（**在板端编译**，经 Unix socket 连 Agent）     │
│   · Agent ⇄ GUI 都在这台机器上，走 /tmp/agent.sock         │
└────────────────────────────────────────────────────────────┘
```

三条通道各自独立，别混：

| 通道 | 谁到谁 | 传什么 |
| --- | --- | --- |
| SSH / scp | PC → 板端 | 部署产物、跑远程测试、看日志 |
| Moonlight / Sunshine | 板端（客户端）⇄ Windows 主机（服务端） | 视频流下行；键鼠输入上行 |
| Unix domain socket | 板端 Agent ⇄ 板端 GUI | 状态推送与命令（§6） |

---

## 2. 工作区划分

| 工作区 | 位置 | 职责 |
| --- | --- | --- |
| PC | Windows 本地仓库 | 源码真源；交叉编译；host / PC / WSL 测试；提交与部署 |
| 板端 | `root@192.168.137.30:/home/kickpi/myproject/assitant` | 运行 Agent 与 GUI；板端测试；板端本地状态 |

规则：

- **PC 是唯一提交方**：板端仓库只作运行环境，**不提交、不手改代码**；板端挂在
  `main` 上并 tracking `origin/main`（`git status` 保持干净）。
- **部署只送已提交的内容**（`deploy.ps1` 用 `git archive HEAD`），所以"板端在跑的东西"
  永远可追溯到某个 commit。
- **板端本地、不入库**：`config/config.yaml`、`llm/`、`sig/`、
  `net/`、`creds/`、`logs/`、`model/`、`runtimes/`、`temp/`。其中 `main` 的
  `.gitignore` 覆盖不了的那几个（`llm/ sig/ net/ runtimes/ …`）写在板端的
  `.git/info/exclude` 里 —— 那是**本机**规则，不该进仓库。
- **解包是增量的**（`tar -x` 无 `--delete`）：仓库里删掉的文件不会自动从板端消失。
  所以 `deploy.ps1` 会写一份逐文件 sha256 的**部署清单**，`health_check.sh` 据此报
  "落后 N 个文件"与"多余文件"，`-Prune` 负责清。

> 部署与同步的完整规则（三条路径各管什么、清单/落后判定、板端不入库清单、常见操作）
> 见 [`docs/deploy.md`](deploy.md)。

---

## 3. 底层 C++ / pybind11 层

### 3.1 读取侧：视频帧 RingBuffer（唯一一条接收通路）

```
Sunshine 视频流
  → moonlight-common-c 接收 RTP
  → Rockchip MPP 硬解 (decoder_mpp.cpp; 详见 docs/decoder-mpp.md)
  → ROI 裁剪 + 256×256 + RGB888
  → Image RingBuffer (lock-free, 300 帧)
  → pybind11: image_rb.read_latest()
```

> **已删除（Phase 6 收尾）**：曾经设计过一条 `Sunshine 回传主机键盘 → Host Input
> RingBuffer (128 事件) → pybind11` 的读取通路。moonlight-common-c 没有"主机 →
> 客户端"的输入 API，所以那个环形缓冲从来没有生产者，属于死代码：native 的
> `HostInputRingBuffer`、`binding` 的 `host_input_rb` 子模块、Python 的
> `HostInputReader` 及 `scheduler.host_input_interval_ms` 配置都已删除。
> （上面这段是**删除说明**，不是现状。）

### 3.2 输出侧：直接调用 API，无 RingBuffer

```
Python Agent
  → pybind11: send_key() / send_mouse() / send_hotkey()
  → LiSendKeyboardEvent / LiSendMouseEvent
  → py::call_guard<py::gil_scoped_release> 释放 GIL
  → Sunshine → Windows 主机执行
```

**原因**：输入事件稀疏（每秒几次到几十次），无需缓冲；直调延迟最低，不阻塞 asyncio。

### 3.3 连接：native 不发任何 HTTP

握手（`/serverinfo`、`/applist`、`/launch`、`/resume`）走 HTTPS 47984 + 已配对的客户端
证书，由 Python 侧 `agent/net/sunshine_client.py` 负责；native 只把握手结果交给
moonlight，**只有这一个连接入口**：

```python
import agent_native

agent_native.moonlight.start_with_session(host, app, w, h, fps,
                                          app_version, gfe_version,
                                          codec_mode_support, session_url)
agent_native.moonlight.stop()
agent_native.moonlight.status()

frame = agent_native.image_rb.read_latest()          # numpy (256,256,3) uint8 | None
agent_native.send_key(modifier, key, action)         # action: True/"down" / False/"up"
agent_native.send_hotkey([0x11, 0x12, ord("S")])     # 自带修饰键的 VK
agent_native.send_mouse(x, y, action)                # 0..255 参考平面, 越界夹住
```

> 原来那个手写裸 socket 的 C++ 明文 HTTP 层（`moonlight_connection.*`）已在 Phase 6
> 删除：它只能拿到 `PairStatus=0` 与 404，留着只会让人以为还有一条能用的路。

---

## 4. Python Agent Core

```
agent/
├── main.py            进程入口：装配全部组件、按序起停、asyncio 主循环
├── config.py          配置加载（白名单 + 不做 schema 校验）
├── core/              state_machine.py / tool_router.py / scheduler.py / wallpaper.py
├── io/                chat_bus.py / image_reader.py / input_sender.py / _native.py
├── llm/               provider.py（edge / cloud / disabled；细节见 docs/llm.md）/ rule_engine.py
├── vision/            roi.py / siglip_encoder.py（实时帧那条路，仍是 mock）/ siglip/（真 RKNN 双塔，离线打标签/检索）
├── ipc/               protocol.py / local_server.py / local_client.py
├── net/               sunshine_client.py（HTTPS 47984 握手）
└── tools/             具体工具（Phase 7）：`build_tools(router)` + 每个工具一个模块
```

| 模块 | 职责 |
| --- | --- |
| `core/state_machine.py` | 状态机 `SLEEP ⇄ IDLE ⇄ STUDY ⇄ GAME`（内部**小写**；IPC 上用大写，转换只在 ipc 层做） |
| `core/tool_router.py` | 工具注册、权限控制、执行调度（JSON Schema 子集校验） |
| `core/scheduler.py` | 日程检查、定时触发、触发监听、触发事实（R 系列）与"删掉已触发的一次性日程"（R3，默认关） |
| `io/chat_bus.py` | Chat Input Bus：把多个输入源汇成一条 `asyncio.Queue`（单消费者 + `subscribe()` 旁观） |
| `io/_native.py` | native 解析 + **每个子系统一个专属单线程执行器**（SPSC 要求，见 §11） |
| `ipc/` | Unix socket server 与协议；接入点是 `ipc/__init__.py` 的 `build_ipc()` |

**启动顺序**（`main.py`，停止时**严格反向**）：

```
日志 → config → native → ChatInputBus → io 层 → StateMachine
     → ToolRouter → LLMProvider → Scheduler → IPC → 终端输入
```

**单组件失败不影响其他组件**：每个组件单独 start/stop，失败记进 `failures` 并在收尾汇总。

**Chat Input Bus 事件格式**：

```python
{"source": "terminal" | "gui", "text": "...", "timestamp": 1234567890.123}
```

### 4.1 工具层（`agent/tools/`，Phase 7）

`core/tool_router.py` 是**机制**（注册 / 权限 / 校验 / 超时 / 异常兜底），`agent/tools/` 是
**具体工具**。两边互不认识：`main.py` 只 import `agent.tools` 并调用它的
`build_tools(router)`，再一个个 `register()` 进去（见 `_register_tools()`）。

| 约定 | 内容 |
| --- | --- |
| 工厂 | `agent/tools/__init__.py::build_tools(router) -> list[Tool]`；工具模块清单在 `TOOL_MODULES`，加工具就加一行 |
| 每个模块 | `NAME` / `DESCRIPTION` / `SCHEMA` / `ALLOWED_STATES` / `build(services) -> Tool｜None` |
| 依赖从哪来 | `router.services`（`main.py` 装配时填 `input_sender` / `image_reader` / `bus` / `config`）。**不让工厂多收参数**：挂在 router 上，`build_tools(router)` 签名与所有测试都不用改 |
| 缺依赖 | `build()` 返回 `None` 并自己记一条 warning（"跳过这个工具"），**不抛异常**；少一个工具不该让工具层起不来 |
| 权限 | `Tool.allowed_states` **默认空集 = 任何状态都不允许**（fail closed）；不在允许状态里只回绝，handler 一次都不跑 |
| 给模型看的说明 | `DESCRIPTION` 要写清**副作用与不确定性** —— 例如 `back_to_desktop` 明说"是开关动作、没有回执、别连着调"。CLI 那边"不假装调了工具"是同一条口径 |

**权限表（T4，唯一写下来的地方是 `tests/test_tool_permissions.py::EXPECTED`）**：

| 状态 | `back_to_desktop` | `next_wallpaper` |
| --- | --- | --- |
| `SLEEP` | ✗ | ✗ |
| `IDLE` | ✗ | ✓ |
| `STUDY` | ✓ | ✓ |
| `GAME` | ✗ | ✗ |

- **SLEEP / GAME 一个都不给**：SLEEP 是"别动系统里的任何东西"；GAME 的主区是视频区，
  换壁纸等于白换，而"回到桌面"是**学习收尾**的动作（T1 的决定）。
- **给模型的候选清单也按状态过滤**（`ToolRouter.allowed_tools()`）：不可用的工具根本不该
  出现在候选里 —— 否则 SLEEP/GAME 下模型会看到 `back_to_desktop`、试着调、吃一个拒绝，
  白花一轮。执行期的 fail-closed 校验照旧（是**少给**，不是放宽）。
- **加工具时先在这张表里决定它在哪些状态可用**，否则 `test_tool_permissions.py` 会红
  （它双向对齐：注册得到的工具必须在表里，表里的工具必须注册得到）。
- ⚠ **同名命令也受同一张表约束**（T6）：`next_wallpaper` 既是工具名也是 GUI 命令名，
  而"这个状态下能不能换壁纸"只有**一个**答案。命令路径（`_handle_next_wallpaper`）在调
  `Runtime.next_wallpaper()` 之前先问 `ToolRouter.allowed_in_current_state()`，被拒时回一条
  `llm` 说明（"换壁纸没成功：… 在当前状态（game）下不可用（可用状态: idle, study）"）。
  否则 GUI 的按钮就成了绕过状态表的后门。工具没注册（缺依赖）时**不拦** ——
  那是"工具没装"，不是"这个状态不允许"。
- **"补推"不受状态表约束**（T6）：新 GUI 连上时 Agent 会补一张**当前**壁纸
  （`Runtime.push_current_wallpaper()`）。那是"同步显示"，不是"换一张" ——
  客户端连上时 Agent 可能正处在 SLEEP/GAME，补一张当前画面不该被拒。

工具**只在真会调模型的模式下才有意义**：`edge` 与 `cloud` 都走同一个工具循环
（T2 起 edge 也接进来了，见 §4.2），`disabled` 是规则引擎，**不假装调过工具**。

⚠ **工具不是唯一的调用方**：`next_wallpaper` 这个动作既有 LLM 工具，也有 GUI 的
`next_wallpaper` 命令（主区「下一张」）。两者**共用** `Runtime.next_wallpaper()`，
真正的语义在 `core/wallpaper.py`（目录 + 游标）—— 工具只把入参转过去。
推给 GUI 的 topic 名只有 `agent/ipc/` 知道（`Runtime.on_wallpaper` 钩子），
`Runtime` 与工具都不认识线格式字段。

### 4.2 LLM 层：edge / cloud / disabled（T2）

| 模式 | 谁答 | 说明 |
| --- | --- | --- |
| `edge` | 板端 llama-server（llama.cpp + GGUF） | Agent **不加载模型**，只连本机 `127.0.0.1:<llm.port>` 的 OpenAI 兼容接口 |
| `cloud` | 远端 OpenAI 兼容 API | 同一套客户端（`_OpenAICompatibleBackend`） |
| `disabled` | RuleEngine | 不联网、不调模型 |

**工具循环只写一份**（`LLMProvider._chat_with_tools(backend)`）：edge 与 cloud 的差别只有
"连哪儿 + 每次请求带什么参数"，所以板端小模型也能 function call。

edge 挂了**不装作答过**：`chat()` 照抛；`chat_with_tools()` 退回规则兜底，正文前缀说明
这不是模型答的（GUI 只看正文），结果里另有 `degraded` 写原因，`main.py` 记 warning。
板端实测数据（`/no_think`、耗时、降级）与验证步骤见 [`llm.md`](llm.md)。

### 4.3 视觉层：两条 SigLIP 路径（T7-1）

| 路径 | 是什么 | 谁用 |
| --- | --- | --- |
| `agent/vision/siglip_encoder.py` | **mock**（由图像内容决定的确定性伪随机向量，`ready` 恒 False） | 给"板端实时帧 → embedding"占位；**目前没有调用方** |
| `agent/vision/siglip/` | **真实现**：SigLIP 双塔 RKNN（图像塔 + 文本塔），走 NPU | **离线**打标签 / 图像检索（`assistant tag`、`list_wallpaper_tags`） |

两条路**接口不同**（前者只 encode 图像；后者是完整双塔：`encode_image` / `encode_text` /
`similarities` / `rank`），不是同一个东西的两个实现 —— 别拿 mock 那条去算文本相似度。

**`agent/vision/siglip/` 的三条规矩**（都是实测逼出来的，别改）：

1. **配置只配"文件放哪儿 / 怎么跑"**（`config.yaml` 的 `vision:` 段：`model_path` /
   `tokenizer_path` / `runtime_lib` / `verbose` / `warmup_runs`）。**模型契约写死在代码里**：
   256×256、3 通道、nhwc、**uint8 原始 0-255**、64 token、pad=1、768 维、L2 归一化、
   只能按余弦。理由是这些值改错**不报错、只会静默变笨**（实测喂 0-1 浮点时不同图余弦
   全挤在 0.98+，判别力全丢；int8 模型的文本塔量化过度，不同文本得到几乎相同的向量）。
2. **Agent 启动路径不碰它**：`import agent.vision.siglip` 不加载 rknnlite / tokenizers /
   numpy（三者都是延迟导入），所以宿主机上也能 import 并跑纯逻辑单测。
3. **搬运要有对齐证据**：这份实现是从板端实验树 `sig/` 搬进仓库的，
   `tests/board/siglip_align.py` 用**同一张图**分别过两条路径比 embedding ——
   实测余弦 1.000000、逐位差 0（搬运没有改变行为）。换模型/改常量后重跑它。

---

## 5. GUI（Qt5 C++，跑在板端）

GUI **不是** PC 上的 Python 程序：它是 `gui/` 下的 Qt5 C++ 程序，**在板端本机编译**
（板端装了 Qt 5.12 aarch64；PC 上没有 Qt），由 `scripts/sync-gui.ps1` 送源码过去
后 `cmake -S gui -B gui/build && cmake --build`。

```
gui/src/
├── main.cpp / main_window.*     入口与主窗口（--socket / --windowed / --config / --page …）
├── core/    config_store / config_sync / view_state / idle_watcher /
│            image_fit / system_stats / lyrics / schedule_model
├── services/ local_client（Unix socket 客户端）/ onboard_ctl（屏幕键盘）
└── ui/       top_bar / bottom_bar / mode_panel / chat_panel / schedule_panel /
             music_bar / video_panel / sys_page / model_page / settings_page / region_host
```

- **配置**：真源只有一份 `config/config.yaml`（GUI 读写 `gui:` 与 `llm:` 段，
  另外**只读** `scheduler:` 段画日程区）。谁写谁读、以及"日程语义只在夹具覆盖范围内
  保证两边等价"这条边界，见 [`config-sources.md`](config-sources.md)。
- **依赖**：板端除 Qt 5.12 外还要 `libyaml-cpp-dev`（日程区只读解析用；写回仍然只有
  `ConfigStore` 的文本级替换，因为它保注释）。
- **测试**：`gui/tests/`（Qt Test），只能**在板端**跑（`sync-gui.ps1 -Test` 会顺手
  重建并 ctest）。其中 `test_local_client` 的对端是真 Python 脚本
  `gui/tests/local_server.py`，不是 mock。

---

## 6. IPC（Agent ⇄ GUI，同机 Unix domain socket）

| 项 | 值 |
| --- | --- |
| 通道 | Unix domain socket `/tmp/agent.sock`（**同机**，不跨机） |
| 角色 | Agent 监听并 `accept`；GUI 作客户端连接，断线 1s 重试 |
| 分隔 | 换行 `\n`（NDJSON，每条消息一行） |
| Agent → GUI | `{"topic": str, "data": object, "timestamp": float}` |
| GUI → Agent | `{"action": str, "payload": object}`（**没有 timestamp**） |

**两个方向的信封不一样** —— 照 GUI 的实际实现定的（Phase 6 决策 1），不是笔误；
混用会被丢弃并记 warning。完整规范与错误处理约定见
[`docs/ipc-protocol.md`](ipc-protocol.md)（线上格式唯一真源）；GUI 侧的界面行为见
[`docs/gui-agent-integration.md`](gui-agent-integration.md)。

### 6.1 日程触发事实（topic `schedule`，P 系列）

日程有**两份**事实，不要混：

| 事实 | 在哪 | 谁看 |
| --- | --- | --- |
| 日程**表**（哪天几点该做什么） | `config/config.yaml` 的 `scheduler:` 段，语义在 `agent/core/scheduler.py` | GUI 日程区（只读 + 窗口筛选）、CLI `assistant schedule` 自己展开 |
| 日程**真的触发过**（本进程内触发过哪条、什么时候） | `Scheduler._history`（有界内存，重启即清零）→ 经 IPC `schedule` 推出去 | CLI「已触发 HH:MM:SS」、`assistant watch`；GUI 目前**不认**这个 topic |

两条通路：`Scheduler.on_fire` → `topic:"schedule"`/`kind:"fired"`（**实时**，每次真的触发一条就推一次）；
命令 `query_schedule` → `kind:"state"`（**快照**，问一次答一次，无请求 id）。
`agent/ipc/__init__.py` 里"有就接"：runtime 没有 `scheduler` 时只少推这一类，其余照常。
命令与 topic 的字段定义**只在** [`docs/ipc-protocol.md` §3/§4](ipc-protocol.md)。

### 6.2 显示窗口：只显示"接下来 N 小时"（R 系列）

两侧（CLI `assistant schedule` 与 GUI 日程区）都只看 **`[现在, 现在 + N 小时)`**，
默认 `N = 24`（CLI 的 `--hours`；GUI 是 `core::kWindowHours`）。判据是**行的 `start`** ——
不是提前量算出的提醒时刻，否则 `14:00` + `remind_before_min=10` 的条目在 13:55 看会消失。

- **两侧都留 30 分钟尾巴**（窗口起点 = `现在 - 30min`）：窗口只往前看的话，"到点了、触发没触发"
  在列表里完全看不见。CLI 靠触发事实写出「已触发 / 已过（未触发）」；GUI 拿不到事实，只把那些行
  **变暗**（同一个 `past` 渲染）。
- **展开层不动**：`ScheduleModel::parse()` / CLI 的 `schedule_rows()` 仍是"今天/明天逐条展开"，
  被 parity 夹具盯着；窗口是**独立一层**（`applyWindow()` / `window_days()`），显示规则不混进去。
- 一次性日程触发后被 Agent 从配置里删掉（见 §6.1 与 [config-sources.md](config-sources.md) §3）时，
  两个列表下一轮刷新就都没有它了 —— CLI 的尾巴靠**事实**把它画出来。

---

## 7. 数据流总览

| 方向 | 通道 | 机制 |
| --- | --- | --- |
| Sunshine → Python（图像） | Moonlight 视频流 | 接收 → MPP 解码 → 预处理 → **Image RB** → pybind11 |
| Python → Sunshine（输入） | Moonlight 输入通道 | pybind11 **直调** `LiSendKeyboardEvent`，释放 GIL |
| 终端 → Agent | stdin | asyncio reader → **Chat Input Bus** |
| GUI → Agent | Unix socket | 命令信封 → **Chat Input Bus** / 命令处理器 |
| Agent → GUI | Unix socket | topic 推送（状态、LLM 输出、壁纸、音乐） |
| PC → 板端 | SSH / scp | `deploy.ps1`（Agent + `.so`）、`sync-gui.ps1`（GUI 源码） |

---

## 8. 关键文件树（不是穷举）

### 8.1 PC 仓库

```
├── CMakeLists.txt / cmake/toolchain.cmake   # host 构建 + aarch64 交叉编译
├── native/            ring_buffer.h, image_rb, preprocess, decoder(+mpp),
│                      input_sender, moonlight_adapter, binding(_utils), third_party/
├── agent/             §4 的包结构
├── gui/               CMakeLists.txt, src/, tests/, tools/, config/, resources/
├── config/            config.example.yaml / user_profile.example.yaml
├── tests/             C++ 单测 + Python 单测 + mocks/ + host/ + board/ + data/
├── scripts/           build / deploy / sync-gui / test-host / test-python(.ps1|.sh) /
│                      run-board-tests / health_check.sh / setup-sysroot-deps / pair_* / authorize_client
├── docs/              本文件与 ipc-protocol / gui / gui-agent-integration / decoder-mpp /
│                      cross-build-rk3568 / sunshine-pairing-findings
└── Readme.md / todo.md
```

### 8.2 板端（运行环境）

```
/home/kickpi/myproject/assitant/          branch main (tracking origin/main)
├── agent_native.cpython-38-aarch64-linux-gnu.so   # deploy 送来的
├── libmoonlight-common-c.so                       # 与上面同目录 (rpath $ORIGIN)
├── agent/ tests/ docs/ scripts/ native/           # = 某个已部署的提交
├── gui/            源码由 sync-gui 送, 二进制在 gui/build/agent_gui
├── logs/           agent.log + deployed-manifest / deployed-rev (后者是部署清单)
├── config/config.yaml                 板端本地（.gitignore 覆盖）
├── llm/ sig/ net/                     板端本地子系统（.git/info/exclude）
├── runtimes/ temp/ creds/ model/      板端本地（同上 / .gitignore）
├── docs/gui-qt5-*.md                  板端 GUI 迭代文档（.git/info/exclude）
├── tests/test_llm_integration.py      板端专有测试（.git/info/exclude）
└── todo                               板端旧清单（.git/info/exclude）
```

> 切换方式与"哪些该留着"的完整说明见 [`docs/deploy.md`](deploy.md) §6。

---

## 9. 开发与部署流程

### 9.1 日常循环

```
1. PC 上改代码
2. scripts/test-host.ps1      # C++ host 单测 (ctest)
3. scripts/test-python.ps1    # Python 单测 (PC; WSL 用 sh scripts/test-python.sh)
4. git commit                 # 部署只送已提交内容, 所以必须先提交
5. scripts/deploy.ps1         # 交叉编译 → git archive → scp → 解包 → 健康检查
6. scripts/run-board-tests.ps1    # 板端 Python 套件
7. scripts/sync-gui.ps1 -Test     # 改过 gui/ 时: 送源码 + 板端重建 + ctest
```

### 9.2 部署（`scripts/deploy.ps1`）

```
build.ps1（交叉编译, 校验 ELF aarch64 与 GLIBC 上限）
→ git archive HEAD 打包 agent/ tests/ docs/ scripts/ config/config.example.yaml
   （-c core.autocrlf=false -c core.eol=lf，防止 CRLF 进 tar）
→ 建部署清单（逐文件 sha256 → logs/deployed-manifest + deployed-rev）
→ scp .so + libmoonlight + tarball + 清单
→ 板端增量解包（不删任何东西）
→ 可选 -Prune：清掉"仓库里已删、板端还在"的残留
→ scripts/health_check.sh（必需项 + 落后判定 + 多余文件）
```

**落后判定**：清单里有而板端缺失/内容不同的文件数 —— 刚部署完就该是 0，不是就
FAIL 并点名。**多余文件**：板端有而清单没有的（只报告，用 `-Prune` 清）。
详细规则见 [`docs/deploy.md`](deploy.md)。

### 9.3 GUI 单独一条路（`scripts/sync-gui.ps1`）

GUI 在板端编译，所以它**不**跟 `deploy.ps1` 走：`sync-gui.ps1` 只送 `gui/` 源码，
带三方冲突守卫（base = 板端 HEAD，ours = PC HEAD），然后可选在板端重建并 ctest。

---

## 10. 测试分层

| 层 | 在哪跑 | 入口 | 说明 |
| --- | --- | --- | --- |
| C++ host 单测 | PC | `scripts/test-host.ps1` | gtest + ctest（不链接 moonlight / MPP / FFmpeg，测状态机、参数校验、纯函数、环形缓冲） |
| Python 单测 | PC / WSL / 板端 | `scripts/test-python.ps1`、`sh scripts/test-python.sh` | 三端跑**同一份文件清单**（`test-python.sh`），避免两边漂移 |
| GUI 单测 | 板端 | `scripts/sync-gui.ps1 -Test` | Qt Test；需要 Qt5，PC 上跑不了 |
| 板端真机验收 | 板端 | `tests/board/test_binding_api.py`、`mpp_decode_smoke` | 验 pybind11 的 numpy 形状/dtype、GIL 释放、MPP 通路 |

用例数量会变，**以实际跑出来的为准**，本文不写死数字。

---

## 11. 关键原则与坑

1. **板端不改代码**：改动一律回 PC，走部署流程。
2. **固件升级后重拉 sysroot 并重新交叉编译** —— 否则 glibc 不匹配。产品要求编译产物
   最高只吃 `GLIBC_2.17`（板端 glibc 是 2.31）。
3. **pybind11 交叉编译**：`Python.h` 必须来自 sysroot；`.so` 带 `$ORIGIN` rpath，
   与 `libmoonlight-common-c.so` 放同目录即可 `import`，不需要 `LD_LIBRARY_PATH`。
4. **`send_*` 必须释放 GIL**，否则阻塞板端 asyncio。
5. **native 的读接口是 SPSC**（单生产者单消费者）：同一个 reader 的所有 native 调用
   必须落在**同一个线程**上 —— 所以 `agent/io/_native.py` 给每个子系统一个专属单线程
   执行器，而不是用 asyncio 默认的共享线程池。
6. **IPC 是同机 Unix socket**，不跨机；两个方向的信封不同（§6），不要"两种都认"。
7. **PowerShell 5.1 的坑**（脚本头注释里都有）：`.ps1` 必须 ASCII-only（无 BOM 时按
   ANSI 解码）；不要对原生命令用 `2>&1`（`$ErrorActionPreference=Stop` 下会把 stderr
   变成终止错误）；ssh 参数里不要嵌引号（会被剥掉）；`git archive` 要显式
   `-c core.autocrlf=false -c core.eol=lf`，否则 CRLF 进 tarball，板端 shell 脚本报
   `$'\r': command not found`。
8. **先跑通，再正规化**：Phase 1+2 打通后立刻做双机联调，再补 GUI 和工具层。
