// ============================================================================
//  decoder.cpp — Decoder 的分发层 + YUV 收拢 + FFmpeg 软解后端
//
//  三部分
//  ---------------------------------------------------------------------------
//  1) collapse_yuv420_to_i420()  纯函数, 两个后端共用, 也是最好测的一块
//  2) Decoder                    参数校验 + 选后端(含回退) + 状态转发
//  3) FfmpegSwBackend            FFmpeg 软解 (AGENT_HAVE_FFMPEG 才真的实现)
//
//  编译期开关
//  ---------------------------------------------------------------------------
//  两个宏都关掉时 (宿主机就是这样), 本文件仍然编译通过: 没有 FFmpeg 符号,
//  工厂返回 nullptr, Decoder::init() 会**明确失败并说明原因**。这样:
//    · host 单测不需要装 FFmpeg, 照样能测纯函数 + Decoder 的契约
//    · 交叉编译打开 AGENT_HAVE_MPP / AGENT_HAVE_FFMPEG, 用 sysroot 里的库
//
//  回退到底怎么判 (旧实现的 bug 就在这)
//  ---------------------------------------------------------------------------
//  旧代码: avcodec_find_decoder_by_name("hevc_v4l2m2m") 不为空 → 认为硬解可用
//          → avcodec_open2 失败 → return false (软解回退永远不执行)
//  现在:   候选后端**必须 init() 成功**才算可用, 失败的把原因记下来换下一个。
//          "名字存在"和"能不能开"是两件事, 只有后者算数。
// ============================================================================

#include "decoder.h"

#include "decoder_backend.h"

#include <cstring>
#include <string>
#include <vector>

#ifdef AGENT_HAVE_FFMPEG
extern "C" {
#include <libavcodec/avcodec.h>
#include <libavutil/error.h>
#include <libavutil/frame.h>
#include <libavutil/pixdesc.h>
#include <libavutil/pixfmt.h>
}
#endif

namespace agent {

// ===========================================================================
//  YUV420 → I420 收拢 (纯函数)
// ===========================================================================
void collapse_yuv420_to_i420(const std::uint8_t* y,
                             const std::uint8_t* u,
                             const std::uint8_t* v,
                             int y_stride,
                             int uv_stride,
                             YuvLayout layout,
                             bool uv_swapped,
                             int width,
                             int height,
                             std::uint8_t* i420_out) {
    // 参数非法就什么都不做 —— 宁可少解一帧, 也不要越界写坏内存
    if (y == nullptr || u == nullptr || i420_out == nullptr) {
        return;
    }
    if (layout == YuvLayout::kPlanar && v == nullptr) {
        return;
    }
    if (width <= 0 || height <= 0) {
        return;
    }
    // YUV420 的色度是 2×2 抽样; 宽高是奇数就没法完整表达, 明确拒绝而不是猜
    if ((width & 1) != 0 || (height & 1) != 0) {
        return;
    }
    if (y_stride < width || uv_stride < width) {
        return;
    }

    const int cw = width / 2;
    const int ch = height / 2;

    std::uint8_t* dst_y = i420_out;
    std::uint8_t* dst_u = dst_y + static_cast<std::size_t>(width) * height;
    std::uint8_t* dst_v = dst_u + static_cast<std::size_t>(cw) * ch;

    // Y 平面: 逐行拷贝, 顺手把 stride 收成 width
    for (int row = 0; row < height; ++row) {
        std::memcpy(dst_y + static_cast<std::size_t>(row) * width,
                    y + static_cast<std::size_t>(row) * y_stride,
                    static_cast<std::size_t>(width));
    }

    // uv_swapped 的语义: **源里第一个色度字节/平面是 V 而不是 U** (NV21 / YV12)
    if (layout == YuvLayout::kSemiPlanar) {
        // NV12 / NV21: 一块交错平面, 每 2 字节一组
        const int u_index = uv_swapped ? 1 : 0;  // U 在组内的下标
        const int v_index = uv_swapped ? 0 : 1;  // V 在组内的下标
        for (int row = 0; row < ch; ++row) {
            const std::uint8_t* src = u + static_cast<std::size_t>(row) * uv_stride;
            std::uint8_t* du = dst_u + static_cast<std::size_t>(row) * cw;
            std::uint8_t* dv = dst_v + static_cast<std::size_t>(row) * cw;
            for (int col = 0; col < cw; ++col) {
                du[col] = src[col * 2 + u_index];
                dv[col] = src[col * 2 + v_index];
            }
        }
    } else {
        // 已经是独立平面 (I420 / YV12): 各自收拢 stride。
        // YV12 时传进来的 u 其实是 V, 所以要按"哪个才是真 U"取。
        const std::uint8_t* src_u = uv_swapped ? v : u;
        const std::uint8_t* src_v = uv_swapped ? u : v;
        for (int row = 0; row < ch; ++row) {
            std::memcpy(dst_u + static_cast<std::size_t>(row) * cw,
                        src_u + static_cast<std::size_t>(row) * uv_stride,
                        static_cast<std::size_t>(cw));
            std::memcpy(dst_v + static_cast<std::size_t>(row) * cw,
                        src_v + static_cast<std::size_t>(row) * uv_stride,
                        static_cast<std::size_t>(cw));
        }
    }
}

void convert_nv12_to_i420(const std::uint8_t* nv12_y,
                          const std::uint8_t* nv12_uv,
                          int y_stride,
                          int uv_stride,
                          int width,
                          int height,
                          std::uint8_t* i420_out) {
    collapse_yuv420_to_i420(nv12_y, nv12_uv, nullptr, y_stride, uv_stride,
                            YuvLayout::kSemiPlanar, /*uv_swapped=*/false, width,
                            height, i420_out);
}

// ===========================================================================
//  Decoder::Impl — 只放"跨后端"的东西
// ===========================================================================
struct Decoder::Impl {
    int width = 0;
    int height = 0;
    DecoderBackend requested = DecoderBackend::kAuto;
    DecodeStatus last_status = DecodeStatus::kNotInitialized;

    /// 初始化期说明 (含"为什么没用上硬解"); 见 Decoder::last_error()
    std::string note;

    std::unique_ptr<detail::VideoDecoderBackend> backend;

    void reset() {
        width = 0;
        height = 0;
        requested = DecoderBackend::kAuto;
        last_status = DecodeStatus::kNotInitialized;
        note.clear();
        backend.reset();
    }

    bool initialized() const { return backend != nullptr; }
};

Decoder::Decoder() : impl_(new Impl()) {}

Decoder::~Decoder() {
    release();
}

// ===========================================================================
//  init: 选后端 (含回退)
// ===========================================================================
namespace {

/// 一个候选后端: 工厂 + 人类可读的名字 (拿不到时用它解释原因)
struct Candidate {
    std::unique_ptr<detail::VideoDecoderBackend> (*factory)();
    const char* label;
    const char* missing_reason;
};

/// 按请求的 backend 排好候选顺序。
/// kAuto 的顺序就是"先硬解, 再软解"; 指定单个后端时只有一个候选 (不回退)。
std::vector<Candidate> candidates_for(DecoderBackend requested) {
    const Candidate mpp = {&detail::make_mpp_backend, "MPP 硬解",
                           "本次构建没编入 MPP (AGENT_HAVE_MPP 未定义)"};
    const Candidate ff = {&detail::make_ffmpeg_sw_backend, "FFmpeg 软解",
                          "本次构建没编入 FFmpeg (AGENT_HAVE_FFMPEG 未定义)"};

    switch (requested) {
        case DecoderBackend::kMpp:
            return {mpp};
        case DecoderBackend::kFfmpegSw:
            return {ff};
        case DecoderBackend::kAuto:
        default:
            return {mpp, ff};
    }
}

void append_reason(std::string* acc, const std::string& text) {
    if (!acc->empty()) {
        *acc += "; ";
    }
    *acc += text;
}

}  // namespace

bool Decoder::init(int width, int height, VideoCodec codec, DecoderBackend backend) {
    release();

    if (width <= 0 || height <= 0) {
        impl_->last_status = DecodeStatus::kInvalidArgument;
        impl_->note = "宽高必须为正 (收到 " + std::to_string(width) + "x" +
                      std::to_string(height) + ")";
        return false;
    }
    if (codec != VideoCodec::kH264 && codec != VideoCodec::kH265) {
        impl_->last_status = DecodeStatus::kInvalidArgument;
        impl_->note = "不支持的编码格式";
        return false;
    }

    // YUV420 要求偶数尺寸
    impl_->width = width & ~1;
    impl_->height = height & ~1;
    impl_->requested = backend;

    if (impl_->width <= 0 || impl_->height <= 0) {
        impl_->last_status = DecodeStatus::kInvalidArgument;
        impl_->note = "宽高取偶后变成 0";
        return false;
    }

    std::string failures;
    for (const Candidate& candidate : candidates_for(backend)) {
        std::unique_ptr<detail::VideoDecoderBackend> made = candidate.factory();
        if (!made) {
            append_reason(&failures,
                          std::string(candidate.label) + ": " + candidate.missing_reason);
            continue;
        }

        std::string reason;
        if (made->init(impl_->width, impl_->height, codec, &reason)) {
            impl_->backend = std::move(made);
            impl_->last_status = DecodeStatus::kOk;
            // 硬解没起来但软解起来了: 把"为什么"留下, 否则这个信息就丢了
            impl_->note = failures.empty()
                              ? ""
                              : (failures + "; 已回退到 " + impl_->backend->name());
            return true;
        }
        append_reason(&failures, std::string(candidate.label) + ": " + reason);
    }

    impl_->last_status = DecodeStatus::kDecodeError;
    impl_->note = failures.empty() ? "没有可用的解码后端" : failures;
    return false;
}

// ===========================================================================
//  decode / flush
// ===========================================================================
bool Decoder::decode(const std::uint8_t* data,
                     std::size_t size,
                     std::uint8_t** yuv_out,
                     int* w,
                     int* h) {
    if (yuv_out != nullptr) {
        *yuv_out = nullptr;
    }
    if (w != nullptr) {
        *w = 0;
    }
    if (h != nullptr) {
        *h = 0;
    }

    // 参数校验优先于状态: 这样空指针/非法 size 永远报 kInvalidArgument,
    // 不会被"还没初始化"掩盖成 kNotInitialized。
    if (data == nullptr && size != 0) {
        impl_->last_status = DecodeStatus::kInvalidArgument;
        return false;
    }
    if (data != nullptr && size == 0) {
        impl_->last_status = DecodeStatus::kInvalidArgument;
        return false;
    }
    // 注意: data == null 且 size == 0 是合法的 —— 表示
    // "不发新包, 只把解码器里已有的帧取出来" (flush 用)。

    if (!impl_->initialized()) {
        impl_->last_status = DecodeStatus::kNotInitialized;
        return false;
    }

    return impl_->backend->decode(data, size, yuv_out, w, h);
}

bool Decoder::flush(std::uint8_t** yuv_out, int* w, int* h) {
    // 和 decode() 一样: 出参先清干净, 失败路径绝不留野指针
    if (yuv_out != nullptr) {
        *yuv_out = nullptr;
    }
    if (w != nullptr) {
        *w = 0;
    }
    if (h != nullptr) {
        *h = 0;
    }

    if (!impl_->initialized()) {
        impl_->last_status = DecodeStatus::kNotInitialized;
        return false;
    }
    return impl_->backend->flush(yuv_out, w, h);
}

void Decoder::release() {
    if (!impl_) {
        return;
    }
    if (impl_->backend) {
        impl_->backend->release();
    }
    impl_->reset();
}

// ===========================================================================
//  状态查询
// ===========================================================================
DecodeStatus Decoder::status() const {
    if (impl_->backend) {
        return impl_->backend->status();
    }
    return impl_->last_status;
}

bool Decoder::is_initialized() const {
    return impl_->initialized();
}

DecoderBackend Decoder::active_backend() const {
    // 没初始化时返回"请求的那个" —— 反正它没生效, 别假装是别的
    return impl_->backend ? impl_->backend->kind() : impl_->requested;
}

const char* Decoder::active_decoder_name() const {
    return impl_->backend ? impl_->backend->name() : nullptr;
}

bool Decoder::is_hardware() const {
    return impl_->backend && impl_->backend->hardware();
}

std::size_t Decoder::frames_decoded() const {
    return impl_->backend ? impl_->backend->frames() : 0;
}

const char* Decoder::last_error() const {
    if (impl_->backend) {
        const char* runtime = impl_->backend->last_error();
        if (runtime != nullptr && runtime[0] != '\0') {
            return runtime;
        }
    }
    return impl_->note.c_str();
}

// ===========================================================================
//  FFmpeg 软解后端
// ===========================================================================
#ifdef AGENT_HAVE_FFMPEG

namespace {

struct FmtInfo {
    bool supported = false;
    YuvLayout layout = YuvLayout::kPlanar;
    bool uv_swapped = false;
};

/// FFmpeg 像素格式 → 平面布局。只认 8bit YUV420; 其余**明确拒绝**, 不猜。
FmtInfo classify_format(int fmt) {
    switch (fmt) {
        case AV_PIX_FMT_NV12:
            return {true, YuvLayout::kSemiPlanar, false};
        case AV_PIX_FMT_NV21:
            return {true, YuvLayout::kSemiPlanar, true};
        case AV_PIX_FMT_YUV420P:
        case AV_PIX_FMT_YUVJ420P:  // 4.2 里仍在用; 与 YUV420P 布局相同
            return {true, YuvLayout::kPlanar, false};
        // YUV422P / YUV444P / 各种 10~16bit: 都不是 8bit YUV420
        default:
            return {false, YuvLayout::kPlanar, false};
    }
}

}  // namespace

class FfmpegSwBackend : public detail::VideoDecoderBackend {
public:
    ~FfmpegSwBackend() override { release(); }

    bool init(int width, int height, VideoCodec codec, std::string* error) override {
        release();

        const bool is_h265 = (codec == VideoCodec::kH265);
        const char* decoder_name = is_h265 ? "hevc" : "h264";

        const AVCodec* av_codec = avcodec_find_decoder_by_name(decoder_name);
        if (av_codec == nullptr) {
            *error = std::string("FFmpeg 里没有 ") + decoder_name + " 解码器";
            return false;
        }

        ctx_ = avcodec_alloc_context3(av_codec);
        if (ctx_ == nullptr) {
            *error = "avcodec_alloc_context3 失败 (内存不足?)";
            return false;
        }

        width_ = width;
        height_ = height;
        ctx_->width = width;
        ctx_->height = height;
        ctx_->pkt_timebase = AVRational{1, 1000000};
        ctx_->thread_count = 0;  // 0 = 让 FFmpeg 按核数自己决定 (软解才需要多线程)

        const int rc = avcodec_open2(ctx_, av_codec, nullptr);
        if (rc < 0) {
            // 这一句就是旧实现缺的东西: 到底为什么开不起来
            *error = std::string("avcodec_open2(") + decoder_name + ") 失败: " +
                     err_text(rc);
            avcodec_free_context(&ctx_);
            return false;
        }

        frame_ = av_frame_alloc();
        pkt_ = av_packet_alloc();
        if (frame_ == nullptr || pkt_ == nullptr) {
            *error = "av_frame_alloc / av_packet_alloc 失败";
            release();
            return false;
        }

        name_ = is_h265 ? "ffmpeg-sw(hevc)" : "ffmpeg-sw(h264)";
        status_ = DecodeStatus::kOk;
        return true;
    }

    bool decode(const std::uint8_t* data,
                std::size_t size,
                std::uint8_t** yuv_out,
                int* w,
                int* h) override {
        if (ctx_ == nullptr) {
            status_ = DecodeStatus::kNotInitialized;
            return false;
        }

        // 1) 送包 (零拷贝: 直接引用调用方缓冲, 不同步也不复制)
        if (data != nullptr && size > 0) {
            av_packet_unref(pkt_);
            pkt_->data = const_cast<std::uint8_t*>(data);
            pkt_->size = static_cast<int>(size);

            const int send_ret = avcodec_send_packet(ctx_, pkt_);
            // EAGAIN = 解码器还没吃下上一包, 这一包先不算错;
            // 继续去 receive 把已有的帧取出来, 下一轮再送。
            if (send_ret < 0 && send_ret != AVERROR(EAGAIN)) {
                error_ = std::string("avcodec_send_packet 失败: ") + err_text(send_ret);
                status_ = DecodeStatus::kDecodeError;
                return false;
            }
        } else {
            // flush: 送空包让解码器把流水线里剩下的帧吐出来
            avcodec_send_packet(ctx_, nullptr);
        }

        // 2) 收帧
        const int recv_ret = avcodec_receive_frame(ctx_, frame_);
        if (recv_ret == AVERROR(EAGAIN) || recv_ret == AVERROR_EOF) {
            status_ = DecodeStatus::kNeedMoreInput;
            return false;
        }
        if (recv_ret < 0) {
            error_ = std::string("avcodec_receive_frame 失败: ") + err_text(recv_ret);
            status_ = DecodeStatus::kDecodeError;
            return false;
        }

        const bool ok = convert_frame();
        av_frame_unref(frame_);  // 立刻还回去, 不跨帧持有
        return ok;
    }

    bool flush(std::uint8_t** yuv_out, int* w, int* h) override {
        if (ctx_ == nullptr) {
            status_ = DecodeStatus::kNotInitialized;
            return false;
        }
        return decode(nullptr, 0, yuv_out, w, h);
    }

    void release() override {
        if (pkt_ != nullptr) {
            av_packet_free(&pkt_);
        }
        if (frame_ != nullptr) {
            av_frame_free(&frame_);
        }
        if (ctx_ != nullptr) {
            avcodec_free_context(&ctx_);
        }
        i420_.clear();
        out_w_ = 0;
        out_h_ = 0;
        frames_ = 0;
        name_ = nullptr;
        error_.clear();
        status_ = DecodeStatus::kNotInitialized;
    }

    const char* name() const override { return name_ != nullptr ? name_ : "ffmpeg-sw"; }

    DecoderBackend kind() const override { return DecoderBackend::kFfmpegSw; }

    bool hardware() const override { return false; }

    DecodeStatus status() const override { return status_; }

    std::size_t frames() const override { return frames_; }

    const char* last_error() const override { return error_.c_str(); }

private:
    static std::string err_text(int rc) {
        char buf[128];
        buf[0] = '\0';
        av_strerror(rc, buf, sizeof(buf));
        return std::string(buf);
    }

    /// 把 frame_ 收拢成连续 I420 放进 i420_
    bool convert_frame() {
        const FmtInfo info = classify_format(frame_->format);
        if (!info.supported) {
            char buf[96];
            av_get_pix_fmt_string(buf, sizeof(buf),
                                  static_cast<AVPixelFormat>(frame_->format));
            error_ = std::string("解码输出像素格式不支持 (只要 8bit YUV420): ") + buf;
            status_ = DecodeStatus::kUnsupportedFormat;
            return false;
        }

        // 用解码器报的可见尺寸, 而不是 init 时猜的
        int vw = frame_->width;
        int vh = frame_->height;
        if (vw <= 0 || vh <= 0) {
            vw = width_;
            vh = height_;
        }
        vw &= ~1;
        vh &= ~1;
        if (vw <= 0 || vh <= 0) {
            error_ = "解码器报了非法尺寸";
            status_ = DecodeStatus::kDecodeError;
            return false;
        }

        const std::size_t need = i420_frame_bytes(vw, vh);
        if (i420_.size() < need) {
            i420_.resize(need);
        }

        collapse_yuv420_to_i420(frame_->data[0], frame_->data[1], frame_->data[2],
                                frame_->linesize[0], frame_->linesize[1], info.layout,
                                info.uv_swapped, vw, vh, i420_.data());

        out_w_ = vw;
        out_h_ = vh;
        ++frames_;
        status_ = DecodeStatus::kOk;
        return true;
    }

    int out_w_ = 0;
    int out_h_ = 0;
    int width_ = 0;
    int height_ = 0;
    std::size_t frames_ = 0;
    const char* name_ = nullptr;
    std::string error_;
    DecodeStatus status_ = DecodeStatus::kNotInitialized;

    /// 收拢后的 I420; 对外借出的指针指向它 (下一次 decode 会被覆写)
    std::vector<std::uint8_t> i420_;

    AVCodecContext* ctx_ = nullptr;
    AVFrame* frame_ = nullptr;
    AVPacket* pkt_ = nullptr;
};

#endif  // AGENT_HAVE_FFMPEG

namespace detail {

std::unique_ptr<VideoDecoderBackend> make_ffmpeg_sw_backend() {
#ifdef AGENT_HAVE_FFMPEG
    return std::unique_ptr<VideoDecoderBackend>(new FfmpegSwBackend());
#else
    return nullptr;
#endif
}

}  // namespace detail

}  // namespace agent
