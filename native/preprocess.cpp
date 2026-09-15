// ============================================================================
//  preprocess.cpp — YUV420P → 256×256 RGB888 实现
//
//  两道处理, 顺序很关键: 先按 ROI 裁剪, 再缩放到 256×256。
//  缩放用的是 "目标像素中心" 映射:
//
//      src_x = roi.x + (dx * roi.w) / 256
//
//  整数运算, 对每个输出像素恒定。采样点再夹一次到 ROI 内, 保证
//  roi.w < 256 (放大) 时也不会越过 ROI 边界。
//
//  YUV→RGB 用定点数 (×1024) 而不是浮点: 这个函数在每个解码帧上跑
//  65536 次, 定点没有浮点转换开销, 系数也能一眼对回标准公式。
// ============================================================================

#include "preprocess.h"

#include <algorithm>

namespace agent {
namespace {

/// 定点小数位
constexpr int kShift = 10;
constexpr int kRound = 1 << (kShift - 1);

/// BT.601 limited range 系数 ×1024
constexpr int kYScale = 1192;   // 1.164 × 1024
constexpr int kRV = 1634;       // 1.596
constexpr int kGU = 400;        // 0.391
constexpr int kGV = 833;        // 0.813
constexpr int kBU = 2066;       // 2.018

constexpr std::uint8_t clamp_u8(int v) {
    return static_cast<std::uint8_t>(v < 0 ? 0 : (v > 255 ? 255 : v));
}

/// 把输出像素填黑 (极简的 memset, 不引 <cstring> 也行)
void fill_black(std::uint8_t* rgb_out) {
    for (int i = 0; i < kRoiOutBytes; ++i) {
        rgb_out[i] = 0;
    }
}

}  // namespace

Roi clamp_roi(const Roi& roi, int src_w, int src_h) {
    Roi out{0, 0, 0, 0};
    if (src_w <= 0 || src_h <= 0 || roi.w <= 0 || roi.h <= 0) {
        return out;  // 空 ROI
    }

    // 先夹左/上边界
    const int x0 = std::max(0, roi.x);
    const int y0 = std::max(0, roi.y);

    // 右/下边界: 用 64 位算, 避免 roi.x + roi.w 在极端入参下溢出 int
    const long long x1 = std::min<long long>(static_cast<long long>(roi.x) + roi.w,
                                            src_w);
    const long long y1 = std::min<long long>(static_cast<long long>(roi.y) + roi.h,
                                            src_h);

    // 完全落在图像外
    if (x1 <= x0 || y1 <= y0) {
        return out;
    }

    out.x = x0;
    out.y = y0;
    out.w = static_cast<int>(x1 - x0);
    out.h = static_cast<int>(y1 - y0);
    return out;
}

bool roi_is_valid(const Roi& roi, int src_w, int src_h) {
    const Roi c = clamp_roi(roi, src_w, src_h);
    return c.w > 0 && c.h > 0;
}

void preprocess_frame(const std::uint8_t* yuv_data,
                      int src_w,
                      int src_h,
                      const Roi& roi,
                      std::uint8_t* rgb_out) {
    if (yuv_data == nullptr || rgb_out == nullptr) {
        return;
    }
    if (src_w <= 0 || src_h <= 0) {
        return;
    }

    const Roi area = clamp_roi(roi, src_w, src_h);
    if (area.w <= 0 || area.h <= 0) {
        fill_black(rgb_out);  // ROI 为空: 给一个确定的输出
        return;
    }

    // YUV420P 三个平面紧密排列
    const int chroma_w = src_w / 2;
    const int chroma_h = src_h / 2;
    const std::uint8_t* y_plane = yuv_data;
    const std::uint8_t* u_plane = yuv_data + static_cast<std::size_t>(src_w) * src_h;
    const std::uint8_t* v_plane =
        u_plane + static_cast<std::size_t>(chroma_w) * chroma_h;

    for (int dy = 0; dy < kRoiOutSize; ++dy) {
        // 目标行中心映射回 ROI 内相对行
        int ry = (dy * area.h) / kRoiOutSize;
        if (ry >= area.h) {
            ry = area.h - 1;
        }
        const int sy = area.y + ry;

        std::uint8_t* dst = rgb_out + static_cast<std::size_t>(dy) * kRoiOutSize * 3;

        for (int dx = 0; dx < kRoiOutSize; ++dx) {
            int rx = (dx * area.w) / kRoiOutSize;
            if (rx >= area.w) {
                rx = area.w - 1;
            }
            const int sx = area.x + rx;

            const int Y = y_plane[static_cast<std::size_t>(sy) * src_w + sx];
            // 色度是 2×2 下采样, 直接用像素坐标除以 2 定位
            const int cu = (sy >> 1) * chroma_w + (sx >> 1);
            const int U = u_plane[cu];
            const int V = v_plane[cu];

            // 先减偏置, 后面三个通道共用
            const int yy = (Y - 16) * kYScale;
            const int uu = U - 128;
            const int vv = V - 128;

            const int r = (yy + kRV * vv + kRound) >> kShift;
            const int g = (yy - kGU * uu - kGV * vv + kRound) >> kShift;
            const int b = (yy + kBU * uu + kRound) >> kShift;

            dst[dx * 3 + 0] = clamp_u8(r);
            dst[dx * 3 + 1] = clamp_u8(g);
            dst[dx * 3 + 2] = clamp_u8(b);
        }
    }
}

}  // namespace agent
