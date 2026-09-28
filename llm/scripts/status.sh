#!/bin/bash
# llm/scripts/status.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LLM_ROOT="$(dirname "$SCRIPT_DIR")"

# 位置覆盖点（T15-2-7，与 start.sh 同一套；见 llm/README.md）
LLM_ENV_FILE="${LLM_ENV_FILE:-${LLM_ROOT}/config/llm.env}"
LLM_STATE_DIR="${LLM_STATE_DIR:-$LLM_ROOT}"

if [ ! -f "$LLM_ENV_FILE" ]; then
    echo "[llm] 未配置（没有 $LLM_ENV_FILE）"
    exit 0
fi

source "$LLM_ENV_FILE"
source "${SCRIPT_DIR}/lib.sh"

PID_FILE="${LLM_STATE_DIR}/${LLM_PID_FILE}"

if [ -f "$PID_FILE" ] && is_llm_server_pid "$(cat "$PID_FILE")"; then
    echo "[llm] 运行中，PID=$(cat "$PID_FILE")"
    echo -n "[llm] 健康检查："
    curl -s "http://127.0.0.1:${LLM_PORT}/health" && echo
else
    echo "[llm] 未运行"
fi
