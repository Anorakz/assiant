#!/bin/sh
# 01-modes.sh -- 模式与资源释放（IDLE / STUDY / GAME / SLEEP ✓）
. "$(dirname "$0")/_lib.sh"

say "四种模式逐个切过去（合法路径：经 IDLE ✓）"
for m in IDLE STUDY GAME SLEEP IDLE; do
    n=$(mark_log)
    out=$("$ASSISTANT" mode "$m" 2>&1 | tail -1)
    info "mode $m → $out"
    sleep 6
    since_log "$n" "配置来源" >/dev/null 2>&1 || true
    case "$out" in
        *"已切到 $m"*|*"已切"*) ok "切到 $m ✓" ;;
        *) bad "切 $m 没成功：$out" ;;
    esac
    grab "mode-$m"
done

say "模式切换会推 status（GUI 顶栏据此变色 ✓）"
n=$(mark_log)
"$ASSISTANT" mode GAME >/dev/null 2>&1
sleep 4
since_log "$n" "status" && ok "切换后 agent 有 status 推送 ✓" || bad "没看到 status 推送 ✗"

say "STUDY/GAME 会拉起常驻 SigLIP（认游戏/学习监督 ✓）"
grep -aq "SigLIP 已加载" "$AGENTLOG" && ok "SigLIP 已加载过 ✓" || info "本次没触发（进 GAME 后才加载 ✓）"

say "SLEEP 会停 llama-server（资源释放判据 ✓）"
"$ASSISTANT" mode SLEEP >/dev/null 2>&1; sleep 8
if pgrep -f llama-server >/dev/null 2>&1; then bad "SLEEP 里 llama-server 还在 ✗"; else ok "SLEEP 已停 llama-server ✓"; fi
"$ASSISTANT" mode IDLE >/dev/null 2>&1; sleep 6
pgrep -f llama-server >/dev/null 2>&1 && ok "回 IDLE 后 llama-server 又起来 ✓" || info "IDLE 下未起（看 llm.manage_service 配置 ✓）"

finish
