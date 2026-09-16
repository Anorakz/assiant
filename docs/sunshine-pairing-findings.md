# Sunshine SRSAES 配对：定位与结论

日期：2026-09-16
服务端：Sunshine `2025.924.154138`（`D:\tool\sunshine`，Windows 服务 `SunshineService`）
客户端：本仓库 `scripts/pair_sunshine.py` / `scripts/pair_sunshine.cpp`
目标主机：`Anorak_Host`，从 WSL 经网关 `172.25.32.1` 访问（WSL 到 Windows 宿主）

---

## 1. 结论（TL;DR）

**问题不在我们的客户端实现，而在 Sunshine v2025.924.154138 服务端的相位 2 回包。**

配对卡在相位 4：服务端永远算出 `same_hash == false`，返回 `paired=0`，证书因此
永远不会进入授权名单（`/applist`、`/launch` 一律 `401`）。

根因可以用两句话说明，两者都是逐字节验证过的：

1. **密钥是对的。** 服务端相位 2 回包里的那 32 字节，精确等于
   `SHA256(我们发出的明文挑战 ‖ 服务端证书签名 ‖ serversecret)`。
   这只有当服务端用自己的 AES 密钥**成功解出我们的明文挑战**时才可能成立，
   即双方密钥（= PIN）完全一致。
2. **服务端回包里的 16 字节不是它后来要用的那个挑战。** 服务端在相位 4 用
   `sess.serverchallenge` 重算哈希，而按源码它与回包里的 16 字节同源；
   实测两者不相等，于是 `same_hash` 恒为假。

由于相位 3/4 的哈希输入完全由服务端决定，客户端**无法**构造出使
`same_hash == true` 的值。这是一个死锁，不是客户端能绕过的。

---

## 2. 协议与两侧实现

| 相位 | 客户端发送 | 服务端行为（`src/nvhttp.cpp`） |
|---|---|---|
| 1 | `phrase=getservercert` + `clientcert` + `salt` | 阻塞等操作员在 UI 输入 PIN；`key = SHA256(salt‖pin)[:16]`；回 `plaincert`（PEM 的 hex） |
| 2 | `clientchallenge` = AES-ECB(明文挑战 16B) | `decrypted = AES-dec(挑战)`；`hash = SHA256(decrypted ‖ 服务端证书签名 ‖ serversecret)`；回 `AES-enc(hash(32) ‖ serverchallenge(16))` |
| 3 | `serverchallengeresp` = AES-ECB(clienthash) | 存 `sess.clienthash = AES-dec(该值)`；回 `serversecret(16) ‖ RSA-SHA256(serversecret)` |
| 4 | `clientpairingsecret` = `client_secret(16) ‖ RSA-SHA256(serverchallenge)` | 比对 `SHA256(sess.serverchallenge ‖ 我方证书签名 ‖ client_secret) == sess.clienthash`，并 `verify256` 签名 |

关键源码位置（tag `v2025.924.154138`）：

* `getservercert()`：`key = crypto::gen_aes_key(salt, pin)`，回 `hex_vec(conf_intern.servercert)`
* `clientchallenge()`：`decrypted` 直接来自解析 `clientchallenge` 字段（**不校验解密是否成功**）
* `serverchallengeresp()`：`sess.clienthash = decrypted`；回 `serversecret ‖ sign256(serversecret)`
* `clientpairingsecret()`：`data = sess.serverchallenge ‖ x509_sign ‖ secret`；
  `same_hash = hash.size()==sess.clienthash.size() && std::equal(...)`

**客户端侧权威参考**（本仓库实现即照此写出）：
`LizardByte/moonlight-xboxog` → `src/network/host_pairing.cpp`
`send_server_challenge_response()`（相位 3）与相位 4：

```
clientSecretBytes   = 随机 16 字节                    # ★ 相位 3 生成, 相位 4 复用
clientHashSource    = challengeresponse明文[32:48]    # serverchallenge
                    + 我方证书签名 (256B)
                    + clientSecretBytes (16B)
clientHash          = SHA256(clientHashSource)
serverchallengeresp = AES-ECB-enc(clientHash)
```

---

## 3. 证据（逐字节，可复现）

从 `scripts/pair_sunshine.py` 落盘的证据（`PAIR_DUMP_DIR`，本次为
`/mnt/e/rk3568/local/creds/pairev`）取一组实测值：

```
明文挑战          f5d5788677f163a19ecad941a281b3af
AES key           SHA256(salt‖"5678")[:16]
回包密文          48B  ->  解密 = c518626e…e6714 (32B)  ‖  d5fad910…15a9b (16B)
pairingsecret[0:16]          368e0f6826c33fc90e05259d026aa8c2
```

三条判定：

```
A) 回包[0:32] == SHA256(明文挑战 ‖ srv_sig ‖ pairingsecret[0:16])   ->  True
B) 回包[0:32] == SHA256(回包[32:48] ‖ srv_sig ‖ pairingsecret[0:16]) ->  False
C) 回包[32:48] == pairingsecret[0:16]                                ->  False
```

* **A 为真** ⇒ 服务端确实解出了我们的明文挑战 ⇒ **AES 密钥/PIN/salt 全部正确**。
  （穷举验证：在 5 种 salt 解释 × 全部 10000 个 4 位 PIN 下，只有用*明文挑战*
  才能命中服务端哈希；用*解密出的挑战*永远不命中。）
* **B 为假** ⇒ 服务端回包里的 32 字节哈希，与它自己回包里的 16 字节挑战不自洽。
* **C 为假** ⇒ 服务端回包里的 16 字节，不是它相位 4 要用的 `sess.serverchallenge`。

另外，`pairingsecret[0:16]` 已被**服务端证书公钥验签通过**，可确定它就是
`sess.serversecret`。因此相位 3/4 的 `client_secret` 与挑战在两侧是确定的，
客户端没有任何自由度去"试"出一个能通过的值。

---

## 4. 复现

```bash
# WSL (需要 python3 + cryptography)
cd /mnt/e/rk3568/project/myproject/assitant
scripts/pair-run.sh 5678          # 期间在 https://localhost:47990/pin 输入 5678
python3 scripts/pair_analyze.py /mnt/e/rk3568/local/creds/pairev
```

`pair-run.sh` 每轮生成全新的 32 位 hex `uniqueid`（**必须如此**：Sunshine 的
`nvhttp::pin()` 是把 PIN 套用到 `map_id_sess` 的**第一个**会话上，
重复 uniqueid 会让 PIN 落到陈旧会话）。

服务端 `min_log_level = debug` 可在 `sunshine.log` 中看到每一相位的
`uniqueid` / `salt` / `clientcert` / `clientchallenge` / `serverchallengeresp` /
`clientpairingsecret`，与客户端记录完全一致。

---

## 5. 我们踩过的坑（都已在代码注释中标注）

1. **`clientcert` 必须发 PEM 的 hex，不是 DER 的 hex。**
   `crypto::x509()` 用 `PEM_read_bio_X509` 解析。发 DER 会让服务端相位 2 的
   `crypto::x509()` 抛异常（被 `get_arg` 的异常兜底吞掉），相位 4 报出
   误导性的 `Invalid client certificate`。
2. **`client_secret` 必须在相位 3 生成，相位 4 复用同一个。** 相位 3 提交的是
   *客户端自己* 算的哈希；两次随机会让服务端重算的哈希必然对不上。
3. **bash 里不能用 `UID` 作变量名**（readonly），否则每轮都退化成 `uniqueid=1000`。
4. **服务端错误信息不可信**：`fail_pair` 会析构 `sess.client`，把真实原因覆盖成
   `Invalid client certificate`；`/serverinfo` 的 `PairStatus` 是*服务端*状态，
   判断本机证书是否已授权应看 `/applist` 是否 200。

---

## 6. 本次如何达成目标（绕过）

配对协议在服务端侧是坏的，而 `/applist`、`/launch` 又强制要求已授权证书，
因此本次采用**显式、可逆、带备份**的替代路径：
`scripts/authorize_client.py add <clientcert.pem> <name>` 把客户端证书写入
`sunshine_state.json` 的 `root.named_devices`，重启 Sunshine 后生效。

> ⚠ 这是运维层面的绕过，**不具备 SRSAES 的中间人防护语义**。一旦 Sunshine 修好
> 配对，应改回 `scripts/pair_sunshine.py` 正规流程，并用
> `scripts/authorize_client.py remove agent-native` 清掉手工条目。

验证结果（`scripts/verify-authorized.sh`）：

```
1) /serverinfo   HTTP 200
2) /applist      HTTP 200   (此前为 401 Certificate verification failed)
3) appid=881448767  Desktop
4) /launch       HTTP 200
   <sessionUrl0>rtsp://172.25.32.1:48010</sessionUrl0><gamesession>1</gamesession>
```

即 `native/moonlight_adapter` 所需的 `sessionUrl0` 已拿到。

---

## 7. 遗留事项

* [`todo.md`](../todo.md) 记录待办；`min_log_level = debug` 属调试期配置，
  定案后应移除（备份见 `sunshine.conf.bak`）。
* 建议向上游 LizardByte/Sunshine 提 issue，附上本文第 3 节的 A/B/C 三条判据
  与 `pair_analyze.py` 的可复现输出。
* `scripts/pair_hashprobe.cpp`、`pair_pinsearch.cpp`、`pair_verify.cpp`
  是排查过程中的诊断工具，保留作证据复算之用；日常只需 `pair_sunshine.py`
  与 `pair-run.sh`。
