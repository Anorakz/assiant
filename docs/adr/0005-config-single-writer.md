# ADR 0005 — `config.yaml` 只有一个写入者：GUI 经 Agent 写

- **状态**：已生效（T14-2 落地协议与 Agent 侧写入器；T14-3 起 GUI 侧全部改道 —— `ConfigStore` 只读、三个写入点走 `set_config`、C++ `ConfigSyncer`/`gui_config_sync` 已退役）
- **影响面**：`agent/ipc/`（新命令 + 回执 topic）、`agent/core/llm_env.py`（新）、
  `agent/core/settings_config.py` / `settings_credentials.py`（复用）、`agent/cli.py`（doctor）、
  `gui/src/core/config_store.*`（写路径退役）、`gui/src/ui/{settings_page,model_page}.cpp`、
  `gui/src/core/config_sync.*` 与 `gui/tools/gui_config_sync.cpp`（退役）

> ⚠ 本文按**已下的决定与已落地的代码**写（T14-2），GUI 那一半在 T14-3 完成。
> 能指出出处的都标了路径；指不出出处的写成"当时的判断"。

## 背景

归一化 D 系列之后**只有一个配置文件** `config/config.yaml`，但它同时被两个人写：

| 写入者 | 写什么 | 怎么写的 |
| --- | --- | --- |
| **GUI**（Qt5 C++） | `gui.*`（设置页）、`llm.*`（模型页）、切输入源时的 `gui.input_source` | `gui/src/core/config_store.cpp`：文本级替换 + `.bak` + 原子写 |
| **Agent**（Python） | 删掉已触发的一次性日程（R3）、`assistant set …` 改设置项、`assistant study freeze/unfreeze` | `agent/core/schedule_config.py` / `settings_config.py`：同一套文本级手术 |

两份实现都承诺"只动目标那一行、注释与顺序逐字节保留、旁边留 `.bak`"，而且各有自己的测试
（`gui/tests/test_config_store.cpp`、`tests/test_settings_config.py`）—— 但**跨进程没有互斥**：

- 同跑时"后写的覆盖先写的"（例如 GUI 正在保存设置、Agent 同时删掉一条日程，
  两边都以自己读到的旧文本为基准 → 一方的改动被整段丢弃）；
- `.bak` 只有一个文件名，谁后写谁盖掉它 —— **回滚材料的含义变得不确定**；
- 派生文件 `llm/config/llm.env` 由 GUI 侧的 C++ `ConfigSyncer` 写（`gui/src/core/config_sync.cpp`），
  又是一份"只在 GUI 里实现"的映射表：`assistant doctor` 要检查一致性，只能去**跑那个 C++ 二进制**。

当时的判断：这不是"加个锁"能干净解决的问题 —— **两个实现 + 两个写入者**本身就是要消掉的东西。

## 决定

**`config.yaml`（以及 `llm.env`、B 站凭据）唯一的写入者是 Agent。**

1. 协议新增一条**带回执**的命令：`set_config`（payload `{id, keys, credentials}`）→
   Agent 回推新 topic **`config_result`**（`{id, ok, changed, backup, path, error, llm_env}`）。
   关联字段放在 **payload 里**，不新增信封字段（协议本来就没有版本号/关联字段）。
2. Agent 侧实现**复用**现有的两个写入器：`agent/core/settings_config.py`（真源，文本级手术）
   与 `agent/core/settings_credentials.py`（凭据文件）；顺序是
   **先全部校验、再动第一份文件**（凭据→配置计划→写配置→写凭据→派生 `llm.env`），
   任何一步校验失败都**一个字节都不写**。
3. 派生 `llm.env` 的实现**从 C++ 搬到 Python**：`agent/core/llm_env.py`（映射表逐条照
   `config_sync.cpp::buildEnv`，8 个键），在同一个回执里报告成败。
4. GUI 侧（T14-3）：`ConfigStore` **退化成只读 + diff 预览**（不再有写路径）；
   三个写入点（设置页卡片、模型页 `llm.*`、切输入源）改为发 `set_config` 并处理回执；
   凭据三个框走 `credentials` 字段；模型页的「启动服务」（`llm/scripts/*.sh`）同样改为
   让 Agent 执行。C++ 的 `ConfigSyncer` 与 `gui_config_sync` 工具退役。
5. **没有"离线回退"**：Agent 没在跑时 GUI 明确失败并提示（不退回自己写）。

## 理由

1. **单写入者 = 竞态从根上消失**：不是"两个写入者 + 冲突提示"，而是**只剩一个写入者**
   （T14-11 的同跑验收会验证这一点）。`.bak` 的含义也随之确定：它永远是"上一次由 Agent
   写入前的原文"。
2. **一份实现**：文本级手术的规矩（只动一行、保注释、缺段按模板新建、类型跟着模板走）
   现在只有 `settings_config.py` 一处 —— `assistant set` 与 GUI 保存走**同一条路**，
   行为差异不再需要靠两边各写一遍测试来对齐。
3. **真源与派生在同一步里一致**：改完 `config.yaml` 立刻派生 `llm.env`，
   不会再出现"改了真源、忘了派生"或"派生用的是另一份映射表"。
4. **少一个跨语言依赖**：`assistant doctor` 不再需要板端**构建过 gui/** 才能检查派生一致性
   （T14-2 起直接调 `agent/core/llm_env.py`）。
5. **顺带解决权限与"GUI 能不能写"的问题**：GUI 只需要**读**配置（`config.yaml` 644、
   凭据 644），写权限全部留在 Agent 那一侧。

## 替代方案与代价

| 方案 | 为什么没选 | 代价 |
| --- | --- | --- |
| 写前重读 + 冲突提示（两个写入者都留着） | 治标：竞态窗口还在，只是变窄；两边仍各有一份实现 | 现在这条路彻底不需要了 |
| 文件锁（`flock`）互斥 | 跨语言锁要两边都实现同一套约定；而且**两份实现**这件事没解决 | — |
| 让 C++ 调 Python 写入器（或反过来共享库） | 板端 GUI 是 Qt5 程序、Agent 是 Python 进程：跨语言调用要么嵌解释器、要么走 IPC —— 那还不如直接走现成的 IPC | 现在走的就是 IPC |
| 保持现状（GUI 自己写） | 就是上面"背景"里那些问题；同跑验收一定会把它测出来 | — |

**真实的代价（写清楚）**：

- **Agent 没在跑就改不了设置**（刻意不退回直写）。界面必须如实说"Agent 没连上"，
  并给一个「启动 Agent」的动作；这也是 T14-7 把 Agent 做成开机自启服务的原因之一。
- 协议多了一条命令 + 一个 topic：老 GUI 不认识 `config_result`，按"未知 topic 忽略"处理
  （所以协议演进对它是安全的）。
- 每次保存多一个本地 IPC 往返（微秒级）与一次"请求 → 回执"的等待：GUI 要设超时并
  处理"Agent 中途没了"这条路径。

## 后果与边界

- **改设置的入口只剩两个**：`assistant set …`（CLI）与设置页/模型页（GUI）—— 两者最终
  都落到 `settings_config.apply_changes()`。
- `config.example.yaml` 仍然是**键清单与类型的唯一真源**（"能改哪些键 = 模板里有的标量键"），
  GUI 侧那条白名单（页面只写 `gui.*`/`llm.*`/三张卡片那几个键）变成
  **"发出去的 `keys` 只许是这些"** 的请求侧契约测试。
- 写 `llm.env` 的 Python 模块**只有一个**（`agent/core/llm_env.py`），
  且**不许把 llm.env 当配置来源** —— 由 `tests/test_config_source_guard.py` 机械守着
  （只有它能读写那个文件；别处连读都不许）。
- 板端 GUI 现在可以**不以 root 运行**（它不写任何受保护文件）。⚠ 但 T14-7 的选择是
  "两个都走 root systemd"，所以 IPC socket 的 0600 权限**不需要**放宽。

## 参考

- 协议与字段：`docs/ipc-protocol.md` §3（`config_result`）、§4（`set_config`）、§8（常量表）
- Agent 侧：`agent/ipc/protocol.py`（`COMMAND_SET_CONFIG` / `TOPIC_CONFIG_RESULT`）、
  `agent/ipc/__init__.py::_handle_set_config`、`agent/core/settings_config.py`、
  `agent/core/settings_credentials.py`、`agent/core/llm_env.py`
- 派生文件的老实现（T14-3 退役）：`gui/src/core/config_sync.cpp`、`gui/tools/gui_config_sync.cpp`
- 测试：`tests/test_ipc_local_server.py::TestSetConfigCommand`、`tests/test_llm_env.py`、
  `tests/test_settings_config.py::TestCredentials.test_clean_values_*`、
  `tests/test_config_source_guard.py`（写入者白名单与 llm.env 边界）
- 相关 ADR：[0001](0001-ipc-unix-socket.md)（为什么这条回执走本机 socket）、
  [0002](0002-gui-on-board.md)（GUI 与 Agent 同机，这是"走 IPC 写"可行的前提）
