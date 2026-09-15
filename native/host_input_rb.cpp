// ============================================================================
//  host_input_rb.cpp — HostInputRingBuffer 实现
//
//  读游标模型 (重要)
//  ---------------------------------------------------------------------------
//  本类只维护 **一个** 已消费游标 read_seq_: 下一条尚未读取事件的绝对序号。
//  read_all() 和 read_latest() 都从它出发, 也都推进它。
//
//  于是:
//    · size() 报的是"还有多少条没读过", 会随着两种读取下降
//    · read_latest() 语义 = 取走最新一条, 并丢弃比它更旧的未读事件
//      (对键盘/鼠标回传这类"要的就是当前状态"的场景, 这是想要的取舍)
//    · read_all() 之后再调, 只会返回期间新到的事件
//
//  为什么不给 read_latest 单独留一个非消费游标: 那样就没有统一的
//  "消费进度", size() 和丢事件统计 (overruns) 都无法定义 —— 生产者需要
//  知道到底读到哪了。
//
//  ⚠ 判断"有没有可读事件"必须用本类的 unread(), **不能** 用 rb_.empty():
//    RingBuffer 的 empty() 看的是它自己的 pop 游标, 而本类走 peek, 两者的
//    "已消费"定义不同, 混用会出现"read_all 明明取空了却还能读出东西"。
//
//  底层用 peek() 而不是 pop(): 这样不依赖 RingBuffer 内部的 pop 游标,
//  覆盖发生时也能按绝对序号精确跳过错失的区间, 并把进度显式上报给生产者。
// ============================================================================

#include "host_input_rb.h"

namespace agent {

HostInputRingBuffer::HostInputRingBuffer() : rb_(kHostInputCapacity) {
    // 告诉生产者"消费者进度从 0 开始", 这样丢事件统计从一开始就是准的
    rb_.consumed();
}

void HostInputRingBuffer::push(const InputEvent& e) {
    // 覆盖式: 永不阻塞、永不失败; 统计走 overruns()
    (void)rb_.push(e);
}

std::size_t HostInputRingBuffer::unread() const {
    // 空的时候 newest_seq() 会给出 0, 直接算会假报 1 条, 所以先判空
    if (!rb_.wrote_anything()) {
        return 0;
    }

    const std::size_t written = rb_.newest_seq() + 1;

    std::size_t from = read_seq_;
    const std::size_t oldest = rb_.oldest_seq();
    if (from < oldest) {
        from = oldest;  // 中间那些已被生产者覆盖, 不可能再读到
    }

    if (written <= from) {
        return 0;
    }
    const std::size_t n = written - from;
    return n > rb_.capacity() ? rb_.capacity() : n;
}

std::size_t HostInputRingBuffer::size() const {
    return unread();
}

bool HostInputRingBuffer::read_latest(InputEvent& out) {
    if (unread() == 0) {
        return false;
    }

    const std::size_t newest = rb_.newest_seq();
    if (!rb_.peek(newest, out)) {
        return false;  // 刚好被生产者覆盖, 这一轮拿不到
    }

    // 读取即消费: 连同比它更旧的未读事件一起丢弃, 只保留"最新"这个结果
    read_seq_ = newest + 1;
    rb_.consumed_up_to(read_seq_);
    return true;
}

std::vector<InputEvent> HostInputRingBuffer::read_all() {
    std::vector<InputEvent> out;

    if (unread() == 0) {
        return out;  // 没有可读事件时不动游标
    }

    const std::size_t newest = rb_.newest_seq();

    // 生产者超速时 read_seq_ 可能已经落在存活区间之前, 那些事件已被覆盖。
    // 直接跳到最老的存活条目 —— 丢了多少由 RingBuffer 的 overruns() 统计。
    std::size_t seq = read_seq_;
    const std::size_t oldest = rb_.oldest_seq();
    if (seq < oldest) {
        seq = oldest;
    }

    if (seq <= newest) {
        out.reserve(newest - seq + 1);
    }

    // 只扫一次, 且只在成功取到硬拷贝之后才推进游标.
    // (即使在扫的过程中生产者又覆盖了几条, peek 会返回 false 而跳过,
    //  游标最终落在 newest + 1, 也就是"把这批看完了")
    while (seq <= newest) {
        InputEvent e{};
        if (rb_.peek(seq, e)) {
            out.push_back(e);
        }
        ++seq;
        read_seq_ = seq;
    }

    // 本类走 peek, 不碰 RingBuffer 自己的 pop 游标, 所以必须显式把消费进度
    // 报给生产者; 否则已经读走的事件还会被继续算成 overrun, 统计虚高。
    rb_.consumed_up_to(read_seq_);

    return out;
}

std::size_t HostInputRingBuffer::capacity() const {
    return rb_.capacity();
}

std::size_t HostInputRingBuffer::overruns() const {
    return rb_.overruns();
}

}  // namespace agent
