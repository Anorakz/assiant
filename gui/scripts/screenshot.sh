#!/bin/bash
# ============================================================================
#  gui/scripts/screenshot.sh — T1 验收用的出图工具
#
#  两种模式
#  ---------------------------------------------------------------------------
#    grab  (默认)  用 --screenshot：GUI 内 QWidget::grab() 渲染成 PNG 后自己退出。
#                   确定性最好，不受窗口管理器/其它窗口干扰，逐页出图用这个。
#    --scrot       真机屏幕截图：起窗口 -> 等 3s -> scrot 抓整屏 -> 收工。
#                   用来证明"确实显示在板上那块 1280x800 面板上"。
#
#  用法
#  ---------------------------------------------------------------------------
#      gui/scripts/screenshot.sh --page home                  # → <repo>/temp/home.png
#      gui/scripts/screenshot.sh --page system --scrot        # 真机整屏，同样落 temp/
#      gui/scripts/screenshot.sh --page home --out /tmp/x.png # 也可以自己指定
#
#  默认输出目录：<repo>/temp/  （验收证据统一放这里，由用户指定）
# ============================================================================
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BIN="$ROOT/gui/build/agent_gui"

export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-/var/run/slim.auth}"

page=home
out=""
mode=grab
delay=900

while [ $# -gt 0 ]; do
    case "$1" in
        --page)  page="$2"; shift 2 ;;
        --out)   out="$2";  shift 2 ;;
        --delay) delay="$2"; shift 2 ;;
        --scrot) mode=scrot; shift ;;
        -h|--help)
            grep '^#' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "未知参数: $1" >&2; exit 2 ;;
    esac
done

if [ -z "$out" ]; then
    if [ "$mode" = scrot ]; then
        out="$ROOT/temp/$page.scrot.png"
    else
        out="$ROOT/temp/$page.png"
    fi
fi
mkdir -p "$(dirname "$out")"

if [ ! -x "$BIN" ]; then
    echo "找不到可执行文件: $BIN" >&2
    echo "先构建: cmake -S gui -B gui/build && cmake --build gui/build -j4" >&2
    exit 1
fi

rm -f "$out"

if [ "$mode" = grab ]; then
    "$BIN" --windowed --page "$page" --screenshot "$out" --screenshot-delay "$delay"
    rc=$?
    if [ -s "$out" ]; then
        echo "OK  page=$page  $out  ($(stat -c%s "$out") 字节)"
    else
        echo "FAIL page=$page 没生成 $out (rc=$rc)" >&2
        exit 1
    fi
    exit "$rc"
fi

# ---- scrot 模式：窗口留在屏幕上，抓整屏 ----
"$BIN" --windowed --page "$page" >/tmp/gui_scrot_run.log 2>&1 &
pid=$!
sleep 3
if scrot -o "$out" && [ -s "$out" ]; then
    echo "OK  page=$page  $out  ($(stat -c%s "$out") 字节, scrot 整屏)"
    rc=0
else
    echo "FAIL scrot 抓图失败（检查 DISPLAY/XAUTHORITY）" >&2
    rc=1
fi
kill "$pid" 2>/dev/null
wait "$pid" 2>/dev/null
exit "$rc"
