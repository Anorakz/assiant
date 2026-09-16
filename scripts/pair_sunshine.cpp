// ============================================================================
//  scripts/pair_sunshine.cpp — 一次性 SRSAES 配对客户端
//
//  为什么需要它
//  ---------------------------------------------------------------------------
//  moonlight-common-c **不做配对**, 而 Sunshine 的 GameStream 端点
//  (/applist, /launch, /resume) 只注册在 HTTPS 服务端上, 且要求客户端证书
//  已在"已配对客户端"名单里 —— 否则一律 401 Certificate verification failed。
//  所以上线前必须先用一个带密码学的工具把客户端证书配上。
//
//  配对流程 (对照 LizardByte/Sunshine master src/nvhttp.cpp)
//  ---------------------------------------------------------------------------
//    step1  GET /pair?phrase=getservercert&clientcert=<hex DER>&salt=<32hex>
//              &devicename=<name>&uniqueid=<32hex>
//           → {"plaincert": <服务端证书 hex DER>, "paired": 1}
//           服务端在此 **阻塞**, 等操作员在 Sunshine UI/托盘 里输入 PIN 并批准。
//    step2  GET /pair?clientchallenge=<hex>
//           AES-128-ECB 加密我们生成的 16 字节挑战 (密钥 = SHA256(salt||pin)[:16])
//           → {"challengeresponse": <hex>}
//    step3  GET /pair?serverchallengeresp=<hex>
//           → {"pairingsecret": <hex>, "paired": 1}
//    step4  GET /pair?clientpairingsecret=<hex(secret||RSA-SHA256(serverchallenge))>
//           → {"paired": 1} 并把我们的证书加入授权名单
//
//  ⚠ PIN 是 **客户端选定** 的 4 位数字, 由操作员在 Sunshine 里输入 —— 不是
//    Sunshine 生成给客户端。两边必须一致, 因为它是 AES 密钥的派生材料。
//
//  构建与运行 (WSL, 需要 libssl-dev)
//  ---------------------------------------------------------------------------
//      g++ -std=c++17 -O2 -o scripts/pair_sunshine scripts/pair_sunshine.cpp -lssl -lcrypto
//      scripts/pair_sunshine <host> <clientcert.pem> <clientkey.pem> <pin> [port] [uniqueid]
// ============================================================================

#include <openssl/aes.h>
#include <openssl/evp.h>
#include <openssl/pem.h>
#include <openssl/rand.h>
#include <openssl/rsa.h>
#include <openssl/sha.h>
#include <openssl/x509.h>

#include <arpa/inet.h>
#include <netdb.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

namespace {

using Bytes = std::vector<unsigned char>;

// ------------------------------------------------------------------ hex ------
std::string to_hex(const Bytes& b) {
    static const char* d = "0123456789abcdef";
    std::string s;
    s.reserve(b.size() * 2);
    for (unsigned char c : b) {
        s.push_back(d[c >> 4]);
        s.push_back(d[c & 0x0F]);
    }
    return s;
}

Bytes from_hex(const std::string& s) {
    Bytes b;
    for (std::size_t i = 0; i + 1 < s.size(); i += 2) {
        const char a = s[i];
        const char c = s[i + 1];
        auto nib = [](char ch) -> int {
            if (ch >= '0' && ch <= '9') return ch - '0';
            if (ch >= 'a' && ch <= 'f') return ch - 'a' + 10;
            if (ch >= 'A' && ch <= 'F') return ch - 'A' + 10;
            return -1;
        };
        const int hi = nib(a);
        const int lo = nib(c);
        if (hi < 0 || lo < 0) {
            std::fprintf(stderr, "[from_hex] 非法字符在 offset %zu: 0x%02X 0x%02X\n", i,
                         static_cast<unsigned char>(a), static_cast<unsigned char>(c));
            return {};
        }
        b.push_back(static_cast<unsigned char>((hi << 4) | lo));
    }
    return b;
}

/// 打印前 n 字节的 hex, 便于诊断
void dump(const char* label, const Bytes& b, std::size_t n = 16) {
    std::fprintf(stderr, "  [dump] %s (%zu bytes):", label, b.size());
    for (std::size_t i = 0; i < b.size() && i < n; ++i) {
        std::fprintf(stderr, " %02x", b[i]);
    }
    std::fprintf(stderr, "%s\n", b.size() > n ? " ..." : "");
}

/// 从 XML 里取 <tag>value</tag>
std::string xml_tag(const std::string& xml, const std::string& tag) {
    const std::string open = "<" + tag;
    std::size_t a = xml.find(open);
    while (a != std::string::npos) {
        const std::size_t after = a + open.size();
        if (after < xml.size() && (xml[after] == '>' || xml[after] == ' ')) {
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
    const std::size_t b = xml.find("</" + tag + ">", gt + 1);
    if (b == std::string::npos) {
        return {};
    }
    return xml.substr(gt + 1, b - (gt + 1));
}

// ------------------------------------------------------------------ HTTP -----
struct HttpResult {
    bool ok = false;
    std::string body;
    std::string error;
};

HttpResult http_get(const std::string& host, unsigned short port, const std::string& path,
                    int timeout_ms) {
    HttpResult r;

    addrinfo hints{};
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;
    addrinfo* res = nullptr;
    char portbuf[16];
    std::snprintf(portbuf, sizeof(portbuf), "%u", static_cast<unsigned>(port));
    if (::getaddrinfo(host.c_str(), portbuf, &hints, &res) != 0 || res == nullptr) {
        r.error = "resolve failed";
        return r;
    }

    int fd = ::socket(res->ai_family, res->ai_socktype, res->ai_protocol);
    if (fd < 0) {
        ::freeaddrinfo(res);
        r.error = "socket failed";
        return r;
    }

    timeval tv{};
    tv.tv_sec = timeout_ms / 1000;
    tv.tv_usec = (timeout_ms % 1000) * 1000;
    ::setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    ::setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));

    if (::connect(fd, res->ai_addr, res->ai_addrlen) != 0) {
        ::close(fd);
        ::freeaddrinfo(res);
        r.error = "connect failed";
        return r;
    }
    ::freeaddrinfo(res);

    std::string req = "GET " + path + " HTTP/1.0\r\nHost: " + host + ":" + portbuf +
                      "\r\nAccept: */*\r\nConnection: close\r\n\r\n";
    std::size_t sent = 0;
    while (sent < req.size()) {
        const ssize_t n = ::send(fd, req.data() + sent, req.size() - sent, 0);
        if (n <= 0) {
            ::close(fd);
            r.error = "send failed";
            return r;
        }
        sent += static_cast<std::size_t>(n);
    }

    std::string resp;
    char buf[4096];
    for (;;) {
        const ssize_t n = ::recv(fd, buf, sizeof(buf), 0);
        if (n > 0) {
            resp.append(buf, static_cast<std::size_t>(n));
        } else {
            break;
        }
    }
    ::close(fd);

    if (resp.empty()) {
        r.error = "empty response";
        return r;
    }
    r.ok = true;
    const std::size_t he = resp.find("\r\n\r\n");
    r.body = (he == std::string::npos) ? resp : resp.substr(he + 4);
    return r;
}

// ------------------------------------------------------------------ crypto ---
Bytes sha256(const Bytes& data) {
    Bytes h(SHA256_DIGEST_LENGTH);
    unsigned int len = 0;
    EVP_Digest(data.data(), data.size(), h.data(), &len, EVP_sha256(), nullptr);
    h.resize(len);
    return h;
}

Bytes rand_bytes(std::size_t n) {
    Bytes b(n);
    if (RAND_bytes(b.data(), static_cast<int>(n)) != 1) {
        std::fprintf(stderr, "FATAL: RAND_bytes failed\n");
        std::exit(3);
    }
    return b;
}

/// AES-128-ECB, 无填充 (配对协议要求长度已经是 16 的倍数)
Bytes aes_ecb(const Bytes& key, const Bytes& in, bool encrypt) {
    EVP_CIPHER_CTX* ctx = EVP_CIPHER_CTX_new();
    Bytes out(in.size() + 32);
    int len1 = 0;
    int len2 = 0;
    EVP_CipherInit_ex(ctx, EVP_aes_128_ecb(), nullptr, key.data(), nullptr, encrypt ? 1 : 0);
    EVP_CIPHER_CTX_set_padding(ctx, 0);
    EVP_CipherUpdate(ctx, out.data(), &len1, in.data(), static_cast<int>(in.size()));
    EVP_CipherFinal_ex(ctx, out.data() + len1, &len2);
    EVP_CIPHER_CTX_free(ctx);
    out.resize(static_cast<std::size_t>(len1 + len2));
    return out;
}

/// 取证书的签名字节 (Sunshine 的 crypto::signature 就是取 X509 的签名字段)
Bytes cert_signature_of(X509* x) {
    Bytes sig;
    const ASN1_BIT_STRING* bs = nullptr;
    const X509_ALGOR* alg = nullptr;
    X509_get0_signature(&bs, &alg, x);
    if (bs != nullptr && bs->data != nullptr && bs->length > 0) {
        sig.assign(bs->data, bs->data + bs->length);
    }
    return sig;
}

/// RSA-SHA256 签名 (Sunshine 的 sign256)
Bytes rsa_sign_sha256(EVP_PKEY* key, const Bytes& data) {
    EVP_MD_CTX* ctx = EVP_MD_CTX_new();
    Bytes sig;
    if (EVP_DigestSignInit(ctx, nullptr, EVP_sha256(), nullptr, key) == 1 &&
        EVP_DigestSignUpdate(ctx, data.data(), data.size()) == 1) {
        std::size_t n = 0;
        if (EVP_DigestSignFinal(ctx, nullptr, &n) == 1) {
            sig.resize(n);
            EVP_DigestSignFinal(ctx, sig.data(), &n);
            sig.resize(n);
        }
    }
    EVP_MD_CTX_free(ctx);
    return sig;
}

// ------------------------------------------------------------------ evidence -
// 把每一步的原始字节落盘 (hex), 便于用 openssl/sha256sum 独立复核, 而不是
// 只相信本程序自己的结论。
std::string g_dump_dir;

void dump_hex(const std::string& name, const Bytes& b) {
    if (g_dump_dir.empty()) {
        return;
    }
    const std::string path = g_dump_dir + "/" + name + ".hex";
    FILE* f = std::fopen(path.c_str(), "w");
    if (f == nullptr) {
        std::fprintf(stderr, "[dump] 无法写入 %s\n", path.c_str());
        return;
    }
    const std::string h = to_hex(b);
    std::fwrite(h.data(), 1, h.size(), f);
    std::fputc('\n', f);
    std::fclose(f);
}

/// 从 **PEM 文本** 解析 X509
/// 注意: Sunshine 的 plaincert 是 hex 编码的 **PEM 文本**;
///       我们发给它的 clientcert 同样必须是 hex 编码的 PEM 文本 (crypto::x509()
///       用的是 PEM_read_bio_X509)。两个方向格式一致, 都是 PEM 的 hex。
X509* cert_from_pem(const std::string& pem) {
    BIO* bio = BIO_new_mem_buf(pem.data(), static_cast<int>(pem.size()));
    if (bio == nullptr) {
        return nullptr;
    }
    X509* x = PEM_read_bio_X509(bio, nullptr, nullptr, nullptr);
    BIO_free(bio);
    return x;
}

X509* load_cert(const char* path) {
    FILE* f = std::fopen(path, "r");
    if (f == nullptr) {
        return nullptr;
    }
    X509* x = PEM_read_X509(f, nullptr, nullptr, nullptr);
    std::fclose(f);
    return x;
}

EVP_PKEY* load_key(const char* path) {
    FILE* f = std::fopen(path, "r");
    if (f == nullptr) {
        return nullptr;
    }
    EVP_PKEY* k = PEM_read_PrivateKey(f, nullptr, nullptr, nullptr);
    std::fclose(f);
    return k;
}

/// AES 密钥 = SHA256(salt || pin)[:16]
Bytes aes_key_from(const Bytes& salt, const std::string& pin) {
    Bytes sp = salt;
    sp.insert(sp.end(), pin.begin(), pin.end());
    Bytes h = sha256(sp);
    h.resize(16);
    return h;
}

void die(const std::string& msg) {
    std::fprintf(stderr, "FAIL: %s\n", msg.c_str());
    std::exit(1);
}

std::string url_encode(const std::string& s) {
    static const char* d = "0123456789ABCDEF";
    std::string o;
    for (unsigned char c : s) {
        if ((c >= '0' && c <= '9') || (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
            c == '-' || c == '_' || c == '.' || c == '~') {
            o.push_back(static_cast<char>(c));
        } else {
            o.push_back('%');
            o.push_back(d[c >> 4]);
            o.push_back(d[c & 0x0F]);
        }
    }
    return o;
}

}  // namespace

int main(int argc, char** argv) {
    if (argc < 5) {
        std::fprintf(stderr,
                     "usage: %s <host> <clientcert.pem> <clientkey.pem> <pin> [port] [uniqueid]\n"
                     "  pin: 4 位数字, 需要你在 Sunshine 界面里输入同样的值\n",
                     argv[0]);
        return 2;
    }
    const std::string host = argv[1];
    const char* cert_path = argv[2];
    const char* key_path = argv[3];
    const std::string pin = argv[4];
    const unsigned short port =
        (argc > 5) ? static_cast<unsigned short>(std::atoi(argv[5])) : 47989;
    const std::string unique_id = (argc > 6) ? argv[6] : "0123456789ABCDEF0123456789ABCDEF";
    const std::string devicename = "agent-native";
    if (const char* d = ::getenv("PAIR_DUMP_DIR")) {
        g_dump_dir = d;
        std::printf("[pair] 证据目录 = %s\n", g_dump_dir.c_str());
    }

    if (pin.size() != 4) {
        die("pin 必须是 4 位数字 (Sunshine 的 is_valid_pairing_pin 要求正好 4 位)");
    }

    X509* cert = load_cert(cert_path);
    if (cert == nullptr) {
        die("无法读取客户端证书");
    }
    EVP_PKEY* key = load_key(key_path);
    if (key == nullptr) {
        die("无法读取客户端私钥");
    }

    // 证书转 hex DER (Sunshine 用 util::from_hex_vec 解析 clientcert)
    Bytes der;
    {
        unsigned char* p = nullptr;
        const int n = i2d_X509(cert, &p);
        if (n > 0 && p != nullptr) {
            der.assign(p, p + n);
            OPENSSL_free(p);
        }
    }
    if (der.empty()) {
        die("i2d_X509 失败");
    }
    // 证书转 PEM 文本, 再取它的 hex —— Sunshine 的 crypto::x509() 用
    // PEM_read_bio_X509 解析 clientcert, 所以这里必须是 **PEM 的 hex**, 不是 DER。
    // (之前发 DER 的 hex, 服务端解析失败, 表现为 step4 "Invalid client certificate"。)
    Bytes cert_pem;
    {
        BIO* bio = BIO_new(BIO_s_mem());
        if (bio != nullptr && PEM_write_bio_X509(bio, cert) == 1) {
            char* p = nullptr;
            const long n = BIO_get_mem_data(bio, &p);
            if (n > 0 && p != nullptr) {
                cert_pem.assign(reinterpret_cast<unsigned char*>(p),
                                reinterpret_cast<unsigned char*>(p) + n);
            }
        }
        if (bio != nullptr) {
            BIO_free(bio);
        }
    }
    if (cert_pem.empty()) {
        die("证书转 PEM 失败");
    }
    std::printf("[pair] client cert: %zu bytes DER, %zu bytes PEM (clientcert 参数发 PEM 的 hex)\n",
                der.size(), cert_pem.size());
    {
        Bytes csig = cert_signature_of(cert);
        Bytes chash = sha256(der);
        std::printf("[pair] 我方证书签名 %zu 字节, 签名 SHA256=%s\n", csig.size(),
                    to_hex(sha256(csig)).c_str());
        std::printf("[pair] 我方证书 DER SHA256=%s\n", to_hex(chash).c_str());
        dump_hex("01_client_cert_der", der);
        dump_hex("02_client_cert_signature", csig);
    }

    // salt 是 32 个 hex 字符 = 16 字节
    const Bytes salt = rand_bytes(16);
    const std::string salt_hex = to_hex(salt);
    const Bytes aes_key = aes_key_from(salt, pin);
    std::printf("[pair] salt=%s  (AES key = SHA256(salt||pin)[:16])\n", salt_hex.c_str());
    std::printf("[pair] PIN=\"%s\"  AES key=%s\n", pin.c_str(), to_hex(aes_key).c_str());
    dump_hex("03_salt", salt);
    dump_hex("04_aes_key", aes_key);

    // ---- step1: getservercert (会阻塞等你输 PIN) ----
    std::printf("\n[step1] 发起配对并等待你在 Sunshine 里输入 PIN=%s ...\n", pin.c_str());
    std::fflush(stdout);
    std::string path1 = "/pair?uniqueid=" + unique_id + "&phrase=getservercert&clientcert=" +
                        to_hex(cert_pem) + "&salt=" + salt_hex +
                        "&devicename=" + url_encode(devicename);
    HttpResult r1 = http_get(host, port, path1, 600000);
    if (!r1.ok) {
        die("step1 失败: " + r1.error);
    }
    const std::string plaincert = xml_tag(r1.body, "plaincert");
    if (plaincert.empty()) {
        die("step1 响应里没有 plaincert, 原始响应: " + r1.body);
    }
    std::printf("[step1] OK, plaincert hex=%zu chars\n", plaincert.size());
    // plaincert 是 hex 编码的 PEM 文本 (不是 DER!)
    const Bytes plaincert_bytes = from_hex(plaincert);
    const std::string server_pem(reinterpret_cast<const char*>(plaincert_bytes.data()),
                                 plaincert_bytes.size());
    std::printf("[step1] plaincert 解码后 %zu 字节, 开头: %.30s\n", plaincert_bytes.size(),
                server_pem.c_str());

    // ---- step2: clientchallenge ----
    // 证据: 把"我方发出的挑战"和"服务端用它算出的哈希"打印出来对比。
    // 服务端用 SHA256(我方挑战 ‖ 它自己的证书签名 ‖ serversecret) 作为回给我们的
    // 32 字节哈希。PIN 一致时, 这 32 字节是稳定的; 不一致时全是随机噪声。
    Bytes client_challenge = rand_bytes(16);
    dump("我方发出的 16 字节挑战 (明文)", client_challenge, 16);
    Bytes enc_challenge = aes_ecb(aes_key, client_challenge, /*encrypt=*/true);
    dump_hex("05_client_challenge", client_challenge);
    dump_hex("06_client_challenge_enc", enc_challenge);
    std::string path2 = "/pair?uniqueid=" + unique_id +
                        "&clientchallenge=" + to_hex(enc_challenge);
    HttpResult r2 = http_get(host, port, path2, 30000);
    if (!r2.ok) {
        die("step2 失败: " + r2.error);
    }
    const std::string resp_hex = xml_tag(r2.body, "challengeresponse");
    if (resp_hex.empty()) {
        die("step2 响应里没有 challengeresponse, 原始响应: " + r2.body);
    }
    Bytes dec_resp_raw = aes_ecb(aes_key, from_hex(resp_hex), /*encrypt=*/false);
    Bytes dec_resp = dec_resp_raw;  // 48 字节: [0,32) = clienthash, [32,48) = 服务端挑战
    std::printf("[step2] challengeresponse 密文=%zu 字节 -> 解密 %zu 字节 (服务端设计值 48 = SHA256 32 + 挑战 16)\n",
                from_hex(resp_hex).size(), dec_resp_raw.size());
    dump("服务端返回的前 32 字节 (即 clienthash)", dec_resp, 32);
    dump_hex("07_challengeresponse_raw", from_hex(resp_hex));
    dump_hex("08_challengeresponse_dec", dec_resp_raw);
    dump_hex("09_server_cert_pem", plaincert_bytes);
    if (dec_resp_raw.size() < 48) {
        die("challengeresponse 解密后不足 48 字节, 服务端响应异常");
    }

    // ---- step3: serverchallengeresp ----
    // plaincert 是 PEM 文本, 用 PEM 解析 (不是 d2i_X509)
    X509* scert = cert_from_pem(server_pem);
    if (scert == nullptr) {
        std::fprintf(stderr, "[step3] PEM_read_bio_X509 失败, plaincert %zu 字节\n",
                     plaincert_bytes.size());
        dump("plaincert 开头", plaincert_bytes, 32);
        die("无法解析服务端证书");
    }
    Bytes scert_sig = cert_signature_of(scert);
    if (scert_sig.empty()) {
        die("取服务端证书签名失败");
    }
    std::printf("[step3] 服务端证书签名 %zu 字节, 签名 SHA256=%s\n", scert_sig.size(),
                to_hex(sha256(scert_sig)).c_str());
    dump_hex("10_server_cert_signature", scert_sig);
    // 我方证书签名 —— phase 3/4 的哈希用的是**我方**证书签名
    Bytes client_sig = cert_signature_of(cert);
    if (client_sig.empty()) {
        die("取我方证书签名失败");
    }
    std::printf("[step3] 我方证书签名 %zu 字节, 签名 SHA256=%s\n", client_sig.size(),
                to_hex(sha256(client_sig)).c_str());
    // ---- step3: serverchallengeresp ----
    // 权威依据: moonlight-xboxog src/network/host_pairing.cpp
    //           send_server_challenge_response()
    //   clientSecretBytes = 16 随机字节          <-- ★ 必须在 phase 3 生成
    //   clientHashSource  = challengeresponse 明文[32:48]  (serverchallenge)
    //                     + **我方证书**签名 (256B)
    //                     + clientSecretBytes (16B)
    //   clientHash        = SHA256(clientHashSource)
    //   serverchallengeresp = AES-128-ECB-enc(clientHash, key)
    // 服务端 clientpairingsecret 用同一公式重算并与 step3 收到的内容比对, 且
    // step4 里的 client_secret 必须与 step3 用的是同一个 —— 这是之前失败的根因。
    const Bytes server_challenge_from_resp(dec_resp.begin() + 32, dec_resp.begin() + 48);
    Bytes client_secret = rand_bytes(16);
    Bytes h_in = server_challenge_from_resp;
    h_in.insert(h_in.end(), client_sig.begin(), client_sig.end());
    h_in.insert(h_in.end(), client_secret.begin(), client_secret.end());
    Bytes client_hash = sha256(h_in);
    Bytes enc_hash = aes_ecb(aes_key, client_hash, /*encrypt=*/true);
    dump_hex("11_serverchallengeresp_plain", client_hash);
    dump_hex("12_serverchallengeresp_enc", enc_hash);
    dump_hex("15_client_secret", client_secret);
    std::printf("[step3] serverchallenge=%s\n", to_hex(server_challenge_from_resp).c_str());
    std::printf("[step3] client_secret=%s  clientHash=%s\n", to_hex(client_secret).c_str(),
                to_hex(client_hash).c_str());

    std::string path3 = "/pair?uniqueid=" + unique_id +
                        "&serverchallengeresp=" + to_hex(enc_hash);
    HttpResult r3 = http_get(host, port, path3, 30000);
    if (!r3.ok) {
        die("step3 失败: " + r3.error);
    }
    const std::string pairingsecret_hex = xml_tag(r3.body, "pairingsecret");
    if (pairingsecret_hex.empty()) {
        die("step3 响应里没有 pairingsecret, 原始响应: " + r3.body);
    }
    Bytes pairingsecret = from_hex(pairingsecret_hex);
    std::printf("[step3] OK, 收到 pairingsecret (%zu bytes)\n", pairingsecret.size());

    // serverchallenge = pairingsecret 前 16 字节 (= sess.serversecret); 后 256 字节是服务端签名
    if (pairingsecret.size() < 16) {
        die("pairingsecret 太短");
    }
    Bytes server_challenge(pairingsecret.begin(), pairingsecret.begin() + 16);
    dump_hex("13_pairingsecret", pairingsecret);
    dump_hex("14_server_challenge", server_challenge);
    std::printf("[step4] pairingsecret[0:16]=%s (服务端 phase4 的会话字段)\n",
                to_hex(server_challenge).c_str());

    // ---- step4: clientpairingsecret ----
    // ★ 权威依据 (temp/pairing.c, Moonlight Embedded, 行 369-380):
    //     sign_it(client_secret_data, 16, &signature, ..., g_PrivateKey)
    //     client_pairing_secret = client_secret_data || signature
    //   即用私钥对 **client_secret 本身** 签名, 而不是对 serverchallenge 签名。
    //   服务端 clientpairingsecret 的两项检查:
    //     1) SHA256(sess.serverchallenge || 我方证书签名 || secret) == step3 的 clienthash
    //     2) crypto::verify256(我方证书, secret, sign)   <-- 验签对象是 secret
    //   之前签的是 serverchallenge, 第 2 项必然失败 -> paired=0。
    //   ★ client_secret 必须复用 step3 已生成的那个, 不能重新随机。
    Bytes signature = rsa_sign_sha256(key, client_secret);
    if (signature.empty()) {
        die("RSA 签名失败");
    }
    std::printf("[step4] 复用 step3 的 client_secret, 对其签名 RSA-SHA256(client_secret) = %zu 字节\n",
                signature.size());
    dump_hex("16_client_signature", signature);
    Bytes payload = client_secret;
    payload.insert(payload.end(), signature.begin(), signature.end());

    std::string path4 = "/pair?uniqueid=" + unique_id +
                        "&clientpairingsecret=" + to_hex(payload);
    HttpResult r4 = http_get(host, port, path4, 30000);
    if (!r4.ok) {
        die("step4 失败: " + r4.error);
    }

    const std::string paired = xml_tag(r4.body, "paired");
    const std::string status = xml_tag(r4.body, "status_message");
    std::printf("\n[step4] 响应: paired=%s%s\n", paired.c_str(),
                status.empty() ? "" : (" status_message=" + status).c_str());

    X509_free(scert);
    X509_free(cert);
    EVP_PKEY_free(key);

    if (paired == "1" && status.empty()) {
        std::printf("\n=== 配对成功 ===\n");
        return 0;
    }
    std::printf("\n=== 配对失败 ===\n%s\n", r4.body.c_str());
    return 1;
}
