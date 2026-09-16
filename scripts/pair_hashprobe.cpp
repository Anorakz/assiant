// scripts/pair_hashprobe.cpp — 穷举"服务端 clientchallenge 哈希输入"的所有合理构造
//
// 背景: pair_verify 已证明
//   * 我们的 AES key 正确 (能解回自己发的挑战, 且服务端回包是 48 字节)
//   * 服务端证书签名字节正确 (E 段验签通过)
//   * serversecret = pairingsecret[0,16) 正确 (E 段验签通过)
// 但 SHA256(挑战 ‖ 服务端签名 ‖ serversecret) != 服务端回给我们的 clienthash。
//
// 既然三个"材料"都验证过了, 那差异只能出在**拼接方式**上。本工具把服务端可能
// 采用的各种拼接方式全部穷举一遍, 命中即说明 src/nvhttp.cpp 的哈希输入与我们
// 的理解不同 (例如 decrypted 为空、多插入了其他字段、字段顺序不同等)。
//
// 用法: ./pair_hashprobe <serverhash_hex> <challenge_plain_hex> <servercert_sig_hex> <serversecret_hex>
#include <openssl/evp.h>
#include <openssl/sha.h>

#include <cstdio>
#include <cstring>
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
            return 0;
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

static Bytes sha256(const Bytes& data) {
    Bytes h(SHA256_DIGEST_LENGTH);
    unsigned int len = 0;
    EVP_Digest(data.data(), data.size(), h.data(), &len, EVP_sha256(), nullptr);
    h.resize(len);
    return h;
}

static void cat(Bytes& out, const Bytes& a) { out.insert(out.end(), a.begin(), a.end()); }

int main(int argc, char** argv) {
    if (argc < 5) {
        std::fprintf(stderr,
                     "usage: %s <serverhash_hex> <challenge_plain_hex> <servercert_sig_hex> "
                     "<serversecret_hex>\n",
                     argv[0]);
        return 2;
    }
    const Bytes want = from_hex(argv[1]);       // 服务端回给我们的 32 字节 clienthash
    const Bytes chal_plain = from_hex(argv[2]);  // 我们发出的 16 字节挑战明文
    const Bytes sig = from_hex(argv[3]);         // 服务端证书签名 (256B)
    const Bytes secret = from_hex(argv[4]);      // serversecret (16B)

    std::printf("目标 clienthash (服务端给的) = %s\n", to_hex(want).c_str());
    std::printf("challenge=%zuB sig=%zuB secret=%zuB   (sig 应为 256B, 否则参数给错)\n\n",
                chal_plain.size(), sig.size(), secret.size());
    if (sig.size() != 256) {
        std::printf("FATAL: sig 不是 256 字节 —— 签名提取有误, 本探测无效\n");
        return 2;
    }

    // 候选: 各种"挑战"字段取值
    std::vector<std::pair<std::string, Bytes>> chals;
    chals.emplace_back("challenge_plain", chal_plain);
    chals.emplace_back("challenge_empty", Bytes{});
    chals.emplace_back("challenge_16_zero", Bytes(16, 0));
    chals.emplace_back("challenge_32_zero", Bytes(32, 0));

    // 候选: 各种拼接顺序 / 字段组合 (名字即拼接顺序)
    const char* combos[] = {
        "chal|sig|secret",  // 源码写法
        "sig|secret",
        "secret|sig|chal",
        "sig|chal|secret",
        "chal|secret|sig",
        "secret|chal|sig",
        "chal|sig",
        "chal|secret",
        "secret",
        "sig",
    };

    bool hit = false;
    for (auto& [cname, cbytes] : chals) {
        for (const char* nm : combos) {
            Bytes in;
            std::string n(nm);
            std::size_t p = 0;
            while (p < n.size()) {
                std::size_t q = n.find('|', p);
                std::string tok = n.substr(p, (q == std::string::npos ? n.size() : q) - p);
                if (tok == "chal") cat(in, cbytes);
                else if (tok == "sig") cat(in, sig);
                else if (tok == "secret") cat(in, secret);
                if (q == std::string::npos) break;
                p = q + 1;
            }
            Bytes h = sha256(in);
            if (h == want) {
                std::printf(">>> 命中: challenge=%s  拼接=%s  (输入 %zu 字节)\n", cname.c_str(),
                            nm, in.size());
                hit = true;
            }
        }
    }

    // 另外试探: 是否服务端把 sig 当成了"整个 plaincert 的 PEM 文本"
    // (若 signature() 实现不同的话) —— 用 sig 的 hex 文本作为签名字节
    {
        std::string sig_hex = to_hex(sig);
        Bytes sig_as_text(sig_hex.begin(), sig_hex.end());
        Bytes in = chal_plain;
        cat(in, sig_as_text);
        cat(in, secret);
        if (sha256(in) == want) {
            std::printf(">>> 命中: 用签名的 hex 文本作为签名字段\n");
            hit = true;
        }
    }

    if (!hit) {
        std::printf("所有组合均未命中 —— 说明差异不在拼接方式, 而在某个字段的**取值**。\n"
                    "下一步只能靠服务端 debug 日志 (min_log_level=debug) 定论。\n");
    }
    return hit ? 0 : 1;
}
