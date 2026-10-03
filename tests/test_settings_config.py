#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`settings_config.py` + `settings_credentials.py` 的单测 —— **全离线**。

`tests/test_schedule_config.py` 的同款安排: 目标文件与模板都写到临时目录（不碰仓库那份）。

这里盯住的是"文本级写入"的全部承诺:

  · 只动**目标那一行**（其余字节不变, 用 difflib 精确比, 不靠 zip 那种会错位的比法）;
  · 注释/空行/顺序/**CRLF** 都留着;
  · 缺键 -> 按模板把带注释的键插进段里; **缺段 -> 按模板新建整段**
    （只带自己那段横幅, 不搬上/下一段的注释）;
  · 类型跟着模板走（布尔/整数/浮点/字符串）, 结构级的键不给改;
  · `.bak` 是改之前的**逐字节**副本; 值没变就一个字节都不写;
  · 凭据那条: 写 JSON + 合并 + **只认三个键** + 值不能换行。
"""
import difflib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.core import settings_config as sc                            # noqa: E402
from agent.core import settings_credentials as creds                    # noqa: E402

REPO = Path(__file__).resolve().parents[1]
TEMPLATE = REPO / "config" / "config.example.yaml"

#: 一个"最小可用"的配置（没有 study 段 —— 板端真配置就是这个状态）。
LEAN = "llm:\n  mode: disabled\n\nipc:\n  socket_path: /tmp/agent.sock\n"


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="settings-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.template = self.tmp / "config.example.yaml"
        shutil.copy2(str(TEMPLATE), str(self.template))
        self.target = self.tmp / "config.yaml"

    def write(self, text, *, name="config.yaml", newline=""):
        path = self.tmp / name
        with open(str(path), "w", encoding="utf-8", newline=newline) as handle:
            handle.write(text)
        return path

    def read(self, path=None):
        with open(str(path or self.target), encoding="utf-8", newline="") as handle:
            return handle.read()

    def full_config(self):
        shutil.copy2(str(self.template), str(self.target))
        return self.target

    def apply(self, changes, **kwargs):
        kwargs.setdefault("target", str(self.target))
        kwargs.setdefault("template", str(self.template))
        return sc.apply_changes(changes, **kwargs)

    def plan(self, changes, **kwargs):
        kwargs.setdefault("target", str(self.target))
        kwargs.setdefault("template", str(self.template))
        return sc.plan_changes(changes, **kwargs)

    def diff(self, before, after):
        return [line for line in difflib.unified_diff(before.splitlines(), after.splitlines(), n=0)
                if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))]


# ===========================================================================
#  模板: 键清单与值
# ===========================================================================
class TestTemplate(TempCase):
    def test_the_key_list_comes_from_the_template(self):
        paths = sc.known_paths(str(self.template))
        self.assertGreater(len(paths), 100, "模板里能设置的键太少了: %d" % len(paths))
        for path in ("study.relative_band", "study.enabled", "study.max_failures",
                     "bilibili.game_watch.interval_s", "profile.trigger_chars",
                     "llm.mode", "ipc.socket_path"):
            self.assertIn(path, paths, "模板里应该有 %s" % path)

    def test_mapping_entries_with_dots_are_not_addressable(self):
        """`study.process_names` 下的 `code.exe` 没法用点号路径寻址 —— 不进键清单。"""
        paths = sc.known_paths(str(self.template))
        self.assertFalse([path for path in paths if path.endswith(".exe")], paths[:5])

    def test_template_value_is_the_bare_scalar(self):
        self.assertEqual(sc.template_value("study.relative_band", str(self.template)), "0.05")
        self.assertEqual(sc.template_value("study.enabled", str(self.template)), "false")

    def test_a_missing_template_is_an_honest_error(self):
        with self.assertRaises(sc.SettingsConfigError):
            sc.known_paths(str(self.tmp / "nope.yaml"))


# ===========================================================================
#  结构级键（模板里写成 [] / {} 的那种）
# ===========================================================================
class TestStructuredKeys(TempCase):
    """`scheduler.recurring` / `wallpaper.tagging.vocab` 这类**不能**被文本级改。

    T15-4 任务 1 实测到的真问题：它们长得像"一行标量"，于是写入器原来**接受**
    `scheduler.recurring = "abc"` —— 那会把整份日程列表替换成一个字符串。
    """

    STRUCTURED = ("scheduler.recurring", "scheduler.oneoff",
                  "scheduler.commands", "wallpaper.tagging.vocab")

    def test_they_are_not_in_the_settable_key_list(self):
        paths = set(sc.known_paths(str(self.template)))
        for path in self.STRUCTURED:
            self.assertNotIn(path, paths, "%s 是结构级, 不该出现在可设置的键清单里" % path)

    def test_planning_them_is_refused_with_a_clear_reason(self):
        self.full_config()
        for path in self.STRUCTURED:
            with self.assertRaises(sc.SettingsConfigError) as caught:
                self.plan({path: "abc"})
            message = str(caught.exception)
            self.assertIn("结构级", message, message)
            self.assertIn(path, message, message)

    def test_nothing_is_written_when_refused(self):
        self.full_config()
        before = self.target.read_text(encoding="utf-8")
        with self.assertRaises(sc.SettingsConfigError):
            self.apply({"scheduler.recurring": "abc"})
        self.assertEqual(self.target.read_text(encoding="utf-8"), before,
                         "被拒的改动一个字节都不该落盘")

    def test_the_refusal_is_not_vacuous(self):
        """反空转：同一个模板里**标量键**照旧能改 —— 拒的是结构级那一类，不是全拒。"""
        self.full_config()
        plans = self.plan({"scheduler.window_min": "2"})
        self.assertEqual([plan["action"] for plan in plans], ["set"])
        with self.assertRaises(sc.SettingsConfigError):
            self.plan({"scheduler.recurring": "abc"})


# ===========================================================================
#  值的类型与渲染
# ===========================================================================
class TestRenderScalar(unittest.TestCase):
    def test_booleans(self):
        self.assertEqual(sc.render_scalar(True, like="false"), "true")
        self.assertEqual(sc.render_scalar("no", like="false"), "false")
        self.assertEqual(sc.render_scalar(0, like="true"), "false")
        with self.assertRaises(sc.SettingsConfigError):
            sc.render_scalar("maybe", like="false")

    def test_numbers(self):
        self.assertEqual(sc.render_scalar(0.07, like="0.05"), "0.07")
        self.assertEqual(sc.render_scalar("0.5", like="0.05"), "0.5")
        self.assertEqual(sc.render_scalar(30, like="30"), "30")
        with self.assertRaises(sc.SettingsConfigError):
            sc.render_scalar(30.5, like="30")            # 模板里是整数 -> 只收整数（不静默截断）
        with self.assertRaises(sc.SettingsConfigError):
            sc.render_scalar("abc", like="0.05")
        with self.assertRaises(sc.SettingsConfigError):
            sc.render_scalar(float("nan"), like="0.05")
        with self.assertRaises(sc.SettingsConfigError):
            sc.render_scalar(float("inf"), like="0.05")

    def test_strings(self):
        self.assertEqual(sc.render_scalar("config/study_anchors.jsonl",
                                          like="config/study_anchors.jsonl"),
                         "config/study_anchors.jsonl")
        self.assertEqual(sc.render_scalar("", like="qwen3-0.6b"), '""')
        self.assertEqual(sc.render_scalar("a b", like="qwen3-0.6b"), '"a b"')
        self.assertEqual(sc.render_scalar("true", like="qwen3-0.6b"), '"true"',
                         "裸词 true 会被 YAML 当布尔 -> 得加引号")
        self.assertEqual(sc.render_scalar('say "hi"', like="qwen3-0.6b"), '"say \\"hi\\""')

    def test_a_newline_in_the_value_is_refused(self):
        with self.assertRaises(sc.SettingsConfigError):
            sc.render_scalar("a\nb", like="qwen3-0.6b")


# ===========================================================================
#  改一个已存在的键
# ===========================================================================
class TestSetExisting(TempCase):
    def test_read_value(self):
        self.full_config()
        self.assertEqual(sc.read_value("study.relative_band", target=str(self.target)), "0.05")
        self.assertEqual(sc.read_value("llm.mode", target=str(self.target)), "disabled")
        self.assertIsNone(sc.read_value("study.nope", target=str(self.target)))

    def test_the_plan_names_the_line_and_the_old_value(self):
        self.full_config()
        plans = self.plan({"study.relative_band": 0.07})
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["action"], "set")
        self.assertEqual(plans[0]["old"], "0.05")
        self.assertEqual(plans[0]["new"], "0.07")
        self.assertGreater(plans[0]["line"], 1)

    def test_planning_does_not_touch_the_file(self):
        self.full_config()
        before = self.read()
        self.plan({"study.relative_band": 0.07})
        self.assertEqual(self.read(), before)

    def test_the_diff_is_exactly_one_line(self):
        """⚠ 本地冒烟抓到的真 bug: 行尾被补了两次 -> 每写一次多一个空行。"""
        self.full_config()
        before = self.read()
        result = self.apply({"study.relative_band": 0.07})
        after = self.read()
        self.assertEqual(result["changed"], 1)
        self.assertEqual(self.diff(before, after),
                         ["-  relative_band: 0.05", "+  relative_band: 0.07"])

    def test_the_inline_comment_survives(self):
        self.target = self.write(LEAN + "\nstudy:\n  enabled: false   # 开不开\n"
                                        "  relative_band: 0.05\n")
        self.apply({"study.enabled": True})
        self.assertIn("enabled: true   # 开不开", self.read())

    def test_the_backup_is_the_byte_exact_previous_state(self):
        self.full_config()
        before = self.read()
        result = self.apply({"study.relative_band": 0.07})
        self.assertTrue(result["backup"].endswith("config.yaml.bak"))
        with open(result["backup"], encoding="utf-8", newline="") as handle:
            backup = handle.read()
        self.assertEqual(backup, before)

    def test_the_same_value_writes_nothing(self):
        self.full_config()
        before = self.read()
        result = self.apply({"study.relative_band": 0.05})
        self.assertEqual(result["changed"], 0)
        self.assertEqual(result["backup"], "")
        self.assertEqual(self.read(), before)
        self.assertFalse(os.path.exists(str(self.target) + ".bak"))

    def test_crlf_is_preserved(self):
        self.full_config()
        crlf_text = self.read().replace("\n", "\r\n")        # ⚠ 先读再开写（"w" 会先把文件清空）
        with open(str(self.target), "w", encoding="utf-8", newline="") as handle:
            handle.write(crlf_text)
        self.assertGreater(crlf_text.count("\r\n"), 100, "样本文件得是 CRLF 的")
        self.apply({"study.relative_band": 0.07})
        text = self.read()
        self.assertEqual(text.count("\r\n"), text.count("\n"), "整份文件应该还是 CRLF")
        self.assertIn("relative_band: 0.07\r\n", text)
        self.assertNotIn("\r\r\n", text, "别把行尾翻译两次")

    def test_no_temp_files_are_left_behind(self):
        self.full_config()
        self.apply({"study.relative_band": 0.07})
        leftovers = [name for name in os.listdir(str(self.tmp)) if name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_an_unknown_key_is_refused(self):
        self.full_config()
        with self.assertRaises(sc.SettingsConfigError) as caught:
            self.plan({"study.nope": 1})
        self.assertIn("不认识", str(caught.exception))

    def test_a_segment_path_is_refused_with_a_pointer(self):
        self.full_config()
        with self.assertRaises(sc.SettingsConfigError) as caught:
            self.plan({"study": 1})
        self.assertIn("一整段", str(caught.exception))

    def test_the_template_itself_is_never_written(self):
        with self.assertRaises(sc.SettingsConfigError):
            sc.apply_changes({"study.relative_band": 0.07},
                             target=str(self.template), template=str(self.template))

    def test_a_missing_target_is_an_honest_error(self):
        with self.assertRaises(sc.SettingsConfigError):
            self.plan({"study.relative_band": 0.07})

    def test_several_keys_at_once(self):
        self.full_config()
        before = self.read()
        result = self.apply({"study.relative_band": 0.07, "study.focus_interval_min": 20})
        self.assertEqual(result["changed"], 2)
        diff = self.diff(before, self.read())
        self.assertEqual(len(diff), 4, diff)
        self.assertTrue(any(line.startswith("+  relative_band: 0.07") for line in diff), diff)
        self.assertTrue(any(line.startswith("-  relative_band: 0.05") for line in diff), diff)
        self.assertTrue(any(line.startswith("+  focus_interval_min: 20") for line in diff), diff)
        self.assertTrue(any(line.startswith("-  focus_interval_min: 30") for line in diff), diff)


# ===========================================================================
#  缺键 / 缺段
# ===========================================================================
class TestMissingKeyAndSegment(TempCase):
    def test_a_missing_key_is_inserted_into_the_existing_segment(self):
        self.target = self.write("study:\n  enabled: false\n")
        plans = self.plan({"study.relative_band": 0.07})
        self.assertEqual(plans[0]["action"], "add-key")
        self.apply({"study.relative_band": 0.07})
        text = self.read()
        self.assertIn("relative_band: 0.07", text)
        self.assertIn("enabled: false", text)
        self.assertIn("# ---- 判定的阈值", text, "插进去的键要带着模板的注释")

    def test_a_missing_segment_is_created_from_the_template(self):
        self.target = self.write(LEAN)
        plans = self.plan({"study.enabled": True})
        self.assertEqual(plans[0]["action"], "add-segment")
        self.apply({"study.enabled": True})
        text = self.read()
        self.assertTrue(text.startswith(LEAN), "原来那几行一个字节都不该动")
        self.assertIn("# 学习内容监督 (Phase 13 T13-5)", text)
        self.assertIn("study:\n  # 关掉它 = 不做学习监督", text)
        self.assertIn("relative_band: 0.05", text)
        self.assertIn("bilibili.exe: anime", text)
        self.assertTrue(text.endswith("\n"))

    def test_the_new_segment_does_not_steal_the_neighbours_comments(self):
        """⚠ 本地冒烟抓到的真 bug: 把上一段的尾巴与下一段的横幅一起搬过来了。"""
        self.target = self.write(LEAN)
        self.apply({"study.enabled": True})
        text = self.read()
        self.assertNotIn("音乐与认游戏问的是**同一台 PC**", text, "上一段（bilibili）的尾巴不该跟着来")
        self.assertNotIn("壁纸 (Phase 7 T3)", text, "下一段（wallpaper）的横幅不该跟着来")

    def test_a_target_without_a_trailing_newline_still_works(self):
        self.target = self.write("llm:\n  mode: disabled", newline="")
        self.apply({"study.enabled": True})
        text = self.read()
        self.assertTrue(text.startswith("llm:\n  mode: disabled\n"))
        self.assertIn("study:", text)

    def test_creating_a_segment_is_idempotent(self):
        self.target = self.write(LEAN)
        self.apply({"study.enabled": True})
        first = self.read()
        result = self.apply({"study.enabled": True})
        self.assertEqual(result["changed"], 0)
        self.assertEqual(self.read(), first)

    def test_a_sane_segment_can_be_appended_to_an_empty_file(self):
        self.target = self.write("")
        self.apply({"study.enabled": True})
        self.assertIn("study:", self.read())


# ===========================================================================
#  与命令行那张表对齐（防"表里写了模板里没有的键"）
# ===========================================================================
class TestCliTableMatchesTemplate(unittest.TestCase):
    def test_every_settable_path_exists_in_the_template(self):
        from agent import cli

        known = set(sc.known_paths(str(TEMPLATE)))
        for group, table in cli.SET_GROUPS.items():
            for flag, path in table["numbers"].items():
                self.assertIn(path, known, "%s 的 %s -> %s 不在模板里" % (group, flag, path))
            for flag, (path, _value) in table["switches"].items():
                self.assertIn(path, known, "%s 的 %s -> %s 不在模板里" % (group, flag, path))

    def test_the_cookie_keys_are_the_three_we_accept(self):
        self.assertEqual(creds.ALLOWED_KEYS, ("SESSDATA", "bili_jct", "DedeUserID"))


# ===========================================================================
#  凭据文件
# ===========================================================================
class TestCredentials(TempCase):
    def setUp(self):
        super(TestCredentials, self).setUp()
        # ⚠ cookie_file 一定要是**绝对路径**: 相对路径按仓库根解析（与 Agent 同一条口径），
        #   测试要是写了相对路径, 就会覆盖仓库里那份**真凭据**（本地冒烟就这么干过一次）。
        self.cookie = self.tmp / "bilibili_cookie.json"
        self.config = {"bilibili": {"cookie_file": str(self.cookie)}}

    def test_writing_and_reading(self):
        path = creds.write_cookie(self.config, {"SESSDATA": "abc123"})
        self.assertEqual(path, str(self.cookie))
        self.assertEqual(creds.read_cookie(self.config), {"SESSDATA": "abc123"})
        with open(str(self.cookie), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), {"SESSDATA": "abc123"})

    def test_writing_merges_instead_of_wiping(self):
        creds.write_cookie(self.config, {"SESSDATA": "abc", "bili_jct": "jct"})
        creds.write_cookie(self.config, {"DedeUserID": "42"})
        self.assertEqual(creds.read_cookie(self.config),
                         {"SESSDATA": "abc", "bili_jct": "jct", "DedeUserID": "42"})

    def test_the_previous_file_is_backed_up(self):
        creds.write_cookie(self.config, {"SESSDATA": "old"})
        creds.write_cookie(self.config, {"SESSDATA": "new"})
        with open(str(self.cookie) + ".bak", encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), {"SESSDATA": "old"})

    def test_a_typo_in_the_key_is_refused(self):
        """⚠ 实测踩过: `SEESSDATA`（多一个 E）B 站不认（nav 回 -101）。"""
        with self.assertRaises(creds.CredentialsError):
            creds.write_cookie(self.config, {"SEESSDATA": "abc"})
        self.assertFalse(os.path.exists(str(self.cookie)))

    def test_a_newline_is_refused(self):
        with self.assertRaises(creds.CredentialsError):
            creds.write_cookie(self.config, {"SESSDATA": "a\nb"})

    def test_nothing_to_write_is_refused(self):
        with self.assertRaises(creds.CredentialsError):
            creds.write_cookie(self.config, {"SESSDATA": "  "})

    def test_clean_values_validates_without_writing(self):
        """T14-2: `clean_values()` 是**纯校验** —— IPC 处理器要"先校验后写"（真源 + 凭据
        一次写两处时, 不许出现"配置写了、凭据没写"的中间态）。"""
        cleaned = creds.clean_values({"SESSDATA": "  abc  ", "bili_jct": "", "DedeUserID": None})
        self.assertEqual(cleaned, {"SESSDATA": "abc"}, "空值 = 不改动那个键")
        self.assertFalse(os.path.exists(str(self.cookie)), "纯校验不该建文件")

    def test_clean_values_refuses_the_same_things(self):
        for values in ({"SEESSDATA": "abc"}, {"SESSDATA": "a\nb"}, {"SESSDATA": 42}):
            with self.subTest(values=values):
                with self.assertRaises(creds.CredentialsError):
                    creds.clean_values(values)
        self.assertEqual(creds.clean_values({}), {}, "一个键都不给 = 没有要写的东西，不是错误")
        self.assertEqual(creds.clean_values(None), {})

    def test_a_missing_or_broken_file_is_anonymous_not_an_error(self):
        self.assertEqual(creds.read_cookie(self.config), {})
        with open(str(self.cookie), "w", encoding="utf-8") as handle:
            handle.write("{不是 JSON")
        self.assertEqual(creds.read_cookie(self.config), {})
        with open(str(self.cookie), "w", encoding="utf-8") as handle:
            handle.write('["not", "a", "mapping"]')
        self.assertEqual(creds.read_cookie(self.config), {})

    def test_unknown_keys_in_the_file_are_ignored_when_reading(self):
        with open(str(self.cookie), "w", encoding="utf-8") as handle:
            json.dump({"SESSDATA": "abc", "旧字段": "x"}, handle, ensure_ascii=False)
        self.assertEqual(creds.read_cookie(self.config), {"SESSDATA": "abc"})

    def test_the_default_path_is_the_one_the_agent_reads(self):
        """配置里没写 cookie_file 时, 与 `bilibili_api` 的默认值同一条。"""
        from agent.net.bilibili_api import DEFAULT_COOKIE_FILE

        self.assertEqual(creds.cookie_path({}), str(REPO / DEFAULT_COOKIE_FILE))

    def test_the_mask_never_shows_the_whole_secret(self):
        from agent.cli import _mask

        self.assertEqual(_mask(""), "")
        self.assertEqual(_mask("short"), "*****")
        masked = _mask("SECRET-VALUE-1234567890")
        self.assertNotIn("SECRET-VALUE", masked)
        self.assertIn("23 位", masked)


if __name__ == "__main__":
    unittest.main(verbosity=2)
