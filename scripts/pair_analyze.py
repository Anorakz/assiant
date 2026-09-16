#!/usr/bin/env python3
"""
scripts/pair_analyze.py — 对 pairev/ 目录里的配对证据做全排列比对

用途: 配对失败时, 不再靠猜测, 而是把"服务端返回的哈希"与所有**可能的**输入组合
逐一比对, 从而唯一定位差异到底在哪一段字节。

用法: python3 pair_analyze.py <evidir>
"""
import binascii
import hashlib
import itertools
import os
import sys


def load(d, name):
    p = os.path.join(d, name + ".hex")
    if not os.path.exists(p):
        return None
    return binascii.unhexlify(open(p).read().strip())


def H(b):
    return hashlib.sha256(b).hexdigest()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    d = sys.argv[1]

    key = load(d, "04_aes_key")
    cli_chal_enc = load(d, "06_client_challenge_enc")
    chalresp_raw = load(d, "07_challengeresponse_raw")
    chalresp_dec = load(d, "08_challengeresponse_dec")
    srv_sig = load(d, "10_server_cert_signature")
    resp_plain = load(d, "11_serverchallengeresp_plain")
    psecret = load(d, "13_pairingsecret")
    cli_req_sig = load(d, "02_client_cert_signature")
    cli_secret = load(d, "15_client_secret")
    cli_sig = load(d, "16_client_signature")

    print("== 证据概览 ==")
    for n, b in (("aes_key", key), ("cli_chal_enc", cli_chal_enc), ("chalresp_raw", chalresp_raw),
                 ("chalresp_dec", chalresp_dec), ("srv_cert_sig", srv_sig),
                 ("resp_plain(我们提交)", resp_plain), ("pairingsecret", psecret),
                 ("cli_cert_sig", cli_req_sig), ("cli_secret", cli_secret),
                 ("cli_signature", cli_sig)):
        print("   %-22s %s" % (n, "%d B  sha256=%s" % (len(b), H(b)) if b else "缺失"))

    if chalresp_dec is None or chalresp_raw is None:
        print("FATAL: 缺 challengeresponse 证据")
        return 2

    srv_hash = chalresp_dec[:32]
    srv_chal = chalresp_dec[32:48]
    srv_secret = psecret[:16] if psecret and len(psecret) >= 16 else None

    print("\n== 关键值 ==")
    print("   服务端返回的 clienthash        = %s" % srv_hash.hex())
    print("   解密出的 serverchallenge       = %s" % srv_chal.hex())
    print("   pairingsecret[0:16]            = %s" % (srv_secret.hex() if srv_secret else "N/A"))
    print("   两者相同? %s" % ("是" if srv_secret == srv_chal else "否"))

    # 反解: 若 AES 密钥正确, dec 必然等于服务端加密前的明文。
    # 服务端明文 = SHA256(<它解出的挑战> || 服务端证书签名 || serversecret) || serverchallenge
    # 于是应有: SHA256(dec[32:48] || srv_sig || serversecret) == dec[0:32]
    print("\n== 自洽性 (只用我们解出的字节) ==")
    if srv_secret:
        recomputed = hashlib.sha256(srv_chal + srv_sig + srv_secret).digest()
        print("   SHA256(dec[32:48] || srv_sig || psecret[0:16]) = %s" % recomputed.hex())
        print("   dec[0:32]                                     = %s" % srv_hash.hex())
        print("   一致? %s" % ("是  => 解密链路正确" if recomputed == srv_hash else "否  => 我们的 AES 密钥与服务端不同"))

    # 列出所有合理候选, 看哪个等于 srv_hash
    print("\n== 用明文挑战做的各种候选哈希 (找 srv_hash 的来源) ==")
    cli_chal = None
    p = os.path.join(d, "05_client_challenge.hex")
    if os.path.exists(p):
        cli_chal = binascii.unhexlify(open(p).read().strip())
    parts = {
        "cli_chal": cli_chal,
        "srv_chal": srv_chal,
        "srv_sig": srv_sig,
        "cli_cert_sig": cli_req_sig,
        "srv_secret": srv_secret,
        "cli_secret": cli_secret,
    }
    names = [k for k, v in parts.items() if v]
    found = []
    for r in (2, 3):
        for combo in itertools.permutations(names, r):
            data = b"".join(parts[c] for c in combo)
            if hashlib.sha256(data).digest() == srv_hash:
                found.append("+".join(combo))
    if found:
        for f in found:
            print("   >>> 命中: SHA256(%s) == 服务端返回的 clienthash" % f)
    else:
        print("   没有 2/3 段排列命中")

    # AES 往返自检
    print("\n== AES 往返自检 ==")
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    c = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    rt = c.update(chalresp_raw) + c.finalize()
    print("   dec(key, chalresp_raw) == chalresp_dec ? %s" % ("是" if rt == chalresp_dec else "否"))
    # 用 pair_sunshine 保存的 enc 与 raw 是否一致
    print("   cli_chal_enc 长度 %d, 用 key 加密一次看是否可复现" % len(cli_chal_enc))
    e = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    print("   (加密是随机的, 无法比对; 只验证解密确定性)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
