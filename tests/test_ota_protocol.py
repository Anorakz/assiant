#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`docs/ipc-protocol.md` ↔ `agent/core/ota.py` 的 `ota_state` **双向守卫**（T15-14-a）。

为什么单独一个文件：`tests/test_ipc_protocol.py` 里那条 music 的守卫是它的同类 ✓，
这里把 `ota_state` 也钉住 —— 文档与代码**谁单方面改了都会红** ✓。

⚠ 反空绿：先断言"文档里确实解析出了字段"（条数下限 ✓）再谈内容 ——
   否则一旦解析规则失效，就会变成"零字段 = 全部匹配"的假绿 ✗（T15-16 的教训 ✓）。
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.core import ota  # noqa: E402

DOC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "docs", "ipc-protocol.md")
TOPIC = "ota_state"
MIN_DOC_FIELDS = 8          # 反空绿下限：少于此数说明解析坏了，不是"文档没问题"


def documented_fields() -> set:
    """从 §3 的 topic 表里抠出 `ota_state` 的字段名（字段列里的 `反引号` 名字）。"""
    fields = set()
    with open(DOC, encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped.startswith("| `%s`" % TOPIC):
                continue
            cells = stripped.split("|")
            if len(cells) < 4:
                continue
            for name in re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", cells[2]):
                fields.add(name)
    return fields


class TestOtaStateRowMatchesTheCode(unittest.TestCase):
    def test_the_doc_row_exists_and_parses(self):
        """先证存在：解析不出字段就直接失败，别让后面的断言变成空的 ✓。"""
        fields = documented_fields()
        self.assertGreaterEqual(len(fields), MIN_DOC_FIELDS,
                                "文档里 ota_state 的字段太少（解析坏了？）：%s" % sorted(fields))
        self.assertIn("current_slot", fields)
        self.assertIn("slots", fields)
        self.assertIn("confirm", fields)

    def test_every_documented_field_is_really_returned(self):
        """文档写的每个字段，代码都得真的给（**读不了槽**时也给 —— 键集是固定的 ✓）。"""
        state = ota.ota_state_snapshot(cmdline_path="/nonexistent-cmdline",
                                       misc_device="/nonexistent-misc")
        for name in sorted(documented_fields()):
            self.assertIn(name, state, "文档写了 %s，但 ota_state_snapshot() 没给" % name)

    def test_every_returned_key_is_documented(self):
        """反过来：代码多给的键也必须写在文档里（`reason` 例外 —— 它在说明列里 ✓）。"""
        state = ota.ota_state_snapshot(cmdline_path="/nonexistent-cmdline",
                                       misc_device="/nonexistent-misc")
        documented = documented_fields() | {"reason"}
        for name in sorted(state):
            self.assertIn(name, documented, "代码给了 %s，但文档里没有" % name)

    def test_the_topic_name_is_in_the_doc(self):
        text = open(DOC, encoding="utf-8").read()
        self.assertIn("`%s`" % TOPIC, text)
        # 三条约定也必须写着（它们是"为什么这么实现"的依据 ✓）
        for token in ("变化才推", "force", "只展示"):
            self.assertIn(token, text)


if __name__ == "__main__":
    unittest.main()
