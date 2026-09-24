# ============================================================================
#  agent/tools/__init__.py — 具体工具（Phase 7）
#
#  谁装它: `agent/main.py::_register_tools()` —— 它 import 本模块并找
#          `build_tools(router)`（或 `TOOLS`），然后把返回的 `Tool` 一个个注册进
#          `ToolRouter`（注册失败只记一条 error，不拖垮启动）。
#
#  每个工具模块的约定
#  ---------------------------------------------------------------------------
#      build(services) -> Optional[Tool]
#        · 依赖齐了   -> 返回 Tool
#        · 缺依赖     -> 返回 None **并自己说清缺什么**（记一条 warning）
#        · **不抛异常** —— 少一个工具不该让整个工具层起不来（与 main.py 的
#          "单组件失败不影响其他组件" 同一条口径）
#
#  依赖从哪来: `router.services`（main.py 装配时填 input_sender / image_reader /
#  bus / config）。工具自己按名字取 —— 本模块不替它们解释依赖。
#
#  为什么工具不直接 import agent.io / agent.main
#  ---------------------------------------------------------------------------
#  那样会绕开装配、也会在测试里拉起 native 这些东西；从 services 取让"缺依赖"
#  变成一个可测的分支（tests/test_tools.py 就是这么钉的）。
# ============================================================================

from __future__ import annotations

import importlib
import logging
from typing import Any, List

from ..core.tool_router import Tool

__all__ = ["build_tools", "TOOL_MODULES"]

_log = logging.getLogger(__name__)

#: 要装的工具模块（相对本包）。加工具时在这里加一行。
#: ⚠ T8-5b 起**只有三个**（你定的"进一步抽象简化"）: 壁纸 / 音乐 / 回到桌面。
#:   每个工具内部用 `action` 分派具体动作（见各自模块头）。为什么合并:
#:   工具清单占第一轮 prompt 的 90%（6 个工具 1699 token, 用户那句话只有 6 token）,
#:   合并后模型要认的名字从 7 个降到 3 个, 上下文与"先想清楚叫哪个名字"一起省下来。
TOOL_MODULES = (
    "back_to_desktop",
    "wallpaper",
    "music",
)


def build_tools(router: Any) -> List[Tool]:
    """把 `TOOL_MODULES` 里每个模块的工具造出来（main.py 的接入点）。

    @param router ToolRouter（用它的 `services` 拿依赖）
    @return 造好的 Tool 列表（缺依赖/造失败的**跳过**，不抛）
    """
    services = getattr(router, "services", None) or {}
    tools: List[Tool] = []

    for name in TOOL_MODULES:
        try:
            module = importlib.import_module("%s.%s" % (__name__, name))
        except Exception as exc:                      # noqa: BLE001
            _log.error("tools: 导入 %s 失败: %r", name, exc, exc_info=True)
            continue

        factory = getattr(module, "build", None)
        if factory is None:
            _log.warning("tools: %s 没有 build(services)，跳过", name)
            continue

        try:
            tool = factory(services)
        except Exception as exc:                      # noqa: BLE001
            _log.error("tools: %s.build() 失败: %r", name, exc, exc_info=True)
            continue

        if tool is None:
            continue                                  # build() 自己说明过原因
        tools.append(tool)

    return tools
