# T15-2-9 断点存档（2026-09-29 00:05 停电前）

> 用途：停电/中断后**照着这份恢复**，不用重新推导。
> 状态：**首次完整构建进行中**（不是失败，是**按用户要求在 00:03:57 主动停的**，早于 00:08 停电）。

## 0. 主动停止时（2026-09-29 00:03:57）的确切状态

```
pgrep t1529-retry.sh = 0     pgrep build-image.sh = 0
pgrep brmake = 0             pgrep 'make -C' = 0        ← 全部已停，不会再自己重启
target  = 472 MB
firmware= MiniLoaderAll.bin boot.img misc.img uboot.img   ← kernel/u-boot/misc 阶段已完成
最后装完的包: qt5multimedia（已 Installing to target）
当时正在编  : Mali 依赖的 X 客户端库链（xorgproto、xutil_util-macros、zlib 等）
```

停止方式：先 `pkill` 续跑循环（否则它会自动重启构建），再 vendor `build.sh`、
再我们的 `build-image.sh`（同一个命令行也匹配到它的时钟看门狗子壳），最后 TERM→KILL 掉
`brmake`/`make` 树。**中途被打断的包目录可能处于"半编译"状态**，恢复时若报
`build/<pkg>-<ver>/.stamp_*` 相关错误，删掉那个包目录重跑即可（第 2/4 节有说明）。

## 1. 现在到哪了

| 阶段 | 状态 |
| --- | --- |
| misc.img | ✅ 完成 |
| u-boot / loader | ✅ 完成（`MiniLoaderAll.bin`、`uboot.img`） |
| kernel | ✅ 完成（`Image` 41,486,848 B；`rk3568-kickpi-k1Mini-assistant.dtb` 在位；`boot.img`、`resource.img`、`zboot.img`） |
| rootfs（buildroot） | 🔄 **进行中**：第 1 次尝试里已装 qt5base，正在编 qt5declarative；target ≈ 440 MB |
| 打包/镜像 | ⬜ 未开始（`output/firmware/` 现在只有 4 个：MiniLoaderAll/boot/misc/uboot） |

构建方式：`image/build-image.sh`（本仓库新增）→ 内部跑 vendor 的 `./build.sh <chip>:<defconfig>` 然后 `./build.sh all`。
真正在跑的是**续跑循环** `temp/t1529-retry.sh`（失败就把脏掉的包目录删掉重试，最多 8 次）。

## 2. 恢复怎么做（停电后照抄）

```bash
# 0) 先确认时钟没有倒退（见下面第 4 条的坑），必要时再停一次 timesyncd
wsl -u root systemctl stop systemd-timesyncd; wsl -u root systemctl mask systemd-timesyncd

# 1) 直接重跑整机构建：make 是增量的，已编好的包会跳过
wsl -u root bash image/build-image.sh \
  /home/anorak/rk3568_buildroot/linux-kernel-6.1/rk-linux6.1-2026060914/rk-linux6.1-2026060914
#   若是被"脏包目录"卡住（日志里出现 build/<pkg>-<ver>/.stamp_*），
#   用 image/../temp/t1529-retry.sh 那套逻辑：删掉那个包目录再重跑
```

成功判据：`$SDK/output/firmware/` 里出现 `rootfs.img` / `update.img` / `*-ota-*.img` 等，
并且 `./build.sh` 退出码 0（`output/sessions/<时间戳>/99-all-build.log` 末尾没有 ERROR）。

## 3. 两条路径记牢（容易搞混）

| 用途 | buildroot output 目录 |
| --- | --- |
| 我们自己的**单包**构建（`image/sdk-make.sh`，T15-2-5/2-6/2-7 用的是它） | `buildroot/output/rockchip_rk3568_kickpi_k1mini_release/` |
| vendor **整机构建**（`./build.sh all`，`mk-buildroot.sh` 算出来的） | `buildroot/output/rockchip_rk3568_kickpi_k1mini_release/rockchip_rk3568_kickpi_k1mini_release/` |

`mk-buildroot.sh` 第 12 行：`output/${RK_DEFCONFIG%_defconfig}/$BUILDROOT_CFG` —— **多一层 defconfig 名**。
所以整机构建是**全新一棵树**（工具链都重编），而 T15-2-5/6/7 的产物在另一棵里。
`dl/` 是共享的（`buildroot/dl`），所以我们预置的 Qt 源码包两边都能用。

## 4. 这次踩到并已修掉的四个坑（都在 `image/build-image.sh` 里落成代码）

1. **PATH**：WSL 把 Windows PATH 接在后面，带空格条目会让 buildroot 的依赖自检直接退出
   → 脚本先清洗 PATH（与 `image/sdk-make.sh` 同一套）。
2. **`python` 这个名字缺失**：vendor 的 `check-kernel.sh` 写死
   `check-package.sh python python-is-python3`，而系统只有 `python3` → 内核根本没开始编就报
   `Your python is missing`。脚本在 `$SDK/output/assistant-tools/` 放一个 `python -> python3` 垫片并前置到 PATH（不改系统）。
3. **宿主宿主工具缺 `gettext`**：`mk-buildroot.sh` 报 `Your msgmerge is missing`。
   已 `apt-get install -y gettext libssl-dev libelf-dev`（**已装好，不用再装**）。
4. **WSL 时钟偏移（最要命的一个）**：`systemd-timesyncd` 处于 active 但 unsynchronized，
   会**步进调整时钟**，于是刚写出的文件 mtime "落在未来"，make 直接失败：
   `ERROR: Clock skew detected. File ... has a time stamp 0.2176s in the future.`
   （先后打在 systemd 与 host-wayland 的 configure 上。）
   已做两件事：① `systemctl stop + mask systemd-timesyncd`（**已做，mask 会持久化**）；
   ② `image/build-image.sh` 里加了看门狗，每 60 秒把 output 里"超前"的时间戳夹回现在
   （`IMG_CLOCK_WATCHDOG=0` 可关）。

   另：被"时钟偏移"打断的包，其 build 目录会处于**半打补丁**的脏状态，
   下次会报 patch 记账冲突（`already applied` / `to be applied` 同一个文件）→ 删掉该包目录即可。

## 5. 还没做的收尾（T15-2-9 的出口要求）

- 三组数字：**时长**（会话日志首末时间戳）、**体积**（`output/firmware/` 各镜像）、
  **组件版本**（kernel/u-boot/buildroot/Qt/gcc/glibc/llama/rknnlite/MPP/libmali）。
  采集脚本草稿在 `temp/t1529-report.sh`（停电后重启 WSL 若 temp 还在就直接用；否则照它重写）。
- 文档：`docs/image.md` 增 §5.8（首次完整构建：命令、时长/体积/版本三组数字、
  上面四个坑、两条 output 路径的区别）；
- `todo2.md` 2-9 标 ✅；`git commit` + push；CI 盯绿。
- 构建是 root 跑的 → 收尾时把属主改回来：
  `chown -R anorak:anorak $SDK/output $SDK/buildroot/output`。

## 6. 断电后先看这里

```bash
wsl -u root bash -c 'date -Is; pgrep -c brmake; ls /home/anorak/rk3568_buildroot/linux-kernel-6.1/rk-linux6.1-2026060914/rk-linux6.1-2026060914/output/firmware/'
```
`brmake` 为 0 且 firmware 里没有更多镜像 = 构建确实被打断了，按第 2 节重跑即可（**不会从头再来**）。
