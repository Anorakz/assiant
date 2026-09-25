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

⚠ **只读问句在进模型之前就被答掉了**（T8-5c，见 §5.3）：`handle_event()` 先问一遍
`agent/core/read_intents.py`，命中就直连回话（**0 次模型推理**），没命中才走 `chat_with_tools()`。

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

所以 edge 默认在 **system 提示末尾 + 最后一条 user 消息末尾**两处都写 `/no_think`
（`EdgeBackend(no_think=True)`，Qwen3 的软开关），并且**每一次请求**都带
`chat_template_kwargs={"enable_thinking": false}`（见 §4.1）。
板端实测（RK3568 + Qwen3-0.6B-Q4_K_M + llama-server build 10677，温度 0，同一句话）：

| | 耗时 | `reasoning_content` | 正文 |
| --- | --- | --- | --- |
| 不关思考 | 27.0 s | 281 字 | 8 字（`适合编程和调试。`） |
| 关思考（默认） | **2.3 s** | 0 字 | 14 字（`天适合做编程、学习、娱乐等。`） |

### 4.1 为什么**两处**都写 + 模板开关（T8-5b/5c 实测）

只写在 system 里时，**带工具**的那条路会时不时又想起来（6 条提示词里有 1 条产出 339 字
`reasoning_content`，那一轮 211 s）。T8-5b 按"三条路按序试、可行即停"测过：

| 手段 | 结果 |
| --- | --- |
| ① `/no_think` 也放进 **user** 消息末尾 | ✅ 采用 —— 6 条提示词 `reasoning_content` 全为 0 字, 单轮 13~54 s |
| ② 请求里带 `chat_template_kwargs={"enable_thinking": false}` | ✅ 板端实测这个 build **收得下**（HTTP 200、工具调用照旧正确），且模板里明确支持这个变量 |
| ③ 换对话模板（`--chat-template-file`） | 不做；模板里本来就只有 `enable_thinking`, **没有** `/no_think` 的处理 —— 软开关是**模型学过的** |

**T8-5c-4 起两层一起上（这是不变量，不是可选项）**：

```
每一次 edge 请求都必须满足:
  ① user 消息末尾带 /no_think                        —— 模型学过的软开关
  ② chat_template_kwargs={"enable_thinking": false}  —— llama.cpp 的模板开关
                                                        （模板里为假时直接吐空思考块）
```

为什么不能只靠 ①：**工具循环的第二轮里最后一条消息是 `tool`（工具结果）**，那条带
`/no_think` 的 user 消息已经被推到历史深处 —— 而 ② 是模板自己保证的，与第几轮、消息形状
都无关。所以"每一轮都关思考"只能靠 ② 兜底。

⚠ ② 是 **llama.cpp 专有参数**，只发给 edge 后端；`cloud` 一个字节都不带（云端 API 会把它
当未知参数）。两条都有测试钉着（`tests/test_llm.py`：
`test_every_round_of_the_tool_loop_is_thinking_free` / `test_cloud_never_gets_the_llama_template_switch`）。

⚠ ② 必须走 **`extra_body=`**（板端实测踩到）：openai SDK **不接受未知的顶层关键字** ——
直接传 `create(chat_template_kwargs=…)` 会 `TypeError: create() got an unexpected keyword
argument`，然后**整轮降级成规则兜底**（`llm_errors` +1，用户看到"板端模型没有响应"）。
`extra_body` 是 SDK 官方的"塞进请求体"通道。顺带一个教训：`tests/test_llm.py` 的假 client
原本对关键字**照单全收**，所以这个 bug 只在板端暴露 —— 现在假 client 会像真 SDK 一样
对未知顶层键抛 `TypeError`（`_FakeCompletions.ALLOWED` + 一条专门的测试）。

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

⚠ **工具循环里的超时特别容易撞上**（T7-4 实测）：调完工具的**下一轮**请求要带上工具结果，
上下文变长、0.6B 在 RK3568 上常超过 30 s → `APITimeoutError` → 降级成规则兜底，
而工具**其实已经执行完了**（于是"壁纸真的换了、回话却像失败"）。
所以 `config.yaml` 的 `llm.timeout_s` 默认给 **90**（云端可以调小）；
`config/config.example.yaml` 里有这条的说明。

### 5.1 上下文预算几乎全是"工具清单"（T8-5 实测）

板端 **7 个工具时**的实测（`llama-server /tokenize` + 真实 `usage.prompt_tokens`）：

| 组成 | token |
| --- | --- |
| `system` 消息（含 `/no_think`） | 44 |
| 用户那一句"放一首听得最少的歌" | 6 |
| **工具清单（7 个）** | **~1850** |
| ├ 其中最大的一个（`next_wallpaper`，说明里塞了整份标签词表） | 392 |
| └ 其余六个 | 217 ~ 313 |
| 第一轮 prompt 合计 | **1898** |
| + 模型自己那条工具调用（`completion_tokens`） | 200 |
| + 工具结果 | 80 |
| **第二轮 prompt** | **2131** |

> **T8-5b 合并后（三个工具, STUDY 全给）**：工具清单 **1309** token
> （`next_music` 744 / `next_wallpaper` 505 / `back_to_desktop` 58），
> IDLE（少一个回桌面）1251；第一轮 prompt **1495**（IDLE 1410）。
> 也就是工具清单从 ~1850 掉到 ~1300，第一轮 prompt 1898 → 1495；
> 关掉思考后模型自己只花 **30~34** token（不再是 200），第二轮 ≈ 1550 —— `ctx_size 2048`
> 也能装下了（现在配的是 4096，留了余量）。
> ⚠ 加**一个 action**（enum 里多一个值）≈ 十几 token；加**一个工具** ≈ 250~750 token ——
> 所以要加能力优先扩 action。**这条现在是机械守卫**：`tests/test_merged_tools.py::TestPromptBudget`
> 按字符卡上限（清单 3600 / 单工具 2000 字符），超了立刻红（见 architecture.md §4.1.1）。
>
> ⚠ **T8-6** 给 `next_wallpaper` 加了 `least`/`most` 两个 action（"挑用得最少的/最多的"），
> 并顺手把几段说明改短 —— 清单 **3274 → 3522 字符**（≈ +40 token，离 3600 只剩 78 字符）。
> 板端 prompt 没有重量过（按上面那条换算 ≈ 1535 token，`ctx_size` 4096 仍有大余量）；
> 下一次再加东西前得先想"能不能只改措辞"。

结论：
- 工具清单占 prompt 的 **~90%**（合并前）；模型自己只花 30~200 token。
- 所以 `llm.ctx_size` **2048 装不下任何一个工具往返**（第二轮 2131 → llama-server 400
  `exceed_context_size_error` → 降级）。**加一个工具 ≈ 加 200~400 token**：改工具说明或
  加工具后要回头看这个数（`config.example.yaml` 的 `ctx_size` 注释里也写着）。
- 硬件速度（同一台板子实测）：prompt 处理 17~23 token/s、**生成只有 ~1.2 token/s**。
  一轮 200 token 的工具调用要 **168 s** —— 比 `timeout_s: 90` 还长，所以超时那一栏
  （上一节）在"两轮往返"上**必然**会撞。`timeout_s` 要按"轮数 × 生成量"留够。

### 5.2 工具调用成功率：关思考前 vs 关思考后（T8-5b 实测）

同一批 6 条提示词（"放一首听得最少的歌" / "现在在放什么" / "把音量调到 30" /
"清空播放队列" / "换一张安静的深色风景壁纸" / "库里有哪些壁纸标签"），
每条只发**第一轮**请求（不执行工具），温度 0.7，判据是"工具名 + action + 关键参数"：

| 阶段 | 成功 | `reasoning_content` | 单轮耗时 |
| --- | --- | --- | --- |
| ① 只在 system 写 `/no_think`（原来） | **1/6** | 0~339 字（6 条里有 1 条想起来） | 11~211 s |
| ② `/no_think` 也写进 user 消息 | **4/6** | **全 0 字** | 13~54 s |
| ③ ②+壁纸工具宽容两种写法（采纳版） | **5/6** | 全 0 字 | 13~40 s |

失败模式的变化（这就是"边界"）：

| 现象 | ①（关思考前） | ③（采纳版） |
| --- | --- | --- |
| 想太久 | 1 条 339 字思考 / 211 s | 无 |
| 调错工具 | "音量"调成了 `next_wallpaper` | 无 |
| 不调工具 | 2 条（直接编答案 / 复读问题） | 1 条（"现在在放什么"复读问题） |
| 参数写错位置 | `action="next"` + `match`（该是 pick） | 无（工具侧归一化吸收了） |
| 编造 id | `track_id="none"`（真的会失败） | 无（`enqueue` 会去 PC 校验, 编的 id 当场被拒） |

⚠ 曾经没解决的边界（**0.6B 的能力问题, 不是接线问题**）：
"现在在放什么"这类**读状态**的问句它不肯调 `status`（直接复读问题）。
**T8-5c 用"读操作不走模型"治掉了** —— 见下一节。

### 5.3 只读问句直连：读操作不走模型（T8-5c）

分工（你拍的板）：

```
只读问句（在放什么 / 库里有什么 / 壁纸有哪些标签 / 现在是什么壁纸 / 哪张壁纸用得最少）
        ->  agent/core/read_intents.py  直连回答（真实数据拼一句话, **0 次模型推理**）
写意图（放首歌 / 换张壁纸 / 小声点 / 加进队列 …）
        ->  照旧进模型（它负责决定"做什么", 以及把失败说清楚）
```

| 规则 | 为什么 |
| --- | --- |
| 只认**读状态**（5 个意图: `music_status` / `music_library` / `wallpaper_tags` / `wallpaper_now` / `wallpaper_usage`），问候/闲聊/时间**不**直连 | 直连是**固定话术**：用在"报数据"上没损失，用在闲聊上会把对话变僵（那两条路仍由模型/RuleEngine 管） |
| 答案与工具用**同一份数据**（`Runtime.music_state()` / `music_candidates()` / `wallpaper_tags()` / `wallpaper_snapshot()`） | 直连答的数字与工具回话一定一致，不会"两套口径" |
| **依赖缺了不算命中**（音乐没开 → 那两条规则跳过） | 直连不假装知道；交回模型（它至少能照着工具说明说清） |
| 入口自己说"不行"（音乐没开 / 标签没打）→ **原样把那句能照做的话给用户** | 这类话不该丢回模型去猜（它只会编） |
| 匹配**锚住名词/动词**（壁纸规则必须出现"壁纸/图"或"显示的是"） | 踩过：早先的宽匹配把"现在在放什么"当成壁纸问句截胡了（已有测试钉住） |
| T8-6 的 `wallpaper_usage` 另加一道**护栏**：整句要有疑问词（哪/什么/几/多少/吗/？）、且不许出现"换一张/放一首/挑一张"这类**动作** | 同款坑再踩一次：松规则会把写意图"**换一张用得最少的壁纸**"当问句抢走 —— 用户是要换图，不是要报表 |
| 顺序即优先级，**第一个命中即返回**；不认识就返回 `None` | 与 RuleEngine 同一条约定；宁可多花一次推理，也不猜 |
| 正则匹配、**不做意图理解** | 与 RuleEngine 同样的取舍：边界清楚、可单测（`tests/test_read_intents.py` 20 项） |

⚠ 直连**不受状态权限表约束**（它一个字节都不写）：SLEEP/GAME 下问"在放什么"照答。
⚠ 报给用户的壁纸序号是**从 1 开始**的（游标本身 0 基 —— 这里做过一次 +1，别再搞混）。

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
