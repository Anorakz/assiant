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
| `core/state_machine.py` | 状态机 `SLEEP ⇄ IDLE ⇄ STUDY ⇄ GAME`（内部**小写**；IPC 上用大写，转换只在 ipc 层做）。⚠ **任何切换都必须经过 IDLE**：`transition_to()` 会按这张表算路径（有直边一跳，否则 `当前→IDLE→目标` 两跳）并**逐跳执行** —— 每一跳都触发 `on_change`，所以"离开那个模式要释放的东西"按步发生（T12-1；`transition()` 是单跳原语，语义没变） |
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
| 超时 | 路由默认 5 s；**工具可以声明自己的**（`Tool.timeout_s`）—— 会同步走一趟 PC 的工具（`next_music` 排队列后起播要 ssh + schtasks, 板端实测 5~8 s）必须声明，否则会被误判成 `timed out`（T8-5b 板端实测踩到过） |
| 给模型看的说明 | `DESCRIPTION` 要写清**副作用与不确定性** —— 例如 `back_to_desktop` 明说"是开关动作、没有回执、别连着调"。CLI 那边"不假装调了工具"是同一条口径 |
| 说明可以是**动态的** | T7-4 起 `next_wallpaper` 在 `build()` 时把**当前词表**拼进 description（"可用标签: scene=…；tone=…；mood=…"，超 320 字符就截断并指向 `action="tags"`）。理由：板端实测小模型会编造标签名，而词表只有配置/代码知道 |
| **参数归一化（T8-5c）** | 每个工具可以声明一个**纯函数** `normalize(args) -> args`（`Tool.normalize`），路由在 **schema 校验之前**跑它 —— 见下面 §4.1.1 |

### 4.1.1 参数归一化：按语义接受，不按参数名挑刺（T8-5c）

板端实测反复证明：0.6B **填得进参数，但常常填错位置/填成老写法**
（`action="play"`、`action="next"` + `match`、条件塞进 `ip_query`、`track_id="none"`、
把 `3` 写成 `"3"`）。这些都是**等价写法**，白丢一轮很亏。

所以定成一条约定：

| 规则 | 为什么 |
| --- | --- |
| 归一化是**纯函数**（`agent/tools/<tool>.py::normalize`），只收参数、只回参数，不碰 services/IO | 能脱离路由单测；规则表就是文档（`tests/test_tool_normalize.py`） |
| 在 **schema 校验之前**跑（`ToolRouter._normalized_args`） | 否则 `action="play"` 这种 enum 外的老写法**轮不到被救**：校验先把它拒了 |
| 只做**等价改写**：老参数名、同义 action、空值字面量（`""`/`none`/`null`）、字符串数字 | 不改语义、**不猜内容** —— 猜不出来的（`action="dance"`、`pick` 却没给 `match`）原样交下去，让校验/handler 如实报错 |
| schema **不因为归一化而放宽** | schema 是"给模型看的声明"；宽容只发生在**入口**，模型看到的 action 列表仍与真实可用完全一致 |
| 每条规则都要写**来源**（哪种板端实测逼出来的） | 免得变成"凭空发明的宽容"，一年后没人敢删 |
| 归一化自己炸了 → 记 warning、**照原参数往下走** | 一个坏规则不该让工具挂掉（`Tool` 边界测试钉着） |
| 没声明 `normalize` = 原样通过 | 简单工具（`back_to_desktop`）没什么可归一化的 |

**工具清单的预算守卫（T8-5c-3）** —— 清单是 prompt 的大头，涨了必须有人知道：

| 事实（板端实测） | 数字 |
| --- | --- |
| 三个工具的清单（STUDY） | **3274 字符 ≈ 1309 token**（中文≈1 字/token，JSON 键/标点≈3~4 字/token） |
| ↑ T8-6 加了 `least`/`most` 两个 action 之后 | **3522 字符**（≈ +40 token） |
| ↑ T10-3 又加了 `stage`（只预挑不切屏）之后 | **3562 字符**（离 3600 的守卫只剩 **38 字符** —— 加之前先把 `next_wallpaper` 的 description 与枚举措辞压了一遍才塞进去） |
| ↑ T11-5 加了第四个工具 `bilibili_search` | **STUDY 一个字符都不涨**（仍 3562）—— 它**只在 GAME 可用**，所以走进的是 GAME 那份清单：**544 字符**（GAME 本来零工具，空间充裕） |
| ↑ T12-6 加了第五个工具 `set_schedule`（日程, IDLE/STUDY） | **STUDY 4528 字符 = 1867 token**（= ctx 4096 的 **45.6%**；它自己 930 字符 / **390 token**；IDLE 1799 token）—— 日程没法塞进任何现有工具的 action（与壁纸/音乐/视频是四件不同的事, 你点名要加），所以预算抬到 **4700**（只留 ~170 余量） |
| 第一轮 prompt | 1495 token（T8-5b 测得；T8-6 之后约 1535 —— **没有重量过板端**）; T12-6 量的是**工具清单那一块**, 不是整轮 prompt |
| 加一个 **action**（enum 多一个值） | ≈ 十几 token |
| 加一个**工具**（像样的 description + schema） | ≈ 250~750 token（`set_schedule` 930 字符 = **390 token** —— 已经把 description 与 schema 措辞压过一遍） |

> ⚠ **"把工具放进哪个状态"是预算问题，不只是权限问题**（T11-5 的教训）：同一个工具放进
> STUDY 会让清单从 3562 涨到 4139（**超 539**），放进 GAME 则**一点不占**别人的额度。
> 所以 T11 的 B 站工具选了 **GAME-only**（也是你定的：视频就是游戏模式主区在放的东西）。

`tests/test_merged_tools.py::TestPromptBudget` 按**字符**卡上限（`TOOL_BLOCK_BUDGET_CHARS = 4700`、
单工具 `TOOL_BUDGET_CHARS = 2000`）—— T8-5c-3 立这条守卫时是 3600（"再加一个像样的工具就会红"），
T12-6 因为按需求加了第五个工具抬到 4700，**只留了 ~170 字符余量**（下一个工具照样会红）。
要抬预算就得**在注释里写清理由**，并重量一遍板端 prompt —— 量法固定下来了：
`python3 tests/board/measure_tool_tokens.py`（起真 llama-server，POST `/tokenize`，四个状态各量一遍）。
守卫带反空转检查：量出来的清单不能太小、真加一个工具必须过不去、
加一个 action 涨的字符必须远小于加一个工具。

⚠ 顺带记一个坑（有测试钉着）：`Tool.to_llm_dict()` 的 `parameters` **就是模块级 `SCHEMA`
那个对象**（不是拷贝）—— 谁原地改清单里的 schema，就等于改了这个工具本身。

规则表（现状，来源都在各自模块的 docstring 里）：

| 工具 | 模型可能这样写 | 归一化成 |
| --- | --- | --- |
| `next_wallpaper` | `action=" NEXT "` / `step=1｜-1｜0`（老参数名） | `action="next"｜"prev"｜"repeat"` |
| | `action="next"` + `match="scene=anime"` | **原样保留**（等价合法："在最像的几张里翻"） |
| | `action="next"` + `ip_query="scene=anime"` | `match` 拿过来（`ip_query` 只有 `tags` 用） |
| | `match=""` / `"none"` | 删掉（当没给） |
| | `sort=" used_asc "` / `sort=""` | 小写 / 删掉（T8-6: `used_asc`｜`used_desc`） |
| | `action="least_used"｜"used_asc"｜"fewest"` | `action="least"`（T8-6） |
| | `action="most_used"｜"used_desc"` | `action="most"` |
| | 只给 `sort="used_asc"`（没给 action） | `action="least"`（落成 `pick` 会撞"pick 需要 match"） |
| | 只给 `match`（没给 action） | `action="pick"` |
| | `action="stage"`（只把挑中的放进"下一个"，**不切屏**） | 原样保留（T10-3；改 `next` 槽、不改 `current`、不计数）—— ⚠ `ip_query` 搬运那条规则只覆盖 `next/prev/repeat`（`_STEPS`），`stage`/`pick` 填错位置**不会**被搬（`pick` 会如实报"要说明按什么挑"，`stage` 会当没给条件按画像挑）—— 见 `tagging.md` §7 |
| `next_music` | `action="play"｜"add"｜"push"｜"clear"` | `enqueue` / `clear_queue`（老版本工具名/同义写法） |
| | 没给 `action`，但给了 `track_id`/`keyword`/`tag`/`sort`/`limit` | `action="enqueue"`（语义就一个） |
| | `track_id="none"｜""｜"null"` | 删掉（走"按条件挑"） |
| | `limit="3"` / `level="30"` | `3` / `30`（schema 会拒字符串） |
| | `action="tag"` 却把标签放进 `tag` | 搬到 `set_tag` |
| `set_schedule` | `action="create"｜"set"｜"schedule"` | `action="add"`（T12-6：同义动词） |
| | `action="delete"｜"cancel"` / `"show"｜"query"` | `remove` / `list` |
| | 没给 `action`，但给了 `start`/`days`/`date` | `action="add"`；只给 `state` → `list` |
| | `state="学习"｜"STUDY"｜"Study模式"` | `study`（中文说法与大小写；认不出的**原样留着**让语义校验报错） |
| | `start="9:30"｜"930"｜"9点30"｜"9点"｜"9点半"` | `09:30` / `09:00` / `09:30`（机械改写；`"九点"` 这种认不出的**原样留着**） |
| | `start` 写进 `time`/`at`/`when` 等键 | 搬到 `start` |
| | `days="mon,wed"｜"周一 周三"｜"工作日"｜[1,3]` | `["mon","wed"]` / `["mon".."fri"]` / `["mon","wed"]`（1=周一） |
| | `date="2026/09/22"｜"2026.09.22"｜"2026年9月22日"` | `"2026-09-22"`（只换分隔符 + 补零） |

**权限表（T4，唯一写下来的地方是 `tests/test_tool_permissions.py::EXPECTED`）**：

| 状态 | `back_to_desktop` | `next_wallpaper` | `next_music` | `bilibili_search` | `set_schedule` |
| --- | --- | --- | --- | --- | --- |
| `SLEEP` | ✗ | ✗ | ✗ | ✗ | ✗ |
| `IDLE` | ✗ | ✓ | ✓ | ✗ | ✓ |
| `STUDY` | ✓ | ✓ | ✓ | ✗ | ✓ |
| `GAME` | ✗ | ✗ | ✗ | ✓ | ✗ |

> **T8-5b: 七个工具合并成三个**（每个工具用 `action` 分派具体动作）。动机是 prompt 预算 ——
> T8-5 板端实测 7 个工具的工具清单占第一轮 prompt 的 **90%**（1699 / 1898 token），
> 第二轮 2131 直接把 `ctx_size 2048` 撞穿。合并后模型只认三个名字：
> `next_wallpaper`（翻页/按内容挑/按用量挑/看标签）、`next_music`（音乐的一切，transport 交给 GUI）、
> `back_to_desktop`（无参数）。标签写法两边共用 `agent/core/label_spec.py`。
> ⚠ **缺一个入口就整个工具不装**（不是"少一个动作"）—— 模型看到的 action 列表必须与
> 真实可用的完全一致。链路见 [music.md](music.md)、[tagging.md](tagging.md)。
>
> **T11-5: 第四个工具 `bilibili_search`（GAME-only、只有一个必填 `keyword`）** —— 它的职责
> **只有"把对话里的关键词交给 B 站队列"**：搜 + 排 + 填预览栏，**不播**（播不播由 GUI 操作决定，
> 见 [bilibili.md](bilibili.md) §7）。清队列/切集/选片**都不进 LLM**（那些只走 GUI 命令与
> Agent 自己的循环）—— 所以它没有 `action` 枚举，也就长得特别小（508 字符）。
>
> **T12-6: 第五个工具 `set_schedule`（IDLE/STUDY，你点名要的日程设置）** —— 三个 action
> `add` / `list` / `remove`，写的是 `config.yaml` 的 `scheduler` 段（**文本级**手术 +
> `.bak`，与 R3 那套同一份实现，见 [config-sources.md](config-sources.md) §3.1），
> 写完**立刻热重载**运行中的调度器。语义校验走**真的** `ScheduleEvent.from_config`
> （同一个判据既管读也管写），所以"写得进去但读不出来"不会发生；一次性日程写在过去会被
> **拒绝并附上今天的日期**（系统提示里没有时钟 —— 模型的日期是猜的）。所以：
> **要写 `date` 就先 `action="list"` 拿今天的日期**（`list` 会回 `today` / `now` / `weekday`），
> 这条要求写进了工具的 description；`list` 也顺带把"读不出来的老条目"如实列出来。
> ⚠ 这是**已知的边界**：不往系统提示里塞时钟，是因为那会动所有模式的行为，属于另一个改动
> （真要让模型自己算"明天"，得先把当前时间放进 prompt）。
>
> - **SLEEP 一个都不给**：SLEEP 是"别动系统里的任何东西"。
> - **GAME 只给 B 站那一个**（T11）：主区是视频区，换壁纸等于白换，而"回到桌面"是**学习收尾**的
>   动作（T1 的决定）；游戏模式下唯一有意义的新工具就是"给队列一个关键词"。
> - **日程只在 IDLE / STUDY**（T12-6）：与壁纸/音乐同一档 —— "什么时候切到什么模式"是日常安排。
>   ⚠ 排出来的日程**可以**切到 `sleep`/`game`：那只是被执行的动作，与"现在能不能调工具"无关。
> - **给模型的候选清单也按状态过滤**（`ToolRouter.allowed_tools()`）：不可用的工具根本不该
>   出现在候选里 —— 否则 SLEEP/GAME 下模型会看到 `back_to_desktop`、试着调、吃一个拒绝，
>   白花一轮。执行期的 fail-closed 校验照旧（是**少给**，不是放宽）。
> - **加工具时先在这张表里决定它在哪些状态可用**，否则 `test_tool_permissions.py` 会红
  （它双向对齐：注册得到的工具必须在表里，表里的工具必须注册得到）。
- ⚠ **T7-3 撤掉了 T6① 的一半**：以前 `next_wallpaper` 既是工具名也是 GUI 命令名，
  于是命令路径要借同一张表判一次（`ToolRouter.allowed_in_current_state()`）。按你的要求
  **手动换壁纸（GUI「下一张」按钮与同名 IPC 命令）已删除**，那个方法随之删除 ——
  这张表现在只管工具，而工具只有**对话**一条路能调到，所以"这个状态下能不能换壁纸"
  仍然只有一个答案（`ToolRouter.execute()` 拦）。见 `tests/test_tool_permissions.py`
  的 `TestTheRevertedT6Rule`。
- **"补推"不受状态表约束**（T6）：新 GUI 连上时 Agent 会补一张**当前**壁纸
  （`Runtime.push_current_wallpaper()`）。那是"同步显示"，不是"换一张" ——
  客户端连上时 Agent 可能正处在 SLEEP/GAME，补一张当前画面不该被拒。

工具**只在真会调模型的模式下才有意义**：`edge` 与 `cloud` 都走同一个工具循环
（T2 起 edge 也接进来了，见 §4.2），`disabled` 是规则引擎，**不假装调过工具**。

⚠ **工具失败不会被模型的嘴盖住**（T7-4）：`LLMProvider` 会把失败追加到最终正文
（`⚠ <工具> 没有成功：…`，工具给的 `tell_user` 原句优先），并在结果里留 `tool_failures`。
起因是板端实测 0.6B **谎报成功**（工具报错、它回"已更换"）。见 [`llm.md` §3](llm.md)。

⚠ **换壁纸只有一条路（T7-3 起）**：对话 → LLM → `next_wallpaper` 工具 →
`Runtime.next_wallpaper(step, match, sort)` → `core/wallpaper.py`（目录 + 游标）。
`match` 的挑图逻辑在 `agent/vision/tag_index.py`（读 `config/wall_data.jsonl`，
**纯 Python 点积、不碰 NPU**），入口见 [`tagging.md` §6](tagging.md)。
推给 GUI 的 topic 名只有 `agent/ipc/` 知道（`Runtime.on_wallpaper` 钩子），
`Runtime` 与工具都不认识线格式字段。

⚠ **"换成了另一张"才计一次使用（T8-6）**：`Runtime._push_wallpaper()` 推出成功（或压根没有
GUI 入口，游标照样动了）之后调 `_note_wallpaper_shown()`，由它写 `wall_data.jsonl` 里那一行的
`used`/`last_used`。判据是"path 跟上次推上去的不一样"—— 重推当前这张（`step=0`）、
GUI 重连补推、开机后的初始画面（`count=False`）都**不算**。数字只用于挑图偏好
（`action="least"/"most"`，入口读的是同一份文件），坏了只记 warning：**统计不该让换图失败**。
按用量排完会**跳过屏幕上当前这张**再拿第 1 名 —— 连说两次"再挑张用得最少的"是一张张往少走
（板端实测过另一种写法"从当前这张往后翻"：第二次跳到 **index=39/40**，跑到用得多的一头去了）。

⚠ **按用量的挑图做成了 action，不是参数（T8-6 板端实测）**：只给 `sort=` 时 0.6B 用不起来
（它照样写 `action="pick"` + 自己编的 `match`），于是加了 `action="least"/"most"` ——
枚举里一眼能看见、还不用参数。另外它写过 `action="tags" + sort="used_asc"`（读动作配挑图
参数）：那种自相矛盾**既不能装没看见**（会谎报"已换好"）**也不能替它改成换图**（读当写），
所以 `tags` 带 `sort` 时如实报错并指出该用 `action="least"`。见 [`tagging.md` §6.1](tagging.md)。

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
| `agent/vision/siglip/` | **真实现**：SigLIP 双塔 RKNN（图像塔 + 文本塔），走 NPU | **离线**打标签 / 图像检索（`assistant tag`、`next_wallpaper(action="tags"/"pick")`） |

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

**T7-3 的挑图不走这里**：`agent/vision/tag_index.py` 读已经打好的
`config/wall_data.jsonl`（标签 + 向量 + 第一行的词表向量），用**纯 Python 点积**排序 ——
不加载 rknnlite / tokenizers / numpy，也不碰 NPU（所以开发机与板端跑同一份代码）。
两条查询：按标签（`scene=anime`）与按 **IP 锚点**（`ip=EVA`：锚点图向量取平均当原型）。
词表向量存在数据文件第一行，所以"没进 top-k 的标签"也能算出真实分数 ——
细节与实测数字见 [`tagging.md`](tagging.md)。

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

> **T12-4：日程到点 = 切状态 + 一行展示。** 事实里的内容是 `state`（没有 `title` 了）：
> 触发时按 `StateMachine.transition_to()` 走到那个状态（跨模式自动经 IDLE，逐跳释放资源），
> 同时 Agent 往 `llm` topic 推**一行**给界面看的文本（`日程到点：切到 STUDY（13:00）`）。
> 这一行**只走推送通道、不进对话总线**，所以**不会变成 LLM 的输入** ——
> 日程是"到点照做"，不是给模型的一句话（`Scheduler._fire()` 也不再往 `bus` 里推任何东西）。

### 6.2 显示窗口：只显示"接下来 N 小时"（R 系列）

两侧（CLI `assistant schedule` 与 GUI 日程区）都只看 **`[现在, 现在 + N 小时)`**，
默认 `N = 24`（CLI 的 `--hours`；GUI 是 `core::kWindowHours`）。判据是**行的 `start`** ——
日程到点就是那一行的 `start`（T12-4 起 `remind_before_min` 不再被读，没有"提前量时刻"这回事）。

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
| Agent → GUI | Unix socket | topic 推送（状态、LLM 输出、壁纸、音乐、**B 站队列**、日程事实） |
| PC → 板端 | SSH / scp | `deploy.ps1`（Agent + `.so`）、`sync-gui.ps1`（GUI 源码） |
| 对话 → 内存 → 画像 | **进程内**（无通道） | `ChatMemory` 攒纯对话（不落盘）→ 攒到 2000 字时 Agent **自己**构建一次用户画像 → `config/user_profile.jsonl`；模型看不到画像（**不是工具**）。见 [`profile.md`](profile.md) |
| 画像 → 壁纸"下一个" | **进程内**（无通道） | 画像构建完由 `_build_profile_task()` 调 `_refill_next()`：没给 `match`/`sort` 时按画像排序挑 `next`（`ip 0.7 / mood 0.2 / fresh 0.1`）；太薄就退回**文件名顺序**。见 [`tagging.md`](tagging.md) §6.2 |
| 画像 → 播放队列 | **进程内**（无通道） | 轮询周期里 `MusicPlayer.refill()` 把队列补到 **30** 首：①本地库按画像排 ②不够**只按画像里的歌手**去 PC 搜；队列里的歌 `MusicPlayer.remove()` 由负反馈去掉。见 [`music.md`](music.md) §4.4/§4.5 |
| 画像 → 心情重置 | **进程内**（无通道） | 两个**已知**心情不同 → 留住正在放的、清其余、按新心情补满（`_reset_queue_on_mood_change()`）；`unknown` 不算变化 |
| 画面 → 游戏名 → 队列 | **进程内**（无通道） | GAME 模式里 Agent 自己的循环（**不走 LLM 工具**）：`ImageReader` 抓一帧 → SigLIP 编码 → 与 `config/game_anchors.jsonl` 的锚点算余弦；不够有把握就问 **PC 进程名**（ssh，复用 `music:` 那套），**两路不一致以进程为准并把这一帧写回锚点库**（自纠错）。**有对话关键词就整个跳过**（一帧都不抓）。认出的游戏**变了**才重搜队列。见 [`bilibili.md`](bilibili.md) §3 |
| 对话/GUI → 视频队列 | **进程内**（无通道） | 关键词只从**对话**（工具 `bilibili_search`，只在 GAME 可见）或**画面**来 → `agent/core/bilibili.py` 维护"3×预览栏格数"的滑动窗口（只存地址，不下载视频） |
| 队列 → GUI | Unix socket | topic `bilibili{queue[],index,current,stream,…}`（变化时推、客户端连上时补推）→ 预览栏 + 地址栏 + 下区域封面。**播放只由 GUI 操作触发** |
| GUI → 播放的流 | **本机 HTTP**（只绑 127.0.0.1，不是外网） | 你点预览图/上一集/下一集 → Agent 取直链（带 UA+Referer）→ `ffmpeg -c copy` → **MPEG-TS** → 内存窗口（15 s 起播门槛）→ `http://127.0.0.1:<port>/stream/<bvid>?v=<token>`（`chunked` 边下边喂）→ GUI 用 `QMediaPlayer` 播它（GStreamer 经 **`curlhttpsrc`** 拉、**`mppvideodec`** 硬解）。⚠ 板端 `souphttpsrc` 是坏的（GUI 里把它的 rank 压到 0），而**播放器读不了 FIFO**（T11-9 实测读 0 字节）—— 所以是**本机 HTTP 而不是管道**；**内容不落盘**。见 [`bilibili.md`](bilibili.md) §1/§4 |
| GUI → Agent（进度） | Unix socket | 命令 `video_state{position_s,duration_s,playing,eof}` 每 2 秒一次（**真进度，不是估算**）：Agent 靠它知道"暂停了"（于是把预取放宽到 60 s）与"放完了"（自动下一集）。⚠ 两条都得**核实**（T11-10e）：起播前那几条 `playing=false` 不是暂停；`eof` 在"我们主动收流（换条/清空）"时也会来一次，所以自动下一集还要看缓冲自己的真值（`finished()`） |

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
