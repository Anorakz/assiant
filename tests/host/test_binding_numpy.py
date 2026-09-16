#!/usr/bin/env python3
"""
tests/host/test_binding_numpy.py — 驱动 test_binding_numpy.cpp 里的 numpy 通路校验

用法 (先按 test_binding_numpy.cpp 头部注释构建出 agent_native_test*.so):
    PYTHONPATH=<so 所在目录> python3 tests/host/test_binding_numpy.py

校验点:
  · frame_to_numpy 产出的数组形状 / dtype / 连续性 / 字节数
  · 像素内容与源 Frame 逐字节一致 (这才是"numpy 通路正确"的真正证据)
  · 线性索引 a.flat[y*256*3 + x*3 + c] 与 (y,x,c) 语义一致
  · InputEvent -> dict 的字段与取值
  · action / button 解析 (含非法输入应抛 ValueError)
"""
import sys

try:
    import numpy as np
except ImportError:
    print("SKIP: 需要 numpy")
    sys.exit(0)

try:
    import agent_native_test as t
except ImportError as e:
    print("SKIP: 找不到 agent_native_test 扩展 (%s)" % e)
    sys.exit(0)

failures = []


def check(ok, what):
    print("  [%s] %s" % ("PASS" if ok else "FAIL", what))
    if not ok:
        failures.append(what)


def main():
    print("numpy %s" % np.__version__)
    print()

    n = t.frame_bytes()
    print("frame_bytes = %d (期望 %d)" % (n, 256 * 256 * 3))
    check(n == 256 * 256 * 3, "frame_bytes == 196608")

    # ---- 已知图案: 与 C++ 侧一致 (i % 251) ----
    pattern = bytes((i % 251) for i in range(n))
    arr = t.frame_to_numpy(pattern)

    print("\n== numpy 数组属性 ==")
    check(isinstance(arr, np.ndarray), "返回的是 numpy.ndarray")
    check(arr.shape == (256, 256, 3), "shape == (256,256,3), 实际 %s" % (arr.shape,))
    check(arr.dtype == np.uint8, "dtype == uint8, 实际 %s" % arr.dtype)
    check(arr.flags["C_CONTIGUOUS"], "C 连续")
    check(arr.nbytes == n, "nbytes == %d" % n)

    print("\n== 像素内容 ==")
    check(arr.tobytes() == pattern, "逐字节与源 Frame 一致 (%.1f KB)" % (n / 1024.0))

    print("\n== 索引语义 ==")
    y, x, c = 100, 200, 2
    idx = (y * 256 + x) * 3 + c
    check(arr[y, x, c] == pattern[idx], "arr[%d,%d,%d] == pattern[%d]" % (y, x, c, idx))
    check(arr.reshape(-1)[idx] == pattern[idx], "arr.reshape(-1)[%d] 一致" % idx)
    check(arr[0, 0, 0] == 0 and arr[255, 255, 2] == pattern[-1], "首尾像素正确")

    print("\n== InputEvent -> dict ==")
    d = t.event_to_dict(0, 2, 65, 0, 0, 0, 999)
    check(d["type"] == "key", "type == 'key'")
    check(d["modifier"] == 2, "modifier == 2")
    check(d["key"] == 65, "key == 65")
    check(d["action"] == "press" and d["pressed"] is True, "action == 'press' / pressed")
    check(d["timestamp_ns"] == 999, "timestamp_ns == 999")
    check(set(d) == {"type", "modifier", "key", "x", "y", "action", "pressed",
                     "timestamp_ns"}, "字段集合完整")

    dm = t.event_to_dict(1, 0, 0, -5, 300, 1, 7)
    check(dm["type"] == "mouse", "type == 'mouse'")
    check(dm["x"] == -5 and dm["y"] == 300, "x/y 原样返回")
    check(dm["action"] == "release" and dm["pressed"] is False, "action == 'release'")

    print("\n== action 解析 ==")
    for v in (True, 1, "down", "up", "press", "release", "DOWN", "Release", "1", "0", 0, False):
        t.parse_press(v)  # 不应抛异常
    check(t.parse_press("down") is True and t.parse_press(1) is True, "down/1 -> True")
    check(t.parse_press("up") is False and t.parse_press(0) is False, "up/0 -> False")
    try:
        t.parse_press("banana")
        check(False, "非法 action 应抛 ValueError")
    except ValueError:
        check(True, "非法 action 抛 ValueError")
    try:
        t.parse_press(1.5)
        check(False, "float action 应抛 TypeError")
    except TypeError:
        check(True, "float action 抛 TypeError")

    print("\n== button 解析 ==")
    check(t.parse_button("left") == 1, "'left' -> 1")
    check(t.parse_button("Right") == 3, "'Right' -> 3 (大小写无关)")
    check(t.parse_button("middle") == 2, "'middle' -> 2")
    check(t.parse_button(None) == 0, "None -> 0 (纯移动)")
    check(t.parse_button(3) == 3, "整数原样返回")

    print()
    if failures:
        print("=== %d 项失败 ===" % len(failures))
        return 1
    print("=== 全部通过 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
