// ============================================================================
//  moonlight_connection.cpp — 最小 HTTP 客户端 + XML 字段提取
//
//  为什么不引 libcurl: 交叉编译已经要处理 FFmpeg / OpenSSL / moonlight 三个
//  依赖, 再加一个 HTTP 库不划算。这里只需要 HTTP/1.0 的裸 GET/POST,
//  原始 socket 足够, 而且能在 host 上直接对着真实服务器测。
//
//  跨平台: Windows 用 Winsock, Linux 用 BSD socket。协议逻辑完全共用。
// ============================================================================

#include "moonlight_connection.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <winsock2.h>
#include <ws2tcpip.h>
// 注意: 这里不能用 #pragma comment(lib, "ws2_32.lib") —— MinGW 会忽略它,
// 链接期会报一堆 __imp_WSAStartup 未定义。ws2_32 由 CMake 显式链接。
using socket_t = SOCKET;
#define AGENT_INVALID_SOCKET INVALID_SOCKET
#define AGENT_CLOSE_SOCKET closesocket
#else
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>
using socket_t = int;
#define AGENT_INVALID_SOCKET (-1)
#define AGENT_CLOSE_SOCKET close
#endif

namespace agent {
namespace moonlight_connection {
namespace {

/// 确保 Winsock 初始化 (Linux 上是空操作)
bool ensure_sockets() {
#ifdef _WIN32
    static bool inited = false;
    if (!inited) {
        WSADATA wsa;
        if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) {
            return false;
        }
        inited = true;
    }
#endif
    return true;
}

/// 用 select 做带超时的连接
bool connect_with_timeout(socket_t s, const sockaddr* addr, int addrlen, int timeout_ms) {
    // 非阻塞 connect + select
#ifdef _WIN32
    u_long nb = 1;
    ioctlsocket(s, FIONBIO, &nb);
#else
    int flags = fcntl(s, F_GETFL, 0);
    fcntl(s, F_SETFL, flags | O_NONBLOCK);
#endif
    const int rc = ::connect(s, addr, addrlen);

    if (rc != 0) {
        fd_set wset;
        FD_ZERO(&wset);
        FD_SET(s, &wset);
        timeval tv;
        tv.tv_sec = timeout_ms / 1000;
        tv.tv_usec = (timeout_ms % 1000) * 1000;
        const int sel = ::select(static_cast<int>(s) + 1, nullptr, &wset, nullptr, &tv);
        if (sel <= 0) {
            return false;
        }
        int err = 0;
#ifdef _WIN32
        int len = sizeof(err);
        getsockopt(s, SOL_SOCKET, SO_ERROR, reinterpret_cast<char*>(&err), &len);
#else
        socklen_t len = sizeof(err);
        getsockopt(s, SOL_SOCKET, SO_ERROR, &err, &len);
#endif
        if (err != 0) {
            return false;
        }
    }

    // 恢复阻塞模式, 后续用 SO_RCVTIMEO/SO_SNDTIMEO 控制超时
#ifdef _WIN32
    u_long blk = 0;
    ioctlsocket(s, FIONBIO, &blk);
    DWORD tv_ms = static_cast<DWORD>(timeout_ms);
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, reinterpret_cast<const char*>(&tv_ms), sizeof(tv_ms));
    setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, reinterpret_cast<const char*>(&tv_ms), sizeof(tv_ms));
#else
    fcntl(s, F_SETFL, flags);
    timeval tv;
    tv.tv_sec = timeout_ms / 1000;
    tv.tv_usec = (timeout_ms % 1000) * 1000;
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
#endif
    return true;
}

HttpResult do_request(const std::string& method,
                      const std::string& host,
                      std::uint16_t port,
                      const std::string& path,
                      int timeout_ms) {
    HttpResult r;

    if (!ensure_sockets()) {
        r.error = "socket init failed";
        return r;
    }
    if (host.empty()) {
        r.error = "empty host";
        return r;
    }

    // 解析地址
    addrinfo hints;
    std::memset(&hints, 0, sizeof(hints));
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;
    hints.ai_protocol = IPPROTO_TCP;

    char portbuf[16];
    std::snprintf(portbuf, sizeof(portbuf), "%u", static_cast<unsigned>(port));

    addrinfo* res = nullptr;
    const int gai = ::getaddrinfo(host.c_str(), portbuf, &hints, &res);
    if (gai != 0 || res == nullptr) {
        r.error = "resolve failed: " + host;
        return r;
    }

    socket_t s = ::socket(res->ai_family, res->ai_socktype, res->ai_protocol);
    if (s == AGENT_INVALID_SOCKET) {
        ::freeaddrinfo(res);
        r.error = "socket() failed";
        return r;
    }

    if (!connect_with_timeout(s, res->ai_addr, static_cast<int>(res->ai_addrlen), timeout_ms)) {
        AGENT_CLOSE_SOCKET(s);
        ::freeaddrinfo(res);
        r.error = "connect failed (is the server running?)";
        return r;
    }
    ::freeaddrinfo(res);

    // HTTP/1.0 + Connection: close —— 响应体长度靠对端关闭连接确定,
    // 省掉 chunked / content-length 两种分支, 对这种小响应足够。
    std::string req;
    req += method + " " + path + " HTTP/1.0\r\n";
    req += "Host: " + host + ":" + portbuf + "\r\n";
    req += "User-Agent: agent-native/0.1\r\n";
    req += "Accept: */*\r\n";
    if (method == "POST") {
        req += "Content-Length: 0\r\n";
    }
    req += "Connection: close\r\n\r\n";

    // 发送
    std::size_t sent = 0;
    while (sent < req.size()) {
        const int n = ::send(s, req.data() + sent, static_cast<int>(req.size() - sent), 0);
        if (n <= 0) {
            AGENT_CLOSE_SOCKET(s);
            r.error = "send failed";
            return r;
        }
        sent += static_cast<std::size_t>(n);
    }

    // 接收
    std::string resp;
    char buf[4096];
    for (;;) {
        const int n = ::recv(s, buf, sizeof(buf), 0);
        if (n > 0) {
            resp.append(buf, static_cast<std::size_t>(n));
            if (resp.size() > 1024 * 1024) {
                break;  // 防御: 响应不该这么大
            }
        } else {
            break;  // 0 = 对端关闭, <0 = 超时/错误
        }
    }
    AGENT_CLOSE_SOCKET(s);

    if (resp.empty()) {
        r.error = "empty response (server accepted but sent nothing)";
        return r;
    }

    r.connected = true;

    // 拆状态行与 body
    const std::size_t hdr_end = resp.find("\r\n\r\n");
    if (hdr_end == std::string::npos) {
        r.body = resp;
        return r;
    }
    // 注意: Sunshine 的一些错误响应是**只发 XML 不发状态行**的,
    // 这种情况下把整个响应体当 body 处理。
    if (resp.compare(0, 5, "HTTP/") == 0) {
        const std::size_t sp1 = resp.find(' ');
        if (sp1 != std::string::npos) {
            r.status_code = std::atoi(resp.c_str() + sp1 + 1);
        }
        r.body = resp.substr(hdr_end + 4);
    } else {
        r.body = resp;
    }
    return r;
}

}  // namespace

std::string extract_tag(const std::string& xml, const std::string& tag) {
    // 找 "<tag>" 或 "<tag ...>" (后者带属性, 例如
    // <root status_code="400" status_message="...">)
    std::string open = "<" + tag;
    std::size_t a = xml.find(open);
    while (a != std::string::npos) {
        const std::size_t after = a + open.size();
        // 紧跟 '>' 或空白才算命中这个标签名; 否则可能是 <tagfoo>
        if (after < xml.size() &&
            (xml[after] == '>' || xml[after] == ' ' || xml[after] == '\t' ||
             xml[after] == '\r' || xml[after] == '\n')) {
            break;
        }
        a = xml.find(open, a + 1);
    }
    if (a == std::string::npos) {
        return {};
    }
    const std::size_t gt = xml.find('>', a);
    if (gt == std::string::npos) {
        return {};
    }
    const std::string close = "</" + tag + ">";
    const std::size_t b = xml.find(close, gt + 1);
    if (b == std::string::npos) {
        return {};
    }
    return xml.substr(gt + 1, b - (gt + 1));
}

std::string extract_attribute(const std::string& xml, const std::string& name) {
    // 找 name 后面紧跟可选的空白和 '='
    std::size_t a = xml.find(name);
    while (a != std::string::npos) {
        std::size_t i = a + name.size();
        while (i < xml.size() && (xml[i] == ' ' || xml[i] == '\t')) {
            ++i;
        }
        if (i < xml.size() && xml[i] == '=') {
            ++i;
            while (i < xml.size() && (xml[i] == ' ' || xml[i] == '\t')) {
                ++i;
            }
            if (i < xml.size() && (xml[i] == '"' || xml[i] == '\'')) {
                const char quote = xml[i];
                const std::size_t start = i + 1;
                const std::size_t end = xml.find(quote, start);
                if (end != std::string::npos) {
                    return xml.substr(start, end - start);
                }
                return {};
            }
        }
        a = xml.find(name, a + 1);
    }
    return {};
}

int extract_status_code(const std::string& xml) {
    const std::size_t a = xml.find("status_code=");
    if (a == std::string::npos) {
        return -1;
    }
    std::size_t i = a + 12;
    // 跳过引号
    if (i < xml.size() && (xml[i] == '"' || xml[i] == '\'')) {
        ++i;
    }
    int v = 0;
    bool any = false;
    while (i < xml.size() && xml[i] >= '0' && xml[i] <= '9') {
        v = v * 10 + (xml[i] - '0');
        ++i;
        any = true;
    }
    return any ? v : -1;
}

void split_host_port(const std::string& in, std::string& host, std::uint16_t& port) {
    const std::size_t colon = in.rfind(':');
    if (colon == std::string::npos) {
        host = in;
        return;
    }
    // 多个冒号 => IPv6 字面量 (fe80::1), 不能按 host:port 拆
    if (in.find(':') != colon) {
        host = in;
        return;
    }
    // 只有 "host:port" 形态才拆; 端口必须是纯数字
    const std::string maybe_port = in.substr(colon + 1);
    if (maybe_port.empty()) {
        host = in.substr(0, colon);
        return;
    }
    for (char c : maybe_port) {
        if (c < '0' || c > '9') {
            host = in;  // 不是端口, 原样返回
            return;
        }
    }
    host = in.substr(0, colon);
    const long p = std::strtol(maybe_port.c_str(), nullptr, 10);
    if (p > 0 && p <= 65535) {
        port = static_cast<std::uint16_t>(p);
    }
}

HttpResult http_get(const std::string& host, std::uint16_t port, const std::string& path,
                    int timeout_ms) {
    return do_request("GET", host, port, path, timeout_ms);
}

HttpResult http_post(const std::string& host, std::uint16_t port, const std::string& path,
                     int timeout_ms) {
    return do_request("POST", host, port, path, timeout_ms);
}

ServerInfo fetch_server_info(const std::string& host_in, std::uint16_t port, int timeout_ms) {
    ServerInfo si;

    std::string host = host_in;
    split_host_port(host_in, host, port);

    const HttpResult r = http_get(host, port, "/serverinfo?uniqueid=0123456789ABCDEF",
                                  timeout_ms);
    si.raw = r.body;

    if (!r.connected) {
        return si;  // ok 保持 false
    }

    const int sc = extract_status_code(r.body);
    si.status_code = (sc >= 0) ? sc : r.status_code;
    if (si.status_code != 200) {
        return si;
    }

    si.hostname = extract_tag(r.body, "hostname");
    si.app_version = extract_tag(r.body, "appversion");
    si.gfe_version = extract_tag(r.body, "GfeVersion");
    si.unique_id = extract_tag(r.body, "uniqueid");

    const std::string codec = extract_tag(r.body, "ServerCodecModeSupport");
    si.codec_mode_support = codec.empty()
                                ? 0u
                                : static_cast<std::uint32_t>(std::strtoul(codec.c_str(), nullptr, 10));

    const std::string pair = extract_tag(r.body, "PairStatus");
    si.pair_status = pair.empty() ? -1 : std::atoi(pair.c_str());

    // appversion 是 LiStartConnection 必需的; 没有它握手没意义
    si.ok = !si.app_version.empty();
    return si;
}

LaunchResult launch_app(const std::string& host_in,
                        const std::string& app_id,
                        const std::string& app_name,
                        const std::string& mode,
                        const std::string& unique_id,
                        std::uint16_t port,
                        int timeout_ms) {
    LaunchResult lr;

    std::string host = host_in;
    split_host_port(host_in, host, port);

    // 先试 /launch, 失败再试 /resume (已运行的会话走 resume)
    for (const char* endpoint : {"/launch", "/resume"}) {
        std::string path = std::string(endpoint) + "?uniqueid=" + unique_id;
        if (!app_id.empty()) {
            path += "&appid=" + app_id;
        }
        if (!app_name.empty()) {
            path += "&appname=" + app_name;
        }
        if (!mode.empty()) {
            path += "&mode=" + mode;
        }

        const HttpResult r = http_post(host, port, path, timeout_ms);
        lr.raw = r.body;
        if (!r.connected) {
            continue;
        }

        const int sc = extract_status_code(r.body);
        lr.status_code = (sc >= 0) ? sc : r.status_code;
        lr.status_message = extract_attribute(r.body, "status_message");

        if (lr.status_code == 200) {
            lr.session_url = extract_tag(r.body, "sessionUrl0");
            // 有的版本字段名是全小写
            if (lr.session_url.empty()) {
                lr.session_url = extract_tag(r.body, "sessionurl0");
            }
            lr.ok = !lr.session_url.empty();
            return lr;
        }
        // 401/403 之类是明确拒绝, 不用再试另一个端点
        if (lr.status_code != 404) {
            return lr;
        }
    }
    return lr;
}

}  // namespace moonlight_connection
}  // namespace agent
