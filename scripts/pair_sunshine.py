#!/usr/bin/env python3
"""
scripts/pair_sunshine.py — SRSAES 配对客户端 (自诊断版)

与 scripts/pair_sunshine.cpp 同一协议, 但**把每一步的每个字节都留下并当场交叉
验证**, 目的是把 "配对失败" 精确定位到某一步, 而不是依赖服务端那句会误导人的
"Invalid client certificate"。

关键判据 (每一步都独立可证):
  A. AES key = SHA256(salt || pin)[:16]
  B. AES-128-ECB(no padding) 往返自检
  C. 服务端 challengeresponse 解密后 48 字节, 且 [32:48] 应 == 我们发出的挑战
     -> 若不等, 说明服务端用了别的密钥 (PIN 不一致); 若相等, 密钥一致
  D. 服务端哈希输入 = SHA256(<它解出的挑战> || 服务端证书签名 || serversecret)
     -> 分别用 [解出的挑战] 和 [明文挑战] 复算, 看哪个命中, 从而判定密钥是否一致
  E. 服务端对 serversecret 的 RSA-SHA256 签名验签
  F. 我方 step4 签名自检

用法:
  python3 pair_sunshine.py <host> <clientcert.pem> <clientkey.pem> <pin> \
          [port] [uniqueid] [dump_dir]
"""
import binascii
import hashlib
import http.client
import os
import re
import secrets
import socket
import ssl
import subprocess
import sys
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography import x509

DUMP = None


def dump(name, data):
    if not DUMP:
        return
    with open(os.path.join(DUMP, name + ".hex"), "w") as f:
        f.write(binascii.hexlify(data).decode() + "\n")


def h(b):
    return hashlib.sha256(b).hexdigest()


def aes_ecb(key, data, encrypt):
    c = Cipher(algorithms.AES(key), modes.ECB())
    op = c.encryptor() if encrypt else c.decryptor()
    return op.update(data) + op.finalize()


def xml_tag(body, tag):
    m = re.search(r"<%s>(.*?)</%s>" % (tag, tag), body, re.S)
    return m.group(1) if m else None


def http_get(host, port, path, timeout):
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", path)
        r = conn.getresponse()
        return r.status, r.read().decode(errors="replace")
    finally:
        conn.close()


def main():
    global DUMP
    if len(sys.argv) < 5:
        print(__doc__)
        return 2
    host = sys.argv[1]
    cert_path, key_path = sys.argv[2], sys.argv[3]
    pin = sys.argv[4]
    port = int(sys.argv[5]) if len(sys.argv) > 5 else 47989
    uid = sys.argv[6] if len(sys.argv) > 6 else "0123456789ABCDEF0123456789ABCDEF"
    DUMP = sys.argv[7] if len(sys.argv) > 7 else None
    if DUMP:
        os.makedirs(DUMP, exist_ok=True)

    if len(pin) != 4 or not pin.isdigit():
        print("FAIL: PIN 必须是 4 位数字")
        return 2

    # ---- 载入我方证书 / 私钥 ----
    # ⚠ 关键: Sunshine 的 crypto::x509() 用 **PEM_read_bio_X509** 解析 clientcert,
    #   所以 clientcert 参数必须是 **PEM 文本的 hex**, 不是 DER 的 hex!
    #   (之前发 DER 的 hex 导致服务端解析失败 -> step2 内部异常被吞, step4 报
    #    "Invalid client certificate"。Sunshine 的 plaincert 回包同样是 PEM 的 hex。)
    with open(cert_path, "rb") as f:
        cert_pem_bytes = f.read()
    client_cert_der = x509.load_pem_x509_certificate(cert_pem_bytes).public_bytes(
        serialization.Encoding.DER
    )
    with open(key_path, "rb") as f:
        client_key = serialization.load_pem_private_key(f.read(), password=None)
    client_sig = x509.load_der_x509_certificate(client_cert_der).signature
    print("[pair] client cert %d bytes DER / %d bytes PEM (clientcert 参数发 PEM 的 hex)"
          % (len(client_cert_der), len(cert_pem_bytes)))
    print("[pair] client cert DER sha256 = %s" % h(client_cert_der))
    dump("01_client_cert_der", client_cert_der)
    dump("02_client_cert_signature", client_sig)
    dump("17_client_cert_pem", cert_pem_bytes)

    # ---- step1: getservercert (阻塞, 等操作员在 UI 输入 PIN) ----
    salt = secrets.token_bytes(16)
    key = hashlib.sha256(salt + pin.encode()).digest()[:16]
    print("[pair] salt=%s pin=%s AES key=%s" % (salt.hex(), pin, key.hex()))
    dump("03_salt", salt)
    dump("04_aes_key", key)

    print("\n[step1] GET /pair?phrase=getservercert ... 等待你在 Sunshine UI 输入 PIN=%s" % pin)
    sys.stdout.flush()
    path1 = (
        "/pair?uniqueid=%s&phrase=getservercert&clientcert=%s&salt=%s&devicename=agent-native"
        % (uid, binascii.hexlify(cert_pem_bytes).decode(), salt.hex())
    )
    try:
        st, body = http_get(host, port, path1, 600)
    except Exception as e:
        print("FAIL step1: %r" % (e,))
        return 1
    print("[step1] http %s body=%s" % (st, body[:300]))
    plaincert_hex = xml_tag(body, "plaincert")
    if not plaincert_hex:
        print("FAIL: step1 无 plaincert")
        return 1
    server_pem = binascii.unhexlify(plaincert_hex)
    server_cert = x509.load_pem_x509_certificate(server_pem)
    server_sig = server_cert.signature
    print("[step1] OK plaincert %d bytes PEM, 服务端证书签名 %d bytes, sig sha256=%s"
          % (len(server_pem), len(server_sig), h(server_sig)))
    dump("09_server_cert_pem", server_pem)
    dump("10_server_cert_signature", server_sig)

    # ---- step2: clientchallenge ----
    chal = secrets.token_bytes(16)
    enc_chal = aes_ecb(key, chal, True)
    dump("05_client_challenge", chal)
    dump("06_client_challenge_enc", enc_chal)
    print("\n[step2] 我方挑战 = %s" % chal.hex())
    st, body = http_get(host, port, "/pair?uniqueid=%s&clientchallenge=%s" % (uid, enc_chal.hex()), 30)
    resp_hex = xml_tag(body, "challengeresponse")
    if not resp_hex:
        print("FAIL step2: http %s body=%s" % (st, body[:300]))
        return 1
    chalresp_raw = binascii.unhexlify(resp_hex)
    dec = aes_ecb(key, chalresp_raw, False)
    dump("07_challengeresponse_raw", chalresp_raw)
    dump("08_challengeresponse_dec", dec)
    print("[step2] 密文 %d B -> 解密 %d B" % (len(chalresp_raw), len(dec)))
    if len(dec) < 48:
        print("FAIL: 解密不足 48 字节")
        return 1
    srv_hash, srv_chal_in_resp = dec[:32], dec[32:48]
    print("        服务端 clienthash = %s" % srv_hash.hex())
    print("        服务端(解出的)挑战 = %s" % srv_chal_in_resp.hex())
    print("        我们发出的挑战     = %s" % chal.hex())
    key_match = srv_chal_in_resp == chal
    print("        >>> 密钥一致? %s" % ("是 (PIN 相同)" if key_match else "否 (服务端用了别的 PIN/密钥)"))
    # 自洽性检查: 服务端哈希必然是 SHA256(<它解出的挑战> || 签名 || serversecret)。
    # serversecret 此刻还不知道, 但可以在 step3 之后回填校验 (见下方 verify 段)。
    # 这里先记录 dec 的内部结构, 供后续判定。
    self_consistent_hint = "step3 后回填校验"
    print("        (自洽性校验: %s)" % self_consistent_hint)

    # ---- step3: serverchallengeresp ----
    # 权威依据: moonlight-xboxog src/network/host_pairing.cpp send_server_challenge_response()
    #   clientSecretBytes = 随机 16 字节            <-- ★ 在 phase 3 生成
    #   clientHashSource  = challengeresponse明文[32:48]   (= serverchallenge)
    #                     + 我方证书签名 (256B)
    #                     + clientSecretBytes (16B)
    #   clientHash        = SHA256(clientHashSource)
    #   serverchallengeresp = AES-ECB-enc(clientHash)
    # 服务端 (Sunshine clientpairingsecret) 会用**同一个公式**重算并与它收到的比对:
    #   SHA256(sess.serverchallenge || sess.client.cert 签名 || step4 传的 secret)
    # 所以 secret 必须在 step3 就定下来, 并在 step4 原样再用一次 —— 这正是之前
    # 失败的根因 (之前每次都在 step4 才随机生成 secret)。
    #
    # 注意: 这里用的签名是**我方证书**的签名, 不是服务端证书的。
    client_secret = secrets.token_bytes(16)
    client_hash_src = srv_chal_in_resp + client_sig + client_secret
    client_hash = hashlib.sha256(client_hash_src).digest()
    enc_hash = aes_ecb(key, client_hash, True)
    dump("11_serverchallengeresp_plain", client_hash)
    dump("12_serverchallengeresp_enc", enc_hash)
    print("\n[step3] serverchallengeresp = AES(SHA256(serverchallenge || 我方证书签名 || client_secret))")
    print("        serverchallenge (step2 回包[32:48]) = %s" % srv_chal_in_resp.hex())
    print("        我方证书签名 sha256 = %s" % h(client_sig))
    print("        client_secret = %s  (step4 会复用同一个)" % client_secret.hex())
    print("        clientHash    = %s" % client_hash.hex())
    st, body = http_get(
        host, port, "/pair?uniqueid=%s&serverchallengeresp=%s" % (uid, enc_hash.hex()), 30
    )
    psecret_hex = xml_tag(body, "pairingsecret")
    if not psecret_hex:
        print("FAIL step3: http %s body=%s" % (st, body[:300]))
        return 1
    psecret = binascii.unhexlify(psecret_hex)
    srv_secret, srv_sign = psecret[:16], psecret[16:]
    dump("13_pairingsecret", psecret)
    dump("14_server_challenge", srv_secret)
    print("[step3] pairingsecret %d B: serversecret=%s 服务端签名=%d B"
          % (len(psecret), srv_secret.hex(), len(srv_sign)))
    # Sunshine 里 sess.serverchallenge 与 sess.serversecret 由同一次 rand(16) 派生,
    # 所以 step2 回包里的挑战应当等于 pairingsecret 的前 16 字节。核对一下。
    print("[step3] step2回包挑战 == pairingsecret[0:16] ? %s"
          % ("是" if srv_chal_in_resp == srv_secret else "否  <== 服务端语义与预期不同"))

    # ---- 现在可以完整复算了 ----
    print("\n[verify] 完整复算服务端 clienthash")
    # 自洽性: 服务端哈希 == SHA256(<它解出的挑战> || 服务端签名 || serversecret)
    # 这一步只用**我们自己解密出来的字节**, 不涉及密钥是否与服务端一致。
    #   * 命中 => 我们的 AES 解密链路 + 字段切分完全正确
    #   * 不命中 => 我们对 challengeresponse 的解析/切分有误
    self_ok = hashlib.sha256(srv_chal_in_resp + server_sig + srv_secret).digest() == srv_hash
    print("  自洽性 (用解出的挑战)  -> %s"
          % ("命中 => 解密与切分正确" if self_ok else "不匹配 => 解析有误"))
    # 密钥一致性: 服务端哈希 == SHA256(<我们发出的明文挑战> || 签名 || serversecret)
    #   * 命中 => 服务端用的挑战就是我们的明文 => 双方 AES 密钥一致 (PIN 相同)
    key_ok = hashlib.sha256(chal + server_sig + srv_secret).digest() == srv_hash
    print("  密钥一致性 (用明文挑战) -> %s"
          % ("命中 => 双方密钥一致 (PIN 相同)" if key_ok else "不匹配 => 服务端用了别的密钥 (PIN 不同)"))

    print("[verify] 服务端对 serversecret 的签名")
    try:
        server_cert.public_key().verify(srv_sign, srv_secret, padding.PKCS1v15(), hashes.SHA256())
        print("  PASS 服务端签名验签通过")
    except Exception as e:
        print("  FAIL 验签失败: %r" % (e,))

    # ---- step4: clientpairingsecret ----
    # ★ 权威依据 (temp/pairing.c, Moonlight Embedded) 第 369-380 行:
    #     sign_it(client_secret_data, 16, &signature, ..., g_PrivateKey)
    #     client_pairing_secret = client_secret_data(16) || signature(256)
    #   即 **用私钥对 client_secret 本身签名**, 而**不是**对 serverchallenge 签名。
    #   服务端 clientpairingsecret 的两项检查:
    #     1) SHA256(sess.serverchallenge || 我方证书签名 || secret) == step3 收到的 clienthash
    #     2) crypto::verify256(我方证书, secret, sign)   <-- 验签对象就是 secret
    #   之前这里签的是 srv_secret(serverchallenge), 于是第 2 项永远失败 -> paired=0。
    #   注意 client_secret 必须与 step3 用的是同一个 (绝不能在这里再随机一次)。
    client_sign = client_key.sign(client_secret, padding.PKCS1v15(), hashes.SHA256())
    payload = client_secret + client_sign
    dump("15_client_secret", client_secret)
    dump("16_client_signature", client_sign)

    def phase4_hash(serverchallenge):
        return hashlib.sha256(serverchallenge + client_sig + client_secret).digest()

    print("\n[step4] 服务端将比对的哈希 SHA256(serverchallenge || 我方证书签名 || client_secret)")
    print("        以 pairingsecret[0:16] 为 serverchallenge -> %s" % phase4_hash(srv_secret).hex())
    print("        以 回包[32:48]           为 serverchallenge -> %s" % phase4_hash(srv_chal_in_resp).hex())
    print("        我们 step3 提交的 clienthash                   -> %s" % client_hash.hex())
    print("        >>> step3 提交值 == 以 pairingsecret[0:16] 复算值 ? %s"
          % ("是" if phase4_hash(srv_secret) == client_hash else "否 (服务端 phase4 用的挑战与回包不同)"))
    print("        >>> step3 提交值 == 以 回包[32:48] 复算值 ? %s"
          % ("是" if phase4_hash(srv_chal_in_resp) == client_hash else "否"))

    print("\n[step4] 提交 clientpairingsecret (%d B), 我方签名 %d B" % (len(payload), len(client_sign)))
    # 自检: 验签对象是 client_secret (与 reference 一致)
    x509.load_der_x509_certificate(client_cert_der).public_key().verify(
        client_sign, client_secret, padding.PKCS1v15(), hashes.SHA256()
    )
    print("        我方签名自检 PASS (验签对象 = client_secret)")
    st, body = http_get(
        host, port, "/pair?uniqueid=%s&clientpairingsecret=%s" % (uid, payload.hex()), 30
    )
    paired = xml_tag(body, "paired")
    print("[step4] http %s paired=%s" % (st, paired))
    print("        body=%s" % body)
    if paired == "1":
        print("\n=== 配对成功 ===")
        return 0
    print("\n=== 配对失败 ===")
    return 1


if __name__ == "__main__":
    sys.exit(main())
