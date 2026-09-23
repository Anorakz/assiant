#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_llm_service.py — Agent 管本机 llama-server 的启停（Phase 7 T7-4）

跑法:
    python tests/test_llm_service.py

被测的是 `agent/llm/service.py::LlamaService` 与 `agent/main.py::Runtime` 的接线：

  1) `from_config()` —— **该不该管**是一条配置说清楚的规则:
     `mode=edge` 且 `llm.manage_service` 为真才返回对象；`cloud`/`disabled`、开关关着、
     开关写成字符串（"true"/"yes"）各是什么结果；端口写坏退回 9000
  2) `start()/stop()` —— 真跑 `llm/scripts/{start,stop}.sh`（这里用**替身脚本**，
     不碰真服务）: 成功、脚本不存在、脚本非零退出，三种情况下**都不抛**
  3) `is_ready()/wait_ready()` —— 探活用的是真 HTTP: 起一个假 `/v1/models`
     （200 带 data / 403 / 根本没服务），并且**只有 200 且带 data 才算就绪**
  4) Runtime 接线（真 StateMachine + 替身 service）: Agent 启动 -> 起服务;
     **进 SLEEP -> 停; 离开 SLEEP -> 起**; 开关关着时**一次都不动**

⚠ 这些用例不碰真 llama-server、不碰网络外部 —— 开发机与板端跑的是同一份。
"""

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.core.state_machine import State, StateMachine  # noqa: E402
from agent.llm.service import LlamaService  # noqa: E402

logging.disable(logging.CRITICAL)          # "不管/启动失败"那几条 warning 别刷屏


# ---------------------------------------------------------------------------
#  替身: 起停脚本 + 假的 /v1/models
# ---------------------------------------------------------------------------
class _FakeRunner(object):
    """假 runner: 不真跑 shell, 直接给一份"脚本的输出/退出码"。

    ⚠ 为什么要注入: 起停脚本是 shell 脚本, 而开发机（Windows）上**没有可靠的 bash** ——
      真跑起来会被 msys 的路径转换搞成"找不到脚本", 那是环境的噪声, 不是被测逻辑。
      所以"退出码怎么解释/最后一行怎么取"用替身验, 真跑脚本另有一条（有 bash 才跑）。
    """

    def __init__(self, returncode=0, output=None):
        self.returncode = returncode
        #: 脚本输出（bytes）—— 默认给一句中文, 用来验"最后一行会带回消息里"
        self.output = output if output is not None else "[llm] 假脚本跑过了\n".encode("utf-8")
        self.calls = []

    def __call__(self, script_path):
        self.calls.append(script_path)
        import subprocess

        return subprocess.CompletedProcess([script_path], self.returncode, self.output)


def make_scripts(tmpdir):
    """造一份替身脚本目录（只要文件在, 内容由 runner 决定）。"""
    directory = os.path.join(tmpdir, "scripts")
    os.makedirs(directory, exist_ok=True)
    for name in ("start.sh", "stop.sh"):
        with open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
            handle.write("#!/bin/bash\nexit 0\n")
    return directory


class _ModelsHandler(BaseHTTPRequestHandler):
    status = 200
    body = b'{"data": [{"id": "qwen3-0.6b"}]}'

    def do_GET(self):                                  # noqa: N802 - http.server 的约定
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):                      # 别往 stderr 刷访问日志
        pass


def serve_models(status=200, body=b'{"data": [{"id": "qwen3-0.6b"}]}'):
    """起一个假 llama-server（后台线程），返回 (port, 关掉的函数)。"""
    handler = type("_H", (_ModelsHandler,), {"status": status, "body": body})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server.server_address[1], server.shutdown


def free_port():
    """拿一个**没人监听**的端口（用来验"连不上 = 没就绪"）。"""
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# ===========================================================================
#  1) 该不该管
# ===========================================================================
class TestFromConfig(unittest.TestCase):
    def test_only_edge_with_the_switch_on_is_managed(self):
        self.assertIsNotNone(LlamaService.from_config(
            {"llm": {"mode": "edge", "manage_service": True}}))
        self.assertIsNone(LlamaService.from_config(
            {"llm": {"mode": "edge", "manage_service": False}}),
            "开关关着就不该动别人的服务")
        self.assertIsNone(LlamaService.from_config({"llm": {"mode": "edge"}}),
                          "缺省 = 不管")
        for mode in ("cloud", "disabled", "EDGE?"):
            self.assertIsNone(LlamaService.from_config(
                {"llm": {"mode": mode, "manage_service": True}}),
                "%s 模式下没有本机服务这回事" % mode)

    def test_mode_is_case_insensitive(self):
        self.assertIsNotNone(LlamaService.from_config(
            {"llm": {"mode": " EDGE ", "manage_service": True}}))

    def test_string_truthy_values_are_accepted(self):
        # 手写配置很容易写成字符串
        for value in ("true", "True", "yes", "on", "1"):
            self.assertIsNotNone(LlamaService.from_config(
                {"llm": {"mode": "edge", "manage_service": value}}), value)
        for value in ("false", "no", "0", "", None, []):
            self.assertIsNone(LlamaService.from_config(
                {"llm": {"mode": "edge", "manage_service": value}}), value)

    def test_port_and_key_are_read(self):
        service = LlamaService.from_config(
            {"llm": {"mode": "edge", "manage_service": True,
                     "port": 1234, "local_api_key": "k"}})
        self.assertEqual(service.port, 1234)
        self.assertEqual(service.api_key, "k")
        self.assertTrue(service.endpoint().endswith(":1234/v1/models"))

    def test_a_broken_port_falls_back(self):
        for bad in ("abc", None, 0):
            service = LlamaService.from_config(
                {"llm": {"mode": "edge", "manage_service": True, "port": bad}})
            self.assertEqual(service.port, 9000, bad)

    def test_nonsense_config_is_not_managed(self):
        self.assertIsNone(LlamaService.from_config(None))
        self.assertIsNone(LlamaService.from_config("nope"))
        self.assertIsNone(LlamaService.from_config({}))


# ===========================================================================
#  2) 起停（替身脚本）
# ===========================================================================
class TestStartStop(unittest.TestCase):
    """起停: 退出码与输出怎么解释（用替身 runner, 不碰真 shell）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.scripts = make_scripts(self._tmp.name)

    def test_start_and_stop_really_run_the_right_scripts(self):
        runner = _FakeRunner()
        service = LlamaService(scripts_dir=self.scripts, runner=runner)
        ok, message = service.start()
        self.assertTrue(ok, message)
        self.assertIn("假脚本跑过了", message, "把脚本最后一行带回来说明情况")
        ok, message = service.stop()
        self.assertTrue(ok, message)
        self.assertEqual([os.path.basename(p) for p in runner.calls],
                         ["start.sh", "stop.sh"])

    def test_a_missing_script_is_reported_not_raised(self):
        service = LlamaService(scripts_dir=os.path.join(self._tmp.name, "nope"),
                               runner=_FakeRunner())
        ok, message = service.start()
        self.assertFalse(ok)
        self.assertIn("脚本不存在", message)
        self.assertFalse(service.stop()[0])

    def test_a_failing_script_reports_the_exit_code_and_the_last_line(self):
        runner = _FakeRunner(returncode=3, output="[llm] 模型文件不存在\n".encode("utf-8"))
        ok, message = LlamaService(scripts_dir=self.scripts, runner=runner).start()
        self.assertFalse(ok)
        self.assertIn("3", message)
        self.assertIn("模型文件不存在", message, "把脚本最后一行带上, 排障才知道它说了什么")

    def test_a_silent_success_still_says_something(self):
        ok, message = LlamaService(scripts_dir=self.scripts,
                                   runner=_FakeRunner(output=b"")).start()
        self.assertTrue(ok)
        self.assertIn("成功", message)

    def test_a_runner_that_raises_is_reported(self):
        def boom(_path):
            raise OSError("bash 不在?")

        ok, message = LlamaService(scripts_dir=self.scripts, runner=boom).start()
        self.assertFalse(ok)
        self.assertIn("跑不起来", message)

    def test_repr_is_readable(self):
        self.assertIn("LlamaService", repr(LlamaService(scripts_dir=self._tmp.name)))


@unittest.skipIf(os.name == "nt",
                 "开发机上的 bash 是 WSL/Git Bash, 路径映射不可靠（C:/… 在 WSL 里是 /mnt/c/…）"
                 "—— 真跑 shell 脚本这条由板端（Ubuntu, bash 是真的）验")
class TestTheRealScriptsRun(unittest.TestCase):
    """真跑一次**真脚本**（不启动服务: 用一份内容已知的迷你脚本）。

    ⚠ 这里不碰仓库里的 `llm/scripts/`（那会真去起 llama-server）—— 用一份内容已知的
      迷你脚本, 验的是"bash 这条路真的通"。
    """

    def test_a_real_shell_script_runs_and_its_output_comes_back(self):
        tmp = tempfile.mkdtemp(prefix="llm-scripts-")
        self.addCleanup(shutil.rmtree, tmp, True)
        directory = os.path.join(tmp, "scripts")
        os.makedirs(directory)
        path = os.path.join(directory, "start.sh")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/bash\necho \"[llm] 真脚本跑过了\"\n")
        ok, message = LlamaService(scripts_dir=directory).start()
        self.assertTrue(ok, message)
        self.assertIn("真脚本跑过了", message)


# ===========================================================================
#  3) 探活
# ===========================================================================
class TestReady(unittest.TestCase):
    def test_ready_when_models_endpoint_answers_with_data(self):
        port, shutdown = serve_models()
        self.addCleanup(shutdown)
        service = LlamaService(scripts_dir=".", port=port)
        self.assertTrue(service.is_ready())
        ready, waited = service.wait_ready(timeout_s=2)
        self.assertTrue(ready)
        self.assertLess(waited, 2.0)

    def test_not_ready_when_the_key_is_refused(self):
        # 端口活着但拒绝 key = "起来了一半" —— 不能报就绪, 否则排障会被骗
        port, shutdown = serve_models(status=403, body=b'{"error": "Invalid API Key"}')
        self.addCleanup(shutdown)
        self.assertFalse(LlamaService(scripts_dir=".", port=port).is_ready())

    def test_not_ready_when_the_body_is_not_a_model_list(self):
        port, shutdown = serve_models(body=b'{"nope": true}')
        self.addCleanup(shutdown)
        self.assertFalse(LlamaService(scripts_dir=".", port=port).is_ready())

    def test_not_ready_when_nothing_listens(self):
        self.assertFalse(LlamaService(scripts_dir=".", port=free_port()).is_ready())

    def test_wait_ready_gives_up_and_says_how_long_it_waited(self):
        service = LlamaService(scripts_dir=".", port=free_port(), ready_timeout_s=0.3)
        started = time.monotonic()
        ready, waited = service.wait_ready()
        self.assertFalse(ready)
        self.assertLess(time.monotonic() - started, 5.0, "不该傻等太久")
        self.assertGreaterEqual(waited, 0.0)


# ===========================================================================
#  4) Runtime 接线
# ===========================================================================
class _FakeService(object):
    """替身 service: 记下 start/stop 各调了几次（**不碰真脚本**）。"""

    last = None

    def __init__(self, *args, **kwargs):
        self.started = 0
        self.stopped = 0
        _FakeService.last = self

    @classmethod
    def from_config(cls, config, scripts_dir=None, log=None):
        llm = (config or {}).get("llm") or {}
        if llm.get("mode") != "edge" or not llm.get("manage_service"):
            return None
        return cls()

    def start(self):
        self.started += 1
        return True, "替身脚本跑过了"

    def stop(self):
        self.stopped += 1
        return True, "替身脚本跑过了"

    def wait_ready(self, timeout_s=None):
        return True, 0.0


class TestRuntimeWiring(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import agent.main as main_module

        self.main = main_module
        self._original = main_module.LlamaService
        main_module.LlamaService = _FakeService
        self.addCleanup(setattr, main_module, "LlamaService", self._original)
        _FakeService.last = None

    def _runtime(self, mode="edge", manage=True):
        runtime = self.main.Runtime(
            config={"llm": {"mode": mode, "manage_service": manage}},
            start_native=False, start_terminal=False,
            log=logging.getLogger("test.llm_service"))
        runtime.state = StateMachine()
        return runtime

    async def _settle(self, seconds=2.0):
        """等后台线程里的起停动作落地（回调是丢线程做的）。"""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(0.02)
            if self._idle():
                return
        self.fail("等了 %.1fs 后台动作还没落地" % seconds)

    @staticmethod
    def _idle():
        for thread in threading.enumerate():
            if thread.name == "llm-service" and thread.is_alive():
                return False
        return True

    async def test_agent_start_starts_the_service(self):
        runtime = self._runtime()
        await runtime._start_llm_service()
        service = runtime.llm_service
        self.assertIsNotNone(service)
        self.assertEqual(service.started, 1)
        self.assertEqual(service.stopped, 0)

    async def test_sleep_stops_and_leaving_sleep_starts_again(self):
        runtime = self._runtime()
        await runtime._start_llm_service()
        service = runtime.llm_service

        runtime.state.transition(State.SLEEP, "test")
        await self._settle()
        self.assertEqual(service.stopped, 1, "进 SLEEP 要停服务")
        self.assertEqual(service.started, 1)

        runtime.state.transition(State.IDLE, "test")
        await self._settle()
        self.assertEqual(service.started, 2, "离开 SLEEP 要起回来")

    async def test_transitions_inside_awake_states_do_not_touch_the_service(self):
        runtime = self._runtime()
        await runtime._start_llm_service()
        service = runtime.llm_service
        runtime.state.transition(State.STUDY, "test")
        runtime.state.transition(State.IDLE, "test")
        await asyncio.sleep(0.2)
        self.assertEqual((service.started, service.stopped), (1, 0),
                         "IDLE ⇄ STUDY 不该动服务")

    async def test_switch_off_means_no_service_object_at_all(self):
        runtime = self._runtime(manage=False)
        await runtime._start_llm_service()
        self.assertIsNone(runtime.llm_service)
        runtime.state.transition(State.SLEEP, "test")
        runtime.state.transition(State.IDLE, "test")
        await asyncio.sleep(0.1)

    async def test_cloud_mode_never_touches_a_local_service(self):
        runtime = self._runtime(mode="cloud")
        await runtime._start_llm_service()
        self.assertIsNone(runtime.llm_service)

    async def test_a_failing_script_does_not_break_startup(self):
        class _Boom(_FakeService):
            def start(self):
                raise RuntimeError("脚本炸了")

        self.main.LlamaService = type("_B", (_Boom,), {
            "from_config": classmethod(lambda cls, config, scripts_dir=None, log=None: cls())})
        runtime = self._runtime()
        await runtime._start_llm_service()          # 不该抛
        runtime.state.transition(State.SLEEP, "test")
        await self._settle()

    async def test_ready_probe_only_logs(self):
        # 就绪探活在后台线程里跑; 真的就绪/不就绪都只写日志, 不影响任何返回值
        runtime = self._runtime()
        await runtime._start_llm_service()
        await self._settle()
        self.assertIsNotNone(runtime.llm_service)


if __name__ == "__main__":
    unittest.main(verbosity=2)
