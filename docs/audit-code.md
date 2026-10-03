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
| 疑似死代码：全仓库零引用 | **10** |
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
