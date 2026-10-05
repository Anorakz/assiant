#!/bin/bash
# ============================================================================
#  scripts/gui-probe.sh — GUI 性能统一探针（T15-16 / G-B-0）
#
#  为什么要有它：G-B 那 7 项（音乐条每秒 setStyleSheet、tintedIcon 重染、
#  日程全量重建、系统页定时器、壁纸淡入、键盘让位 …）都要求"**改前改后同一口径的数字**"✓，
#  否则就是"感觉更流畅了" ✗ —— 那种结论在本项目不算数 ✓。
#
#  口径（三条，都能在板上跑到 ✓）
#  ---------------------------------------------------------------------------
#    · **CPU**：cgroup v2 的 `/sys/fs/cgroup/system.slice/agent-gui.service/cpu.stat`
#      里的 `usage_usec`（**累计微秒** ✓）⇒ 取前后差值 ✓。它是**内核记账**，不靠采样频率 ✓，
#      比 `top` 稳 ✓，也不受"采样本身占 CPU"影响 ✓。百分比口径 = **占满一个核 = 100%** ✓。
#    · **内存**：`/proc/<pid>/status` 的 `VmRSS`（当前 ✓）与 **`VmHWM`（峰值 ✓）** ——
#      峰值是"水位"的正解 ✓（内核自己维护 ✓，不靠我们采样 ✓）。
#    · **计数**（可选）：`--marks <文件>` 时数该文件里 `GUI-MARK` 出现次数 ✓ ——
#      G-B-1 那类"每秒少做 N 次"的优化就靠它给证据 ✓。
#
#  用法
#  ---------------------------------------------------------------------------
#      bash gui-probe.sh [采样秒数=30] [标签] [--marks <文件>]
#  输出：一行机器可读（便于贴进 todo2.md / 对比表 ✓）+ 一行人类可读摘要 ✓
#
#  ⚠ 不用 `perf`/`pidstat`/`mpstat`：镜像里**没有**它们 ✗（实测过 ✓）。
#  ⚠ GUI 的 PID **不要**用 `pgrep -f agent/main.py` 那种写法 ✗ —— unit 里是
#    `ExecStart=/usr/lib/assistant/gui/agent_gui` ⇒ 用 `systemctl show -p MainPID` ✓
#    （agent 侧同理：`-m agent.main`，cmdline 里根本没有 "agent/main.py" 这个串 ✗）。
# ============================================================================
set -u

DUR="${1:-30}"
LABEL="${2:-run}"
MARKS=""
if [ "${3:-}" = "--marks" ] && [ -n "${4:-}" ]; then
    MARKS="$4"
fi

CPU_STAT=/sys/fs/cgroup/system.slice/agent-gui.service/cpu.stat
pid_of_gui() {
    local pid
    pid="$(systemctl show -p MainPID --value agent-gui.service 2>/dev/null || true)"
    case "$pid" in
        ''|0|*[!0-9]*) pid="$(pgrep -x agent_gui | head -1)" ;;
    esac
    printf '%s' "$pid"
}

ctxt_switches() {   # $1 = pid ：自愿上下文切换（= "醒来等事件"的次数 ✓）
    awk '/^voluntary_ctxt_switches/{print $2}' "/proc/$1/status" 2>/dev/null || printf '0'
}

usage_usec() {
    [ -r "$CPU_STAT" ] || { printf '0'; return; }
    awk '/^usage_usec/{print $2}' "$CPU_STAT" 2>/dev/null || printf '0'
}

vmsize() {   # $1 = pid ; $2 = 字段名（VmRSS / VmHWM）
    awk -v k="$2" '$1==k":"{print $2}' "/proc/$1/status" 2>/dev/null || printf '0'
}

PID="$(pid_of_gui)"
[ -n "$PID" ] || { echo "!! 找不到 agent-gui 的 PID —— 探针中止 ✗"; exit 2; }

U0="$(usage_usec)"
W0="$(ctxt_switches "$PID")"
sleep "$DUR"
U1="$(usage_usec)"
W1="$(ctxt_switches "$PID")"
PID2="$(pid_of_gui)"
[ -n "$PID2" ] && PID="$PID2"          # 采样期间重启过就取新的 ✓（并在标签里提一句）

DELTA="$((U1 - U0))"
CPU_PCT="$(awk -v d="$DELTA" -v s="$DUR" 'BEGIN{printf "%.2f", d/1000000.0/s*100.0}')"
RSS="$(vmsize "$PID" VmRSS)"
HWM="$(vmsize "$PID" VmHWM)"
MARKS_N="0"
if [ -n "$MARKS" ] && [ -r "$MARKS" ]; then
    MARKS_N="$(grep -c 'GUI-MARK' "$MARKS" 2>/dev/null || printf '0')"
fi

WAKE_S="$(awk -v a="$W0" -v b="$W1" -v s="$DUR" 'BEGIN{printf "%.2f", (b-a)/s}')"

echo "PROBE label=$LABEL secs=$DUR cpu_pct=$CPU_PCT wakeups_s=$WAKE_S rss_kb=${RSS:-0} hwm_kb=${HWM:-0} marks=$MARKS_N pid=$PID"
echo "   $LABEL：CPU **${CPU_PCT}%**（一个核 = 100%）· 自愿唤醒 **${WAKE_S}/s** · RSS ${RSS:-0} kB · 峰值 ${HWM:-0} kB · 计数 ${MARKS_N}（采样 ${DUR}s）"
