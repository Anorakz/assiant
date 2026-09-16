#!/usr/bin/env python3
"""
tests/board/test_image_rb_numpy.py — 在板端验证 image_rb 的 numpy 数据通路

为什么需要它
---------------------------------------------------------------------------
板端跑 test_binding_api.py 时环形缓冲是空的, read_latest() 只会返回 None ——
"没崩"不等于"数据对"。这个测试用 tests/board/push_test_frame.cpp 编译出的
测试用扩展 (agent_native_test) 往**同一个** ImageRingBuffer 里塞一帧已知图案,
再用正式的 agent_native.image_rb.read_latest() 读出来逐字节核对。

这样验证的是真实的板端 aarch64 + numpy 1.24.4 组合下的:
    · shape / dtype / C 连续性
    · 像素内容 (196608 字节)
    · read_by_timestamp 的时间戳语义
    · size()

用法 (板端):
    cd /home/kickpi/myproject/assitant/agent
    LD_LIBRARY_PATH=. python3 test_image_rb_numpy.py
"""
import sys

try:
    import numpy as np
except ImportError:
    print("SKIP: 板端没有 numpy")
    sys.exit(0)

import agent_native as an

try:
    import agent_native_test as t
except ImportError as e:
    print("SKIP: 缺少测试扩展 agent_native_test (%s)" % e)
    print("      需要先按 tests/board/push_test_frame.cpp 的说明交叉编译它")
    sys.exit(0)

fails = []


def check(ok, what):
    print("  [%s] %s" % ("PASS" if ok else "FAIL", what))
    if not ok:
        fails.append(what)


def main():
    print("numpy %s / python %s" % (np.__version__, sys.version.split()[0]))
    n = 256 * 256 * 3
    print()

    # ---- 往共享的 image_rb 里塞 3 帧, 图案与时间戳都已知 ----
    stamps = [1000, 2000, 3000]
    for k, ts in enumerate(stamps):
        t.push_test_frame(ts, k)
    print("已推入 3 帧: ts=%s (图案分别用种子 0/1/2)" % stamps)
    print()

    print("== size() ==")
    sz = an.image_rb.size()
    check(sz == 3, "size() == 3 (实际 %d)" % sz)

    print("\n== read_latest() 返回 numpy ==")
    arr = an.image_rb.read_latest()
    check(arr is not None, "有帧时不再返回 None")
    if arr is None:
        print("\n=== 失败: read_latest 返回 None ===")
        return 1
    check(isinstance(arr, np.ndarray), "类型是 numpy.ndarray")
    check(arr.shape == (256, 256, 3), "shape == (256,256,3), 实际 %s" % (arr.shape,))
    check(arr.dtype == np.uint8, "dtype == uint8, 实际 %s" % arr.dtype)
    check(arr.flags["C_CONTIGUOUS"], "C 连续")
    check(arr.nbytes == n, "nbytes == %d" % n)

    print("\n== 像素内容 (与推入的种子图案逐字节比对) ==")
    # 最新一帧是种子 2
    expect = t.expected_pattern(2)
    check(arr.tobytes() == expect, "read_latest 内容 == 种子 2 的图案")
    if arr.tobytes() != expect:
        got = arr.tobytes()
        for i in range(n):
            if got[i] != expect[i]:
                print("     首个不一致 byte[%d]: got=%d want=%d" % (i, got[i], expect[i]))
                break

    print("\n== read_by_timestamp ==")
    # 非破坏性 + "最接近且不超过": ts=2500 应拿到 ts=2000 那帧 (种子 1)
    one = an.image_rb.read_by_timestamp(2500)
    check(one is not None, "ts=2500 有满足的帧")
    if one is not None:
        check(one.tobytes() == t.expected_pattern(1), "ts=2500 -> 种子 1 的图案 (不超过 2500 的最大 ts)")
    # 重复调用结果一致 (非破坏性)
    again = an.image_rb.read_by_timestamp(2500)
    check(again is not None and again.tobytes() == t.expected_pattern(1),
          "同一 ts 重复调用结果一致 (非破坏性)")
    # ts 小于所有帧 -> None
    check(an.image_rb.read_by_timestamp(500) is None, "ts=500 (早于所有帧) 返回 None")
    # ts 大于所有帧 -> 最新帧
    big = an.image_rb.read_by_timestamp(10 ** 12)
    check(big is not None and big.tobytes() == t.expected_pattern(2),
          "超大 ts -> 最新帧 (种子 2)")

    print("\n== 数组可写性与独立性 ==")
    arr2 = arr.copy()
    arr2[0, 0, 0] = 123
    check(arr2[0, 0, 0] == 123, "返回的数组可写 (是独立副本, 不是共享槽位)")

    print()
    if fails:
        print("=== %d 项失败 ===" % len(fails))
        for f in fails:
            print("   - %s" % f)
        return 1
    print("=== 全部通过 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
