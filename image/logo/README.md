# 启动画面（boot logo）—— 机制与"方向"经验

## 这张图是怎么进镜像的

```
image/logo/source.png          ← 人给的图（1080x1920，PNG）
        │  make-logo.py       ← 转成内核要的格式（24bpp BMP）
        ▼
image/logo/logo-kernel.bmp     ← 仓库里只放这一份
        │  install-into-sdk.sh（同时装成两个名字）
        ▼
<SDK>/kernel-6.1/logo.bmp      ← u-boot 用
<SDK>/kernel-6.1/logo_kernel.bmp ← 内核用
        │  mk-kernel.sh → scripts/resource_tool <dtb> logo.bmp logo_kernel.bmp
        ▼
<SDK>/kernel-6.1/resource.img  ← DTB + logo 的**裸像素**（不是 BMP 文件）
        │  mk-fitimage.sh（resource 作为 boot.img FIT 的一个子镜像）
        ▼
boot.img → update.img → 刷进 boot_a / boot_b
```

内核侧由 `drivers/gpu/drm/rockchip/rockchip_drm_logo.c` 按 DTB 里的
`logo,offset` / `logo,width` / `logo,height` / **`logo,bpp`** 贴图；这些属性是
`resource_tool` 解析 BMP 头时写进去的。两条硬约束（源码里读出来的）：

- **bpp 只支持 16 / 24 / 32**（厂商原图是 8bpp 调色板，靠工具转换）；
- 路由里写着 `logo,mode = "center"` —— 居中摆放。

所以我们的做法是：**BMP 做成与屏等大（1080×1920）、24bpp** —— 与屏等大 ⇒
"居中"就是整屏，不依赖任何偏移逻辑。屏的原生 mode 是 1080×1920（竖）。

## ⚠ "方向"这件事只能实测（踩过两次）

文件里的方向与屏上的方向**没有直觉关系**（面板是怎么装的、u-boot/内核有没有转，
都不写在配置里）。实测记录：

| 版本 | 文件里的图 | 屏上看到 |
| --- | --- | --- |
| b6 | 原图按宽度铺满居中（未旋转） | 用户说要旋转铺满，于是自己转了 90° 给了新图 |
| b7 | 用户转好的 90° 版，**原样**编码 | **上下颠倒 180°** |
| b8 | 在 b7 基础上再转 **180°** | （待验收） |

结论：**当前正确的编码是 `--asis --r180`**（即"文件里相对原图转 270°"）。
以后换图，直接用这个组合：

```bash
python make-logo.py image/logo/source.png image/logo/logo-kernel.bmp --asis --r180
```

其它模式（默认按宽居中、`--fill` 旋转铺满、`--rotate-ccw` 逆时针）都还在，
只是想换观感时用；**方向一律以"屏上看着正"为准，改了就在上表里补一行**。

## 不刷机也能验证到哪一步

`resource_tool` 那步可以单独跑（它是最容易因格式不对而失败的地方）：

```bash
scripts/resource_tool <某个 dtb> logo.bmp logo_kernel.bmp   # → Pack to resource.img successed!
```

做完的产物大小对得上就说明进了 FIT：`resource.img` = DTB + 两张 6.2 MB 的裸像素
≈ 12,631,040 字节；`boot.img` ≈ 54.3 MB（boot 分区 64 MB，放得下）。
