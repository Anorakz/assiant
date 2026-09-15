// ============================================================================
//  tests/test_host_input_rb.cpp — HostInputRingBuffer 单元测试 (host 侧, L1)
//
//  运行方式:
//      scripts/test-host.ps1
//
//  覆盖范围
//  ---------------------------------------------------------------------------
//  空读 / 单事件 / 写满 / 覆盖 / read_all 语义 / read_all 与 read_latest 互不干扰
//
//  这类事件的载荷很小 (~40 字节), 不像图像帧那样需要省拷贝, 所以用例里
//  直接按值比较即可。
// ============================================================================

#include "host_input_rb.h"

#include <gtest/gtest.h>

#include <vector>

using agent::HostInputRingBuffer;
using agent::InputEvent;
using agent::kHostInputCapacity;

namespace {

/// 造一条键盘事件, 用 key 当编号方便断言
InputEvent key_event(std::uint32_t key, std::int64_t ts, InputEvent::Action action = InputEvent::PRESS) {
    InputEvent e{};
    e.type = InputEvent::KEY;
    e.modifier = 0;
    e.key = key;
    e.x = 0;
    e.y = 0;
    e.action = action;
    e.timestamp_ns = ts;
    return e;
}

/// 造一条鼠标事件
InputEvent mouse_event(std::int32_t x, std::int32_t y, std::int64_t ts) {
    InputEvent e{};
    e.type = InputEvent::MOUSE;
    e.modifier = 0;
    e.key = 0;
    e.x = x;
    e.y = y;
    e.action = InputEvent::PRESS;
    e.timestamp_ns = ts;
    return e;
}

/// 取出 read_all 结果里的 key 序列, 便于比对顺序
std::vector<std::uint32_t> keys_of(const std::vector<InputEvent>& v) {
    std::vector<std::uint32_t> ks;
    ks.reserve(v.size());
    for (const auto& e : v) {
        ks.push_back(e.key);
    }
    return ks;
}

}  // namespace

// ===========================================================================
//  空读
// ===========================================================================

TEST(HostInputRingBuffer, FreshBufferIsEmpty) {
    HostInputRingBuffer rb;

    EXPECT_EQ(rb.size(), 0u);
    EXPECT_EQ(rb.capacity(), kHostInputCapacity);
    EXPECT_EQ(rb.overruns(), 0u);
}

TEST(HostInputRingBuffer, ReadLatestOnEmptyFails) {
    HostInputRingBuffer rb;
    InputEvent out = key_event(0xAA, 0);
    const std::uint32_t before = out.key;

    EXPECT_FALSE(rb.read_latest(out));
    EXPECT_EQ(out.key, before) << "失败时不该写坏调用方的对象";
}

TEST(HostInputRingBuffer, ReadAllOnEmptyReturnsEmptyVector) {
    HostInputRingBuffer rb;

    EXPECT_TRUE(rb.read_all().empty());
    EXPECT_TRUE(rb.read_all().empty()) << "重复调用也应当稳定返回空";
}

// ===========================================================================
//  单事件
// ===========================================================================

TEST(HostInputRingBuffer, SingleEventReadLatest) {
    HostInputRingBuffer rb;
    rb.push(key_event(0x41, 1000));

    EXPECT_EQ(rb.size(), 1u);

    InputEvent out{};
    ASSERT_TRUE(rb.read_latest(out));
    EXPECT_EQ(out.type, InputEvent::KEY);
    EXPECT_EQ(out.key, 0x41u);
    EXPECT_EQ(out.timestamp_ns, 1000);
}

TEST(HostInputRingBuffer, ReadLatestConsumesWholeBatch) {
    // read_latest 是消费式: 取走最新一条, 同时丢弃比它更旧的未读事件
    HostInputRingBuffer rb;
    rb.push(key_event(0x41, 1000));

    InputEvent out{};
    ASSERT_TRUE(rb.read_latest(out));
    EXPECT_EQ(out.key, 0x41u);
    EXPECT_EQ(rb.size(), 0u) << "读过就该算消费掉";

    EXPECT_FALSE(rb.read_latest(out)) << "没有新事件时不该重复返回同一条";
    EXPECT_TRUE(rb.read_all().empty());

    // 来了新事件照常能拿到
    rb.push(key_event(0x42, 2000));
    ASSERT_TRUE(rb.read_latest(out));
    EXPECT_EQ(out.key, 0x42u);
}

TEST(HostInputRingBuffer, SingleEventReadAllConsumesIt) {
    HostInputRingBuffer rb;
    rb.push(key_event(0x42, 2000));

    auto first = rb.read_all();
    ASSERT_EQ(first.size(), 1u);
    EXPECT_EQ(first[0].key, 0x42u);
    EXPECT_EQ(first[0].timestamp_ns, 2000);

    EXPECT_EQ(rb.size(), 0u);
    EXPECT_TRUE(rb.read_all().empty()) << "已取走的事件不该再返回";
}

TEST(HostInputRingBuffer, MouseEventFieldsPreserved) {
    HostInputRingBuffer rb;
    rb.push(mouse_event(1920, 1080, 777));

    InputEvent out{};
    ASSERT_TRUE(rb.read_latest(out));
    EXPECT_EQ(out.type, InputEvent::MOUSE);
    EXPECT_EQ(out.x, 1920);
    EXPECT_EQ(out.y, 1080);
    EXPECT_EQ(out.timestamp_ns, 777);
}

TEST(HostInputRingBuffer, KeyEventFieldsPreserved) {
    HostInputRingBuffer rb;
    InputEvent e = key_event(0x1B, 555, InputEvent::RELEASE);
    e.modifier = 0x08;  // 假装是 META
    rb.push(e);

    InputEvent out{};
    ASSERT_TRUE(rb.read_latest(out));
    EXPECT_EQ(out.type, InputEvent::KEY);
    EXPECT_EQ(out.modifier, 0x08u);
    EXPECT_EQ(out.key, 0x1Bu);
    EXPECT_EQ(out.action, InputEvent::RELEASE);
    EXPECT_EQ(out.timestamp_ns, 555);
}

// ===========================================================================
//  写满
// ===========================================================================

TEST(HostInputRingBuffer, FillsToCapacityWithoutOverrun) {
    HostInputRingBuffer rb;

    for (std::size_t i = 0; i < rb.capacity(); ++i) {
        rb.push(key_event(static_cast<std::uint32_t>(i), static_cast<std::int64_t>(i)));
    }

    EXPECT_EQ(rb.size(), rb.capacity());
    EXPECT_EQ(rb.overruns(), 0u) << "写满过程中不该丢事件";
}

TEST(HostInputRingBuffer, ReadAllReturnsExactlyTheEventsPushed) {
    HostInputRingBuffer rb;
    const std::size_t n = rb.capacity();
    const std::size_t half = n / 2;

    for (std::size_t i = 0; i < half; ++i) {
        rb.push(key_event(static_cast<std::uint32_t>(i + 1), static_cast<std::int64_t>(i)));
    }

    auto all = rb.read_all();
    ASSERT_EQ(all.size(), half);

    // FIFO 顺序
    for (std::size_t i = 0; i < half; ++i) {
        EXPECT_EQ(all[i].key, static_cast<std::uint32_t>(i + 1)) << "第 " << i << " 条顺序不对";
    }
}

// ===========================================================================
//  覆盖
// ===========================================================================

TEST(HostInputRingBuffer, OverwriteDropsOldestAndCountsIt) {
    HostInputRingBuffer rb;
    const std::size_t cap = rb.capacity();

    for (std::size_t i = 0; i < cap; ++i) {
        rb.push(key_event(static_cast<std::uint32_t>(i + 1), static_cast<std::int64_t>(i)));
    }
    ASSERT_EQ(rb.overruns(), 0u);

    // 再写 1 条 -> 丢掉最旧的 key=1
    rb.push(key_event(0xEEEE, static_cast<std::int64_t>(cap)));

    EXPECT_EQ(rb.overruns(), 1u);
    EXPECT_EQ(rb.size(), cap);

    // read_all 里不该再出现被丢掉的事件
    auto all = rb.read_all();
    ASSERT_EQ(all.size(), cap);
    EXPECT_EQ(all.front().key, 2u) << "最旧的 key=1 应已被丢弃";
    EXPECT_EQ(all.back().key, 0xEEEEu);
}

TEST(HostInputRingBuffer, PushNeverBlocksWhenFull) {
    HostInputRingBuffer rb;
    const std::size_t cap = rb.capacity();

    // 写 3 倍容量
    for (std::size_t i = 0; i < cap * 3; ++i) {
        rb.push(key_event(static_cast<std::uint32_t>(i), static_cast<std::int64_t>(i)));
    }

    EXPECT_EQ(rb.size(), cap);
    EXPECT_EQ(rb.overruns(), cap * 2) << "多写的每一条都该丢掉一条未读事件";

    // 只该剩最后 cap 条
    auto all = rb.read_all();
    ASSERT_EQ(all.size(), cap);
    EXPECT_EQ(all.front().key, static_cast<std::uint32_t>(cap * 2));
    EXPECT_EQ(all.back().key, static_cast<std::uint32_t>(cap * 3 - 1));
}

TEST(HostInputRingBuffer, ReadLatestSeesNewestAfterOverwrite) {
    HostInputRingBuffer rb;
    const std::size_t cap = rb.capacity();

    for (std::size_t i = 0; i < cap + 5; ++i) {
        rb.push(key_event(static_cast<std::uint32_t>(i), static_cast<std::int64_t>(i)));
    }

    InputEvent out{};
    ASSERT_TRUE(rb.read_latest(out));
    EXPECT_EQ(out.key, static_cast<std::uint32_t>(cap + 4));
    EXPECT_EQ(rb.overruns(), 5u);
}

// ===========================================================================
//  read_all 的游标语义
// ===========================================================================

TEST(HostInputRingBuffer, ReadAllAdvancesCursorAndReturnsOnlyNewEvents) {
    HostInputRingBuffer rb;

    rb.push(key_event(1, 100));
    rb.push(key_event(2, 200));

    auto first = rb.read_all();
    ASSERT_EQ(keys_of(first), (std::vector<std::uint32_t>{1, 2}));

    // 期间没有新事件 -> 空
    EXPECT_TRUE(rb.read_all().empty());

    // 来两条新的 -> 只返回这两条
    rb.push(key_event(3, 300));
    rb.push(key_event(4, 400));

    auto second = rb.read_all();
    EXPECT_EQ(keys_of(second), (std::vector<std::uint32_t>{3, 4}));

    EXPECT_TRUE(rb.read_all().empty());
}

TEST(HostInputRingBuffer, ReadAllAfterOverwriteReturnsOldestSurvivors) {
    // 消费者长时间没读, 生产写满了 => 老事件丢掉, 剩下的按序返回
    HostInputRingBuffer rb;
    const std::size_t cap = rb.capacity();

    for (std::size_t i = 0; i < cap + 10; ++i) {
        rb.push(key_event(static_cast<std::uint32_t>(i), static_cast<std::int64_t>(i)));
    }
    ASSERT_EQ(rb.overruns(), 10u);

    auto all = rb.read_all();
    ASSERT_EQ(all.size(), cap) << "只该拿到存活的 cap 条";
    EXPECT_EQ(all.front().key, 10u) << "前 10 条已被覆盖";
    EXPECT_EQ(all.back().key, static_cast<std::uint32_t>(cap + 9));

    // 顺序必须仍然递增
    for (std::size_t i = 1; i < all.size(); ++i) {
        EXPECT_LT(all[i - 1].key, all[i].key) << "第 " << i << " 条顺序乱了";
    }
}

TEST(HostInputRingBuffer, ReadAllThenOverwriteThenReadAllAgain) {
    // 覆盖发生在两次 read_all 之间 —— 游标要能跳过被覆盖的区间
    HostInputRingBuffer rb;
    const std::size_t cap = rb.capacity();

    // 先塞满
    for (std::size_t i = 0; i < cap; ++i) {
        rb.push(key_event(static_cast<std::uint32_t>(i), static_cast<std::int64_t>(i)));
    }
    auto first = rb.read_all();
    ASSERT_EQ(first.size(), cap);

    // 再写 cap + 3 条: 全部覆盖, 只该剩最后 cap 条
    for (std::size_t i = 0; i < cap + 3; ++i) {
        rb.push(key_event(static_cast<std::uint32_t>(1000 + i), static_cast<std::int64_t>(1000 + i)));
    }

    auto second = rb.read_all();
    ASSERT_EQ(second.size(), cap);
    EXPECT_EQ(second.front().key, 1003u)
        << "前 3 条 (1000/1001/1002) 应已被覆盖";
    EXPECT_EQ(second.back().key, static_cast<std::uint32_t>(1000 + cap + 2));
}

TEST(HostInputRingBuffer, ReadAllSurvivesManyCycles) {
    // 反复"写一批 -> 读空", 每次都不该丢
    HostInputRingBuffer rb;

    for (int cycle = 0; cycle < 20; ++cycle) {
        for (std::uint32_t k = 0; k < 50; ++k) {
            rb.push(key_event(cycle * 1000 + k, cycle * 1000 + k));
        }
        auto all = rb.read_all();
        ASSERT_EQ(all.size(), 50u) << "cycle " << cycle;
        for (std::uint32_t k = 0; k < 50; ++k) {
            EXPECT_EQ(all[k].key, static_cast<std::uint32_t>(cycle * 1000) + k);
        }
        EXPECT_TRUE(rb.read_all().empty());
    }

    EXPECT_EQ(rb.overruns(), 0u) << "边写边读不该丢事件";
}

// ===========================================================================
//  read_all 与 read_latest 互不干扰
// ===========================================================================

TEST(HostInputRingBuffer, ReadLatestDiscardsOlderUnreadEvents) {
    // 单游标模型的核心取舍: 调了 read_latest 之后, 比它更旧的未读事件
    // 就被丢掉了, 不会再出现在 read_all 里
    HostInputRingBuffer rb;
    for (std::uint32_t k = 1; k <= 5; ++k) {
        rb.push(key_event(k, k * 100));
    }
    ASSERT_EQ(rb.size(), 5u);

    InputEvent latest{};
    ASSERT_TRUE(rb.read_latest(latest));
    EXPECT_EQ(latest.key, 5u);
    EXPECT_EQ(rb.size(), 0u) << "read_latest 之后整批都算已消费";

    EXPECT_TRUE(rb.read_all().empty())
        << "旧的 1..4 应当已被丢弃, 而不是留给 read_all";
}

TEST(HostInputRingBuffer, ReadAllThenReadLatestYieldsNothingUntilNewEvent) {
    // 反向: read_all 取走全部之后, read_latest 也看不到旧事件了
    HostInputRingBuffer rb;
    for (std::uint32_t k = 1; k <= 4; ++k) {
        rb.push(key_event(k, k * 100));
    }

    auto all = rb.read_all();
    ASSERT_EQ(all.size(), 4u);
    EXPECT_EQ(rb.size(), 0u);

    InputEvent latest{};
    EXPECT_FALSE(rb.read_latest(latest)) << "没有未读事件了";
    EXPECT_FALSE(rb.read_latest(latest));

    // 来一条新的, 两边的表现都要正常
    rb.push(key_event(5, 500));
    ASSERT_TRUE(rb.read_latest(latest));
    EXPECT_EQ(latest.key, 5u);

    EXPECT_TRUE(rb.read_all().empty()) << "刚被 read_latest 消费掉";
}

TEST(HostInputRingBuffer, InterleavedReadLatestAndReadAll) {
    // 交替调用, 共享同一个消费游标
    HostInputRingBuffer rb;

    rb.push(key_event(1, 100));
    InputEvent latest{};
    ASSERT_TRUE(rb.read_latest(latest));
    EXPECT_EQ(latest.key, 1u);
    EXPECT_EQ(rb.size(), 0u);

    EXPECT_TRUE(rb.read_all().empty()) << "1 已被 read_latest 消费";

    rb.push(key_event(2, 200));
    rb.push(key_event(3, 300));

    // read_latest 看到 3, 并把 2 一起丢掉
    ASSERT_TRUE(rb.read_latest(latest));
    EXPECT_EQ(latest.key, 3u);
    EXPECT_EQ(rb.size(), 0u);

    // read_all 只该看到之后新到的
    rb.push(key_event(4, 400));
    auto batch = rb.read_all();
    EXPECT_EQ(keys_of(batch), (std::vector<std::uint32_t>{4}));
}

// ===========================================================================
//  容量
// ===========================================================================

TEST(HostInputRingBuffer, CapacityIsOneTwentyEight) {
    HostInputRingBuffer rb;
    EXPECT_EQ(rb.capacity(), 128u);
    EXPECT_EQ(kHostInputCapacity, 128u);
}
