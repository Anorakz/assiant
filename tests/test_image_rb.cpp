// ============================================================================
//  tests/test_image_rb.cpp — ImageRingBuffer 单元测试 (host 侧, L1)
//
//  运行方式:
//      scripts/test-host.ps1
//
//  覆盖范围
//  ---------------------------------------------------------------------------
//  空读 / 单帧 / 写满 / 覆盖 / 按时间戳读
//  外加: 非破坏性查询、容量与内存、边界时间戳、多轮覆盖后按时间戳回溯
//
//  注意: 一帧 ≈ 196KB, 300 帧 ≈ 56MB。这些用例不复制整个 ImageRingBuffer,
//  避免把栈撑爆; 需要多帧时用 helper 按需构造。
// ============================================================================

#include "image_rb.h"

#include <gtest/gtest.h>

#include <cstddef>
#include <cstring>
#include <vector>

using agent::Frame;
using agent::ImageRingBuffer;
using agent::kFrameBytes;
using agent::kImageRingCapacity;

namespace {

/// 造一帧: 像素填 pattern, 时间戳 ts; 不复制大对象
Frame make_frame(std::uint8_t pattern, std::int64_t ts) {
    Frame f;
    std::memset(f.data, pattern, kFrameBytes);
    f.timestamp_ns = ts;
    return f;
}

/// 校验 out 的像素是否全为 pattern
bool pixels_are(const Frame& out, std::uint8_t pattern) {
    for (std::size_t i = 0; i < kFrameBytes; ++i) {
        if (out.data[i] != pattern) {
            return false;
        }
    }
    return true;
}

/// 第 i 个字节的采样值, 用来快速区分不同帧 (全量扫描 196KB 太慢)
std::uint8_t probe(const Frame& f, std::size_t i = 0) { return f.data[i]; }

}  // namespace

// ===========================================================================
//  空读
// ===========================================================================

TEST(ImageRingBuffer, FreshBufferIsEmpty) {
    ImageRingBuffer rb;

    EXPECT_EQ(rb.size(), 0u);
    EXPECT_EQ(rb.capacity(), kImageRingCapacity);
    EXPECT_EQ(rb.overruns(), 0u);
}

TEST(ImageRingBuffer, ReadLatestOnEmptyFails) {
    ImageRingBuffer rb;
    Frame out = make_frame(0xAA, 0);
    const std::int64_t before = out.timestamp_ns;

    EXPECT_FALSE(rb.read_latest(out));
    // 失败时不应写坏调用方的缓冲
    EXPECT_EQ(out.timestamp_ns, before);
}

TEST(ImageRingBuffer, ReadByTimestampOnEmptyFails) {
    ImageRingBuffer rb;
    Frame out = make_frame(0xAA, 0);

    EXPECT_FALSE(rb.read_by_timestamp(0, out));
    EXPECT_FALSE(rb.read_by_timestamp(123456, out));
}

// ===========================================================================
//  单帧
// ===========================================================================

TEST(ImageRingBuffer, SingleFrameReadLatest) {
    ImageRingBuffer rb;
    rb.push(make_frame(0x11, 1000));

    EXPECT_EQ(rb.size(), 1u);

    Frame out = make_frame(0x00, 0);
    ASSERT_TRUE(rb.read_latest(out));
    EXPECT_EQ(out.timestamp_ns, 1000);
    EXPECT_EQ(probe(out), 0x11);
}

TEST(ImageRingBuffer, SingleFrameReadByTimestampCoversBothSides) {
    ImageRingBuffer rb;
    rb.push(make_frame(0x22, 5000));

    Frame out = make_frame(0x00, 0);

    // ts 正好等于该帧
    ASSERT_TRUE(rb.read_by_timestamp(5000, out));
    EXPECT_EQ(out.timestamp_ns, 5000);
    EXPECT_EQ(probe(out), 0x22);

    // ts 比该帧晚 -> 仍是这一帧
    ASSERT_TRUE(rb.read_by_timestamp(9999, out));
    EXPECT_EQ(out.timestamp_ns, 5000);

    // ts 比该帧早 -> 没有"不晚于 ts"的帧
    EXPECT_FALSE(rb.read_by_timestamp(4999, out));
}

TEST(ImageRingBuffer, SingleFrameReadLatestIsNotRepeated) {
    // read_latest 是流式语义: 取过一次就不再重复返回同一帧
    ImageRingBuffer rb;
    rb.push(make_frame(0x33, 100));

    Frame out = make_frame(0x00, 0);
    ASSERT_TRUE(rb.read_latest(out));
    EXPECT_EQ(out.timestamp_ns, 100);

    EXPECT_FALSE(rb.read_latest(out)) << "没有新帧时不该重复返回旧的";

    rb.push(make_frame(0x44, 200));
    ASSERT_TRUE(rb.read_latest(out));
    EXPECT_EQ(out.timestamp_ns, 200);
    EXPECT_EQ(probe(out), 0x44);
}

// ===========================================================================
//  写满
// ===========================================================================

TEST(ImageRingBuffer, FillsToCapacityWithoutOverrun) {
    ImageRingBuffer rb;

    for (std::size_t i = 0; i < rb.capacity(); ++i) {
        rb.push(make_frame(static_cast<std::uint8_t>(i), static_cast<std::int64_t>(i) * 1000));
    }

    EXPECT_EQ(rb.size(), rb.capacity());
    EXPECT_EQ(rb.overruns(), 0u) << "写满过程中不该丢帧";
}

TEST(ImageRingBuffer, SizeIsClampedAtCapacityWhenOverwritten) {
    ImageRingBuffer rb;

    // 写 1.5 倍容量
    const std::size_t n = rb.capacity() + rb.capacity() / 2;
    for (std::size_t i = 0; i < n; ++i) {
        rb.push(make_frame(static_cast<std::uint8_t>(i), static_cast<std::int64_t>(i) * 1000));
    }

    EXPECT_EQ(rb.size(), rb.capacity()) << "size 必须被 capacity 夹住";
}

// ===========================================================================
//  覆盖
// ===========================================================================

TEST(ImageRingBuffer, OverwriteDropsOldestAndCountsIt) {
    ImageRingBuffer rb;
    const std::size_t cap = rb.capacity();

    for (std::size_t i = 0; i < cap; ++i) {
        rb.push(make_frame(static_cast<std::uint8_t>(i), static_cast<std::int64_t>(i) * 1000));
    }
    ASSERT_EQ(rb.overruns(), 0u);

    // 再写 1 帧 -> 盖掉最旧的那帧 (timestamp 0)
    rb.push(make_frame(0xEE, static_cast<std::int64_t>(cap) * 1000));

    EXPECT_EQ(rb.overruns(), 1u);
    EXPECT_EQ(rb.size(), cap);

    // 最旧存活帧的 ts 变成 1000; 被盖掉的 ts=0 已经查不到了
    Frame out = make_frame(0x00, 0);
    EXPECT_FALSE(rb.read_by_timestamp(0, out)) << "ts=0 的帧应已被覆盖";
    ASSERT_TRUE(rb.read_by_timestamp(1000, out));
    EXPECT_EQ(out.timestamp_ns, 1000);
}

TEST(ImageRingBuffer, ReadLatestSeesNewestAfterOverwrite) {
    ImageRingBuffer rb;
    const std::size_t cap = rb.capacity();

    for (std::size_t i = 0; i < cap + 10; ++i) {
        rb.push(make_frame(static_cast<std::uint8_t>(i), static_cast<std::int64_t>(i) * 1000));
    }

    Frame out = make_frame(0x00, 0);
    ASSERT_TRUE(rb.read_latest(out));
    EXPECT_EQ(out.timestamp_ns, static_cast<std::int64_t>(cap + 9) * 1000);
    EXPECT_EQ(probe(out), static_cast<std::uint8_t>(cap + 9));

    EXPECT_EQ(rb.overruns(), 10u);
}

TEST(ImageRingBuffer, PushNeverBlocksWhenFull) {
    // 覆盖式的核心承诺: 满了也照写不误
    ImageRingBuffer rb;
    const std::size_t cap = rb.capacity();

    for (std::size_t i = 0; i < cap * 3; ++i) {
        rb.push(make_frame(static_cast<std::uint8_t>(i), static_cast<std::int64_t>(i)));
    }

    EXPECT_EQ(rb.size(), cap);
    EXPECT_EQ(rb.overruns(), cap * 2) << "多写的每一帧都该盖掉一帧未读数据";
}

// ===========================================================================
//  按时间戳读
// ===========================================================================

TEST(ImageRingBuffer, ReadByTimestampPicksClosestNotExceeding) {
    ImageRingBuffer rb;
    // 帧时间戳: 1000, 2000, 3000, 4000, 5000
    for (int i = 1; i <= 5; ++i) {
        rb.push(make_frame(static_cast<std::uint8_t>(i), i * 1000));
    }

    Frame out = make_frame(0x00, 0);

    // 正好命中
    ASSERT_TRUE(rb.read_by_timestamp(3000, out));
    EXPECT_EQ(out.timestamp_ns, 3000);
    EXPECT_EQ(probe(out), 3);

    // 落在两帧之间 -> 取不晚于 ts 的那帧 (3500 -> 3000)
    ASSERT_TRUE(rb.read_by_timestamp(3500, out));
    EXPECT_EQ(out.timestamp_ns, 3000);

    // 比所有帧都晚 -> 取最新
    ASSERT_TRUE(rb.read_by_timestamp(99999, out));
    EXPECT_EQ(out.timestamp_ns, 5000);

    // 比所有帧都早 -> 没得取
    EXPECT_FALSE(rb.read_by_timestamp(999, out));
}

TEST(ImageRingBuffer, ReadByTimestampIsNonDestructiveAndRepeatable) {
    // 这是它和 read_latest 的关键区别: 查询式, 不推进游标
    ImageRingBuffer rb;
    for (int i = 1; i <= 4; ++i) {
        rb.push(make_frame(static_cast<std::uint8_t>(i), i * 1000));
    }

    Frame a = make_frame(0x00, 0);
    Frame b = make_frame(0x00, 0);

    ASSERT_TRUE(rb.read_by_timestamp(2500, a));
    EXPECT_EQ(a.timestamp_ns, 2000);

    // 同样的查询再来一次, 结果必须一致
    ASSERT_TRUE(rb.read_by_timestamp(2500, b));
    EXPECT_EQ(b.timestamp_ns, 2000);
    EXPECT_EQ(probe(b), 2);

    // 也别影响 read_latest
    Frame latest = make_frame(0x00, 0);
    ASSERT_TRUE(rb.read_latest(latest));
    EXPECT_EQ(latest.timestamp_ns, 4000);
}

TEST(ImageRingBuffer, ReadByTimestampDoesNotAffectLatestWatermark) {
    ImageRingBuffer rb;
    for (int i = 1; i <= 3; ++i) {
        rb.push(make_frame(static_cast<std::uint8_t>(i), i * 1000));
    }

    Frame out = make_frame(0x00, 0);
    // 先做几次时间戳查询
    EXPECT_TRUE(rb.read_by_timestamp(1000, out));
    EXPECT_TRUE(rb.read_by_timestamp(2000, out));

    // read_latest 仍然应当拿到最新那帧
    ASSERT_TRUE(rb.read_latest(out));
    EXPECT_EQ(out.timestamp_ns, 3000);
}

TEST(ImageRingBuffer, ReadByTimestampExactBoundaries) {
    ImageRingBuffer rb;
    for (int i = 1; i <= 3; ++i) {
        rb.push(make_frame(static_cast<std::uint8_t>(i), i * 1000));
    }

    Frame out = make_frame(0x00, 0);

    // 等于最旧帧的 ts
    ASSERT_TRUE(rb.read_by_timestamp(1000, out));
    EXPECT_EQ(out.timestamp_ns, 1000);

    // 比最旧帧小 1ns -> 取不到
    EXPECT_FALSE(rb.read_by_timestamp(999, out));

    // 等于最新帧的 ts
    ASSERT_TRUE(rb.read_by_timestamp(3000, out));
    EXPECT_EQ(out.timestamp_ns, 3000);
}

TEST(ImageRingBuffer, ReadByTimestampAfterWrapStillFindsOldestSurvivor) {
    ImageRingBuffer rb;
    const std::size_t cap = rb.capacity();

    // 写满 + 再写 50 帧, 最旧的 50 帧被覆盖
    for (std::size_t i = 0; i < cap + 50; ++i) {
        rb.push(make_frame(static_cast<std::uint8_t>(i), static_cast<std::int64_t>(i) * 1000));
    }

    Frame out = make_frame(0x00, 0);

    // 最旧的存活帧是 i=50 (ts=50000)
    ASSERT_TRUE(rb.read_by_timestamp(50000, out));
    EXPECT_EQ(out.timestamp_ns, 50000);
    EXPECT_EQ(probe(out), static_cast<std::uint8_t>(50));

    // 再早一点就没了
    EXPECT_FALSE(rb.read_by_timestamp(49999, out));

    // 最新一帧
    ASSERT_TRUE(rb.read_by_timestamp(1LL << 40, out));
    EXPECT_EQ(out.timestamp_ns, static_cast<std::int64_t>(cap + 49) * 1000);
}

TEST(ImageRingBuffer, ReadByTimestampBetweenWrappedFrames) {
    ImageRingBuffer rb;
    const std::size_t cap = rb.capacity();

    for (std::size_t i = 0; i < cap + 20; ++i) {
        rb.push(make_frame(static_cast<std::uint8_t>(i), static_cast<std::int64_t>(i) * 100));
    }

    Frame out = make_frame(0x00, 0);
    // ts=2650, 落在 i=26 (2600) 和 i=27 (2700) 之间 -> 应取 i=26
    ASSERT_TRUE(rb.read_by_timestamp(2650, out));
    EXPECT_EQ(out.timestamp_ns, 2600);
    EXPECT_EQ(probe(out), static_cast<std::uint8_t>(26));
}

// ===========================================================================
//  容量 / 内存形态
// ===========================================================================

TEST(ImageRingBuffer, CapacityIsThreeHundred) {
    ImageRingBuffer rb;
    EXPECT_EQ(rb.capacity(), 300u);
    EXPECT_EQ(kImageRingCapacity, 300u);
}

TEST(ImageRingBuffer, FrameLayoutIsPackedAsExpected) {
    // timestamp_ns 必须紧跟在像素后面 —— timestamp_of() 的按前缀读取依赖这点
    EXPECT_EQ(offsetof(Frame, timestamp_ns), kFrameBytes);
    EXPECT_EQ(sizeof(Frame), kFrameBytes + sizeof(std::int64_t));
}

TEST(ImageRingBuffer, LargeStorageLivesOnHeapNotStack) {
    // 300 帧 ≈ 56MB。如果槽位是栈/内联成员, 这个对象本身就会大得离谱;
    // 这里断言 ImageRingBuffer 自身很小 (槽位在堆上)。
    EXPECT_LT(sizeof(ImageRingBuffer), 1024u)
        << "ImageRingBuffer 对象本身应当很小, 大块内存在堆上";
}
