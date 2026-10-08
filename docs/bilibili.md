# B 站视频（Phase 7 T11）

> 一句话：**GAME 模式下，画面认出你在玩什么游戏（或你在对话里点名），Agent 去 B 站搜一批视频
> 排成队列，把封面与地址推给 GUI；你点预览图/上一集/下一集才开始播 —— 视频在板端放，
> 内容只在缓冲里、不落盘。**
>
> 事实来源：本文 + [`ipc-protocol.md`](ipc-protocol.md) §3/§4（线格式）+ `config/config.example.yaml`
> 的 `bilibili:` 段（配置键）。所有数字都是**板端实测**（T11-0/1/3/6/7 的探针与取证），
> 猜的地方都标了"未实测"。

---

## 1. 为什么是这么一条链路（先看约束，再看设计）

| 实测约束 | 后果 |
| --- | --- |
| 板端 **`souphttpsrc` 是坏的**（`souphttpsrc ! fakesink` 都 SIGABRT；`libgstreamer 1.18.5` 配 `gst-plugins-good 1.16.3` 版本错配，`dmesg` 无 OOM） | **不用 soup**：GUI 启动时设 `GST_PLUGIN_FEATURE_RANK=souphttpsrc:0` 把它降权，`playbin` 就自动选板上的 **`curlhttpsrc`**（实测正常）→ 供流走本机 HTTP |
| **FIFO 供流播放器一个字节都读不到**（T11-9 板端实测：`playbin uri=file://<FIFO>` 12 秒读走 **0 字节** —— FIFO `stat` size 恒为 0、不可 seek，preroll 过不去；同一时刻 `dd` 读到 291 s、`filesrc ! decodebin` 读到 210 s，说明管道通、是**播放器不认**） | 供流改 **本机 HTTP**（`http://127.0.0.1:<port>/stream/<bvid>?v=<token>`，`chunked` 边下边喂）；实测 static 与 chunked 两种都 preroll 通过并放到 EOS |
| `mp4` 写管道会 `Could not write header … Broken pipe`（管道不可 seek） | 中间格式固定 **MPEG-TS**（`-f mpegts`） |
| 直链/封面**必须带 `User-Agent` + `Referer`**，否则 CDN 回 **403** | 取流与取封面都带这两个头（`bilibili_api.ffmpeg_input_args` / GUI 的 `CoverLoader`） |
| `ffmpeg -c copy` 比实时快 **68×** | 必须有**背压**：窗口够了就不读，管道自然堵住 ffmpeg（否则几秒灌满内存）；走 http 之后还要**自己限速**（见 §4） |
| 板端**已装 MPP 硬解**（`mppvideodec`，rank 257，HEVC/AVC/VP8/VP9） | 解码交给播放器（GStreamer），**ffmpeg 只做 `-c copy` 合流/重封装，不转码**（你定的方案 ①） |
| 匿名会话要先访问一次首页拿 `buvid3`/`b_nut`，否则接口直接 **412** | `BilibiliApi.session()` 懒引导一次，cookie 只在内存 |
| 搜索分页**同一页连取 3 次完全一致**、页间零重叠，但 **`page > numPages` 不报错、会回重复内容** | 窗口靠**翻页 + 按 `bvid` 去重**，边界**按 `numPages` 判**，不看"结果是否为空" |

一句话的数据通路：

```
对话关键词 / 画面认出的游戏
        │
        ▼
agent/core/bilibili.py ── 滑动窗口队列（只存地址：bvid/标题/作者/时长/播放量/封面 URL）
        │  topic bilibili{queue[], index, current, keyword, source, …}
        ▼
GUI：预览栏（缩略图，点击 = bilibili_pick）+ 地址栏（只读）+ 下区域封面
        │  只有 GUI 操作才继续（你定的"等 GUI 操作才开始播放，不提前缓存"）
        ▼
agent/core/bilibili_buffer.py ── ffmpeg -c copy → MPEG-TS → 内存窗口（15 s 起播门槛）
        │  topic bilibili{stream: "http://127.0.0.1:8765/stream/<bvid>?v=<8位token>", ready: true}
        ▼
GUI：QMediaPlayer 读**那个本机地址**（GStreamer 经 curlhttpsrc 拉，硬解仍选 mppvideodec）
        └─ video_state{position_s,duration_s,playing,eof} 回报 → 放完自动下一集
```

**不内嵌播放器、不折腾 DASH**：视频就是"板端主区那个 VideoPanel"，与本地文件播放**同一条路**
（`VideoPanel::setSource(路径或 URL)`）；DASH 只用来**取一条**合并后的流，播放端不必知道。

---

## 2. 队列：3× 预览栏格数的滑动窗口（D4）

- 目标长度 = **3 × 预览栏格数 N**（GUI 连上后上报 `bilibili_viewport{visible:N}`；
  没上报就兜底 `6` → 18 条），上限 `bilibili.queue.max`（默认 60）。
- 排序 = **B 站综合排序**（`order=totalrank`，不传 order 也是它）—— 跨页顺序稳定，窗口拼接才可信。
  ⚠ 实测：`order=click` 才是**按播放量降序**；`order=play` / `order=pubdate` **参数不生效**（回落综合排序）。
- 移动与补页：`next` 从**下一页**往尾部补、`prev` 从**上一页**往头部补；偏离太远的那一侧丢掉，
  `index` 跟着修正（当前那一条不动）。
- 边界（都实测过）：

| 情况 | 行为 |
| --- | --- |
| 已在 `page=1` 第 1 条还往前 | 如实说"前面没有了"，**不请求** `page=0` |
| 已在 `page >= numPages` 还往后 | 如实说"这批没有了" |
| `page > numPages` | **禁止请求**（它会回重复内容）；边界一律按 `numPages` 判 |
| 结果不足 3N（总共就这么多） | 队列就是这么长，如实说共几条 |
| 跨页/换关键词撞 `bvid` | 按 `bvid` 去重（窗口内去重） |

- **只存地址**：每条只有 `{bvid,title,author,duration_s,play,like,cover,url,pubdate}` ——
  不下载视频、不下载封面到磁盘（封面是 GUI 自己去 CDN 取的，见 §5）。
- 队列**不落盘**（进程内状态，重启即空）—— 与音乐队列、壁纸窗口同款。
- 两个来源，**对话优先**（D8）：

| 来源 | `source` | 谁触发 |
| --- | --- | --- |
| 对话里说了关键词 | `dialogue` | LLM 调工具 `bilibili_search{keyword}`（**只在 GAME 模式可见**） |
| 画面认出的游戏 | `screen` | Agent 自己在 GAME 里的观察循环（**不走 LLM 工具**） |

⚠ **有对话关键词就不抓画面**：关键词在效期内（直到换词/清空/离开 GAME）观察器**整个跳过**，一帧都不抓。

---

## 3. 游戏识别：画面锚点 + PC 进程双路（D5）

| 环节 | 规则 |
| --- | --- |
| 触发 | 默认 **60 s** 一次（`bilibili.game_watch.interval_s`）；**只在 GAME 模式**、**有对话关键词就跳过**、没帧/串流没跑也跳过（记 debug，不报错） |
| 第 1 路（画面） | 抓一帧 → SigLIP 编码 → 与 `config/game_anchors.jsonl` 里的锚点算余弦；`top1 ≥ 0.82` **且** 余量 `≥ 0.05` 才算"有把握" |
| 第 2 路（真值） | ssh 到 PC 读**进程名**（`Get-Process`；窗口标题在 session 0 里恒为空，实测拿不到）→ 按 `process_names` 映射成游戏名 |
| 融合 | 画面有把握且与进程一致 → 用它；锚点空/不够分 → 问进程；**两路不一致 → 以进程为准，并把这一帧登记成该游戏的锚点**（自纠错，越用越准） |
| 认不出 | **什么都不做**（不瞎搜、不改队列），日志如实写"认不出" |
| 更新队列 | **只有识别到的游戏变了**才重搜重建（否则会把你正在看的列表刷掉） |

**标定实测**（5 个游戏 / 13 张截图，`/home/kickpi/game_samples`）：画面锚点 leave-one-out top-1
**9/13 = 69%**（平均原型 62%）；正确时得分 0.616~0.915、误判时错误分最高 0.835 ——
**阈值区间重叠，单一阈值分不开**，所以才要"画面筛一遍 + 进程裁决 + 不一致就学下来"。
`top1 ≥ 0.82 且 margin ≥ 0.05` 这条规则在 13 张里只认 6 张，但**零误判**。

**锚点存哪**：索引 `config/game_anchors.jsonl`（一行一锚点：游戏名 + 768 维 float16 向量 +
截图路径 + 来源 + 时间），截图 `config/game_anchors/<游戏>/<时间戳>.jpg`（都在 `.gitignore` 里）。
写者只有 Agent 自己（`agent/core/game_anchors.py`）。

**模型常驻**：SigLIP 在你定的 `STUDY`/`GAME` 两个状态**常驻**（离开就卸载）；加载前先看
`MemAvailable`，低于水位就不加载并如实记日志。实测加载 **2.2~4.1 s**、单帧编码 **1.6~3.3 s**、
常驻 **923 MB**（卸载后回落到 111 MB）。

---

## 4. 播放与缓冲（D3）：15 s 起播、暂停延到 60 s

| 环节 | 做法 |
| --- | --- |
| **不提前缓存** | 搜索、识别、填预览栏**都不取直链、不起缓冲**；队列条目只占"元数据 + 封面" |
| 起播 | 你点预览图/上一集/下一集 → 取流（有 cookie 时单文件与 DASH **都问一遍，取清晰度高的**）→ `ffmpeg -c copy` → 内存窗口 → 一个**只绑 127.0.0.1** 的小 HTTP 服务按播放器的消费速度喂（`chunked`） |
| 门槛 | 窗口攒够 **15 s**（按码率折算字节）才把 **本机 URL** 推给 GUI（`stream` 字段）；**没开闸一个字节都不喂**（谁先连上也不能把门槛作废）；凑不够就"按现有缓冲起播"并如实说一句 |
| 播放中 | 领先 **15 s**（`buffer.initial_s`）—— ⚠ 走 http 之后播放器能一口气把整段吞进它自己的缓冲（板端实测几十秒就把 4 分半拉完），所以这条领先是**缓冲代理自己限速**（`_lead_ok()`：喂出去的秒数 ≤ 开闸至今的秒数 + 封顶），**不是**靠管道背压；⚠ **限速 = 让连接等着**（背压），**不是**把连接收掉 |
| **暂停**（GUI 回报 `playing=false`） | **继续预取**到 **60 s**（`buffer.max_s`）或 `MemAvailable < 400 MB` 为止。⚠ 只认**真播过之后**的 `playing=false`：GUI 在 `setMedia()` 之后头几秒还在 Loading，那几条报的也是 `false`，以前照单全收 -> 封顶当场跳到 60 s，播放器一口气吞掉 60 s 正好撞上限速（T11-10e 板端实测 `buffered=26.6s`） |
| 释放 | 客户端读到哪就放到哪（`_release_below`）——"播过即释放"；换条/清空 → **服务收掉 + 窗口整个放掉**；**全程不落盘** |
| 直链过期 | 上游 ffmpeg 非 0 退出（403 等）→ **重取直链 + 带 `-ss` 重连**，重试有限次；对 GUI 透明，日志如实记 |
| seek | **没有**（chunked 的流没有总长度，播放器也没法跳；GUI 也没有进度条）—— 想跳就换一条 |
| 片尾 | ffmpeg 退出码 `0/None` = **正常放完**（不重连）→ 缓冲 `finished()` 为真；GUI 报 `eof=true` → Agent **自动下一集**。⚠ `eof` 只是**信号**：换条/清空时我们主动收流，播放器照样收到一次干净的 EOS（板端实测曾因此**一路连跳 6 条**）—— 所以自动下一集要问**我们自己的真值**（缓冲到了 `ended` 才跳） |
| **谁能让它播放/暂停** | 三处，最后都落到 GUI 的播放器上：① GUI 内嵌控制条那颗按钮（本地点）；② **GAME 里 CLI** `assistant video play\|pause\|toggle`（T11-10f，走新命令 `video_control` → Agent 推 `bilibili{control{action,seq}}` → GUI 按）；③ Agent 侧的 `Runtime.bilibili_control("control", …)`（同一条路，CLI 就是它的前端）。⚠ GUI **没在放就不动手**（日志记一笔）；⚠ Agent 那边**没在播的缓冲 / 没 GUI 连上**都**如实拒绝**，不假装按了 |

内存量级（实测）：720P 码率 ≈ 258 KB/s → 15 s ≈ **3.9 MB**、60 s ≈ **15.5 MB**；360P 更低。
板端带宽 ≈ 658 KB/s，够 1× 实时（约 2.5× 余量）。

| 谁占多少（板端实测） | 量 |
| --- | --- |
| MemTotal / 无 swap | 3901 MB |
| 空载 used | 499 MB |
| llama-server | 1219 MB（长跑见过 2030 MB） |
| Agent | 121 MB |
| GUI | 127 MB |
| SigLIP（常驻时） | 923 MB |
| ffmpeg（`-c copy`） | 46 MB |
| 播放器 + 缓冲 | ~15 MB + 3.8~15 MB |
| → 常驻 + llama 冷启 | ≈ 2965 MB（剩 ~900 MB）→ 所以水位定 **400 MB** |

⚠ **不与 `assistant tag`（离线打标签）同时跑**：那个也要 SigLIP/NPU。

---

## 5. 封面与预览栏（GUI 侧）

- 队列里的 `cover` 是**B 站 CDN 的图片地址**，Agent **不代下图**。GUI 自己取：
  带 **UA + Referer**（不带就是 403）、内存缓存、同一张只下一次、**失败记下来不重试**
  （失败时封面那块写"封面没下来"，预览栏那一格就空着）。
- 预览栏：一排缩略图（图标 + 标题 + `时长 · 播放量`），点第 N 格 = `bilibili_pick{index:N}`
  （**只有真点击才发**；Agent 改 `index` 只是移动高亮）。
- 地址栏**只读**（显示 `https://www.bilibili.com/video/<bvid>`）：GUI 自己不能搜 ——
  关键词只从**对话或画面**来。
- ⚠ **界面上一处清晰度都没有**（你定的）：清晰度提示**只走 LLM 聊天气泡**。

---

## 6. 清晰度与 cookie 的真相

配置：`bilibili.cookie_file`（默认 `config/bilibili_cookie.json`，**相对路径按仓库根**）。
文件是人手写的凭据、**已进 `.gitignore`**；键名 `SESSDATA`（`SEESSDATA` 是常见笔误，
代码会**认下来并提醒**，实测那个笔误 B 站不认 → `nav code=-101`）。

| 路径 | 匿名 | 带 `SESSDATA` |
| --- | --- | --- |
| `fnval=1&platform=html5`（**单文件 mp4**） | 实测 `quality=64`（720P），`accept=[64,16]` | **一样**（cookie 不抬单文件的阶梯） |
| `fnval=16&platform=pc`（**DASH**，音视频分离） | 只到 480P | **`quality=80`（1080P）** |

所以：**要更高清晰度就得走 DASH**，而 DASH 是两条流 —— 我们用 `ffmpeg -c copy` 合成一条，
再交给播放端硬解（**不转码**）。实际拿到多少就报多少：

| 情况 | 聊天气泡里那句话 |
| --- | --- |
| 没配 cookie | `（没配 config/bilibili_cookie.json，这条只给到 360P；要高清得在板端配上 SESSDATA）` |
| 配了但这条就是低 | `（这条 B 站只给到 360P，和登不登录无关）` |
| 1080P 及以上 | 不念叨 |
| cookie 失效（`-101`） | 如实说"cookie 失效，去更新那个文件" |

---

## 7. 工具与权限（D6）

| 项 | 值 |
| --- | --- |
| 工具名 | `bilibili_search` |
| 可见状态 | **只有 `GAME`**（所以 STUDY 的工具清单**一个字符都不涨**：3562/3600 不变） |
| 参数 | 只有一个必填 `keyword`（1~64 字）。**没有 action 枚举** —— 清队列/切集/选片都不进 LLM |
| 职责 | **只把对话里的关键词交给队列**（搜 + 排 + 填预览栏）；**不播** |
| 播不播 | 只有 GUI 操作（点预览图/上一集/下一集）才播 |

其余动作**只走 Agent**：GUI 命令（`bilibili_pick` / `next_bilibili` / `prev_bilibili` /
`bilibili_viewport` / `video_state` / **`video_control`**）与 Agent 自己的观察循环。线格式见
[`ipc-protocol.md`](ipc-protocol.md) §4。

> ⚠ `video_control`（T11-10f）是**唯一**能让播放器播放/暂停的命令 —— 它由 `assistant video …`
> （或别的客户端）发进来，Agent 再推 `bilibili{control{action,seq}}` 让 **GUI 去按**。
> 见 [`cli.md`](cli.md) §4 的 `video` 一节。

---

## 8. 配置键（`bilibili:` 段）

模板与逐键注释在 `config/config.example.yaml`；这里只列"有读取者的键"：

| 键 | 默认 | 谁读 |
| --- | --- | --- |
| `enabled` | `true` | `BilibiliApi.from_config`（`false` → 工具整个不装、GPU/IPC 那条路都不起） |
| `cookie_file` | `config/bilibili_cookie.json` | 同上（**凭据文件，别提交**） |
| `timeout_s` | 15 | 同上（单次 HTTP 超时） |
| `queue.viewport_fallback` | 6 | `Runtime._start_bilibili`（GUI 还没上报格数时的兜底） |
| `queue.max` | 60 | 同上（窗口硬上限） |
| `buffer.transport` | `http` | `Runtime._bilibili_play`（`http` 供流给播放器 / `fifo` 只给 dd/cat 排障） |
| `buffer.port` | `8765` | `BilibiliBuffer`（本机 HTTP 端口；被占了自动换一个并把真端口推给 GUI） |
| `buffer.dir` | `/tmp` | `BilibiliBuffer`（**只有 `transport: fifo` 用**：管道建在哪） |
| `buffer.initial_s` / `max_s` | 15 / 60 | 同上（起播门槛 / 暂停时的封顶） |
| `buffer.mem_watermark_mb` | 400 | 同上（`MemAvailable` 低于它就停读，让管道堵 ffmpeg） |
| `game_watch.enabled` | `true` | `Runtime._start_bilibili`（关掉就只看画面锚点、不问 PC 进程） |
| `game_watch.interval_s` | 60 | `GameWatcher`（多久认一次） |
| `game_watch.confident_score` / `confident_margin` | 0.82 / 0.05 | 同上（"有把握"的门槛，来自 T11-0 标定） |
| `game_watch.anchor_file` | `config/game_anchors.jsonl` | `GameAnchors`（锚点索引，派生数据） |
| `game_watch.process_names` | `{}` | `PcProbe`（**进程名 → 游戏名**的映射表） |
| `game_watch.mem_watermark_mb` | 400 | 同 `buffer.mem_watermark_mb`（加载 SigLIP 前的检查） |

`process_names` 例子（老板给的那三个 + 两个按名字猜的）：

```yaml
bilibili:
  game_watch:
    process_names:
      WHITE ALBUM Memories like Falling Snow.exe: white album
      hatsuyuki.exe: 初雪樱
      Amakano3.exe: 甜蜜女友3
      hoi4.exe: hearts of iron iv
      stellaris.exe: stellaris
```

⚠ `PcProbe` 的 ssh 连接复用 `music:` 段的 `pc_host` / `pc_user` / `pc_port` / `ssh`
（**同一台 PC**，不另配一套）。

---

## 9. 已知边界（如实记，没做就是没做）

- **DASH 1080P 需要 cookie**：没配 cookie 就是单文件的 360P~720P（视视频而定）。
  ⚠ 部分视频匿名连 720P 都拿不到（实测阶梯有 `[80,16]` / `[64,16]` / `[16]` 三种）。
- **没有倍速**（你定的：搁置）。控制条那颗 `1.0x` 仍是占位，点了只给一句说明。
- **没有 seek**（chunked 流没有总长度、播放器没法跳）。想跳就换一条。
- **不做反馈切换**（D5）：没有"不想看这个"的整批拉黑。
- **不做**：弹幕、点赞/投币/收藏、直播、番剧、字幕、后台预下载下一集、把视频落到磁盘。
- **多分P 只放 P1**（分P 数如实显示，不做选集）。
- **窗口标题拿不到**（sshd 在 session 0，`MainWindowTitle` 恒为空）→ 进程路只用**进程名**。
- **接口是非官方的**：只在这一个模块里封装（`agent/net/bilibili_api.py`），坏了只修一处；
  错误话术都写得能照做（412 风控等几分钟、`-101` 去更新 cookie、`-404` 视频没了）。
- **队列不落盘**：重启即空（故意的，与音乐/壁纸同款）。
- **串流没跑就没有帧**：画面识别在"没有帧"时跳过（记 debug），不是错误。

---

## 10. 怎么验

**离线单测**（PC 与板端都跑，`scripts/test-python.sh` / `.ps1` 的清单里）：

| 测试 | 钉什么 |
| --- | --- |
| `tests/test_bilibili_api.py` | 假 transport：清洗/分页/阶梯/cookie 三态/错误话术 |
| `tests/test_bilibili_queue.py` | 窗口不变量、边界、去重、极端 N |
| `tests/test_bilibili_buffer.py` | 假 ffmpeg + 假 FIFO/假 HTTP 客户端：门槛、封顶、限速、水位、背压、过期重取、释放 |
| `tests/test_game_watch.py` | 假编码器 + 假进程读数：阈值、自纠错写锚点、防抖、关键词优先 |
| `tests/test_bilibili_tool.py` | GAME-only、单参数、诚实失败 |
| `tests/test_bilibili_config.py` | **模板守卫**：`config.example.yaml` 的 `bilibili:` 段能被真构造器吃下、键不多不少、默认值与代码一致 |
| `tests/test_main.py::TestBilibiliWiring` | 只有 GUI 操作才起播、`eof` 自动下一集、载荷带 `queue`、**播放控制**（`control`：没在播/没 GUI 拒绝、toggle 按真值解析、`control_result` 只在播放器回报后推一条） |

**板端 C++ 单测**（`ctest`，`scripts/sync-gui.ps1 -Test`）：`test_bilibili_format` /
`test_bilibili_preview` / `test_cover_loader` / `test_video_panel`（含**播放控制**
`control{action,seq}`：没源忽略、同 seq 不重复动手）/ `test_bottom_bar`。

**板端真跑**（`tests/board/t11_accept.py`，见本地工作记录 `todo.md` 的 T11 块）：
真搜索 → 真队列 3N → 真封面下到 → 点预览图起播（15 s 门槛日志 + `position_s` 递增 + 出声）→
上一集/下一集换条 → 暂停缓冲涨到 60 s → 断网诚实报错 → cookie 三态三句话 → 识别双路 + 锚点自纠错。

手动试（板端）：

```bash
# 推一条**真队列**给 GUI（只搜不播），并顺手推一条 GAME 状态
python3 gui/tools/fake_agent.py --path /tmp/t11fake.sock --echo --no-reply \
    --bilibili "luna say maybe"
# 另开一个终端：GUI 连上去（全屏 kiosk）
gui/build/agent_gui --socket /tmp/t11fake.sock
```

```bash
# 真 Agent（GAME 模式里对助手说"放个 luna say maybe 的视频"），然后：
assistant mode game
```
