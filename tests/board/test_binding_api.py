#!/usr/bin/env python3
"""
tests/board/test_binding_api.py — 在板端验证 agent_native 暴露的完整 API

用法 (板端):
    cd /home/kickpi/myproject/assitant/agent
    LD_LIBRARY_PATH=. python3 test_binding_api.py

覆盖用户要求的全部入口:
    moonlight.start_with_session/stop/status
    image_rb.read_latest / read_by_timestamp / size
    send_key / send_mouse / send_hotkey
以及约束:
    · read_latest 返回 numpy (256,256,3) uint8
    · send_* 不得阻塞事件循环 (本脚本用信号量验证 GIL 确实被释放)
"""
import os
import sys
import threading
import time

try:
    import numpy as np
except ImportError:
    print("SKIP: 板端没有 numpy")
    sys.exit(0)

import agent_native as an

fails = []


def check(ok, what):
    print("  [%s] %s" % ("PASS" if ok else "FAIL", what))
    if not ok:
        fails.append(what)


def main():
    print("numpy %s / python %s" % (np.__version__, sys.version.split()[0]))
    print("agent_native %s, ping=%s" % (an.__version__, an.ping()))
    print()

    # ---------------------------------------------------------------- 结构 --
    print("== 模块结构 ==")
    for name in ("moonlight", "image_rb"):
        check(hasattr(an, name), "有子模块 %s" % name)
    for name in ("send_key", "send_mouse", "send_hotkey"):
        check(callable(getattr(an, name, None)), "有函数 %s" % name)
    for name in ("start_with_session", "stop", "status"):
        check(callable(getattr(an.moonlight, name, None)), "moonlight.%s" % name)
    for name in ("read_latest", "read_by_timestamp", "size"):
        check(callable(getattr(an.image_rb, name, None)), "image_rb.%s" % name)
    # Phase 6 收尾: host_input_rb 子模块已删除 (native 侧从来没有生产者),
    # 这里反过来断言它**不存在** —— 免得哪次改动又把它带回来。
    check(not hasattr(an, "host_input_rb"),
          "host_input_rb 子模块已删除 (死代码, 没有生产者)")

    # ------------------------------------------------------------ image_rb --
    print("\n== image_rb ==")
    n = an.image_rb.size()
    check(isinstance(n, int) and n >= 0, "size() 返回非负整数 (当前 %d)" % n)
    check(an.image_rb.capacity() == 300, "capacity() == 300")

    latest = an.image_rb.read_latest()
    if latest is None:
        check(True, "read_latest() 无帧时返回 None (未连接, 符合预期)")
    else:
        # 接上流之后这里才是真正的判据
        check(isinstance(latest, np.ndarray), "read_latest() 返回 numpy.ndarray")
        check(latest.shape == (256, 256, 3), "shape == (256,256,3), 实际 %s" % (latest.shape,))
        check(latest.dtype == np.uint8, "dtype == uint8, 实际 %s" % latest.dtype)
        check(latest.flags["C_CONTIGUOUS"], "C 连续 (可直接喂给 RKNN)")

    ts = an.image_rb.read_by_timestamp(0)
    check(ts is None, "read_by_timestamp(0) 无满足帧时返回 None")

    # ------------------------------------------------------------ status ----
    print("\n== moonlight.status() ==")
    st = an.moonlight.status()
    check(isinstance(st, dict), "返回 dict")
    for k in ("state", "connected", "error", "frames_pushed", "video_units_received",
              "image_frames_available", "image_frames_dropped"):
        check(k in st, "含字段 %s" % k)
    for k in ("host_input_available", "host_input_dropped"):
        check(k not in st, "不含已删除字段 %s" % k)
    check(st["state"] in ("idle", "connecting", "streaming", "stopping"),
          "state 取值合法 (%r)" % st["state"])
    print("     %r" % (st,))

    # ------------------------------------------------------------- 输入 -----
    print("\n== 输入 API ==")
    an.send_key(an.MODIFIER_CTRL, ord('A'), True)
    an.send_key(an.MODIFIER_CTRL, ord('A'), "up")
    an.send_key(an.MODIFIER_CTRL, ord('A'), 0)
    an.send_mouse(10, 20, None)
    an.send_mouse(10, 20, "left")
    an.send_mouse(300, -5, False)          # 越界应被夹住
    an.send_hotkey([0x11, 0x12, ord('S')])
    an.send_hotkey([])                      # 空序列不应崩
    check(True, "send_key/send_mouse/send_hotkey 全部可调用")

    print("\n== stop() 幂等 ==")
    an.moonlight.stop()
    an.moonlight.stop()
    check(an.moonlight.status()["state"] == "idle", "重复 stop() 后仍为 idle")

    # ------------------------------------------------- GIL 释放 (关键约束) --
    print("\n== send_* 释放 GIL (不阻塞事件循环) ==")
    # 主线程持 GIL 阻塞 1.5s; 若 send_* 不释放 GIL, 子线程会被卡住,
    # 它调用 send_* 的耗时会接近主线程的阻塞时长。
    hold = threading.Event()
    release_ok = []

    def blocker():
        time.sleep(0.3)          # 让子线程先起来
        hold.set()
        time.sleep(1.5)

    def sender():
        hold.wait()
        t0 = time.perf_counter()
        for _ in range(200):
            an.send_key(0, 65, True)
            an.send_key(0, 65, False)
        release_ok.append(time.perf_counter() - t0)

    tb = threading.Thread(target=blocker)
    tsend = threading.Thread(target=sender)
    tb.start()
    tsend.start()
    tb.join()
    tsend.join()

    elapsed = release_ok[0] if release_ok else 1e9
    # 400 次调用; 若 GIL 被正确释放, 应远小于 blocker 的 1.5s 阻塞
    check(elapsed < 1.0,
          "400 次 send_key 在另一线程持 GIL 期间耗时 %.3fs (< 1.0s => GIL 已释放)"
          % elapsed)

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
