// ============================================================================
//  tests/test_ring_buffer.cpp — RingBuffer<T> 单元测试 (host 侧, L1)
//
//  运行方式:
//      scripts/test-host.ps1
//
//  覆盖范围
//  ---------------------------------------------------------------------------
//  阶段 1  读写基本语义 : push/pop/read_latest 顺序、空返回值
//  阶段 2  满空边界     : 容量边界、覆盖行为、size 缩水、环绕
//  阶段 3  并发         : SPSC 无撕裂、生产者超速、read_latest 单调
// ============================================================================

#include "ring_buffer.h"

#include <gtest/gtest.h>

#include <atomic>
#include <cstdint>
#include <thread>
#include <vector>

using agent::PushResult;
using agent::RingBuffer;

namespace {

/// 一个"自洽"的载荷: half 是 value 的固定派生量.
/// 用来抓"撕裂读" —— 如果生产者写一半被读到, half 就对不上.
struct Payload {
    std::uint32_t value = 0;
    std::uint32_t half = 0;

    bool consistent() const { return half == value / 2; }
};

Payload make_payload(std::uint32_t v) { return Payload{v, v / 2}; }

}  // namespace

// ===========================================================================
//  阶段 1 — 读写基本语义
// ===========================================================================

TEST(RingBufferBasic, CapacityIsReportedAsGiven) {
    RingBuffer<std::uint64_t> rb(8);
    EXPECT_EQ(rb.capacity(), 8u);
}

TEST(RingBufferBasic, FreshBufferIsEmpty) {
    RingBuffer<std::uint64_t> rb(4);

    EXPECT_TRUE(rb.empty());
    EXPECT_FALSE(rb.full());
    EXPECT_EQ(rb.size(), 0u);
    EXPECT_EQ(rb.pop(), std::nullopt);
    EXPECT_EQ(rb.read_latest(), std::nullopt);
}

TEST(RingBufferBasic, PushThenPopReturnsSameValue) {
    RingBuffer<std::uint64_t> rb(4);

    PushResult r = rb.push(42);

    EXPECT_TRUE(r.ok);
    EXPECT_FALSE(r.overwrote_unread);
    EXPECT_EQ(rb.size(), 1u);
    EXPECT_FALSE(rb.empty());

    auto got = rb.pop();
    ASSERT_TRUE(got.has_value());
    EXPECT_EQ(*got, 42u);
    EXPECT_EQ(rb.size(), 0u);
    EXPECT_TRUE(rb.empty());
}

TEST(RingBufferBasic, PopIsFifoOrdered) {
    RingBuffer<std::uint64_t> rb(8);
    for (std::uint64_t i = 1; i <= 5; ++i) {
        rb.push(i * 10);
    }

    for (std::uint64_t i = 1; i <= 5; ++i) {
        auto got = rb.pop();
        ASSERT_TRUE(got.has_value()) << "第 " << i << " 次 pop 不该为空";
        EXPECT_EQ(*got, i * 10);
    }

    EXPECT_TRUE(rb.empty());
    EXPECT_EQ(rb.pop(), std::nullopt);
}

TEST(RingBufferBasic, ReadLatestReturnsNewestAndSkipsOlder) {
    RingBuffer<std::uint64_t> rb(8);
    for (std::uint64_t i = 1; i <= 5; ++i) {
        rb.push(i);
    }

    auto got = rb.read_latest();
    ASSERT_TRUE(got.has_value());
    EXPECT_EQ(*got, 5u);

    // 再读一次没有新数据, 应当返回空而不是重复吐同一条
    EXPECT_EQ(rb.read_latest(), std::nullopt);
}

TEST(RingBufferBasic, ReadLatestDoesNotReturnStaleValueTwice) {
    RingBuffer<std::uint64_t> rb(8);
    rb.push(1);

    EXPECT_EQ(rb.read_latest(), 1u);
    EXPECT_EQ(rb.read_latest(), std::nullopt);

    rb.push(2);
    EXPECT_EQ(rb.read_latest(), 2u);
    EXPECT_EQ(rb.read_latest(), std::nullopt);
}

TEST(RingBufferBasic, PushByMoveIsAccepted) {
    RingBuffer<std::uint64_t> rb(4);

    std::uint64_t v = 7;
    PushResult r = rb.push(std::move(v));

    EXPECT_TRUE(r.ok);
    EXPECT_EQ(rb.pop(), 7u);
}

TEST(RingBufferBasic, DropDiscardsEverything) {
    RingBuffer<std::uint64_t> rb(4);
    rb.push(1);
    rb.push(2);
    ASSERT_EQ(rb.size(), 2u);

    rb.drop();

    EXPECT_EQ(rb.size(), 0u);
    EXPECT_TRUE(rb.empty());
    EXPECT_EQ(rb.pop(), std::nullopt);
    EXPECT_EQ(rb.read_latest(), std::nullopt);
}

TEST(RingBufferBasic, NonTrivialPayloadIsPreserved) {
    RingBuffer<Payload> rb(4);
    for (std::uint32_t i = 1; i <= 3; ++i) {
        rb.push(make_payload(i));
    }

    auto a = rb.pop();
    ASSERT_TRUE(a.has_value());
    EXPECT_EQ(a->value, 1u);
    EXPECT_TRUE(a->consistent());

    auto latest = rb.read_latest();
    ASSERT_TRUE(latest.has_value());
    EXPECT_EQ(latest->value, 3u);
    EXPECT_TRUE(latest->consistent());
}

// ===========================================================================
//  阶段 2 — 满 / 空 边界
// ===========================================================================

TEST(RingBufferBoundary, EmptyThenFullThenEmptyAgain) {
    RingBuffer<std::uint64_t> rb(4);

    EXPECT_TRUE(rb.empty());
    EXPECT_FALSE(rb.full());

    // 填满
    for (std::uint64_t i = 1; i <= 4; ++i) {
        rb.push(i);
        EXPECT_EQ(rb.size(), i);
    }
    EXPECT_TRUE(rb.full());
    EXPECT_FALSE(rb.empty());

    // 抽干
    for (std::uint64_t i = 1; i <= 4; ++i) {
        if (i == 1) {
            EXPECT_TRUE(rb.full()) << "还没 pop, 应当仍是满的";
        } else {
            EXPECT_FALSE(rb.full()) << "第 " << i << " 次 pop 前不该是满的";
        }
        auto got = rb.pop();
        ASSERT_TRUE(got.has_value());
        EXPECT_EQ(*got, i);
    }
    EXPECT_TRUE(rb.empty());
    EXPECT_FALSE(rb.full());
    EXPECT_EQ(rb.pop(), std::nullopt);
}

TEST(RingBufferBoundary, SizeNeverExceedsCapacity) {
    RingBuffer<std::uint64_t> rb(4);
    for (std::uint64_t i = 0; i < 50; ++i) {
        rb.push(i);
        EXPECT_LE(rb.size(), rb.capacity());
    }
    EXPECT_EQ(rb.size(), 4u);
}

TEST(RingBufferBoundary, FullCapacityIsUsable) {
    // 本实现不牺牲槽位: 容量 4 就能存 4 条, 不是 3 条
    RingBuffer<std::uint64_t> rb(4);
    for (std::uint64_t i = 1; i <= 4; ++i) {
        EXPECT_FALSE(rb.push(i).overwrote_unread) << "前 4 条都不该覆盖";
    }
    EXPECT_EQ(rb.size(), 4u);
    EXPECT_TRUE(rb.full());
    EXPECT_EQ(rb.overruns(), 0u);
}

TEST(RingBufferBoundary, PopOnEmptyIsAlwaysNullopt) {
    RingBuffer<std::uint64_t> rb(2);
    for (int i = 0; i < 5; ++i) {
        EXPECT_EQ(rb.pop(), std::nullopt);
    }
    EXPECT_EQ(rb.size(), 0u);
}

TEST(RingBufferBoundary, ReadLatestOnEmptyIsAlwaysNullopt) {
    RingBuffer<std::uint64_t> rb(2);
    for (int i = 0; i < 5; ++i) {
        EXPECT_EQ(rb.read_latest(), std::nullopt);
    }
}

TEST(RingBufferBoundary, CapacityOneBehavesCorrectly) {
    RingBuffer<std::uint64_t> rb(1);

    EXPECT_TRUE(rb.push(1).ok);
    EXPECT_TRUE(rb.full());
    EXPECT_EQ(rb.size(), 1u);
    EXPECT_EQ(rb.pop(), 1u);

    // 再写两条, 第 2 条被覆盖
    rb.push(2);
    rb.push(3);
    EXPECT_EQ(rb.size(), 1u);
    EXPECT_EQ(rb.pop(), 3u);
    EXPECT_EQ(rb.pop(), std::nullopt);
}

TEST(RingBufferBoundary, PushOverwritesOldestUnreadAndSaysSo) {
    RingBuffer<std::uint64_t> rb(3);
    for (std::uint64_t i = 1; i <= 3; ++i) {
        EXPECT_FALSE(rb.push(i).overwrote_unread);
    }
    ASSERT_TRUE(rb.full());

    // 第 4 条会盖掉最老的 "1"
    PushResult r = rb.push(4);
    EXPECT_TRUE(r.overwrote_unread);
    EXPECT_TRUE(r.ok);
    EXPECT_EQ(rb.overruns(), 1u);

    // size 一直被 capacity 夹住, 不会涨到 4
    EXPECT_EQ(rb.size(), 3u);

    // 最老的存活条目是 2, 被覆盖的 1 不会出现
    EXPECT_EQ(rb.pop(), 2u);
    EXPECT_EQ(rb.pop(), 3u);
    EXPECT_EQ(rb.pop(), 4u);
    EXPECT_EQ(rb.pop(), std::nullopt);
}

TEST(RingBufferBoundary, OverrunsAccumulateWhileConsumerSleeps) {
    RingBuffer<std::uint64_t> rb(4);
    for (std::uint64_t i = 1; i <= 4; ++i) {
        rb.push(i);
    }
    EXPECT_EQ(rb.overruns(), 0u);

    // 消费者不动, 生产者再写 6 条 -> 每条盖掉一条未读数据
    for (std::uint64_t i = 5; i <= 10; ++i) {
        rb.push(i);
    }

    // 消费者确实什么都没读
    EXPECT_EQ(rb.consumed(/*publish=*/false), 0u);
    EXPECT_EQ(rb.overruns(), 6u);
    EXPECT_EQ(rb.size(), 4u);
    // 只剩最后 4 条
    EXPECT_EQ(rb.pop(), 7u);
    EXPECT_EQ(rb.pop(), 8u);
    EXPECT_EQ(rb.pop(), 9u);
    EXPECT_EQ(rb.pop(), 10u);
    EXPECT_EQ(rb.pop(), std::nullopt);
}

TEST(RingBufferBoundary, PopSkipsOverwrittenEntriesInsteadOfReturningStale) {
    RingBuffer<std::uint64_t> rb(4);
    for (std::uint64_t i = 1; i <= 4; ++i) {
        rb.push(i);
    }
    // 覆盖掉 1 和 2
    rb.push(5);
    rb.push(6);

    auto got = rb.pop();
    ASSERT_TRUE(got.has_value());
    EXPECT_EQ(*got, 3u) << "应当跳过已被覆盖的 1、2";
    EXPECT_EQ(rb.pop(), 4u);
    EXPECT_EQ(rb.pop(), 5u);
    EXPECT_EQ(rb.pop(), 6u);
    EXPECT_EQ(rb.pop(), std::nullopt);
}

TEST(RingBufferBoundary, WrapsAroundManyTimesKeepingFifoOrder) {
    RingBuffer<std::uint64_t> rb(8);
    constexpr std::uint64_t kN = 1000;

    for (std::uint64_t i = 1; i <= kN; ++i) {
        rb.push(i);
        auto got = rb.pop();
        ASSERT_TRUE(got.has_value()) << "i=" << i;
        EXPECT_EQ(*got, i) << "环绕 " << i << " 次后顺序错乱";
    }
    EXPECT_TRUE(rb.empty());
    EXPECT_EQ(rb.pop(), std::nullopt);

    // 生产者每写一条消费者立刻取走, 因此不该出现任何 overrun
    EXPECT_EQ(rb.overruns(), 0u) << "边写边读不该丢数据";
}

TEST(RingBufferBoundary, ReadLatestAfterFullBufferReturnsNewest) {
    RingBuffer<std::uint64_t> rb(3);
    for (std::uint64_t i = 1; i <= 3; ++i) {
        rb.push(i);
    }
    ASSERT_TRUE(rb.full());

    EXPECT_EQ(rb.read_latest(), 3u);
    // 已经取过 3, 没有新数据
    EXPECT_EQ(rb.read_latest(), std::nullopt);

    rb.push(4);
    EXPECT_EQ(rb.read_latest(), 4u);
}

TEST(RingBufferBoundary, PopAndReadLatestDoNotInterfere) {
    RingBuffer<std::uint64_t> rb(4);
    for (std::uint64_t i = 1; i <= 4; ++i) {
        rb.push(i);
    }

    // read_latest 只看最新, 不消费 FIFO 队列
    EXPECT_EQ(rb.read_latest(), 4u);
    EXPECT_EQ(rb.size(), 4u);
    EXPECT_EQ(rb.pop(), 1u);
    EXPECT_EQ(rb.pop(), 2u);
    EXPECT_EQ(rb.pop(), 3u);
    EXPECT_EQ(rb.pop(), 4u);
    EXPECT_TRUE(rb.empty());
}

TEST(RingBufferBoundary, DropResetsBothReadPositions) {
    RingBuffer<std::uint64_t> rb(4);
    for (std::uint64_t i = 1; i <= 3; ++i) {
        rb.push(i);
    }
    ASSERT_EQ(rb.read_latest(), 3u);

    rb.drop();

    EXPECT_TRUE(rb.empty());
    EXPECT_EQ(rb.pop(), std::nullopt);
    EXPECT_EQ(rb.read_latest(), std::nullopt);

    // drop 之后还能继续正常用
    rb.push(99);
    EXPECT_EQ(rb.read_latest(), 99u);
    EXPECT_EQ(rb.pop(), 99u);
}

TEST(RingBufferBoundary, ReadLatestAfterSkipsOldFrames) {
    // ImageFrame 那种带 timestamp_us 的载荷
    struct Frame {
        std::uint64_t timestamp_us = 0;
    };
    RingBuffer<Frame> rb(8);

    for (std::uint64_t t = 100; t <= 500; t += 100) {
        rb.push(Frame{t});
    }

    auto f = rb.read_latest_after(350);
    ASSERT_TRUE(f.has_value());
    EXPECT_EQ(f->timestamp_us, 500u) << "应当返回最新的、且不早于 350 的那条";

    // 450 的写入顺序比 500 新, 时间戳也满足 >= 350, 所以它是"新到的合格帧"
    rb.push(Frame{450});
    auto g = rb.read_latest_after(350);
    ASSERT_TRUE(g.has_value());
    EXPECT_EQ(g->timestamp_us, 450u) << "新写入的合格帧应当能被取到";

    // 更晚写入的 600 也能正常取到
    rb.push(Frame{600});
    auto h = rb.read_latest_after(350);
    ASSERT_TRUE(h.has_value());
    EXPECT_EQ(h->timestamp_us, 600u);

    // 阈值高于任何现存帧 -> 空
    EXPECT_EQ(rb.read_latest_after(9999), std::nullopt);

    // 阈值很低 -> 直接拿最新那条
    rb.push(Frame{800});
    auto k = rb.read_latest_after(0);
    ASSERT_TRUE(k.has_value());
    EXPECT_EQ(k->timestamp_us, 800u);
}

TEST(RingBufferBoundary, ReadLatestAfterDropsStaleFrames) {
    // 时间戳太旧的积压帧应当被丢弃, 而不是把后续帧一起挡死
    struct Frame {
        std::uint64_t timestamp_us = 0;
    };
    RingBuffer<Frame> rb(8);
    rb.push(Frame{100});
    rb.push(Frame{200});

    // 阈值 500: 两条都太旧, 应当空
    EXPECT_EQ(rb.read_latest_after(500), std::nullopt);
    EXPECT_EQ(rb.read_latest_after(500), std::nullopt);

    // 之后来一条合格帧, 能正常取到 (说明没有被旧帧挡死)
    rb.push(Frame{700});
    auto f = rb.read_latest_after(500);
    ASSERT_TRUE(f.has_value());
    EXPECT_EQ(f->timestamp_us, 700u);

    // 已取过 700, 没有新数据
    EXPECT_EQ(rb.read_latest_after(500), std::nullopt);
}

TEST(RingBufferBoundary, ReadLatestWithTimeoutReturnsImmediatelyWhenDataReady) {
    RingBuffer<std::uint64_t> rb(4);
    rb.push(7);

    const auto t0 = std::chrono::steady_clock::now();
    auto got = rb.read_latest(std::chrono::milliseconds(500));
    const auto elapsed = std::chrono::steady_clock::now() - t0;

    ASSERT_TRUE(got.has_value());
    EXPECT_EQ(*got, 7u);
    EXPECT_LT(elapsed, std::chrono::milliseconds(100)) << "有数据时不该等满超时";
}

TEST(RingBufferBoundary, ReadLatestWithTimeoutGivesUpWhenNoData) {
    RingBuffer<std::uint64_t> rb(4);

    const auto t0 = std::chrono::steady_clock::now();
    auto got = rb.read_latest(std::chrono::milliseconds(120));
    const auto elapsed = std::chrono::steady_clock::now() - t0;

    EXPECT_EQ(got, std::nullopt);
    EXPECT_GE(elapsed, std::chrono::milliseconds(100)) << "没数据时应当等到超时";
}

TEST(RingBufferBoundary, ReadLatestWithTimeoutWakesUpOnLateData) {
    RingBuffer<std::uint64_t> rb(4);

    // 50ms 后由另一个线程写入
    std::thread producer([&rb] {
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
        rb.push(123);
    });

    auto got = rb.read_latest(std::chrono::milliseconds(2000));
    producer.join();

    ASSERT_TRUE(got.has_value()) << "等待期间来的数据应当被取到";
    EXPECT_EQ(*got, 123u);
}

// ===========================================================================
//  阶段 3 — 并发 (严格 SPSC: 一个生产者线程 + 一个消费者线程)
//
//  这些用例断言的是"不变量", 而不是精确条数 —— 覆盖式环形缓冲在生产者
//  超速时本来就会丢数据, 断言精确条数会变成随机失败的 flaky 测试.
//  真正必须守住的不变量是:
//    · 绝不读到"撕裂"的半个对象 (半写状态)
//    · 读到的值必须单调不减 (先进先出顺序不被破坏)
//    · 边界安全, 不崩溃、不死锁
//
//  Windows 上新建线程不一定马上被调度, 而主线程的紧凑读循环会一路跑完,
//  于是出现 "reads == 0" 的假失败. 所以每个用例都:
//    1) 等生产者真正开始写入 (started 标志)
//    2) 再等环形缓冲里真的出现数据 (rb 自身状态, 不会死等)
//  之后主线程才进入读循环.
// ===========================================================================

namespace {

/// 自旋等待生产者线程真正跑起来
void wait_until_started(std::atomic<bool>& started, int spins = 200000) {
    for (int i = 0; i < spins && !started.load(std::memory_order_acquire); ++i) {
        if ((i & 0xFF) == 0) {
            std::this_thread::yield();
        }
    }
}

/// 等环形缓冲里真的出现数据.
/// 只依赖 rb 自己的状态, 不依赖生产者侧标志, 所以不会死等.
template <typename T>
bool wait_for_data(RingBuffer<T>& rb, int spins = 4000000) {
    for (int i = 0; i < spins; ++i) {
        if (!rb.empty()) {
            return true;
        }
        if ((i & 0xFFF) == 0) {
            std::this_thread::yield();
        }
    }
    return false;
}

}  // namespace

TEST(RingBufferConcurrency, SingleProducerSingleConsumerKeepsEveryItem) {
    // 这是覆盖式环形缓冲: 生产者一旦领先超过容量就必然丢数据.
    // 所以"一条都不丢"只有在 写入总数 <= 容量 时才是可判定的 —— 否则断言
    // 会随调度抖动随机失败. 这里取 kCap > kN 来验证 SPSC 的无损通路.
    constexpr std::uint64_t kN = 4000;
    constexpr std::size_t kCap = 4096;

    RingBuffer<std::uint64_t> rb(kCap);
    rb.consumed();  // 声明起点, 让 overrun 统计准确

    std::atomic<bool> started{false};
    std::atomic<bool> producer_done{false};
    std::atomic<std::uint64_t> received{0};
    std::uint64_t sum = 0;
    bool order_ok = true;
    std::uint64_t expected = 1;

    std::thread producer([&] {
        for (std::uint64_t i = 1; i <= kN; ++i) {
            rb.push(i);
            if (i == 1) {
                started.store(true, std::memory_order_release);
            }
        }
        producer_done.store(true, std::memory_order_release);
    });

    std::thread consumer([&] {
        wait_until_started(started);
        wait_for_data(rb);
        for (;;) {
            auto v = rb.pop();
            if (v) {
                if (*v != expected) {
                    order_ok = false;
                }
                ++expected;
                sum += *v;
                received.fetch_add(1, std::memory_order_relaxed);
                continue;
            }
            if (producer_done.load(std::memory_order_acquire) && rb.empty()) {
                break;
            }
            std::this_thread::yield();
        }
    });

    producer.join();
    consumer.join();

    EXPECT_TRUE(order_ok) << "FIFO 顺序被破坏";
    EXPECT_EQ(received.load(), kN) << "写入总数未超过容量, 不该丢任何一条";
    EXPECT_EQ(sum, kN * (kN + 1) / 2) << "求和校验失败, 说明有重复或缺失";
    EXPECT_EQ(rb.overruns(), 0u) << "容量足够时不该有覆盖";
}

TEST(RingBufferConcurrency, OverwritingProducerNeverExposesTornValues) {
    // 生产者全速写, 容量很小, 必然发生覆盖.
    // 关键不变量: 读到的每个 Payload 必须自洽 (不是半写状态), 且单调不减.
    constexpr std::size_t kCap = 8;

    RingBuffer<Payload> rb(kCap);
    std::atomic<bool> started{false};
    std::atomic<bool> stop{false};

    std::thread producer([&] {
        std::uint32_t v = 0;
        started.store(true, std::memory_order_release);
        while (!stop.load(std::memory_order_relaxed)) {
            rb.push(make_payload(v));
            ++v;
        }
    });

    wait_until_started(started);
    wait_for_data(rb);

    std::uint32_t last = 0;
    bool first = true;
    bool consistent = true;
    bool monotonic = true;
    std::size_t reads = 0;

    for (int i = 0; i < 200000; ++i) {
        auto p = rb.read_latest();
        if (p) {
            if (!p->consistent()) {
                consistent = false;  // 读到撕裂数据
            }
            if (!first && p->value < last) {
                monotonic = false;  // 读到了更旧的值
            }
            last = p->value;
            first = false;
            ++reads;
        }
        if ((i & 0x3F) == 0) {
            std::this_thread::yield();
        }
    }

    stop.store(true, std::memory_order_relaxed);
    producer.join();

    EXPECT_GT(reads, 0u) << "应当至少读到一些帧";
    EXPECT_TRUE(consistent) << "读到了被覆盖到一半的撕裂对象";
    EXPECT_TRUE(monotonic) << "read_latest 返回了更旧的值";
}

TEST(RingBufferConcurrency, PopUnderOverwriteStaysConsistentAndAdvances) {
    // 生产者持续覆盖, 消费者用 pop() 顺序取.
    // 覆盖会把最老的挤掉, 所以允许"跳号" (那是丢失), 但不允许:
    //   · 返回值不自洽
    //   · 值回退
    constexpr std::size_t kCap = 8;

    RingBuffer<Payload> rb(kCap);
    std::atomic<bool> started{false};
    std::atomic<bool> stop{false};

    std::thread producer([&] {
        std::uint32_t v = 0;
        started.store(true, std::memory_order_release);
        while (!stop.load(std::memory_order_relaxed)) {
            rb.push(make_payload(v));
            ++v;
        }
    });

    wait_until_started(started);
    wait_for_data(rb);

    bool consistent = true;
    bool monotonic = true;
    bool have_last = false;
    std::uint32_t last = 0;
    std::size_t got = 0;

    for (int i = 0; i < 200000; ++i) {
        auto p = rb.pop();
        if (p) {
            if (!p->consistent()) {
                consistent = false;
            }
            if (have_last && p->value < last) {
                monotonic = false;
            }
            last = p->value;
            have_last = true;
            ++got;
        } else {
            std::this_thread::yield();
        }
    }

    stop.store(true, std::memory_order_relaxed);
    producer.join();

    EXPECT_GT(got, 0u);
    EXPECT_TRUE(consistent) << "pop 读到了撕裂对象";
    EXPECT_TRUE(monotonic) << "pop 破坏了先进先出顺序";
}

TEST(RingBufferConcurrency, ReadLatestNeverGoesBackwardsUnderLoad) {
    // 专项盯 read_latest 的单调性: 与生产者完全并发地读
    RingBuffer<std::uint64_t> rb(16);
    std::atomic<bool> started{false};
    std::atomic<bool> stop{false};

    std::thread producer([&] {
        std::uint64_t v = 0;
        started.store(true, std::memory_order_release);
        while (!stop.load(std::memory_order_relaxed)) {
            rb.push(v);
            ++v;
        }
    });

    wait_until_started(started);
    wait_for_data(rb);

    std::uint64_t last = 0;
    bool first = true;
    bool monotonic = true;
    std::size_t reads = 0;

    for (int i = 0; i < 200000; ++i) {
        auto v = rb.read_latest();
        if (v) {
            if (!first && *v < last) {
                monotonic = false;
            }
            last = *v;
            first = false;
            ++reads;
        }
        if ((i & 0x3F) == 0) {
            std::this_thread::yield();
        }
    }

    stop.store(true, std::memory_order_relaxed);
    producer.join();

    EXPECT_GT(reads, 0u);
    EXPECT_TRUE(monotonic) << "read_latest 水位倒退了";
}

TEST(RingBufferConcurrency, ConcurrentReadLatestWithTimeoutIsSafe) {
    // read_latest(timeout) 内部会轮询, 与生产者并发时不能卡死也不能读到脏值
    RingBuffer<std::uint64_t> rb(8);
    std::atomic<bool> started{false};
    std::atomic<bool> stop{false};

    std::thread producer([&] {
        std::uint64_t v = 1;
        started.store(true, std::memory_order_release);
        while (!stop.load(std::memory_order_relaxed)) {
            rb.push(v);
            ++v;
            std::this_thread::sleep_for(std::chrono::microseconds(50));
        }
    });

    wait_until_started(started);
    wait_for_data(rb);

    std::uint64_t last = 0;
    bool monotonic = true;
    bool first = true;

    for (int i = 0; i < 60; ++i) {
        auto v = rb.read_latest(std::chrono::milliseconds(20));
        if (v) {
            if (!first && *v < last) {
                monotonic = false;
            }
            last = *v;
            first = false;
        }
    }

    stop.store(true, std::memory_order_relaxed);
    producer.join();

    EXPECT_FALSE(first) << "生产者一直在写, 应当至少读到一次";
    EXPECT_TRUE(monotonic);
}

TEST(RingBufferConcurrency, CapacityOneStressDoesNotInvokeUb) {
    // 容量 1 是最极端的争用: 每个槽位都被反复覆盖
    RingBuffer<std::uint64_t> rb(1);
    std::atomic<bool> started{false};
    std::atomic<bool> stop{false};

    std::thread producer([&] {
        std::uint64_t v = 0;
        started.store(true, std::memory_order_release);
        while (!stop.load(std::memory_order_relaxed)) {
            rb.push(v);
            ++v;
        }
    });

    wait_until_started(started);
    wait_for_data(rb);

    std::uint64_t last = 0;
    bool monotonic = true;
    bool first = true;
    std::size_t reads = 0;

    for (int i = 0; i < 100000; ++i) {
        auto v = rb.read_latest();
        if (v) {
            if (!first && *v < last) {
                monotonic = false;
            }
            last = *v;
            first = false;
            ++reads;
        }
        auto p = rb.pop();
        if (p) {
            ++reads;
        }
        if ((i & 0x3F) == 0) {
            std::this_thread::yield();
        }
    }

    stop.store(true, std::memory_order_relaxed);
    producer.join();

    EXPECT_GT(reads, 0u);
    EXPECT_TRUE(monotonic);
    EXPECT_LE(rb.size(), 1u);
}
