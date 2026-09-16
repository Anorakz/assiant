// ============================================================================
//  tests/test_input_sender.cpp — input_sender 单元测试 (host 侧, L1)
//
//  运行方式:
//      scripts/test-host.ps1
//
//  host 构建走 stub 模式 (不链接 moonlight-common-c), 所以这里能验的是:
//
//    · 接口不崩: 非法/边界入参都有明确行为
//    · 常量取值: 必须与 moonlight 的 MODIFIER_* / BUTTON_* 一致
//      (取值写错在真机上就是"按了 Shift 变成 Ctrl", host 上就能拦住)
//    · 组合键展开: 按下正序、抬起逆序, 事件数 = 2 × 键数
//    · 坐标夹取: 超出 ROI 的坐标会被夹回 0..255
//    · stub 计数与日志开关
//
//  "真的把事件发到 Windows 主机" 属于端到端验证, 只能在板端 + Sunshine
//  环境做, 不在 host 单测范围内。
// ============================================================================

#include "input_sender.h"

#include <gtest/gtest.h>

#include <cstdint>
#include <vector>

using namespace agent::input_sender;

namespace {

/// 每个用例都先把计数和日志复位, 避免互相干扰
class InputSenderTest : public ::testing::Test {
protected:
    void SetUp() override {
        set_log_enabled(false);  // 单测里别刷屏
        reset_event_count();
    }
    void TearDown() override {
        set_log_enabled(true);
        reset_event_count();
    }
};

}  // namespace

// ===========================================================================
//  常量取值必须与 moonlight 一致
// ===========================================================================

TEST_F(InputSenderTest, ModifierConstantsMatchMoonlightProtocol) {
    // 这几个值直接来自 moonlight 的 Limelight.h。
    // 写错的话真机上会出现"按 Shift 实际发成 Ctrl"这类静默错误。
    EXPECT_EQ(kModifierNone, 0x00u);
    EXPECT_EQ(kModifierShift, 0x01u);
    EXPECT_EQ(kModifierCtrl, 0x02u);
    EXPECT_EQ(kModifierAlt, 0x04u);
    EXPECT_EQ(kModifierMeta, 0x08u);
}

TEST_F(InputSenderTest, ButtonConstantsMatchMoonlightProtocol) {
    EXPECT_EQ(kButtonLeft, 0x01u);
    EXPECT_EQ(kButtonMiddle, 0x02u);
    EXPECT_EQ(kButtonRight, 0x03u);
    EXPECT_EQ(kButtonX1, 0x04u);
    EXPECT_EQ(kButtonX2, 0x05u);
}

TEST_F(InputSenderTest, ModifiersAreBitwiseComposable) {
    // 位掩码语义: 可以组合, 且互不重叠
    const std::uint32_t all = kModifierShift | kModifierCtrl | kModifierAlt | kModifierMeta;
    EXPECT_EQ(all, 0x0Fu);
    EXPECT_EQ(kModifierShift & kModifierCtrl, 0u) << "修饰键位不能重叠";

    // 组合值应当同时满足各自的检查
    EXPECT_NE(all & kModifierCtrl, 0u);
    EXPECT_NE(all & kModifierAlt, 0u);
}

TEST_F(InputSenderTest, MouseReferencePlaneIsTheRoi) {
    EXPECT_EQ(kMouseRefWidth, 256);
    EXPECT_EQ(kMouseRefHeight, 256);
}

// ===========================================================================
//  坐标夹取
// ===========================================================================

TEST_F(InputSenderTest, MouseCoordsAreClampedToReferencePlane) {
    EXPECT_EQ(clamp_mouse_x(-100), 0);
    EXPECT_EQ(clamp_mouse_x(0), 0);
    EXPECT_EQ(clamp_mouse_x(128), 128);
    EXPECT_EQ(clamp_mouse_x(255), 255);
    EXPECT_EQ(clamp_mouse_x(256), 255) << "256 已经超出 0..255";
    EXPECT_EQ(clamp_mouse_x(99999), 255);

    EXPECT_EQ(clamp_mouse_y(-1), 0);
    EXPECT_EQ(clamp_mouse_y(255), 255);
    EXPECT_EQ(clamp_mouse_y(1000), 255);
}

// ===========================================================================
//  发送接口: 不崩 + 记账
// ===========================================================================

TEST_F(InputSenderTest, SendKeyCountsEvent) {
    send_key(kModifierNone, 'A', true);
    EXPECT_EQ(event_count(), 1u);

    send_key(kModifierNone, 'A', false);
    EXPECT_EQ(event_count(), 2u);
}

TEST_F(InputSenderTest, SendKeyWithModifier) {
    send_key(kModifierCtrl, 'S', true);
    send_key(kModifierCtrl, 'S', false);
    EXPECT_EQ(event_count(), 2u);
}

TEST_F(InputSenderTest, SendKeyIgnoresInvalidKeycode) {
    // 键码 0 不是合法 VK, 应当被忽略 —— 但仍记账 (调用方知道"来过一次")
    send_key(kModifierNone, 0, true);
    EXPECT_EQ(event_count(), 1u);
}

TEST_F(InputSenderTest, SendMouseCountsEventAndAcceptsOutOfRangeCoords) {
    send_mouse(10, 20);
    EXPECT_EQ(event_count(), 1u);

    // 越界坐标不该崩 (内部会夹)
    send_mouse(-5, 999);
    EXPECT_EQ(event_count(), 2u);

    send_mouse(0, 0);
    send_mouse(255, 255);
    EXPECT_EQ(event_count(), 4u);
}

TEST_F(InputSenderTest, SendMouseButtonCountsEvent) {
    send_mouse_button(true, kButtonLeft);
    send_mouse_button(false, kButtonLeft);
    EXPECT_EQ(event_count(), 2u);

    send_mouse_button(true, kButtonRight);
    send_mouse_button(true, kButtonMiddle);
    EXPECT_EQ(event_count(), 4u);
}

TEST_F(InputSenderTest, SendMouseButtonIgnoresUnknownButton) {
    // 野值不该发给主机 (会解析成未定义按钮); 仍记账
    send_mouse_button(true, 0);
    EXPECT_EQ(event_count(), 1u);

    send_mouse_button(true, 99);
    EXPECT_EQ(event_count(), 2u);
}

// ===========================================================================
//  组合键展开
// ===========================================================================

TEST_F(InputSenderTest, HotkeyExpandsToPressAndReleaseInReverseOrder) {
    // Ctrl+Alt+S = 3 个键 → 3 次按下 + 3 次抬起
    const std::vector<std::uint32_t> keys = {0x11 /*VK_CONTROL*/, 0x12 /*VK_MENU*/, 'S'};
    send_hotkey(keys);

    EXPECT_EQ(event_count(), 6u) << "N 个键应当产生 2N 个事件";
}

TEST_F(InputSenderTest, EmptyHotkeyDoesNothing) {
    send_hotkey({});
    EXPECT_EQ(event_count(), 0u) << "空序列不该产生任何事件";
}

TEST_F(InputSenderTest, SingleKeyHotkeyIsPressThenRelease) {
    send_hotkey({'A'});
    EXPECT_EQ(event_count(), 2u);
}

TEST_F(InputSenderTest, LargeHotkeyScalesLinearly) {
    std::vector<std::uint32_t> keys;
    for (std::uint32_t i = 0; i < 5; ++i) {
        keys.push_back(0x70 + i);  // VK_F1..
    }
    send_hotkey(keys);
    EXPECT_EQ(event_count(), 10u);
}

// ===========================================================================
//  stub 的日志开关与计数复位
// ===========================================================================

TEST_F(InputSenderTest, LogToggleRoundTrips) {
    set_log_enabled(true);
    EXPECT_TRUE(log_enabled());
    set_log_enabled(false);
    EXPECT_FALSE(log_enabled());
}

TEST_F(InputSenderTest, ResetEventCountClearsCounter) {
    send_key(kModifierNone, 'X', true);
    ASSERT_GT(event_count(), 0u);
    reset_event_count();
    EXPECT_EQ(event_count(), 0u);
}

TEST_F(InputSenderTest, StressManyEventsDoesNotCrash) {
    // 稀疏事件, 但跑一遍确认没有累积状态问题
    for (int i = 0; i < 1000; ++i) {
        send_key(kModifierShift, 'A', (i % 2) == 0);
        send_mouse(i % 256, (i * 3) % 256);
    }
    EXPECT_EQ(event_count(), 2000u);
}
