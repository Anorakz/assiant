// ============================================================================
//  moonlight_adapter.h — 连接管理 + 两条接收线程
//
//  架构位置 (docs/architecure.md §3.1)
//  ---------------------------------------------------------------------------
//      MoonlightAdapter
//        ├── 连接握手 (moonlight_connection) → LiStartConnection
//        ├── 视频接收线程 (moonlight 内部): submitDecodeUnit (Annex-B 帧)
//        │     → Decoder(H.265 硬解) → preprocess_frame
//        │     → image_rb_ (256×256 RGB888)
//        └── 主机输入接收线程 (moonlight 内部): connectionStatusUpdate /
//              moonlight 的输入回调 → host_input_rb_
//
//  ⚠ 线程归属 (很重要, 决定了能不能加锁)
//  ---------------------------------------------------------------------------
//  moonlight-common-c 自己起线程, 我们的 C 回调 **在它的线程上被调用**:
//
//      submitDecodeUnit        ← 视频线程 (每帧一次)
//      各 ConnListener*        ← 连接/控制线程
//
//  所以适配器主体不需要自己起线程, 也不该在回调里做重活。当前实现里,
//  这些回调只做"转成内部格式 + 写环形缓冲", 环形缓冲本身是 SPSC 无锁的,
//  正好匹配"moonlight 单线程写 / Python 单线程读"的模型。
//
//  状态机
//  ---------------------------------------------------------------------------
//      kIdle ──start()──▶ kConnecting ──ConnectionStarted──▶ kStreaming
//        ▲                    │                                  │
//        └──── stop() ────────┴──────── ConnectionTerminated ────┘
//
//  is_connected() 只在 kStreaming 时为 true —— 也就是真正开始收流之后,
//  而不是 TCP 建连成功。
// ============================================================================

#pragma once

#include <atomic>
#include <cstdint>
#include <memory>
#include <string>

#include "decoder.h"
#include "host_input_rb.h"
#include "image_rb.h"

namespace agent {

enum class AdapterState {
    kIdle = 0,
    kConnecting,
    kStreaming,
    kStopping,
};

class MoonlightAdapter {
public:
    MoonlightAdapter();
    ~MoonlightAdapter();

    MoonlightAdapter(const MoonlightAdapter&) = delete;
    MoonlightAdapter& operator=(const MoonlightAdapter&) = delete;

    /// 连接并开始收流 (阻塞直到连接建立或失败)
    /// @param host  Sunshine/GFE 主机地址 ("192.168.1.10" 或 "host:port")
    /// @param app   应用名或 appid (见 /applist; Sunshine 直接支持应用名)
    /// @param width/height/fps 期望的流参数
    /// @return false 表示失败, 原因见 last_error()
    ///
    /// @note 这是阻塞调用, 内部会做 HTTP 握手 + LiStartConnection。
    bool start(const std::string& host,
               const std::string& app,
               int width,
               int height,
               int fps);

    /// 断开并停止接收线程; 可重复调用
    void stop();

    /// 是否已经在收流 (kStreaming)
    bool is_connected() const;

    /// 当前状态
    AdapterState state() const;

    /// 最近一次失败原因 (空串表示没有)
    std::string last_error() const;

    ImageRingBuffer& image_rb();
    HostInputRingBuffer& host_input_rb();

    // ------------------------------------------------------------- 诊断 ---
    /// 视频线程已收到的解码单元数 (含解码失败的)
    std::size_t video_units_received() const;
    /// 成功写进 image_rb 的帧数
    std::size_t frames_pushed() const;

    // 回调入口 (public 是为了让 C 风格静态函数能转发进来; 不要直接调用)
    /// @param annex_b  moonlight 给的 Annex-B 码流 (可能跨多个 buffer)
    /// @return true 表示这一帧处理成功
    bool on_video_frame(const std::uint8_t* annex_b, std::size_t size);
    /// 连接真正建立 (moonlight 的 ConnectionStarted 阶段)
    void on_connection_started();
    /// 连接断开 (errorCode != 0 表示异常断开)
    void on_connection_terminated(int error_code);

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

}  // namespace agent
