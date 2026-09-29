# T15-2-9 首次完整构建：断点存档（已收尾 ✅）

> 状态：**2026-09-29 20:29:20 构建成功 → T15-2-9 完成**。
> 本文保留两件事：① 当时为什么停在半路、怎么续跑（以后被中断可以照抄）；
> ② 完整结果（命令、三组数字、六个坑）已写进 **`docs/image.md` §5.8**，这里只留指针，不再重复。
>
> 结论：日志尾 `Running 99-all.sh - build_all succeeded`，会话目录
> `output/sessions/2026-09-29_20-27-14/`；`update.img` **747,516,490 B**、
> `rootfs.img` **679,477,248 B**；没有 `recovery.img`。

## 1. 当时的中断点（2026-09-29 00:03:57，用户要求早于 00:08 停电主动停）

```
pgrep t1529-retry.sh = 0     pgrep build-image.sh = 0
pgrep brmake = 0             pgrep 'make -C' = 0        ← 全部已停，不会再自己重启
target  = 472 MB
firmware= MiniLoaderAll.bin boot.img misc.img uboot.img   ← kernel/u-boot/misc 阶段已完成
最后装完的包: qt5multimedia（已 Installing to target）
当时正在编  : Mali 依赖的 X 客户端库链（xorgproto、xutil_util-macros、zlib 等）
```

停止方式：先 `pkill` 续跑循环（否则它会自动重启构建），再 vendor `build.sh`、
再 `image/build-image.sh`（同一个命令行也匹配到它的时钟看门狗子壳），最后 TERM→KILL 掉 `brmake`/`make` 树。

## 2. 续跑怎么做（**已验证有效**，增量 26 分钟收尾）

```bash
# 0) 先确认时钟不会倒退（坑 4）
wsl -u root systemctl stop systemd-timesyncd; wsl -u root systemctl mask systemd-timesyncd

# 1) 直接重跑整机构建：make 是增量的，已编好的包会跳过
wsl -u root bash image/build-image.sh \
  /home/anorak/rk3568_buildroot/linux-kernel-6.1/rk-linux6.1-2026060914/rk-linux6.1-2026060914
```

- **确认是增量的**：第二次进入时那棵树里 **178 个包目录都还在**，只补了剩下的包 + 打包。
- 若被"脏包目录"卡住（日志里出现 `build/<pkg>-<ver>/.stamp_*`，或补丁记账冲突
  `already applied` / `to be applied` 同一个文件）→ **删掉那个包目录**再重跑
  （`temp/t1529-retry.sh` 就是这套逻辑的自动循环）。
- 成功判据：`$SDK/output/firmware/` 里出现 `rootfs.img` / `update.img`，
  且 `output/sessions/<时间戳>/99-all-build.log` 末尾没有 ERROR。

## 3. 两条 output 路径（最容易让人以为"白编了"）

| 用途 | buildroot output 目录 |
| --- | --- |
| 我们自己的**单包**构建（`image/sdk-make.sh`，T15-2-5/2-6/2-7 用的是它） | `buildroot/output/rockchip_rk3568_kickpi_k1mini_release/` |
| vendor **整机构建**（`./build.sh all`，`mk-buildroot.sh` 算出来的） | `buildroot/output/rockchip_rk3568_kickpi_k1mini_release/rockchip_rk3568_kickpi_k1mini_release/` |

`mk-buildroot.sh` 第 12 行：`output/${RK_DEFCONFIG%_defconfig}/$BUILDROOT_CFG` —— **多一层 defconfig 名**。
`dl/`（`buildroot/dl`）是共享的，预置的 6 个 Qt 源码包两边都能用。

## 4. 六个坑 → 已成配方/文档

四个落在 `image/build-image.sh`（PATH 清洗、`python` 垫片、缺 gettext 的宿主前置、时钟看门狗），
两个落在配方里（**厂商 `libxcrypt.mk` 缺 host 变体** → 仓库覆盖；
**`RK_AB_UPDATE=y` 下 recovery 段算出的 defconfig 名不存在** → 板级 `# RK_RECOVERY is not set`）。
逐条细节见 **`docs/image.md` §5.8**。

## 5. 收尾清单（全部已完成）

- [x] 三组数字采集（时长 / 体积 / 组件版本）→ §5.8
- [x] `docs/image.md` §5.8
- [x] `todo2.md` 2-9 标 ✅
- [x] commit + push + CI 绿
- [ ] 属主（root 构建遗留，非阻塞）：
      `chown -R anorak:anorak $SDK/output $SDK/buildroot/output`
      —— T15-2-10 的 chroot 若以普通用户跑才需要，root 跑则无所谓。

## 6. 下一步

**T15-2-10**：刷板前验证 —— 在 `BR/target` 上 chroot 跑 ctest + 仓库 Python 套件 + `ldd`/符号检查，
出结果表（**区分"缺包"与"只能板上测"**）。之后再 T15-2-11 首次刷板。
