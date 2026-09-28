#!/usr/bin/env bash
# ============================================================================
#  image/prepare-mpp.sh — 补上厂商 MPP 快照漏掉的那个 CMake 助手（T15-2-7）
#
#  为什么需要它（实测，不是猜）
#  ---------------------------------------------------------------------------
#  这份 SDK 的 `external/mpp` 是 **MPP 1.0.11（2025-09-10）** 的新源码（见
#  `external/mpp/CHANGELOG.md` 第一行），它的 `CMakeLists.txt:42` **无条件** include：
#
#      include(${PROJECT_SOURCE_DIR}/build/cmake/merge_objects.cmake)
#
#  而 `external/mpp/build/cmake/` 里**只有 `version.in`** —— 这个文件厂商快照里没有
#  （全 SDK 搜过，只有 CMakeLists / mpp/codec / mpp/hal 三处**调用**它），
#  于是 `make rockchip-mpp` 在 configure 阶段就死：
#
#      CMake Error at CMakeLists.txt:42 (include):
#        include could not find requested file: .../build/cmake/merge_objects.cmake
#      CMake Error at mpp/codec/CMakeLists.txt:24 (merge_objects):
#        Unknown CMake command "merge_objects".
#
#  这不是我们配错了：厂商 buildroot 那条路在这份快照里本来就缺这一块。
#  `merge_objects()` 是 MPP 新引入的静态库对象合并助手（把多个 OBJECT 库合成一个
#  `.o` 再包成 IMPORTED OBJECT 库），被 mpp/codec 与 mpp/hal 大量使用。
#
#  上游有，而且接口与我们的调用一致
#  ---------------------------------------------------------------------------
#      https://raw.githubusercontent.com/rockchip-linux/mpp/develop/build/cmake/merge_objects.cmake
#      2037 B  sha256 4411cd9a8df3d456aa53f0b9f7ea49c354dc977ed55dab5ec67b26add9ba4e2a
#  实测 `develop` 与 `master` 上这份**逐字节相同**，且它导出的正是
#  `function(merge_objects out_obj ...)` —— 与 `mpp/codec|hal/CMakeLists.txt` 里
#  `merge_objects("<target>" <objlib>...)` 的调用形式对得上。
#  （tag `1.0.11` 的 raw 路径取不到 —— 404；所以这里**钉字节哈希**而不是钉 ref：
#    上游哪天改了内容，我们的校验会当场报错，让人重新复核，而不是静默换一份。）
#
#  用法
#  ---------------------------------------------------------------------------
#      bash image/prepare-mpp.sh <SDK 根目录> [--dry-run]
#
#  幂等；如果文件已经存在（例如厂商后来补上了），只要它确实定义了 merge_objects
#  就跳过，并把"指纹与我们钉的不同"如实打出来。
# ============================================================================
set -euo pipefail

SDK="${1:-}"
shift || true
DRY=""
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY="1" ;;
        *) echo "未知参数: $1" >&2; exit 2 ;;
    esac
    shift
done

if [ -z "$SDK" ]; then
    echo "用法: bash image/prepare-mpp.sh <SDK 根目录> [--dry-run]" >&2
    exit 2
fi

MPP="$SDK/external/mpp"
DEST="$MPP/build/cmake/merge_objects.cmake"
REL="external/mpp/build/cmake/merge_objects.cmake"
URL="https://raw.githubusercontent.com/rockchip-linux/mpp/develop/build/cmake/merge_objects.cmake"
EXPECT_SHA256="4411cd9a8df3d456aa53f0b9f7ea49c354dc977ed55dab5ec67b26add9ba4e2a"
EXPECT_SIZE="2037"

echo "== T15-2-7 MPP 缺件修复"
echo "   SDK : $SDK"
if [ ! -d "$MPP" ]; then
    echo "!! 找不到 $MPP（本 SDK 的 MPP 源码）" >&2
    exit 2
fi

# 先确认"确实需要"：CMakeLists.txt 里 include 了它
if ! grep -q 'build/cmake/merge_objects.cmake' "$MPP/CMakeLists.txt" 2>/dev/null; then
    echo "   = 这份 MPP 的 CMakeLists.txt 并不 include merge_objects.cmake —— 不需要补，跳过"
    exit 0
fi

if [ -f "$DEST" ]; then
    got="$(sha256sum "$DEST" | cut -d' ' -f1)"
    if [ "$got" = "$EXPECT_SHA256" ]; then
        echo "   = 已经一致: $REL"
    elif grep -q 'function(merge_objects' "$DEST"; then
        echo "   = 已存在且定义了 merge_objects，但指纹与我们钉的不同（$got）—— 按厂商/上游那份用，跳过"
    else
        echo "!! $REL 存在但没有定义 merge_objects：内容不对，需要人工复核" >&2
        exit 1
    fi
    exit 0
fi

echo "   + 取回   : $REL <- $URL"
if [ -n "$DRY" ]; then
    echo "   (dry-run：不下载)"
    exit 0
fi

tmp="$DEST.part"
mkdir -p "$(dirname "$DEST")"
rm -f "$tmp"
if command -v wget >/dev/null 2>&1; then
    wget -q --no-check-certificate -t 3 -O "$tmp" "$URL"
else
    curl -sL --max-time 60 -o "$tmp" "$URL"
fi

got="$(sha256sum "$tmp" | cut -d' ' -f1)"
size="$(stat -c %s "$tmp")"
if [ "$got" != "$EXPECT_SHA256" ] || [ "$size" != "$EXPECT_SIZE" ]; then
    echo "!! 校验失败：期望 sha256=$EXPECT_SHA256 size=$EXPECT_SIZE，实得 sha256=$got size=$size" >&2
    rm -f "$tmp"
    exit 1
fi
grep -q 'function(merge_objects' "$tmp" || { echo "!! 取回的文件没有定义 merge_objects" >&2; rm -f "$tmp"; exit 1; }
mv -f "$tmp" "$DEST"
echo "     = 校验通过 $got ($size B)"

# ⚠ 如果这一步之前已经跑过一次构建，buildroot 的 local site 已经把源码 rsync 到
#   output/<cfg>/build/rockchip-mpp-develop/ 了；那边是**旧副本**，光补 external/mpp
#   不会生效（.stamp_rsynced 在就不会重新同步）。这里如实提醒，并给出确切命令。
CFG="${IMG_CFG:-rockchip_rk3568_kickpi_k1mini_release}"
STALE="$SDK/buildroot/output/$CFG/build/rockchip-mpp-develop"
if [ -d "$STALE" ]; then
    echo
    echo "⚠ 检测到已经同步过的构建目录：$STALE"
    echo "  它是补件**之前**的副本，必须清掉才会重新同步："
    echo "      rm -rf $STALE"
fi

echo "== 完成。现在 rockchip-mpp 的 configure 能过了。"
