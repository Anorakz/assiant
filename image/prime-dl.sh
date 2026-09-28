#!/usr/bin/env bash
# ============================================================================
#  image/prime-dl.sh — 把 Qt 源码包"原始的、与 buildroot 钉的 hash 一致的那份"
#  预置进 buildroot 的下载缓存（T15-2-6 起）
#
#  为什么要这一步（T15-2-6 实测踩到的）
#  ---------------------------------------------------------------------------
#  这个 buildroot 的 Qt5 不走"官方发行 tarball"，而是 `invent.kde.org` 的
#  **按 commit 现生成的归档**：
#      QT5_SITE = https://invent.kde.org/qt/qt
#      QT5BASE_SITE = $(QT5_SITE)/qtbase/-/archive/$(QT5BASE_VERSION)
#  问题是 KDE 的 GitLab 会**重新打包**这些归档：同一 commit、同样内容，外层 tar
#  的字节变了 —— 于是 buildroot 里钉的 sha256 对不上，构建在**下载校验**就死：
#      ERROR: qtbase-<commit>.tar.bz2 has wrong sha256 hash:
#      ERROR: expected: 935d01f5...   got: 3067c4d8...
#      ERROR: Incomplete download, or man-in-the-middle (MITM) attack
#  （实测：KDE 那份 57,931,736 B；buildroot 钉的那份 57,929,506 B。两次下载
#    KDE 的字节是**确定**的，说明不是传输损坏，是服务端换了打包方式。）
#
#  **怎么证明 KDE 那份内容是对的**（我们确实验过，不是猜）：
#    · 包内 `.qmake.conf` 写着 MODULE_VERSION = 5.15.11（就是这个版本）；
#    · 包内 LICENSE.LGPLv3 / LICENSE.GPL2 / LICENSE.FDL 的 sha256 与 buildroot
#      在同一个 .hash 文件里钉的**逐文件哈希完全一致** → 内容就是它记的那份源码。
#  所以这不是"来路不明的包"，而是"同一个源码树的另一种打包"。
#
#  **但我们的修法不是改 hash**（那会跟着 KDE 的打包行为漂，而且等于改厂商树）。
#  buildroot 的 primary site 上放着**原始的、与 hash 一致的那份**：
#      https://sources.buildroot.net/<包名>/<源文件名>
#  本脚本就把它逐个下载下来、**用 buildroot 自己钉的 sha256 校验**，放进
#      <SDK>/buildroot/dl/<包名>/<源文件名>
#  buildroot 看到 dl 里已有且校验通过的文件就不再下载，也永远不会 fallback 到
#  invent.kde.org 那份重打包的。
#
#  为什么要按 .config 过滤：qt5 目录下有 qtwebengine 这种几百 MB 的包，我们没启用，
#  不能顺手全下。判据是 buildroot 自己的命名规律：包目录名 qt5base → 符号
#  （BR2_ 前缀 + 包名大写）。
#
#  用法
#  ---------------------------------------------------------------------------
#      bash image/prime-dl.sh <SDK 根目录> [--config <配置文件>] [--dry-run]
#
#  "哪些 Qt 包启用了"优先读 output/<cfg>/.config；**还没有 .config 时回落到我们自己的
#  products 片段**（配方里写了什么就是什么），所以注入之后、defconfig 之前也能先预置。
#  幂等：dl 里已有且 sha256 一致 = "已经一致"，不重复下载。
# ============================================================================
set -euo pipefail

SDK="${1:-}"
shift || true
DRY=""
CONF_OVERRIDE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY="1" ;;
        --config)  CONF_OVERRIDE="${2:-}"; shift ;;
        *) echo "未知参数: $1" >&2; exit 2 ;;
    esac
    shift
done

if [ -z "$SDK" ]; then
    echo "用法: bash image/prime-dl.sh <SDK 根目录> [--config <配置文件>] [--dry-run]" >&2
    exit 2
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CFG="${IMG_CFG:-rockchip_rk3568_kickpi_k1mini_release}"
MIRROR="${IMG_PRIMARY_SITE:-https://sources.buildroot.net}"
BR="$SDK/buildroot"
CONF="$BR/output/$CFG/.config"

[ -d "$BR/package/qt5" ] || { echo "不是 Rockchip SDK 的 buildroot: $BR" >&2; exit 2; }

# 判断"哪些 Qt 包启用"用哪份文件：优先生成好的 .config；没有就回落到**我们自己的
# 片段**（配方里写了哪些 Qt 包是确定的），这样注入之后、defconfig 之前也能先预置。
CONF_SRC=""
if [ -n "$CONF_OVERRIDE" ]; then
    CONF_SRC="$CONF_OVERRIDE"
elif [ -f "$CONF" ]; then
    CONF_SRC="$CONF"
else
    CONF_SRC="$HERE/buildroot/configs/rockchip/products/kickpi-k1mini-release.config"
fi
if [ ! -f "$CONF_SRC" ]; then
    echo "!! 找不到用来判断启用项的文件: $CONF_SRC" >&2
    echo "   请先跑 defconfig，或用 --config 指定。" >&2
    exit 2
fi

FETCH=""
if command -v wget >/dev/null 2>&1; then
    FETCH="wget"
elif command -v curl >/dev/null 2>&1; then
    FETCH="curl"
else
    echo "!! 需要 wget 或 curl" >&2
    exit 2
fi

# ---------------------------------------------------------------------------
#  .mk 里的 _SITE / _SOURCE 是**变量引用**（`$(QT5_SITE)/qtbase/-/archive/$(QT5BASE_VERSION)`），
#  直接对文本做匹配会全部漏掉 —— 实测第一次就是这么跳过了全部 6 个包。
#  所以先把两边 .mk 里的 `名字 = 值` 读进一张表，再把 `$(名字)` 迭代展开。
# ---------------------------------------------------------------------------
declare -A VARS
load_vars() {
    local line k v
    while IFS= read -r line; do
        case "$line" in \#*) continue ;; esac
        if [[ "$line" =~ ^([A-Za-z0-9_]+)[[:space:]]*=(.*)$ ]]; then
            k="${BASH_REMATCH[1]}"
            v="${BASH_REMATCH[2]}"
            v="${v#"${v%%[![:space:]]*}"}"
            v="${v%"${v##*[![:space:]]}"}"
            VARS[$k]="$v"
        fi
    done < "$1"
}
expand() {
    local s="$1" i=0 name
    while (( i++ < 8 )); do
        [[ "$s" =~ \$\(([A-Za-z0-9_]+)\) ]] || break
        name="${BASH_REMATCH[1]}"
        [ -n "${VARS[$name]+x}" ] || break
        s="${s/\$($name)/${VARS[$name]}}"
    done
    printf '%s' "$s"
}

load_vars "$BR/package/qt5/qt5.mk"
QT5_SITE_RESOLVED="$(expand '$(QT5_SITE)')"

echo "== Qt 源码预置（用 buildroot 钉的 sha256 校验原始字节）"
echo "   SDK    : $SDK"
echo "   配置   : $CFG"
echo "   判据   : $CONF_SRC"
echo "   镜像   : $MIRROR"
echo "   Qt 来源: $QT5_SITE_RESOLVED"

n_ok=0
n_new=0
n_skip=0
n_bad=0
failed=""

for dir in "$BR"/package/qt5/*/; do
    name="$(basename "$dir")"
    sym="BR2_PACKAGE_$(printf '%s' "$name" | tr 'a-z' 'A-Z')"
    if ! grep -q "^${sym}=y" "$CONF_SRC"; then
        continue
    fi
    mkfile="$dir$name.mk"
    hashfile="$dir$name.hash"
    [ -f "$mkfile" ] || continue
    [ -f "$hashfile" ] || { echo "   !! $name 没有 .hash，跳过"; continue; }
    load_vars "$mkfile"

    site="$(expand "$(sed -n 's/^[A-Z0-9_]*_SITE[[:space:]]*=[[:space:]]*//p' "$mkfile" | head -1)")"
    src="$(expand "$(sed -n 's/^[A-Z0-9_]*_SOURCE[[:space:]]*=[[:space:]]*//p' "$mkfile" | head -1)")"
    if [ -z "$src" ]; then
        echo "   - $name: 没有 _SOURCE，跳过"
        continue
    fi

    # 只处理"KDE 按 commit 现生成"的那类归档：主机是 invent.kde.org，
    # 或者文件名长得就是 <pkg>-<40 位 commit>.tar.bz2（双重判据，避免漏判）。
    is_kde=0
    case "$site" in *invent.kde.org*) is_kde=1 ;; esac
    if printf '%s' "$src" | grep -qE -- '-[0-9a-f]{40}\.tar\.bz2$'; then is_kde=1; fi
    if [ "$is_kde" != "1" ]; then
        echo "   - $name: 不是 KDE 的 commit 归档（$src），跳过"
        continue
    fi

    want="$(awk -v f="$src" '$1=="sha256" && $3==f {print $2; exit}' "$hashfile")"
    if [ -z "$want" ]; then
        echo "   !! $name: .hash 里没有 $src 的 sha256"
        n_bad=$((n_bad + 1)); failed="$failed $name"
        continue
    fi

    dest="$BR/dl/$name/$src"
    if [ -f "$dest" ] && [ "$(sha256sum "$dest" | cut -d' ' -f1)" = "$want" ]; then
        printf '   = 已经一致: dl/%s/%s\n' "$name" "$src"
        n_skip=$((n_skip + 1))
        continue
    fi

    url="$MIRROR/$name/$src"
    printf '   + 取回   : %s <- %s\n' "dl/$name/$src" "$url"
    if [ -n "$DRY" ]; then
        n_new=$((n_new + 1))
        continue
    fi

    mkdir -p "$(dirname "$dest")"
    tmp="$dest.part"
    rm -f "$tmp"
    if [ "$FETCH" = "wget" ]; then
        wget -q --no-check-certificate -t 3 -O "$tmp" "$url" || true
    else
        curl -sL --max-time 900 -o "$tmp" "$url" || true
    fi
    got="$( [ -f "$tmp" ] && sha256sum "$tmp" | cut -d' ' -f1 || echo none )"
    if [ "$got" != "$want" ]; then
        printf '   !! %s 校验失败\n      期望 %s\n      实得 %s\n' "$name" "$want" "$got" >&2
        rm -f "$tmp"
        n_bad=$((n_bad + 1)); failed="$failed $name"
        continue
    fi
    mv -f "$tmp" "$dest"
    printf '     = 校验通过 %s (%s B)\n' "$want" "$(stat -c %s "$dest")"
    n_ok=$((n_ok + 1))
done

echo "== 小结：新取回 $n_ok 个、已一致 $n_skip 个、失败 $n_bad 个"
if [ "$n_bad" != "0" ]; then
    echo "!! 这些包没能拿到与 hash 一致的字节:$failed" >&2
    echo "   如果 sources.buildroot.net 上没有（404），说明该版本没被 buildroot 的镜像缓存过；" >&2
    echo "   那就只能改用 invent.kde.org 那份（内容可验）并同步更新 buildroot 里钉的 hash。" >&2
    exit 1
fi
echo "== 完成。现在 buildroot 不会再 fallback 到 invent.kde.org 那份重打包归档。"
