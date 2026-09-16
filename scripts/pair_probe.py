#!/usr/bin/env python3
"""
scripts/pair_probe.py — 一次打通 step1..step3 并做对称性判定

目的: 判定 "服务端 AES 密钥 == 我们的密钥" 与否, 用两条互相独立的证据:
  (甲) 服务端 step2 回给我们的哈希, 是否等于 SHA256(我们发出的明文挑战 ‖ 服务端
       证书签名 ‖ pairingsecret[0:16])。命中 => 服务端解密我们的挑战得到的正是
       明文挑战 => 双方密钥相同。
  (乙) 我们解密 step2 回包得到的 [32:48], 是否等于 pairingsecret[0:16]。
       Sunshine 里这两个值都由同一次 rand(16) 派生, 本应相同。

甲与乙互相矛盾时, 说明问题不在密钥, 而在"服务端当成挑战的那个值"与"它放进
回包的那个 16 字节"之间 —— 这正是需要看源码/日志的地方。

用法: python3 pair_probe.py <host> <clientcert.pem> <clientkey.pem> <pin> <evidir>
"""
import binascii
import hashlib
import os
import re
import secrets
import sys

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pair_sunshine import aes_ecb, xml_tag, http_get  # noqa: E402


def H(b):
    return hashlib.sha256(b).hexdigest()


def main():
    if len(sys.argv) < 6:
        print(__doc__)
        return 2
    host, cert_path, key_path, pin, evidir = sys.argv[1:6]
    os.makedirs(evidir, exist_ok=True)
    uid = secrets.token_hex(16)

    cert_pem = open(cert_path, "rb").read()
    k = serialization.load_pem_private_key(open(key_path, "rb").read(), password=None)
    cli_sig = x509.load_pem_x509_certificate(cert_pem).signature

    salt = secrets.token_bytes(16)
    key = hashlib.sha256(salt + pin.encode()).digest()[:16]
    print("uniqueid=%s" % uid)
    print("salt=%s pin=%s key=%s" % (salt.hex(), pin, key.hex()))

    print("\n>>> step1: 请在 https://localhost:47990/pin 输入 %s 并提交" % pin)
    sys.stdout.flush()
    url = ("/pair?uniqueid=%s&phrase=getservercert&clientcert=%s&salt=%s&devicename=agent-native"
           % (uid, binascii.hexlify(cert_pem).decode(), salt.hex()))
    st, body = http_get(host, 47989, url, 600)
    pc = xml_tag(body, "plaincert")
    if not pc:
        print("step1 FAIL: %s" % body[:200])
        return 1
    scert = x509.load_pem_x509_certificate(binascii.unhexlify(pc))
    srv_sig = scert.signature
    print("step1 OK (服务端证书签名 %d B)" % len(srv_sig))

    chal = secrets.token_bytes(16)
    enc = aes_ecb(key, chal, True)
    st, body = http_get(host, 47989, "/pair?uniqueid=%s&clientchallenge=%s" % (uid, enc.hex()), 30)
    cr = xml_tag(body, "challengeresponse")
    if not cr:
        print("step2 FAIL: %s" % body[:200])
        return 1
    raw = binascii.unhexlify(cr)
    dec = aes_ecb(key, raw, False)
    srv_hash = dec[:32]
    dec_chal = dec[32:48]
    print("step2 密文 %d B -> 解密 %d B" % (len(raw), len(dec)))
    print("  我们发出的挑战        = %s" % chal.hex())
    print("  服务端回包哈希        = %s" % srv_hash.hex())
    print("  服务端回包[32:48]     = %s" % dec_chal.hex())

    # step3: 用权威公式提交 (serverchallenge = 回包[32:48], 我方证书签名, client_secret)
    cli_secret = secrets.token_bytes(16)
    cli_hash = hashlib.sha256(dec_chal + cli_sig + cli_secret).digest()
    st, body = http_get(
        host, 47989,
        "/pair?uniqueid=%s&serverchallengeresp=%s" % (uid, aes_ecb(key, cli_hash, True).hex()), 30)
    ps = xml_tag(body, "pairingsecret")
    if not ps:
        print("step3 FAIL: %s" % body[:200])
        return 1
    psecret = binascii.unhexlify(ps)
    srv_secret = psecret[:16]
    print("step3 pairingsecret %d B: [0:16]=%s" % (len(psecret), srv_secret.hex()))

    # ---- 判定 ----
    print("\n" + "=" * 68)
    jia = hashlib.sha256(chal + srv_sig + srv_secret).digest() == srv_hash
    yi = dec_chal == srv_secret
    print("(甲) 服务端哈希 == SHA256(明文挑战 || 服务端证书签名 || psecret[0:16]) ? %s"
          % ("是 => 双方 AES 密钥相同" if jia else "否"))
    print("(乙) 我们解出的[32:48] == pairingsecret[0:16] ?                            %s"
          % ("是 => 服务端语义与预期一致" if yi else "否 => 服务端把*另一个*16字节放进了回包"))
    zi = hashlib.sha256(dec_chal + srv_sig + srv_secret).digest() == srv_hash
    print("(自洽) 服务端哈希 == SHA256(解出的[32:48] || 签名 || psecret[0:16]) ?       %s"
          % ("是" if zi else "否"))
    print("=" * 68)
    if jia and not yi:
        print("""
结论: 服务端**正确解出了我们的明文挑战**(密钥一致, 甲命中), 但它放进 challengeresponse
      的 16 字节(乙) 与它自己在 step4 用来重算哈希的 sess.serverchallenge 不是同一个值。
      也就是说: 差异不在我们的实现, 而在服务端把 serverchallenge 弄混/覆盖了。
""")
    elif not jia:
        print("\n结论: 服务端用的密钥确实与我们不同 (PIN 或 salt 不同)。\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
