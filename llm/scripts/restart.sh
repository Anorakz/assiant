#!/bin/bash
# llm/scripts/restart.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

"${SCRIPT_DIR}/stop.sh" || {
    echo "[llm] 错误：停止失败，已中止重启"
    exit 1
}
sleep 1
"${SCRIPT_DIR}/start.sh"
