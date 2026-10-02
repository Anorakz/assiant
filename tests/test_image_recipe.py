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

T15-2-9 又添了两个"只有真整机构建才炸"的坑，也一并钉在这里：
  · **recovery 段**：`RK_AB_UPDATE=y` 会让 recovery 算出 `rockchip_rk3566_rk3568_recovery`
    这个并不存在的 defconfig 名 → Makefile 解析阶段就 Stop。我们的分区表没有 recovery 槽，
    所以在板级 defconfig 里显式关掉（两件事必须同时成立，故两个断言成对出现）。
  · **厂商 `libxcrypt.mk` 少 host 变体** → `No rule to make target 'host-libxcrypt'`。
    我们在本仓库覆盖同名 .mk（install-into-sdk.sh 注入），并钉住"只加 host、target 不动"。
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
INSTALL_SDK = _ROOT / "image" / "install-into-sdk.sh"
# T15-2-9：SDK 板级 defconfig（recovery 开关）+ 我们的 A/B 分区表 + 厂商 .mk 的补丁
BOARD_DEFCONFIG = (_ROOT / "image" / "device" / "rockchip" / ".chips" / "rk3566_rk3568"
                   / "rockchip_rk3568_kickpi_k1mini_release_defconfig")
AB_PARAMETER = BOARD_DEFCONFIG.parent / "parameter-assistant-ab.txt"
LIBXCRYPT_MK = _ROOT / "image" / "buildroot" / "package" / "libxcrypt" / "libxcrypt.mk"

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


class TestRecoveryIsOffAndThePartitionTableAgrees(unittest.TestCase):
    """T15-2-9：`RK_AB_UPDATE=y` 会让 recovery 段算出一个**并不存在**的 defconfig 名。

    实测死在整机构建的 Makefile 解析阶段：
        Makefile:1056: *** "Can't find rockchip_rk3566_rk3568_recovery_defconfig".  Stop.
    根因在 `Config.in.recovery`：`RK_RECOVERY_BASE_CFG` 的第一条默认值是
    `"" if RK_AB_UPDATE`（我们先命中它），于是名字按**芯片家族**拼成
    `rockchip_rk3566_rk3568_recovery`，而 SDK 里只有单芯片命名的那两个。

    这里钉住两个必须同时成立的事实（缺一个配方就自相矛盾）：
      ① 板级 defconfig 里 recovery **显式关掉**；
      ② 我们的 A/B 分区表里**没有 recovery 分区** —— 回退走 A/B + misc，
         所以关掉不是"绕过"，而是本来就不该编（编出来也没地方放）。
    """

    @classmethod
    def setUpClass(cls):
        cls.text = BOARD_DEFCONFIG.read_text(encoding="utf-8")

    def test_ab_update_is_on(self):
        self.assertIn("RK_AB_UPDATE=y", self.text)

    def test_recovery_is_explicitly_off(self):
        self.assertIn("# RK_RECOVERY is not set", self.text)
        self.assertNotIn("RK_RECOVERY=y", self.text)

    def test_parameter_has_no_recovery_slot(self):
        cmdline = ""
        for ln in AB_PARAMETER.read_text(encoding="utf-8").splitlines():
            if ln.startswith("CMDLINE:"):
                cmdline = ln
        self.assertTrue(cmdline, "parameter 里没有 CMDLINE: 行")
        self.assertNotIn("recovery", cmdline)
        for want in ("misc", "boot_a", "boot_b", "system_a", "system_b", "userdata:grow"):
            self.assertIn(want, cmdline)

    def test_board_defconfig_names_the_ab_parameter(self):
        """关掉 recovery 的前提是"确实走 A/B 那条路线"。"""
        self.assertIn('RK_PARAMETER="parameter-assistant-ab.txt"', self.text)


class TestLibxcryptHostVariantIsPatched(unittest.TestCase):
    """T15-2-9：厂商 `libxcrypt.mk` 少了 **host 变体** → 整机构建死在
    `No rule to make target 'host-libxcrypt'`（systemd 的
    `HOST_SYSTEMD_DEPENDENCIES` 要它，而 buildroot 只有包定义了 host 变体才生成该目标）。

    处理办法是**不动厂商树**，在本仓库覆盖同名 `.mk`，由 install-into-sdk.sh 注入。
    覆盖的风险是"顺手把 target 侧也改了"，所以这里同时钉住：
    target 变体、版本、SITE 都还在，只多了 host 两行 + 最后那行 host-autotools-package。
    """

    @classmethod
    def setUpClass(cls):
        cls.mk = LIBXCRYPT_MK.read_text(encoding="utf-8")
        cls.install = INSTALL_SDK.read_text(encoding="utf-8")

    def test_both_variants_are_evaluated(self):
        self.assertIn("$(eval $(autotools-package))", self.mk)
        self.assertIn("$(eval $(host-autotools-package))", self.mk)

    def test_host_conf_opts_come_in_pairs(self):
        """每条 LIBXCRYPT_CONF_OPTS 都要有对应的 HOST_ 版本。

        上游是这么补的；漏一条会让 host 侧退回默认值（这里恰好都是
        --disable-werror / --disable-obsolete_api，漏了不一定当场炸，
        所以更要在 CI 里对齐）。
        """
        target = re.findall(r"^LIBXCRYPT_CONF_OPTS \+?= (.*)$", self.mk, re.M)
        host = re.findall(r"^HOST_LIBXCRYPT_CONF_OPTS \+?= (.*)$", self.mk, re.M)
        self.assertTrue(target, "没有 LIBXCRYPT_CONF_OPTS 行")
        self.assertEqual(target, host)

    def test_vendor_version_and_site_are_untouched(self):
        self.assertIn("LIBXCRYPT_VERSION = 4.4.36", self.mk)
        self.assertIn("$(call github,besser82,libxcrypt,v$(LIBXCRYPT_VERSION))", self.mk)
        self.assertIn("LIBXCRYPT_INSTALL_STAGING = YES", self.mk)

    def test_installer_injects_it(self):
        self.assertIn(str(LIBXCRYPT_MK.relative_to(_ROOT)).replace("\\", "/"),
                      self.install.replace("\\", "/"))
        self.assertIn("buildroot/package/libxcrypt/libxcrypt.mk", self.install)


class TestNoSymbolNamesInComments(unittest.TestCase):
    """T15-2-10b-2：配置文件的注释里**一个符号全名都不许出现**。

    SDK 那个 sed 合并器不认"注释"：

      · 第一次（T15-2-2 那轮）：注释里一句带等号的写法被当成 CA_CERTIFICATES 的新值；
      · 第二次（T15-2-10b-2）：我为了写清楚"为什么开 python3 的 ssl"，
        在注释里写了符号全名 —— 合并时它被当成了那个开关的"上一个值"，
        于是**那个开关怎么改都不生效**，而 .config 里始终是 "is not set"。
        日志原话：
            Value of <python3 的 ssl 符号> is redefined by ..._defconfig:
            Previous value:  #    · `<符号全名>` → `_ssl` ...
            New value:       <符号全名>=y

    所以规矩是硬性的：注释里只用文字描述（"python3 的 SSL 子选项"），
    不写全名 —— **连举例说明也不行**。唯一的例外是合并器本来就认的那种
    "关掉某符号"行（`# <符号> is not set`），那是真配置，不是散文。
    """

    #: 合并器认的"关掉"形式（这是真配置行，允许）
    OFF_LINE = re.compile(r"^#\s*BR2_[A-Za-z0-9_]+ is not set\s*$")
    #: 符号全名（出现在散文注释里就是违规）
    SYMBOL = re.compile(r"BR2_[A-Za-z0-9_]+")

    def _violations(self, path):
        bad = []
        for i, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.strip()
            if not line.startswith("#"):
                continue
            if self.OFF_LINE.match(line):
                continue
            m = self.SYMBOL.search(line)
            if m:
                bad.append("%s:%d %s" % (path.name, i, line[:90]))
        return bad

    def test_fragment_comments_have_no_symbol_names(self):
        self.assertEqual(self._violations(FRAGMENT), [],
                         "products 片段的注释里出现了符号全名（合并器会当成配置解析）")

    def test_defconfig_comments_have_no_symbol_names(self):
        self.assertEqual(self._violations(DEFCONFIG), [],
                         "defconfig 的注释里出现了符号全名（合并器会当成配置解析）")


class TestPythonModulesTheAgentNeeds(unittest.TestCase):
    """T15-2-10b-2：python3 的 C 扩展模块 —— 缺了 agent **根本起不来**。

    实测（chroot 里真跑）：
        import agent.cli
          → agent/core/__init__ → ... → agent/net/sunshine_client.py:57
          → import ssl → ModuleNotFoundError: No module named '_ssl'
    也就是板上 `agent.service` 会一直崩溃重启。厂商基座把 python3 的这些模块
    全关着（"最小"思路），我们必须显式打开。

    这里钉两件事：
      ① 这两个符号必须是 `=y`（不是 `is not set`）；
      ② 它们必须出现在 **所有 `#include` 之后** —— kconfig 的值由**最后一个**
         赋值决定，放末尾才是"顺序上必胜"的位置（放片段里也行，但这条更直白、
         也不会被以后新增的片段压掉）。
    """

    #: agent 运行期真要用的（`: `_ssl` 由 SSL 一起带出 `_hashlib`）
    NEEDED = ("BR2_PACKAGE_PYTHON3_SSL", "BR2_PACKAGE_PYTHON3_READLINE")

    @classmethod
    def setUpClass(cls):
        cls.text = DEFCONFIG.read_text(encoding="utf-8")
        lines = cls.text.splitlines()
        cls.last_include = max((i for i, ln in enumerate(lines)
                                if ln.lstrip().startswith("#include")), default=-1)
        cls.lines = lines

    def test_modules_are_enabled(self):
        for sym in self.NEEDED:
            self.assertIn("%s=y" % sym, self.text, "%s 没打开" % sym)
            self.assertNotIn("# %s is not set" % sym, self.text,
                             "%s 同时被关掉了（两个相反的赋值同时存在）" % sym)

    def test_they_come_after_every_include(self):
        for sym in self.NEEDED:
            idx = [i for i, ln in enumerate(self.lines) if ln.strip() == "%s=y" % sym]
            self.assertTrue(idx, "%s=y 不在 defconfig 里" % sym)
            self.assertGreater(min(idx), self.last_include,
                               "%s=y 必须写在所有 #include 之后（kconfig 取最后一个赋值）" % sym)

    def test_reason_is_recorded(self):
        """注释里要写清"为什么非开不可" —— 否则以后有人为了省体积又把它关了。"""
        self.assertIn("_ssl", self.text)
        self.assertIn("sunshine_client", self.text)


class TestUbootHasAbSupport(unittest.TestCase):
    """T15-2-11 救砖：**分区表是 A/B 的，u-boot 也必须开 A/B**。

    首刷之后的串口日志（真事）：

        U-Boot next-dev (Sep 29 2026 - 22:02:18 +0800)   ← 我们编的 u-boot 起来了
        PartType: EFI
        FIT: No boot partition                           ← 它在找名叫 boot 的分区
        android_image_load_by_partname: Can't find part: boot
        Could not find userdata part
        =>                                               ← 掉到 u-boot 命令行

    而板上 `part list mmc 0` 证明 GPT 完全正确（boot_a/boot_b/system_a/system_b…）。
    根因：SDK 的 `RK_AB_UPDATE=y` 只影响**分区表模板与打包**，**传不到 u-boot**；
    厂商给 rk3588/rv1126/rk3576 都带了 A/B 片段，**rk3568 没有**。

    这一组把四件事钉住：片段存在且真的开了那个开关、注释干净、板级 defconfig 指过来、
    注入脚本把它拷进 SDK。
    """

    UBOOT_FRAGMENT = _ROOT / "image" / "uboot" / "rk3568-assistant-ab.config"
    FRAGMENT_NAME = "rk3568-assistant-ab"

    def test_fragment_exists_and_enables_ab(self):
        self.assertTrue(self.UBOOT_FRAGMENT.is_file(),
                        "缺 image/uboot/rk3568-assistant-ab.config（u-boot 的 A/B 支持）")
        text = self.UBOOT_FRAGMENT.read_text(encoding="utf-8")
        self.assertIn("CONFIG_ANDROID_AB=y", text)

    def test_fragment_comments_have_no_config_tokens(self):
        """片段里的注释**不许出现 `CONFIG_xxx` 记号**。

        u-boot 的 make.sh 会去片段里找"基础 defconfig"那一行，而它的解析不认注释。
        第一版在注释里举例写了那行符号，整个 u-boot 构建 4 秒就死：
            sed: can't read configs/#: No such file or directory
            ## make  #      rk3588_defconfig
            /bin/sh: 3: Syntax error: word unexpected (expecting "do")

        （同一类坑在 buildroot 的 products 片段上也踩过一次：合并器把一句中文注释
          当成了某个开关的一次赋值。凡是被脚本 grep 的片段，注释都要干净。）
        """
        bad = []
        off_re = re.compile(r"^#\s*CONFIG_[A-Za-z0-9_]+ is not set\s*$")   # 真配置行，允许
        for i, raw in enumerate(self.UBOOT_FRAGMENT.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.strip()
            if not line.startswith("#"):
                continue
            if off_re.match(line):
                continue
            if re.search(r"CONFIG_[A-Za-z0-9_]+", line):
                bad.append("第 %d 行: %s" % (i, line[:90]))
        self.assertEqual(bad, [],
                         "u-boot 片段的注释里出现了 CONFIG_ 记号（解析器会当成配置）:\n"
                         + "\n".join(bad))

    def test_board_defconfig_points_at_the_fragment(self):
        text = BOARD_DEFCONFIG.read_text(encoding="utf-8")
        self.assertIn('RK_UBOOT_CFG_FRAGMENTS="%s"' % self.FRAGMENT_NAME, text,
                      "板级 defconfig 必须用 RK_UBOOT_CFG_FRAGMENTS 指到我们的 A/B 片段")

    def test_installer_injects_the_fragment(self):
        install = INSTALL_SDK.read_text(encoding="utf-8")
        self.assertIn("image/uboot/rk3568-assistant-ab.config", install)
        self.assertIn("u-boot/configs/rk3568-assistant-ab.config", install)


class TestLocalWifiCredentialPlumbing(unittest.TestCase):
    """T15-2-11：板子**只有 wlan0 能通**，所以镜像得能把现场 WiFi 凭据带进去。

    这条链是三段，少一段都白搭（而且都是"没网时才发现"的那类）：

        仓库 image/local/wifi.nmconnection（**不进 git**，只留 .example 模板）
          → install-into-sdk.sh 注入 <SDK>/tools/assistant/local/
          → post-build 装成 <target>/etc/NetworkManager/system-connections/…（0600）

    这里只钉"三段都在、权限是 0600、模板不许带真凭据"。真凭据不进仓库由
    .gitignore 的 `image/local/*` + `!image/local/*.example` 保证（也在下面钉住）。
    """

    INSTALL = _ROOT / "image" / "install-into-sdk.sh"
    POSTBUILD = _ROOT / "image" / "board" / "rockchip" / "kickpi" / "k1mini" / "post-build.sh"
    EXAMPLE = _ROOT / "image" / "local" / "wifi.nmconnection.example"
    GITIGNORE = _ROOT / ".gitignore"

    def test_example_template_exists_and_is_a_template(self):
        self.assertTrue(self.EXAMPLE.is_file(), "缺 image/local/wifi.nmconnection.example")
        text = self.EXAMPLE.read_text(encoding="utf-8")
        self.assertIn("[wifi]", text)
        self.assertIn("ssid=", text)
        self.assertIn("psk=", text)
        # 模板里必须是占位符，不能是真凭据
        self.assertIn("在这里填", text)

    def test_gitignore_keeps_the_real_file_out_but_tracks_the_example(self):
        text = self.GITIGNORE.read_text(encoding="utf-8")
        self.assertIn("image/local/*", text)
        self.assertIn("!image/local/*.example", text)

    def test_installer_injects_it_and_marks_it_private(self):
        text = self.INSTALL.read_text(encoding="utf-8")
        self.assertIn("*.nmconnection", text)
        self.assertIn("chmod 0600", text)

    def test_post_build_installs_every_keyfile_0600(self):
        """支持**多份** keyfile：现场 SSID 记错时两个都写、谁对连谁
        （T15-2-11 实况：用户口头 `Anroak_host`，PC 侧实际是 `Anorak_host`）。"""
        text = self.POSTBUILD.read_text(encoding="utf-8")
        self.assertIn("NetworkManager/system-connections", text)
        self.assertIn("install -m 0600", text)
        self.assertIn("*.nmconnection", text)

    def test_post_build_masks_networkds_online_waiter(self):
        """T15-2-11 实测：`90-systemd.preset` 把 networkd 的等网器打开了，而这个镜像里
        networkd **没有 link 可管** → 它的启动任务**无超时**地干等，把
        `network-online.target` 顶住，我们的 agent/gui 全被拖着起不来：
            [ 8.0] Finished Network Manager Wait Online.        ← NM 这个 8 秒就过了
            [11–19s+] A start job is running for "Wait for Network to be Configured" (no limit)
        所以必须 mask 掉它。
        """
        text = self.POSTBUILD.read_text(encoding="utf-8")
        self.assertIn("systemd-networkd-wait-online.service", text)
        self.assertIn("/dev/null", text)

    def test_our_units_get_mode_0644(self):
        """systemd 会对每个 0755 的单元打一行 `marked executable` 警告
        （DrvFs 带进来的权限），post-build 里统一 chmod 0644。"""
        text = self.POSTBUILD.read_text(encoding="utf-8")
        self.assertIn("chmod 0644", text)


class TestTouchRotationIsDoneInLibinput(unittest.TestCase):
    """T15-2-11 板上定标：触摸旋转**不能**用 Qt 的 evdev 环境变量，必须用 libinput 矩阵。

    板端实测（`grep <gui pid>/maps`）：跑的是 `QT_QPA_PLATFORM=eglfs` +
    `QT_QPA_EGLFS_INTEGRATION=eglfs_kms`，进程里**没有** `libqevdevtouchplugin.so`，
    却有 `libinput.so.10` —— eglfs_kms 自己用 libinput 处理输入。于是
    `QT_QPA_EVDEV_TOUCHSCREEN_PARAMETERS=rotate=90` 这类设置**完全无效**：
    90/180/270/invertx/inverty 改了一圈都"差 90°"，白折腾好几轮。

    真正生效的是 udev 属性 `LIBINPUT_CALIBRATION_MATRIX`（见规则文件里的推导）：
    实测四角坐标反推出 (x,y) -> (1-y, x)，即矩阵 [0 -1 1; 1 0 0; 0 0 1]；
    板上加规则 + 重启 GUI 后用户确认"手指点哪，界面就反应在哪"。
    """

    RULE = _ROOT / "image" / "board" / "rockchip" / "kickpi" / "k1mini" / "udev" / "99-assistant-touch.rules"
    INSTALL = _ROOT / "image" / "install-into-sdk.sh"
    MATRIX = "0 -1 1 1 0 0 0 0 1"

    def test_rule_file_exists_with_the_measured_matrix(self):
        self.assertTrue(self.RULE.is_file(), "缺 udev 触摸校准规则文件")
        text = self.RULE.read_text(encoding="utf-8")
        self.assertIn('LIBINPUT_CALIBRATION_MATRIX}="%s"' % self.MATRIX, text)
        self.assertIn('ATTRS{name}=="goodix-ts"', text)

    def test_rule_file_records_why_not_the_qt_env_var(self):
        text = self.RULE.read_text(encoding="utf-8")
        self.assertIn("libinput", text.lower())
        self.assertIn("QT_QPA_EVDEV_TOUCHSCREEN_PARAMETERS", text)

    def test_installer_injects_the_rule(self):
        text = self.INSTALL.read_text(encoding="utf-8")
        self.assertIn("99-assistant-touch.rules", text)
        self.assertIn("etc/udev/rules.d", text)

    def test_units_do_not_pretend_to_rotate_touch(self):
        """单元里**不许**再留 `QT_QPA_EVDEV_TOUCHSCREEN_PARAMETERS`：
        它在这套 eglfs_kms+libinput 组合下无效，留着只会让下一个人继续白折腾。"""
        for rel in ("systemd/image/agent-gui.service", "systemd/agent-gui-nox.service"):
            text = (_ROOT / rel).read_text(encoding="utf-8")
            for line in text.splitlines():
                if line.strip().startswith("#"):
                    continue
                self.assertNotIn("QT_QPA_EVDEV_TOUCHSCREEN_PARAMETERS", line,
                                 "%s 里还有那条无效的触摸环境变量" % rel)


class TestUserdataGrowsAndIsNotMountedTwice(unittest.TestCase):
    """T15-2-11 板端：`/data` 首刷后只有 **3.5 MB**（分区却有 22.8 GiB）。

    `/proc/partitions` 证明 GPT 是对的（`userdata:grow` 让 p9 有 23,932,911 KB），
    但烧进去的文件系统还是 userdata.img 那个小 ext4，没人长大它 → 模型（4.9 GB）
    根本放不下。修法：fstab 上 `x-systemd.growfs`（挂载时按分区扩到底，幂等）。
    另外厂商那份 fstab 里还有 `PARTLABEL=userdata /userdata`，同一个文件系统被挂两次
    （板端 `df` 里 /data 和 /userdata 指向同一个 mmcblk0p9），我们的配方把它删掉。
    """

    POSTBUILD = _ROOT / "image" / "board" / "rockchip" / "kickpi" / "k1mini" / "post-build.sh"

    def test_fstab_line_has_growfs(self):
        text = self.POSTBUILD.read_text(encoding="utf-8")
        self.assertIn("x-systemd.growfs", text)

    def test_vendor_duplicate_userdata_mount_is_removed(self):
        text = self.POSTBUILD.read_text(encoding="utf-8")
        self.assertIn("/userdata", text)          # 删它的那几行得在
        self.assertIn("sed -i", text)


class TestWpaSupplicantHasDbusControlInterface(unittest.TestCase):
    """T15-2-11 板端：NM 起不了 supplicant（`Failed to D-Bus activate wpa_supplicant
    service`）→ wlan0 一直 `unavailable`，两份 WiFi 凭据都没机会用。

    厂商的 wireless.config 开了 WPA_SUPPLICANT 一堆选项，**唯独没开 DBUS/DBUS_NEW**；
    于是 `wpa_supplicant -u` 直接打用法退出，`wpa_supplicant.service`（Type=dbus）
    起不来，dbus 激活文件也不存在。NM 只认**新** API，所以两个开关都要。
    """

    DEFCONFIG = _ROOT / "image" / "buildroot" / "configs" / "rockchip_rk3568_kickpi_k1mini_release_defconfig"

    def test_dbus_switch_is_on(self):
        text = self.DEFCONFIG.read_text(encoding="utf-8")
        self.assertIn("BR2_PACKAGE_WPA_SUPPLICANT_DBUS=y", text)

    def test_legacy_dbus_new_symbol_is_not_written(self):
        """**别写 `..._DBUS_NEW`**：buildroot 2024.02 里它是 legacy 符号
        （2019.08 把 new/old 合并成一个 `DBUS`），写了会 `select BR2_LEGACY`
        → 整机构建直接死：
            Makefile.legacy:9: *** "You have legacy configuration in your .config!"
        这条守卫就是防止以后有人"顺手补上"再踩一次。
        """
        lines = self.DEFCONFIG.read_text(encoding="utf-8").splitlines()
        bad = [ln for ln in lines
               if ln.strip() == "BR2_PACKAGE_WPA_SUPPLICANT_DBUS_NEW=y"]
        self.assertEqual(bad, [], "DBUS_NEW 是 legacy 符号，写了构建会失败")

    def test_they_are_after_every_include(self):
        """必须在所有 `#include` 之后（同 PYTHON3_SSL 那条教训：最后一个赋值说了算）。"""
        lines = self.DEFCONFIG.read_text(encoding="utf-8").splitlines()
        last_inc = max((i for i, ln in enumerate(lines) if ln.lstrip().startswith("#include")), default=-1)
        for sym in ("BR2_PACKAGE_WPA_SUPPLICANT_DBUS=y",):
            idx = [i for i, ln in enumerate(lines) if ln.strip() == sym]
            self.assertTrue(idx, "%s 不在 defconfig 里" % sym)
            self.assertGreater(min(idx), last_inc, "%s 必须写在所有 #include 之后" % sym)


if __name__ == "__main__":
    unittest.main(verbosity=2)
