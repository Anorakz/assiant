# llm/ — 板端 LLM 运行时（llama.cpp / llama-server）

这个目录是**板端本地推理**那一层：`edge` 模式的 Agent 不加载模型，它连的是本机
`llama-server` 的 OpenAI 兼容接口。真正的模型加载、上下文、线程参数都在这里。

```
llm/
├─ bin/            llama-server 及其 ggml/llama 动态库（**不进仓库**，由镜像构建产出，见下）
├─ scripts/        start / stop / restart / status / lib —— Agent 用它启停服务
└─ config/
    └─ llm.env     **派生文件**：由 Agent 从 config/config.yaml 的 llm: 段生成
                   （见 agent/core/llm_env.py），不是配置来源
```

## ⚠ 来源与改动（T15-2-7）

`scripts/` 与 `config/llm.env` 这**原先都不在仓库里**：板端
`/home/kickpi/myproject/assitant/llm/` 是当初**手工投放**的（`docs/image.md` §2.4
把它记作"手工投放"），仓库里既没有跟踪、也没有本地副本 —— 也就是说，光看仓库
复现不出板端正在跑的那套脚本。T15-2-7 把它们收进仓库（脚本 4 个 + lib），
并做了**两处**必要改动：

1. **配置与状态位置可被环境变量覆盖**（`LLM_ENV_FILE` / `LLM_STATE_DIR`）。
   原脚本把 `config/llm.env`、`run/`、`logs/` 都放在 `llm/` 自己目录下
   （靠 `LLM_ROOT="$(dirname "$SCRIPT_DIR")"`）。镜像里代码在 rootfs（A/B 系统槽，
   OTA 会整槽换掉），而这三样都是**运行期状态**：派生出来的模型参数、PID 文件、
   日志。写进系统槽的后果很具体 —— 每次 OTA 都把用户的模型参数冲回默认、日志丢光、
   PID 文件残留。所以镜像里把状态指到 userdata：
   ```
   LLM_ENV_FILE=/data/assistant/llm/config/llm.env
   LLM_STATE_DIR=/data/assistant/llm
   ```
   **不设这两个变量时行为与板端那一版完全一致**（都留在 `llm/` 内），
   所以板端现有那套（`/home/kickpi/...`）不受影响。
2. **PID 身份判断改用 `/proc/<pid>/cmdline`**（原来用 `ps -p PID -o args=`）。
   镜像里没有 procps，只有 busybox 的 ps，`-o args=` 不保证可用；而
   `/proc` 在任何 Linux 上都在，且不看 PATH。判断逻辑本身没变：
   命令行里含 `llama-server` 才认（防止 PID 被复用后误杀无关进程）。

`llm/bin/` **不入库**：它是 llama.cpp 的构建产物（`llama-server` + 8 个
`libggml*/libllama*` 库，共 16 MB）。镜像里的那份由 `image/build-llama.sh`
**交叉编译**产出，钉在板端验证过的那个 commit 上：

| 项 | 值 |
| --- | --- |
| 上游 | `github.com/ggml-org/llama.cpp` |
| commit | `b387ddfd8`（板端 `git describe` = `b10675-2-gb387ddfd8`，即 build 10677） |
| 构建参数 | 与板端那次一致：`BUILD_SHARED_LIBS=ON`、`Release (-O3)`、`GGML_OPENMP=ON`、`GGML_CPU_REPACK=ON`、`LLAMA_CURL=OFF`、关闭 tests/examples |
| 交叉编译差异 | 板端是 `GGML_NATIVE=ON`（本机 A55 调优）；交叉编译必须 `GGML_NATIVE=OFF` 并显式给 `GGML_CPU_ARM_ARCH=armv8.2-a+dotprod+fp16`（RK3568 的 A55 支持 dotprod + fp16） |

## 模型

模型**不在仓库、也不在 rootfs**：4.9 GB 走 userdata 分区（`docs/image.md` §7 的
取舍：OTA 只该动系统槽，模型和配置留在共享分区，不必每次重下）。镜像里的约定路径是
`/data/model/`，`llm.env` 的 `LLM_MODEL_PATH` 指过去（由 Agent 从 config.yaml 派生）。

## 跑起来

```bash
# 板端/镜像里（Agent 通常自己调这两条；LLM_ENV_FILE/LLM_STATE_DIR 由 unit 给）
llm/scripts/start.sh          # 起 llama-server（读 config/llm.env）
llm/scripts/status.sh         # PID + /health
llm/scripts/stop.sh           # 优雅停，超时强杀
```

`requirements.txt` 是 `llm/llm_client.py` 那个 SDK 的依赖（`openai` / `httpx`），
**不是 Agent 主链路的**：Agent 自己用 `agent/llm/provider.py`（openai 是懒加载的
可选项）。镜像里没装它，因为主链路不需要。
