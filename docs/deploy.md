# 部署与双机同步

> 一句话：**PC 是唯一提交方，板端是运行环境**；两者靠"commit → 推 → 部署"这条链对齐，
> 而且有机制能**证明**它们真的一致（不再靠人肉记忆）。架构与拓扑见
> [`architecture.md`](architecture.md)。

---

## 1. 三方角色

| 角色 | 是什么 | 负责 | 不负责 |
| --- | --- | --- | --- |
| **PC**（Windows 开发机） | 源码真源 | 写代码、交叉编译、跑 host / PC / WSL 测试、**commit / push**、部署 | — |
| **GitHub**（`Anorakz/assiant`） | 中转 | 让板端能 `fetch` 到 `main` | — |
| **板端**（RK3568） | 运行环境 | 跑 Agent 与 GUI、板端测试 | **不提交、不手改代码** |

板端路径：`root@192.168.137.30:/home/kickpi/myproject/assitant`（分支 `main`，
tracking `origin/main`）。

---

## 2. 三条同步路径，各管什么

| 路径 | 命令 | 管什么 | **不管**什么 |
| --- | --- | --- | --- |
| **部署** | `scripts/deploy.ps1` | 让板端**运行的东西**与某个提交一致：交叉编译 `.so`、`git archive` 送 `agent/ tests/ docs/ scripts/ config/config.example.yaml`、写部署清单、跑健康检查 | 不碰 `gui/` |
| **GUI** | `scripts/sync-gui.ps1` | `gui/` 源码 + **板端本机重编** + 可选 ctest | 不碰 `agent/`、`tests/`、`.so` |
| **拉源码** | 板端 `git pull` | 更新**源码副本**（给人看 / 对照 / 在板端排查） | 不重建 `.so`、不更新部署清单 |

**默认走 `deploy.ps1`。** `git pull` 只用于"我想在板端看源码"——它拉完不会重编
`.so`，健康检查随后会（**正确地**）报"落后"，那是在说"你拉了源码但还没部署"。

> ⚠ `deploy.ps1` 只送**已提交**的内容（`git archive HEAD`）。所以顺序永远是
> **先 commit，再部署** —— 未提交的改动不会被送过去。

---

## 3. `deploy.ps1` 按顺序做了什么

```
1. build.ps1          交叉编译 + 校验 ELF aarch64 与 GLIBC 上限 (产品要求 <= 2.17)
2. git archive HEAD   打包 agent/ tests/ docs/ scripts/ config/config.example.yaml
                      (-c core.autocrlf=false -c core.eol=lf, 并检查 tar 里没有 CRLF)
3. 建部署清单          逐文件 sha256 -> logs/deployed-manifest + logs/deployed-rev
4. scp                tarball / .so / libmoonlight / 冒烟二进制 / 清单 /
                      health_check.sh (行尾归一化成 LF 放 /tmp)
5. 板端 tar -xzf      增量解包 —— **不删任何东西**
6. -Prune (可选)      按清单清掉"仓库里已删、板端还在"的残留
7. health_check.sh    必需项 + 落后判定 + 多余文件
```

### 清单为什么放在 `logs/`

`logs/` 已被 `.gitignore` 覆盖，所以 `logs/deployed-manifest` 与 `logs/deployed-rev`
**不会**让板端的 `git status` 变脏（板端可用
`git check-ignore -v logs/deployed-manifest` 自查）。
清单一律写成 **LF + UTF-8 无 BOM**：板端用 `while read` 读它，CRLF 会让每个路径
尾巴多个 `\r`，于是全部对不上。

---

## 4. 落后判定：为什么需要它

增量解包有两面：好的一面是板端专有文件能活下来；坏的一面是**仓库里删掉的文件会一直
留在板端**。再加上人手改板端文件，就有两种"悄悄不一致"：

| 形态 | 含义 | 健康检查怎么处理 |
| --- | --- | --- |
| **落后** | 清单里有，但板端缺失 / 内容不同 | **FAIL（exit 1）**，并点名是哪几个文件 |
| **多余** | 板端有，清单里没有，且不在豁免名单 | 只报告（+ 前 8 条 + 怎么清），**不影响退出码** |

`scripts/deploy.ps1` 跑完会打印这一段：

```
--- 7. 部署一致性 (板端 vs 部署清单) ---
落后判定    OK   一致: 94 个文件全部匹配 (f78d273)      <- 文件数/提交号是当时快照
多余文件    OK   无 (板端没有仓库里已删除的文件)
```

落后判定刻意做成 FAIL：**刚部署完就应该一致**；不一致说明板端被改过，或没部署全。
（如果你希望它只警告不影响退出码，说一声即可。）

---

## 5. 清理板端残留

```powershell
scripts/deploy.ps1 -NoBuild -PruneDryRun   # 先看会删什么 (不动手)
scripts/deploy.ps1 -NoBuild -Prune         # 确认后再删
```

规则的分工（刻意如此，避免两处规则漂移）：

- **"哪些算多余、哪些豁免"只写在 `scripts/health_check.sh` 里**（`SCOPE` + `exempt()`
  + `--list-extra` 模式），因为它是板端脚本，能直接看到板端实况；
- `deploy.ps1 -Prune` 只是**消费** `health_check.sh --list-extra` 的输出，并逐条
  二次校验（必须在 `agent|tests|docs|scripts/` 内、不是绝对路径、不含 `..`、不含空白
  与 shell 元字符、不在豁免名单），然后才删。远端输出直接喂给 `rm` 太危险。

---

## 6. 板端哪些东西不入库

两处规则，**别混**：

**(a) `main` 的 `.gitignore`** —— 仓库级，所有人 clone 都会继承：
`config/config.yaml`、`logs/`、`creds/`、`*.so`、`model/`、`temp/`、`__pycache__` 等。

**(b) 板端 `.git/info/exclude`** —— **本机级，不入库**：

```
llm/  sig/  net/  runtimes/  temp/
docs/gui-qt5-plan.md  docs/gui-qt5-tasks.md
tests/test_llm_integration.py  todo
tests/board/mpp_decode_smoke
```

为什么分两处：`.gitignore` 说的是"**这个仓库**忽略什么"；`info/exclude` 说的是
"**这台机器**上有什么"。换块板子、换个人 clone，不该继承后面这些规则。

> 这些路径原来是板端 `gui` 分支**跟踪**的文件；板端切到 `main` 后它们不再属于任何
> 分支，于是变成未跟踪文件，写进 `info/exclude` 才让板端 `git status` 干净。

---

## 7. 常见操作

| 我想… | 怎么做 |
| --- | --- |
| 改 `agent/` `tests/` `docs/` `scripts/` `native/` | PC 改 → `test-host.ps1` / `test-python.ps1` → **commit** → `deploy.ps1` → `run-board-tests.ps1` |
| 改 `gui/` | PC 改 → **commit** → `sync-gui.ps1 -Test`（板端重编 + ctest） |
| 只想在板端看源码 | 板端 `git pull`（之后健康检查报"落后"是对的） |
| 板端多了不该有的文件 | `deploy.ps1 -PruneDryRun` 看 → `deploy.ps1 -Prune` 删 |
| 板端被手改了 | 健康检查会 FAIL 并点名；`deploy.ps1` 覆盖回去即可 |
| 回退板端 | 板端 `git checkout -f gui`（旧 `gui` 分支仍在本地，指向旧提交）；或 PC 切到旧提交后重新部署 |
| 板端本地文件（`llm/` 等）被删了 | 它们的内容在旧提交里：`git checkout 982ac65 -- <路径>` |
| 板端 GUI 起不来 | `sync-gui.ps1 -Test` 会重编并跑 ctest；二进制在 `gui/build/agent_gui` |
| 想在 WSL 里跑测试 | `sh scripts/test-python.sh` 可以；但**别在那里看 `git status`**（见 §8 第 7 条） |

---

## 8. 必须守住的约定（都是踩过的坑）

1. **`.ps1` 必须 ASCII-only**：PowerShell 5.1 在没有 BOM 时按 ANSI 解码 `.ps1`，
   中文注释会直接把脚本**解析坏**（F2 真踩过：5 处语法错误）。
2. **shell 脚本必须 LF**：`git archive` 要显式
   `-c core.autocrlf=false -c core.eol=lf`，否则 CRLF 进 tar，板端报
   `$'\r': command not found`。
3. **别对原生命令用 `2>&1`**：`$ErrorActionPreference = "Stop"` 下 PS 5.1 会把
   native stderr 变成**终止错误**（哪怕 stderr 是空的、是上一条命令残留的）。
4. **ssh 参数里不要嵌引号**：会被剥掉，远端拿到的是残缺命令。
5. **板端不提交、不手改代码**：改动一律回 PC，走部署流程。
6. **部署前必须 commit**：`deploy.ps1` 只送 committed content。
7. **同一份 checkout 别在 WSL 里跑 `git status`**：PC 侧是 `core.autocrlf=true`，WSL 的 git
   默认 `false`，于是同一棵工作树在 WSL 会**假报约 100 个文件被改**（G1 实测：
   `git status` 一片红，`git -c core.autocrlf=true status` 立刻干净）。**状态以 PC 为准**
   —— PC 是唯一提交方。要看漂移就回 PC 看，别被 WSL 的视图带偏。
8. **别用整树 `scp`/`cp -r` 部署**：早期整树拷贝把 PC/WSL 版本的 `__pycache__`
   （`cpython-312` / `313` / `314`）带到了板端，而板端只有 python3.8，那些缓存
   **永远不可能被加载**（G1 清掉 65 个）。`deploy.ps1` 用 `git archive` 只送已跟踪文件，
   所以正常流程不会再发生 —— 手工补文件时也别整树拷。

---

## 9. 开机自启（T14-7）

板端开机自动跑的东西**都在仓库里的 [`systemd/`](../systemd) 目录**（三个文件，缺一个就会
出问题，见下面"为什么要替换 xrandr"）：

| 单元 | 跑什么 | 谁拉起它 | 重启策略 |
| --- | --- | --- | --- |
| `agent.service` | `python3 -m agent.main`（**root**） | `multi-user.target` | `Restart=on-failure` |
| `agent-gui.service` | `gui/build/agent_gui`（**root**，`DISPLAY=:0`） | `graphical.target` | `Restart=always` |
| `xrandr-startup.service` | 把 DSI 面板转成 **1280×800** | `graphical.target` | —（板端自带，**必须替换**） |

两个都要 root：IPC socket 是 **0600**，桌面自启（kickpi 的 autostart）连不上 Agent；
而"GUI 也走 root 的 systemd 服务"是 2026-09-27 定的，于是 socket 权限不用放宽。

### 安装

```bash
sudo cp systemd/agent.service systemd/agent-gui.service systemd/xrandr-startup.service \
        /etc/systemd/system/
sudo chmod 644 /etc/systemd/system/agent.service /etc/systemd/system/agent-gui.service \
               /etc/systemd/system/xrandr-startup.service
sudo systemctl daemon-reload
sudo systemctl enable agent.service agent-gui.service
# 不想重启机器就立刻起：
sudo systemctl start agent.service agent-gui.service
```

### 为什么要替换板端自带的 `xrandr-startup.service`

板端那份写的是 `After=graphical.target` **且** `WantedBy=graphical.target` —— 后者会让
systemd 给它补一条隐式的 `Before=graphical.target`，于是**自相矛盾**。任何
`After=xrandr-startup.service` 的单元（GUI 就是）只要和它进同一个开机事务就会被拖进
排序环，而 systemd 破环的手段是**直接删掉环里某个启动作业**：现象就是 GUI"开机没启动"
（`inactive (dead)`、`MainPID=0`、journal 里连 `Starting ...` 都没有），**不是**启动失败。
完整取证（journal 原文 + 三条边的环）写在 `systemd/agent-gui.service` 与
`systemd/xrandr-startup.service` 的文件头。

⚠ 顺带两条板端实测的坑（都写进单元文件注释了）：

- drop-in（`xrandr-startup.service.d/override.conf`）里写 `After=` 想**重置**原列表，
  在 systemd 245 上**不生效**（后加的 `After=display-manager.service` 生效了，
  `graphical.target` 却还在）—— 所以这里是**整份替换**，不是 drop-in；
- `StartLimitIntervalSec` / `StartLimitBurst` 在 systemd 245 属于 **`[Unit]`**：原来写在
  `[Service]` 里只会得到一条 `Unknown key name ... ignoring`，限流等于没配。

### 装完怎么自证（都在板端跑）

```bash
systemctl is-active agent.service agent-gui.service      # 两个都 active
journalctl -b | grep -i 'ordering cycle'                 # 必须**空**（有输出就是还在环里）
systemctl --failed --no-pager                            # 不该有这三个单元
systemctl show agent-gui.service -p MainPID -p NRestarts # MainPID 非 0、NRestarts 是 0
DISPLAY=:0 xrandr --query | grep DSI                     # 1280x800 ... left
DISPLAY=:0 xwininfo -root -tree | grep agent_gui         # 有 GUI 窗口
DISPLAY=:0 xwininfo -root -tree | grep 板端助手          # 尺寸必须是 1280x800（不是 883，见下）
assistant status                                         # 已连上 /tmp/agent.sock
```

2026-09-27 的实测记录：重启后 60 秒，两个单元都 `active`、`NRestarts=0`、截图是转正后的
1280×800 首页；分别对两个 `MainPID` 做 `kill -9`，两个单元各自被拉回来（`NRestarts=1`），
GUI 自己重连上 Agent。守卫测试是 `tests/test_systemd_units.py`（不改板端也能跑：
单元形状、键写在哪一段、三个单元合起来有没有环、文档有没有点名三个文件）。

### GUI 换了代码就必须真的重编（T14-7b 的教训）

`agent_gui` 与各 GUI 测试二进制都是**文件**：`cmake --build` 失败时它们**原地不动**，
于是 ctest 照样能跑出一串绿（甚至跑的是上一版的行为）。T14-3 删掉
`gui/src/core/config_sync.*` 时漏删了 `model_page.cpp` 里那行 include，
结果板端从那时起 **GUI 编不过**，而报出来的一切都是"通过"。

规矩：改完 `gui/` 之后看**构建日志**与**二进制时间戳**，别只看 ctest 结果：

```bash
cmake -S gui -B gui/build && cmake --build gui/build -j4   # 必须 exit 0、且无 error 行
ls -l --time-style=+%H:%M:%S gui/build/agent_gui            # 时间该是刚刚
cd gui/build && QT_QPA_PLATFORM=offscreen ctest --output-on-failure
```

PC 侧另有一条守卫 `tests/test_gui_includes.py`：GUI 源码里任何 `#include "…"` 都必须在
仓库里找得到（悬空 include 在 PC 上永远不会暴露，因为 PC 不编 GUI）。

### 跑起来之后看什么：`scripts/monitor.sh`（T14-10）

与本节的自检**互补**：这里管"部署对不对"，它管"机器什么状态"——
`bash scripts/monitor.sh`（人看）/ `--csv`（喂给脚本）/ `--json` / `--watch 5 --csv`（连续采样）/
`--health`（顺手跑一次本节的 health_check.sh）。CPU / 内存 / 各进程 RSS / NPU 频率与负载 /
soc·gpu 温度都在里面，**只读内核接口**（Agent 挂了它照样能跑）。

### 窗口必须真的是一屏（T14-7b，已修）

第一次交付时 kiosk 窗口是 `1280x883`、底部 83px 在屏幕外（`WM_NORMAL_HINTS` 里写着
`program specified minimum size: 935 by 883`）—— **不是"没全屏"**，而是隐藏着的模型页
把 `QStackedWidget` 的最小高度顶到了 811（页面只有 728）。修法：模型页整页放进
`QScrollArea`；现在窗口是 `1280x800`、最小尺寸 **935x674**。
机制、取证命令与"每页 min ≤ 728"这条规矩写在 [`gui.md` §2.1](gui.md)。

---

## 10. 相关文档

- 架构与拓扑、板端目录布局：[`architecture.md`](architecture.md)
- GUI 的构建与使用：[`gui.md`](gui.md)
- 崩溃日志（崩了之后看哪里）：[`crash.md`](crash.md)
- 交叉编译与 sysroot：[`cross-build-rk3568.md`](cross-build-rk3568.md)
- Agent ⇄ GUI 协议：[`ipc-protocol.md`](ipc-protocol.md)
