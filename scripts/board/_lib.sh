#!/bin/sh
# scripts/board/_lib.sh -- 板端功能脚本的共享库（判据/日志/抓帧 ✓）
#   用法： . "$(dirname "$0")/_lib.sh"
#   约定：每个功能脚本自己 judge 出 PASS/FAIL，退出码 0=通过、1=失败 ✓；
#         设 CAPTURE=1 时，在**该功能出画面那一刻**调 grab raw，供统一截图脚本回收 ✓。
set -u
ASSISTANT="${ASSISTANT:-assistant}"
LOGDIR="${LOGDIR:-/data/assistant/logs}"
AGENTLOG="$LOGDIR/agent.log"
SHOTDIR="${SHOTDIR:-/data/assistant/shots}"
FEATURE="${FEATURE:-$(basename "$0" .sh)}"
mkdir -p "$SHOTDIR" 2>/dev/null || true

_n_pass=0
_n_fail=0

say()  { printf '\n=== %s\n' "$*"; }
ok()   { _n_pass=$((_n_pass + 1)); printf '  [PASS] %s\n' "$*"; }
bad()  { _n_fail=$((_n_fail + 1)); printf '  [FAIL] %s\n' "$*"; }
info() { printf '  · %s\n' "$*"; }

# judge <判定表达式结果 0/1> <说明>
judge() { if [ "$1" = "0" ]; then ok "$2"; else bad "$2"; fi; }

# 期望字符串出现在文件里： have <文件> <模式>
have() { grep -aq -- "$2" "$1" 2>/dev/null; }

# 在日志里等一条新行出现（带超时）：wait_log <模式> <秒>
wait_log() {
    pat="$1"; limit="${2:-60}"; i=0
    while [ "$i" -lt "$limit" ]; do
        i=$((i + 1))
        grep -aq -- "$pat" "$AGENTLOG" 2>/dev/null && return 0
        sleep 1
    done
    return 1
}

# 记下行数（抓"之后新增"的判据）：mark_log -> 数字
mark_log() { wc -l < "$AGENTLOG" 2>/dev/null | tr -d ' '; }
# 自 <行号> 起是否出现过某模式：since_log <行号> <模式>
since_log() { tail -n +"$(( $1 + 1 ))" "$AGENTLOG" 2>/dev/null | grep -aq -- "$2"; }

svc() { systemctl is-active "$1" 2>/dev/null; }

# 抓一帧（kmssrc 裸帧，3 帧 ✓）：grab <名字>
grab() {
    name="$1"
    [ "${CAPTURE:-0}" = "1" ] || return 0
    f="$SHOTDIR/${FEATURE}-${name}.raw"
    rm -f "$f"
    timeout 25 gst-launch-1.0 -q kmssrc num-buffers=3 ! videoconvert ! filesink location="$f" >/dev/null 2>&1
    sz=$(wc -c < "$f" 2>/dev/null || echo 0)
    if [ "${sz:-0}" -gt 0 ]; then info "抓帧 $name：$((sz / 4096000)) 帧 ✓"; else bad "抓帧 $name 失败 ✗"; fi
}

# 收尾：打印小结并返回退出码 ✓（统一入口据此汇总 ✓）
finish() {
    printf '\n-- %s：通过 %d / 失败 %d\n' "$FEATURE" "$_n_pass" "$_n_fail"
    [ "$_n_fail" -eq 0 ]
}
