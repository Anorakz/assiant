#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_image_recipe.py — 镜像配方守卫（T15-2-5）

T15-2-2 那轮踩的两次坑都不是逻辑错，而是**文件格式**的坑：

  · buildroot defconfig 的 include 行**带尾注释** → SDK 的片段合并器把注释当路径去读，
    直接 "The merge file 'configs/rockchip/#' does not exist. Exit."
  · **注释里写了一个带等号的符号名** → 合并器的 sed 把它当成一条**真配置**去解析
    （实测把一句注释当成了 CA 证书那条的新值）。

这类错误有个共同点：**改配方的人当场看不出来，只有真去 make 才炸**。所以这里把
docs/image.md §5.2 那"四条纪律"里能机器检查的部分钉进 CI。

另外还钉住一条**跨文件不变量**：G52 的 blob 文件名。
rockchip-mali.mk 是按**已启用的 winsys** 拼 `-Dplatform` 的，而
external/libmali/scripts/grabber.sh 是拿这个串去 `find` 文件名。所以：

    片段里的 HAS_X11 / HAS_WAYLAND / HAS_OPENCL / HAS_GBM
        → 平台串 → image/prepare-libmali.sh 里放那份 blob 的文件名

三者必须一致。改一个 HAS_* 而忘了同步 prepare-libmali.sh，后果是构建期
`ERROR: Failed to find matched library`（同样只有真构建才发现）。
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
FRAGMENT = (_ROOT / "image" / "buildroot" / "configs" / "rockchip"
            / "products" / "kickpi-k1mini-release.config")
DEFCONFIG = (_ROOT / "image" / "buildroot" / "configs"
             / "rockchip_rk3568_kickpi_k1mini_release_defconfig")
PREPARE = _ROOT / "image" / "prepare-libmali.sh"
SDK_MAKE = _ROOT / "image" / "sdk-make.sh"
PRIME_DL = _ROOT / "image" / "prime-dl.sh"

MALI = "BR2_PACKAGE_ROCKCHIP_MALI_"

CONFIG_RE = re.compile(r"^(BR2_[A-Za-z0-9_]+)=(.*)$")
OFF_RE = re.compile(r"^#\s*(BR2_[A-Za-z0-9_]+) is not set\s*$")
INCLUDE_RE = re.compile(r'^\s*#include\s+"[^"#]+"\s*$')
SMUGGLED_RE = re.compile(r"BR2_[A-Za-z0-9_]+\s*=")


def parse_fragment(text):
    """返回 (={符号: 值}, {被显式关掉的符号})。

    只认三种行：`符号=值`、`# 符号 is not set`、其余一律当注释/空行。
    这正是 SDK 那个 sed 合并器的判别口径 —— 我们复刻它，是为了在 CI 里
    提前发现"注释被它当成配置"的情况。
    """
    on, off = {}, set()
    for raw in text.splitlines():
        line = raw.strip()
        m = CONFIG_RE.match(line)
        if m:
            val = m.group(2).strip()
            if len(val) >= 2 and val.startswith('"') and val.endswith('"'):
                val = val[1:-1]
            on[m.group(1)] = val
            continue
        m = OFF_RE.match(line)
        if m:
            off.add(m.group(1))
    return on, off


def sh_var(text, name):
    m = re.search(r'^%s="([^"]*)"' % re.escape(name), text, re.M)
    if not m:
        raise AssertionError("prepare-libmali.sh 里没有 %s= 这一行" % name)
    return m.group(1)


class TestFragmentDiscipline(unittest.TestCase):
    """docs/image.md §5.2 的四条纪律（能机器检查的部分）。"""

    def test_include_lines_have_no_trailing_comment(self):
        text = DEFCONFIG.read_text(encoding="utf-8")
        bad = [ln for ln in text.splitlines()
               if ln.lstrip().startswith("#include") and not INCLUDE_RE.match(ln)]
        self.assertEqual(bad, [], "include 行带了尾注释（合并器会把注释当路径）: %r" % bad)

    def test_fragment_has_no_config_hiding_in_comments(self):
        text = FRAGMENT.read_text(encoding="utf-8")
        bad = []
        for ln in text.splitlines():
            s = ln.strip()
            if not s.startswith("#") or OFF_RE.match(s):
                continue
            if SMUGGLED_RE.search(ln):
                bad.append(ln)
        self.assertEqual(bad, [], "注释里出现了带等号的符号名（会被当成真配置）: %r" % bad)

    def test_no_trailing_comment_on_config_lines(self):
        text = FRAGMENT.read_text(encoding="utf-8")
        bad = [ln for ln in text.splitlines()
               if CONFIG_RE.match(ln.strip()) and "#" in ln.split("=", 1)[1]]
        self.assertEqual(bad, [], "配置行带了尾注释（注释会被并进值里）: %r" % bad)


class TestMaliPlatformMatchesTheBlobName(unittest.TestCase):
    """片段里的 winsys 选择 → 平台串 → prepare-libmali.sh 的 blob 文件名。"""

    @classmethod
    def setUpClass(cls):
        cls.on, cls.off = parse_fragment(FRAGMENT.read_text(encoding="utf-8"))
        cls.prepare = PREPARE.read_text(encoding="utf-8")

    def expected_platform(self):
        """复刻 rockchip-mali.mk 拼 ROCKCHIP_MALI_PLATFORM 的顺序。"""
        parts = []
        # 非 utgard 且没开 OpenCL 时会先塞一个 nocl（见 .mk 第 47-51 行）
        is_utgard = "UTGARD" in "".join(k for k in self.on if k.startswith(MALI))
        if not is_utgard and (MALI + "HAS_OPENCL") not in self.on:
            parts.append("nocl")
        for sym, name in ((MALI + "HAS_VULKAN", "vulkan"),
                          (MALI + "HAS_DUMMY", "dummy"),
                          (MALI + "HAS_X11", "x11"),
                          (MALI + "HAS_WAYLAND", "wayland"),
                          (MALI + "HAS_GBM", "gbm")):
            if self.on.get(sym) == "y":
                parts.append(name)
        return "-".join(parts)

    def test_g52_is_selected(self):
        self.assertEqual(self.on.get(MALI + "BIFROST_G52"), "y")
        self.assertNotIn(MALI + "VALHALL_G610", self.on)

    def test_platform_is_x11_wayland_gbm(self):
        self.assertEqual(self.expected_platform(), "x11-wayland-gbm")

    def test_vulkan_stays_off_because_the_blob_has_no_vk_symbols(self):
        # 这份 G52 blob 导出 0 个 vk* 符号；开了会往平台串塞 vulkan → 找不到 .so
        self.assertNotIn(MALI + "HAS_VULKAN", self.on)

    def test_gbm_egl_gles_opencl_on(self):
        for sym in ("HAS_GBM", "HAS_EGL", "HAS_GLES", "HAS_OPENCL"):
            self.assertEqual(self.on.get(MALI + sym), "y", sym)

    def test_xorg7_and_wayland_are_enabled(self):
        """HAS_X11 依赖 XORG7、HAS_WAYLAND 依赖 WAYLAND。

        依赖不满足时 kconfig 会让这两个符号**直接消失**，然后平台串会悄悄
        变成别的值（blob 就找不到了）。所以必须把"它们真的能成立"也钉住。
        """
        self.assertEqual(self.on.get("BR2_PACKAGE_XORG7"), "y")
        self.assertEqual(self.on.get("BR2_PACKAGE_WAYLAND"), "y")

    def test_prepare_script_agrees_with_the_fragment(self):
        self.assertEqual(sh_var(self.prepare, "PLATFORM"), self.expected_platform())
        # Config.in 里 bifrost-g52 的 version 默认值是 g24p0（第 65 行）
        self.assertEqual(sh_var(self.prepare, "VERSION"), "g24p0")
        self.assertEqual(sh_var(self.prepare, "BLOB_NAME"),
                         "libmali-${GPU}-${VERSION}-${PLATFORM}.so")
        self.assertEqual(sh_var(self.prepare, "GPU"), "bifrost-g52")

    def test_default_deb_name_carries_the_same_platform_string(self):
        """厂商 deb 名里的平台串必须与 blob 名一致（否则就是从两份不同的东西里拼的）。"""
        deb = sh_var(self.prepare, "DEFAULT_DEB_REL")
        self.assertIn("libmali-${GPU}-${VERSION}-${PLATFORM}", deb)
        self.assertIn("_1.9-1_arm64.deb", deb)

    def test_pinned_fingerprints_are_well_formed(self):
        so_sha = sh_var(self.prepare, "EXPECT_SO_SHA256")
        deb_sha = sh_var(self.prepare, "EXPECT_DEB_SHA256")
        self.assertRegex(so_sha, r"^[0-9a-f]{64}$")
        self.assertRegex(deb_sha, r"^[0-9a-f]{64}$")
        self.assertEqual(sh_var(self.prepare, "EXPECT_SO_SIZE"), "56387136")


class TestQtStaysOnEglfs(unittest.TestCase):
    """路线 C：Qt 只能跑 EGLFS —— 不能因为引进了 X 的客户端库就把 QPA 变成 xcb。"""

    @classmethod
    def setUpClass(cls):
        cls.on, _ = parse_fragment(FRAGMENT.read_text(encoding="utf-8"))

    def test_default_qpa_is_eglfs(self):
        self.assertEqual(self.on.get("BR2_PACKAGE_QT5BASE_DEFAULT_QPA"), "eglfs")

    def test_eglfs_is_on_and_xcb_backend_is_not(self):
        self.assertEqual(self.on.get("BR2_PACKAGE_QT5BASE_EGLFS"), "y")
        self.assertNotIn("BR2_PACKAGE_QT5BASE_XCB", self.on)


class TestSdkMakeSanitisesPath(unittest.TestCase):
    """sdk-make.sh 必须把带空格的 / Windows 的 PATH 条目剔掉。

    背景：WSL 会把 Windows 的 PATH 接到 Linux PATH 后面，里面有
    "/mnt/c/Program Files/..."。buildroot 的 support/dependencies/dependencies.sh
    只要看到带空格的条目就直接 "This doesn't work. Fix you PATH." 退出 —— 症状
    看上去像 buildroot 坏了。这个用例用假的 make 把"实际传给 make 的 PATH"抓下来。
    """

    def setUp(self):
        self.bash = shutil.which("bash")
        if not self.bash:
            self.skipTest("需要 bash（Windows 上由 .ps1 那套跑）")

    def run_script(self, args, path=None):
        env = os.environ.copy()
        if path is not None:
            env["PATH"] = path
        return subprocess.run([self.bash, str(SDK_MAKE)] + args,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              universal_newlines=True, timeout=60, env=env)

    def test_usage_without_arguments(self):
        self.assertEqual(self.run_script([]).returncode, 2)

    def test_rejects_a_directory_that_is_not_an_sdk(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.run_script([tmp]).returncode, 2)

    def test_spaced_and_windows_path_entries_are_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            sdk = tmp / "sdk"
            (sdk / "buildroot").mkdir(parents=True)
            bindir = tmp / "bin"
            bindir.mkdir()
            seen = tmp / "path.txt"
            fake_make = bindir / "make"
            fake_make.write_text('#!/bin/sh\nprintf "%s" "$PATH" > "%s"\nexit 0\n'
                                 % ("%s", seen), encoding="utf-8")
            fake_make.chmod(fake_make.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP)

            spaced = tmp / "dir with space"
            spaced.mkdir()
            path = ":".join([str(bindir), str(spaced), "/mnt/c/Program Files/x",
                             "/usr/bin", "/bin"])
            proc = self.run_script([str(sdk), "rockchip-mali"], path=path)
            self.assertEqual(proc.returncode, 0, proc.stdout)
            seen_path = seen.read_text(encoding="utf-8")
            self.assertNotIn(" ", seen_path, "带空格的条目没被剔掉: %r" % seen_path)
            self.assertNotIn("/mnt/", seen_path, "Windows 路径没被剔掉: %r" % seen_path)
            self.assertIn(str(bindir), seen_path)


class TestPrimeDlExpandsMakeVariables(unittest.TestCase):
    """prime-dl.sh 必须能**展开 .mk 里的变量**再判断。

    背景（T15-2-6 实测踩到的）：qt5 的 .mk 里是
        QT5BASE_SITE = $(QT5_SITE)/qtbase/-/archive/$(QT5BASE_VERSION)
        QT5BASE_SOURCE = qtbase-$(QT5BASE_VERSION).tar.bz2
    第一版脚本直接对**文本**匹配 "invent.kde.org"，结果 6 个 Qt 包全被跳过、
    一个都没预置（不报错、静默什么都不做）。这个用例用一棵假 SDK 树把那次的
    失败方式钉死：必须认出这是 KDE 的 commit 归档，并算出确切的目标文件名。
    """

    COMMIT = "da6e958319e95fe564d3b30c931492dd666bfaff"
    SHA = "935d01f5c34903ad9e979431cec7a8a59332ed3fc539e639f5ba87e8d6989b9d"

    def _fake_sdk(self, tmp: Path, with_kde: bool = True, with_other: bool = False):
        br = tmp / "buildroot"
        qt5 = br / "package" / "qt5"
        (qt5 / "qt5base").mkdir(parents=True)
        (qt5 / "qt5.mk").write_text(
            "QT5_VERSION_MAJOR = 5.15\n"
            "QT5_VERSION = $(QT5_VERSION_MAJOR).11\n"
            "QT5_SITE = %s\n" % ("https://invent.kde.org/qt/qt" if with_kde
                                 else "https://example.invalid/qt"),
            encoding="utf-8")
        (qt5 / "qt5base" / "qt5base.mk").write_text(
            "QT5BASE_VERSION = %s\n"
            "QT5BASE_SITE = $(QT5_SITE)/qtbase/-/archive/$(QT5BASE_VERSION)\n"
            "QT5BASE_SOURCE = qtbase-$(QT5BASE_VERSION).tar.bz2\n" % self.COMMIT,
            encoding="utf-8")
        (qt5 / "qt5base" / "qt5base.hash").write_text(
            "sha256  %s  qtbase-%s.tar.bz2\n" % (self.SHA, self.COMMIT),
            encoding="utf-8")
        if with_other:
            (qt5 / "qt5other").mkdir()
            (qt5 / "qt5other" / "qt5other.mk").write_text(
                "QT5OTHER_VERSION = 1.0\n"
                "QT5OTHER_SITE = https://example.invalid/other\n"
                "QT5OTHER_SOURCE = other-$(QT5OTHER_VERSION).tar.gz\n",
                encoding="utf-8")
            (qt5 / "qt5other" / "qt5other.hash").write_text(
                "sha256  %s  other-1.0.tar.gz\n" % ("0" * 64), encoding="utf-8")
        cfg = tmp / "config"
        cfg.write_text("BR2_PACKAGE_QT5BASE=y\n"
                       + ("BR2_PACKAGE_QT5OTHER=y\n" if with_other else ""),
                       encoding="utf-8")
        return tmp, cfg

    def setUp(self):
        self.bash = shutil.which("bash")
        if not self.bash:
            self.skipTest("需要 bash（Windows 上由 .ps1 那套跑）")

    def run_prime(self, sdk, cfg):
        return subprocess.run([self.bash, str(PRIME_DL), str(sdk),
                               "--config", str(cfg), "--dry-run"],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              universal_newlines=True, timeout=60)

    def test_expands_variables_and_finds_the_commit_archive(self):
        with tempfile.TemporaryDirectory() as t:
            sdk, cfg = self._fake_sdk(Path(t))
            proc = self.run_prime(sdk, cfg)
            self.assertEqual(proc.returncode, 0, proc.stdout)
            self.assertIn("dl/qt5base/qtbase-%s.tar.bz2" % self.COMMIT, proc.stdout)
            self.assertIn("sources.buildroot.net/qt5base/qtbase-%s.tar.bz2" % self.COMMIT,
                          proc.stdout)
            self.assertIn("invent.kde.org", proc.stdout)   # 打印解析出来的 Qt 来源

    def test_non_kde_package_is_skipped(self):
        with tempfile.TemporaryDirectory() as t:
            sdk, cfg = self._fake_sdk(Path(t), with_other=True)
            proc = self.run_prime(sdk, cfg)
            self.assertEqual(proc.returncode, 0, proc.stdout)
            self.assertIn("qt5other: 不是 KDE 的 commit 归档", proc.stdout)

    def test_usage_and_bad_sdk(self):
        self.assertEqual(subprocess.run([self.bash, str(PRIME_DL)],
                                        stdout=subprocess.PIPE,
                                        universal_newlines=True).returncode, 2)
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(self.run_prime(Path(t), Path(t) / "nope").returncode, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
