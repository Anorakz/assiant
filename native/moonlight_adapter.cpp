// ============================================================================
//  moonlight_adapter.cpp — 连接管理 + 视频接收线程实现
//
//  两种构建模式
//  ---------------------------------------------------------------------------
//  定义了 AGENT_HAVE_MOONLIGHT (交叉编译):
//      真正调用 LiStartConnection / LiStopConnection, 并注册 C 回调。
//
//  未定义 (host 构建):
//      不引用 moonlight 符号。start_with_session() 会做完整参数校验,
//      然后在"要调 LiStartConnection"那一步返回 false 并说明原因。
//      这样 host 上仍能验证: 状态机、参数校验、
//      以及 on_video_frame() 的"Annex-B → 解码 → 预处理 → 写 RB"通路。
//      (握手全在 Python 侧 —— 原来的 HTTP 分支已在 Phase 6 删除。)
//
//  回调是 C 风格的静态函数, 通过单例指针找回实例 —— moonlight 的 API
//  没有 user context 交给 submitDecodeUnit, 只能用文件级单例。
// ============================================================================

#include "moonlight_adapter.h"

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

/// moonlight 协议里的编码能力位 (Limelight.h 的 VIDEO_FORMAT_*)。
///
/// 为什么不直接 include Limelight.h 用那些宏: 这段映射是纯逻辑, 应该能在
/// 宿主机上单独测, 而 host 构建根本不链接 moonlight。它们是**协议常量**,
/// 不会跟着库版本变; 下面还有 static_assert 在交叉编译时和真头文件对一遍,
/// 所以抄一份不会悄悄跑偏。
constexpr int kFormatMaskH264 = 0x000F;
constexpr int kFormatMaskH265 = 0x0F00;
constexpr int kFormatMask10Bit = 0xAA00;
constexpr int kFormatMaskYuv444 = 0xCC04;

#ifdef AGENT_HAVE_MOONLIGHT
static_assert(kFormatMaskH264 == VIDEO_FORMAT_MASK_H264, "H.264 掩码和 Limelight.h 不一致");
static_assert(kFormatMaskH265 == VIDEO_FORMAT_MASK_H265, "H.265 掩码和 Limelight.h 不一致");
static_assert(kFormatMask10Bit == VIDEO_FORMAT_MASK_10BIT, "10bit 掩码和 Limelight.h 不一致");
static_assert(kFormatMaskYuv444 == VIDEO_FORMAT_MASK_YUV444, "444 掩码和 Limelight.h 不一致");
#endif

/// moonlight 的 submitDecodeUnit 没有 user context, 只能用单例找回实例。
/// 用原子指针, 保证在 stop() 之后回调不会再摸到已析构的对象。
std::atomic<MoonlightAdapter*> g_instance{nullptr};

void set_log(const std::string& msg) {
    std::fprintf(stderr, "[moonlight] %s\n", msg.c_str());
}

/// "0x0100" 这样的十六进制, 用于把 moonlight 的视频格式码打进日志
std::string to_hex(int value) {
    char buf[16];
    std::snprintf(buf, sizeof(buf), "%#06x", value);
    return std::string(buf);
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

// ---------------------------------------------------------------------------
//  视频格式 → 编码格式 (纯函数, 宿主机可测)
// ---------------------------------------------------------------------------
VideoFormatCheck classify_video_format(int video_format, VideoCodec* out_codec) {
    // 顺序有讲究: 先排除 10bit / 4:4:4, 再认编码。
    // H265_MAIN10 同时落在 H265 掩码里, 如果先认编码就会把 10bit 当成能解的
    // H.265 收下来 —— 然后解码器在运行时才发现格式不对。
    if ((video_format & kFormatMask10Bit) != 0) {
        return VideoFormatCheck::kTenBit;
    }
    if ((video_format & kFormatMaskYuv444) != 0) {
        return VideoFormatCheck::kYuv444;
    }
    if ((video_format & kFormatMaskH265) != 0) {
        if (out_codec != nullptr) {
            *out_codec = VideoCodec::kH265;
        }
        return VideoFormatCheck::kOk;
    }
    if ((video_format & kFormatMaskH264) != 0) {
        if (out_codec != nullptr) {
            *out_codec = VideoCodec::kH264;
        }
        return VideoFormatCheck::kOk;
    }
    return VideoFormatCheck::kUnknown;
}

struct MoonlightAdapter::Impl {
    ImageRingBuffer image_rb;
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
bool MoonlightAdapter::prepare_start(const std::string& host,
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
    return true;
}

bool MoonlightAdapter::start_with_session(const std::string& host,
                                          const std::string& app,
                                          int width,
                                          int height,
                                          int fps,
                                          const std::string& app_version,
                                          const std::string& gfe_version,
                                          int codec_mode_support,
                                          const std::string& session_url) {
    if (!prepare_start(host, app, width, height, fps)) {
        return false;
    }

    // 握手是调用方做完的 (Sunshine 那半在 agent/net/sunshine_client.py: HTTPS
    // 47984 + 客户端证书)。这里只校验拿到的字段 —— 一次 HTTP 都不发, 连主机地址
    // 都不碰, 所以"HTTPS 通了但这里失败"的原因只可能是字段不对。
    if (app_version.empty()) {
        impl_->set_error("empty app_version (需要 /serverinfo 的 <appversion>)");
        impl_->state.store(static_cast<int>(AdapterState::kIdle));
        return false;
    }
    if (session_url.empty()) {
        impl_->set_error(
            "empty session_url (需要 /launch 或 /resume 的 <sessionUrl0>)");
        impl_->state.store(static_cast<int>(AdapterState::kIdle));
        return false;
    }

    set_log("session supplied by caller: appversion=" + app_version +
            " session=" + session_url);
    return connect_limelight(app_version, gfe_version, codec_mode_support,
                             session_url);
}

bool MoonlightAdapter::connect_limelight(const std::string& app_version,
                                         const std::string& gfe_version,
                                         int codec_mode_support,
                                         const std::string& session_url) {
    // ---- 交给 moonlight 接管 ----
    //
    // ⚠ 解码器**不在这里**初始化: 用什么编码 (H.264/H.265) 是 LiStartConnection
    //   过程中协商出来的, 提前猜就是猜。真正的初始化在 cb_decoder_setup 里,
    //   那里能拿到 NegotiatedVideoFormat。
    //   失败也仍然是"快速失败": setup 返回非 0 会让 LiStartConnection 直接失败,
    //   所以连上了却一帧解不出来的情况不会发生。
#ifndef AGENT_HAVE_MOONLIGHT
    impl_->set_error(
        "moonlight integration not linked in this build "
        "(AGENT_HAVE_MOONLIGHT undefined)");
    impl_->state.store(static_cast<int>(AdapterState::kIdle));
    return false;
#else
    // 单例必须在 LiStartConnection 之前挂上 —— 回调随时可能进来
    g_instance.store(this);

    // --- 流配置 ---
    STREAM_CONFIGURATION sc;
    LiInitializeStreamConfiguration(&sc);
    // 尺寸/帧率来自 prepare_start() 落地的值 (两条入口都先过它)
    sc.width = impl_->width;
    sc.height = impl_->height;
    sc.fps = impl_->fps;
    sc.bitrate = 20000;          // kbps; 后续按网络状况自适应
    sc.packetSize = 1024;
    sc.streamingRemotely = STREAM_CFG_AUTO;
    sc.audioConfiguration = AUDIO_CONFIGURATION_STEREO;
    // 两种都报, 让服务端挑: 板端 MPP 对 H.264/H.265 都有硬解, 不挑食反而更兼容
    // (以前只报 H.265, 服务端没开 HEVC 时根本连不上)。
    sc.supportedVideoFormats = VIDEO_FORMAT_H264 | VIDEO_FORMAT_H265;

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
    srv.serverInfoAppVersion = app_version.c_str();
    srv.serverInfoGfeVersion = gfe_version.c_str();
    srv.rtspSessionUrl = session_url.c_str();
    srv.serverCodecModeSupport = codec_mode_support;

    // 音频先不接 (arCallbacks = nullptr)。moonlight 允许这样吗? 见文档:
    // 传 nullptr 表示不要音频。
    const int rc = LiStartConnection(&srv, &sc, &cl, &dr, nullptr,
                                     /*renderContext=*/nullptr, /*drFlags=*/0,
                                     /*audioContext=*/nullptr, /*arFlags=*/0);
    if (rc != 0) {
        g_instance.store(nullptr);
        // 解码器起不来是最常见的失败原因, 把它的原话带上, 别只说"rc=-1"
        const char* decoder_why = impl_->decoder.last_error();
        std::string detail = "LiStartConnection failed, rc=" + std::to_string(rc);
        if (decoder_why != nullptr && decoder_why[0] != '\0') {
            detail += "; decoder: ";
            detail += decoder_why;
        }
        impl_->set_error(detail);
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

std::size_t MoonlightAdapter::video_units_received() const {
    return impl_->video_units.load();
}

std::size_t MoonlightAdapter::frames_pushed() const {
    return impl_->frames_pushed.load();
}

// ===========================================================================
//  视频通路: 协商 → 初始化解码器 → Annex-B → 解码 → 预处理 → 写 Image RB
// ===========================================================================
bool MoonlightAdapter::on_decoder_setup(int video_format, int width, int height) {
    VideoCodec codec = VideoCodec::kH265;
    switch (classify_video_format(video_format, &codec)) {
        case VideoFormatCheck::kTenBit:
            impl_->set_error("协商到 10bit 编码 (HDR), 当前只支持 8bit; "
                             "请在 Sunshine 侧关掉 HDR / 10bit");
            set_log("decoder setup rejected: 10bit format 0x" + to_hex(video_format));
            return false;
        case VideoFormatCheck::kYuv444:
            impl_->set_error("协商到 4:4:4 编码, 当前只支持 4:2:0");
            set_log("decoder setup rejected: 444 format 0x" + to_hex(video_format));
            return false;
        case VideoFormatCheck::kUnknown:
            impl_->set_error("协商到不支持的视频格式 0x" + to_hex(video_format) +
                             " (只支持 H.264 / H.265)");
            set_log("decoder setup rejected: unknown format 0x" + to_hex(video_format));
            return false;
        case VideoFormatCheck::kOk:
        default:
            break;
    }

    if (width <= 0 || height <= 0) {
        width = impl_->width;
        height = impl_->height;
    }

    // 后端选择交给 Decoder 自己: kAuto = 先 MPP 硬解, 起不来才退 FFmpeg 软解,
    // 并且会把"为什么没用上硬解"记在 last_error() 里。
    if (!impl_->decoder.init(width, height, codec, DecoderBackend::kAuto)) {
        impl_->set_error(std::string("decoder init failed: ") + impl_->decoder.last_error());
        set_log(std::string("decoder init FAILED: ") + impl_->decoder.last_error());
        return false;
    }

    // 板端确认走的是哪条路全靠这一行 (status() 里没有这个字段)
    set_log(std::string("decoder ready: ") + impl_->decoder.active_decoder_name() +
            (impl_->decoder.is_hardware() ? " [硬件]" : " [软件]") + " codec=" +
            (codec == VideoCodec::kH265 ? "H.265" : "H.264") + " " +
            std::to_string(width) + "x" + std::to_string(height));
    const char* note = impl_->decoder.last_error();
    if (note != nullptr && note[0] != '\0') {
        set_log(std::string("decoder note: ") + note);
    }
    return true;
}

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
    (void)redrawRate;
    (void)context;
    (void)drFlags;
    MoonlightAdapter* self = g_instance.load();
    if (self == nullptr) {
        return -1;
    }
    // 返回非 0 会让 LiStartConnection 直接失败 —— 这正是想要的:
    // 编解码对不上 / 硬解起不来时, 宁可在连接阶段就明确失败。
    return self->on_decoder_setup(videoFormat, width, height) ? 0 : -1;
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
