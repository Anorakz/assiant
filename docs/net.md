# 网络（WiFi 链路，T14-9）

> 一句话：**板子只有 wlan0 这一条链路**（eth0/eth1 都是 `unavailable`），断网就是失联。
> 所以这里不仅做"连 WiFi"的界面，还做**链路健康检查 + 主动重连** —— 上次断网板端
> 一条日志都没有，这一节就是补那个洞。
>
> 相关：[`gui.md`](gui.md)（设置页那张卡片）、[`ipc-protocol.md`](ipc-protocol.md)（`wifi` / `wifi_control`）、
> [`crash.md`](crash.md)（Agent 崩了看哪里）。

---

## 1. 板端事实（2026-09-27 实测，别再靠猜）

| 项 | 实测值 |
| --- | --- |
| 网络管理 | **NetworkManager 1.22.10 + nmcli**（`wpa_supplicant` 在跑；systemd-networkd 没开；没有 netplan） |
| 唯一链路 | `wlan0`（`eth0`/`eth1` = `unavailable`，`dummy0`/`lo` = unmanaged） |
| 当前连接 | `Anorak_host`（WPA2，`AUTOCONNECT=yes`），IP `192.168.137.30/24`，网关/DNS `192.168.137.1` |
| 密码存哪 | `/etc/NetworkManager/system-connections/Anorak_host.nmconnection`（**0600 root**） |
| 联网时能不能扫 | **能**（`nmcli device wifi list --rescan yes`） |
| 机读输出 | `nmcli -t -e yes -f …`（`\:` 表示字段里的冒号） |
| 健康判据可用 | 网关 `ping -c1 -W1`；`nmcli networking connectivity check` 也能用 |

⚠ 两个坑：

- `GENERAL.STATE` 的真实样子是 **`100 (connected)`** —— 状态词在括号里，`split()` 会拿到数字；
- `nmcli connection show` 的**列表**形式只认连接级字段：带 `802-11-wireless.ssid` 会报
  `Error: invalid field`。要知道每个档案的 SSID 得逐个 `connection show <name>` 问。

---

## 2. Agent 侧：`agent/net/wifi.py`

只用 nmcli（不走 dbus），一层薄封装：

| 方法 | 干什么 |
| --- | --- |
| `status()` | 设备/状态/SSID/信号/安全/IP/网关/DNS/档案名/autoconnect/connectivity 一次快照 |
| `scan()` | 扫一圈，**同名多 AP 只留信号最强的那个**（板端实测 `SZU_CTC&CMCC` 一次扫到 4 个），标 `in_use` |
| `connect(ssid, password, autoconnect)` | 连；已有档案就走 `connection up`，没有就**写 NM 的 keyfile** 再 `reload && up` |
| `forget(ssid)` | 删**点名的那一个**档案（返回 `was_active`） |
| `set_autoconnect(ssid, on)` | 记住 / 不记住 |
| `health()` | 体检：有没有 IP、探不探得到网关（**不抛异常**） |
| `reconnect()` / `cycle_device()` | 主动修：`con up` →（还不通）`device disconnect/connect` |

### 密码的边界（重要）

- **不进 argv**：`ps` 里永远看不到密码。已有档案走 `nmcli --ask connection up`（密码从 **stdin** 进）；
  新档案由 Agent 写 keyfile（`0600`）再 `con reload && con up`。
- **不进 `config.yaml`**：真源里只可能出现"记住哪个 SSID / 要不要自动连"这种非秘密项。
- **不进日志**：日志里只有 action 与 SSID。`tests/test_wifi.py` 专门盯这条（含"任何 argv 里
  都不许出现密码"）。

---

## 3. 链路守护 `LinkGuard`（你定的力度：连续 3 次才动手）

```
每 30 秒探一次健康（有 IP + 探得到网关）
  连续 3 次不通（约 90 秒）→ 开始修：
     ① nmcli connection up <当前档案>      等 15 秒 → 复查
     ② 还不通 → device disconnect/connect   等 15 秒 → 复查
     ③ 还不行 → 记 error，间隔按次数退避（30 → 60 → 120 → 240 秒封顶）
恢复后计数归零，间隔回到 30 秒
```

**每一步都写日志**（`logs/agent.log`）：`wifi: 链路不通（探不到网关 192.168.137.1）— 第 1/3 次`、
`wifi: 开始主动重连（原因：…，第 1 次）`、`wifi: 重连成功（步骤 con-up:Anorak_host → IP …）`。
界面那张卡片也能看到 `probes / consecutive_failures / repairs`。

配置在 [`config.example.yaml`](../config/config.example.yaml) 的 `net:` 段：

```yaml
net:
  enabled: true            # 关掉 = 不做健康检查（界面仍能看状态、仍能连）
  ifname: wlan0
  health_interval_s: 30
  probe_failures: 3
  reconnect_wait_s: 15
```

⚠ 为什么判据不用 `nmcli networking connectivity check`：那是 NM 去访问**外网**探针，
路由器不出网时它会报 `limited`，而链路其实是好的 —— 我们要救的是**链路**。它只作为参考
信息一起显示。

---

## 4. 界面：设置页第 4 张卡片「网络」

- 状态行：`wlan0 · 已连接 Anorak_host · 信号 96 · IP … · 网关 … · 自动连接 开` + 一行"体检"数字；
- 「扫描」→ 列表（`●` 已连、`🔒` 加密、`开放`），**同名只留最强的那条**；
- 选中 → 填密码（开放网络不用填）→「连接」，旁边「记住并自动连接」；
- 「忘记」**只在选中的是当前网络时可用**，并且弹确认（板子只有这一条链路，删它 = 失联）；
- 「重连」= 让 Agent 立刻体检一次（`wifi_control reconnect`）。

整张卡片**只发 IPC**，nmcli 由 Agent 调（[`adr/0005`](adr/0005-config-single-writer.md) 同一条立场）；
Agent 没在跑时如实显示"Agent 没连上"，不静默失败。

---

## 5. 手动救回来（板子已经离线时）

网线没得插，所以只能**现场**操作（键盘 + 显示器，或串口）：

```bash
# 看现在什么情况
nmcli device status
nmcli -f GENERAL.STATE,GENERAL.CONNECTION,IP4.ADDRESS device show wlan0

# 把已知的那张档案拉起来（最常用的一招）
nmcli connection up Anorak_host

# 忘了密码 / 换了路由器：直接连一次（会问密码）
nmcli --ask device wifi connect "SSID"
# 或者新建一张记得住的档案
nmcli device wifi connect "SSID" password "密码"     # ⚠ 密码会进 argv（现场操作可接受）

# 还是不行：把设备重启一遍
nmcli device disconnect wlan0 && nmcli device connect wlan0

# 看看是谁在拦（NetworkManager 自己的日志）
journalctl -u NetworkManager -n 50 --no-pager
```

⚠ **删除档案之前先想清楚**：`nmcli connection delete <name>` 删掉唯一那张能连的档案之后，
板子不会自己回来 —— 只能现场重连。Agent 侧的 `forget` 也只删点名的那一个。

---

## 6. 怎么测

```bash
python tests/test_wifi.py                      # PC / 板端都能跑（假 nmcli，不碰真网络）
cd gui/build && QT_QPA_PLATFORM=offscreen ctest -R test_settings_page --output-on-failure
# 板端门禁（只读 + 只在哑档案上做动作；真断链路那步要显式开）
setsid nohup python3 tests/board/t14_9_accept.py > /tmp/t14_9.log 2>&1 &
setsid nohup python3 tests/board/t14_9_accept.py --outage-test > /tmp/t14_9.log 2>&1 &
```

⚠ `--outage-test` 会**真的断网**（先关 autoconnect 再 disconnect 设备），所以：
必须用 `setsid nohup … &` 脱离 SSH 会话跑（否则 ssh 一断脚本就被 SIGHUP 杀掉，
autoconnect 停在 `no` 上就真回不来了），而且脚本自己会先挂一个"每 20 秒无条件把
`Anorak_host` 拉回来、试 12 次"的兜底任务。
