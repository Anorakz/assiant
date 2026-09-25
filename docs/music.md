# 音乐：对话点歌 / 控制播放（声音从 PC 出，PC 上没有我们写的程序）

> Phase 7 T8 的产物。板端 `agent/net/netease_cli.py`（T8-2）通过 **ssh** 调 PC 上的第三方
> 命令行 `neteasecli`，由 **PC 本机的 mpv** 出声；歌单/标签/播放次数存在**板端本地**
> （`config/music_library.jsonl`，T8-3）。配置归属见 [config-sources.md](config-sources.md) §2。

## 1. 一句话

你说"放一首安静的日语歌"、"下一首"、"小声点" —— 板端把这条命令交给 **PC 上的
`neteasecli`** 执行（PC 出声），并把**真实的播放进度**读回来推给 GUI 音乐条。

## 2. 为什么是这套（不是"板端自己播"，也不是"写个 PC 程序"）

| 约束（你定的） | 结论 |
| --- | --- |
| 声音从 **PC** 出 | 播放器必须在 PC 上 → PC 必须有 `mpv` |
| PC 上**不能有我们写的程序** | 只能用**系统组件 + 第三方工具**：Windows 自带 **OpenSSH Server** + 第三方 `neteasecli`/`mpv` |
| 要能拿到**真实进度**（不是估算） | 只有"能读回输出"的通道才行 → **ssh**（Sunshine 的输入注入能跑命令但读不回，那样进度只能瞎猜） |

链路：

```
板端 Agent ──ssh:22──▶ PC: neteasecli --json … ──▶ 本机 mpv ──▶ PC 音箱
   │                                   （进度/时长/控制都从这里读）
   └── 本地库 config/music_library.jsonl（id + tags + 播放次数）
```

`neteasecli --json` 的输出**形状固定**：`{"success":bool,"data":…,"error":{code,message}|null}`。

## 3. PC 侧一次性配置（两件事，都不是"我们的程序"）

### 3.1 装 mpv（neteasecli 的播放后端）

```powershell
winget install --id mpv          # 或 choco install mpv
mpv --version                    # 要能在**新开的**终端里跑起来
```

⚠ 必须是 **PATH 上**能找到的 `mpv`：`ssh` 登录进来的会话不走你的交互式 shell，
找不到就会报 `Failed to start mpv: spawn mpv ENOENT`（板端的错误提示里会直说这一条）。

### 3.2 开 Windows 自带 OpenSSH Server + 放板端公钥

**管理员** PowerShell（一次性）：

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Start-Service sshd
Set-Service -Name sshd -StartupType Automatic
New-NetFirewallRule -Name sshd -DisplayName 'OpenSSH Server' -Enabled True `
  -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22

# 板端公钥（板端 /root/.ssh/id_ed25519.pub 的内容；rod 用户同理）
$key = 'ssh-ed25519 AAAAC3… 2129741519@qq.com'
Add-Content -Path C:\ProgramData\ssh\administrators_authorized_keys -Value $key
icacls C:\ProgramData\ssh\administrators_authorized_keys /inheritance:r `
  /grant "Administrators:F" /grant "SYSTEM:F"
```

> ⚠ 管理员账户用的是 **`administrators_authorized_keys`**（不是用户目录下的
> `~/.ssh/authorized_keys`），而且它的 ACL 必须是"只有 Administrators/SYSTEM"，
> 否则 sshd 会**静默忽略**它 —— 症状就是一直 `Permission denied (publickey)`。

**验证（在板端跑）**：

```bash
ssh -o BatchMode=yes Anorak@192.168.137.1 neteasecli --json player status
# 应当返回 {"success":true,"data":{...}}
```

## 4. 本地音乐库 `config/music_library.jsonl`（**不再依赖云歌单**）

一行一首歌：

```json
{"version":1,"id":"2747166493","name":"JANE DOE","artists":"米津玄師, 宇多田ヒカル",
 "album":"JANE DOE","duration_ms":236007,
 "tags":{"genre":["日语","流行"],"artist":["米津玄師"],"lang":["jp"],"mood":["energetic"]},
 "plays":3,"last_played":"2026-09-23T18:31:00","added_at":"2026-09-23T18:02:11",
 "source":"playlist:8503898111"}
```

| 字段 | 说明 |
| --- | --- |
| `id` | 网易云歌曲 id（**唯一键**；再导入同一个歌单是幂等的） |
| `tags` | `{轴: [值…]}` —— 见下 |
| `plays` | **真的听了 30 秒**才算一次（阈值 `music.count_after_s`） |
| `last_played` | 上次算作"听过"的时间（挑"很久没听"用得上） |
| `source` | 从哪儿来的（`playlist:8503898111` / `search:…` / `chat`） |

### 4.1 tag 从哪来（"元数据自动 + chat 补充"）

| 来源 | 轴 | 说明 |
| --- | --- | --- |
| **自动**（导入时） | `genre` | 云歌单自己的标签（"日语"/"流行"…） |
| | `artist` | 歌手名（逐个） |
| | `lang` | 从歌名/专辑名**猜**的语种（假名→`jp`、汉字→`zh`、看不出→**不写**） |
| | `era` | 专辑发行年代的十年段（`2020s`） |
| **chat 补充** | 任意 | 你说"这首很燃" → `mood=energetic`；想加什么轴都行（`scene`/`style`/…） |

> ⚠ **我们没有音频特征**（没做音乐 embedding，只有壁纸那边才有 SigLIP 向量）。
> 所以 tag **不可能**来自"听感相似"，只能来自上表这两路 —— 这一点在对话里也要如实说。

### 4.2 "下一首由 chat 决定" = chat 决定**队列内容**

队列是**环形队列**（`agent/core/music.py`）。T8-5b 起分工很明确：

| 谁 | 决定什么 |
| --- | --- |
| **chat**（工具 `next_music`） | 队列**内容**: `enqueue`（加一首/几首；`replace=true` 先清空）、`clear_queue`（清空） |
| **GUI**（四个按钮） | **transport**: 播放 / 暂停 / 上一首 / 下一首（走 IPC，不经过工具） |
| Runtime | 曲终**自动下一首**（环形：到尾回第一首）；队列空才停下 |

模型不用"先看清单再挑 id"了 —— 给条件就行，工具自己挑并把挑中的记录回给它：

```
next_music(action="enqueue")                                   # 库里听得最少的那首
next_music(action="enqueue", tag="mood=energetic", limit=3)    # 按标签挑 3 首
next_music(action="enqueue", keyword="JANE DOE")               # 去 PC 上搜，排第一条
next_music(action="clear_queue")                               # 换一批
```

`sort` 支持 `plays_asc`（默认，听得最少优先）/ `plays_desc` / `recent` / `oldest` /
`added` / `random`。`enqueue` 时如果 **PC 上什么都没在放**，会顺手从新排的那首起播
（否则"chat 安排了队列、用户却什么都听不到"）。

### 4.3 三个工具里的那个 `next_music`（T8-5b；音乐只有这一条工具入口）

| action | 干什么 | 落到哪 |
| --- | --- | --- |
| `enqueue` | 排进队列：`track_id` / `keyword`（PC 搜）/ `tag`（本地库按条件）/ 什么都不给（默认听得最少的）；`replace` 先清空 | `Runtime.music_enqueue` |
| `clear_queue` | 清空队列（**不停播放** —— 停/放是 GUI 的事） | `Runtime.music_queue_clear` |
| `list` | **只读**清单：id / 名字 / 歌手 / `plays` / tags；默认 `plays_asc` | `Runtime.music_candidates` |
| `search` | **只读**：去 PC 上搜（只回候选，**不放歌**） | `Runtime.music_search` |
| `status` | 现在在放什么 + 队列里有几首（队列**只给工具/日志看**, 不推 GUI） | `Runtime.music_state` + `music_queue_state` |
| `tag` | 给某首（或**当前这首**）加/删标签：`set_tag="mood=燃"`（多轴用 `;`） | `Runtime.music_tag` |
| `volume` | 调音量（`level` 0..100） | `Runtime.music_control("volume")` |

- ⚠ **没有 play/pause/next/prev/stop**（T8-5b 你定的）：transport 是 GUI 四个按钮的活。
  唯一留下的"控制"是 `volume` —— "小声点"是调节，不是换曲。
- ⚠ **`tag` 只能打"元数据自动 + 你说过的"那两路标签**（§4.1）：我们没有音频特征，
  工具说明里也写明这一条 —— 别说成"听起来像"。
- 标签写法与壁纸的 `match` **同一套语法**（`agent/core/label_spec.py`）：
  `mood=energetic` / `energetic`（所有轴）/ `mood=energetic/calm`（任一命中）/
  多轴用 `;`（`mood=燃; style=rock`）。
- `music.enabled: false` 时这个工具**整个不装**（`build()` 拿不到入口就返回 `None`）;
  状态权限表见 [`tests/test_tool_permissions.py::EXPECTED`](../tests/test_tool_permissions.py)
  （唯一真源）与 [architecture.md](architecture.md) §工具的权限表。

## 5. 板端配置（`config.yaml` 的 `music:` 段）

```yaml
music:
  enabled: true
  pc_host: 192.168.137.1
  pc_user: Anorak
  pc_port: 22
  binary: neteasecli
  timeout_s: 45
  library_file: config/music_library.jsonl
  count_after_s: 30
  poll_interval_s: 3      # 多久问一次 PC 的真实进度（走 ssh，别太密）
```

`enabled: false` = 音乐工具全部不装（与其它工具"缺依赖就跳过"同一条口径）。

### 5.1 GUI 那边怎么联动（T8-4）

**推送**（Agent → GUI，topic `music`）—— 每 `poll_interval_s` 一次，**变化才推**：

| 字段 | 含义 |
| --- | --- |
| `title` / `artist` / `album` | 当前曲目（空 = 没在放） |
| `position_s` / `duration_s` | 进度/时长（**真实值 + 本地外推**: 每次轮询重新对齐） |
| `playing` | 在放 / 暂停 / 停了 |
| `track_id` / `plays` / `tags` | 库里那条记录（GUI 现在只显示前两个 + 状态） |

**命令**（GUI → Agent，`payload` 必须是 `{}`）：`music_play_pause` / `music_next` /
`music_prev` / `music_stop`。T8-5b 起它们是**唯一的 transport**（工具里没有 play/pause）：
- `music_play_pause` 在"PC 上没在放"时**从环形队列当前位置起播**（队列空就回一条
  `llm` 说明"先让对话安排一首"）；
- `music_next` / `music_prev` 在环形队列里走 —— **到尾回第一首、到首回最后一首**；
- 队列是 chat 排的（`next_music`），**不推给 GUI**（T8-5b 你定的: 队列只在内部用）。

### 5.2 一次点歌要等多久（板端实测）

**T8-5（七个工具时）的两轮链路**：①"用户想放歌" → 工具调用"看清单" → ②带着清单再问一次
「放哪首」→ "放这一首" → ③带着结果回话。板端 0.6B 的实测速度：

| 项 | 实测（T8-5，7 个工具） |
| --- | --- |
| prompt 处理 | 17~23 token/s |
| **生成** | **~1.2 token/s**（一轮 200 token 的工具调用 = 168 s） |
| 第一轮 prompt | 1898 token（其中工具清单 1699） |
| 第二轮 prompt | 2131 token（> 当时的 ctx 2048 → 400） |

T8-5b 合并成三个工具后：**一次 `next_music(action="enqueue")` 就能放歌**（工具自己按条件
挑），链路从三轮模型降到两轮；工具清单也从 ~1700 token 掉到 ~700（合并后的实测数字见
[llm.md](llm.md) §5.1）。

两件事因此必须留够余量（都在 `config.yaml` / `config.example.yaml`）：

- `llm.ctx_size` **4096**：2048 装不下"带工具结果的那一轮"（T8-5 实测 2131），llama-server
  会回 400 `exceed_context_size_error`，用户看到的是"听不懂这句话"（见 [llm.md](llm.md) §5.1）。
- `llm.timeout_s`：默认 90 对"两轮往返"偏紧（一轮生成就可能上百秒）。板端把这一轮跑完需要
  **数分钟**，这是 0.6B + RK3568 的硬现实，不是 bug；T8-5b 同时在查"工具路径上的隐藏思考"
  （`reasoning_content`，见 [llm.md](llm.md) §5.2）。

## 6. 失败都要"能照做"（板端排障四类）

| 症状 | 板端会说什么 |
| --- | --- |
| 连不上 PC | "连不上 PC（ssh: …Connection refused）—— 检查 PC 是否开机、Windows OpenSSH Server 是否启动、板端与 PC 是否在同一网段" |
| `Permission denied (publickey)` | "板端连 PC 的 ssh 被拒（认证失败）—— 把板端公钥放进 PC 的 `administrators_authorized_keys`"（并把公钥内容带上） |
| `AUTH_ERROR` / rc=2 | "网易云登录态失效 —— 在 **PC 上**重跑 `neteasecli auth login`" |
| `spawn mpv ENOENT` / 起不来 | "PC 上起不了 mpv（neteasecli 报 …）—— 确认 PC 装了 mpv 且在 PATH 里" |

### 6.1 四类失败**板端实跑**的原文（T8-7，2026-09-25）

四条都在板端真跑过（真 ssh → 真 neteasecli → 真 mpv），话术就是用户实际看到的那句：

| 场景（怎么造出来的） | 模型/工具回给用户的话 |
| --- | --- |
| **VIP/无版权**（点《晴天》186016；能搜到、拿不到流） | `这首在 PC 上要不到播放地址（VIP/无版权/已下架）: Track unavailable (no copyright or VIP required)` |
| **cookie 过期**（把 PC 上 `~/.config/neteasecli/profiles/default/session.json` 移走） | `网易云登录态失效 —— 在 **PC 上**重跑 neteasecli auth login（neteasecli 把"没登录"报成了"要不到播放地址（VIP/无版权）", 别当成会员问题）` |
| **PC 连不上**（板端 `iptables -I OUTPUT -d <PC> -p tcp --dport 22 -j REJECT`） | `连不上 PC（ssh: connect to host 192.168.137.1 port 22: Connection refused）—— 检查 PC 是否开机、Windows OpenSSH Server 是否启动、板端与 PC 是否在同一网段` |
| **mpv 缺失**（PATH 前面插一个"立刻退出"的假 mpv.exe） | `PC 上没有开始播放 —— 播放地址是有的（所以不是版权问题）, 常见原因: ①PC 上**没人登录桌面**（计划任务只在用户登录时运行）②PC 上 mpv 不在 PATH。可以先在 PC 上手动跑一次 neteasecli track play <id> 看它报什么` |

⚠ 这四条里有**三处是我们自己"报喜不报忧"**，都是这一轮跑出来的、当场修掉的：

1. **VIP 那首被报成成功**。判据只有"PC 上有没有出声"（`duration > 0`），而 PC 上**本来
   就在放别的歌** —— 实测拿到 `{"ok": true, "started": true}`，屏幕上还是上一首。
   现在 `NeteaseCli.play()` **先要一次播放地址**（`track url`）：要不到就直接失败，既不去起播、
   也不动用户正在听的那首。
2. **cookie 过期被报成版权问题**。没登录时 neteasecli 报的就是
   `Track unavailable (no copyright or VIP required)`（跟 VIP 那首**同一句话**）。
   现在只在**失败路径**上多问一句 `auth check`：没登录就给"去 PC 上重登"那句，
   是登录着的才说"要不到播放地址（VIP/无版权/已下架）"。
3. **`tracks[].id` 是 `null`**（工具回话里本来该带真 id）—— `enqueue()` 返回的键是
   `track_id`，Runtime 那边在读 `id`。

另外顺手把两个"说话指错方向"的地方改了：`TRACK_ERROR` 的退出码是 **3**，按退出码翻会变成
"PC 那边网络请求失败"（现在按 `error_code` 先判）；ssh 失败时信封里的 message 就是 stderr
原文，直接拼会说**两遍**（现在同一句只说一次）。

**起播前还会先停下来放着的**（`play()` 里）：mpv 的 IPC 是固定管道名，旧 mpv 还在时新的
一首根本起不来 —— 不停的话"有没有出声"就分不清是新歌还是旧歌。走到 `play()` 一定是
"决定要起播"（enqueue 只在 PC 上没在放时才起、曲终才换下一首），所以不会打断用户正在听的。

**排歌之前先问一次 PC 的真值**（`Runtime.music_enqueue` → `music.refresh()`）：
`snapshot()` 是纯本地的（不碰 PC），刚重启的 Agent 会以为"PC 上没在放"，
于是把用户正在听的那首停掉从头起播（实测踩到）。

## 7. 怎么验

```bash
# 纯逻辑（开发机与板端同一份）
python tests/test_netease_cli.py     # SSH 封装: 拼命令行 / 解信封 / 四类错误翻译 / 起播前后的两处规矩
python tests/test_music_library.py   # 本地库: 读写/合并/打标/计数/挑选
python tests/test_music_player.py    # 播放内核: 30 秒计一次 / 曲终停 / 状态推送 / 不打断在放的
python tests/test_music_tools.py     # 四个工具: schema / 缺依赖跳过 / 只做该做的事
```

板端实跑见 T8-1/T8-7 的验收记录（`todo.md`）。
