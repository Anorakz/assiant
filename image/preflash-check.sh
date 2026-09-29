#!/usr/bin/env bash
# ============================================================================
#  image/preflash-check.sh — 刷板前验证（T15-2-10）
#
#  一件事：在**还没有板子**的情况下，把"能在构建机上证明的"全部证明掉，
#  并把"只能板上测的"明确列出来 —— 免得刷完板才发现某个包根本没进镜像。
#
#  为什么要有这个入口（而不是散着跑几个检查器）
#  ---------------------------------------------------------------------------
#  T15-2-10 的第一版就是散着跑的，结果**看错了树**：整机构建（`./build.sh all`）
#  与单包构建（`image/sdk-make.sh`）是两棵 output 树，只差一层同名目录
#  （docs/image.md §5.8），而当时的检查器都写死成单包那棵。于是"整机构建后的验证"
#  其实在验另一棵树，`rknnlite` 明明没进整机镜像却全绿通过了。
#  所以这里：
#    · 统一用 `--target <整机构建的 target 树>`（选树逻辑收在 image/imagelib.py）；
#    · 每个检查器都把"我在看哪棵树"打印出来；
#    · 结果分三段：**在位/闭包/冒烟**（缺了就是 bug）、**payload**（已计划未做）、
#      **只能板上测**（写清楚是哪几项，不要把"没测"混进"通过"里）。
#
#  用法
#  ---------------------------------------------------------------------------
#      wsl -u root bash image/preflash-check.sh <SDK 根目录> [--target <target 树>]
#
#    root 是给 chroot 冒烟用的（qemu-aarch64 已注册在 binfmt_misc 上，直接 chroot 即可）。
#    不给 --target 时自动选整机构建那棵树。
#
#  退出码：0 全过（含 payload）；1 有缺件（必须修）；3 系统层过了但 payload 还没装。
# ============================================================================
set -uo pipefail

SDK="${1:-}"
shift || true
TARGET_ARG=""
while [ $# -gt 0 ]; do
    case "$1" in
        --target) TARGET_ARG="${2:-}"; shift ;;
        --target=*) TARGET_ARG="${1#--target=}" ;;
        *) echo "未知参数: $1" >&2; exit 2 ;;
    esac
    shift
done

if [ -z "$SDK" ] || [ ! -d "$SDK" ]; then
    echo "用法: bash image/preflash-check.sh <SDK 根目录> [--target <target 树>]" >&2
    exit 2
fi
REPO="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
CHK_ARGS=("$SDK")
TGT_ARGS=()
if [ -n "$TARGET_ARG" ]; then
    CHK_ARGS+=(--target "$TARGET_ARG")
    TGT_ARGS=(--target "$TARGET_ARG")
fi

echo "############ T15-2-10 刷板前验证"
echo "# 仓库 : $REPO"
echo "# SDK  : $SDK"
echo "# 时间 : $(date -Is)"
echo "# uid  : $(id -u)（chroot 冒烟要 root；不是 root 会如实标『跳过』）"
echo

echo "############ 1/3 运行时依赖：在位 + DT_NEEDED 闭包 + chroot 冒烟 + payload"
python3 "$REPO/image/check-runtime-deps.py" "${CHK_ARGS[@]}"
rc_deps=$?
echo

echo "############ 2/3 开机目标闭包：default.target 起来之后只有我们的东西"
python3 "$REPO/image/check-assistant-target.py" --sdk "$SDK" "${TGT_ARGS[@]}"
rc_target=$?
echo

echo "############ 3/3 只能板上测的（构建机上给不出证据，别当成"没做"）"
cat <<'EOF'
   · DRM/KMS 真出图：Mali G52 的 EGLFS 实跑、旋转 90°、开机到首帧
     （构建机只能证明 libqeglfs.so 在位且链到 libmali.so.1、gbm_* 符号齐）
   · 触摸：Goodix gt9xx 的 /dev/input/event*、坐标与旋转后的映射
   · 网络：wlan0(rtl8822cs) 联网、NetworkManager 真起、network-online 真的等到了
   · RTC：hym8563 @i2c5 0x51、掉电走时
   · NPU 真推理：rknnlite 能 import 只是第一步，真跑要做 RKNNLite.init_runtime()
   · 硬解真解码：gst-inspect 能看到 mppvideodec 只是第一步，真解码要看帧率与 CPU
   · 温度/功耗/启动耗时：systemd-analyze、/sys/class/thermal、电流表
   · A/B 槽与 OTA：misc 元数据、失败回滚
  → 这些是 T15-2-11 首次刷板的正文。
EOF
echo

echo "############ 汇总"
echo "  运行时依赖 : $([ $rc_deps -eq 0 ] && echo 全过 || ([ $rc_deps -eq 3 ] && echo "系统层过 / payload 未装" || echo "有缺件（退出码 $rc_deps）"))"
echo "  开机目标   : $([ $rc_target -eq 0 ] && echo 全过 || echo "有问题（退出码 $rc_target）")"
if [ $rc_deps -eq 1 ] || [ $rc_target -ne 0 ]; then
    echo "  == 不通过：先修上面的缺件，再谈刷板"
    exit 1
fi
if [ $rc_deps -eq 3 ]; then
    echo "  == 系统层通过；**payload 未装** → 刷板前需要先补 payload（agent/GUI/native/默认配置）"
    exit 3
fi
echo "  == 通过"
