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

# 3b) **把"等网"限时**（T15-2-11 板端：没网时每次开机白等）
#  ---------------------------------------------------------------------------
#  buildroot 那份 `NetworkManager-wait-online.service` 跑的是 `nm-online -s -q`，
#  没有 `-t` → 用默认超时。板子没网时会等到超时，而 `agent.service` 是
#  `After=network-online.target` → 整条启动链被拖着等。
#  这里加 drop-in 把它限到 5 秒：保留 T15-2-8 的意图（"agent 起来时网络尽量已经就绪"），
#  但不再让"网永远不来"拖死开机（Agent 自己有 LinkGuard 处理链路变化）。
#  注意 `ExecStart=` 空一行是 systemd 的标准手法：先把原值清空，再给新值。
mkdir -p "$ETC/NetworkManager-wait-online.service.d"
cat > "$ETC/NetworkManager-wait-online.service.d/timeout.conf" <<'EOF'
# assistant: 限时等网（没网时不该拖住 agent/gui 的启动）
[Service]
ExecStart=
ExecStart=/usr/bin/nm-online -s -q -t 5
EOF
echo "   + NetworkManager-wait-online.service.d/timeout.conf（等网限时 5s）"

# 3c) **mask 掉 networkd 的等网器**（T15-2-11 板端实测：这才是"启动很久"的真凶）
#  ---------------------------------------------------------------------------
#  `90-systemd.preset` 第 21 行是 `enable systemd-networkd-wait-online.service` ——
#  buildroot 把 **networkd 的等待器**打开了，而这个镜像里 networkd **没有任何 link 可管**
#  （`/etc/systemd/network/` 是空的；网络全归 NetworkManager）。于是它的启动任务
#  **无超时地干等**，把 `network-online.target` 顶住，而我们的
#  `assistant.target` / `agent.service` 都挂在那上面 → agent/gui 一直不启动。
#  实测日志（冷启动）：
#      [  8.0] Finished Network Manager Wait Online.        ← NM 自己这个 8 秒就过了
#      [ 11s–19s+] A start job is running for "Wait for Network to be Configured" (no limit)
#  所以：mask 它（`/dev/null`）。networkd 本体留着（无害；T15-12 收最小镜像时再议）。
if [ -f "$SYSD/systemd-networkd-wait-online.service" ]; then
    ln -sfn /dev/null "$ETC/systemd-networkd-wait-online.service"
    echo "   - 已 mask systemd-networkd-wait-online.service（networkd 等待器，无超时会拖死启动）"
fi

# 3d) 单元权限：**必须是 0644**（T15-2-11 板端日志：systemd 抱怨 marked executable）
#  我们的 overlay 文件在 Windows 盘上（DrvFs）权限是 777，tar/cp 进镜像就变 0755，
#  systemd 会对每个单元打一行警告。顺手把 overlay 里的单元统一成 0644。
for u in assistant.target agent.service agent-gui.service assistant-init.service; do
    [ -f "$SYSD/$u" ] && chmod 0644 "$SYSD/$u"
done
echo "   + 我们的 4 个单元已 chmod 0644（不再被 systemd 抱怨 executable）"

# 4) **串口控制台要有 shell**（T15-2-11 板端实测）
#  ---------------------------------------------------------------------------
#  镜像里 cmdline 是 `console=ttyFIQ0`，但 systemd 的 getty 生成器**不给 ttyFIQ0 建 getty**
#  （它是 Rockchip 的 fiq_debugger tty，不是标准串口）—— 于是板子起来之后串口上
#  只有内核日志、没有登录提示：出问题时"看不见、也没法敲命令"。首刷那天就是被这个
#  卡住的（明明起来了，却只能在串口干瞪眼）。
#  这里显式 enable 一份 serial-getty（`%I` 就是设备名 → /dev/ttyFIQ0）。
#  ⚠ 这条通道也是 T15-7 的 "cli 唤醒" 要用的（睡眠关屏后靠触摸 + 串口两条路唤醒）。
mkdir -p "$ETC/getty.target.wants"
if [ -f "$SYSD/serial-getty@.service" ]; then
    ln -sfn "/usr/lib/systemd/system/serial-getty@.service" \
            "$ETC/getty.target.wants/serial-getty@ttyFIQ0.service"
    echo "   + getty.target.wants/serial-getty@ttyFIQ0.service（串口 shell）"
else
    echo "   !! 没有 serial-getty@.service —— 串口不会有 shell" >&2
fi

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

# ---------------------------------------------------------------------------
#  T15-2-11：把本机 WiFi 凭据装进镜像（有就装、没有就跳过）
#  ---------------------------------------------------------------------------
#  板子只有 wlan0 能通；镜像里没有网络配置的话，首刷后"起来了但没网也没 shell"。
#  凭据由 image/install-into-sdk.sh 从 image/local/*.nmconnection（不进仓库）
#  注入到 tools/assistant/local/，NetworkManager 的 keyfile 格式。
#  ⚠ 支持**多份**：现场可能把 SSID 记错（这次就是 Anorak_host / Anroak_host 差两个字母），
#    多装一份 autoconnect 的配置不影响任何事，谁对连谁。
NMSRC="$TOOLS/local"
if [ -d "$NMSRC" ] && ls "$NMSRC"/*.nmconnection >/dev/null 2>&1; then
    NMDIR="$TARGET_DIR/etc/NetworkManager/system-connections"
    mkdir -p "$NMDIR"
    for f in "$NMSRC"/*.nmconnection; do
        install -m 0600 "$f" "$NMDIR/$(basename "$f")"
        echo "   + WiFi 凭据：$(basename "$f")（0600）"
    done
else
    echo "   = 没有本机 WiFi 凭据（tools/assistant/local/*.nmconnection）—— 镜像里不含 WiFi 配置"
fi

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

# --- 4) payload：我们的 agent / GUI / native / 默认配置（T15-2-10b-5）---------
#  这是"unit 文件早就指向、但一直没装"的那批东西（docs/image.md §5.9 的 F5）：
#  缺了它 assistant.target 起得来、agent/gui 会一直重启（No module named agent）。
#  ⚠ 源在 <SDK>/tools/assistant/payload-src/（由 image/install-into-sdk.sh 注入）——
#    post-build 在 SDK 里跑，够不到我们的仓库。
#  ⚠ 落点一律传 $TARGET_DIR（T15-2-10 的教训：脚本自己猜树会静默装到另一棵树）。
if [ -x "$TOOLS/build-payload.sh" ]; then
    echo "== [assistant post-build] payload（agent / GUI / native / 配置模板）"
    bash "$TOOLS/build-payload.sh" --target "$TARGET_DIR" \
         --src-root "$TOOLS/payload-src" \
        || { echo "!! payload 安装失败" >&2; exit 1; }
else
    echo "!! 找不到 $TOOLS/build-payload.sh（先跑 image/install-into-sdk.sh）" >&2
    exit 1
fi

echo "== [assistant post-build] 完成"
