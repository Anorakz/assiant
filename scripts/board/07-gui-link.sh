#!/bin/sh
# 07-gui-link.sh -- GUI 链路三态与 status 补推（T15-2-10e 修复的回归判据 ✓）
. "$(dirname "$_lib" )/_lib.sh" 2>/dev/null || . "$(dirname "$0")/_lib.sh"

say "① IPC socket 与客户端数"
[ -S /tmp/agent.sock ] && ok "/tmp/agent.sock ✓" || bad "socket 不在 ✗"
journalctl -u agent --no-pager --since='-30min' 2>/dev/null | grep -a 'GUI 已连接' | tail -1 | sed 's/^/  · /'

say "② 新客户端连上会补推 status（T15-2-10e ✓）"
if grep -aq 'runtime.on_status' /usr/lib/assistant/agent/ipc/__init__.py 2>/dev/null; then
    ok "镜像里已带 on_status 钩子 ✓（新代码 ✓）"
else
    bad "镜像里没有 on_status ⇒ 还是旧代码 ✗"
fi
grep -aq 'on_client_connect' /usr/lib/assistant/agent/ipc/__init__.py 2>/dev/null && ok "连上回调在位 ✓" || info "没找到连上回调 ✓"

say "③ GUI 现在的链路状态（应『已连接』✓；『主机』= moonlight ✓）"
last=$(journalctl -u agent-gui --no-pager --since='-30min' 2>/dev/null | grep -a '连接:' | tail -1)
printf '  · %s\n' "$(printf '%s' "$last" | cut -c1-180)"
printf '%s' "$last" | grep -aq '已连接' && ok "GUI 链路=已连接 ✓" || bad "GUI 没显示已连接 ✗"
printf '%s' "$last" | grep -aq '主机=已连接' && ok "串流主机=已连接 ✓" || info "主机=未知/未连接（moonlight 没连时正常 ✓）"

say "④ 强制补推一次（切模式会推 status ✓）"
n=$(mark_log)
"$ASSISTANT" mode GAME >/dev/null 2>&1; sleep 4
since_log "$n" "status" && ok "切换触发 status 推送 ✓" || bad "没推送 ✗"
journalctl -u agent-gui --no-pager --since='-1min' 2>/dev/null | grep -aq '\[recv\] status' && ok "GUI 侧收到 status ✓" || bad "GUI 没收到 status ✗"
"$ASSISTANT" mode IDLE >/dev/null 2>&1
grab "gui-link"

finish
