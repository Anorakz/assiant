# 板端控制 CLI（`assistant`）

Agent 有**两条前端**：板端那块屏上的 GUI，和这里的 CLI。CLI 给 ssh / 脚本用 ——
看一眼状态、发一条消息、切模式、控音乐**与视频**、盯推送、查日程、做体检。

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

## 4. 十二条命令

> 原先那十条走 IPC（要 Agent 在跑）；T13-8 加的两条（`set` / `study`）**不经过 Agent** ——
> 它们直接改文件（配置真源 / 派生数据），所以 Agent 没起也能用。

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

### `music` —— 音乐传输控制（播放 / 暂停 / 上一首 / 下一首）

```bash
$ assistant music play
现在是：夜曲（已暂停）
播放中：夜曲（正在播放）

$ assistant music play          # 已经在放 -> 不发命令（幂等）
现在是：夜曲（正在播放）
已经是播放了 —— 没发命令。

$ assistant music pause
现在是：夜曲（正在播放）
已暂停：夜曲（已暂停）

$ assistant music next          # 下一首
现在是：夜曲（正在播放）
下一首：晴天（正在播放）

$ assistant music prev
$ assistant music toggle        # 直接切一下（不看当前状态）
$ assistant music next --no-wait        # 发出去就返回
$ assistant music next --timeout 15     # 音乐要走一趟 PC（neteasecli/mpv），慢就加
```

三条口径（都写进单测了）：

| 事 | 怎么办 |
| --- | --- |
| **走哪条协议** | 用**现成的**三条命令：`music_play_pause` / `music_next` / `music_prev`（T8-4 已接进 Runtime）——**没有新增协议** |
| **`play` / `pause` 为什么不是 toggle** | 协议里音乐**只有一个 toggle**。所以 CLI 连上后先等一小会儿**补推的 `music{playing}`**，已经在那个状态就**只打印、不发命令**（否则"播放"会把正在放的歌暂停掉）。状态问不出来就照发，并如实说 |
| **成功/失败看什么** | **成功**看 Agent 推的 `music`（当前曲目 + 在不在放）；**失败**看 `llm`（音乐没开 / PC 上没在放 / 队列是空的）——**Agent 的原话照抄**并退出码 1。命令发出去了但 `playing` 没按预期变，也是**错误**（不假装成功） |

> ⚠ 音乐**放什么**由**对话**决定（`assistant chat 放首周杰伦`）；这里只管传输。
> 音乐没开（`music.enabled: false`）时，Agent 会回一句 `音乐没开（config.yaml 的 music.enabled）`，
> CLI 原样打印并返回 1 —— 不会静默无事发生。

### `video` —— 视频传输控制（播放 / 暂停 / 上一集 / 下一集）

```bash
$ assistant video pause
现在是：Luna say maybe（正在播放）
已让播放器暂停：Luna say maybe

$ assistant video pause          # 已经暂停 -> 不发命令（幂等）
现在是：Luna say maybe（已暂停）
已经是暂停了 —— 没发命令。

$ assistant video play
$ assistant video toggle         # 交给 Agent 按它自己的真值解析成 play/pause
$ assistant video next           # 队列里下一集（队列内容由对话/画面决定）
下一集：<标题>（正在缓冲 —— 攒够 15 秒才让播放器开；`assistant watch` 能看到「可以播了」）
$ assistant video prev
$ assistant video pause --wait-player   # 再等一步"播放器真的按了"（等 GUI 的回报）
已让播放器暂停：Luna say maybe（播放器已回报）
$ assistant video next --no-wait        # 发出去就返回
```

三条口径（都写进单测了）：

| 事 | 怎么办 |
| --- | --- |
| **走哪条协议** | `play`/`pause`/`toggle` 走 **T11-10f 新增的 `video_control{action}`**（GUI 那颗播放/暂停按钮是"本地点"，原来没有任何命令能让 Agent/CLI 去按它）；`next`/`prev` 走现成的 `next_bilibili`/`prev_bilibili`。Agent 收到后推一条 `bilibili{control{action,seq}}`，**真正按播放器的是 GUI** |
| **`play` / `pause` 为什么是幂等意图** | 连上后先看**补推**的 `bilibili`（`buffer.saw_playing/playing` = "现在真的在放吗"），已经在那个状态就**只打印、不发命令**。⚠ 只看 `playing` 不够 —— 它默认就是 true（还没起播时也是），所以要 `saw_playing` 也为真 |
| **成功/失败看什么** | **成功**看 Agent 推的带 `control` 的 `bilibili`（= 收下并推给 GUI 了）；`next`/`prev` 看队列真的走了一格。**失败**看 `llm`（没在播 / 没 GUI 连上 / 到队尾）——**原话照抄**并退出 1 |

> ⚠ **协议没有请求 id**，Agent→GUI 是单向的：所以默认只确认到"Agent 收下并推给播放器了"。
> 要确认"播放器真的按了"，加 `--wait-player` —— 它等的是 Agent 转发的 `control_result{seq,action,playing}`
> （`playing` 是 **GUI 报回来的原话**）。等不到、或播放器回报的和你要的不一样，都**如实报错并退出 1**。
> ⚠ 视频**放什么**由**对话或画面**决定（`assistant chat 放个 luna say maybe 的视频`）；
> 这里只管传输控制。没有在播的视频时 Agent 会回一句"现在没有在播的视频 —— 先在 GAME 里点一下预览图"。

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

$ assistant mode sleep            # 从 GAME/STUDY 去 SLEEP 要经 IDLE（状态机的规矩）
已切到 SLEEP（路上经过 IDLE）

$ assistant mode game             # 已经在 GAME / 认不出的值 -> 如实报错
Agent 没切过去：最后看到的是 IDLE（0.4 秒内没等到 GAME；路上经过 IDLE）—— 切不动时
Agent 会把真实状态推回来（协议 §4）。      # stderr, 退出码 1
```

> ⚠ **跨模式切换是两跳**：状态机要求"任何切换都必须经过 IDLE"，所以 `GAME -> SLEEP`
> 实际是 `GAME -> IDLE -> SLEEP`（每一跳都会释放那个模式里的东西：视频/队列/SigLIP…）。
> CLI 会一直等到**目标**那个模式的推送，并把中间经过的状态如实写出来（T12-2）。
> ⚠ T12-7：中间那几跳是**连着推**的，所以 CLI 记的是**收到的每一条** `status` ——
> 早先"只留当前那条"的写法在真机上会把中间那一跳丢掉（打印成 `已切到 SLEEP` 而少了
> "（路上经过 IDLE）"）；板端验收 `tests/board/t12_accept.py` 的 D 段盯着这个。

取值 `SLEEP/IDLE/STUDY/GAME`，大小写不限。CLI **不乐观地**认为切换成功：它等 Agent 推回来的
`status`，只有线上报的真的是目标模式才算成功。

### `watch` —— 盯推送（排障主力）

```bash
$ assistant watch --topics schedule
在听 /tmp/agent.sock 的推送（只看 schedule，Ctrl-C 退出）
13:28:09  schedule  kind=fired state=study date=2026-09-22 scheduled_at=2026-09-22T13:28 fired_at=2026-09-22T13:28:09
收到 1 条推送，用时 81.7 秒
```

| 选项 | 说明 |
| --- | --- |
| `--topics a,b` | 只看这些 topic（默认全看）。`schedule` 的推送**单独渲染**成一行 key=value，不会把嵌套的事实对象原样倒出来 |
| `--count N` | 收够 N 条就退出（0 = 不限；给脚本/测试用） |

Ctrl-C 会打印"收到 N 条推送，用时 X 秒"再干净退出。

### `schedule` —— **接下来 N 小时**（默认 24）的日程，标出**真的触发过**的那些

```bash
$ assistant schedule
日程（配置共 3 条；/home/kickpi/myproject/assitant/config/config.yaml）
窗口：18:00 → 明天 18:30（24 小时；另有最近 30 分钟里刚过去的）
明天（2026-09-23 周三）
  08:30  STUDY
  10:00  GAME
  13:00  SLEEP
✓ 触发记录来自运行中的 Agent 本人（本次 0 条）——「未触发」是**这个 Agent 进程**没触发过，
  不是配置里没有；记录只在内存里，Agent 重启即清零。
```

| 选项 | 说明 |
| --- | --- |
| `--hours N` | 窗口长度（小时，**必须是正数**，默认 24）。窗口里还带"最近 30 分钟刚过去的"那一段 |
| `--limit N` | **整个窗口**最多列几行（0 = 不限，默认 10）—— 不是每天几行：窗口是一段连续时间 |
| `--no-ask` | **不问** Agent，只按时间比较（离线/对比用） |

#### 窗口：只看"接下来"，所以已经过去的不在里面

窗口 = `[现在 - 30 分钟, 现在 + N 小时)`，判据是**行的 `start`**（日程到点就是那一行的 `start`，
没有"提前量算出来的提醒时刻"这种东西了 —— T12-4 起 `remind_before_min` 不再被读）。

所以下午 18:00 跑上面那条命令时，**今天 08:30 / 13:00 那两条是不出现的**；
每天 08:30 的条目在 24 小时窗口里只出现**明天**那一次。这不是丢数据，是"接下来"的定义。
段标题按天给（今天 / 明天 / 后天，更远直接给日期），窗口终点写在头一行。

**那 30 分钟是为了什么**：窗口只往前看，但"刚刚过去"的那一小段要留着 —— 否则
`← 已触发 HH:MM:SS` / `← 已过（未触发）` 这两层信息在列表里**完全看不见**。
尾巴里还会出现**配置里已经没有**的一次性日程（见下面 §"一次性日程会被删掉"），
它是靠 Agent 的**触发事实**画出来的（事实里有 state / date / scheduled_at / fired_at）。

**行格式与 GUI 日程区逐行一致**（`HH:MM  状态`，状态大写），所以"CLI 列出来的"和"界面上显示的"
可以直接 diff。日程本身用**真的** `agent.core.scheduler` 语义展开（`occurs_on()` / `trigger_at()`），
所以 recurring 看星期、oneoff 看日期都对。

#### 三种标记，别把它们读成同一件事

| 标记 | 意思 | 谁说的 |
| --- | --- | --- |
| `← 已触发 13:00:03` | **这个 Agent 进程真的触发过这一条**，时刻是 `fired_at` | 运行中的 Agent（`query_schedule` 的应答） |
| `← 已过（未触发）` | 现在过了 `start`，而 Agent 的记录里**没有**它 | 时间比较 + Agent 的记录（两件事都成立才敢这么标） |
| `← 已过` | 现在过了 `start`（纯时间比较） | CLI 自己算的 |
| （什么都不标） | 还没到点 | —— |

> ⚠ 窗口里只会有"尾巴"那 30 分钟是过去的 —— 所以这两种标记**基本只在尾巴上出现**。

> ⚠ **CLI 只能"看"日程，不能加/删**（T12-6 的边界）：写 `config.yaml` 的那条路是
> **对话工具**（`set_schedule`）—— 跟助手说"每天 23 点睡觉"就行。CLI 这边保留只读立场，
> 唯一的写操作是 `cleanup --apply`（那也是在**删**已经过去的一次性日程）。
> 理由：日程是"生活安排"，用嘴说比敲参数自然；而 CLI 的参数一旦成为第二条写入路径，
> 两条路的校验就得各写一遍。
> 想看清一整天，用 `--hours` 往前看（窗口不会往后看）。

**为什么这么较真**：`现在 > start` 与"Agent 触发过"是**两个不同的事实**。前者在 Agent 根本没跑时
照样成立，后者只存在于运行中进程的内存里。所以：

- 问到了 Agent → 敢说「已触发」/「未触发」，页脚写明记录来自运行中的 Agent；
- 问不到（Agent 没跑 / `--no-ask`）→ 只说「已过」，页脚写明"这是按时间比较的"，**一个字都不提
  触发与否**：

```bash
$ assistant schedule --no-ask --hours 30
日程（配置共 4 条；/home/kickpi/myproject/assitant/config/config.yaml）
窗口：18:21 → 后天 00:51（30 小时；另有最近 30 分钟里刚过去的）
明天（2026-09-23 周三）
  08:30  晨间计划
  10:00  周会
  13:00-13:30  午休
⚠ 「已过」只是「现在过了 start 时刻」（按时间比较），**不代表 Agent 已经触发过**。
（你用了 --no-ask —— 所以这里只能按时间比较。）
```

Agent 没在跑时（原因会写在 stderr 与页脚里，两种原因分得清）：

```text
问不到 Agent 的触发记录：连不上 Agent（连接 /tmp/agent.sock 失败: [Errno 2] No such file or directory）
问不到 Agent 的触发记录：Agent 在 3.0 秒内没回日程快照
```

> ⚠ **触发记录只在内存里**：Agent 重启即清零，且不是"落盘的日志"。所以 `已触发` 是**本进程**的事实。

#### 一次性日程触发后会被删掉（Agent 侧开关控制）

Agent 的 `scheduler.remove_fired_oneoff` 打开时（**默认关**），一条 **oneoff** 触发之后会被
**从 `config/config.yaml` 里删掉** —— 所以它不是"标成已触发"，而是**下次列出来就没有它了**
（`assistant schedule` 与 GUI 日程区都会少一条）。

- **CLI 自己不写配置**：删除是运行中的 Agent 干的（它才是"知道真的触发了"的那一方）。
- 上面说的"尾巴"能把它画出来，靠的是**触发事实**，不是配置。
- 只删 `oneoff`；`recurring` 一条都不动（删了明天就不响了）。
- 写之前会重新读盘、核对 `title`+`date`+`start`，对不上就不删；原文件旁留一份 `config.yaml.bak`。
- 细节与边界见 [`config-sources.md`](config-sources.md) §3。

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
跑的是 Python 那份唯一实现（`agent/core/llm_env.py`，T14-2 从 C++ 搬过来 —— 映射表只有一份）
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

### `cleanup` —— 清理**已经过去**的一次性日程

**默认只列出**（dry-run），`--apply` 才真删：

```bash
$ assistant cleanup
要清理的一次性日程（已经过去、不会再触发）：
  项目评审  2026-09-20 14:00
共 1 条；加 --apply 才会真删（会留一份 config.yaml.bak）
（今天已经过了 start 的一次性日程有 1 条，**没动** —— late_grace_min 可能还认它；Agent 侧开关打开时，真触发了会自动删。）

$ assistant cleanup --apply
已删除 项目评审  2026-09-20 14:00
共删除 1 条（原文件留了一份 config.yaml.bak）。
```

| 选项 | 说明 |
| --- | --- |
| `--apply` | 真的删。文本级删除 + 原子写 + `.bak` —— 与 Agent 触发后自动删 **同一套实现**（`agent/core/schedule_config.py`） |

规矩：

- **只清 `date < 今天`** 的 oneoff（它们不会再触发）。**今天的不动** —— `late_grace_min`
  可能还认它，留给 Agent 的正常路径；这条会在输出里提示一句。
- `recurring` 一条都不动。
- **不写 `config.example.yaml`**：那是模板不是真源，给 `--config` 指模板会直接报错拒绝。
- 这是 CLI"默认只读"的**唯一显式例外**：它不会自动发生，是你敲了 `--apply`。
  "触发了就自动删"那条在 **Agent 侧**（`scheduler.remove_fired_oneoff`）。

---

### `tag` —— 给壁纸打标签（SigLIP 零样本，写 `config/wall_data.jsonl`）

**默认只算计划**（dry-run），`--apply` 才真打：

```bash
$ assistant tag
词表 32 条标签 / 3 个轴（scene 16、tone 8、mood 8），指纹 b780f2b5；词表向量: 复用数据文件里的缓存
壁纸目录里 40 张图；数据文件 /home/kickpi/myproject/assitant/config/wall_data.jsonl
已是最新 40 张（没有要打的）
（dry-run：加 --apply 才会真打。会写 …/wall_data.jsonl，并在旁边留一份 .bak）

$ assistant tag --apply
已是最新 38 张；要打 2 张（图变了 1，新图 1）
  [1/2] 01_landscape_1280x800.png  1280x800  5801 ms  scene=space
  [2/2] wallhaven-zy31jv_3840x2160.png  3840x2160  3771 ms  scene=anime
打过 2 张，失败 0 张；总耗时 9.7s，每张中位 5801 ms（模型加载 0.0s、词表 复用缓存）
已写入 …/wall_data.jsonl（旁边留了一份 wall_data.jsonl.bak）。
```

| 选项 | 说明 |
| --- | --- |
| `--apply` | 真打。**逐张写盘**（跑一半崩了不丢已打好的）+ 原子写 + `.bak` |
| `--force` | 全部重打（不看指纹）。**词表缓存照旧复用** —— force 说的是"图片标签重打"，模型和词表没变就没必要重编 61 s |
| `--limit N` | 本轮最多打 N 张（分批用） |
| `--dir` / `--data-file` / `--top-k` | 覆盖配置里的值 |
| `--prune` | 顺手清掉"图已经不在了"的行（与 `--apply` 一起才真写） |

规矩：

- **增量判据**：图变了（`sha256`）/ 模型变了（`model_sha8`）/ 词表变了（`vocab_sha8`）/ 格式版本变了。
  第二次跑应当说"都已是最新"。
- **词表向量存在数据文件第一行**、按 `model_sha8 + vocab_sha8 + 标签列表` 认指纹：
  对得上就复用（省 **61 s** 的文本塔编码），对不上就重编再覆盖。
  没要打的图、缓存也是最新的 → **什么都不写**（不白改文件）。见
  [`tagging.md`](tagging.md) §4。
- **只能在板端跑**（要 NPU + numpy + cv2 + tokenizers）。开发机上 `--apply` 会明确说缺什么；
  **dry-run 不需要这些**，所以"看计划"在哪儿都能跑。
- 这是 CLI 的**第二个会写文件的命令**，但写的**不是配置真源**：`cleanup --apply` 写
  `config.yaml`，`tag --apply` 写的是派生数据（机器生成的标签 + 向量，已进 `.gitignore`）。
- 词表、数据文件格式、IP 检索、已知不准的地方：见 [`tagging.md`](tagging.md)。

---

### `set` —— 改设置项（文本级；默认只看）

```bash
$ assistant set study --show                      # 这一组能改什么、现在是什么
$ assistant set study --relative-band 0.07        # 只看：会说改哪一行、旧值 -> 新值
$ assistant set study --relative-band 0.07 --apply # 真写（旁边留 config.yaml.bak）
$ assistant set game-watch --interval-s 60 --apply
$ assistant set profile --trigger-chars 2000 --trigger-turns 12 --apply
$ assistant set study --disabled --apply           # 开关类：--enabled/--disabled / --remind/--no-remind …
$ assistant set study --set study.relative_band=0.07 --apply   # 也可以直接给点号路径
$ assistant set cookie --sessdata '<值>' --apply --verify      # B 站凭据（写 JSON, 只回显掩码）
```

三组 + cookie：`study` / `game-watch` / `profile` / `cookie`。

规矩（承诺与 GUI 的 ConfigStore **同一套**，实现见 `agent/core/settings_config.py`）：

- **只动目标那一行**：注释、顺序、空行、**CRLF** 逐字节保留；写完在旁边留一份 `.bak`
  （值本来就是这样 -> **一个字节都不写**，也不留 `.bak`）。
- **能改的键 = `config.example.yaml` 里有的标量键**（模板是键清单的唯一真源）：
  不在模板里的键、结构级的键（映射/序列，例如 `study.classes`）一律拒绝并说清为什么。
- **值的类型跟着模板走**：模板里是 `false` 就只能给布尔、是 `0.05` 就只能给数字 ——
  类型写错在 YAML 里不报错（`"0.05"` 是字符串），只会让 Agent 读出来变成另一个东西。
- **缺键** -> 按模板把带注释的那一行插进段尾；**缺段** -> 把模板那一整段（含说明横幅）
  追加到文件末尾（板端真配置**没有** `study:` 段，第一次 `set study …` 就是这么长出来的）。
- `cookie` 写的是 `config/bilibili_cookie.json`（**凭据**，不是真源）：只认 `SESSDATA` /
  `bili_jct` / `DedeUserID` 三个键（`SEESSDATA` 那种笔误会被拒 —— 实测 B 站把它当没登录），
  终端**只回显掩码**；`--verify` 顺手问一次 B 站看这份 cookie 好不好使。

> **GUI 也改同一批键**（板端设置页三张卡片，T13-9）：学习监督的时间参数与起始阈值、
> 游戏检测的开关/间隔/"有把握"分数 + B 站凭据、画像压缩的开关与两个触发阈值。
> 两边**同一套约定**（只动一行 + `.bak` + 缺段按模板新建 + 类型跟着模板走），
> 但 GUI **只写白名单**里那些键；下面这些仍然只有 CLI/真源能改：
> `study.remind`、`study.back_to_desktop`、`study.skip_on_keyword`、`study.learn`、
> `study.adapt`、`study.target_unknown_rate`、`study.min_labeled`、`study.anchor_file` /
> `study.stats_file` / `study.max_anchors_per_class` / `study.keep_shots`。
> 键清单与白名单守卫见 [`study.md`](study.md) §8.1、[`config-sources.md`](config-sources.md) §3.3。

### `study` —— 学习内容监督的日常操作（不用起 Agent）

```bash
$ assistant study status                  # 锚点/阈值/时间参数/自适应开关/最近一次判定
$ assistant study status --json
$ assistant study check  --image shot.png # 判一张截图（按真管线还原成板子看到的那一帧）
$ assistant study label  --image shot.png --class code --apply   # 学成锚点（默认只看）
$ assistant study freeze                  # = set study.adapt false（判定照旧, 只是不再自己挪带）
$ assistant study unfreeze
$ assistant study reset --class anime --apply          # 清掉这一类的锚点
$ assistant study reset --thresholds --apply           # 连阈值/EWMA 一起回到配置初值
```

- `status` / `reset` 只读那两份**派生数据**（`config/study_anchors.jsonl` /
  `config/study_stats.json`），**不需要 NPU、也不需要 Agent**。
- `check` / `label` 要**板端的 SigLIP**（过 NPU）；开发机上会明确说缺什么。
  "看真串流帧"不是 CLI 的事 —— 那是 Agent 在 STUDY 里的循环（见 [`study.md`](study.md)）。
- `freeze` / `unfreeze` 改的是配置真源里那一行（走 `set` 那套文本级写入器）。

---

## 5. 它**不**做什么（边界）

| 不做 | 为什么 |
| --- | --- |
| 不写配置、不改日程、不重启 Agent | **默认只读**。需要你亲手敲 `--apply` 的例外现在有四个：`cleanup --apply`（删已触发的一次性日程）、`tag --apply`（写派生数据 `wall_data.jsonl`）、`set … --apply`（写**配置真源**里那一行 + `.bak`）、`study label/reset --apply`（写派生数据）。写日程本身仍然是人在 PC 上做的事，配置是真源 |
| 不是唯一能改设置的地方 | 板端**设置页**的三张卡片改的是同一批键（T13-9，见 `set` 那节末尾的清单）。GUI 只写白名单里的键；动作开关/自学习开关/数据文件路径仍然只有 CLI 这条路 |
| 不手动换壁纸 | 换壁纸**只走对话**（对 Agent 说"换一张安静的深色风景"）。CLI 没有换壁纸命令，GUI 也没有「下一张」按钮 —— 见 [`tagging.md`](tagging.md) |
| 不 `--json` | 输出给人看；要机器读，用 `watch --count` + 原始行，或直接 `LocalClient` |
| 不 import Agent 去读内存 | 那会拿到"另一份状态"。所有跨进程信息都走 IPC 协议 |
| GUI 界面不显示"已触发 / 未触发" | GUI 不认 `schedule` topic（合同见 `gui-agent-integration.md`）；它只把"刚过去的"那条**变暗**显示（与 CLI 同一条 30 分钟尾巴），不写字 |
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
- 日程语义与去重、触发窗口：[`architecture.md`](architecture.md) §6.1 与 `agent/core/scheduler.py` 模块头
- GUI 那一端： [`gui.md`](gui.md)、[`gui-agent-integration.md`](gui-agent-integration.md)
- 部署与双机同步： [`deploy.md`](deploy.md)
- 学习监督（`assistant study` 背后的功能、判定口径与边界）： [`study.md`](study.md)
- 设置写入的承诺（只动一行 / `.bak` / 缺段新建 / 哪些键能改）： [`config-sources.md`](config-sources.md) §3.2（CLI）与 §3.3（GUI 设置页）
