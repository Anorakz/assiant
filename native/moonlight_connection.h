// ============================================================================
//  moonlight_connection.h — GameStream 连接握手 (HTTP + XML)
//
//  为什么需要这一层
//  ---------------------------------------------------------------------------
//  moonlight-common-c 只做 GameStream/RTSP 协议本身, **不含 HTTP 与 XML**。
//  但 LiStartConnection() 需要两个由宿主 HTTP 层拿到的字段:
//
//      serverInfo.serverInfoAppVersion  ← /serverinfo 的 <appversion>
//      serverInfo.rtspSessionUrl        ← /launch 或 /resume 的 <sessionUrl0>
//
//  所以"连接管理"里真正的难点不在 moonlight, 而在这套握手:
//
//      GET  /serverinfo?uniqueid=..   → appversion / GfeVersion / codec 支持
//      POST /launch?uniqueid=..&appid=..&mode=WxHxFPS
//                                    → sessionUrl0 (会话 URL, 交给 RTSP)
//
//  本模块用 **原始 socket** 实现最小 HTTP 客户端 (不引 libcurl, 免得再给
//  交叉编译加一个依赖), 以及针对这些已知响应的极简 XML 字段提取。
//
//  只依赖 /serverinfo 与 /launch 两个接口 + 一个 <sessionUrl0> 字段,
//  刻意不做通用实现 —— 越通用越难在小样本上验证。
// ============================================================================

#pragma once

#include <cstdint>
#include <string>

namespace agent {
namespace moonlight_connection {

/// GameStream 默认端口
inline constexpr std::uint16_t kDefaultHttpPort = 47989;

/// /serverinfo 里我们关心的字段
struct ServerInfo {
    bool ok = false;
    int status_code = 0;
    std::string hostname;
    /// <appversion> —— 就是 moonlight 的 serverInfoAppVersion
    std::string app_version;
    /// <GfeVersion> —— 就是 moonlight 的 serverInfoGfeVersion
    std::string gfe_version;
    std::string unique_id;
    /// <ServerCodecModeSupport> 位掩码 (SCM_H264 / SCM_HEVC ...)
    std::uint32_t codec_mode_support = 0;
    /// <PairStatus> 0 = 未配对
    int pair_status = -1;
    /// 原始响应体, 失败时用来诊断
    std::string raw;
};

/// /launch (或 /resume) 的结果
struct LaunchResult {
    bool ok = false;
    int status_code = 0;
    /// <sessionUrl0> —— 就是 moonlight 的 rtspSessionUrl
    std::string session_url;
    std::string status_message;
    std::string raw;
};

/// @param host 主机地址, 允许 "192.168.1.10" 或 "192.168.1.10:47989"
/// @param port 当 host 里没带端口时使用
struct HttpResult {
    bool connected = false;  ///< TCP 层是否连上并拿到响应
    int status_code = 0;
    std::string body;
    std::string error;  ///< connected == false 时的原因
};

/// 发一个 HTTP GET (HTTP/1.0, Connection: close), 阻塞直到读完或超时
/// @param timeout_ms 连接与读写的总超时
HttpResult http_get(const std::string& host,
                    std::uint16_t port,
                    const std::string& path,
                    int timeout_ms = 5000);

/// 发一个 HTTP POST (无 body)
HttpResult http_post(const std::string& host,
                     std::uint16_t port,
                     const std::string& path,
                     int timeout_ms = 5000);

/// 取 /serverinfo
ServerInfo fetch_server_info(const std::string& host,
                             std::uint16_t port = kDefaultHttpPort,
                             int timeout_ms = 5000);

/// 请求启动应用, 拿会话 URL
/// @param app_id     /applist 里的应用 id; 用 app_id 或 app_name 之一
/// @param app_name   应用名 (Sunshine 支持按名字启动)
/// @param mode       形如 "1920x1080x60"
LaunchResult launch_app(const std::string& host,
                        const std::string& app_id,
                        const std::string& app_name,
                        const std::string& mode,
                        const std::string& unique_id,
                        std::uint16_t port = kDefaultHttpPort,
                        int timeout_ms = 8000);

// ---------------------------------------------------------------------------
//  纯函数 (单独导出以便单测, 都不碰网络)
// ---------------------------------------------------------------------------

/// 从 XML 里取 <tag>value</tag> 的 value; 找不到返回空串
/// @note 只处理这些固定响应里的简单形态, 不做通用 XML 解析
/// @note 开标签可以带属性 (<tag a="b">value</tag>)
std::string extract_tag(const std::string& xml, const std::string& tag);

/// 从 XML 里取属性值, 例如 extract_attribute(x, "status_message")
/// 对应 <root status_message="Invalid uniqueid"/>
/// @note Sunshine 的 status_message 是 **属性** 不是元素, 所以需要单独一个
///       函数; 用 extract_tag 是取不到的。
std::string extract_attribute(const std::string& xml, const std::string& name);

/// 取 <root status_code="200" .../> 里的 status_code; 解析失败返回 -1
int extract_status_code(const std::string& xml);

/// 把 "host" 或 "host:port" 拆开; 没写端口时 port 保持不变
void split_host_port(const std::string& in, std::string& host, std::uint16_t& port);

}  // namespace moonlight_connection
}  // namespace agent
