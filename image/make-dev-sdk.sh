#!/usr/bin/env bash
# ============================================================================
#  image/make-dev-sdk.sh — 一条命令造出"开发树"（第二份 SDK，硬链接复制）
#
#  为什么需要第二棵树（T15-2-12 预研结论，2026-10-02）
#  ---------------------------------------------------------------------------
#  开发镜像与发行镜像要**两套 buildroot 配置**。但 SDK 把输出目录写死在
#      device/rockchip/common/scripts/build.sh:  export RK_OUTDIR="$RK_SDK_DIR/output"
#  而且是**多处**硬编码——实测把它改成可由环境变量指定后，`output-dev/` 确实
#  建起来了、release 的 `output/` 也没被动，**但 lunch 仍把配置写进了
#  `output/.config`**。要接着走就得在 vendor 脚本上打几处补丁：改动面越大越难
#  维护，还可能被下次 SDK 更新悄悄破坏。所以选择"同一套配方 + 第二棵树"。
#
#  为什么用 `cp -al`（硬链接）而不是真复制
#  ---------------------------------------------------------------------------
#  实测（2026-10-02）：`cp -al` 64 秒，开发树**实际只多占 582 MB**（65 GB 逻辑
#  大小里绝大部分是硬链接，共享 inode）。更关键的是：buildroot 的产物与 stamp
#  也共享 → 开发树**继承发行镜像已经编好的东西**，首编只需编 dev 新增的那些包，
#  不必从零再编一遍整条工具链。
#
#  ⚠ 硬链接的注意点（已实测过一遍，release 树完好）
#  ---------------------------------------------------------------------------
#  · 改动某个文件（sed -i / cp 覆盖）会**打断该文件的硬链接**，两棵树从此各持一份
#    ——这正是我们要的隔离；只有"原地截断写入"才会互相影响，构建系统极少这么干。
#  · 兜底：真出问题就跑 `bash image/make-dev-sdk.sh <SDK> --copy`（真复制，+39 GB）。
#  · 每次在开发树里构建完，建议跑一次 release 的增量构建核验（几分钟）：
#        bash image/build-image.sh <SDK>
#
#  用法
#  ---------------------------------------------------------------------------
#      bash image/make-dev-sdk.sh <SDK 根目录> [开发树路径] [--copy|--force]
#
#    · 开发树路径默认 = `<SDK 根目录>-dev`（必须是这个后缀，脚本有安全护栏）
#    · `--copy`  ：真复制（不用硬链接）
#    · `--force` ：已存在就删掉重建（默认是"存在则只重新注入配方"）
# ============================================================================
set -euo pipefail

SDK="${1:-}"
shift || true

if [ -z "$SDK" ]; then
    echo "用法: bash image/make-dev-sdk.sh <SDK 根目录> [开发树路径] [--copy|--force]" >&2
    exit 2
fi
SDK="$(readlink -f "$SDK")"
[ -d "$SDK" ] || { echo "!! SDK 目录不存在: $SDK" >&2; exit 2; }

DEV=""
COPY=no
FORCE=no
for arg in "$@"; do
    case "$arg" in
        --copy)  COPY=yes ;;
        --force) FORCE=yes ;;
        -*)      echo "!! 不认识的参数: $arg" >&2; exit 2 ;;
        *)       DEV="$arg" ;;
    esac
done
if [ -z "$DEV" ]; then
    DEV="${SDK}-dev"
fi
DEV="$(readlink -m "$DEV")"

# ---- 安全护栏：删除前必须确认"这就是我们要开发的树" ------------------------
#  这套护栏的存在理由很直白：下面有 rm -rf。万一有人把参数写成 "/" 或写成一个
#  正常目录，护栏必须挡下来。
if [ "$DEV" = "/" ] || [ -z "$DEV" ]; then
    echo "!! 开发树路径不合法: '$DEV'" >&2
    exit 2
fi
case "$(basename "$DEV")" in
    *-dev) : ;;
    *) echo "!! 开发树目录名必须以 -dev 结尾（安全护栏）：$DEV" >&2; exit 2 ;;
esac
if [ "$DEV" = "$SDK" ]; then
    echo "!! 开发树不能就是 SDK 自己" >&2
    exit 2
fi

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "== 开发树"
echo "   源 SDK : $SDK"
echo "   开发树 : $DEV"
echo "   方式   : $([ "$COPY" = yes ] && echo '真复制 (cp -a)' || echo '硬链接 (cp -al，省空间且继承已编译产物)')"

if [ -e "$DEV" ]; then
    if [ "$FORCE" = yes ]; then
        echo "   --force：删除现有开发树后重建"
        rm -rf -- "$DEV"
    else
        echo "   已存在 → 只重新注入配方（要重建请加 --force）"
    fi
fi

if [ ! -e "$DEV" ]; then
    t0=$(date +%s)
    if [ "$COPY" = yes ]; then
        cp -a "$SDK" "$DEV"
    else
        cp -al "$SDK" "$DEV"
    fi
    t1=$(date +%s)
    echo "   复制完成，用时 $((t1 - t0)) 秒"
fi

echo
echo "== 注入配方到开发树（与发行树同一套，另有 dev 专属三个文件）"
bash "$REPO/image/install-into-sdk.sh" "$DEV" | grep -E 'dev|新增|覆盖' | sed 's/^/   /' || true

echo
echo "== 完成。构建开发镜像："
echo "     wsl -u root bash image/build-image.sh --flavor dev $DEV"
echo "   （首次会编 dev 新增的包；发行树 $SDK 不受影响）"
echo "   ⚠ root 跑完记得把属主改回来：chown -R anorak:anorak $DEV/output $DEV/buildroot/output"
