#!/bin/sh
# 08-ota-slots.sh -- OTA / A-B 槽：状态、确认、tries 倒计时（两次 fastboot 事故的回归判据 ✓）
. "$(dirname "$0")/_lib.sh"

say "① 槽与 misc 元数据"
slot=$(tr ' ' '\n' < /proc/cmdline | grep slotsufix | cut -d= -f2)
info "当前槽: $slot"
raw=$(dd if=/dev/mmcblk0p2 bs=1 skip=2048 count=20 2>/dev/null | od -An -tx1 | tr -s ' ' | tr -d '\n')
info "misc: $raw"
prio_a=$(printf '%s' "$raw" | awk '{print $9}'); tries_a=$(printf '%s' "$raw" | awk '{print $10}'); succ_a=$(printf '%s' "$raw" | awk '{print $11}')
prio_b=$(printf '%s' "$raw" | awk '{print $13}'); tries_b=$(printf '%s' "$raw" | awk '{print $14}'); succ_b=$(printf '%s' "$raw" | awk '{print $15}')
info "A: prio=$prio_a tries=$tries_a succ=$succ_a ｜ B: prio=$prio_b tries=$tries_b succ=$succ_b"
[ "$prio_a" != "00" ] || [ "$prio_b" != "00" ] && ok "至少一个槽可用 ✓" || bad "两个槽 prio 都为 0 ⇒ 会掉 fastboot ✗"
if [ "$slot" = "_a" ]; then s=$succ_a; else s=$succ_b; fi
[ "$s" = "01" ] && ok "当前槽已确认成功（succ=1 ✓、tries 不再递减 ✓）" || bad "当前槽 succ≠1 ⇒ tries 会一直扣 ⇒ 有掉 fastboot 风险 ✗"

say "② ota_state（协议推送 ✓）"
timeout 20 "$ASSISTANT" status --timeout 8 2>&1 | head -4 | sed 's/^/  · /'

say "③ ota-confirm 的判据是否健康（socket 要在 ✓）"
if [ -S /tmp/agent.sock ]; then ok "/tmp/agent.sock 在位 ⇒ confirm 不会因缺 socket 判失败 ✓"; else bad "socket 不在 ⇒ confirm 必然失败 ✗"; fi
journalctl -u assistant-ota-confirm -b --no-pager 2>/dev/null | tail -4 | sed 's/^/  · /'
if systemctl is-active --quiet assistant-ota-confirm 2>/dev/null; then ok "ota-confirm active ✓"
elif systemctl show assistant-ota-confirm -p Result --value 2>/dev/null | grep -aq success; then ok "ota-confirm 跑过且结果 success ✓"
else bad "ota-confirm 结果不是 success ✗（看上面日志 ✓）"; fi

say "④ 手动确认一次（幂等 ✓，把当前槽标成功）"
systemctl restart assistant-ota-confirm 2>/dev/null; sleep 12
raw2=$(dd if=/dev/mmcblk0p2 bs=1 skip=2048 count=20 2>/dev/null | od -An -tx1 | tr -s ' ' | tr -d '\n')
info "确认后 misc: $raw2"
if [ "$slot" = "_a" ]; then s2=$(printf '%s' "$raw2" | awk '{print $11}'); else s2=$(printf '%s' "$raw2" | awk '{print $15}'); fi
[ "$s2" = "01" ] && ok "确认后当前槽 succ=1 ✓" || bad "确认后仍不是 1 ✗"
grab "ota-slots"

finish
