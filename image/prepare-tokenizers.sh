#!/bin/bash
# ============================================================================
#  image/prepare-tokenizers.sh — 把 tokenizers 注入镜像的 site-packages
#                                （T15-2-10d，2026-10-07 moonlight 连上之后暴露）
#
#  为什么需要它
#  ---------------------------------------------------------------------------
#  moonlight 会话跑起来之后，`study` / 认游戏那一路（SigLIP）报：
#      game_watch: SigLIP 加载失败: SiglipConfigError("缺少 tokenizers 库
#                  （板端装法：pip3 install --no-deps tokenizers==0.20.3）")
#  `docs/image.md` §5.6 把 tokenizers 记为 optional ✓ —— 与 openai 那次同一类缺口 ✗：
#  它挡的是「认游戏 / 学习监督吃上真 moonlight 帧」这条功能 ✓。
#  ⇒ 照 prepare-openai.sh / prepare-rknnlite.sh 的套路注入（**--no-deps** ✓，与板端提示一致 ✓）。
#
#  做法
#  ---------------------------------------------------------------------------
#  · 版本钉死 `tokenizers==0.20.3`（= 板端错误信息里给的那个 ✓）
#  · `image/tokenizers-wheel.lock` 记 sha256 + 字节数 ✓，**逐个校验**，不符就退出 ✗
#  · 轮子是 `manylinux_2_17_aarch64` + `cp311`（本树 python 3.11.8 ✓）—— 带 Rust 扩展 ✓
#  · 幂等戳在 **rootfs 之外**（`<target 的兄弟目录>/.assistant-wheels/tokenizers.stamp` ✓）
#
#  用法
#  ---------------------------------------------------------------------------
#      bash image/prepare-tokenizers.sh <SDK 根目录> --target <target 树> [--wheels <缓存目录>] [--dry-run]
# ============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
SDK_ARG=""; TARGET_ARG=""; WHEELS=""; DRY=""; LOCK_ARG=""
while [ $# -gt 0 ]; do
    case "$1" in
        --target) TARGET_ARG="${2:-}"; shift ;;
        --target=*) TARGET_ARG="${1#--target=}" ;;
        --wheels) WHEELS="${2:-}"; shift ;;
        --wheels=*) WHEELS="${1#--wheels=}" ;;
        --lock) LOCK_ARG="${2:-}"; shift ;;
        --dry-run) DRY="1" ;;
        -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
        -*) echo "未知参数: $1" >&2; exit 2 ;;
        *) SDK_ARG="$1" ;;
    esac
    shift
done

[ -n "$SDK_ARG" ] && [ -d "$SDK_ARG" ] || { echo "!! 需要 <SDK 根目录>（且必须存在）" >&2; exit 2; }
SDK="$(cd "$SDK_ARG" && pwd)"
CFG="${IMG_CFG:-rockchip_rk3568_kickpi_k1mini_release}"
LOCK="${LOCK_ARG:-$HERE/tokenizers-wheel.lock}"
[ -f "$LOCK" ] || { echo "!! 找不到锁文件: $LOCK" >&2; exit 2; }

TARGET=""; HOW=""
if [ -n "$TARGET_ARG" ]; then TARGET="$TARGET_ARG"; HOW="--target 指定"
elif [ -n "${TARGET_DIR:-}" ]; then TARGET="$TARGET_DIR"; HOW="TARGET_DIR（buildroot post-build 环境）"
else
    INT="$SDK/buildroot/output/$CFG/$CFG/target"
    SINGLE="$SDK/buildroot/output/$CFG/target"
    if [ -d "$INT/usr/lib" ]; then TARGET="$INT"; HOW="自动选中：整机构建树"
    elif [ -d "$SINGLE/usr/lib" ]; then
        echo "!! 只找到单包构建树；若要整机镜像请显式给 --target ✗" >&2
        TARGET="$SINGLE"; HOW="自动选中：单包构建树（⚠）"
    fi
fi
[ -n "$TARGET" ] && [ -d "$TARGET/usr/lib" ] || { echo "!! 找不到 target 树（用 --target 指定）" >&2; exit 2; }
TARGET="$(cd "$TARGET" && pwd)"

echo "== tokenizers 注入（T15-2-10d）"
echo "   SDK=$SDK"
echo "   target=$TARGET（$HOW）"

PYDIR="$(ls -d "$TARGET"/usr/lib/python3.[0-9]* 2>/dev/null | head -1 || true)"
[ -n "$PYDIR" ] || { echo "!! target 里没有 usr/lib/python3.x" >&2; exit 2; }
PYVER="$(basename "$PYDIR" | sed 's/^python//')"
SITE="$PYDIR/site-packages"
[ -n "$WHEELS" ] || WHEELS="$SDK/dl/assistant-wheels"
LOCK_SUM="$(sha256sum "$LOCK" | awk '{print $1}')"
STAMP="$(dirname "$TARGET")/.assistant-wheels/tokenizers.stamp"

if [ -f "$STAMP" ] && [ "$(cat "$STAMP")" = "$LOCK_SUM" ] && [ -d "$SITE/tokenizers" ]; then
    echo "   = 已经一致（戳 + site-packages/tokenizers ✓）"
    exit 0
fi

read -r WANT_SUM WANT_SZ NAME < "$LOCK"
[ -n "${NAME:-}" ] || { echo "!! 锁文件格式不对: $LOCK" >&2; exit 2; }
mkdir -p "$WHEELS"

# 取轮子：缓存优先（sha256 相符就用）；否则按锁文件里的文件名去 PyPI 取（--no-deps ✓）
if [ ! -f "$WHEELS/$NAME" ] || [ "$(sha256sum "$WHEELS/$NAME" | awk '{print $1}')" != "$WANT_SUM" ]; then
    if [ "$DRY" = "1" ]; then
        echo "   ~ (dry-run) 需要下载 $NAME"; exit 1
    fi
    command -v python3 >/dev/null || { echo "!! 需要 python3 + pip" >&2; exit 2; }
    echo "   取轮子（pip download --no-deps …）"
    python3 -m pip download --only-binary=:all: --no-deps \
        --platform manylinux2014_aarch64 --platform manylinux_2_17_aarch64 \
        --python-version 311 --implementation cp --abi cp311 \
        -d "$WHEELS" "tokenizers==0.20.3" >/dev/null 2>&1 \
        || { echo "!! pip download 失败（要联网；见 docs/image.md 决策 D2）" >&2; exit 2; }
fi

GOT_SUM="$(sha256sum "$WHEELS/$NAME" | awk '{print $1}')"
GOT_SZ="$(stat -c %s "$WHEELS/$NAME")"
[ "$GOT_SUM" = "$WANT_SUM" ] && [ "$GOT_SZ" = "$WANT_SZ" ] \
    || { echo "!! 指纹不符: $NAME（期望 $WANT_SUM/$WANT_SZ，实际 $GOT_SUM/$GOT_SZ）" >&2; exit 2; }
echo "   ✓ $NAME 与锁文件一致（$GOT_SZ B）"

if [ "$DRY" = "1" ]; then
    echo "   ~ (dry-run) 会解到 $SITE 并写戳 $STAMP"; exit 1
fi

command -v unzip >/dev/null || { echo "!! 需要 unzip" >&2; exit 2; }
mkdir -p "$SITE"
unzip -o -q "$WHEELS/$NAME" -d "$SITE"
# ⚠ **千万别清 `__pycache__` / `*.pyc`** ✗ —— 镜像开了 `BR2_PACKAGE_PYTHON3_PYC_ONLY=y` ✓，
#    buildroot 只留 `.pyc`、删 `.py` 源码 ⇔ **`.pyc` 是唯一副本** ✗✓（清了整套 site-packages 就废了 ✓）
mkdir -p "$(dirname "$STAMP")"
printf '%s\n' "$LOCK_SUM" > "$STAMP"
echo "   + tokenizers 已装（真 import 由 check-runtime-deps.py 的 chroot 冒烟验 ✓）"
exit 0
