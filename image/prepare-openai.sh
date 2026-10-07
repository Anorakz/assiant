#!/bin/bash
# ============================================================================
#  image/prepare-openai.sh — 把 openai SDK（+ 依赖）注入镜像的 site-packages
#                            （T15-2-10c，2026-10-07 刷板时暴露的缺口）
#
#  为什么需要它
#  ---------------------------------------------------------------------------
#  镜像形态下 `llm.mode=edge` 的对话/搜索**全部降级到规则兜底** ✗ —— 板端实测：
#      agent: LLM 降级为规则兜底: OpenAIClientError: 需要 openai SDK。安装: pip3 install openai
#  `docs/image.md` §5.6 那张表把 openai 标成"optional／镜像里没有（provider.py 懒加载）"，
#  但它挡住的其实是**主功能**：工具调用 ⇒ `bilibili_search` ⇒ 队列 ⇒ 视频 ✗。
#  板端 Ubuntu 期那份是装在 `/data` 里的 `openai 3.24.0`（docs/perf-cpu-mem.md ✓），
#  镜像里没有对应机制 ⇒ 打不开对话/搜索。
#
#  做法（钉版本 + 锁文件 + 戳放 rootfs 之外）
#  ---------------------------------------------------------------------------
#  · 顶层版本**钉死** `openai==3.24.0`（= 板端实测能用的那个 ✓），其余由 pip 解析 ✓
#  · `image/openai-wheels.lock` 记每个轮子的 sha256 + 字节数 ✓，**逐个校验**，不符就退出 ✗
#    （轮子不进仓库：openai 2.0 MB + pydantic_core 1.9 MB + jiter 343 KB，构建时从 PyPI 取 ✓）
#  · 轮子目标平台 `manylinux_2_17_aarch64` + `cp311`（本树 python 3.11.8 ✓）：
#    12 个纯 Python ✓ + 2 个带二进制（jiter / pydantic_core ✓）
#  · 幂等戳在 **rootfs 之外**（`<target 的兄弟目录>/.assistant-wheels/openai.stamp` ✓）——
#    戳写进 rootfs 等于往镜像里塞构建垃圾（T15-2-10b 的教训 ✓）
#
#  用法
#  ---------------------------------------------------------------------------
#      bash image/prepare-openai.sh <SDK 根目录> --target <target 树> [--wheels <缓存目录>] [--dry-run]
# ============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
SDK_ARG=""; TARGET_ARG=""; WHEELS=""; DRY=""
while [ $# -gt 0 ]; do
    case "$1" in
        --target) TARGET_ARG="${2:-}"; shift ;;
        --target=*) TARGET_ARG="${1#--target=}" ;;
        --wheels) WHEELS="${2:-}"; shift ;;
        --wheels=*) WHEELS="${1#--wheels=}" ;;
        --lock) LOCK_ARG="${2:-}"; shift ;;
        --dry-run) DRY="1" ;;
        -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
        -*) echo "未知参数: $1" >&2; exit 2 ;;
        *) SDK_ARG="$1" ;;
    esac
    shift
done

[ -n "$SDK_ARG" ] && [ -d "$SDK_ARG" ] || { echo "!! 需要 <SDK 根目录>（且必须存在）" >&2; exit 2; }
SDK="$(cd "$SDK_ARG" && pwd)"
CFG="${IMG_CFG:-rockchip_rk3568_kickpi_k1mini_release}"
LOCK="${LOCK_ARG:-$HERE/openai-wheels.lock}"
[ -f "$LOCK" ] || { echo "!! 找不到锁文件: $LOCK" >&2; exit 2; }

# 落点：--target > 调用方导出的 TARGET_DIR > 整机构建树（并**大声说明选了谁** ✓）
TARGET=""; HOW=""
if [ -n "$TARGET_ARG" ]; then TARGET="$TARGET_ARG"; HOW="--target 指定"
elif [ -n "${TARGET_DIR:-}" ]; then TARGET="$TARGET_DIR"; HOW="TARGET_DIR（buildroot post-build 环境）"
else
    INT="$SDK/buildroot/output/$CFG/$CFG/target"
    SINGLE="$SDK/buildroot/output/$CFG/target"
    if [ -d "$INT/usr/lib" ]; then TARGET="$INT"; HOW="自动选中：整机构建树（update.img 的来源）"
    elif [ -d "$SINGLE/usr/lib" ]; then
        echo "!! 只找到单包构建树：$SINGLE" >&2
        echo "!!   若你的目的是整机构建（update.img），请显式给 --target，否则装错树 ✗" >&2
        TARGET="$SINGLE"; HOW="自动选中：单包构建树（⚠ 可能不是你要的那棵）"
    fi
fi
[ -n "$TARGET" ] && [ -d "$TARGET/usr/lib" ] || { echo "!! 找不到 target 树（用 --target 指定）" >&2; exit 2; }
TARGET="$(cd "$TARGET" && pwd)"

echo "== openai SDK 注入（T15-2-10c）"
echo "   SDK    : $SDK"
echo "   target : $TARGET"
echo "   （$HOW）"
echo "   锁文件 : $LOCK"

PYDIR="$(ls -d "$TARGET"/usr/lib/python3.[0-9]* 2>/dev/null | head -1 || true)"
[ -n "$PYDIR" ] || { echo "!! target 里没有 usr/lib/python3.x —— target 选错了？" >&2; exit 2; }
PYVER="$(basename "$PYDIR" | sed 's/^python//')"
SITE="$PYDIR/site-packages"
echo "   python : $PYVER   site-packages: $SITE"

[ -n "$WHEELS" ] || WHEELS="$SDK/dl/assistant-wheels"
echo "   轮子缓存: $WHEELS"

# ---- 幂等戳（rootfs 之外 ✓）------------------------------------------------
STAMPDIR="$(dirname "$SITE")/../.."          # target/usr/lib/.. -> target/usr
STAMPDIR="$(cd "$TARGET" && pwd)/../.assistant-wheels"
STAMP="$STAMPDIR/openai.stamp"
LOCK_SUM="$(sha256sum "$LOCK" | awk '{print $1}')"
if [ -f "$STAMP" ] && [ "$(cat "$STAMP")" = "$LOCK_SUM" ] && [ -d "$SITE/openai" ]; then
    echo "   = 已经一致（戳 $STAMP 与锁文件相同，且 site-packages 里有 openai ✓）"
    exit 0
fi

# ---- 取轮子（缓存优先；缺就用 pip 按锁文件里的顶层版本取）------------------
mkdir -p "$WHEELS"
need_download=0
while read -r want_sum want_sz name; do
    [ -n "${name:-}" ] || continue
    if [ -f "$WHEELS/$name" ] && [ "$(sha256sum "$WHEELS/$name" | awk '{print $1}')" = "$want_sum" ]; then
        continue
    fi
    need_download=1
done < "$LOCK"

if [ "$need_download" = "1" ]; then
    if [ "$DRY" = "1" ]; then
        echo "   ~ (dry-run) 需要下轮子：pip download --only-binary=:all: --platform manylinux2014_aarch64 \\"
        echo "                   --python-version 311 --implementation cp --abi cp311 openai==3.24.0"
        exit 1
    fi
    command -v python3 >/dev/null || { echo "!! 需要 python3 + pip 来下轮子" >&2; exit 2; }
    echo "   取轮子（pip download，钉 openai==3.24.0）..."
    python3 -m pip download --only-binary=:all: \
        --platform manylinux2014_aarch64 --platform manylinux_2_17_aarch64 \
        --python-version 311 --implementation cp --abi cp311 \
        -d "$WHEELS" "openai==3.24.0" >/dev/null 2>&1 || {
            echo "!! pip download 失败（要联网；见 docs/image.md 决策 D2）" >&2; exit 2; }
fi

# ---- 逐个校验（锁文件是权威 ✓）--------------------------------------------
bad=0; n=0
while read -r want_sum want_sz name; do
    [ -n "${name:-}" ] || continue
    f="$WHEELS/$name"
    n=$((n + 1))
    if [ ! -f "$f" ]; then echo "   ✗ 缺轮子: $name" >&2; bad=1; continue; fi
    got_sum="$(sha256sum "$f" | awk '{print $1}')"
    got_sz="$(stat -c %s "$f")"
    if [ "$got_sum" != "$want_sum" ] || [ "$got_sz" != "$want_sz" ]; then
        echo "   ✗ 指纹不符: $name（期望 $want_sum/$want_sz，实际 $got_sum/$got_sz）" >&2
        bad=1
    fi
done < "$LOCK"
[ "$bad" = "0" ] || { echo "!! 轮子校验没过，不往镜像里装 ✗" >&2; exit 2; }
echo "   ✓ $n 个轮子全部与锁文件一致"

if [ "$DRY" = "1" ]; then
    echo "   ~ (dry-run) 会把它们解到 $SITE 并写戳 $STAMP"
    exit 1
fi

# ---- 解包进 site-packages --------------------------------------------------
command -v unzip >/dev/null || { echo "!! 需要 unzip" >&2; exit 2; }
mkdir -p "$SITE"
while read -r want_sum want_sz name; do
    [ -n "${name:-}" ] || continue
    unzip -o -q "$WHEELS/$name" -d "$SITE"
    echo "   + $name"
done < "$LOCK"
# ⚠ **千万别清 `__pycache__` / `*.pyc`** ✗ —— 这套镜像开了 `BR2_PACKAGE_PYTHON3_PYC_ONLY=y` ✓，
#    buildroot 只保留编译后的 `.pyc`、删掉 `.py` 源码 ⇔ **`.pyc` 就是唯一副本** ✗✓
#    实测后果（2026-10-07 22:02 那次构建）：清了之后 numpy/cv2/openai/rknnlite 全退化成
#    **空命名空间包** ⇒ `import numpy` 报 `module 'numpy' has no attribute '__version__'` ✗，
#    chroot 冒烟当场红 3 条 ✓。原先这里那版"清构建垃圾"是错的 ✗，已删 ✓。

mkdir -p "$STAMPDIR"
printf '%s\n' "$LOCK_SUM" > "$STAMP"

echo "== 完成。装了 $(find "$SITE" -maxdepth 1 -name 'openai*' | wc -l) 个 openai 相关目录；"
echo "   真 import 由 image/check-runtime-deps.py 的 chroot 冒烟验 ✓（构建机上不猜 ✓）"
exit 0
