#!/bin/bash
# llm/scripts/stop.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LLM_ROOT="$(dirname "$SCRIPT_DIR")"

# 位置覆盖点（T15-2-7，与 start.sh 同一套；见 llm/README.md）
LLM_ENV_FILE="${LLM_ENV_FILE:-${LLM_ROOT}/config/llm.env}"
LLM_STATE_DIR="${LLM_STATE_DIR:-$LLM_ROOT}"

if [ ! -f "$LLM_ENV_FILE" ]; then
    echo "[llm] 未运行（没有配置文件 $LLM_ENV_FILE，也就没有 PID 文件）"
    exit 0
fi

source "$LLM_ENV_FILE"
source "${SCRIPT_DIR}/lib.sh"

PID_FILE="${LLM_STATE_DIR}/${LLM_PID_FILE}"

if [ ! -f "$PID_FILE" ]; then
    echo "[llm] 未运行（无 PID 文件）"
    exit 0
fi

PID=$(cat "$PID_FILE")

# PID 身份校验：PID 可能已被系统复用给其它进程，避免误杀无关进程。
if process_alive "$PID" && ! is_llm_server_pid "$PID"; then
    echo "[llm] 警告：PID=$PID 已存在但并非 llama-server，判定为残留 PID 文件，不发送信号"
    rm -f "$PID_FILE"
    echo "[llm] 已停止"
    exit 0
fi

if [ -n "$PID" ] && process_alive "$PID"; then
    echo "[llm] 停止 PID=$PID"
    kill "$PID"
    if wait_gone "$PID" 10; then
        : # 已优雅退出
    else
        echo "[llm] 强制终止"
        kill -9 "$PID"
        wait_gone "$PID" 5 || echo "[llm] 警告：PID=$PID 仍未退出（可能为僵尸进程）"
    fi
fi

rm -f "$PID_FILE"
echo "[llm] 已停止"
