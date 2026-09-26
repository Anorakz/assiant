# Agent

开发机（Windows PC）上的源码工作区，也是**唯一提交方**。负责 C++/pybind11 底层、
Python Agent Core、Qt5 C++ GUI 的开发、交叉编译与部署；GUI 的**编译在板端**进行
（板端装了 Qt5，PC 上没有），见 [`docs/architecture.md`](docs/architecture.md)。

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
│   ├── cli.py                   # 板端控制 CLI (assistant): status/chat/mode/watch/schedule/doctor/cleanup
│   ├── core/                    # 状态层、工具路由、调度层、壁纸游标
│   │   ├── state_machine.py     # SLEEP ⇄ IDLE ⇄ STUDY/GAME 状态机
│   │   ├── tool_router.py       # 工具注册 / 权限控制 / 执行调度
│   │   ├── label_spec.py        # 标签/条件的**统一小语法**（壁纸 match= 与音乐 tag= 共用，T8-5b）
│   │   ├── read_intents.py      # **只读问句直连**（在放什么/库里有什么/壁纸标签/哪张用得最少，0 次模型推理，T8-5c/T8-6）
│   │   ├── chat_memory.py       # **纯对话记忆**（有界、不落盘；记"谁说的/说了什么/当时哪个模式"，T9-1）
│   │   ├── user_profile.py      # **用户画像内核**（IP/歌手**权重** + 心情调模型 + 负反馈清零 + 挑图/补歌配方，T9-2/T10）
│   │   ├── scheduler.py         # 日程检查 + 定时触发 + 终端命令监听
│   │   ├── wallpaper.py         # 壁纸目录 + 游标 + **三格窗口 prev/current/next**（可在候选里翻，T7-3/T10-3）
│   │   └── music.py             # 播放内核: 环形队列 / 轮询真实进度 / 30 秒计一次 / **补歌到目标长度** (T8-4/T10-4)
│   ├── media/                   # 本地媒体库 (T8-3)
│   │   └── music_library.py     # config/music_library.jsonl: 读写/合并/打标/挑选（纯 Python）
│   ├── tools/                   # 具体工具 (Phase 7; T8-5b 合并成三个):
│   │                            #   back_to_desktop / next_wallpaper / next_music
│   ├── llm/                     # LLM 三模式 + 规则兜底
│   │   ├── provider.py          # edge / cloud / disabled 分发 (edge 连本机 llama-server)
│   │   └── rule_engine.py       # 无 LLM 时的正则规则兜底
│   ├── ipc/                     # Agent ⇄ GUI 通信
│   │   ├── protocol.py          # IPC 协议: topic/command 常量 + 编解码
│   │   ├── local_server.py      # 唯一的生产 server (Unix socket, 收命令信封)
│   │   └── local_client.py      # 客户端 (板端脚本 / 活体验证用)
│   ├── vision/                  # 视觉层
│   │   ├── roi.py               # ROI 字符串解析 ("x,y,w,h")
│   │   ├── siglip_encoder.py    # 实时帧那条路 (⚠ 仍是 mock)
│   │   ├── siglip/              # 真 RKNN 双塔: 离线打标签 / 图像检索 (T7-1)
│   │   ├── tag_vocab.py         # 三轴标签词表 (T7-2)
│   │   ├── wall_data.py         # config/wall_data.jsonl 的唯一写者 (T7-2)
│   │   ├── tagger.py            # 打标签 (板端 NPU, T7-2)
│   │   └── tag_index.py         # 标签索引 + IP 锚点挑图 (纯 Python, T7-3)
│   └── io/                      # native 的 asyncio 包装 + 输入汇聚
│       ├── chat_bus.py          # ChatInputBus: 终端/GUI 统一事件流
│       ├── image_reader.py      # ImageReader: image_rb → numpy 帧
│       ├── input_sender.py      # InputSender: send_key / send_hotkey / send_mouse
│       └── _native.py           # native 解析 + 专属单线程执行器 (SPSC)
│   └── net/                     # 对外服务客户端
│       ├── sunshine_client.py   # Sunshine 串流主机 API
│       ├── netease_cli.py       # 板端 ssh 调 PC 上第三方 neteasecli（PC 出声，T8-2）
│       ├── bilibili_api.py      # B 站唯一网络层: 匿名会话/cookie/搜索/直链/错误话术（T11-1）
│       └── pc_probe.py          # 问 PC 上跑着什么进程（认游戏的"真值"那一路，T11-4）
├── gui/                         # Qt5 C++ GUI (在板端编译: src/ tests/ tools/)
│                                 #   GAME 主区: 视频区 + 预览栏 + 只读地址栏 (T9/T11-7)
│                                 #   下区域 GAME=封面 / 其它=音乐条
├── config/                      # 配置模板 (真实配置不入 git)
│   ├── config.example.yaml      # 唯一真源模板: llm.* / bilibili.* / wallpaper.* / gui.* / sunshine.* / ipc.*
│   ├── user_profile.example.yaml# 用户画像
├── scripts/                     # 构建 / 部署 / 测试 / 配对工具
│   ├── build.ps1                # aarch64 交叉编译
│   ├── test-host.ps1            # 宿主机 C++ 单测 (ctest)
│   ├── test-python.ps1          # 宿主机 Python 单测 (unittest)
│   ├── deploy.ps1               # 打包并推到板端
│   ├── setup-sysroot-deps.ps1   # 给 sysroot 补 python3.8-dev / ffmpeg-dev
│   ├── make-wallpaper-samples.py# 造几张不同比例的纯色壁纸样张 (T3 验收用)
│   └── pair_sunshine.py 等      # Sunshine SRSAES 配对 (见 docs/)
├── tests/                       # 宿主机单测
│   ├── CMakeLists.txt
│   ├── test_*.cpp               # C++ 单测 (gtest, 由 ctest 驱动)
│   ├── test_config.py           # 配置模块单测 (unittest)
│   ├── test_state_machine.py    # 状态机单测
│   ├── test_tool_router.py      # 工具注册/权限/参数校验/超时/异常单测
│   ├── test_llm.py              # LLM 三模式切换 / edge 真后端 / 工具循环 / 规则匹配
│   ├── test_vision.py           # ROI 解析 / SigLIP mock (有无 numpy 两条路径)
│   ├── test_siglip.py           # SigLIP 双塔: 配置契约 / 分词 / 余弦排序 (T7-1)
│   ├── test_wall_data.py        # 壁纸词表 / 标签数据文件 / 增量计划 (T7-2)
│   ├── test_main.py             # 进程装配: 启停顺序 / 异常隔离 / 主循环
│   ├── test_ipc_protocol.py     # IPC 线格式契约 (字节级)
│   ├── test_scheduler.py        # 日程触发 / 去重 / 终端命令识别
│   ├── test_chat_bus.py         # ChatInputBus 单测
│   ├── test_io.py               # image_reader / input_sender
│   ├── test_docs.py             # 文档守卫: 链接有效 + 过时说法黑名单
│   ├── test_config_source_guard.py  # 配置真源守卫: agent/ 只认 config/config.yaml
│   ├── test_schedule_config.py  # 文本级删掉已触发的一次性日程
│   ├── test_cli.py              # CLI 九条命令（T11-10c 加了 music 传输控制）/ 窗口与尾巴 / cleanup / tag
│   ├── test_tools.py            # 工具层: 注册 / 状态权限 / 参数校验 / 缺依赖跳过 (T1)
│   ├── test_tool_permissions.py # 状态权限表: 4 状态 × 每个工具, 禁止的组合真的被拒 (T4)
│   ├── test_wallpaper.py        # 壁纸目录游标 / 三格窗口(prev/current/next) / next_wallpaper 工具与命令 (T3/T10-3)
│   ├── test_netease_cli.py      # ssh 调 PC 的第三方 neteasecli: 命令行 / 信封 / 四类错误 (T8-2)
│   ├── test_bilibili_api.py     # B 站唯一网络层: 搜索/详情/直链(单文件 vs DASH)/cookie/错误话术 (T11-1)
│   ├── test_bilibili_queue.py   # B 站队列: 3×预览栏格数的滑动窗口 / 往哪边走往哪边补页 / 边界 (T11-2)
│   ├── test_bilibili_buffer.py  # B 站缓冲代理: ffmpeg 合流->MPEG-TS->FIFO / 15 s 门槛 / 暂停延到 60 s (T11-3)
│   ├── test_bilibili_tool.py    # B 站工具: 只有一个 keyword / 只在 GAME 可见 / 参数归一化 (T11-5)
│   ├── test_bilibili_config.py  # B 站配置守卫: 模板能被真构造器吃下 / 键不多不少 / 默认值对齐 (T11-8)
│   ├── test_game_watch.py       # 游戏观察器: 画面锚点 vs PC 进程双路 / 自学习纠错 / 常驻策略 (T11-4)
│   ├── test_music_library.py    # 本地音乐库: 读写 / 合并 / 打标 / 挑选 (T8-3)
│   ├── test_music_player.py     # 播放内核: 环形队列 / 30 秒计一次 / 曲终自动下一首 / 补歌两段式 (T8-4/T10-4)
│   ├── test_label_spec.py       # 统一标签语法: 拆键 / 拆值 / 多轴 / 壁纸那边只用这一份 (T8-5b)
│   ├── test_merged_tools.py     # 三个工具: action 分派 / 缺依赖跳过 / 清单**预算守卫** (T8-5b/5c)
│   ├── test_tool_normalize.py   # 参数归一化: 真实错法 → 规范形 / 校验前跑 / 边界 (T8-5c)
│   ├── test_read_intents.py     # 只读问句直连: 该直连的/不该截胡的/拿不到数据 (T8-5c)
│   ├── test_chat_memory.py      # 纯对话记忆: 只收对话源 / 有界 / **不碰盘** / 场景与时间 (T9-1)
│   ├── test_user_profile.py     # 用户画像: 权重配方 / 负反馈清零(两层) / 心情解析 / 落盘 / 消费方 (T9-2/T10)
│   ├── mocks/                   # mock_agent_native: native 替身
│   ├── host/                    # 需要 numpy 的绑定层测试 (按需手动跑)
│   └── board/                   # 板端真机验收脚本
├── docs/                        # 文档 (入口: docs/architecture.md)
│   ├── architecture.md           # 架构与拓扑、板端布局
│   ├── deploy.md                # 部署与双机同步规则
│   ├── config-sources.md        # 配置来源: 谁写 / 谁读 / 谁派生
│   ├── cli.md                   # 板端控制 CLI (assistant) 使用手册
│   ├── tagging.md               # 壁纸标签化: 词表 / wall_data.jsonl / IP 检索 / 三格窗口与画像挑图 (T7/T10-3)
│   ├── music.md                 # 音乐: 板端 ssh 调 PC neteasecli / 本地库 / 工具四件套 / 自动补歌 (T8/T10-4)
│   ├── profile.md               # 用户画像: 纯对话记忆 / IP·歌手权重 / 负反馈两层清零 / 心情 / **谁在消费它** (T9/T10)
│   ├── bilibili.md              # B 站视频: 队列滑动窗口 / 缓冲 FIFO / 清晰度与 cookie 真相 / 双路认游戏 / 实测数字 (T11)
│   ├── llm.md                   # LLM 三模式 / edge 接 llama-server / 工具循环 / 降级
│   ├── ipc-protocol.md          # Agent ⇄ GUI 协议 (线上格式唯一真源)
│   ├── gui.md                   # GUI 构建与使用
│   ├── gui-agent-integration.md # GUI 那一端实际收/发什么
│   ├── cross-build-rk3568.md    # 交叉编译与 sysroot
│   ├── decoder-mpp.md           # MPP 硬解
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

router.list_tools()                       # 注册了什么（与状态无关）
router.allowed_tools()                    # 当前**状态**下能用的那些（给 LLM 的候选）
await router.execute("screenshot", {})    # {"ok": True, "result": ...}
                                          # 或 {"ok": False, "error": "..."}
```

一次 `execute` 的顺序：**工具存在 → 状态允许 → 参数符合 schema → 执行（带超时）→ 异常兜底**。
`execute` **永不抛异常**，调用方（LLM 循环）只看 `ok`。默认超时 5s。

约定：

- 状态不允许时工具**绝不执行**；拿不到当前状态时 **fail closed**（拒绝而不是放行）。
  **Phase 7 起丢给模型的候选也按状态过滤**（`allowed_tools()`）—— 不可用的工具不该出现在
  候选里。当前这张权限表（`tests/test_tool_permissions.py::EXPECTED` 是唯一真源）：

  | 状态 | `back_to_desktop` | `next_wallpaper` | `next_music` | `bilibili_search` |
  | --- | --- | --- | --- | --- |
  | `SLEEP` | ✗ | ✗ | ✗ | ✗ |
  | `IDLE` | ✗ | ✓ | ✓ | ✗ |
  | `STUDY` | ✓ | ✓ | ✓ | ✗ |
  | `GAME` | ✗ | ✗ | ✗ | ✓ |

  **T8-5b 把七个工具合并成三个**（每个工具用 `action` 分派具体动作）：
  `next_wallpaper` = 翻页/按内容挑/**按用量挑**（`action=least`/`most`，T8-6）/
  **只预备下一个**（`action=stage`，T10-3）/看标签；
  `next_music` = 排队列/清空/清单/搜/状态/标签/音量
  （transport 交给 GUI 四个按钮）；`back_to_desktop` = 回桌面（无参数，只 STUDY）。
  动机是 **prompt 预算**：T8-5 实测 7 个工具的工具清单占第一轮 prompt 的 90%
  （1699 / 1898 token），第二轮 2131 直接撞穿 ctx。链路见
  [`docs/music.md`](docs/music.md) 与 [`docs/tagging.md`](docs/tagging.md)。

  **T11-5 加了第四个工具 `bilibili_search`**（**只在 GAME**）：只有一个必填 `keyword`、
  没有 `action` 枚举，职责**只有"把对话里的关键词交给 B 站队列"**（搜+排+填预览栏，**不播**）。
  放在 GAME 而不是 STUDY 有**预算**上的道理：放进 STUDY 会把清单从 3562 顶到 4139（超预算），
  放 GAME 则一个字符都不占别人的额度（GAME 那份清单 544 字符）。链路见
  [`docs/bilibili.md`](docs/bilibili.md)。

  **T7-3 撤掉了 T6① 的一半**：以前 GUI 的 `next_wallpaper` 命令与 LLM 的工具共用这张表
  （命令路径问 `ToolRouter.allowed_in_current_state()`）。按需求**手动换壁纸已删除**
  （GUI 按钮 + 同名 IPC 命令），那个方法随之删除 —— 这张表现在只管工具，
  而工具只有**对话**一条路能调到，所以答案仍然只有一个。
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

启动顺序（按依赖，停止时**严格反向**）—— 就是 `Runtime._STEPS` 那一串：

```
日志 → config → native → ChatInputBus → io 层
     → music（工具要在建的时候就位）→ bilibili（同上）
     → StateMachine + ToolRouter → llama-server 服务 → LLMProvider
     → profile（判心情要问模型）→ Scheduler → IPC → 终端输入
```

> ⚠ **音乐与 B 站都排在"建工具"之前**：`agent/tools/` 里的那几个工具要**建的时候**就知道
> 对应子系统"开没开"（没开就整个不装）—— 顺序错了会变成"工具装了但一调就报错"。

约定：

- **单组件失败不影响其他组件**：每个组件包两层保护（组件自身失败、步骤里组件之外的代码
  失败），失败记进 `failures` 并在收尾汇总。可操作性优先于完整性 —— 摄像头没插不该让
  命令监听也失效。
- **日志**：同时写 `logs/agent.log`（含 DEBUG）和 stdout（INFO 起）。
  正常跑只有 INFO + 少量自解释的 WARNING；**完整 traceback 只进 DEBUG**，
  免得正常日志看起来像崩了。排查时把级别调到 DEBUG 即可。
- 关闭：`SIGINT` / `SIGTERM` 都会触发干净退出（systemd 停服务发的是 SIGTERM）。
- 尚未实现、启动时跳过并记一条 WARNING 的组件：`agent/tools/` 里某个工具**缺依赖**时只跳过
  那一个（Phase 7 起 `agent/tools/` 本身有真工具了：`back_to_desktop` / `next_wallpaper`）、
  `agent/ipc/` 缺失或没有 `build_ipc()`（GUI 连不上）。它们**不算失败**，放到位即自动接入，
  不用改 `main.py`。

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

## 板端控制 CLI（`assistant`）

ssh 进来不用敲一长串 `python3 -m agent.cli`，板端装了启动器 `/usr/local/bin/assistant`：

```bash
assistant status            # 当前模式 + 串流连接（等不到就如实说没推，不编一个）
assistant chat 现在几点      # 发一条给 Agent 并等回复（与 GUI 输入框同一条路）
assistant mode study        # 切模式（大小写不限；非法转换会被状态机拒掉）
assistant watch             # 盯推送（排障主力；--topics a,b / --count N）
assistant schedule          # **接下来 24 小时**（--hours 可调）的日程，标出真的触发过的
assistant cleanup           # 清理**已经过去**的一次性日程（默认只看，--apply 才删）
assistant doctor            # 体检: 配置 / socket / 派生 llm.env / 日程 / 关键路径
```

- 走的是**现有 IPC 协议**：不 import Agent、不读它的内存。所以 Agent 没跑时它会明确说"连不上"
  并给出下一步，而不是给一份假状态。
- 日程有两件事分得很清：**列出来**（CLI 自己用 `agent.core.scheduler` 语义算，不需要 Agent 在跑）
  与 **"到底触发过哪条"**（只能问运行中的 Agent —— 协议 `query_schedule`）。问不到时只标
  「已过（按时间比较）」并写明原因，**不会**含糊成"没触发"；触发记录只在 Agent 内存里，重启即清零。
- **只显示"接下来 N 小时"**（默认 24，`--hours` 可调；窗口里另带最近 30 分钟刚过去的那些）——
  所以下午看时"今天早上那条"是不出现的。GUI 日程区同一套口径。
- 完整说明（窗口与尾巴、三种标记、退出码、公共选项、边界、排障）：[`docs/cli.md`](docs/cli.md)。

---

## 调度器

日程检查 + 定时触发 + 终端命令监听。触发动作只有两种：**状态转换** 和 **发消息给 Agent**
（`bus.push("scheduler", ...)`，和终端/GUI 同一个入口）。触发时两者可同时做。

```python
from agent.core import Scheduler

scheduler = Scheduler(state=sm, bus=bus, config=cfg)
await scheduler.start()          # 起日程循环 + 命令订阅
await scheduler.stop()

await scheduler.check_schedule() # 也可手动跑一轮，返回本次真正触发的事件
```

配置（`interval_min` / `window_min` / `late_grace_min` / `commands` 在 `scheduler` 段，
`recurring` / `oneoff` 两处都认，方便直接喂 `schedule.yaml`）：

```yaml
scheduler:
  interval_min: 1          # 每 1 分钟检查一次
  window_min: 1            # 触发窗口宽度
  late_grace_min: 5        # 迟到的容忍度(休眠/重启后晚几分钟仍认)
  commands:                # 终端命令 -> 动作; 整行相等才命中
    - command: "study"
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
- **命令是订阅式的**：`bus.subscribe()` 只看不取。**不能**用 `get()` 读——那会把
  终端/GUI 的用户消息一起吃进调度器，下游再也看不到。只认 `source == "terminal"`。
- **命令匹配是"整行相等"**（去首尾空白、大小写不敏感，不做分词/前缀/缩写）——
  少一层规则就少一处两边理解不一致的地方。
- **⚠ 命中不会把事件拿走**：终端里敲 `study` 会有两个效果——状态切到 STUDY，
  **并且那行文本照常被主循环送给 LLM**。要"只生效一次"就得让命令走一条不经过 LLM
  的通道，那是另一个改动（见 `listen_commands` 的 @note）。
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
edge      板端本地模型: llama.cpp GGUF, 经本机 llama-server（OpenAI 兼容接口）
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
  `chat_with_tools()` 是自动循环入口，**永不抛**，错误收进
  `{"ok", "text", "tool_calls", "error", "mode", "degraded"}`。
- 模式从 `llm.mode` 读；非法模式名**降级到 disabled**（记在 `mode_errors`），
  配置写错时系统应该降级可用而不是起不来。默认模式是 `disabled`——
  默认值不该在用户没配置的时候就去调模型/发网络请求。
- `edge` 是**真后端**：Agent 不加载 GGUF，它只连本机 llama-server
  （`llm.port` / `llm.model_name` / `llm.local_api_key`）。edge 与 cloud **共用同一个
  工具循环**，所以板端小模型也能 function call。
- edge 挂了不会装作答过：`chat()` 照抛，`chat_with_tools()` **退回规则兜底**，
  正文前面带一句"（板端模型没有响应，这条是规则兜底）"，结果里另有 `degraded` 写原因。
- `openai` SDK **只在真正要发请求时**才 import；缺包会给带安装提示的
  `OpenAIClientError`。只有 `disabled` 完全不依赖它。
- 细节（板端实测数据、`/no_think`、怎么验）：[`docs/llm.md`](docs/llm.md)。

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
```

日程写在 `config.yaml` 的 `scheduler:` 段（`recurring` / `oneoff` 的写法与真正会被读的键，
见 `config/config.example.yaml` 里那一大段注释 —— 模板里不放真日程，免得刚 clone 下来
就凭空多出提醒）。

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
- **壁纸是"三格窗口"**（`prev`/`current`/`next`，进程内、不落盘）：换图 = `next` 变当前并**立刻**按画像补一个新的 `next`；`action=stage` 只预挑不切屏（T10-3）。
- **用户画像只被 Agent 自己读**（挑下一个壁纸 / 补歌 / 判负反馈 / 心情变了重置队列），模型既看不到也调不到它（T9 定、T10 消费，见 [`docs/profile.md`](docs/profile.md) §8）。
- **音乐队列维持 30 首**：缺了先吃本地库、不够**只按画像里的歌手**去 PC 搜（`music.autofill`，T10-4）；负反馈把类似的歌整批移出队列。
- **B 站视频只在板端放、内容不落盘**：队列只存地址（3× 预览栏格数的滑动窗口），**只有 GUI 操作才开始播**（点预览图/上一集/下一集），流走 **ffmpeg `-c copy` → MPEG-TS → FIFO**（板端 `souphttpsrc` 是坏的，不走 HTTP），解码用板端 MPP 硬解；清晰度只走聊天气泡、界面不显示 —— 见 [`docs/bilibili.md`](docs/bilibili.md)。
- **认游戏是"画面筛一遍 + PC 进程裁决"**：两路不一致时**以进程为准**并把那一帧写回锚点库（自纠错）；**有对话关键词就一帧都不抓**（T11）。
- `send_key` / `send_mouse` / `send_hotkey` 必须释放 GIL，不阻塞 asyncio。
- Agent ⇄ GUI 走**同机 Unix domain socket**（`/tmp/agent.sock`），两个方向的信封不同 —— 见 [`docs/ipc-protocol.md`](docs/ipc-protocol.md)。
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
