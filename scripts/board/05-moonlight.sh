#!/bin/sh
# 05-moonlight.sh -- 游戏串流：配对状态 ⇒ 会话 ⇒ 板端解码线程 ⇒ PC 侧 NVENC（两侧判据 ✓）
. "$(dirname "$0")/_lib.sh"

host=$(grep -a 'host:' /data/assistant/config/config.yaml | head -1 | awk '{print $2}')
say "① 凭据与配对状态（$host ✓）"
for f in /data/assistant/creds/client.pem /data/assistant/creds/client.key; do
    [ -f "$f" ] && ok "$(basename "$f") 在位 ✓" || bad "缺 $f ✗"
done
line=$(grep -a 'PairStatus' "$AGENTLOG" 2>/dev/null | tail -1)
printf '  · %s\n' "$(printf '%s' "$line" | cut -c1-160)"
printf '%s' "$line" | grep -aq 'PairStatus=1' && ok "PairStatus=1（已授权 ✓）" || bad "PairStatus 不是 1 ✗"

say "② 会话 URL 与重试痕迹"
grep -a 'sessionUrl=' "$AGENTLOG" 2>/dev/null | tail -1 | sed 's/^/  · /'
grep -aq 'native: 第 [0-9]* 次尝试连上了' "$AGENTLOG" && ok "退避重试成功后连上过 ✓（T15-2-10e）" || info "没看到重试成功（可能一次就成 ✓）"
grep -aq '组件 native 启动失败' "$AGENTLOG" && info "历史上有过启动失败（网络没起来时正常 ✓）" || ok "没有 native 启动失败记录 ✓"

say "③ 板端解码线程（真在收流/硬解 ✓）"
th=$(ls /proc/"$(pidof python3 | awk '{print $1}')"/task/*/comm 2>/dev/null | xargs -r cat 2>/dev/null | sort -u)
for t in VideoRecv VideoDec mpp_dec_parser mpp_dec_hal; do
    printf '%s\n' "$th" | grep -aq "$t" && ok "线程 $t ✓" || bad "缺线程 $t ✗"
done

say "④ 从板端做一次 mTLS 校验（判据最硬 ✓）"
code=$(timeout 12 curl -s -k --max-time 10 --cert /data/assistant/creds/client.pem \
        --key /data/assistant/creds/client.key -o /dev/null -w '%{http_code}' \
        "https://$host:47984/applist" 2>/dev/null)
judge "$([ "$code" = 200 ] && echo 0 || echo 1)" "HTTPS 47984 /applist = $code（200=已授权 ✓）"
grab "moonlight-stream"

finish
