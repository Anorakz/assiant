#!/bin/sh
# capture-all.sh -- **统一截图脚本**（板端）
#   一次跑完所有功能：摆状态 → 抓裸帧 → 写清单（PC 侧再转 PNG ✓，因为板端没有 PIL ✗）
#   用法：sh scripts/board/capture-all.sh [功能前缀…]
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
SHOTDIR="${SHOTDIR:-/data/assistant/shots}"
CAPTURE=1
export CAPTURE SHOTDIR
rm -f "$SHOTDIR"/*.raw 2>/dev/null || true

printf '########## 统一截图 @ %s ##########\n' "$(date '+%F %T')"
printf '裸帧目录: %s（每张 3 帧 × 800×1280×4 ✓）\n' "$SHOTDIR"

CAPTURE=1 sh "$HERE/run-all.sh" "$@"
rc=$?

printf '\n########## 清单（PC 侧 pull-shots.ps1 按它转 PNG ✓）##########\n'
manifest="$SHOTDIR/manifest.txt"
: > "$manifest"
for f in "$SHOTDIR"/*.raw; do
    [ -f "$f" ] || continue
    n=$(basename "$f")
    sz=$(wc -c < "$f")
    frames=$((sz / 4096000))
    printf '%s\t%s\t%s\n' "$n" "$sz" "$frames" | tee -a "$manifest"
done
printf '  清单: %s（%s 个）\n' "$manifest" "$(grep -c . "$manifest" 2>/dev/null || echo 0)"
printf '\n下一步（PC 上跑）：powershell -File scripts\\board\\pull-shots.ps1\n'
exit $rc
