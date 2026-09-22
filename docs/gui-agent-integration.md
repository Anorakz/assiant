# GUI ⇄ Agent 对接说明（给 Agent 侧完善用）

> 本文只讲**GUI 这一端实际发什么、收什么、怎么反应**，用途是让你把 Agent 补齐。
> 协议总说明在 `docs/ipc-protocol.md`（唯一真源）；GUI 自身的使用/配置见 `docs/gui.md`。
> 文中每条载荷都对着 GUI 代码核过。

---

## 1. 传输

| 项 | 值 |
|---|---|
| 通道 | Unix domain socket，默认 `/tmp/agent.sock`（`--socket` 可改） |
| 角色 | **Agent 是服务端**（`bind`+`listen`+`accept`），**GUI 是客户端** |
| 编码 | UTF-8，**一行一条 JSON**（NDJSON），不要多行、不要 BOM |
| 方向 | 同一条连接**双向**：GUI → Agent 发命令；Agent → GUI 推状态 |
| 信封 | ⚠ **两个方向不一样**：命令 `{"action","payload"}`（§3），推送 `{"topic","data","timestamp"}`（§2）。别混用 |
| 重连 | GUI 断线后**每 1s 重试**；Agent 重启后重新 `accept` 即可 |

GUI 侧日志（排查用）：连上打 `[ipc] 已连接`；每条收到的报文打 `[recv] <topic> {...}`；
每个命令打 `[send] ...`。**Agent 只要保证"能连上 + 按行收发 JSON"**，界面就会跟着动。

调试替代：`agent_gui --stdio` 走终端（`@switch_mode` 之类），`agent_gui <socket>` 等价 `--socket`。

---

## 2. Agent → GUI：推送（topic）

**信封与每个 topic 的 data 字段/类型以 [`ipc-protocol.md` §2/§3](ipc-protocol.md) 为准**
（那是唯一真源）。本节只讲**收不到协议细节的那一半**：GUI 收到之后做了什么。

| topic | GUI 反应 |
|---|---|
| `status` | 顶栏三态结论 + 模式徽标；`mode=GAME` → 主区切**视频区**、下区域切**B站封面**；其它模式 → 主区还给壁纸、下区域切**音乐条**；右区域按钮组按模式变化（见 §4） |
| `llm` | 追加一条助手气泡；并收起"思考中…"。**只在你回过 `chat_input` 之后才有意义** |
| `wallpaper` | 设为全局壁纸（等比铺满 + 居中裁切，200ms 淡入）。同一个 `path` 重复推会被忽略；**文件读不到** → 兜底底色 + 主区橙色提示，程序不崩 |
| `music` | 音乐条曲目名 + 播放/暂停图标。`title` 为空 → 显示"未播放" |

**兼容性要点**

- **未知 topic 不会被当成错误**：GUI 计入 `ignoredTopicCount` 后忽略。所以你**新增 topic 不会打崩 GUI**（但也不会显示，需要 GUI 侧加代码）。
- **字段是部分更新**：只推你有的字段，缺的字段保持原值（例如 `status` 只带 `mode` 不会把 `connected` 清掉）。
- `status.connected` 表示**串流主机（moonlight/sunshine）**是否就绪，**不是** IPC 链路本身。
  不确定就别带这个字段，GUI 会按"主机状态未知"处理。

---

## 3. GUI → Agent：命令（action）

信封（`{"action","payload"}`，无 `timestamp`）与 action / payload 的定义见
[`ipc-protocol.md` §4](ipc-protocol.md)。本节只讲 **GUI 在哪儿发、期望你做什么**。

| action | GUI 里何处触发 | 期望 Agent 做什么 |
|---|---|---|
| `switch_mode` | 右区域模式按钮。当前是 IDLE/未知/非法值时给三个入口（睡眠/学习/游戏）；非空闲时只给「退出当前模式」，点它发 `IDLE` | 切模式（真正行为在你这边），然后**回推 `status{mode}`** 让界面同步 |
| `chat_input` | 对话区输入行 + 发送按钮（**仅"已连接"时可发**；断连时按钮禁用并提示） | 跑 LLM，然后推 `llm{text}`（若模式有变再推 `status`） |
| `next_wallpaper` | 主区右下角「下一张」 | 挑下一张壁纸并推 `wallpaper{path,index}` |
| `next_bilibili` | 视频控制条「下一集」 | 切下一集，并推你有的状态（`status`/封面等） |

⚠ 注意 `switch_mode` 的键是 **`value`**（不是 `mode`）—— `mode` 是 §2 里 `status` **推送**
的字段，方向不同，别混。这条以及"没有参数的 command 也必须带 `payload: {}`"等细节
都在 `ipc-protocol.md` §4，**改动请改那边**。

上面这个信封（`{"action","payload"}`，无 `timestamp`）已与 `docs/ipc-protocol.md` §4 对齐。
历史提醒（Phase 6 D1/D2 之前，勿再退回）：Agent 侧当时收的是 `{"topic","data","timestamp"}`
**并且**读 `payload["mode"]` —— 两个不一致叠加的结果是 **GUI 发的每一条命令都被整条丢掉**，
而测试全绿（测试客户端发的是 topic 形态）。现在 Agent 侧由 `decode_command()` 收这个信封、
`switch_mode` 读 `value`，有专门的用例钉住这两点。


GUI 在**未连接 / 主机未就绪**时不会发这些命令（会先在界面上提示"没发出去：与 Agent 未连接"）。

`next_wallpaper` / `next_bilibili` 的下游在 Phase 7，Agent 收到后会**回推一条 `llm`** 说明
「还没接入」—— GUI 按普通助手气泡显示即可。所以现在点这两下会看到一句话，而不是没反应
（这是刻意的：静默忽略会让人以为界面坏了）。

---

## 4. 界面状态是怎么由你的消息决定的

**三态结论（顶栏那个圆点）**是 GUI 自己合成的，规则：

| 情况 | 显示 |
|---|---|
| 从没连上过 Agent | 未连接（灰） |
| 连过但当前断了 | 重连中（黄） |
| Agent 在线、主机状态未知或就绪 | 已连接（绿） |
| Agent 在线、但 `status.connected=false` | 重连中（黄） |

**模式 → 界面**（`status.mode` 驱动）：

| mode | 主区 | 下区域 | 右区域按钮组 |
|---|---|---|---|
| `GAME` | 视频区（本地文件可播；控制条内嵌） | B站封面（与音乐条**互斥**） | 只给「退出当前模式」（发 `IDLE`） |
| `STUDY` / `SLEEP` | 留给壁纸 | 音乐条 | 只给「退出当前模式」（发 `IDLE`） |
| `IDLE` / 未知 | 留给壁纸 | 音乐条 | 睡眠 / 学习 / 游戏 三选 |

---

## 5. 待你完善 Agent 才能"由占位变真"的清单

界面里这些位置**现在是占位**（看得见、点了只给"未接入"说明、不发协议）。要让它们变真，
需要你在 Agent 侧提供对应能力（同时告诉我，我加 GUI 侧的命令/字段）：

| 界面位置 | 现状 | 建议的协议扩展 |
|---|---|---|
| 音乐条 歌手 / 专辑 / 播放进度 | 灰显 + `未接入` | `music` 增加 `artist` `album` `position_s` `duration_s` |
| 音乐条 歌词字幕槽 | 灰显 + `未接入` | `music` 增加 `lyric`（或新 topic `lyric{line,next}`）；GUI 已预留 `LyricsProvider` 接口 |
| 音乐条 上一首 / 播放暂停 / 下一首 | 占位 | 新命令 `music_prev` `music_play_pause` `music_next` |
| 视频 上一集 / 倍速 | 占位（播放/暂停/全屏是**本地真功能**） | 新命令 `prev_bilibili` `set_speed{value}`；可选 topic `video{url}` 让 GUI 播串流 |
| 下区域 B站封面缩略图 | `未接入` | 新 topic `bilibili{cover, title}`（封面路径/URL + 标题） |
| 系统页 看门狗 | `未接入` | 新命令 `watchdog{enable}` + 新 topic `watchdog{enabled, last_feed}` |
| SigLIP 视觉 | 固定只读块 | 按 D2 定案**不提供开关**；如需可配置再说 |

另外两件事**本来就该 Agent 负责**（GUI 只发命令/显示结果）：模式（SLEEP/STUDY/GAME）的实际行为、
LLM 调用与降级。配置真源只有一份：GUI 的"模型测试页"写 `config/config.yaml` 的 `llm` 段
（规范词只认 `edge`／`cloud`／`disabled`，`local`／`board` 是历史别名），再由它**派生**出
`llm/config/llm.env` 喂 llama-server。**Agent 读 `config/config.yaml`**（唯一被允许的写是
"删掉已触发的一次性日程"，见 [`config-sources.md`](config-sources.md) §3.1）—— 不要读派生文件，
也不要自己另存一份。约定见 [`config-sources.md`](config-sources.md)。

---

## 6. 没有真 Agent 时怎么测

仓里已有两个假 Agent，**都在版本库里**（不需要 `temp/` 下的临时脚本）：

| 脚本 | 能力 | 用来验证 |
|---|---|---|
| `gui/tools/fake_agent.py` | 默认**只推**：4 个已知 topic + "一条消息拆成两次 send" + 4 种坏消息 + 坏消息之后的合法消息。加 `--echo` 则**收命令**（打印 `action`/`payload`，默认回推一条 `llm`，`--no-reply` 可关） | GUI 的**收**与**发**、重连（可 kill 后重启） |
| `gui/tests/local_server.py` | 连上即按 `--push` 推，逐行打印 `RECV <原始行>` | GUI 的 QTest（`test_local_client` 的真对端，不是 mock） |

```bash
# 终端 A：假 Agent（推一组边界用例，并把收到的命令打印出来）
python3 gui/tools/fake_agent.py --path /tmp/a.sock          # 只推
python3 gui/tools/fake_agent.py --path /tmp/a.sock --echo   # 收命令 + 回推
# 终端 B：GUI
./gui/build/agent_gui --windowed --socket /tmp/a.sock
```

两者的编码都走 `agent/ipc/protocol.py`，与真 Agent 同一套实现 —— 所以不会出现
"两边各自照文档手写、字段名不一致"（那正是 Phase 6 D1 那次 GUI 命令被整条丢掉的成因）。

---

## 7. 不要破坏的约定（改 Agent 时请守住）

1. `status.mode` 用**大写** `SLEEP/IDLE/STUDY/GAME`（GUI 按字面比较）。
2. 一行一条 JSON；不要发非 JSON 的行（会被记成解析错误并计数）。
3. `llm.text` 必须是字符串（GUI 直接当纯文本显示）。
4. 新增字段随便加（GUI 忽略未知字段）；**新增命令要跟 GUI 同步**，否则界面上不会有入口。
5. 断开时把连接关干净，让 GUI 走重连逻辑（不要只半关，GUI 会一直等）。
6. `wallpaper.path` 用**绝对路径**；同一张重复推会被 GUI 忽略（想强制刷新就先换个 index/path）。
