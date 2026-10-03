# 代码规范审计（T15-3）

> 状态：**3-1 工具与口径已落地**（2026-10-03）。本文件是审计的**唯一落点**：
> 方法、基线数字、以及后续每一项发现与收敛动作都记在这里。
> 相关：`scripts/audit-code.py`（机械普查器）、`scripts/audit-baseline.json`（棘轮基线）、
> `tests/test_audit_script.py`（工具自身的测试）。

## 1. 为什么这么做（而不是"读一遍代码"）

仓库规模：Python **170 文件 / 76,474 行**（含审计器与它自己的测试），原生 **110 文件**。靠眼睛"读一遍"
只能得到印象，得不到**可复现的数字**，也无法回答"这次改动有没有新增问题"。

所以先把问题变成**机器可复算的清单**，人只在清单上做判断：
选哪一份当唯一实现、哪些直接删、哪些保留并写明理由。

## 2. 口径（3-1 定稿，与工具注释一致）

| 维度 | 口径 | 为什么 |
| --- | --- | --- |
| 范围 | 只看**我们自己的源码**：文件清单优先用 `git ls-files`（自动排除 submodule 内容与构建产物）；无 git 时用目录白名单 + 黑名单 | submodule（moonlight-common-c / pybind11 / googletest）与 `build*/` 不是我们的代码，混进来只会把数字灌水 |
| 生产 / 测试 | **分开报** | 测试里的重复通常不值得收敛；混在一起会把生产问题埋掉 |
| 重复判定 | 函数体归一化（去 docstring、变量名/常量值/函数名抽象成占位符，**保留属性名与关键字参数名**）后取指纹；**≥3 条语句**才参与比较 | 属性名与关键字参数有语义（`.read()` ≠ `.write()`、`timeout=` 不是噪音）；短函数比重复纯属刷屏 |
| 同名不同体 | **只在模块级函数之间**比 | 不同类上的同名方法是正常设计，不是重复 |
| 方法只登记一次 | 方法带类名登记一次（`Class.m`），不在 walk 里再登记裸名 | 实测踩过：登记两次会让每个方法"与自己重复"，430 组假阳性 |
| 死代码 | 一律标"**疑似**"并给引用计数；分"全仓库零引用"与"仅测试引用"两档 | 本仓库大量使用**字符串注册 / 框架回调 / getattr 派发**，零引用不等于死 |
| 棘轮比较 | 比较前**抹掉行号**（`file:123 name` → `file name`） | 行号会随任何无关改动漂移，不抹的话棘轮立刻变噪音源 |

## 3. 怎么跑

```bash
# 人读报告（默认每节打 10 条，--all 看全部）
python3 scripts/audit-code.py

# 机器可读（给文档/棘轮用）
python3 scripts/audit-code.py --json /tmp/audit.json

# 棘轮：只拦"新增问题"（CI 用这条；有新增退出码 2）
python3 scripts/audit-code.py --baseline scripts/audit-baseline.json

# 收敛之后刷新基线
python3 scripts/audit-code.py --write-baseline scripts/audit-baseline.json
```

配套的静态规则检查（ruff，固定版本，CI 里跑）：

```bash
ruff check . --statistics        # 0.6.9；本轮基线见 §4
```

⚠ ruff 是**独立依赖**：仓库不把它写进运行期依赖，CI 里 `pip install ruff==0.6.9`，
本地要跑就自己装（作者本机放在 `~/.local/bin/ruff`，静态二进制，不动系统 Python）。

## 4. 基线数字（2026-10-03，可复现）

**机械普查器**（`scripts/audit-code.py`，运行 5.8 秒）：

| 项 | 数量 |
| --- | --- |
| Python 文件 / 行 | 170 / 76,474 |
| 原生文件（我们自己的） | 110 |
| 重复实现：生产 | **11 组** |
| 重复实现：测试 | 46 组（通常不收敛） |
| 同名不同体（生产模块级） | **15** |
| 疑似死代码：从未被 import / 未被提到 | **0** |
| 疑似死代码：全仓库零引用 | **3**（全是框架钩子假阳性，见 §8.2） |
| 仅测试引用（需人工确认） | 22 |
| 规范启发式合计 | 171 条 |

规范启发式分项：

| 类别 | 条数 |
| --- | --- |
| `open()` 没写 encoding（默认随 locale） | 69 |
| 深嵌套（≥6 层） | 65 |
| 超长函数（≥120 行） | 16 |
| 异步函数里用 `time.sleep`（阻塞事件循环） | 10 |
| 库代码里用 `print`（绕过日志出口） | 6 |
| 待办标记 `TODO` / `FIXME` / `HACK` / `XXX` | 各 1 / 1 / 1 / 2 |

**ruff 0.6.9 默认规则集**：**89 条**（其中 62 条可自动修）：

| 规则 | 条数 | 含义 |
| --- | --- | --- |
| F401 | 60 | 未使用的 import |
| F841 | 13 | 未使用的变量 |
| E702 | 5 | 一行多条语句（分号） |
| E402 | 4 | import 不在文件顶部 |
| PT004 | 2 | pytest fixture 命名 |
| E713 / E731 / E741 / F402 / F811 | 各 1 | 杂项 |

棘轮基线：`scripts/audit-baseline.json`（**196 条**发现，行号已归一化）。

棘轮**双向验证过**：基线刷新后立刻复跑 = 0；故意注入一条新发现 = 退出码 2；清理后回到 0。

## 5. 结论与后续任务

**已确认的三类问题规模**：重复实现（生产 11 组 / 同名多体 15）、死代码（零引用 8 + 仅测试引用 23）、
规范偏差（启发式 163 + ruff 88）。**没有任何"从未被 import 的模块"** —— 这条是好消息，
说明模块边界是干净的。

后续（按 `todo2.md` 的 3-x）：

| # | 任务 | 出口 |
| --- | --- | --- |
| 3-2 | Python 重复实现普查 | "重复实现 → 收敛到哪一份"表（含风险与顺序） |
| 3-3 | 死代码普查（含 CLI 子命令/配置项孤儿） | 三分类清单（直接删 / 先标记 / 保留说明） |
| 3-4 | 规范一致性普查（Python） | 机械可修批（ruff --fix）+ 需判断清单 |
| 3-5 | 原生侧（C/C++）告警与重复 | 告警清单 + 重复组件表 |
| 3-6 | 收敛实施（用户已定：**直接删**） | 每项：改动 + 守卫测试 + 全套测试绿 + 刷新基线 |
| 3-7 | 文档 + 提交 + CI | CI 在 tip 绿 |

## 6. 本轮（3-1）改了什么

- 新增 `scripts/audit-code.py`（纯标准库，零依赖；人读报告 + `--json` + `--baseline` 棘轮）
- 新增 `scripts/audit-baseline.json`（190 条基线）
- 新增 `tests/test_audit_script.py`（6 条：拿**样例树**断言能力 —— 找得到重复、找得到零引用、
  区分"重复"与"死"；再拿真实仓库跑一遍棘轮，断言"没有新增"）
- CI 增加棘轮步骤（见 `.github/workflows/host-ci.yml`）

⚠ 修工具过程中自身踩的两个坑（都留在代码注释里，避免以后再犯）：

1. 方法被登记两次 → 每个方法"与自己重复"，430 组假阳性；
2. 把"≥3 条语句"的门槛用在了**登记**阶段 → 短函数根本没进索引，死代码检查因此漏报；
3. **审计器把自己的基线当源码扫**：基线里存着 `unused_defs|a/b.py Foo.bar` 这类字符串，
   一旦被扫进来，那些名字就"被引用"了 → 引用计数改变 → 同一条命令在"有基线/没基线"
   两种情况下给出不同数字（实测 166 vs 195），棘轮因此永远报"新增"。基线是**产物**，
   已在 `scan()` 里显式排除。

## 7. 3-2 Python 重复实现普查（收敛表）

口径见 §2。逐条看过源码后判定如下 —— **"结构相同"不等于"该合并"**，
所以表里既有"收敛"，也有"保留（并写明为什么）"，还有一个反例。

### 7.1 该收敛的（11 项，按建议顺序）

| # | 功能 | 现存处 | 收敛到 | 动作 | 风险 |
| --- | --- | --- | --- | --- | --- |
| 1 | `notes()`：攒话 + 取走即清空 | `core/bilibili_buffer.py:328`、`core/game_watch.py:286`、`core/study_watch.py:981` | 新增 `core/notes.py` 的 `NoteBag`（或三处继承的小 mixin） | 三处改用同一个容器；`notes()` 语义（返回副本 + 清空）保持一致 | 低 |
| 2 | `resolve_*_file()`：配置路径 → 绝对路径（相对仓库根） | `core/game_anchors.py:52`、`core/study_anchors.py:143`、`core/study_stats.py:82`、`media/music_library.py:112`、`vision/wall_data.py:141` | 新增 `core/paths.py: resolve_data_file(configured, default)` | 五处改为调用它；保留各自的 `DEFAULT_*` 常量 | 低 |
| 3 | `_clean_text()`：字段清洗（空值字面量 → None） | `tools/music.py:179`、`tools/schedule.py:153` | 新增 `tools/_common.py` | 两处导入；顺带把 `normalize()` 里的同类小助手一起搬进去 | 低 |
| 4 | `_int` / `_float`：读配置里的数字 | `llm/provider.py:250`、`llm/provider.py:259` | 同文件内一个 `_num(key, cast)` | 同一文件相邻闭包，合并后少一层重复 | 低 |
| 5 | `site_packages()`：按实际 python3.x 目录找 site-packages | `image/imagelib.py:84`、`image/check-runtime-deps.py:122` | `imagelib.site_packages`（前者已是共享库） | 后者改为导入；检查脚本不再自带副本 | 低 |
| 6 | `http_get()`：脚本里的取页面 | `scripts/pair_ref.py:65`、`scripts/pair_sunshine.py:65` | 新增 `scripts/_common.py` | 两处导入（签名不同，取并集：`url, timeout`） | 低 |
| 7 | `_log_task_exception()`：吃掉订阅者协程异常并记录 | `core/scheduler.py:124`、`io/chat_bus.py:203` | `io/` 或 `core/` 下一个共享助手 | 两处导入；日志出口统一（见 §3-4 的 print 项） | 低 |
| 8 | `launch` / `resume`：两个只差路径的 GET | `net/sunshine_client.py:553`、`:564` | 同类的私有 `_launch_like(path, app_id, mode)` | 公开方法保留（对外 API 不变），内部合一 | 低-中 |
| 9 | `cosine()`：余弦相似度 | `core/game_anchors.py:60`、`vision/tag_index.py:119` | 新增 `vision/similarity.py`（或 `core/math.py`） | 两处导入；**先核对数值细节**（长度不一致/全零的处理是否一致） | 中 |
| 10 | `vectors()` / `_save_shot()`：锚点库的"解码 + 跳过并记一条"与截图落盘 | `core/game_anchors.py:130,198`、`core/study_anchors.py:298,615` | 两个类抽公共基类 `core/anchors_base.py` | 改动最大，放最后；行为不变，只有日志措辞差异要统一 | 中 |
| 11 | `_int`-类"参数归一化"模式 | `tools/{bilibili,music,schedule,wallpaper}.py` 的 `normalize()` | `tools/_common.py`（与 #3 同一处） | 只收敛**共用小步骤**（别名取值、空值清洗、数值夹取），各工具的业务归一化留在原地 | 中 |

### 7.2 判定为"不是重复、保留"的（写明理由，避免下次又被当成问题）

| 族 | 处数 | 为什么保留 |
| --- | --- | --- |
| `build(services)` | 5（`tools/*`） | 每个工具的**依赖检查与装配**不同（缺哪个入口、给什么提示都不同）。可复用的是"缺依赖就返回 None 并说清原因"这个**模式**，不是代码 |
| `handler(...)` | 6（`tools/*` + `llm/rule_engine.py`） | 动作语义完全不同（关键字/动作/状态各一套） |
| `on_message(topic, data)` | 7（全在 `agent/cli.py`，各自作用域内的闭包） | 处理不同主题的回调，**名字相同是巧合**；留到 3-4 讨论可读性（是否该改名为 `_on_xxx`） |
| `load(path)` / `read_records(path)` | 4 / 2 | 各自的数据文件与校验规则不同 |
| `make_record(...)` | 2 | 两个 schema，各自的 docstring 都写着"schema 只在这里定义一次" —— 这正是**不**该合并的情形 |
| `run(...)` | 4 | 入口/测试/脚本的顶层函数 |
| `summarise(...)`、`clear_plays(...)` | 2 / 2 | 不同数据集、不同模块职责 |
| `decode` / `encode` | 2 / 3 | 语义不同：向量 base64(float16)、IPC 协议编解码、测试数据生成 —— 名字撞车而已（3-4 可议改名） |

### 7.3 一个反例：该**保留两份**的重复

`crc32_ieee()` 在 `image/make-misc-img.py:62` 与 `image/payload/ab-mark.py:93` 各一份。
**不合并**：前者在**镜像构建侧**（PC/WSL）跑，后者要**进板端 payload**（板端 python3
没有 zlib，所以必须自带实现）。跨环境共享代码会引入构建期依赖，得不偿失。
处理方式：两处互相注释指认 + **加一条测试断言两者常量与实现一致**（防止将来只改一处）。

### 7.4 收敛顺序（3-6 执行时按此顺序，每项都带守卫测试）

低风险先行（1→2→3→4→5→6→7），再中风险（8→9→10→11）；每完成一项就刷新
`scripts/audit-baseline.json`，让棘轮把"已收敛"这件事记下来。

## 8. 3-3 死代码普查（用户授权：直接删）

### 8.1 删掉的（7 处零引用 + 1 个孤儿配置项 + 对应文档行）

| 位置 | 是什么 | 为什么确实是死的 |
| --- | --- | --- |
| `agent/core/settings_config.py` `_Template.block_text` | 取模板里某段的原文行 | **连测试都不用**（15 行）；`segment_span` / `lines` 才是被调的那对 |
| `agent/core/study_watch.py` `StudyWatcher.adapt_on` | `adapt(False)` == `freeze()` 的糖 | 零引用；实际调的是 `freeze()` / `unfreeze()` |
| `agent/net/bilibili_api.py` `_now()` | `return time.time()` | 零引用（两行的包装，没人要） |
| `agent/vision/tag_index.py` `TagIndex.vector_of` | 取某张图的向量 | 零引用；被用的是 `has_vector` |
| `agent/vision/siglip/config.py` `SiglipConfig.as_dict` | 给日志看的摘要 dict | 零引用；`describe()` / `__repr__` 才是出口 |
| `gui/tests/e2e_ipc.py` `Proc.log_tail` | 取日志尾部若干行 | 零引用（测试辅助也要算死代码） |
| `image/check-runtime-deps.py` `target_dir()` | "兼容旧调用"的选树包装 | 零引用；`site_packages` 现在直接委托 `imagelib` |
| `config/config.example.yaml` `chat_channel` + `docs/gui.md` 对应表项 | 对话通道开关 | **全仓库唯一真孤儿键**：agent 不读、GUI 不读（`gui/` 里 grep 不到），只有 example 与 gui.md 提它 —— 是"原来的 `pc` 输入源被移除"那次留下的残渣 |

删除后：**全量套件 59 files 仍然 OK**（没有任何测试引用它们 —— 这是"确实没人用"的最强证据），
`scripts/test-python.sh` 的棘轮显示"已消掉 7 条，没有新增"。

### 8.2 假阳性：两类"零引用但不是死代码"

| 类别 | 例子 | 为什么不是死的 |
| --- | --- | --- |
| **框架/基类按名派发** | `agent/core/bilibili_buffer.py` `Handler.do_HEAD`（`http.server` 按方法名派发）；`agent/core/bilibili_buffer.py` `Handler.log_message`（同上，仅测试引用） | 调用方不在我们代码里，是标准库按名字找 |
| **访问器/访问者钩子** | `scripts/audit-code.py` 自己的 `BodyNormalizer.visit_Name` / `visit_Constant` | `ast.NodeTransformer` 按 `visit_<Type>` 命名约定回调 |

⚠ 有意思的是：**审计器把自己的钩子报成了死代码** —— 这正是"零引用只能当线索、
不能当判决"的最好例子。剩下的 3 条零引用全是这一类，保留。

### 8.3 仅测试引用（22 条）：判定为"测试锁定的契约"，保留

这 22 条是 public API（`can_transition`、`remove_callback`、`callback_count`、
`is_allowed`、`unregister`、`last_rule`、`remove_rule`、`subscriber_count`、
`class_names`、`last_sample`、`cached_count`、`has_vector`、`media_headers`、
`playlist_detail/list`、`auth_check`、`encode_batch`、`load_image`、
`update_thresholds`、`run_derivation_check`、`log_message` …）。

**处理**：保留。理由 —— 它们是**测试锁定的行为契约**（改坏了测试会红），删掉等于
把测试的观测面一起拆掉；真要缩，应该"删取值器 + 删对应测试"成对进行，而不是单删。
其中 `Handler.log_message` 属 §8.2 的框架钩子类。

### 8.4 另外两类孤儿（任务表点名的）

| 类别 | 结果 |
| --- | --- |
| **CLI 子命令孤儿**（定义了但进不了派发表） | **0**：按零引用口径，`agent/cli.py` 里没有"定义了却没人引用"的命令函数 |
| **配置项孤儿**（example 里有、没人读） | 初筛 14 个"`agent/*.py` 里一次都没出现"的键，逐个到 `gui/` 复核后：**13 个是 GUI 在读**（`theme`/`fullscreen`/`wake`/`idle_ms`/`speed`/`start_page`/`video_overlay`/`max_rows`/`bottom`/`input_source`/`onboard_auto`/`monitor_interval_ms`/`api_base`）—— 这是**跨语言配置契约**，不是孤儿；**1 个（`chat_channel`）谁都不读 → 已删** |

### 8.5 顺带学到的（写进工具注释）

删代码这件事，**"零引用"是线索不是判决**：
· 框架钩子、`getattr`/字符串注册、跨语言契约都会让"没人引用"成立而实际上必须保留；
· 所以流程是：机械普查 → 人工判定 → 小步删 → **套件绿 + 棘轮"已消掉、无新增"** 才算完成。
