#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_wifi.py — WiFi 链路模块守卫（T14-9）

跑法:
    python tests/test_wifi.py

⚠ **这个文件绝不碰真网络**：所有 nmcli / ip / ping 都是假的（`FakeNmcli`），
  连 keyfile 也只写进临时目录 —— 板端只有 wlan0 这一条链路，测试里手滑一下
  就可能把板子锁在门外。

守什么
  1) **解析**（纯函数）：terse 转义、同 SSID 多 AP 去重取最强、隐藏 SSID 跳过、排序；
  2) **只读快照**：设备缺失时优雅退化（state=unavailable，不抛）；
  3) **密码的边界**（最重要）：
     · 已有档案 → `nmcli --ask connection up`，密码走 **stdin**；
     · 新档案 → 我们写的 **0600 keyfile**；
     · **任何 argv 里都不许出现密码**（对应 `ps` 泄露）；
  4) **forget 只删点名的那个**：不认识的 SSID 要如实报错，别的档案一个都不许动
     （板端就一条链路，删错 = 板子失联）；
  5) **健康判定**：有 IP 且探得到网关才算 OK；网关从 status 拿，拿不到退回 `ip route`；
  6) **LinkGuard**：连续 3 次才动手、修好后计数归零、修不好会退避（`next_interval` 翻倍）、
     每一步都留日志（上次断网板端一条日志都没有，就是要解决这个）。
"""

import logging
import unittest.mock as mock
import os
import sys
import tempfile
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.net.wifi import (  # noqa: E402
    AccessPoint,
    LinkGuard,
    Wifi,
    WifiError,
    WifiNotFound,
    build_connection_keyfile,
    parse_status_fields,
    parse_wifi_list,
    unescape_terse,
)

#: 板端实测的那种输出（含同名多 AP、开放网络、带 `&` 的 SSID）
LIST_OUTPUT = "\n".join([
    "Anorak_host:100:WPA2:*",
    "806:50:WPA1 WPA2: ",
    "SZU_CTC&CMCC:49:: ",
    "LAPTOP-OJTIUSOL 2880:47:WPA2: ",
    "SZU_CTC&CMCC:47:: ",
    "404 not found:22:WPA2: ",
    "SZU_CTC&CMCC:20:: ",
    ":0:WPA2: ",                       # 隐藏 SSID：跳过
    "带\\:冒号的网:33:WPA2: ",          # terse 转义：SSID 里真的有个冒号
])


def device_output(state="connected", connection="Anorak_host", ip="192.168.137.30/24",
                  gateway="192.168.137.1", dns="192.168.137.1"):
    """按**板端实测的原样**造（注意状态是 `100 (connected)` 这种带数字的形式）。"""
    codes = {"connected": "100", "disconnected": "30", "unavailable": "20"}
    return "\n".join([
        "GENERAL.DEVICE:wlan0",
        "GENERAL.STATE:%s (%s)" % (codes.get(state, "0"), state),
        "GENERAL.CONNECTION:%s" % connection,
        "IP4.ADDRESS[1]:%s" % ip if ip else "IP4.ADDRESS[1]:",
        "IP4.GATEWAY:%s" % gateway if gateway else "IP4.GATEWAY:",
        "IP4.DNS[1]:%s" % dns if dns else "IP4.DNS[1]:",
    ])


def ap_output(ssid="Anorak_host", signal=100, security="WPA2"):
    """`nmcli -f AP device show` 的真实样子（板端实测）。"""
    return "\n".join([
        "AP[1].IN-USE:*",
        "AP[1].BSSID:E6:C7:67:48:44:F5",
        "AP[1].SSID:%s" % ssid,
        "AP[1].MODE:Infra",
        "AP[1].CHAN:149",
        "AP[1].RATE:270 Mbit/s",
        "AP[1].SIGNAL:%d" % signal,
        "AP[1].BARS:▂▄▆█",
    ])


def profile_output(ssid="Anorak_host", autoconnect="yes", key_mgmt="wpa-psk"):
    return "\n".join([
        "connection.autoconnect:%s" % autoconnect,
        "802-11-wireless.ssid:%s" % ssid,
        "802-11-wireless-security.key-mgmt:%s" % key_mgmt,
    ])


def connections_output(*rows):
    """`nmcli -t -f NAME,TYPE connection show`（**列表形式只有连接级字段**，板端实测）。"""
    return "\n".join(rows) if rows else "Anorak_host:802-11-wireless"


class FakeNmcli:
    """假的 nmcli + ip + ping。记录每一次调用（argv / stdin）供断言。"""

    def __init__(self, connected=True, device=None, connections=None, list_out=LIST_OUTPUT,
                 ping_rc=0, up_rc=0, delete_rc=0, modify_rc=0, reload_rc=0,
                 connectivity="full", gateway="192.168.137.1", heal_on_up=False):
        self.calls = []
        self.connected = connected
        self.gateway = gateway
        self.ping_rc = ping_rc
        self.up_rc = up_rc
        self.delete_rc = delete_rc
        self.modify_rc = modify_rc
        self.reload_rc = reload_rc
        self.connectivity = connectivity
        #: True = 一旦 `connection up` 被调用，链路就"通了"（模拟重连成功）
        self.heal_on_up = heal_on_up
        self._device = device
        self._connections = connections if connections is not None else \
            connections_output()
        self._list = list_out

    # -- 断言辅助 -----------------------------------------------------------
    def argv_strings(self):
        return [" ".join(call["argv"]) for call in self.calls]

    def stdin_strings(self):
        return [call["stdin"] for call in self.calls if call["stdin"]]

    def called(self, *needles):
        return any(all(n in argv for n in needles) for argv in self.argv_strings())

    # -- 实现 ---------------------------------------------------------------
    def __call__(self, argv, stdin=None, timeout=None):
        self.calls.append({"argv": list(argv), "stdin": stdin, "timeout": timeout})
        program = argv[0]
        if program.endswith("nmcli"):
            return self._nmcli(argv)
        if program == "ping":
            return (self.ping_rc, "", "" if self.ping_rc == 0 else "unreachable")
        if program == "ip":
            if self.gateway:
                return (0, "default via %s dev wlan0 proto dhcp metric 600\n" % self.gateway, "")
            return (0, "", "")
        return (127, "", "unknown program")

    def _nmcli(self, argv):
        if "device" in argv and "wifi" in argv and "list" in argv:
            return (0, self._list, "")
        if "device" in argv and "show" in argv:
            if self._device is not None:
                return (0, self._device, "")
            if not self.connected:
                return (0, device_output(state="disconnected", connection="", ip="",
                                        gateway="", dns="") + "\n" + ap_output(), "")
            return (0, device_output(gateway=self.gateway) + "\n" + ap_output(), "")
        if "connection" in argv and "show" in argv:
            idx = argv.index("show")
            if idx + 1 < len(argv):
                # 逐个档案问细节（`connection show <name>`）
                return (0, profile_output(), "")
            return (0, self._connections, "")
        if "networking" in argv and "connectivity" in argv:
            return (0, self.connectivity + "\n", "")
        if "connection" in argv and "up" in argv:
            if self.up_rc != 0:
                return (self.up_rc, "", "Error: Connection activation failed.")
            self.connected = True
            if self.heal_on_up:
                self.ping_rc = 0          # 重连真的把链路救回来了
            return (0, "Device 'wlan0' successfully activated.\n", "")
        if "connection" in argv and "delete" in argv:
            if self.delete_rc != 0:
                return (self.delete_rc, "", "Error: unknown connection.")
            name = argv[-1]
            self._connections = "\n".join(
                row for row in self._connections.splitlines()
                if not row.startswith(name + ":"))
            return (0, "Connection '%s' deleted.\n" % name, "")
        if "connection" in argv and "modify" in argv:
            return (self.modify_rc, "", "" if self.modify_rc == 0 else "Error: bad value")
        if "connection" in argv and "reload" in argv:
            return (self.reload_rc, "", "")
        return (0, "", "")


def make_wifi(tmpdir, fake, ifname="wlan0"):
    return Wifi(ifname=ifname, runner=fake, nm_dir=Path(tmpdir))


class TestParsing(unittest.TestCase):
    def test_unescape(self):
        self.assertEqual(unescape_terse("a\\:b"), "a:b")
        self.assertEqual(unescape_terse("a\\\\b"), "a\\b")
        self.assertEqual(unescape_terse("plain"), "plain")
        self.assertEqual(unescape_terse("trailing\\"), "trailing\\")

    def test_wifi_list_dedupes_and_sorts(self):
        points = parse_wifi_list(LIST_OUTPUT)
        names = [p.ssid for p in points]
        self.assertEqual(names[0], "Anorak_host", "已连的排最前")
        self.assertTrue(points[0].in_use)
        self.assertNotIn("", names, "隐藏 SSID 要跳过")
        self.assertIn("带:冒号的网", names, "terse 转义要还原")
        # 同名多 AP 只留一条，取信号最强的（49，不是 47/20）
        szu = [p for p in points if p.ssid == "SZU_CTC&CMCC"]
        self.assertEqual(len(szu), 1)
        self.assertEqual(szu[0].signal, 49)
        # 其余按信号降序
        rest = [p.signal for p in points if not p.in_use]
        self.assertEqual(rest, sorted(rest, reverse=True))

    def test_wifi_list_marks_in_use_on_the_strongest_row(self):
        out = "X:10:WPA2:\nX:90:WPA2:*"
        points = parse_wifi_list(out)
        self.assertEqual(len(points), 1)
        self.assertTrue(points[0].in_use)
        self.assertEqual(points[0].signal, 90)

    def test_open_network_is_not_secured(self):
        points = {p.ssid: p for p in parse_wifi_list(LIST_OUTPUT)}
        self.assertFalse(points["SZU_CTC&CMCC"].secured)
        self.assertTrue(points["Anorak_host"].secured)

    def test_status_fields(self):
        fields = parse_status_fields("GENERAL.DEVICE:wlan0\nIP4.ADDRESS[1]:10.0.0.2/24\n")
        self.assertEqual(fields["GENERAL.DEVICE"], "wlan0")
        self.assertEqual(fields["IP4.ADDRESS[1]"], "10.0.0.2/24")

    def test_broken_lines_are_skipped(self):
        self.assertEqual(parse_wifi_list("only-one-field\n:22:WPA2:\n"), [])
        self.assertEqual(parse_status_fields("no-colon-here\n"), {})


class TestStatus(unittest.TestCase):
    def test_connected_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            wifi = make_wifi(tmp, FakeNmcli())
            status = wifi.status()
        self.assertTrue(status.connected)
        self.assertEqual(status.ssid, "Anorak_host")
        self.assertEqual(status.ip, "192.168.137.30")
        self.assertEqual(status.gateway, "192.168.137.1")
        self.assertEqual(status.dns, ["192.168.137.1"])
        self.assertEqual(status.profile, "Anorak_host")
        self.assertEqual(status.signal, 100)
        self.assertTrue(status.autoconnect)
        self.assertEqual(status.connectivity, "full")

    def test_state_is_read_from_the_parentheses(self):
        """板端实测 `GENERAL.STATE:100 (connected)` —— 别把 100 当成状态。"""
        from agent.net.wifi import state_text
        self.assertEqual(state_text("100 (connected)"), "connected")
        self.assertEqual(state_text("30 (disconnected)"), "disconnected")
        self.assertEqual(state_text("20 (unavailable)"), "unavailable")
        self.assertEqual(state_text(""), "unknown")
        with tempfile.TemporaryDirectory() as tmp:
            wifi = make_wifi(tmp, FakeNmcli())
            self.assertTrue(wifi.status().connected, "connected 属性必须认得出来")

    def test_missing_device_degrades(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeNmcli()
            fake._nmcli = lambda argv: (10, "", "Error: no such device")  # type: ignore
            wifi = make_wifi(tmp, fake)
            status = wifi.status()
        self.assertEqual(status.state, "unavailable")
        self.assertFalse(status.connected)

    def test_scan_parses(self):
        with tempfile.TemporaryDirectory() as tmp:
            wifi = make_wifi(tmp, FakeNmcli())
            points = wifi.scan()
        self.assertTrue(points)
        self.assertEqual(points[0].ssid, "Anorak_host")


class TestPasswordBoundary(unittest.TestCase):
    """密码不许出现在 argv 里（`ps` 会把它暴露出去）。"""

    def test_existing_profile_uses_ask_and_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeNmcli()
            wifi = make_wifi(tmp, fake)
            result = wifi.connect("Anorak_host", "s3cret-psk")
        self.assertTrue(result["ok"])
        self.assertEqual(result["method"], "existing-profile")
        self.assertTrue(fake.called("--ask", "--ask") or fake.called("--ask", "connection", "up"),
                        fake.argv_strings())
        self.assertIn("s3cret-psk\n", fake.stdin_strings())
        self.assertNotIn("s3cret-psk", " ".join(fake.argv_strings()),
                         "密码绝不能出现在 argv 里")

    def test_new_profile_writes_a_0600_keyfile(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeNmcli()
            wifi = make_wifi(tmp, fake)
            with mock.patch("agent.net.wifi.os.chmod") as chmod:
                result = wifi.connect("新网络", "topsecret")
            files = list(Path(tmp).glob("*.nmconnection"))
            # ⚠ 临时目录出了 with 就没了 —— 读内容必须在里面做
            text = files[0].read_text(encoding="utf-8") if files else ""
            # ⚠ Windows 上 st_mode 看不出 0600（chmod 只能切只读位），所以断言"确实调了
            #    chmod(0o600)"——板端（Linux）由 t14_9 门禁真查权限。
            self.assertTrue(any(call.args[1] == 0o600 for call in chmod.call_args_list),
                            "必须显式 chmod 0600（密码就在这个文件里）: %r"
                            % (chmod.call_args_list,))
        self.assertEqual(result["method"], "keyfile")
        self.assertEqual(len(files), 1)
        self.assertIn("psk=topsecret", text)
        self.assertIn("ssid=新网络", text)
        self.assertIn("autoconnect=true", text)
        self.assertNotIn("topsecret", " ".join(fake.argv_strings()))
        self.assertTrue(fake.called("connection", "reload"))
        # 档案名是 sanitize 过的文件名（中文 → `_`），所以只断言"确实 up 了那个新档案"
        self.assertTrue(any(argv.startswith("nmcli connection up ") for argv in fake.argv_strings()),
                        fake.argv_strings())

    def test_open_network_has_no_security_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeNmcli(connections="Anorak_host:802-11-wireless:Anorak_host")
            wifi = make_wifi(tmp, fake)
            wifi.connect("免费WiFi", None)
            text = list(Path(tmp).glob("*.nmconnection"))[0].read_text(encoding="utf-8")
        self.assertNotIn("[wifi-security]", text)
        self.assertNotIn("psk", text)

    def test_autoconnect_false_is_written_into_the_keyfile(self):
        """新档案的 autoconnect 直接写进 keyfile（不再依赖 modify 去查一遍档案）。"""
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeNmcli()
            wifi = make_wifi(tmp, fake)
            wifi.connect("随手连的", "pw", autoconnect=False)
            text = list(Path(tmp).glob("*.nmconnection"))[0].read_text(encoding="utf-8")
        self.assertIn("autoconnect=false", text)

    def test_existing_profile_autoconnect_off_uses_modify(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeNmcli()
            wifi = make_wifi(tmp, fake)
            wifi.connect("Anorak_host", "pw", autoconnect=False)
        self.assertTrue(fake.called("modify", "connection.autoconnect", "no"),
                        fake.argv_strings())

    def test_keyfile_escapes_semicolon_and_backslash(self):
        text = build_connection_keyfile("a;b\\c", "p;w")
        self.assertIn("ssid=a\\;b\\\\c", text)
        self.assertIn("psk=p\\;w", text)

    def test_failure_is_reported_honestly(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeNmcli()
            fake.up_rc = 4
            fake.connected = False
            wifi = make_wifi(tmp, fake)
            with self.assertRaises(WifiError) as ctx:
                wifi.connect("Anorak_host", "pw")
        self.assertIn("activation failed", str(ctx.exception).lower())

    def test_empty_ssid_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            wifi = make_wifi(tmp, FakeNmcli())
            with self.assertRaises(WifiError):
                wifi.connect("", "pw")


class TestForgetAndAutoconnect(unittest.TestCase):
    def test_forget_only_deletes_the_named_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeNmcli(connections="Anorak_host:802-11-wireless:Anorak_host\n"
                                         "邻居家:802-11-wireless:邻居家")
            wifi = make_wifi(tmp, fake)
            result = wifi.forget("邻居家")
            deleted = [argv for argv in fake.argv_strings() if "delete" in argv]
        self.assertEqual(result["profile"], "邻居家")
        self.assertFalse(result["was_active"])
        self.assertEqual(len(deleted), 1)
        self.assertIn("邻居家", deleted[0])
        self.assertNotIn("Anorak_host", deleted[0], "绝不许顺手删掉当前链路")

    def test_forget_unknown_profile_is_an_honest_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            wifi = make_wifi(tmp, FakeNmcli())
            with self.assertRaises(WifiNotFound):
                wifi.forget("不存在的网")
            fake = FakeNmcli()
            wifi = make_wifi(tmp, fake)
            with self.assertRaises(WifiNotFound):
                wifi.forget("不存在的网")
            self.assertFalse(any("delete" in argv for argv in fake.argv_strings()),
                             "报错时一个档案都不该动")

    def test_forget_active_profile_flags_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            wifi = make_wifi(tmp, FakeNmcli())
            result = wifi.forget("Anorak_host")
        self.assertTrue(result["was_active"])

    def test_autoconnect_toggle(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeNmcli()
            wifi = make_wifi(tmp, fake)
            wifi.set_autoconnect("Anorak_host", False)
            wifi.set_autoconnect("Anorak_host", True)
        argv = fake.argv_strings()
        self.assertTrue(any("connection.autoconnect no" in a for a in argv), argv)
        self.assertTrue(any("connection.autoconnect yes" in a for a in argv), argv)


class TestHealth(unittest.TestCase):
    def test_ok_when_ip_and_gateway_reachable(self):
        with tempfile.TemporaryDirectory() as tmp:
            wifi = make_wifi(tmp, FakeNmcli(ping_rc=0))
            health = wifi.health()
        self.assertTrue(health.ok)
        self.assertEqual(health.gateway, "192.168.137.1")

    def test_ping_failure_is_not_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            wifi = make_wifi(tmp, FakeNmcli(ping_rc=1))
            health = wifi.health()
        self.assertTrue(health.has_ip)
        self.assertFalse(health.gateway_reachable)
        self.assertFalse(health.ok)

    def test_no_ip_is_not_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeNmcli()
            fake.connected = False
            wifi = make_wifi(tmp, fake)
            health = wifi.health()
        self.assertFalse(health.has_ip)
        self.assertFalse(health.ok)

    def test_gateway_falls_back_to_ip_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeNmcli(gateway="192.168.137.1",
                             device=device_output(gateway=""))
            wifi = make_wifi(tmp, fake)
            health = wifi.health()
        self.assertEqual(health.gateway, "192.168.137.1")
        self.assertTrue(fake.called("ip", "-4", "route"), fake.argv_strings())

    def test_connectivity_is_only_reference(self):
        """路由器不出网时 NM 会说 limited，但链路是好的 —— 别把它当判据。"""
        with tempfile.TemporaryDirectory() as tmp:
            wifi = make_wifi(tmp, FakeNmcli(connectivity="limited", ping_rc=0))
            health = wifi.health()
        self.assertEqual(health.connectivity, "limited")
        self.assertTrue(health.ok)


class TestLinkGuard(unittest.TestCase):
    def _guard(self, **kw):
        calls = []
        fake = FakeNmcli(**kw)
        guard = LinkGuard(make_wifi(tempfile.mkdtemp(), fake), log=logging.getLogger("t"),
                          sleep=lambda _s: calls.append("sleep"))
        return guard, fake, calls

    def test_healthy_probe_does_nothing(self):
        guard, fake, _ = self._guard(ping_rc=0)
        health = guard.probe()
        self.assertTrue(health.ok)
        self.assertEqual(guard.consecutive_failures, 0)
        self.assertFalse(fake.called("connection", "up"))

    def test_two_failures_do_not_trigger_a_repair(self):
        guard, fake, _ = self._guard(ping_rc=1)
        guard.probe()
        guard.probe()
        self.assertEqual(guard.consecutive_failures, 2)
        self.assertFalse(fake.called("connection", "up"), "第 3 次之前不许动手")

    def test_third_failure_repairs_and_recovers(self):
        # 前三次都不通；`connection up` 一被调用链路就"通了"（heal_on_up）
        guard, fake, calls = self._guard(ping_rc=1, heal_on_up=True)
        self.assertFalse(guard.probe().ok)
        self.assertFalse(guard.probe().ok)
        health = guard.probe()          # 第三次 → 触发重连 → 复查时已经好了
        self.assertTrue(health.ok)
        self.assertEqual(guard.consecutive_failures, 0, "恢复后计数归零")
        self.assertGreaterEqual(guard.repairs, 1)
        self.assertTrue(fake.called("connection", "up"), fake.argv_strings())
        self.assertIn("sleep", calls, "重连之后要等一会儿再复查")

    def test_repair_failure_escalates_to_device_cycle(self):
        guard, fake, _ = self._guard(ping_rc=1)
        guard.probe(); guard.probe()
        health = guard.probe()          # 修复也修不好
        self.assertFalse(health.ok)
        self.assertTrue(fake.called("device", "disconnect"), fake.argv_strings())
        self.assertTrue(fake.called("device", "connect"), fake.argv_strings())

    def test_backoff_grows_after_repair(self):
        guard, fake, _ = self._guard(ping_rc=1)
        base = guard.next_interval
        self.assertEqual(base, guard.interval_s)
        guard.probe(); guard.probe(); guard.probe()
        self.assertGreater(guard.next_interval, base, "修过之后要退避，别一直猛敲")

    def test_repairs_are_rate_limited(self):
        """反复修不好时会继续记日志，但不该无限刷重连。"""
        guard, fake, _ = self._guard(ping_rc=1)
        for _ in range(12):
            guard.probe()
        self.assertLessEqual(guard.repairs, 4, "重连次数要有上限: %d" % guard.repairs)

    def test_every_step_is_logged(self):
        """上次断网板端一条日志都没有 —— 这条是核心诉求。"""
        records = []

        class Sink(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        logger = logging.getLogger("wifi-test")
        logger.setLevel(logging.DEBUG)
        logger.addHandler(Sink())
        fake = FakeNmcli(ping_rc=1)
        guard = LinkGuard(make_wifi(tempfile.mkdtemp(), fake), log=logger,
                          sleep=lambda _s: None)
        guard.probe(); guard.probe(); guard.probe()
        joined = "\n".join(records)
        self.assertIn("链路不通", joined)
        self.assertIn("第 1/3 次", joined, "要说清是第几次失败")
        self.assertIn("开始主动重连", joined)

    def test_status_dict_is_serialisable(self):
        guard, _fake, _ = self._guard(ping_rc=0)
        guard.probe()
        payload = guard.status_dict()
        self.assertEqual(payload["threshold"], 3)
        self.assertIn("last", payload)
        self.assertTrue(payload["last"]["ok"])
        import json
        json.dumps(payload)          # 能进 IPC 推送（不能有不可序列化的东西）


class TestUnavailableEnvironment(unittest.TestCase):
    def test_available_reports_missing_nmcli(self):
        wifi = Wifi(runner=FakeNmcli())
        wifi.nmcli = "definitely-not-here-nmcli"
        self.assertFalse(wifi.available())

    def test_scan_error_is_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeNmcli()
            fake._nmcli = lambda argv: (10, "", "Error: Scanning not allowed")  # type: ignore
            wifi = make_wifi(tmp, fake)
            with self.assertRaises(WifiError) as ctx:
                wifi.scan()
        self.assertIn("Scanning not allowed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
