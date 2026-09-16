// ============================================================================
//  tests/host/test_binding_numpy.cpp — 验证 binding 层的 numpy / dict 转换通路
//
//  为什么需要它
//  ---------------------------------------------------------------------------
//  image_rb.read_latest() 在还没有帧时返回 None, 所以"能 import、调用不报错"
//  证明不了 numpy 那条路径的正确性 (形状 / dtype / 内存布局 / 像素值)。
//  这里直接在 C++ 侧构造已知图案的 Frame, 调用 **与 binding.cpp 同一份**
//  agent::binding::frame_to_numpy(), 再把它当 C 连续的 (256,256,3) uint8
//  逐字节核对。
//
//  构建与运行 (WSL, 需要 numpy):
//      EXT=$(python3 -c "import sysconfig;print(sysconfig.get_config_var('EXT_SUFFIX'))")
//      g++ -std=c++17 -O1 -shared -fPIC -I native
//          -I native/third_party/pybind11/include
//          -I $(python3 -c "import sysconfig;print(sysconfig.get_paths()['include'])")
//          tests/host/test_binding_numpy.cpp
//          -o /tmp/agent_native_test$EXT
//      PYTHONPATH=/tmp python3 tests/host/test_binding_numpy.py
//
//  只依赖 binding_utils.h (不拉进 moonlight_adapter / decoder), 所以构建很快。
//  不进 ctest: 它需要 numpy, 由人工/CI 按需运行。
// ============================================================================

#include <pybind11/pybind11.h>

#include "../../native/binding_utils.h"

namespace py = pybind11;
using agent::InputEvent;
using agent::Frame;
namespace ab = agent::binding;

PYBIND11_MODULE(agent_native_test, m) {
    m.doc() = "binding_utils.h 的 numpy / dict 转换测试入口";

    m.def("frame_to_numpy", [](py::bytes raw) {
        Frame f{};
        const std::string s = raw;
        std::memcpy(f.data, s.data(), agent::kFrameBytes);
        return ab::frame_to_numpy(f);
    });

    m.def("event_to_dict", [](int type, std::uint32_t mod, std::uint32_t key, int x, int y,
                              int action, std::int64_t ts) {
        InputEvent e{};
        e.type = (type == 0) ? InputEvent::KEY : InputEvent::MOUSE;
        e.modifier = mod;
        e.key = key;
        e.x = x;
        e.y = y;
        e.action = (action == 0) ? InputEvent::PRESS : InputEvent::RELEASE;
        e.timestamp_ns = ts;
        return ab::event_to_dict(e);
    });

    m.def("parse_press", &ab::parse_press);
    m.def("parse_button", &ab::parse_button);
    m.def("frame_bytes", []() { return agent::kFrameBytes; });
}
