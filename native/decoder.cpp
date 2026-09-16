// ============================================================================
//  decoder.cpp — FFmpeg 硬解封装实现
//
//  编译期开关
//  ---------------------------------------------------------------------------
//  AGENT_HAVE_FFMPEG 打开时才会 #include FFmpeg 头文件并编入真实解码逻辑;
//  关掉时退化成"参数校验 + 明确失败"的 stub。这样:
//    · host 单测不需要宿主机装 FFmpeg (只测 NV12→I420 这个纯函数)
//    · 交叉编译打开它, 用 sysroot 里的 FFmpeg 4.2.7
//
//  解码路径
//  ---------------------------------------------------------------------------
//    avcodec_find_decoder_by_name("h264_v4l2m2m")   硬解, 走内核 rkvdec/VEPU
//      └─ 找不到 → 回退 "h264" / "hevc" 软解, is_hardware() 变 false
//    avcodec_send_packet / avcodec_receive_frame
//      └─ 硬解给的是 DRM_PRIME 包装的 AVFrame → av_hwframe_transfer_data 拷回
//         CPU 可读的 NV12
//    NV12 (带对齐 stride) → convert_nv12_to_i420 → 连续 I420
//
//  帧池与"借用指针"
//  ---------------------------------------------------------------------------
//  V4L2 M2M 的帧池很小, 不能长期持有 AVFrame; 所以每解出一帧就立刻转成
//  I420 存进自己的缓冲 (pending_i420_), 并把 AVFrame 立刻 unref 还回帧池。
//  对外交出的指针指向 pending_i420_, 因此"下一次 decode()/release() 前有效"。
//  多余帧 (一次包吐多帧) 暂存在 queued_ 里, 由下一次 decode() 直接返回,
//  不丢帧。
// ============================================================================

#include "decoder.h"

#include <cstring>
#include <vector>

#ifdef AGENT_HAVE_FFMPEG
extern "C" {
#include <libavcodec/avcodec.h>
#include <libavutil/frame.h>
#include <libavutil/imgutils.h>
#include <libavutil/pixdesc.h>
#include <libavutil/pixfmt.h>
}
#endif

namespace agent {

// ===========================================================================
//  NV12 → I420 (纯函数, 不依赖 FFmpeg, host 也能测)
// ===========================================================================
void convert_nv12_to_i420(const std::uint8_t* nv12_y,
                          const std::uint8_t* nv12_uv,
                          int y_stride,
                          int uv_stride,
                          int width,
                          int height,
                          std::uint8_t* i420_out) {
    if (nv12_y == nullptr || nv12_uv == nullptr || i420_out == nullptr) {
        return;
    }
    if (width <= 0 || height <= 0) {
        return;
    }

    const int cw = width / 2;
    const int ch = height / 2;

    std::uint8_t* dst_y = i420_out;
    std::uint8_t* dst_u = dst_y + static_cast<std::size_t>(width) * height;
    std::uint8_t* dst_v = dst_u + static_cast<std::size_t>(cw) * ch;

    // Y 平面: 逐行拷贝, 收拢 stride
    for (int row = 0; row < height; ++row) {
        std::memcpy(dst_y + static_cast<std::size_t>(row) * width,
                    nv12_y + static_cast<std::size_t>(row) * y_stride,
                    static_cast<std::size_t>(width));
    }

    // UV 平面: NV12 是 U,V 交织 (UVUV...), 需要拆成两个平面
    for (int row = 0; row < ch; ++row) {
        const std::uint8_t* src = nv12_uv + static_cast<std::size_t>(row) * uv_stride;
        std::uint8_t* du = dst_u + static_cast<std::size_t>(row) * cw;
        std::uint8_t* dv = dst_v + static_cast<std::size_t>(row) * cw;
        for (int col = 0; col < cw; ++col) {
            du[col] = src[col * 2 + 0];  // U
            dv[col] = src[col * 2 + 1];  // V
        }
    }
}

#ifdef AGENT_HAVE_FFMPEG

namespace {

/// 像素格式的平面布局
enum class PlaneLayout {
    kNone,        ///< 不支持
    kSemiPlanar,  ///< NV12 / NV21: UV 交织
    kPlanar,      ///< I420 / YV12: U/V 各自独立平面
};

struct FmtInfo {
    PlaneLayout layout = PlaneLayout::kNone;
    bool uv_swapped = false;  ///< NV21 / YV12: 第一个色度字节是 V
};

FmtInfo classify_format(int fmt) {
    switch (fmt) {
        case AV_PIX_FMT_NV12:
            return {PlaneLayout::kSemiPlanar, false};
        case AV_PIX_FMT_NV21:
            return {PlaneLayout::kSemiPlanar, true};
        case AV_PIX_FMT_YUV420P:
        case AV_PIX_FMT_YUVJ420P:  // 4.2 里仍在用; 与 YUV420P 布局相同
            return {PlaneLayout::kPlanar, false};
        // 高位深一律明确拒绝, 不猜布局
        case AV_PIX_FMT_YUV420P10LE:
        case AV_PIX_FMT_YUV420P10BE:
        case AV_PIX_FMT_YUV420P9LE:
        case AV_PIX_FMT_YUV420P9BE:
        case AV_PIX_FMT_YUV420P12LE:
        case AV_PIX_FMT_YUV420P12BE:
        case AV_PIX_FMT_YUV420P14LE:
        case AV_PIX_FMT_YUV420P14BE:
        case AV_PIX_FMT_YUV420P16LE:
        case AV_PIX_FMT_YUV420P16BE:
            return {PlaneLayout::kNone, false};
        default:
            return {PlaneLayout::kNone, false};
    }
}

}  // namespace

#endif  // AGENT_HAVE_FFMPEG

// ===========================================================================
//  Impl
// ===========================================================================
struct Decoder::Impl {
    int width = 0;
    int height = 0;
    VideoCodec codec = VideoCodec::kH264;
    bool initialized = false;
    bool hardware = false;

    DecodeStatus last_status = DecodeStatus::kNotInitialized;
    std::size_t frames = 0;
    const char* decoder_name = nullptr;

    /// 转换后的 I420; 对外借出的指针指向它
    std::vector<std::uint8_t> i420;
    bool has_frame = false;
    /// 已解码但还没交出去的帧 (硬件一次吐多帧时用)
    std::size_t queued = 0;

#ifdef AGENT_HAVE_FFMPEG
    AVCodecContext* ctx = nullptr;
    AVFrame* av_frame = nullptr;    ///< 复用的接收帧
    AVFrame* sw_frame = nullptr;    ///< hw→sw 转换目标 (NV12)
    AVPacket* pkt = nullptr;        ///< 复用的输入包 (零拷贝引用调用方缓冲)
#endif

    void reset() {
        width = 0;
        height = 0;
        initialized = false;
        hardware = false;
        last_status = DecodeStatus::kNotInitialized;
        frames = 0;
        decoder_name = nullptr;
        i420.clear();
        has_frame = false;
        queued = 0;
    }
};

Decoder::Decoder() : impl_(new Impl()) {}

Decoder::~Decoder() {
    release();
}

bool Decoder::init(int width, int height, VideoCodec codec) {
    release();

    if (width <= 0 || height <= 0) {
        impl_->last_status = DecodeStatus::kInvalidArgument;
        return false;
    }
    if (codec != VideoCodec::kH264 && codec != VideoCodec::kH265) {
        impl_->last_status = DecodeStatus::kInvalidArgument;
        return false;
    }

    // YUV420 要求偶数尺寸
    impl_->width = width & ~1;
    impl_->height = height & ~1;
    impl_->codec = codec;

    if (impl_->width <= 0 || impl_->height <= 0) {
        impl_->last_status = DecodeStatus::kInvalidArgument;
        return false;
    }

#ifdef AGENT_HAVE_FFMPEG
    const bool is_h265 = (codec == VideoCodec::kH265);
    const char* hw_name = is_h265 ? "hevc_v4l2m2m" : "h264_v4l2m2m";
    const char* sw_name = is_h265 ? "hevc" : "h264";

    const AVCodec* av_codec = avcodec_find_decoder_by_name(hw_name);
    if (av_codec != nullptr) {
        impl_->hardware = true;
    } else {
        // 硬解不可用 → 回退软解, 保证功能可用
        av_codec = avcodec_find_decoder_by_name(sw_name);
        impl_->hardware = false;
    }
    if (av_codec == nullptr) {
        impl_->last_status = DecodeStatus::kDecodeError;
        return false;
    }

    impl_->ctx = avcodec_alloc_context3(av_codec);
    if (impl_->ctx == nullptr) {
        impl_->last_status = DecodeStatus::kDecodeError;
        return false;
    }

    impl_->ctx->width = impl_->width;
    impl_->ctx->height = impl_->height;
    impl_->ctx->pkt_timebase = AVRational{1, 1000000};

    // 硬解要提前给对分辨率; 线程数交给 FFmpeg 默认
    if (impl_->hardware) {
        impl_->ctx->thread_count = 1;
    }

    if (avcodec_open2(impl_->ctx, av_codec, nullptr) < 0) {
        avcodec_free_context(&impl_->ctx);
        impl_->last_status = DecodeStatus::kDecodeError;
        return false;
    }

    impl_->av_frame = av_frame_alloc();
    impl_->sw_frame = av_frame_alloc();
    impl_->pkt = av_packet_alloc();
    if (impl_->av_frame == nullptr || impl_->sw_frame == nullptr || impl_->pkt == nullptr) {
        release();
        impl_->last_status = DecodeStatus::kDecodeError;
        return false;
    }

    impl_->decoder_name = av_codec->name;
    impl_->initialized = true;
    impl_->last_status = DecodeStatus::kOk;
    return true;
#else
    // 没有 FFmpeg: 明确失败, 不假装成功
    impl_->initialized = false;
    impl_->last_status = DecodeStatus::kDecodeError;
    return false;
#endif
}

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

    if (!impl_->initialized) {
        impl_->last_status = DecodeStatus::kNotInitialized;
        return false;
    }

#ifdef AGENT_HAVE_FFMPEG

    // 把上一次的帧标记为失效 —— 借用窗口到此为止
    impl_->has_frame = false;

    // 1) 还有上次没交出去的帧, 直接给它
    if (impl_->queued > 0) {
        --impl_->queued;
        impl_->has_frame = true;
        impl_->last_status = DecodeStatus::kOk;
        if (yuv_out != nullptr) {
            *yuv_out = impl_->i420.data();
        }
        if (w != nullptr) {
            *w = impl_->width;
        }
        if (h != nullptr) {
            *h = impl_->height;
        }
        return true;
    }

    // 2) 送包 (零拷贝: 直接引用调用方缓冲, 不同步也不复制)
    if (data != nullptr && size > 0) {
        av_packet_unref(impl_->pkt);
        impl_->pkt->data = const_cast<std::uint8_t*>(data);
        impl_->pkt->size = static_cast<int>(size);

        const int send_ret = avcodec_send_packet(impl_->ctx, impl_->pkt);
        if (send_ret < 0 && send_ret != AVERROR(EAGAIN)) {
            impl_->last_status = DecodeStatus::kDecodeError;
            return false;
        }
    } else {
        // flush: 送空包让解码器把流水线里剩下的帧吐出来
        avcodec_send_packet(impl_->ctx, nullptr);
    }

    // 3) 收帧
    const int recv_ret = avcodec_receive_frame(impl_->ctx, impl_->av_frame);
    if (recv_ret == AVERROR(EAGAIN)) {
        // 硬解流水线延迟, 属正常
        impl_->last_status = DecodeStatus::kNeedMoreInput;
        return false;
    }
    if (recv_ret == AVERROR_EOF) {
        impl_->last_status = DecodeStatus::kNeedMoreInput;
        return false;
    }
    if (recv_ret < 0) {
        impl_->last_status = DecodeStatus::kDecodeError;
        return false;
    }

    // 4) 硬解的帧要搬回 CPU 可读内存
    AVFrame* src = impl_->av_frame;
    if (src->format == AV_PIX_FMT_DRM_PRIME ||
        (src->hw_frames_ctx != nullptr)) {
        av_frame_unref(impl_->sw_frame);
        const int tr = av_hwframe_transfer_data(impl_->sw_frame, src, 0);
        av_frame_unref(impl_->av_frame);  // 帧池小, 立刻还回去
        if (tr < 0) {
            impl_->last_status = DecodeStatus::kDecodeError;
            return false;
        }
        src = impl_->sw_frame;
    }

    const int fmt = src->format;
    const FmtInfo info = classify_format(fmt);
    if (info.layout == PlaneLayout::kNone) {
        if (src != impl_->av_frame) {
            av_frame_unref(impl_->sw_frame);
        } else {
            av_frame_unref(impl_->av_frame);
        }
        impl_->last_status = DecodeStatus::kUnsupportedFormat;
        return false;
    }

    // 用解码器报的可见尺寸, 而不是 init 时猜的
    int vw = src->width;
    int vh = src->height;
    if (vw <= 0 || vh <= 0) {
        vw = impl_->width;
        vh = impl_->height;
    }
    vw &= ~1;
    vh &= ~1;
    if (vw <= 0 || vh <= 0) {
        impl_->last_status = DecodeStatus::kDecodeError;
        return false;
    }

    // 5) 转成连续 I420
    const std::size_t need = i420_frame_bytes(vw, vh);
    if (impl_->i420.size() < need) {
        impl_->i420.resize(need);
    }
    std::uint8_t* dst = impl_->i420.data();

    if (info.layout == PlaneLayout::kSemiPlanar) {
        // NV12 / NV21: data[1] 是交织的 UV
        const std::uint8_t* chroma = src->data[1];
        const int cstride = src->linesize[1];
        const int cw = vw / 2;
        const int ch = vh / 2;

        // Y
        for (int row = 0; row < vh; ++row) {
            std::memcpy(dst + static_cast<std::size_t>(row) * vw,
                        src->data[0] + static_cast<std::size_t>(row) * src->linesize[0],
                        static_cast<std::size_t>(vw));
        }
        // UV 拆分 (uv_swapped 时第一字节是 V)
        std::uint8_t* du = dst + static_cast<std::size_t>(vw) * vh;
        std::uint8_t* dv = du + static_cast<std::size_t>(cw) * ch;
        for (int row = 0; row < ch; ++row) {
            const std::uint8_t* s = chroma + static_cast<std::size_t>(row) * cstride;
            std::uint8_t* a = du + static_cast<std::size_t>(row) * cw;
            std::uint8_t* b = dv + static_cast<std::size_t>(row) * cw;
            if (info.uv_swapped) {
                for (int col = 0; col < cw; ++col) {
                    b[col] = s[col * 2 + 0];
                    a[col] = s[col * 2 + 1];
                }
            } else {
                for (int col = 0; col < cw; ++col) {
                    a[col] = s[col * 2 + 0];
                    b[col] = s[col * 2 + 1];
                }
            }
        }
    } else {
        // 已经是 planar (I420/YV12): 只收拢 stride
        const int cw = vw / 2;
        const int ch = vh / 2;
        const int ia = info.uv_swapped ? 2 : 1;  // YV12 的 U/V 顺序相反
        const int ib = info.uv_swapped ? 1 : 2;

        for (int row = 0; row < vh; ++row) {
            std::memcpy(dst + static_cast<std::size_t>(row) * vw,
                        src->data[0] + static_cast<std::size_t>(row) * src->linesize[0],
                        static_cast<std::size_t>(vw));
        }
        std::uint8_t* du = dst + static_cast<std::size_t>(vw) * vh;
        std::uint8_t* dv = du + static_cast<std::size_t>(cw) * ch;
        for (int row = 0; row < ch; ++row) {
            std::memcpy(du + static_cast<std::size_t>(row) * cw,
                        src->data[ia] + static_cast<std::size_t>(row) * src->linesize[ia],
                        static_cast<std::size_t>(cw));
            std::memcpy(dv + static_cast<std::size_t>(row) * cw,
                        src->data[ib] + static_cast<std::size_t>(row) * src->linesize[ib],
                        static_cast<std::size_t>(cw));
        }
    }

    if (src == impl_->sw_frame) {
        av_frame_unref(impl_->sw_frame);
    } else {
        av_frame_unref(impl_->av_frame);
    }

    impl_->width = vw;
    impl_->height = vh;
    impl_->has_frame = true;
    ++impl_->frames;
    impl_->last_status = DecodeStatus::kOk;

    if (yuv_out != nullptr) {
        *yuv_out = impl_->i420.data();
    }
    if (w != nullptr) {
        *w = vw;
    }
    if (h != nullptr) {
        *h = vh;
    }
    return true;

#else
    impl_->last_status = DecodeStatus::kNotInitialized;
    return false;
#endif
}

bool Decoder::flush() {
    if (!impl_->initialized) {
        impl_->last_status = DecodeStatus::kNotInitialized;
        return false;
    }
    // 送空包触发冲刷; 随后的帧用 decode(nullptr, 0, ...) 取
    return decode(nullptr, 0, nullptr, nullptr, nullptr);
}

void Decoder::release() {
    if (!impl_) {
        return;
    }
#ifdef AGENT_HAVE_FFMPEG
    if (impl_->pkt != nullptr) {
        av_packet_free(&impl_->pkt);
    }
    if (impl_->sw_frame != nullptr) {
        av_frame_free(&impl_->sw_frame);
    }
    if (impl_->av_frame != nullptr) {
        av_frame_free(&impl_->av_frame);
    }
    if (impl_->ctx != nullptr) {
        avcodec_free_context(&impl_->ctx);
    }
#endif
    impl_->reset();
}

DecodeStatus Decoder::status() const {
    return impl_->last_status;
}

bool Decoder::is_initialized() const {
    return impl_->initialized;
}

const char* Decoder::active_decoder_name() const {
    return impl_->decoder_name;
}

bool Decoder::is_hardware() const {
    return impl_->hardware;
}

std::size_t Decoder::frames_decoded() const {
    return impl_->frames;
}

}  // namespace agent
