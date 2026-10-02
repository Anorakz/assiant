#!/usr/bin/env bash
# ============================================================================
#  image/install-into-sdk.sh — 把本仓库的镜像配方**注入**厂商 SDK（T15-2）
#
#  为什么要有这个脚本
#  ---------------------------------------------------------------------------
#  配方（buildroot 片段 / defconfig / 板级 defconfig / 内核 dts·dtsi）由**我们的仓库**
#  维护，厂商 SDK 只是一棵被注入的树：
#    · 好处 1：配方能 code review、能回滚、能进 CI（docs/image.md 引用它）；
#    · 好处 2：SDK 里"哪些是厂商的、哪些是我们加的"一眼可辨；
#    · 好处 3：换 SDK 版本时重跑这个脚本即可，不用手工再点一遍。
#
#  用法
#  ---------------------------------------------------------------------------
#      bash image/install-into-sdk.sh <SDK 根目录> [--dry-run]
#
#      # 例（WSL）：
#      bash image/install-into-sdk.sh \
#        /home/anorak/rk3568_buildroot/linux-kernel-6.1/rk-linux6.1-2026060914/rk-linux6.1-2026060914
#
#  只做"复制我们的文件进去"：目标已存在且内容不同 = 覆盖（打印 ~ 覆盖）；
#  内容相同 = 跳过（= 已一致）。重复执行幂等。
#  ⚠ 我们的内核 dts/dtsi 一律用 **-assistant 后缀的新文件名**，不覆盖厂商同名文件。
# ============================================================================
set -euo pipefail

SDK="${1:-}"
DRY=""
if [ "${2:-}" = "--dry-run" ]; then
    DRY="1"
fi

if [ -z "$SDK" ]; then
    echo "用法: bash image/install-into-sdk.sh <SDK 根目录> [--dry-run]" >&2
    exit 2
fi
if [ ! -d "$SDK/buildroot" ] || [ ! -d "$SDK/device/rockchip" ]; then
    echo "看起来不是 Rockchip SDK 根目录（缺 buildroot/ 或 device/rockchip/）: $SDK" >&2
    exit 2
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DTS_DIR="kernel-6.1/arch/arm64/boot/dts/rockchip"
# 启动图（logo.bmp / logo_kernel.bmp）在**内核树根**下，mk-kernel.sh 从那儿取。
# 树名可能是 kernel-6.1 或 kernel，探测一下（不写死）。
if [ -d "$SDK/kernel-6.1" ]; then
    KERNEL_LOGO_DIR="kernel-6.1"
elif [ -d "$SDK/kernel" ]; then
    KERNEL_LOGO_DIR="kernel"
else
    KERNEL_LOGO_DIR="kernel-6.1"     # 兜底：报错信息里也能看出预期路径
fi
FAILED=0

# install_file <本仓库相对路径> <SDK 相对路径>
install_file() {
    local src="$HERE/../$1"
    local dst="$SDK/$2"
    if [ ! -f "$src" ]; then
        echo "!! 源文件不在: $src" >&2
        FAILED=1
        return
    fi
    if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
        echo "   = 已经一致: $2"
        return
    fi
    if [ -f "$dst" ]; then
        echo "   ~ 覆盖: $2"
    else
        echo "   + 新增: $2"
    fi
    if [ -z "$DRY" ]; then
        mkdir -p "$(dirname "$dst")"
        cp "$src" "$dst"
    fi
}

echo "== 注入配方到 SDK: $SDK"
# buildroot：defconfig + 我们自己的片段
install_file image/buildroot/configs/rockchip_rk3568_kickpi_k1mini_release_defconfig \
             buildroot/configs/rockchip_rk3568_kickpi_k1mini_release_defconfig
install_file image/buildroot/configs/rockchip/products/kickpi-k1mini-release.config \
             buildroot/configs/rockchip/products/kickpi-k1mini-release.config
# 厂商快照的**包定义**缺件修补（T15-2-9）：libxcrypt.mk 少了 host 变体，
# 而 systemd 的 HOST_SYSTEMD_DEPENDENCIES 要 host-libxcrypt
# → 整机构建死在 "No rule to make target 'host-libxcrypt'"。
# 这是"覆盖同名文件"（注入时会打印 ~ 覆盖），target 侧行为不变。
install_file image/buildroot/package/libxcrypt/libxcrypt.mk \
             buildroot/package/libxcrypt/libxcrypt.mk
# SDK 板级 defconfig（lunch 用）+ 我们的 A/B 分区表
install_file image/device/rockchip/.chips/rk3566_rk3568/rockchip_rk3568_kickpi_k1mini_release_defconfig \
             device/rockchip/.chips/rk3566_rk3568/rockchip_rk3568_kickpi_k1mini_release_defconfig
install_file image/device/rockchip/.chips/rk3566_rk3568/parameter-assistant-ab.txt \
             device/rockchip/.chips/rk3566_rk3568/parameter-assistant-ab.txt
# u-boot 的 A/B 片段（T15-2-11 救砖）：厂商只给 rk3588/rv1126/rk3576 带了 -ab.config，
# rk3568 没有 → 我们补一份；板级 defconfig 里用 RK_UBOOT_CFG_FRAGMENTS 指过来。
install_file image/uboot/rk3568-assistant-ab.config \
             u-boot/configs/rk3568-assistant-ab.config
# 内核：我们的板级 dts/dtsi（全部 -assistant 后缀，不动厂商同名文件）
install_file image/kernel/rk3568-kickpi-k1Mini-assistant.dts "$DTS_DIR/rk3568-kickpi-k1Mini-assistant.dts"
install_file image/kernel/rk3568-kickpi-k1Mini-assistant.dtsi "$DTS_DIR/rk3568-kickpi-k1Mini-assistant.dtsi"
install_file image/kernel/rk3568-kickpi-assistant-overrides.dtsi \
             "$DTS_DIR/rk3568-kickpi-assistant-overrides.dtsi"

# ---------------------------------------------------------------------------
#  T15-2-7：overlay 里的运行期脚本 + 构建工具 + post-build 钩子
#  ---------------------------------------------------------------------------
#  · llm/scripts/* 进 overlay（镜像里落在 /usr/lib/assistant/llm/scripts/）。
#    仓库里只维护这一份来源，overlay 那份由这里复制生成——避免两处漂。
#  · post-build.sh 进 buildroot/board/rockchip/kickpi/k1mini/（片段里用全量值挂上去）。
#  · build-llama.sh / prepare-rknnlite.sh / check-runtime-deps.py 进 <SDK>/tools/assistant/：
#    post-build 钩子是**在 SDK 里**跑的，够不到我们的仓库，所以工具得跟着进去。
#  ⚠ 目的地是 `buildroot/board/...` 而不是 `board/...`：buildroot 片段里的路径是
#    **以 buildroot 为根**解析的，但注入是相对 SDK 根写文件——第一版就写错了一级，
#    结果 post-build.sh 落在 <SDK>/board/ 下，buildroot 根本看不到它。
OVERLAY_DIR="buildroot/board/rockchip/kickpi/k1mini/rootfs-overlay"
for f in lib.sh start.sh stop.sh status.sh restart.sh; do
    install_file "llm/scripts/$f" "$OVERLAY_DIR/usr/lib/assistant/llm/scripts/$f"
done
# systemd 单元**镜像形态**（T15-2-8）：进 overlay 的 /usr/lib/systemd/system/
#   ⚠ 与 systemd/ 下板端形态的单元是**两套**：板端那份指向 git checkout
#     (/home/kickpi/...)，镜像这份指向 /usr/lib/assistant 与 /data。
#     tests/test_image_target.py 守住"除记录在案的差异外必须一致"，防止两套漂。
for u in assistant.target agent.service agent-gui.service assistant-init.service ab-mark.service; do
    install_file "systemd/image/$u" "$OVERLAY_DIR/usr/lib/systemd/system/$u"
done
# T15-2-11：A/B「标记启动成功」的用户态工具（ab-mark.service 调用它）
#   为什么必须要有：SPL/u-boot 每次启动都扣一次 tries_remaining，而镜像里
#   没有任何东西置 successful_boot → 扣完两个槽都判死、掉 fastboot（实测踩过）。
install_file image/payload/ab-mark.py "$OVERLAY_DIR/usr/lib/assistant/ab-mark.py"
# T15-2-11：启动画面（开机那张图）
#  ---------------------------------------------------------------------------
#  Rockchip 的启动图机制：`kernel-6.1/logo.bmp`（u-boot 用）与
#  `logo_kernel.bmp`（内核用）由 `mk-kernel.sh` 经 `scripts/resource_tool` 打进
#  `resource.img` → 再进 boot.img 的 FIT 的 resource 子镜像；DTB 里的
#  `logo,offset/width/height/bpp` 就是由 resource_tool 按 BMP 头写进去的，
#  内核按这些属性贴图（`rockchip_drm_logo.c`，bpp 只支持 16/24/32），
#  路由里写着 `logo,mode = "center"`（居中）。
#  所以只要把这两张 BMP 换成我们的即可 —— 尺寸做成**与屏等大 1080x1920**，
#  居中/偏移怎么写都是整屏一张图。
#  ⚠ 只提交一份 BMP，这里复制成两个名字（两份内容相同）。
install_file image/logo/logo-kernel.bmp "$KERNEL_LOGO_DIR/logo_kernel.bmp"
install_file image/logo/logo-kernel.bmp "$KERNEL_LOGO_DIR/logo.bmp"
# T15-2-11：触摸旋转必须做在 **libinput** 这一层（eglfs_kms 用 libinput 处理输入，
# 通用 evdev 插件的环境变量完全无效 —— 板端实测）。见规则文件里的推导过程。
install_file image/board/rockchip/kickpi/k1mini/udev/99-assistant-touch.rules \
             "$OVERLAY_DIR/etc/udev/rules.d/99-assistant-touch.rules"
# T15-2-11：NTP 源换成国内（默认的 Google 池在国内不通，板子时间会停在旧值）
#   ⚠ 这个文件**必须显式列在这里**：注入是"逐个文件"拷的，不在清单里就等于没建
#     （实测踩过：文件写好了、post-build 里那段也执行了，却在全 SDK 里找不到它）。
install_file image/board/rockchip/kickpi/k1mini/rootfs-overlay/etc/systemd/timesyncd.conf.d/assistant-ntp.conf \
             "$OVERLAY_DIR/etc/systemd/timesyncd.conf.d/assistant-ntp.conf"
install_file image/board/rockchip/kickpi/k1mini/post-build.sh \
             buildroot/board/rockchip/kickpi/k1mini/post-build.sh
install_file image/build-llama.sh tools/assistant/build-llama.sh
install_file image/prepare-rknnlite.sh tools/assistant/prepare-rknnlite.sh
install_file image/check-runtime-deps.py tools/assistant/check-runtime-deps.py
install_file image/check-assistant-target.py tools/assistant/check-assistant-target.py
# T15-2-10：两个检查器共用的"看哪棵 target 树"模块（imagelib.py），
# 以及刷板前一键入口 preflash-check.sh。
# ⚠ imagelib.py 必须跟着进去：两个检查器都 `import imagelib`，
#   只拷检查器不拷它 → SDK 里那两个脚本直接 ImportError（只有真跑才发现）。
install_file image/imagelib.py tools/assistant/imagelib.py
install_file image/preflash-check.sh tools/assistant/preflash-check.sh
# T15-2-10b：payload 的构建脚本（post-build 里按清单装 agent/GUI/native）
install_file image/build-payload.sh tools/assistant/build-payload.sh
install_file image/build-native.sh tools/assistant/build-native.sh
install_file image/build-gui.sh tools/assistant/build-gui.sh
install_file image/payload.manifest tools/assistant/payload.manifest
for f in assistant assistant.sh; do
    install_file "image/payload/$f" "tools/assistant/payload/$f"
done

# ---------------------------------------------------------------------------
#  T15-2-11：**本机私有**的镜像料（WiFi 凭据等）
#  ---------------------------------------------------------------------------
#  板子只有 wlan0 能通，而镜像里没有任何网络配置 → 首刷之后"起来了但没网"。
#  把现场凭据（用 image/local/wifi.nmconnection.example 做模板）烘进镜像，
#  板子开机就自己连上，之后 ssh 进去干活。
#  ⚠ 真凭据**不进仓库**（.gitignore 里忽略 image/local/*，只留 *.example）；
#    这里只是"有就注入、没有就跳过"，所以没凭据的构建也照常能出镜像。
#    支持**多份** *.nmconnection：现场 SSID 记不准时两个都写，谁对连谁
#    （这次就是 Anorak_host / Anroak_host 差两个字母）。
NMLOCAL="$HERE/local"
if ls "$NMLOCAL"/*.nmconnection >/dev/null 2>&1; then
    if [ -n "$DRY" ]; then
        echo "   + (dry-run) 注入本机 WiFi 凭据：$(cd "$NMLOCAL" && ls *.nmconnection | tr '\n' ' ')"
    else
        mkdir -p "$SDK/tools/assistant/local"
        for f in "$NMLOCAL"/*.nmconnection; do
            cp "$f" "$SDK/tools/assistant/local/$(basename "$f")"
            chmod 0600 "$SDK/tools/assistant/local/$(basename "$f")"
            echo "   + 已注入 WiFi 凭据：$(basename "$f")（0600）"
        done
    fi
else
    echo "   = 没有 image/local/*.nmconnection（跳过；镜像里不会有 WiFi 凭据）"
fi

# SSH 公钥（T15-2-11）：同样是"本机私有"的镜像料。板子上 root 是空密码、
# sshd 又不允许空密码登录，装一份公钥现场就能直接 ssh（比每次敲密码方便）。
if [ -f "$HERE/local/authorized_keys" ]; then
    if [ -n "$DRY" ]; then
        echo "   + (dry-run) 注入本机 SSH 公钥 → tools/assistant/local/authorized_keys"
    else
        mkdir -p "$SDK/tools/assistant/local"
        cp "$HERE/local/authorized_keys" "$SDK/tools/assistant/local/authorized_keys"
        chmod 0600 "$SDK/tools/assistant/local/authorized_keys"
        echo "   + 已注入本机 SSH 公钥（tools/assistant/local/authorized_keys，0600）"
    fi
else
    echo "   = 没有 image/local/authorized_keys（跳过；镜像里只能用密码登录）"
fi

# ---------------------------------------------------------------------------
#  T15-2-10b-5：payload 的**源码**也要进 SDK
#  ---------------------------------------------------------------------------
#  理由与 llm/scripts 那次一样：post-build 是在 **SDK 里**跑的，够不到我们的仓库。
#  仓库仍是唯一来源 —— 这里只是把它复制成 SDK 内的 `tools/assistant/payload-src/`。
#  ⚠ 用 rsync 而不是 install_file：这些是**目录树**（agent/ 66 个文件、native/ 带
#    submodule、gui/ 几十个源文件）。排除项：
#      __pycache__/*.pyc  —— 别把宿主机字节码带进镜像
#      build*/            —— 本仓库的构建产物（Windows 盘上还可能是 777）
#      native/third_party/googletest —— 只有 host 单测用，交叉编译不需要
#      gui/tests/         —— 镜像里 -DGUI_BUILD_TESTS=OFF，不需要
echo
echo "== payload 源码注入 → tools/assistant/payload-src/（post-build 在那里取源）"
if ! command -v rsync >/dev/null 2>&1; then
    echo "!! 没有 rsync，payload 源码无法注入（apt-get install rsync）" >&2
    FAILED=1
else
    PAYLOAD_SRC="$SDK/tools/assistant/payload-src"
    if [ -n "$DRY" ]; then
        echo "   + (dry-run) rsync agent/ config/*.example.yaml Readme.md docs/{gui,image}.md"
        echo "   + (dry-run) rsync native/ gui/ → $PAYLOAD_SRC"
    else
        mkdir -p "$PAYLOAD_SRC"
        rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' --exclude '.pytest_cache' \
              --exclude 'build/' --exclude 'build-*/' --exclude '.git' \
              "$HERE/../agent/" "$PAYLOAD_SRC/agent/"
        for f in config/config.example.yaml config/user_profile.example.yaml Readme.md docs/gui.md docs/image.md; do
            mkdir -p "$PAYLOAD_SRC/$(dirname "$f")"
            cp "$HERE/../$f" "$PAYLOAD_SRC/$f"
        done
        rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' \
              --exclude 'build/' --exclude 'build-*/' --exclude '.git' \
              --exclude 'third_party/googletest/' \
              "$HERE/../native/" "$PAYLOAD_SRC/native/"
        rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' \
              --exclude 'build/' --exclude 'build-*/' --exclude '.git' \
              --exclude 'tests/' \
              "$HERE/../gui/" "$PAYLOAD_SRC/gui/"
        # 两个 shell 模板也放一份进 payload-src（build-payload.sh 会先在那里找；
        # 找不到才回退到 tools/assistant/payload/ —— 两处都在，谁先被注入都不怕）
        mkdir -p "$PAYLOAD_SRC/payload"
        for f in assistant assistant.sh; do
            cp "$HERE/payload/$f" "$PAYLOAD_SRC/payload/$f"
        done
        cp "$HERE/payload.manifest" "$PAYLOAD_SRC/payload.manifest"
        echo "   + agent/ $(find "$PAYLOAD_SRC/agent" -name '*.py' | wc -l) 个 .py、native/ $(du -sh "$PAYLOAD_SRC/native" | cut -f1)、gui/ $(du -sh "$PAYLOAD_SRC/gui" | cut -f1)"
    fi
fi

if [ "$FAILED" != "0" ]; then
    echo "!! 有源文件缺失，注入不完整" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
#  唯一一个"二进制输入"：G52 的预编译 Mali blob（T15-2-5）
#  ---------------------------------------------------------------------------
#  它**不进我们的仓库**（56 MB，git 里不合适），而是用脚本从 SDK 自带的厂商 deb 里
#  取出来、按 buildroot 期望的文件名放进 external/libmali。脚本对 blob 做指纹
#  （deb 与 .so 两级 sha256）与能力（SONAME / gbm_* 符号 / DT_NEEDED）校验，
#  所以这一步失败一定是"东西不对"，不是"路径顺手写错了"。
echo
if [ -n "$DRY" ]; then
    bash "$HERE/prepare-libmali.sh" "$SDK" --dry-run
else
    bash "$HERE/prepare-libmali.sh" "$SDK"
fi

# ---------------------------------------------------------------------------
#  Qt 源码预置（T15-2-6）
#  ---------------------------------------------------------------------------
#  这个 buildroot 的 Qt5 走 invent.kde.org 的"按 commit 现生成"归档，而 KDE 会**重新打包**
#  （同一 commit、内容一致、外层 tar 字节不同）→ buildroot 钉的 sha256 对不上，构建在下载
#  校验就死。primary site 上放着原始的那份，这个脚本把它按 hash 校验后放进 dl/。
#  细节与验证方式见脚本头注释与 docs/image.md。
echo
if [ -n "$DRY" ]; then
    bash "$HERE/prime-dl.sh" "$SDK" --dry-run
else
    bash "$HERE/prime-dl.sh" "$SDK"
fi

# ---------------------------------------------------------------------------
#  MPP 缺件修复（T15-2-7）
#  ---------------------------------------------------------------------------
#  厂商快照的 external/mpp（1.0.11）少了 build/cmake/merge_objects.cmake，
#  而 CMakeLists.txt 无条件 include 它 → `make rockchip-mpp` 在 configure 就死。
#  上游那份接口一致，脚本按字节哈希校验后补上。
echo
if [ -n "$DRY" ]; then
    bash "$HERE/prepare-mpp.sh" "$SDK" --dry-run
else
    bash "$HERE/prepare-mpp.sh" "$SDK"
fi

echo "== 完成。下一步（在 SDK 根目录）："
echo "     # 1) 生成 .config（片段里已挂上 overlay 与 post-build 钩子）"
echo "     bash image/sdk-make.sh <SDK> rockchip_rk3568_kickpi_k1mini_release_defconfig"
echo "     # 2) 整机构建（post-build 会顺带交叉编译 llama-server 并装 rknnlite）"
echo "     cd <SDK> && ./build.sh rk3566_rk3568:rockchip_rk3568_kickpi_k1mini_release_defconfig"
echo "     # 3) 验收运行时闭环（逐项在位 + DT_NEEDED 闭包 + chroot 冒烟）"
echo "     python3 <SDK>/tools/assistant/check-runtime-deps.py <SDK>"
