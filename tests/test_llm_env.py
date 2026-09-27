#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_llm_env.py — 派生 llm.env 的 Python 实现（T14-2）

跑法:
    python tests/test_llm_env.py

为什么单独一个文件
    T14-2 把这份映射表从 `gui/src/core/config_sync.cpp` 搬到了 `agent/core/llm_env.py`
    —— "唯一写入者是 Agent"这条决定（docs/adr/0005）的落地件之一。这里钉住三件事:

      1. **映射表逐条对齐**（8 个键、连 `LLM_API_KEY <- llm.local_api_key` 这个
         容易写错的都写死在测试里）—— 端口写错不会报错, 只会让 llama-server 起在
         另一个端口上, 那种问题最难查;
      2. **只动目标行**: 注释 / 空行 / 顺序 / 行尾（含"原来就没有尾换行"）逐字节保留;
      3. **不写多余的东西**: 值没变就不写文件、不留 `.bak`; 缺键才追加;
         没有可写的键也不报错（真源里没写那一行 = 派生不动它）。

⚠ 与 C++ 老实现**刻意不同**的一处: 行尾注释保留（老版 Env 分支会把 `=` 之后整段
  重写掉）。板端真实那份 llm.env 没有行内注释, 所以这是"顺手修好", 有测试钉住。
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.core import llm_env                                        # noqa: E402

#: 板端那份 llm.env 的形状（注释 + 空行 + 不属于映射表的键 + **结尾没有换行**）
ENV_SAMPLE = (
    "# llm/config/llm.env\n"
    "\n"
    "# ==================== 模型配置 ====================\n"
    "LLM_MODEL_PATH=/home/kickpi/model/Qwen3-0.6B-Q4_K_M.gguf\n"
    "LLM_MODEL_NAME=qwen3-0.6b\n"
    "\n"
    "# ==================== 服务配置 ====================\n"
    "LLM_HOST=0.0.0.0\n"
    "LLM_PORT=9000\n"
    "LLM_API_KEY=***REMOVED-LLM-KEY***\n"
    "\n"
    "# ==================== 上下文与性能 ====================\n"
    "LLM_CTX_SIZE=4096\n"
    "LLM_BATCH_SIZE=256\n"
    "LLM_THREADS=4\n"
    "LLM_THREADS_BATCH=4\n"
    "\n"
    "# ==================== 运行时路径（相对 llm/ 根目录）====================\n"
    "LLM_LOG_DIR=logs\n"
    "LLM_RUN_DIR=run\n"
    "LLM_PID_FILE=run/llama-server.pid\n"
    "LLM_LOG_FILE=logs/llama-server.log"      # ⚠ 没有结尾换行
)

CONFIG_SAMPLE = """\
llm:
  mode: edge
  model_path: /home/kickpi/model/Qwen3-0.6B-Q4_K_M.gguf
  model_name: qwen3-0.6b
  port: 9000
  ctx_size: 4096
  batch_size: 256
  threads: 4
  threads_batch: 4
  local_api_key: ***REMOVED-LLM-KEY***

ipc:
  socket_path: /tmp/agent.sock
"""


class LlmEnvCase(unittest.TestCase):
    """把 config.yaml + llm.env 摆进临时目录（形状 = <root>/config/ 与 <root>/llm/config/）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="t14-llm-env-"))
        (self.tmp / "config").mkdir()
        (self.tmp / "llm" / "config").mkdir(parents=True)
        self.config = self.tmp / "config" / "config.yaml"
        self.env = self.tmp / "llm" / "config" / "llm.env"
        self.write_config(CONFIG_SAMPLE)
        self.write_env(ENV_SAMPLE)

    def tearDown(self):
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def write_config(self, text):
        self.config.write_text(text, encoding="utf-8")

    def write_env(self, text):
        with open(str(self.env), "w", encoding="utf-8", newline="") as handle:
            handle.write(text)

    def env_text(self):
        with open(str(self.env), "r", encoding="utf-8", newline="") as handle:
            return handle.read()

    def plan(self):
        return llm_env.plan(config_path=self.config, env_path=self.env)

    def apply(self):
        return llm_env.apply(config_path=self.config, env_path=self.env)


class TestMappingTable(LlmEnvCase):
    """映射表就是契约 —— 写死在这里。"""

    def test_the_eight_pairs_are_exactly_the_cpp_ones(self):
        self.assertEqual(
            llm_env.LLM_ENV_KEYS,
            (("LLM_MODEL_PATH", "llm.model_path"),
             ("LLM_MODEL_NAME", "llm.model_name"),
             ("LLM_PORT", "llm.port"),
             ("LLM_CTX_SIZE", "llm.ctx_size"),
             ("LLM_BATCH_SIZE", "llm.batch_size"),
             ("LLM_THREADS", "llm.threads"),
             ("LLM_THREADS_BATCH", "llm.threads_batch"),
             # ⚠ 是 local_api_key（edge 的 key），不是 api_key（云端的 key）
             ("LLM_API_KEY", "llm.local_api_key")),
        )

    def test_keys_outside_the_table_are_never_touched(self):
        """LLM_HOST / 路径那几个键由板端自己维护, 派生不碰。"""
        self.write_config(CONFIG_SAMPLE.replace("port: 9000", "port: 9100"))
        self.apply()
        text = self.env_text()
        for key in ("LLM_HOST", "LLM_LOG_DIR", "LLM_RUN_DIR", "LLM_PID_FILE", "LLM_LOG_FILE"):
            self.assertIn(key, text, "%s 被派生弄丢了" % key)

    def test_default_env_path_is_relative_to_the_repo_root(self):
        self.assertEqual(llm_env.default_env_path(self.config), self.env)
        self.assertEqual(llm_env.default_env_path(str(self.config)), self.env)


class TestPlanAndApply(LlmEnvCase):
    def test_no_difference_is_an_empty_plan_and_no_write(self):
        before = self.env_text()
        self.assertEqual(self.plan(), [])
        result = self.apply()
        self.assertEqual(result["changed"], 0)
        self.assertEqual(result["backup"], "")
        self.assertEqual(self.env_text(), before)
        self.assertFalse((self.env.parent / "llm.env.bak").exists())

    def test_changing_one_key_touches_exactly_one_line(self):
        self.write_config(CONFIG_SAMPLE.replace("port: 9000", "port: 9100"))
        plans = self.plan()
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["key"], "LLM_PORT")
        self.assertEqual(plans[0]["old"], "9000")
        self.assertEqual(plans[0]["new"], "9100")

        result = self.apply()
        self.assertEqual(result["changed"], 1)
        after = self.env_text()
        self.assertIn("LLM_PORT=9100\n", after)
        # 除目标行外逐字节不变（含"原来结尾没有换行"这件事）
        self.assertEqual(after, ENV_SAMPLE.replace("LLM_PORT=9000", "LLM_PORT=9100"))
        self.assertFalse(after.endswith("\n"), "结尾没有换行的状态必须保住")

    def test_backup_is_the_byte_exact_previous_state(self):
        self.write_config(CONFIG_SAMPLE.replace("ctx_size: 4096", "ctx_size: 8192"))
        self.apply()
        with open(str(self.env) + ".bak", "r", encoding="utf-8", newline="") as handle:
            self.assertEqual(handle.read(), ENV_SAMPLE)

    def test_inline_comment_survives(self):
        """刻意与 C++ 老实现不同: 行尾注释保留（老版会把 `=` 之后整段重写）。"""
        self.write_env(ENV_SAMPLE.replace("LLM_PORT=9000", "LLM_PORT=9000      # 对齐的注释"))
        self.write_config(CONFIG_SAMPLE.replace("port: 9000", "port: 9200"))
        self.apply()
        self.assertIn("LLM_PORT=9200      # 对齐的注释", self.env_text())

    def test_missing_key_in_config_leaves_the_line_alone(self):
        text = CONFIG_SAMPLE.replace("  threads: 4\n", "")
        self.write_config(text)
        self.write_env(ENV_SAMPLE.replace("LLM_THREADS=4", "LLM_THREADS=8"))
        self.assertEqual(self.plan(), [])
        self.apply()
        self.assertIn("LLM_THREADS=8", self.env_text())

    def test_key_missing_in_env_is_appended_at_the_end(self):
        self.write_env(ENV_SAMPLE.replace("LLM_THREADS_BATCH=4\n", ""))
        self.write_config(CONFIG_SAMPLE.replace("threads_batch: 4", "threads_batch: 6"))
        plans = self.plan()
        self.assertEqual([item["key"] for item in plans], ["LLM_THREADS_BATCH"])
        self.assertIsNone(plans[0]["old"])
        self.apply()
        text = self.env_text()
        self.assertTrue(text.endswith("LLM_THREADS_BATCH=6\n"), repr(text[-60:]))

    def test_scalar_types_are_rendered_like_the_raw_yaml(self):
        self.write_config(CONFIG_SAMPLE.replace("threads: 4", "threads: 6")
                          .replace("model_name: qwen3-0.6b", "model_name: qwen3.5-0.8b"))
        self.apply()
        text = self.env_text()
        self.assertIn("LLM_THREADS=6\n", text)
        self.assertIn("LLM_MODEL_NAME=qwen3.5-0.8b\n", text)

    def test_empty_value_in_config_is_skipped(self):
        """`port:`（空值）在 C++ 那版是"容器", 派生不动它 —— 这里同口径。"""
        self.write_config(CONFIG_SAMPLE.replace("port: 9000", "port:"))
        self.write_env(ENV_SAMPLE.replace("LLM_PORT=9000", "LLM_PORT=9000"))
        self.assertEqual(self.plan(), [])

    def test_crlf_is_preserved(self):
        crlf = ENV_SAMPLE.replace("\n", "\r\n")
        self.write_env(crlf)
        self.write_config(CONFIG_SAMPLE.replace("port: 9000", "port: 9300"))
        self.apply()
        text = self.env_text()
        self.assertIn("LLM_PORT=9300\r\n", text)
        self.assertEqual(text.count("\r\n"), crlf.count("\r\n"), "换行数量必须一致（不多空行）")
        # 把 CRLF 归一之后应当与"LF 版改一行"逐字符相同
        self.assertEqual(text.replace("\r\n", "\n"),
                         ENV_SAMPLE.replace("LLM_PORT=9000", "LLM_PORT=9300"))

    def test_second_apply_is_a_no_op(self):
        self.write_config(CONFIG_SAMPLE.replace("port: 9000", "port: 9400"))
        self.apply()
        stamp = os.stat(str(self.env) + ".bak").st_mtime_ns
        second = self.apply()
        self.assertEqual(second["changed"], 0)
        self.assertEqual(os.stat(str(self.env) + ".bak").st_mtime_ns, stamp,
                         "没有改动就不该覆盖 .bak")

    def test_no_temp_files_left_behind(self):
        self.write_config(CONFIG_SAMPLE.replace("port: 9000", "port: 9500"))
        self.apply()
        leftovers = [name for name in os.listdir(str(self.env.parent)) if name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_read_env_value_reads_back(self):
        self.write_config(CONFIG_SAMPLE.replace("port: 9000", "port: 9600"))
        self.apply()
        self.assertEqual(llm_env.read_env_value("LLM_PORT", env_path=self.env), "9600")
        self.assertIsNone(llm_env.read_env_value("LLM_NOPE", env_path=self.env))


class TestHonestErrors(LlmEnvCase):
    def test_missing_env_file_is_an_honest_error(self):
        self.env.unlink()
        with self.assertRaises(llm_env.LlmEnvError) as caught:
            self.plan()
        self.assertIn("llm.env 不在", str(caught.exception))

    def test_missing_config_is_an_honest_error(self):
        self.config.unlink()
        with self.assertRaises(llm_env.LlmEnvError) as caught:
            self.plan()
        self.assertIn("读不到真源", str(caught.exception))

    def test_config_without_llm_section_is_an_honest_error(self):
        self.write_config("ipc:\n  socket_path: /tmp/agent.sock\n")
        with self.assertRaises(llm_env.LlmEnvError) as caught:
            self.plan()
        self.assertIn("没有 llm:", str(caught.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
