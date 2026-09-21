# Agent

开发机上的源码工作区。负责 C++/pybind11 底层、Python Agent Core、PySide6 GUI 的开发、交叉编译与部署。

目标板：RK3568 / Ubuntu 20.04 / aarch64 / glibc 2.31

---

## 目录结构

```
agent/
├── cmake/
│   └── toolchain.cmake          # aarch64 交叉编译工具链 (sysroot / -isystem 多架构路径)
├── native/                      # C++ / pybind11
│   ├── ring_buffer.h            # SPSC 无锁环形缓冲 (模板)
│   ├── image_rb.cpp/.h          # 256×256 RGB888 视频帧环形缓冲 (容量 300)
│   ├── preprocess.cpp/.h        # YUV420P → 256×256 RGB888
│   ├── decoder.cpp/.h           # H.265 硬解 (板端 V4L2/FFmpeg)
│   ├── input_sender.cpp/.h      # send_key / send_mouse / send_hotkey
│   ├── moonlight_adapter.cpp/.h # 连接状态机 + 视频接收线程 (握手在 Python 侧, 见 agent/net/)
│   ├── binding_utils.h          # binding 与测试共用的转换工具 (Frame→numpy 等)
│   ├── binding.cpp              # pybind11 模块 agent_native
│   └── third_party/             # 子模块: moonlight-common-c, pybind11, googletest
├── agent/                       # Python Agent Core
│   ├── __init__.py
│   ├── main.py                  # 进程入口: 装配全部组件 + asyncio 主循环
│   ├── config.py                # YAML 配置加载/保存/点号路径读取
│   ├── core/                    # 状态层、工具路由、调度层
│   │   ├── state_machine.py     # SLEEP ⇄ IDLE ⇄ STUDY/GAME 状态机
│   │   ├── tool_router.py       # 工具注册 / 权限控制 / 执行调度
│   │   └── scheduler.py         # 日程检查 + 定时触发 + 快捷键监听
│   ├── llm/                     # LLM 三模式 + 规则兜底
│   │   ├── provider.py          # edge / cloud / disabled 分发
│   │   └── rule_engine.py       # 无 LLM 时的正则规则兜底
│   ├── ipc/                     # Agent ⇄ GUI 通信
│   │   └── protocol.py          # IPC 协议: topic/command 常量 + 编解码 (无 server)
│   ├── vision/                  # 视觉层
│   │   ├── roi.py               # ROI 字符串解析 ("x,y,w,h")
│   │   └── siglip_encoder.py    # SigLIP 图像编码 (⚠ 当前 mock)
│   └── io/                      # native 的 asyncio 包装 + 输入汇聚
│       ├── chat_bus.py          # ChatInputBus: 终端/GUI 统一事件流
│       ├── image_reader.py      # ImageReader: image_rb → numpy 帧
│       ├── input_sender.py      # InputSender: send_key / send_hotkey / send_mouse
│       └── _native.py           # native 解析 + 专属单线程执行器 (SPSC)
│   (main.py / scheduler.py / router.py / llm.py / vision.py / ipc.py /
│    tools/ —— 待实现)
├── gui/                         # PySide6 GUI (待实现: main.py / panels.py / zmq_client.py)
├── config/                      # 配置模板 (真实配置不入 git)
│   ├── config.example.yaml      # 全局: llm.mode, sunshine.*, ipc.*
│   ├── user_profile.example.yaml# 用户画像
│   └── schedule.example.yaml    # 日程
├── scripts/                     # 构建 / 部署 / 测试 / 配对工具
│   ├── build.ps1                # aarch64 交叉编译
│   ├── test-host.ps1            # 宿主机 C++ 单测 (ctest)
│   ├── test-python.ps1          # 宿主机 Python 单测 (unittest)
│   ├── deploy.ps1               # 打包并推到板端
│   ├── setup-sysroot-deps.ps1   # 给 sysroot 补 python3.8-dev / ffmpeg-dev
│   └── pair_sunshine.py 等      # Sunshine SRSAES 配对 (见 docs/)
├── tests/                       # 宿主机单测
│   ├── CMakeLists.txt
│   ├── test_*.cpp               # C++ 单测 (gtest, 由 ctest 驱动)
│   ├── test_config.py           # 配置模块单测 (unittest)
│   ├── test_state_machine.py    # 状态机单测
│   ├── test_tool_router.py      # 工具注册/权限/参数校验/超时/异常单测
│   ├── test_llm.py              # LLM 三模式切换 / cloud mock / 规则匹配
│   ├── test_vision.py           # ROI 解析 / SigLIP mock (有无 numpy 两条路径)
│   ├── test_main.py             # 进程装配: 启停顺序 / 异常隔离 / 主循环
│   ├── test_ipc_protocol.py     # IPC 线格式契约 (字节级)
│   ├── test_scheduler.py        # 日程触发 / 去重 / 快捷键识别
│   ├── test_chat_bus.py         # ChatInputBus 单测
│   ├── test_io.py               # image_reader / input_sender
│   ├── mocks/                   # mock_agent_native: native 替身
│   ├── host/                    # 需要 numpy 的绑定层测试 (按需手动跑)
│   └── board/                   # 板端真机验收脚本
├── docs/                        # 文档
│   └── sunshine-pairing-findings.md
├── build-host/                  # 宿主机构建产物 (不入 git)
├── build-rk3568/                # 交叉编译产物 (不入 git)
├── CMakeLists.txt
├── Readme.md
└── todo.md
```

---

## 快速开始

```bash
# 初始化子模块 (必须 --recursive: moonlight-common-c 还有 enet / nanors 子模块)
git submodule update --init --recursive

# 交叉编译 (aarch64)
scripts/build.ps1

# 宿主机单测 (C++)
scripts/test-host.ps1

# 宿主机单测 (Python)
scripts/test-python.ps1

# 部署到板端
scripts/deploy.ps1
```

---

## I/O 层
`agent/io/` 把 native (`agent_native`) 的阻塞接口包成 asyncio 友好的 awaitable，
并把输入源汇成一条事件流：

```
终端 ─────┐
GUI ──────┴─→ ChatInputBus ─→ 下游 (scheduler / agent core)

ImageReader   → numpy (256,256,3) 帧
InputSender   → send_key / send_hotkey / send_mouse
```

```python
from agent.io import ChatInputBus, ImageReader, InputSender

bus = ChatInputBus()

event = await bus.get()          # {"source", "text", "timestamp"}
frame = await ImageReader().read_latest()

sender = InputSender()
await sender.send_key("ctrl", "C", "down")
await sender.send_hotkey(["ctrl", "alt", "S"])
```

约定：

- **所有 native 调用都跑在专属的单线程执行器上**，不阻塞事件循环。这不只是性能：
  `image_rb` 是 SPSC 无锁结构，消费者必须**始终是同一个线程**，
  所以不能用 asyncio 默认的共享线程池。
- `agent/io` **不在 import 时加载 native 扩展**（宿主机没有 `.so`）。缺 `.so` 只在真正
  调用时报错；测试用 `agent.io.set_native(mock)` 注入替身。

---

## 工具路由

负责**注册**、**权限控制**、**执行调度**三件事。具体工具在 `agent/tools/`，
本模块只认接口，不做编排（谁调谁由 LLM 决定）。

```python
from agent.core import State, StateMachine, Tool, ToolRouter

sm = StateMachine()
router = ToolRouter(state_provider=sm)

router.register(Tool(
    name="screenshot",
    description="截取主机当前画面",
    schema={"type": "object", "properties": {}, "additionalProperties": False},
    handler=do_screenshot,
    allowed_states={State.GAME, State.STUDY},
))

router.list_tools()                       # 给 LLM 的 schema 列表
await router.execute("screenshot", {})    # {"ok": True, "result": ...}
                                          # 或 {"ok": False, "error": "..."}
```

一次 `execute` 的顺序：**工具存在 → 状态允许 → 参数符合 schema → 执行（带超时）→ 异常兜底**。
`execute` **永不抛异常**，调用方（LLM 循环）只看 `ok`。默认超时 5s。

约定：

- 状态不允许时工具**绝不执行**；拿不到当前状态时 **fail closed**（拒绝而不是放行）。
- 同步 handler 会被丢到线程池，不阻塞事件循环；超时后线程仍在跑（Python 无法强杀线程），
  调用方需自行考虑幂等。异步 handler 会被真正 cancel。
- 参数校验用的是**自己实现的 JSON Schema 子集**（`SUPPORTED_KEYWORDS`）——实测宿主/WSL/板端
  都没有 `jsonschema`，不为一个字段校验往板端塞依赖。子集外的关键字默认静默忽略，
  用 `ToolRouter(permissive_schema=False)` 可以把"写了但没生效"变成报错。

---

## IPC 协议（Agent ⇄ GUI）

完整规范见 [`docs/ipc-protocol.md`](docs/ipc-protocol.md)（**唯一真源**）。常量与编解码在
`agent/ipc/protocol.py`，Agent 侧接入点是 `agent/ipc/__init__.py` 的 `build_ipc()`，
GUI 侧实现在 `gui/src/services/local_client.cpp`。

| 项 | 值 |
| --- | --- |
| 传输 | Unix domain socket `/tmp/agent.sock`（Agent 监听，GUI 作客户端连接） |
| 分隔 | 换行 `\n`（NDJSON，每条消息一行） |
| 编码 | UTF-8 |
| Agent → GUI（推送） | `{"topic": str, "data": object, "timestamp": float}` |
| GUI → Agent（命令） | `{"action": str, "payload": object}` —— **没有 `timestamp`** |
| 时间戳 | Unix epoch **秒**（浮点，C++ 侧必须用 `double`，`float` 在 epoch 尺度只有约 128 秒分辨率） |

```python
from agent.ipc import encode, decode_command, TOPIC_STATUS, MODE_STUDY

sock.sendall(encode(TOPIC_STATUS, {"mode": MODE_STUDY, "connected": True}))  # 推状态
action, payload = decode_command(line)    # 收命令 -> ("chat_input", {"text": "..."})
```

**两个方向的信封不一样，这不是笔误**（Phase 6 决策 1：命令格式以 GUI 的实际实现为准）。
推送用 `topic`/`data`/`timestamp`，命令用 `action`/`payload`；**混用会被丢弃并记 warning**，
不做"两种都认"的兼容。两端各自只收一个方向：Agent 只解命令、只发推送，GUI 反过来。

约定：

- **`data` / `payload` 必须是 object**，数组/标量判为非法；没有参数也要写 `{}`。
- **未知字段忽略、未知 topic / action 忽略** —— 这是没有版本号时唯一的向前兼容手段。
- **一条坏消息只影响它自己**：非法 JSON / 结构不对 → 丢弃 + 记日志，**不断开连接**。
- `status.mode` 用**大写**（`SLEEP`/`IDLE`/`STUDY`/`GAME`），而 `StateMachine` 内部是
  小写；转换只在 ipc server 那层做，不要混着传。
- `switch_mode` 的参数键是 **`value`**（不是 `mode` —— 那是 `status` 推送里的字段）。


---

## 运行

```bash
python3 agent/main.py                 # 板端主进程
python3 agent/main.py --check-config  # 只校验配置
python3 agent/main.py --dry-run       # 不连串流/不读 stdin, 只验证装配
AGENT_RUN_SECONDS=5 python3 agent/main.py   # 跑 5 秒自动退出 (冒烟)
```

启动顺序（按依赖，停止时**严格反向**）：

```
日志 → config → native → ChatInputBus → io 层 → StateMachine
     → ToolRouter → LLMProvider → Scheduler → IPC → 终端输入
```

约定：

- **单组件失败不影响其他组件**：每个组件包两层保护（组件自身失败、步骤里组件之外的代码
  失败），失败记进 `failures` 并在收尾汇总。可操作性优先于完整性 —— 摄像头没插不该让
  快捷键也失效。
- **日志**：同时写 `logs/agent.log`（含 DEBUG）和 stdout（INFO 起）。
  正常跑只有 INFO + 少量自解释的 WARNING；**完整 traceback 只进 DEBUG**，
  免得正常日志看起来像崩了。排查时把级别调到 DEBUG 即可。
- 关闭：`SIGINT` / `SIGTERM` 都会触发干净退出（systemd 停服务发的是 SIGTERM）。
- 尚未实现、启动时跳过并记一条 WARNING 的组件：`agent/tools/`（工具为空）、
  `agent/ipc.py`（GUI 连不上）。它们**不算失败**，放到位即自动接入，不用改 `main.py`。

systemd 管理（不做 daemon 化）：

```ini
[Unit]
Description=Agent (RK3568)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/kickpi/myproject/assitant
Environment=LD_LIBRARY_PATH=/home/kickpi/myproject/assitant
ExecStart=/usr/bin/python3 /home/kickpi/myproject/assitant/agent/main.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

---

## 调度器

日程检查 + 定时触发 + 快捷键监听。触发动作只有两种：**状态转换** 和 **发消息给 Agent**
（`bus.push("scheduler", ...)`，和终端/GUI 同一个入口）。触发时两者可同时做。

```python
from agent.core import Scheduler

scheduler = Scheduler(state=sm, bus=bus, config=cfg)
await scheduler.start()          # 起日程循环 + 快捷键订阅
await scheduler.stop()

await scheduler.check_schedule() # 也可手动跑一轮，返回本次真正触发的事件
```

配置（`interval_min` / `window_min` / `late_grace_min` / `hotkeys` 在 `scheduler` 段，
`recurring` / `oneoff` 两处都认，方便直接喂 `schedule.yaml`）：

```yaml
scheduler:
  interval_min: 1          # 每 1 分钟检查一次
  window_min: 1            # 触发窗口宽度
  late_grace_min: 5        # 迟到的容忍度(休眠/重启后晚几分钟仍认)
  hotkeys:
    - keys: ["ctrl", "alt", "s"]
      action: {state: study, prompt: "开始学习"}
recurring:
  - {title: 站会, days: [mon, tue, wed, thu, fri], start: "09:30", remind_before_min: 5}
oneoff:
  - {title: 评审, date: "2026-09-20", start: "14:00", action: {state: study}}
```

约定：

- **去重**：`check_schedule()` 每 `interval_min` 跑一次，但同一时间窗内只触发一次
  （key 是「哪一天 + 事件 + 触发分钟」）。重启后同一窗口内会再触发一次——不做持久化。
- **提前量可以跨天**：`00:05` 提前 10 分钟 → 前一天 `23:55` 触发，所以查找时同时看
  「今天」和「明天」两个事件日。
- **快捷键是订阅式的**：`bus.subscribe()` 只看不取。**不能**用 `get()` 读——那会把
  终端/GUI 的用户消息一起吃进调度器，下游再也看不到。只认 `source == "host_keyboard"`。
- **按住不放不会重复触发**（主机端按键重复会连发 press），抬起后可再次触发。
- **`sync_time()` 不修改系统时间**，只校验时钟是否明显不对（板端无 NTP 服务/客户端库；
  真要联网校时应配 systemd-timesyncd 或 chrony）。
- **不做** cron 表达式解析、**不做**日程持久化。

---

## 视觉层

```python
from agent.vision import parse_roi, SigLIPEncoder

roi = parse_roi("100,100,200,200")   # {"x","y","w","h","x2","y2","area","space","clamped",...}
frame[y2:y1, x2:x1] = ...            # x2/y2 是开区间边界，可直接切片

encoder = SigLIPEncoder("models/siglip.rknn")
emb = encoder.encode(frame)          # (768,) float32；encoder.ready 为 False
```

约定：

- **坐标平面是 256×256**（native preprocess 的输出），与鼠标坐标同一平面，
  所以 ROI 坐标可以直接交给 `InputSender.send_mouse`，不用换算。
- **超界不报错，而是夹到平面内并置 `clamped=True`**，同时保留 `raw` / `requested`。
  超界是语义问题不是格式问题（`"100,100,200,200"` 在 256 平面上确实超界，
  但那是很自然的写法）；报错会卡死链路，静默截断又会让"以为 200×200、实际 156×156"
  查不出来。格式错误（字段数/非整数/空字段/w,h≤0/负坐标）仍然抛 `RoiError`。
- `SigLIPEncoder` 目前是 **mock**：返回由图像内容决定的**确定性**伪随机向量
  （同一张图恒得同一向量，便于上层逻辑先写先测），但**没有语义**，别拿它做识别。
  `ready` 恒为 `False`。真实现只需替换 `encode()`。
- 视觉层**不做**预处理（native 已做）也**不做** embedding 缓存（失效策略依赖调用场景）。

---

## LLM 三模式

```
edge      板端 RKNN 0.6B 小模型   ⚠ 当前是 mock，真实现待接
cloud     OpenAI 兼容 API          (openai SDK，按需 import)
disabled  不调模型，走 RuleEngine 规则兜底
```

```python
from agent.llm import LLMProvider

provider = LLMProvider(mode=None, tools=router)   # None = 去 config 读 llm.mode
await provider.chat("现在几点", {"state": "idle"})        # -> str，失败抛异常
await provider.chat_with_tools("截个图", {"state": "game"})  # -> dict，失败不抛

provider.set_mode("cloud")     # 运行时切换；set_mode(None) 重新读配置
```

约定：

- **两条入口的错误语义不同**：`chat()` 返回 str、失败**抛出**（终端/GUI 要知道模型挂了）；
  `chat_with_tools()` 是自动循环入口，**永不抛**，错误收进 `{"ok", "text", "tool_calls", "error", "mode"}`。
- 模式从 `llm.mode` 读；非法模式名**降级到 disabled**（记在 `mode_errors`），
  配置写错时系统应该降级可用而不是起不来。默认模式是 `disabled`——
  默认值不该在用户没配置的时候就去调模型/发网络请求。
- `edge` 目前是 mock（`EdgeBackend.is_ready()` 恒为 False），接口先定死，真实现只需替换 `respond()`。
- `openai` SDK **只在 cloud 模式真正请求时**才 import；缺包会给带安装提示的
  `OpenAIClientError`。宿主/板端都没装它，所以 disabled/edge 完全不依赖。

---

## 状态机

四个运行状态，**任何切换都必须经过 IDLE**（IDLE 是唯一的公共锚点）：

```
SLEEP ⇄ IDLE ⇄ STUDY
         ↕
        GAME
```

```python
from agent.core import State, StateMachine

sm = StateMachine()
sm.on_change(lambda old, new: ...)        # 通知 GUI

sm.transition(State.STUDY, "用户说开始学习")   # True
sm.transition(State.GAME, "顺便打会儿游戏")    # False: STUDY 不能直接进 GAME
sm.current()                              # State.STUDY
sm.is_connected()                         # moonlight 连接状态 (与状态无关)
```

约定：

- 非法转换**返回 False**，不抛异常（触发源可能是 IPC 消息或 LLM 输出）。
- 目标状态也接受字符串（`"study"`），方便直接接 IPC 传来的值。
- `is_connected()` 与当前状态**无关**，直接问 native；缺 `.so` 时返回 False。
- 只做"记状态 + 判合法性 + 通知"，**不做**持久化、超时自动转换、变更日志。

---

## 配置

配置放在 `config/`，**真实 `*.yaml` 不入 git**，仓库里只提交 `*.example.yaml` 模板。

```bash
cp config/config.example.yaml       config/config.yaml
cp config/user_profile.example.yaml config/user_profile.yaml
cp config/schedule.example.yaml     config/schedule.yaml
```

没有建真实配置时会自动退回读模板，所以刚 clone 下来也能直接跑。

```python
from agent import config

cfg = config.load_config("config")     # -> dict (命中缓存时不读盘)
config.get("sunshine.host")            # -> "192.168.137.1" (点号路径)
config.get("llm.mode", "board")        # 缺失时给默认值

config.save_config("config", {...})    # 写 config.yaml 并同步刷新缓存
```

约定：

- 配置名走**白名单**（`config.ALLOWED_CONFIGS`），新增配置要同时加名字和模板。
- 不做 schema 校验（各模块校验自己的字段）、不做热重载、不做环境变量替换。
- `AGENT_CONFIG_DIR` 可覆盖配置目录（板端把配置放别处时用）。

---

## 关键约定

- 板端不手改代码，`agent/`、`.so`、`VERSION` 由 `deploy.ps1` 覆盖。
- `send_key` / `send_mouse` / `send_hotkey` 必须释放 GIL，不阻塞 asyncio。
- ZeroMQ PUB 绑定 `0.0.0.0:5555`，SUB 连板端 `:5556`。
- 固件升级后重拉 sysroot 并重新交叉编译，否则 glibc 不匹配。
- GameStream 的 `/applist`、`/launch`、`/resume` **只在 HTTPS 47984** 上，且需要已配对的客户端证书。

---

## 环境

| 项 | 值 |
| --- | --- |
| 工具链 | GCC 9.2-2019.12，`E:/rk3568/arm/` |
| sysroot | `E:/rk3568/sysroot`（从板端 tar 同步；dev 头文件由 `setup-sysroot-deps.ps1` 补） |
| 板端 | `root@192.168.137.30`，`/home/kickpi/myproject/assitant/` |
| 板端 Python | 3.8.10 |
| 板端 GLIBC | 2.31，编译产物最高要求 2.17 |
