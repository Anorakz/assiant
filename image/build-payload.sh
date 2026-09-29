#!/usr/bin/env bash
# ============================================================================
#  image/build-payload.sh — 把 payload 装进 rootfs（T15-2-10b）
#
#  payload 是什么：unit 文件**已经指向**、但 T15-2-8 当时还没做的那批东西
#  （agent 包 / GUI 二进制 / pybind11 扩展 / 默认配置 / unit 的 Documentation= 文档）。
#  清单在 image/payload.manifest（唯一来源表），这里只负责按清单干活。
#
#  为什么必须由"调用方给 target 树"
#  ---------------------------------------------------------------------------
#  T15-2-10 的教训（docs/image.md §5.9）：整机构建与单包构建是**两棵** output 树，
#  而本仓库的脚本当初自己猜落点，结果整机镜像里少了一整个 rknnlite 而构建全绿。
#  所以：**只认 `--target`（或 buildroot 导出的 TARGET_DIR），绝不自己猜**。
#
#  用法
#  ---------------------------------------------------------------------------
#      # 镜像构建时（post-build 里）
#      bash tools/assistant/build-payload.sh --target "$TARGET_DIR"
#
#      # 开发机上手动跑（源树给仓库，落点给 SDK 的 target）
#      bash image/build-payload.sh --target <target 树> --src-root <仓库根> \
#           [--only copy,gen] [--dry-run]
#
#  `--only` 收 `copy,gen,native,gui` 的任意组合：2-10b-2 只做 copy/gen，
#  native 与 gui 分别由 2-10b-3、2-10b-4 实现（在那之前单独指定它们会明确报错，
#  而不是"静默跳过"——静默跳过正是 rknnlite 那次事故的形态）。
#
#  幂等：落点旁边放内容哈希戳（`.payload-stamp`），一致就跳过；`--force` 可强制。
#  退出码：0 成功；1 落点/源不对或某一步失败；2 用法错。
# ============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

TARGET=""
SRC_ROOT=""
ONLY=""
DRY=""
FORCE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --target) TARGET="${2:-}"; shift ;;
        --target=*) TARGET="${1#--target=}" ;;
        --src-root) SRC_ROOT="${2:-}"; shift ;;
        --src-root=*) SRC_ROOT="${1#--src-root=}" ;;
        --only) ONLY="${2:-}"; shift ;;
        --only=*) ONLY="${1#--only=}" ;;
        --dry-run) DRY="1" ;;
        --force) FORCE="1" ;;
        -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
        *) echo "未知参数: $1" >&2; exit 2 ;;
    esac
    shift
done

# ---------------------------------------------------------------------------
#  落点：只认 --target / TARGET_DIR（见文件头）
# ---------------------------------------------------------------------------
if [ -z "$TARGET" ]; then
    TARGET="${TARGET_DIR:-}"
    [ -n "$TARGET" ] && echo "   target        : $TARGET（buildroot 的 TARGET_DIR）"
fi
if [ -z "$TARGET" ]; then
    echo "!! 必须显式给落点：--target <target 树>（或由 buildroot 的 TARGET_DIR 传入）。" >&2
    echo "!! 这里刻意**不猜** —— 整机构建与单包构建是两棵 output 树，猜错会静默装错树。" >&2
    exit 2
fi
if [ ! -d "$TARGET" ]; then
    echo "!! target 树不存在：$TARGET" >&2
    exit 2
fi

# 源树：默认是注入进来的那份副本（post-build 在 SDK 里跑，够不到我们的仓库）
if [ -z "$SRC_ROOT" ]; then
    SRC_ROOT="${PAYLOAD_SRC:-}"
fi
if [ -z "$SRC_ROOT" ]; then
    # 本脚本在 <SDK>/tools/assistant/ 或 <仓库>/image/ 下都能推断
    if [ -d "$HERE/payload-src" ]; then
        SRC_ROOT="$HERE/payload-src"
    else
        SRC_ROOT="$(cd "$HERE/.." && pwd)"
    fi
fi
MANIFEST=""
for m in "$SRC_ROOT/payload.manifest" "$HERE/payload.manifest" "$HERE/../payload.manifest"; do
    [ -f "$m" ] && { MANIFEST="$m"; break; }
done
if [ -z "$MANIFEST" ]; then
    echo "!! 找不到 payload.manifest（找过：$SRC_ROOT、$HERE、$HERE/..）" >&2
    exit 2
fi

echo "== T15-2-10b payload 安装"
echo "   target        : $TARGET"
echo "   src-root      : $SRC_ROOT"
echo "   manifest      : $MANIFEST"
[ -n "$ONLY" ] && echo "   only          : $ONLY"
[ -n "$DRY" ] && echo "   ** dry-run：只打印，不落盘 **"

# ---------------------------------------------------------------------------
#  小工具
# ---------------------------------------------------------------------------
want() {
    [ -z "$ONLY" ] && return 0
    case ",$ONLY," in *",$1,"*) return 0 ;; *) return 1 ;; esac
}

#: 内容哈希戳：把"这次装进去的东西"的指纹写进落点目录
stamp_of() {
    # $1 = 要算指纹的东西（文件或目录）
    if [ -d "$1" ]; then
        find "$1" -type f -not -name '.payload-stamp' -print0 2>/dev/null \
            | sort -z | xargs -0 -r cat 2>/dev/null | sha256sum | cut -d' ' -f1
    else
        sha256sum "$1" 2>/dev/null | cut -d' ' -f1
    fi
}

stamp_matches() {
    # $1 = 落点（文件或目录）, $2 = 期望指纹
    local sfile
    if [ -d "$1" ]; then sfile="$1/.payload-stamp"; else sfile="$(dirname "$1")/.payload-stamp.$(basename "$1")"; fi
    [ -n "$FORCE" ] && return 1
    [ -f "$sfile" ] && [ "$(cat "$sfile")" = "$2" ]
}

write_stamp() {
    local sfile
    if [ -d "$1" ]; then sfile="$1/.payload-stamp"; else sfile="$(dirname "$1")/.payload-stamp.$(basename "$1")"; fi
    [ -n "$DRY" ] && { echo "       (dry-run) 写戳 $sfile"; return 0; }
    mkdir -p "$(dirname "$sfile")"
    printf '%s\n' "$2" > "$sfile"
}

# ---------------------------------------------------------------------------
#  按清单干活
# ---------------------------------------------------------------------------
n_copy=0; n_skip=0; n_gen=0; n_todo=0
while IFS='|' read -r how src dest; do
    case "$how" in ""|\#*) continue ;; esac
    # 去掉行尾空白与注释
    src="$(printf '%s' "$src" | sed 's/[[:space:]]*$//')"
    dest="$(printf '%s' "$dest" | sed 's/[[:space:]]*#.*$//; s/[[:space:]]*$//')"
    [ -n "$dest" ] || continue

    case "$how" in
    copy)
        want copy || continue
        [ -z "$dest" ] && continue
        srcdir="$SRC_ROOT/${src%/}"
        if [ ! -e "$srcdir" ]; then
            echo "!! 清单里的源不存在：$srcdir（manifest 第 ${src} 行？）" >&2
            exit 1
        fi
        if [ -d "$srcdir" ]; then
            echo "   [copy] $src -> $dest"
            if [ -z "$DRY" ]; then
                mkdir -p "$TARGET/$dest"
                # 目录复制：用 tar 管道（保留权限、排除 __pycache__ 与 pyc）
                ( cd "$srcdir" && tar cf - \
                    --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' . ) \
                  | ( cd "$TARGET/$dest" && tar xf - )
            fi
            n_copy=$((n_copy+1))
        else
            h="$(stamp_of "$srcdir")"
            if stamp_matches "$TARGET/$dest" "$h"; then
                echo "   [copy] $src -> $dest  （已一致，跳过）"
                n_skip=$((n_skip+1))
                continue
            fi
            echo "   [copy] $src -> $dest"
            if [ -z "$DRY" ]; then
                mkdir -p "$(dirname "$TARGET/$dest")"
                cp -a "$srcdir" "$TARGET/$dest"
                chmod 0644 "$TARGET/$dest"
            fi
            write_stamp "$TARGET/$dest" "$h"
            n_copy=$((n_copy+1))
        fi
        ;;
    gen)
        want gen || continue
        srcdir="$SRC_ROOT/${src}"
        [ -f "$srcdir" ] || srcdir="$HERE/../${src}"
        if [ ! -f "$srcdir" ]; then
            echo "!! 清单里的模板不存在：$src" >&2
            exit 1
        fi
        h="$(stamp_of "$srcdir")"
        if stamp_matches "$TARGET/$dest" "$h"; then
            echo "   [gen ] $src -> $dest  （已一致，跳过）"
            n_skip=$((n_skip+1))
            continue
        fi
        echo "   [gen ] $src -> $dest"
        if [ -z "$DRY" ]; then
            mkdir -p "$(dirname "$TARGET/$dest")"
            install -m 0755 "$srcdir" "$TARGET/$dest"
        fi
        write_stamp "$TARGET/$dest" "$h"
        n_gen=$((n_gen+1))
        ;;
    native)
        want native || continue
        # 落点固定为清单里的 dest；真正干活的是 build-native.sh（T15-2-10b-3）
        if [ -x "$HERE/build-native.sh" ]; then
            echo "   [native] 交叉编译 pybind11 扩展 -> $dest"
            if [ -z "$DRY" ]; then
                bash "$HERE/build-native.sh" --target "$TARGET" --src-root "$SRC_ROOT" \
                    --dest "$dest" || { echo "!! native 构建失败" >&2; exit 1; }
            fi
        else
            echo "   [native] 还没实现（image/build-native.sh 属于 T15-2-10b-3）" >&2
            n_todo=$((n_todo+1))
        fi
        ;;
    gui)
        want gui || continue
        if [ -x "$HERE/build-gui.sh" ]; then
            echo "   [gui   ] 交叉编译 Qt5 GUI -> $dest"
            if [ -z "$DRY" ]; then
                bash "$HERE/build-gui.sh" --target "$TARGET" --src-root "$SRC_ROOT" \
                    --dest "$dest" || { echo "!! GUI 构建失败" >&2; exit 1; }
            fi
        else
            echo "   [gui   ] 还没实现（image/build-gui.sh 属于 T15-2-10b-4）" >&2
            n_todo=$((n_todo+1))
        fi
        ;;
    *)
        echo "!! manifest 里有未知的 how：$how" >&2
        exit 1
        ;;
    esac
done < "$MANIFEST"

echo
echo "   copy $n_copy 项、gen $n_gen 项、跳过 $n_skip 项、未实现 $n_todo 项"
if [ "$n_todo" -gt 0 ]; then
    echo "!! 有 $n_todo 项还没实现 —— payload **不完整**（这不是成功，是半成品）" >&2
    exit 1
fi
echo "== payload 完成（target = $TARGET）"
