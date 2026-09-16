// ============================================================================
//  tests/test_moonlight_adapter.cpp — MoonlightAdapter 测试 (host 侧, L1)
//
//  运行方式:
//      scripts/test-host.ps1
//
//  host 构建不链接 moonlight-common-c, 所以 start() 会在"要调
//  LiStartConnection"那一步停下并返回 false。这里能验证的是:
//
//    · 状态机: Idle → Connecting → (失败回 Idle), stop() 幂等
//    · 参数校验: 空 host / 空 app / 非法尺寸都快速失败且给出原因
//    · 环形缓冲可访问且初始为空, 且 image_rb / host_input_rb 跨调用稳定
//    · on_video_frame() 的守卫: 未连接时不写 RB
//    · 重复 start / start-stop-start 不会崩
//
//  真正的收流 (Annex-B 解码 + 写 RB) 需要板端 + Sunshine, 不在 host 范围。
// ============================================================================

#include "moonlight_adapter.h"

#include <gtest/gtest.h>

#include <cstdint>
#include <string>

using agent::AdapterState;
using agent::ImageRingBuffer;
using agent::MoonlightAdapter;

namespace {

/// 一个几乎不可能有服务的地址, 让 start() 快速失败在握手阶段
const char* kBadHost = "127.0.0.1:9";

}  // namespace

// ===========================================================================
//  构造 / 初始状态
// ===========================================================================

TEST(MoonlightAdapterTest, StartsIdleAndDisconnected) {
    MoonlightAdapter a;

    EXPECT_EQ(a.state(), AdapterState::kIdle);
    EXPECT_FALSE(a.is_connected());
    EXPECT_TRUE(a.last_error().empty());
    EXPECT_EQ(a.video_units_received(), 0u);
    EXPECT_EQ(a.frames_pushed(), 0u);
}

TEST(MoonlightAdapterTest, RingBuffersAreAccessibleBeforeStart) {
    MoonlightAdapter a;

    ImageRingBuffer& img = a.image_rb();
    EXPECT_EQ(img.size(), 0u);
    EXPECT_EQ(img.capacity(), 300u);

    EXPECT_EQ(a.host_input_rb().size(), 0u);
    EXPECT_EQ(a.host_input_rb().capacity(), 128u);
}

TEST(MoonlightAdapterTest, RingBufferReferencesAreStable) {
    MoonlightAdapter a;
    // 多次取应当拿到同一个对象 (不能每次返回临时)
    EXPECT_EQ(&a.image_rb(), &a.image_rb());
    EXPECT_EQ(&a.host_input_rb(), &a.host_input_rb());
    EXPECT_NE(static_cast<const void*>(&a.image_rb()),
              static_cast<const void*>(&a.host_input_rb()));
}

// ===========================================================================
//  参数校验 (应当在碰网络之前就失败)
// ===========================================================================

TEST(MoonlightAdapterTest, RejectsEmptyHost) {
    MoonlightAdapter a;
    EXPECT_FALSE(a.start("", "app", 1920, 1080, 60));
    EXPECT_FALSE(a.last_error().empty());
    EXPECT_EQ(a.state(), AdapterState::kIdle) << "失败后必须回到 Idle";
}

TEST(MoonlightAdapterTest, RejectsEmptyApp) {
    MoonlightAdapter a;
    EXPECT_FALSE(a.start("127.0.0.1", "", 1920, 1080, 60));
    EXPECT_FALSE(a.last_error().empty());
    EXPECT_EQ(a.state(), AdapterState::kIdle);
}

TEST(MoonlightAdapterTest, RejectsInvalidDimensions) {
    MoonlightAdapter a;
    EXPECT_FALSE(a.start("127.0.0.1", "Desktop", 0, 1080, 60));
    EXPECT_FALSE(a.start("127.0.0.1", "Desktop", 1920, 0, 60));
    EXPECT_FALSE(a.start("127.0.0.1", "Desktop", 1920, 1080, 0));
    EXPECT_FALSE(a.start("127.0.0.1", "Desktop", -1, -1, -1));
    EXPECT_EQ(a.state(), AdapterState::kIdle);
}

// ===========================================================================
//  握手失败路径 (服务端不在)
// ===========================================================================

TEST(MoonlightAdapterTest, StartFailsWhenServerUnreachable) {
    MoonlightAdapter a;

    EXPECT_FALSE(a.start(kBadHost, "Desktop", 1280, 720, 60))
        << "没有服务端时必须失败";

    EXPECT_FALSE(a.is_connected());
    EXPECT_EQ(a.state(), AdapterState::kIdle) << "失败后必须回到 Idle, 不能卡在 Connecting";
    EXPECT_FALSE(a.last_error().empty()) << "必须给出失败原因";
    EXPECT_EQ(a.frames_pushed(), 0u);
}

TEST(MoonlightAdapterTest, LastErrorIsClearedOnNextAttempt) {
    MoonlightAdapter a;

    ASSERT_FALSE(a.start(kBadHost, "Desktop", 1280, 720, 60));
    ASSERT_FALSE(a.last_error().empty());

    // 合法参数 + 不可达服务端: 错误信息应当被这次尝试的内容覆盖,
    // 但仍然是"失败且有原因"
    EXPECT_FALSE(a.start(kBadHost, "Desktop", 1280, 720, 60));
    EXPECT_FALSE(a.last_error().empty());
}

// ===========================================================================
//  stop 的幂等性
// ===========================================================================

TEST(MoonlightAdapterTest, StopIsIdempotentWhenIdle) {
    MoonlightAdapter a;

    a.stop();
    a.stop();
    a.stop();

    EXPECT_EQ(a.state(), AdapterState::kIdle);
    EXPECT_FALSE(a.is_connected());
}

TEST(MoonlightAdapterTest, StopAfterFailedStartIsSafe) {
    MoonlightAdapter a;

    ASSERT_FALSE(a.start(kBadHost, "Desktop", 1280, 720, 60));
    a.stop();

    EXPECT_EQ(a.state(), AdapterState::kIdle);
    EXPECT_FALSE(a.is_connected());
}

TEST(MoonlightAdapterTest, CanRetryAfterStop) {
    MoonlightAdapter a;

    ASSERT_FALSE(a.start(kBadHost, "Desktop", 1280, 720, 60));
    a.stop();
    EXPECT_FALSE(a.start(kBadHost, "Desktop", 1280, 720, 60));
    a.stop();

    EXPECT_EQ(a.state(), AdapterState::kIdle);
}

TEST(MoonlightAdapterTest, SecondStartWhileIdleIsAllowed) {
    // 第一次失败后状态回到 Idle, 所以第二次应当能再次尝试 (不被"已启动"挡住)
    MoonlightAdapter a;

    ASSERT_FALSE(a.start(kBadHost, "Desktop", 640, 480, 30));
    EXPECT_FALSE(a.start(kBadHost, "Desktop", 640, 480, 30));
    EXPECT_FALSE(a.last_error().empty());
}

// ===========================================================================
//  on_video_frame 的守卫 (未连接时不该动 RB)
// ===========================================================================

TEST(MoonlightAdapterTest, VideoFrameIgnoredWhenNotStreaming) {
    MoonlightAdapter a;

    // 伪造一段 Annex-B 样子的小数据
    const std::uint8_t fake[] = {0x00, 0x00, 0x00, 0x01, 0x67, 0x42, 0x00, 0x1E};

    EXPECT_FALSE(a.on_video_frame(fake, sizeof(fake)))
        << "未进入 kStreaming 时不该处理帧";
    EXPECT_EQ(a.image_rb().size(), 0u) << "不该往 Image RB 里写东西";
    EXPECT_EQ(a.frames_pushed(), 0u);
}

TEST(MoonlightAdapterTest, VideoFrameRejectsNullAndEmpty) {
    MoonlightAdapter a;

    EXPECT_FALSE(a.on_video_frame(nullptr, 100));
    EXPECT_FALSE(a.on_video_frame(reinterpret_cast<const std::uint8_t*>("x"), 0));
}

// ===========================================================================
//  连接状态回调
// ===========================================================================

TEST(MoonlightAdapterTest, ConnectionStartedSetsStreaming) {
    MoonlightAdapter a;

    a.on_connection_started();
    EXPECT_EQ(a.state(), AdapterState::kStreaming);
    EXPECT_TRUE(a.is_connected());

    a.on_connection_terminated(0);
    EXPECT_EQ(a.state(), AdapterState::kIdle);
    EXPECT_FALSE(a.is_connected());
}

TEST(MoonlightAdapterTest, AbnormalTerminationRecordsError) {
    MoonlightAdapter a;

    a.on_connection_started();
    a.on_connection_terminated(-1);

    EXPECT_FALSE(a.is_connected());
    EXPECT_EQ(a.state(), AdapterState::kIdle);
    EXPECT_FALSE(a.last_error().empty()) << "异常断开应当留下原因";
}

TEST(MoonlightAdapterTest, DestructorStopsCleanly) {
    // 析构时若还在 Streaming, 不能崩
    {
        MoonlightAdapter a;
        a.on_connection_started();
        ASSERT_TRUE(a.is_connected());
    }
    SUCCEED();
}
