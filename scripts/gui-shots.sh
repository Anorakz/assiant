#!/usr/bin/env bash
# ============================================================================
#  scripts/gui-shots.sh — 逐页对照图（T15-16 G-D-4）
#
#  为什么不用 `--screenshot-seq` ✗
#  ---------------------------------------------------------------------------
#   核实过：那个开关存在（main.cpp:93/146 ✓），但它出的是 **01_visible / 02_idle /
#   03_woke 三连图**（T3 的"唤醒"验收用 ✓），**不是**"四页对照" ✗。
#   四页对照要用的是 `--page <key>` + `--screenshot <png>` ✓（两者都真实存在 ✓）。
#
#  用法（**在板子上跑** ✓ —— 宿主没有 CJK 字体，中文会变成豆腐块 ✗）
#  ---------------------------------------------------------------------------
#      scripts/gui-shots.sh <输出目录> <标签> [二进制] [每页等待毫秒]
#      例：scripts/gui-shots.sh /data/shots after
#      例：scripts/gui-shots.sh /data/shots before /data/old-agent_gui 2500
#
#  ⚠ 关于"改前 / 改后"（**如实说明** ✗）
#  ---------------------------------------------------------------------------
#    · 本脚本**支持** before/after 两套命名与两份二进制 ✓ —— 但"改前"那份**得你自己有** ✗：
#      板子上现在跑的是**改后**的二进制 ✓，改前要旧镜像/旧二进制 ✓。
#    · 所以 G-D-4 的十六张图**不是**"跑一遍就有" ✗ —— 能出的是**改后四页** ✓。
# ============================================================================
set -u

OUT="${1:?用法: gui-shots.sh <输出目录> <标签> [二进制] [等待毫秒]}"
LABEL="${2:?缺标签（before / after）}"
BIN="${3:-/usr/lib/assistant/gui/agent_gui}"
DELAY="${4:-2000}"

# 页面 key 与文件名（与 main.cpp:143 的 --page 取值一致 ✓）
PAGES="home model system settings"

mkdir -p "$OUT"
echo "[shots] 二进制: $BIN"
echo "[shots] 标签: $LABEL   每页等待: ${DELAY}ms   输出: $OUT"

for page in $PAGES; do
    png="$OUT/${LABEL}-${page}.png"
    rm -f "$png"
    # ⚠ 必须给 cwd 与 --config ✓ —— 这个二进制在找不到 config 时会**静默退出** ✗
    #   （G-B-5 那轮实测过：不带 --config 时 rc=0 但一字不输出 ✓）
    ( cd /data/assistant 2>/dev/null || cd / ; \
      QT_QPA_PLATFORM=offscreen "$BIN" --config config/config.yaml \
        --page "$page" --screenshot "$png" --screenshot-delay "$DELAY" ) >/dev/null 2>&1
    if [ -s "$png" ]; then
        echo "[shots] ✓ $page -> $png ($(wc -c < "$png") 字节)"
    else
        echo "[shots] ✗ $page 没出图 —— 检查二进制 / --config / 平台插件" >&2
    fi
done

echo "[shots] 完成：$(ls -1 "$OUT"/${LABEL}-*.png 2>/dev/null | wc -l) 张（期望 $(echo $PAGES | wc -w) 张）"
