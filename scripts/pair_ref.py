#!/usr/bin/env python3
"""
scripts/pair_ref.py — 忠实移植 Moonlight Embedded 的配对实现 (temp/pairing.c)

与 scripts/pair_sunshine.py 的区别（刻意保留，用于对照验证）:
  1. 明文 HTTP 47989, URL 带 `devicename=` 与 `updateState=1`
  2. 全程使用 AES-128-ECB **逐块** 加解密 (对应 C 里的 AES_encrypt/AES_decrypt)
  3. step1 用 g_CertHex (PEM 的 hex), 并检查 <paired>1</paired>
  4. step3 哈希 = SHA256( 回包[32:48] || 我方证书签名[256] || client_secret[16] )
  5. step4 用**私钥对 client_secret 本身签名** (reference 是 sign_it(client_secret_data,16))
     而不是对 serverchallenge 签名
  6. 每一步都校验 <paired>1</paired>

原文关键行:
  L262-272  salt(16) || pin(4) -> SHA256 -> AES-128 key
  L305-313  challengeresponse 解密 48B
  L325-329  challenge_response = 回包[32:48] || cert_signature || client_secret -> SHA256
  L369-380  sign_it(client_secret_data, 16) -> clientpairingsecret = secret || sig

用法: python3 pair_ref.py <host> <clientcert.pem> <clientkey.pem> <pin> [uniqueid]
"""
import binascii
import hashlib
import os
import secrets
import ssl
import sys
import urllib.request

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


# ---------------------------------------------------------------- AES 逐块 ----
class AesEcbBlocks:
    """对应 C 里 AES_set_encrypt_key/AES_set_decrypt_key + AES_encrypt/AES_decrypt"""

    def __init__(self, key: bytes):
        assert len(key) == 16
        self.key = key

    def encrypt_block(self, block: bytes) -> bytes:
        c = Cipher(algorithms.AES(self.key), modes.ECB()).encryptor()
        return c.update(block) + c.finalize()

    def decrypt_block(self, block: bytes) -> bytes:
        c = Cipher(algorithms.AES(self.key), modes.ECB()).decryptor()
        return c.update(block) + c.finalize()

    def encrypt(self, data: bytes) -> bytes:
        out = b""
        for i in range(0, len(data), 16):
            out += self.encrypt_block(data[i:i + 16])
        return out

    def decrypt(self, data: bytes) -> bytes:
        out = b""
        for i in range(0, len(data), 16):
            out += self.decrypt_block(data[i:i + 16])
        return out


def http_get(url: str, timeout: int = 600) -> str:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode(errors="replace")


def xml_search(xml: str, node: str):
    """对应 C 的 xml_search: 取第一个 <node>...</node>"""
    a = xml.find("<%s>" % node)
    if a < 0:
        return None
    a += len(node) + 2
    b = xml.find("</%s>" % node, a)
    if b < 0:
        return None
    return xml[a:b]


def main() -> int:
    if len(sys.argv) < 5:
        print(__doc__)
        return 2
    host, cert_path, key_path, pin = sys.argv[1:5]
    uid = sys.argv[5] if len(sys.argv) > 5 else secrets.token_hex(16)

    cert_pem = open(cert_path, "rb").read()
    cert = x509.load_pem_x509_certificate(cert_pem)
    cli_key = serialization.load_pem_private_key(open(key_path, "rb").read(), password=None)
    cert_hex = binascii.hexlify(cert_pem).decode()          # g_CertHex
    cert_sig = cert.signature                                # ASN1_BIT_STRING->data
    print("uniqueid = %s" % uid)
    print("cert_pem %d bytes / cert_sig %d bytes" % (len(cert_pem), len(cert_sig)))

    base = "http://%s:47989/pair?uniqueid=%s&devicename=roth&updateState=1" % (host, uid)

    # ---------------- step 1: getservercert ----------------
    salt = secrets.token_bytes(16)
    salt_hex = salt.hex()
    print("\n>>> step1: 请在 https://localhost:47990/pin 输入 %s 并提交" % pin)
    sys.stdout.flush()
    body = http_get("%s&phrase=getservercert&salt=%s&clientcert=%s" % (base, salt_hex, cert_hex))
    paired = xml_search(body, "paired")
    print("step1 paired=%s" % paired)
    if paired != "1":
        print("step1 FAIL: %s" % body[:300])
        return 1
    plaincert_hex = xml_search(body, "plaincert")
    if not plaincert_hex:
        print("step1 无 plaincert")
        return 1
    server_pem = binascii.unhexlify(plaincert_hex)
    server_cert = x509.load_pem_x509_certificate(server_pem)
    print("step1 OK, server cert %d bytes PEM" % len(server_pem))

    # ---------------- 密钥派生 (L262-275) ----------------
    salt_pin = salt + pin.encode()                            # salt(16) || pin(4)
    assert len(salt_pin) == 20, len(salt_pin)
    hash_length = 32                                          # serverMajorVersion >= 7
    aes_key_hash = hashlib.sha256(salt_pin).digest()
    aes = AesEcbBlocks(aes_key_hash[:16])                     # AES_set_*_key(..., 128)
    print("salt_pin=%s  aes_key=%s" % (salt_pin.hex(), aes_key_hash[:16].hex()))

    # ---------------- step 2: clientchallenge ----------------
    challenge_data = secrets.token_bytes(16)
    challenge_enc = aes.encrypt_block(challenge_data)
    body = http_get("%s&clientchallenge=%s" % (base, challenge_enc.hex()))
    if xml_search(body, "paired") != "1":
        print("step2 paired != 1: %s" % body[:300])
        return 1
    cr_hex = xml_search(body, "challengeresponse")
    if not cr_hex:
        print("step2 无 challengeresponse")
        return 1
    cr_enc = binascii.unhexlify(cr_hex)
    cr = aes.decrypt(cr_enc)                                  # L311-313 逐块
    print("step2 OK: 密文 %d B -> 解密 %d B" % (len(cr_enc), len(cr)))
    print("       明文挑战 = %s" % challenge_data.hex())
    print("       回包[0:32]  = %s" % cr[:32].hex())
    print("       回包[32:48] = %s" % cr[32:48].hex())

    # ---------------- step 3: serverchallengeresp ----------------
    client_secret_data = secrets.token_bytes(16)              # L315-316
    src = cr[hash_length:hash_length + 16] + cert_sig + client_secret_data   # L325-327
    crh = hashlib.sha256(src).digest()                        # L328-329
    crh_enc = aes.encrypt(crh)                                # L333-335 逐块
    body = http_get("%s&serverchallengeresp=%s" % (base, crh_enc.hex()))
    if xml_search(body, "paired") != "1":
        print("step3 paired != 1: %s" % body[:300])
        return 1
    ps_hex = xml_search(body, "pairingsecret")
    if not ps_hex:
        print("step3 无 pairingsecret")
        return 1
    pairing_secret = binascii.unhexlify(ps_hex)
    print("step3 OK: pairingsecret %d B" % len(pairing_secret))

    # ---------------- 验服务端签名 (L364) ----------------
    try:
        server_cert.public_key().verify(pairing_secret[16:], pairing_secret[:16],
                                        padding.PKCS1v15(), hashes.SHA256())
        print("      服务端签名验签 PASS")
    except Exception as e:
        print("      服务端签名验签 FAIL: %r" % (e,))
        return 1

    # ---------------- step 4: clientpairingsecret ----------------
    # L369-371: sign_it(client_secret_data, 16) —— 签的是 client_secret 本身
    signature = cli_key.sign(client_secret_data, padding.PKCS1v15(), hashes.SHA256())
    assert len(signature) == 256, len(signature)
    payload = client_secret_data + signature                  # L376-380
    print("step4 提交 %d B (secret 16 + sig 256); 签的是 client_secret" % len(payload))
    body = http_get("%s&clientpairingsecret=%s" % (base, payload.hex()))
    paired = xml_search(body, "paired")
    print("step4 paired=%s" % paired)
    print("      body=%s" % body[:300])
    if paired == "1":
        print("\n=== 配对成功 (reference 实现) ===")
        return 0
    print("\n=== 配对失败 (reference 实现同样失败) ===")
    return 1


if __name__ == "__main__":
    sys.exit(main())
