#!/usr/bin/env python3
"""
tests/test_config.py — agent/config.py 的单测

运行方式 (不需要 pytest, 也不用装任何东西):
    python tests/test_config.py
    # 或在仓库根:
    python -m unittest discover -s tests -p 'test_config.py'

覆盖:
  · 加载: 真实 .yaml 优先于 .example.yaml; 退回模板; 两者都缺时抛错
  · 保存: 只写 .yaml、不碰 .example.yaml; 自动建目录; 落盘内容可再读回
  · 缓存: 命中缓存不读盘; load_config 返回深拷贝; save_config 后缓存同步
  · 点号路径: 嵌套取值、缺失 key、中间层不是 mapping、空路径
  · 类型错误: data 非 mapping / 顶层非 mapping / YAML 非法 / 名字不合法
  · 目录穿越防护: ".."、"a/b"、绝对路径都要被拒

⚠ 每个测试都在 tmp 目录里造自己的配置, 通过 AGENT_CONFIG_DIR 指过去,
   绝不碰仓库里真实的 config/。
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# 让 tests/ 直接跑也能 import 到 agent.config (不依赖 CWD, 也不依赖装包)
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent import config as cfg  # noqa: E402


class ConfigTestBase(unittest.TestCase):
    """每个用例一个干净的临时配置目录 + 干净缓存。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agentcfg_"))
        self._old_env = os.environ.get(cfg.CONFIG_DIR_ENV)
        os.environ[cfg.CONFIG_DIR_ENV] = str(self.tmp)
        cfg.clear_cache()

    def tearDown(self):
        cfg.clear_cache()
        if self._old_env is None:
            os.environ.pop(cfg.CONFIG_DIR_ENV, None)
        else:
            os.environ[cfg.CONFIG_DIR_ENV] = self._old_env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, text):
        p = self.tmp / name
        p.write_text(text, encoding="utf-8")
        return p


# ===========================================================================
#  加载
# ===========================================================================
class TestLoad(ConfigTestBase):
    def test_load_real_yaml(self):
        self.write("config.yaml", "llm:\n  mode: board\n")
        self.assertEqual(cfg.load_config("config"), {"llm": {"mode": "board"}})

    def test_default_name_is_config(self):
        self.write("config.yaml", "a: 1\n")
        self.assertEqual(cfg.load_config(), {"a": 1})

    def test_real_yaml_wins_over_example(self):
        self.write("config.example.yaml", "which: example\n")
        self.write("config.yaml", "which: real\n")
        self.assertEqual(cfg.load_config("config")["which"], "real")

    def test_falls_back_to_example(self):
        self.write("config.example.yaml", "which: example\n")
        self.assertEqual(cfg.load_config("config")["which"], "example")

    def test_accepts_name_with_yaml_suffix(self):
        self.write("user_profile.yaml", "name: x\n")
        # "user_profile.yaml" 与 "user_profile" 应当等价, 且共用同一份缓存
        self.assertEqual(cfg.load_config("user_profile.yaml"), {"name": "x"})
        self.assertEqual(cfg.load_config("user_profile"), {"name": "x"})

    def test_empty_file_is_rejected(self):
        # 空文件几乎总是"模板没复制好"或"写盘被中断", 静默当 {} 会让故障延后暴露
        self.write("config.yaml", "")
        with self.assertRaises(cfg.ConfigError):
            cfg.load_config("config")

    def test_whitespace_only_file_is_rejected(self):
        self.write("config.yaml", "   \n\n\t\n")
        with self.assertRaises(cfg.ConfigError):
            cfg.load_config("config")

    def test_document_with_only_comments_is_rejected(self):
        self.write("config.yaml", "# just a comment\n")
        with self.assertRaises(cfg.ConfigError):
            cfg.load_config("config")

    def test_empty_real_config_falls_back_to_nothing_not_template(self):
        # 存在 .yaml 但它是空的 -> 报错, 而不是悄悄改用 .example.yaml
        self.write("config.example.yaml", "a: 1\n")
        self.write("config.yaml", "")
        with self.assertRaises(cfg.ConfigError):
            cfg.load_config("config")

    def test_nested_and_list_values(self):
        self.write(
            "user_profile.yaml",
            "name: 主人\n"
            "habits:\n"
            "  - title: standup\n"
            "    days: [mon, fri]\n"
            "  - title: report\n"
            "    days: [fri]\n",
        )
        data = cfg.load_config("user_profile")
        self.assertEqual(data["habits"][0]["title"], "standup")
        self.assertEqual(data["habits"][1]["days"], ["fri"])

    def test_unicode_roundtrip_on_load(self):
        self.write("user_profile.yaml", "name: 主人\nnotes:\n  - 早上别排会\n")
        data = cfg.load_config("user_profile")
        self.assertEqual(data["name"], "主人")
        self.assertEqual(data["notes"], ["早上别排会"])

    def test_whitelisted_name_with_no_file_raises_not_found(self):
        with self.assertRaises(cfg.ConfigNotFoundError):
            cfg.load_config("user_profile")

    def test_missing_raises_even_with_other_files_present(self):
        self.write("config.yaml", "a: 1\n")
        with self.assertRaises(cfg.ConfigNotFoundError):
            cfg.load_config("user_profile")

    def test_directory_named_like_config_is_not_a_config(self):
        (self.tmp / "config.yaml").mkdir()
        with self.assertRaises(cfg.ConfigNotFoundError):
            cfg.load_config("config")


# ===========================================================================
#  配置名白名单
# ===========================================================================
class TestWhitelist(ConfigTestBase):
    def test_allowed_names_load(self):
        for name in cfg.ALLOWED_CONFIGS:
            with self.subTest(name=name):
                self.write(name + ".yaml", "ok: 1\n")
                cfg.clear_cache()
                self.assertEqual(cfg.load_config(name), {"ok": 1})

    def test_unknown_name_rejected_even_if_file_exists(self):
        # 这是白名单相对黑名单的关键区别: 恶意/意外构造的文件名也没用
        self.write("evil.yaml", "pwned: 1\n")
        with self.assertRaises(cfg.ConfigError):
            cfg.load_config("evil")

    def test_unknown_name_rejected_for_save(self):
        with self.assertRaises(cfg.ConfigError):
            cfg.save_config("evil", {"a": 1})
        self.assertFalse((self.tmp / "evil.yaml").exists(), "不该写出未授权文件")

    def test_suffix_variants_normalize_to_same_config(self):
        for spell in ("config", "config.yaml", "config.example.yaml"):
            with self.subTest(spell=spell):
                cfg.clear_cache()
                self.write("config.yaml", "v: 1\n")
                self.assertEqual(cfg.load_config(spell), {"v": 1})

    def test_writes_only_to_real_name(self):
        cfg.save_config("config.example.yaml", {"v": 1})
        self.assertTrue((self.tmp / "config.yaml").is_file())
        self.assertFalse((self.tmp / "config.example.yaml").exists())

    def test_case_sensitive(self):
        self.write("Config.yaml", "v: 1\n")
        with self.assertRaises(cfg.ConfigError):
            cfg.load_config("Config")

    def test_message_lists_allowed_names(self):
        try:
            cfg.load_config("nope")
        except cfg.ConfigError as exc:
            for name in cfg.ALLOWED_CONFIGS:
                self.assertIn(name, str(exc))
        else:
            self.fail("应当抛 ConfigError")

    def test_whitelist_covers_shipped_examples(self):
        # 每个白名单条目都应当有对应的模板文件, 否则是漏加模板
        example_dir = _PROJECT_ROOT / "config"
        for name in cfg.ALLOWED_CONFIGS:
            with self.subTest(name=name):
                self.assertTrue(
                    (example_dir / (name + ".example.yaml")).is_file(),
                    "缺少 config/%s.example.yaml" % name,
                )


# ===========================================================================
#  保存
# ===========================================================================
class TestSave(ConfigTestBase):
    def test_save_then_load(self):
        cfg.save_config("config", {"llm": {"mode": "remote"}, "n": 3})
        self.assertEqual(cfg.load_config("config"), {"llm": {"mode": "remote"}, "n": 3})

    def test_save_writes_real_yaml_only(self):
        example = self.write("config.example.yaml", "template: keep-me\n")
        cfg.save_config("config", {"v": 1})
        # 真实文件写出来了
        self.assertTrue((self.tmp / "config.yaml").is_file())
        # 模板一个字节都没动
        self.assertEqual(example.read_text(encoding="utf-8"), "template: keep-me\n")

    def test_save_creates_missing_directory(self):
        nested = self.tmp / "deep" / "deeper"
        os.environ[cfg.CONFIG_DIR_ENV] = str(nested)
        cfg.save_config("config", {"a": 1})
        self.assertTrue((nested / "config.yaml").is_file())

    def test_save_overwrites_previous_content(self):
        cfg.save_config("config", {"a": 1, "b": 2})
        cfg.save_config("config", {"c": 3})
        self.assertEqual(cfg.load_config("config"), {"c": 3})

    def test_save_accepts_name_with_suffix(self):
        cfg.save_config("user_profile.yaml", {"tz": "UTC"})
        self.assertTrue((self.tmp / "user_profile.yaml").is_file())
        self.assertEqual(cfg.load_config("user_profile"), {"tz": "UTC"})

    def test_save_unicode_is_readable(self):
        cfg.save_config("user_profile", {"name": "主人"})
        raw = (self.tmp / "user_profile.yaml").read_text(encoding="utf-8")
        self.assertIn("主人", raw)  # allow_unicode=True: 不是 \u4e3b\u4eba
        self.assertEqual(cfg.load_config("user_profile")["name"], "主人")

    def test_save_preserves_key_order(self):
        cfg.save_config("config", {"z": 1, "a": 2, "m": 3})
        raw = (self.tmp / "config.yaml").read_text(encoding="utf-8")
        self.assertLess(raw.index("z:"), raw.index("a:"))
        self.assertLess(raw.index("a:"), raw.index("m:"))

    def test_save_leaves_no_temp_files(self):
        cfg.save_config("config", {"a": 1})
        leftovers = [p.name for p in self.tmp.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_write_text_atomic_does_not_translate_line_endings(self):
        """⚠ T12-5 板端实测抓到的：文本模式会把 `\\n` 翻成 `os.linesep`。

        同一个 CRLF 文本要是被翻译一次，在 Linux 上就变成 LF、在 Windows 上变成 `\\r\\r\\n` ——
        而"文本级改配置"承诺的是"除了目标那几行，其余**字节**不变"。所以这一层必须逐字节写。
        """
        target = self.tmp / "crlf.yaml"
        cfg.write_text_atomic(target, "a: 1\r\nb: 2\r\n")
        self.assertEqual(target.read_bytes(), b"a: 1\r\nb: 2\r\n")
        cfg.write_text_atomic(target, "a: 1\nb: 2\n")
        self.assertEqual(target.read_bytes(), b"a: 1\nb: 2\n")

    def test_temp_file_is_created_in_target_directory(self):
        # 临时文件必须与目标同目录: 否则 os.replace 跨设备会抛 OSError(EXDEV),
        # 原子换入就退化成"复制+删除"。这里通过挂在 mkstemp 上的探针确认 dir 参数。
        import agent.config as mod

        seen = {}
        real_mkstemp = mod.tempfile.mkstemp

        def probe(*args, **kwargs):
            seen["dir"] = kwargs.get("dir")
            return real_mkstemp(*args, **kwargs)

        mod.tempfile.mkstemp = probe
        try:
            cfg.save_config("config", {"a": 1})
        finally:
            mod.tempfile.mkstemp = real_mkstemp

        self.assertIsNotNone(seen.get("dir"), "mkstemp 必须显式传 dir=")
        self.assertEqual(Path(seen["dir"]).resolve(), self.tmp.resolve(),
                         "临时文件目录必须是目标文件所在目录")

    def test_temp_file_removed_when_write_fails(self):
        # 让写盘在中途失败, 临时文件不能留在 config/ 里
        import agent.config as mod

        real_dump = mod.yaml.safe_dump

        def boom(*args, **kwargs):
            raise RuntimeError("boom")

        mod.yaml.safe_dump = boom
        try:
            with self.assertRaises(RuntimeError):
                cfg.save_config("config", {"a": 1})
        finally:
            mod.yaml.safe_dump = real_dump

        leftovers = sorted(p.name for p in self.tmp.iterdir())
        self.assertEqual(leftovers, [], "失败后不该残留任何文件")

    def test_save_then_save_again_is_stable(self):
        data = {"llm": {"mode": "board", "temperature": 0.7}, "l": [1, 2, 3]}
        cfg.save_config("config", data)
        first = (self.tmp / "config.yaml").read_text(encoding="utf-8")
        cfg.save_config("config", data)
        second = (self.tmp / "config.yaml").read_text(encoding="utf-8")
        self.assertEqual(first, second)

    def test_save_does_not_mutate_caller_dict(self):
        data = {"a": {"b": 1}}
        cfg.save_config("config", data)
        data["a"]["b"] = 999
        # 缓存里应当还是保存时的值
        self.assertEqual(cfg.get("a.b"), 1)


# ===========================================================================
#  缓存
# ===========================================================================
class TestCache(ConfigTestBase):
    def test_cache_avoids_rereading_disk(self):
        path = self.write("config.yaml", "v: 1\n")
        self.assertEqual(cfg.load_config("config")["v"], 1)

        # 绕过 config.py 直接改盘: 若不读缓存, 第二次就会看到 2
        path.write_text("v: 2\n", encoding="utf-8")
        self.assertEqual(cfg.load_config("config")["v"], 1, "第二次应从缓存来")

        # 清缓存后才看到新值
        cfg.clear_cache()
        self.assertEqual(cfg.load_config("config")["v"], 2)

    def test_cache_is_shared_across_name_spellings(self):
        path = self.write("config.yaml", "v: 1\n")
        cfg.load_config("config")
        path.write_text("v: 2\n", encoding="utf-8")
        # 带后缀的写法也应命中同一份缓存
        self.assertEqual(cfg.load_config("config.yaml")["v"], 1)

    def test_load_returns_deep_copy(self):
        self.write("config.yaml", "a:\n  b: 1\nl: [1, 2]\n")
        first = cfg.load_config("config")
        first["a"]["b"] = 999
        first["l"].append(3)

        second = cfg.load_config("config")
        self.assertEqual(second["a"]["b"], 1, "改返回值不应污染缓存")
        self.assertEqual(second["l"], [1, 2])

    def test_save_updates_cache(self):
        self.write("config.yaml", "v: 1\n")
        self.assertEqual(cfg.get("v"), 1)

        cfg.save_config("config", {"v": 42})
        # 关键约束: 保存后立刻 get 到新值, 且不需要清缓存
        self.assertEqual(cfg.get("v"), 42)
        self.assertEqual(cfg.load_config("config")["v"], 42)

    def test_save_updates_cache_even_if_file_changed_behind_our_back(self):
        self.write("config.yaml", "v: 1\n")
        cfg.load_config("config")
        # 模拟外部进程改了盘
        (self.tmp / "config.yaml").write_text("v: 99\n", encoding="utf-8")
        # 我们 save 之后, 缓存必须与"我们写下去的内容"一致
        cfg.save_config("config", {"v": 7})
        self.assertEqual(cfg.get("v"), 7)

    def test_separate_configs_have_separate_cache(self):
        self.write("config.yaml", "v: 1\n")
        self.write("user_profile.yaml", "v: 2\n")
        self.assertEqual(cfg.load_config("config")["v"], 1)
        self.assertEqual(cfg.load_config("user_profile")["v"], 2)

    def test_clear_cache_is_idempotent(self):
        cfg.clear_cache()
        cfg.clear_cache()
        self.write("config.yaml", "v: 1\n")
        self.assertEqual(cfg.load_config("config")["v"], 1)


# ===========================================================================
#  点号路径 get()
# ===========================================================================
class TestDotPath(ConfigTestBase):
    def setUp(self):
        super().setUp()
        self.write(
            "config.yaml",
            "llm:\n"
            "  mode: board\n"
            "  nested:\n"
            "    deep:\n"
            "      value: 7\n"
            "sunshine:\n"
            "  host: 192.168.137.1\n"
            "  port: 47989\n"
            "  flags:\n"
            "    - a\n"
            "    - b\n"
            "zero: 0\n"
            "empty_str: ''\n"
            "false_val: false\n"
            "scalar: hello\n",
        )
        cfg.clear_cache()

    def test_top_level(self):
        self.assertEqual(cfg.get("zero"), 0)

    def test_one_level(self):
        self.assertEqual(cfg.get("llm.mode"), "board")

    def test_two_levels(self):
        self.assertEqual(cfg.get("sunshine.host"), "192.168.137.1")
        self.assertEqual(cfg.get("sunshine.port"), 47989)

    def test_three_levels(self):
        self.assertEqual(cfg.get("llm.nested.deep.value"), 7)

    def test_returns_container_when_path_points_to_mapping(self):
        self.assertEqual(cfg.get("llm.nested.deep"), {"value": 7})

    def test_returns_list_when_path_points_to_list(self):
        self.assertEqual(cfg.get("sunshine.flags"), ["a", "b"])

    def test_missing_leaf_returns_default(self):
        self.assertIsNone(cfg.get("llm.nope"))
        self.assertEqual(cfg.get("llm.nope", "fallback"), "fallback")

    def test_missing_middle_returns_default(self):
        self.assertIsNone(cfg.get("nope.deeper.deepest"))
        self.assertEqual(cfg.get("nope.deeper.deepest", -1), -1)

    def test_missing_top_returns_default(self):
        self.assertEqual(cfg.get("absent", "d"), "d")

    def test_descending_into_scalar_returns_default(self):
        # scalar 是字符串, 再往下取就是错的 -> default, 不抛异常
        self.assertEqual(cfg.get("scalar.sub", "d"), "d")

    def test_descending_into_list_returns_default(self):
        self.assertEqual(cfg.get("sunshine.flags.0", "d"), "d")

    def test_falsy_values_are_returned_not_treated_as_missing(self):
        # 0 / '' / False 都是合法值, 不能被当成"没取到"
        self.assertEqual(cfg.get("zero", "d"), 0)
        self.assertEqual(cfg.get("empty_str", "d"), "")
        self.assertIs(cfg.get("false_val", "d"), False)

    def test_empty_key_returns_default(self):
        self.assertEqual(cfg.get("", "d"), "d")

    def test_key_with_empty_segment_returns_default(self):
        self.assertEqual(cfg.get("llm..mode", "d"), "d")
        self.assertEqual(cfg.get("llm.", "d"), "d")
        self.assertEqual(cfg.get(".llm", "d"), "d")

    def test_non_str_key_returns_default(self):
        self.assertEqual(cfg.get(None, "d"), "d")
        self.assertEqual(cfg.get(123, "d"), "d")

    def test_default_none_when_config_missing_entirely(self):
        # 配置文件一个都没有时, get 不该炸, 而是给 default
        (self.tmp / "config.yaml").unlink()
        cfg.clear_cache()
        self.assertIsNone(cfg.get("anything"))
        self.assertEqual(cfg.get("anything", "d"), "d")

    def test_get_after_save(self):
        cfg.save_config("config", {"sunshine": {"host": "10.0.0.2"}})
        self.assertEqual(cfg.get("sunshine.host"), "10.0.0.2")

    def test_get_reads_only_default_config(self):
        # 只有 schedule.yaml, 没有 config.* -> get 用不上它
        (self.tmp / "config.yaml").unlink()
        self.write("schedule.yaml", "tz: UTC\n")
        cfg.clear_cache()
        self.assertEqual(cfg.get("tz", "d"), "d")


# ===========================================================================
#  类型错误 / 非法输入
# ===========================================================================
class TestTypeErrors(ConfigTestBase):
    def test_save_rejects_non_mapping(self):
        for bad in ([1, 2], "text", 3, None, 1.5):
            with self.subTest(bad=bad):
                with self.assertRaises(cfg.ConfigError):
                    cfg.save_config("config", bad)

    def test_save_rejects_non_mapping_is_also_valueerror(self):
        # ConfigError 继承 ValueError, 调用方两种写法都能用
        with self.assertRaises(ValueError):
            cfg.save_config("config", [1])

    def test_top_level_list_is_rejected(self):
        self.write("config.yaml", "- a\n- b\n")
        with self.assertRaises(cfg.ConfigError):
            cfg.load_config("config")

    def test_top_level_scalar_is_rejected(self):
        self.write("config.yaml", "just a string\n")
        with self.assertRaises(cfg.ConfigError):
            cfg.load_config("config")

    def test_invalid_yaml_is_rejected(self):
        self.write("config.yaml", "a: [1, 2\n")  # 括号没闭合
        with self.assertRaises(cfg.ConfigError):
            cfg.load_config("config")

    def test_tab_indentation_yaml_is_rejected(self):
        self.write("config.yaml", "a:\n\tb: 1\n")
        with self.assertRaises(cfg.ConfigError):
            cfg.load_config("config")

    def test_non_str_name_rejected(self):
        for bad in (None, 123, [1]):
            with self.subTest(bad=bad):
                with self.assertRaises(cfg.ConfigError):
                    cfg.load_config(bad)
        for bad in (None, 123):
            with self.subTest(bad=bad):
                with self.assertRaises(cfg.ConfigError):
                    cfg.save_config(bad, {})

    def test_empty_name_rejected(self):
        with self.assertRaises(cfg.ConfigError):
            cfg.load_config("")
        with self.assertRaises(cfg.ConfigError):
            cfg.load_config("   ")
        with self.assertRaises(cfg.ConfigError):
            cfg.save_config("", {})

    def test_name_with_only_suffix_rejected(self):
        with self.assertRaises(cfg.ConfigError):
            cfg.load_config(".yaml")

    def test_path_traversal_rejected(self):
        # 白名单顺带免疫目录穿越: 这些名字都不在 ALLOWED_CONFIGS 里
        for bad in ("../secret", "../../config", "a/b", "a\\b", "..", "."):
            with self.subTest(bad=bad):
                with self.assertRaises(cfg.ConfigError):
                    cfg.load_config(bad)
                with self.assertRaises(cfg.ConfigError):
                    cfg.save_config(bad, {})

    def test_absolute_path_rejected_as_config_name(self):
        for bad in ("/etc/passwd", "C:/x", "C:\\x"):
            with self.subTest(bad=bad):
                with self.assertRaises(cfg.ConfigError):
                    cfg.load_config(bad)
                with self.assertRaises(cfg.ConfigError):
                    cfg.save_config(bad, {})

    def test_error_is_a_valueerror(self):
        # 契约: ConfigError 继承 ValueError, 所以两种 except 写法都成立
        self.assertTrue(issubclass(cfg.ConfigError, ValueError))
        self.assertTrue(issubclass(cfg.ConfigNotFoundError, cfg.ConfigError))
        with self.assertRaises(ValueError):
            cfg.save_config("config", [1])
        with self.assertRaises(ValueError):
            cfg.load_config("not_whitelisted")
        with self.assertRaises(ValueError):
            cfg.load_config("user_profile")  # 白名单内, 但本测试的临时目录里没有它


# ===========================================================================
#  目录解析
# ===========================================================================
class TestConfigDir(ConfigTestBase):
    def test_env_var_overrides_dir(self):
        self.assertEqual(cfg.config_dir(), self.tmp)

    def test_default_dir_is_repo_config(self):
        os.environ.pop(cfg.CONFIG_DIR_ENV, None)
        self.assertEqual(cfg.config_dir(), _PROJECT_ROOT / "config")

    def test_config_path_reports_example_when_real_missing(self):
        p = self.write("config.example.yaml", "a: 1\n")
        self.assertEqual(cfg.config_path("config"), p)

    def test_config_path_prefers_real(self):
        self.write("config.example.yaml", "a: 1\n")
        real = self.write("config.yaml", "a: 2\n")
        self.assertEqual(cfg.config_path("config"), real)

    def test_config_path_raises_when_missing(self):
        # 白名单内但没有文件 -> NotFound (而不是"名字非法")
        with self.assertRaises(cfg.ConfigNotFoundError):
            cfg.config_path("user_profile")


# ===========================================================================
#  与仓库里真实模板的一致性 (回归保护)
# ===========================================================================
class TestShippedExamples(unittest.TestCase):
    """仓库自带的 *.example.yaml 必须能被 load_config 正常读出来。"""

    def setUp(self):
        self._old_env = os.environ.get(cfg.CONFIG_DIR_ENV)
        os.environ.pop(cfg.CONFIG_DIR_ENV, None)
        cfg.clear_cache()

    def tearDown(self):
        cfg.clear_cache()
        if self._old_env is not None:
            os.environ[cfg.CONFIG_DIR_ENV] = self._old_env

    def test_examples_exist_and_load(self):
        for name in ("config", "user_profile"):
            with self.subTest(name=name):
                data = cfg.load_config(name)
                self.assertIsInstance(data, dict)
                self.assertTrue(data, "%s.example.yaml 不该是空的" % name)

    def test_config_example_has_expected_sections(self):
        # ⚠ 这里必须**显式读 config.example.yaml**, 不能走 load_config("config"):
        #   后者在存在真实 config/config.yaml 时会优先返回**它** —— 板端就是这样,
        #   于是这个名字里写着 "example" 的测试实际在测运维手里的 live 配置,
        #   只要那份配置旧一点(比如还留着 zmq 的 pub_bind)就会红, 而且红得莫名其妙。
        #   (PC 上因为真实 config 被 gitignore、根本没这个文件, 所以永远看不到)
        example = cfg.config_dir() / "config.example.yaml"
        self.assertTrue(example.is_file(), "缺少 config.example.yaml: %s" % example)
        data = cfg._read_yaml(example)

        for section in ("llm", "sunshine", "ipc"):
            self.assertIn(section, data)
        self.assertIn("mode", data["llm"])
        self.assertIn("host", data["sunshine"])
        self.assertIn("socket_path", data["ipc"])

    def test_falls_back_to_example_when_no_real_config(self):
        # 仓库里没有 config.yaml 时 (刚 clone), 应当读到 example
        real = _PROJECT_ROOT / "config" / "config.yaml"
        if real.is_file():
            self.skipTest("本机存在真实 config.yaml, 跳过回退断言")
        self.assertEqual(cfg.config_path("config").name, "config.example.yaml")


if __name__ == "__main__":
    unittest.main(verbosity=2)
