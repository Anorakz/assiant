#!/usr/bin/env bash
# ============================================================================
#  image/sdk-make.sh — 在厂商 SDK 里跑 buildroot 的 make（T15-2-5 起的统一入口）
#
#  为什么需要它（不是多此一举）
#  ---------------------------------------------------------------------------
#  WSL 默认把 **Windows 的 PATH 接到 Linux PATH 后面**，里面有
#      /mnt/c/Program Files/...
#  这种**带空格**的条目。buildroot 在 `make` 的第一步就会跑
#  support/dependencies/dependencies.sh，它对 PATH 的检查很硬：
#  只要 PATH 里有带空格的条目，就直接
#      This doesn't work. Fix you PATH.
#      make: *** [support/dependencies/dependencies.mk:27: dependencies] Error 1
#  退出码 2 —— 看上去像 buildroot 坏了，其实跟 buildroot 毫无关系。
#  （亲测：从 Windows 这边 `wsl bash -lc 'make ...'` 必然踩到；
#    在 WSL 自己的交互 shell 里则取决于是否继承了 Windows PATH。）
#
#  这个脚本只做三件事：剔掉带 Windows 路径 / 带空格的 PATH 条目、
#  切到 SDK 的 buildroot 目录、把 O 与 -j 固定成我们这套配置。
#
#  用法
#  ---------------------------------------------------------------------------
#      bash image/sdk-make.sh <SDK 根目录> <make 目标> [额外 make 参数...]
#
#      # 例：
#      bash image/sdk-make.sh "$SDK" rockchip_rk3568_kickpi_k1mini_release_defconfig
#      bash image/sdk-make.sh "$SDK" rockchip-mali
#      bash image/sdk-make.sh "$SDK"            # 不给目标 = 整机构建
#
#  环境变量：IMG_CFG（默认 rockchip_rk3568_kickpi_k1mini_release）、IMG_JOBS（默认 8）
# ============================================================================
set -euo pipefail

SDK="${1:-}"
if [ -z "$SDK" ]; then
    echo "用法: bash image/sdk-make.sh <SDK 根目录> [make 目标] [额外 make 参数...]" >&2
    exit 2
fi
shift

CFG="${IMG_CFG:-rockchip_rk3568_kickpi_k1mini_release}"
JOBS="${IMG_JOBS:-8}"

if [ ! -d "$SDK/buildroot" ]; then
    echo "看起来不是 Rockchip SDK 根目录（缺 buildroot/）: $SDK" >&2
    exit 2
fi

# ---------------------------------------------------------------------------
#  PATH 清洗：去掉 /mnt/ 开头的（Windows 盘）与任何带空格的条目
# ---------------------------------------------------------------------------
CLEAN_PATH="$(printf '%s' "$PATH" | tr ':' '\n' | grep -v '^/mnt/' | grep -v '[[:space:]]' | paste -sd: -)"
if [ -z "$CLEAN_PATH" ]; then
    echo "!! PATH 清洗后为空，检查环境" >&2
    exit 2
fi
case "$CLEAN_PATH" in
    *" "*) echo "!! PATH 清洗后仍含空格: $CLEAN_PATH" >&2; exit 2 ;;
esac

cd "$SDK/buildroot"
echo "== sdk-make: cfg=$CFG jobs=$JOBS target=${*:-<整机>}"
echo "   PATH(清洗后)=$CLEAN_PATH"
exec env PATH="$CLEAN_PATH" make O="output/$CFG" -j"$JOBS" "$@"
