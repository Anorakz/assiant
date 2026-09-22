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
 "embedding":"<base64 float16 × 768>"}
```

| 字段 | 为什么留着 |
| --- | --- |
| `sha256` | 图换了（重新导出、被替换）→ 只有那一行要重打 |
| `model_sha8` / `vocab_sha8` | 换模型或改词表 → 所有标签作废，机械可查 |
| `tags` | 每轴 top-k（默认 3）+ **余弦分数** |
| `embedding` | L2 归一化的图像向量：IP 检索/相似图召回**不用再过 NPU**（一张 ≈ 2KB） |
| `ms` | 每张耗时，性能回归有据可查 |

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

## 6. 已知边界（如实写下来）

- **分数是余弦，不是概率**：`logit_scale/bias` 没随模型导出，拿不到标定过的 sigmoid。
  所以**每轴的阈值要实测标定**，而且不同轴的分数不可比（不要做"总分"）。
- **词表上限**：零样本分类选不出词表外的东西；`abstract` 这类图最容易"勉强归到某个类"。
- **IP 检索的"准"取决于锚点**：一个 IP 只有 1 张锚点图时，它只能"像这张"；
  多给几张（同 IP 不同构图）会明显更稳。
- **不对 UI 截图负责**：SigLIP 对整屏截图会坍缩（实测见 `llm/multimodal_report.md` §7.2）——
  这是壁纸（自然图像）场景之外的用法，别拿它当借口。
- **预处理照官方口径**：`cv2` 读 → BGR→RGB → **压扁**到 256×256 → 原始 0-255。
  不要"改成等比 letterbox"：那会跑到模型训练分布之外（`preprocessor_config.json` 就是
  256×256 压扁，归一化已烧进 rknn）。

## 7. 实测数字（板端，2026-09-22，人工真值 12 张）

这些数字**照实记录**（包括不好看的那些），跑法见 §8。⚠ 真值是我按 4×3 联系表
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

## 8. 怎么验

```bash
python tests/test_wall_data.py        # 词表 / 数据文件 / 增量计划（开发机也能跑）
python tests/test_cli.py              # assistant tag 的计划逻辑（dry-run）
python tests/test_siglip.py           # SigLIP 双塔（板端才跑模型那几条）
python tests/board/siglip_align.py    # 搬运对齐（仓库版 vs 板端实验树 sig/）
python tests/board/tag_quality.py     # 命中率 / 预处理 A/B / IP 检索（板端，§7 的数字出自它）
```

板端验收用 `tests/data/wallpaper_tags/truth.json` 里的人工真值算每轴 top-1/top-2
命中率，并用 IP 锚点做 leave-one-out 自检索 + 跨 IP 混淆矩阵。**图片不入库**（真值文件按
文件名引用你壁纸目录里的图），所以换台机器要重打标签、也要重新放图。

> ⚠ 写这类测量脚本时踩过一个坑（记下来）：第一版 LOO 的排序写成
> `sorted((cosine(target, proto), name) for name in records)` —— 分数**与候选无关**，
> 于是 40 个候选全都同分，"排名 31"只是按文件名排出来的假结果。
> 现在脚本会打印**候选分数是否唯一**，并在同分时直接标"排名无效"。
