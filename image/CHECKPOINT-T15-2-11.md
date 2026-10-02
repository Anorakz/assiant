# T15-2-11 检查点：首发镜像刷板 + 板端基线（2026-10-02）

> 这一阶段的目标（审核通过的任务列表）：把 T15-2-1 ~ 2-10 做出来的**发行镜像**
> 第一次真刷进 K1 Mini，跑起来，并留下可复核的板端数字。
>
> 过程比预想的曲折：首刷**起不来**，救回来之后又连着挖出五个真问题。
> 下面按"现象 → 证据 → 根因 → 修法"记录，全部有板端日志/命令输出支撑。

---

## 0. 结论速览

| 项 | 结果 |
| --- | --- |
| 板子状态 | ✅ 正常运行（`assistant.target` active，GUI 在刷界面，Agent 活着） |
| 启动耗时 | **1.855 s 内核 + 8.342 s 用户态 = 10.198 s** 到 `assistant.target`（冷启动实测） |
| 当前槽位 | **`_b`**（`android_slotsufix=_b`，root 挂 `system-b` 的 PARTUUID）—— A/B 回退真的生效了 |
| 内存占用 | 空闲 **150 MB / 3901 MB**（可用 3700 MB） |
| 温度 | soc **31.9 ℃**、gpu 29.4 ℃（空闲） |
| NPU | ✅ 真推理：`init_runtime=0`，mobilenet_v1 单帧 **7.1 ms**（首帧 17.8 ms），librknnrt 2.3.2 |
| 硬解 | ✅ 真编真解：`mpph264enc ! h264parse ! mppvideodec`，720p × 120 帧 **1.465 s** |
| 触摸 | ✅ 点哪准哪（修法见 §5） |
| WiFi | ✅ 硬件/驱动正常，已拿到 `192.168.137.30`；NM 侧通路见 §6（b4 镜像已修） |
| 外网 | ✅ `ping 8.8.8.8` 175 ms 0% 丢包（DNS 待 NM 接管后复测） |
| 串口 | ✅ `serial-getty@ttyFIQ0` 起来，`root` + 空密码直接进（修法见 §4） |
| `/data` | 22.7 GB（修法见 §7） |

---

## 1. 首刷起不来：u-boot 缺 A/B 支持（已修）

**现象**（串口）：

```
U-Boot next-dev (Sep 29 2026 - 22:02:18 +0800)
PartType: EFI
FIT: No boot partition                       ← 它在找名叫 boot 的分区
android_image_load_by_partname: Can't find part: boot
Could not find userdata part
=>                                           ← 掉到 u-boot 命令行（屏幕全黑）
```

**证据**：u-boot 提示符下 `part list mmc 0` 显示 GPT **完全正确**
（`uboot misc boot_a boot_b backup system_a system_b oem userdata`，GUID 齐全）。

**根因**：`RK_AB_UPDATE=y` 只影响**分区表模板与打包**（`Config.in.firmware`、
`common/scripts/mk-updateimg.sh`），**传不到 u-boot**。厂商给 rk3588/rv1126/rk3576
都带了 A/B 片段，**rk3568 没有** → u-boot 按非 A/B 路径找 `boot`。

**修法**：新增 `image/uboot/rk3568-assistant-ab.config`（开 `CONFIG_ANDROID_AB`），
板级 defconfig 用 `RK_UBOOT_CFG_FRAGMENTS` 指过来，`install-into-sdk.sh` 注入。
校验：`u-boot/.config` 有 `CONFIG_ANDROID_AB=y`；ELF 里有
`ab_get_slot_suffix` / `ab_update_root_partition` 两个全局符号。

> 顺带一个坑：片段里的**注释不许出现 `CONFIG_*` 记号** —— make.sh 会 grep 它，
> 第一版因此让 u-boot 构建 4 秒就死（`sed: can't read configs/#`）。
> 守卫测试：`TestUbootHasAbSupport`。

---

## 2. 启动卡死：networkd 的"等网器"无超时

**现象**：冷启动日志停在 19 秒，之后控制台再无输出，agent/gui 一直不启动。

```
[  7.0] Started Network Manager
[  8.0] Finished Network Manager Wait Online.      ← NM 自己这个 8 秒就过了 ✓
[11s–19s+] A start job is running for "Wait for Network to be Configured" (no limit)
```

**根因**：buildroot 的 `90-systemd.preset` 把 **`systemd-networkd-wait-online`**
打开了，而这个镜像里 networkd 没有任何 link 可管（网络全归 NetworkManager，
`/etc/systemd/network/` 为空）→ 它的启动任务**无超时**地干等，把
`network-online.target` 顶住，而 `assistant.target` / `agent.service` 都挂在那上面。

**修法**：post-build 里 `mask` 它（`/dev/null`）；另把 `nm-online` 限时 5 秒
（`NetworkManager-wait-online.service.d/timeout.conf`）作为兜底。

---

## 3. 启动耗时基线（修完之后，冷启动实测）

```
Startup finished in 1.855s (kernel) + 8.342s (userspace) = 10.198s
assistant.target reached after 8.341s in userspace.
blame: NetworkManager-wait-online 5.06s（当时还没连上网，撞 5 秒上限）
       wifibt-init 0.96s / dev-mmcblk0p7 0.94s / NetworkManager 0.86s / 其余 <0.7s
```

WiFi 通了之后 `NetworkManager-wait-online` 应当秒过 → 预期降到 ~5 s 量级（b4 复测）。

---

## 4. 串口没有 shell：`getty.target` 没人拉

**现象**：板子起来了、屏上有界面，但串口**一个字符都不回**（不是乱码，是根本没有输入回显）。

**根因**：我们把 `default.target` 指到 `assistant.target` 之后，`getty.target`
就**没有人拉**了（普通系统是 `multi-user.target` 拉它），于是 post-build 里那份
`serial-getty@ttyFIQ0.service` 的软链永远到不了。
（buildroot 的 `BR2_TARGET_GENERIC_GETTY_PORT="ttyFIQ0"` 在 systemd 形态下**没有**
生成软链，`getty.target.wants/` 是空的；systemd 的 getty 生成器又不认 `ttyFIQ0`
这个 Rockchip fiq_debugger tty。）

**修法**：`assistant.target` 显式 `Wants=getty.target` + `After=getty.target`；
post-build 显式建 `getty.target.wants/serial-getty@ttyFIQ0.service`。
板上验证：`systemctl is-active getty.target serial-getty@ttyFIQ0.service` → 都 active，
登录后 `id` = `uid=0(root)`。
（`serial-getty@.service` 用 `--keep-baud` + `TTYPath=/dev/%I`，所以不会把 1500000
改成 115200 —— 口径安全。）

---

## 5. 触摸位置异常：必须用 libinput 校准矩阵，不能用 Qt 的 evdev 环境变量

**现象**：界面被 EGLFS 转了 90° 显示正常，但触摸点哪都不对；反手改
`QT_QPA_EVDEV_TOUCHSCREEN_PARAMETERS` 的 90/180/270/invertx/inverty **全都没用**。

**关键证据**（板端 `grep <gui pid>/maps`）：

```
/usr/lib/qt/plugins/platforminputcontexts/libqtvirtualkeyboardplugin.so   ← 只有这个
（没有 libqevdevtouchplugin.so）
/usr/lib/libinput.so.10.13.0                                             ← 但有 libinput
```

→ 这套 `QT_QPA_PLATFORM=eglfs` + `QT_QPA_EGLFS_INTEGRATION=eglfs_kms` 是
**eglfs_kms 自己用 libinput 处理输入**，通用 evdev 插件根本不参与，所以那些
环境变量**完全无效**。

**定标**（抓 `/dev/input/event2` 原始事件，用户依次点**界面上**的四角+中间）：

| 界面上点的位置 | 原始 X | 原始 Y |
| --- | --- | --- |
| 左上 | 51 | 1237 |
| 右上 | 37 | 49 |
| 左下 | 731 | 1266 |
| 右下 | 754 | 28 |
| 中间 | 351 | 705 |

规律：**原始 X 对应界面的上下、原始 Y 对应界面的左右**（差 90°），且原始 Y 随向右而变小
→ 归一化后需要 `x' = 1 − y`、`y' = x` → libinput 矩阵 `[0 -1 1; 1 0 0; 0 0 1]`。

**修法**：`/etc/udev/rules.d/99-assistant-touch.rules`：

```
ACTION=="add|change", KERNEL=="event*", ATTRS{name}=="goodix-ts",
    ENV{LIBINPUT_CALIBRATION_MATRIX}="0 -1 1 1 0 0 0 0 1"
```

板上验证：`udevadm info /dev/input/event2` 里出现该属性；重启 GUI 后**用户确认
"手指点哪，界面就反应在哪"** ✓。同时把单元里那条**无效**的环境变量删掉了
（留着只会让下一个人继续白折腾），守卫测试：`TestTouchRotationIsDoneInLibinput`。

---

## 6. WiFi：NM 驱动不了 wpa_supplicant（缺 D-Bus 接口）

**现象**：`nmcli device status` 里 `wlan0` 恒为 `unavailable`，两份 WiFi 凭据都没机会用。

```
device (wlan0): Couldn't initialize supplicant interface:
                Failed to D-Bus activate wpa_supplicant service
device (wlan0): supplicant interface keeps failing, giving up
```

**根因**：厂商的 `wireless.config` 开了 WPA_SUPPLICANT/AP/AUTOSCAN/EAP/CLI/…
**唯独没开 DBUS** → `wpa_supplicant -u` 直接把用法打出来就退出（`-u` 不被识别）、
`wpa_supplicant.service`（`Type=dbus`）起不来、
`/usr/share/dbus-1/system-services/fi.w1.wpa_supplicant1.service` 激活文件也不存在。
NM 只能走**新** D-Bus API。

**硬件是好的**（手工验证）：`wpa_supplicant -B -i wlan0 -c /tmp/wpa.conf` →
`[SKWIFI6621S STATE] NONE → AUTHING → AUTHED → ASSOCING → ASSOCED → COMPLETED`，
`udhcpc` 拿到 `192.168.137.30`（lease 604800），网卡 MAC `60:48:9C:B7:6C:C8`。

**修法**：buildroot defconfig 末尾加 `BR2_PACKAGE_WPA_SUPPLICANT_DBUS=y`
（**注意**：那个"NEW"版开关在 2024.02 里是 **legacy 符号**，写了会 `select BR2_LEGACY`
→ 构建直接死在 `Makefile.legacy:9`；守卫测试 `TestWpaSupplicantHasDbusControlInterface`）。
因为 buildroot **不会因选项变化自动重编包**，另外用 SDK 的
`./build.sh bmake:wpa_supplicant-dirclean` + `bmake:wpa_supplicant` 定向重编。
校验：target 里有 `fi.w1.wpa_supplicant1.service`，二进制里该名字出现 29 次。

**另外两件相关的事**：
- 这块板子的无线模块实际是 **Seekwave SV6160LITE**（驱动 `skw_sdio_lite`），
  不是 DTB 里写的 `rtl8822cs`；驱动想要 NV 文件 `SWT6621S_NV_SDIO.bin`，而厂商只给了
  `_ALONE`/`_SHARE` 两个变体（编译期宏二选一），日志里
  `no BT_antenna setting` / `no DTS setting` → 它回退到默认名后**容错继续**，
  关联照样成功（NV 只影响校准质量，待后续专项）。
- MAC 在 WiFi 服务起来之前是随机的（`rk_vendor_read wifi mac address failed`），
  起来之后是真实 MAC。`Anorak_host` 才是热点真名（用户口头给的是 `Anroak_host`），
  所以镜像里**两份凭据都装**，谁对连谁。

---

## 7. `/data` 只有 3.5 MB：分区长大了，文件系统没长

**证据**：`/proc/partitions` 里 `mmcblk0p9` = **23,932,911 KB（22.8 GiB）**（GPT 正确），
但 `df -h /data` 只有 **3.5 MB** —— 烧进去的还是 `userdata.img` 那个小 ext4。

**修法**：fstab 上 `x-systemd.growfs`（挂载时按分区扩到底，幂等）；另外删掉厂商那份
`PARTLABEL=userdata /userdata`（同一个文件系统被挂两次，板端 `df` 里 `/data` 与
`/userdata` 指向同一个 `mmcblk0p9`）。
⚠ post-build 里做成**幂等修补**：target 树跨次构建复用，只判断"有没有那行"会让旧选项
永远留着（这次就踩到：target 里那行还是旧的、没有 growfs）。
板上手工验证：`resize2fs /dev/mmcblk0p9` → `22.7G` ✓。

---

## 8. 本轮遗留（下一批处理，不算未通过）

1. **NTP 没同步**：`systemd-timesyncd` active 但 `System clock synchronized: no`；
   IP 层外网是通的（8.8.8.8 175 ms），等 NM 接管 DNS 后复测。RTC（hym8563）可读可写，
   已把系统时间写回（`hwclock -w`）。
2. **`/` 余量偏紧**：632 MB 里用了 528 MB（**90%**，剩 57.5 MB）。T15-12 收体积时处理
   （`/usr/lib/assistant` 自己占 17 MB）。
3. **Seekwave NV 校准文件**：见 §6，待专项（需要时可用厂商 Ubuntu 镜像里的
   `/lib/firmware` 做对照）。
4. **模型拷贝**：4.86 GB 模型还没进 `/data/model`（首刷后 `/data` 是空的）。
5. **GL 后端的 GUI 对比**（`QT_QUICK_BACKEND=software` vs GL）—— T15-2-11 原计划项。
6. **`rk-pcie ... failed to initialize host`** 每次开机重试 ~1.5 s（板上没有 PCIe 设备），
   属 T15-12 收尾。
7. 首刷那次的 `misc` 状态：BCB 在 **0x800** 偏移、含 `AB0` 标记 + 非零状态
   → A/B 元数据确实在用（`misc` 前 64 字节全零容易误判成"没用"）。

---

## 10. b5 镜像（第二批修复）板端验收 —— 2026-10-02 16:4x

b5 = b4 的全部修复 + SSH 密码/公钥烘进镜像。刷完之后**逐项在板上核对**：

| 项 | 证据 |
| --- | --- |
| 串口要密码了 | `rk3568-buildroot login: root` → `Password:` → 进（密码 `assistant`） |
| SSH 免密可用 | `ssh rk3568` → `uid=0(root)`、`hostname=rk3568-buildroot` |
| **WiFi 自己连上** | `nmcli device status` → `wlan0 wifi connected assistant-wifi`；`ip addr` → `inet 192.168.137.30/24`；`dmesg` → `SKWIFI6621S … ASSOCED -> COMPLETED` |
| agent 也认了网 | `agent.log`：`net: WiFi 就绪 (wlan0, connected, ssid=Anorak_host, ip=192.168.137.30)；每 30.0s 体检一次` |
| **DNS 通了** | `ping www.baidu.com` 0% 丢包 18 ms（`resolv.conf` → `nameserver 192.168.137.1`） |
| **`/data` 自动长大** | `df -h /data` → **22.7 G**；`systemd-analyze blame` 里有 `196ms systemd-growfs@data.service` |
| 触摸 | 用户确认"点哪准哪"（`LIBINPUT_CALIBRATION_MATRIX` 那条 udev 规则） |
| 启动 | `1.860s 内核 + 8.556s 用户态 = 10.417s`（其中 `NetworkManager-wait-online` 占 5.05s，见 §11） |

### 10.1 首次开机收尾：模型落位 + 真加载

| 项 | 证据 |
| --- | --- |
| 模型搬进 `/data/model` | 4.86 GB / **18 个文件**，`scp` 用时 **131 秒**（≈ **37 MB/s**，WiFi 实测）→ `/data` 用 21% |
| **本地 LLM 真跑** | `llama-server -m /data/model/Qwen3-0.6B-Q4_K_M.gguf -c 2048 -t 4` → `model loaded` + `listening on http://127.0.0.1:8080`；一次生成：**9.3 tok/s**（prompt 31.2 tok/s，48 token / 5.73 秒） |
| 推理时资源 | 内存 148 MB → **804 MB**；soc 温度 31 ℃ → **44.4 ℃**（余量足） |
| **视觉塔（SigLIP）在 NPU 上加载** | `rknnlite.load_rknn(/data/model/siglip_full.rknn)` → `0`、`init_runtime()` → `0`（403 MB，toolkit 2.3.2，target rk3568） |
| agent 健康 | `agent.log`：`LLMProvider 就绪 (mode=disabled)`、`IPC server 就绪 /tmp/agent.sock`、`GUI 已连接`；唯一 WARNING 是 `native(客户端证书不存在: /data/assistant/creds/client.pem —— 配对时生成)`（预期内，配对后才有） |

### 10.2 b5 阶段又挖出的两个真问题（都已修进配方）

**① NTP 默认源在国内网络下不通**

```
systemd-timesyncd: Timed out waiting for reply from 216.239.35.4:123 (time2.google.com).
System clock synchronized: no        ← 板子时间停在 2024-01-25（差两年多）
```
buildroot 编译进的默认池是 Google 的 time1..4；实测 `ntp.aliyun.com` / `cn.pool.ntp.org` /
`ntp.tencent.com` **都回包**。修法：post-build 写
`/etc/systemd/timesyncd.conf.d/assistant-ntp.conf`（不覆盖 buildroot 那份）。
板上验证：`Contacted time server 203.107.6.88:123 (ntp.aliyun.com)` →
`Initial clock synchronization to Fri 2026-10-02 16:40:21 CST` → `synchronized: yes`，
并 `hwclock -w` 写回 RTC。
（时间不对会连带影响 TLS 证书校验 —— 云端 LLM / OTA 都要用它。）

**② 配置里的默认路径还是厂商老路径**

板端 `/data/assistant/config/config.yaml`（从我们的模板铺出来的）里
`llm.model_path` / `vision.model_path` / `tokenizer_path` 仍写着
`/home/kickpi/model/...`，而镜像布局（D7）里模型在 **`/data/model`**（rootfs 只有
633 MB、剩 57 MB，放不下 4.9 GB）。照旧模板跑 = Agent 找不到模型。
修法：`config/config.example.yaml` 改成 `/data/model/…`、壁纸 `/data/assistant/wallpapers`、
证书 `/data/assistant/creds/`；`assistant-init` 补建这两个目录；板上同步改了一份。

### 10.3 顺手记下的两个"环境事实"

- **镜像里没有 `pgrep` / `pkill`**（busybox 只给 `ps`/`kill`）—— 我的测试脚本第一版
  因此把"在跑的服务"误判成"已退出"（`pgrep` 返回 127 被当成"没进程"）。
- 这块板子的无线模块是 **Seekwave SV6160LITE**；`setup TXBA failed, ret: -1` 是聚合
  告警（**不影响连通性**：ping 网关 0% 丢包、37 MB/s 传输、ssh 都正常），
  NV 校准文件缺失留作专项（§8.3）。

---

## 11. 待你决定的启动耗时优化（不改，先提方案）

现在的 10.417 s 里，**5.05 s** 是 `NetworkManager-wait-online` 撞了我们限的 5 秒上限：

- 板子起来后 WiFi 要 **~11 s** 才关联完成（`wifibt-init` 0.8 s + 驱动扫描/关联），
  而 `agent.service` 是 `After=network-online.target` → 它必须等；
- 我们给 `nm-online` 设了 `-t 5`，所以最多等 5 s 就放行（超时会留下一个 `failed` 状态）。

三个选项（等你选）：

| 方案 | 效果 | 代价 |
| --- | --- | --- |
| **A（推荐）**：`agent.service` 去掉 `After=network-online.target`（保留 `Wants=`） | 开机 ~5 s 到 agent/gui；网络 ~11 s 到位后由 agent 自己的 LinkGuard 接管 | 改 T15-2-8 的"起来时网络已就绪"约定 |
| B：把等网上限从 5 s 提到 15 s | 我们的服务起来时网络**确实**已就绪 | 总启动变成 ~13–15 s（更慢），且没网时更慢 |
| C：维持现状（5 s 上限） | 折中；`NetworkManager-wait-online` 每次开机显示 `failed`（无害但难看） | 不改 |

---

## 12. 本轮涉及的文件

| 文件 | 作用 |
| --- | --- |
| `image/uboot/rk3568-assistant-ab.config` | u-boot 的 A/B 支持（§1） |
| `image/device/.../rockchip_rk3568_kickpi_k1mini_release_defconfig` | `RK_UBOOT_CFG_FRAGMENTS`（§1） |
| `image/buildroot/configs/..._defconfig` | wpa_supplicant D-Bus（§6） |
| `image/board/.../udev/99-assistant-touch.rules` | Android→libinput 触摸矩阵（§5） |
| `image/board/.../post-build.sh` | networkd mask / getty / growfs / 单元权限 / WiFi 凭据（§2 §4 §6 §7） |
| `systemd/image/assistant.target` | `Wants/After=getty.target`（§4） |
| `systemd/image/agent-gui.service` | 去掉无效触摸变量、加 `EnvironmentFile`（§5） |
| `image/local/*.nmconnection.example` | WiFi 凭据通道（不进仓库、0600、支持多份拼写） |
| `tests/test_image_recipe.py` | 上述每条的守卫（50 → 56 项） |
