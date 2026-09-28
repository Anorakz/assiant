#!/usr/bin/env bash
# ============================================================================
#  image/install-into-sdk.sh — 把本仓库的镜像配方**注入**厂商 SDK（T15-2）
#
#  为什么要有这个脚本
#  ---------------------------------------------------------------------------
#  配方（buildroot 片段 / defconfig / 板级 defconfig）由**我们的仓库**维护，
#  厂商 SDK 只是一棵被注入的树：
#    · 好处 1：配方能 code review、能回滚、能进 CI（docs/image.md 引用它）；
#    · 好处 2：SDK 里"哪些是厂商的、哪些是我们加的"一眼可辨（不散落在各处改动里）；
#    · 好处 3：换 SDK 版本时重跑这个脚本即可，不用手工再点一遍。
#
#  用法
#  ---------------------------------------------------------------------------
#      bash image/install-into-sdk.sh <SDK 根目录> [--dry-run]
#
#      # 例（WSL）：
#      bash image/install-into-sdk.sh \
#        /home/anorak/rk3568_buildroot/linux-kernel-6.1/rk-linux6.1-2026060914/rk-linux6.1-2026060914
#
#  它只做"复制我们的文件进去"，**不改** SDK 的任何既有文件（同路径覆盖 = 我们故意的，
#  会打印 COVER 提示）。重复执行是幂等的。
# ============================================================================
set -euo pipefail

SDK="${1:-}"
DRY=""
if [ "${2:-}" = "--dry-run" ]; then
    DRY="1"
fi

if [ -z "$SDK" ]; then
    echo "用法: bash image/install-into-sdk.sh <SDK 根目录> [--dry-run]" >&2
    exit 2
fi
if [ ! -d "$SDK/buildroot" ] || [ ! -d "$SDK/device/rockchip" ]; then
    echo "看起来不是 Rockchip SDK 根目录（缺 buildroot/ 或 device/rockchip/）: $SDK" >&2
    exit 2
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 源（本仓库）→ 目标（SDK）相对路径
FILES="
buildroot/configs/rockchip_rk3568_kickpi_k1mini_release_defconfig
buildroot/configs/rockchip/products/kickpi-k1mini-release.config
device/rockchip/.chips/rk3566_rk3568/rockchip_rk3568_kickpi_k1mini_release_defconfig
"

echo "== 注入配方到 SDK: $SDK"
for rel in $FILES; do
    src="$HERE/$rel"
    dst="$SDK/$rel"
    if [ ! -f "$src" ]; then
        echo "!! 源文件不在: $src" >&2
        exit 1
    fi
    if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
        echo "   = 已经一致: $rel"
        continue
    fi
    if [ -f "$dst" ]; then
        echo "   ~ 覆盖: $rel"
    else
        echo "   + 新增: $rel"
    fi
    if [ -z "$DRY" ]; then
        mkdir -p "$(dirname "$dst")"
        cp "$src" "$dst"
    fi
done

echo "== 完成。下一步（在 SDK 根目录）："
echo "     cd buildroot && make O=output/rockchip_rk3568_kickpi_k1mini_release \\"
echo "          rockchip_rk3568_kickpi_k1mini_release_defconfig"
echo "   （整机构建走 lunch：./build.sh rk3566_rk3568:rockchip_rk3568_kickpi_k1mini_release_defconfig）"
