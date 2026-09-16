// ============================================================================
//  decoder.h — FFmpeg 硬解封装 (H.264 / H.265)
//
//  架构位置 (docs/architecure.md §3.1)
//  ---------------------------------------------------------------------------
//      Sunshine 视频流 → moonlight-common-c 收 RTP → **Decoder (硬解)**
//        → YUV(I420) → preprocess_frame() → ImageRingBuffer → pybind11
//
//  解码后端选型 (已核实)
//  ---------------------------------------------------------------------------
//  sysroot 里的 FFmpeg 是板端 Ubuntu 20.04 自带的 **4.2.7**:
//      libavcodec.so.58.54.100 / libavutil.so.56.31.100
//  这个构建 **没有 rkmpp 支持** (libavcodec 里搜不到任何 rkmpp 符号),
//  但它带了 V4L2 M2M 硬解:
//
//      h264_v4l2m2m     ← RK3568 上走内核 rkvdec/VEPU, 即硬件解码
//      hevc_v4l2m2m
//
//  所以本封装走 **FFmpeg + V4L2 M2M**。硬解不可用时自动回退软解
//  (h264 / hevc), 用 is_hardware() 可以区分。
//
//  像素格式
//  ---------------------------------------------------------------------------
//  V4L2 M2M 出来的是 **NV12** (Y 平面 + 交织的 UV 平面), 而下游
//  preprocess_frame() 要的是 **I420** (Y/U/V 三个平面依次紧跟, 行 stride
//  等于 width)。本封装负责 NV12 → I420, 并顺手把 stride 收拢掉。
//
//  也就是说 preprocess_frame() 那条 "stride 必须等于 width" 的前提, 由这里
//  来保证 —— 解码器给的行宽通常是 16/32 对齐的, 不收拢会整幅图斜掉。
//
//  YUV 缓冲的生命周期 (务必先读这段)
//  ---------------------------------------------------------------------------
//  decode() 通过 yuv_out 交出的指针是 **借用** 的:
//
//      · 所有权属于解码器
//      · 只在 **下一次 decode() 或 release() 之前** 有效
//      · 调用方必须在这一窗口内消费完, 不得保存该指针
//
//  实现上是把转换后的 I420 存进解码器自己的一块连续缓冲; 这块缓冲在下一次
//  decode() 时被覆写。所以"指针对应的是解码器缓冲", 但"下一次 decode 就失效"
//  这个约束仍然成立, 不要跨帧持有。
//
//  需要持有更久就自己拷一份。
// ============================================================================

#pragma once

#include <cstddef>
#include <cstdint>
#include <memory>

namespace agent {

/// 支持的压缩格式
enum class VideoCodec {
    kH264 = 0,
    kH265 = 1,
};

/// 解码结果状态 (比 bool 返回值信息更全)
enum class DecodeStatus {
    kOk = 0,
    kNotInitialized,   ///< 没调 init() 或 init() 失败
    kNeedMoreInput,    ///< 数据不足, 这一帧还没吐出来 (正常现象)
    kInvalidArgument,  ///< 空指针 / size 为 0 / 尺寸非法
    kUnsupportedFormat,///< 解出来的像素格式不是 YUV420 planar/semi-planar
    kDecodeError,      ///< 真正的解码错误
};

class Decoder {
public:
    Decoder();
    ~Decoder();

    // 持有 FFmpeg 上下文, 不允许拷贝
    Decoder(const Decoder&) = delete;
    Decoder& operator=(const Decoder&) = delete;

    /// 初始化解码器
    /// @param width  期望的视频宽 (像素)
    /// @param height 期望的视频高 (像素)
    /// @param codec  kH264 / kH265
    /// @return false 表示初始化失败 (见 status())
    ///
    /// @note 优先选 V4L2 M2M 硬解; 找不到就回退软解, 此时仍返回 true,
    ///       用 is_hardware() 判断走的是不是硬解。
    bool init(int width, int height, VideoCodec codec);

    /// 送入一个压缩帧, 取出解好的 YUV (I420)
    ///
    /// @param data    码流指针 (Annex-B 或 AVCC)
    /// @param size    码流字节数
    /// @param yuv_out [out] 收到借用指针 (见文件头"生命周期"); 失败时为 null
    /// @param w       [out] 可见宽度
    /// @param h       [out] 可见高度
    /// @return false 表示这一帧没有产出
    ///
    /// @note 硬解有流水线延迟: 前几帧送进去通常拿不到输出, 要等后续帧。
    ///       所以 **不能** 把 false 当致命错误。看 status():
    ///         kNeedMoreInput  → 正常, 继续喂
    ///         kDecodeError    → 真错误
    ///       另外硬件解码器帧池有限, 同一轮里第 2 帧会缓存起来, 由下一次
    ///       decode() 直接吐出 (不丢帧)。
    bool decode(const std::uint8_t* data,
                std::size_t size,
                std::uint8_t** yuv_out,
                int* w,
                int* h);

    /// 冲刷解码器, 取出流水线里剩余帧
    /// @return 有帧可取时 true, 之后用 decode() (传上次的包或空) 取
    /// @note 简单实现: 送一个空包触发 flush, 后续帧通过 last_frame() 取
    bool flush();

    /// 释放所有底层资源; 可重复调用, 之后可以再次 init()
    void release();

    /// 最近一次操作的状态
    DecodeStatus status() const;

    /// 是否已完成初始化
    bool is_initialized() const;

    /// 实际生效的解码器名 (如 "h264_v4l2m2m" / "h264"); 未初始化时 nullptr
    const char* active_decoder_name() const;

    /// 是否在用硬件解码 (相对于软解回退)
    bool is_hardware() const;

    /// 已成功输出的帧数
    std::size_t frames_decoded() const;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

// ---------------------------------------------------------------------------
//  NV12 → I420 (暴露出来是为了能单独测; 也可给别的模块复用)
// ---------------------------------------------------------------------------

/// @param nv12_y       Y 平面起始
/// @param nv12_uv      交织的 UV 平面起始 (U 在前, V 在后)
/// @param y_stride     Y 平面行宽 (字节), 可能 > width
/// @param uv_stride    UV 平面行宽 (字节), 可能 > width
/// @param width        可见宽 (偶数)
/// @param height       可见高 (偶数)
/// @param i420_out     输出缓冲, 至少 width*height*3/2 字节;
///                     布局为 Y (h 行) + U (h/2 行) + V (h/2 行), stride 均为 width
void convert_nv12_to_i420(const std::uint8_t* nv12_y,
                          const std::uint8_t* nv12_uv,
                          int y_stride,
                          int uv_stride,
                          int width,
                          int height,
                          std::uint8_t* i420_out);

/// 一帧 I420 需要的字节数
inline std::size_t i420_frame_bytes(int width, int height) {
    return static_cast<std::size_t>(width) * height * 3 / 2;
}

}  // namespace agent
