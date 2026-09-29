#!/usr/bin/env bash
# ============================================================================
#  image/build-native.sh — 交叉编译 pybind11 扩展 agent_native（T15-2-10b-3）
#
#  为什么需要它
#  ---------------------------------------------------------------------------
#  仓库里现成的 `build-rk3568/native/agent_native.cpython-38-...so` 是**板端 Ubuntu
#  （python 3.8）**那套；镜像里的 python 是 buildroot 编出来的 **3.11** —— 装进去
#  `import agent_native` 必然失败（扩展模块的 ABI 是按版本绑死的）。所以镜像里这份
#  必须用 buildroot 的工具链 + sysroot 的 python3.11 头文件**重新编**。
#
#  用法（开发机上，或由 build-payload.sh 调用）
#  ---------------------------------------------------------------------------
#      bash image/build-native.sh --target <target 树> --dest <target 相对路径> \
#           [--src-root <仓库根>] [--build-dir <dir>]
#
#  落点由调用方给（T15-2-10 的教训：**不猜树**）。默认从 target 树自己推 SDK 根。
#
#  怎么找工具链
#  ---------------------------------------------------------------------------
#      <SDK>/buildroot/output/<CFG>/<CFG>/host/share/buildroot/toolchainfile.cmake
#      （整机构建那棵；单包构建那棵在 …/<CFG>/host/… —— 两棵都认，优先整机）
#  sysroot 里的 Python.h 由 native/CMakeLists.txt 按版本探测（别再写死 3.8）。
# ============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

TARGET=""
DEST=""
SRC_ROOT=""
BUILD_DIR=""
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
        -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
        *) echo "未知参数: $1" >&2; exit 2 ;;
    esac
    shift
done

[ -n "$TARGET" ] && [ -d "$TARGET" ] || { echo "!! 需要 --target <已存在的 target 树>" >&2; exit 2; }
[ -n "$DEST" ] || DEST="usr/lib/assistant/agent_native.cpython-311-aarch64-linux-gnu.so"
[ -n "$SRC_ROOT" ] && [ -d "$SRC_ROOT" ] || SRC_ROOT="$HERE/.."
[ -n "$BUILD_DIR" ] || BUILD_DIR="$HERE/../build-rk3568-native-payload"

# ---------------------------------------------------------------------------
#  SDK 根：从 target 树往上走，找到**含 buildroot/ 的那一级**
#  （两棵 output 树深度不同：…/<cfg>/target 与 …/<cfg>/<cfg>/target，
#    写死层数是错的 —— 第一版就是这么挂的）
# ---------------------------------------------------------------------------
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

SR="${TF%/host/share/buildroot/toolchainfile.cmake}/host/aarch64-buildroot-linux-gnu/sysroot"
[ -d "$SR/usr/include" ] || { echo "!! sysroot 不对：$SR" >&2; exit 2; }

echo "== T15-2-10b-3 交叉编译 agent_native（pybind11 扩展）"
echo "   SDK        : $SDK"
echo "   sysroot    : $SR"
echo "   src-root   : $SRC_ROOT"
echo "   build-dir  : $BUILD_DIR"
echo "   dest       : $TARGET/$DEST"

SRC_NATIVE="$SRC_ROOT/native"
[ -d "$SRC_NATIVE" ] || { echo "!! 没有 $SRC_NATIVE" >&2; exit 1; }
[ -f "$SRC_NATIVE/third_party/pybind11/CMakeLists.txt" ] || {
    echo "!! pybind11 子模块不在（$SRC_NATIVE/third_party/pybind11）—— 先 git submodule update --init" >&2
    exit 1
}
[ -f "$SRC_NATIVE/third_party/moonlight-common-c/src/Limelight.h" ] || {
    echo "!! moonlight-common-c 子模块不在（交叉编译时它是要链的）" >&2
    exit 1
}

CMAKE="$(command -v cmake)"
[ -n "$CMAKE" ] || { echo "!! 没有 cmake" >&2; exit 1; }

# 交叉编译 Python 扩展：CMAKE_FIND_ROOT_PATH_MODE_* 由 toolchainfile 管；
# 我们只需要保证 python 头/库都从 sysroot 来（native/CMakeLists.txt 里按版本探测）。
"$CMAKE" -S "$SRC_NATIVE" -B "$BUILD_DIR" \
    -DCMAKE_TOOLCHAIN_FILE="$TF" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_SYSROOT="$SR" \
    -DPython3_EXECUTABLE="$(command -v python3)" \
    --log-level=STATUS

"$CMAKE" --build "$BUILD_DIR" -j"$(nproc)"

# 产物名带 python tag（cpython-311-…），直接找出来
SO="$(find "$BUILD_DIR" -maxdepth 1 -name 'agent_native*.so' | head -1)"
[ -n "$SO" ] || { echo "!! 构建完了却没找到 agent_native*.so" >&2; exit 1; }
echo "   产物       : $(basename "$SO")  ($(stat -c %s "$SO") B)"

# ---------------------------------------------------------------------------
#  ⚠ **判据是 ELF 头，不是文件名**（第一版只看名字，差点放过去一个 x86_64 的产物）
#  pybind11 的 EXT_SUFFIX 是从**宿主机解释器**推出来的，所以交叉编出来的东西
#  名字里可能写 `x86_64` —— 而 ELF 头里才是真的（实测那份是 AArch64，名不副实）。
#  这里两件事都做：① 认 ELF 机器类型；② 用 ELF 的真实结论决定落点文件名。
# ---------------------------------------------------------------------------
MACHINE="$(readelf -h "$SO" 2>/dev/null | awk -F: '/Machine/ {gsub(/^[ \t]+/, "", $2); print $2}')"
echo "   ELF 机器   : ${MACHINE:-（读不出来）}"
case "$MACHINE" in
    AArch64) : ;;
    *) echo "!! 这不是 aarch64 的产物（Machine=$MACHINE）—— toolchainfile 没生效？" >&2
       echo "   构建目录：$BUILD_DIR（删掉重来，别让它留在树里）" >&2
       rm -rf "$BUILD_DIR"
       exit 1 ;;
esac

# 版本对齐检查：ELF 是 aarch64 了，python tag 还得与 target 里的 python 一致
PYVER="$(ls -d "$TARGET"/usr/lib/python3.[0-9]* 2>/dev/null | head -1 | xargs -r basename | sed 's/^python//')"
[ -n "$PYVER" ] || { echo "!! target 里没有 python3.x，落点可能不对：$TARGET" >&2; exit 1; }
TAG="cpython-$(printf '%s' "$PYVER" | tr -d '.')"
case "$(basename "$SO")" in
    *"$TAG"*) echo "   = python tag 匹配（$TAG，target python $PYVER）" ;;
    *) echo "!! 产物 tag 与 target python 不符：$(basename "$SO") 期望含 $TAG" >&2; exit 1 ;;
esac

# 落点：文件名统一成清单里那个（cpython-311-aarch64-linux-gnu.so），方便 unit/检查器盯
install -D -m 0755 "$SO" "$TARGET/$DEST"
echo "   + 已装到 $TARGET/$DEST"

# 依赖闭包粗查：libmoonlight-common-c.so 必须与它同目录（RPATH=\$ORIGIN）
DIR="$(dirname "$TARGET/$DEST")"
if command -v readelf >/dev/null 2>&1; then
    if readelf -d "$TARGET/$DEST" 2>/dev/null | grep -q 'moonlight'; then
        if [ ! -f "$DIR/libmoonlight-common-c.so" ]; then
            ML="$(find "$SR/usr/lib" "$BUILD_DIR" -name 'libmoonlight-common-c.so*' 2>/dev/null | head -1)"
            if [ -n "$ML" ]; then
                cp -a "$ML" "$DIR/"
                echo "   + 从 $ML 补了 libmoonlight-common-c.so"
            else
                echo "!! 扩展链到 moonlight 但找不到 libmoonlight-common-c.so —— import 会失败" >&2
                exit 1
            fi
        fi
        # ⚠ 权限归一：源在 Windows 盘上（DrvFs），cp -a 会把 777 带进镜像
        chmod 0755 "$DIR/libmoonlight-common-c.so"
        echo "   = libmoonlight-common-c.so 已同目录（$(stat -c %a "$DIR/libmoonlight-common-c.so")，RPATH=\$ORIGIN 能解析）"
    fi
fi
echo "== 完成。chroot 冒烟："
echo "   chroot $TARGET /usr/bin/env PYTHONPATH=/usr/lib/assistant \\"
echo "       /usr/bin/python3 -c 'import agent_native; print(agent_native.__file__)'"
