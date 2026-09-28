#!/usr/bin/env bash
# ============================================================================
#  image/build-llama.sh — 交叉编译 llama.cpp（llama-server）并装进镜像（T15-2-7）
#
#  为什么不用厂商的东西
#  ---------------------------------------------------------------------------
#  · buildroot 里**没有** llama-cpp 包（本树 package/ 下搜过，没有）。
#  · SDK 里有的是 Rockchip 的 `external/rknn-llm`（NPU 上跑 LLM，要 rkllm 格式的模型），
#    与我们已经验证过的运行时（llama.cpp + Qwen3 GGUF，CPU 4 线程）不是一条路。
#  · 板端那份 `llm/bin/llama-server` 是**在板子上本地编的**、而且 `llm/` 根本没进仓库
#    （见 llm/README.md）—— 拿它当唯一来源就等于"发行镜像依赖一份凭空出现的二进制"。
#
#  所以：**钉住板端验证过的那个 commit，用 buildroot 的工具链交叉编译**。
#  仓库里只放配方，不放 16 MB 构建产物（用户 2026-09-28 批准的做法 A）。
#
#  钉的是哪个 commit、为什么
#  ---------------------------------------------------------------------------
#      上游   github.com/ggml-org/llama.cpp
#      commit b387ddfd8   （板端 `git describe` = b10675-2-gb387ddfd8，即 build 10677）
#  这个版本是**板端实测过**的那个（docs/llm.md 里的 token 预算、SLEEP 启停、
#  降级行为都是它跑出来的），所以不追新：换版本要重跑 T15-1/T14 那几套验收。
#
#  构建参数：与板端那次逐条对齐（对齐才有可比性），只差交叉编译必须改的那一项
#  ---------------------------------------------------------------------------
#      板端 CMakeCache（实测读出来的）      本脚本
#      BUILD_SHARED_LIBS=ON            →   一样（板端 bin/ 下有一堆 .so）
#      CMAKE_BUILD_TYPE=Release (-O3)  →   一样
#      GGML_OPENMP=ON                  →   一样
#      GGML_CPU_REPACK=ON              →   一样
#      LLAMA_CURL=OFF                  →   一样（所以不需要 libcurl 给它）
#      GGML_NATIVE=ON（本机 A55 调优）  →   **改了**：交叉编译不能探测本机，
#                                          改为 GGML_NATIVE=OFF +
#                                          GGML_CPU_ARM_ARCH=armv8.2-a+dotprod+fp16
#                                          （RK3568 的 A55 支持 dotprod 与 fp16）
#      LLAMA_BUILD_TESTS/EXAMPLES=ON   →   关闭（镜像只要 server，省时间与体积）
#
#  源码怎么拿：**多源 + 逐源校验**（踩过：直连下到过一次截断的归档）
#  ---------------------------------------------------------------------------
#  实测 github 直连拿到过一个**只有 15.5 MB、缺 tools/server/server.cpp** 的残档，
#  而两个国内反代拿到的是完整的 37,067,727 B 且**彼此逐字节相同**。
#  所以这里按顺序试多个源，**每个都按 sha256 校验**，第一个对上的才用；
#  全都不对就报错退出（残档绝不会被静默接受）。
#
#  用法
#  ---------------------------------------------------------------------------
#      bash image/build-llama.sh <SDK 根目录> [--dest <目录>] [--force] [--dry-run]
#
#      默认 --dest = <SDK>/buildroot/output/<cfg>/target/usr/lib/assistant/llm/bin
#      （即直接装进已经建好的 target 树；镜像构建时由 post-build 脚本调用）
#
#  幂等：目标里已有同一 commit 的产物（`.llama-source-commit` 戳）就直接跳过。
# ============================================================================
set -euo pipefail

SDK="${1:-}"
shift || true
DEST=""
FORCE=""
DRY=""
JOBS="${IMG_JOBS:-8}"
while [ $# -gt 0 ]; do
    case "$1" in
        --dest)    DEST="${2:-}"; shift ;;
        --force)   FORCE="1" ;;
        --dry-run) DRY="1" ;;
        --jobs)    JOBS="${2:-8}"; shift ;;
        *) echo "未知参数: $1" >&2; exit 2 ;;
    esac
    shift
done

if [ -z "$SDK" ]; then
    echo "用法: bash image/build-llama.sh <SDK 根目录> [--dest <目录>] [--force] [--dry-run]" >&2
    exit 2
fi

COMMIT="b387ddfd8"
FULL_COMMIT="b387ddfd84b4b1f79a6e09910748195e3320e89e"
TARBALL="llama.cpp-$COMMIT.tar.gz"
EXPECT_SHA256="98a839fe09febe79c2240bb7b7a516388ed8eaf273cfbe2838fdb60d12467a65"
EXPECT_SIZE="37067727"

# 多源：直连优先，其次两个国内反代（实测都能拿到完整档）
SRC_URLS=(
    "https://github.com/ggml-org/llama.cpp/archive/$COMMIT.tar.gz"
    "https://ghfast.top/https://github.com/ggml-org/llama.cpp/archive/$COMMIT.tar.gz"
    "https://gh-proxy.com/https://github.com/ggml-org/llama.cpp/archive/$COMMIT.tar.gz"
)

CFG="${IMG_CFG:-rockchip_rk3568_kickpi_k1mini_release}"
OUT="$SDK/buildroot/output/$CFG"
TOOLCHAIN="$OUT/host/share/buildroot/toolchainfile.cmake"
WORK="$OUT/build-assistant"
DL="$WORK/dl"
SRC="$WORK/llama.cpp-$COMMIT"
BUILD="$WORK/llama.cpp-build"
[ -n "$DEST" ] || DEST="$OUT/target/usr/lib/assistant/llm/bin"
STAMP="$DEST/.llama-source-commit"

echo "== T15-2-7 交叉编译 llama.cpp"
echo "   SDK    : $SDK"
echo "   commit : $COMMIT ($FULL_COMMIT)"
echo "   目标   : $DEST"
echo "   jobs   : $JOBS"

if [ ! -f "$TOOLCHAIN" ]; then
    echo "!! 找不到 buildroot 工具链文件：$TOOLCHAIN" >&2
    echo "   先把它建出来：bash image/sdk-make.sh <SDK> <某个包>（或直接整机构建）" >&2
    exit 2
fi

# ---------------------------------------------------------------------------
#  幂等：目标里有同 commit 的产物就收工
# ---------------------------------------------------------------------------
if [ -z "$FORCE" ] && [ -f "$STAMP" ] && [ -x "$DEST/llama-server" ] \
        && [ "$(cat "$STAMP")" = "$FULL_COMMIT" ]; then
    echo "   = 已经一致：$DEST 里就是 $COMMIT 的产物（要重编加 --force）"
    ls -la "$DEST" | sed 's/^/     /'
    exit 0
fi

# ---------------------------------------------------------------------------
#  源码：多源下载 + 逐源 sha256 校验（残档会被挡住）
# ---------------------------------------------------------------------------
mkdir -p "$DL"
CACHE="$DL/$TARBALL"
good=""
if [ -f "$CACHE" ] && [ "$(sha256sum "$CACHE" | cut -d' ' -f1)" = "$EXPECT_SHA256" ]; then
    echo "   = 源码缓存可用: $CACHE"
    good="$CACHE"
fi

if [ -z "$good" ]; then
    for u in "${SRC_URLS[@]}"; do
        echo "   + 取回源码 <- $u"
        if [ -n "$DRY" ]; then
            good="(dry-run)"
            break
        fi
        rm -f "$CACHE.part"
        curl -sL --max-time 900 --retry 2 -o "$CACHE.part" "$u" || true
        sz="$(stat -c %s "$CACHE.part" 2>/dev/null || echo 0)"
        sha="$(sha256sum "$CACHE.part" 2>/dev/null | cut -d' ' -f1 || echo none)"
        if [ "$sha" = "$EXPECT_SHA256" ] && [ "$sz" = "$EXPECT_SIZE" ]; then
            mv -f "$CACHE.part" "$CACHE"
            echo "     = 校验通过（$sz B）"
            good="$CACHE"
            break
        fi
        echo "     ! 这个源不对：size=$sz（期望 $EXPECT_SIZE）sha256=$sha" >&2
        rm -f "$CACHE.part"
    done
fi

if [ -z "$good" ]; then
    echo "!! 所有源都没能拿到与钉死指纹一致的源码（$EXPECT_SHA256 / $EXPECT_SIZE B）" >&2
    exit 1
fi
if [ -n "$DRY" ]; then
    echo "   (dry-run：到此为止)"
    exit 0
fi

# ---------------------------------------------------------------------------
#  解包 + 配置 + 编译
# ---------------------------------------------------------------------------
if [ ! -d "$SRC" ]; then
    echo "== 解包到 $SRC"
    mkdir -p "$SRC"
    tar -xzf "$CACHE" --strip-components=1 -C "$SRC"
fi

if [ ! -f "$SRC/CMakeLists.txt" ]; then
    echo "!! 解包后没有 $SRC/CMakeLists.txt，源码不完整" >&2
    exit 1
fi

echo "== cmake 配置（用 buildroot 工具链）"
cmake -S "$SRC" -B "$BUILD" \
    -DCMAKE_TOOLCHAIN_FILE="$TOOLCHAIN" \
    -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_SHARED_LIBS=ON \
    -DCMAKE_BUILD_RPATH='$ORIGIN' \
    -DCMAKE_INSTALL_RPATH='$ORIGIN' \
    -DCMAKE_BUILD_WITH_INSTALL_RPATH=ON \
    -DLLAMA_CURL=OFF \
    -DLLAMA_BUILD_TESTS=OFF \
    -DLLAMA_BUILD_EXAMPLES=OFF \
    -DLLAMA_BUILD_SERVER=ON \
    -DGGML_NATIVE=OFF \
    -DGGML_OPENMP=ON \
    -DGGML_CPU_REPACK=ON \
    -DGGML_CPU_ARM_ARCH=armv8.2-a+dotprod+fp16 \
    -DGGML_VULKAN=OFF -DGGML_CUDA=OFF -DGGML_METAL=OFF \
    > "$WORK/cmake-configure.log" 2>&1 || {
        echo "!! cmake 配置失败，日志末尾：" >&2
        tail -30 "$WORK/cmake-configure.log" >&2
        exit 1
    }
grep -a -E 'GGML_NATIVE|CMAKE_BUILD_TYPE|LLAMA_CURL|CMAKE_C_COMPILER:' "$BUILD/CMakeCache.txt" 2>/dev/null | sed 's/^/     /' || true

echo "== 编译 llama-server（-j$JOBS）"
cmake --build "$BUILD" --target llama-server -j "$JOBS" > "$WORK/build.log" 2>&1 || {
    echo "!! 编译失败，日志末尾：" >&2
    tail -30 "$WORK/build.log" >&2
    exit 1
}

# ---------------------------------------------------------------------------
#  安装：板端那套是"平铺在 llm/bin/"（可执行文件与它自己的 .so 在一起），
#  照抄这个布局，脚本就不用动。
# ---------------------------------------------------------------------------
echo "== 安装到 $DEST"
mkdir -p "$DEST"
cp -a "$BUILD/bin/llama-server" "$DEST/"
# 只要 llama/ggml 自己的库（llama.cpp 把可执行文件与它自己的 .so 都放在 bin/）
for f in "$BUILD"/bin/libggml*.so* "$BUILD"/bin/libllama*.so* "$BUILD"/bin/libmtmd*.so*; do
    [ -e "$f" ] && cp -a "$f" "$DEST/"
done
echo "$FULL_COMMIT" > "$STAMP"

# ---------------------------------------------------------------------------
#  自检：架构对不对、动态依赖缺不缺
# ---------------------------------------------------------------------------
echo "== 自检"
arch="$(readelf -h "$DEST/llama-server" | sed -n 's/.*Machine: *//p')"
echo "   架构: $arch"
case "$arch" in
    AArch64*) : ;;
    *) echo "!! 产物不是 aarch64（$arch）" >&2; exit 1 ;;
esac

echo "   产物清单:"
ls -la "$DEST" | grep -E 'llama-server|lib' | sed 's/^/     /'

echo "   动态依赖解析（对着 target 树与工具链 sysroot）:"
SYSLIB="$OUT/host/aarch64-buildroot-linux-gnu/sysroot/usr/lib"
missing=0
for so in "$DEST/llama-server" "$DEST"/lib*.so.0.*; do
    [ -e "$so" ] || continue
    for n in $(readelf -d "$so" 2>/dev/null | sed -n 's/.*NEEDED.*\[\(.*\)\].*/\1/p'); do
        case "$n" in
            libllama*|libggml*|libmtmd*) [ -e "$DEST/$n" ] && continue ;;
        esac
        if [ -e "$DEST/$n" ] || [ -e "$SYSLIB/$n" ] || [ -e "$OUT/target/usr/lib/$n" ] || [ -e "$OUT/target/lib/$n" ]; then
            continue
        fi
        echo "     [缺] $(basename "$so") -> $n"
        missing=$((missing + 1))
    done
done
echo "   缺失: $missing"

if command -v qemu-aarch64-static >/dev/null 2>&1; then
    echo "   qemu 冒烟（--version）:"
    qemu-aarch64-static -L "$OUT/target" "$DEST/llama-server" --version 2>&1 | head -5 | sed 's/^/     /' || \
        echo "     （跑不起来：多半是 target 里还缺工具链的 libgomp/libstdc++，整机构建后再验）"
fi

echo "== 完成。镜像里靠 post-build 脚本调用本脚本，所以整机构建会自动带上它。"
