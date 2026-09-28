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
                  "assistant-init.service"):
            self.assertTrue((IMG / u).is_file(), "缺镜像单元 %s" % u)

    def test_target_wants_only_our_units_plus_networkmanager(self):
        target = parse(IMG / "assistant.target")
        wants = " ".join(values(target, "Unit", "Wants"))
        names = {t for t in wants.split() if t.endswith(".service")}
        self.assertEqual(
            names,
            {"agent.service", "agent-gui.service", "assistant-init.service", "NetworkManager.service"},
            "assistant.target 的 Wants 只该有我们这三个服务 + NetworkManager（别的都别加）")

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
