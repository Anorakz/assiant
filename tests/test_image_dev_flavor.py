#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_image_dev_flavor.py — 开发镜像（T15-2-12）配方的守卫测试

开发镜像与发行镜像的关系必须是**结构性的**，不靠人去同步两份配置：

    buildroot:  rockchip_rk3568_kickpi_k1mini_dev_defconfig
                  ├─ #include 发行那份 defconfig（原样）
                  └─ #include products/kickpi-k1mini-dev-assistant.config（只加不改）

    板级:       rockchip_rk3568_kickpi_k1mini_dev_defconfig
                  = 发行那份，**只差** RK_BUILDROOT_BASE_CFG 一行

这些不变量一旦破掉（比如有人"顺手"在 dev 片段里改了发行镜像的选择、或者两份
板级 defconfig 开始漂），下面这些用例就会红。
"""
import pathlib
import re
import unittest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
IMG = _ROOT / "image"
BC = IMG / "buildroot" / "configs"
BOARD = IMG / "device" / "rockchip" / ".chips" / "rk3566_rk3568"

DEV_BC_DEFCONFIG = BC / "rockchip_rk3568_kickpi_k1mini_dev_defconfig"
REL_BC_DEFCONFIG = BC / "rockchip_rk3568_kickpi_k1mini_release_defconfig"
DEV_FRAGMENT = BC / "rockchip" / "products" / "kickpi-k1mini-dev-assistant.config"
DEV_BOARD = BOARD / "rockchip_rk3568_kickpi_k1mini_dev_defconfig"
REL_BOARD = BOARD / "rockchip_rk3568_kickpi_k1mini_release_defconfig"
INSTALL = IMG / "install-into-sdk.sh"
BUILD_IMAGE = IMG / "build-image.sh"
MAKE_DEV_SDK = IMG / "make-dev-sdk.sh"


def config_lines(path: pathlib.Path):
    """只取"配置行"（跳过空行与注释）——注释可以自由写，配置才是承诺。"""
    return [ln.rstrip("\n") for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


class TestDevBuildrootDefconfig(unittest.TestCase):
    def test_dev_defconfig_only_includes(self):
        """dev 的 buildroot defconfig 里**不该有具体配置项**：差异全在片段里。"""
        self.assertEqual(config_lines(DEV_BC_DEFCONFIG), [],
                         "dev defconfig 只该有两行 #include，配置项请放进 dev 片段")

    def test_dev_defconfig_includes_release_then_dev_fragment(self):
        text = DEV_BC_DEFCONFIG.read_text(encoding="utf-8")
        inc = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("#include")]
        self.assertEqual(
            inc,
            ['#include "rockchip_rk3568_kickpi_k1mini_release_defconfig"',
             '#include "products/kickpi-k1mini-dev-assistant.config"'],
            "顺序很重要：先引发行那份（拿到全部基础），再叠加 dev 增量")

    def test_include_lines_have_no_trailing_comment(self):
        """`#include` 行带尾注释会被 SDK 的 sed 合并器当路径读 → 直接报错。"""
        for p in (DEV_BC_DEFCONFIG, REL_BC_DEFCONFIG, DEV_FRAGMENT):
            for n, ln in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                s = ln.strip()
                if s.startswith("#include"):
                    self.assertRegex(s, r'^#include "[^"]+"$',
                                     "%s:%d 的 #include 行必须裸写" % (p.name, n))


class TestDevFragment(unittest.TestCase):
    """清单就是承诺：用户 2026-10-02 批的是"方案1 + 不用 nano + 开 ccache"。"""

    REQUIRED = [
        "BR2_PACKAGE_GCC_TARGET",     # 板上 gcc（工具链是 BR2_TOOLCHAIN_BUILDROOT）
        "BR2_PACKAGE_BINUTILS_TARGET",
        "BR2_PACKAGE_MAKE",
        # ⚠ 板上 cmake 只能靠 ctest 那个有提示的选项 select（`BR2_PACKAGE_CMAKE`
        #   在本套 buildroot 里是隐藏符号，defconfig 写了会被静默丢掉，实测踩过）
        "BR2_PACKAGE_CMAKE_CTEST",
        "BR2_PACKAGE_PKGCONF",
        "BR2_PACKAGE_GDB",
        "BR2_PACKAGE_GDB_DEBUGGER",
        "BR2_PACKAGE_GDB_SERVER",     # gdbserver
        "BR2_PACKAGE_STRACE",
        "BR2_PACKAGE_PROCPS_NG",      # pgrep/pkill/top（T15-2-11 被它坑过）
        "BR2_PACKAGE_VIM",
        "BR2_PACKAGE_TCPDUMP",
        "BR2_PACKAGE_IPERF3",
        "BR2_PACKAGE_GIT",
        "BR2_PACKAGE_PYTHON_PYTEST",
        "BR2_PACKAGE_PYTHON3_ZLIB",   # 发行镜像没有它，板端脚本会 ModuleNotFoundError
    ]

    def test_every_agreed_package_is_enabled(self):
        text = DEV_FRAGMENT.read_text(encoding="utf-8")
        missing = [s for s in self.REQUIRED if ("%s=y" % s) not in text]
        self.assertEqual(missing, [], "dev 片段缺这些（用户批过的清单）：%s" % missing)

    def test_no_nano(self):
        """用户 2026-10-02 明确：不要 nano。"""
        text = DEV_FRAGMENT.read_text(encoding="utf-8")
        self.assertNotIn("BR2_PACKAGE_NANO=y", text)

    def test_ninja_is_intentionally_absent(self):
        """这套 buildroot 里 ninja 只有 host 版（没有板上包）→ 清单里不该出现它。

        哪天真有板上 ninja 了，这条会红，提醒我们回来更新注释与清单。
        """
        text = DEV_FRAGMENT.read_text(encoding="utf-8")
        self.assertNotIn("BR2_PACKAGE_NINJA=y", text)
        self.assertIn("ninja", text, "至少要在注释里写明为什么没有它")

    def test_hidden_cmake_symbol_is_not_used(self):
        """`BR2_PACKAGE_CMAKE` 是隐藏符号（无提示）→ 写了等于没写，必须用 CTEST。

        实测（2026-10-02）：片段里写 `BR2_PACKAGE_CMAKE=y`，生成的 .config 里
        既没有它、也没有 cmake 相关依赖，`make <defconfig>` **不报错**——
        这种"静默失效"只有逐项核对 .config 才抓得到。
        """
        text = DEV_FRAGMENT.read_text(encoding="utf-8")
        self.assertNotIn("\nBR2_PACKAGE_CMAKE=y", text)
        self.assertNotIn("BR2_PACKAGE_NINJA=y", text)

    def test_comments_do_not_carry_config_tokens(self):
        """老坑（T15-2-2）：注释里出现构造记号会被 sed 合并器当配置行解析。"""
        bad = []
        for n, ln in enumerate(DEV_FRAGMENT.read_text(encoding="utf-8").splitlines(), 1):
            s = ln.lstrip()
            if s.startswith("#") and re.search(r"\bBR2_[A-Z0-9_]+|\bCONFIG_[A-Z0-9_]+", ln):
                bad.append("%s:%d %s" % (DEV_FRAGMENT.name, n, ln.strip()[:70]))
        self.assertEqual(bad, [], "注释里不许写字面记号：\n" + "\n".join(bad))


class TestDevBoardDefconfig(unittest.TestCase):
    def test_board_configs_differ_only_in_buildroot_base_cfg(self):
        rel, dev = config_lines(REL_BOARD), config_lines(DEV_BOARD)
        self.assertEqual(len(rel), len(dev), "两份板级 defconfig 的配置行数应相同")
        diff = [(a, b) for a, b in zip(rel, dev) if a != b]
        self.assertEqual(len(diff), 1, "两份只该差一处，实际差 %d 处：%r" % (len(diff), diff))
        self.assertEqual(diff[0][0], 'RK_BUILDROOT_BASE_CFG="rk3568_kickpi_k1mini_release"')
        self.assertEqual(diff[0][1], 'RK_BUILDROOT_BASE_CFG="rk3568_kickpi_k1mini_dev"')

    def test_dev_board_points_at_dev_buildroot_defconfig(self):
        """RK_BUILDROOT_BASE_CFG 派生出 buildroot/configs/rockchip_<值>_defconfig。"""
        text = DEV_BOARD.read_text(encoding="utf-8")
        m = re.search(r'^RK_BUILDROOT_BASE_CFG="([^"]+)"$', text, re.M)
        self.assertIsNotNone(m)
        derived = BC / ("rockchip_%s_defconfig" % m.group(1))
        self.assertTrue(derived.is_file(), "派生出的 buildroot defconfig 不存在：%s" % derived.name)


class TestInstallerInjectsDevFlavor(unittest.TestCase):
    def test_all_three_dev_files_are_injected(self):
        """注入是逐个文件拷的：漏一个就等于没建（T15-2-11 的 NTP 就是这么丢的）。"""
        text = INSTALL.read_text(encoding="utf-8")
        for rel, dst in (
            ("image/buildroot/configs/rockchip_rk3568_kickpi_k1mini_dev_defconfig",
             "buildroot/configs/rockchip_rk3568_kickpi_k1mini_dev_defconfig"),
            ("image/buildroot/configs/rockchip/products/kickpi-k1mini-dev-assistant.config",
             "buildroot/configs/rockchip/products/kickpi-k1mini-dev-assistant.config"),
            ("image/device/rockchip/.chips/rk3566_rk3568/rockchip_rk3568_kickpi_k1mini_dev_defconfig",
             "device/rockchip/.chips/rk3566_rk3568/rockchip_rk3568_kickpi_k1mini_dev_defconfig"),
        ):
            self.assertIn(rel, text, "注入源缺 %s" % rel)
            self.assertIn(dst, text, "注入目标缺 %s" % dst)


class TestDevEntryPoints(unittest.TestCase):
    """2-12-3：入口脚本（造开发树 + 按形态构建），以及"别覆盖发行产物"。"""

    def test_make_dev_sdk_uses_hardlink_copy_with_fallback(self):
        text = MAKE_DEV_SDK.read_text(encoding="utf-8")
        self.assertIn("cp -al", text, "默认走硬链接复制（省空间 + 继承已编译产物）")
        self.assertIn("cp -a ", text, "要留一条真复制的兜底（--copy）")
        self.assertIn("install-into-sdk.sh", text, "造完树要顺带注入配方")

    def test_make_dev_sdk_has_delete_safety_rails(self):
        """脚本里有 rm -rf：删除前必须确认那是"我们的开发树"。"""
        text = MAKE_DEV_SDK.read_text(encoding="utf-8")
        self.assertIn("*-dev)", text, "目录名必须以 -dev 结尾才允许动")
        self.assertIn('"$DEV" = "/"', text)
        self.assertIn("--force", text)

    def test_build_image_supports_flavor_and_maps_to_dev_defconfig(self):
        text = BUILD_IMAGE.read_text(encoding="utf-8")
        self.assertIn("--flavor", text)
        self.assertIn('rk3566_rk3568:rockchip_rk3568_kickpi_k1mini_dev_defconfig', text)
        self.assertIn('rk3566_rk3568:rockchip_rk3568_kickpi_k1mini_release_defconfig', text)
        self.assertIn("ASSISTANT_FLAVOR=dev", text, "dev 形态要传给 post-build（保留 usb-gadget）")
        self.assertIn("ASSISTANT_FLAVOR=release", text)

    def test_dev_products_are_named_separately(self):
        """开发产物按日期命名拷出，绝不覆盖发行镜像的人工命名（b1…b11）。"""
        text = BUILD_IMAGE.read_text(encoding="utf-8")
        self.assertIn("update-assistant-dev-$STAMP.img", text)
        self.assertIn('if [ "$FLAVOR" = "dev" ]', text)

    def test_dev_tree_path_is_warned_about(self):
        """形态是 dev 但 SDK 不是 *-dev 时要提醒（两套配置会互相覆盖）。"""
        text = BUILD_IMAGE.read_text(encoding="utf-8")
        self.assertIn("*-dev)", text)

    def test_ccache_is_enabled_for_dev_only(self):
        """用户 2026-10-02 批准开 ccache；先只给开发树开（发行树开要全量重编）。"""
        dev = DEV_FRAGMENT.read_text(encoding="utf-8")
        self.assertIn("BR2_CCACHE=y", dev)
        rel = REL_BC_DEFCONFIG.read_text(encoding="utf-8")
        self.assertNotIn("BR2_CCACHE=y", rel, "发行树这轮不动它（会触发全量重编）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
