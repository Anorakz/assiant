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

### 4.2 "下一首由 chat 决定"

不做自动轮转。LLM 的流程是：**看清单 → 自己挑 → 播**：

```
list_music_library(tag="energetic", sort="plays_asc", limit=10)   # 听得最少的先
play_track(id="2747166493")
```

`sort` 支持 `plays_asc`（默认，听得最少优先）/ `plays_desc` / `recent` / `oldest` /
`added` / `random`。曲终**停下**（推一条 `music{playing:false}`），等 chat 再决定。

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
`music_prev` / `music_stop`。⚠ 这四个**不决定放什么** —— `next/prev` 只在"chat 上次挑出来的
候选顺序"里走；失败（音乐没开 / PC 上没在放 / 还没有队列）会回一条 `llm` 说明。

## 6. 失败都要"能照做"（板端排障四类）

| 症状 | 板端会说什么 |
| --- | --- |
| 连不上 PC | "PC 没开机 / OpenSSH Server 没启动 / 不在同一网段" |
| `Permission denied (publickey)` | "把板端公钥放进 PC 的 `administrators_authorized_keys`"（并把公钥内容带上） |
| `AUTH_ERROR` / rc=2 | "网易云登录态失效 —— 在 **PC 上**重跑 `neteasecli auth login`" |
| `spawn mpv ENOENT` | "PC 上 mpv 没装 / 不在 PATH" |

## 7. 怎么验

```bash
# 纯逻辑（开发机与板端同一份）
python tests/test_netease_cli.py     # SSH 封装: 拼命令行 / 解信封 / 四类错误翻译
python tests/test_music_library.py   # 本地库: 读写/合并/打标/计数/挑选
```

板端实跑见 T8-1/T8-7 的验收记录（`todo.md`）。
