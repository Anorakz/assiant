# CI（GitHub Actions）

> T14-4 起的**主机侧**门禁。板端门禁是板端 ctest（`scripts/sync-gui.ps1 -Test`）与
> `tests/board/*` 那些验收脚本 —— 它们在真板子上跑，CI 够不着。

## 1. 有哪些 workflow

| workflow | 触发 | 干什么 |
| --- | --- | --- |
| [`host-ci.yml`](../.github/workflows/host-ci.yml) | push 到 main / PR / 手动 | host C++ 单测（真编译 + ctest）、Python 套件、ruff 窄口径 |
| [`release.yml`](../.github/workflows/release.yml) | tag `v*` / 手动 | **源码包**（+ 子模块 SHA + sha256）；`CROSS_BUILD=1` 时在**自托管 runner** 上真交叉编译并附 `.so` |

## 2. `host-ci` 到底验了什么（**以及没验什么**）

验：

- **host C++ 单测**：`cmake -S . -B build-host -DAGENT_BUILD_TESTS=ON` + `ctest`。
  根 `CMakeLists.txt` 把 moonlight-common-c 与 Rockchip MPP 都套在 `if(CMAKE_CROSSCOMPILING)`
  里，所以**主机上真的能编能跑**（不需要板端库）。这一步同时也把 `native/` 的 host 分支
  **真编译**了一遍 —— 比 `-fsyntax-only` 强，所以没再单独做语法检查那一步。
- **Python 套件**：`bash scripts/test-python.sh` —— 与开发机、板端**同一个入口**
  （45+ 个文件，含 `tests/test_docs.py` / `test_adr.py` / `test_config_source_guard.py`
  那几条守卫）。
- **ruff 窄口径**：`ruff check --select E9,F63,F7,F82`（语法错、未定义名、坏 f-string）。
  ⚠ 刻意**不做**全量风格检查：仓库里原本一行 lint 配置都没有，全量会把几千行卷进来 ——
  那是一次单独的、要你点头的改动。

**没验**（写清楚，别误以为 CI 绿 = 板端能编）：

- **真交叉编译**：需要 Arm GNU 工具链 + 板端 sysroot（含 RKNN/MPP 私有库），两者都不在
  仓库里。真交叉编译是**本地门禁**（`scripts/build.ps1`）与 release 的**自托管 runner**。
- **GUI（Qt5 aarch64）**：板端才有 Qt；GUI 的门禁是板端 `ctest`（现在 22 个目标）。

## 3. 本地跑同一套（提交前自查）

```bash
# host C++ 单测
cmake -S . -B build-host -DAGENT_BUILD_TESTS=ON && cmake --build build-host --parallel 4
ctest --test-dir build-host --output-on-failure

# Python 套件（PC: scripts/test-python.ps1；板端/Linux: scripts/test-python.sh）
python3 -m pip install ruff pytest pytest-asyncio   # 只装一次
ruff check --select E9,F63,F7,F82 agent tests
bash scripts/test-python.sh
```

## 4. 自托管 runner（给 release 用；T14-5）

`release.yml` 里那条"真交叉编译"的 job 跑在**你自己的机器**上（它才有
`E:/rk3568/arm` 工具链与 sysroot）。注册一次即可：

```powershell
# 在 GitHub 仓库 Settings -> Actions -> Runners -> New self-hosted runner 里拿到 token，然后：
mkdir E:\actions-runner; cd E:\actions-runner
Invoke-WebRequest -Uri https://github.com/actions/runner/releases/download/v2.319.1/actions-runner-win-x64-2.319.1.zip -OutFile runner.zip
Expand-Archive runner.zip -DestinationPath .
.\config.cmd --url https://github.com/Anorakz/assiant --token <TOKEN> --labels rk3568-cross --name pc-cross
.\run.cmd            # 前台跑（装成服务： .\svc.cmd install + .\svc.cmd start）
```

- 标签必须是 **`rk3568-cross`**（job 里 `runs-on: [self-hosted, rk3568-cross]`）。
- 想临时关掉那条 job：仓库变量 `CROSS_BUILD` 不设（或设成 `0`）—— job 的 `if` 看的就是它。
