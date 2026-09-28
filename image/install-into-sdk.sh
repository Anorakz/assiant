#!/usr/bin/env bash
# ============================================================================
#  image/install-into-sdk.sh — 把本仓库的镜像配方**注入**厂商 SDK（T15-2）
#
#  为什么要有这个脚本
#  ---------------------------------------------------------------------------
#  配方（buildroot 片段 / defconfig / 板级 defconfig / 内核 dts·dtsi）由**我们的仓库**
#  维护，厂商 SDK 只是一棵被注入的树：
#    · 好处 1：配方能 code review、能回滚、能进 CI（docs/image.md 引用它）；
#    · 好处 2：SDK 里"哪些是厂商的、哪些是我们加的"一眼可辨；
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
#  只做"复制我们的文件进去"：目标已存在且内容不同 = 覆盖（打印 ~ 覆盖）；
#  内容相同 = 跳过（= 已一致）。重复执行幂等。
#  ⚠ 我们的内核 dts/dtsi 一律用 **-assistant 后缀的新文件名**，不覆盖厂商同名文件。
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
DTS_DIR="kernel-6.1/arch/arm64/boot/dts/rockchip"
FAILED=0

# install_file <本仓库相对路径> <SDK 相对路径>
install_file() {
    local src="$HERE/../$1"
    local dst="$SDK/$2"
    if [ ! -f "$src" ]; then
        echo "!! 源文件不在: $src" >&2
        FAILED=1
        return
    fi
    if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
        echo "   = 已经一致: $2"
        return
    fi
    if [ -f "$dst" ]; then
        echo "   ~ 覆盖: $2"
    else
        echo "   + 新增: $2"
    fi
    if [ -z "$DRY" ]; then
        mkdir -p "$(dirname "$dst")"
        cp "$src" "$dst"
    fi
}

echo "== 注入配方到 SDK: $SDK"
# buildroot：defconfig + 我们自己的片段
install_file image/buildroot/configs/rockchip_rk3568_kickpi_k1mini_release_defconfig \
             buildroot/configs/rockchip_rk3568_kickpi_k1mini_release_defconfig
install_file image/buildroot/configs/rockchip/products/kickpi-k1mini-release.config \
             buildroot/configs/rockchip/products/kickpi-k1mini-release.config
# SDK 板级 defconfig（lunch 用）+ 我们的 A/B 分区表
install_file image/device/rockchip/.chips/rk3566_rk3568/rockchip_rk3568_kickpi_k1mini_release_defconfig \
             device/rockchip/.chips/rk3566_rk3568/rockchip_rk3568_kickpi_k1mini_release_defconfig
install_file image/device/rockchip/.chips/rk3566_rk3568/parameter-assistant-ab.txt \
             device/rockchip/.chips/rk3566_rk3568/parameter-assistant-ab.txt
# 内核：我们的板级 dts/dtsi（全部 -assistant 后缀，不动厂商同名文件）
install_file image/kernel/rk3568-kickpi-k1Mini-assistant.dts "$DTS_DIR/rk3568-kickpi-k1Mini-assistant.dts"
install_file image/kernel/rk3568-kickpi-k1Mini-assistant.dtsi "$DTS_DIR/rk3568-kickpi-k1Mini-assistant.dtsi"
install_file image/kernel/rk3568-kickpi-assistant-overrides.dtsi \
             "$DTS_DIR/rk3568-kickpi-assistant-overrides.dtsi"

if [ "$FAILED" != "0" ]; then
    echo "!! 有源文件缺失，注入不完整" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
#  唯一一个"二进制输入"：G52 的预编译 Mali blob（T15-2-5）
#  ---------------------------------------------------------------------------
#  它**不进我们的仓库**（56 MB，git 里不合适），而是用脚本从 SDK 自带的厂商 deb 里
#  取出来、按 buildroot 期望的文件名放进 external/libmali。脚本对 blob 做指纹
#  （deb 与 .so 两级 sha256）与能力（SONAME / gbm_* 符号 / DT_NEEDED）校验，
#  所以这一步失败一定是"东西不对"，不是"路径顺手写错了"。
echo
if [ -n "$DRY" ]; then
    bash "$HERE/prepare-libmali.sh" "$SDK" --dry-run
else
    bash "$HERE/prepare-libmali.sh" "$SDK"
fi

echo "== 完成。下一步（在 SDK 根目录）："
echo "     cd buildroot && make O=output/rockchip_rk3568_kickpi_k1mini_release \\"
echo "          rockchip_rk3568_kickpi_k1mini_release_defconfig"
echo "   （整机构建走 lunch：./build.sh rk3566_rk3568:rockchip_rk3568_kickpi_k1mini_release_defconfig）"
