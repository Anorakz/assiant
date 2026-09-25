#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_netease_cli.py — 在 PC 上跑 neteasecli 的那一层（Phase 7 T8-2）

跑法:
    python tests/test_netease_cli.py

被测的是 `agent/net/netease_cli.py`：板端**不写** PC 侧程序，只通过 ssh 调第三方
`neteasecli --json …`。这里用**替身 runner**覆盖：

  1) `from_config()` —— 该不该管（`music.enabled` / `music.pc_host`）、端口与 binary 的默认值
  2) `argv()` —— 拼出来的 ssh 命令行（BatchMode、ConnectTimeout、远程命令与参数）
  3) `run()` —— 把 neteasecli 的 `{"success",…}` 信封解成 `data`；混着别的行的输出也能解
  4) **错误翻译**（板端排障最需要的四类, 都要"能照做"）:
       · 连不上 PC（ssh 255 / Connection refused / 超时）
       · 认证失败（Permission denied → 提示把板端公钥放进 PC）
       · 登录态失效（AUTH_ERROR / rc=2 → 提示在 PC 上 auth login）
       · mpv 起不来（PLAYER_ERROR + ENOENT → 提示 PC 装 mpv）
  5) 各动作方法把参数拼对（play/pause/seek/volume/search/lyric/playlist…）

⚠ 不连真 PC（板端才连）—— 开发机与板端跑的是同一份断言。
"""

import json
import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.net.netease_cli import (  # noqa: E402
    DEFAULT_BINARY,
    AuthFailure,
    ConnectionFailure,
    NeteaseCli,
    NeteaseCliError,
    PlayerFailure,
    ssh_available,
)

logging.disable(logging.CRITICAL)


def envelope(success=True, data=None, error=None):
    return json.dumps({"success": success, "data": data, "error": error}, ensure_ascii=False)


class FakeRunner(object):
    """替身 runner: 记下每条 ssh 命令, 返回预置的 (rc, out, err) 或抛异常。"""

    def __init__(self, rc=0, out="", err="", exc=None):
        self.rc = rc
        self.out = out
        self.err = err
        self.exc = exc
        self.calls = []

    def __call__(self, argv, timeout_s):
        self.calls.append((list(argv), timeout_s))
        if self.exc is not None:
            raise self.exc
        return self.rc, self.out, self.err

    @property
    def last(self):
        return self.calls[-1][0]


def make_cli(**kwargs):
    runner = kwargs.pop("runner", None) or FakeRunner(
        out=envelope(True, {"playing": True, "position": 9.0}))
    cli = NeteaseCli(host="192.168.137.1", user="Anorak", runner=runner, **kwargs)
    return cli, runner


# ===========================================================================
#  1) 装配
# ===========================================================================
class TestFromConfig(unittest.TestCase):
    def test_disabled_or_unconfigured_returns_none(self):
        self.assertIsNone(NeteaseCli.from_config(None))
        self.assertIsNone(NeteaseCli.from_config({}))
        self.assertIsNone(NeteaseCli.from_config({"music": {}}))
        self.assertIsNone(NeteaseCli.from_config({"music": {"enabled": True}}),
                          "没有 pc_host 就没法连")

    def test_enabled_with_host(self):
        cli = NeteaseCli.from_config({"music": {"enabled": True, "pc_host": "10.0.0.2",
                                                "pc_user": "me", "pc_port": 2200}})
        self.assertIsNotNone(cli)
        self.assertEqual((cli.host, cli.user, cli.port), ("10.0.0.2", "me", 2200))

    def test_string_truthy_and_defaults(self):
        cli = NeteaseCli.from_config({"music": {"enabled": "true", "pc_host": "10.0.0.2"}})
        self.assertIsNotNone(cli)
        self.assertEqual(cli.port, 22)
        self.assertEqual(cli.binary, DEFAULT_BINARY)
        self.assertIsNone(NeteaseCli.from_config(
            {"music": {"enabled": "false", "pc_host": "10.0.0.2"}}))

    def test_ssh_available_is_true_on_both_machines(self):
        # 板端与开发机都该有 ssh 客户端; 没有的话错误信息会提示装
        self.assertTrue(ssh_available(), "这台机器没有 ssh 客户端?")

    def test_repr(self):
        cli, _ = make_cli()
        self.assertIn("Anorak@192.168.137.1", repr(cli))


# ===========================================================================
#  2) 拼命令行
# ===========================================================================
class TestArgv(unittest.TestCase):
    def test_batch_mode_and_connect_timeout_are_mandatory(self):
        cli, _ = make_cli()
        argv = cli.argv(["player", "status"])
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("ConnectTimeout=6", argv)
        self.assertEqual(argv[-1], "neteasecli player status")

    def test_port_and_target(self):
        cli, _ = make_cli(port=2200)
        argv = cli.argv(["auth", "check"])
        self.assertEqual(argv[argv.index("-p") + 1], "2200")
        self.assertIn("Anorak@192.168.137.1", argv)

    def test_arguments_with_spaces_are_quoted(self):
        cli, _ = make_cli()
        self.assertTrue(cli.argv(["search", "track", "Jane Doe"])[-1].endswith('"Jane Doe"'))

    def test_plain_arguments_are_not_quoted(self):
        cli, _ = make_cli()
        self.assertEqual(cli.argv(["track", "play", "2747166493"])[-1],
                         "neteasecli track play 2747166493")


# ===========================================================================
#  3) 解信封
# ===========================================================================
class TestRun(unittest.TestCase):
    def test_success_returns_data(self):
        cli, runner = make_cli()
        data = cli.status()
        self.assertEqual(data["position"], 9.0)
        self.assertIn("player status", runner.last[-1])

    def test_json_with_noise_around_it_is_still_parsed(self):
        runner = FakeRunner(out="mpv: some log line\n" + envelope(True, {"ok": 1}) + "\n")
        cli = NeteaseCli(host="h", user="u", runner=runner)
        self.assertEqual(cli.run("player", "status"), {"ok": 1})

    def test_missing_host_is_reported(self):
        cli = NeteaseCli(host="", user="u")
        with self.assertRaises(ConnectionFailure) as ctx:
            cli.run("player", "status")
        self.assertIn("pc_host", str(ctx.exception))

    def test_missing_ssh_binary_says_what_to_install(self):
        cli = NeteaseCli(host="h", user="u", runner=FakeRunner(exc=FileNotFoundError("ssh")))
        with self.assertRaises(ConnectionFailure) as ctx:
            cli.run("player", "status")
        self.assertIn("openssh-client", str(ctx.exception))

    def test_timeout_is_reported(self):
        import subprocess

        cli = NeteaseCli(host="h", user="u",
                         runner=FakeRunner(exc=subprocess.TimeoutExpired("ssh", 45)))
        with self.assertRaises(ConnectionFailure) as ctx:
            cli.run("player", "status")
        self.assertIn("超过", str(ctx.exception))

    def test_unreadable_output_is_reported_verbatim(self):
        cli = NeteaseCli(host="h", user="u", runner=FakeRunner(rc=1, out="??? nobody here"))
        with self.assertRaises(NeteaseCliError) as ctx:
            cli.run("player", "status")
        self.assertIn("看不懂", str(ctx.exception))


# ===========================================================================
#  4) 错误翻译
# ===========================================================================
class TestErrorTranslation(unittest.TestCase):
    def test_permission_denied_points_at_the_public_key(self):
        runner = FakeRunner(rc=255, err="Anorak@192.168.137.1: Permission denied (publickey)")
        cli = NeteaseCli(host="192.168.137.1", user="Anorak", runner=runner)
        with self.assertRaises(AuthFailure) as ctx:
            cli.status()
        message = str(ctx.exception)
        self.assertIn("administrators_authorized_keys", message)
        # 板端会把**真公钥内容**写进错误里（方便直接复制）; 开发机上文件不在
        # （那台机器没有 /root/.ssh/id_ed25519.pub），退化成给路径 —— 两种都算合格
        self.assertTrue("ssh-ed25519" in message or "id_ed25519.pub" in message,
                        "要把该放的公钥（或它的路径）直接写在错误里: %s" % message)

    def test_connection_refused_says_check_pc_and_sshd(self):
        runner = FakeRunner(rc=255, err="ssh: connect to host 192.168.137.1 port 22: Connection refused")
        cli = NeteaseCli(host="192.168.137.1", user="Anorak", runner=runner)
        with self.assertRaises(ConnectionFailure) as ctx:
            cli.status()
        self.assertIn("OpenSSH", str(ctx.exception))

    def test_the_ssh_reason_is_not_said_twice(self):
        """ssh 失败时信封里的 message 就是 stderr 原文 —— 直接拼会说两遍（板端实测）。"""
        err = "ssh: connect to host 192.168.137.1 port 22: Connection refused"
        runner = FakeRunner(rc=255, out=envelope(False, None,
                                                 {"code": "SSH", "message": err}), err=err)
        cli = NeteaseCli(host="192.168.137.1", user="Anorak", runner=runner)
        with self.assertRaises(ConnectionFailure) as ctx:
            cli.status()
        self.assertEqual(str(ctx.exception).count("Connection refused"), 1)

    def test_auth_error_from_neteasecli_says_where_to_relogin(self):
        runner = FakeRunner(rc=2, out=envelope(False, None,
                                               {"code": "AUTH_ERROR", "message": "not logged in"}))
        cli = NeteaseCli(host="h", user="u", runner=runner)
        with self.assertRaises(AuthFailure) as ctx:
            cli.status()
        self.assertIn("auth login", str(ctx.exception))
        self.assertIn("PC", str(ctx.exception))

    def test_player_error_about_mpv_says_install_mpv(self):
        runner = FakeRunner(rc=1, out=envelope(False, None, {
            "code": "PLAYER_ERROR", "message": "Failed to start mpv: spawn mpv ENOENT"}))
        cli = NeteaseCli(host="h", user="u", runner=runner)
        with self.assertRaises(PlayerFailure) as ctx:
            cli.play("1")
        self.assertIn("mpv", str(ctx.exception))

    def test_network_exit_code_is_a_connection_problem(self):
        runner = FakeRunner(rc=3, out=envelope(False, None,
                                               {"code": "NETWORK", "message": "timeout"}))
        cli = NeteaseCli(host="h", user="u", runner=runner)
        with self.assertRaises(ConnectionFailure):
            cli.search("track", "x")

    def test_unknown_error_keeps_the_message(self):
        runner = FakeRunner(rc=1, out=envelope(False, None,
                                               {"code": "WEIRD", "message": "怪错误"}))
        cli = NeteaseCli(host="h", user="u", runner=runner)
        with self.assertRaises(NeteaseCliError) as ctx:
            cli.status()
        self.assertIn("怪错误", str(ctx.exception))

    def test_vip_track_error_is_not_a_network_problem(self):
        """T8-7 板端实测: "这首要不到地址"的**退出码是 3**, 但 error_code 是 TRACK_ERROR。

        按退出码翻就成了"PC 那边网络请求失败" —— 看日志/看回话都指错方向。
        """
        runner = FakeRunner(rc=3, out=envelope(False, None, error={
            "code": "TRACK_ERROR",
            "message": "Track unavailable (no copyright or VIP required)"}))
        cli = NeteaseCli(host="h", user="u", runner=runner)
        with self.assertRaises(NeteaseCliError) as ctx:
            cli.run("track", "url", "186016")
        self.assertNotIsInstance(ctx.exception, ConnectionFailure,
                                 "版权/VIP 不是网络问题")
        self.assertIn("VIP", str(ctx.exception))
        self.assertIn("要不到播放地址", str(ctx.exception))

    def test_errors_are_all_valueerror_free_runtimeerrors(self):
        # 与仓库里其它错误家族一致: 能精确 except, 也能统一 except RuntimeError
        for cls in (NeteaseCliError, ConnectionFailure, AuthFailure, PlayerFailure):
            self.assertTrue(issubclass(cls, RuntimeError), cls)


# ===========================================================================
#  5) 动作方法拼参数
# ===========================================================================
class TestPlayGoesThroughTheInteractiveSession(unittest.TestCase):
    """播放必须落到**交互会话**（T8-1 实测）。

    背景: Windows 的 sshd 是服务, 它起的进程在 session 0 —— **没有音频设备**（放不出声），
    而且 ssh 会话一断子进程就被杀。实测: 直接 ssh `track play` 之后 3 s 就查不到 mpv.exe。
    所以播放走 `schtasks /it`（只在用户登录时运行 = 落到用户桌面会话）,
    而**读取与控制仍然走 ssh**（mpv 的 IPC socket 在 session 0 也够得着）。
    """

    def _cli(self, status_after="pos"):
        """runner: 按远程命令分派 —— schtasks 三条 + status 两条。"""
        calls = []

        class _Runner(object):
            def __call__(self, argv, timeout_s):
                remote = argv[-1]
                calls.append(remote)
                if "player status" in remote:
                    if status_after == "pos":
                        return 0, envelope(True, {"playing": True, "position": 3.0,
                                                  "duration": 236.0}), ""
                    return 0, envelope(True, {"playing": True, "position": 0,
                                              "duration": 0}), ""
                if remote.startswith("schtasks"):
                    return 0, "", ""
                return 0, envelope(True, {}), ""

        runner = _Runner()
        cli = NeteaseCli(host="h", user="u", runner=runner)
        return cli, runner, calls

    def test_play_uses_schtasks_interactive_not_plain_ssh(self):
        cli, _runner, calls = self._cli()
        with mock.patch("agent.net.netease_cli.time.sleep", lambda _s: None):
            result = cli.play("2747166493")
        self.assertIn("播放", result["message"])
        joined = "\n".join(calls)
        self.assertEqual([c for c in calls if c.startswith("neteasecli track play")], [],
                         "**不许**把 track play 当普通 ssh 命令直接跑"
                         "（session 0 没声音, 而且会话一断就被杀）")
        self.assertTrue(any("schtasks /create" in c and "/it" in c for c in calls),
                        "要建**交互式**计划任务（/it）")
        self.assertIn("track play 2747166493", joined, "播放命令要发给 PC 上的 neteasecli")
        self.assertTrue(any("schtasks /run" in c for c in calls))
        self.assertTrue(any("schtasks /delete" in c for c in calls),
                        "用完要删掉, 不留垃圾任务")

    def test_play_confirms_it_really_started(self):
        # duration>0 = 真的有个 mpv 在放（neteasecli 连不上 mpv 时会返回 duration=0 的乐观值）
        cli, _runner, _calls = self._cli(status_after="pos")
        with mock.patch("agent.net.netease_cli.time.sleep", lambda _s: None):
            cli.play("1")

    def test_play_fails_honestly_when_nothing_started(self):
        cli, _runner, _calls = self._cli(status_after="zero")
        with mock.patch("agent.net.netease_cli.time.sleep", lambda _s: None):
            with self.assertRaises(PlayerFailure) as ctx:
                cli.play("1")
        message = str(ctx.exception)
        self.assertIn("没人登录", message, "要把「PC 上没人登录桌面」这条最常见原因说清")
        self.assertIn("mpv", message)

    def test_quality_is_forwarded_into_the_task_command(self):
        cli, _runner, calls = self._cli()
        with mock.patch("agent.net.netease_cli.time.sleep", lambda _s: None):
            cli.play("1", quality="lossless")
        create = [c for c in calls if "schtasks /create" in c][0]
        self.assertIn("track play 1 -q lossless", create)

    def test_confirm_false_skips_the_check(self):
        cli, runner, calls = self._cli(status_after="zero")
        with mock.patch("agent.net.netease_cli.time.sleep", lambda _s: None):
            result = cli.play("1", confirm=False)
        self.assertIn("请求播放", result["message"])
        self.assertEqual([c for c in calls if "player status" in c], [])

    def test_a_vip_track_is_refused_before_touching_the_player(self):
        """T8-7 板端实测修的一处"报喜不报忧"。

        VIP/无版权那首在 PC 上**拿不到播放地址**; 只看"有没有出声"的话, PC 上本来
        正放着别的歌时就会判成成功 —— 实测点《晴天》(VIP) 拿到
        `{"ok": true, "started": true}`, 而屏幕上放的还是上一首。
        """
        calls = []

        class _Runner(object):
            def __call__(self, argv, timeout_s):
                remote = argv[-1]
                calls.append(remote)
                if "track url" in remote:
                    return 0, envelope(False, None, error={
                        "code": "TRACK_ERROR",
                        "message": "Track unavailable (no copyright or VIP required)"}), ""
                return 0, envelope(True, {"playing": True, "duration": 236.0}), ""

        cli = NeteaseCli(host="h", user="u", runner=_Runner())
        with mock.patch("agent.net.netease_cli.time.sleep", lambda _s: None):
            with self.assertRaises(NeteaseCliError) as ctx:
                cli.play("186016")
        message = str(ctx.exception)
        self.assertIn("VIP", message, "要把真实原因说出来")
        self.assertIn("要不到播放地址", message)
        self.assertNotIn("网络请求失败", message, "别把版权问题报成网络问题")
        self.assertEqual([c for c in calls if "schtasks" in c], [], "别去起播")
        self.assertEqual([c for c in calls if "player stop" in c], [],
                         "还没确认能放, 不该把用户正在听的那首停掉")

    def test_a_missing_login_is_not_reported_as_a_copyright_problem(self):
        """T8-7 板端实测: **没登录**时 neteasecli 报的还是
        `Track unavailable (no copyright or VIP required)` —— 跟 VIP 那首**同一句话**。

        所以失败之后再问一句登录态; 问出来是"没登录"就给"去 PC 上重登"那句能照做的话,
        而不是让用户以为这首歌要会员。
        """
        calls = []

        class _Runner(object):
            def __call__(self, argv, timeout_s):
                remote = argv[-1]
                calls.append(remote)
                if "track url" in remote:
                    return 3, envelope(False, None, error={
                        "code": "TRACK_ERROR",
                        "message": "Track unavailable (no copyright or VIP required)"}), ""
                if "auth check" in remote:
                    return 0, envelope(True, {"valid": False, "message": "Not logged in"}), ""
                return 0, envelope(True, {}), ""

        cli = NeteaseCli(host="h", user="u", runner=_Runner())
        with mock.patch("agent.net.netease_cli.time.sleep", lambda _s: None):
            with self.assertRaises(AuthFailure) as ctx:
                cli.play("2747166493")
        message = str(ctx.exception)
        self.assertIn("auth login", message, "要给能照做的那句")
        self.assertIn("登录态失效", message)
        self.assertEqual([c for c in calls if "schtasks" in c], [], "别去起播")

    def test_a_url_check_that_cannot_reach_the_pc_stays_a_connection_error(self):
        calls = []

        class _Runner(object):
            def __call__(self, argv, timeout_s):
                calls.append(argv[-1])
                return 255, "", "ssh: connect to host h port 22: Connection refused"

        cli = NeteaseCli(host="h", user="u", runner=_Runner())
        with mock.patch("agent.net.netease_cli.time.sleep", lambda _s: None):
            with self.assertRaises(ConnectionFailure) as ctx:
                cli.play("1")
        self.assertNotIn("版权", str(ctx.exception), "连不上就是连不上, 别赖版权")

    def test_play_stops_whatever_was_playing_first(self):
        # 不然"有没有出声"分不清是新歌还是旧歌（mpv 的 IPC 是固定管道名）
        cli, _runner, calls = self._cli(status_after="pos")
        with mock.patch("agent.net.netease_cli.time.sleep", lambda _s: None):
            cli.play("1")
        stop_at = [i for i, c in enumerate(calls) if "player stop" in c]
        create_at = [i for i, c in enumerate(calls) if "schtasks /create" in c]
        self.assertTrue(stop_at, "起播前要先停下来放着的")
        self.assertTrue(create_at and stop_at[0] < create_at[0], "stop 要在起播之前")

    def test_a_failed_stop_does_not_block_the_play(self):
        class _Runner(object):
            def __call__(self, argv, timeout_s):
                remote = argv[-1]
                if "player stop" in remote:
                    return 1, envelope(False, None, error={"code": "PLAYER_ERROR",
                                                           "message": "没有播放器"}), ""
                if "player status" in remote:
                    return 0, envelope(True, {"playing": True, "duration": 236.0}), ""
                return 0, envelope(True, {}), ""

        cli = NeteaseCli(host="h", user="u", runner=_Runner())
        with mock.patch("agent.net.netease_cli.time.sleep", lambda _s: None):
            self.assertIn("播放", cli.play("1")["message"])

    def test_is_playing_uses_duration(self):
        # duration=0 是"连不上 mpv"的乐观默认值 —— 不能当成在放
        cli, _runner, _calls = self._cli(status_after="zero")
        self.assertFalse(cli.is_playing())
        cli2, _r2, _c2 = self._cli(status_after="pos")
        self.assertTrue(cli2.is_playing())

    def test_a_broken_schtasks_does_not_raise_by_itself(self):
        # schtasks 经常返回 0 却什么都没跑 —— 所以真正的判据是"有没有声音", 不是它的退出码
        class _Runner(object):
            def __call__(self, argv, timeout_s):
                if argv[-1].startswith("schtasks"):
                    return 1, "", "拒绝访问"
                return 0, envelope(True, {"duration": 0}), ""

        cli = NeteaseCli(host="h", user="u", runner=_Runner())
        with mock.patch("agent.net.netease_cli.time.sleep", lambda _s: None):
            with self.assertRaises(PlayerFailure):
                cli.play("1")


class TestActions(unittest.TestCase):
    def _last(self, call):
        cli, runner = make_cli()
        call(cli)
        return runner.last[-1]

    def test_player_commands(self):
        self.assertEqual(self._last(lambda c: c.pause()), "neteasecli player pause")
        self.assertEqual(self._last(lambda c: c.stop()), "neteasecli player stop")
        self.assertEqual(self._last(lambda c: c.seek(10)), "neteasecli player seek 10")
        self.assertEqual(self._last(lambda c: c.seek(-10)), "neteasecli player seek -10")
        self.assertEqual(self._last(lambda c: c.seek(30, absolute=True)),
                         "neteasecli player seek 30 --absolute")
        self.assertEqual(self._last(lambda c: c.set_volume(80)), "neteasecli player volume 80")
        self.assertEqual(self._last(lambda c: c.volume()), "neteasecli player volume")
        self.assertEqual(self._last(lambda c: c.status()), "neteasecli player status")

    def test_search_and_library(self):
        self.assertEqual(self._last(lambda c: c.search("track", "周杰伦", limit=5)),
                         "neteasecli search track 周杰伦 -l 5 -o 0")
        self.assertEqual(self._last(lambda c: c.playlist_detail("8503898111")),
                         "neteasecli playlist detail 8503898111")
        self.assertEqual(self._last(lambda c: c.playlist_list()), "neteasecli playlist list")
        self.assertEqual(self._last(lambda c: c.auth_check()), "neteasecli auth check")

    def test_track_detail_url_lyric(self):
        self.assertEqual(self._last(lambda c: c.track_detail("1")), "neteasecli track detail 1")
        self.assertEqual(self._last(lambda c: c.track_url("1")),
                         "neteasecli track url 1 -q exhigh")
        self.assertEqual(self._last(lambda c: c.lyric("1")), "neteasecli track lyric 1")

    def test_every_call_goes_through_ssh(self):
        cli, runner = make_cli()
        cli.status()
        cli.pause()
        self.assertEqual(len(runner.calls), 2)
        for argv, _timeout in runner.calls:
            self.assertEqual(argv[0], "ssh")


if __name__ == "__main__":
    unittest.main(verbosity=2)
