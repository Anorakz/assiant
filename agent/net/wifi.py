#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent/net/wifi.py — 本机 WiFi 链路（NetworkManager / nmcli 的唯一入口，T14-9）

板端现实（2026-09-27 实测）
    · 唯一可用的链路就是 **wlan0**（eth0/eth1 都是 `unavailable`），连的是 `Anorak_host`；
    · NetworkManager 1.22.10 + nmcli；连接档案在
      `/etc/NetworkManager/system-connections/Anorak_host.nmconnection`（**0600 root**），
      WPA2 的 psk 就在那里面；
    · 联网状态下**可以扫**（`nmcli device wifi list --rescan yes` 有结果）；
    · 网关 192.168.137.1，`ping -c1 -W1` 可用；`nmcli networking connectivity check` 也能用。

为什么要有这个文件
    上次断网时板端**一条日志都没有** —— NetworkManager 不会替我们做"业务级"的判断
    （它只看链路层与自己的 connectivity 检查），也不会在"网卡连着但网不通"时主动重连。
    所以这里做两件事：
      1) **读写 WiFi 状态**（状态 / 扫描 / 连接 / 忘记 / 自动连接）给 GUI 与 CLI 用；
      2) **健康检查 + 主动重连**（`LinkGuard`）：连续 N 次探不到网关就自己修，
         每一步都写日志 —— "断网了但不知道断了多久" 这件事从此有据可查。

三条硬规矩
    · **只用 nmcli**（不 import dbus / pyroute2）：板端就装了它，行为可预测、输出可机读；
    · **密码不进 argv、不进 config.yaml、不进日志**：
      - 已有档案 → `nmcli --ask connection up <name>`，密码走 **stdin**；
      - 新档案 → 我们自己写 NM 的 keyfile（`0600`，就在 NM 本来要放的位置）再 `con reload && con up`。
      `ps` 里永远不会出现密码；config.yaml 里只可能出现"记住哪个 SSID / 要不要自动连"这种非秘密项。
    · **破坏性操作只碰调用方点名的那个 SSID**：`forget()` 绝不"顺手"删别的档案
      （板端只有这一条链路，删错就等于把板子锁在门外）。

机读输出怎么读
    统一走 `-t -e yes -f …`（terse + 转义），`\\:` 表示字段里的冒号、`\\\\` 表示反斜杠 ——
    `_split_terse()` / `_unescape()` 负责还原。SSID 里有 `&`、空格、中文都很常见。

不做什么（刻意的）
    · 不做 WPA-Enterprise（802.1X 用户名/证书）—— 家里/实验室用不到，真需要再加；
    · 不做热点（AP）模式、不碰 `eth0/eth1`；
    · 不改 `powersave`（只报告；真怀疑省电影响重连再说）。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
import uuid as uuid_mod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "AccessPoint",
    "DEFAULT_IFNAME",
    "DEFAULT_PROBE_FAILURES",
    "DEFAULT_PROBE_INTERVAL_S",
    "DEFAULT_RECONNECT_WAIT_S",
    "LinkGuard",
    "LinkHealth",
    "Wifi",
    "WifiError",
    "WifiNotFound",
    "WifiTimeout",
    "build_connection_keyfile",
    "parse_status_fields",
    "parse_wifi_list",
    "unescape_terse",
]

#: 默认无线接口（板端实测只有 wlan0）
DEFAULT_IFNAME = "wlan0"
#: 健康检查：连续失败几次才动手（你 2026-09-27 定的 3）
DEFAULT_PROBE_FAILURES = 3
#: 两次探测之间隔多久（秒）
DEFAULT_PROBE_INTERVAL_S = 30.0
#: 触发重连后等多久再复查（秒）
DEFAULT_RECONNECT_WAIT_S = 15.0
#: nmcli / ping 的单次超时（秒）
DEFAULT_TIMEOUT_S = 25.0

_ENV_NMCLI = "AGENT_NMCLI"                  # 单测注入假 nmcli 用
_ENV_NM_DIR = "AGENT_NM_CONF_DIR"           # 单测不让它真写 /etc
DEFAULT_NM_DIR = "/etc/NetworkManager/system-connections"

#: nmcli 档案里的密钥字段（日志里一律隐去）
_SECRET_KEYS = ("psk", "password", "802-11-wireless-security.psk")


class WifiError(Exception):
    """WiFi 操作失败（消息是要给人看的，不要再包一层看不懂的话）。"""


class WifiNotFound(WifiError):
    """找不到对应的 SSID / 连接档案。"""


class WifiTimeout(WifiError):
    """nmcli 超时（扫描最常见的失败方式）。"""


# ---------------------------------------------------------------------------
#  terse 输出解析（纯函数，无网络可测）
# ---------------------------------------------------------------------------
def unescape_terse(value: str) -> str:
    """还原 nmcli `-t -e yes` 的转义：`\\:` → `:`、`\\\\` → `\\`。"""
    out: List[str] = []
    escaped = False
    for char in value:
        if escaped:
            out.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        else:
            out.append(char)
    if escaped:                      # 结尾一个孤立的反斜杠：原样留着
        out.append("\\")
    return "".join(out)


def _split_terse(line: str) -> List[str]:
    """按**未转义**的冒号切一行。"""
    fields: List[str] = []
    current: List[str] = []
    escaped = False
    for char in line:
        if escaped:
            current.append("\\")
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append(unescape_terse("".join(current)))
            current = []
        else:
            current.append(char)
    fields.append(unescape_terse("".join(current)))
    return fields


def parse_wifi_list(output: str) -> List["AccessPoint"]:
    """解析 `nmcli -t -e yes -f SSID,SIGNAL,SECURITY,IN-USE device wifi list`。

    同一个 SSID 可能有多个 AP（板端实测 `SZU_CTC&CMCC` 一次扫到 4 个）——
    这里**按 SSID 去重、取信号最强**的那条，并保留"是否已连"。
    隐藏 SSID（空名字）跳过：界面上没法点，也没有可用信息。
    """
    best: Dict[str, AccessPoint] = {}
    for raw in output.splitlines():
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        fields = _split_terse(line)
        if len(fields) < 4:
            continue
        ssid, signal_text, security, in_use = fields[0], fields[1], fields[2], fields[3]
        if not ssid:
            continue
        try:
            signal = int(signal_text)
        except ValueError:
            signal = 0
        point = AccessPoint(ssid=ssid, signal=signal, security=security.strip(),
                            in_use=in_use.strip() == "*")
        previous = best.get(ssid)
        if previous is None or point.signal > previous.signal:
            best[ssid] = point
        elif point.in_use:
            previous.in_use = True
    return sorted(best.values(), key=lambda p: (not p.in_use, -p.signal, p.ssid))


def parse_status_fields(output: str) -> Dict[str, str]:
    """把 `nmcli -t -f A.B,C.D device show X` 的 `A.B:value` 行读成字典。"""
    out: Dict[str, str] = {}
    for raw in output.splitlines():
        line = raw.rstrip("\n")
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = unescape_terse(value)
    return out


def state_text(raw: str) -> str:
    """`GENERAL.STATE` 的真实样子是 `100 (connected)` —— 要的是括号里那个词。

    板端实测（NM 1.22）：`GENERAL.STATE:100 (connected)` / `30 (disconnected)` /
    `20 (unavailable)`。直接 `split()` 会拿到数字 "100"，`startswith("connected")` 就永远假。
    """
    if not raw:
        return "unknown"
    match = re.search(r"\(([^)]+)\)", raw)
    if match:
        return match.group(1).strip()
    return raw.strip().split(" ")[0] or "unknown"


# ---------------------------------------------------------------------------
#  数据结构
# ---------------------------------------------------------------------------
@dataclass
class AccessPoint:
    ssid: str
    signal: int = 0
    security: str = ""
    in_use: bool = False

    @property
    def secured(self) -> bool:
        return bool(self.security.strip())

    def to_dict(self) -> Dict[str, object]:
        return {"ssid": self.ssid, "signal": self.signal, "security": self.security,
                "secured": self.secured, "in_use": self.in_use}


@dataclass
class WifiStatus:
    """一次状态快照（`Wifi.status()` 的返回）。"""

    device: str = DEFAULT_IFNAME
    state: str = "unknown"                   # connected / disconnected / unavailable…
    ssid: str = ""
    signal: int = 0
    security: str = ""
    ip: str = ""
    gateway: str = ""
    dns: List[str] = field(default_factory=list)
    profile: str = ""                        # 当前生效的连接档案名
    autoconnect: bool = False
    connectivity: str = ""                   # nmcli networking connectivity check 的结果

    @property
    def connected(self) -> bool:
        return self.state.startswith("connected") and bool(self.ip)

    def to_dict(self) -> Dict[str, object]:
        return {
            "device": self.device, "state": self.state, "ssid": self.ssid,
            "signal": self.signal, "security": self.security, "ip": self.ip,
            "gateway": self.gateway, "dns": list(self.dns), "profile": self.profile,
            "autoconnect": self.autoconnect, "connectivity": self.connectivity,
            "connected": self.connected,
        }


@dataclass
class LinkHealth:
    """一次链路体检的结果。"""

    has_ip: bool = False
    ip: str = ""
    gateway: str = ""
    gateway_reachable: bool = False
    connectivity: str = ""
    device_state: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        """判据：**有 IP 且探得到网关**。

        为什么不用 `nmcli networking connectivity check`：那是 NM 去访问**外网**探针，
        家里/实验室的路由器不出网时它会报 limited，但链路其实是好的（我们要救的是链路）。
        它只作为参考信息一起带出来。
        """
        return self.has_ip and self.gateway_reachable

    def to_dict(self) -> Dict[str, object]:
        return {
            "ok": self.ok, "has_ip": self.has_ip, "ip": self.ip, "gateway": self.gateway,
            "gateway_reachable": self.gateway_reachable, "connectivity": self.connectivity,
            "device_state": self.device_state, "error": self.error,
        }


# ---------------------------------------------------------------------------
#  keyfile（新档案由我们自己写 —— 密码不进 argv）
# ---------------------------------------------------------------------------
def build_connection_keyfile(ssid: str, password: Optional[str], ifname: str = DEFAULT_IFNAME,
                             autoconnect: bool = True, uuid: Optional[str] = None) -> str:
    """生成一份 NetworkManager 的 wifi keyfile（`nmconnection`）。

    @param password 空/None = 开放网络（不写 `[wifi-security]` 段）
    @note 值里的 `\\` 与 `;` 要转义：keyfile 用 `;` 当列表分隔符。
    """
    def escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace(";", "\\;")

    lines = [
        "[connection]",
        "id=%s" % escape(ssid),
        "uuid=%s" % (uuid or str(uuid_mod.uuid4())),
        "type=wifi",
        "interface-name=%s" % ifname,
        "autoconnect=%s" % ("true" if autoconnect else "false"),
        "",
        "[wifi]",
        "mode=infrastructure",
        "ssid=%s" % escape(ssid),
        "",
    ]
    if password:
        lines += [
            "[wifi-security]",
            "key-mgmt=wpa-psk",
            "psk=%s" % escape(password),
            "",
        ]
    lines += [
        "[ipv4]",
        "method=auto",
        "",
        "[ipv6]",
        "method=auto",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
#  主体
# ---------------------------------------------------------------------------
class Wifi:
    """nmcli 的薄封装。所有方法都可能抛 `WifiError`（消息可直接给用户看）。"""

    def __init__(self, ifname: str = DEFAULT_IFNAME,
                 runner: Optional[Callable[..., Tuple[int, str, str]]] = None,
                 timeout: float = DEFAULT_TIMEOUT_S,
                 nm_dir: Optional[Path] = None,
                 log: Optional[logging.Logger] = None) -> None:
        self.ifname = ifname
        self.timeout = timeout
        self.log = log
        self.nmcli = os.environ.get(_ENV_NMCLI, "nmcli")
        self.nm_dir = Path(nm_dir or os.environ.get(_ENV_NM_DIR, DEFAULT_NM_DIR))
        self._runner = runner or self._run_subprocess

    # -- 底层 ---------------------------------------------------------------
    @staticmethod
    def _run_subprocess(argv: Sequence[str], stdin: Optional[str] = None,
                        timeout: float = DEFAULT_TIMEOUT_S) -> Tuple[int, str, str]:
        try:
            proc = subprocess.run(
                list(argv), input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                universal_newlines=True, timeout=timeout,
            )
        except FileNotFoundError:
            raise WifiError("找不到 %s（板端没装 NetworkManager？）" % argv[0])
        except subprocess.TimeoutExpired:
            raise WifiTimeout("%s 超时（%ss）" % (" ".join(argv[:3]), timeout))
        return proc.returncode, proc.stdout or "", proc.stderr or ""

    def _run(self, argv: Sequence[str], stdin: Optional[str] = None,
             timeout: Optional[float] = None, check: bool = True) -> Tuple[int, str, str]:
        rc, out, err = self._runner(list(argv), stdin,
                                    timeout if timeout is not None else self.timeout)
        if check and rc != 0:
            detail = (err or out or "").strip().splitlines()
            message = detail[-1] if detail else "退出码 %d" % rc
            if self.log is not None:
                self.log.warning("wifi: %s 失败: %s", " ".join(argv[:3]), message)
            raise WifiError("%s" % message)
        return rc, out, err

    def _nmcli(self, args: Sequence[str], stdin: Optional[str] = None,
               timeout: Optional[float] = None, check: bool = True) -> str:
        """跑一条 nmcli 并返回 stdout（统一 terse 风格由调用方给 `-t`）。"""
        return self._run([self.nmcli] + list(args), stdin=stdin, timeout=timeout,
                         check=check)[1]

    # -- 只读 ---------------------------------------------------------------
    def available(self) -> bool:
        """板端能不能用（没有 nmcli / 没有无线设备时优雅退化）。"""
        return shutil.which(self.nmcli) is not None

    def known_profiles(self) -> Dict[str, str]:
        """`{档案名: ssid}`（只列 wifi 类型）。

        ⚠ 不能一条命令搞定：`nmcli connection show` 的**列表**形式只认连接级字段
        （板端实测：带 `802-11-wireless.ssid` 会报 `Error: invalid field`），
        所以先列 `NAME,TYPE`，再逐个问 wifi 档案的 ssid。
        """
        out = self._nmcli(["-t", "-e", "yes", "-f", "NAME,TYPE", "connection", "show"],
                          check=False)
        names: List[str] = []
        for raw in out.splitlines():
            fields = _split_terse(raw)
            if len(fields) >= 2 and fields[1] == "802-11-wireless":
                names.append(fields[0])
        profiles: Dict[str, str] = {}
        for name in names:
            detail = self._nmcli(["-t", "-e", "yes", "-f", "802-11-wireless.ssid",
                                  "connection", "show", name], check=False)
            ssid = parse_status_fields(detail).get("802-11-wireless.ssid", "")
            profiles[name] = ssid
        return profiles

    def status(self) -> WifiStatus:
        """当前状态快照。设备不存在时返回 `state=unavailable` 而不是抛异常。"""
        status = WifiStatus(device=self.ifname)
        rc, out, _err = self._run(
            [self.nmcli, "-t", "-e", "yes", "-f",
             "GENERAL.STATE,GENERAL.CONNECTION,IP4.ADDRESS,IP4.GATEWAY,IP4.DNS",
             "device", "show", self.ifname], check=False)
        if rc != 0:
            status.state = "unavailable"
            return status
        fields = parse_status_fields(out)
        status.state = state_text(fields.get("GENERAL.STATE", ""))
        status.profile = fields.get("GENERAL.CONNECTION", "")
        # ⚠ nmcli 的键带下标（`IP4.ADDRESS[1]` / `IP4.DNS[1]`）—— 不能按原名取，
        #    板端实测就是这么打的（见 tests/test_wifi.py 里那份实测输出）。
        address = ""
        gateway = ""
        dns: List[str] = []
        for key, value in fields.items():
            if key.startswith("IP4.ADDRESS") and not address:
                address = value
            elif key.startswith("IP4.GATEWAY") and not gateway:
                gateway = value
            elif key.startswith("IP4.DNS") and value:
                dns.append(value)
        status.ip = address.split("/")[0] if address else ""
        status.gateway = gateway
        status.dns = dns
        # 无线细节（SSID / 信号 / 安全 / 自动连接）。`-f AP` 展开成 AP[1].SSID / SIGNAL /
        # SECURITY / IN-USE（板端实测的字段名就是这些，`AP*` 反而不认）。
        detail = self._nmcli(["-t", "-e", "yes", "-f", "GENERAL.CONNECTION,AP",
                              "device", "show", self.ifname], check=False)
        for raw in detail.splitlines():
            key, _, value = raw.partition(":")
            key = key.strip()
            if key == "AP[1].SSID":
                status.ssid = unescape_terse(value)
            elif key == "AP[1].SIGNAL":
                try:
                    status.signal = int(value.strip())
                except ValueError:
                    status.signal = 0
            elif key == "AP[1].SECURITY":
                status.security = unescape_terse(value).strip()
        if status.profile:
            profile = self._nmcli(["-t", "-e", "yes", "-f",
                                   "connection.autoconnect,802-11-wireless.ssid,"
                                   "802-11-wireless-security.key-mgmt",
                                   "connection", "show", status.profile], check=False)
            prof_fields = parse_status_fields(profile)
            if not status.ssid:
                status.ssid = prof_fields.get("802-11-wireless.ssid", "")
            if not status.security:
                status.security = prof_fields.get("802-11-wireless-security.key-mgmt", "")
            status.autoconnect = prof_fields.get("connection.autoconnect", "no") == "yes"
        # NM 自己的在线判断（只作参考）
        rc, out, _err = self._run([self.nmcli, "networking", "connectivity", "check"],
                                  timeout=8.0, check=False)
        if rc == 0:
            status.connectivity = out.strip().splitlines()[-1] if out.strip() else ""
        return status

    def scan(self, rescan: bool = True) -> List[AccessPoint]:
        """扫一圈。失败时抛 `WifiError`（界面要把原因显示出来，别静默空列表）。"""
        args = ["-t", "-e", "yes", "-f", "SSID,SIGNAL,SECURITY,IN-USE",
                "device", "wifi", "list"]
        if rescan:
            args.append("--rescan")
            args.append("yes")
        args += ["ifname", self.ifname]
        out = self._nmcli(args, timeout=max(self.timeout, 30.0))
        return parse_wifi_list(out)

    # -- 写 ---------------------------------------------------------------
    def connect(self, ssid: str, password: Optional[str] = None,
                autoconnect: bool = True) -> Dict[str, object]:
        """连到 `ssid`。

        @param password None/"" = 开放网络（也要能连）
        @return `{ok, ssid, profile, method, autoconnect}`；失败抛 `WifiError`
        @note 密码走 **stdin**（已有档案）或 **0600 keyfile**（新档案），永不进 argv。
        """
        if not ssid:
            raise WifiError("SSID 不能为空")
        profiles = self.known_profiles()
        existing = None
        for name, ssid_of in profiles.items():
            if ssid_of == ssid:
                existing = name
                break

        if existing is not None:
            if self.log is not None:
                self.log.info("wifi: 用已有档案 %r 连 %r", existing, ssid)
            if password:
                # `--ask` 会从 stdin 读密码 —— 这是"密码不进 argv"的关键
                self._nmcli(["--ask", "connection", "up", existing], stdin=password + "\n",
                            timeout=max(self.timeout, 60.0))
            else:
                self._nmcli(["connection", "up", existing], timeout=max(self.timeout, 60.0))
            profile = existing
            method = "existing-profile"
            if not autoconnect:
                # 已有档案：keyfile 不归我们写，只能 modify
                self.set_autoconnect(profile, False)
        else:
            if self.log is not None:
                self.log.info("wifi: 为 %r 新建档案（keyfile 0600，密码不进 argv）", ssid)
            # 新档案的 autoconnect 直接写在 keyfile 里（不必再 modify —— 那还要依赖
            # `known_profiles()` 已经"看见"这个刚落盘的档案）
            profile = self._write_keyfile(ssid, password, autoconnect)
            self._nmcli(["connection", "reload"], timeout=max(self.timeout, 30.0))
            self._nmcli(["connection", "up", profile], timeout=max(self.timeout, 60.0))
            method = "keyfile"
        state = self.status()
        if not state.connected:
            raise WifiError("档案 %r 起不来（设备状态 %s）" % (profile, state.state or "未知"))
        return {"ok": True, "ssid": ssid, "profile": profile, "method": method,
                "autoconnect": autoconnect, "ip": state.ip}

    def _write_keyfile(self, ssid: str, password: Optional[str],
                       autoconnect: bool) -> str:
        """把新档案写进 NM 的目录（**0600 root**）—— 密码只落在这里。"""
        try:
            self.nm_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WifiError("建不出 %s: %s" % (self.nm_dir, exc))
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", ssid) or "wifi"
        path = self.nm_dir / ("%s.nmconnection" % name)
        if path.exists():
            raise WifiError("档案已存在: %s（先忘记它或换一个 SSID）" % path.name)
        text = build_connection_keyfile(ssid, password, self.ifname, autoconnect)
        try:
            path.write_text(text, encoding="utf-8")
            os.chmod(path, 0o600)
        except OSError as exc:
            raise WifiError("写 %s 失败: %s" % (path, exc))
        return name

    def forget(self, ssid_or_profile: str) -> Dict[str, object]:
        """删掉某个档案（**只删点名的这个**）。

        ⚠ 板端只有 wlan0 这一条链路：忘记正在用的那一个 = 板子马上离线。
        调用方（GUI）有确认弹窗；这里只多做一步——如果是当前生效的档案，日志里写明。
        """
        if not ssid_or_profile:
            raise WifiError("要忘记哪个 SSID / 档案？")
        profiles = self.known_profiles()
        target = ssid_or_profile if ssid_or_profile in profiles else None
        if target is None:
            for name, ssid_of in profiles.items():
                if ssid_of == ssid_or_profile:
                    target = name
                    break
        if target is None:
            raise WifiNotFound("没有 %r 的连接档案" % ssid_or_profile)
        active = self.status()
        if active.profile == target and self.log is not None:
            self.log.warning("wifi: 正在使用的档案 %r 被删除 —— 链路会断（除非有别的档案能连）",
                             target)
        self._nmcli(["connection", "delete", target], timeout=max(self.timeout, 30.0))
        return {"ok": True, "profile": target, "ssid": profiles.get(target, ""),
                "was_active": active.profile == target}

    def set_autoconnect(self, ssid_or_profile: str, on: bool) -> Dict[str, object]:
        """打开/关闭"记住并自动连接"。"""
        profiles = self.known_profiles()
        target = ssid_or_profile if ssid_or_profile in profiles else None
        if target is None:
            for name, ssid_of in profiles.items():
                if ssid_of == ssid_or_profile:
                    target = name
                    break
        if target is None:
            raise WifiNotFound("没有 %r 的连接档案" % ssid_or_profile)
        self._nmcli(["connection", "modify", target, "connection.autoconnect",
                     "yes" if on else "no"], timeout=max(self.timeout, 30.0))
        return {"ok": True, "profile": target, "autoconnect": bool(on)}

    # -- 健康检查 -----------------------------------------------------------
    def health(self, ping_timeout_s: float = 1.0) -> LinkHealth:
        """体检：有没有 IP、探不探得到网关。**不抛异常**（失败也是一种结果）。"""
        health = LinkHealth()
        try:
            state = self.status()
        except WifiError as exc:
            health.error = str(exc)
            return health
        health.has_ip = bool(state.ip)
        health.ip = state.ip
        health.device_state = state.state
        health.connectivity = state.connectivity
        health.gateway = state.gateway
        if not health.gateway:
            rc, out, _err = self._run(["ip", "-4", "route", "show", "default"],
                                      timeout=5.0, check=False)
            if rc == 0 and out.strip():
                parts = out.split()
                if "via" in parts:
                    health.gateway = parts[parts.index("via") + 1]
        if health.gateway:
            rc, _out, _err = self._run(
                ["ping", "-c", "1", "-W", str(int(max(1, round(ping_timeout_s)))),
                 health.gateway], timeout=ping_timeout_s + 4.0, check=False)
            health.gateway_reachable = rc == 0
        return health

    def reconnect(self) -> Dict[str, object]:
        """主动修一次链路：先把当前档案重新 up，不行再断开/连上设备。"""
        steps: List[str] = []
        active = self.status()
        if active.profile:
            steps.append("con-up:%s" % active.profile)
            self._nmcli(["connection", "up", active.profile], timeout=max(self.timeout, 60.0),
                        check=False)
        else:
            steps.append("device-connect")
            self._nmcli(["device", "connect", self.ifname], timeout=max(self.timeout, 60.0),
                        check=False)
        return {"ok": True, "steps": steps}

    def cycle_device(self) -> Dict[str, object]:
        """更狠一招：断开设备再连上（会真的掉线几秒）。"""
        self._nmcli(["device", "disconnect", self.ifname], timeout=max(self.timeout, 30.0),
                    check=False)
        time.sleep(2.0)
        self._nmcli(["device", "connect", self.ifname], timeout=max(self.timeout, 60.0),
                    check=False)
        return {"ok": True, "steps": ["device-disconnect", "device-connect"]}


# ---------------------------------------------------------------------------
#  链路守护（连续 N 次探不到网关 → 主动重连）
# ---------------------------------------------------------------------------
class LinkGuard:
    """周期体检 + 主动重连（你 2026-09-27 定的力度：**连续 3 次**探不到网关才动手）。

    为什么不做成"一探不到就重连"：偶发丢包 / 路由器瞬断很常见，立刻重连会打乱本来
    正在恢复的链路（`nmcli con up` 自己也要几秒）。三次约 90 秒 —— 真断网时这个代价可以接受。

    每一步都写日志（`logger.info/warning`）：上次断网板端一条日志都没有，这是要解决的核心问题。
    """

    def __init__(self, wifi: Wifi, failures: int = DEFAULT_PROBE_FAILURES,
                 interval_s: float = DEFAULT_PROBE_INTERVAL_S,
                 reconnect_wait_s: float = DEFAULT_RECONNECT_WAIT_S,
                 log: Optional[logging.Logger] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 max_repairs_per_outage: int = 2) -> None:
        self.wifi = wifi
        self.failures = max(1, int(failures))
        self.interval_s = float(interval_s)
        self.reconnect_wait_s = float(reconnect_wait_s)
        self.log = log
        self._sleep = sleep
        self.max_repairs_per_outage = max(1, int(max_repairs_per_outage))
        self.consecutive_failures = 0
        self.repairs = 0                      # 累计修过几次（诊断用）
        self.probes = 0
        self.last: Optional[LinkHealth] = None

    #: 下次该等多久（修过就退避，别一直猛敲）
    @property
    def next_interval(self) -> float:
        if self.consecutive_failures < self.failures:
            return self.interval_s
        digs = min(self.repairs, 3)
        return self.interval_s * (2 ** digs)

    def probe(self) -> LinkHealth:
        """一次体检；该修就修。返回这次的结果（修复后是复查的结果）。"""
        self.probes += 1
        health = self.wifi.health()
        self.last = health
        if health.ok:
            if self.consecutive_failures:
                if self.log is not None:
                    self.log.info("wifi: 链路恢复（连续 %d 次失败后）",
                                  self.consecutive_failures)
            self.consecutive_failures = 0
            return health

        self.consecutive_failures += 1
        reason = "没有 IP" if not health.has_ip else "探不到网关 %s" % (health.gateway or "?")
        if self.log is not None:
            self.log.warning("wifi: 链路不通（%s）— 第 %d/%d 次",
                             reason, self.consecutive_failures, self.failures)
        if self.consecutive_failures < self.failures:
            return health

        if self.repairs >= self.max_repairs_per_outage + 1 and not health.ok:
            # 反复修也修不好：继续记日志，但别再刷屏
            if self.log is not None:
                self.log.error("wifi: 已经修了 %d 次仍然不通（%s）—— 需要人工看看",
                               self.repairs, reason)
            return health

        health2 = self._repair(reason)
        self.last = health2
        return health2

    def _repair(self, reason: str) -> LinkHealth:
        self.repairs += 1
        if self.log is not None:
            self.log.warning("wifi: 开始主动重连（原因：%s，第 %d 次）", reason, self.repairs)
        steps: List[str] = []
        try:
            steps += list(self.wifi.reconnect().get("steps", []))
        except WifiError as exc:
            steps.append("reconnect 失败: %s" % exc)
        self._sleep(self.reconnect_wait_s)
        health = self.wifi.health()
        if health.ok:
            self.consecutive_failures = 0
            if self.log is not None:
                self.log.info("wifi: 重连成功（步骤 %s → IP %s）", ",".join(steps), health.ip)
            return health

        if self.log is not None:
            self.log.warning("wifi: 重新 up 没救回来，改用断开/连上设备")
        try:
            steps += list(self.wifi.cycle_device().get("steps", []))
        except WifiError as exc:
            steps.append("cycle 失败: %s" % exc)
        self._sleep(self.reconnect_wait_s)
        health = self.wifi.health()
        if health.ok:
            self.consecutive_failures = 0
            if self.log is not None:
                self.log.info("wifi: 断开/连上之后恢复了（步骤 %s → IP %s）",
                              ",".join(steps), health.ip)
        elif self.log is not None:
            self.log.error("wifi: 两种办法都没救回来（步骤 %s）", ",".join(steps))
        return health

    def status_dict(self) -> Dict[str, object]:
        return {
            "probes": self.probes,
            "consecutive_failures": self.consecutive_failures,
            "repairs": self.repairs,
            "threshold": self.failures,
            "interval_s": self.interval_s,
            "last": self.last.to_dict() if self.last is not None else None,
        }
