// ============================================================================
//  tests/test_decoder.cpp — Decoder 测试 (host 侧, L1)
//
//  运行方式:
//      scripts/test-host.ps1
//
//  host 构建 **不** 定义 AGENT_HAVE_FFMPEG (宿主机没装 FFmpeg), 所以这里能测
//  的是两类东西:
//
//    1) NV12 → I420 转换  —— 纯函数, 不依赖 FFmpeg, 是真正能验的正确性
//    2) Decoder 的状态机契约 —— 未初始化/非法入参时行为明确、不崩、不吐野指针
//
//  "真解出一帧" 必须在带硬解的板端做 (需要真实 H.264 码流 + rkvdec),
//  那里由部署后的板端冒烟测试覆盖。
// ============================================================================

#include "decoder.h"

#include <gtest/gtest.h>

#include <cstdint>
#include <vector>

using agent::convert_nv12_to_i420;
using agent::Decoder;
using agent::DecodeStatus;
using agent::i420_frame_bytes;
using agent::VideoCodec;

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
