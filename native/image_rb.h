// ============================================================================
//  image_rb.h — 视频帧环形缓冲 (摄像头/解码 → Python 读)
//
//  架构位置 (docs/architecture.md §3.1)
//  ---------------------------------------------------------------------------
//      Sunshine 视频流 → moonlight-common-c → 解码 → ROI 裁剪
//        → 256×256 RGB888 → **ImageRingBuffer** → pybind11: read_latest()
//
//  设计要点
//  ---------------------------------------------------------------------------
//  · 定长 300 帧: 固定容量, 不做运行时扩容
//  · 覆盖式: 写满后新帧直接盖掉最旧的, push 永不阻塞、永不失败。
//    视频流要的是"尽力跟上", 丢旧帧比拖死解码线程正确得多。
//  · 两条读取路径语义不同, 不要混用:
//      read_latest()        —— 流式, 只前进, 拿到就不再重复返回
//      read_by_timestamp()  —— 查询式, 非破坏性, 同一 ts 重复调用结果一致
//
//  内存
//  ---------------------------------------------------------------------------
//  一帧 256×256×3 = 196 608 字节, 300 帧 ≈ 56 MB。
//  这么大不能放栈上, 所以 RingBuffer 的槽位是堆分配;
//  ImageRingBuffer 对象本身也很小, 可以安全地按值持有。
//  (下一版若改用 RKNN 的 ROI 输入尺寸, 内存会随之下降。)
// ============================================================================

#pragma once

#include <cstddef>
#include <cstdint>

#include "ring_buffer.h"

namespace agent {

/// 一帧已经预处理好的 ROI: 256×256 RGB888 + 采集时间戳
struct Frame {
    std::uint8_t data[256 * 256 * 3];
    /// 采集时刻, 纳秒 (steady_clock 或 CLOCK_MONOTONIC 的纳秒读数)
    ///
    /// 放在结构体最后一个字段是有意的: timestamp_of() 只按前缀 memcpy
    /// 读这个字段, 靠后能让拷贝量最小 (而且不必动 196KB 的像素数据)。
    std::int64_t timestamp_ns;
};

/// 256×256×3
inline constexpr std::size_t kFrameBytes = 256 * 256 * 3;
/// 固定容量: 约 10 秒 @30fps 的缓冲深度
inline constexpr std::size_t kImageRingCapacity = 300;

class ImageRingBuffer {
public:
    ImageRingBuffer();

    ImageRingBuffer(const ImageRingBuffer&) = delete;
    ImageRingBuffer& operator=(const ImageRingBuffer&) = delete;

    // ------------------------------------------------------------ 生产者侧 ---
    /// 写入一帧; 满时覆盖最旧的一帧 (永不阻塞)
    void push(const Frame& f);

    // ------------------------------------------------------------ 消费者侧 ---
    /// 取最近一帧 (非阻塞)
    /// @return false 表示还没有任何帧
    bool read_latest(Frame& out);

    /// 取"时间戳最接近且不超过 ts"的一帧
    ///
    /// 即在所有 timestamp_ns <= ts 的帧里取时间戳最大的那帧。
    /// 非破坏性: 不推进任何读游标, 同一个 ts 重复调用结果一致。
    ///
    /// @return false 表示缓冲区为空, 或所有帧都晚于 ts
    bool read_by_timestamp(std::int64_t ts, Frame& out);

    /// 当前可读帧数, 取值 [0, capacity()]
    std::size_t size() const;

    /// 固定为 kImageRingCapacity
    std::size_t capacity() const;

    /// 因覆盖而丢掉的未读帧数累计值 (诊断用)
    std::size_t overruns() const;

private:
    /// 第三个模板参数把时间戳字段告诉 RingBuffer, 这样 timestamp_of()
    /// 只读这一个字段, 不用把 196KB 的像素数据也搬一遍。
    RingBuffer<Frame, kImageRingCapacity, &Frame::timestamp_ns> rb_;
};

// 编译期自检: timestamp_ns 必须真的在最后一个字段, 否则 timestamp_of() 的
// "按前缀只读时间戳" 优化前提就不成立, 且会让人觉得它读的是别的字段。
static_assert(offsetof(Frame, timestamp_ns) == kFrameBytes,
              "Frame::timestamp_ns 必须是最后一个字段");
static_assert(sizeof(Frame) == kFrameBytes + sizeof(std::int64_t),
              "Frame 不应有额外填充");

}  // namespace agent
