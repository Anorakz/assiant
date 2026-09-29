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

# ---------------------------------------------------------------------------
#  T15-2-8：开机目标 = assistant.target，并把它要拉的东西 enable 上
#  ---------------------------------------------------------------------------
#  为什么是 `ln -sf` 而不是 `systemctl enable`：
#    enable 只是"建符号链接"这一件事，用 ln 做**不需要主机上有 systemd**、
#    也不受 systemctl 版本/preset 影响，构建机上可复现。
SYSD="$TARGET_DIR/usr/lib/systemd/system"
ETC="$TARGET_DIR/etc/systemd/system"

# 1) 开机目标：default.target -> assistant.target
ln -sfn "/usr/lib/systemd/system/assistant.target" "$ETC/default.target"
echo "   + default.target -> assistant.target"

# 2) assistant.target.wants/：单元自己的 [Install] WantedBy 落地形式
mkdir -p "$ETC/assistant.target.wants"
for u in assistant-init.service agent.service agent-gui.service; do
    if [ -f "$SYSD/$u" ]; then
        ln -sfn "/usr/lib/systemd/system/$u" "$ETC/assistant.target.wants/$u"
    else
        echo "   !! 缺单元文件 $SYSD/$u（overlay 没铺上？）" >&2
        exit 1
    fi
done
echo "   + assistant.target.wants/ 已挂上我们的三个单元"

# 3) network-online.target 要真的"等网"：buildroot 并不启用 wait-online，
#    而我们的 target 只在 basic.target 之外依赖它，不挂它就是个空等。
mkdir -p "$ETC/network-online.target.wants"
ln -sfn "/usr/lib/systemd/system/NetworkManager-wait-online.service" \
        "$ETC/network-online.target.wants/NetworkManager-wait-online.service" 2>/dev/null || true
echo "   + network-online.target.wants/NetworkManager-wait-online.service"

echo "   ---- /etc/systemd/system 现在长这样："
ls -la "$ETC" | sed 's/^/     /'

# ---------------------------------------------------------------------------
#  T15-2-10：屏蔽厂商的调试/传输类常驻服务
#  ---------------------------------------------------------------------------
#  实测（对着**整机构建**那棵树查闭包，见 image/preflash-check.sh）：厂商在
#  `local-fs.target.wants/` 里挂了 usb-gadget.service，而它是 `Type=simple` 的常驻服务
#  （/usr/bin/usb-gadget start，adb/rndis 那一类），**每次开机都会起来** ——
#  这与 T15-2-8 的出口"只起我们的东西"直接矛盾。
#  另外一个厂商服务 wifibt-init.service（sysinit.target.wants/）**要留**：
#  wlan0(rtl8822cs) 的固件是它加载的，屏蔽了就没网（它在检查器的白名单里有理由）。
#  屏蔽方式用 systemd 的标准做法：`/etc/systemd/system/<unit> -> /dev/null`；
#  开发镜像可以设 ASSISTANT_FLAVOR=dev 保留它（T15-2-12 用得上 adb）。
MASK_UNITS="usb-gadget.service"
for u in $MASK_UNITS; do
    if [ "${ASSISTANT_FLAVOR:-release}" = "dev" ]; then
        echo "   ~ 开发镜像：保留 $u（不屏蔽）"
        continue
    fi
    if [ -e "$SYSD/$u" ]; then
        ln -sfn /dev/null "$ETC/$u"
        echo "   - 已屏蔽厂商常驻服务 $u（$ETC/$u -> /dev/null）"
    fi
done

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
    # ⚠ 必须把 TARGET_DIR 传下去（T15-2-10 修的）：
    #   整机构建与单包构建是**两棵** output 树（docs/image.md §5.8），而这个脚本第一版
    #   自己算落点，算的是**单包那棵** —— 于是 T15-2-9 的整机镜像里根本没进 rknnlite，
    #   构建却全绿（它还因为那棵树里留着 T15-2-7 的旧戳，打印"已经一致"就退了）。
    #   post-build 环境里的 TARGET_DIR 就是当前这棵 target 树，传它最准。
    bash "$TOOLS/prepare-rknnlite.sh" "$SDK" --target "$TARGET_DIR" \
        || { echo "!! rknnlite 安装失败" >&2; exit 1; }
else
    echo "!! 找不到 $TOOLS/prepare-rknnlite.sh" >&2
    exit 1
fi

echo "== [assistant post-build] 完成"
