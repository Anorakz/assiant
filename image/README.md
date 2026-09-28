# image/ — 系统镜像配方（路线 C：厂商 6.1 SDK + Buildroot）

这个目录放**我们的镜像配方**，由本仓库维护、注入到厂商 SDK 里使用。
方案、决策、现状基线、风险与任务列表都在 [`docs/image.md`](../docs/image.md)；
这里只放能直接用的文件。

```
image/
├─ install-into-sdk.sh                 # 把下面这些文件复制进厂商 SDK（幂等，支持 --dry-run）
├─ prepare-libmali.sh                  # 把厂商 deb 里的 G52 blob 按 buildroot 期望的名字落位（T15-2-5）
├─ sdk-make.sh                         # 构建入口：先剔掉 PATH 里的 Windows 条目，再 exec make
├─ prime-dl.sh                         # 把 Qt 源码（与 buildroot 钉的 hash 一致的那份）预置进 dl/（T15-2-6）
├─ prepare-mpp.sh                      # 补上厂商 MPP 快照漏掉的 build/cmake/merge_objects.cmake（T15-2-7）
├─ prepare-rknnlite.sh                 # 把 SDK 里 cp311 那份 rknn-toolkit-lite2 wheel 装进 site-packages（T15-2-7）
├─ build-llama.sh                      # 交叉编译 llama.cpp（钉 b387ddfd8）装进 /usr/lib/assistant/llm/bin（T15-2-7）
├─ check-runtime-deps.py               # 运行时闭环验收：逐项在位 + DT_NEEDED 闭包 + chroot 冒烟（T15-2-7）
├─ check-parameter.py                  # 分区表校验器（纯算术；tests/test_image_parameter.py 有 8 项守卫）
├─ buildroot/configs/
│   ├─ rockchip_rk3568_kickpi_k1mini_release_defconfig     # buildroot defconfig（片段式）
│   └─ rockchip/products/kickpi-k1mini-release.config      # 我们自己的片段：systemd + Qt5(EGLFS/虚拟键盘) + 运行时
├─ device/rockchip/.chips/rk3566_rk3568/
│   ├─ rockchip_rk3568_kickpi_k1mini_release_defconfig     # SDK 的板级 defconfig（lunch 用）
│   └─ parameter-assistant-ab.txt                          # 我们的 A/B 分区表（模型放共享 userdata）
└─ kernel/                                                 # 板级 dts/dtsi（全部 -assistant 后缀，不动厂商同名文件）
    ├─ rk3568-kickpi-k1Mini-assistant.dts
    ├─ rk3568-kickpi-k1Mini-assistant.dtsi
    └─ rk3568-kickpi-assistant-overrides.dtsi
└─ board/rockchip/kickpi/k1mini/                            # 注入到 SDK 的 board 目录（T15-2-7）
    ├─ post-build.sh                                        # /data 与 fstab + 交叉编译 llama + 装 rknnlite
    └─ rootfs-overlay/usr/lib/assistant/llm/scripts/        # 由 llm/scripts/ 复制而来（单一来源在仓库）
```

## 用起来

```bash
# 1) 注入：配方文件 + G52 的 Mali blob + Qt 源码预置（逐条打印 新增/覆盖/已一致）
bash image/install-into-sdk.sh /home/anorak/rk3568_buildroot/linux-kernel-6.1/rk-linux6.1-2026060914/rk-linux6.1-2026060914

# 2) 只配置（快，用来验证配方本身）
bash image/sdk-make.sh <SDK> rockchip_rk3568_kickpi_k1mini_release_defconfig

# 3) 构建：单包（例如只验 Mali）/ 整机（内核 + u-boot + rootfs + 镜像）
bash image/sdk-make.sh <SDK> rockchip-mali
cd <SDK> && ./build.sh rk3566_rk3568:rockchip_rk3568_kickpi_k1mini_release_defconfig
```

## 约定

- **厂商 SDK 只被注入、不被改**：所有我们的配置都走"新增文件 + 覆盖同名文件"，脚本会把
  `新增/覆盖/已一致` 逐条打出来；换 SDK 版本时重跑一遍即可。
- **构建一律走 `image/sdk-make.sh`**：WSL 会把 Windows 的 PATH 接到 Linux PATH 后面，里面有
  `/mnt/c/Program Files/...` 这种带空格的条目，buildroot 第一步
  `support/dependencies/dependencies.sh` 就会以 `This doesn't work. Fix you PATH.` 退出
  （与 buildroot 本身无关）。整机构建的 `./build.sh` 也要先清 PATH。
- **G52 的 Mali blob 不放在本仓库**（56 MB）：它从 SDK 自带的厂商 deb 里现取，
  `prepare-libmali.sh` 负责指纹校验与文件名推导（为什么文件名是关键，见 `docs/image.md` §5.4）。
- **Qt 源码走 `prime-dl.sh` 预置**：`invent.kde.org` 会重新打包同一 commit 的归档，
  与 buildroot 钉的 sha256 不符；primary site 上有与 hash 一致的那份，脚本按 hash 校验后放进
  `dl/`（细节与"怎么证明内容没问题"见 `docs/image.md` §5.5）。
- 片段顺序有意义：`rockchip_rk3568_kickpi_k1mini_release_defconfig` 里**基座片段在前、
  我们的 products 片段在后** —— 后面能覆盖前面的选择。
- 片段文件有**四条书写纪律**（尾注释、注释里藏配置、符号必须存在），
  `tests/test_image_recipe.py` 把能机器检查的都钉住了，改片段前先看一眼它的 docstring。
- 命名链：chip defconfig 里的 `RK_BUILDROOT_BASE_CFG="rk3568_kickpi_k1mini_release"`
  → 派生 `RK_BUILDROOT_CFG="rockchip_rk3568_kickpi_k1mini_release"`
  → 正好是 `buildroot/configs/rockchip_rk3568_kickpi_k1mini_release_defconfig` 去掉 `_defconfig`。
