#!/bin/sh
# run-all.sh -- 统一入口：跑全部功能脚本，汇总 PASS/FAIL（可选 --capture 顺手抓帧 ✓）
#   用法（板端）：
#       sh scripts/board/run-all.sh                 # 只跑测试
#       CAPTURE=1 sh scripts/board/run-all.sh       # 跑测试 + 每个功能抓一组裸帧 ✓
#       sh scripts/board/run-all.sh 01 03           # 只跑指定编号 ✓
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
FEATURES="00-env 01-modes 02-chat 03-video 04-music 05-moonlight 06-wallpaper 07-gui-link 08-ota-slots"

pick=""
for a in "$@"; do pick="$pick $a"; done

printf '########## 板端功能自检 %s ##########\n' "$(date '+%F %T')"
[ "${CAPTURE:-0}" = "1" ] && printf '（CAPTURE=1 ⇒ 每个功能在出画面时会抓一组裸帧到 %s ✓）\n' "${SHOTDIR:-/data/assistant/shots}"

pass=0; fail=0; failed=""
for f in $FEATURES; do
    if [ -n "$pick" ]; then
        hit=0
        for p in $pick; do case "$f" in "$p"*) hit=1 ;; esac; done
        [ "$hit" = "1" ] || continue
    fi
    [ -f "$HERE/$f.sh" ] || { printf '\n!! 缺脚本 %s.sh\n' "$f"; fail=$((fail + 1)); failed="$failed $f"; continue; }
    printf '\n========================= %s =========================\n' "$f"
    if sh "$HERE/$f.sh"; then pass=$((pass + 1)); else fail=$((fail + 1)); failed="$failed $f"; fi
done

printf '\n########## 汇总 ##########\n'
printf '  通过 %d 个功能脚本 ｜ 失败 %d 个%s\n' "$pass" "$fail" "${failed:-}"
[ "$fail" -eq 0 ] && { printf '  == 全部通过 ✓\n'; exit 0; }
printf '  == 有失败项：%s ✗\n' "$failed"
exit 1
