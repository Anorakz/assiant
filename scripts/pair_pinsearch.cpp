// scripts/pair_pinsearch.cpp — 在"服务端证书签名"可能不是 plaincert 的假设下反推 PIN
//
// 动机: pair_verify 的 D 段失败(本地复算的 clienthash != 服务端返回值), 且 H 段在
// 5 种 salt 解释 × 全部 4 位 PIN 下都无命中。剩下的未知量只有"服务端 step2 用的
// 证书签名字节" —— 如果 conf_intern.servercert 与我们收到的 plaincert 不是同一
// 张证书, 那么任何 PIN 都复现不出服务端哈希。
//
// 本工具用 **磁盘上的服务端证书** 取签名, 再穷举 PIN, 从而判定:
//   * 命中 => PIN 是对的, 但 plaincert != 服务端签名所用证书 (协议实现有 bug 或
//             服务端证书在配对过程中被重新生成)
//   * 不命中 => PIN 确实是错的 (或密钥派生输入与我们的假设不同)
//
// 构建: g++ -std=c++17 -O2 -o pair_pinsearch pair_pinsearch.cpp -lssl -lcrypto
// 用法: ./pair_pinsearch <dump_dir> <servercert.pem> [salt_variant]
#include <openssl/evp.h>
#include <openssl/pem.h>
#include <openssl/x509.h>

#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

using Bytes = std::vector<unsigned char>;

static Bytes from_hex(const std::string& s) {
    Bytes b;
    for (std::size_t i = 0; i + 1 < s.size(); i += 2) {
        auto nib = [](char ch) -> int {
            if (ch >= '0' && ch <= '9') return ch - '0';
            if (ch >= 'a' && ch <= 'f') return ch - 'a' + 10;
            if (ch >= 'A' && ch <= 'F') return ch - 'A' + 10;
            return -1;
        };
        b.push_back(static_cast<unsigned char>((nib(s[i]) << 4) | nib(s[i + 1])));
    }
    return b;
}

static std::string to_hex(const Bytes& b) {
    static const char* d = "0123456789abcdef";
    std::string s;
    for (unsigned char c : b) {
        s.push_back(d[c >> 4]);
        s.push_back(d[c & 0x0F]);
    }
    return s;
}

static Bytes read_hex_file(const std::string& path) {
    FILE* f = std::fopen(path.c_str(), "r");
    if (f == nullptr) {
        std::fprintf(stderr, "FATAL: 打不开 %s\n", path.c_str());
        std::exit(2);
    }
    std::string s;
    int c;
    while ((c = std::fgetc(f)) != EOF) {
        if (c != '\n' && c != '\r' && c != ' ' && c != '\t') s.push_back(static_cast<char>(c));
    }
    std::fclose(f);
    return from_hex(s);
}

static Bytes sha256(const Bytes& data) {
    Bytes h(SHA256_DIGEST_LENGTH);
    unsigned int len = 0;
    EVP_Digest(data.data(), data.size(), h.data(), &len, EVP_sha256(), nullptr);
    h.resize(len);
    return h;
}

static Bytes aes_ecb_dec(const Bytes& key, const Bytes& in) {
    EVP_CIPHER_CTX* ctx = EVP_CIPHER_CTX_new();
    Bytes out(in.size() + 32);
    int l1 = 0, l2 = 0;
    EVP_CipherInit_ex(ctx, EVP_aes_128_ecb(), nullptr, key.data(), nullptr, 0);
    EVP_CIPHER_CTX_set_padding(ctx, 0);
    EVP_CipherUpdate(ctx, out.data(), &l1, in.data(), static_cast<int>(in.size()));
    EVP_CipherFinal_ex(ctx, out.data() + l1, &l2);
    EVP_CIPHER_CTX_free(ctx);
    out.resize(static_cast<std::size_t>(l1 + l2));
    return out;
}

static Bytes cert_signature_of(X509* x) {
    Bytes sig;
    const ASN1_BIT_STRING* bs = nullptr;
    X509_get0_signature(&bs, nullptr, x);
    if (bs != nullptr && bs->data != nullptr && bs->length > 0) {
        sig.assign(bs->data, bs->data + bs->length);
    }
    return sig;
}

int main(int argc, char** argv) {
    if (argc < 3) {
        std::fprintf(stderr, "usage: %s <dump_dir> <servercert.pem>\n", argv[0]);
        return 2;
    }
    const std::string dir = argv[1];

    auto ev = [&](const char* n) { return read_hex_file(dir + "/" + n + ".hex"); };
    const Bytes salt = ev("03_salt");
    const Bytes chalresp_raw = ev("07_challengeresponse_raw");
    const Bytes chalresp_dec = ev("08_challengeresponse_dec");
    const Bytes plaincert_pem = ev("09_server_cert_pem");
    const Bytes plaincert_sig = ev("10_server_cert_signature");
    const Bytes psecret = ev("13_pairingsecret");

    BIO* bio = BIO_new_mem_buf(plaincert_pem.data(), static_cast<int>(plaincert_pem.size()));
    X509* pcert = PEM_read_bio_X509(bio, nullptr, nullptr, nullptr);
    BIO_free(bio);
    Bytes pcert_sig = pcert ? cert_signature_of(pcert) : Bytes{};

    FILE* f = std::fopen(argv[2], "r");
    if (f == nullptr) {
        std::fprintf(stderr, "FATAL: 打不开服务端证书 %s\n", argv[2]);
        return 2;
    }
    X509* scert = PEM_read_X509(f, nullptr, nullptr, nullptr);
    std::fclose(f);
    if (scert == nullptr) {
        std::fprintf(stderr, "FATAL: 解析服务端证书失败\n");
        return 2;
    }
    Bytes scert_sig = cert_signature_of(scert);

    std::printf("plaincert 签名   %zuB sha256=%s\n", plaincert_sig.size(),
                to_hex(sha256(plaincert_sig)).c_str());
    std::printf("plaincert(解析)  %zuB sha256=%s\n", pcert_sig.size(), to_hex(sha256(pcert_sig)).c_str());
    std::printf("磁盘服务端证书签名 %zuB sha256=%s\n", scert_sig.size(),
                to_hex(sha256(scert_sig)).c_str());
    std::printf("两者是否相同: %s\n", (scert_sig == pcert_sig) ? "相同" : "不同  <== 关键");

    if (psecret.size() < 16) {
        std::printf("pairingsecret 太短\n");
        return 1;
    }
    Bytes serversecret(psecret.begin(), psecret.begin() + 16);
    Bytes want(chalresp_dec.begin(), chalresp_dec.begin() + 32);

    for (int which = 0; which < 3; ++which) {
        const Bytes& sig = (which == 0) ? scert_sig : (which == 1 ? pcert_sig : plaincert_sig);
        const char* name = (which == 0) ? "磁盘证书签名" : (which == 1 ? "plaincert解析签名" : "plaincert落盘签名");
        bool hit = false;
        for (int i = 0; i <= 9999 && !hit; ++i) {
            char buf[8];
            std::snprintf(buf, sizeof(buf), "%04d", i);
            const std::string cand(buf);
            Bytes sp = salt;
            sp.insert(sp.end(), cand.begin(), cand.end());
            Bytes k = sha256(sp);
            k.resize(16);
            Bytes d = aes_ecb_dec(k, chalresp_raw);
            if (d.size() < 48) continue;
            Bytes hin(d.begin(), d.begin() + 32);
            hin.insert(hin.end(), sig.begin(), sig.end());
            hin.insert(hin.end(), serversecret.begin(), serversecret.end());
            if (sha256(hin) == want) {
                std::printf(">>> 命中 [%s]: PIN=\"%s\"  AES key=%s\n", name, cand.c_str(),
                            to_hex(k).c_str());
                hit = true;
            }
        }
        if (!hit) {
            std::printf("    [%s] 0000..9999 无命中\n", name);
        }
    }

    X509_free(scert);
    if (pcert) X509_free(pcert);
    return 0;
}
