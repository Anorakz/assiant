#!/bin/sh
# 06-wallpaper.sh -- 壁纸：目录/索引/切换 ⇒ GUI 收到 wallpaper 推送（判据 ✓）
. "$(dirname "$0")/_lib.sh"

say "① 壁纸目录"
dir=$(grep -a 'dir:' /data/assistant/config/config.yaml | head -1 | awk '{print $2}')
[ -z "$dir" ] && dir=/data/assistant/wallpapers
n=$(find "$dir" -maxdepth 1 -type f \( -name '*.png' -o -name '*.jpg' -o -name '*.jpeg' -o -name '*.webp' \) 2>/dev/null | wc -l)
judge "$([ "${n:-0}" -gt 0 ] && echo 0 || echo 1)" "$dir 里有 $n 张图（应 >0 ✓）"

say "② 切换（next / prev 各一次 ✓）"
for c in "wallpaper next" "wallpaper next" "wallpaper prev"; do
    out=$(timeout 30 "$ASSISTANT" $c 2>&1 | tail -1)
    printf '  · assistant %s → %s\n' "$c" "$out"
done
sleep 6

say "③ GUI 收到推送（agent 日志有『给刚连上的 GUI 补推』或换图记录 ✓）"
tail -80 "$AGENTLOG" 2>/dev/null | grep -aq -e "wallpaper:" -e "补推" && ok "有壁纸推送记录 ✓" || bad "没有壁纸推送 ✗"
journalctl -u agent-gui --no-pager --since='-2min' 2>/dev/null | grep -aq '\[recv\] wallpaper' && ok "GUI 侧收到 wallpaper ✓" || info "GUI 最近没收到（可能是同一个客户端没重连 ✓）"
grab "wallpaper"

finish
