#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_image_target.py — 开机目标的守卫（T15-2-8）

两件事，都在 CI 里跑（不需要 SDK）：

  1. **镜像单元自洽**：调 `image/check-assistant-target.py --units systemd/image`。
     它解析 `assistant.target` 的依赖闭包，只允许我们的单元 + 白名单基础设施
     （NetworkManager）+ systemd 基座；出现 graphical/weston/slim/X11/蓝牙 一律失败；
     并且单元里不许留板端 git checkout 路径（/home/kickpi/...）。

  2. **两套单元不许悄悄漂**：板端形态在 `systemd/`（指向 /home/kickpi/... 那个工作区），
     镜像形态在 `systemd/image/`（指向 /usr/lib/assistant 与 /data）。同一个服务的**行为**
     必须一致 —— 只允许差异表里那几项（路径/日志/环境/WantedBy/After）。
     没有这条，改一边忘另一边是迟早的事。
"""
from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile
import unittest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
CHECKER = _ROOT / "image" / "check-assistant-target.py"
IMG = _ROOT / "systemd" / "image"
BOARD = _ROOT / "systemd"

#: 板端形态 -> 镜像形态
PAIRS = {
    "agent.service": "agent.service",
    "agent-gui-nox.service": "agent-gui.service",
}

#: 允许不同的键（都是"落点"性质的差异；其余一律要比）
ALLOWED_DIFF_KEYS = {
    ("Unit", "Description"),    # 镜像那份的描述会写明"镜像形态"
    ("Unit", "Documentation"),
    ("Unit", "After"),          # 镜像多了 assistant-init.service
    ("Unit", "Wants"),          # 理论上一样，但留一点余地
    ("Service", "WorkingDirectory"),
    ("Service", "ExecStart"),
    ("Service", "ExecStartPre"),
    ("Service", "StandardOutput"),
    ("Service", "StandardError"),
    ("Service", "Environment"),  # 镜像加了 D7 的那几个覆盖点
    # T15-2-11：镜像那份多一行 `EnvironmentFile=-/data/assistant/env/gui.env`
    # （现场调触摸旋转/渲染后端用，不用重刷镜像）。板端那份跑在 git 工作区里，
    # 直接改单元即可，不需要这个覆盖点 —— 所以这里如实记成"允许的差异"。
    ("Service", "EnvironmentFile"),
    ("Install", "WantedBy"),
}


def parse(path: pathlib.Path) -> dict:
    """{section: {key: [values]}} —— 值**按列表**存。

    ⚠ 第一版只留最后一个值，于是 assistant.target 里那**两行** `Wants=`
    （我们的三个服务 + NetworkManager）只看到后一行，用例直接误报。
    """
    out: dict = {}
    section = ""
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            out.setdefault(section, {})
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            out.setdefault(section, {}).setdefault(k.strip(), []).append(v.strip())
    return out


def values(unit: dict, section: str, key: str) -> list:
    return unit.get(section, {}).get(key, [])


def all_values(unit: dict, key: str) -> list:
    out = []
    for section in unit.values():
        out.extend(section.get(key, []))
    return out


class TestImageTarget(unittest.TestCase):
    def test_repo_mode_checker_passes(self):
        proc = subprocess.run([sys.executable, str(CHECKER), "--units", str(IMG)],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              universal_newlines=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("通过", proc.stdout)

    def test_the_four_units_exist(self):
        for u in ("assistant.target", "agent.service", "agent-gui.service",
                  "assistant-init.service", "ab-mark.service"):
            self.assertTrue((IMG / u).is_file(), "缺镜像单元 %s" % u)

    def test_target_wants_only_our_units_plus_networkmanager(self):
        target = parse(IMG / "assistant.target")
        wants = " ".join(values(target, "Unit", "Wants"))
        names = {t for t in wants.split() if t.endswith(".service")}
        self.assertEqual(
            names,
            {"agent.service", "agent-gui.service", "assistant-init.service",
             # T15-14-a：标记这件事改由**确认单元**做（判据：Agent 与 GUI 都 active
             # 且 IPC 通 ✓）。`ab-mark.service` 已从 target 的 `Wants=` 里移除 ✗ ——
             # 它开机**无条件**把当前槽标成功，会直接废掉失败回退路径 ✗
             # （2026-10-04 刷机后实测：post-build 改过之后它照样被拉起 ✗）。
             "assistant-ota-confirm.service",
             "NetworkManager.service"},
            "assistant.target 的 Wants 只该有我们的服务 + NetworkManager（别的都别加）")

    def test_units_have_no_board_paths_or_x(self):
        """只看**解析后的键值**，不扫注释 —— 注释里会正经提到"镜像里没有 xrandr 那条链"，
        第一版扫全文，于是被自己的说明文字误报。"""
        for u in IMG.glob("*"):
            unit = parse(u)
            for section, keys in unit.items():
                for key, vals in keys.items():
                    for v in vals:
                        low = v.lower()
                        self.assertNotIn("/home/kickpi", v, "%s: %s 里还留着板端路径" % (u.name, key))
                        if key in ("Environment", "ExecStart", "ExecStartPre", "After", "Wants",
                                   "Requires", "WantedBy"):
                            for bad in ("display-manager", "xrandr", "x11-unix", "display="):
                                self.assertNotIn(bad, low,
                                                 "%s: %s=%s 里出现 X 相关的东西（%s）" % (u.name, key, v, bad))


class TestBoardAndImageUnitsDoNotDrift(unittest.TestCase):
    def test_behavioural_keys_match(self):
        problems = []
        for board_name, img_name in PAIRS.items():
            b_path, i_path = BOARD / board_name, IMG / img_name
            if not b_path.is_file():
                continue
            b, i = parse(b_path), parse(i_path)
            for section in sorted(set(b) | set(i)):
                if section not in ("Unit", "Service", "Install"):
                    continue
                for key in sorted(set(b.get(section, {})) | set(i.get(section, {}))):
                    if (section, key) in ALLOWED_DIFF_KEYS:
                        continue
                    bv = values(b, section, key)
                    iv = values(i, section, key)
                    if bv != iv:
                        problems.append("%s vs %s: [%s] %s = %r / %r"
                                        % (board_name, img_name, section, key, bv, iv))
        self.assertEqual(problems, [],
                         "镜像单元与板端单元除了差异表之外不该不一样（要么同步，要么往差异表里加）:\n"
                         + "\n".join(problems))


class TestVendorServicesInOurBootChain(unittest.TestCase):
    """T15-2-10：**整机构建**那棵树里，我们的启动链上其实还挂着厂商服务。

    2-8 那次"只有我们的东西"是在**单包构建**那棵树上验的（那时还没装到这些），
    整机构建后对着真树一查就露出来了：

      · `wifibt-init.service`（`WantedBy=sysinit.target`，oneshot，加载 rtl8822cs 固件）
        —— **必须留**：屏蔽了 wlan0 连设备都没有，network-online 永远等不到；
      · `usb-gadget.service`（`WantedBy=local-fs.target`，`Type=simple` 常驻）
        —— 发行镜像 **mask 掉**（与"只起我们的东西"矛盾），开发镜像保留。

    这一组用例把"白名单有理由"与"mask 由 post-build 落"两件事钉住，
    并且用一棵**假的 target 树**真跑一遍检查器验证 mask 的语义
    （masked = systemd 语义上起不来 = 不算进闭包）。
    """

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(_ROOT / "image"))
        import importlib.util
        spec = importlib.util.spec_from_file_location("check_assistant_target", str(CHECKER))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cls.mod = mod

    def test_wifibt_init_is_allowlisted_with_a_reason(self):
        self.assertIn("wifibt-init.service", self.mod.INFRA_UNITS)

    def test_usb_gadget_is_masked_not_allowlisted(self):
        self.assertIn("usb-gadget.service", self.mod.MASKED_BY_US)
        self.assertNotIn("usb-gadget.service", self.mod.INFRA_UNITS,
                         "usb-gadget 是常驻的调试通道，不该进白名单（要么 mask，要么真需要）")

    def test_post_build_masks_it(self):
        post = (_ROOT / "image" / "board" / "rockchip" / "kickpi" / "k1mini"
                / "post-build.sh").read_text(encoding="utf-8")
        self.assertIn("usb-gadget.service", post)
        self.assertIn("MASK_UNITS", post)
        self.assertIn("/dev/null", post)

    # -- 用假树真跑检查器 ---------------------------------------------------
    def _fake_target(self, tmp: pathlib.Path, mask_usb_gadget: bool):
        target = pathlib.Path(tmp) / "target"
        sysd = target / "usr/lib/systemd/system"
        etc = target / "etc/systemd/system"
        sysd.mkdir(parents=True)
        for u in ("assistant.target", "agent.service", "agent-gui.service",
                  "assistant-init.service"):
            (sysd / u).write_text((IMG / u).read_text(encoding="utf-8"), encoding="utf-8")
        # systemd 基座的最小链条（**必须有**，否则闭包走不到 local-fs/sysinit，
        # 也就看不见厂商挂在那两个 target 上的服务 —— 第一版假树就漏了这个，
        # 于是"未 mask 应该失败"的用例假通过了）
        (sysd / "basic.target").write_text(
            "[Unit]\nDescription=Basic System\nRequires=sysinit.target\n", encoding="utf-8")
        (sysd / "sysinit.target").write_text(
            "[Unit]\nDescription=System Initialization\nRequires=local-fs.target\n",
            encoding="utf-8")
        (sysd / "local-fs.target").write_text(
            "[Unit]\nDescription=Local File Systems\n", encoding="utf-8")
        # 厂商那两个（内容够解析即可）
        (sysd / "wifibt-init.service").write_text(
            "[Unit]\nDescription=Init Rockchip Wifi/BT\n[Service]\n"
            "Type=oneshot\nExecStart=/usr/bin/wifibt-init.sh start\n[Install]\n"
            "WantedBy=sysinit.target\n", encoding="utf-8")
        (sysd / "usb-gadget.service").write_text(
            "[Unit]\nDescription=Manage USB gadget functions\n[Service]\n"
            "Type=simple\nExecStart=/usr/bin/usb-gadget start\n[Install]\n"
            "WantedBy=local-fs.target\n", encoding="utf-8")

        (etc / "assistant.target.wants").mkdir(parents=True)
        for u in ("agent.service", "agent-gui.service"):
            (etc / "assistant.target.wants" / u).symlink_to("/usr/lib/systemd/system/" + u)
        (etc / "sysinit.target.wants").mkdir()
        (etc / "sysinit.target.wants" / "wifibt-init.service").symlink_to(
            "/usr/lib/systemd/system/wifibt-init.service")
        (etc / "local-fs.target.wants").mkdir()
        (etc / "local-fs.target.wants" / "usb-gadget.service").symlink_to(
            "/usr/lib/systemd/system/usb-gadget.service")
        (etc / "default.target").symlink_to("/usr/lib/systemd/system/assistant.target")
        if mask_usb_gadget:
            (etc / "usb-gadget.service").symlink_to("/dev/null")
        return target

    def _run(self, target: pathlib.Path):
        sdk = target.parent
        return subprocess.run([sys.executable, str(CHECKER), "--sdk", str(sdk),
                               "--target", str(target)],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              universal_newlines=True, timeout=120)

    def test_unmasked_vendor_service_fails_the_check(self):
        with tempfile.TemporaryDirectory() as t:
            target = self._fake_target(pathlib.Path(t), mask_usb_gadget=False)
            proc = self._run(target)
            self.assertEqual(proc.returncode, 1, proc.stdout)
            self.assertIn("usb-gadget.service", proc.stdout)

    def test_masked_vendor_service_passes_and_is_reported(self):
        with tempfile.TemporaryDirectory() as t:
            target = self._fake_target(pathlib.Path(t), mask_usb_gadget=True)
            proc = self._run(target)
            self.assertEqual(proc.returncode, 0, proc.stdout)
            self.assertIn("被我们 mask 掉", proc.stdout)
            self.assertIn("wifibt-init.service", proc.stdout)
            self.assertIn("--target 指定", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
