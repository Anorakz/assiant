#!/usr/bin/env bash
# ============================================================================
#  image/build-image.sh — 整机构建入口（内核 + u-boot + rootfs + 镜像，T15-2-9）
#
#  为什么要它（不是"多包一层"）
#  ---------------------------------------------------------------------------
#  1) **PATH 必须清洗**：WSL 会把 Windows 的 PATH 接在 Linux PATH 后面，里面有
#     `/mnt/c/Program Files/...` 这种带空格的条目。buildroot 那条链在第一步
#     `support/dependencies/dependencies.sh` 就会因此退出（sdk-make.sh 的注释里有完整记录），
#     vendor 的 `./build.sh` 同样吃这口亏。
#  2) **必须用 root 跑**（`sudo` 或 `wsl -u root`）：`build.sh` 有一段**无条件**的
#     sudo 密码缓存 —— `sudo -n true` 失败就 `read -s` 读密码，非交互后台跑会**直接卡死**。
#     用 root 跑就没有这一步（脚本自己会检测 RK_SUDO_ROOT 并打印 "Running within sudo(root)"）。
#     ⚠ root 跑完 `$SDK/output` 与 `$SDK/buildroot/output` 里会有 root 属主的文件；
#       构建完记得把属主改回来（脚本末尾会提示，2-10 的 chroot 测试要在非 root 下跑）。
#  3) **不要 export 任何 RK_* 变量**（这一条是踩出来的）
#     build.sh 的会话机制是：`RK_LOG_DIR` 不存在就判 `Session(x) is invalid!` 并**改名成时间戳**；
#     然后它还会把"当前环境里与 initial.env 不一致的 RK_*"收集进 `custom.env`，
#     如果非空就 `warning` + `read -t 10 -p "Press enter to continue."` ——
#     **非交互跑会直接以 exit 1 失败**（实测：`export RK_SESSION=assistant-image` 就是这么挂的）。
#     所以会话名交给 build.sh 自己生成，日志去 `$SDK/output/sessions/<时间戳>/` 找
#     （`$SDK/output/log` 是指向 `sessions/` 的软链）。
#
#  用法
#  ---------------------------------------------------------------------------
#      wsl -u root bash image/build-image.sh <SDK 根目录> [目标...]
#
#    目标就是 vendor 的那些 hook 名（不给 = `all`）：
#      all / kernel / loader / rootfs / firmware / updateimg / recovery ...
#
#  它做两件事：先按板级 defconfig **lunch 一次**（`chip:defconfig` 形式），
#  再按目标构建。
# ============================================================================
set -euo pipefail

SDK="${1:-}"
shift || true
if [ -z "$SDK" ]; then
    echo "用法: [sudo|wsl -u root] bash image/build-image.sh <SDK 根目录> [目标...]" >&2
    exit 2
fi
TARGETS=("$@")
if [ ${#TARGETS[@]} -eq 0 ]; then
    TARGETS=("all")
fi

DEFCONFIG="${IMG_CHIP_DEFCONFIG:-rk3566_rk3568:rockchip_rk3568_kickpi_k1mini_release_defconfig}"

if [ ! -x "$SDK/build.sh" ]; then
    echo "!! $SDK/build.sh 不存在或不可执行" >&2
    exit 2
fi

# ---- PATH 清洗（与 image/sdk-make.sh 同一套规则）----------------------------
CLEAN_PATH="$(printf '%s' "$PATH" | tr ':' '\n' | grep -v '^/mnt/' | grep -v '[[:space:]]' | paste -sd: -)"
if [ -z "$CLEAN_PATH" ]; then
    echo "!! PATH 清洗后为空" >&2
    exit 2
fi

# ---- 垫片：vendor 要 `python` 这个名字 --------------------------------------
# check-kernel.sh 里写死一句 `check-package.sh python python-is-python3`，
# 而现代发行版只有 `python3` —— 于是**内核还没开始编**就报：
#     Your python is missing / Please install it: sudo apt-get install python-is-python3
# 这里放一个只含 `python -> python3` 软链的垫片目录并放到 PATH 最前，
# 不去装 `python-is-python3`、也不动系统（可复现、可回退）。
SHIM="$SDK/output/assistant-tools"
mkdir -p "$SHIM"
ln -sfn "$(command -v python3)" "$SHIM/python"
CLEAN_PATH="$SHIM:$CLEAN_PATH"

echo "== 整机构建"
echo "   SDK      : $SDK"
echo "   板级配置 : $DEFCONFIG"
echo "   目标     : ${TARGETS[*]}"
echo "   uid      : $(id -u)（必须是 0，否则 build.sh 会交互要 sudo 密码）"
if [ "$(id -u)" != "0" ]; then
    echo "!! 请用 root 跑：sudo bash $0 $SDK ${TARGETS[*]}" >&2
    exit 2
fi

cd "$SDK"
export PATH="$CLEAN_PATH"

# ---- WSL 时钟偏移看门狗（踩过：Clock skew detected）-------------------------
#  WSL2 的墙上时钟会偶发往回跳零点几秒，于是**刚写出的文件 mtime 落在未来**，
#  make 判定后直接失败：
#      ERROR: Clock skew detected. File .../systemd-254.9/build/meson-private/coredata.dat
#             has a time stamp 0.2176s in the future.
#      make: *** [.../systemd-254.9/.stamp_configured] Error 1
#  实测就是 systemd 的 configure 这一步挂的（不是配置错、也不是缺工具）。
#  这里起一个后台循环，每 60 秒把 output 里"超前"的时间戳夹回现在。
#  关掉：IMG_CLOCK_WATCHDOG=0
if [ "${IMG_CLOCK_WATCHDOG:-1}" != "0" ]; then
    (
        while :; do
            now="$(date +%s)"
            find "$SDK/output" "$SDK/buildroot/output" -newermt "@$now" -print0 2>/dev/null \
                | xargs -0 -r touch 2>/dev/null
            sleep 60
        done
    ) &
    WATCHDOG_PID=$!
    trap 'kill "$WATCHDOG_PID" 2>/dev/null || true' EXIT
    echo "   时钟看门狗: 已启动（pid $WATCHDOG_PID，每 60s 夹一次未来时间戳）"
fi

echo
echo "==== 1) lunch（$DEFCONFIG）"
./build.sh "$DEFCONFIG"

echo
echo "==== 2) 构建：${TARGETS[*]}"
for t in "${TARGETS[@]}"; do
    ./build.sh "$t"
done

SESSION_DIR="$(ls -dt "$SDK"/output/sessions/*/ 2>/dev/null | head -1 || true)"
echo
echo "== 完成。产物在 $SDK/output/firmware/"
echo "   本次会话日志: ${SESSION_DIR:-$SDK/output/sessions/}"
echo "⚠ root 跑过之后，把属主改回来再跑非 root 的活（例如 2-10 的 chroot 测试）："
echo "     chown -R anorak:anorak $SDK/output $SDK/buildroot/output"
