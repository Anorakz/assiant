// ============================================================================
//  decoder_backend.h — 解码后端的**内部**接口 (不属于对外 API)
//
//  Decoder (decoder.h 里的公开类) 只负责三件事:
//      · 参数校验
//      · 按 DecoderBackend 选后端 / 决定要不要回退
//      · 把 last_error / status / frames 这些状态转发出去
//  真正的解码在 VideoDecoderBackend 的实现里, 一个后端一个文件:
//
//      decoder_mpp.cpp    MPP 硬解        (只有 AGENT_HAVE_MPP 时才真的实现)
//      decoder.cpp        FFmpeg 软解     (只有 AGENT_HAVE_FFMPEG 时才真的实现)
//
//  拆开的好处: decoder_mpp.cpp 不用认识 FFmpeg, decoder.cpp 也不用认识 MPP;
//  宿主机(两个宏都没开)也照样能编, 只是两个工厂都返回 nullptr ——
//  "没有可用后端"这件事由 Decoder::init() 统一报出来, 而不是散在各处 #ifdef。
// ============================================================================

#pragma once

#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>

#include "decoder.h"

namespace agent {
namespace detail {

class VideoDecoderBackend {
public:
    virtual ~VideoDecoderBackend() = default;

    /// 初始化解码器。
    /// @param error [out] 失败原因 (可读); 成功时不要动它
    /// @return false = 这个后端不可用; 调用方可以据此换下一个后端
    virtual bool init(int width, int height, VideoCodec codec, std::string* error) = 0;

    /// 送一帧, 取一帧。语义与 Decoder::decode 完全一致
    /// (data==nullptr && size==0 表示"只取帧", 用于 flush)。
    virtual bool decode(const std::uint8_t* data,
                        std::size_t size,
                        std::uint8_t** yuv_out,
                        int* w,
                        int* h) = 0;

    /// 冲刷流水线里的剩余帧; 语义与 Decoder::flush 完全一致
    virtual bool flush(std::uint8_t** yuv_out, int* w, int* h) = 0;

    /// 释放资源; 可重复调用
    virtual void release() = 0;

    /// 后端名 ("rkmpp" / "ffmpeg-sw")
    virtual const char* name() const = 0;

    /// 这个后端是哪一种 (给 Decoder::active_backend() 用)
    virtual DecoderBackend kind() const = 0;

    /// 这个后端是不是硬件解码
    virtual bool hardware() const = 0;

    /// 最近一次解码操作的状态
    virtual DecodeStatus status() const = 0;

    /// 已成功输出的帧数
    virtual std::size_t frames() const = 0;

    /// 运行期最近一次失败原因; 没出错时返回 ""
    virtual const char* last_error() const = 0;
};

/// MPP 硬解后端。编不过 / 平台没有 MPP 时返回 nullptr。
std::unique_ptr<VideoDecoderBackend> make_mpp_backend();

/// FFmpeg 软解后端。编不过 / 没有 FFmpeg 时返回 nullptr。
std::unique_ptr<VideoDecoderBackend> make_ffmpeg_sw_backend();

}  // namespace detail
}  // namespace agent
