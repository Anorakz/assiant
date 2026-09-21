// ============================================================================
//  native/binding_utils.h — binding.cpp 与绑定层测试共用的转换/解析工具
//
//  为什么单独放一个头
//  ---------------------------------------------------------------------------
//  这些函数 (Frame -> numpy、action 解析) 都需要**在持有
//  GIL 的前提下**运行, 且必须能脱离 MoonlightAdapter 单独测试:
//  环形缓冲在还没收到帧时读出来是空的, 所以"能 import、能调用"证明不了 numpy
//  那条路径的正确性 (形状/dtype/布局/像素值)。抽到头文件里, 测试就可以直接
//  构造已知图案的 Frame 调用同一份代码逐字节核对, 而不是复制一份实现。
//
//  约定
//  ---------------------------------------------------------------------------
//  · 全部 inline, 避免多处 include 触犯 ODR
//  · 不引入任何 Python 之外的头, 也不依赖 moonlight-common-c
// ============================================================================

#pragma once

#include <pybind11/numpy.h>
#include <pybind11/pybind11.h>

#include <cstdint>
#include <cstring>
#include <string>

#include "image_rb.h"
#include "input_sender.h"

namespace agent {
namespace binding {

// ---------------------------------------------------------------------------
//  action 解析
// ---------------------------------------------------------------------------
// send_key / send_mouse 的 action 参数同时接受:
//   bool     True=按下 False=抬起
//   整数     非 0=按下 0=抬起
//   字符串   "down"/"press"/"1"/"true"  => 按下
//            "up"/"release"/"0"/"false" => 抬起
// 这样 Python 侧写 True、1、"down" 都可以, 不必记住某个特定约定。
inline bool parse_press(const pybind11::object& action) {
    if (pybind11::isinstance<pybind11::bool_>(action)) {
        return action.cast<bool>();
    }
    if (pybind11::isinstance<pybind11::int_>(action)) {
        return action.cast<long long>() != 0;
    }
    if (pybind11::isinstance<pybind11::str>(action)) {
        std::string s = action.cast<std::string>();
        for (char& c : s) {
            if (c >= 'A' && c <= 'Z') {
                c = static_cast<char>(c - 'A' + 'a');
            }
        }
        if (s == "down" || s == "press" || s == "pressed" || s == "1" || s == "true") {
            return true;
        }
        if (s == "up" || s == "release" || s == "released" || s == "0" || s == "false") {
            return false;
        }
        throw pybind11::value_error(
            "action must be a bool, an int, or one of 'down'/'up'/'press'/'release' (got '" +
            s + "')");
    }
    throw pybind11::type_error("action must be bool, int or str");
}

/// 鼠标按钮名 -> input_sender 的 kButton*; 返回 0 表示"不按键, 只移动"
inline std::uint32_t parse_button(const pybind11::object& action) {
    if (action.is_none()) {
        return 0;
    }
    if (pybind11::isinstance<pybind11::str>(action)) {
        std::string s = action.cast<std::string>();
        for (char& c : s) {
            if (c >= 'A' && c <= 'Z') {
                c = static_cast<char>(c - 'A' + 'a');
            }
        }
        if (s.empty()) {
            return 0;
        }
        if (s == "left" || s == "l") return input_sender::kButtonLeft;
        if (s == "middle" || s == "m") return input_sender::kButtonMiddle;
        if (s == "right" || s == "r") return input_sender::kButtonRight;
        if (s == "x1") return input_sender::kButtonX1;
        if (s == "x2") return input_sender::kButtonX2;
        throw pybind11::value_error(
            "unknown mouse button '" + s +
            "' (expected left/right/middle/x1/x2, or None for move-only)");
    }
    if (pybind11::isinstance<pybind11::int_>(action)) {
        return static_cast<std::uint32_t>(action.cast<long long>());
    }
    throw pybind11::type_error("action must be None, an int button code, or a button name");
}

// ---------------------------------------------------------------------------
//  Frame -> numpy
// ---------------------------------------------------------------------------
/// 把一帧 RGB888 拷成 numpy uint8 (256,256,3)
///
/// @note 必须持有 GIL: py::array_t 的构造会分配 Python 对象。
/// @note 这里是一次 196KB 的 memcpy —— 之所以不返回"零拷贝视图", 是因为
///       环形缓冲的槽位随时可能被生产者覆盖, 不能让 Python 侧长期持有裸指针。
inline pybind11::object frame_to_numpy(const Frame& f) {
    pybind11::array_t<std::uint8_t> arr({kFrameBytes});
    std::memcpy(arr.mutable_data(), f.data, kFrameBytes);
    return arr.reshape({256, 256, 3});
}

/// 读最新一帧并转 numpy; 没数据返回 None
///
/// @note 只把"读缓冲 + 拷帧"放到 GIL 之外, numpy 数组的分配仍在 GIL 内。
inline pybind11::object read_latest_numpy(ImageRingBuffer& rb) {
    Frame frame;
    bool ok = false;
    {
        pybind11::gil_scoped_release release;
        ok = rb.read_latest(frame);
    }
    if (!ok) {
        return pybind11::none();
    }
    return frame_to_numpy(frame);
}

/// 按时间戳取帧并转 numpy; 没数据返回 None
inline pybind11::object read_by_timestamp_numpy(ImageRingBuffer& rb, std::int64_t ts) {
    Frame frame;
    bool ok = false;
    {
        pybind11::gil_scoped_release release;
        ok = rb.read_by_timestamp(ts, frame);
    }
    if (!ok) {
        return pybind11::none();
    }
    return frame_to_numpy(frame);
}

}  // namespace binding
}  // namespace agent
