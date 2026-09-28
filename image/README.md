# image/ — 系统镜像配方（路线 C：厂商 6.1 SDK + Buildroot）

这个目录放**我们的镜像配方**，由本仓库维护、注入到厂商 SDK 里使用。
方案、决策、现状基线、风险与任务列表都在 [`docs/image.md`](../docs/image.md)；
这里只放能直接用的文件。

```
image/
├─ install-into-sdk.sh                     # 把下面这些文件复制进厂商 SDK（幂等，支持 --dry-run）
├─ buildroot/configs/
│   ├─ rockchip_rk3568_kickpi_k1mini_release_defconfig     # buildroot defconfig（片段式）
│   └─ rockchip/products/kickpi-k1mini-release.config      # 我们自己的片段：systemd + Qt5(EGLFS/VK) + 运行时
└─ device/rockchip/.chips/rk3566_rk3568/
    └─ rockchip_rk3568_kickpi_k1mini_release_defconfig     # SDK 的板级 defconfig（lunch 用）
```

## 用起来

```bash
# 1) 注入（把本仓库的配方复制进 SDK）
bash image/install-into-sdk.sh /home/anorak/rk3568_buildroot/linux-kernel-6.1/rk-linux6.1-2026060914/rk-linux6.1-2026060914

# 2) 只配置（快，用来验证配方本身）
cd <SDK>/buildroot
make O=output/rockchip_rk3568_kickpi_k1mini_release rockchip_rk3568_kickpi_k1mini_release_defconfig

# 3) 整机构建（内核 + u-boot + rootfs + 镜像）—— 会联网拉 Qt 等源码
cd <SDK>
./build.sh rk3566_rk3568:rockchip_rk3568_kickpi_k1mini_release_defconfig
```

## 约定

- **厂商 SDK 只被注入、不被改**：所有我们的配置都走"新增文件 + 覆盖同名文件"，脚本会把
  `新增/覆盖/已一致` 逐条打出来；换 SDK 版本时重跑一遍即可。
- 片段顺序有意义：`rockchip_rk3568_kickpi_k1mini_release_defconfig` 里**基座片段在前、
  我们的 products 片段在后** —— 后面能覆盖前面的选择（例如把 `base` 的 eudev 换成 devtmpfs，
  给 systemd 让路）。
- 命名链：chip defconfig 里的 `RK_BUILDROOT_BASE_CFG="rk3568_kickpi_k1mini_release"`
  → 派生 `RK_BUILDROOT_CFG="rockchip_rk3568_kickpi_k1mini_release"`
  → 正好是 `buildroot/configs/rockchip_rk3568_kickpi_k1mini_release_defconfig` 去掉 `_defconfig`。
