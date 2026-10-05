#!/bin/bash
# ============================================================================
#  scripts/cpu-sample.sh — 板上 CPU 占用表（只用 /proc，零外部依赖）
#
#  为什么不用 top/pidstat：发行镜像里那些工具不一定在（busybox 也没有 `top -H`）✗，
#  而 /proc/<pid>/stat 的 utime+stime 是**唯一永远都在**的度量 ✓。
#
#  用法（板上）：bash cpu-sample.sh [采样秒数，默认 10]
#  输出：① 场景（槽 / 模式 / 串流是否连着）② 系统总占用与每核
#        ③ 进程占用表（按 CPU 排序）④ agent / agent-gui 的**逐线程**占用表
#
#  ⚠ 百分比口径：**相对一个核**（100% = 占满 1 个 A55）✓，不是相对 4 核 ✗。
#  ⚠ `comm`（/proc/<pid>/comm）可能含空格 ⇒ 这里用 `|` 分隔并让 PID 做键 ✓；
#     /proc/<pid>/stat 的字段用 awk 取 14+15（comm 里的空格不影响 awk 的字段切分口径）✓。
# ============================================================================
set -u
DUR="${1:-10}"
HZ="$(getconf CLK_TCK 2>/dev/null || echo 100)"

snap_pids() {
    for d in /proc/[0-9]*; do
        [ -r "$d/stat" ] || continue
        t="$(awk '{print $14+$15}' "$d/stat" 2>/dev/null)" || continue
        [ -n "$t" ] || continue
        printf '%s|%s|%s\n' "${d#/proc/}" "$(cat "$d/comm" 2>/dev/null)" "$t"
    done
}

snap_tasks() {           # $1 = pid
    for d in /proc/$1/task/[0-9]*; do
        [ -r "$d/stat" ] || continue
        t="$(awk '{print $14+$15}' "$d/stat" 2>/dev/null)" || continue
        [ -n "$t" ] || continue
        printf '%s|%s|%s\n' "${d##*/}" "$(cat "$d/comm" 2>/dev/null)" "$t"
    done
}

table() {                # $1=t0 文件 $2=t1 文件 $3=表头
    echo "--- $3（% = 占满一个核的比例）---"
    awk -F'|' -v hz="$HZ" -v d="$DUR" '
        NR==FNR { a[$1]=$3; next }
        { delta = $3 - a[$1]; if (delta > 0) printf "  %6.1f%%  %-22s (pid %s)\n", delta/hz/d*100, $2, $1 }
    ' "$1" "$2" | sort -rn | head -22
}

echo "===== 场景 ====="
echo "  时间      : $(date '+%F %T')"
echo "  槽        : $(grep -o 'android_slotsufix=_[ab]' /proc/cmdline 2>/dev/null || echo '?')"
echo "  模式      : $(assistant status 2>/dev/null | grep -i -m1 -E 'mode|模式' || echo '（读不到）')"
echo "  串流连接  : $(ss -tn 2>/dev/null | grep -c '48010\|:48010' || echo 0) 条到 48010"
echo "  agent RSS : $(awk '/VmRSS/{print $2" kB"}' /proc/$(pgrep -f 'agent.main' | head -1)/status 2>/dev/null || echo '?')"
echo "  gui   RSS : $(awk '/VmRSS/{print $2" kB"}' /proc/$(pgrep -f agent_gui | head -1)/status 2>/dev/null || echo '?')"
echo "  线程数    : agent=$(ls -d /proc/$(pgrep -f 'agent.main' | head -1)/task 2>/dev/null | wc -l) gui=$(ls -d /proc/$(pgrep -f agent_gui | head -1)/task 2>/dev/null | wc -l)"

cpu_line() { awk -v t0="$1" -v t1="$2" -v hz="$HZ" -v d="$DUR" 'BEGIN{printf "  %6.1f%%  %s\n", (t1-t0)/hz/d*100, "总"}' ; }
read -r _ u0 n0 s0 i0 w0 q0 x0 y0 z0 < /proc/stat
sys0="$(awk '/^cpu /{print $2+$3+$4+$5+$6+$7+$8}' /proc/stat)"
cp0="$(awk '/^cpu[0-9]/{print $2+$3+$4+$5+$6+$7+$8}' /proc/stat | tr '\n' ' ')"

snap_pids > /tmp/cpu-p0.txt
A_PID="$(pgrep -f 'agent.main' | head -1)"; G_PID="$(pgrep -f agent_gui | head -1)"
[ -n "${A_PID:-}" ] && snap_tasks "$A_PID" > /tmp/cpu-a0.txt
[ -n "${G_PID:-}" ] && snap_tasks "$G_PID" > /tmp/cpu-g0.txt

sleep "$DUR"

snap_pids > /tmp/cpu-p1.txt
[ -n "${A_PID:-}" ] && snap_tasks "$A_PID" > /tmp/cpu-a1.txt
[ -n "${G_PID:-}" ] && snap_tasks "$G_PID" > /tmp/cpu-g1.txt
sys1="$(awk '/^cpu /{print $2+$3+$4+$5+$6+$7+$8}' /proc/stat)"
cp1="$(awk '/^cpu[0-9]/{print $2+$3+$4+$5+$6+$7+$8}' /proc/stat | tr '\n' ' ')"

echo
echo "===== 系统（采样 ${DUR}s）====="
cpu_line "$sys0" "$sys1" | sed 's/总/整机（100% = 1 个核，满 4 核 = 400%）/'
i=0
for v in $cp1; do
    o="$(echo $cp0 | cut -d' ' -f$((i+1)))"
    printf '  cpu%d: %5.1f%%\n' "$i" "$(awk -v a="$o" -v b="$v" -v hz="$HZ" -v d="$DUR" 'BEGIN{print (b-a)/hz/d*100}')"
    i=$((i+1))
done

echo
table /tmp/cpu-p0.txt /tmp/cpu-p1.txt "进程占用（top 22）"
echo
[ -s /tmp/cpu-a0.txt ] && table /tmp/cpu-a0.txt /tmp/cpu-a1.txt "agent 逐线程（top 22）"
echo
[ -s /tmp/cpu-g0.txt ] && table /tmp/cpu-g0.txt /tmp/cpu-g1.txt "agent-gui 逐线程（top 22）"
