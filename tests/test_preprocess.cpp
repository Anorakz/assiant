// ============================================================================
//  tests/test_preprocess.cpp — YUV420P → 256×256 RGB888 单元测试 (host 侧, L1)
//
//  运行方式:
//      scripts/test-host.ps1
//
//  覆盖范围
//  ---------------------------------------------------------------------------
//  纯色帧 / 四象限图案 (验证最近邻采样) / ROI 裁剪 / 放大 / 边界与非法入参
//
//  期望值怎么来的
//  ---------------------------------------------------------------------------
//  全部由 BT.601 limited range 公式手工推导 (见 preprocess.h), 系数是
//  ×1024 定点加四舍五入。用到的三个约定值:
//
//      红   (Y=82,  U=90,  V=240) → (255,   0,   0)
//      绿   (Y=145, U=54,  V=34)  → (  0, 255,   0)
//      蓝   (Y=41,  U=240, V=110) → (  0,   0, 255)
//
//  ⚠ 定点实现与浮点参考值在通道边界上可能差 1 (例如红色的 G 浮点算出 -0.05,
//    四舍五入后 0 或 1 都算合理), 所以边界用例留 tol=1。
//    这是纯舍入差异, 不是公式错 —— RoundTrip 那个用例把误差锁定在 ±1 内。
// ============================================================================

#include "preprocess.h"

#include <gtest/gtest.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <vector>

using agent::clamp_roi;
using agent::kRoiOutBytes;
using agent::kRoiOutSize;
using agent::preprocess_frame;
using agent::Roi;
using agent::roi_is_valid;

namespace {

double clamp01(double v) {
    if (v < 0.0) {
        return 0.0;
    }
    if (v > 255.0) {
        return 255.0;
    }
    return v;
}

/// 用 YUV 反推 RGB (BT.601 limited range, 浮点)
/// 只用于构造测试期望值, 正式实现走定点
std::uint8_t yuv_to_r(int Y, int U, int V) {
    (void)U;  // R 通道不需要 U
    const double y = 1.164 * (Y - 16);
    const double v = V - 128;
    return static_cast<std::uint8_t>(std::lround(clamp01(y + 1.596 * v)));
}
std::uint8_t yuv_to_g(int Y, int U, int V) {
    const double y = 1.164 * (Y - 16);
    const double u = U - 128;
    const double v = V - 128;
    return static_cast<std::uint8_t>(std::lround(clamp01(y - 0.391 * u - 0.813 * v)));
}
std::uint8_t yuv_to_b(int Y, int U, int V) {
    (void)V;  // B 通道不需要 V
    const double y = 1.164 * (Y - 16);
    const double u = U - 128;
    return static_cast<std::uint8_t>(std::lround(clamp01(y + 2.018 * u)));
}

/// 构造一整帧 YUV420P: 每个像素用 pixel_fn(px, py) -> (Y,U,V)
template <typename Fn>
std::vector<std::uint8_t> make_yuv420(int w, int h, Fn pixel_fn) {
    const int cw = w / 2;
    const int ch = h / 2;
    std::vector<std::uint8_t> buf(
        static_cast<std::size_t>(w) * h + static_cast<std::size_t>(cw) * ch * 2, 0);

    std::uint8_t* Y = buf.data();
    std::uint8_t* U = Y + static_cast<std::size_t>(w) * h;
    std::uint8_t* V = U + static_cast<std::size_t>(cw) * ch;

    for (int py = 0; py < h; ++py) {
        for (int px = 0; px < w; ++px) {
            int y = 0, u = 0, v = 0;
            pixel_fn(px, py, y, u, v);
            Y[static_cast<std::size_t>(py) * w + px] = static_cast<std::uint8_t>(y);
        }
    }
    for (int cy = 0; cy < ch; ++cy) {
        for (int cx = 0; cx < cw; ++cx) {
            int y = 0, u = 0, v = 0;
            pixel_fn(cx * 2, cy * 2, y, u, v);  // 色度取该 2×2 块左上角
            U[static_cast<std::size_t>(cy) * cw + cx] = static_cast<std::uint8_t>(u);
            V[static_cast<std::size_t>(cy) * cw + cx] = static_cast<std::uint8_t>(v);
        }
    }
    return buf;
}

/// 输出缓冲
using RgbBuf = std::vector<std::uint8_t>;

/// 取出输出像素
void get_px(const RgbBuf& buf, int x, int y, int& r, int& g, int& b) {
    const std::size_t i = (static_cast<std::size_t>(y) * kRoiOutSize + x) * 3;
    r = buf[i + 0];
    g = buf[i + 1];
    b = buf[i + 2];
}

/// 全量检查输出 256×256 是否都等于期望三元组
/// @param tol 容差: 定点(×1024 四舍五入) 与浮点参考值可能差 1
void expect_uniform(const RgbBuf& buf,
                    int er,
                    int eg,
                    int eb,
                    const char* ctx = "",
                    int tol = 0) {
    for (int y = 0; y < kRoiOutSize; ++y) {
        for (int x = 0; x < kRoiOutSize; ++x) {
            const std::size_t i = (static_cast<std::size_t>(y) * kRoiOutSize + x) * 3;
            ASSERT_NEAR(buf[i + 0], er, tol) << ctx << " 像素 (" << x << "," << y << ") R";
            ASSERT_NEAR(buf[i + 1], eg, tol) << ctx << " 像素 (" << x << "," << y << ") G";
            ASSERT_NEAR(buf[i + 2], eb, tol) << ctx << " 像素 (" << x << "," << y << ") B";
        }
    }
}

/// 检查输出某个矩形区域是否都是期望三元组
void expect_rect(const RgbBuf& buf,
                 int x0,
                 int y0,
                 int x1,
                 int y1,
                 int er,
                 int eg,
                 int eb,
                 int tol = 0) {
    for (int y = y0; y < y1; ++y) {
        for (int x = x0; x < x1; ++x) {
            const std::size_t i = (static_cast<std::size_t>(y) * kRoiOutSize + x) * 3;
            ASSERT_NEAR(buf[i + 0], er, tol) << "像素 (" << x << "," << y << ") R";
            ASSERT_NEAR(buf[i + 1], eg, tol) << "像素 (" << x << "," << y << ") G";
            ASSERT_NEAR(buf[i + 2], eb, tol) << "像素 (" << x << "," << y << ") B";
        }
    }
}

/// 常用颜色 (YUV 三元组)
struct Yuv {
    int y, u, v;
};
constexpr Yuv kRed{82, 90, 240};    // → (255,   0,   0)
constexpr Yuv kGreen{145, 54, 34};  // → (  0, 255,   0)
constexpr Yuv kBlue{41, 240, 110};  // → (  0,   0, 255)
constexpr Yuv kGray{128, 128, 128};

/// 造一个"四象限"源帧: 左上红 右上绿 左下蓝 右下灰
/// 每个象限至少 2×2 像素 —— YUV420P 的色度是 2×2 下采样, 一帧只有
/// (w/2)×(h/2) 个色度样本, 所以源尺寸 < 4×4 时**根本表达不出**四种颜色
/// (2×2 全帧只有 1 个色度样本, 整帧必然同色)。
std::vector<std::uint8_t> make_quadrants(int w, int h) {
    return make_yuv420(w, h, [w, h](int px, int py, int& Y, int& U, int& V) {
        const bool right = (px >= w / 2);
        const bool bottom = (py >= h / 2);
        Yuv c = kGray;
        if (!right && !bottom) c = kRed;
        if (right && !bottom) c = kGreen;
        if (!right && bottom) c = kBlue;
        Y = c.y;
        U = c.u;
        V = c.v;
    });
}

}  // namespace

// ===========================================================================
//  纯色帧
// ===========================================================================

TEST(Preprocess, SolidFrameProducesUniformOutput) {
    const int w = 64, h = 64;
    auto yuv = make_yuv420(w, h, [](int, int, int& Y, int& U, int& V) {
        Y = kGray.y;
        U = kGray.u;
        V = kGray.v;
    });

    RgbBuf out(kRoiOutBytes);
    preprocess_frame(yuv.data(), w, h, Roi{0, 0, w, h}, out.data());

    expect_uniform(out, yuv_to_r(kGray.y, kGray.u, kGray.v),
                   yuv_to_g(kGray.y, kGray.u, kGray.v),
                   yuv_to_b(kGray.y, kGray.u, kGray.v));
}

TEST(Preprocess, GrayIsNeutralRgb) {
    // Y=128 U=V=128 应当得到 R=G=B (无偏色)
    const int w = 8, h = 8;
    auto yuv = make_yuv420(w, h, [](int, int, int& Y, int& U, int& V) {
        Y = 128;
        U = 128;
        V = 128;
    });

    RgbBuf out(kRoiOutBytes);
    preprocess_frame(yuv.data(), w, h, Roi{0, 0, w, h}, out.data());

    int r = 0, g = 0, b = 0;
    get_px(out, 0, 0, r, g, b);
    EXPECT_EQ(r, g) << "灰色不该有色调偏移";
    EXPECT_EQ(g, b);
    EXPECT_GT(r, 0) << "Y=128 不该算出全黑";
}

TEST(Preprocess, RedFrameProducesRedOutput) {
    const int w = 16, h = 16;
    auto yuv = make_yuv420(w, h, [](int, int, int& Y, int& U, int& V) {
        Y = kRed.y;
        U = kRed.u;
        V = kRed.v;
    });

    RgbBuf out(kRoiOutBytes);
    preprocess_frame(yuv.data(), w, h, Roi{0, 0, w, h}, out.data());

    expect_uniform(out, 255, 0, 0, "红色帧", 1);
}

TEST(Preprocess, GreenFrameProducesGreenOutput) {
    const int w = 16, h = 16;
    auto yuv = make_yuv420(w, h, [](int, int, int& Y, int& U, int& V) {
        Y = kGreen.y;
        U = kGreen.u;
        V = kGreen.v;
    });

    RgbBuf out(kRoiOutBytes);
    preprocess_frame(yuv.data(), w, h, Roi{0, 0, w, h}, out.data());

    expect_uniform(out, 0, 255, 0, "绿色帧", 1);
}

TEST(Preprocess, BlueFrameProducesBlueOutput) {
    const int w = 16, h = 16;
    auto yuv = make_yuv420(w, h, [](int, int, int& Y, int& U, int& V) {
        Y = kBlue.y;
        U = kBlue.u;
        V = kBlue.v;
    });

    RgbBuf out(kRoiOutBytes);
    preprocess_frame(yuv.data(), w, h, Roi{0, 0, w, h}, out.data());

    expect_uniform(out, 0, 0, 255, "蓝色帧", 1);
}

TEST(Preprocess, OutputChannelOrderIsRgb) {
    // 红色帧的 R 通道必须比 B 通道大 —— 防止把 RGB / BGR 写反
    const int w = 8, h = 8;
    auto yuv = make_yuv420(w, h, [](int, int, int& Y, int& U, int& V) {
        Y = kRed.y;
        U = kRed.u;
        V = kRed.v;
    });

    RgbBuf out(kRoiOutBytes);
    preprocess_frame(yuv.data(), w, h, Roi{0, 0, w, h}, out.data());

    int r = 0, g = 0, b = 0;
    get_px(out, 10, 10, r, g, b);
    EXPECT_GT(r, b) << "红色帧的 R 应远大于 B, 否则通道顺序反了";
}

// ===========================================================================
//  四象限图案 — 验证最近邻采样的位置映射
// ===========================================================================

TEST(Preprocess, QuadrantPatternMapsToCorrectQuadrants) {
    // 4×4 源图: 色度平面 2×2, 正好每个象限一个色度样本
    const int w = 4, h = 4;
    auto yuv = make_quadrants(w, h);

    RgbBuf out(kRoiOutBytes);
    preprocess_frame(yuv.data(), w, h, Roi{0, 0, w, h}, out.data());

    const int half = kRoiOutSize / 2;                            // 128
    expect_rect(out, 0, 0, half, half, 255, 0, 0, 1);            // 左上 红
    expect_rect(out, half, 0, kRoiOutSize, half, 0, 255, 0, 1);  // 右上 绿
    expect_rect(out, 0, half, half, kRoiOutSize, 0, 0, 255, 1);  // 左下 蓝

    const int gray = yuv_to_r(kGray.y, kGray.u, kGray.v);
    expect_rect(out, half, half, kRoiOutSize, kRoiOutSize, gray, gray, gray);
}

TEST(Preprocess, DownscaleFromLargeFrameKeepsQuadrants) {
    // 512×512 源图, 四个 256×256 象限; 缩到 256×256 后每个输出象限应当
    // 仍是纯色 (最近邻取样落点不对的话这题会立刻暴露)
    const int w = 512, h = 512;
    auto yuv = make_quadrants(w, h);

    RgbBuf out(kRoiOutBytes);
    preprocess_frame(yuv.data(), w, h, Roi{0, 0, w, h}, out.data());

    const int half = kRoiOutSize / 2;
    expect_rect(out, 0, 0, half, half, 255, 0, 0, 1);
    expect_rect(out, half, 0, kRoiOutSize, half, 0, 255, 0, 1);
    expect_rect(out, 0, half, half, kRoiOutSize, 0, 0, 255, 1);
}

TEST(Preprocess, ZoomInReplicatesPixels) {
    // 放大: 4×4 图放大到 256×256, 每个源像素占 64×64
    // (源不能小于 4×4, 否则色度不足以表达四色 —— 见 make_quadrants 注释)
    const int w = 4, h = 4;
    auto yuv = make_quadrants(w, h);

    RgbBuf out(kRoiOutBytes);
    preprocess_frame(yuv.data(), w, h, Roi{0, 0, w, h}, out.data());

    // 中线在 128: 127 属于左上象限 (红), 128 切到右上 (绿) / 左下 (蓝)
    int r = 0, g = 0, b = 0;
    get_px(out, 127, 127, r, g, b);
    EXPECT_NEAR(r, 255, 1) << "左上象限最后一个像素仍应是红";
    get_px(out, 128, 127, r, g, b);
    EXPECT_NEAR(g, 255, 1) << "跨过中线应切到绿";
    get_px(out, 127, 128, r, g, b);
    EXPECT_NEAR(b, 255, 1) << "跨过中线应切到蓝";
}

// ===========================================================================
//  ROI 裁剪
// ===========================================================================

TEST(Preprocess, RoiSelectsSubRegion) {
    // 4×4 图: 左半红, 右半蓝. 取右半 ROI → 输出应全蓝
    const int w = 4, h = 4;
    auto yuv = make_yuv420(w, h, [](int px, int, int& Y, int& U, int& V) {
        const Yuv c = (px < 2) ? kRed : kBlue;
        Y = c.y;
        U = c.u;
        V = c.v;
    });

    RgbBuf out(kRoiOutBytes);
    preprocess_frame(yuv.data(), w, h, Roi{2, 0, 2, 4}, out.data());

    expect_uniform(out, 0, 0, 255, "右半 ROI", 1);
}

TEST(Preprocess, RoiLeftHalfSelectsRed) {
    const int w = 4, h = 4;
    auto yuv = make_yuv420(w, h, [](int px, int, int& Y, int& U, int& V) {
        const Yuv c = (px < 2) ? kRed : kBlue;
        Y = c.y;
        U = c.u;
        V = c.v;
    });

    RgbBuf out(kRoiOutBytes);
    preprocess_frame(yuv.data(), w, h, Roi{0, 0, 2, 4}, out.data());

    expect_uniform(out, 255, 0, 0, "左半 ROI", 1);
}

TEST(Preprocess, RoiOffsetIsRespected) {
    // 8×8 图: 上半绿下半蓝. 取下半 → 全蓝
    const int w = 8, h = 8;
    auto yuv = make_yuv420(w, h, [](int, int py, int& Y, int& U, int& V) {
        const Yuv c = (py < 4) ? kGreen : kBlue;
        Y = c.y;
        U = c.u;
        V = c.v;
    });

    RgbBuf out(kRoiOutBytes);
    preprocess_frame(yuv.data(), w, h, Roi{0, 4, 8, 4}, out.data());

    expect_uniform(out, 0, 0, 255, "下半 ROI", 1);
}

TEST(Preprocess, SinglePixelRoiFillsOutput) {
    // 目标是 (3,3). 注意 YUV420P 的色度按 2×2 块共享, 所以要让 (3,3) 真的是
    // 绿色, 整个 2×2 块 {(2,2),(2,3),(3,2),(3,3)} 都必须是绿 —— 只改一个像素
    // 的话色度仍取自块内左上角, 颜色不会是绿的。
    const int w = 4, h = 4;
    auto yuv = make_yuv420(w, h, [](int px, int py, int& Y, int& U, int& V) {
        const bool green_block = (px >= 2 && py >= 2);
        const Yuv c = green_block ? kGreen : kRed;
        Y = c.y;
        U = c.u;
        V = c.v;
    });

    RgbBuf out(kRoiOutBytes);
    preprocess_frame(yuv.data(), w, h, Roi{3, 3, 1, 1}, out.data());

    expect_uniform(out, 0, 255, 0, "1×1 ROI 应铺满输出", 1);
}

// ===========================================================================
//  边界: ROI 超出图像范围
// ===========================================================================

TEST(Preprocess, RoiLargerThanImageIsClampedToImage) {
    // 4×4 全绿图, ROI 给 (-100,-100,1000,1000) → 夹成整图 → 全绿
    const int w = 4, h = 4;
    auto yuv = make_yuv420(w, h, [](int, int, int& Y, int& U, int& V) {
        Y = kGreen.y;
        U = kGreen.u;
        V = kGreen.v;
    });

    RgbBuf out(kRoiOutBytes);
    preprocess_frame(yuv.data(), w, h, Roi{-100, -100, 1000, 1000}, out.data());

    expect_uniform(out, 0, 255, 0, "超大 ROI 应夹成整图", 1);
}

TEST(Preprocess, RoiPartiallyOutsideTopLeftIsClamped) {
    // 左半红右半蓝; ROI 从 x=-2 开始覆盖 x∈[-2,2) → 夹成 [0,2) → 全红
    const int w = 4, h = 4;
    auto yuv = make_yuv420(w, h, [](int px, int, int& Y, int& U, int& V) {
        const Yuv c = (px < 2) ? kRed : kBlue;
        Y = c.y;
        U = c.u;
        V = c.v;
    });

    RgbBuf out(kRoiOutBytes);
    preprocess_frame(yuv.data(), w, h, Roi{-2, 0, 4, 4}, out.data());

    expect_uniform(out, 255, 0, 0, "左上越界应夹取", 1);
}

TEST(Preprocess, RoiPartiallyOutsideBottomRightIsClamped) {
    // 左半红右半蓝; ROI 覆盖 x∈[2,8) → 夹成 [2,4) → 全蓝
    const int w = 4, h = 4;
    auto yuv = make_yuv420(w, h, [](int px, int, int& Y, int& U, int& V) {
        const Yuv c = (px < 2) ? kRed : kBlue;
        Y = c.y;
        U = c.u;
        V = c.v;
    });

    RgbBuf out(kRoiOutBytes);
    preprocess_frame(yuv.data(), w, h, Roi{2, 0, 6, 4}, out.data());

    expect_uniform(out, 0, 0, 255, "右下越界应夹取", 1);
}

TEST(Preprocess, RoiCompletelyOutsideFillsBlack) {
    const int w = 4, h = 4;
    auto yuv = make_yuv420(w, h, [](int, int, int& Y, int& U, int& V) {
        Y = kGreen.y;
        U = kGreen.u;
        V = kGreen.v;
    });

    RgbBuf out(kRoiOutBytes, 0xAB);  // 先填非零, 验证真的被写黑
    preprocess_frame(yuv.data(), w, h, Roi{100, 100, 10, 10}, out.data());
    expect_uniform(out, 0, 0, 0, "图像外 ROI 应全黑");

    std::fill(out.begin(), out.end(), 0xAB);
    preprocess_frame(yuv.data(), w, h, Roi{-50, 0, 10, 4}, out.data());
    expect_uniform(out, 0, 0, 0, "左边界外 ROI 应判为空");
}

TEST(Preprocess, ZeroOrNegativeRoiSizeFillsBlack) {
    const int w = 4, h = 4;
    auto yuv = make_yuv420(w, h, [](int, int, int& Y, int& U, int& V) {
        Y = kRed.y;
        U = kRed.u;
        V = kRed.v;
    });

    const Roi cases[] = {Roi{0, 0, 0, 4}, Roi{0, 0, 4, 0}, Roi{0, 0, -3, 4},
                         Roi{0, 0, 4, -3}};
    for (const Roi& r : cases) {
        RgbBuf out(kRoiOutBytes, 0xCD);
        preprocess_frame(yuv.data(), w, h, r, out.data());
        expect_uniform(out, 0, 0, 0, "ROI 尺寸非正应输出全黑");
    }
}

TEST(Preprocess, RoiTouchingEdgeExactly) {
    // ROI 正好贴右下角边界, 不该越界也不该判空
    const int w = 8, h = 8;
    auto yuv = make_yuv420(w, h, [](int px, int py, int& Y, int& U, int& V) {
        const bool corner = (px >= 6 && py >= 6);
        const Yuv c = corner ? kGreen : kRed;
        Y = c.y;
        U = c.u;
        V = c.v;
    });

    RgbBuf out(kRoiOutBytes);
    preprocess_frame(yuv.data(), w, h, Roi{6, 6, 2, 2}, out.data());

    expect_uniform(out, 0, 255, 0, "贴边 ROI 应正常", 1);
}

TEST(Preprocess, NullPointersAreIgnored) {
    const int w = 4, h = 4;
    auto yuv = make_yuv420(w, h, [](int, int, int& Y, int& U, int& V) {
        Y = kGreen.y;
        U = kGreen.u;
        V = kGreen.v;
    });
    RgbBuf out(kRoiOutBytes, 0x5A);

    // 空输入: 不该崩, 也不该写输出
    preprocess_frame(nullptr, w, h, Roi{0, 0, w, h}, out.data());
    expect_uniform(out, 0x5A, 0x5A, 0x5A, "空输入不该写输出");

    // 空输出: 不该崩
    preprocess_frame(yuv.data(), w, h, Roi{0, 0, w, h}, nullptr);
}

TEST(Preprocess, NonPositiveDimensionsAreIgnored) {
    const int w = 4, h = 4;
    auto yuv = make_yuv420(w, h, [](int, int, int& Y, int& U, int& V) {
        Y = kGreen.y;
        U = kGreen.u;
        V = kGreen.v;
    });
    RgbBuf out(kRoiOutBytes, 0x3C);

    preprocess_frame(yuv.data(), 0, h, Roi{0, 0, w, h}, out.data());
    preprocess_frame(yuv.data(), w, 0, Roi{0, 0, w, h}, out.data());
    preprocess_frame(yuv.data(), -4, -4, Roi{0, 0, w, h}, out.data());
    expect_uniform(out, 0x3C, 0x3C, 0x3C, "非法尺寸不该写输出");
}

// ===========================================================================
//  clamp_roi / roi_is_valid
// ===========================================================================

TEST(ClampRoi, PassesThroughWhenInsideImage) {
    const Roi c = clamp_roi(Roi{2, 3, 10, 20}, 100, 100);
    EXPECT_EQ(c.x, 2);
    EXPECT_EQ(c.y, 3);
    EXPECT_EQ(c.w, 10);
    EXPECT_EQ(c.h, 20);
}

TEST(ClampRoi, ClampsAllFourSides) {
    const Roi c = clamp_roi(Roi{-5, -7, 20, 30}, 10, 10);
    EXPECT_EQ(c.x, 0);
    EXPECT_EQ(c.y, 0);
    EXPECT_EQ(c.w, 10) << "右边界应收在图像宽度";
    EXPECT_EQ(c.h, 10);
}

TEST(ClampRoi, EmptyWhenCompletelyOutside) {
    EXPECT_EQ(clamp_roi(Roi{50, 0, 5, 5}, 10, 10).w, 0);
    EXPECT_EQ(clamp_roi(Roi{0, 50, 5, 5}, 10, 10).h, 0);
    EXPECT_EQ(clamp_roi(Roi{-20, -20, 5, 5}, 10, 10).w, 0);
    EXPECT_EQ(clamp_roi(Roi{0, 0, 0, 0}, 10, 10).w, 0);
    EXPECT_EQ(clamp_roi(Roi{0, 0, 5, 5}, 0, 0).w, 0);
}

TEST(ClampRoi, RejectsCompletelyOutsideHugeRoi) {
    // 若 x + w 用 int 相加会溢出成负数, 会被误判成"与图像有交集"。
    // 这里断言它确实是空 ROI (实现里用 64 位算右/下边界)。
    const Roi huge{1000000, 1000000, 2000000000, 2000000000};
    const Roi c = clamp_roi(huge, 640, 480);
    EXPECT_EQ(c.w, 0);
    EXPECT_EQ(c.h, 0);
    EXPECT_FALSE(roi_is_valid(huge, 640, 480));
}

TEST(ClampRoi, HugeRoiStartingInsideImageClampsToImageEdge) {
    // 起点在图像内、宽度大到会溢出: 结果应当是"从起点到右边界"
    const Roi huge{600, 400, 2000000000, 2000000000};
    const Roi c = clamp_roi(huge, 640, 480);
    EXPECT_EQ(c.x, 600);
    EXPECT_EQ(c.y, 400);
    EXPECT_EQ(c.w, 40) << "600..640";
    EXPECT_EQ(c.h, 80) << "400..480";
    EXPECT_TRUE(roi_is_valid(huge, 640, 480));
}

TEST(RoiIsValid, MatchesExpectations) {
    EXPECT_TRUE(roi_is_valid(Roi{0, 0, 10, 10}, 100, 100));
    EXPECT_TRUE(roi_is_valid(Roi{-5, -5, 20, 20}, 100, 100));
    EXPECT_TRUE(roi_is_valid(Roi{95, 95, 100, 100}, 100, 100));
    EXPECT_FALSE(roi_is_valid(Roi{100, 0, 10, 10}, 100, 100));
    EXPECT_FALSE(roi_is_valid(Roi{0, 0, 0, 10}, 100, 100));
    EXPECT_FALSE(roi_is_valid(Roi{0, 0, 10, 10}, 0, 100));
}

// ===========================================================================
//  往返一致性 (任意 RGB 颜色)
// ===========================================================================

TEST(Preprocess, RoundTripMatchesFloatingPointReference) {
    // 取若干组 YUV, 期望值用浮点公式算, 验证定点实现只有舍入级差异
    const struct {
        int y, u, v;
    } cases[] = {
        {16, 128, 128},  {235, 128, 128}, {128, 128, 128}, {100, 200, 50},
        {200, 50, 200},  {50, 100, 150},  {145, 54, 34},   {82, 90, 240},
        {41, 240, 110},  {180, 160, 60},
    };

    for (const auto& c : cases) {
        const int w = 4, h = 4;
        auto yuv = make_yuv420(w, h, [&](int, int, int& Y, int& U, int& V) {
            Y = c.y;
            U = c.u;
            V = c.v;
        });

        RgbBuf out(kRoiOutBytes);
        preprocess_frame(yuv.data(), w, h, Roi{0, 0, w, h}, out.data());

        const int er = yuv_to_r(c.y, c.u, c.v);
        const int eg = yuv_to_g(c.y, c.u, c.v);
        const int eb = yuv_to_b(c.y, c.u, c.v);

        int r = 0, g = 0, b = 0;
        get_px(out, 0, 0, r, g, b);
        EXPECT_NEAR(r, er, 1) << "YUV(" << c.y << "," << c.u << "," << c.v << ") R";
        EXPECT_NEAR(g, eg, 1) << "YUV(" << c.y << "," << c.u << "," << c.v << ") G";
        EXPECT_NEAR(b, eb, 1) << "YUV(" << c.y << "," << c.u << "," << c.v << ") B";
    }
}

TEST(Preprocess, ChromaIsTakenFromCorrectSubsampledBlock) {
    // 4×2 图: 左 2×2 块 U=240(偏蓝), 右 2×2 块 U=90(偏红), Y 恒为灰度
    // 若色度索引算错 (用了 sx 而不是 sx/2 之类), 输出左右会串色
    const int w = 4, h = 2;
    std::vector<std::uint8_t> buf(
        static_cast<std::size_t>(w) * h + (w / 2) * (h / 2) * 2, 0);
    std::uint8_t* Y = buf.data();
    std::uint8_t* U = Y + static_cast<std::size_t>(w) * h;
    std::uint8_t* V = U + (w / 2) * (h / 2);

    for (int i = 0; i < w * h; ++i) {
        Y[i] = 128;
    }
    U[0] = 240;  // 左块
    U[1] = 90;   // 右块
    V[0] = 110;
    V[1] = 240;

    RgbBuf out(kRoiOutBytes);
    preprocess_frame(buf.data(), w, h, Roi{0, 0, w, h}, out.data());

    int lr = 0, lg = 0, lb = 0;
    int rr = 0, rg = 0, rb = 0;
    get_px(out, 0, 0, lr, lg, lb);                // 左半
    get_px(out, kRoiOutSize - 1, 0, rr, rg, rb);  // 右半

    EXPECT_NE(lb, rb) << "左右两块的色度不同, 输出 B 通道应当不同";
    EXPECT_GT(lb, lr) << "左块 U=240 偏蓝, B 应大于 R";
    EXPECT_GT(rr, rb) << "右块 V=240 偏红, R 应大于 B";
}
