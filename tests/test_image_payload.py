#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_image_payload.py — payload 清单守卫（T15-2-10b-1）

T15-2-10 的结论里最扎人的一条是 **F5：payload 9/9 未做** —— unit 文件早就
写好了 `ExecStart=/usr/bin/python3 -m agent.main`、`ExecStart=/usr/lib/assistant/gui/agent_gui`，
而 rootfs 里那些东西一个都没有。构建全绿、板子起来后服务一直重启。

这个文件把"**清单不许与真实意图漂**"钉进 CI，三方互校：

    unit 文件（ExecStart / Documentation / assistant-init 的 cp 源）
        ↕  必须被  image/check-runtime-deps.py 的 PAYLOAD 表盯着
        ↕  必须由    image/payload.manifest 真的装出来
        ↕           image/build-payload.sh 按清单干活

任何一侧改了而另一侧没跟上（改路径、加文件、漏装），这里当场红 ——
不必等到刷板后看 `systemctl status` 才发现。
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
import unittest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHECKER = _ROOT / "image" / "check-runtime-deps.py"
_MANIFEST = _ROOT / "image" / "payload.manifest"
_BUILDER = _ROOT / "image" / "build-payload.sh"
_IMG_UNITS = _ROOT / "systemd" / "image"

#: payload 表允许的类别（新增一类要同时想清楚"它落在哪、谁装它"）
KINDS = ("agent", "gui", "native", "config", "doc", "bin", "profile")

ASSISTANT_ROOT = "usr/lib/assistant/"


def load_checker():
    spec = importlib.util.spec_from_file_location("check_runtime_deps_payload", str(_CHECKER))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def parse_manifest(text: str) -> list:
    """`how|src|dest` → [(how, src, dest)]（`#` 注释与空行忽略，dest 去尾注释）。"""
    rows = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 3:
            raise AssertionError("manifest 行格式应为 how|src|dest：%r" % raw)
        how, src, dest = parts
        dest = dest.split("#", 1)[0].strip()
        rows.append((how, src, dest))
    return rows


def unit_unit_paths(units_dir: pathlib.Path) -> set:
    """unit 文件里指向 rootfs 的路径（ExecStart/ExecStartPre/Documentation）。"""
    found = set()
    for p in sorted(units_dir.glob("*.service")):
        text = p.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key in ("ExecStart", "ExecStartPre"):
                # 模型：ExecStart=/usr/bin/python3 -m agent.main --config config/config.yaml
                #       ExecStart=/usr/lib/assistant/gui/agent_gui --config config/config.yaml
                exe = value.split()[0]
                if exe.startswith("/usr/lib/assistant/"):
                    found.add(exe.lstrip("/"))
            elif key == "Documentation":
                for token in value.split():
                    if token.startswith("file:///usr/lib/assistant/"):
                        found.add(token[len("file:///"):])
    return found


def init_service_inputs(units_dir: pathlib.Path) -> set:
    """assistant-init.service 里 `cp -n "/usr/lib/assistant/config/$f.example.yaml"` 的源。

    ⚠ 那个单元里是个 `for f in config user_profile` 循环 + `$f` 变量，所以要把
    循环列表展开 —— 第一版直接抓引号里的字符串，抓到的是字面量
    `usr/lib/assistant/config/$f.example.yaml`，用例自己假失败了。
    """
    text = (units_dir / "assistant-init.service").read_text(encoding="utf-8", errors="replace")
    names = []
    m = re.search(r"for\s+\w+\s+in\s+([^;]+);", text)
    if m:
        names = m.group(1).split()
    found = set()
    for raw in re.finditer(r'"(/usr/lib/assistant/[^"]+)"', text):
        p = raw.group(1)
        if "$" in p:
            for n in names:
                found.add(re.sub(r"\$\w+", n, p).lstrip("/"))
        else:
            found.add(p.lstrip("/"))
    return found


class TestPayloadManifestMatchesTheChecker(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_checker()
        cls.rows = parse_manifest(_MANIFEST.read_text(encoding="utf-8"))
        cls.dests = {dest for _h, _s, dest in cls.rows}
        cls.payload = {rel: (kind, why) for kind, rel, why in cls.mod.PAYLOAD}

    def _covered(self, rel: str) -> bool:
        """清单里有没有条目会产出这个路径（目录条目按前缀覆盖）。"""
        for dest in self.dests:
            if dest.endswith("/"):
                if rel.startswith(dest):
                    return True
            elif dest == rel:
                return True
        return False

    def test_every_watched_path_is_produced_by_the_manifest(self):
        missing = sorted(rel for rel in self.payload if not self._covered(rel))
        self.assertEqual(missing, [],
                         "检查器盯着这些路径，但 payload.manifest 里没有条目产出它们：%r" % missing)

    def test_manifest_installs_nothing_unwatched(self):
        """反向：清单装的东西也必须有人在盯 —— 否则"装进去了但没人验"会悄悄长出来。"""
        extra = []
        for dest in sorted(self.dests):
            if dest.endswith("/"):
                if not any(rel.startswith(dest) for rel in self.payload):
                    extra.append(dest)
            elif dest not in self.payload:
                extra.append(dest)
        self.assertEqual(extra, [],
                         "payload.manifest 里这些落点没有任何 PAYLOAD 项盯着：%r" % extra)

    def test_kinds_are_known_and_reasons_present(self):
        for rel, (kind, why) in self.payload.items():
            self.assertIn(kind, KINDS, "未知的 payload 类别: %s（%s）" % (kind, rel))
            self.assertTrue(why, "%s 缺少 why（检查器要拿它解释「为什么盯着这条」）" % rel)

    def test_manifest_hows_are_known(self):
        for how, src, dest in self.rows:
            self.assertIn(how, ("copy", "gen", "native", "gui"), "未知的 how: %s" % how)
            self.assertTrue(src and dest, "src/dest 不该为空: %r" % ((how, src, dest),))
            self.assertFalse(dest.startswith("/"), "dest 应为 target 相对路径: %s" % dest)

    def test_builder_reads_the_manifest_and_takes_a_target(self):
        """build-payload.sh 必须**只认显式落点**（T15-2-10 的教训：猜树会被静默装错）。"""
        text = _BUILDER.read_text(encoding="utf-8")
        self.assertIn("payload.manifest", text)
        self.assertIn("--target", text)
        self.assertIn("TARGET_DIR", text)
        # 猜树的写法不许再出现
        self.assertNotIn("buildroot/output", text)


class TestUnitsPointAtThePayload(unittest.TestCase):
    """unit 文件引用的每个 rootfs 路径，都必须在 PAYLOAD 表里被盯着。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = load_checker()
        cls.payload = {rel for _k, rel, _w in cls.mod.PAYLOAD}
        cls.referenced = unit_unit_paths(_IMG_UNITS)

    def test_units_reference_something(self):
        self.assertTrue(self.referenced, "没从 systemd/image/*.service 里解析出任何 rootfs 路径？")

    def test_every_referenced_path_is_watched(self):
        missing = sorted(p for p in self.referenced if p not in self.payload)
        self.assertEqual(missing, [],
                         "unit 指向这些路径，但检查器的 PAYLOAD 表没盯着它们：%r" % missing)

    def test_init_service_config_sources_are_watched(self):
        for p in sorted(init_service_inputs(_IMG_UNITS)):
            self.assertIn(p, self.payload,
                          "assistant-init.service 要从 %s 复制，但 PAYLOAD 没盯着它" % p)

    def test_the_two_binaries_the_units_exec_are_watched(self):
        for p in ("usr/lib/assistant/agent/main.py",
                  "usr/lib/assistant/gui/agent_gui",
                  "usr/bin/assistant"):
            self.assertIn(p, self.payload)


class TestProfileAndUnitsAgree(unittest.TestCase):
    """profile.d 那套环境变量必须与两个单元逐条对齐（否则交互式与服务的语义会漂）。"""

    @classmethod
    def setUpClass(cls):
        cls.profile = (_ROOT / "image" / "payload" / "assistant.sh").read_text(encoding="utf-8")
        cls.wrapper = (_ROOT / "image" / "payload" / "assistant").read_text(encoding="utf-8")

    def _unit_env(self, name):
        text = (_IMG_UNITS / name).read_text(encoding="utf-8")
        env = {}
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("Environment="):
                k, _, v = line[len("Environment="):].partition("=")
                env[k.strip()] = v.strip()
        return env

    def test_profile_covers_every_d7_variable_from_agent_service(self):
        env = self._unit_env("agent.service")
        d7 = {k: v for k, v in env.items() if k != "PYTHONPATH"}
        self.assertTrue(d7, "agent.service 里应该有 D7 那几条 Environment=")
        for key, value in sorted(d7.items()):
            self.assertIn(key, self.profile, "profile.d 里少了 %s" % key)
            self.assertIn(value, self.profile,
                          "profile.d 里 %s 的默认值与 unit 不一致（unit 是 %s）" % (key, value))

    def test_profile_sets_the_same_pythonpath(self):
        self.assertEqual(self._unit_env("agent.service").get("PYTHONPATH"), "/usr/lib/assistant")
        self.assertIn("/usr/lib/assistant", self.profile)
        self.assertIn("PYTHONPATH", self.wrapper)

    def test_wrapper_execs_the_cli_module(self):
        self.assertIn("-m agent.cli", self.wrapper)
        self.assertIn("exec", self.wrapper)

    def test_both_are_posix_sh(self):
        """它们会被 POSIX shell 与 bash 都跑到 —— 不许出现 bashism。"""
        for name, text in (("profile", self.profile), ("wrapper", self.wrapper)):
            self.assertNotIn("[[", text, "%s 里出现了 bashism [[" % name)
            self.assertNotIn("function ", text, "%s 里出现了 bashism function" % name)


class TestPostBuildInstallsThePayload(unittest.TestCase):
    """T15-2-10b-5：post-build 必须按清单把 payload 装进**它自己那棵树**。

    这一组存在的理由：payload 是"unit 早就指向、但一直没装"的那批东西
    （§5.9 的 F5）。装不上不会让构建失败 —— 只会让板子起来之后服务反复重启，
    所以必须由 CI 盯着"这一步真的被调用了、而且落点传对了"。
    """

    @classmethod
    def setUpClass(cls):
        cls.post = (_ROOT / "image" / "board" / "rockchip" / "kickpi" / "k1mini"
                    / "post-build.sh").read_text(encoding="utf-8")
        cls.builder = (_ROOT / "image" / "build-payload.sh").read_text(encoding="utf-8")
        cls.installer = (_ROOT / "image" / "install-into-sdk.sh").read_text(encoding="utf-8")

    def test_post_build_calls_it_with_the_target_dir(self):
        self.assertIn("build-payload.sh", self.post)
        self.assertIn('--target "$TARGET_DIR"', self.post,
                      "post-build 调 build-payload.sh 时必须传 --target \"$TARGET_DIR\"（别猜树）")

    def test_post_build_points_at_the_injected_source_tree(self):
        self.assertIn("--src-root", self.post)
        self.assertIn("payload-src", self.post)

    def test_build_payload_runs_each_how_once(self):
        """清单里有两条 native（扩展 + moonlight 库），**不能**各跑一遍构建。

        第一版的症状：第二条把 moonlight 的落点覆盖成了扩展自己，chroot 里
        `import agent_native` 报 `undefined symbol: LiStartConnection`。
        """
        self.assertIn("native_done", self.builder)
        self.assertIn("gui_done", self.builder)

    def test_build_native_verifies_the_moonlight_symbol(self):
        """取 moonlight 库时必须**校验它定义了 LiStartConnection**。

        第一版用 `find … -name 'libmoonlight-common-c.so*' | head -1`，
        在构建目录里抓到了同名但不是那个库的文件（扩展自己）。
        """
        native = (_ROOT / "image" / "build-native.sh").read_text(encoding="utf-8")
        self.assertIn("LiStartConnection", native)
        self.assertIn("--defined-only", native)

    def test_installer_injects_the_payload_sources(self):
        self.assertIn("payload-src", self.installer)
        for needle in ("agent/", "native/", "gui/", "payload.manifest"):
            self.assertIn(needle, self.installer,
                          "install-into-sdk.sh 应把 %s 注入 payload-src" % needle)

    def test_installer_ships_the_payload_builders(self):
        for tool in ("build-payload.sh", "build-native.sh", "build-gui.sh", "payload.manifest"):
            self.assertIn("tools/assistant/" + tool, self.installer,
                          "%s 必须注入到 <SDK>/tools/assistant/" % tool)


if __name__ == "__main__":
    unittest.main(verbosity=2)
