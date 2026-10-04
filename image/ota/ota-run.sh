#!/bin/sh
# ota-run.sh — 板端 OTA 的**薄壳**（T15-14）
#
# 真正的实现分两半，都不在这里：
#   · 算：`agent/core/ota.py`（清单 / sha256 / 槽守卫 / 写盘计划 / 目标 BCB，纯函数 ✓）
#   · 做：`image/ota/ota-apply.py`（备份 misc → dd 非当前槽 → 整块写 misc → 可选重启 ✓）
#
# 本脚本只做四件事：找 python、把参数原样递过去、**透传退出码**、把用法印出来。
# 之所以还留着它：板上/文档里"一条命令"的入口要稳定，内部实现已经换过两轮 ✓。
#
# 用法:
#   ota-run.sh <包文件> --sha256 <期望哈希> [--images <放 boot.img/rootfs.img 的目录>]
#              [--package-file <清单>] [--dry-run] [--reboot]
#
# 退出码：0 成功；2 用法/读取问题；3 环境不对；4 被安全规则拒绝；5 写盘失败。
#
# 注：第一版这里直接调 `rkupdate`（并带"当前槽守卫 + sha256"）—— 实测那条路走不通 ✗
#     （rkupdate 是 USB/NAND 时代工具，要 CRKUsbComm 与 /dev/rkflash0，见 §7.5 ✓），
#     所以落盘改成 dd，守卫与校验上移到 `agent/core/ota.py`（有单测 ✓）。
set -u

HERE=$(dirname "$0")
for cand in "$HERE" /usr/lib/assistant/image/ota /usr/lib/assistant; do
    if [ -f "$cand/ota-apply.py" ]; then
        SCRIPT="$cand/ota-apply.py"
        break
    fi
done

if [ -z "${SCRIPT:-}" ]; then
    echo "!! 找不到 ota-apply.py（应随镜像装在 /usr/lib/assistant/image/ota/）" >&2
    exit 3
fi

if [ "$#" -eq 0 ]; then
    sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
fi

PY=python3
command -v "$PY" >/dev/null 2>&1 || PY=/usr/bin/python3

exec "$PY" "$SCRIPT" "$@"
