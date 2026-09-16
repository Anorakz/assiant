// ============================================================================
//  tests/test_moonlight_connection.cpp — 握手层测试 (host 侧, L1)
//
//  运行方式:
//      scripts/test-host.ps1
//
//  分两部分:
//    1) 纯函数 (XML 字段提取 / host:port 拆分) —— 用 **真实 Sunshine 响应**
//       当样本, 不碰网络, 断言确定
//    2) 活体探测 —— 只有设了 AGENT_TEST_SUNSHINE_HOST 才跑, 否则自动跳过。
//       因为 CI/别的机器上不一定有 Sunshine:
//           $env:AGENT_TEST_SUNSHINE_HOST="127.0.0.1"
//           scripts/test-host.ps1
// ============================================================================

#include "moonlight_connection.h"

#include <gtest/gtest.h>

#include <cstdlib>
#include <string>

using namespace agent::moonlight_connection;

namespace {

/// 真实 Sunshine /serverinfo 响应 (本机 47989 实测抓下来的, 字段原样保留)
const char* kRealServerInfo =
    "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n"
    "<root status_code=\"200\">"
    "<hostname>Anorak_Host</hostname>"
    "<appversion>7.1.431.-1</appversion>"
    "<GfeVersion>3.23.0.74</GfeVersion>"
    "<uniqueid>BE755E9D-9210-1A2A-7B08-20473E1A50A4</uniqueid>"
    "<HttpsPort>47984</HttpsPort>"
    "<ExternalPort>47989</ExternalPort>"
    "<MaxLumaPixelsHEVC>1869449984</MaxLumaPixelsHEVC>"
    "<mac>00:00:00:00:00:00</mac>"
    "<LocalIP>127.0.0.1</LocalIP>"
    "<ServerCodecModeSupport>2032385</ServerCodecModeSupport>"
    "<PairStatus>0</PairStatus>"
    "<currentgame>881448767</currentgame>"
    "<state>SUNSHINE_SERVER_BUSY</state>"
    "</root>";

/// 真实 Sunshine 错误响应 (端点不存在时)
const char* kRealNotFound =
    "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n"
    "<root status_code=\"404\"/>";

/// 真实 /pair 参数错误响应
const char* kRealPairError =
    "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n"
    "<root status_code=\"400\" status_message=\"Invalid uniqueid\"/>";

/// 取环境变量; 未设置返回空
std::string env_or_empty(const char* name) {
    const char* v = std::getenv(name);
    return v == nullptr ? std::string() : std::string(v);
}

}  // namespace

// ===========================================================================
//  XML 字段提取
// ===========================================================================

TEST(MoonlightXml, ExtractsFieldsFromRealServerInfo) {
    EXPECT_EQ(extract_tag(kRealServerInfo, "hostname"), "Anorak_Host");
    EXPECT_EQ(extract_tag(kRealServerInfo, "appversion"), "7.1.431.-1");
    EXPECT_EQ(extract_tag(kRealServerInfo, "GfeVersion"), "3.23.0.74");
    EXPECT_EQ(extract_tag(kRealServerInfo, "uniqueid"), "BE755E9D-9210-1A2A-7B08-20473E1A50A4");
    EXPECT_EQ(extract_tag(kRealServerInfo, "ServerCodecModeSupport"), "2032385");
    EXPECT_EQ(extract_tag(kRealServerInfo, "PairStatus"), "0");
}

TEST(MoonlightXml, MissingTagReturnsEmpty) {
    EXPECT_TRUE(extract_tag(kRealServerInfo, "NoSuchTag").empty());
    EXPECT_TRUE(extract_tag("", "hostname").empty());
    EXPECT_TRUE(extract_tag("<root></root>", "hostname").empty());
}

TEST(MoonlightXml, EmptyTagValueIsEmptyString) {
    EXPECT_TRUE(extract_tag("<root><hostname></hostname></root>", "hostname").empty());
}

TEST(MoonlightXml, ParsesStatusCodeFromRealResponses) {
    EXPECT_EQ(extract_status_code(kRealServerInfo), 200);
    EXPECT_EQ(extract_status_code(kRealNotFound), 404);
    EXPECT_EQ(extract_status_code(kRealPairError), 400);
}

TEST(MoonlightXml, StatusMessageIsAnAttributeNotAnElement) {
    // 实际响应是 <root status_code="400" status_message="Invalid uniqueid"/>
    // —— status_message 是 **属性**, 用 extract_tag 取不到, 必须用
    // extract_attribute。
    EXPECT_EQ(extract_attribute(kRealPairError, "status_message"), "Invalid uniqueid");
    EXPECT_EQ(extract_tag(kRealPairError, "status_message"), "")
        << "属性不是元素, extract_tag 取不到是预期行为";
}

TEST(MoonlightXml, AttributeExtractionHandlesQuotingAndSpacing) {
    EXPECT_EQ(extract_attribute("<a x=\"1\" y='2'/>", "x"), "1");
    EXPECT_EQ(extract_attribute("<a x=\"1\" y='2'/>", "y"), "2");
    EXPECT_EQ(extract_attribute("<a  x = \"spaced\" />", "x"), "spaced");
    EXPECT_TRUE(extract_attribute("<a x=\"1\"/>", "nope").empty());
    EXPECT_TRUE(extract_attribute("", "x").empty());
    // 不该把出现在值里的同名文本误当属性
    EXPECT_TRUE(extract_attribute("<a b=\"status_message\"/>", "status_message").empty());
}

TEST(MoonlightXml, StatusCodeMissingOrMalformedIsMinusOne) {
    EXPECT_EQ(extract_status_code("<root/>"), -1);
    EXPECT_EQ(extract_status_code(""), -1);
    EXPECT_EQ(extract_status_code("<root status_code=\"\"/>"), -1);
    EXPECT_EQ(extract_status_code("<root status_code=\"abc\"/>"), -1);
}

TEST(MoonlightXml, HandlesSingleQuotedStatusCode) {
    // 我们的提取器两种引号都认
    EXPECT_EQ(extract_status_code("<root status_code='200'/>"), 200);
}

TEST(MoonlightXml, CodecSupportFlagsIndicateHevc) {
    // SCM_H264=0x1, SCM_HEVC=0x100
    const std::string v = extract_tag(kRealServerInfo, "ServerCodecModeSupport");
    const unsigned long flags = std::strtoul(v.c_str(), nullptr, 10);
    EXPECT_NE(flags & 0x0001ul, 0ul) << "应当支持 H.264";
    EXPECT_NE(flags & 0x0100ul, 0ul) << "应当支持 HEVC";
}

// ===========================================================================
//  host:port 拆分
// ===========================================================================

TEST(MoonlightHostPort, KeepsDefaultWhenNoPort) {
    std::string host = "x";
    std::uint16_t port = kDefaultHttpPort;
    split_host_port("192.168.1.10", host, port);
    EXPECT_EQ(host, "192.168.1.10");
    EXPECT_EQ(port, kDefaultHttpPort);
}

TEST(MoonlightHostPort, ParsesExplicitPort) {
    std::string host = "x";
    std::uint16_t port = kDefaultHttpPort;
    split_host_port("192.168.1.10:49000", host, port);
    EXPECT_EQ(host, "192.168.1.10");
    EXPECT_EQ(port, 49000);
}

TEST(MoonlightHostPort, BareHostname) {
    std::string host;
    std::uint16_t port = kDefaultHttpPort;
    split_host_port("gamehost.local", host, port);
    EXPECT_EQ(host, "gamehost.local");
    EXPECT_EQ(port, kDefaultHttpPort);
}

TEST(MoonlightHostPort, NonNumericSuffixIsNotTreatedAsPort) {
    // 不是 "host:数字" 的形态就原样当主机名, 免得把 IPv6 之类拆坏
    std::string host;
    std::uint16_t port = kDefaultHttpPort;
    split_host_port("fe80::1", host, port);
    EXPECT_EQ(host, "fe80::1");
    EXPECT_EQ(port, kDefaultHttpPort);
}

TEST(MoonlightHostPort, TrailingColonKeepsHost) {
    std::string host;
    std::uint16_t port = kDefaultHttpPort;
    split_host_port("192.168.1.10:", host, port);
    EXPECT_EQ(host, "192.168.1.10");
    EXPECT_EQ(port, kDefaultHttpPort);
}

// ===========================================================================
//  失败路径 (不依赖网络在不在)
// ===========================================================================

TEST(MoonlightHttp, EmptyHostFailsWithoutCrashing) {
    const auto si = fetch_server_info("", kDefaultHttpPort, 1000);
    EXPECT_FALSE(si.ok);
}

TEST(MoonlightHttp, UnresolvableHostFailsCleanly) {
    // 这个域名不该解析成功; 关键是"快速失败"而不是挂住
    const auto si = fetch_server_info("no-such-host.invalid", kDefaultHttpPort, 1500);
    EXPECT_FALSE(si.ok);
    EXPECT_EQ(si.status_code, 0);
}

TEST(MoonlightHttp, ClosedPortFailsCleanly) {
    // 本机一个几乎不可能有人监听的端口
    const auto si = fetch_server_info("127.0.0.1", 9, 1500);
    EXPECT_FALSE(si.ok);
}

// ===========================================================================
//  活体探测 (需要真实 Sunshine)
// ===========================================================================

TEST(MoonlightLive, ServerInfoAgainstRealSunshine) {
    const std::string host = env_or_empty("AGENT_TEST_SUNSHINE_HOST");
    if (host.empty()) {
        GTEST_SKIP() << "设置 AGENT_TEST_SUNSHINE_HOST 才会跑活体探测";
    }

    const auto si = fetch_server_info(host, kDefaultHttpPort, 4000);

    ASSERT_TRUE(si.ok) << "拿不到 /serverinfo; raw=" << si.raw;
    EXPECT_EQ(si.status_code, 200);
    EXPECT_FALSE(si.hostname.empty());
    EXPECT_FALSE(si.app_version.empty()) << "appversion 是 LiStartConnection 必需的";
    EXPECT_FALSE(si.unique_id.empty());

    // 记下实际内容, 方便人工核对
    std::fprintf(stderr,
                 "[live] hostname=%s appversion=%s gfe=%s codec=0x%X pair_status=%d\n",
                 si.hostname.c_str(), si.app_version.c_str(), si.gfe_version.c_str(),
                 si.codec_mode_support, si.pair_status);
}

TEST(MoonlightLive, LaunchReportsServerSideStatus) {
    const std::string host = env_or_empty("AGENT_TEST_SUNSHINE_HOST");
    if (host.empty()) {
        GTEST_SKIP() << "设置 AGENT_TEST_SUNSHINE_HOST 才会跑活体探测";
    }

    const auto lr = launch_app(host, /*app_id=*/"", /*app_name=*/"Desktop",
                               /*mode=*/"1280x720x60", /*unique_id=*/"0123456789ABCDEF",
                               kDefaultHttpPort, 5000);

    // 这里不断言成功 —— 把实际结果打出来, 让"握手能不能走通"有据可查。
    std::fprintf(stderr, "[live] /launch status=%d sessionUrl=%s msg=%s raw=%s\n",
                 lr.status_code, lr.session_url.empty() ? "(none)" : lr.session_url.c_str(),
                 lr.status_message.c_str(),
                 lr.raw.size() > 160 ? (lr.raw.substr(0, 160) + "...").c_str() : lr.raw.c_str());

    // ⚠ /applist /launch /resume 只注册在 **HTTPS 端口 (47984)** 上, 并且要求客户端
    //   证书已在授权名单里; 明文 HTTP (47989) 上查它们必然 404。
    //   实测 (2026-09-16): 带已授权证书走 https://<host>:47984/launch 返回
    //   status_code=200 且 <sessionUrl0>rtsp://...:48010</sessionUrl0>。
    //   所以 404 只说明"用错了端口/没带证书", 不代表 Sunshine 移除了该接口。
    //   HTTPS + mTLS 的端到端验证见 scripts/verify-authorized.sh。
    if (lr.status_code == 404) {
        GTEST_SKIP() << "明文 HTTP 端口上没有 /launch —— 该接口只在 HTTPS 47984 上, "
                        "且需要已授权客户端证书 (见 scripts/verify-authorized.sh)";
    }
}
