// ============================================================================
//  moonlight_adapter.h — 连接管理 + 视频接收线程
//
//  架构位置 (docs/architecture.md §3.1)
//  ---------------------------------------------------------------------------
//      MoonlightAdapter
//        ├── 连接握手 —— 两条入口, 只差"谁做握手":
//        │     start()                明文 HTTP 47989 (留给无 TLS 的 GFE 主机)
//        │     start_with_session()   握手在外面做完 (Sunshine: HTTPS 47984 +
//        │                            客户端证书, 见 agent/net/sunshine_client.py)
//        │   两条都汇到 connect_limelight() → LiStartConnection
//        ├── 视频接收线程 (moonlight 内部): decoderSetup (协商结果)
//        │     → Decoder(MPP 硬解) → submitDecodeUnit (Annex-B 帧)
//        │     → preprocess_frame → image_rb_ (256×256 RGB888)
//        └── 只有**一条**接收通路 —— 原来那个"主机→客户端"的输入回传环形缓冲
//              (HostInputRingBuffer) 已删除: moonlight-common-c 根本没有这个 API,
//              它从来没有生产者 (todo.md 决策记录)。
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
#include "image_rb.h"

namespace agent {

enum class AdapterState {
    kIdle = 0,
    kConnecting,
    kStreaming,
    kStopping,
};

/// moonlight 协商到的视频格式分类 (on_decoder_setup 用)
enum class VideoFormatCheck {
    kOk = 0,      ///< 能用, 编码格式见 out_codec
    kTenBit,      ///< 10bit (HDR) —— I420 这条路表达不了, 明确拒绝
    kYuv444,      ///< 4:4:4 —— 只支持 4:2:0
    kUnknown,     ///< 既不是 H.264 也不是 H.265 (比如 AV1)
};

/// 把 moonlight 的 VIDEO_FORMAT_* 位掩码翻译成我们的编码格式。
///
/// @param video_format Limelight.h 里的 VIDEO_FORMAT_* 值
/// @param out_codec    [out] kOk 时写入编码格式 (其它情况不动)
/// @return 分类结果
///
/// @note 这里是**纯函数**且不 include Limelight.h: 宿主机(不链接 moonlight)
///       也能单独测这段映射 —— 它是"协商到 10bit 要拒绝"这类判断的唯一出处。
VideoFormatCheck classify_video_format(int video_format, VideoCodec* out_codec);

class MoonlightAdapter {
public:
    MoonlightAdapter();
    ~MoonlightAdapter();

    MoonlightAdapter(const MoonlightAdapter&) = delete;
    MoonlightAdapter& operator=(const MoonlightAdapter&) = delete;

    /// 连接并开始收流 —— **握手已经由调用方做完了**。
    ///
    /// @param host  Sunshine/GFE 主机地址 ("192.168.1.10"; 端口不在这里用)
    /// @param app   应用名或 appid (只影响日志; 真正决定会话的是 session_url)
    /// @param width/height/fps 期望的流参数
    /// @param app_version        /serverinfo 的 <appversion>  → serverInfoAppVersion
    /// @param gfe_version        /serverinfo 的 <GfeVersion>  → serverInfoGfeVersion
    /// @param codec_mode_support /serverinfo 的 <ServerCodecModeSupport>
    /// @param session_url        /launch 或 /resume 的 <sessionUrl0> → rtspSessionUrl
    /// @return false 表示失败, 原因见 last_error()
    ///
    /// @note **这是唯一的连接入口**: native 侧不发任何 HTTP。Sunshine 的
    ///       /serverinfo /applist /launch 都在 HTTPS 47984 + 客户端证书后面, 那半由
    ///       agent/net/sunshine_client.py 负责。
    ///       原来那个手写裸 socket 的明文 HTTP 层 (moonlight_connection.*) 已在
    ///       Phase 6 删除 —— 它只能拿到 PairStatus=0 与 404, 留着只会让人以为
    ///       还有一条能用的路。
    /// @note app_version 为空时 LiStartConnection 必然失败, session_url 为空则没有
    ///       会话可接 —— 两者都在这里当场拦下, 报错话说清缺的是哪一项。
    bool start_with_session(const std::string& host,
                            const std::string& app,
                            int width,
                            int height,
                            int fps,
                            const std::string& app_version,
                            const std::string& gfe_version,
                            int codec_mode_support,
                            const std::string& session_url);

    /// 断开并停止接收线程; 可重复调用
    void stop();

    /// 是否已经在收流 (kStreaming)
    bool is_connected() const;

    /// 当前状态
    AdapterState state() const;

    /// 最近一次失败原因 (空串表示没有)
    std::string last_error() const;

    ImageRingBuffer& image_rb();

    // ------------------------------------------------------------- 诊断 ---
    /// 视频线程已收到的解码单元数 (含解码失败的)
    std::size_t video_units_received() const;
    /// 成功写进 image_rb 的帧数
    std::size_t frames_pushed() const;

    // 回调入口 (public 是为了让 C 风格静态函数能转发进来; 不要直接调用)
    /// 视频参数协商好了 —— 在这里按**协商到的编码格式**初始化解码器
    /// @param video_format moonlight 的 VIDEO_FORMAT_* (见 Limelight.h)
    /// @param width/height 流参数 (来自 StreamConfig, 不是屏幕分辨率)
    /// @return false 表示解码器起不来 (会让 LiStartConnection 失败, 这是有意的:
    ///         与其连上了却一帧都解不出来, 不如当场失败并说清原因)
    bool on_decoder_setup(int video_format, int width, int height);
    /// @param annex_b  moonlight 给的 Annex-B 码流 (可能跨多个 buffer)
    /// @return true 表示这一帧处理成功
    bool on_video_frame(const std::uint8_t* annex_b, std::size_t size);
    /// 连接真正建立 (moonlight 的 ConnectionStarted 阶段)
    void on_connection_started();
    /// 连接断开 (errorCode != 0 表示异常断开)
    void on_connection_terminated(int error_code);

private:
    /// start() / start_with_session() 共用的前置检查与参数落地
    /// (状态必须是 kIdle、host/app 非空、尺寸为正; 通过后进入 kConnecting)
    bool prepare_start(const std::string& host,
                       const std::string& app,
                       int width,
                       int height,
                       int fps);

    /// 握手字段到手之后真正交给 moonlight —— start() 与 start_with_session() 共用,
    /// 保证两条入口在"进 LiStartConnection 之前"的行为完全一致。
    bool connect_limelight(const std::string& app_version,
                           const std::string& gfe_version,
                           int codec_mode_support,
                           const std::string& session_url);

    struct Impl;
    std::unique_ptr<Impl> impl_;
};

}  // namespace agent
