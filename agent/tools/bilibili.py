# ============================================================================
#  agent/tools/bilibili.py — "B 站搜视频" 工具（Phase 7 T11-5）
#
#  它做什么: **只做一件事** —— 把对话里的**关键词**交给 B 站视频队列
#            （`Runtime.bilibili_search(keyword)`）。搜完把"搜到什么"如实回给模型。
#
#  为什么只有一个参数、没有 action（你定的）
#  ---------------------------------------------------------------------------
#  你 T11-4 的原话: "工具动作只有根据对话猜测是否需要作为关键词推入 bilibili 工具并调用;
#  然后画面捕获器不走 llm 工具, 而是作为 agent 在 game 状态的循环; bilibili 清空队列之类的
#  动作只走 agent 不走 llm"。
#  所以:
#      · **模型只干"把关键词交出去"这一件事** —— 猜"用户是不是点了个视频"、把话浓缩成关键词;
#      · 清队列 / 下一集 / 上一集 / 挑第几个 **全部只走 Agent**（GUI 按钮 + IPC 命令 + 内部循环）,
#        **不进工具**（模型连"切下一集"这个动作都看不到）;
#      · 画面那条路（认游戏 -> 搜队列）是 Agent 在 GAME 里的循环, **与模型无关**。
#
#  为什么只在 GAME 可见（你定的 D6）
#  ---------------------------------------------------------------------------
#  · 视频就是**游戏模式主区**在放的东西（GUI 的 `VideoPanel`）;
#  · STUDY 清单已经 3562/3600 字符（只剩 38）, 加第四个工具**必然撞预算**;
#    放 GAME 里则**一个字符都不占** STUDY 的清单（GAME 以前一个工具都没有）。
#  ⚠ 代价如实写在这里: 想用对话点视频, **得先切到 GAME 模式**。
#
#  ⚠ 让 LLM 知道的五件事（都写进 description, 不然 0.6B 会自己脑补）
#  ---------------------------------------------------------------------------
#    1. 只能**搜/排队列**, 不会自己开始播 —— **要用户去点预览图**（你定的"等 GUI 操作才播"）;
#    2. 视频在**板端**放（不是 PC 出声那套）;
#    3. 清晰度看 B 站给多少（可能只有 360P/720P; 高清要配 cookie）—— 别承诺"高清";
#    4. 关键词由用户给, **别自己编**（用户没点名视频就别调）;
#    5. 搜到了多少条、当前第几条会如实回; 失败也如实（风控/网络/没配 cookie 各有话说）。
#
#  ⚠ 超时必须自己声明: 搜一次要走网络（首页引导 + 搜索 + 可能再补一页）, 板端实测
#    1~4 s, 路由默认 5 s 太紧（`next_music` 当初就是这么踩到的）。
# ============================================================================

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from ..core.state_machine import State
from ..core.tool_router import Tool
from ._common import EMPTY_VALUES

__all__ = ["NAME", "ALLOWED_STATES", "TIMEOUT_S", "DESCRIPTION", "SCHEMA", "normalize",
           "build"]

_log = logging.getLogger(__name__)

NAME = "bilibili_search"

#: **只在 GAME**: 视频就是游戏模式主区在放的东西（见模块头"为什么只在 GAME 可见"）。
ALLOWED_STATES = (State.GAME,)

#: 搜一次要过网络（实测 1~4 s; 补一页更久）—— 路由默认 5 s 太紧, 自己声明。
TIMEOUT_S = 30.0

DESCRIPTION = (
    "在 B 站搜视频并把它排进预览队列（**只排不播**：播不播由用户在界面里点）。"
    "只在游戏模式（GAME）可用。"
    "keyword 要填**用户点名想看的内容**（例如「Luna say maybe」「stellaris 实况」）；"
    "用户没点名视频就别调这个工具。"
    "视频在**板端**播放，清晰度看 B 站给多少（可能只有 360P/720P，高清要在板端配 cookie），"
    "所以**不要**向用户承诺高清。"
    "搜完会如实告诉你搜到几条、当前是第几条；失败原因也会如实说（风控/网络/搜索词太泛）。"
)

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "keyword": {
            "type": "string",
            "minLength": 1,
            "maxLength": 64,
            "description": "要搜的内容（用户嘴上说的那个词/作品名/游戏名），不要填整句话",
        },
    },
    "required": ["keyword"],
    "additionalProperties": False,
}


def normalize(args: Dict[str, Any]) -> Dict[str, Any]:
    """参数归一化（T8-5c 的那套）—— **只做等价改写, 不猜内容**。

    规则（每条都写明来源）:

    ============================================  ==========================================
    模型可能这样写                                 归一化成
    ============================================  ==========================================
    `keyword="  Luna say maybe  "`                去掉首尾空白（用户说话常带空格）
    `keyword=""` / `"none"` / `"无"` 这类空写法     删掉（让 schema 如实报"缺关键字"）
    `keyword` 给了个列表 `["a","b"]`               取第一个非空的（模型偶尔这么填）
    `query="…"` / `text="…"` / `q="…"` 等同义键     搬到 `keyword`（板端实测这类错位很常见）
    ============================================  ==========================================
    """
    out = dict(args or {})
    for alias in ("query", "q", "text", "search", "keyword_text", "content"):
        if alias in out and not str(out.get("keyword") or "").strip():
            out["keyword"] = out.pop(alias)
        else:
            out.pop(alias, None)
    value = out.get("keyword")
    if isinstance(value, (list, tuple)):                    # 列表 -> 第一个非空的
        value = next((item for item in value if str(item or "").strip()), "")
    if isinstance(value, str):
        value = value.strip()
        if value.lower() in EMPTY_VALUES:
            out.pop("keyword", None)                        # 当没给 -> schema 报缺参数
        else:
            out["keyword"] = value
    elif value is None:
        out.pop("keyword", None)
    return out


def build(services: Dict[str, Any]) -> Optional[Tool]:
    """造工具。缺 `bilibili_search` 入口就返回 None（并说清为什么）。

    @note 依赖的是**运行时入口**（`agent/main.py::Runtime.bilibili_search`），不是 B 站客户端
          本身: 队列没建起来（没配 cookie/没网）属于**运行期**问题, 调用时才知道 ——
          那时该如实报错给用户, 而不是让工具在启动时凭空消失。
    """
    entry = (services or {}).get("bilibili_search")
    if entry is None:
        _log.warning("tools: bilibili_search 需要 services['bilibili_search'] —— 跳过")
        return None
    if not callable(entry):
        _log.warning("tools: services['bilibili_search'] 不是可调用的 —— 跳过")
        return None

    def handler(keyword: str = "") -> Dict[str, Any]:
        # ⚠ 归一化已经在路由层跑过（`Tool.normalize`, 见上面的规则表）——
        #   这里只把关键词交出去, 不自己搜、不自己碰队列。
        text = str(keyword or "").strip()
        if not text:
            return {"ok": False, "tell_user": "要搜什么？给我一个关键词",
                    "error": "keyword 是空的"}
        try:
            return entry(text)
        except Exception as exc:                            # noqa: BLE001 - 如实回给模型
            return {"ok": False, "tell_user": "搜 B 站没成功：%s" % exc, "error": str(exc)}

    return Tool(name=NAME, description=DESCRIPTION, schema=SCHEMA, handler=handler,
                allowed_states=set(ALLOWED_STATES), timeout_s=TIMEOUT_S, normalize=normalize)
