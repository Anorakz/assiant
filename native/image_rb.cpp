// ============================================================================
//  image_rb.cpp — ImageRingBuffer 实现
//
//  这一层只做两件事:
//    1) 把 RingBuffer 的流式接口翻译成 ImageRingBuffer 的查询式接口
//    2) 按时间戳做"最接近且不超过"的查找
//
//  时间戳查找为什么从新往旧扫
//  ---------------------------------------------------------------------------
//  生产者保证 timestamp_ns 单调不减 (视频帧按采集顺序入队),
//  所以从 newest 往 oldest 走, 遇到的第一个 <= ts 的帧就是"最接近且不超过"的
//  那帧 —— 不需要二分, 命中通常也很快 (积压不严重时基本第一次就中)。
//  最坏情况走满 300 次, 每次只读时间戳, 不碰像素数据。
// ============================================================================

#include "image_rb.h"

namespace agent {

ImageRingBuffer::ImageRingBuffer() : rb_(kImageRingCapacity) {
    // 告诉生产者"消费者进度从 0 开始", 这样丢帧统计从一开始就是准的
    rb_.consumed();
}

void ImageRingBuffer::push(const Frame& f) {
    // 覆盖式: 永不阻塞、永不失败, 返回值这里用不上 (统计走 overruns())
    (void)rb_.push(f);
}

bool ImageRingBuffer::read_latest(Frame& out) {
    // 用引用版: 196KB/帧, 不走 optional 那条多一次拷贝的路径
    return rb_.read_latest(out);
}

bool ImageRingBuffer::read_by_timestamp(std::int64_t ts, Frame& out) {
    if (rb_.empty()) {
        return false;
    }

    const std::size_t newest = rb_.newest_seq();
    const std::size_t oldest = rb_.oldest_seq();

    // 从新到旧找第一个 timestamp_ns <= ts 的帧
    // 用有符号游标循环, 避免 size_t 下溢
    for (std::int64_t seq = static_cast<std::int64_t>(newest);
         seq >= static_cast<std::int64_t>(oldest); --seq) {
        std::int64_t frame_ts = 0;
        if (!rb_.timestamp_of(static_cast<std::size_t>(seq), frame_ts)) {
            // 这一帧刚好被生产者覆盖掉了, 跳过继续往前找
            continue;
        }
        if (frame_ts <= ts) {
            return rb_.peek(static_cast<std::size_t>(seq), out);
        }
    }

    // 所有帧都晚于 ts (或都被覆盖了)
    return false;
}

std::size_t ImageRingBuffer::size() const {
    return rb_.size();
}

std::size_t ImageRingBuffer::capacity() const {
    return rb_.capacity();
}

std::size_t ImageRingBuffer::overruns() const {
    return rb_.overruns();
}

}  // namespace agent
