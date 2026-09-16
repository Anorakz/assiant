// ============================================================================
//  moonlight_adapter.cpp — 连接管理 + 两条接收线程实现
//
//  两种构建模式
//  ---------------------------------------------------------------------------
//  定义了 AGENT_HAVE_MOONLIGHT (交叉编译):
//      真正调用 LiStartConnection / LiStopConnection, 并注册 C 回调。
//
//  未定义 (host 构建):
//      不引用 moonlight 符号。start() 会做完整参数校验 + HTTP 握手,
//      然后在"要调 LiStartConnection"那一步返回 false 并说明原因。
//      这样 host 上仍能验证: 状态机、参数校验、HTTP/XML 解析、
//      以及 on_video_frame() 的"Annex-B → 解码 → 预处理 → 写 RB"通路。
//
//  回调是 C 风格的静态函数, 通过单例指针找回实例 —— moonlight 的 API
//  没有 user context 交给 submitDecodeUnit, 只能用文件级单例。
// ============================================================================

#include "moonlight_adapter.h"

#include "moonlight_connection.h"
#include "preprocess.h"

#include <cstdio>
#include <cstring>
#include <mutex>
#include <vector>

#ifdef AGENT_HAVE_MOONLIGHT
extern "C" {
#include "Limelight.h"
}
#endif

namespace agent {
namespace {

/// moonlight 的 submitDecodeUnit 没有 user context, 只能用单例找回实例。
/// 用原子指针, 保证在 stop() 之后回调不会再摸到已析构的对象。
std::atomic<MoonlightAdapter*> g_instance{nullptr};

void set_log(const std::string& msg) {
    std::fprintf(stderr, "[moonlight] %s\n", msg.c_str());
}

#ifdef AGENT_HAVE_MOONLIGHT
// moonlight 的 C 回调定义在本文件末尾 (它们在 moonlight 自己的线程上被调用)。
// start() 里要取这些函数指针, 所以先在这里声明。
int cb_submit_decode_unit(PDECODE_UNIT du);
int cb_decoder_setup(int videoFormat, int width, int height, int redrawRate, void* context,
                     int drFlags);
void cb_decoder_start();
void cb_decoder_stop();
void cb_decoder_cleanup();
void cb_conn_started();
void cb_conn_terminated(int errorCode);
void cb_conn_stage_starting(int stage);
void cb_conn_stage_failed(int stage, int errorCode);
void cb_conn_log(const char* format, ...);
#endif

}  // namespace

struct MoonlightAdapter::Impl {
    ImageRingBuffer image_rb;
    HostInputRingBuffer input_rb;
    Decoder decoder;

    std::atomic<int> state{static_cast<int>(AdapterState::kIdle)};
    std::atomic<std::size_t> video_units{0};
    std::atomic<std::size_t> frames_pushed{0};

    std::string host;
    std::string app;
    int width = 0;
    int height = 0;
    int fps = 0;
    std::string unique_id = "0123456789ABCDEF";

    mutable std::mutex err_mutex;
    std::string last_error;

    void set_error(const std::string& e) {
        std::lock_guard<std::mutex> lk(err_mutex);
        last_error = e;
    }
};

MoonlightAdapter::MoonlightAdapter() : impl_(new Impl()) {}

MoonlightAdapter::~MoonlightAdapter() {
    stop();
}

// ===========================================================================
//  start / stop
// ===========================================================================
bool MoonlightAdapter::start(const std::string& host,
                             const std::string& app,
                             int width,
                             int height,
                             int fps) {
    if (impl_->state.load() != static_cast<int>(AdapterState::kIdle)) {
        impl_->set_error("already started (call stop() first)");
        return false;
    }
    if (host.empty()) {
        impl_->set_error("empty host");
        return false;
    }
    if (app.empty()) {
        impl_->set_error("empty app");
        return false;
    }
    if (width <= 0 || height <= 0 || fps <= 0) {
        impl_->set_error("invalid stream dimensions");
        return false;
    }

    impl_->host = host;
    impl_->app = app;
    impl_->width = width;
    impl_->height = height;
    impl_->fps = fps;
    impl_->set_error("");
    impl_->state.store(static_cast<int>(AdapterState::kConnecting));

    // ---- 1) 初始化硬解 (H.265) ----
    if (!impl_->decoder.init(width, height, VideoCodec::kH265)) {
        impl_->set_error("decoder init failed");
        impl_->state.store(static_cast<int>(AdapterState::kIdle));
        return false;
    }

    // ---- 2) HTTP 握手: /serverinfo → appversion ----
    const auto si = moonlight_connection::fetch_server_info(host);
    if (!si.ok) {
        char buf[256];
        std::snprintf(buf, sizeof(buf),
                      "handshake failed: /serverinfo status=%d (connect=%s)",
                      si.status_code, si.raw.empty() ? "no" : "yes");
        impl_->set_error(buf);
        impl_->decoder.release();
        impl_->state.store(static_cast<int>(AdapterState::kIdle));
        return false;
    }
    set_log("serverinfo ok: host=" + si.hostname + " appversion=" + si.app_version +
            " pair_status=" + std::to_string(si.pair_status));

    // ---- 3) HTTP 握手: /launch → sessionUrl0 ----
    char mode[64];
    std::snprintf(mode, sizeof(mode), "%dx%dx%d", width, height, fps);
    const auto lr = moonlight_connection::launch_app(host, /*app_id=*/"", /*app_name=*/app, mode,
                                                     impl_->unique_id);
    if (!lr.ok) {
        char buf[320];
        std::snprintf(buf, sizeof(buf),
                      "handshake failed: /launch status=%d %s",
                      lr.status_code, lr.status_message.c_str());
        impl_->set_error(buf);
        impl_->decoder.release();
        impl_->state.store(static_cast<int>(AdapterState::kIdle));
        return false;
    }

    // ---- 4) 交给 moonlight 接管 ----
#ifndef AGENT_HAVE_MOONLIGHT
    impl_->set_error(
        "moonlight integration not linked in this build "
        "(AGENT_HAVE_MOONLIGHT undefined); HTTP handshake succeeded");
    impl_->decoder.release();
    impl_->state.store(static_cast<int>(AdapterState::kIdle));
    return false;
#else
    // 单例必须在 LiStartConnection 之前挂上 —— 回调随时可能进来
    g_instance.store(this);

    // --- 流配置 ---
    STREAM_CONFIGURATION sc;
    LiInitializeStreamConfiguration(&sc);
    sc.width = width;
    sc.height = height;
    sc.fps = fps;
    sc.bitrate = 20000;          // kbps; 后续按网络状况自适应
    sc.packetSize = 1024;
    sc.streamingRemotely = STREAM_CFG_AUTO;
    sc.audioConfiguration = AUDIO_CONFIGURATION_STEREO;
    // 只协商 H.265 (用户选择); 硬解走 hevc_v4l2m2m
    sc.supportedVideoFormats = VIDEO_FORMAT_H265;

    // --- 解码器回调 ---
    DECODER_RENDERER_CALLBACKS dr;
    LiInitializeVideoCallbacks(&dr);
    dr.setup = cb_decoder_setup;
    dr.start = cb_decoder_start;
    dr.stop = cb_decoder_stop;
    dr.cleanup = cb_decoder_cleanup;
    dr.submitDecodeUnit = cb_submit_decode_unit;
    dr.capabilities = 0;

    // --- 连接回调 ---
    CONNECTION_LISTENER_CALLBACKS cl;
    LiInitializeConnectionCallbacks(&cl);
    cl.stageStarting = cb_conn_stage_starting;
    cl.stageFailed = cb_conn_stage_failed;
    cl.connectionStarted = cb_conn_started;
    cl.connectionTerminated = cb_conn_terminated;
    cl.logMessage = cb_conn_log;

    // --- 服务器信息 ---
    SERVER_INFORMATION srv;
    LiInitializeServerInformation(&srv);
    srv.address = impl_->host.c_str();
    srv.serverInfoAppVersion = si.app_version.c_str();
    srv.serverInfoGfeVersion = si.gfe_version.c_str();
    srv.rtspSessionUrl = lr.session_url.c_str();
    srv.serverCodecModeSupport = static_cast<int>(si.codec_mode_support);

    // 音频先不接 (arCallbacks = nullptr)。moonlight 允许这样吗? 见文档:
    // 传 nullptr 表示不要音频。
    const int rc = LiStartConnection(&srv, &sc, &cl, &dr, nullptr,
                                     /*renderContext=*/nullptr, /*drFlags=*/0,
                                     /*audioContext=*/nullptr, /*arFlags=*/0);
    if (rc != 0) {
        g_instance.store(nullptr);
        impl_->set_error("LiStartConnection failed, rc=" + std::to_string(rc));
        impl_->decoder.release();
        impl_->state.store(static_cast<int>(AdapterState::kIdle));
        return false;
    }

    // LiStartConnection 返回即表示已连上并开始收流
    impl_->state.store(static_cast<int>(AdapterState::kStreaming));
    return true;
#endif
}

void MoonlightAdapter::stop() {
    if (impl_->state.load() == static_cast<int>(AdapterState::kIdle)) {
        // 即使空闲也要清掉单例, 避免析构后回调还指过来
        g_instance.store(nullptr);
        return;
    }

    impl_->state.store(static_cast<int>(AdapterState::kStopping));

#ifdef AGENT_HAVE_MOONLIGHT
    LiStopConnection();
#endif

    g_instance.store(nullptr);
    impl_->decoder.release();
    impl_->state.store(static_cast<int>(AdapterState::kIdle));
}

bool MoonlightAdapter::is_connected() const {
    return impl_->state.load() == static_cast<int>(AdapterState::kStreaming);
}

AdapterState MoonlightAdapter::state() const {
    return static_cast<AdapterState>(impl_->state.load());
}

std::string MoonlightAdapter::last_error() const {
    std::lock_guard<std::mutex> lk(impl_->err_mutex);
    return impl_->last_error;
}

ImageRingBuffer& MoonlightAdapter::image_rb() { return impl_->image_rb; }
HostInputRingBuffer& MoonlightAdapter::host_input_rb() { return impl_->input_rb; }

std::size_t MoonlightAdapter::video_units_received() const {
    return impl_->video_units.load();
}

std::size_t MoonlightAdapter::frames_pushed() const {
    return impl_->frames_pushed.load();
}

// ===========================================================================
//  视频通路: Annex-B → 解码 → 预处理 → 写 Image RB
// ===========================================================================
bool MoonlightAdapter::on_video_frame(const std::uint8_t* annex_b, std::size_t size) {
    if (annex_b == nullptr || size == 0) {
        return false;
    }
    if (impl_->state.load() != static_cast<int>(AdapterState::kStreaming)) {
        return false;
    }

    impl_->video_units.fetch_add(1);

    // 解码。注意硬解有流水线延迟: 前几帧返回 false 是正常现象。
    std::uint8_t* yuv = nullptr;
    int vw = 0;
    int vh = 0;
    if (!impl_->decoder.decode(annex_b, size, &yuv, &vw, &vh)) {
        return false;
    }
    if (yuv == nullptr || vw <= 0 || vh <= 0) {
        return false;
    }

    // 预处理: 整帧作为 ROI → 256×256 RGB888
    // 这里先做"全画面缩放到 256×256"; 真正的 ROI 策略 (跟随视线/焦点)
    // 后续再接。
    Frame frame{};
    const Roi roi{0, 0, vw, vh};
    preprocess_frame(yuv, vw, vh, roi, frame.data);
    frame.timestamp_ns = 0;  // TODO: 用 moonlight 的 presentationTimeUs

    impl_->image_rb.push(frame);
    impl_->frames_pushed.fetch_add(1);
    return true;
}

void MoonlightAdapter::on_connection_started() {
    impl_->state.store(static_cast<int>(AdapterState::kStreaming));
    set_log("connection started");
}

void MoonlightAdapter::on_connection_terminated(int error_code) {
    if (error_code != 0) {
        impl_->set_error("connection terminated, errorCode=" + std::to_string(error_code));
    }
    impl_->state.store(static_cast<int>(AdapterState::kIdle));
    set_log("connection terminated");
}

// ===========================================================================
//  C 风格回调 (moonlight 在它自己的线程上调用)
// ===========================================================================
#ifdef AGENT_HAVE_MOONLIGHT

namespace {

/// 视频线程: 每帧一个 DECODE_UNIT, 数据在 bufferList 链上 (Annex-B)
int cb_submit_decode_unit(PDECODE_UNIT du) {
    MoonlightAdapter* self = g_instance.load();
    if (self == nullptr || du == nullptr || du->fullLength <= 0) {
        return DR_OK;
    }

    // moonlight 把一帧拆成多个 buffer; 拼成连续缓冲再交给解码器
    std::vector<std::uint8_t> frame;
    frame.reserve(static_cast<std::size_t>(du->fullLength));
    for (PLENTRY e = du->bufferList; e != nullptr; e = e->next) {
        if (e->data != nullptr && e->length > 0) {
            frame.insert(frame.end(), reinterpret_cast<const std::uint8_t*>(e->data),
                         reinterpret_cast<const std::uint8_t*>(e->data) + e->length);
        }
    }

    if (!self->on_video_frame(frame.data(), frame.size())) {
        // 解码器没吃下这一帧 (常见于硬解起步阶段), 请求关键帧兜底
        // 只在确实出错时请求, 避免每帧都请求 IDR 把码率打爆。
        // 这里保守地不请求; 后续按 Decoder::status() 判断。
    }
    return DR_OK;
}

void cb_decoder_setup_ok() {}

int cb_decoder_setup(int videoFormat, int width, int height, int redrawRate, void* context,
                     int drFlags) {
    (void)videoFormat;
    (void)width;
    (void)height;
    (void)redrawRate;
    (void)context;
    (void)drFlags;
    // 解码器已在 start() 里初始化; moonlight 只要求这里返回 0
    return 0;
}

void cb_decoder_start() {
    MoonlightAdapter* self = g_instance.load();
    if (self != nullptr) {
        self->on_connection_started();
    }
}

void cb_decoder_stop() {}

void cb_decoder_cleanup() {}

void cb_conn_started() {
    MoonlightAdapter* self = g_instance.load();
    if (self != nullptr) {
        self->on_connection_started();
    }
}

void cb_conn_terminated(int errorCode) {
    MoonlightAdapter* self = g_instance.load();
    if (self != nullptr) {
        self->on_connection_terminated(errorCode);
    }
}

void cb_conn_stage_starting(int stage) {
    set_log("stage starting: " + std::to_string(stage));
}

void cb_conn_stage_failed(int stage, int errorCode) {
    set_log("stage " + std::to_string(stage) + " failed: " + std::to_string(errorCode));
}

void cb_conn_log(const char* format, ...) {
    (void)format;  // 需要 va_list 转发; 调试期先不接
}

}  // namespace

#endif  // AGENT_HAVE_MOONLIGHT

}  // namespace agent
