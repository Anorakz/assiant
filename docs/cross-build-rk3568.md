# RK3568 交叉编译环境搭建记录

> 目标板: **RK3568 / Ubuntu 20.04.6 LTS (focal) / aarch64 / glibc 2.31 / Python 3.8.10**
> 产出: `build-rk3568/native/agent_native.cpython-38-aarch64-linux-gnu.so`

本文档记录让交叉编译真正跑通所需的安装步骤与踩过的坑，换机器时照做即可。

---

## 1. 为什么必须是"某个特定版本"的工具链

板上 glibc 是 **2.31**。交叉编译时链接的是工具链**自带的 libstdc++.so / libgcc_s.so.1**，
而它们的 `.gnu.version_r` 里会记录"最低需要哪个 glibc 符号版本"。

| 工具链 | 自带 glibc | 结果 |
|---|---|---|
| Arm GNU Toolchain **15.3.Rel1** | 2.38 | ❌ `libstdc++.so: undefined reference to fstat@GLIBC_2.33 / pthread_create@GLIBC_2.34 / __isoc23_strtoul@GLIBC_2.38 ...` |
| Arm GNU Toolchain **9.2-2019.12** | **2.30** | ✅ 链接通过，产出只要求 `GLIBC_2.17` |

只要 **工具链自带 glibc ≤ 板端 glibc**，符号版本就不会越界。9.2-2019.12 是官方
最后一个基于 glibc 2.30 的 aarch64-none-linux-gnu 版本，正好卡在 2.31 之下。

> 注意：`--sysroot=板端 sysroot` **不能**解决这个问题。sysroot 提供的是 `libc.so.6`，
> 而 `libstdc++.so` 来自工具链自己的目录，不受 sysroot 影响。

---

## 2. 工具链安装

下载（官方 Windows 托管版，tar.xz 压缩包）：

```
https://developer.arm.com/-/media/Files/downloads/gnu-a/9.2-2019.12/binrel/
    gcc-arm-9.2-2019.12-mingw-w64-i686-aarch64-none-linux-gnu.tar.xz
```

- 实际下载会被 302 重定向到 `armkeil.blob.core.windows.net`，大小 **353,130,172 字节**
- 该 CDN 单连接常被限速到 ~25 KB/s（约 4 小时）。用多段并发（16~24 段）可提到
  ~500 KB/s（约 15 分钟）。`_dl/pget.ps1` 就是为此写的分段下载器。

解压后把内容**直接铺到** `E:\rk3568\arm`（不要多一层目录）：

```
E:\rk3568\arm\
├── bin\aarch64-none-linux-gnu-g++.exe      ← 关键文件
├── aarch64-none-linux-gnu\                 (自带 sysroot, libc 2.30)
├── include\  lib\  libexec\  share\
```

验证：

```powershell
& E:\rk3568\arm\bin\aarch64-none-linux-gnu-g++.exe --version
# aarch64-none-linux-gnu-g++.exe (GNU Toolchain for the A-profile Architecture 9.2-2019.12 (arm-9.10)) 9.2.1 20191025
```

---

## 3. sysroot 补 Python 3.8 开发文件

## 3. sysroot 补开发包 (头文件 + 链接名)

从板子同步下来的 rootfs **只有运行库**, 缺头文件和 `.so` 链接名, 交叉编译会失败。
两类包必须补:

| 缺什么 | 症状 | 包 |
|---|---|---|
| `Python.h` | pybind11 编不过 | `libpython3.8-dev` |
| `libavcodec/avcodec.h` 等 | decoder 编不过 | `libavcodec-dev` 等 5 个 |
| `lib*.so` 链接名 | 链接期 `cannot find -lavcodec` | 同上（dev 包里带） |

**一条命令搞定**（幂等，可重复执行；板端固件升级后重跑）：

```powershell
scripts/setup-sysroot-deps.ps1
```

### 3.1 Python 3.8 开发文件

板端 Python 是 **3.8.10 / Ubuntu focal**，所以取 focal 的 `libpython3.8-dev`：

```
http://ports.ubuntu.com/ubuntu-ports/pool/main/p/python3.8/
    libpython3.8-dev_3.8.10-0ubuntu1~20.04.18_arm64.deb
```

头文件落在 `usr/include/python3.8/`（**不是** 多架构目录）。

### 3.2 FFmpeg 开发文件

板端 FFmpeg 是 **4.2.7-0ubuntu0.1**，所以取 focal-updates 的 4.2.7，
**不是** focal 初始的 4.2.2 —— 版本必须与板端运行库一致：

```
http://ports.ubuntu.com/ubuntu-ports/pool/universe/f/ffmpeg/
    libavcodec-dev_4.2.7-0ubuntu0.1_arm64.deb
    libavutil-dev_4.2.7-0ubuntu0.1_arm64.deb
    libswscale-dev_4.2.7-0ubuntu0.1_arm64.deb
    libswresample-dev_4.2.7-0ubuntu0.1_arm64.deb
    libavformat-dev_4.2.7-0ubuntu0.1_arm64.deb
```

头文件装在**多架构目录** `usr/include/aarch64-linux-gnu/libavcodec/` 下 ——
和 `cmake/toolchain.cmake` 里的 `-isystem .../usr/include/aarch64-linux-gnu` 正好对上，
所以源码里直接 `#include <libavcodec/avcodec.h>` 即可。

> **这个 FFmpeg 没有 rkmpp 支持。** 在 `libavcodec.so.58.54.100` 里搜不到任何
> rkmpp 符号（`rkmpp` / `h264_rkmpp` / `drm_prime` 全是 0 次）。
> 它带的是 **V4L2 M2M** 硬解：`h264_v4l2m2m` / `hevc_v4l2m2m`。
> 在 RK3568 上这条路径走内核 `rkvdec` / `VEPU`，**就是硬件解码**，
> 所以 `decoder.cpp` 封的是它，而不是 rkmpp。

### 3.3 Windows 上的两个坑

1. **tar 建不出 .deb 里的符号链接** → 缺 `libX.so` 时脚本补一个链接器脚本
   （一行 `INPUT ( /usr/lib/aarch64-linux-gnu/libX.so.N)`），用法等价。
2. **Windows PowerShell 5.1 按 ANSI 解码无 BOM 的 .ps1** → 脚本里写中文会
   乱码甚至解析失败。`scripts/` 下的 .ps1 **一律保持纯 ASCII**。
   另外 `param()` 必须是脚本第一条**语句**，`$ErrorActionPreference` 得放在它后面，
   否则 PowerShell 会把 `param` 当命令名去找。

### 3.4 目标端的扩展名后缀

从板端 `/usr/lib/python3.8/config-3.8-aarch64-linux-gnu/Makefile` 读到：

```
SOABI      = cpython-38-aarch64-linux-gnu
EXT_SUFFIX = .cpython-38-aarch64-linux-gnu.so
```

这个后缀就是最终 `.so` 文件名，必须**手工**告诉 CMake（见下）。

---

## 4. 代码侧的必要修改

### 4.1 目录名笔误：`nativate/` → `native/`

`CMakeLists.txt` 写的是 `add_subdirectory(native)`，`.vscode/settings.json` 里的
moonlight-common-c 路径也是 `native/...`，但源码实际躺在 `nativate/`（拼错）。
已把 `CMakeLists.txt`、`binding.cpp` 移入 `native/` 并删掉 `nativate/`。

### 4.2 pybind11 钉在 v3.0.4

pybind11 子模块原先 checkout 在 master（v3.1.0-13），**v3.1.0 起不再支持 Python 3.8**：

```
error: "PYTHON < 3.9 IS UNSUPPORTED. pybind11 v3.0 was the last to support Python 3.8."
```

`v3.0.4` 是最后一个支持 3.8 的版本（其 `common.h` 里门槛是 `PY_VERSION_HEX < 0x03080000`），
已改为：

```powershell
git -C native/third_party/pybind11 checkout v3.0.4
```

### 4.3 `cmake/toolchain.cmake`

在原有基础上补三点，缺一不可：

1. **多架构头文件目录**：`sys/cdefs.h`、`bits/*.h` 在 `usr/include/aarch64-linux-gnu`
   下，要 `-isystem` 进去，否则 `features.h` 报 `sys/cdefs.h: No such file or directory`。
2. **路径驱动器号小写**：这套 mingw 版 gcc 在拼接 `-B` / `-isystem` 搜索路径时对
   盘符大小写敏感，写成 `E:/` 会找不到 `crt1.o` / `crti.o` / `libc.so`。统一 `e:/`。
3. **交叉编译的 Python 信息**：设 `PYBIND11_PYTHONLIBS_OVERWRITE=OFF`，并直接给出
   `PYTHON_INCLUDE_DIRS` / `PYTHON_LIBRARY` / `PYTHON_MODULE_EXTENSION`。
   否则 pybind11 会去**跑宿主机的 python.exe**，拿到 `.pyd` 或 `cpython-314` 之类
   的错误后缀——而宿主机是 Python 3.14，目标是 3.8。

   > 不要设 `PYTHON_VERSION` / `PYTHON_VERSION_STRING`，pybind11 视其为输出变量，
   > 设了会告警。

### 4.4 `native/CMakeLists.txt`

加了一条前置检查：`sysroot/usr/include/python3.8/Python.h` 不存在就直接
`FATAL_ERROR` 并说明原因，避免又退回去编译宿主机 Python。

---

## 5. 构建

```powershell
scripts/build.ps1
```

脚本会：检查工具链与 `Python.h` → `cmake` 配置 → 编译 → 校验 ELF/GLIBC 并打印。

产物：

```
build-rk3568/native/agent_native.cpython-38-aarch64-linux-gnu.so
```

---

## 6. 校验结果

```
Class:   ELF64
Data:    2's complement, little endian
Type:    DYN (Shared object file)
Machine: AArch64

NEEDED:  libstdc++.so.6, libm.so.6, libgcc_s.so.1, libc.so.6
导出符号: PyInit_agent_native
GLIBC 版本需求: GLIBC_2.17        ← 远低于板端 2.31，安全
```

板端验证（部署后）：

```bash
python3 -c "import agent_native; print(agent_native.ping())"   # pong
```

---

## 7. 排查速查

| 症状 | 原因 |
|---|---|
| `undefined reference to xxx@GLIBC_2.3x` | 工具链自带 glibc 高于板端，换低版本工具链 |
| `fatal error: sys/cdefs.h: No such file or directory` | 缺 `-isystem .../usr/include/aarch64-linux-gnu` |
| `cannot find crt1.o / -lc / -lm` | `-B`/`-L` 路径盘符大小写不对 |
| `PYTHON < 3.9 IS UNSUPPORTED` | pybind11 版本太新，回退 v3.0.4 |
| `fatal error: Python.h: No such file` | sysroot 没装 `libpython3.8-dev` |
| 产物叫 `.pyd` / `.cpython-314-*.so` | 没关掉 pybind11 的宿主机解释器探测 |
| `Join-Path : Cannot bind argument to parameter 'Path' because it is null` | `build.ps1` 里 `Split-Path -Parent $PSScriptRoot` 用法有误，已修 |
