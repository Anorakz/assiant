# ============================================================================
#  agent/io/input_sender.py — 输入发送的 asyncio 包装
#
#  对应的 native 接口 (native/binding.cpp)
#  ---------------------------------------------------------------------------
#      agent_native.send_key(modifier: int, key: int, action)   # action 可为 bool/str
#      agent_native.send_hotkey(keys: list[int])
#      agent_native.send_mouse(x: int, y: int, action)          # action 可为 None/str/bool
#      agent_native.MODIFIER_* / BUTTON_*
#
#  为什么字符串要在这里"翻译"一下
#  ---------------------------------------------------------------------------
#  native 的 key 是 **Windows 虚拟键码 (VK)** 的 uint32, modifier 是**位掩码**。
#  调用方写 "ctrl" / "A" 更自然, 所以这里做一层**编码**(encoding)转换。
#
#  ⚠ 注意区分两个不同的东西 (很容易混):
#      · **修饰键位掩码**: native send_key() 的 modifier 参数, 取值 MODIFIER_CTRL(0x02)
#        等, 它只是随事件捎带的"此刻修饰键被按住"提示位, 本身不产生按键
#      · **修饰键的 VK 码**: Ctrl 这个键本身的键码 0x11, 用在 send_hotkey 里
#    所以 send_key 的 modifier 用 _MODIFIER_BITS, send_hotkey 的键用 _KEY_CODES。
#
#  这不是"输入合法性校验"(按约定不做): 查不到的名字原样往下传, 由 native 报错。
#  也**不是**快捷键识别 —— send_hotkey 只是逐个发出, 不是"把 ctrl+S 认成一个动作"。
#
#  ⚠ native 的 send_* 已经 py::call_guard<py::gil_scoped_release>, 但"取 GIL +
#    进 native" 这段仍会占用事件循环, 所以这里统一再包一层专属执行器。
#    这几次调用都是非阻塞的快速调用, 用同一线程即可 (无 SPSC 约束, 但保持
#    "同一子系统同一线程"的一致性更好排查)。
# ============================================================================

from __future__ import annotations

import operator
from functools import reduce
from typing import Any, Dict, Iterable, List, Union

from ._native import get_native, run_native

__all__ = ["InputSender", "resolve_key", "resolve_modifier", "KEY_CODES", "MODIFIER_BITS"]

#: 归属的子系统名 (决定用哪个专属执行器)
_SUBSYS = "input_sender"

KeyLike = Union[int, str]

#: 修饰键名 -> 位掩码 (native send_key 的 modifier 参数)
#: 取值与 native/input_sender.h 的 kModifier* / binding.cpp 的 MODIFIER_* 一致
MODIFIER_BITS: Dict[str, int] = {
    "none": 0x00,
    "shift": 0x01,
    "ctrl": 0x02,
    "control": 0x02,
    "alt": 0x04,
    "meta": 0x08,
    "win": 0x08,
    "super": 0x08,
    "cmd": 0x08,
    "command": 0x08,
}

#: 具名键 -> Windows 虚拟键码 (VK)
#:
#: 单字符键 ("A" / "9" / "/") 由 resolve_key() 直接 ord(), 不列在这里。
#: 这里只列"敲一个字符表达不出来"的键。取值就是 Windows VK 码 ——
#: moonlight 的 LiSendKeyboardEvent 把 VK 原样发给主机, 中间不转换。
KEY_CODES: Dict[str, int] = {
    # 修饰键本身的键码 (给 send_hotkey 用)
    "ctrl": 0x11,
    "control": 0x11,
    "shift": 0x10,
    "alt": 0x12,
    "menu": 0x12,
    "meta": 0x5B,   # 左 Win
    "win": 0x5B,
    "super": 0x5B,
    "cmd": 0x5B,
    "command": 0x5B,
    # 编辑键
    "enter": 0x0D,
    "return": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "tab": 0x09,
    "space": 0x20,
    "backspace": 0x08,
    "delete": 0x2E,
    "del": 0x2E,
    "insert": 0x2D,
    # 方向 / 导航
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    # 锁定键
    "capslock": 0x14,
    "numlock": 0x90,
    "scrolllock": 0x91,
    # F1..F24
    **{"f%d" % i: 0x6F + i for i in range(1, 25)},
}


def resolve_key(key: KeyLike) -> int:
    """把 key 归一成 VK 码。

    · 整数       -> 原样 (已经是 VK)
    · 单字符     -> 大写后 ord()          "A" / "a" -> 65
    · 具名键     -> KEY_CODES             "enter" -> 13 (大小写/空格无关)
    · 查不到的名字 -> 原样透传, 交给 native 报错 (不做校验)
    """
    if isinstance(key, int):
        return key
    if isinstance(key, str):
        if len(key) == 1:
            # ⚠ 必须大写化: Windows 的字母 VK 就是**大写** ASCII ('A'=0x41)。
            #   直接 ord('a') 会得到 0x61, 而 0x61 是 VK_NUMPAD1 —— 于是
            #   send_key("ctrl", "c") 会变成 Ctrl+数字键盘3, 而不是 Ctrl+C。
            #   (数字与标点本来就没这个问题: '7'=0x37, '/'=0x2F。)
            return ord(key.upper())
        code = KEY_CODES.get(key.strip().lower())
        if code is not None:
            return code
    return key  # type: ignore[return-value]


def resolve_modifier(modifier: Union[None, int, str, Iterable[KeyLike]]) -> int:
    """把 modifier 归一成**位掩码**整数 (native send_key 的 modifier 参数)。

    · None            -> 0
    · 整数            -> 原样 (已经是位掩码)
    · 名字            -> MODIFIER_BITS        "ctrl" -> 0x02
    · 序列/集合       -> 逐个查表后按位或      ["ctrl","alt"] -> 0x06
    · 单字符          -> ord() (容错: 有人会直接传 "\\x02")
    · 查不到的名字    -> 原样透传, 交给 native 报错
    """
    if modifier is None:
        return 0
    if isinstance(modifier, int):
        return modifier
    if isinstance(modifier, str):
        return _one_modifier(modifier)
    if isinstance(modifier, Iterable):
        parts = [resolve_modifier(item) for item in modifier]
        if not parts:
            return 0
        return reduce(operator.or_, parts)
    return modifier  # type: ignore[return-value]


def _one_modifier(name: str) -> int:
    bits = MODIFIER_BITS.get(name.strip().lower())
    if bits is not None:
        return bits
    if len(name) == 1:
        return ord(name)
    return name  # type: ignore[return-value]


def _norm_action(action: Any) -> Any:
    """把 action 小写化, 让 'DOWN' / 'Down' 与 native 的约定一致。

    native 的 parse_press() 接受 bool / int / "down"/"up"/"press"/"release"
    (并且自己做大小写归一), 所以这里只在是字符串时统一成小写, 其余原样。
    """
    if isinstance(action, str):
        return action.strip().lower()
    return action


class InputSender:
    """把输入动作发给主机 (Sunshine), 不阻塞事件循环。

    典型用法::

        sender = InputSender()
        await sender.send_key("ctrl", "C", "down")
        await sender.send_key("ctrl", "C", "up")
        await sender.send_hotkey(["ctrl", "alt", "S"])
        await sender.send_mouse(128, 128, "left")
    """

    def __init__(self, native: Any = None) -> None:
        """
        @param native 注入的 native 模块 (测试用); None = 按需 import agent_native
        """
        self._native = native

    # ------------------------------------------------------------ 内部 ---
    def _mod(self) -> Any:
        return self._native if self._native is not None else get_native()

    # ------------------------------------------------------------ 发送 ---
    async def send_key(self, modifier: str, key: str, action: str) -> None:
        """发送一次按键。

        @param modifier 修饰键: "ctrl" / ["ctrl","alt"] / 位掩码整数 / None
        @param key      键: "A" (单字符 -> VK) 或 65 (VK 码)
        @param action   "down"/"press" 按下, "up"/"release" 抬起
        """
        native = self._mod()
        mod = resolve_modifier(modifier)
        code = resolve_key(key)
        await run_native(_SUBSYS, native.send_key, mod, code, _norm_action(action))

    async def send_hotkey(self, keys: List[str]) -> None:
        """发送组合键。

        ⚠ keys 必须**自带修饰键**, 例如 ["ctrl", "alt", "S"]。
          native 会按顺序逐个按下, 再按相反顺序逐个抬起。
        """
        native = self._mod()
        codes = [resolve_key(k) for k in keys]
        await run_native(_SUBSYS, native.send_hotkey, codes)

    async def send_mouse(self, x: int, y: int, action: str) -> None:
        """把鼠标移到 ROI 坐标 (x, y) 并可选地按键。

        @param x,y    0..255 (参考平面 256×256, 越界由 native 夹住)
        @param action None/"move" 只移动; "left"/"right"/"middle" 按下该键;
                      True/False 左键按下/抬起
        """
        native = self._mod()
        await run_native(_SUBSYS, native.send_mouse, int(x), int(y), _norm_action(action))
