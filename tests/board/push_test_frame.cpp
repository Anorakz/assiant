// ============================================================================
//  tests/board/push_test_frame.cpp — 板端测试用扩展 (agent_native_test)
//
//  为什么单独做一个扩展
//  ---------------------------------------------------------------------------
//  正式模块 agent_native 刻意**不**暴露"往 image_rb 写帧"的接口 (生产者只有
//  moonlight 视频线程)。但板端要验证 numpy 数据通路, 就必须有帧进缓冲 ——
//  否则 read_latest() 永远返回 None, "不崩"证明不了形状/内容正确。
//
//  这里用一个**只用于测试**的扩展往同一个 ImageRingBuffer 写帧:
//      · 与 agent_native 在同一个进程里, 所以拿到的是同一个环形缓冲
//        (靠 binding_utils / moonlight_adapter 的进程级单例? 不是 ——
//         见下, 这里用显式共享指针)
//      · agent_native.image_rb.* 读的就是它写的那个缓冲
//
//  ⚠ 关键点: agent_native 与 agent_native_test 是两个独立的 .so, 各有自己的
//    全局变量。所以不能靠"各自的单例"共享缓冲, 必须让 agent_native 暴露一个
//    "拿到适配器内部 ImageRingBuffer 指针" 的隐式约定。
//    这里采用的是 pybind11 的 capsule 传递: agent_native 提供 _image_rb_capsule(),
//    本扩展 import agent_native 并把 capsule 取出来, 于是两边指向同一个对象。
//
//  构建 (交叉编译, 需要 sysroot 的 python3.8 头):
//      ARM=E:/rk3568/arm/bin/aarch64-none-linux-gnu-g++
//      PB=native/third_party/pybind11/include
//      PY=E:/rk3568/sysroot/usr/include/python3.8
//      $ARM -std=c++17 -O2 -shared -fPIC -I native -I $PB -I $PY
//           -I E:/rk3568/sysroot/usr/include/aarch64-linux-gnu
//           tests/board/push_test_frame.cpp native/image_rb.cpp
//           -o build-rk3568/agent_native_test.cpython-38-aarch64-linux-gnu.so
// ============================================================================

#include <pybind11/pybind11.h>

#include "../../native/image_rb.h"

namespace py = pybind11;

namespace {

/// 从 agent_native._image_rb_capsule() 取得共享的 ImageRingBuffer*
agent::ImageRingBuffer* shared_image_rb() {
    py::module_ mod = py::module_::import("agent_native");
    py::object cap = mod.attr("_image_rb_capsule")();
    void* p = cap.cast<py::capsule>().get_pointer();
    return static_cast<agent::ImageRingBuffer*>(p);
}

/// 生成第 seed 号的可复现图案 (与测试脚本里的 expected_pattern 一致)
void fill_pattern(agent::Frame& f, int seed) {
    // 简单线性同余, 保证 Python 侧能独立复现
    std::uint32_t x = 0x9E3779B9u ^ static_cast<std::uint32_t>(seed) * 2654435761u;
    for (std::size_t i = 0; i < agent::kFrameBytes; ++i) {
        x = x * 1664525u + 1013904223u;
        f.data[i] = static_cast<std::uint8_t>(x >> 24);
    }
}

}  // namespace

PYBIND11_MODULE(agent_native_test, m) {
    m.doc() = "板端测试用: 往 agent_native 的 image_rb 里推入已知图案的帧";

    m.def("push_test_frame", [](std::int64_t ts, int seed) {
        agent::Frame f{};
        fill_pattern(f, seed);
        f.timestamp_ns = ts;
        shared_image_rb()->push(f);
    }, py::arg("ts"), py::arg("seed") = 0);

    m.def("expected_pattern", [](int seed) {
        agent::Frame f{};
        fill_pattern(f, seed);
        return py::bytes(reinterpret_cast<const char*>(f.data), agent::kFrameBytes);
    }, py::arg("seed") = 0);
}
