// ============================================================================
//  scripts/pair_verify.cpp — 配对证据离线复核器
//
//  用途
//  ---------------------------------------------------------------------------
//  pair_sunshine 在 PAIR_DUMP_DIR 下把每一步的原始字节以 hex 落盘。本工具把
//  这些字节**独立地**重新算一遍, 目的是把"配对失败"定位到具体某一步, 而不是
//  依赖服务端那句具有误导性的 "Invalid client certificate"。
//
//  复核内容
//  ---------------------------------------------------------------------------
//    A. AES key == SHA256(salt || pin)[:16]        (协议派生路径)
//    B. AES-128-ECB-dec(key, clientchallenge_enc) == clientchallenge   (往返)
//    C. AES-128-ECB-dec(key, challengeresp_raw)   == 48 字节           (长度)
//    D. clienthash == SHA256(clientchallenge ‖ servercert_sign ‖ serversecret)
//       ↑ 这是我们 step3 实际上交给服务端的那 32 字节。它==服务端算的哈希
//         当且仅当 **PIN 正确** 且 **证书签名字节一致**。
//    E. RSA-SHA256(servercert_sign, serversecret) 用服务端证书公钥验签通过
//       (确认 "serversecret" 确实来自服务端, 而非我们看错偏移)
//    F. RSA-SHA256(client_signature, serverchallenge) 用我方证书公钥验签通过
//       (确认我们 step4 提交的签名格式/model 与 Sunshine verify256 一致)
//
//  构建 / 运行
//  ---------------------------------------------------------------------------
//      g++ -std=c++17 -O2 -Wall -o pair_verify pair_verify.cpp -lssl -lcrypto
//      ./pair_verify <dump_dir> <servercert.pem> <clientcert.pem> <pin>
// ============================================================================

#include <openssl/evp.h>
#include <openssl/pem.h>
#include <openssl/x509.h>

#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

namespace {

using Bytes = std::vector<unsigned char>;

Bytes from_hex(const std::string& s) {
    Bytes b;
    for (std::size_t i = 0; i + 1 < s.size(); i += 2) {
        auto nib = [](char ch) -> int {
            if (ch >= '0' && ch <= '9') return ch - '0';
            if (ch >= 'a' && ch <= 'f') return ch - 'a' + 10;
            if (ch >= 'A' && ch <= 'F') return ch - 'A' + 10;
            return -1;
        };
        const int hi = nib(s[i]);
        const int lo = nib(s[i + 1]);
        if (hi < 0 || lo < 0) {
            std::fprintf(stderr, "非法 hex 在 offset %zu\n", i);
            std::exit(2);
        }
        b.push_back(static_cast<unsigned char>((hi << 4) | lo));
    }
    return b;
}

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

Bytes read_hex_file(const std::string& path) {
    FILE* f = std::fopen(path.c_str(), "r");
    if (f == nullptr) {
        std::fprintf(stderr, "FATAL: 打不开 %s\n", path.c_str());
        std::exit(2);
    }
    std::string s;
    int c;
    while ((c = std::fgetc(f)) != EOF) {
        if (c != '\n' && c != '\r' && c != ' ' && c != '\t') {
            s.push_back(static_cast<char>(c));
        }
    }
    std::fclose(f);
    return from_hex(s);
}

Bytes sha256(const Bytes& data) {
    Bytes h(SHA256_DIGEST_LENGTH);
    unsigned int len = 0;
    EVP_Digest(data.data(), data.size(), h.data(), &len, EVP_sha256(), nullptr);
    h.resize(len);
    return h;
}

Bytes aes_ecb_dec(const Bytes& key, const Bytes& in) {
    EVP_CIPHER_CTX* ctx = EVP_CIPHER_CTX_new();
    Bytes out(in.size() + 32);
    int l1 = 0;
    int l2 = 0;
    EVP_CipherInit_ex(ctx, EVP_aes_128_ecb(), nullptr, key.data(), nullptr, 0);
    EVP_CIPHER_CTX_set_padding(ctx, 0);  // Sunshine 用 ecb_t(key, false)
    EVP_CipherUpdate(ctx, out.data(), &l1, in.data(), static_cast<int>(in.size()));
    EVP_CipherFinal_ex(ctx, out.data() + l1, &l2);
    EVP_CIPHER_CTX_free(ctx);
    out.resize(static_cast<std::size_t>(l1 + l2));
    return out;
}

Bytes cert_signature_of(X509* x) {
    Bytes sig;
    const ASN1_BIT_STRING* bs = nullptr;
    X509_get0_signature(&bs, nullptr, x);
    if (bs != nullptr && bs->data != nullptr && bs->length > 0) {
        sig.assign(bs->data, bs->data + bs->length);
    }
    return sig;
}

X509* load_cert(const char* path) {
    FILE* f = std::fopen(path, "r");
    if (f == nullptr) {
        std::fprintf(stderr, "FATAL: 打不开证书 %s\n", path);
        std::exit(2);
    }
    X509* x = PEM_read_X509(f, nullptr, nullptr, nullptr);
    std::fclose(f);
    return x;
}

bool rsa_verify_sha256(X509* x, const Bytes& data, const Bytes& sig) {
    EVP_PKEY* pk = X509_get0_pubkey(x);
    if (pk == nullptr) {
        return false;
    }
    EVP_MD_CTX* ctx = EVP_MD_CTX_new();
    bool ok = EVP_DigestVerifyInit(ctx, nullptr, EVP_sha256(), nullptr, pk) == 1 &&
              EVP_DigestVerifyUpdate(ctx, data.data(), data.size()) == 1 &&
              EVP_DigestVerifyFinal(ctx, sig.data(), sig.size()) == 1;
    EVP_MD_CTX_free(ctx);
    return ok;
}

int g_fail = 0;

void check(const char* label, bool ok) {
    std::printf("  [%s] %s\n", ok ? "PASS" : "FAIL", label);
    if (!ok) {
        ++g_fail;
    }
}

}  // namespace

int main(int argc, char** argv) {
    if (argc < 5) {
        std::fprintf(stderr, "usage: %s <dump_dir> <servercert.pem> <clientcert.pem> <pin>\n",
                     argv[0]);
        return 2;
    }
    const std::string dir = argv[1];
    const std::string pin = argv[4];

    auto ev = [&](const char* name) { return read_hex_file(dir + "/" + name + ".hex"); };

    const Bytes salt = ev("03_salt");
    const Bytes dump_key = ev("04_aes_key");
    const Bytes cli_chal = ev("05_client_challenge");
    const Bytes cli_chal_enc = ev("06_client_challenge_enc");
    const Bytes chalresp_raw = ev("07_challengeresponse_raw");
    const Bytes chalresp_dec = ev("08_challengeresponse_dec");
    const Bytes server_pem = ev("09_server_cert_pem");
    const Bytes srv_sig = ev("10_server_cert_signature");
    const Bytes resp_plain = ev("11_serverchallengeresp_plain");
    const Bytes resp_enc = ev("12_serverchallengeresp_enc");
    const Bytes psecret = ev("13_pairingsecret");
    const Bytes srv_chal = ev("14_server_challenge");
    const Bytes cli_secret = ev("15_client_secret");
    const Bytes cli_sig = ev("16_client_signature");

    std::printf("=== 证据来源 %s ===\n", dir.c_str());
    std::printf("salt=%zuB clientcert_challenge=%zuB/enc=%zuB chalresp=%zuB/%zuB "
                "servercert_pem=%zuB srv_sig=%zuB pairingsecret=%zuB cli_sig=%zuB\n\n",
                salt.size(), cli_chal.size(), cli_chal_enc.size(), chalresp_raw.size(),
                chalresp_dec.size(), server_pem.size(), srv_sig.size(), psecret.size(),
                cli_sig.size());

    // ---- A. AES key 派生 (用 pkeyutl 也能复现: SHA256(salt||pin)[:16]) ----
    std::printf("A. AES key 派生 = SHA256(salt || pin)[:16]\n");
    Bytes sp = salt;
    sp.insert(sp.end(), pin.begin(), pin.end());
    Bytes derived = sha256(sp);
    derived.resize(16);
    std::printf("     derived=%s\n", to_hex(derived).c_str());
    std::printf("     dumped =%s\n", to_hex(dump_key).c_str());
    check("派生密钥与落盘密钥一致", derived == dump_key);

    // ---- B. 我方挑战的 AES 往返 ----
    std::printf("\nB. AES-128-ECB 无填充: dec(key, clientchallenge_enc) == clientchallenge\n");
    Bytes rt = aes_ecb_dec(dump_key, cli_chal_enc);
    std::printf("     sent=%s\n", to_hex(cli_chal).c_str());
    std::printf("     back=%s\n", to_hex(rt).c_str());
    check("我方加密的挑战能被同一密钥解回", rt == cli_chal);

    // ---- C. 服务端 challengeresponse 长度 ----
    std::printf("\nC. 服务端 challengeresponse 解密长度\n");
    Bytes dec = aes_ecb_dec(dump_key, chalresp_raw);
    std::printf("     密文=%zuB 解密=%zuB (协议值 48 = SHA256 32 + 挑战 16)\n", chalresp_raw.size(),
                dec.size());
    check("解密长度 == 48", dec.size() == 48);
    check("与 pair_sunshine 落盘一致", dec == chalresp_dec);

    // ---- D. clienthash 复算 ----
    std::printf("\nD. clienthash 复算 (决定 step4 same_hash)\n");
    Bytes srv_pem_str = server_pem;
    std::string pem_text(srv_pem_str.begin(), srv_pem_str.end());
    BIO* bio = BIO_new_mem_buf(pem_text.data(), static_cast<int>(pem_text.size()));
    X509* scert = PEM_read_bio_X509(bio, nullptr, nullptr, nullptr);
    BIO_free(bio);
    if (scert == nullptr) {
        std::printf("  [FAIL] 服务端证书 PEM 解析失败\n");
        return 1;
    }
    Bytes scert_sig = cert_signature_of(scert);
    std::printf("     证书签名 %zuB, SHA256=%s\n", scert_sig.size(),
                to_hex(sha256(scert_sig)).c_str());
    std::printf("     落盘签名 %zuB, SHA256=%s\n", srv_sig.size(), to_hex(sha256(srv_sig)).c_str());
    check("服务端证书签名字节一致", scert_sig == srv_sig);

    // serversecret = pairingsecret[0,16); 服务端签名 = pairingsecret[16,272)
    if (psecret.size() < 16) {
        std::printf("  [FAIL] pairingsecret 太短\n");
        return 1;
    }
    Bytes serversecret(psecret.begin(), psecret.begin() + 16);
    Bytes srv_sign(psecret.begin() + 16, psecret.end());
    std::printf("     serversecret=%s\n", to_hex(serversecret).c_str());

    Bytes h_in = dec;  // clientchallenge(16) + sign + serversecret
    h_in.insert(h_in.end(), scert_sig.begin(), scert_sig.end());
    h_in.insert(h_in.end(), serversecret.begin(), serversecret.end());
    Bytes recomputed = sha256(h_in);
    std::printf("     clienthash(服务端回给我们的) =%s\n", to_hex(Bytes(dec.begin(), dec.begin() + 32)).c_str());
    std::printf("     clienthash(本地复算)         =%s\n", to_hex(recomputed).c_str());
    check("clienthash 复算 == 服务端返回值  => PIN 正确且证书签名字节一致",
          recomputed.size() == 32 && dec.size() >= 32 &&
              std::equal(recomputed.begin(), recomputed.end(), dec.begin()));

    // D2. 关键判据: 服务端解密我们的挑战, 得到的应是我们发出的明文。
    //     challengeresponse = AES-ECB( SHA256(<服务端解出的挑战> ‖ sign ‖ serversecret)
    //                                  ‖ serverchallenge , key)
    //     所以解密后 [32,48) 应当**逐字节等于我们发出去的挑战**。
    //     * 相等  => 双方密钥一致 (PIN 一致): 协议实现无误
    //     * 不等  => 服务端用的是别的 PIN/密钥, 这是配对失败的唯一根因
    Bytes srv_chal_in_resp(dec.begin() + 32, dec.begin() + 48);
    std::printf("\nD2. 服务端解密出的挑战 vs 我们发出的挑战\n");
    std::printf("     我们发出 = %s\n", to_hex(cli_chal).c_str());
    std::printf("     服务端解 = %s\n", to_hex(srv_chal_in_resp).c_str());
    const bool key_ok = (srv_chal_in_resp == cli_chal);
    check("服务端解出的挑战 == 我们发出的挑战  => 双方 AES 密钥一致", key_ok);

    // D3. 若密钥一致, 服务端哈希的输入是【明文挑战】; 用明文复算应当命中
    Bytes h_in2 = cli_chal;
    h_in2.insert(h_in2.end(), scert_sig.begin(), scert_sig.end());
    h_in2.insert(h_in2.end(), serversecret.begin(), serversecret.end());
    Bytes recomputed2 = sha256(h_in2);
    std::printf("     用明文挑战复算 = %s\n", to_hex(recomputed2).c_str());
    check("用明文挑战复算 == 服务端返回值",
          recomputed2.size() == 32 && dec.size() >= 32 &&
              std::equal(recomputed2.begin(), recomputed2.end(), dec.begin()));

    // ---- E. 服务端对 serversecret 的签名 ----
    std::printf("\nE. 服务端 RSA-SHA256 签名校验 (pairingsecret 后段)\n");
    std::printf("     签名=%zuB\n", srv_sign.size());
    check("服务端签名能被服务端证书公钥验证", rsa_verify_sha256(scert, serversecret, srv_sign));

    // ---- F. 我方 step4 签名 ----
    std::printf("\nF. 我方 RSA-SHA256(serverchallenge) 签名自检\n");
    X509* ccert = load_cert(argv[3]);
    if (ccert == nullptr) {
        std::printf("  [FAIL] 我方证书解析失败\n");
        return 1;
    }
    std::printf("     签名=%zuB\n", cli_sig.size());
    check("我方签名能被我方证书公钥验证 (与 Sunshine verify256 同构)",
          rsa_verify_sha256(ccert, srv_chal, cli_sig));

    // ---- G. step3 密文与明文自洽 ----
    std::printf("\nG. step3 提交内容自洽\n");
    std::printf("     plain=%s\n", to_hex(resp_plain).c_str());
    std::printf("     enc  =%s\n", to_hex(resp_enc).c_str());
    check("resp_plain == 复算 clienthash", resp_plain == recomputed);
    check("resp_plain 长度 32 且 resp_enc 长度 32", resp_plain.size() == 32 && resp_enc.size() == 32);

    // ---- H. 反推操作员实际输入的 PIN ----
    // 若 D 失败, 未知量可能有两处: (a) AES key = SHA256(salt||pin)[:16] 里的 pin,
    // (b) salt 的编码解释。先在几种 salt 解释下穷举 0000..9999, 命中即定位。
    std::printf("\nH. 反推 PIN × salt 编码 (clienthash 命中判定)\n");
    Bytes want(dec.begin(), dec.begin() + 32);

    // 服务端可能拿到的 salt 字符串形式
    std::string salt_hex_lower = to_hex(salt);
    std::string salt_hex_upper = salt_hex_lower;
    for (char& c : salt_hex_upper) {
        if (c >= 'a' && c <= 'f') c = static_cast<char>(c - 'a' + 'A');
    }
    std::vector<std::pair<std::string, Bytes>> salts;
    salts.emplace_back("raw16", salt);                                   // 正确解释
    salts.emplace_back("ascii_hex_lower", Bytes(salt_hex_lower.begin(), salt_hex_lower.end()));
    salts.emplace_back("ascii_hex_upper", Bytes(salt_hex_upper.begin(), salt_hex_upper.end()));
    salts.emplace_back("ascii_hex_lower_nopad",  // 前 31 字符 (若长度判断写错)
                       Bytes(salt_hex_lower.begin(), salt_hex_lower.end() - 1));
    salts.emplace_back("prefix16", Bytes(salt.begin(), salt.begin() + 8));  // 只取 8 字节

    bool found = false;
    for (auto& [sname, sbytes] : salts) {
        for (int i = 0; i <= 9999 && !found; ++i) {
            char buf[8];
            std::snprintf(buf, sizeof(buf), "%04d", i);
            const std::string cand(buf);
            Bytes csp = sbytes;
            csp.insert(csp.end(), cand.begin(), cand.end());
            Bytes ck = sha256(csp);
            ck.resize(16);
            Bytes cd = aes_ecb_dec(ck, chalresp_raw);
            if (cd.size() < 48) {
                continue;
            }
            Bytes hin(cd.begin(), cd.begin() + 32);
            hin.insert(hin.end(), scert_sig.begin(), scert_sig.end());
            hin.insert(hin.end(), serversecret.begin(), serversecret.end());
            if (sha256(hin) == want) {
                std::printf("     >>> 命中: salt 解释 = %s, PIN = \"%s\"\n", sname.c_str(),
                            cand.c_str());
                std::printf("         该组合的 AES key = %s\n", to_hex(ck).c_str());
                found = true;
            }
        }
        if (!found) {
            std::printf("     salt=%-18s 0000..9999 无命中\n", sname.c_str());
        }
    }
    if (!found) {
        std::printf("     结论: 在 5 种 salt 解释 × 全部 4 位 PIN 下都无法复现服务端哈希。\n"
                    "     即差异不是 \"PIN 记错\", 而是服务端使用的密钥/证书材料与我们\n"
                    "     提交的不同 —— 必须开启 Sunshine debug 日志才能看到真实原因。\n");
    }
    check("PIN 反推命中", found || recomputed == want);

    X509_free(scert);
    X509_free(ccert);

    std::printf("\n=== 复核结束: %s ===\n", g_fail == 0 ? "全部通过" : "有失败项");
    if (g_fail != 0) {
        std::printf("说明: D 失败 => PIN 不一致 或 服务端看到的 clientcert 与我们本地不同;\n"
                    "      E 失败 => pairingsecret 偏移理解错误;\n"
                    "      F 失败 => 我方私钥/证书不匹配。\n");
    }
    return g_fail == 0 ? 0 : 1;
}
