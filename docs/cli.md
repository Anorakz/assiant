# 板端控制 CLI（`assistant`）

Agent 有**两条前端**：板端那块屏上的 GUI，和这里的 CLI。CLI 给 ssh / 脚本用 ——
看一眼状态、发一条消息、切模式、盯推送、查日程、做体检。

| 项 | 值 |
| --- | --- |
| 代码 | `agent/cli.py`（单文件，标准库 + 仓库其它模块，没有额外依赖） |
| 板端入口 | `/usr/local/bin/assistant` → `scripts/assistant`（见 §1） |
| 走的通道 | **现有 IPC 协议**（[`ipc-protocol.md`](ipc-protocol.md) 是线上格式唯一真源） |
| 单测 | `tests/test_cli.py`（PC 与板端都跑；涉及真 socket 的用例在 Windows 上 skip） |

---

## 1. 怎么跑

板端（推荐，装好启动器之后从任何目录都能用）：

```bash
assistant status
assistant schedule
assistant watch --topics schedule
```

没有启动器时，或想在 PC 上看代码行为时：

```bash
python3 -m agent.cli status          # ⚠ 用 -m，不要在 agent/ 里直接 python cli.py
```

> ⚠ **必须用 `-m agent.cli`**：直接 `python3 agent/cli.py` 会让 `sys.path[0]` 变成 `agent/`，
> `import agent.config` 立刻 `ModuleNotFoundError`。启动器做的就是"把 cwd 固定到仓库根 + 用
> `-m` 起解释器"这两件事。

---

## 2. 公共选项

| 选项 | 默认 | 说明 |
| --- | --- | --- |
| `--socket PATH` | 配置里的 `ipc.socket_path`，再不行协议默认 `/tmp/agent.sock` | 优先级：**命令行 > 配置 > 协议默认** |
| `--config PATH` | 按名字找（`AGENT_CONFIG_DIR` / 仓库 `config/`） | ⚠ 语义与 `agent/main.py` 一致：给的是**文件路径**，其**父目录**必须含 `config.yaml` 或 `config.example.yaml` |
| `--timeout SEC` | `3.0` | 等一条推送的秒数（`schedule` 用它等 `query_schedule` 的应答） |

这三个选项写在**子命令前或后都行**：`assistant --socket X status` ≡ `assistant status --socket X`。

⚠ `watch` **没有** `--timeout`：它一直盯到 Ctrl-C / `--count`，一个"超时"对它没有意义；
给 `watch` 传 `--timeout` 会被当成参数错误（退出码 2）。

---

## 3. 退出码

| 码 | 含义 |
| --- | --- |
| `0` | 成功。**包括**"连上了但对方没推送"这类"如实说明"的情况（见 `status`） |
| `1` | 环境或连接问题（socket 不存在、等不到回复、配置读不出来） |
| `2` | 参数错（argparse 的默认行为：不认识的命令/选项、`mode` 给了非法值） |

判据是"用户是不是得到了他要的答案"，不是"有没有发生 IO"：`status` 等不到推送**不是**错误
（协议上 Agent 只在状态变化时推），而"我发了话却没回音"（`chat` 超时）**是**错误。

---

## 4. 六条命令

### `status` —— 看一眼当前模式与串流连接

```bash
$ assistant status
已连上 Agent：/tmp/agent.sock
模式 STUDY · 串流已连接
```

Agent 只在**状态变化**时推 `status`，客户端连上时不会补一条 —— 等不到就如实说：

```bash
$ assistant status
已连上 Agent：/tmp/agent.sock
但它还没推 status：协议上 Agent 只在状态**变化**时推，客户端连上时不会主动补一条。
想看后续变化：assistant watch（Ctrl-C 退出）。
```

> 这就是"不编一个 IDLE 出来"：CLI 不 import Agent 去读它的内存，所以它**只能**报它真的收到的东西。

### `chat` —— 发一条消息给 Agent 并等回复

```bash
$ assistant chat 现在几点
助手: 现在是 2026-09-22 13:11:03。

$ assistant chat 你好 世界        # 多段自动用空格连起来
$ assistant chat 喂 --no-wait     # 发出去就返回，不等 llm 回复
```

走的是与 GUI 输入框**完全相同**的一条路（`chat_input` → ChatInputBus → LLM/规则兜底）。

### `mode` —— 切模式

```bash
$ assistant mode study
已切到 STUDY

$ assistant mode game            # STUDY -> GAME 非法
Agent 没切过去：当前仍是 STUDY —— 非法转换会被状态机拒掉（协议 §4）。   # stderr, 退出码 1
```

取值 `SLEEP/IDLE/STUDY/GAME`，大小写不限。CLI **不乐观地**认为切换成功：它等 Agent 推回来的
`status`，只有线上报的真的是目标模式才算成功。

### `watch` —— 盯推送（排障主力）

```bash
$ assistant watch --topics schedule
在听 /tmp/agent.sock 的推送（只看 schedule，Ctrl-C 退出）
13:28:09  schedule  kind=fired title=触发事实验收 date=2026-09-22 scheduled_at=2026-09-22T13:28 fired_at=2026-09-22T13:28:09
收到 1 条推送，用时 81.7 秒
```

| 选项 | 说明 |
| --- | --- |
| `--topics a,b` | 只看这些 topic（默认全看）。`schedule` 的推送**单独渲染**成一行 key=value，不会把嵌套的事实对象原样倒出来 |
| `--count N` | 收够 N 条就退出（0 = 不限；给脚本/测试用） |

Ctrl-C 会打印"收到 N 条推送，用时 X 秒"再干净退出。

### `schedule` —— 今天/明天的日程，标出**真的触发过**的那些

```bash
$ assistant schedule
日程（共 4 条；/home/kickpi/myproject/assitant/config/config.yaml）
今天（2026-09-22 周二）
  08:30  晨间计划  ← 已过（未触发）
  13:00-13:30  午休  ← 已触发 13:00:03
  14:00-15:30  项目评审
明天（2026-09-23 周三）
  08:30  晨间计划
  10:00  周会
  13:00-13:30  午休
✓ 触发记录来自运行中的 Agent 本人（本次 1 条）——「未触发」是**这个 Agent 进程**没触发过，
  不是配置里没有；记录只在内存里，Agent 重启即清零。
```

| 选项 | 说明 |
| --- | --- |
| `--today` / `--tomorrow` | 只列一段（默认两段都列） |
| `--limit N` | 每天最多列几行（0 = 不限，默认 10） |
| `--no-ask` | **不问** Agent，只按时间比较（离线/对比用） |

**行格式与 GUI 日程区逐行一致**（`HH:MM[-HH:MM]  标题`），所以"CLI 列出来的"和"界面上显示的"
可以直接 diff。日程本身用**真的** `agent.core.scheduler` 语义展开（`occurs_on()` / `trigger_at()`），
所以 recurring 看星期、oneoff 看日期、提前量跨天都对。

#### 三种标记，别把它们读成同一件事

| 标记 | 意思 | 谁说的 |
| --- | --- | --- |
| `← 已触发 13:00:03` | **这个 Agent 进程真的触发过这一条**，时刻是 `fired_at` | 运行中的 Agent（`query_schedule` 的应答） |
| `← 已过（未触发）` | 现在过了 `start`，而 Agent 的记录里**没有**它 | 时间比较 + Agent 的记录（两件事都成立才敢这么标） |
| `← 已过` | 现在过了 `start`（纯时间比较） | CLI 自己算的 |
| （什么都不标） | 还没到点 | —— |

**为什么这么较真**：`现在 > start` 与"Agent 触发过"是**两个不同的事实**。前者在 Agent 根本没跑时
照样成立，后者只存在于运行中进程的内存里。所以：

- 问到了 Agent → 敢说「已触发」/「未触发」，页脚写明记录来自运行中的 Agent；
- 问不到（Agent 没跑 / `--no-ask`）→ 只说「已过」，页脚写明"这是按时间比较的"，**一个字都不提
  触发与否**：

```bash
$ assistant schedule --no-ask --today
日程（共 4 条；/home/kickpi/myproject/assitant/config/config.yaml）
今天（2026-09-22 周二）
  08:30  晨间计划  ← 已过
  13:00-13:30  午休  ← 已过
  14:00-15:30  项目评审
⚠ 「已过」只是「现在过了 start 时刻」（按时间比较），**不代表 Agent 已经触发过**。
（你用了 --no-ask —— 所以这里只能按时间比较。）
```

Agent 没在跑时（原因会写在 stderr 与页脚里，两种原因分得清）：

```text
问不到 Agent 的触发记录：连不上 Agent（连接 /tmp/agent.sock 失败: [Errno 2] No such file or directory）
问不到 Agent 的触发记录：Agent 在 3.0 秒内没回日程快照
```

> ⚠ **触发记录只在内存里**：Agent 重启即清零，且不是"落盘的日志"。所以 `已触发` 是**本进程**的事实。
> CLI **不写**任何配置 —— 改日程是人在 PC 上改 `config/config.yaml`（配置永远是唯一真源）。

### `doctor` —— 体检

```bash
$ assistant doctor
配置                  OK    /home/kickpi/myproject/assitant/config/config.yaml
Agent socket        OK    /tmp/agent.sock 可连（Agent 在跑）
派生 llm.env          OK    llm.env 与 config.yaml 一致
日程                  OK    装载 4 条（语义来自 agent/core/scheduler.py）
config/config.yaml  OK    在
llm/config/llm.env  OK    在

体检结果：6 项全部 OK
```

六项各自独立判 OK/警告，**不因为一项失败就跳过其余**。**只要有一项警告，退出码就是 1** ——
所以它能直接当脚本里的健康检查用（`assistant doctor || echo 需要处理`）。"派生 llm.env"那一项
跑的是 C++ 侧 `gui/build/gui_config_sync` 的 dry-run（映射表只有 `gui/src/core/config_sync.cpp`
一份），CLI 只负责跑它、读它的结论。

Agent 没在跑时它照样能跑，只是 socket 那一项变成警告：

```bash
$ assistant doctor
配置                  OK    /home/kickpi/myproject/assitant/config/config.yaml
Agent socket        警告    /tmp/agent.sock 不存在（Agent 没在跑？）
派生 llm.env          OK    llm.env 与 config.yaml 一致
日程                  OK    装载 4 条（语义来自 agent/core/scheduler.py）
config/config.yaml  OK    在
llm/config/llm.env  OK    在

体检结果：5 项 OK，1 项警告（Agent socket）        # 退出码 1
```

---

## 5. 它**不**做什么（边界）

| 不做 | 为什么 |
| --- | --- |
| 不写配置、不改日程、不重启 Agent | 只读/只控制。写配置是人在 PC 上做的事，配置是真源 |
| 不 `--json` | 输出给人看；要机器读，用 `watch --count` + 原始行，或直接 `LocalClient` |
| 不 import Agent 去读内存 | 那会拿到"另一份状态"。所有跨进程信息都走 IPC 协议 |
| GUI 界面不显示"已触发" | 协议已经把 `schedule` 推出来了，GUI 侧目前按"未知 topic 忽略"处理；要不要显示是另一个任务 |
| 触发记录不落盘 | `Scheduler._history` 是有界内存（默认 50 条）。跨重启保留是另一套持久化设计 |

协议侧的字段定义**只在** [`ipc-protocol.md`](ipc-protocol.md) §3/§4 —— 本文不抄一份字段表。

---

## 6. 排障

| 现象 | 先看 |
| --- | --- |
| `连不上 Agent（连接 … No such file or directory）` | Agent 没跑（或 socket 路径不对：`--socket` / `config.yaml` 的 `ipc.socket_path`）。`assistant doctor` 会直接说清楚 |
| `问不到 Agent 的触发记录：… 没回日程快照` | 连上了但没应答 —— Agent 版本旧（不认识 `query_schedule`）或它在忙。`--no-ask` 可先绕开 |
| `chat` 超时 | Agent 在跑但没回：看 `assistant watch` 里它到底推了什么；`llm.mode=disabled` 时走规则兜底，只回问候/时间/状态 |
| `mode` 报"没切过去" | 状态机的合法转换表（`STUDY → GAME` 这类是被拒的，不是坏了） |
| 输出里中文是乱码 | PC 的 cmd 是 GBK；CLI 已经把换行之类的符号限制成 ASCII，仍有乱码就换 UTF-8 终端（`chcp 65001`） |

---

## 7. 相关

- 线上格式唯一真源：[`ipc-protocol.md`](ipc-protocol.md)（§3 topic / §4 command / §8 常量）
- 日程语义与去重、触发窗口：[`architecure.md`](architecure.md) §6.1 与 `agent/core/scheduler.py` 模块头
- GUI 那一端： [`gui.md`](gui.md)、[`gui-agent-integration.md`](gui-agent-integration.md)
- 部署与双机同步： [`deploy.md`](deploy.md)
