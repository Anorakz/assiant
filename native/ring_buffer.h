// ============================================================================
//  ring_buffer.h — 单生产者 / 单消费者无锁环形缓冲
//
//  设计目标 (见 docs/architecure.md §3.1)
//  ---------------------------------------------------------------------------
//  · Image RingBuffer      : 视频帧, 容量 300, 消费者只关心"最新一帧"
//  · Host Input RingBuffer : 主机键盘事件, 容量 128, 消费者要 read_all()
//
//  消费者有两个入口:
//    pop()          —— FIFO 顺序取最老的一条 (read_all 用)
//    read_latest()  —— 丢弃过期数据, 直接取最新一条 (图像用)
//
//  ⚠ 本实现是 **覆盖式 (overwrite)** 的: 生产永不阻塞、永不失败。
//    写满之后新数据直接盖掉最老的未读数据, push() 通过返回值告诉你
//    "这次覆盖了 N 条没读过的数据", 由调用方决定要不要记日志。
//    这对视频流是正确取舍 —— 宁可丢帧, 也不能让解码线程被消费者拖死。
//    这也正是 size() 会"缩水"的原因: 未读数据被生产者吃掉了。
//
//  并发约定 (非常重要)
//  ---------------------------------------------------------------------------
//  严格 SPSC:
//    · push()  只能由 **一个** 线程调用 (生产者)
//    · pop() / read_latest() / read_latest_after() / drop()
//              只能由 **另一个** 线程调用 (消费者)
//  两个消费者线程并发读是未定义行为。
//
//  槽位序号机制
//  ---------------------------------------------------------------------------
//  每个槽位保存绝对序号 seq, 生产者写数据"前后"各发布一次:
//
//      old = slot.seq.load(relaxed)            // 读旧值, 仅供断言
//      slot.seq.store(seq + capacity, release) // 占位: "我在写这个槽"
//      slot.value = value                      // 写数据
//      slot.seq.store(seq + 1, release)        // 发布: 数据就绪
//      write_seq_.store(seq + 1, release)      // 最后才推进全局写序号
//
//  消费者只认 seq == index + 1 的槽位, 读完后 **必须复查** seq 是否仍是
//  读之前的值; 变了就说明读到一半被生产者覆盖, 丢弃重来。这样即使容量满、
//  生产者直接覆盖未读数据, 也不会读到"撕裂"的半个对象。
//
//  为什么这是安全的 (C++ 内存模型)
//  ---------------------------------------------------------------------------
//  消费者对 slot.seq 的 acquire 读与生产者那次 release 写构成
//  synchronizes-with, 于是 slot.value 的写 happens-before 对应的读 ——
//  载荷访问本身不是数据竞争, 复查只是额外的保险。
//
//  注意: ThreadSanitizer **会** 把 slot.value 的读写报成 data race, 因为
//  它只认"同一个原子变量上的 acquire/release 配对", 看不懂"先读 seq 拿到
//  值、再据此判定 value 安全"这种间接推理。这是锁自由结构的经典误报,
//  抑制规则见 tests/tsan.supp (只抑制载荷, 控制面不抑制)。
//
//  满/空判定天然成立, 不需要像传统实现那样牺牲一个槽位:
//      seq == index               → 空 (还没写过)
//      seq == index + 1           → 满 (写好了, 消费者还没取)
//      seq == index + capacity    → 生产者正在写这个槽
//      seq >  index + capacity    → 已被覆盖, 该条数据已过期
// ============================================================================

#pragma once

#include <atomic>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <memory>
#include <optional>
#include <thread>
#include <utility>
#include <vector>

namespace agent {

/// 写入结果, 供调用方判断是否发生了数据丢失
struct PushResult {
    bool overwrote_unread = false;  ///< 本次写入是否覆盖了尚未读取的数据
    bool ok = true;                 ///< 写入是否成功 (当前实现恒为 true)
};

/// @tparam T        载荷类型
/// @tparam Capacity 编译期容量:
///                     Capacity > 0  → 定长, 容量就是 Capacity, 构造参数被忽略
///                     Capacity == 0 → 运行时容量, 由构造函数参数决定
///
/// 定长支路用堆分配 (不是 std::array 成员): 300 帧 256×256×3 接近 56MB,
/// 放成员里会把栈撑爆, 而且会让 RingBuffer 变得不可移动。
template <typename T, std::size_t Capacity = 0, auto TimestampMember = nullptr>
class RingBuffer {
public:
    /// @param dynamic_capacity 运行时容量, 必须 >= 1 (仅 Capacity == 0 时生效)
    explicit RingBuffer(std::size_t dynamic_capacity = 1);

    RingBuffer(const RingBuffer&) = delete;
    RingBuffer& operator=(const RingBuffer&) = delete;

    // ------------------------------------------------------------ 容量查询 ---
    /// 缓冲区总槽位数 (构造后不变)
    std::size_t capacity() const noexcept;

    /// 当前可读条数, 取值 [0, capacity]
    /// 注意: 生产者覆盖未读数据时该值会下降, 不是单调的
    std::size_t size() const noexcept;

    /// 是否无数据可读
    bool empty() const noexcept;

    /// 是否已满 (size() == capacity())
    bool full() const noexcept;

    /// 因被覆盖而丢失的未读条数累计值 (诊断用)
    ///
    /// @note 生产者的 overrun 判定依赖消费者发布读进度 (见 consumed())。
    ///       若消费者还没读过任何东西, 生产者只能保守地认为"一条都没读过",
    ///       从缓冲区第一次写满开始就会记 overrun —— 这属于预期噪声。
    ///       图像环形缓冲只取最新帧、丢帧是常态, 用这个数当丢帧统计即可;
    ///       真正要精确统计事件丢失时, 在开始消费前先调一次 consumed()。
    /// @note 内部用 max(tail_, latest_seq_) 当消费者进度, 因此 pop() 和
    ///       read_latest() 混用时, 进度会偏乐观 (可能少报)。这两个入口
    ///       按架构是给不同消费者用的, 不要在同一实例上混用。
    std::size_t overruns() const noexcept;

    /// 消费者发布"我已经消费到哪":
    ///   返回 max(tail_, latest_seq_), 也就是所有已读条目的绝对序号上界。
    /// @param publish true  = 顺带把该值告诉生产者 (推荐, 用于告诉生产者
    ///                       "起点是空的" 或 "我已经空转到这了")
    ///                false = 只取诊断值, 不修改生产者可见的读进度
    ///
    /// 生产者把"未开始消费"和"读到位置 0"当成同一件事, 所以缓冲区写满
    /// 之前不会有 overrun, 不需要在构造后特意调这个函数。它的用途是:
    ///   · 消费者空转/主动丢弃一批数据后, 同步读进度, 避免误报
    ///   · 诊断: 查看消费者当前进度
    std::size_t consumed(bool publish = true) noexcept;

    /// 告诉生产者"绝对序号 < seq 的条目我都已经处理掉了" (单调, 传小值不会倒退)
    ///
    /// 给 **自带游标** 的封装层用: 例如某个封装类用 peek() 取数据、完全不碰
    /// RingBuffer 的 pop/read 游标, 于是生产者看不到
    /// 消费进度, 会把已经读走的数据继续算成 overrun。那一批取完之后调一次
    /// 这个函数, 丢数据统计才是准的。
    ///
    /// @note 只应由消费者线程调用
    void consumed_up_to(std::size_t seq) noexcept;

    // ------------------------------------------------------------ 生产者侧 ---
    /// 写入一条数据. 永不阻塞、永不失败; 满时覆盖最老的未读数据.
    PushResult push(const T& value);

    /// 同 push, 允许移动
    PushResult push(T&& value);

    // ------------------------------------------------------------ 消费者侧 ---
    /// FIFO 取最老的一条; 空则返回 nullopt
    std::optional<T> pop();

    /// 丢弃过期数据, 返回最新一条 (非阻塞); 无数据则 nullopt
    std::optional<T> read_latest();

    /// 同上, 但写进调用方提供的对象并返回是否成功 —— 大载荷 (如 196KB 的帧)
    /// 用这个可以避免"先拷进 optional 再拷出来"的额外一次拷贝.
    bool read_latest(T& out);

    /// 取最新一条, 若无数据则最多等待 timeout (轮询实现, 非条件变量)
    /// @param timeout 为 0 时等价于 read_latest()
    std::optional<T> read_latest(std::chrono::milliseconds timeout);

    // -------------------------------------------------- 按时间戳随机访问 ---
    //  给"按时间戳找帧"这类查询用的 **非破坏性** 接口。
    //  它们不动 tail_ / latest_seq_ / read_pos_ 任何游标, 所以同一个查询
    //  可以重复调用并得到一致结果。
    //
    //  注意: 时间戳过滤本身不在这里做。RingBuffer 是泛型的, 不该假设 T 有
    //  什么时间字段; "最接近且不超过某时间戳" 这类语义放在具体封装里
    //  (例如 ImageRingBuffer::read_by_timestamp)。

    /// 可访问范围: 绝对序号区间 [oldest_seq(), newest_seq() + 1)
    ///
    /// @note 缓冲区为空时 newest_seq() == 0 且 oldest_seq() == 0, 这个区间
    ///       [0, 1) 是**假的** —— 序号 0 还没有被写过。用之前先用 empty()
    ///       或 wrote_anything() 判断。
    std::size_t oldest_seq() const noexcept;
    std::size_t newest_seq() const noexcept;

    /// 生产者是否写过至少一条 (empty() 的等价判断, 语义更直白)
    bool wrote_anything() const noexcept;

    /// 把绝对序号 seq 的数据拷进 out (语义等价于 pop 后回退游标)
    /// @return false 表示该序号已经不在缓冲区里 (太旧或还没写)
    bool peek(std::size_t seq, T& out) const;

    /// 只取绝对序号 seq 的时间戳字段, 不拷贝整条数据
    /// @param ts [out] 时间戳
    /// @return false 表示该序号已经不在缓冲区里
    /// @note 字段用模板参数给出 (如 &Frame::timestamp_ns), 所以 RingBuffer
    ///       不需要知道 T 里叫什么名字; 只有调用这个函数时才要求该字段存在。
    /// @note 为减少拷贝, 建议把时间戳放在 T 的 **最后一个** 字段。
    bool timestamp_of(std::size_t seq, std::int64_t& ts) const;

    /// 丢弃所有未读数据 (消费者侧调用), 之后 size() == 0
    void drop() noexcept;

private:
    /// 一个槽位: 绝对序号 + 数据
    struct Slot {
        std::atomic<std::size_t> seq{0};
        T value{};
    };

    /// 定位绝对序号 pos 对应的槽位
    Slot& slot_of(std::size_t pos) noexcept;

    /// 消费者: 把"已读到哪"发布给生产者.
    /// 生产者靠它判断一次写入会不会盖掉没读过的数据.
    /// pos 只单调递增, 所以这是良性竞争 (最坏多算一条 overrun).
    void publish_read_pos(std::size_t pos) noexcept;

    /// 生产者: 在真正覆盖槽位 seq 之前做覆盖统计
    /// @return 本次覆盖掉的未读条数 (0 表示没覆盖)
    std::size_t note_write_slot(std::size_t seq, Slot& slot) noexcept;

    /// pop 的核心实现
    /// @param allow_skip 允许跳过已被生产者覆盖的条目
    bool pop_impl(T& out, bool allow_skip);

    /// 消费者: 抢占式的"最新一条"读取核心
    /// @param min_seq 只接受绝对序号 >= min_seq 的数据
    bool read_latest_impl(T& out, std::size_t min_seq);

    // Slot 含 std::atomic, 既不可拷贝也不可移动, 所以槽位数组一旦建好就不能
    // 再增长/重分配 —— 这也是不用 std::vector 做定长支路的原因之一。
    // 定长支路 (Capacity > 0) 的容量在编译期就定了, capacity_ 只是个常量镜像。
    std::size_t capacity_;
    std::unique_ptr<Slot[]> slots_;

    /// 生产者: 已写入的总条数. 严格单调, 写入完成后以 release 发布
    std::atomic<std::size_t> write_seq_{0};

    /// 消费者私有: pop() 的下一条绝对序号 (不跨线程共享)
    std::size_t tail_{0};

    /// 消费者私有: read_latest() 已取到的绝对序号 (不跨线程共享)
    std::size_t latest_seq_{0};

    /// 消费者发布给生产者的读进度 (生产者只读); 0 = 尚未开始消费
    std::atomic<std::size_t> read_pos_{0};

    /// 生产者私有: 上一次统计 overrun 时所用的"消费者进度"基准.
    /// 每次覆盖只按增量累加, 否则同一个停滞的消费者会被反复计入.
    std::size_t last_counted_{0};

    /// 生产者私有: 累计因覆盖丢失的未读条数 (只由生产者写)
    std::atomic<std::size_t> overruns_{0};
};

// ============================================================================
//  实现
// ============================================================================

template <typename T, std::size_t Capacity, auto TimestampMember>
RingBuffer<T, Capacity, TimestampMember>::RingBuffer(std::size_t dynamic_capacity)
    : capacity_(Capacity == 0 ? (dynamic_capacity == 0 ? 1 : dynamic_capacity)
                              : Capacity),
      slots_(new Slot[capacity_]) {
    // 初始 seq 必须是自己的下标 —— 后面所有满/空判定都建立在这个基准上
    for (std::size_t i = 0; i < capacity_; ++i) {
        slots_[i].seq.store(i, std::memory_order_relaxed);
    }
}

template <typename T, std::size_t Capacity, auto TimestampMember>
std::size_t RingBuffer<T, Capacity, TimestampMember>::capacity() const noexcept {
    return capacity_;
}

template <typename T, std::size_t Capacity, auto TimestampMember>
typename RingBuffer<T, Capacity, TimestampMember>::Slot& RingBuffer<T, Capacity, TimestampMember>::slot_of(std::size_t pos) noexcept {
    return slots_[pos % capacity_];
}

template <typename T, std::size_t Capacity, auto TimestampMember>
void RingBuffer<T, Capacity, TimestampMember>::publish_read_pos(std::size_t pos) noexcept {
    std::size_t pub = read_pos_.load(std::memory_order_relaxed);
    if (pub < pos) {
        read_pos_.store(pos, std::memory_order_relaxed);
    }
}

template <typename T, std::size_t Capacity, auto TimestampMember>
std::size_t RingBuffer<T, Capacity, TimestampMember>::note_write_slot(std::size_t seq, Slot& slot) noexcept {
    (void)slot;
    // 写 seq 之前, 缓冲区里最早还活着的条目是 seq - capacity_ + 1.
    // 也就是说: 写完之后, tail 一定被顶到 clamped_tail.
    const std::size_t clamped_tail = seq + 1 > capacity_ ? seq + 1 - capacity_ : 0;

    // 消费者已发布的读进度 (可能滞后), 以及它读不到超过已写入的位置
    std::size_t consumed = read_pos_.load(std::memory_order_relaxed);
    const std::size_t written = write_seq_.load(std::memory_order_relaxed);
    if (consumed > written) {
        consumed = written;
    }

    // 消费者还没读到的部分是 [consumed, clamped_tail); 其中相对上次统计
    // 新增的那一段才是本次真正丢掉的. 用增量避免重复累加.
    const std::size_t unread = clamped_tail > consumed ? clamped_tail - consumed : 0;
    const std::size_t delta = unread > last_counted_ ? unread - last_counted_ : 0;

    if (clamped_tail > last_counted_) {
        last_counted_ = clamped_tail;
    }

    if (delta == 0) {
        return 0;
    }
    overruns_.fetch_add(delta, std::memory_order_relaxed);
    return delta;
}

template <typename T, std::size_t Capacity, auto TimestampMember>
std::size_t RingBuffer<T, Capacity, TimestampMember>::size() const noexcept {
    const std::size_t written = write_seq_.load(std::memory_order_acquire);
    if (written <= tail_) {
        return 0;  // 还没写, 或者读端已经追平
    }
    const std::size_t unread = written - tail_;
    // 生产者超速时未读数据被覆盖, 实际可读只剩 capacity_ 条
    return unread > capacity_ ? capacity_ : unread;
}

template <typename T, std::size_t Capacity, auto TimestampMember>
bool RingBuffer<T, Capacity, TimestampMember>::empty() const noexcept {
    return size() == 0;
}

template <typename T, std::size_t Capacity, auto TimestampMember>
bool RingBuffer<T, Capacity, TimestampMember>::full() const noexcept {
    return size() == capacity_;
}

template <typename T, std::size_t Capacity, auto TimestampMember>
std::size_t RingBuffer<T, Capacity, TimestampMember>::overruns() const noexcept {
    return overruns_.load(std::memory_order_relaxed);
}

template <typename T, std::size_t Capacity, auto TimestampMember>
std::size_t RingBuffer<T, Capacity, TimestampMember>::consumed(bool publish) noexcept {
    const std::size_t fence = tail_ > latest_seq_ ? tail_ : latest_seq_;
    if (publish) {
        // 只写这个原子量; 生产者的私有水位由它自己维护.
        publish_read_pos(fence);
    }
    return fence;
}

template <typename T, std::size_t Capacity, auto TimestampMember>
PushResult RingBuffer<T, Capacity, TimestampMember>::push(const T& value) {
    const std::size_t seq = write_seq_.load(std::memory_order_relaxed);
    Slot& slot = slot_of(seq);
    const std::size_t overwritten = note_write_slot(seq, slot);
    (void)overwritten;

    // 占位: 消费者看到 seq + capacity 就知道这格正在被写
    slot.seq.store(seq + capacity_, std::memory_order_release);
    slot.value = value;
    // 发布: 数据就绪
    slot.seq.store(seq + 1, std::memory_order_release);

    // 最后才推进全局写序号, 消费者靠它判断"有多少条可读"
    write_seq_.store(seq + 1, std::memory_order_release);

    return PushResult{overwritten > 0, true};
}

template <typename T, std::size_t Capacity, auto TimestampMember>
PushResult RingBuffer<T, Capacity, TimestampMember>::push(T&& value) {
    const std::size_t seq = write_seq_.load(std::memory_order_relaxed);
    Slot& slot = slot_of(seq);
    const std::size_t overwritten = note_write_slot(seq, slot);

    slot.seq.store(seq + capacity_, std::memory_order_release);
    slot.value = std::move(value);
    slot.seq.store(seq + 1, std::memory_order_release);

    write_seq_.store(seq + 1, std::memory_order_release);

    return PushResult{overwritten > 0, true};
}

template <typename T, std::size_t Capacity, auto TimestampMember>
std::optional<T> RingBuffer<T, Capacity, TimestampMember>::pop() {
    T out{};
    if (!pop_impl(out, /*allow_skip=*/true)) {
        return std::nullopt;
    }
    return std::optional<T>(std::move(out));
}

template <typename T, std::size_t Capacity, auto TimestampMember>
bool RingBuffer<T, Capacity, TimestampMember>::pop_impl(T& out, bool allow_skip) {
    // 生产者可能连续抢写同一个槽位 (容量 1 时必然如此), 所以自旋必须有上界,
    // 否则消费者会被活活饿死在这里.
    constexpr int kMaxSpins = 64;

    for (int spin = 0;; ++spin) {
        const std::size_t written = write_seq_.load(std::memory_order_acquire);
        if (tail_ >= written) {
            return false;  // 没有新数据
        }

        Slot& slot = slot_of(tail_);
        const std::size_t seq = slot.seq.load(std::memory_order_acquire);

        if (seq == tail_ + 1) {
            // 数据就绪, 拷出来
            out = slot.value;
            // 复查: 拷贝期间被生产者覆盖了就只能重来
            if (slot.seq.load(std::memory_order_acquire) != seq) {
                if (spin < kMaxSpins) {
                    std::this_thread::yield();
                    continue;
                }
                // 一直被覆盖, 这一条已经拿不到了
                return false;
            }
            ++tail_;
            publish_read_pos(tail_);
            return true;
        }

        if (seq == tail_ + capacity_ || seq > tail_ + capacity_) {
            // 这一格要么正被生产者写, 要么旧数据已被覆盖 -> 跳到最老的存活条目
            if (!allow_skip) {
                return false;
            }
            if (spin >= kMaxSpins) {
                return false;  // 抢不过生产者, 让调用方稍后再试
            }
            // 最老存活条目的绝对序号是 written - capacity_.
            // 只有当它确实还在 tail_ 前面时才跳, 否则跳过去会让 tail_ 超过
            // 已写入的位置, 表现得像"凭空前进".
            if (written > capacity_) {
                const std::size_t next = written - capacity_;
                if (next > tail_) {
                    tail_ = next;
                    publish_read_pos(tail_);
                }
            }
            std::this_thread::yield();
            continue;
        }

        // 剩下的情况是 seq < tail_ + 1, 逻辑上不该出现
        return false;
    }
}

template <typename T, std::size_t Capacity, auto TimestampMember>
bool RingBuffer<T, Capacity, TimestampMember>::read_latest_impl(T& out, std::size_t min_seq) {
    // 生产者可能一直在抢写最新那格, 自旋必须有上界, 否则会活锁.
    constexpr int kMaxSpins = 64;

    for (int spin = 0; spin <= kMaxSpins; ++spin) {
        const std::size_t written = write_seq_.load(std::memory_order_acquire);
        if (written <= min_seq) {
            return false;  // 没有比 min_seq 更新的数据
        }
        const std::size_t idx = written - 1;
        Slot& slot = slot_of(idx);
        const std::size_t seq = slot.seq.load(std::memory_order_acquire);

        if (seq != idx + 1) {
            // 生产者正抢写这一格 (seq == idx + capacity_), 或者它已经被
            // 更后面的写入取代. 两种情况都退回去重取最新序号.
            // 注意 seq 不可能大于 idx + capacity_.
            if (spin == kMaxSpins) {
                return false;  // 抢不到, 让调用方稍后再试, 不要死等
            }
            std::this_thread::yield();
            continue;
        }

        out = slot.value;
        // 读完复查, 变了说明拷到一半被覆盖
        if (slot.seq.load(std::memory_order_acquire) != seq) {
            if (spin == kMaxSpins) {
                return false;
            }
            std::this_thread::yield();
            continue;
        }
        latest_seq_ = written;
        publish_read_pos(written);
        return true;
    }
    return false;
}

template <typename T, std::size_t Capacity, auto TimestampMember>
std::optional<T> RingBuffer<T, Capacity, TimestampMember>::read_latest() {
    T out{};
    if (!read_latest_impl(out, latest_seq_)) {
        return std::nullopt;
    }
    return std::optional<T>(std::move(out));
}

template <typename T, std::size_t Capacity, auto TimestampMember>
bool RingBuffer<T, Capacity, TimestampMember>::read_latest(T& out) {
    return read_latest_impl(out, latest_seq_);
}

template <typename T, std::size_t Capacity, auto TimestampMember>
std::optional<T> RingBuffer<T, Capacity, TimestampMember>::read_latest(std::chrono::milliseconds timeout) {
    if (timeout.count() <= 0) {
        return read_latest();
    }

    const auto deadline = std::chrono::steady_clock::now() + timeout;
    for (;;) {
        T out{};
        if (read_latest_impl(out, latest_seq_)) {
            return std::optional<T>(std::move(out));
        }
        if (std::chrono::steady_clock::now() >= deadline) {
            return std::nullopt;
        }
        std::this_thread::sleep_for(std::chrono::microseconds(200));
    }
}

template <typename T, std::size_t Capacity, auto TimestampMember>
void RingBuffer<T, Capacity, TimestampMember>::consumed_up_to(std::size_t seq) noexcept {
    publish_read_pos(seq);
}

template <typename T, std::size_t Capacity, auto TimestampMember>
bool RingBuffer<T, Capacity, TimestampMember>::wrote_anything() const noexcept {
    return write_seq_.load(std::memory_order_acquire) != 0;
}

template <typename T, std::size_t Capacity, auto TimestampMember>
std::size_t RingBuffer<T, Capacity, TimestampMember>::oldest_seq() const noexcept {
    // 写满之后最老的存活条目就被顶到 write_seq_ - capacity_
    const std::size_t written = write_seq_.load(std::memory_order_acquire);
    return written <= capacity_ ? 0 : written - capacity_;
}

template <typename T, std::size_t Capacity, auto TimestampMember>
std::size_t RingBuffer<T, Capacity, TimestampMember>::newest_seq() const noexcept {
    const std::size_t written = write_seq_.load(std::memory_order_acquire);
    return written == 0 ? 0 : written - 1;
}

template <typename T, std::size_t Capacity, auto TimestampMember>
bool RingBuffer<T, Capacity, TimestampMember>::peek(std::size_t seq, T& out) const {
    const std::size_t written = write_seq_.load(std::memory_order_acquire);

    // 越界: 还没写 (seq >= written), 或太旧已被覆盖
    if (seq >= written) {
        return false;
    }
    if (written > capacity_ && seq < written - capacity_) {
        return false;
    }

    const Slot& slot = slots_[seq % capacity_];
    const std::size_t s = slot.seq.load(std::memory_order_acquire);
    if (s != seq + 1) {
        return false;  // 正被生产者改写, 或已被更晚的写入取代
    }

    out = slot.value;
    // 复查: 拷贝期间被覆盖就放弃这次查询
    return slot.seq.load(std::memory_order_acquire) == s;
}

template <typename T, std::size_t Capacity, auto TimestampMember>
bool RingBuffer<T, Capacity, TimestampMember>::timestamp_of(std::size_t seq, std::int64_t& ts) const {
    const std::size_t written = write_seq_.load(std::memory_order_acquire);

    if (seq >= written) {
        return false;
    }
    if (written > capacity_ && seq < written - capacity_) {
        return false;
    }

    const Slot& slot = slots_[seq % capacity_];
    const std::size_t s = slot.seq.load(std::memory_order_acquire);
    if (s != seq + 1) {
        return false;
    }

    // 只搬时间戳一个字段 (不拷整条数据), 所以强烈建议把它放在 T 的最后一个
    // 字段 —— memcpy 的跨度是 offsetof(T, ts) + sizeof(ts), 越靠前搬得越少。
    static_assert(TimestampMember != nullptr,
                  "timestamp_of() 需要指定时间戳成员, 例如 "
                  "RingBuffer<Frame, 300, &Frame::timestamp_ns>");
    std::int64_t value = 0;
    std::memcpy(&value, &(slot.value.*TimestampMember), sizeof(value));

    if (slot.seq.load(std::memory_order_acquire) != s) {
        return false;  // 复查: 读的过程中被覆盖了
    }
    ts = value;
    return true;
}

template <typename T, std::size_t Capacity, auto TimestampMember>
void RingBuffer<T, Capacity, TimestampMember>::drop() noexcept {
    // 消费端独占 tail_/latest_seq_, 直接追平写端即可
    const std::size_t written = write_seq_.load(std::memory_order_acquire);
    tail_ = written;
    latest_seq_ = written;
    publish_read_pos(written);
}

}  // namespace agent
