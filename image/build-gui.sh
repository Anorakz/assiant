#!/usr/bin/env bash
# ============================================================================
#  image/build-gui.sh — 交叉编译 Qt5 GUI（agent_gui）并装进 rootfs（T15-2-10b-4）
#
#  为什么要在镜像里重编
#  ---------------------------------------------------------------------------
#  板端那份 `agent_gui` 是拿 **Ubuntu 的 Qt 5.12** 编的；镜像里是 buildroot 的
#  **Qt 5.15.11**。Qt 的小版本之间不做二进制兼容 —— 直接拷板端那份会在运行时报
#  找不到符号（或更糟：起得来但行为诡异）。所以镜像里的 GUI 必须用 buildroot 的
#  sysroot Qt 重新编。
#
#  用法（开发机上，或由 build-payload.sh 调用）
#  ---------------------------------------------------------------------------
#      bash image/build-gui.sh --target <target 树> --dest <target 相对路径> \
#           [--src-root <仓库根>] [--build-dir <dir>] [--keep-debug]
#
#  与 build-native.sh 同一条纪律：**只认调用方给的 target**（不猜树）；
#  产物判据是 **ELF 的 Machine 是 AArch64**，不是文件名（T15-2-10b-3 的教训：
#  pybind11 把交叉产物命名成 x86_64，只看名字会放过去）。
# ============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

TARGET=""
DEST=""
SRC_ROOT=""
BUILD_DIR=""
KEEP_DEBUG=""
while [ $# -gt 0 ]; do
    case "$1" in
        --target) TARGET="${2:-}"; shift ;;
        --target=*) TARGET="${1#--target=}" ;;
        --dest) DEST="${2:-}"; shift ;;
        --dest=*) DEST="${1#--dest=}" ;;
        --src-root) SRC_ROOT="${2:-}"; shift ;;
        --src-root=*) SRC_ROOT="${1#--src-root=}" ;;
        --build-dir) BUILD_DIR="${2:-}"; shift ;;
        --build-dir=*) BUILD_DIR="${1#--build-dir=}" ;;
        --keep-debug) KEEP_DEBUG="1" ;;
        -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
        *) echo "未知参数: $1" >&2; exit 2 ;;
    esac
    shift
done

[ -n "$TARGET" ] && [ -d "$TARGET" ] || { echo "!! 需要 --target <已存在的 target 树>" >&2; exit 2; }
[ -n "$DEST" ] || DEST="usr/lib/assistant/gui/agent_gui"
[ -n "$SRC_ROOT" ] && [ -d "$SRC_ROOT" ] || SRC_ROOT="$HERE/.."
[ -n "$BUILD_DIR" ] || BUILD_DIR="$HERE/../build-rk3568-gui-payload"

# SDK 根：从 target 往上找到含 buildroot/ 的那一级（两棵 output 树深度不同，别写死）
guess_sdk() {
    local d="$1" i
    for i in 1 2 3 4 5 6; do
        d="$(cd "$d/.." && pwd)"
        if [ -d "$d/buildroot" ]; then printf '%s\n' "$d"; return 0; fi
    done
    return 1
}
SDK="${SDK:-$(guess_sdk "$TARGET" || true)}"
[ -n "$SDK" ] && [ -d "$SDK/buildroot" ] || { echo "!! 推不出 SDK 根（用 SDK=<SDK> 指定）" >&2; exit 2; }

CFG="${IMG_CFG:-rockchip_rk3568_kickpi_k1mini_release}"
TF=""
for cand in \
    "$SDK/buildroot/output/$CFG/$CFG/host/share/buildroot/toolchainfile.cmake" \
    "$SDK/buildroot/output/$CFG/host/share/buildroot/toolchainfile.cmake"; do
    [ -f "$cand" ] && { TF="$cand"; break; }
done
[ -n "$TF" ] || { echo "!! 找不到 toolchainfile.cmake（$CFG）" >&2; exit 2; }
BR_HOST="${TF%/share/buildroot/toolchainfile.cmake}"
SR="$BR_HOST/aarch64-buildroot-linux-gnu/sysroot"
STRIP="$BR_HOST/bin/aarch64-buildroot-linux-gnu-strip"

echo "== T15-2-10b-4 交叉编译 GUI（Qt5 Widgets）"
echo "   SDK        : $SDK"
echo "   sysroot    : $SR"
echo "   src-root   : $SRC_ROOT"
echo "   build-dir  : $BUILD_DIR"
echo "   dest       : $TARGET/$DEST"

SRC_GUI="$SRC_ROOT/gui"
[ -f "$SRC_GUI/CMakeLists.txt" ] || { echo "!! 没有 $SRC_GUI/CMakeLists.txt" >&2; exit 1; }

# Qt5 的 cmake 配置在 sysroot 的 usr/lib/cmake 下；moc/rcc/uic 是**宿主机**上的那份
# （buildroot 把 Qt 工具装在 host/bin）。find_package 会经由 CMAKE_FIND_ROOT_PATH 找到
# sysroot 那份 Qt5Config，而 Qt5CoreConfig 里的 _qt5Core_install_prefix 指向 sysroot，
# 找不到可执行工具时会回退到 PATH —— 所以把 host/bin 放进 PATH。
export PATH="$BR_HOST/bin:$PATH"

"$(command -v cmake)" -S "$SRC_GUI" -B "$BUILD_DIR" \
    -DCMAKE_TOOLCHAIN_FILE="$TF" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_SYSROOT="$SR" \
    -DCMAKE_PREFIX_PATH="$SR/usr" \
    -DGUI_BUILD_TESTS=OFF \
    --log-level=STATUS

"$(command -v cmake)" --build "$BUILD_DIR" -j"$(nproc)"

BIN="$(find "$BUILD_DIR" -maxdepth 1 -type f -name 'agent_gui' | head -1)"
[ -n "$BIN" ] || BIN="$(find "$BUILD_DIR" -maxdepth 2 -type f -name 'agent_gui' | head -1)"
[ -n "$BIN" ] || { echo "!! 构建完了却没找到 agent_gui" >&2; exit 1; }
echo "   产物       : $BIN  ($(stat -c %s "$BIN") B)"

# ⚠ 判据是 ELF 头（T15-2-10b-3 的教训：名字会骗人）
MACHINE="$(readelf -h "$BIN" 2>/dev/null | awk -F: '/Machine/ {gsub(/^[ \t]+/, "", $2); print $2}')"
echo "   ELF 机器   : ${MACHINE:-（读不出来）}"
case "$MACHINE" in
    AArch64) : ;;
    *) echo "!! 不是 aarch64 产物（Machine=$MACHINE）—— toolchainfile 没生效？" >&2
       rm -rf "$BUILD_DIR"; exit 1 ;;
esac

# strip：Release 带符号有几十 MB，镜像是要烧进 eMMC 的
if [ -z "$KEEP_DEBUG" ] && [ -x "$STRIP" ]; then
    cp -a "$BIN" "$BIN.debug.tmp"
    "$STRIP" "$BIN"
    echo "   strip      : $(stat -c %s "$BIN.debug.tmp") B -> $(stat -c %s "$BIN") B"
    rm -f "$BIN.debug.tmp"
fi

install -D -m 0755 "$BIN" "$TARGET/$DEST"
echo "   + 已装到 $TARGET/$DEST"

# 依赖：Qt5 那几个必须能在镜像里解析（$ORIGIN 之外，Qt 库在 /usr/lib）
if command -v readelf >/dev/null 2>&1; then
    echo "   NEEDED:"
    readelf -d "$TARGET/$DEST" | awk -F'[][]' '/NEEDED/ {print "     " $2}'
fi
echo "== 完成。chroot 冒烟（真出图只能板上测）："
echo "   chroot $TARGET /usr/lib/assistant/gui/agent_gui --help"
