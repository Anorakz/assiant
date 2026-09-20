// ============================================================================
//  tests/test_decoder.cpp — Decoder 测试 (host 侧, L1)
//
//  运行方式:
//      scripts/test-host.ps1
//
//  host 构建 **不** 定义 AGENT_HAVE_MPP / AGENT_HAVE_FFMPEG (宿主机两个都没有),
//  所以这里能测的是三类东西:
//
//    1) collapse_yuv420_to_i420 / convert_nv12_to_i420 —— 纯函数, 两个后端共用,
//       是真正能验的正确性 (含 stride 收拢、U/V 顺序、平面布局)
//    2) Decoder 的契约 —— 未初始化/非法入参时行为明确、不崩、不吐野指针;
//       以及**后端选择**: 没编进来的后端要明确失败并说清原因, 而不是假成功
//    3) classify_video_format 在 test_moonlight_adapter.cpp 里
//
//  "真解出一帧" 必须在板端做 (需要 MPP + 真实码流), 由板端冒烟测试覆盖:
//      tests/board/mpp_decode_smoke.cpp + tests/data/color_*.h26x
// ============================================================================

#include "decoder.h"

#include <gtest/gtest.h>

#include <cstdint>
#include <string>
#include <vector>

using agent::collapse_yuv420_to_i420;
using agent::convert_nv12_to_i420;
using agent::Decoder;
using agent::DecoderBackend;
using agent::DecodeStatus;
using agent::i420_frame_bytes;
using agent::VideoCodec;
using agent::YuvLayout;

namespace {

/// 一块 NV12 画面: Y 平面 (y_stride × h) + UV 交织平面 (uv_stride × h/2)
struct Nv12 {
    std::vector<std::uint8_t> y;
    std::vector<std::uint8_t> uv;
    int y_stride = 0;
    int uv_stride = 0;
    int w = 0;
    int h = 0;
};

/// 造一块 NV12, 每行前 w 个 Y / 前 w/2 组 UV 填指定值, 行尾留填充
/// @param y_stride 可以 > width, 用来模拟解码器的行对齐填充
Nv12 make_nv12(int w, int h, int y_stride, int uv_stride,
               std::uint8_t y_val, std::uint8_t u_val, std::uint8_t v_val) {
    Nv12 f;
    f.w = w;
    f.h = h;
    f.y_stride = y_stride;
    f.uv_stride = uv_stride;
    f.y.assign(static_cast<std::size_t>(y_stride) * h, 0xEE);           // 填充值故意不同
    f.uv.assign(static_cast<std::size_t>(uv_stride) * (h / 2), 0xEE);

    for (int row = 0; row < h; ++row) {
        for (int col = 0; col < w; ++col) {
            f.y[static_cast<std::size_t>(row) * y_stride + col] = y_val;
        }
    }
    // 注意: UV 是交织的, "一行" 里第 col 组 UV 占 offset col*2 和 col*2+1。
    // 行跨度是 uv_stride, 所以不能按连续下标写 —— 那样一旦 uv_stride > w
    // 就会把数据写到行尾的填充区里。
    for (int row = 0; row < h / 2; ++row) {
        const std::size_t base = static_cast<std::size_t>(row) * uv_stride;
        for (int col = 0; col < w / 2; ++col) {
            f.uv[base + col * 2 + 0] = u_val;
            f.uv[base + col * 2 + 1] = v_val;
        }
    }
    return f;
}

/// 一块独立平面的 YUV420 (I420 / YV12): Y / U / V 三块, 各自的行跨度可 > width
struct Planar {
    std::vector<std::uint8_t> y, u, v;
    int y_stride = 0;
    int uv_stride = 0;
    int w = 0;
    int h = 0;
};

/// @param swap_uv true 时把两个色度平面的**内容**对调 (模拟 YV12: 先 V 后 U)
Planar make_planar(int w, int h, int y_stride, int uv_stride, std::uint8_t y_val,
                   std::uint8_t u_val, std::uint8_t v_val, bool swap_uv = false) {
    Planar f;
    f.w = w;
    f.h = h;
    f.y_stride = y_stride;
    f.uv_stride = uv_stride;
    const int cw = w / 2;
    const int ch = h / 2;
    f.y.assign(static_cast<std::size_t>(y_stride) * h, 0xEE);
    // 第一块色度平面 (I420 里是 U; YV12 里是 V)
    f.u.assign(static_cast<std::size_t>(uv_stride) * ch, 0xEE);
    f.v.assign(static_cast<std::size_t>(uv_stride) * ch, 0xEE);

    for (int row = 0; row < h; ++row) {
        for (int col = 0; col < w; ++col) {
            f.y[static_cast<std::size_t>(row) * y_stride + col] = y_val;
        }
    }
    for (int row = 0; row < ch; ++row) {
        for (int col = 0; col < cw; ++col) {
            f.u[static_cast<std::size_t>(row) * uv_stride + col] = swap_uv ? v_val : u_val;
            f.v[static_cast<std::size_t>(row) * uv_stride + col] = swap_uv ? u_val : v_val;
        }
    }
    return f;
}

/// 把 I420 的三个平面取出来
struct Planes {
    std::vector<std::uint8_t> y, u, v;
};

Planes split_i420(const std::vector<std::uint8_t>& buf, int w, int h) {
    const std::size_t ys = static_cast<std::size_t>(w) * h;
    const std::size_t cs = ys / 4;
    Planes p;
    p.y.assign(buf.begin(), buf.begin() + ys);
    p.u.assign(buf.begin() + ys, buf.begin() + ys + cs);
    p.v.assign(buf.begin() + ys + cs, buf.begin() + ys + 2 * cs);
    return p;
}

}  // namespace

// ===========================================================================
//  NV12 → I420
// ===========================================================================

TEST(Nv12ToI420, ProducesCorrectPlaneLayout) {
    const int w = 4, h = 4;
    auto nv = make_nv12(w, h, w, w, /*Y=*/100, /*U=*/120, /*V=*/140);

    std::vector<std::uint8_t> out(i420_frame_bytes(w, h));
    convert_nv12_to_i420(nv.y.data(), nv.uv.data(), nv.y_stride, nv.uv_stride,
                         w, h, out.data());

    const Planes p = split_i420(out, w, h);

    // Y 平面全是 100
    for (std::uint8_t v : p.y) { EXPECT_EQ(v, 100); }
    // U 平面全是 120 (来自 UV 交织的第一个字节)
    for (std::uint8_t v : p.u) { EXPECT_EQ(v, 120); }
    // V 平面全是 140 (来自 UV 交织的第二个字节)
    for (std::uint8_t v : p.v) { EXPECT_EQ(v, 140); }
}

TEST(Nv12ToI420, SplitsInterleavedUvCorrectly) {
    // 4×2 画面, UV 平面逐列不同: U=10,11 / V=20,21
    // 交织序列应为 U0 V0 U1 V1 = 10 20 11 21
    const int w = 4, h = 2;
    const int y_stride = w, uv_stride = w;

    std::vector<std::uint8_t> yplane(static_cast<std::size_t>(y_stride) * h, 50);
    std::vector<std::uint8_t> uvplane(static_cast<std::size_t>(uv_stride) * (h / 2));
    uvplane[0] = 10;  // U0
    uvplane[1] = 20;  // V0
    uvplane[2] = 11;  // U1
    uvplane[3] = 21;  // V1

    std::vector<std::uint8_t> out(i420_frame_bytes(w, h));
    convert_nv12_to_i420(yplane.data(), uvplane.data(), y_stride, uv_stride,
                         w, h, out.data());

    const Planes p = split_i420(out, w, h);
    ASSERT_EQ(p.u.size(), 2u);
    ASSERT_EQ(p.v.size(), 2u);
    EXPECT_EQ(p.u[0], 10);
    EXPECT_EQ(p.u[1], 11);
    EXPECT_EQ(p.v[0], 20);
    EXPECT_EQ(p.v[1], 21);
}

TEST(Nv12ToI420, CollapsesYStride) {
    // y_stride 16, 可见宽 4: 只该取每行前 4 字节, 跳过后面的填充
    const int w = 4, h = 4, y_stride = 16, uv_stride = 16;
    auto nv = make_nv12(w, h, y_stride, uv_stride, 200, 30, 60);

    std::vector<std::uint8_t> out(i420_frame_bytes(w, h));
    convert_nv12_to_i420(nv.y.data(), nv.uv.data(), y_stride, uv_stride,
                         w, h, out.data());

    const Planes p = split_i420(out, w, h);
    EXPECT_EQ(p.y.size(), 16u);
    for (std::uint8_t v : p.y) {
        EXPECT_EQ(v, 200) << "填充字节 0xEE 不该被拷进来";
    }
    for (std::uint8_t v : p.u) { EXPECT_EQ(v, 30); }
    for (std::uint8_t v : p.v) { EXPECT_EQ(v, 60); }
}

TEST(Nv12ToI420, CollapsesUvStride) {
    // uv_stride 16, 可见色度宽 2: 每行只取前 2 组 UV (4 字节), 跳过后面的填充。
    // 4×4 画面 → 色度 2×2 = 4 个样本。
    const int w = 4, h = 4, y_stride = 4, uv_stride = 16;
    auto nv = make_nv12(w, h, y_stride, uv_stride, 90, 111, 222);

    std::vector<std::uint8_t> out(i420_frame_bytes(w, h));
    convert_nv12_to_i420(nv.y.data(), nv.uv.data(), y_stride, uv_stride,
                         w, h, out.data());

    const Planes p = split_i420(out, w, h);
    ASSERT_EQ(p.u.size(), 4u);
    ASSERT_EQ(p.v.size(), 4u);
    for (std::uint8_t v : p.u) { EXPECT_EQ(v, 111) << "不该读到 UV 行尾填充"; }
    for (std::uint8_t v : p.v) { EXPECT_EQ(v, 222) << "不该读到 UV 行尾填充"; }
}

TEST(Nv12ToI420, FullFrameRoundTrip) {
    // 更大一点的画面, 逐行逐列校验映射关系
    const int w = 8, h = 6, y_stride = 12, uv_stride = 12;

    std::vector<std::uint8_t> yplane(static_cast<std::size_t>(y_stride) * h, 0xEE);
    std::vector<std::uint8_t> uvplane(static_cast<std::size_t>(uv_stride) * (h / 2), 0xEE);
    for (int row = 0; row < h; ++row) {
        for (int col = 0; col < w; ++col) {
            yplane[static_cast<std::size_t>(row) * y_stride + col] =
                static_cast<std::uint8_t>(row * 16 + col);
        }
    }
    for (int row = 0; row < h / 2; ++row) {
        for (int col = 0; col < w / 2; ++col) {
            uvplane[static_cast<std::size_t>(row) * uv_stride + col * 2 + 0] =
                static_cast<std::uint8_t>(100 + row);  // U
            uvplane[static_cast<std::size_t>(row) * uv_stride + col * 2 + 1] =
                static_cast<std::uint8_t>(200 + row);  // V
        }
    }

    std::vector<std::uint8_t> out(i420_frame_bytes(w, h));
    convert_nv12_to_i420(yplane.data(), uvplane.data(), y_stride, uv_stride,
                         w, h, out.data());

    const Planes p = split_i420(out, w, h);
    for (int row = 0; row < h; ++row) {
        for (int col = 0; col < w; ++col) {
            EXPECT_EQ(p.y[static_cast<std::size_t>(row) * w + col], row * 16 + col)
                << "Y(" << col << "," << row << ")";
        }
    }
    for (int row = 0; row < h / 2; ++row) {
        for (int col = 0; col < w / 2; ++col) {
            const std::size_t i = static_cast<std::size_t>(row) * (w / 2) + col;
            EXPECT_EQ(p.u[i], 100 + row) << "U(" << col << "," << row << ")";
            EXPECT_EQ(p.v[i], 200 + row) << "V(" << col << "," << row << ")";
        }
    }
}

TEST(Nv12ToI420, OutputSizeMatchesI420Formula) {
    // 宽高比 4:2:0 时总字节数 = w*h*3/2
    EXPECT_EQ(i420_frame_bytes(256, 256), 256u * 256u * 3u / 2u);
    EXPECT_EQ(i420_frame_bytes(4, 2), 12u);

    const int w = 4, h = 2;
    auto nv = make_nv12(w, h, w, w, 1, 2, 3);
    std::vector<std::uint8_t> out(i420_frame_bytes(w, h), 0);
    convert_nv12_to_i420(nv.y.data(), nv.uv.data(), w, w, w, h, out.data());

    const Planes p = split_i420(out, w, h);
    EXPECT_EQ(p.y.size(), static_cast<std::size_t>(w) * h);
    EXPECT_EQ(p.u.size(), static_cast<std::size_t>(w / 2) * (h / 2));
    EXPECT_EQ(p.v.size(), static_cast<std::size_t>(w / 2) * (h / 2));
}

TEST(Nv12ToI420, NullAndInvalidArgsAreSafe) {
    std::vector<std::uint8_t> buf(64, 0);

    // 空指针: 不该崩
    convert_nv12_to_i420(nullptr, buf.data(), 4, 4, 4, 4, buf.data());
    convert_nv12_to_i420(buf.data(), nullptr, 4, 4, 4, 4, buf.data());
    convert_nv12_to_i420(buf.data(), buf.data(), 4, 4, 4, 4, nullptr);

    // 非法尺寸: 不该崩
    convert_nv12_to_i420(buf.data(), buf.data(), 4, 4, 0, 4, buf.data());
    convert_nv12_to_i420(buf.data(), buf.data(), 4, 4, 4, -2, buf.data());
}

// ===========================================================================
//  构造 / 析构
// ===========================================================================

TEST(DecoderStub, ConstructsAndDestructsSafely) {
    Decoder d;
    EXPECT_FALSE(d.is_initialized());
}

// ===========================================================================
//  未初始化时的状态
// ===========================================================================

TEST(DecoderStub, FreshDecoderIsNotInitialized) {
    Decoder d;

    EXPECT_FALSE(d.is_initialized());
    EXPECT_FALSE(d.is_hardware());
    EXPECT_EQ(d.frames_decoded(), 0u);
    EXPECT_EQ(d.active_decoder_name(), nullptr) << "还没初始化就不该有解码器名";
    EXPECT_EQ(d.status(), DecodeStatus::kNotInitialized);
}

TEST(DecoderStub, DecodeBeforeInitReturnsFalseAndClearsOutParams) {
    Decoder d;

    // 故意把出参填成垃圾值, 验证 decode 会先清干净
    const std::uint8_t fake[] = {0x00, 0x00, 0x00, 0x01, 0x67};
    std::uint8_t* yuv = reinterpret_cast<std::uint8_t*>(0xDEADBEEF);
    int w = 12345;
    int h = 67890;

    EXPECT_FALSE(d.decode(fake, sizeof(fake), &yuv, &w, &h));

    EXPECT_EQ(yuv, nullptr) << "失败时不该留下野指针";
    EXPECT_EQ(w, 0);
    EXPECT_EQ(h, 0);
    EXPECT_EQ(d.status(), DecodeStatus::kNotInitialized);
}

TEST(DecoderStub, DecodeIsSafeWithNullOutParams) {
    Decoder d;
    const std::uint8_t fake[] = {0x01};

    EXPECT_FALSE(d.decode(fake, sizeof(fake), nullptr, nullptr, nullptr));
}

// ===========================================================================
//  非法入参
// ===========================================================================

TEST(DecoderStub, InitRejectsNonPositiveDimensions) {
    Decoder d;

    EXPECT_FALSE(d.init(0, 1080, VideoCodec::kH264));
    EXPECT_EQ(d.status(), DecodeStatus::kInvalidArgument);
    EXPECT_FALSE(d.is_initialized());

    EXPECT_FALSE(d.init(1920, 0, VideoCodec::kH264));
    EXPECT_EQ(d.status(), DecodeStatus::kInvalidArgument);

    EXPECT_FALSE(d.init(-1920, -1080, VideoCodec::kH264));
    EXPECT_EQ(d.status(), DecodeStatus::kInvalidArgument);
}

TEST(DecoderStub, DecodeRejectsNullDataOrZeroSize) {
    Decoder d;

    EXPECT_FALSE(d.decode(nullptr, 100, nullptr, nullptr, nullptr));
    EXPECT_EQ(d.status(), DecodeStatus::kInvalidArgument)
        << "空指针应当先被参数校验拦下";

    const std::uint8_t x = 0;
    EXPECT_FALSE(d.decode(&x, 0, nullptr, nullptr, nullptr));
    EXPECT_EQ(d.status(), DecodeStatus::kInvalidArgument) << "size=0 应当被拦下";
}

TEST(DecoderStub, FlushBeforeInitFailsCleanly) {
    Decoder d;
    EXPECT_FALSE(d.flush());
    EXPECT_EQ(d.status(), DecodeStatus::kNotInitialized);
}

// ===========================================================================
//  host 上 init 必然失败 (没有 FFmpeg): 契约是"明确失败", 不是假成功
// ===========================================================================

TEST(DecoderStub, InitReturnsFalseWithoutFFmpeg) {
    Decoder d;

    EXPECT_FALSE(d.init(1920, 1080, VideoCodec::kH264))
        << "host 构建没有 FFmpeg, init 必须返回 false";
    EXPECT_FALSE(d.is_initialized())
        << "init 失败后不能把自己标成已初始化";
    EXPECT_EQ(d.frames_decoded(), 0u);
}

TEST(DecoderStub, InitIsSafeForBothCodecs) {
    for (const VideoCodec c : {VideoCodec::kH264, VideoCodec::kH265}) {
        Decoder d;
        EXPECT_FALSE(d.init(1280, 720, c));
        EXPECT_FALSE(d.is_initialized());
    }
}

TEST(DecoderStub, InitIsIdempotentAndReleasesPreviousState) {
    Decoder d;

    EXPECT_FALSE(d.init(1920, 1080, VideoCodec::kH264));
    EXPECT_FALSE(d.init(1280, 720, VideoCodec::kH265)) << "重复 init 不该崩";
    EXPECT_FALSE(d.init(640, 480, VideoCodec::kH264));

    EXPECT_FALSE(d.is_initialized());
    EXPECT_EQ(d.frames_decoded(), 0u);
}

// ===========================================================================
//  release
// ===========================================================================

TEST(DecoderStub, ReleaseIsIdempotent) {
    Decoder d;

    d.release();
    d.release();
    d.release();

    EXPECT_FALSE(d.is_initialized());
    EXPECT_EQ(d.frames_decoded(), 0u);
    EXPECT_EQ(d.status(), DecodeStatus::kNotInitialized);
}

TEST(DecoderStub, ReleaseAfterFailedInitLeavesCleanState) {
    Decoder d;

    EXPECT_FALSE(d.init(1920, 1080, VideoCodec::kH264));
    d.release();

    EXPECT_FALSE(d.is_initialized());
    EXPECT_FALSE(d.is_hardware());
    EXPECT_EQ(d.active_decoder_name(), nullptr);
    EXPECT_EQ(d.frames_decoded(), 0u);
}

TEST(DecoderStub, CanInitAgainAfterRelease) {
    Decoder d;

    EXPECT_FALSE(d.init(1920, 1080, VideoCodec::kH264));
    d.release();
    EXPECT_FALSE(d.init(1920, 1080, VideoCodec::kH264));
    d.release();

    EXPECT_FALSE(d.is_initialized());
}

// ===========================================================================
//  collapse_yuv420_to_i420: 独立平面 (I420 / YV12)
//
//  这一组是 board 上真解码之前的最后一道保险: MPP 给的是 I420 (planar) 还是
//  NV12 (semi-planar) 由 mpp_frame_get_fmt() 决定, 两条路都得对。而 1272 这种
//  不对齐的宽会带来 hor_stride=1280 —— stride 收拢错一点整幅图就斜掉。
// ===========================================================================

TEST(CollapseYuv420, PlanarI420PassthroughWithStrideCollapse) {
    const int w = 8;
    const int h = 4;
    // stride 故意大于 width, 行尾填 0xEE 当"垃圾": 收拢对了就绝不会读到它
    const Planar src = make_planar(w, h, /*y_stride=*/12, /*uv_stride=*/8,
                                   /*y=*/100, /*u=*/120, /*v=*/140);

    std::vector<std::uint8_t> out(i420_frame_bytes(w, h));
    collapse_yuv420_to_i420(src.y.data(), src.u.data(), src.v.data(), src.y_stride,
                            src.uv_stride, YuvLayout::kPlanar, /*uv_swapped=*/false,
                            w, h, out.data());

    const Planes p = split_i420(out, w, h);
    EXPECT_EQ(p.y, std::vector<std::uint8_t>(p.y.size(), 100));
    EXPECT_EQ(p.u, std::vector<std::uint8_t>(p.u.size(), 120));
    EXPECT_EQ(p.v, std::vector<std::uint8_t>(p.v.size(), 140));
}

TEST(CollapseYuv420, PlanarYv12SwapsUv) {
    const int w = 8;
    const int h = 4;
    // YV12: 内存里第一块色度是 V 不是 U。给 swap_uv=true, 期望输出仍然是 I420
    const Planar src = make_planar(w, h, w, w, /*y=*/10, /*u=*/200, /*v=*/30,
                                   /*swap_uv=*/true);

    std::vector<std::uint8_t> out(i420_frame_bytes(w, h));
    collapse_yuv420_to_i420(src.y.data(), src.u.data(), src.v.data(), src.y_stride,
                            src.uv_stride, YuvLayout::kPlanar, /*uv_swapped=*/true, w, h,
                            out.data());

    const Planes p = split_i420(out, w, h);
    EXPECT_EQ(p.u[0], 200) << "U 必须还原成真正的 U (不能把 V 当 U)";
    EXPECT_EQ(p.v[0], 30);
}

TEST(CollapseYuv420, SemiPlanarNv12SplitsInterleavedUv) {
    const int w = 8;
    const int h = 4;
    const Nv12 src = make_nv12(w, h, /*y_stride=*/16, /*uv_stride=*/16, 50, 60, 70);

    std::vector<std::uint8_t> out(i420_frame_bytes(w, h));
    collapse_yuv420_to_i420(src.y.data(), src.uv.data(), nullptr, src.y_stride,
                            src.uv_stride, YuvLayout::kSemiPlanar, /*uv_swapped=*/false,
                            w, h, out.data());

    const Planes p = split_i420(out, w, h);
    EXPECT_EQ(p.y, std::vector<std::uint8_t>(p.y.size(), 50));
    EXPECT_EQ(p.u, std::vector<std::uint8_t>(p.u.size(), 60));
    EXPECT_EQ(p.v, std::vector<std::uint8_t>(p.v.size(), 70));
}

TEST(CollapseYuv420, SemiPlanarNv21SwapsUv) {
    const int w = 8;
    const int h = 4;
    // NV21: 交织平面里第一个字节是 V。用 make_nv12 把 u_val/v_val 对调来模拟
    const Nv12 src = make_nv12(w, h, w, w, /*y=*/50, /*u_val=*/70, /*v_val=*/60);

    std::vector<std::uint8_t> out(i420_frame_bytes(w, h));
    collapse_yuv420_to_i420(src.y.data(), src.uv.data(), nullptr, src.y_stride,
                            src.uv_stride, YuvLayout::kSemiPlanar, /*uv_swapped=*/true, w,
                            h, out.data());

    const Planes p = split_i420(out, w, h);
    EXPECT_EQ(p.u[0], 60) << "NV21 的第一个字节是 V, 要当成 U 的邻居";
    EXPECT_EQ(p.v[0], 70);
}

TEST(CollapseYuv420, RejectsOddDimensionsWithoutWriting) {
    const int w = 7;  // 奇数
    const int h = 4;
    const Planar src = make_planar(8, h, 8, 8, 10, 20, 30);

    std::vector<std::uint8_t> out(i420_frame_bytes(8, h), 0xAB);
    const std::vector<std::uint8_t> before = out;
    collapse_yuv420_to_i420(src.y.data(), src.u.data(), src.v.data(), 8, 8,
                            YuvLayout::kPlanar, false, w, h, out.data());
    EXPECT_EQ(out, before) << "奇数宽必须原样返回, 不许写半个像素";
}

TEST(CollapseYuv420, RejectsStrideSmallerThanWidth) {
    const int w = 8;
    const int h = 4;
    const Planar src = make_planar(w, h, w, w, 10, 20, 30);

    std::vector<std::uint8_t> out(i420_frame_bytes(w, h), 0xAB);
    const std::vector<std::uint8_t> before = out;
    // uv_stride < width 是明显不可能的输入
    collapse_yuv420_to_i420(src.y.data(), src.u.data(), src.v.data(), w, /*uv_stride=*/4,
                            YuvLayout::kPlanar, false, w, h, out.data());
    EXPECT_EQ(out, before);
}

TEST(CollapseYuv420, PlanarRequiresBothChromaPlanes) {
    const int w = 8;
    const int h = 4;
    const Planar src = make_planar(w, h, w, w, 10, 20, 30);

    std::vector<std::uint8_t> out(i420_frame_bytes(w, h), 0xAB);
    const std::vector<std::uint8_t> before = out;
    // planar 布局下 v 不能为空 (semi-planar 才允许)
    collapse_yuv420_to_i420(src.y.data(), src.u.data(), nullptr, w, w,
                            YuvLayout::kPlanar, false, w, h, out.data());
    EXPECT_EQ(out, before);
}

TEST(CollapseYuv420, SemiPlanarIgnoresNullV) {
    const int w = 8;
    const int h = 4;
    const Nv12 src = make_nv12(w, h, w, w, 50, 60, 70);

    std::vector<std::uint8_t> out(i420_frame_bytes(w, h));
    // semi-planar 时第三个参数本来就该是 null, 不该因此拒绝
    collapse_yuv420_to_i420(src.y.data(), src.uv.data(), nullptr, w, w,
                            YuvLayout::kSemiPlanar, false, w, h, out.data());

    const Planes p = split_i420(out, w, h);
    EXPECT_EQ(p.u[0], 60);
}

// ===========================================================================
//  后端选择: 没编进来的后端必须"明确失败 + 说清原因"
//
//  这正是旧实现的病根 —— 它用"解码器名字存不存在"来判断硬解可用, 于是
//  hevc_v4l2m2m 明明开不起来却当成硬解, 软解回退永远不执行, 最后只对外丢一句
//  "decoder init failed"。现在每个候选失败都会带上自己的原因。
// ===========================================================================

TEST(DecoderBackend, MppOnlyFailsWithReasonOnHost) {
    Decoder d;

    EXPECT_FALSE(d.init(1280, 720, VideoCodec::kH265, DecoderBackend::kMpp))
        << "host 构建没有 MPP, kMpp 必须失败";
    EXPECT_FALSE(d.is_initialized());

    const std::string err = d.last_error();
    EXPECT_NE(err.find("MPP"), std::string::npos)
        << "失败原因必须点出是 MPP, 实际: " << err;
    EXPECT_NE(err.find("AGENT_HAVE_MPP"), std::string::npos)
        << "要说清是这一次构建没编进来, 而不是含糊的失败。实际: " << err;
}

TEST(DecoderBackend, FfmpegSwOnlyFailsWithReasonOnHost) {
    Decoder d;

    EXPECT_FALSE(d.init(1280, 720, VideoCodec::kH264, DecoderBackend::kFfmpegSw));
    const std::string err = d.last_error();
    EXPECT_NE(err.find("FFmpeg"), std::string::npos) << "实际: " << err;
}

TEST(DecoderBackend, AutoTriesBothAndReportsBothReasons) {
    Decoder d;

    EXPECT_FALSE(d.init(1280, 720, VideoCodec::kH265, DecoderBackend::kAuto));
    const std::string err = d.last_error();
    // kAuto = 先 MPP 后软解, 两个都不行时两边的理由都要留下
    EXPECT_NE(err.find("MPP"), std::string::npos) << "实际: " << err;
    EXPECT_NE(err.find("FFmpeg"), std::string::npos)
        << "回退过去了也要说明它为什么也不行。实际: " << err;
}

TEST(DecoderBackend, AutoIsTheDefault) {
    Decoder a;
    Decoder b;

    EXPECT_FALSE(a.init(1280, 720, VideoCodec::kH265));
    EXPECT_FALSE(b.init(1280, 720, VideoCodec::kH265, DecoderBackend::kAuto));
    // 不带参数 == 显式 kAuto (默认值不能变, 否则 adapter 的行为会跟着变)。
    // 注意用 STREQ: 两个 const char* 用 EXPECT_EQ 比的是指针不是内容。
    EXPECT_STREQ(a.last_error(), b.last_error());
}

TEST(DecoderBackend, ActiveBackendBeforeInitIsWhatWasRequested) {
    Decoder d;

    EXPECT_EQ(d.active_backend(), DecoderBackend::kAuto) << "默认是 kAuto";
    EXPECT_FALSE(d.init(640, 480, VideoCodec::kH264, DecoderBackend::kMpp));
    // 没生效时返回"请求的那个", 不假装成别的
    EXPECT_EQ(d.active_backend(), DecoderBackend::kMpp);
}

TEST(DecoderBackend, LastErrorIsEmptyBeforeAnyFailure) {
    Decoder d;
    EXPECT_STREQ(d.last_error(), "");
}

TEST(DecoderBackend, InvalidArgumentsReportWhy) {
    Decoder d;

    EXPECT_FALSE(d.init(0, 720, VideoCodec::kH264));
    EXPECT_EQ(d.status(), DecodeStatus::kInvalidArgument);
    EXPECT_NE(std::string(d.last_error()).find("宽高"), std::string::npos)
        << "非法宽高要说清楚, 实际: " << d.last_error();

    EXPECT_FALSE(d.init(-1, -1, VideoCodec::kH265));
    EXPECT_EQ(d.status(), DecodeStatus::kInvalidArgument);
}

TEST(DecoderBackend, DecodeAfterFailedInitSaysNotInitialized) {
    Decoder d;
    EXPECT_FALSE(d.init(1280, 720, VideoCodec::kH265, DecoderBackend::kMpp));

    std::uint8_t* yuv = reinterpret_cast<std::uint8_t*>(0x1);
    int w = 7;
    int h = 9;
    const std::uint8_t fake[4] = {0, 0, 0, 1};
    EXPECT_FALSE(d.decode(fake, sizeof(fake), &yuv, &w, &h));
    EXPECT_EQ(yuv, nullptr) << "失败时必须把出参清干净, 不能留野指针";
    EXPECT_EQ(w, 0);
    EXPECT_EQ(h, 0);
}

// ===========================================================================
//  flush 的契约
//
//  flush 会给出**新的**借用指针, 这一点很要紧: 调用方在 decode() 拿到的指针在
//  flush() 之后就失效了 (缓冲被下一帧覆盖)。板端冒烟测试第一版就是拿旧指针去读,
//  直接段错误 —— 所以这里把"出参必须每次都给全/清干净"钉住。
// ===========================================================================

TEST(DecoderFlush, BeforeInitClearsOutParamsAndFails) {
    Decoder d;

    std::uint8_t* yuv = reinterpret_cast<std::uint8_t*>(0x1);
    int w = 11;
    int h = 13;
    EXPECT_FALSE(d.flush(&yuv, &w, &h));
    EXPECT_EQ(d.status(), DecodeStatus::kNotInitialized);
    EXPECT_EQ(yuv, nullptr) << "flush 失败也要把出参清干净";
    EXPECT_EQ(w, 0);
    EXPECT_EQ(h, 0);
}

TEST(DecoderFlush, IsSafeWithoutOutParams) {
    Decoder d;
    // 老用法 (不带出参) 必须继续能编能跑
    EXPECT_FALSE(d.flush());
    EXPECT_EQ(d.status(), DecodeStatus::kNotInitialized);
}

TEST(DecoderFlush, AfterFailedInitStillFailsCleanly) {
    Decoder d;
    EXPECT_FALSE(d.init(1280, 720, VideoCodec::kH265, DecoderBackend::kMpp));

    std::uint8_t* yuv = reinterpret_cast<std::uint8_t*>(0x1);
    int w = 5;
    int h = 6;
    EXPECT_FALSE(d.flush(&yuv, &w, &h));
    EXPECT_EQ(yuv, nullptr);
    EXPECT_EQ(w, 0);
    EXPECT_EQ(h, 0);
}

TEST(DecoderFlush, IsIdempotentOnAHostBuildWithoutBackends) {
    Decoder d;
    d.flush();
    d.flush();
    d.flush();
    d.release();
    EXPECT_FALSE(d.is_initialized());
}
