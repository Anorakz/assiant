#!/usr/bin/env python3
"""tests/test_cli_shell.py — `assistant shell` 与配置的两套权限（T15-4 任务 2）

覆盖:
  · 会话里的**权限层**: `mode root` 进 / `mode user` 降回 / `exit` 退出（含 root 层里退出）
  · user 层改 **root 级**设置项 -> 拒（退出码 2）+ **一个字节都不写**
  · 进 root 层之后同一条命令 -> 成功（并留 `.bak`）
  · euid 不是 0 -> `mode root` 进不去
  · 一条命令出错/`--show`/`--list-tiers` 不该带走会话；`shell` 里不许再 `shell`
  · `-c "mode root; …"`、非 tty 的 stdin、提示符（层级一眼可见）
  · 一次性 CLI 里 `assistant mode root` 只是**提示**（真入口是 shell）

运行:
    python3 tests/test_cli_shell.py
"""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent import cli, shell                                    # noqa: E402
from agent.config import clear_cache                            # noqa: E402
from agent.core import config_tiers                             # noqa: E402

TEMPLATE = _PROJECT_ROOT / "config" / "config.example.yaml"


class FakeTty(io.StringIO):
    """长得像终端（只为让 shell 打提示符）。"""

    def isatty(self) -> bool:
        return True


class ShellCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shell-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        for name in ("config.yaml", "config.example.yaml"):
            shutil.copy2(str(TEMPLATE), str(self.tmp / name))
        self.config = str(self.tmp / "config.yaml")
        # `load_plane_config` 会 setdefault `AGENT_CONFIG_DIR`（进程内首次生效）——
        # 所以每个用例前后都要把它与配置缓存清干净，别污染别的用例。
        self._old_dir = os.environ.get("AGENT_CONFIG_DIR")
        clear_cache()
        self.addCleanup(self._restore_env)
        # 开发机/WSL 上测试进程是普通用户（板上才是 root）—— 所以默认把 `os.geteuid`
        # 打桩成 0（= 板上的真实情形），单独那条"非 root 进不去"的用例用注入的 Session 验。
        patcher = mock.patch("agent.shell.os.geteuid", return_value=0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _restore_env(self):
        if self._old_dir is None:
            os.environ.pop("AGENT_CONFIG_DIR", None)
        else:
            os.environ["AGENT_CONFIG_DIR"] = self._old_dir
        clear_cache()

    def text(self) -> str:
        return (self.tmp / "config.yaml").read_text(encoding="utf-8")

    def drive(self, lines, *, euid=0, session=None):
        """跑几行，返回 `(退出码, stdout, stderr, 会话)`。"""
        out, err = io.StringIO(), io.StringIO()
        session = session or shell.Session(euid=euid)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = shell.run_lines(list(lines), session)
        return code, out.getvalue(), err.getvalue(), session

    def set_line(self, *flags):
        return " ".join(["set", "study", "--config", self.config] + list(flags))


class TestTierGate(ShellCase):
    def test_user_tier_refuses_a_root_key_and_writes_nothing(self):
        before = self.text()
        code, _out, err, _session = self.drive([self.set_line("--no-adapt", "--apply")])
        self.assertEqual(code, cli.EXIT_USAGE, err)
        self.assertIn("root 级", err)
        self.assertIn("mode root", err, "拒绝的话里要直接告诉人怎么进 root 体系")
        self.assertIn("study.adapt", err)
        self.assertEqual(self.text(), before, "被拒的改动一个字节都不该写")

    def test_the_same_command_works_after_mode_root(self):
        code, out, err, session = self.drive(["mode root",
                                            self.set_line("--no-adapt", "--apply")])
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertTrue(session.is_root, "会话应当已经在 root 层")
        self.assertIn("已进入 root 体系", out)
        self.assertIn("adapt: false", self.text(), "root 层里的写入应当真落盘")
        self.assertTrue((self.tmp / "config.yaml.bak").is_file(), "写入要留 .bak")

    def test_mode_user_downgrades_again(self):
        code, _out, err, session = self.drive(["mode root", "mode user",
                                             self.set_line("--no-adapt", "--apply")])
        self.assertEqual(code, cli.EXIT_USAGE, err)
        self.assertFalse(session.is_root)
        self.assertIn("adapt: true", self.text(), "降回 user 层之后不该再写进去")

    def test_mode_root_is_refused_without_root_privileges(self):
        code, _out, err, session = self.drive(["mode root"], euid=1000)
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertFalse(session.is_root, "非 root 进不去 root 层")
        self.assertIn("euid=1000", err)

    def test_a_user_key_needs_no_privilege(self):
        """反空转：user 级的键在 user 层就该能改（不是"什么都要 root"）。"""
        code, _out, err, _session = self.drive([self.set_line("--relative-band", "0.07", "--apply")])
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertIn("relative_band: 0.07", self.text())


class TestSessionBehaviour(ShellCase):
    def test_exit_stops_the_session(self):
        before = self.text()
        code, _out, _err, _session = self.drive(["exit", self.set_line("--no-adapt", "--apply")])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(self.text(), before, "`exit` 之后那一行不该再跑")

    def test_exit_from_root_says_so(self):
        _code, out, _err, _session = self.drive(["mode root", "exit"])
        self.assertIn("已退出 root 体系", out)

    def test_a_broken_command_does_not_kill_the_session(self):
        code, out, _err, _session = self.drive(["set nosuchgroup", self.set_line("--show")])
        self.assertEqual(code, cli.EXIT_OK, "后面那条命令应当照常跑")
        self.assertIn("study 组能改的设置项", out)

    def test_show_marks_both_tiers(self):
        code, out, err, _session = self.drive([self.set_line("--show")])
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertIn("[user]", out)
        self.assertIn("---- root 级", out)
        self.assertIn("[root]", out)
        user_at = out.index("[user]")
        root_at = out.index("[root]")
        self.assertLess(user_at, root_at, "user 那段应当排在 root 那段前面")
        self.assertIn("study.enabled", out)
        self.assertIn("study.adapt", out)

    def test_list_tiers_matches_the_module(self):
        data = config_tiers.summary()
        code, out, err, _session = self.drive(["set --list-tiers --config %s" % self.config])
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertIn("user 级 %d 个" % data["counts"]["user"], out)
        self.assertIn("root 级 %d 个" % data["counts"]["root"], out)
        self.assertIn(data["user"][0], out)
        self.assertIn(data["root"][0], out)

    def test_shell_inside_shell_is_refused(self):
        code, _out, err, _session = self.drive(["shell"])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("嵌套", err)


class TestShellEntry(ShellCase):
    def test_dash_c_runs_several_commands(self):
        code = cli.main(["shell", "-c", "mode root; %s" % self.set_line("--no-adapt", "--apply")])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("adapt: false", self.text())

    def test_dash_c_stops_at_the_first_failure(self):
        """`-c` 是脚本语义：一条失败就停（与 `set -e` 同款，可预期）。"""
        code = cli.main(["shell", "-c", "set nosuchgroup; %s" % self.set_line("--no-adapt",
                                                                             "--apply")])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("adapt: true", self.text(), "失败之后那条不该跑")

    def test_stdin_lines_are_accepted_when_not_a_tty(self):
        stream = io.StringIO("%s\nexit\n" % self.set_line("--show"))
        code = shell.main(stdin=stream)
        self.assertEqual(code, cli.EXIT_OK)

    def test_the_prompt_shows_the_tier(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = shell.main(stdin=FakeTty("mode root\nexit\n"))
        self.assertEqual(code, cli.EXIT_OK)
        printed = out.getvalue()
        self.assertIn("assistant(user)> ", printed)
        self.assertIn("assistant(root)> ", printed)

    def test_one_shot_mode_root_is_only_a_hint(self):
        """`assistant mode root` 不切权限层 —— 只提示真入口在 shell 里。"""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(["mode", "root"])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("assistant shell", err.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
