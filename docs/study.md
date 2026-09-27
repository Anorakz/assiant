# 学习内容监督：分辨屏幕上是不是学习内容

> Phase 13（T13-1…T13-7）落地。**判定不进 LLM** —— 判据是屏画面的 **SigLIP 向量**，
> 阈值在运行期自己修正。这份文档写清：判什么、怎么判、动什么、边界在哪、怎么验。
>
> 相关：`docs/tagging.md`（同一份 SigLIP 的另一条用途）、`docs/architecture.md` §4.3（视觉层）、
> `docs/config-sources.md`（谁写哪个文件）。

---

## 1. 它做什么 / 不做什么

在 **STUDY** 状态下，Agent 每隔一段时间看一帧串流画面，判断"现在是不是学习内容"：

```
不像学习 ──▶ 气泡「现在是学习时间」（**只提醒一次**，且**不算一次不通过**）
        ──5 分钟──▶ 第 1 次不通过 ──5 分钟──▶ 第 2 次 ──5 分钟──▶ 第 3 次
        ──▶ **返回桌面（Win+D）** + 冷却 30 分钟
判成学习 ──▶ 失败计数清零，下一次 30 分钟后再看
判不出来 ──▶ **完全中性**：不提醒、不计数、不弹桌面、也不重置那条链
```

**它不做什么**（都是你定的）：

| 不做 | 为什么 |
| --- | --- |
| **不让模型判决** | 判定是余弦 + 阈值，**一次模型推理都不花**（SigLIP 编码是视觉层的事，不是 LLM） |
| **不用 LLM 工具** | 不进 `TOOL_MODULES`、模型看不到也调不到它；动作只由 Agent 自己发 |
| **不判科目 / 不走神** | 256×256 的帧**看不清文字** —— 只判"像不像学习"这个大类 |
| **不在别的状态跑** | 只有 STUDY 才有"学习时间"这回事；SLEEP/IDLE/GAME 里一帧都不看 |
| **不看对话期间** | 队列是**对话关键词**驱动时这一轮不抓帧（沿用认游戏那条规矩，`skip_on_keyword`） |

---

## 2. 判定口径：两个大类原型 + 相对分（T13-4 板端标定定的）

```
relative = cos(帧, study 原型) − cos(帧, not_study 原型)

relative ≥ +band  -> study
relative ≤ −band  -> not_study
|relative| < band -> unknown（中性）
```

- **原型** = 该大类**所有锚点的单位向量平均方向**（`StudyAnchors.category_prototypes()`）;
- **两个大类都得有锚点** —— 只有一边时 `relative` 是 `None`（"更像哪边"没有信息量），
  那时走进程名辅助，或者判 unknown；
- `band` 是**没把握带**（`study.relative_band`，初值 **0.05**）。

### 为什么**不能**用绝对余弦阈值（别改回去）

39 张真截图的实测（[`tests/board/t13_study_calib.py`](../tests/board/t13_study_calib.py)）：

| 口径 | 5 类准确率 | 2 类准确率 | 说明 |
| --- | --- | --- | --- |
| 文本提示词（零样本） | 51–56% | 87–97% | 2 类数字好看只是因为"学习"占多数 + 它把 `real` 全推成 `doc` |
| 图像锚点（逐条，留一） | 72–74% | — | |
| 图像锚点（类原型，留一） | 77–85% | — | |
| 锚点 + 文本**融合** | 62–64% | — | **融合反而更差** → 判据不用文本 |
| **大类原型 + 相对分** | — | **95–97%** | 判定走这条 |

而**绝对**余弦在两个大类之间**重叠**：study 的 5% 分位 0.796 / not_study 的 95% 分位 0.906，
且 study 最小 0.753 < not_study 最大 0.906。按"两个分位中点"定出来的 0.85/0.16 会让
**69% 的帧判不出来** —— 等于监督不干活。所以 `confident_score` / `confident_margin`
这两个键**只是标定记录**（不参与判定），判定吃的是 `relative_band`。

### 相对分的取舍（h1 管线，39 张）

| band | 准确率 | 误打扰（学习→非学习） | 监督失效（非学习→学习） | 判不出 |
| --- | --- | --- | --- | --- |
| 0.00 | 95% | 0 | 2 | 0 (0%) |
| 0.02 | 92% | 0 | 2 | 1 (3%) |
| **0.05**（当前初值） | **90%** | **0** | **1** | **3 (8%)** |
| 0.10 | 74% | 0 | 0 | 10 (26%) |

选初值的口径写在标定脚本里（人的取舍，写死免得每次跑出不同答案）：
**先保证"一次都不误打扰"，再要求"监督失效 ≤1 且判不出 ≤10%"，在其中取最小的 band**。
两个方向的代价不一样：**误打扰**会弹气泡/弹桌面（最不能忍），**监督失效**只是没起作用。

---

## 3. 三个结论，第三态是一等公民

| 结论 | 含义 | 动作 |
| --- | --- | --- |
| `study` | 像学习（真人场景 / 代码 / 文档） | 计数清零，下一次 30 分钟后 |
| `not_study` | 不像学习（动漫 / 游戏场景） | 走上面那条升级链 |
| `unknown` | **判不出来** | **什么都不动**（不提醒、不计数、不弹桌面、不推进也不重置） |

`unknown` 的四种来源：带内（两边差不多）、两路**冲突**、库里缺一个大类、锚点库还空着。

**为什么"冲突"判 unknown 而不是像认游戏那样"以进程为准"**：认错游戏只是搜错视频；
这里判错一个方向是**打扰人**、另一个方向是**监督静默失效**，两个方向都不该靠单边证据硬猜。

---

## 4. 升级链的细节（都在 `study_watch.py`）

| 细节 | 规矩 |
| --- | --- |
| 提醒几次 | **一次**。那句话是写死的：「现在是学习时间」 |
| 3 次不通过**含不含**提醒 | **不含**。提醒那次计 0 次，之后每 5 分钟算一次 |
| 返回桌面后补不补话 | **不补**（同一件事不吵两次） |
| 冷却（默认 30 分钟） | 冷却管的是**动作**，**不是观察** —— 冷却期里照样看（`cooldown_probe_min` 默认 1 分钟一次），只看不动。不看的后果是"弹回桌面后 5 分钟内又变回学习"这条**永远发现不了** |
| 冷却期里判成学习 | **冷却立刻结束**（人家已经回到学习内容了） |
| 「可能误判」 | 弹回桌面后 **5 分钟内**又判成学习 -> 记一条「可能误判」+ 给那个类**停学 30 分钟**（别把一次误判立刻学成锚点） |
| 每个 STUDY 周期 | 进入 STUDY 时 `reset_cycle()`: 失败计数/提醒状态/冷却/停学都清掉，**学到的带与锚点留着**，并且**立刻判一眼** |

---

## 5. 阈值怎么"不定死"（你定的"运行过程中不断优化"）

**输入只有带标签的样本**（PC 进程名；以及 T13-8 之后的 `assistant study label` 人工标注），
护栏全部写死在代码里：

| 护栏 | 值 |
| --- | --- |
| 平滑 | EWMA `0.9 × 旧 + 0.1 × 新`（`relative_study` / `relative_not_study` 各一份） |
| 样本不够不动 | 两个大类**各**攒够 `min_labeled`（默认 20）个才允许挪 |
| 目标 | 两类相对分分布的交界中点（study 的 5% 分位与 not_study 的 95% 分位的中点） |
| 步长 | **一次只动 0.01** |
| 界 | `relative_band ∈ [0.01, 0.20]`，到底了就停 |
| 判不出来太多 | 比例 > `target_unknown_rate`（0.30）-> 把带**收窄** 0.01（判得多一点） |
| 画面对反了 | 最近 20 条带标签样本里出现"标着 study 却判成 not_study"（或反过来）-> 把带**放宽** 0.01 |
| `unknown` | **从不学习**（它连往哪边挪都不知道） |
| 每次调整 | 往 `study_stats.notes()` 写一条**人能看懂的理由**（含数字） |
| 人的开关 | `freeze()` 冻住 / `reset_learning()` 回到配置初值（锚点不动） |

**持久化**：阈值与 EWMA 都落在 `config/study_stats.json` —— 不落盘的话 Agent 一重启就退回
标定初值，"运行中不断优化"就只优化到下一次重启。

---

## 6. 数据文件（两份都是**派生数据**，已进 `.gitignore`）

| 文件 | 内容 | 谁写 |
| --- | --- | --- |
| `config/study_anchors.jsonl` | 一行一个锚点：子标签 + 768 维 float16 向量 + 截图路径 + 来源 + 时间 | `agent/core/study_anchors.py`（自学习**追加**；丢最旧/清空时**整篇原子重写**） |
| `config/study_anchors/<子标签>/` | 自学习时存的截图（`keep_shots` 打开才有；标定播的种不存） | 同上 |
| `config/study_stats.json` | 阈值 + EWMA + 计数 + 分数分布 + 最近明细/备注（**全程有界**） | `agent/core/study_stats.py`（整篇原子重写） |

边界：**这两个模块是仅有的写入者**（登记在 `tests/test_config_source_guard.py::ALLOWED_WRITERS`
里）。配置真源仍然只有 `config/config.yaml` 一个。

**有界**：每类锚点上限 `max_anchors_per_class`（默认 200，超了**丢最旧**，内存与文件一起丢）；
直方图固定 20 档 × 3 类；明细 200 条、备注 50 条（定长环形）；计数器 64 项、阈值 16 项、EWMA 16 项。

**与游戏锚点库分开**（你定的）：游戏库要认**作品**（小类）且运行期被进程名纠错大量追加，
混进来会把类别分布压偏；生命周期也不同（游戏库在 GAME 攒、学习库在 STUDY 攒）。

---

## 7. 管线：截图 ≠ 板子看到的画面

板子拿到的不是 `screenshot.png`，真实链路是：

```
PC 桌面 ──Sunshine 按 sunshine.width/height 编码──▶ 板端解码 1280×720 YUV420P
        ──native/preprocess.cpp──▶ 256×256 RGB888 ──▶ SigLIP
```

`agent/vision/frame_pipeline.py` 把最后两步**一模一样**地复现出来（T13-4 标定用它；
T13-8 的 `assistant study label --image` 也走它 —— 那条 CLI 入口当时还没落地）。关键一条：
native 那步是**点采样不是面积平均**
（`rx = (dx * roi.w) / 256`，整数除；1280 宽就是"每 5 列取 1 列"），所以细笔画会走样 ——
标定必须照原样复现，不能"顺手好好缩一下"（那样量的是另一条管线）。

**板端实测的几何（T13-6）**：PC 桌面是 **1024×768**、流是 **1280×720** ->
**Sunshine 在缩放**（不是把主机切成流分辨率），所以"先缩到流分辨率"这一步是真的；
两个宽高比不同（4:3 vs 16:9），真帧里**多半有 pillarbox 黑边** —— 黑边宽度这次没量准
（当时画面很暗，黑边检测不可靠）。**想标定与真帧完全一致，最省事的是串流时把 PC 桌面设成
1280×720**；或者给标定加一条"pillarbox"假设重播锚点。

---

## 8. 配置（`config.yaml` 的 `study:` 段）

| 键 | 默认 | 含义 |
| --- | --- | --- |
| `enabled` | `false`（模板）；**板端真配置 T13-10 起 = `true`** | 关掉 = 什么都不做（组件照旧注册） |
| `relative_band` | `0.05` | **判定的阈值**：没把握带（运行期会自己挪） |
| `target_unknown_rate` | `0.30` | 判不出来的比例超过它就把带收窄 |
| `min_labeled` | `20` | 两类各攒够多少带标签样本才允许挪带 |
| `focus_interval_min` | `30` | 判成学习之后多久再看 |
| `recheck_interval_min` | `5` | 提醒之后多久复查 |
| `max_failures` | `3` | 连续几次不通过就弹回桌面（**不含**提醒） |
| `cooldown_min` | `30` | 弹回桌面后多久不再管（只看不动） |
| `cooldown_probe_min` | `1` | 冷却期里多久看一眼 |
| `remind` / `back_to_desktop` | `true` / `true` | 两个动作各自的开关 |
| `skip_on_keyword` | `true` | 对话关键词驱动时不抓帧 |
| `learn` / `adapt` | `true` / `true` | 自学习 / 带自适应 |
| `anchor_file` / `stats_file` | `config/study_anchors.jsonl` / `config/study_stats.json` | 两份派生数据放哪 |
| `max_anchors_per_class` | `200` | 每类锚点上限（丢最旧） |
| `keep_shots` | `false` | 自学习时存不存截图 |
| `classes` | 五类映射 | 子标签 -> 大类（`code`/`doc`/`real` = study，`anime`/`game` = not_study） |
| `process_names` | 31 条 | 进程名 -> **子标签**（辅助证据；复用 `music:` 的 ssh 配置） |
| `confident_score` / `confident_margin` | 标定记录 | ⚠ **不参与判定**（见 §2） |

### 8.1 GUI 设置页的三张卡片（T13-9 / T13-10）

板端设置页把"最常改的那几项"做成了三张卡片。它们写的是**同一批键**、走**同一套约定**
（与 `assistant set` 那份 `agent/core/settings_config.py` 一致：只动目标那一行、注释/顺序
逐字节保留、旁边留 `.bak`、**缺段按 `config.example.yaml` 整块新建**、值的类型跟着模板走）——
GUI 是 C++、跑在板端，与 Agent 侧的 Python 没法共用代码，所以只能靠**同口径 + 测试**对齐。

| 卡片 | 写哪些键（**白名单就是这些**） |
| --- | --- |
| **学习监督** | `study.enabled`、`study.focus_interval_min`、`study.recheck_interval_min`、`study.max_failures`、`study.cooldown_min`、`study.cooldown_probe_min`、`study.relative_band`（起始值；运行期仍会自己挪） |
| **游戏检测** | `bilibili.game_watch.enabled`、`bilibili.game_watch.interval_s`、`bilibili.game_watch.confident_score` |
| ↳ 卡里的 **B 站凭据** | `bilibili.cookie_file`（配置里那一行）+ `SESSDATA` / `bili_jct` / `DedeUserID`（写进**凭据文件**，不是真源） |
| **画像压缩** | `profile.enabled`、`profile.trigger_chars`、`profile.trigger_turns` |
| 原有卡片 | `gui.debug`、`gui.wake.*`、`gui.video_overlay.*`、`gui.fullscreen`、`gui.start_page`、`gui.input_source` |

- **GUI 不碰的键**（只能在 CLI / 真源里改）：`study.remind`、`study.back_to_desktop`、
  `study.skip_on_keyword`、`study.learn`、`study.adapt`、`study.target_unknown_rate`、
  `study.min_labeled`、`study.anchor_file` / `stats_file` / `max_anchors_per_class` / `keep_shots`
  —— 用 `assistant set study --set study.remind=false --apply` 这类写法。
- **凭据框故意不预填**：里面存的是账号，状态标签只给掩码（`se…90（15 位）`）；
  三个框**留空 = 不改动那个键**（合并写），填了才写，写完就清空。
- `config_store.cpp` 的实现细节：载入模板后才有"缺段新建 + 类型校验"；**没载入模板时行为与
  T13-9 之前完全一致**（父块不存在就拒绝写入）—— 老部署不会因为少一份模板就写出残桩配置。
- 守卫：`gui/tests/test_settings_page.cpp::saveOnlyTouchesWhitelistedKeys` 拿仓库里那份
  `config.example.yaml` 当真源，保存后**逐行 diff** 出被动过的键，断言全部落在上表范围内
  （`study.classes` / `process_names` 这些结构级内容必须逐字节不变）。

---

## 9. 怎么验

```sh
# 1) 离线单测（开发机与板端都能跑）
python tests/test_study_watch.py        # 判定/升级链/unknown 中性/带自适应/黑帧跳过（70 项）
python tests/test_study_anchors.py      # 锚点库（上限/相对分/大类原型）+ 有界统计（96 项）
python tests/test_frame_pipeline.py     # "截图 -> 板子看到的那一帧"（点采样与 C++ 一致）
python tests/test_main.py               # Runtime 接法: 只在 STUDY / 气泡原话 / Win+D 不补话 / 开关

# 2) 板端标定（要真 NPU + 数据集 /home/kickpi/study_dataset/<子标签>/*）
python3 tests/board/t13_study_calib.py --plan          # 只看数据集与管线, 不加载模型
python3 tests/board/t13_study_calib.py --all           # 四种管线 × 五路方法（≈10 分钟）
python3 tests/board/t13_study_calib.py --apply         # 把锚点与起始阈值写进 config/

# 3) 板端端到端验收（要 PC 开着 Sunshine; --no-stream 可只跑 B/C/D）
python3 tests/board/t13_accept.py

# 4) 板端 GUI 验收（T13-10: 真配置上保存 + 截图取证; 不碰派生数据与凭据）
python3 tests/board/t13_gui_accept.py                 # 只在副本上跑 + 打印计划
python3 tests/board/t13_gui_accept.py --apply-live     # 真在 config/config.yaml 上点保存

# 手动取证（GUI 自己的读取路径，不靠截图读字）:
python3 tests/board/t13_gui_accept.py --dump-cards     # = agent_gui --settings-dump-cards
QT_QPA_PLATFORM=offscreen ./gui/build/agent_gui --windowed --page settings \
    --screenshot /tmp/cards.png --settings-scroll-demo 1500   # 滚到卡片位置再截图
```

`t13_accept.py` 验的 26 项里，值得记住的几条：

- **真帧**这一路（真 moonlight 帧 → 真 NPU → 真锚点库）+ 几何结论（见 §7）；
- **升级链**用**假时钟**走完（不真等 15 分钟），并且提醒那条要从**真 IPC** 的 `llm` topic 上看到原文；
- **"不确定时绝不按键"**：把带临时调到 0.5 让真帧落带内 -> `unknown` -> 一个键都不按、计数一点没动；
- **动态带**：喂带标签样本 -> 正好挪 0.01、有理由、落盘、**重启后还在**、`freeze()` 后不动；
- **不变量**：真 `config/config.yaml` / 两份派生数据 md5 全未变（验收用副本）、`git status` 干净、
  没留下 moonlight/llama-server 进程。

### 板端实测（T13-4 / T13-6 的数字）

| 项 | 数字 |
| --- | --- |
| SigLIP 编码 | 1.26–1.72 s/张（板端 NPU）；模型加载 2.5 s |
| 一次判定（含编码） | ≈1.26 s；满库 1000 条锚点时匹配那部分 ≈0.31 s |
| 峰值 RSS | 962–1021 MB（SigLIP 常驻 923 MB，**与认游戏共用一份**） |
| 39 张自匹配（T13-6） | 学习帧 22/22 判成 study；非学习帧 15/17 判成 not_study、2 张落带内；**0 次误打扰** |
| 留一法（T13-4） | 大类原型 + 相对分 **2 类 95–97%**；band 0.05 下 误打扰 0 / 失效 1 / 判不出 8% |
| GUI 验收（T13-9 / T13-10） | 板端 `ctest` **23/23**；真 GUI 在真配置上一次保存，`study:`/`bilibili:`/`profile:` 三段**与模板逐字节相同**地长出来；行尾注释的对齐也留在原地 |

---

## 10. 已知边界（别在别处吹过头）

1. **样本薄**：数据集 39 张，其中 doc 5 / real 5 / **anime 只有 4** —— 标定只够定**初值**，
   收敛靠运行期的带标签样本。`anime` 是最弱的一类（T13-4 的混淆矩阵里 anime↔game 最乱，
   T13-6 的自匹配里 4 张有 2 张落带内）。想更稳就多截几张动漫/游戏场景丢进数据集重播一次。
2. **几何没完全定死**：pillarbox 黑边宽度没量准（当时画面暗）。串流时把 PC 桌面设成 1280×720
   最省事；否则要给标定加一条 pillarbox 假设。
3. **进程名那条路没有前台信息**（Windows 的 sshd 在 session 0，窗口标题恒空）：
   开着浏览器查资料 + 挂着游戏，它照样说"游戏在跑" —— 所以它只是**辅助**，
   而且浏览器（chrome/msedge/firefox）与我们自己的播放器（mpv）**故意不映射**。
4. **整幅全黑 = 跳过**（串流没起来 / 屏幕休眠 / 锁屏时 `read_latest()` 会给全黑帧，
   实测它会被判成"学习"）。判据是"整幅 ≤ 8/255"，深色主题不受影响。
5. **只判大类**：256×256 看不清文字，判不出"在学哪一科 / 有没有走神"。
6. **共用一份 SigLIP 是刻意的**（不是边界，是设计）：谁先到谁负责把它加载起来
   （`GameWatcher.encoder()`）。923 MB × 2 会把 3.9 GB 的板子撑死，而且"一个能力只有一条实现"。
7. **GUI 只能改白名单里那些键**：气泡原话、两个动作开关（`remind` / `back_to_desktop`）、
   `learn` / `adapt`、自适应那几个参数、数据文件路径，都只在 CLI 或真源里改（见 §8.1）。
   想"临时别弹桌面"就 `assistant set study --disabled --apply`，别去点卡片的开关再改回来。
8. **验收怎么算数**：截图只证明"渲染出来了"，字段值一律以 **GUI 自己读回来的文本**为准
   （`agent_gui --settings-dump-cards`，或 `t13_gui_accept.py --dump-cards`）——
   人眼从 1280×800 的图上读小字不算证据。真配置上的保存由
   `t13_gui_accept.py --apply-live` 驱动（保存前留 `.bak`，回滚就把它拷回来）。
