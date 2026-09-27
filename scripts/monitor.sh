#!/bin/bash
# ============================================================================
#  scripts/monitor.sh — 板端运行时监控（T14-10）
#
#  它回答的问题与 health_check.sh **不一样**，别混：
#      health_check.sh  = "部署对不对"（native 能 import 吗 / 配置在不在 / 与清单一致吗）
#      monitor.sh       = "**跑起来之后**机器什么状态"（CPU / 内存 / NPU / 温度 / 各进程 RSS）
#  所以这里刻意**不重复**部署一致性那套；要看那个请跑 `--health`（本脚本会转调它）。
#
#  跑法
#  ---------------------------------------------------------------------------
#      bash scripts/monitor.sh                 # 打印一段人看的快照
#      bash scripts/monitor.sh --csv           # 一行 CSV（配 --header 先打表头）
#      bash scripts/monitor.sh --json          # 一行 JSON
#      bash scripts/monitor.sh --watch 5 --csv --header > /tmp/mon.csv   # 每 5 秒一行
#      bash scripts/monitor.sh --count 3 --csv --header   # 采 3 次就退出（脚本里用）
#      bash scripts/monitor.sh --health        # 顺手跑一次 health_check.sh
#
#  它读什么（全是内核接口，不装任何东西）
#  ---------------------------------------------------------------------------
#      CPU      /proc/stat（两次采样取差算占用）+ /proc/loadavg
#      内存     /proc/meminfo（总量 / 可用 / 已用）
#      进程 RSS /proc/<pid>/status 的 VmRSS（按进程名累加，可 --procs 改）
#      NPU      /sys/class/devfreq/fde40000.npu 的 cur_freq/load
#               + /sys/kernel/debug/rknpu/load（有就给真实利用率，没有就打 -）
#      温度     /sys/class/thermal/thermal_zone*/（soc / gpu）
#      帧计数   **Agent 目前没有对外暴露**（环境里找不到现成计数器）——
#               所以那两列在没有数据源时打 `-`，不编数字。等 Agent 侧有了计数器面
#               （IPC 或小文件），把 --frames-file 指过去即可，脚本不用改。
#
#  为什么用 bash 而不是 python
#  ---------------------------------------------------------------------------
#      监控脚本要能在"Agent 挂了 / python 环境坏了"的时候照样跑起来 —— 它依赖越少越好。
#      （真需要交叉分析（比如画图）再把 CSV 丢给 PC。）
#
#  退出码: 0 = 采到并打印了; 1 = 参数错 / 必需的路径读不到
# ============================================================================
set -u

PROC_ROOT="/proc"
SYS_ROOT="/sys"
DEBUGFS_ROOT="/sys/kernel/debug"
CSV=0
JSON=0
HEADER=0
WATCH=0
INTERVAL=5
COUNT=1
PROCS="agent.main agent_gui llama-server moonlight"
FRAMES_FILE=""
RUN_HEALTH=0
NPU_DEVFREQ="/sys/class/devfreq/fde40000.npu"
NPU_DEBUGFS="/sys/kernel/debug/rknpu/load"

usage() {
    sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        # ⚠ 每个分支都要 shift：漏了就是死循环（T14-10 实测：`--csv --json` 直接挂住，
        #   是 tests/test_monitor_sh.py 用 30 秒超时抓出来的）
        --csv) CSV=1; shift ;;
        --json) JSON=1; shift ;;
        --header) HEADER=1; shift ;;
        --watch) WATCH=1; shift; case "${1:-}" in [0-9]*) INTERVAL="$1"; shift ;; esac ;;
        --interval) shift; INTERVAL="${1:-5}"; shift ;;
        --count) shift; COUNT="${1:-1}"; shift ;;
        --procs) shift; PROCS="${1:-}"; shift ;;
        --frames-file) shift; FRAMES_FILE="${1:-}"; shift ;;
        --proc-root) shift; PROC_ROOT="${1:-/proc}"; shift ;;
        --sysfs-root) shift; SYS_ROOT="${1:-/sys}"; shift ;;
        --debugfs-root) shift; DEBUGFS_ROOT="${1:-/sys/kernel/debug}"; shift ;;
        --health) RUN_HEALTH=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "monitor.sh: 不认识的参数 $1" >&2; usage >&2; exit 1 ;;
    esac
done
[ "$CSV" = 1 ] && [ "$JSON" = 1 ] && { echo "monitor.sh: --csv 与 --json 只能选一个" >&2; exit 1; }

# ---------------------------------------------------------------------------
#  读一个文件的第一行（读不到就空，绝不报错刷屏）
# ---------------------------------------------------------------------------
read_first() { [ -r "$1" ] && head -1 "$1" 2>/dev/null | tr -d '\n' || true; }

#: /proc/stat 的 cpu 行 -> 总(jiffies) 与 空闲(idle+iowait)
cpu_total_idle() {
    local line
    line="$(grep -m1 '^cpu ' "$PROC_ROOT/stat" 2>/dev/null || true)"
    [ -z "$line" ] && { echo "0 0"; return; }
    echo "$line" | awk '{idle=$5+$6; total=0; for(i=2;i<=NF;i++) total+=$i; print total, idle}'
}

cpu_percent() {
    local t1 i1 t2 i2 first second
    read -r t1 i1 <<EOF
$(cpu_total_idle)
EOF
    sleep "${SAMPLE_GAP:-0.5}"
    read -r t2 i2 <<EOF
$(cpu_total_idle)
EOF
    awk -v t1="$t1" -v i1="$i1" -v t2="$t2" -v i2="$i2" 'BEGIN{
        dt=t2-t1; di=i2-i1;
        if (dt<=0) { printf "-"; exit }
        printf "%.1f", (dt-di)*100.0/dt
    }'
}

mem_field() {                       # mem_field MemTotal -> kB
    awk -v key="$1:" '$1==key {print $2; found=1} END{if(!found) print "-"}' \
        "$PROC_ROOT/meminfo" 2>/dev/null || echo "-"
}

num() {                             # JSON 里不能用 "-"（不是合法数字）→ 缺失打 -1
    case "${1:--}" in ''|-) echo "-1" ;; *) echo "$1" ;; esac
}

mb() {                              # kB -> MB（保留一位）: mb 3994992
    awk -v kb="${1:--}" 'BEGIN{ if (kb=="-"||kb=="") {print "-"; exit} printf "%.0f", kb/1024 }'
}

rss_kb() {                          # 按进程名累加 VmRSS（kB）
    local total=0 pid rss
    for pid in $(pgrep -f "$1" 2>/dev/null || true); do
        rss="$(awk '/^VmRSS/{print $2}' "/proc/$pid/status" 2>/dev/null || true)"
        [ -n "$rss" ] && total=$((total + rss))
    done
    echo "$total"
}

npu_freq_mhz() {
    local raw
    raw="$(read_first "$SYS_ROOT/class/devfreq/fde40000.npu/cur_freq")"
    [ -z "$raw" ] && raw="$(read_first "$SYS_ROOT/class/devfreq/$(basename "$NPU_DEVFREQ")/cur_freq")"
    [ -z "$raw" ] && { echo "-"; return; }
    awk -v hz="$raw" 'BEGIN{printf "%.0f", hz/1000000}'
}

npu_load() {                        # 优先 debugfs 的真实利用率，否则用 devfreq 的 load
    local raw
    raw="$(read_first "$DEBUGFS_ROOT/rknpu/load")"
    if [ -n "$raw" ]; then
        # 板端实测两种格式都要认：`NPU load:  0%`（单值）与
        # `NPU load:  Core0: 12%, Core1: 30%`（多核）—— 多个就取平均。
        # ⚠ 别写成"打印 $i"：`NPU load: 0%` 会被前一个字段 `load:` 命中（T14-10 实测踩到）。
        echo "$raw" | awk '{
            n=0; sum=0;
            for (i=1; i<=NF; i++) {
                if ($i ~ /%/) { v=$i; gsub(/[%,]/,"",v); if (v ~ /^[0-9.]+$/) { sum+=v; n++ } }
            }
            if (n==0) { print "-" } else { printf "%.0f", sum/n }
        }'
        return
    fi
    raw="$(read_first "$SYS_ROOT/class/devfreq/fde40000.npu/load")"
    [ -z "$raw" ] && { echo "-"; return; }
    echo "$raw" | awk -F'@' '{print $1}'          # "100@600000000Hz" -> 100
}

temp_c() {                          # temp_c soc -> 33.8（找不到打 -）
    local zone type
    for zone in "$SYS_ROOT"/class/thermal/thermal_zone*; do
        [ -r "$zone/type" ] || continue
        type="$(read_first "$zone/type")"
        case "$type" in
            *"$1"*)
                awk -v t="$(read_first "$zone/temp")" 'BEGIN{
                    if (t=="" ) {print "-"; exit}
                    if (t > 1000) printf "%.1f", t/1000; else printf "%.1f", t
                }'
                return ;;
        esac
    done
    echo "-"
}

frames_pair() {                     # 帧计数：Agent 目前没有对外暴露 → "- -"
    if [ -n "$FRAMES_FILE" ] && [ -r "$FRAMES_FILE" ]; then
        awk -F'[ ,]+' '/frames/{for(i=1;i<=NF;i++){if($i ~ /^ok=/){ok=$i} if($i ~ /^dropped=/){d=$i}}}
             END{gsub("ok=","",ok); gsub("dropped=","",d); print (ok==""?"-":ok), (d==""?"-":d)}' \
            "$FRAMES_FILE"
        return
    fi
    echo "- -"
}

# ---------------------------------------------------------------------------
#  采一次样
# ---------------------------------------------------------------------------
sample() {
    local ts uptime_s load cpu
    local mt ma mu
    local ar gr lr
    local nf nl tsoc tgpu fok fdrop
    ts="$(date '+%Y-%m-%d %H:%M:%S')"
    uptime_s="$(awk '{printf "%.0f", $1}' "$PROC_ROOT/uptime" 2>/dev/null || echo '-')"
    load="$(awk '{print $1","$2","$3}' "$PROC_ROOT/loadavg" 2>/dev/null || echo '-,-,-')"
    cpu="$(cpu_percent)"
    mt="$(mem_field MemTotal)"; ma="$(mem_field MemAvailable)"
    if [ "$mt" != "-" ] && [ "$ma" != "-" ]; then mu=$((mt - ma)); else mu="-"; fi
    ar="$(rss_kb agent.main)"; gr="$(rss_kb agent_gui)"; lr="$(rss_kb llama-server)"
    nf="$(npu_freq_mhz)"; nl="$(npu_load)"
    tsoc="$(temp_c soc)"; tgpu="$(temp_c gpu)"
    read -r fok fdrop <<EOF
$(frames_pair)
EOF

    if [ "$JSON" = 1 ]; then
        printf '{"ts":"%s","uptime_s":%s,"load1":%s,"load5":%s,"load15":%s,"cpu_pct":%s,' \
            "$ts" "$uptime_s" "${load%%,*}" "$(echo "$load" | cut -d, -f2)" \
            "$(echo "$load" | cut -d, -f3)" "$(num "$cpu")"
        printf '"mem_total_mb":%s,"mem_used_mb":%s,"mem_avail_mb":%s,' \
            "$(num "$(mb "$mt")")" "$(num "$(mb "$mu")")" "$(num "$(mb "$ma")")"
        printf '"agent_rss_mb":%s,"gui_rss_mb":%s,"llama_rss_mb":%s,' \
            "$(num "$(mb "$ar")")" "$(num "$(mb "$gr")")" "$(num "$(mb "$lr")")"
        printf '"npu_freq_mhz":%s,"npu_load_pct":%s,"temp_soc_c":%s,"temp_gpu_c":%s,' \
            "$(num "$nf")" "$(num "$nl")" "$(num "$tsoc")" "$(num "$tgpu")"
        printf '"frames_ok":%s,"frames_dropped":%s}\n' "$(num "$fok")" "$(num "$fdrop")"
        return
    fi

    if [ "$CSV" = 1 ]; then
        echo "$ts,$uptime_s,${load},${cpu},$(mb "$mt"),$(mb "$mu"),$(mb "$ma"),$(mb "$ar"),$(mb "$gr"),$(mb "$lr"),${nf},${nl},${tsoc},${tgpu},${fok},${fdrop}"
        return
    fi

    cat <<EOF
时间        $ts（开机 ${uptime_s}s）
CPU         占用 ${cpu}%   负载 ${load}
内存        总 $(mb "$mt") MB / 已用 $(mb "$mu") MB / 可用 $(mb "$ma") MB
进程 RSS    agent $(mb "$ar") MB   gui $(mb "$gr") MB   llama-server $(mb "$lr") MB
NPU         ${nf} MHz   负载 ${nl}%
温度        soc ${tsoc}°C   gpu ${tgpu}°C
帧计数      ok=${fok} dropped=${fdrop}（Agent 还没暴露计数器时是 -）
EOF
}

csv_header() {
    echo "ts,uptime_s,load1,load5,load15,cpu_pct,mem_total_mb,mem_used_mb,mem_avail_mb,agent_rss_mb,gui_rss_mb,llama_rss_mb,npu_freq_mhz,npu_load_pct,temp_soc_c,temp_gpu_c,frames_ok,frames_dropped"
}

# ---------------------------------------------------------------------------
#  主流程
# ---------------------------------------------------------------------------
[ -r "$PROC_ROOT/stat" ] || { echo "monitor.sh: 读不到 $PROC_ROOT/stat —— --proc-root 给对了吗？" >&2; exit 1; }

if [ "$HEADER" = 1 ]; then
    if [ "$CSV" = 1 ]; then csv_header; else echo "(表头只对 --csv 有意义)"; fi
fi

if [ "$RUN_HEALTH" = 1 ]; then
    here="$(cd "$(dirname "$0")" && pwd)"
    if [ -x "$here/health_check.sh" ] || [ -r "$here/health_check.sh" ]; then
        echo "--- health_check.sh（部署一致性；与上面的运行时快照互补）---"
        sh "$here/health_check.sh" || true
        echo "--- 下面是运行时快照 ---"
    else
        echo "monitor.sh: 找不到 $here/health_check.sh（--health 跳过）" >&2
    fi
fi

i=0
while :; do
    sample
    i=$((i + 1))
    [ "$COUNT" != "0" ] && [ "$i" -ge "$COUNT" ] && break
    [ "$WATCH" = 1 ] || break
    sleep "$INTERVAL"
done
