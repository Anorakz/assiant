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
using agent::classify_video_format;
using agent::ImageRingBuffer;
using agent::MoonlightAdapter;
using agent::VideoCodec;
using agent::VideoFormatCheck;

namespace {

/// 一个几乎不可能有服务的地址, 让 start() 快速失败在握手阶段
const char* kBadHost = "127.0.0.1:9";

// moonlight 的视频格式码 (Limelight.h 的 VIDEO_FORMAT_*)。这里写字面量是因为
// host 构建不 include Limelight.h; decoder 侧的 static_assert 会在交叉编译时
// 拿真头文件核对同一组值。
constexpr int kFmtH264 = 0x0001;
constexpr int kFmtH264High8444 = 0x0004;
constexpr int kFmtH265 = 0x0100;
constexpr int kFmtH265Main10 = 0x0200;
constexpr int kFmtAv1 = 0x1000;

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
//  start_with_session: 握手由调用方 (Python/HTTPS) 做完, 这里一次 HTTP 都不发
// ===========================================================================

TEST(MoonlightAdapterTest, StartWithSessionRejectsEmptyAppVersion) {
    MoonlightAdapter a;

    EXPECT_FALSE(a.start_with_session("127.0.0.1", "Desktop", 1280, 720, 60,
                                      /*app_version=*/"", "3.23.0.74", 0x1f0301,
                                      "rtsp://127.0.0.1:48010"));
    EXPECT_EQ(a.state(), AdapterState::kIdle) << "失败后必须回到 Idle";
    EXPECT_NE(a.last_error().find("app_version"), std::string::npos)
        << "要说清缺的是哪一项: " << a.last_error();
}

TEST(MoonlightAdapterTest, StartWithSessionRejectsEmptySessionUrl) {
    MoonlightAdapter a;

    EXPECT_FALSE(a.start_with_session("127.0.0.1", "Desktop", 1280, 720, 60,
                                      "7.1.431.-1", "3.23.0.74", 0x1f0301,
                                      /*session_url=*/""));
    EXPECT_EQ(a.state(), AdapterState::kIdle);
    EXPECT_NE(a.last_error().find("session_url"), std::string::npos) << a.last_error();
}

TEST(MoonlightAdapterTest, StartWithSessionDoesNotTouchTheNetwork) {
    // 与 start() 的关键区别: 给一个必然连不上的主机也**不会**出现握手阶段的报错,
    // 因为它压根不发 HTTP —— 这就是"握手交给 Python"的全部意义。
    MoonlightAdapter a;

    EXPECT_FALSE(a.start_with_session(kBadHost, "Desktop", 1280, 720, 60,
                                      "7.1.431.-1", "3.23.0.74", 0x1f0301,
                                      "rtsp://127.0.0.1:48010"))
        << "host 构建没链接 moonlight, 必然失败";

    const std::string err = a.last_error();
    EXPECT_EQ(err.find("handshake failed"), std::string::npos)
        << "不该出现握手阶段的错误 (说明它去发 HTTP 了): " << err;
#ifndef AGENT_HAVE_MOONLIGHT
    // host 构建走的是"未链接 moonlight"那条分支; 交叉编译里会真去连, 不属单测范围
    EXPECT_NE(err.find("not linked"), std::string::npos) << err;
#endif
    EXPECT_EQ(a.state(), AdapterState::kIdle);
}

TEST(MoonlightAdapterTest, StartWithSessionValidatesArgsLikeStart) {
    // 两条入口的前置检查必须一致 (共用 prepare_start)
    MoonlightAdapter a;

    EXPECT_FALSE(a.start_with_session("", "app", 1280, 720, 60, "v", "", 0, "rtsp://x"));
    EXPECT_FALSE(a.start_with_session("h", "", 1280, 720, 60, "v", "", 0, "rtsp://x"));
    EXPECT_FALSE(a.start_with_session("h", "app", 0, 720, 60, "v", "", 0, "rtsp://x"));
    EXPECT_FALSE(a.start_with_session("h", "app", 1280, 720, 0, "v", "", 0, "rtsp://x"));
    EXPECT_EQ(a.state(), AdapterState::kIdle);
    EXPECT_FALSE(a.last_error().empty());
}

TEST(MoonlightAdapterTest, StartWithSessionCanBeRetriedAfterFailure) {
    MoonlightAdapter a;

    ASSERT_FALSE(a.start_with_session(kBadHost, "Desktop", 1280, 720, 60,
                                      "7.1.431.-1", "", 0, "rtsp://x"));
    // 失败后回到 Idle, 所以还能再来一次 (状态机与 start() 一致)
    EXPECT_FALSE(a.start_with_session(kBadHost, "Desktop", 1280, 720, 60,
                                      "7.1.431.-1", "", 0, "rtsp://x"));
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

// ===========================================================================
//  协商到的视频格式 → 编码格式
//
//  解码器什么时候初始化、用哪种编码, 全看这个映射。以前这里是写死的 H.265,
//  服务端只给 H.264 时就会拿 H.265 解码器去解 H.264 的流。
// ===========================================================================

TEST(ClassifyVideoFormat, AcceptsH264AndH265) {
    VideoCodec codec = VideoCodec::kH265;

    EXPECT_EQ(classify_video_format(kFmtH264, &codec), VideoFormatCheck::kOk);
    EXPECT_EQ(codec, VideoCodec::kH264);

    EXPECT_EQ(classify_video_format(kFmtH265, &codec), VideoFormatCheck::kOk);
    EXPECT_EQ(codec, VideoCodec::kH265);
}

TEST(ClassifyVideoFormat, RejectsTenBitBeforeLookingAtCodec) {
    // H265_MAIN10 同时落在 H.265 掩码里 —— 必须先认出 10bit, 否则会被当成
    // 能解的 H.265 收下, 然后解码器在运行期才发现格式不对。
    VideoCodec codec = VideoCodec::kH264;
    EXPECT_EQ(classify_video_format(kFmtH265Main10, &codec), VideoFormatCheck::kTenBit);
    EXPECT_EQ(codec, VideoCodec::kH264) << "被拒绝时不该动 out 参数";
}

TEST(ClassifyVideoFormat, RejectsYuv444) {
    VideoCodec codec = VideoCodec::kH265;
    EXPECT_EQ(classify_video_format(kFmtH264High8444, &codec), VideoFormatCheck::kYuv444);
}

TEST(ClassifyVideoFormat, RejectsUnknownCodecs) {
    VideoCodec codec = VideoCodec::kH265;
    EXPECT_EQ(classify_video_format(kFmtAv1, &codec), VideoFormatCheck::kUnknown);
    EXPECT_EQ(classify_video_format(0, &codec), VideoFormatCheck::kUnknown);
}

TEST(ClassifyVideoFormat, ToleratesNullOutCodec) {
    // 只想问"能不能用"时不该要求必须给出参
    EXPECT_EQ(classify_video_format(kFmtH265, nullptr), VideoFormatCheck::kOk);
}

// ===========================================================================
//  on_decoder_setup: moonlight 协商完之后走这里
// ===========================================================================

TEST(AdapterDecoderSetup, RejectsTenBitWithActionableMessage) {
    MoonlightAdapter a;

    EXPECT_FALSE(a.on_decoder_setup(kFmtH265Main10, 1280, 720));
    const std::string err = a.last_error();
    EXPECT_NE(err.find("10bit"), std::string::npos) << "实际: " << err;
    EXPECT_NE(err.find("Sunshine"), std::string::npos)
        << "要说清去哪儿改, 不是只报错。实际: " << err;
}

TEST(AdapterDecoderSetup, RejectsYuv444) {
    MoonlightAdapter a;

    EXPECT_FALSE(a.on_decoder_setup(kFmtH264High8444, 1280, 720));
    EXPECT_NE(a.last_error().find("4:4:4"), std::string::npos) << a.last_error();
}

TEST(AdapterDecoderSetup, RejectsUnknownFormat) {
    MoonlightAdapter a;

    EXPECT_FALSE(a.on_decoder_setup(kFmtAv1, 1280, 720));
    EXPECT_NE(a.last_error().find("H.264"), std::string::npos) << a.last_error();
}

TEST(AdapterDecoderSetup, FailedDecodeInitSurfacesTheReason) {
    MoonlightAdapter a;

    // host 上 MPP/FFmpeg 都没编进来, 所以这里必然失败 —— 但关键是不能只给
    // "decoder init failed", 必须带上后面那串原因。
    EXPECT_FALSE(a.on_decoder_setup(kFmtH265, 1280, 720));
    const std::string err = a.last_error();
    EXPECT_NE(err.find("decoder init failed"), std::string::npos) << "实际: " << err;
    EXPECT_GT(err.size(), std::string("decoder init failed: ").size() + 10)
        << "失败原因不能是空的。实际: " << err;
}
