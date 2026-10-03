#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_audit_script.py — 审计器自身的测试（T15-3）

工具本身不可信，它给出的结论就没有意义。所以这里喂一个**含已知问题的小样例树**，
断言它能把那几类问题找出来；再用当前仓库跑一遍，断言"能跑完、有数字"。

刻意不用真实仓库做断言（那种断言会随代码变化天天红），只用样例树断言**能力**。
"""
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
AUDIT = _ROOT / "scripts" / "audit-code.py"
BASELINE = _ROOT / "scripts" / "audit-baseline.json"


SAMPLE = {
    # 两处相同的函数体 → 应被报成"重复"
    "pkg/a.py": (
        "def build_one(cfg):\n"
        "    items = []\n"
        "    for k in cfg:\n"
        "        items.append(k.strip())\n"
        "    return items\n"
        "\n"
        "def used_elsewhere():\n"
        "    return 1\n"
    ),
    "pkg/b.py": (
        "def build_two(cfg):\n"
        "    items = []\n"
        "    for k in cfg:\n"
        "        items.append(k.strip())\n"
        "    return items\n"
    ),
    # 零引用定义 + open() 不带 encoding + 裸 except
    "pkg/c.py": (
        "def nobody_calls_me(path):\n"
        "    try:\n"
        "        with open(path) as f:\n"
        "            return f.read()\n"
        "    except:\n"
        "        return ''\n"
    ),
    # 有人调用 build_one → 它**不该**被判成死代码（样例要能区分"重复"与"死"）
    "pkg/d.py": (
        "from pkg.a import build_one\n"
        "\n"
        "def use_it(cfg):\n"
        "    return build_one(cfg)\n"
    ),
}


class TestAuditScript(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(cls.tmp.name)
        for rel, text in SAMPLE.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        out = root / "report.json"
        cls.proc = subprocess.run([sys.executable, str(AUDIT), str(root),
                                   "--json", str(out), "--quiet"],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  universal_newlines=True, timeout=180)
        cls.report = json.loads(out.read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_runs_on_a_sample_tree(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stdout)
        self.assertEqual(len(SAMPLE), 4)
        self.assertEqual(self.report["files"]["py"], 4, "应扫到 4 个 py 文件")
        self.assertEqual(self.report["parse_errors"], [])

    def test_finds_the_duplicate_pair(self):
        groups = self.report["duplicates_prod"]
        self.assertEqual(len(groups), 1, "样例里正好一对重复：%r" % groups)
        joined = " ".join(groups[0])
        self.assertIn("build_one", joined)
        self.assertIn("build_two", joined)

    def test_finds_the_zero_reference_function(self):
        defs = " ".join(self.report["unused_defs"])
        self.assertIn("nobody_calls_me", defs)
        self.assertNotIn("build_one", defs, "被引用的函数不该被当成死代码")

    def test_finds_open_without_encoding_and_bare_except(self):
        h = self.report["heuristics"]
        self.assertTrue(any("encoding" in k for k in h), h.keys())
        self.assertTrue(any("裸 except" in k for k in h), h.keys())

    def test_ratchet_has_no_new_findings_on_the_real_repo(self):
        """当前仓库跑棘轮必须"没有新增"（基线是新写的，等价于能跑通且自洽）。"""
        if not BASELINE.is_file():
            self.skipTest("还没有基线文件")
        proc = subprocess.run([sys.executable, str(AUDIT), str(_ROOT),
                               "--baseline", str(BASELINE), "--quiet"],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              universal_newlines=True, timeout=600)
        self.assertEqual(proc.returncode, 0,
                         "棘轮报新增（要么真新增、要么基线该刷新）：\n" + proc.stdout)

    def test_baseline_file_is_readable_and_nonempty(self):
        data = json.loads(BASELINE.read_text(encoding="utf-8"))
        self.assertGreater(len(data["findings"]), 0)
        self.assertTrue(all(isinstance(x, str) for x in data["findings"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
