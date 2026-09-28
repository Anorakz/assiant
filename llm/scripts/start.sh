#!/bin/bash
# llm/scripts/start.sh

# 注意：本脚本不使用 `set -e`。
# 所有外部命令的失败均已显式检查（source / mkdir / kill / nohup / 模型校验），
# 而 `set -e` 在 `cmd &` 后台启动等场景下行为容易出乎意料，反而掩盖真实错误。

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LLM_ROOT="$(dirname "$SCRIPT_DIR")"

# ---- 位置覆盖点（T15-2-7 新增，见 llm/README.md）----------------------------
#   LLM_ENV_FILE  : 派生文件 llm.env 的路径（Agent 写它）。镜像里它在 userdata 上，
#                   这样 A/B OTA 换掉 rootfs 不会把用户的模型参数冲回默认值。
#   LLM_STATE_DIR : run/ 与 logs/ 的根。同上，属于运行期状态，不该写进系统槽。
#   两者都不设时保持原行为（都留在 llm/ 目录内）—— 与板端手工投放那套完全一致。
LLM_ENV_FILE="${LLM_ENV_FILE:-${LLM_ROOT}/config/llm.env}"
LLM_STATE_DIR="${LLM_STATE_DIR:-$LLM_ROOT}"

if [ ! -f "$LLM_ENV_FILE" ]; then
    echo "[llm] 错误：找不到配置文件：$LLM_ENV_FILE"
    echo "[llm]       （它由 Agent 从 config/config.yaml 派生；先跑一次 Agent 或 assistant set --apply）"
    exit 1
fi

source "$LLM_ENV_FILE"
source "${SCRIPT_DIR}/lib.sh"

mkdir -p "${LLM_STATE_DIR}/${LLM_LOG_DIR}"
mkdir -p "${LLM_STATE_DIR}/${LLM_RUN_DIR}"

PID_FILE="${LLM_STATE_DIR}/${LLM_PID_FILE}"
LOG_FILE="${LLM_STATE_DIR}/${LLM_LOG_FILE}"

if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if is_llm_server_pid "$OLD_PID"; then
        echo "[llm] 已在运行，PID=$OLD_PID"
        exit 0
    fi
    echo "[llm] 清理残留 PID 文件"
    rm -f "$PID_FILE"
fi

if [ ! -f "$LLM_MODEL_PATH" ]; then
    echo "[llm] 错误：模型文件不存在：$LLM_MODEL_PATH"
    exit 1
fi

# llama-server 自己的库（libggml*/libllama*）就放在同目录 bin/ 下。
# 构建时已经给了 $ORIGIN 的 RPATH（image/build-llama.sh），这里再显式加一条
# LD_LIBRARY_PATH 作为兜底：目录被整体搬走、或换了一份没带 RPATH 的构建产物时，
# 不至于只报一句 "cannot open shared object file" 就完事。
export LD_LIBRARY_PATH="${LLM_ROOT}/bin${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

echo "[llm] 启动 llama-server..."
nohup "${LLM_ROOT}/bin/llama-server" \
    --model "$LLM_MODEL_PATH" \
    --alias "$LLM_MODEL_NAME" \
    --host "$LLM_HOST" \
    --port "$LLM_PORT" \
    --api-key "$LLM_API_KEY" \
    --ctx-size "$LLM_CTX_SIZE" \
    --batch-size "$LLM_BATCH_SIZE" \
    --threads "$LLM_THREADS" \
    --threads-batch "$LLM_THREADS_BATCH" \
    > "$LOG_FILE" 2>&1 &

echo $! > "$PID_FILE"
echo "[llm] 已启动，PID=$(cat $PID_FILE)"
echo "[llm] 日志：$LOG_FILE"
