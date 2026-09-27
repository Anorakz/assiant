#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T14-9 门禁实测：WiFi（真 Agent / 真 nmcli / 真 IPC）。

⚠ 板端 **只有 wlan0 这一条链路**（eth0/eth1 都 unavailable），所以这个脚本刻意分三段，
  把"会不会把板子锁在门外"这件事挡在设计里：

  A. 只读（真机执行）
     · `wifi_control status` → 设备/SSID/信号/IP/网关/自动连接都非空，且 `connected=true`；
     · `wifi_control scan` → 有结果、**按 SSID 去重**、已连的那个排最前且标 in_use；
     · status 里带 LinkGuard 那一段（probes / threshold=3）；
     · **交叉验证**：payload 里的 ssid/ip 必须与直接跑 nmcli 的结果一致（不能自说自话）。

  B. 动作（**只在哑档案上**，碰不到 Anorak_host）
     · 由 Agent 写一份 NM keyfile（`T14-TEST`）→ 权限 **0600**、里面有 psk、ssid 正确；
     · `nmcli connection reload` 之后 `known_profiles()` 能看见它；
     · `autoconnect=false` 能写进去、读得回来；
     · `forget("T14-TEST")` → 档案消失、`Anorak_host` 一动没动、链路还在（SSH 没断）。
     ⚠ **不激活**这个哑档案（`con up` 一个连不上的 SSID 会把当前连接顶掉 = 板子失联）。

  C. 链路守护
     · 守护真在跑：隔 ~35s 再问一次 status，`guard.probes` 必须 +≥1（真周期、真 nmcli）；
     · `wifi_control reconnect`（安全动作：只把当前档案再 up 一次）→ ack ok，IP/SSH 不变。

  D. 真断链路的主动恢复（`--outage-test` 才跑，**默认关闭**）
     · 先挂一个"每 20 秒无条件把 Anorak_host 拉回来、试 12 次"的兜底后台任务；
     · 把 autoconnect 关掉（不让 NM 自己抢修）+ `nmcli device disconnect wlan0`；
     · 等守护按"连续 3 次探不到网关"动手 → 链路自己回来；
     · 恢复 autoconnect，检查 Agent 日志里那几行（"开始主动重连"、"重连成功"）。
     ⚠ 这一段会真的断网：**必须**用 `setsid nohup ... &` 脱离 SSH 会话跑，否则 ssh 一断
       脚本就被 SIGHUP 杀掉，autoconnect 停在 no 上就真的回不来了。

  E. 不变量：真实 config/config.yaml md5 未变、`git status` 干净、
     `Anorak_host` 档案仍在且 autoconnect 仍是 yes、systemd 两个服务仍 active。

跑法（板端，仓库根）::

    setsid nohup python3 tests/board/t14_9_accept.py > /tmp/t14_9.log 2>&1 &
    # 带真断网那一段（读完上面那段警告再跑）：
    setsid nohup python3 tests/board/t14_9_accept.py --outage-test > /tmp/t14_9.log 2>&1 &
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SOCKET_PATH = "/tmp/agent.sock"


def configured_socket_path():
    """从 board 的真配置里读 `ipc.socket_path`（不硬编码：配置改了这个门禁要跟着走）。"""
    config = os.path.join(REPO, "config", "config.yaml")
    if not os.path.exists(config):
        return SOCKET_PATH
    try:
        import yaml
        with open(config, encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        return str((data.get("ipc") or {}).get("socket_path") or SOCKET_PATH)
    except Exception:                                     # noqa: BLE001
        return SOCKET_PATH


def ensure_agent_socket(path):
    """确保真 Agent 的 socket 在（板端实测过一种现网故障：socket 的文件名被删掉、
    内核里还在监听 —— 那时 CLI/GUI 全连不上）。这里重启一次服务把它重建出来。"""
    if os.path.exists(path):
        return True
    subprocess.run(["systemctl", "restart", "agent.service"], stdout=subprocess.DEVNULL)
    for _ in range(20):
        time.sleep(1)
        if os.path.exists(path):
            return True
    return False
REAL_FILES = ["config/config.yaml"]
LIVE_PROFILE = "Anorak_host"
TEST_PROFILE = "T14-TEST"
TEST_PSK = "t14-test-psk-12345"
NM_DIR = "/etc/NetworkManager/system-connections"

sys.path.insert(0, REPO)

_PASSED = []
_FAILED = []


def check(name, ok, detail=""):
    (_PASSED if ok else _FAILED).append(name)
    print("  %s %s%s" % ("✔" if ok else "✘", name, ("  —— %s" % detail) if detail else ""),
          flush=True)
    return ok


def md5(path):
    with open(path, "rb") as handle:
        return hashlib.md5(handle.read()).hexdigest()


def nmcli(*args, timeout=30):
    proc = subprocess.run(["nmcli"] + list(args), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True, timeout=timeout)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


# ---------------------------------------------------------------------------
#  一个极小的 IPC 客户端（只为本门禁用；不 import agent.*，免得把 Agent 带进同进程）
# ---------------------------------------------------------------------------
class Ipc:
    def __init__(self, path=SOCKET_PATH, timeout=40.0):
        self.timeout = timeout
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(path)
        self.buf = b""

    def send(self, action, payload):
        line = json.dumps({"action": action, "payload": payload},
                          ensure_ascii=False, separators=(",", ":")) + "\n"
        self.sock.sendall(line.encode("utf-8"))

    def wait_for(self, topic, kind=None, predicate=None, timeout=None):
        """读到一条指定 topic（可选 kind / 附加判据）的推送；超时抛 TimeoutError。"""
        deadline = time.time() + (timeout or self.timeout)
        while time.time() < deadline:
            self.sock.settimeout(max(0.5, deadline - time.time()))
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            self.buf += chunk
            while b"\n" in self.buf:
                raw, self.buf = self.buf.split(b"\n", 1)
                if not raw.strip():
                    continue
                try:
                    message = json.loads(raw.decode("utf-8"))
                except ValueError:
                    continue
                if message.get("topic") != topic:
                    continue
                data = message.get("data") or {}
                if kind is not None and data.get("kind") != kind:
                    continue
                if predicate is not None and not predicate(data):
                    continue
                return data
        raise TimeoutError("等 %s/%s 超时" % (topic, kind))

    def close(self):
        self.sock.close()


def wifi(action, payload=None, kind=None, timeout=40.0, predicate=None):
    """发一条 wifi_control 并等它那条推送。"""
    client = Ipc(timeout=timeout)
    try:
        client.send("wifi_control", dict(payload or {}, action=action))
        return client.wait_for("wifi", kind=kind, predicate=predicate, timeout=timeout)
    finally:
        client.close()


# ---------------------------------------------------------------------------
#  A. 只读
# ---------------------------------------------------------------------------
def check_readonly():
    print("\n== A. 只读（真 Agent / 真 nmcli）")
    status = wifi("status", kind="status")
    check("A1 status：设备是 wlan0", status.get("device") == "wlan0", str(status.get("device")))
    check("A1 status：connected=true", status.get("connected") is True)
    check("A1 status：SSID / IP / 网关都非空",
          bool(status.get("ssid")) and bool(status.get("ip")) and bool(status.get("gateway")),
          "ssid=%s ip=%s gw=%s" % (status.get("ssid"), status.get("ip"), status.get("gateway")))
    check("A1 status：autoconnect 是布尔", isinstance(status.get("autoconnect"), bool))

    # 交叉验证：payload 必须与直接跑 nmcli 的结果一致
    rc, out, _err = nmcli("-t", "-e", "yes", "-f", "IP4.ADDRESS", "device", "show", "wlan0")
    real_ip = out.split(":")[-1].split("/")[0].strip() if rc == 0 and out else ""
    check("A1 交叉验证：IP 与 nmcli 直出的一致", real_ip == status.get("ip"),
          "nmcli=%r payload=%r" % (real_ip, status.get("ip")))

    scan = wifi("scan", kind="scan", timeout=60.0)
    points = scan.get("points") or []
    names = [p.get("ssid") for p in points]
    check("A2 scan：有结果", len(points) >= 2, "count=%s" % scan.get("count"))
    check("A2 scan：按 SSID 去重", len(names) == len(set(names)),
          "重复项=%r" % ([n for n in names if names.count(n) > 1][:3],))
    check("A2 scan：已连的那个排最前且标 in_use",
          bool(points) and points[0].get("ssid") == status.get("ssid")
          and points[0].get("in_use") is True,
          "%r" % (points[0] if points else None,))
    check("A2 scan：每项都有 signal / secured 字段",
          all(isinstance(p.get("signal"), int) and isinstance(p.get("secured"), bool)
              for p in points))

    guard = status.get("guard") or {}
    check("A3 status 带 LinkGuard 段（probes / threshold=3）",
          isinstance(guard.get("probes"), int) and guard.get("threshold") == 3,
          json.dumps(guard, ensure_ascii=False)[:120])
    return status, guard


# ---------------------------------------------------------------------------
#  B. 动作（只在哑档案上）
# ---------------------------------------------------------------------------
def check_actions():
    print("\n== B. 动作（哑档案 %s，绝不碰 %s）" % (TEST_PROFILE, LIVE_PROFILE))
    from agent.net.wifi import Wifi, WifiError      # noqa: E402

    wifi_api = Wifi()
    # 先确保干净（上一次失败留下的）
    try:
        wifi_api.forget(TEST_PROFILE)
    except WifiError:
        pass

    # B1：由 Agent 的代码路径写一份 keyfile（**不** activate）
    profile_name = None
    try:
        profile_name = wifi_api._write_keyfile(TEST_PROFILE, TEST_PSK, autoconnect=True)
    except WifiError as exc:
        check("B1 写 keyfile", False, str(exc))
        return
    path = os.path.join(NM_DIR, profile_name + ".nmconnection")
    check("B1 keyfile 落在 NM 的目录里", os.path.exists(path), path)
    mode = oct(os.stat(path).st_mode & 0o777)
    check("B1 keyfile 权限是 0600（密码就在里面）", mode == "0o600", mode)
    text = open(path, encoding="utf-8").read()
    check("B1 keyfile 里有 ssid", ("ssid=%s" % TEST_PROFILE) in text)
    check("B1 keyfile 里有 psk", ("psk=%s" % TEST_PSK) in text)
    check("B1 keyfile 里有 autoconnect=true", "autoconnect=true" in text)

    rc, _out, _err = nmcli("connection", "reload")
    check("B1 nmcli connection reload 成功", rc == 0)
    profiles = wifi_api.known_profiles()
    check("B1 reload 之后能被 known_profiles() 看见",
          profiles.get(profile_name) == TEST_PROFILE, json.dumps(profiles, ensure_ascii=False))

    # B2：autoconnect 改得动、读得回
    wifi_api.set_autoconnect(TEST_PROFILE, False)
    rc, out, _err = nmcli("-t", "-f", "connection.autoconnect", "connection", "show",
                          profile_name)
    check("B2 autoconnect=false 写进去了", "connection.autoconnect:no" in out, out)

    # B3：忘记它 —— 只删这一个
    result = wifi_api.forget(TEST_PROFILE)
    check("B3 forget 删的是这个档案", result.get("profile") == profile_name)
    check("B3 文件没了", not os.path.exists(path))
    check("B3 它不在档案列表里了",
          TEST_PROFILE not in wifi_api.known_profiles().values())
    live = wifi_api.known_profiles()
    check("B3 Anorak_host 一动没动（唯一链路安全）", live.get(LIVE_PROFILE) == LIVE_PROFILE,
          json.dumps(live, ensure_ascii=False))
    state = wifi_api.status()
    check("B3 链路还在（IP 仍有）", bool(state.ip), state.ip)


# ---------------------------------------------------------------------------
#  C. 守护
# ---------------------------------------------------------------------------
def check_guard(before_guard):
    print("\n== C. 链路守护")
    # 真周期：等一个间隔多一点，probes 必须前进（守护在 Agent 里真的跑着）
    print("  等 35 秒看守护是否在按周期体检…", flush=True)
    time.sleep(35)
    status = wifi("status", kind="status")
    guard = status.get("guard") or {}
    before = int(before_guard.get("probes") or 0)
    after = int(guard.get("probes") or 0)
    check("C1 守护在按周期体检（probes 前进）", after > before,
          "%d -> %d" % (before, after))
    check("C1 健康时不动手（repairs 没涨）",
          int(guard.get("repairs") or 0) == int(before_guard.get("repairs") or 0),
          "repairs=%s" % guard.get("repairs"))

    ip_before = status.get("ip")
    ack = wifi("reconnect", kind="ack", timeout=90.0)
    check("C2 wifi_control reconnect 回执 ok", ack.get("ok") is True,
          str(ack.get("message")))
    status2 = wifi("status", kind="status")
    check("C2 重连之后 IP 不变（安全动作）", status2.get("ip") == ip_before,
          "%r -> %r" % (ip_before, status2.get("ip")))


# ---------------------------------------------------------------------------
#  D. 真断链路（可选）
# ---------------------------------------------------------------------------
def outage_test():
    print("\n== D. 真断链路的主动恢复（--outage-test）")
    watchdog = ("for i in $(seq 1 12); do nmcli connection up %s >/dev/null 2>&1; "
                "sleep 20; done" % LIVE_PROFILE)
    subprocess.Popen(["setsid", "sh", "-c", "sleep 60; " + watchdog],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("  兜底已挂：60 秒后每 20 秒把 %s 拉回来一次（试 12 次）" % LIVE_PROFILE, flush=True)

    nmcli("connection", "modify", LIVE_PROFILE, "connection.autoconnect", "no")
    print("  已关掉 autoconnect（不让 NM 自己抢修），现在断开设备…", flush=True)
    started = time.time()
    subprocess.Popen(["setsid", "sh", "-c", "sleep 2; nmcli device disconnect wlan0"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 等链路自己回来（守护：连续 3 次 × 30s ≈ 90s 后动手）
    healthy = False
    for _ in range(40):                      # 最多 ~200 秒
        time.sleep(5)
        rc, out, _err = nmcli("-t", "-f", "GENERAL.STATE", "device", "show", "wlan0")
        if rc == 0 and "connected" in out:
            healthy = True
            break
    elapsed = time.time() - started
    check("D1 断掉之后链路自己回来了", healthy, "用了 %.0f 秒" % elapsed)

    nmcli("connection", "modify", LIVE_PROFILE, "connection.autoconnect", "yes")
    rc, out, _err = nmcli("-t", "-f", "connection.autoconnect", "connection", "show",
                          LIVE_PROFILE)
    check("D2 autoconnect 已恢复成 yes", "connection.autoconnect:yes" in out, out)

    log = "/home/kickpi/myproject/assitant/logs/agent.log"
    tail = ""
    if os.path.exists(log):
        with open(log, encoding="utf-8", errors="replace") as handle:
            tail = handle.read()[-20000:]
    check("D3 Agent 日志里有'链路不通'", "链路不通" in tail)
    check("D3 Agent 日志里有'开始主动重连'", "开始主动重连" in tail)
    check("D3 Agent 日志里有'重连成功'", "重连成功" in tail)


# ---------------------------------------------------------------------------
#  E. 不变量
# ---------------------------------------------------------------------------
def check_invariants(real_before):
    print("\n== E. 不变量")
    for name, digest in real_before.items():
        check("真实文件 %s 未变" % name, md5(os.path.join(REPO, name)) == digest,
              md5(os.path.join(REPO, name)))
    rc, out, _err = nmcli("-t", "-e", "yes", "-f", "NAME,TYPE,connection.autoconnect",
                          "connection", "show", LIVE_PROFILE)
    check("E 当前链路档案仍在且 autoconnect=yes",
          rc == 0 and "connection.autoconnect:yes" in out, out)
    rc = subprocess.run(["git", "status", "--porcelain"], cwd=REPO,
                        stdout=subprocess.PIPE, universal_newlines=True)
    check("板端 git status 干净", not rc.stdout.strip(), rc.stdout.strip()[:80])
    services_ok = True
    for unit in ("agent.service", "agent-gui.service"):
        proc = subprocess.run(["systemctl", "is-active", unit], stdout=subprocess.PIPE,
                              universal_newlines=True)
        if proc.stdout.strip() != "active":
            services_ok = False
    check("常驻服务仍 active", services_ok)
    check("哑档案没留下", TEST_PROFILE not in os.listdir(NM_DIR)
          if os.path.isdir(NM_DIR) else True)


def main():
    global SOCKET_PATH
    outage = "--outage-test" in sys.argv
    SOCKET_PATH = configured_socket_path()
    print("== T14-9 WiFi 门禁（真 Agent / 真 nmcli / 真 IPC）%s"
          % ("+ 真断链路" if outage else ""))
    print("  socket: %s（来自 config.yaml 的 ipc.socket_path）" % SOCKET_PATH)
    if not ensure_agent_socket(SOCKET_PATH):
        print("  ✘ Agent 的 socket 不在（%s）—— 先看 systemctl status agent.service"
              % SOCKET_PATH)
        return 1
    real_before = {name: md5(os.path.join(REPO, name)) for name in REAL_FILES
                   if os.path.exists(os.path.join(REPO, name))}
    for name, digest in real_before.items():
        print("  %-28s %s（跑完必须一样）" % (name, digest))
    rc, out, _err = nmcli("-t", "-e", "yes", "-f", "NAME", "connection", "show")
    print("  当前档案: %s" % (out.replace("\n", " "),))

    status, guard = check_readonly()
    check_actions()
    check_guard(guard)
    if outage:
        outage_test()
    check_invariants(real_before)

    print("\n== 结果: %s（%d 项通过%s）"
          % ("全部通过" if not _FAILED else "失败 %d 项" % len(_FAILED), len(_PASSED),
             (", 失败: " + "、".join(_FAILED)) if _FAILED else ""), flush=True)
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
