# 部署与双机同步

> 一句话：**PC 是唯一提交方，板端是运行环境**；两者靠"commit → 推 → 部署"这条链对齐，
> 而且有机制能**证明**它们真的一致（不再靠人肉记忆）。架构与拓扑见
> [`architecure.md`](architecure.md)。

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

---

## 9. 相关文档

- 架构与拓扑、板端目录布局：[`architecure.md`](architecure.md)
- GUI 的构建与使用：[`gui.md`](gui.md)
- 交叉编译与 sysroot：[`cross-build-rk3568.md`](cross-build-rk3568.md)
- Agent ⇄ GUI 协议：[`ipc-protocol.md`](ipc-protocol.md)
