// ============================================================================
//  preprocess.h — 解码后 YUV420P 帧 → 256×256 RGB888
//
//  架构位置 (docs/architecture.md §3.1)
//  ---------------------------------------------------------------------------
//      Sunshine 视频流 → 解码 (H.264/H.265) → **ROI 裁剪 + 缩放到 256×256
//      + RGB888** → ImageRingBuffer → pybind11 → 视觉模型 (SigLIP)
//
//  设计约束
//  ---------------------------------------------------------------------------
//  · 纯 C++ 实现, 不依赖任何库 (不用 ffmpeg swscale / libyuv / OpenCV)
//  · 输入输出都是裸指针, 不用 STL 容器 —— 这个函数在解码回调里被高频调用,
//    不想引入额外分配
//  · 缩放用最近邻: 先保证正确, 之后再换 SIMD / swscale 优化性能
//
//  像素格式: YUV420P (I420)
//  ---------------------------------------------------------------------------
//  三个平面依次紧密排列 (plane stride == width):
//
//      Y 平面: src_w × src_h         每像素 1 字节
//      U 平面: (src_w/2) × (src_h/2) 每 2×2 像素共享 1 字节
//      V 平面: (src_w/2) × (src_h/2)
//
//  所以 src_w / src_h 必须是偶数。
//
//  色彩换算: BT.601 limited range (视频常用的 16-235 / 16-240)
//  ---------------------------------------------------------------------------
//      R = 1.164(Y-16)              + 1.596(V-128)
//      G = 1.164(Y-16) - 0.391(U-128) - 0.813(V-128)
//      B = 1.164(Y-16) + 2.018(U-128)
//
//  ⚠ 如果矩阵 (BT.709) 或 full range 的源接进来, 这个换算会偏色。
//    届时再加一个 enum 参数区分, 现在不预先复杂化。
// ============================================================================

#pragma once

#include <cstdint>

namespace agent {

/// 感兴趣区域 (源图像坐标系, 单位: 像素)
struct Roi {
    int x;
    int y;
    int w;
    int h;
};

/// 预处理输出尺寸 (固定)
inline constexpr int kRoiOutSize = 256;
/// 输出缓冲区字节数 = 256×256×3 (RGB888)
inline constexpr int kRoiOutBytes = kRoiOutSize * kRoiOutSize * 3;

/// 把 ROI 夹到图像范围内; w/h <= 0 或完全落在图像外时归一化为空 (w=h=0)
///
/// 这是 preprocess_frame() 内部实际使用的区域, 单独暴露出来方便调用方
/// 提前判断 (例如"这块 ROI 里没有有效内容, 跳过这一帧")。
Roi clamp_roi(const Roi& roi, int src_w, int src_h);

/// ROI 是否有效 (夹取后仍有正面积)
bool roi_is_valid(const Roi& roi, int src_w, int src_h);

/// 把 YUV420P 帧的 ROI 区域缩放成 256×256 RGB888 写入 rgb_out
///
/// @param yuv_data  YUV420P 三平面紧密排列的起始地址
///                  (Y 之后紧跟 U, 再跟 V; 每行 stride == 对应平面宽度)
/// @param src_w     源宽, 必须 > 0 且为偶数
/// @param src_h     源高, 必须 > 0 且为偶数
/// @param roi       ROI 裁剪区域; 会自动夹到图像范围内
/// @param rgb_out   输出缓冲, 必须至少 kRoiOutBytes 字节, 行优先 RGB
///
/// ⚠ 本函数假设 **平面紧密排列且 stride == width**, 也就是 line stride 没有
///   对齐填充。多数硬解码器 (含 FFmpeg 的 AVFrame) 会按 16/32/64 字节对齐
///   行宽, 这时不能把 AVFrame::data[0] 直接传进来 —— 需要先把各行收拢
///   (或改成接收 stride 参数的版本)。接解码器时这是第一个要确认的点。
///
/// 行为约定
///  ---------------------------------------------------------------------------
///  · ROI 超出图像范围 → 按交集夹取, 只对实际存在的像素采样 (不越界读)
///  · 夹取后 ROI 为空 (w/h <= 0, 或完全在图像外) → 整个输出填黑 (0,0,0),
///    不写输入、不抛异常。用 roi_is_valid() 可以提前判断这种情况。
///  · yuv_data / rgb_out 为空指针, 或 src_w/src_h <= 0 → 直接返回 (不做任何事)
///  · 缩放为最近邻, 采样点按 ROI 内相对位置映射, 并夹在 ROI 内
void preprocess_frame(const std::uint8_t* yuv_data,
                      int src_w,
                      int src_h,
                      const Roi& roi,
                      std::uint8_t* rgb_out);

}  // namespace agent
