#!/bin/bash
# board/rockchip/kickpi/k1mini/post-build.sh
# ============================================================================
#  K1 Mini「助手」镜像的 buildroot post-build 钩子（由
#  BR2_ROOTFS_POST_BUILD_SCRIPT 调用，T15-2-7）
#
#  buildroot 会在**根文件系统装配完成、镜像生成之前**调用它，并把这些变量放进环境：
#      TARGET_DIR / HOST_DIR / BUILD_DIR / BINARIES_DIR / BASE_DIR / BR2_CONFIG
#
#  这里干三件事（都是"配方文件装不进去、只能在构建期做"的）：
#    1. **/data（userdata 分区）的落点**：建挂载点 + 往 /etc/fstab 补一行。
#       为什么不是放 overlay 里：overlay 会**整份替换** /etc/fstab，
#       而 buildroot 生成的那份里有根文件系统那行（`/dev/root / auto rw 0 1`），
#       替换掉等于把根挂载项弄丢。所以用追加 + 幂等判断。
#    2. **llama-server**：交叉编译并装进 /usr/lib/assistant/llm/bin
#       （见 docs/image.md §5.6 与 image/build-llama.sh）
#    3. **rknnlite**：把 SDK 里 cp311 那份 wheel 解进 site-packages
#       （见 image/prepare-rknnlite.sh）
#
#  这两个工具由 image/install-into-sdk.sh 注入到 <SDK>/tools/assistant/，
#  所以这个脚本只依赖 SDK 自己的路径。
# ============================================================================
set -e

# SDK 根：**从脚本自己的位置推**，不依赖 buildroot 导出的变量语义
#   本脚本在 <SDK>/buildroot/board/rockchip/kickpi/k1mini/post-build.sh
#   → 往上五级就是 <SDK>（kickpi ← rockchip ← board ← buildroot ← SDK）
SELF="$(readlink -f "$0")"
SDK="$(cd "$(dirname "$SELF")/../../../../.." && pwd)"
TOOLS="$SDK/tools/assistant"
echo "== [assistant post-build] SDK=$SDK"
echo "   TARGET_DIR=$TARGET_DIR"

# --- 1) /data 挂载点 + fstab -------------------------------------------------
mkdir -p "$TARGET_DIR/data"

FSTAB="$TARGET_DIR/etc/fstab"
DATA_LINE='# assistant: 共享数据分区（模型/配置/日志都在这；A/B OTA 只换系统槽）'
DATA_MNT='/dev/disk/by-partlabel/userdata  /data  ext4  defaults,noatime  0  2'

if [ -f "$FSTAB" ]; then
    if grep -q "by-partlabel/userdata" "$FSTAB"; then
        echo "   = /etc/fstab 里已经有 userdata（跳过）"
    else
        {
            echo ""
            echo "$DATA_LINE"
            echo "$DATA_MNT"
        } >> "$FSTAB"
        echo "   + 已往 /etc/fstab 追加 userdata -> /data"
    fi
else
    printf '%s\n%s\n' "$DATA_LINE" "$DATA_MNT" > "$FSTAB"
    echo "   + 已创建 /etc/fstab（只有 userdata 那行）"
fi
echo "   ---- /etc/fstab 现在长这样："
sed 's/^/     /' "$FSTAB"

# 目录骨架：代码在 rootfs，可写状态在 /data（T15-2-7 的 D7）
for d in assistant assistant/llm assistant/llm/config assistant/llm/run assistant/llm/logs \
         assistant/logs assistant/run model; do
    mkdir -p "$TARGET_DIR/data/$d"
done
echo "   + 已建 /data 目录骨架（assistant/、model/）"

# --- 2) llama-server（交叉编译）---------------------------------------------
if [ -x "$TOOLS/build-llama.sh" ]; then
    echo "== [assistant post-build] llama.cpp"
    bash "$TOOLS/build-llama.sh" "$SDK" --dest "$TARGET_DIR/usr/lib/assistant/llm/bin" \
        || { echo "!! llama.cpp 交叉编译失败" >&2; exit 1; }
else
    echo "!! 找不到 $TOOLS/build-llama.sh（先跑 image/install-into-sdk.sh）" >&2
    exit 1
fi

# --- 3) rknnlite -------------------------------------------------------------
if [ -x "$TOOLS/prepare-rknnlite.sh" ]; then
    echo "== [assistant post-build] rknnlite"
    bash "$TOOLS/prepare-rknnlite.sh" "$SDK" || { echo "!! rknnlite 安装失败" >&2; exit 1; }
else
    echo "!! 找不到 $TOOLS/prepare-rknnlite.sh" >&2
    exit 1
fi

echo "== [assistant post-build] 完成"
