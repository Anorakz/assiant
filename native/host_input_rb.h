// ============================================================================
//  host_input_rb.h — 主机输入事件环形缓冲 (Sunshine 回传键盘/鼠标 → Python 读)
//
//  架构位置 (docs/architecure.md §3.1)
//  ---------------------------------------------------------------------------
//      Sunshine 回传主机键盘 → moonlight-common-c 接收
//        → **HostInputRingBuffer** → pybind11: read_latest() / read_all()
//        → Chat Input Bus
//
//  设计要点
//  ---------------------------------------------------------------------------
//  · 定长 128 事件
//  · 覆盖式: 满时丢弃最旧的事件, push 永不阻塞 —— 和图像一样, 宁可丢旧事件
//    也不能让 moonlight 接收线程被 Python 拖住。丢了多少看 overruns()。
//  · 只维护一个"已消费"游标, read_all() 与 read_latest() 都推进它:
//      read_latest() —— 取最新一条, 并丢弃比它更旧的未读事件
//      read_all()    —— 取走当前全部未读事件 (FIFO), 相当于清空缓冲
//    size() 报的就是"还有多少条没读过"。
//    若给 read_latest 单独留一个不消费的游标, 就不存在统一的消费进度,
//    size() 和丢事件统计都无法定义 —— 生产者需要知道到底读到哪了。
//
//  为什么底层用 peek() 而不是 pop()
//  ---------------------------------------------------------------------------
//  peek() 按绝对序号读取, 不依赖 RingBuffer 内部的 pop 游标。覆盖发生时
//  本类可以精确跳过已经错失的序号区间, 并把消费进度显式上报给生产者
//  (consumed_up_to), 让 overruns() 保持准确。
//
//  并发约定: 生产者单线程调用 push(); 消费者单线程调用 read_latest/read_all。
// ============================================================================

#pragma once

#include <cstddef>
#include <cstdint>
#include <vector>

#include "ring_buffer.h"

namespace agent {

/// 一条主机输入事件 (键盘或鼠标)
struct InputEvent {
    enum Type { KEY, MOUSE } type;

    /// 修饰键位图 (如 META/CTRL/ALT/SHIFT); KEY 事件使用
    std::uint32_t modifier;
    /// 键码; KEY 事件使用
    std::uint32_t key;
    /// 鼠标坐标; MOUSE 事件使用
    std::int32_t x;
    std::int32_t y;

    enum Action { PRESS, RELEASE } action;

    /// 事件发生时刻, 纳秒。放在最后: timestamp_of() 只按前缀读取该字段。
    std::int64_t timestamp_ns;
};

/// 固定容量: 约 2 秒 @60Hz 的事件深度
inline constexpr std::size_t kHostInputCapacity = 128;

class HostInputRingBuffer {
public:
    HostInputRingBuffer();

    HostInputRingBuffer(const HostInputRingBuffer&) = delete;
    HostInputRingBuffer& operator=(const HostInputRingBuffer&) = delete;

    // ------------------------------------------------------------ 生产者侧 ---
    /// 写入一条事件; 满时丢弃最旧的一条 (永不阻塞)
    void push(const InputEvent& e);

    // ------------------------------------------------------------ 消费者侧 ---
    /// 取最新一条事件, 并丢弃比它更旧的未读事件
    /// @return false 表示还没有任何未读事件
    /// @note 会推进消费游标: 调用之后 size() 变 0, read_all() 不再返回旧事件
    bool read_latest(InputEvent& out);

    /// 取走当前全部未读事件 (FIFO 顺序) 并清空缓冲
    ///
    /// 返回的条数可能少于调用前的 size(), 因为期间被生产者覆盖掉的事件
    /// 已经不在缓冲区里了 (详见 overruns())。
    std::vector<InputEvent> read_all();

    /// 还有多少条事件没被读过, 取值 [0, capacity()]
    std::size_t size() const;

    /// 固定为 kHostInputCapacity
    std::size_t capacity() const;

    /// 因覆盖而丢掉的事件数累计值 (诊断用)
    std::size_t overruns() const;

private:
    /// 还有多少条事件没被读过 (内部与 size() 同义)
    /// @note 这是本类"已消费"的唯一判据; 不要用 rb_.empty() 代替,
    ///       它看的是 RingBuffer 自己的 pop 游标, 两者定义不同。
    std::size_t unread() const;

    /// 第三个模板参数把时间戳字段告诉 RingBuffer, 这样 timestamp_of()
    /// 只读这一个字段, 不必搬整条事件。
    RingBuffer<InputEvent, kHostInputCapacity, &InputEvent::timestamp_ns> rb_;

    /// 已消费游标: 下一条待读取事件的绝对序号。
    /// read_all() 与 read_latest() 共用并推进它。
    std::size_t read_seq_{0};
};

}  // namespace agent
