// ============================================================================
//  native/binding.cpp — pybind11 绑定层
//
//  暴露给 Python 的名字空间
//  ---------------------------------------------------------------------------
//      agent_native.moonlight.start_with_session(host, app, w, h, fps,
//                                                app_version, gfe_version,
//                                                codec_mode_support, session_url) -> bool
//      agent_native.moonlight.stop()                      -> None
//      agent_native.moonlight.status()                    -> dict
//
//      agent_native.image_rb.read_latest()                -> numpy (256,256,3) uint8 | None
//      agent_native.image_rb.read_by_timestamp(ts)        -> numpy (256,256,3) uint8 | None
//      agent_native.image_rb.size()                       -> int
//
//      agent_native.send_key(modifier, key, action)
//      agent_native.send_mouse(x, y, action)
//      agent_native.send_hotkey(keys)
//
//  设计要点
//  ---------------------------------------------------------------------------
//  · **单一适配器实例**: MoonlightAdapter 内部持有 image_rb,
//    所以绑定层不能再各建一个环形缓冲 —— 否则 moonlight 写一个、Python 读另一个,
//    永远读不到数据。这里用一个进程级单例, 子模块都引用它的成员。
//
//  · **GIL**: 所有会阻塞的调用 (send_* / start / stop, 以及 196KB 的帧拷贝)
//    都用 py::call_guard<py::gil_scoped_release>() 释放 GIL, 否则会卡住板端
//    asyncio 事件循环。注意 numpy 数组的**分配**必须在持有 GIL 时进行, 所以
//    read_latest 是"先释放 GIL 读进本地缓冲, 再持 GIL 建数组"。
//
//  · **SPSC 语义**: image_rb 是单生产者单消费者无锁结构。
//    生产者是 moonlight 内部线程; 消费者**必须是同一个 Python 线程**。
//    从多个 Python 线程并发调用 read_* 是未定义行为, docstring 里已写明。
//
//  · **numpy 支持不需要 numpy 的 C 头文件**: pybind11/numpy.h 自带 API 结构体声明,
//    运行期通过 import numpy.core._multiarray_umath 取符号。所以交叉编译期
//    不需要 sysroot 里有 numpy 头 (板端有 numpy 即可)。
// ============================================================================

#include <pybind11/numpy.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <cstdint>
#include <memory>
#include <string>
#include <vector>

#include "binding_utils.h"
#include "image_rb.h"
#include "input_sender.h"
#include "moonlight_adapter.h"

#ifdef AGENT_HAVE_MOONLIGHT
// 只有交叉编译才配置了这个头文件的搜索路径与宏 (见 native/CMakeLists.txt):
// LiGetLaunchUrlQueryParameters() 的声明在 Limelight.h 里。
#include "Limelight.h"
#endif

namespace py = pybind11;
using agent::AdapterState;
using agent::ImageRingBuffer;
using agent::MoonlightAdapter;
// 转换/解析工具 (Frame->numpy, action 解析) 在 binding_utils.h,
// 与 tests/host/test_binding_numpy.cpp 共用同一份实现。
using agent::binding::frame_to_numpy;
using agent::binding::parse_button;
using agent::binding::parse_press;
using agent::binding::read_by_timestamp_numpy;
using agent::binding::read_latest_numpy;

namespace {

// ---------------------------------------------------------------------------
//  进程级单例
// ---------------------------------------------------------------------------
// 刻意用不析构的 shared_ptr: Python 侧的 py::module 引用与 moonlight 回调线程
// 都间接指向这个适配器, 而回调线程的生命周期不受 CPython finalization 控制。
// 让它在进程结束时由操作系统回收, 可以避免"解释器关闭顺序"带来的销毁竞态。
// (环形缓冲本身是定长堆分配, 59MB 左右, 与架构文档一致。)
std::shared_ptr<MoonlightAdapter>& adapter_singleton() {
    static std::shared_ptr<MoonlightAdapter>* holder = [] {
        auto* p = new std::shared_ptr<MoonlightAdapter>(std::make_shared<MoonlightAdapter>());
        return p;
    }();
    return *holder;
}

MoonlightAdapter& adapter() {
    return *adapter_singleton();
}

// ---------------------------------------------------------------------------
//  状态 <-> 字符串
// ---------------------------------------------------------------------------
const char* state_name(AdapterState s) {
    switch (s) {
        case AdapterState::kIdle: return "idle";
        case AdapterState::kConnecting: return "connecting";
        case AdapterState::kStreaming: return "streaming";
        case AdapterState::kStopping: return "stopping";
    }
    return "unknown";
}

}  // namespace

// ===========================================================================
//  模块
// ===========================================================================
PYBIND11_MODULE(agent_native, m) {
    m.doc() = "agent native extension (moonlight / image_rb / input)";
    m.attr("__version__") = "0.2.0";

    // 保留原有自检入口 (scripts/deploy.ps1 用它做部署校验)
    m.def("ping", []() { return "pong"; });

    // ------------------------------------------------------------- moonlight --
    py::module_ ml = m.def_submodule("moonlight", "Moonlight 连接与接收线程");

    ml.def(
        "start_with_session",
        [](const std::string& host, const std::string& app, int width, int height, int fps,
           const std::string& app_version, const std::string& gfe_version,
           int codec_mode_support, const std::string& session_url) {
            bool ok = false;
            {
                // LiStartConnection 是阻塞的, 必须放开 GIL
                py::gil_scoped_release release;
                ok = adapter().start_with_session(host, app, width, height, fps,
                                                  app_version, gfe_version,
                                                  codec_mode_support, session_url);
            }
            return ok;
        },
        py::arg("host"), py::arg("app"), py::arg("w"), py::arg("h"), py::arg("fps"),
        py::arg("app_version"), py::arg("gfe_version"), py::arg("codec_mode_support"),
        py::arg("session_url"),
        "连接主机并开始收流 —— 握手**已经在外面做完了**。\n"
        "Sunshine 的 /serverinfo /applist /launch 在 HTTPS 47984 + 客户端证书后面,\n"
        "那半由 agent/net/sunshine_client.py 负责; 这里只接 app_version 与 session_url\n"
        "(= /launch 或 /resume 的 sessionUrl0) 直接进 LiStartConnection, 一次 HTTP 都不发。\n"
        "失败原因见 status()['error']。");

    // LiGetLaunchUrlQueryParameters() 由 moonlight-common-c 提供, 返回的串要追加到
    // /launch 与 /resume 的 query 后面 (Sunshine 的扩展参数)。导出它是为了这份知识
    // 只有一处真源: Python 侧拼串时用它, 而不是抄一份常量进 Python。
    // host 构建没有链接 moonlight-common-c -> 返回空串。
    ml.def(
        "launch_url_query_parameters",
        []() -> std::string {
#ifdef AGENT_HAVE_MOONLIGHT
            const char* params = LiGetLaunchUrlQueryParameters();
            return params != nullptr ? std::string(params) : std::string();
#else
            return std::string();
#endif
        },
        "追加到 /launch 与 /resume query 串后面的库级扩展参数\n"
        "(LiGetLaunchUrlQueryParameters(); 本版本返回 \"&corever=1\")。\n"
        "host 构建未链接 moonlight-common-c, 返回空串。");

    ml.def(
        "stop",
        []() {
            py::gil_scoped_release release;
            adapter().stop();
        },
        "断开并停止接收线程; 可重复调用。");

    ml.def(
        "status",
        []() {
            py::dict d;
            MoonlightAdapter& a = adapter();
            const AdapterState st = a.state();
            d["state"] = state_name(st);
            d["state_code"] = static_cast<int>(st);
            d["connected"] = a.is_connected();
            d["error"] = a.last_error();
            d["frames_pushed"] = a.frames_pushed();
            d["video_units_received"] = a.video_units_received();
            d["image_frames_available"] = a.image_rb().size();
            d["image_frames_dropped"] = a.image_rb().overruns();
            return d;
        },
        "返回当前状态快照 (dict)。");

    // --------------------------------------------------------------- image_rb --
    py::module_ irb = m.def_submodule("image_rb", "视频帧环形缓冲 (256x256 RGB888)");

    irb.def(
        "read_latest",
        []() { return read_latest_numpy(adapter().image_rb()); },
        "取最近一帧 (非破坏性: 不推进游标), 返回 numpy uint8 (256,256,3)。\n"
        "还没有任何帧时返回 None。\n"
        "⚠ 只能由同一个 Python 线程调用 (SPSC)。");

    irb.def(
        "read_by_timestamp",
        [](std::int64_t ts) { return read_by_timestamp_numpy(adapter().image_rb(), ts); },
        py::arg("ts"),
        "取时间戳最接近且不超过 ts 的一帧 (纳秒), 返回 numpy uint8 (256,256,3)。\n"
        "非破坏性: 同一个 ts 重复调用结果一致。\n"
        "没有满足条件的帧时返回 None。");

    irb.def(
        "size",
        []() { return adapter().image_rb().size(); },
        "当前可读帧数。");

    irb.def(
        "capacity",
        []() { return adapter().image_rb().capacity(); },
        "固定容量 (kImageRingCapacity)。");

    irb.def(
        "overruns",
        []() { return adapter().image_rb().overruns(); },
        "因覆盖而丢掉的未读帧数累计值。");

    // --------------------------------------------------------- host_input_rb --
    // 已删除 (Phase 6 收尾): 原来这里导出 host_input_rb.read_latest/read_all/size/
    // capacity/overruns。那整条路是死代码 —— moonlight-common-c 没有"主机→客户端"
    // 的输入 API, 所以 HostInputRingBuffer 从来没有生产者, Python 侧读到的永远是空。
    // 留着只会让人以为"主机键盘回传"这条路是通的。详见 todo.md 决策记录。

    // ------------------------------------------------------------------ 输入 --
    m.def(
        "send_key",
        [](std::uint32_t modifier, std::uint32_t key, const py::object& action) {
            const bool press = parse_press(action);
            agent::input_sender::send_key(modifier, key, press);
        },
        py::arg("modifier"), py::arg("key"), py::arg("action"),
        py::call_guard<py::gil_scoped_release>(),
        "发送一次按键。\n"
        "  modifier : 修饰键位掩码 (MODIFIER_* 的按位或)\n"
        "  key      : Windows 虚拟键码 (VK)\n"
        "  action   : True/'down' 按下, False/'up' 抬起");

    m.def(
        "send_mouse",
        [](int x, int y, const py::object& action) {
            // 先定位再按键 —— 与真实鼠标"移动后点击"的顺序一致
            agent::input_sender::send_mouse(x, y);

            const std::uint32_t button = parse_button(action);
            if (button == 0) {
                return;  // action 为空 -> 纯移动
            }
            const bool press = py::isinstance<py::bool_>(action) || py::isinstance<py::int_>(action)
                                   ? parse_press(action)
                                   : true;  // 传按钮名时默认按下
            agent::input_sender::send_mouse_button(press, button);
        },
        py::arg("x"), py::arg("y"), py::arg("action") = py::none(),
        py::call_guard<py::gil_scoped_release>(),
        "把鼠标移动到 ROI 坐标 (x, y) 并可选地按一个键。\n"
        "  x, y   : 0..255 (参考平面 256x256, 越界会被夹住)\n"
        "  action : None            -> 只移动\n"
        "           True/False/1/0  -> 左键 按下/抬起\n"
        "           'left'/'right'/'middle'/'x1'/'x2' -> 该键 按下");

    m.def(
        "send_hotkey",
        [](const std::vector<std::uint32_t>& keys) {
            agent::input_sender::send_hotkey(keys);
        },
        py::arg("keys"),
        py::call_guard<py::gil_scoped_release>(),
        "发送组合键: 按顺序逐个按下, 再按相反顺序逐个抬起。\n"
        "⚠ keys 必须自带修饰键的 VK 码, 例如 Ctrl+Alt+S = [0x11, 0x12, ord('S')]。");

    // -------------------------------------------------------- 测试协作接口 --
    // 供 tests/board/push_test_frame.cpp 在同一进程内取到**同一个** image_rb,
    // 以便往里面塞已知图案的帧来核对 numpy 数据通路。
    //
    // 之所以要这个: agent_native 与 agent_native_test 是两个独立的 .so, 各有
    // 自己的全局变量, 不能靠"各自的单例"共享缓冲。这里把一个**不持有所有权**
    // 的裸指针包成 capsule 传出去 (无析构器: 缓冲归适配器单例所有, 而该单例
    // 按设计活到进程结束)。
    //
    // 不是生产 API, 正常 Python 代码不要调用。
    m.def("_image_rb_capsule", []() {
        return py::capsule(static_cast<void*>(&adapter().image_rb()), "agent::ImageRingBuffer");
    }, "内部/测试用: 返回 image_rb 的裸指针 capsule (不要在生产代码里用)");

    // -------------------------------------------------------------- 常量 ------
    m.attr("MODIFIER_NONE") = static_cast<std::uint32_t>(agent::input_sender::kModifierNone);
    m.attr("MODIFIER_SHIFT") = static_cast<std::uint32_t>(agent::input_sender::kModifierShift);
    m.attr("MODIFIER_CTRL") = static_cast<std::uint32_t>(agent::input_sender::kModifierCtrl);
    m.attr("MODIFIER_ALT") = static_cast<std::uint32_t>(agent::input_sender::kModifierAlt);
    m.attr("MODIFIER_META") = static_cast<std::uint32_t>(agent::input_sender::kModifierMeta);

    m.attr("BUTTON_LEFT") = static_cast<std::uint32_t>(agent::input_sender::kButtonLeft);
    m.attr("BUTTON_MIDDLE") = static_cast<std::uint32_t>(agent::input_sender::kButtonMiddle);
    m.attr("BUTTON_RIGHT") = static_cast<std::uint32_t>(agent::input_sender::kButtonRight);

    m.attr("FRAME_WIDTH") = 256;
    m.attr("FRAME_HEIGHT") = 256;
    m.attr("FRAME_CHANNELS") = 3;
    m.attr("IMAGE_RING_CAPACITY") = agent::kImageRingCapacity;
}
