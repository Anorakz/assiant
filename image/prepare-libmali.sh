#!/usr/bin/env bash
# ============================================================================
#  image/prepare-libmali.sh — 把厂商的 G52 预编译 blob 放到 buildroot 期望的位置
#  （T15-2-5）
#
#  为什么需要这一步
#  ---------------------------------------------------------------------------
#  我们选了 rockchip-mali 这个包（gpu/mali 片段给了 ROCKCHIP_MALI=y），
#  K1 Mini 是 **Bifrost G52**，但 SDK 自带的 external/libmali 里**只有
#  valhall-g610（RK3588）的 blob** —— 没有 bifrost-g52 的任何 .so，
#  所以 `make rockchip-mali` 会在 meson 阶段直接
#      ERROR: Failed to find matched library
#  死掉。G52 的 blob 厂商**放在 SDK 里了**，只是放在 deb 包（Ubuntu rootfs 那套）
#  里，而不是放在 buildroot 会去找的 external/libmali/lib 下。
#  本脚本就干这一件事：从那个 deb 里把 .so 取出来、按 buildroot 期望的**文件名**
#  放到 external/libmali/lib/aarch64-linux-gnu/。这是唯一"配方之外"的二进制输入，
#  所以脚本对它做完整的指纹与符号校验，而不是"复制过去就算完"。
#
#  期望的文件名是怎么算出来的（不是猜的）
#  ---------------------------------------------------------------------------
#  buildroot/package/rockchip/rockchip-mali/rockchip-mali.mk 把
#      -Dgpu=<GPU> -Dversion=<VERSION> -Dsubversion=<SUB_VERSION> -Dplatform=<PLATFORM>
#  （取值来自 rockchip-mali/Config.in）交给 external/libmali/meson.build，
#  meson 再调 scripts/grabber.sh 去 find：
#      optimize_<O>/<arch>*/libmali-<gpu>-<version>[-<subversion>]-<platform>.so
#  对我们这棵树（已实测 .config）：
#      GPU=bifrost-g52       （Config.in 第 55 行）
#      VERSION=g24p0         （第 65 行）
#      SUB_VERSION=""        （bifrost 没有默认值 → 空；grabber 里 ${4:-none} 会当 none 处理）
#      OPTIMIZE=O3           → 目录 optimize_3（本树里 optimize_3 就是 lib 的软链）
#      PLATFORM=x11-wayland-gbm（见下）
#  ⇒ 文件名 = libmali-bifrost-g52-g24p0-x11-wayland-gbm.so
#  这**正好是厂商自己的命名**：同一个 SDK 里
#      ubuntu/packages/arm64/libmali/libmali-bifrost-g52-g24p0-x11-wayland-gbm_1.9-1_arm64.deb
#  以及 lib/ 下已有的 valhall 同规格文件（libmali-valhall-g610-g24p0-x11-wayland-gbm.so）
#  都是这个格式。脚本最后会用厂商自带的 scripts/parse_name.sh --format 把文件名
#  反解回原名来证明"我们没写错名字"。
#
#  PLATFORM 为什么必须是 x11-wayland-gbm（而不是 gbm）
#  ---------------------------------------------------------------------------
#  rockchip-mali.mk 是按**已启用的 winsys**拼 PLATFORM 的：HAS_X11 → x11、
#  HAS_WAYLAND → wayland、HAS_GBM → gbm（HAS_OPENCL 关着才会多一个 nocl）。
#  而 blob 自己**硬依赖** X11/Wayland 的客户端库：
#      readelf -d 里 DT_NEEDED 有
#        libdrm.so.2 libwayland-client.so.0 libwayland-server.so.0
#        libX11.so.6 libX11-xcb.so.1 libxcb.so.1 libxcb-dri2.so.0
#        libxcb-dri3.so.0 libxcb-xfixes.so.0 libxcb-present.so.0
#  这是"整库加载"级别的依赖：ld.so 载入 libmali.so 时这些必须都在，
#  否则 EGLFS 连 EGL 都拿不到。所以镜像里**必须**带这些客户端库
#  （注意：只是客户端库，不需要 X server / 桌面，路线 C 不冲突）。
#  meson.build 也会按 blob 里的 "libxcb.so"/"libwayland-client.so" 字符串判定
#  has_x11/has_wayland，然后 dependency() 这些包 —— 不启用就连配置阶段都过不去。
#  结论：products 片段里开 XORG7 + WAYLAND，文件名用 x11-wayland-gbm。
#
#  用法
#  ---------------------------------------------------------------------------
#      bash image/prepare-libmali.sh <SDK 根目录> [--deb <deb 路径>] [--dry-run]
#
#  幂等：目标已存在且 sha256 一致 = "已经一致"，什么都不做。
# ============================================================================
set -euo pipefail

SDK="${1:-}"
shift || true
DRY=""
DEB_OVERRIDE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY="1" ;;
        --deb)     DEB_OVERRIDE="${2:-}"; shift ;;
        *) echo "未知参数: $1" >&2; exit 2 ;;
    esac
    shift
done

if [ -z "$SDK" ]; then
    echo "用法: bash image/prepare-libmali.sh <SDK 根目录> [--deb <deb 路径>] [--dry-run]" >&2
    exit 2
fi
if [ ! -d "$SDK/buildroot" ] || [ ! -d "$SDK/external/libmali" ]; then
    echo "看起来不是 Rockchip SDK 根目录（缺 buildroot/ 或 external/libmali/）: $SDK" >&2
    exit 2
fi

# ---------------------------------------------------------------------------
#  钉死的期望值：换 SDK / 换 deb 时这里的 sha256 会变，脚本会明确报错让你复核，
#  而不是悄悄装一个来路不明的 blob。
# ---------------------------------------------------------------------------
GPU="bifrost-g52"
VERSION="g24p0"
PLATFORM="x11-wayland-gbm"
BLOB_NAME="libmali-${GPU}-${VERSION}-${PLATFORM}.so"
BLOB_REL="external/libmali/lib/aarch64-linux-gnu/${BLOB_NAME}"
EXPECT_SO_SHA256="e208194f2ec03a35f15fce3141ccfa2fac954feebe59c368fb6e9b82f32f8bdf"
EXPECT_SO_SIZE="56387136"
EXPECT_DEB_SHA256="c8707755f9e3734c051cd6b71b63214290e70cc653eea7de799e013fe67098de"
DEFAULT_DEB_REL="ubuntu/packages/arm64/libmali/libmali-${GPU}-${VERSION}-${PLATFORM}_1.9-1_arm64.deb"
MIN_GBM_SYMBOLS=30

DEB="${DEB_OVERRIDE:-$SDK/$DEFAULT_DEB_REL}"
DEST="$SDK/$BLOB_REL"

echo "== T15-2-5 libmali blob 准备"
echo "   SDK      : $SDK"
echo "   期望文件名: $BLOB_NAME"
echo "   平台串   : $PLATFORM （由 HAS_X11 + HAS_WAYLAND + HAS_OPENCL + HAS_GBM 拼出）"

for tool in dpkg-deb readelf sha256sum; do
    command -v "$tool" >/dev/null 2>&1 || { echo "!! 缺工具: $tool" >&2; exit 2; }
done

if [ ! -f "$DEB" ]; then
    echo "!! 找不到厂商 deb: $DEB" >&2
    echo "   默认位置: <SDK>/$DEFAULT_DEB_REL" >&2
    echo "   可用 --deb <路径> 指定别处的那份。" >&2
    exit 2
fi

echo "== 1/5 校验 deb 指纹"
deb_sha=$(sha256sum "$DEB" | cut -d' ' -f1)
echo "   $DEB"
echo "   sha256 $deb_sha"
if [ "$deb_sha" != "$EXPECT_DEB_SHA256" ]; then
    echo "   ! 与脚本里钉死的 sha256 不一致（期望 $EXPECT_DEB_SHA256）" >&2
    echo "     如果 SDK 换版本了，请复核这个 blob 之后更新脚本里的常量。" >&2
    exit 1
fi
echo "   = deb 指纹一致"

echo "== 2/5 解包并取出 libmali.so.1.9.0"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
dpkg-deb -x "$DEB" "$TMP"
SRC_LIB="$TMP/usr/lib/aarch64-linux-gnu/libmali.so.1.9.0"
if [ ! -f "$SRC_LIB" ]; then
    echo "!! 解包后没找到 usr/lib/aarch64-linux-gnu/libmali.so.1.9.0" >&2
    find "$TMP" -name 'libmali*' >&2
    exit 1
fi
src_size=$(stat -c %s "$SRC_LIB")
src_sha=$(sha256sum "$SRC_LIB" | cut -d' ' -f1)
echo "   大小   $src_size"
echo "   sha256 $src_sha"
[ "$src_size" = "$EXPECT_SO_SIZE" ] || { echo "!! 大小与期望 $EXPECT_SO_SIZE 不一致" >&2; exit 1; }
[ "$src_sha" = "$EXPECT_SO_SHA256" ] || { echo "!! sha256 与期望不一致" >&2; exit 1; }

echo "== 3/5 自检 blob 能力（SONAME / gbm 符号 / DT_NEEDED）"
soname=$(readelf -d "$SRC_LIB" | sed -n 's/.*SONAME.*\[\(.*\)\].*/\1/p')
gbm_n=$(readelf --dyn-syms -W "$SRC_LIB" | grep -c ' gbm_' || true)
echo "   SONAME = $soname"
echo "   gbm_*  = $gbm_n （EGLFS 走 eglfs_kms 需要 >= $MIN_GBM_SYMBOLS）"
[ "$soname" = "libmali.so.1" ] || { echo "!! SONAME 不是 libmali.so.1" >&2; exit 1; }
[ "$gbm_n" -ge "$MIN_GBM_SYMBOLS" ] || { echo "!! gbm_* 符号不足" >&2; exit 1; }
echo "   DT_NEEDED（X11/Wayland 客户端库是硬依赖，镜像里必须有）:"
readelf -d "$SRC_LIB" | sed -n 's/.*NEEDED.*\[\(.*\)\].*/     \1/p'

echo "== 4/5 安装到 external/libmali/lib/aarch64-linux-gnu/"
if [ -f "$DEST" ] && [ "$(sha256sum "$DEST" | cut -d' ' -f1)" = "$EXPECT_SO_SHA256" ]; then
    echo "   = 已经一致: $BLOB_REL"
elif [ -f "$DEST" ]; then
    echo "   ~ 覆盖: $BLOB_REL"
    [ -n "$DRY" ] || cp -f "$SRC_LIB" "$DEST"
else
    echo "   + 新增: $BLOB_REL"
    [ -n "$DRY" ] || { mkdir -p "$(dirname "$DEST")"; cp -f "$SRC_LIB" "$DEST"; }
fi
if [ -z "$DRY" ]; then
    chmod 0755 "$DEST"
fi

echo "== 5/5 用厂商自己的脚本证明这个文件名是对的"
if [ -n "$DRY" ] && [ ! -f "$DEST" ]; then
    echo "   (dry-run：目标还没真的放进去，跳过 grabber 校验)"
else
    grabber_out=$(cd "$SDK/external/libmali" && scripts/grabber.sh aarch64 "$GPU" "$VERSION" "" "$PLATFORM" O3)
    echo "   grabber.sh aarch64 $GPU $VERSION '' $PLATFORM O3"
    echo "     -> ${grabber_out:-<空>}"
    case "$grabber_out" in
        *"$BLOB_NAME"*) echo "     = buildroot 找的就是这个文件" ;;
        *) echo "!! grabber 没找到我们的 blob（文件名或位置不对）" >&2; exit 1 ;;
    esac
    fmt=$(cd "$SDK/external/libmali" && scripts/parse_name.sh --format "lib/aarch64-linux-gnu/$BLOB_NAME")
    echo "   parse_name.sh --format -> $fmt"
    [ "$fmt" = "$BLOB_NAME" ] || { echo "!! 文件名不是厂商规范格式（反解不一致）" >&2; exit 1; }
    echo "     = 文件名是厂商规范格式"
fi

echo "== 完成。下一步（在 SDK 根目录）："
echo "     cd buildroot && make O=output/rockchip_rk3568_kickpi_k1mini_release \\"
echo "          rockchip_rk3568_kickpi_k1mini_release_defconfig"
echo "     cd buildroot && make O=output/rockchip_rk3568_kickpi_k1mini_release rockchip-mali"
