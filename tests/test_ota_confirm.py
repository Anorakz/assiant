#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`image/payload/ota-confirm.py` 的单测（T15-14-a）。

规矩：断言**先证存在、再证内容** —— 不吃空绿 ✓。
判定三件事，所以这里把三分支都钉住：
    · 两服务都 active 且 IPC 通  -> 标成功 + 写 ok:true（退出 0）
    · 任一不满足 / 超时          -> **什么都不标** ✗ + 写 ok:false（退出 1）
    · 结果文件里已是 ok:true     -> 幂等，直接返回（不重复标）
"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "image", "payload", "ota-confirm.py")


def _load():
    spec = importlib.util.spec_from_file_location("ota_confirm", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


confirm = _load()


class TestConfirm(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ota-confirm-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.json_path = os.path.join(self.tmp, "confirm.json")
        self.marked = []

    def _run(self, active: bool, ipc: bool, mark_ok: bool = True, argv=None):
        """把三个外部依赖换掉后跑 main()（不打桩就真去问 systemd 了 ✗）。"""
        original = (confirm.services_active, confirm.ipc_ok, confirm.mark_current_slot)
        confirm.services_active = lambda *a, **k: active
        confirm.ipc_ok = lambda *a, **k: ipc

        def fake_mark():
            self.marked.append(True)
            return mark_ok

        confirm.mark_current_slot = fake_mark
        try:
            args = argv if argv is not None else ["--json", self.json_path, "--timeout", "6"]
            old_argv = sys.argv
            sys.argv = ["ota-confirm.py"] + args
            try:
                return confirm.main()
            finally:
                sys.argv = old_argv
        finally:
            confirm.services_active, confirm.ipc_ok, confirm.mark_current_slot = original

    def _result(self):
        with open(self.json_path, encoding="utf-8") as handle:
            return json.load(handle)

    def test_happy_path_marks_and_reports_ok(self):
        code = self._run(active=True, ipc=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.marked, [True], "判过了必须去标当前槽")
        got = self._result()
        self.assertTrue(got["ok"])
        self.assertIn("elapsed_s", got)
        self.assertIn("at", got)

    def test_ipc_down_does_not_mark_anything(self):
        """IPC 不通 = 这次不算成功 ⇒ **一个标记都不许写**（让 tries 继续递减→自动回退）。"""
        code = self._run(active=True, ipc=False)
        self.assertEqual(code, 1)
        self.assertEqual(self.marked, [], "不通过时绝不能标成功")
        self.assertFalse(self._result()["ok"])

    def test_services_down_does_not_mark_anything(self):
        code = self._run(active=False, ipc=True)
        self.assertEqual(code, 1)
        self.assertEqual(self.marked, [])
        self.assertFalse(self._result()["ok"])

    def test_mark_failure_is_reported_as_failure(self):
        """IPC 通了但标成功失败 ⇒ 整体仍算不通过（不谎报成功 ✓）。"""
        code = self._run(active=True, ipc=True, mark_ok=False)
        self.assertEqual(code, 1)
        self.assertEqual(len(self.marked), 1)
        self.assertFalse(self._result()["ok"])

    def test_idempotent_when_already_ok(self):
        with open(self.json_path, "w", encoding="utf-8") as handle:
            json.dump({"ok": True}, handle)
        code = self._run(active=True, ipc=True, argv=["--json", self.json_path])
        self.assertEqual(code, 0)
        self.assertEqual(self.marked, [], "已经确认过就不再标一次（幂等 ✓）")

    def test_force_rejudges(self):
        with open(self.json_path, "w", encoding="utf-8") as handle:
            json.dump({"ok": True}, handle)
        code = self._run(active=True, ipc=True,
                         argv=["--json", self.json_path, "--force", "--timeout", "6"])
        self.assertEqual(code, 0)
        self.assertEqual(len(self.marked), 1, "--force 要真的重判并重标 ✓")

    def test_service_list_is_the_agreed_pair(self):
        """判据里的两个服务名就是约定那对（改错了这条会红 ✓）。"""
        self.assertEqual(confirm.REQUIRED_SERVICES, ("agent", "agent-gui"))

    def test_unit_file_waits_for_both_services(self):
        unit = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "systemd", "image", "assistant-ota-confirm.service")
        text = open(unit, encoding="utf-8").read()
        for token in ("After=agent.service agent-gui.service",
                      "ExecStart=/usr/bin/python3 /usr/lib/assistant/ota-confirm.py",
                      # 项目约定：我们的单元挂 assistant.target（不是 multi-user ✗，
                      # check-assistant-target.py 会当场报错 ✓）
                      "WantedBy=assistant.target"):
            self.assertIn(token, text)


if __name__ == "__main__":
    unittest.main()
