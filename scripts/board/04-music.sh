#!/bin/sh
# 04-music.sh -- 音乐：PC 侧 neteasecli 通道 ⇒ 播放 ⇒ 状态判据（含"洪流"回归检查 ✓）
. "$(dirname "$0")/_lib.sh"

pc_user=$(grep -a 'pc_user' /data/assistant/config/config.yaml | head -1 | awk '{print $2}')
pc_host=$(grep -a 'pc_host' /data/assistant/config/config.yaml | head -1 | awk '{print $2}')
say "① 板端→PC 免密（$pc_user@$pc_host ✓）"
if timeout 15 ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new \
        -p 22 "$pc_user@$pc_host" "echo pc-ok" 2>/dev/null | grep -aq pc-ok; then
    ok "免密通 ✓"
else
    bad "认证失败 ✗（docs/music.md §3.2.1：把 /data/assistant/keys/id_ed25519* 拷回 /root/.ssh/ ✓）"
fi

say "② PC 上 neteasecli 可用"
timeout 20 ssh -o BatchMode=yes -o ConnectTimeout=8 -p 22 "$pc_user@$pc_host" \
    "neteasecli --json player status" 2>/dev/null | head -c 200 | sed 's/^/  · /'

say "③ 洪流回归（最近 1 分钟『问状态失败』应为 0 ✓）"
n=$(journalctl -u agent --no-pager --since='-1min' 2>/dev/null | grep -ac '问状态失败')
judge "$([ "${n:-1}" -eq 0 ] && echo 0 || echo 1)" "最近 1 分钟『问状态失败』= $n（0 ✓）"

say "④ 播放（CLI 三个动作各一次 ✓）"
for cmd in "music play" "music next" "music pause"; do
    out=$(timeout 30 "$ASSISTANT" $cmd 2>&1 | tail -1)
    printf '  · assistant %s → %s\n' "$cmd" "$out"
done
sleep 8
grab "music-playing"

say "⑤ agent 日志里音乐在动（播放/入队/推送任一条 ✓）"
tail -60 "$AGENTLOG" 2>/dev/null | grep -aq -e "music:" -e "播放" && ok "有音乐相关日志 ✓" || bad "没有音乐日志 ✗"

finish
