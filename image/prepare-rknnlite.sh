#!/usr/bin/env bash
# ============================================================================
#  image/prepare-rknnlite.sh — 把 rknn-toolkit-lite2（rknnlite）装进镜像（T15-2-7）
#
#  为什么需要它
#  ---------------------------------------------------------------------------
#  Agent 的 SigLIP 走 NPU 推理，入口是 `from rknnlite.api import RKNNLite`
#  （agent/vision/siglip/runtime.py；缺它会明确报 "缺少 rknnlite"）。
#  但 buildroot 里**没有** rknn-toolkit-lite2 这个包（它是 Rockchip 以 **wheel**
#  形式发的），SDK 把它和 rknn-toolkit2 一起放在：
#      external/rknn-toolkit2/rknn-toolkit-lite2/packages/*.whl
#  所以镜像里要靠本脚本把对应 python 版本的那份 wheel 解到 site-packages。
#
#  为什么必须按 python 版本挑（不是随便拿一份）
#  ---------------------------------------------------------------------------
#  实测那份 wheel 里有 **8 个编译扩展**：
#      rknnlite/api/rknn_runtime.cpython-311-aarch64-linux-gnu.so 等
#  也就是说它**不是纯 Python**，cp38 的 wheel 不能用在 python3.11 上。
#  SDK 里同时提供 cp37/cp38/cp39/cp310/**cp311**/cp312 的 aarch64 版本，
#  而本树的 buildroot python3 是 **3.11.8** —— 正好有 cp311 那份，
#  所以这里按"镜像里实际编译出来的 python 版本"去挑，并对不上就报错退出
#  （换 python 版本时宁可失败，也不要装一个 import 不进来的轮子）。
#
#  依赖（wheel 的 METADATA 里写着，本树已开）
#  ---------------------------------------------------------------------------
#      Requires-Dist: numpy        -> BR2_PACKAGE_PYTHON_NUMPY
#      Requires-Dist: psutil       -> BR2_PACKAGE_PYTHON_PSUTIL
#      Requires-Dist: ruamel.yaml  -> BR2_PACKAGE_PYTHON_RUAMEL_YAML
#  另外它 dlopen 的是 `librknnrt.so`（由 BR2_PACKAGE_RKNPU2 装进 /usr/lib）。
#  脚本最后会把这三样在不在如实报出来。
#
#  用法
#  ---------------------------------------------------------------------------
#      bash image/prepare-rknnlite.sh <SDK 根目录> [--dry-run]
#
#  幂等：site-packages 里已有同一份 wheel 的戳就跳过。**不改 SDK 的源码树**
#  （只往已经建好的 target 树里放东西；镜像构建时由 post-build 脚本调用）。
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
    echo "用法: bash image/prepare-rknnlite.sh <SDK 根目录> [--dry-run]" >&2
    exit 2
fi

CFG="${IMG_CFG:-rockchip_rk3568_kickpi_k1mini_release}"
OUT="$SDK/buildroot/output/$CFG"
TARGET="$OUT/target"
PKGDIR="$SDK/external/rknn-toolkit2/rknn-toolkit-lite2/packages"

echo "== T15-2-7 rknnlite（rknn-toolkit-lite2）装进镜像"

if [ ! -d "$TARGET" ]; then
    echo "!! 还没有 target 树：$TARGET（先把包编出来）" >&2
    exit 2
fi
if [ ! -d "$PKGDIR" ]; then
    echo "!! 找不到 wheel 目录：$PKGDIR" >&2
    exit 2
fi

# ---------------------------------------------------------------------------
#  镜像里实际的 python 版本（从编译出来的 python3 目录名取）
# ---------------------------------------------------------------------------
PYDIR="$(ls -d "$OUT"/build/python3-[0-9]* 2>/dev/null | head -1 || true)"
if [ -z "$PYDIR" ]; then
    echo "!! 找不到编译出来的 python3 目录（$OUT/build/python3-*）" >&2
    exit 2
fi
PYVER="$(basename "$PYDIR" | sed 's/^python3-//' | cut -d. -f1,2)"
PYTAG="cp$(printf '%s' "$PYVER" | tr -d '.')"
echo "   镜像 python : $PYVER（tag $PYTAG）"

SITE="$TARGET/usr/lib/python$PYVER/site-packages"
echo "   site-packages: $SITE"

WHEEL="$(ls "$PKGDIR"/rknn_toolkit_lite2-*-"$PYTAG"-"$PYTAG"-manylinux*aarch64.whl 2>/dev/null | head -1 || true)"
if [ -z "$WHEEL" ]; then
    echo "!! 没有 $PYTAG 的 aarch64 wheel。SDK 里现有：" >&2
    ls "$PKGDIR"/*.whl 2>/dev/null | sed 's/^/     /' >&2
    exit 1
fi
echo "   wheel      : $(basename "$WHEEL")"

# ---------------------------------------------------------------------------
#  指纹校验：SDK 自己在同一个目录放了 packages.md5sum
# ---------------------------------------------------------------------------
if [ -f "$PKGDIR/packages.md5sum" ]; then
    if (cd "$PKGDIR" && grep -F "$(basename "$WHEEL")" packages.md5sum | md5sum -c - >/dev/null 2>&1); then
        echo "   = md5sum 校验通过（对着 $PKGDIR/packages.md5sum）"
    else
        echo "!! md5sum 校验失败（wheel 与 SDK 记的不一致）" >&2
        exit 1
    fi
else
    echo "   ! 没有 packages.md5sum，跳过指纹校验"
fi

VER="$(basename "$WHEEL" | sed 's/^rknn_toolkit_lite2-//' | cut -d- -f1)"
STAMP="$SITE/.rknnlite-wheel"

if [ -f "$STAMP" ] && [ "$(cat "$STAMP")" = "$(basename "$WHEEL")" ]; then
    echo "   = 已经一致：$SITE 里已经是 $(basename "$WHEEL")"
else
    if [ -n "$DRY" ]; then
        echo "   + 将解包 $(basename "$WHEEL") -> $SITE（dry-run）"
    else
        mkdir -p "$SITE"
        rm -rf "$SITE/rknnlite" "$SITE"/rknn_toolkit_lite2-*.dist-info
        python3 -m zipfile -e "$WHEEL" "$SITE"
        echo "   + 已解包 $VER 到 $SITE"
        # 编译扩展的 python tag 必须与镜像一致 —— 这是这个脚本存在的理由，验一下。
        # ⚠ 文件名里的 tag 是 `cpython-311`（带 cpython 前缀、无点），不是 wheel 名里的 `cp311`：
        #   第一版就是按 cp311 去匹配，结果把**对的**文件全判成错的。
        PYSO="${PYTAG#cp}"
        bad="$(find "$SITE/rknnlite" -name '*.so' 2>/dev/null | grep -v -- "cpython-$PYSO-" | head -5 || true)"
        if [ -n "$bad" ]; then
            echo "!! 解出来的扩展不是 $PYTAG（找 cpython-$PYSO）的：" >&2
            echo "$bad" | sed 's/^/     /' >&2
            echo "   已将解出来的内容删掉，避免留下一个 import 不进来的 rknnlite" >&2
            rm -rf "$SITE/rknnlite" "$SITE"/rknn_toolkit_lite2-*.dist-info
            exit 1
        fi
        echo "   = 扩展 tag 全部匹配 cpython-$PYSO"
        # 所有检查通过之后才落戳
        echo "$(basename "$WHEEL")" > "$STAMP"
    fi
fi

# ---------------------------------------------------------------------------
#  依赖如实报告（缺了 import 会失败，但不该在这里假装没看见）
# ---------------------------------------------------------------------------
echo "   依赖检查:"
check_py() {
    if [ -d "$SITE/$1" ] || [ -f "$SITE/$1.py" ]; then
        printf '     [OK]  %s\n' "$1"
    else
        printf '     [缺]  %s\n' "$1"
    fi
}
check_py numpy
check_py psutil
check_py ruamel
if [ -e "$TARGET/usr/lib/librknnrt.so" ]; then
    printf '     [OK]  librknnrt.so（rknpu2）\n'
else
    printf '     [缺]  librknnrt.so（rknpu2）\n'
fi

echo "   文件数: $(find "$SITE/rknnlite" -type f 2>/dev/null | wc -l) 个"
echo "== 完成。"
