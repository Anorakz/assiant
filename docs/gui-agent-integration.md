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
| `wallpaper` | 设为全局壁纸（等比铺满 + 居中裁切，200ms 淡入）。同一个 `path` 重复推会被忽略；**文件读不到** → 兜底底色 + 主区橙色提示，程序不崩。⚠ 收到第一张之后，主区那两行开发占位文字会**收起来**（T3），让位给壁纸。⚠ T6 起**刚连上就会收到一条**（Agent 补推当前壁纸），不用等下一次切换 |
| `music` | 音乐条曲目名 + 播放/暂停图标。`title` 为空 → 显示"未播放" |
| `bilibili` | **B 站队列**（T11-7）：`queue[]` 填**预览栏**（缩略图 + 标题 + `时长 · 播放量`，选中项高亮）、`current` 填**地址栏**（只读 `https://www.bilibili.com/video/<bvid>`）与**下区域封面**（封面图 + 标题 + 作者/时长/播放量 + `第 N / M 条` + 来源行）。`stream` 非空就**换源播放**（板端是 FIFO 本地路径，不是 URL；空 = 清屏）。⚠ **界面上一处清晰度都没有** —— 清晰度只走 `llm` 气泡（你定的）。⚠ 队列为空时预览栏显示一行灰字说明，**不上报格数** |

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
| `next_bilibili` | 视频控制条「下一集」 | 队列往前走一格并**开始放那一集**；队列到头就推一句 `llm` 说明 |
| `prev_bilibili` | 视频控制条「上一集」（T11-7 从占位转正） | 同上，往**前**走一格 |
| `bilibili_pick` | **预览栏第 N 格被点**（`{"index":N}`） | 放队列里的第 N 条（**只有用户点才会播**）；失败推一句 `llm` |
| `bilibili_viewport` | 预览栏格数变化（连接后首次算出 + 尺寸变化，`{"visible":N}`） | 把队列目标改成 `3×N`（**不播**、不提前缓冲） |
| `video_state` | 每 2 秒一次 + 这一条放完时（`{position_s,duration_s,playing,eof}`） | **不播**：暂停时把预取放宽到 60 s；`eof=true` → 自动下一集 |

> ⚠ **T7-3 删掉了 `next_wallpaper` 命令**（以及主区那个「下一张」按钮）：**换壁纸只走对话**
> —— 对 Agent 说"换一张安静的深色风景"，由 LLM 调 `next_wallpaper` 工具，Agent 再推
> `wallpaper{path,index}`（§2）。所以 GUI 这一侧**不需要为换壁纸做任何事**，
> 只负责画收到的 `wallpaper` 推送。理由见 [`tagging.md` §6](tagging.md)。

⚠ 注意 `switch_mode` 的键是 **`value`**（不是 `mode`）—— `mode` 是 §2 里 `status` **推送**
的字段，方向不同，别混。这条以及"没有参数的 command 也必须带 `payload: {}`"等细节
都在 `ipc-protocol.md` §4，**改动请改那边**。

上面这个信封（`{"action","payload"}`，无 `timestamp`）已与 `docs/ipc-protocol.md` §4 对齐。
历史提醒（Phase 6 D1/D2 之前，勿再退回）：Agent 侧当时收的是 `{"topic","data","timestamp"}`
**并且**读 `payload["mode"]` —— 两个不一致叠加的结果是 **GUI 发的每一条命令都被整条丢掉**，
而测试全绿（测试客户端发的是 topic 形态）。现在 Agent 侧由 `decode_command()` 收这个信封、
`switch_mode` 读 `value`，有专门的用例钉住这两点。


GUI 在**未连接 / 主机未就绪**时不会发这些命令（会先在界面上提示"没发出去：与 Agent 未连接"）。
⚠ 例外是 `bilibili_viewport` / `video_state` 这两条**自动回报**：没连上就**静静不发**
（只记一条 debug）—— 用户没点什么，却每 2 秒冒一句"没发出去"更糟。

**B 站那五条的现状（T11 已全部接上）**：`next_bilibili` 从 Phase 6 就存在，Phase 7 T11-6
把它与新增的四条一起接进 Runtime（`ipc-protocol.md` §4 是线格式的真源）。**播放只由 GUI 操作触发**
（点预览图 / 上一集 / 下一集），搜索与识别都**不起播、不提前缓冲**；队列内容只有两个来源 ——
**对话关键词**（工具 `bilibili_search`，只在 GAME 可见）与**画面认出的游戏**。
⚠ 队列一条都没有时点「下一集」，Agent 会推一句 `llm` 说明（不是静默无反应）。
细节见 [`bilibili.md`](bilibili.md)。

**壁纸现在是"对话驱动"的（T7-3）**：Agent 那边由 LLM 调 `next_wallpaper` 工具
（`match` 可以写成 `scene=anime` / `anime` / `ip=EVA`），成功就推 `wallpaper{path,index}`，
GUI 收到就画 —— 与过去按钮触发时**完全一样的那条推送**。两条相关行为（T6 留下来的）：
- **连上就有一张**：新客户端连上时 Agent 会补推当前壁纸（没有就选第一张），所以刚打开
  GUI 主区不再是兜底底色。
- **状态权限表照旧管着"换壁纸"这件事**：`SLEEP` / `GAME` 下模型调这个工具会被拒
  （表见 `architecture.md` §4.1）。补推不受限。
只是现在**没有按钮**了：想换壁纸就在对话里说。
造几张不同比例的样张来试: `python3 scripts/make-wallpaper-samples.py`。

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
| `GAME` | 视频区（本地文件/板端 FIFO 流可播；控制条内嵌；**底下一条预览栏 + 地址栏**） | B站封面/标题（与音乐条**互斥**） | 只给「退出当前模式」（发 `IDLE`） |
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
| 视频 倍速 | 占位（播放/暂停/全屏是**本地真功能**） | 新命令 `set_speed{value}`（T11 定：**倍速搁置**，不做） |
| 系统页 看门狗 | `未接入` | 新命令 `watchdog{enable}` + 新 topic `watchdog{enabled, last_feed}` |
| SigLIP 视觉 | 固定只读块 | 按 D2 定案**不提供开关**；如需可配置再说 |

> ✅ **T11 已经"由占位变真"的三处**（不用再做了）：视频「上一集」→ `prev_bilibili`；
> 下区域 B站封面 → topic `bilibili`（封面/标题/第几条/来源）；视频控制条「下一集」→ 原来那个
> "还没接入"的说明也撤了。另加**预览栏 + 只读地址栏**两条新界面。
> 完整链路见 [`bilibili.md`](bilibili.md)。

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
| `gui/tools/fake_agent.py` | 默认**只推**：4 个已知 topic + "一条消息拆成两次 send" + 4 种坏消息 + 坏消息之后的合法消息。加 `--echo` 则**收命令**（打印 `action`/`payload`，默认回推一条 `llm`，`--no-reply` 可关）。T11-7 起还能推**真 B 站队列**：`--bilibili <关键词｜json>`（走真 API 搜一页，或直接读一份载荷），`--bilibili-stream <本地文件>` 会在收到"要放"的命令时**补推一条 `stream` 就绪**的队列（替身缓冲线程，用来验播放与进度回报） | GUI 的**收**与**发**、重连（可 kill 后重启）、B 站预览栏/封面/播放全链路 |
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
