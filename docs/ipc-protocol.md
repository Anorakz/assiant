# Agent ⇄ GUI IPC 协议

Agent（Python）与 GUI（C++ / Qt5）都跑在 RK3568 板端，通过 Unix domain socket 通信。

## 0. 本文负责什么（真源边界）

**唯一真源：线上格式。** 具体包括 —— 信封字段、topic / action 取值、错误处理约定、
常量表。本文与代码的分工是：格式以本文为准，**代码侧的唯一实现**是
`agent/ipc/protocol.py`（C++ 侧照 §8 的字面值实现，不另立一份）。

本文**不**负责下面这些 —— 它们各有自己的真源，**不要在这里复制一份**：

| 不负责的内容 | 去哪看 |
| --- | --- |
| GUI 界面行为：哪个控件发什么、收到 topic 后界面怎么变、哪些位置还是占位 | `docs/gui-agent-integration.md` |
| 模式的实际语义（SLEEP/STUDY/GAME 各自做什么）、LLM 调用与降级 | `docs/architecture.md`、`agent/core/`、`agent/llm/` |
| 日程怎么写、什么时候算"到点"、去重、触发事实从哪来 | `agent/core/scheduler.py` 模块头 + `config/config.example.yaml` |
| Agent 侧 server 的接入点与装配 | `agent/ipc/__init__.py` 的 `build_ipc()`、`agent/main.py` |
| 配置项从哪来、谁能改 | `config/config.example.yaml` 的注释 + `Readme.md` 的配置相关小节 |

判断标准很简单：**"线上一个字节长什么样"归本文；"谁为什么发它 / 收到后界面怎么动"不归本文。**

---

## 1. 传输层

| 项 | 值 |
| --- | --- |
| socket 类型 | Unix domain socket（`AF_UNIX` / `SOCK_STREAM`） |
| 路径 | `/tmp/agent.sock` |
| 消息分隔 | 换行 `\n`（LF，0x0A）；每条消息恰好一行 |
| 编码 | UTF-8（无 BOM） |
| 单行上限 | 1 MiB（`MAX_LINE_BYTES`） |
| 谁监听 | Agent 监听并 `accept`；GUI 作为客户端连接 |

### 为什么用换行分隔（NDJSON）

**消息里不可能出现裸换行**：JSON 编码器会把字符串中的换行转义成 `\n`（两个字符
`\` `n`）。Python 的 `json.dumps` 与 Qt 的 `QJsonDocument::toJson` 都如此，所以：

- 按 `\n` 切分永远是安全的，不需要长度前缀，也不需要额外转义层；
- 一条消息可以边读边解析，接收方不必先知道总长度。

接收方按行读即可，**不要**按"读到多少字节"切分。结尾的 `\n` 是分隔符、不属于消息内容。
实现上容忍行尾出现 `\r\n`（`decode()` 会剥掉），但**发送方必须发 `\n`**。

### 连接生命周期

- GUI 断线重连即可，Agent 不做会话保持：**连接本身不携带状态**，
  状态全在 `status` topic 里。
- 一条坏消息只影响它自己（见 §6），不断开连接。
- Agent 退出前会关闭监听 socket 并删除 `/tmp/agent.sock`；GUI 收到 EOF 后重试连接。

---

## 2. 消息信封

**两个方向的信封不一样** —— 这不是笔误，是照 GUI 的实际实现定的
（Phase 6 决策 1：命令格式以 GUI 为准）。同一层里两种信封共存，混用会被丢弃（见 §6）。

### Agent → GUI（推送）

```json
{"topic": "status", "data": {"mode": "STUDY", "connected": true}, "timestamp": 1234567890.123}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `topic` | string | ✅ | 消息名。取值见 §3 |
| `data` | **object** | ✅ | 负载。**必须是 JSON object**，不能是 array / string / number；没有参数时给 `{}` |
| `timestamp` | number | ✅ | Unix epoch **秒**（浮点），例如 `1234567890.123` |

### GUI → Agent（命令）

```json
{"action": "chat_input", "payload": {"text": "现在几点了"}}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `action` | string | ✅ | 命令名。取值见 §4 |
| `payload` | **object** | ✅ | 参数。**必须是 JSON object**；没有参数时给 `{}` |

**命令方向没有 `timestamp`**：GUI 不发，Agent 也不要求（`decode_command()` 不检查它）
—— 少一个字段就少一处两边可能不一致的地方。字段顺序是 `action` 在前（Qt 按插入顺序
序列化，GUI 先插 `action`），但两侧都应**按 key 取值**，不要依赖顺序。

> ⚠ `topic`/`data` 与 `action`/`payload` **不能混用**：推送用前者，命令用后者。
> Agent 侧的对应关系是 `encode/decode/decode_full`（推送）与
> `encode_command/decode_command`（命令）。收错形态的行会被丢弃并记 warning ——
> **不**做"两种都认"的兼容（那等于"文档说 A、代码也收 B"）。

规则：

1. **字段顺序无关**。JSON object 无序，两侧都必须按 key 取值。
2. **未知字段必须忽略**，不要报错。这是本协议没有版本号时唯一的向前兼容手段
   —— 将来加字段时，老实现忽略它即可继续工作。
3. **`data` / `payload` 必须是 object**。这条是硬约束：放行标量会让两侧的取值代码
   到处写类型分支，而 array 与 object 的边界最容易两边理解不一致。
4. **没有版本号字段**（v1 不需要）。需要演进时靠"加字段 + 忽略未知字段"，
   而不是靠版本协商。

### 时间戳的精度（⚠ 容易踩）

`timestamp` 是 **Unix epoch 秒的浮点数**。C++ 侧**必须用 `double`**：

| 类型 | 有效位 | epoch 尺度（~1.8e9）下的分辨率 |
| --- | --- | --- |
| `float`（32 位） | ~7 位十进制 | **约 128 秒** ← 完全不可用 |
| `double`（64 位） | ~15–16 位 | 约 0.2 微秒 |

Qt 里 `QJsonValue::toDouble()` 返回 `double`，正常使用即可；但**不要**把它存进
`float` 变量或 `qreal`（在部分平台上 `qreal` 是 `float`）。

Python 侧 `time.time()` 本来就是 `double`，无此问题。

数值精度：JSON number 在两侧都按 IEEE-754 double 处理，因此**整数超过 2^53
会丢精度**。协议里的 `index` 这类小整数没问题；将来若有长 ID，用字符串传。

---

## 3. Topic 表（Agent → GUI）

GUI 收到后按 `topic` 分发。**不认识的 topic 忽略**。

| topic | data 字段 | 类型 | 说明 |
| --- | --- | --- | --- |
| `status` | `mode` | string | 当前状态，取值见下：`SLEEP` / `IDLE` / `STUDY` / `GAME` |
| | `connected` | bool | 是否已连上串流主机（moonlight 已连接） |
| `llm` | `text` | string | Agent 给用户看的一段回复文本 |
| `wallpaper` | `path` | string | 壁纸文件绝对路径 |
| | `index` | number | 该壁纸在列表里的序号（从 0 开始） |

> ⚠ `wallpaper` 是**变化时推**（T3 起 Agent 真的会推了）：只在"换了一张"的那一刻发。
> **T6 起客户端连上时会补推一条当前壁纸**（`server.on_client_connect` -> `wallpaper{path,index}`）——
> 所以刚打开的 GUI 主区不再是兜底底色，直接就是当前那张。补推**不受状态权限表约束**
> （它是"同步显示"，不是"换一张"）：Agent 正处在 SLEEP/GAME 时也照补。
> 从来没换过任何一张时，补推会**顺手选第一张**当初始画面。
| `music` | `title` | string | 当前曲目标题（空 = 没有曲目信息，界面显示"未播放"） |
| | `playing` | bool | 是否正在播放 |
| | `track_id` | string｜null | 当前这首在**本地库**里的 id（对账用；没有曲目时 `null`） |
| | `artist` / `album` | string | 歌手 / 专辑（库里那一行的值，可能是空串） |
| | `position_s` / `duration_s` | number | **进度 / 总时长**（秒，1 位小数）。⚠ `position_s` 是"锚在 mpv 真值上的估算"：每轮轮询重新对齐，中间靠本地时钟走（暂停就停住、不超过时长） |
| | `plays` | number | 这首在本地库里的播放次数 |
| | `tags` | object | 本地库给这首打的标签（`{轴: [标签…]}`） |
| | `lyric_ok` | bool | **这首歌有没有歌词**（T15-16）。`false` 时 `lyric_lines` 必为空 |
| | `lyric_lines` | array | 歌词时间轴：`[{t, text, tr}…]`，按 `t` 升序。`t` 是秒（**已算进 `music.lyric_offset_ms` 的补偿**，客户端不用再移）、`text` 原文、`tr` 译文（**可能为空串** = 这一行没有译文，显示时回退原文）。⚠ **一条只推一次**，不跟进度重复推 |
| | `lyric_rev` | number | 歌词**版本号**：内容真的变了才 +1。客户端拿它判断"要不要重设时间轴"（Agent 侧也拿它当推送去重的键之一）—— 换个写法收到新 `rev` 就该整份换掉 |
| | `lyric_reason` | string | 没有歌词时给人看的一句话（`没有歌词` / `歌词取不到（…）`）。**空串 = 还没有结果**：新曲目刚起播、歌词还没拉回来的那几秒 |

> ⚠ `music` 是**变化时推**：曲目、播放状态、歌词（`lyric_rev`）任一变就推；**播放中每轮都推**
> （带上新的 `position_s`，默认 3 s 一轮，见配置键 `music.poll_interval_s`）。GUI 刚连上时
> 补推一次当前状态（`push_current_music`）。
> ⚠ **歌词只在换歌时去 PC 拉一次**（一次 ssh，和 `player status` 同量级）—— 所以它不是"跟着进度
> 流式推"的，客户端要**自己按 `position_s` 在时间轴里选行**（见 `docs/gui-agent-integration.md`）。
> 取不到会退避 5 分钟再试，期间 `lyric_reason` 一直挂着同一句话（同一条原因 Agent 也只记一行日志）。
> 对轴补偿是配置键 `music.lyric_offset_ms`（**正值 = 歌词提前**，与 LRC 自己的 `[offset:]` 同号）。
> ⚠ 老 Agent 不带 `lyric_*` 四个字段 —— 按 §2 规则 2 忽略即可（客户端当"没有歌词"处理）。
| `bilibili` | `queue` | array | **B 站预览队列**（T11-6）：每条 `{bvid,title,author,duration_s,play,cover,url}`。窗口 = **3×预览栏格数**（见 `bilibili_viewport`）；**只存地址, 不下载视频** |
| | `index` | number | 当前第几条（在 `queue` 里的下标） |
| | `current` | object｜null | 当前那条（字段同上）；队列空时 `null` |
| | `keyword` / `source` | string | 队列是谁驱动的：`keyword` 是那个词, `source` 是 `dialogue`（对话指定）或 `screen`（画面认出的游戏） |
| | `viewport` / `target` | number | 预览栏格数 / 目标条数（= 3×格数, 上限 `queue.max`） |
| | `stream` | string | **要播的流地址** —— 板端**本机 HTTP**：`http://127.0.0.1:<port>/stream/<bvid>?v=<8位token>`（只绑 127.0.0.1, `chunked` 边下边喂）。⚠ 播放器**读不了 FIFO**（T11-9 实测 `playbin` 对 FIFO 读到 0 字节），所以这里**是 URL 不是文件路径**；GUI 里 `GST_PLUGIN_FEATURE_RANK=souphttpsrc:0` 让 GStreamer 走 `curlhttpsrc` |
| | `ready` | bool | 缓冲够不够（够了才有 `stream`）；`buffer` 里还有 `buffered_s/written_s/cap_s/state/restarts` 等实情 |
| | `quality` | string | 这一条的实际清晰度（`"360P"`/`"720P"`/…）。⚠ 清晰度的**提醒只走聊天气泡**（`llm`），GUI 不显示它 |
| | `control` | object | **Agent 让播放器播放/暂停**（T11-10f）：`{action: "play"｜"pause", seq: n}`。⚠ 只在"真的下了命令"的那一条推送里出现（**补推里没有** → 刚连上的客户端不会重放旧命令）；`seq` 单调递增，GUI 只认**比上一条大**的（同一条重复推不再动手）。CLI 的 `assistant video play\|pause\|toggle` 就是发 `video_control` 让 Agent 推这个 |
| | `control_result` | object | **播放器自己回报的落地结果**（T11-10f）：`{seq, action, playing}`，`playing` 是 **GUI 报回来的原话**。Agent 推了 `control` 之后，GUI 的下一句 `video_state` 一到就补这一条（所以只出现一次）—— `assistant video --wait-player` 等的就是它 |
| | `why` / `note` | string | 队列/缓冲如实说的话（认不出、没搜到、只凑到 N 条…），可直接显示 |

> ⚠ `bilibili` 也是**变化时推**：搜到一批、走了一格、缓冲就绪、清空时各推一次；
> **新客户端连上时补推当前队列**（`push_current_bilibili`）。
> ⚠ **队列内容只有两个来源**：**对话关键词**（工具 `bilibili_search`）与**画面认出的游戏**
> （Agent 在 GAME 模式里的循环）。**播放只由 GUI 操作触发** —— 不点预览图/不按下一集就不播、
> 也不提前缓冲（你定的"等 GUI 操作才开始播放"）。
| `schedule` | `kind` | string | `"state"`（应答 `query_schedule` 的快照）或 `"fired"`（刚刚真的触发了一条） |
| | `now` | string | **仅 `kind:"state"`**：生成快照的本地时刻 `YYYY-MM-DDTHH:MM:SS` |
| | `limit` | number | **仅 `kind:"state"`**：这份快照最多带多少条事实（当前实现 50） |
| | `fired` | array | **仅 `kind:"state"`**：**事实对象**数组，按触发时间正序 |
| | `event` | object | **仅 `kind:"fired"`**：刚触发的那个**事实对象** |

#### `schedule` 里"事实对象"的字段

`fired` 的元素与 `event` 是同一个结构：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `state` | string | **这条日程要切到的状态**（小写 `sleep`/`idle`/`study`/`game`）—— T12-4 起日程的内容就是"时间 + 状态"，没有 `title` 了 |
| `date` | string | **事件日** `YYYY-MM-DD`（配置里那条日程属于哪天） |
| `scheduled_at` | string | 该触发的时刻 `YYYY-MM-DDTHH:MM`（= `date` + 配置里那个 `start`） |
| `fired_at` | string | Agent **真的触发**它的时刻 `YYYY-MM-DDTHH:MM:SS`。通常比 `scheduled_at` 晚几秒 —— 检查是每分钟一次，窗口内第一次检查才触发 |
| `actions` | array | Agent 实际做了的动作摘要，每条是 object 且至少含 `type`。当前只有 `type:"state"`：`{type, state, ok, steps, why, current}` —— `ok=false` 时 `why` 是状态机/machine 给的原话，`steps` 是**逐跳**走过的 `[{from,to}…]`（例如 `GAME→SLEEP` 会是 `game→idle`、`idle→sleep` 两跳，见 `docs/architecture.md` 的"状态转换"）。**动作种类会增长，客户端按 `type` 取自己认识的，其余忽略** |

> ⚠ **`schedule` 不是"日程表"**：日程本身在配置里，想显示"接下来有什么安排"应当读配置
> （GUI 的日程区就是这么做的，见 `docs/gui-agent-integration.md`）。这条 topic 传的是
> **运行中 Agent 身上真发生过的事** —— 本进程内触发过什么、什么时候触发的，**进程重启即清零**。
> 它**不等于**"现在过了 `start` 时刻"那种纯时间比较：Agent 没在跑时那种比较照样成立，
> 但一条事实都不会有。
>
> **不认识 `schedule` 的客户端按未知 topic 忽略即可**（§3 开头就要求如此）——
> 因此加这条 topic **不需要**老 GUI 改任何代码。
| `config_result` | `id` | string | **一次 `set_config` 的回执**（T14-2）：把请求里那个 `id` 原样带回来 |
| | `ok` | bool | 真源写成功没有（**校验没过 = false, 且一个字节都没写**） |
| | `changed` | number | 真的改了几行（值本来就一样 -> 0） |
| | `backup` / `path` | string | 改动前的 `.bak` 路径 / 真源路径（没写时 `backup` 是空串） |
| | `error` | string | 失败原话（成功时空串）—— 直接可以显示给用户 |
| | `llm_env` | object | **派生文件那一步**的结果：`{ok, changed, path, error}`。⚠ 它**不影响 `ok`**：真源写成功就是成功，派生失败只在这里如实报（下一次保存会再同步一次） |

> ⚠ 为什么要有 `config_result`：T14 起 **GUI 不再自己写 `config.yaml`**（唯一写入者是
> Agent，见 [`adr/0005`](adr/0005-config-single-writer.md)）。"我让你改的那几行到底写进去没有"
> 必须有个明确回执，否则界面只能猜或者一直等。
> ⚠ **关联字段放在 payload 里**（`id`），不是新增信封字段 —— 协议从来没有版本号/关联字段
> （见 §4 里 `query_schedule` 的说明），这里照旧：Agent 把同一个 `id` 塞回这条推送。
> ⚠ 老 GUI 不认识这条 topic —— 按 §3 开头的要求**忽略**即可，不会因此出错。
| `wifi` | `kind` | string | **本机 WiFi**（T14-9）：`"status"` 状态快照 / `"scan"` 扫描结果 / `"ack"` 动作回执 |
| | `device` / `state` / `ssid` / `signal` / `security` / `ip` / `gateway` / `dns` / `profile` / `autoconnect` / `connectivity` / `connected` | — | `kind=status` 时：一次 `Wifi.status()` 的快照（`connected` = `state` 以 connected 开头**且**有 IP） |
| | `guard` | object | `kind=status` 时附带：链路守护的真实数字 `{probes, consecutive_failures, repairs, threshold, interval_s, last{ok,gateway_reachable,…}}` |
| | `points` / `count` | array / number | `kind=scan` 时：`[{ssid, signal, security, secured, in_use}…]`，**同名多 AP 只留信号最强的那个** |
| | `id` / `ok` / `action` / `message` | — | `kind=ack` 时：把请求里的 `id` 原样带回 + 成败 + 人话（失败时是 nmcli 的原话） |
| | `was_active` | bool | `action=forget` 的回执里：**删掉的是不是当前正在用的那个**（⚠ 板端只有这一条链路，删它 = 失联） |

> ⚠ 为什么扫描/动作走**推送**而不是"请求-应答"：扫描要几秒（`nmcli --rescan yes`），
> 而且它不幂等 —— 与 `video_state` / `config_result` 同一条路子（§4 里 `query_schedule`
> 那段说明同样适用）。**不认识的客户端忽略即可**。
> ⚠ 板端实测：eth0/eth1 都是 `unavailable`，**wlan0 是唯一链路**。

### `status.mode` 的取值

**全大写**：`SLEEP`、`IDLE`、`STUDY`、`GAME`（常量 `MODE_*`，全集 `MODES`）。

> ⚠ 与内部状态机的大小写不同：`agent/core/state_machine.py` 的 `State` 值是**小写**
> （`"sleep"`/`"idle"`/`"study"`/`"game"`），配置里也写小写。**IPC 上用大写是协议
> 约定**（GUI 直接拿去比较/显示）。转换只在 ipc server 那一层做，不要把两套大小写
> 混着传。

**示例**

```
{"topic":"status","data":{"mode":"STUDY","connected":true},"timestamp":1234567890.123}
{"topic":"llm","data":{"text":"已经切换到学习模式。"},"timestamp":1234567890.456}
{"topic":"wallpaper","data":{"path":"/home/kickpi/wallpapers/04.jpg","index":3},"timestamp":1234567891.0}
{"topic":"music","data":{"track_id":"186016","title":"晴天","artist":"周杰伦","album":"叶惠美","position_s":70.3,"duration_s":269.0,"playing":true,"plays":12,"tags":{"mood":["calm"]},"lyric_ok":true,"lyric_lines":[{"t":0.0,"text":"作词 : 周杰伦","tr":""},{"t":28.95,"text":"故事的小黄花","tr":""}],"lyric_rev":1,"lyric_reason":""},"timestamp":1234567891.5}
{"topic":"schedule","data":{"kind":"fired","event":{"state":"study","date":"2026-09-22","scheduled_at":"2026-09-22T13:00","fired_at":"2026-09-22T13:00:03","actions":[{"type":"state","state":"study","ok":true,"steps":[{"from":"idle","to":"study"}],"why":"日程: 13:00 → study","current":"study"}]}},"timestamp":1234567891.8}
{"topic":"schedule","data":{"kind":"state","now":"2026-09-22T13:05:00","limit":50,"fired":[{"state":"study","date":"2026-09-22","scheduled_at":"2026-09-22T13:00","fired_at":"2026-09-22T13:00:03","actions":[{"type":"state","state":"study","ok":true,"steps":[{"from":"idle","to":"study"}],"why":"日程: 13:00 → study","current":"study"}]}]},"timestamp":1234567891.9}
```

> ⚠ 到点触发时，Agent 还会往 `llm` topic 推**一行展示文本**（`日程到点：切到 STUDY（13:00）`；
> 切不过去就是 `日程到点：想切到 SLEEP，但没切过去 —— <原因>`）。那一行**只给界面显示**
> —— 它压根不经过对话总线，所以**不进模型输入**（日程是"到点照做"，不是给 LLM 的一句话）。

---

## 4. Command 表（GUI → Agent）

命令用 §2 的**命令信封**：`{"action": ..., "payload": {...}}`，**没有 `timestamp`**。
Agent 收到后按 `action` 分发。**不认识的 action 忽略**（记 warning，不是错误 —— 两侧
版本可能不一致）。

| action | payload 字段 | 类型 | 说明 |
| --- | --- | --- | --- |
| `switch_mode` | `value` | string | 目标状态，取值同 `MODES`（`SLEEP`/`IDLE`/`STUDY`/`GAME`）。⚠ **键是 `value`，不是 `mode`**：`mode` 是 §3 里 `status` **推送**的字段，方向不同，别混 |
| `chat_input` | `text` | string | 用户在 GUI 里敲的一行输入，等价于终端输入 |
| `next_bilibili` | — | — | **下一集**（T11-6 起真的接上了）：队列里往后一格并**开始放那一集**。`payload` 必须是 `{}` |
| `prev_bilibili` | — | — | **上一集**：队列里往前一格并开始放（走到头就回一条 `llm` 说明"前面没有了"） |
| `bilibili_pick` | `index` | number | **挑了预览栏的第几条**（用户点了缩略图）→ 开始放那一条。⚠ **只有它（和上面两条）会让视频开始播**（你定的"等 GUI 操作才开始播放, 不提前缓存"） |
| `bilibili_viewport` | `visible` | number | GUI 的预览栏**能放下几个缩略图** → 队列目标 = 3×它（夹在 1..20）。**不播** |
| `video_state` | `position_s` | number | 当前播放位置（秒）—— GUI 回报的**真实进度** |
| | `duration_s` | number | 时长（秒；流式播放时播放器可能给 0, 以 Agent 从 B 站拿到的为准） |
| | `playing` | bool | 在放 / 暂停 → Agent 据此调整预取（**暂停时窗口从 15 s 放到 60 s**）。⚠ 只认**真播过之后**的 `playing=false`：换源后播放器还在 Loading 时也报 `false`，那不算暂停（T11-10e） |
| | `eof` | bool | **这一集放完了**（"该下一集了"的**信号**）→ Agent 自动下一集；到队尾就安静停下。⚠ 信号≠真值：换条/清空时我们主动收流，播放器也会收到一次干净的 EOS —— 所以 Agent 还要问自己缓冲的 `finished()`（ffmpeg 正常收尾），没到就**不跳集** |
| `video_control` | `action` | string | **让播放器播放/暂停**（T11-10f）：`"play"` / `"pause"` / `"toggle"`。⚠ 与音乐不同, 这是**新增**的一条 —— GUI 那颗播放/暂停按钮是"本地点", 原来没有任何命令能让 Agent/CLI 去按它（`video_state` 是回报, 不兼职）。Agent 收到后推一条 §3 的 `bilibili{control{action,seq}}` 给 GUI 去执行, 再等 GUI 的回报推 `control_result`。失败（没在播 / 没 GUI 连上 / 不认识的 action）一律回 `llm`。CLI 入口: `assistant video …` |
| `query_schedule` | — | — | 问一句"你最近触发过哪些日程"；`payload` 必须是 `{}`。应答是随后那条 §3 的 `schedule`（`kind:"state"`） |
| `music_play_pause` | — | — | 暂停/继续**当前这首**（T8-4）；PC 上没在放时**从环形队列当前位置起播**（T8-5b）。应答是随后那条 `music` 推送（§3） |
| `music_next` / `music_prev` | — | — | 在**环形队列**里前后走一格 —— 到尾回第一首、到首回最后一首（T8-5b）；队列空时回一条 `llm` 说明 |
| `music_stop` | — | — | 停止播放（**不改**本地库） |
| `set_config` | `id` | string | **回执 id**（调用方随便给，非空字符串；Agent 原样带回）—— 见 §3 的 `config_result` |
| | `keys` | object | `{"点号路径": "字符串值"}`：要改的那些设置项（T14-2）。⚠ **键清单以 `config.example.yaml` 为准**：不在模板里的键、结构级（映射/序列）的键、类型不对的值一律**拒绝且一个字节都不写**；值没变就不写、不留 `.bak`。空对象 `{}` 是合法的（= 只让 Agent 重新派生一次 `llm.env`） |
| | `credentials` | object | **可选**：B 站凭据 `{"SESSDATA"/"bili_jct"/"DedeUserID": "…"}`（写进 `bilibili.cookie_file` 指的**另一个文件**）。⚠ 它在**写配置之前**就校验：键写错时配置那一行也不落盘（不留"配置写了、凭据没写"的中间态） |
| `wifi_control` | `action` | string | **本机 WiFi**（T14-9）：`status` / `scan` / `connect` / `forget` / `autoconnect` / `reconnect`。应答**就是**随后那条 `wifi` 推送（§3） |
| | `id` | string | 回执 id（**动作类建议给**，界面靠它认领；查询类可省） |
| | `ssid` | string | `connect` / `forget` / `autoconnect` 要操作的那个网络 |
| | `password` | string | `connect` 时的密码。⚠ **只在这一条命令里经过一次**：Agent 侧要么走 `nmcli --ask` 的 **stdin**，要么写进 NM 自己的 keyfile（**0600 root**）—— **永不进 argv、不进 `config.yaml`、不进日志** |
| | `autoconnect` | bool | `connect` 时"记住并自动连接"（默认 true）；`autoconnect` 动作时是目标值 |

> ⚠ **为什么名字是 `wifi_control` 而不是 `wifi`**：协议不变式是"topic 与 command 的名字
> **不相交**"（`tests/test_ipc_protocol.py::test_topics_and_commands_do_not_overlap`），
> 而 topic 已经叫 `wifi` 了。同款的还有 `video_control`（topic 是 `bilibili`）。
> ⚠ **为什么 GUI 不自己调 nmcli**：与 `set_config` 同一条立场（[`adr/0005`](adr/0005-config-single-writer.md)）——
> 系统动作只由板端那个 root 服务做；GUI 只发命令。
> ⚠ **`forget` 的危险**：板端 wlan0 是唯一链路，忘记正在用的档案 = 板子失联。
> 回执里的 `was_active` 就是给界面弹确认用的；Agent 侧只删点名的那一个档案。

> ⚠ **为什么 GUI 要"求"Agent 去写**（T14-2）：`config.yaml` 与 `llm.env` 的写入者**只有 Agent 一个**
> （见 [`adr/0005`](adr/0005-config-single-writer.md)）—— 两个进程各写一份，就会互相覆盖、
> `.bak` 也会互相盖掉。所以 GUI 只发"要改哪些键"，落盘、校验、`.bak`、派生 `llm.env`
> 全在 Agent 侧一次做完，再回一条 `config_result`。
> ⚠ Agent 没在跑时 GUI 就改不了设置（**刻意不退回直写**）：界面要如实说"Agent 没连上"。

> ⚠ **音乐那条线的分工（T8-5b）**：**队列内容**由**对话**决定（工具 `next_music` 的
> `enqueue` / `clear_queue`），
> **播放控制**由**四个按钮**决定（`agent/core/music.py::MusicPlayer.toggle()/step()`）。
> 队列是**环形**的，曲终**自动下一首**；队列只在 Agent 内部用，**不推给 GUI**。
> 工具侧因此**没有** play/pause/next/prev（只有 `volume`）。见 [`music.md`](music.md) §4.2。
> 这三个按钮失败时（音乐没开 / PC 上没在放 / 队列是空的）都会回一条 `llm` 说明 ——
> 不假装换了一首。

> ⚠ **B 站那条线的分工（T11-6）**：**队列内容**只有两个来源 —— **对话关键词**
> （工具 `bilibili_search`）与**画面认出的游戏**（Agent 在 GAME 模式里的循环, 与 LLM 无关）；
> **播放/换集**由 GUI 的三个动作决定（`next_bilibili` / `prev_bilibili` / `bilibili_pick`）。
> 也就是说"清队列/切集"这类动作**只走 Agent, 不进 LLM 工具**（你定的）。
> ⚠ 队列**只在 GAME 模式里有意义**（视频就是游戏模式主区在放的东西）; 那个工具也只在 GAME 可见。
> ⚠ 队列是**窗口**不是全集：目标 = 3×预览栏格数, 往哪边走就往哪边补页
> （见 [`../agent/core/bilibili.py`](../agent/core/bilibili.py) 模块头）。

注意：

- **没有参数的 command 也必须带 `payload`**，写成 `{}`。缺 `payload` 字段会被判为非法消息。
- 少了 `payload` 里该有的键（例如 `switch_mode` 缺 `value`）**不会**被当成"用默认值"：
  该条命令被丢弃，并记一条**点名字段**的 warning。
- ⚠ **T7-3 删掉了 `next_wallpaper` 命令**（Phase 7 T3 加的，T6 还给它接上了状态权限表）：
  **换壁纸只走对话** —— 对 Agent 说"换一张安静的深色风景"，由 LLM 调
  `next_wallpaper` 工具（见 [`tagging.md` §6](tagging.md)）。删它的理由：按钮只能
  "按文件名翻下一张"，而标签化之后"换成什么样"只有自然语言说得清。GUI 侧同步删掉了
  主区那个「下一张」按钮与 `--next-wallpaper-demo`。老客户端真发这条命令过来时，
  Agent 按"不认识的 action"处理：**记 warning、不回话**（不会假装换好了）。
- ⚠ **`next_bilibili` 自 T11-6 起接上下游了**：它会真的换到下一集并开始放。
  Phase 6 那张"还没接线的命令"表（`UNWIRED_COMMAND_NOTES`）**现在是空的** ——
  也就是说没有任何命令会再回"还没接入"。表留着是给"以后再出现没接下游的命令"用的。
  未知命令（老客户端发来的）仍按"不认识的 action"处理：**记 warning、不回话**。
  失败（队列是空的 / B 站没开 / 这条放不了）会回一条 `llm` 说明 —— 点了没反应最难查。
- `switch_mode` 的合法性由 Agent 侧状态机判定：非法转换（例如 `STUDY → GAME`）
  **不会**报协议错，而是被拒绝并回一条 `status` 说明当前真实状态。
  GUI 应当以随后收到的 `status` 为准，不要乐观地自行切换显示。
- `query_schedule` 的应答**就是**随后那条 `schedule` 推送（`kind:"state"`）——
  与上面 `switch_mode` 的应答是随后那条 `status` 是同一个套路：**没有请求 id**，
  发出请求的一方等自己那条推送即可。Agent 侧若没有调度器（例如没接 runtime），
  它会回一条 `llm` 说明这件事，**不会静默**。

**示例**

```
{"action":"switch_mode","payload":{"value":"GAME"}}
{"action":"next_wallpaper","payload":{}}
{"action":"chat_input","payload":{"text":"帮我看看现在几点了"}}
{"action":"next_bilibili","payload":{}}
{"action":"query_schedule","payload":{}}
{"action":"set_config","payload":{"id":"7f2","keys":{"study.relative_band":"0.06"}}}
```

---

## 5. 完整消息示例（字节级）

`status` 一条，UTF-8 编码后写进 socket 的字节：

```
7b 22 74 6f 70 69 63 22 3a 22 73 74 61 74 75 73   {"topic":"status
22 2c 22 64 61 74 61 22 3a 7b 22 6d 6f 64 65 22   ","data":{"mode"
3a 22 53 54 55 44 59 22 2c 22 63 6f 6e 6e 65 63   :"STUDY","connec
74 65 64 22 3a 74 72 75 65 7d 2c 22 74 69 6d 65   ted":true},"time
73 74 61 6d 70 22 3a 31 32 33 34 35 36 37 38 39   stamp":123456789
30 2e 31 32 33 7d 0a                              0.123}.
```

文本形式（`\n` 表示 0x0A）：

```json
{"topic":"status","data":{"mode":"STUDY","connected":true},"timestamp":1234567890.123}
```

带中文的 `llm`（`ensure_ascii=False`，中文以 UTF-8 原样出现）：

```json
{"topic":"llm","data":{"text":"已经切换到学习模式。"},"timestamp":1234567890.456}
```

> 两种写法都能解析：中文既可以直接是 UTF-8 字节，也可以是 `\uXXXX` 转义
> （`"\u5df2\u7ecf..."`）。**两侧都必须接受这两种形式** —— JSON 解析器本来就都支持。
> Python 侧 `encode()` 用原样 UTF-8（便于抓包/看日志），Qt 侧 `toJson()` 也只输出
> 原样 UTF-8。

命令方向（`encode_command()`，注意**没有 `timestamp`**，长度 52 字节）：

```
7b 22 61 63 74 69 6f 6e 22 3a 22 63 68 61 74 5f   {"action":"chat_
69 6e 70 75 74 22 2c 22 70 61 79 6c 6f 61 64 22   input","payload"
3a 7b 22 74 65 78 74 22 3a 22 e4 bd a0 e5 a5 bd   :{"text":"你好
22 7d 7d 0a                                       "}}.
```

文本形式：

```json
{"action":"chat_input","payload":{"text":"你好"}}
```

---

## 6. 错误处理约定

**核心原则：一条坏消息只影响它自己。丢弃 + 记日志，不断开连接。**

| 情况 | 处理 |
| --- | --- |
| 不是合法 JSON | 丢弃该行，记 warning，不断开 |
| 不是合法 UTF-8 | 丢弃该行，记 warning，不断开 |
| 是 JSON 但不是 object（数组/标量） | 丢弃，记 warning |
| **推送**方向：缺 `topic` / 不是非空字符串 | 丢弃，记 warning |
| **推送**方向：缺 `data` / `data` 不是 object | 丢弃，记 warning |
| **推送**方向：缺 `timestamp` / 不是数字 | 丢弃，记 warning |
| **命令**方向：缺 `action` / 不是非空字符串 | 丢弃，记 warning |
| **命令**方向：缺 `payload` / `payload` 不是 object | 丢弃，记 warning |
| **信封发错方向**（命令位给了 `{topic,data,…}`，或推送位给了 `{action,payload}`） | 丢弃，记 warning —— 报的正是"缺本方向那个必填字段"（如缺 `action`）。**不**做双信封兼容 |
| 超过 1 MiB（一直不发换行） | 丢弃该行，记 warning，不断开 |
| `topic` / `action` 不认识 | **忽略**（不是错误）：可能对端版本更新 |
| 已知命令，但 `payload` 里缺该有的键（如 `switch_mode` 缺 `value`） | 丢弃并记**点名字段**的 warning；**不影响其它消息** |
| 对端关闭连接（EOF） | 关闭该连接，继续 `accept` 新连接 |

约定细节：

- 丢弃时**必须记日志**（包含原始行内容的截断形式，便于定位），否则坏消息会变成
  静默丢数据，非常难查。
- **不做**任何"猜测性修复"：例如缺 `data` / `payload` 就去猜它想干什么，或者
  `switch_mode` 少了 `value` 就当成默认模式。宁可丢一条。
- 解析失败的**计数器**建议暴露出来（日志或诊断接口），方便判断是协议不兼容还是
  偶发抖动。
- 单条消息处理失败（例如 `switch_mode` 触发非法状态转换）**不算协议错误**，
  按业务逻辑处理并回 `status`。

---

## 7. 两侧实现要点

### Python（Agent）

```python
from agent.ipc import (encode, decode_full, encode_command, decode_command,
                       TOPIC_STATUS, COMMAND_CHAT_INPUT, MODE_STUDY)

# 发推送 (Agent -> GUI): topic 信封
sock.sendall(encode(TOPIC_STATUS, {"mode": MODE_STUDY, "connected": True}))

# 发命令 (GUI -> Agent): action 信封, 没有 timestamp
sock.sendall(encode_command(COMMAND_CHAT_INPUT, {"text": "现在几点了"}))

# 按行读 (这里以 Agent 为例: 入方向是命令):
buf = b""
buf += sock.recv(4096)
while b"\n" in buf:
    line, buf = buf.split(b"\n", 1)
    try:
        action, payload = decode_command(line)   # 命令信封
    except IpcProtocolError as exc:              # 丢弃 + 记日志, 连接继续
        log.warning("丢弃非法 IPC 命令: %r (%r)", line[:200], exc)
        continue
    handle(action, payload)
```

> **两端各自只收一个方向**：Agent 的读循环只解命令（`decode_command`），只发推送
> （`encode`）；GUI 反过来 —— 只发命令（`encode_command`），只解推送（`decode_full`）。
> 所以"收错形态"永远是**对端发错了方向**或版本不一致，丢掉并记 warning 是对的。

要点：

- `encode()` / `encode_command()` 都返回**含结尾 `\n` 的完整字节**，直接 `sendall` 即可，
  不要重复加换行。
- `decode()` / `decode_full()` / `decode_command()` 结尾有无 `\n` 都能吃，但只剥**结尾**：
  行首空白会让 JSON 解析失败，那是应该报错的。
- 推送要时间戳用 `decode_full()`（`decode()` 按签名只返回 `(topic, data)`）；
  命令没有时间戳，用 `decode_command() -> (action, payload)`。
- 三个 encode 的负载传错类型（list/str/…）都会抛 `InvalidMessageError`，不要吞。

### C++ / Qt5（GUI）

- 读：`QTcpSocket` 不支持 `AF_UNIX`，用 `QLocalSocket`（Windows 上是命名管道，
  在 Linux 上就是 Unix domain socket）。用 `readyRead` → 追加到缓冲 →
  `while` 找 `\n` 切分。**不要**假设一次 `readyRead` 正好是一条消息。
- 解析：`QJsonDocument::fromJson(line, &err)`；`err.error != QJsonParseError::NoError`
  → 丢弃 + `qWarning`。
- 取值（**收**推送）：`doc.object().value("data").toObject()`；**校验 `isObject()`**，
  别直接 `toObject()`（标量会被静默变成空对象，把协议错误吞掉）。
- 时间戳：`double ts = obj.value("timestamp").toDouble();`（见 §2 的精度警告）。
- 写（**发**命令）：信封是 `{"action","payload"}`（**没有 timestamp**）——
  照 `gui/src/services/local_client.cpp::sendCommand()` 那样按插入顺序塞两个字段，
  再 `QJsonDocument(obj).toJson(QJsonDocument::Compact) + "\n"` 然后 `flush()`。
  参考实现见 `gui/src/services/local_client.cpp`。
- 发送前校验 `payload` 是 object；`next_wallpaper` / `next_bilibili` 发 `{}`。
- 断线重连：`disconnected` → 定时重连 `/tmp/agent.sock`。
  重复连接前先删掉自己创建的 socket 文件（Agent 侧负责 `unlink`）。

---

## 8. 常量对照

`agent/ipc/protocol.py` 里的名字（C++ 侧请照抄字面值）：

| Python 常量 | 值 |
| --- | --- |
| `SOCKET_PATH` | `"/tmp/agent.sock"` |
| `MESSAGE_SEPARATOR` | `b"\n"` |
| `ENCODING` | `"utf-8"` |
| `MAX_LINE_BYTES` | `1048576` |
| `TOPIC_STATUS` | `"status"` |
| `TOPIC_LLM` | `"llm"` |
| `TOPIC_WALLPAPER` | `"wallpaper"` |
| `TOPIC_MUSIC` | `"music"` |
| `TOPIC_SCHEDULE` | `"schedule"` |
| `TOPIC_BILIBILI` | `"bilibili"` |
| `TOPIC_CONFIG_RESULT` | `"config_result"` |
| `TOPIC_SERVICE_RESULT` | `"service_result"` |
| `TOPIC_WIFI` | `"wifi"` |
| `COMMAND_SWITCH_MODE` | `"switch_mode"` |
| `COMMAND_CHAT_INPUT` | `"chat_input"` |
| `COMMAND_NEXT_BILIBILI` | `"next_bilibili"` |
| `COMMAND_QUERY_SCHEDULE` | `"query_schedule"` |
| `COMMAND_MUSIC_PLAY_PAUSE` | `"music_play_pause"` |
| `COMMAND_MUSIC_NEXT` | `"music_next"` |
| `COMMAND_MUSIC_PREV` | `"music_prev"` |
| `COMMAND_MUSIC_STOP` | `"music_stop"` |
| `COMMAND_SET_CONFIG` | `"set_config"` |
| `COMMAND_WIFI` | `"wifi_control"` |

> ⚠ T7-3 删掉了 `COMMAND_NEXT_WALLPAPER`（`"next_wallpaper"`）：换壁纸只走对话，
> 不再有这条命令 —— 见 §4 的说明。C++ 侧也请把对应分支删掉（留着也不会有人发）。
| `ACTION_FIELD` | `"action"`（命令信封的字段名） |
| `PAYLOAD_FIELD` | `"payload"`（命令信封的字段名） |
| `MODE_SLEEP` / `MODE_IDLE` / `MODE_STUDY` / `MODE_GAME` | `"SLEEP"` / `"IDLE"` / `"STUDY"` / `"GAME"` |

---

## 9. 本协议不涉及

- **没有二进制帧**：全程文本 JSON，可读性优先于极致带宽。
- **没有版本号字段**：v1 不需要；演进靠"加字段 + 忽略未知字段"。
- **没有鉴权/加密**：同机 Unix socket，靠文件权限（`/tmp/agent.sock` 属主与 mode）
  隔离；不要跨机暴露这个 socket。

## 10. 实现位置

| 一侧 | 实现 | 说明 |
| --- | --- | --- |
| Agent（server） | `agent/ipc/local_server.py` 的 `LocalServer` | 监听、`push()` 推状态、`on_command()` 收命令（收的是**命令信封**）；`agent/ipc/__init__.py` 的 `build_ipc()` 是 `agent/main.py` 的接入点 |
| GUI（client） | `gui/src/services/local_client.cpp`（C++ / Qt5） | 发命令用 `{"action","payload"}`（`sendCommand`），收推送解 `{"topic","data","timestamp"}`（`handleLine`）—— 两个方向的信封不同，见 §2 |
| 测试用 client | `agent/ipc/local_client.py` 的 `LocalClient` | **只用于测试/联调**，不是生产 GUI；它发的命令信封与 C++ GUI 逐字节一致 |

