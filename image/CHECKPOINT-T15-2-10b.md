# T15-2-10b 断点存档（payload 装进 rootfs）

> 用途：**按用户要求在 23:00 断电前保存**。照着本文即可继续，不需要重新推导。
> 状态：**2-10b-1 … 2-10b-6 全部完成** —— payload **0/12**、`preflash-check.sh` **退出码 0**、
> 镜像已重新打包（`update-ab` 759,050,826 B）。剩下只有 **2-10b-7 的提交/CI 与你的验收**，
> 然后就是 **2-11 首次刷板**。
> 任务列表与出口见 `todo2.md` 的 2-10b-1…7。

## 0. 关键路径（照抄）

```bash
SDK=/home/anorak/rk3568_buildroot/linux-kernel-6.1/rk-linux6.1-2026060914/rk-linux6.1-2026060914
CFG=rockchip_rk3568_kickpi_k1mini_release
# ⚠ 整机构建那棵树是**嵌一层**的那个（docs/image.md §5.8）
T=$SDK/buildroot/output/$CFG/$CFG/target
REPO=/mnt/e/rk3568/project/myproject/assitant
```

- 仓库：PC `E:\rk3568\project\myproject\assitant`（WSL `/mnt/e/...`）
- 单包构建树：`$SDK/buildroot/output/$CFG/target`
- 整机构建树（**update.img 的来源**）：`$SDK/buildroot/output/$CFG/$CFG/target`

## 1. 这一轮做到哪了

| 步骤 | 状态 | 证据 |
| --- | --- | --- |
| 2-10b-1 源清单 + `build-payload.sh` 骨架 | ✅ | `--dry-run` 三种走法：copy+gen → RC 0；全量 → RC 1（明确报"半成品：未实现 2 项"）；不给 `--target` → RC 2 拒绝猜树。`tests/test_image_payload.py` 13 项与检查器/unit 三方互校 |
| 2-10b-2 python 侧进镜像 | ✅ | 装进整机 target：`agent/` **66 个 .py、0 个 pyc**、两个 example.yaml、Readme、docs、`/usr/bin/assistant`、`/etc/profile.d/assistant.sh`；幂等（二次跑 7 跳过）；戳在 **rootfs 之外**；权限归一 0755/0644。**`_ssl` 缺件修好**：chroot 里 `ssl ok: OpenSSL 3.2.1 | hashlib ok` 且 **`agent import ok`** |
| 2-10b-3 native cp311 | ✅ | `Machine: AArch64`；`NEEDED` = moonlight + `librockchip_mpp.so.1`（都在同目录/镜像里）；chroot `import agent_native` **RC=0**，导出 `BUTTON_*/MODIFIER_*/FRAME_*` |
| 2-10b-4 GUI 交叉编译 | ✅ | `image/build-gui.sh`：1,084,712 B → strip **821,368 B**；`Machine: AArch64`；`NEEDED` 的 Qt5 全家 + yaml-cpp 都在镜像里；chroot `agent_gui --help` RC=0 |
| 2-10b-5 串进 post-build | ✅ | 先删掉 target 里的 payload 再整机构建 → post-build 自己装回来（agent/gui/native/配置/文档/两个入口） |
| 2-10b-6 重新打包 + 全量验证 | ✅ | `preflash-check.sh` **RC=0、payload 0/12**；`update-ab` **759,050,826 B**；chroot 里九个模块**全部 import ok** |

## 1.5 这一轮的三个新坑（都在串线时暴露，已修并加了守卫）

1. **模板路径两种布局**：清单里 `gen` 的源是仓库相对路径，SDK 里在 `tools/assistant/payload/`
   → 改成按四个候选位置找，找不到才报错（整机构建当场断过一次）。
2. **同一 `how` 只许跑一次**：清单有**两条** `native`（扩展 + moonlight），两条都跑会把
   moonlight 落点覆盖成扩展本身 → `import agent_native` 报 `undefined symbol: LiStartConnection`。
3. **取依赖库认符号不认文件名**：`find … -name 'libmoonlight-common-c.so*' | head -1`
   抓到过同名错文件 → 只从 `$BUILD_DIR/moonlight-common-c/` 取，**且**验 `T LiStartConnection`。

## 2. 两个坑（都写进配方注释了）

### 2.1 镜像里的 python3 **没有 `_ssl`** → agent 起不来

chroot 实测（本轮的 X 光片）：

```
import agent.cli
  → agent/core/__init__ → agent/core/music → agent/net/__init__
  → agent/net/sunshine_client.py:57 → import ssl
  → ModuleNotFoundError: No module named '_ssl'
```

`agent/core/__init__` 会连锁 import 到它 ⇒ 板上 `agent.service` 会**一直崩溃重启**。
厂商基座把 python3 的扩展模块全关着（`# BR2_PACKAGE_PYTHON3_SSL is not set`）。

修法（已进配方）：在我们的 buildroot defconfig **末尾**（所有 include 之后）加

```
BR2_PACKAGE_PYTHON3_SSL=y        # → _ssl + _hashlib（python3.mk 一次开两个）
BR2_PACKAGE_PYTHON3_READLINE=y   # → 串口 CLI 的行编辑/历史
```

守卫：`tests/test_image_recipe.py::TestPythonModulesTheAgentNeeds`（3 项：必须 `=y`、
必须在所有 `#include` **之后**、注释里必须写明理由）。

### 2.2 vendor 的 `./build.sh <defconfig>`（lunch）**不会重新生成 buildroot 的 .config**

这是上面那个坑的"为什么我改了没生效"：

```
./build.sh rk3566_rk3568:<cfg>_defconfig              → .config 纹丝不动（时间戳不变）
cd buildroot && make O=output/<cfg> <cfg>_defconfig   → 立刻 PYTHON3_SSL=y
```

`./build.sh <defconfig>` 在已配置过的树上只打印
`Running within sudo(root) environment! / Using last kernel version(6.1)` 就结束了。
**整机构建（`./build.sh all` → `mk-buildroot.sh`）走的是后者那条路**，所以 2-9 的
`.config` 是新的（当时我们还没加这两行，所以是 `not set`）。
→ 以后**验证配置改动**一律用 `make O=... <cfg>_defconfig`，别用 lunch。

## 3. 断电后怎么继续

```bash
# 0) 现状复核（三条命令应该全绿）
SDK=/home/anorak/rk3568_buildroot/linux-kernel-6.1/rk-linux6.1-2026060914/rk-linux6.1-2026060914
CFG=rockchip_rk3568_kickpi_k1mini_release
T=$SDK/buildroot/output/$CFG/$CFG/target
chroot $T /usr/bin/env PYTHONPATH=/usr/lib/assistant /usr/bin/python3 -c \
  "import agent.cli, agent.main, agent_native, ssl, hashlib; print('payload import ok')"
chroot $T /usr/bin/assistant --help | head -5          # 包装脚本（自己会设 PYTHONPATH）
wsl -u root bash image/preflash-check.sh $SDK           # payload 应为 1/12

# 1) 下一步：写 image/build-gui.sh 并交叉编译 GUI（2-10b-4）
#    工具链要点见下面第 4 节
```

## 4. 接下来（2-10b-4 GUI）已经确认的可用条件

- `toolchainfile.cmake`：`$SDK/buildroot/output/$CFG/$CFG/host/share/buildroot/toolchainfile.cmake`
- Qt host 工具：`…/host/bin/{moc,qmake,rcc,uic}`；sysroot 的 Qt5 cmake 配置在
  `…/host/aarch64-buildroot-linux-gnu/sysroot/usr/lib/cmake/Qt5*`
  （`Core/Gui/Widgets/Network/Multimedia/Quick/Qml/Svg/QuickControls2` **都在**）
- GUI 需要 `find_package(Qt5 5.12 COMPONENTS Core Network Widgets Multimedia MultimediaWidgets)`
  + `yaml-cpp`；镜像里 `libQt5Widgets/Qt5Multimedia/Qt5MultimediaWidgets/libyaml-cpp` **都在**
- ⚠ GUI 的 CMake 里也可能有板端 Ubuntu 假设（就像 native 的 3.8 与 MPP 路径），
  遇到就按 native 那次的办法处理（认多种布局 / 缺的可选化），别硬改板端行为

## 5. 已经落进仓库的东西（这一轮）

- `image/payload.manifest`（源 → 落点唯一来源表；`how` = copy/gen/native/gui）
- `image/build-payload.sh`（只认 `--target`/`TARGET_DIR`；`--only`/`--dry-run`/`--force`；
  幂等内容哈希戳放在 rootfs 之外；目录复制后**权限归一** 0755/0644 —— 因为仓库在
  Windows 盘上（DrvFs），源文件是 777，照抄进镜像既丑又不安全）
- `image/payload/assistant`（`/usr/bin/assistant` 包装）、`image/payload/assistant.sh`（profile.d）
- `tests/test_image_payload.py`（13 项：清单 ↔ 检查器 ↔ unit 三方互校）
- `image/check-runtime-deps.py`：PAYLOAD 表 +2 项（`usr/bin/assistant`、`etc/profile.d/assistant.sh`）
- buildroot defconfig：`BR2_PACKAGE_PYTHON3_SSL=y`、`BR2_PACKAGE_PYTHON3_READLINE=y`
