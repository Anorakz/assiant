// ============================================================================
//  decoder.h — 视频硬解封装 (H.264 / H.265)
//
//  架构位置 (docs/architecure.md §3.1)
//  ---------------------------------------------------------------------------
//      Sunshine 视频流 → moonlight-common-c 收 RTP → **Decoder (硬解)**
//        → YUV(I420) → preprocess_frame() → ImageRingBuffer → pybind11
//
//  解码后端选型 (板端实测, 见 docs/decoder-mpp.md)
//  ---------------------------------------------------------------------------
//  **MPP (Rockchip Media Process Platform)** 是这块板子上唯一可用的硬解通路:
//
//      · /dev/mpp_service + /proc/mpp_service/rkvdec0  ← 内核 MPPM 驱动在
//      · librockchip_mpp 1.3.8, 头文件 /usr/include/rockchip/
//
//  原来的方案是 **FFmpeg 4.2.7 + V4L2 M2M** (`h264_v4l2m2m` / `hevc_v4l2m2m`),
//  在 RK3568 上**不可能工作**: 板端 `v4l2-ctl --list-devices` 只有 rkisp 摄像头,
//  没有任何 V4L2 M2M 解码设备 (`/dev/video-dec0` 是个写着 "dec" 的普通文件,
//  不是设备节点)。所以那两个解码器虽然编在 libavcodec 里, 一 open 就失败。
//  该分支已删除, 不再保留。
//
//  现在只有两个后端:
//      kMpp        MPP 硬解 (板端默认)
//      kFfmpegSw   FFmpeg 软解 —— 只在 MPP 起不来时兜底, 以及在宿主机上
//                  做"解码器契约"测试时用; 720p60 软解在 A55 上跑不动实时,
//                  它的价值是"不崩 + 报清楚原因", 不是保证流畅
//
//  ⚠ 后端选择看的是**能不能真的开起来**, 不是"解码器名字存不存在"。
//    这正是旧实现的 bug: 它用 avcodec_find_decoder_by_name() 判断硬解可用,
//    而 hevc_v4l2m2m 是编进库里的(存在), 于是 hardware=true, 然后 open 失败
//    → 软解回退**永远不会发生** → 直接 "decoder init failed"。
//
//  像素格式
//  ---------------------------------------------------------------------------
//  解码器出来的可能是 NV12 / NV21 / I420 (取决于后端和它的输出设置),
//  而下游 preprocess_frame() 要的是 **I420**: Y/U/V 三个平面依次紧跟,
//  行 stride **等于 width**。
//
//  收拢 stride 这件事**由本封装负责**: 解码器给的行宽通常是 16/32/64 对齐的
//  (1280 是, 1272 就不是 —— 会拿到 hor_stride=1280), 不收拢会整幅图斜掉。
//  collapse_yuv420_to_i420() 是干这个的纯函数, 单独暴露出来方便单测。
//
//  YUV 缓冲的生命周期 (务必先读这段)
//  ---------------------------------------------------------------------------
//  decode() 通过 yuv_out 交出的指针是 **借用** 的:
//
//      · 所有权属于解码器
//      · 只在 **下一次 decode() 或 release() 之前** 有效
//      · 调用方必须在这一窗口内消费完, 不得保存该指针
//
//  实现上: 每解出一帧就**立刻**把它收拢进解码器自己的一块连续 I420 缓冲,
//  然后马上把解码器的帧/缓冲还回去 (MPP 的帧池、FFmpeg 的 AVFrame 都一样小)。
//  所以"下一次 decode 就失效"这个约束成立, 不要跨帧持有; 要留就自己拷一份。
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

/// 解码后端
enum class DecoderBackend {
    /// 先试 MPP 硬解, 失败再退 FFmpeg 软解 (板端默认; 会在 last_error() 里
    /// 记下"为什么没用上硬解")
    kAuto = 0,
    /// 只用 MPP 硬解; 起不来就 init() 失败, **不回退**
    kMpp = 1,
    /// 只用 FFmpeg 软解 (宿主机测试 / 排障对比)
    kFfmpegSw = 2,
};

/// 解码结果状态 (比 bool 返回值信息更全)
enum class DecodeStatus {
    kOk = 0,
    kNotInitialized,   ///< 没调 init() 或 init() 失败
    kNeedMoreInput,    ///< 数据不足, 这一帧还没吐出来 (硬解流水线延迟, 正常现象)
    kInvalidArgument,  ///< 空指针 / size 为 0 / 尺寸非法
    kUnsupportedFormat,///< 像素格式不是 8bit YUV420 (如 10bit HDR) —— 明确拒绝, 不猜
    kDecodeError,      ///< 真正的解码错误
};

class Decoder {
public:
    Decoder();
    ~Decoder();

    // 持有解码器上下文, 不允许拷贝
    Decoder(const Decoder&) = delete;
    Decoder& operator=(const Decoder&) = delete;

    /// 初始化解码器
    /// @param width  期望的视频宽 (像素)
    /// @param height 期望的视频高 (像素)
    /// @param codec  kH264 / kH265
    /// @param backend 用哪个后端; 默认 kAuto (MPP 优先, 失败退软解)
    /// @return false 表示初始化失败 (原因见 last_error())
    ///
    /// @note 宽高会被向下取整到偶数 (YUV420 的要求)。
    /// @note kAuto 下如果硬解没起来但软解起来了, 仍然返回 true ——
    ///       用 is_hardware() 判断走的是不是硬解。
    bool init(int width, int height, VideoCodec codec,
              DecoderBackend backend = DecoderBackend::kAuto);

    /// 送入一个压缩帧, 取出解好的 YUV (I420)
    ///
    /// @param data    码流指针 (Annex-B)
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
    ///       另外硬件解码器帧池有限, 一次包可能吐多帧, 多余的会缓起来由下一次
    ///       decode() 直接返回 (不丢帧)。
    bool decode(const std::uint8_t* data,
                std::size_t size,
                std::uint8_t** yuv_out,
                int* w,
                int* h);

    /// 冲刷解码器, 取出流水线里剩余帧
    ///
    /// @param yuv_out [out] 收到借用指针 (规则同 decode()); 可为 null
    /// @param w       [out] 可见宽度; 可为 null
    /// @param h       [out] 可见高度; 可为 null
    /// @return true 表示取到一帧 (此时 yuv_out 有效)
    ///
    /// @note 送一个空包 (EOS) 触发, 然后取一帧。
    /// @note **取到的帧同样只在"下一次 decode()/flush()/release() 之前"有效** ——
    ///       这一点很容易踩: flush() 之前从 decode() 拿到的指针, 在 flush() 之后就
    ///       已经失效了 (缓冲被下一帧覆盖), 必须用本次 flush() 给出的新指针。
    ///       板端冒烟测试第一版就是拿旧指针去读, 直接段错误。
    /// @note 反复调用是安全的: EOS 只会送一次, 后续调用只是继续把帧取出来。
    bool flush(std::uint8_t** yuv_out = nullptr, int* w = nullptr, int* h = nullptr);

    /// 释放所有底层资源; 可重复调用, 之后可以再次 init()
    void release();

    /// 最近一次操作的状态
    DecodeStatus status() const;

    /// 是否已完成初始化
    bool is_initialized() const;

    /// 实际生效的后端
    DecoderBackend active_backend() const;

    /// 实际生效的解码器名 ("rkmpp" / "ffmpeg-sw"); 未初始化时 nullptr
    const char* active_decoder_name() const;

    /// 是否在用硬件解码 (相对于软解回退)
    bool is_hardware() const;

    /// 已成功输出的帧数
    std::size_t frames_decoded() const;

    /// 初始化/运行中最近一次失败原因的**可读描述**; 没出错时是 ""
    ///
    /// @note 存在的理由: 旧实现只对外给一句 "decoder init failed", 板端排查
    ///       时完全看不出是"没有设备"还是"格式不支持"还是"内存不够"。
    const char* last_error() const;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

// ---------------------------------------------------------------------------
//  YUV420 → I420 收拢 (暴露出来是为了能单独测; 两个后端共用)
// ---------------------------------------------------------------------------
/// 源数据的平面布局
enum class YuvLayout {
    /// Y / U / V 三个独立平面 (I420, YV12)
    kPlanar = 0,
    /// Y 平面 + 交织的 UV 平面 (NV12: U 在前; NV21: V 在前)
    kSemiPlanar = 1,
};

/// 把任意 stride 的 YUV420 收拢成连续 I420 (stride == width)。
///
/// @param y           Y 平面起始
/// @param u           kPlanar: 第一个色度平面; kSemiPlanar: 交织的 UV 平面
/// @param v           kPlanar: 第二个色度平面; kSemiPlanar: **忽略 (可为 null)**
/// @param y_stride    Y 平面行宽 (字节), 可能 > width
/// @param uv_stride   色度平面行宽 (字节), 可能 > width
/// @param layout      平面布局, 见 YuvLayout
/// @param uv_swapped  true = 源里第一个色度是 V 而不是 U (NV21 / YV12)
/// @param width       可见宽 (偶数)
/// @param height      可见高 (偶数)
/// @param i420_out    输出缓冲, 至少 width*height*3/2 字节;
///                    布局为 Y (h 行) + U (h/2 行) + V (h/2 行), stride 均为 width
///
/// @note 参数非法 (空指针 / 宽高非正或奇数) 时**什么都不做**, 不写越界。
void collapse_yuv420_to_i420(const std::uint8_t* y,
                             const std::uint8_t* u,
                             const std::uint8_t* v,
                             int y_stride,
                             int uv_stride,
                             YuvLayout layout,
                             bool uv_swapped,
                             int width,
                             int height,
                             std::uint8_t* i420_out);

/// NV12 → I420 (collapse_yuv420_to_i420 的薄封装, 保留原签名以免动老调用方)
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
