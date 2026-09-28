#!/bin/bash
# llm/scripts/lib.sh
#
# start.sh / stop.sh / restart.sh / status.sh 共用的辅助函数。
# 单独抽出是为了避免在 4 个脚本里重复同一段 PID 处理逻辑。
#
# ⚠ 来源与改动（T15-2-7）：这几个脚本原先**不在仓库里**，只存在于板端
#   /home/kickpi/myproject/assitant/llm/（当初手工投放）。T15-2-7 把它们收进仓库，
#   并做了**两处**必要改动，见 llm/README.md：
#     1) 配置/状态位置可被环境变量覆盖（LLM_ENV_FILE / LLM_STATE_DIR）——
#        代码进 rootfs、可写状态进 userdata；
#     2) 判断 PID 身份改用 /proc/<pid>/cmdline，不再依赖 `ps -o args=`
#        （镜像里只有 busybox 的 ps，`-o args=` 不保证可用）。

# 取进程命令行：用 /proc 而不是 ps。
# 为什么不用 `ps -p PID -o args=`：镜像里没有 procps，busybox 的 ps 对 `-o args=`
# 支持不保证；而 /proc/<pid>/cmdline 在任何 Linux 上都在，且不受 PATH 影响。
_process_cmdline() {
    local pid="$1"
    [ -r "/proc/${pid}/cmdline" ] || return 1
    tr '\0' ' ' < "/proc/${pid}/cmdline"
}

# 判断 PID 是否为存活的 llama-server 进程。
# 用法: is_llm_server_pid <pid>
# 返回: 0=是存活的 llama-server  1=不是
is_llm_server_pid() {
    local pid="$1"
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    local cmd
    cmd="$(_process_cmdline "$pid")" || return 1
    case "$cmd" in
        *llama-server*) return 0 ;;
        *)              return 1 ;;
    esac
}

# 判断 PID 是否存活（不校验进程身份）。
# 用法: process_alive <pid>
process_alive() {
    local pid="$1"
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null
}

# 等待进程退出，最多等待 timeout 秒。
# 用法: wait_gone <pid> [timeout]
# 返回: 0=已退出  1=超时仍存活
wait_gone() {
    local pid="$1" timeout="${2:-10}" i
    for ((i = 0; i < timeout; i++)); do
        process_alive "$pid" || return 0
        sleep 1
    done
    process_alive "$pid" && return 1
    return 0
}
