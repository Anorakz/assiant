# 壁纸标签化：词表、数据文件、IP 检索

> Phase 7 T7 的产物。打标签用 `assistant tag`（板端），检索/挑图由 Agent 的工具用
> （见 [architecture.md](architecture.md) §4.1 的权限表与工具层）。
> 配置归属见 [config-sources.md](config-sources.md) §2.2/§2.3。

## 1. 一句话

给壁纸目录里每张图打上**三轴标签**（场景 / 色调 / 氛围），外加一个
**开放的 IP 检索**（"这张像不像某个作品"）；标签和图像向量落在
`config/wall_data.jsonl`，挑图时**不用再过 NPU**。

## 2. 词表（`agent/vision/tag_vocab.py`）

| 轴 | 中文 | 候选数 | 例子 |
| --- | --- | --- | --- |
| `scene` | 场景 | 16 | `landscape` 风景、`city` 城市、`anime` 动漫、`abstract` 抽象、`minimal` 极简… |
| `tone` | 色调 | 8 | `dark` 暗色、`bright` 明亮、`monochrome` 黑白灰、`warm` 暖色… |
| `mood` | 氛围 | 8 | `calm` 安静、`energetic` 活力、`mysterious` 神秘、`cozy` 温馨… |

三条规矩：

- **标签是英文裸串**（没有 `a photo of …` 模板）。SigLIP-base 是英文图文对训出来的，
  中文标签对它没有意义 —— 中文只用于显示（`label_zh`）。
- **词表就是上限**：零样本分类选不出词表里没有的东西。所以词表要覆盖库里真会有的类别
  （`abstract` / `anime` 就是为此留的兜底类）。词表改了 → `vocab_sha8` 变 →
  `assistant tag` 会判定这些图要重打。
- **配置只能追加**：`wallpaper.tagging.vocab: {scene: [cyberpunk]}` 会加在默认词表后面。
  想**删**默认标签就改 `tag_vocab.py`，再重跑 `assistant tag`。

## 3. IP：不是分类，是**锚点检索**

SigLIP 不认识"Kara no Kyoukai"这种具体作品名 —— 拿它去编码文本塔基本是噪声。
所以 IP 走**少样本锚点**：

```yaml
wallpaper:
  tagging:
    ip_presets:
      EVA:  {query: Evangelion, anchors: [Rei_1.png, wallhaven-85j9qo_2560x1080.png]}
      Nier: {query: "NieR: Automata", anchors: [wallhaven-kxpyy1_2560x1440.png, wallhaven-3l78rd_2560x1440.png]}
```

- **锚点检索（主）**：把锚点图的图像向量求平均当该 IP 的**原型**，与全库算余弦 →
  排序。不要求模型认识这个作品，**新放的同 IP 图会被自动找到**（这才是"检索"）。
- **文本查询（兜底）**：没锚点的名字退化到文本塔，返回时明确标注"无锚点，可能不准"。
- 锚点写**文件名**（相对 `wallpaper.dir`）；换图/改名后要跟着改配置。
- 检索**不需要重打标签**：向量已经存在数据文件里，纯 CPU 点积。

## 4. 数据文件 `config/wall_data.jsonl`

**第一行是词表记录**（标签向量缓存），**第二行起一行一张图**：

```json
{"kind":"vocab","version":1,"model_sha8":"a50233e0","vocab_sha8":"b780f2b5","dim":768,
 "axes":{"scene":["landscape","city",…],"tone":[…],"mood":[…]},
 "embeds":{"scene":["<base64 float32 × 768>",…],"tone":[…],"mood":[…]}}
{"version":1,"path":"/home/kickpi/wallpapers/01_….png","w":1280,"h":800,"bytes":4296,
 "sha256":"…","tagged_at":"2026-09-22T22:53:01","ms":1420,
 "model_sha8":"a50233e0","vocab_sha8":"b780f2b5",
 "tags":{"scene":[["landscape",0.21],["city",0.08]],"tone":[["dark",0.15]],"mood":[["calm",0.11]]},
 "embedding":"<base64 float16 × 768>","used":3,"last_used":"2026-09-24T21:07:12"}
```

| 字段 | 为什么留着 |
| --- | --- |
| `sha256` | 图换了（重新导出、被替换）→ 只有那一行要重打 |
| `model_sha8` / `vocab_sha8` | 换模型或改词表 → 所有标签作废，机械可查 |
| `tags` | 每轴 top-k（默认 3）+ **余弦分数** |
| `embedding` | L2 归一化的图像向量：IP 检索/相似图召回**不用再过 NPU**（一张 ≈ 2KB） |
| `ms` | 每张耗时，性能回归有据可查 |
| `used` / `last_used` | **运行期**字段（T8-6）：这张当壁纸显示过几次 / 上次是什么时候 —— "挑一张用得最少的"靠它（见 §6.1） |

**为什么第一行是标签向量**：32 条标签每条都要过一次文本塔，板端实测 **61 s**
（占一次全量 206 s 的 30%），而这 32 个向量跟图片目录毫无关系。
存进数据文件后，**同模型同词表**的第二次运行直接复用 —— 增量打 1 张新图从 ~70 s 降到 ~6 s。
指纹对不上（`model_sha8` / `vocab_sha8` / 某轴的标签列表逐条不等）就**不用缓存**，
重编一遍再覆盖第一行；缓存解不开（base64 坏了）也当没有缓存。

- 词表向量用 **float32**、图像向量用 **float16**（故意的）：float32 往返**逐位精确**，
  所以"复用缓存"和"现场重编码"算出来的分数**完全相同**，不会出现"看着一样、数字差 1e-3"。
  实测（板端）：拿缓存解出的 32×768 = **24576 个分量**与现场重编码（60.6 s）逐个比，
  **最大绝对差 0、float32 逐位不同的分量 0** —— 文本塔是确定性的，缓存值就是原值。
- 代价是文件大了一截（词表 32×768×4 ≈ 98 KB → base64 约 131 KB），换来 61 s。

**边界（重要）**：

- 它是**派生数据**，不是配置真源 —— `config/config.yaml` 才是人写的真源。
  手改它没有意义（下次打标签会覆盖），所以它**进了 `.gitignore`**（板端本地数据）。
- **唯一写入者**是 `assistant tag --apply`（`agent/vision/wall_data.py`）；Agent **只读**。
  写之前会在旁边留一份 `wall_data.jsonl.bak`（原子写，走 `agent/config.py::write_text_atomic`）。
  ⚠ T8-6 起有**两个写入时机**，但仍然是同一个函数（`wall_data.write_records()`）：
  打标签时整表重写、以及**换壁纸时给那一张 +1**（`bump_usage_in_file()` 读整表 → 改一行 →
  原子重写）。所以文件里不会出现"两个写者各写一半"。
- `tests/test_config_source_guard.py` 的写入者白名单里因此多了第三个文件 —— 那是**显式**决定，
  见 `docs/config-sources.md` §3.1 的口径。

## 5. 怎么打标签（`assistant tag`）

```bash
assistant tag                       # 默认 dry-run：只说"哪些要打、为什么"
assistant tag --apply               # 真打（逐张写盘，边打边落盘）
assistant tag --apply --limit 10    # 分批（40 张跑一两分钟，想分开跑就加它）
assistant tag --apply --force       # 全部重打（不看指纹）
assistant tag --apply --prune       # 顺手清掉"图已经不在了"的行
assistant tag --dir /path --data-file /path --top-k 3   # 覆盖配置
```

- **增量判据**：图变了（`sha256`）/ 模型变了（`model_sha8`）/ 词表变了（`vocab_sha8`）/
  格式版本变了 → 只有这些行重打。**第二次跑应当说"都已是最新"**。
- **逐张写盘**：跑在中途崩了，已经打好的不会丢。
- **在哪儿跑**：必须在**板端**（要 NPU + numpy + cv2 + tokenizers）。
  开发机上 `--apply` 会明确说缺什么（dry-run 不需要这些，哪儿都能跑）。
- **Agent 不会自动打标签**（`wallpaper.tagging.auto` 默认 false）—— 它可能会占 NPU 一两分钟。

## 6. 怎么挑图（在**对话里**说）

⚠ **T7-3 起换壁纸只有对话这一条路**：主区那个「下一张」按钮与同名的 `next_wallpaper`
IPC 命令都删掉了（理由见 `agent/core/wallpaper.py` 模块头）。挑图**不过 NPU** ——
用数据文件里已经存好的向量做纯 Python 点积（`agent/vision/tag_index.py`），毫秒级。

对 Agent 说一句人话，模型自己决定调哪个工具：

| 你说 | 模型会调 | 干什么 |
| --- | --- | --- |
| "换一张安静的深色风景" | `next_wallpaper(action="pick", match="scene=landscape")` | 挑最像的几张里翻 |
| "有哪些风格？" | `next_wallpaper(action="tags")` | 先看清单（每轴各标签几张） |
| "换一张像 EVA 的" | `next_wallpaper(action="pick", match="ip=EVA")` | 锚点原型检索（纯 CPU） |
| "换一张壁纸" | `next_wallpaper(action="next")` | **往后走一格**（三格窗口: next 变当前, 再按画像补一个新的 next） |
| "上一张" | `next_wallpaper(action="prev")` | **往回走一格**（`prev` 只留 1 张真实历史） |
| "挑一张我用得最少的" | `next_wallpaper(action="least")` | 按使用次数挑：最少的在前（T8-6） |
| "下一张想要 EVA 的"（**先别换**） | `next_wallpaper(action="stage", match="ip=EVA")` | 只把那张放进"下一个"，**不切屏**（T10-3） |

⚠ **`action="tags"` 的 `ip_query` 只填作品名**（例如 `EVA`）——板端实测模型会把
一句问句（"这个作品最像哪几张"）填进去，工具如实报错后它又把"可用的 IP 名"当成壁纸标签
答给用户。所以 schema 里写死"只填名字，不要填问句或句子"（T7-4），`maxLength` 收到 32。
另外：模型**谎报成功**时也有兜底 —— 工具失败会被追加进最终正文
（`⚠ 换壁纸没有成功：…`），见 [`llm.md` §3](llm.md)。

**`match` 的写法**（写错**如实报错**，不会随便换一张糊弄过去）：

| 写法 | 含义 |
| --- | --- |
| `scene=anime` | 某个轴上的某条标签（轴名必须是真轴：`scene`/`tone`/`mood`） |
| `anime` | 只写标签名 → 在所有轴里找同名标签（命中多个轴时按分数合并且去重） |
| `ip=EVA` | 配置里那个 IP 的**锚点原型**最像的图（名字大小写不敏感） |
| `scene=anime/landscape` | **多条标签 = 任一命中**：每张图取它在这些标签里的最高分（分隔符 `/ , ; 、 ，` 与空白都认） |

⚠ 多标签那条是 T7-4 按板端实测加的：0.6B 会把一串标签用斜杠拼成一条
（`scene=space/technology/fantasy/anime`）。老实报错也算"如实"，但用户的意思显然是
"这几个里随便挑一张像的"，所以按**任一命中**算，并在结果里记下**是哪条标签**打的分
（`match.why`）；其中有认不出的标签会**照用认得的、并如实列出忽略掉的那些**；
**全都认不出**才报错并列词表。

- **候选按相关度排序**：`match` 一次调用挑的是**第 1 名**（最像的那张），
  `match.rank` 报的就是它的名次；**同一个条件再来一次**才往下翻一名（"再换一张同类的"）,
  换了条件又回到第 1 名。`total` 报的是候选数，不是目录里的张数。
  ⚠ T7-4 修过一处：以前"当前那张恰好在候选里"时会从**它在候选里的名次**往后走 ——
  实测说"换一张动漫的"却挑回第 29 名（当前那张排第 28）。现在只有"上一张就是这个条件挑的"
  才从它往后翻（`WallpaperDeck.step(anchor=…)`）。
  ⚠ T8-6 又修了同一处的**另一半**（写 T8-6 的测试时才发现的）：不给 `match` 的普通
  "换一张"也一直在传 `anchor=None`，那是"假装还没选过"的意思 —— 于是连叫 4 次
  `next_wallpaper(1)` 每次都挑回 `01_a.png`（**"下一张"其实没往后走**）。
  现在不给 `match`/`sort` 时不传 `anchor`，按游标一格格走；
  `tests/test_wallpaper.py::TestRuntimeWallpaperUsage.test_a_plain_switch_walks_forward` 钉住它。
- **分数是余弦**（不是概率）：排序可信，绝对值不要当置信度（阈值还没标定，见 §8）。
- **当前词表写进了工具的说明**（T7-4）：`next_wallpaper` 的 description 末尾带着
  `可用标签: scene=landscape/city/…；tone=dark/…；mood=calm/…`（按 `wallpaper.tagging.vocab`
  的追加项一起算；超过 320 字符就截断并指向 `action="tags"`）。
  原因：板端实测 0.6B 会**编造标签**（把 `tone` 的 `dark` 说成 `scene=darkness`），
  第一次调用就撞错、白跑一轮。词表被加得很长时以 `action="tags"` 的结果为准。
- **按标签算分不受 top-k 截断**：数据文件第一行存着全部标签向量，所以
  "`scene=anime` 第 4 名"也能算出来（图片记录里只存了 top-3）。
- **失败都有一句能照做的话**：不认识的轴会列出真轴、不认识的标签会列词表、
  没配置锚点的 IP 会说去 `ip_presets` 加、锚点没打过标签会说"先跑 `assistant tag`"、
  整个库还没打标签会说去跑 `assistant tag --apply`。

### 6.1 使用次数（T8-6）

"挑一张我最少看到的"要有依据 —— 依据就是**这张图被换上去过几次**，记在数据文件的
`used` / `last_used` 里（§4）。计数**不需要额外的文件**，也不需要 NPU。

**什么时候 +1**（判据只有一条：屏幕上**换成了另一张**）：

| 动作 | 算不算 |
| --- | --- |
| 对话里换了一张（`action=next/prev/pick`，真的换了另一张） | **算**（+1） |
| 没有 GUI 连着（`on_wallpaper is None`）时换图 | **算** —— 游标确实动了，"换成了"这件事发生了 |
| 同一张再推一次（`action=repeat` / `step=0`） | 不算 |
| GUI 断开重连，补推**当前**这张（`push_current_wallpaper`） | 不算 |
| 开机/重启后 GUI 第一次连上（还没有"当前"，补推第一张当初始画面） | 不算（`count=False`：那是"同步显示"，不是用户换的） |
| 这张图不在数据文件里（没打过标签 / 是新图） | 不计、不报错（换图照常成功，`used` 回 0） |
| 计数写盘失败（磁盘满 / 文件被改成目录…） | 只记 warning —— **统计坏了不该让换图失败** |

**怎么挑**：`next_wallpaper(action="least")` 挑**用得最少**的一张、`action="most"` 挑用得最多的
（这两个不用给 `match`）；要"在最像的那批里挑用得最少的"就用
`action="pick", match="scene=anime", sort="used_asc"`（`sort` 只有挑图/翻页才有意义）。
⚠ 排序后会**跳过屏幕上当前这张**再从第 1 名拿 —— 连说两次"再挑一张用得最少的"是一张张往少走，
不会挑回同一张、也不会跳回用得多的一头（板端实测过这两种错法，见下表）。
`test_asking_twice_never_hands_back_the_same_one` 钉住它。

**板端实测的两种误用（0.6B，T8-6 验收时抓到的）**：

| 模型实际写的 | 结果 | 现在的处理 |
| --- | --- | --- |
| `action="pick", match="scene=landscape"`（自己编了个标签，**完全不提"用得最少"**） | 换了一张风景图 —— 能用，但不是用户要的 | 把"按用量挑"做成**单字 action**（`least`/`most`）写进枚举与说明，别再指望它去设 `sort=` |
| `action="tags", sort="used_asc"`（"看标签"是**只读**的，却被当成了换图） | 修前：安静回一份标签清单，模型接着说"已找到使用次数最少的壁纸，您可以在界面中看到它" —— **谎报**（屏幕没换） | 如实报错 + 指出该用 `action="least"`（下一轮它有机会改；不改也有 `tell_user` 兜底出现在正文） |
| 连着说两次"再挑一张用得最少的" | 修前（从当前这张在排序里的名次往后翻）：第二次跳到 **index=39/40** —— 跑到**用得多**的那一档去了 | 排序后先**跳过屏幕上这张**再从第 1 名拿（"在屏幕上没有的那些里挑最少的"） |

> 教训（和 T8-5c 那次同源）：**只加参数不给 action，小模型用不起来**；
> 而**参数与 action 自相矛盾**时既不能装没看见（会谎报）也不能替它改（读当写），
> 只能**如实报错 + 告诉它正确的写法**。

**怎么看**：

- 对话里问"哪张壁纸我用得最少 / 我用得最多的壁纸是哪张 / 壁纸都用过几次" —— 这是**只读问句**，
  按 T8-5c 的分工**直连回答（0 次模型推理）**，报的是 `usage` 块里的真实数字
  （共换过几次、几张没换到过、最少/最多的三张）。一次都没换过时只说实话
  （"还一次都没换过"），**不会**报"用得最多的是谁"（那只是文件名顺序，板端第一次问就撞上）。
- 问"有哪些壁纸标签"（`action="tags"`）时，回话里也带同一份 `usage` 摘要。

**已知边界（如实写）**：

- 计数是**软信号**，只统计"Agent 自己换上去的那几次"：断电重启、崩溃重启期间屏幕上是哪张、
  以及**用别的方式**（桌面环境自己改壁纸）换过什么，它都不知道 —— 板端**没有 RTC 也没有持久游标**。
- 数据文件是**派生数据**（进了 `.gitignore`）。删了它 / 换台机器重打标签，计数就从 0 开始。
- 重打标签**不会清零**：`assistant tag` 会用 `wall_data.with_usage()` 把 `used`/`last_used`
  带到新记录上（`tests/test_cli.py` 与 `test_wall_data.py::TestUsage` 钉住）。
- **`action="most"` 目前"到不了"（板端实测 2 次）**：说"换一张我老看的那张壁纸"它调
  `action="next"`；说"挑一张我看得最多的壁纸"它调 `action="least"`（**方向反了**）。
  `least` 相反很稳（3/3 都调对）。机制本身没问题 —— 板端直接调
  `next_wallpaper(1, sort="used_desc")` 验过（挑到用得最多的那张，再调一次会跳过屏幕上那张）；
  而且**问**"我看得最多的是哪张"走只读直连，那个是准的（见上）。
  结论：这条是 0.6B 的能力边界，不是接线问题 —— 记在这，别再往描述里堆字（预算只剩 78 字符）。

### 6.2 三格窗口与"下一个"是**预挑**出来的（T10-3）

T10-3 之前每次换图都是"当场挑一张"，屏幕上是哪张、下一张会是谁**没有记账**。
现在 `WallpaperDeck` 维护一个**三格窗口**（`agent/core/wallpaper.py`）：

```
prev  ──advance()──▶  current  ──advance()──▶  prev
                        next  ──────────────────┘
```

| 槽 | 含义 | 有几张 |
| --- | --- | --- |
| `prev` | **真实历史**（上一次真的显示过的那张） | 只留 **1** 张（T10-2 用户决定） |
| `current` | 屏幕上现在这张 | 1 |
| `next` | **已经挑好的下一张**（还没显示） | 1 |

三个槽存的都是**路径**，而且 `next` 是**提前挑好的**：屏幕上一切换，Agent 就紧接着
按当前画像再补一个新的 `next`（`Runtime._refill_next()`）。好处是"下一张"是可预告、
可替换、可被画像影响的 —— 而不是"等你说换的时候我再临时掷一次骰子"。

**四种动作怎么动窗口**（`WallpaperDeck.step()` / `advance()` / `back()`）：

| action | prev | current | next |
| --- | --- | --- | --- |
| `next`（`step>0`，普通"换一张"） | ← 旧的 current | ← 旧的 next（没有就现挑） | 清空 → 补一个新挑的 |
| `pick` / `least` / `most`（带 `match`/`sort`） | ← 旧的 current | ← 新挑的那张 | 清空 → 补一个 |
| `prev`（`step<0`，"上一张"） | ← 交换，`prev` 空 → 按文件名往前翻一张并把它记成 `prev`（A5） | | 不动 |
| `stage`（只预挑） | 不动 | **不动** | ← 换成你点名的那张 |
| `repeat`（同条件再来一张） | 不动 | **不动**（还是这张） | 不动 |
| `pick` 再来一次（同 `match`） | ← 旧的 current | ← 候选里往后翻一名 | 清空 → 补一个 |

窗口**不落盘**（和游标一样：板端没有 RTC 也没有持久游标），重启后从"没有 prev/next"开始
—— 和 T8-6 的计数规矩不冲突：只有**"换成了另一张"**才 +1，`stage`/`repeat`/补推当前/开机首屏都不算。

**`next` 什么时候会被换掉（T10-2 用户决定的三种情况）**：

1. 用户**直接点名**要换下一个（`action="stage"`，或带 `match` 的 `pick`）；
2. **用户画像更新**了 —— 画像是"下一个"的排序依据之一，画像变了就该重挑
   （`_build_profile_task()` 里在画像落盘后调 `_refill_next()`，见 [profile.md](profile.md) §8）；
3. 用户**要求挑某一个**（`match`/`sort` 命中了具体一张）。

> 用户原话（T10-2 验收时）：`1、pick = 重新挑一个替换掉当前的下一个，然后队列接着走，
> 然后再重新塞入下一个`；`3、next 更新三种情况：①用户要直接更换下一个 ②用户画像更新
> ③用户要求选某一个`。

**画像怎么参与挑图**（T10-2 的 `Runtime._choose_next()`，只在**没有** `match`/`sort` 时生效）：

```
match / sort 命中？  ── 是 ──▶ 按条件挑（老行为，画像不插手）
        │ 否
        ▼
   画像能用吗？ ── 能 ──▶ _rank_next_by_profile()：按画像排序取第 1 名
        │ 不能（画像太薄 / 关掉了 / 没有候选）
        ▼
   按文件名顺序翻一格（**故意不用"用得最少"**）
```

- 画像排序的权重在 [`user_profile.py`](profile.md)：壁纸 `ip 0.7 / mood 0.2 / fresh 0.1`；
  `ip` 那一项用**排序名次当相似度代理**（`1 - rank/N`，池子按 IP 缓存），
  因为"像不像"本来就是个相对量（§3 说过分数是余弦，不可比）。
- **回退是文件名顺序，不是"用得最少"**：这是刻意的 —— 画像关掉/太薄时行为要**可预测**，
  老用户能料到"换一张"就是往后走一格。之前拿"用得最少"当回退，把 T8-6 的
  `test_a_plain_switch_walks_forward` 撞红了（连叫 4 次每次都挑回 `01_a.png`）。
- 挑的时候**永远跳过窗口里的 `prev`/`current`**（重新补 `next` 时还要跳过现在这个 `next`）——
  不然"换一张"会当场换回刚才那张。
- **画像不是工具**（T9 决定）：它没有自己的 tool schema，是 **Agent 结构的一部分**，
  在 `_choose_next()` 里被**读**，模型既看不见也调不动它。

**板端实测**（T10-6, 真目录 40 张 + 真索引 + 真画像, `tests/board/t10_accept.py`）:

```
起始      prev=None                  current=None                        next=None
① next    prev=None                  current=wallhaven-858vpj_1200x1920  next=wallhaven-y85mqx_2560x1440
② next    prev=wallhaven-858vpj…    current=wallhaven-y85mqx_2560x1440  next=wallhaven-w8j79x_1280x720
③ prev    prev=None                  current=wallhaven-858vpj_1200x1920  next=wallhaven-y85mqx_2560x1440
④ stage   prev=None                  current=wallhaven-858vpj_1200x1920  next=Rei_1.png   ← 不切屏、不计次数
⑤ 推进    prev=wallhaven-858vpj…    current=Rei_1.png                   next=wallhaven-w8j79x_1280x720
⑥ pick    prev=Rei_1.png             current=wallhaven-85j9qo_2560x1080  next=wallhaven-w8j79x_1280x720
```

- 画像挑图那条的原文：`像 SNF（0.70）；还没看过`（`basis=profile`）；
  把画像文件挪走再挑 -> `basis=order`（`回退: 按文件名`）。
- `pick "ip=EVA"` 那次的 `match` 详情：`rank=2`、`candidates=40`、
  `EVA：用 2 张锚点图的平均向量检索，全库 40 张按相似度排序`。

## 7. 已知边界（如实写下来）

- **分数是余弦，不是概率**：`logit_scale/bias` 没随模型导出，拿不到标定过的 sigmoid。
  所以**每轴的阈值要实测标定**，而且不同轴的分数不可比（不要做"总分"）。
- **词表上限**：零样本分类选不出词表外的东西；`abstract` 这类图最容易"勉强归到某个类"。
- **IP 检索的"准"取决于锚点**：一个 IP 只有 1 张锚点图时，它只能"像这张"；
  多给几张（同 IP 不同构图）会明显更稳。
- **不对 UI 截图负责**：SigLIP 对整屏截图会坍缩（实测见 `llm/multimodal_report.md` §7.2）——
  这是壁纸（自然图像）场景之外的用法，别拿它当借口。
- **`ip_query` 搬运只覆盖 `next/prev/repeat`**（写 T10-6 文档时对出来的，如实记下）：
  模型把条件填错位置（填进 `ip_query`）时，`next` 那条路会被**搬**到 `match` 上；
  但 `pick`/`stage` 不在 `_STEPS` 里，**搬不了** —— `pick` 会如实报
  "挑图要说明按什么挑"（诚实，用户能改），`stage` 会当成"没给条件"**按画像挑一张**放进
  `next`（不切屏、不计数，但**不是你要的那个条件**）。要修就是把那条搬运规则从
  `_STEPS` 放宽到"除 `tags` 以外"，属于改工具行为，得单独做（T10-6 只记不改）。
- **预处理照官方口径**：`cv2` 读 → BGR→RGB → **压扁**到 256×256 → 原始 0-255。
  不要"改成等比 letterbox"：那会跑到模型训练分布之外（`preprocessor_config.json` 就是
  256×256 压扁，归一化已烧进 rknn）。

## 8. 实测数字（板端，2026-09-22，人工真值 12 张）

这些数字**照实记录**（包括不好看的那些），跑法见 §9。⚠ 真值是我按 4×3 联系表
（`logs/eval_sheet.png`）逐张标的，**待你复核**——改真值就等于改基线。

| 指标 | 结果 |
| --- | --- |
| 打标签耗时 | 40 张全量 **205.7 s**（中位 **3.56 s/张**，含 4K PNG 解码）；其中**词表编码 61.2 s**、模型加载 2.6 s |
| 词表缓存 | 首次写头 ~**67 s**（含编码 61.3 s）；之后同模型同词表的运行 **复用缓存**（增量 1 张图 **5.8 s**、2 张图 9.7 s）；两者 24576 个分量**逐位相同** |
| `scene` 命中 | top-1 **7/12 (58%)**，top-2 10/12 (83%) |
| `tone` 命中 | top-1 **5/12 (42%)**，top-2 7/12 (58%) |
| `mood` 命中 | top-1 **5/12 (42%)**，top-2 7/12 (58%) |
| 预处理 A/B | 官方**压扁 7/5/5** vs 中心裁切 7/4/4 → **保持官方口径**，不加开关 |
| 数据文件体积 | 40 张 = **234 KB**（第一行词表 ≈ 131 KB + 一张图 ≈ 2.6 KB，其中向量占约 2 KB） |
| 两两余弦基线 | 原始向量两两余弦：均值 **0.560**、中位 0.557、最大 0.943（库里几乎全是动画插画） |

**`tone` / `mood` 为什么这么弱**（如实分析，别赖模型）：

- 我的真值是**单标签**，而"亮/暖/多彩/柔和"这些词在真实图上经常同时成立 ——
  模型给 `colorful`/`pastel`，我标 `bright`，两种读法都说得通。
- 模型对**动画人物**有系统性偏好：`mood` 里 `cute` 被过度预测（Rei 线稿、和服人物都被判 cute）。
- `scene` 明显更好（58%/83%）：类别之间差异大（city vs landscape vs abstract），
  这正是零样本分类擅长的；而 `tone`/`mood` 是连续量，硬切离散标签本身就不合适。

**IP 锚点检索**（纯 CPU，用已存向量）：

| 项 | 结果 |
| --- | --- |
| LOO 自检索（拿掉一张锚点，看能否被剩余锚点的原型找回） | EVA **3/40、5/40**；GitS **3/40、2/40**；Nier **15/40、10/40** |
| 跨 IP 检索前 5 名 | own=1~2、foreign=0~1（precision 0.2–0.4）：**自己的图排在最前**，后面接的是"没有 IP 真值"的同类动画图 |
| 去均值 / PCA 白化能改善吗 | 基本不能（raw 3,5,15,10,3,2 → 去均值 5,3,7,5,2,2）→ **保持原始余弦**，不引入额外变换 |

结论：**IP 检索可用但不稳** —— EVA/GitS 这类"风格鲜明的图"能找回自己人；
Nier 那两张锚点本身差异大（黑底徽记 vs 黑白人影），均值原型被冲淡。
**每个 IP 给 3–5 张锚点会明显更稳**（现在多数只有 1–2 张）。

## 9. 怎么验

```bash
python tests/test_wall_data.py        # 词表 / 数据文件 / 增量计划 / 使用次数（开发机也能跑）
python tests/test_tag_index.py        # 标签索引 / 锚点检索 / match 语法 / 按用量排序（开发机也能跑）
python tests/test_cli.py              # assistant tag 的计划逻辑（dry-run）+ 重打标签保住 used
python tests/test_wallpaper.py        # 游标 / 工具 / match 透传 / "换成另一张才算一次"（开发机也能跑）
python tests/test_siglip.py           # SigLIP 双塔（板端才跑模型那几条）
python tests/board/siglip_align.py    # 搬运对齐（仓库版 vs 板端实验树 sig/）
python tests/board/tag_quality.py     # 命中率 / 预处理 A/B / IP 检索（板端，§8 的数字出自它）
python tests/board/t10_accept.py      # 三格窗口 + 画像挑图 + 队列动作（板端；§6.2 的窗口原文出自它）
```

板端验收用 `tests/data/wallpaper_tags/truth.json` 里的人工真值算每轴 top-1/top-2
命中率，并用 IP 锚点做 leave-one-out 自检索 + 跨 IP 混淆矩阵。**图片不入库**（真值文件按
文件名引用你壁纸目录里的图），所以换台机器要重打标签、也要重新放图。

> ⚠ 写这类测量脚本时踩过一个坑（记下来）：第一版 LOO 的排序写成
> `sorted((cosine(target, proto), name) for name in records)` —— 分数**与候选无关**，
> 于是 40 个候选全都同分，"排名 31"只是按文件名排出来的假结果。
> 现在脚本会打印**候选分数是否唯一**，并在同分时直接标"排名无效"。
