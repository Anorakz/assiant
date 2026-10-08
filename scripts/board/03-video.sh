#!/bin/sh
# 03-video.sh -- B 站视频：搜索 ⇒ 队列 ⇒ 起播 ⇒ **零拷贝**帧率判据（T15-2-10 的那条 ✓）
. "$(dirname "$0")/_lib.sh"

say "先确保有队列（没有就搜一次 ✓）"
if "$ASSISTANT" video next 2>&1 | grep -aq "队列是空的"; then
    info "队列空 ⇒ 用对话搜索一次"
    timeout 280 "$ASSISTANT" chat --timeout 260 "搜索 罗刹海市" >/dev/null 2>&1
fi

say "起播（等『可以播了』✓）"
n=$(mark_log)
"$ASSISTANT" mode GAME >/dev/null 2>&1; sleep 4
"$ASSISTANT" video next 2>&1 | tail -1
i=0
while [ "$i" -lt 30 ]; do
    i=$((i + 1))
    since_log "$n" "可以播了" && { ok "可以播了 ✓（等 $((i * 5)) 秒）"; break; }
    sleep 5
done
[ "$i" -lt 30 ] || bad "60 秒内没等到『可以播了』✗"

say "零拷贝判据（等 50 秒取一次绘制帧率 ✓）"
sleep 50
line=$(journalctl -u agent-gui --no-pager --since='-2min' 2>/dev/null | grep -a '绘制帧率' | tail -1)
printf '  · %s\n' "$(printf '%s' "$line" | cut -c1-200)"
fps=$(printf '%s' "$line" | sed -n 's/.*fps= "\([0-9.]*\)".*/\1/p')
gap=$(printf '%s' "$line" | sed -n 's/.*平均画帧间隔(ms)= "\([0-9.]*\)".*/\1/p')
zero=$(printf '%s' "$line" | grep -c '走零拷贝' 2>/dev/null || true)
journalctl -u agent-gui --no-pager --since='-3min' 2>/dev/null | grep -aq "走零拷贝主路径" && ok "走了零拷贝主路径 ✓" || bad "没看到零拷贝主路径 ✗"
awk -v f="${fps:-0}" 'BEGIN { exit !(f >= 20) }' && ok "绘制帧率 $fps fps ≥ 20 ✓" || bad "帧率偏低：$fps ✗"
awk -v g="${gap:-999}" 'BEGIN { exit !(g <= 60) }' && ok "平均画帧间隔 ${gap} ms ≤ 60 ✓" || bad "画帧间隔偏大：${gap} ms ✗"
[ "$(pgrep -c vqueue 2>/dev/null || echo 0)" = "0" ] && ok "vqueue=0（零拷贝，无中间进程 ✓）" || bad "vqueue 进程非 0 ✗"
journalctl -u agent-gui -b --no-pager 2>/dev/null | grep -aq "page flip" && bad "有 page flip 抢屏错误 ✗" || ok "无 page flip 抢屏错误 ✓"
grab "video-zerocopy"

finish
