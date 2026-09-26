#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""量一量**工具清单**在板端真实 tokenizer 下有多少 token（T12-6 起）。

跑法（板端, 仓库根）::

    sh llm/scripts/start.sh                 # 先起本机 llama-server（9000）
    python3 tests/board/measure_tool_tokens.py
    sh llm/scripts/stop.sh                  # 量完停掉（不留下进程）

为什么要有这个脚本
    `tests/test_merged_tools.py::TestPromptBudget` 用**字符数**当预算 —— 因为开发机与板端
    都没有 Qwen 的 tokenizer, 不为一条断言装一份。但"改预算"这种决定必须拿**真 token**
    说话（那个文件里的注释就写着"确实要加工具就先量一遍板端 prompt 再改这个预算"）。
    这个脚本把那次测量固定下来: 起真 llama-server, POST 到 `/tokenize`。

它量什么
    · 四个状态各自的工具清单（与 `provider.request_kwargs` 里那段 JSON 逐字节一致）;
    · 每个工具自己占多少（把清单里的其它工具去掉再量一次）;
    · 与 `LLM_CTX_SIZE`（llama-server 的上下文长度）的对比 —— 这是**唯一**的硬上限。

⚠ 不启动也不停止 llama-server（那是 `llm/scripts/start.sh` / `stop.sh` 的事:
  谁起的谁停）; 端口连不上就打印怎么起, 直接退出 2。
"""
from __future__ import annotations

import io
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from agent.core.state_machine import State, StateMachine                   # noqa: E402
from agent.core.tool_router import ToolRouter                              # noqa: E402
from agent.llm import provider as provider_module                          # noqa: E402
from agent.tools import build_tools                                        # noqa: E402

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
ENV_FILE = os.path.join(REPO, "llm", "config", "llm.env")
STATES = (State.SLEEP, State.IDLE, State.STUDY, State.GAME)


def llm_env():
    """读 llm/config/llm.env（它是 config.yaml 的派生文件, 里面有端口与 key）。"""
    values = {}
    with open(ENV_FILE, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def tokenize(text, host, port, api_key):
    """一段文本 -> token 数（llama-server 的 `/tokenize`）。"""
    request = urllib.request.Request(
        "http://%s:%s/tokenize" % (host, port),
        data=json.dumps({"content": text}).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer %s" % api_key},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))
    tokens = data.get("tokens")
    if not isinstance(tokens, list):
        raise RuntimeError("不认识的 /tokenize 应答: %r" % (list(data)[:5],))
    return len(tokens)


def tool_block(tools):
    """与 `provider.request_kwargs` 里 `tools=[...]` 那段**逐字节一致**的 JSON。"""
    return json.dumps([{"type": "function", "function": tool} for tool in tools],
                      ensure_ascii=False)


def services():
    """工具要的入口替身（只为把工具**造出来**, 一个都不会被调用）。"""
    return {
        "input_sender": type("S", (), {"show_desktop": lambda self: None})(),
        "next_wallpaper": lambda **kwargs: {}, "wallpaper_tags": lambda **kwargs: {},
        "music_list": lambda **kwargs: {}, "music_search": lambda **kwargs: {},
        "music_state": lambda: {}, "music_enqueue": lambda **kwargs: {},
        "music_queue_clear": lambda: {}, "music_queue_state": lambda: {},
        "music_tag": lambda **kwargs: {}, "music_control": lambda **kwargs: {},
        "bilibili_search": lambda **kwargs: {},
        "schedule_add": lambda values: {}, "schedule_list": lambda: {},
        "schedule_remove": lambda values: {},
    }


def advertised(state):
    machine = StateMachine()
    if state is not State.IDLE:
        machine.transition(state, "measure")
    router = ToolRouter(state_provider=machine, services=services())
    for tool in build_tools(router):
        router.register(tool)
    return provider_module._advertised_tools(router)


def main():
    if not os.path.isfile(ENV_FILE):
        print("找不到 %s —— 这个脚本要在板端仓库根跑" % ENV_FILE)
        return 2
    env = llm_env()
    host = "127.0.0.1"
    port = env.get("LLM_PORT", "9000")
    ctx = int(env.get("LLM_CTX_SIZE", "4096") or 4096)
    api_key = env.get("LLM_API_KEY", "")

    try:
        tokenize("ping", host, port, api_key)
    except (urllib.error.URLError, OSError) as exc:
        print("连不上 llama-server（%s:%s）：%s" % (host, port, exc))
        print("先起它：sh llm/scripts/start.sh（量完记得 sh llm/scripts/stop.sh）")
        return 2

    char_total = 0
    token_total = 0
    print("ctx-size = %d token（这才是硬上限）\n" % ctx)
    for state in STATES:
        tools = advertised(state)
        if not tools:
            print("%-6s 一个工具都没有" % state.value)
            continue
        block = tool_block(tools)
        tokens = tokenize(block, host, port, api_key)
        names = "、".join(tool["name"] for tool in tools)
        print("%-6s %2d 个工具  %5d 字符  %4d token   %s"
              % (state.value, len(tools), len(block), tokens, names))
        for tool in tools:
            alone = tokenize(tool_block([tool]), host, port, api_key)
            print("        · %-16s %5d 字符  %4d token"
                  % (tool["name"], len(json.dumps(tool, ensure_ascii=False)), alone))
        if state is State.STUDY:
            char_total, token_total = len(block), tokens

    if token_total:
        print("\nSTUDY（工具最多的一档）: %d 字符 / %d token = ctx 的 %.1f%%"
              % (char_total, token_total, 100.0 * token_total / ctx))
    return 0


if __name__ == "__main__":
    sys.exit(main())
