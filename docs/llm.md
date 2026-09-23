# LLM 层：三种模式、edge 怎么接、怎么验

> 面向"要改 LLM 行为"或"板端模型不答话"的人。配置归属见
> [`config-sources.md`](config-sources.md)；IPC 上怎么推给 GUI 见 [`ipc-protocol.md`](ipc-protocol.md)。
> 代码：`agent/llm/provider.py`（分发 + 两个后端 + 工具循环）、`agent/llm/rule_engine.py`（兜底规则）。

## 1. 三种模式与两条入口

| 模式 | 谁答 | 怎么答 |
| --- | --- | --- |
| `edge` | 板端 llama-server（llama.cpp + GGUF） | OpenAI 兼容 HTTP，**本机** `127.0.0.1:<llm.port>` |
| `cloud` | 远端 OpenAI 兼容 API | 同一套客户端，连 `llm.api_base` + `llm.api_key` |
| `disabled` | RuleEngine（问候/时间/状态…） | 不联网、不调模型 |

别名：`board` / `local` / `onboard` → `edge`，`off` / `none` / `nollm` → `disabled`。
模式从 `llm.mode` 读；非法值降级到 `disabled` 并记进 `mode_errors`（见 `agent/main.py` 启动日志）。

两条入口的错误语义**故意不同**：

| 入口 | 返回 | 后端挂了 |
| --- | --- | --- |
| `chat()` | `str` | **抛**（调用方要能知道模型挂了） |
| `chat_with_tools()` | dict | **不抛**（自动循环必须能继续跑） |

`agent/main.py::handle_event()` 用的都是 `chat_with_tools()`；返回的 dict 形状固定：
`{"ok", "text", "tool_calls", "error", "mode", "degraded"}`。

## 2. edge 就是"连本机 llama-server"

Agent **不加载 GGUF、不跑推理**。真正加载模型的是板端的 `llama-server` 进程
（启动脚本 `llm/scripts/start.sh`，参数来自派生文件 `llm/config/llm.env`）。
Agent 只是它的 OpenAI 兼容客户端：

| Agent 读的键（`llm:` 段） | 变成什么 |
| --- | --- |
| `port` | `base_url = http://127.0.0.1:<port>/v1` |
| `model_name` | 请求里的 `model`（llama-server 的 `--alias`） |
| `local_api_key` | 请求带的 key（**不是**云端的 `llm.api_key`） |
| `max_tokens` / `temperature` | 每次请求的对应参数 |
| `timeout_s` | 请求超时（edge 与 cloud 共用） |
| `model_path` | 只打进日志（Agent 不读这个文件） |

读不到的键各自退回默认值（`port=9000`、`model_name=qwen3-0.6b`），**不抛异常**。
完整的两读者表见 [`config-sources.md` §2.1](config-sources.md)。

⚠ `EdgeBackend.is_ready()` **不联网**：它只查"配置齐不齐 + openai SDK 在不在"。
`True` 不代表 llama-server 活着 —— 真活着的证据是一次成功的请求。

### 2.1 让 Agent 管这个进程的启停（T7-4）

默认**不管**（服务由你或 GUI 的「启动服务」按钮管）。在 `config.yaml` 里打开：

```yaml
llm:
  mode: edge
  manage_service: true     # 只在 mode=edge 时有效
```

打开后（`agent/llm/service.py`）：

| 时机 | 动作 |
| --- | --- |
| Agent 启动 | 跑 `llm/scripts/start.sh` |
| 离开 SLEEP（SLEEP → IDLE） | 跑 `llm/scripts/start.sh` |
| 进入 SLEEP | 跑 `llm/scripts/stop.sh` |
| Agent 退出 | **不停**（退出不等于"睡觉"；要停就进 SLEEP 或手动停） |

- 只调 `llm/scripts/{start,stop}.sh`（**与 GUI 那个按钮同一条路**，PID 文件/日志/端口都在
  脚本里维护）—— 不自己 fork，免得出现"GUI 说在跑、Agent 说没跑"。
- 起完**不等**模型加载：后台探 `/v1/models`（带 key，**200 且带 data** 才算就绪），
  只写一行日志 `llm_service: llama-server 就绪 (等了 23.4 s)`。没就绪时 edge 照旧降级。
- 脚本失败**不 fatal**：记一条 warning，Agent 照常起（服务没起来只该让 edge 降级）。
- 起停动作在**后台线程**里做（状态回调是同步的，不能让 subprocess 卡住状态切换与 GUI 推送）。
- 配置开关认 `true/yes/on/1` 与 `"true"` 这类手写字符串；`mode` 大小写与空格都容错。

## 3. 工具循环：edge 与 cloud 共用一份

```python
for _ in range(max_tool_rounds):          # 默认 4 轮, 防止模型无限要工具
    response = backend.create(messages, tool_schemas)   # 阻塞调用丢线程池
    if 没有 tool_calls:  返回正文
    执行工具 -> 把结果作为 role="tool" 消息回喂 -> 下一轮
```

- 工具清单来自 `ToolRouter.list_tools()`；工具能不能跑由 `Tool.allowed_states` 决定
  （**fail closed**：不允许的状态下只回绝，handler 一次都不跑）。工具层见
  [`architecture.md` §4.1](architecture.md)。
  ⚠ T4 起**丢给模型的清单是按状态过滤的**（`allowed_tools()`）：SLEEP / GAME 下模型根本
  看不到任何工具，而不是"看得见但一调就被拒"。权限表在 `architecture.md` §4.1。
- 轮数用尽 → `ok=False` + `tool loop exceeded N rounds`（不假装正常结束）。
- 工具被拒/参数不合法**不会**让整轮失败：错误进 `tool_calls[].result`，循环继续。
- ⚠ **工具失败一定会出现在正文里**（T7-4）：板端实测 0.6B 会**谎报成功** —— 工具返回
  `{"ok": false, "error": "scene 轴上没有 'darkness'…"}`，它却回"已更换为宁静的深色风景"。
  工具结果只有模型看得见，所以 `_result()` 把失败追加到 `text` 末尾
  （`⚠ 换壁纸没有成功：…`，工具给的 `tell_user` 原句优先，否则 `⚠ <工具名> 没有成功：<error>`），
  另外在结果里留一份 `tool_failures` 给测试与上层查。成功时**一个字都不加**。

## 4. Qwen3 的思考模式：`/no_think`（板端实测）

Qwen3 默认"先想再答"。0.6B 上这一步**只花时间不涨质量**，而且会把 `max_tokens` 烧掉，
出现 `finish_reason="length"` + `content=""`（正文一个字都没有）。

所以 edge 默认在 system 提示末尾加 `/no_think`（`EdgeBackend(no_think=True)`，Qwen3 的软开关）。
板端实测（RK3568 + Qwen3-0.6B-Q4_K_M + llama-server build 10677，温度 0，同一句话）：

| | 耗时 | `reasoning_content` | 正文 |
| --- | --- | --- | --- |
| 不关思考 | 27.0 s | 281 字 | 8 字（`适合编程和调试。`） |
| 关思考（默认） | **2.3 s** | 0 字 | 14 字（`天适合做编程、学习、娱乐等。`） |

正文为空时**不当成"模型说了空话"**：`chat_with_tools()` 按"模型没给正文"降级（下面一节），
理由里会写明是 `finish_reason` 还是"思考吃掉了预算"。

## 5. edge 挂了怎么办：降级，但说清

| 情形 | 结果 |
| --- | --- |
| `chat()` 且后端不可达 | 抛出原异常（如 `APIConnectionError`） |
| `chat_with_tools()` 且后端不可达 | `ok=True` + **规则兜底**，正文前缀"（板端模型没有响应，这条是规则兜底）"，`degraded` 写原因 |
| `chat_with_tools()` 且模型没给正文 | 同上，`degraded` 里是 `finish_reason` / 思考占满的解释 |
| 规则兜底自己也炸了 | `ok=False` + `error`（两条原因都在里面） |
| `cloud` 后端不可达 | 保持原样：`ok=False` + 原因，**不降级** |

为什么正文里也要带那句：**GUI 只显示 `text`**，看不到日志里的 warning。
`agent/main.py` 另外会记一条 `LLM 降级为规则兜底: <原因>` 的 warning 并计入 `llm_errors`。

## 6. 怎么验

```bash
# 单测（PC 与板端同一套）: 替身是同一个假 openai client, 工具循环是同一份代码
python tests/test_llm.py

# 板端: 起 llama-server（健康检查 /health, 日志 llm/logs/llama-server.log）
bash llm/scripts/start.sh

# 板端: 探针（不起 Agent, 直接打 provider）—— 对照 /no_think、工具循环、降级三种情形
python3 /tmp/t2_demo.py

# 板端: 起 Agent 后用 CLI 问一句（走 IPC 到 Agent, 由 active 模式回答）
python3 agent/main.py --config config/config.yaml &
python3 -m agent.cli chat "你好，你是谁"
```

启动日志里那行 `llm: edge 后端 = llama-server http://127.0.0.1:9000/v1 model=… max_tokens=… temperature=… no_think=True`
就是"连哪儿、哪个模型、带什么参数"的答案 —— 板端排障第一眼看它。

板端跑过的证据（T2 验收时记录，探针在临时文件 `logs/t2_demo.py`）：

- `provider.chat("你好，你是谁？")` → `我是RK3568开发板上的桌面助手。`（2.6 s）
- `chat_with_tools("现在板子上几点？用工具查一下。")` → 模型自己调 `get_board_time`，
  工具返回 `2026-09-22 20:05:31`，模型据此作答
- 端口指向没人听的 `9099`：`chat()` 抛 `APIConnectionError`；
  `chat_with_tools()` 降级并给出 `degraded="APIConnectionError: Connection error."`

## 7. 边界（按约定不做的事）

- **不做**模型文件的加载/量化/转换 —— 那是 llama.cpp 与 `llm/` 那一堆脚本的事。
- **不做** prompt 模板管理：系统提示写死在 `provider.py` 的 `_SYSTEM_PROMPT`。
- **不做** token 计数 / 成本统计 / 会话历史持久化（每次调用都是新的 messages）。
- **不做**多模型路由：edge 只有一个模型，就是 `llm.model_name`。
