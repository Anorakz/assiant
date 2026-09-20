# Sunshine SRSAES 配对：定位与结论（已修正）

日期：2026-09-16
服务端：Sunshine **`2026.914.233613`**（升级前 `2025.924.154138`）
客户端：本仓库 `scripts/pair_sunshine.py` / `pair_sunshine.cpp`（已修正）
参考实现：`temp/pairing.c`（Moonlight Embedded，Iwan Timmer）
目标主机：`Anorak_Host`，从 WSL 经网关 `172.25.32.1` 访问

---

## 0. 最终验证（正规配对，无任何绕过）

```
1) /serverinfo   HTTP 200
2) /applist      HTTP 200   <App>Desktop(881448767)</App><App>Steam Big Picture(1093255277)</App>
3) /launch       HTTP 200   <sessionUrl0>rtsp://172.25.32.1:48010</sessionUrl0>
                            <gamesession>1</gamesession>
```

`scripts/pair_sunshine.py` 与 `scripts/pair_ref.py` 两个独立实现均返回 `paired=1`。

---

## 1. 结论（TL;DR）

**配对协议没问题，Sunshine 也没问题。之前一直失败是两个客户端侧错误叠加：**

### 错误 A（致命）：第 4 步签名对象签错

* ❌ 错：`clientpairingsecret` 里签的是 **serverchallenge**
* ✅ 对：签的是 **client_secret**（`temp/pairing.c` 第 371 行
  `sign_it(client_secret_data, 16, &signature, &s_len, g_PrivateKey)`）

服务端 `clientpairingsecret` 有两项检查，第 2 项是
`crypto::verify256(x509(client.cert), secret, sign)` —— 验签对象是 **secret**。
签了 serverchallenge，第 2 项恒假，`if (same_hash && verify)` 永不成立，
`paired` 恒为 0。

### 错误 B（授权阶段）：同一张证书在名单里出现两条

新版 Sunshine（`2026.914.233613`）的 `nvhttp::is_client_enabled()`：

```cpp
if (matched || !named_cert.enabled) {
  return false;      // 同一张证书匹配到第二条 => 直接判定不可用
}
matched = true;
```

重复条目（早前手工注入的绕过条目 + 正规配对写入的条目）会让该证书**被主动否决**，
表现为 `401 Certificate verification failed`，即使证书本身完全正确、`enabled=true`。
用 `scripts/authorize_client.py dedup` 清理后即恢复 200。

> ⚠ 我此前在本文档里写的"服务端相位 2/4 内部自相矛盾、属 Sunshine 缺陷"是
> **错误结论**，已删除。两轮误判的真实原因就是上面 A、B 两条。
> 教训：自己的实现与权威参考不一致时，应先逐字节对照参考实现，而不是先怀疑服务端。

---

## 2. 协议与两侧实现（对照 `temp/pairing.c`）

| 相位 | 客户端（`pairing.c`） | 服务端（`src/nvhttp.cpp`） |
|---|---|---|
| 1 | `phrase=getservercert` + `clientcert`(PEM 的 hex) + `salt`(32 hex) → 校验 `<paired>1</paired>`，取 `plaincert` | 阻塞等操作员在 UI 输入 PIN；`key = SHA256(salt‖pin)[:16]`；回 `plaincert` |
| 2 | `clientchallenge` = AES-128-ECB(明文挑战 16B) | `decrypted = AES-dec(挑战)`；`hash = SHA256(decrypted ‖ 服务端证书签名 ‖ serversecret)`；回 `AES-enc(hash(32) ‖ serverchallenge(16))` |
| 3 | `serverchallengeresp` = AES-128-ECB(SHA256(回包[32:48] ‖ **我方证书签名** ‖ client_secret)) | `sess.clienthash = AES-dec(该值)`；回 `serversecret(16) ‖ RSA-SHA256(serversecret)` |
| 4 | `clientpairingsecret` = `client_secret(16) ‖ RSA-SHA256(**client_secret**)` | `same_hash = SHA256(sess.serverchallenge ‖ 我方证书签名 ‖ secret) == sess.clienthash`；`verify = verify256(我方证书, secret, sign)`；两者皆真才 `paired=1` |

要点：

* **`hash_length = 32`**（serverMajorVersion ≥ 7 用 SHA256，否则 SHA1/20）
* `client_secret` 在 **相位 3** 生成，相位 4 **复用同一个**（不能重新随机）
* 相位 4 的签名对象 = `client_secret`；被签数据与验签数据都是它
* `devicename` / `updateState` 服务端**不读**，可任意（参考实现写 `devicename=roth`）

---

## 3. 修正清单（本次改动）

`scripts/pair_sunshine.py` / `scripts/pair_sunshine.cpp`：

1. **相位 4 签名对象由 `serverchallenge` 改为 `client_secret`** ← 关键修复（错误 A）
2. `clientcert` 必须是 **PEM 的 hex**（不是 DER 的 hex）。
   Sunshine `crypto::x509()` 用 `PEM_read_bio_X509`；发 DER 会让相位 2 的
   `crypto::x509()` 抛异常（被异常兜底吞掉），相位 4 报误导性的
   `Invalid client certificate`。
3. `client_secret` 必须在相位 3 生成、相位 4 复用（曾两次各自随机）
4. `pair-run.sh` 不能用 `UID` 作变量名（bash 里是 readonly，会静默失败，
   让每轮都退化成同一个 `uniqueid`）

`scripts/authorize_client.py`：新增 `dedup` 子命令，并在 `list` 里检出
**重复证书条目**（错误 B），因为重复会让服务端主动否决该证书。

**授权名单必须保证同一张证书只有一条**；重复条目常见成因是"手工注入的绕过条目"
与"正规配对写入的条目"并存。

---

## 4. 复现

```bash
# WSL (python3 + cryptography)
cd /mnt/e/rk3568/project/myproject/assitant
scripts/pair-run.sh 5678        # 期间在 https://localhost:47990/pin 输入 5678
# 直接跑参考实现移植版:
python3 scripts/pair_ref.py 172.25.32.1 \
        /mnt/e/rk3568/local/creds/client.pem \
        /mnt/e/rk3568/local/creds/client.key 5678
```

成功输出：

```
step1 paired=1 → step2 OK → step3 OK (验签 PASS) → step4 paired=1
=== 配对成功 ===
```

⚠ 配对成功后**必须重启一次 Sunshine**：`add_cert->raise(...)` 把证书写进
`sunshine_state.json` 并刷新运行中的 `cert_chain`，但 HTTPS 侧的客户端证书
校验链是**启动时**从 state 载入的。不重启的话 `/applist` 仍会
`401 Certificate verification failed`。

---

## 5. 验证（`scripts/verify-authorized.sh`）

重启后（去重 + 正规配对完成）：

```
1) /serverinfo   HTTP 200
2) /applist      HTTP 200   <App>Desktop(881448767)</App><App>Steam Big Picture(1093255277)</App>
3) appid=881448767  Desktop
4) /launch       HTTP 200   <sessionUrl0>rtsp://172.25.32.1:48010</sessionUrl0>
                            <gamesession>1</gamesession>
```

若 `/launch` 返回 `400 An app is already running on this host`，那是**业务规则**
拒绝（主机上已有应用在跑，`state=SUNSHINE_SERVER_BUSY`），不是失败；
此时用 `/resume` 取当前会话地址。

---

### 5.1 握手分层：TLS 在 Python，会话在 C++（Phase 6 B1）

上面这串 curl 能成、而 Agent 当时连不上，差别只在**传输层**。同一台主机、同一时刻的对照
（2026-09-20；主机上另有一个 Moonlight 客户端正在串流）：

| 请求 | 明文 HTTP 47989（无证书） | HTTPS 47984 + 客户端证书 |
|---|---|---|
| `/serverinfo` | `<PairStatus>0</PairStatus>`，另带 `<state>SUNSHINE_SERVER_BUSY</state>`、`<currentgame>881448767</currentgame>` | `PairStatus=1`、`appversion=7.1.431.-1` |
| `/applist` | HTTP 200，但 `<root status_code="404"/>` —— 该接口不注册在明文端口上 | 两个应用：`Desktop=881448767`、`Steam Big Picture=1093255277` |
| `/launch` | 同样 `<root status_code="404"/>` | 主机忙 → `400 An app is already running on this host`；改 `/resume` → `200 <sessionUrl0>rtsp://…:48010</sessionUrl0><resume>1</resume>` |

> ⚠ Sunshine 的"404"是 **HTTP 200 + XML `<root status_code="404"/>`** —— 只看 HTTP 状态码
> 会以为成功了。`native/moonlight_connection.cpp` 取的是 XML 里的 `status_code`，所以它报
> `handshake failed: /launch status=404`，两者并不矛盾。
> `/serverinfo` 还会返回 `<HttpsPort>47984</HttpsPort>`：HTTPS 端口本来就是主机告诉我们的。

分层实现（`agent/net/sunshine_client.py` + `native/moonlight_adapter.*`）：

| 步骤 | 谁做 | 怎么做的 |
|---|---|---|
| `/serverinfo` `/applist` `/launch` `/resume` | **Python** | `ssl.SSLContext(PROTOCOL_TLS_CLIENT)` + `load_cert_chain`，`verify_mode=CERT_NONE`（Sunshine 用自签证书，等价于 `curl -sk`）；证书/私钥缺了或坏了在**构造时**就报，不拖到握手 |
| `LiStartConnection` | **C++** | `moonlight.start_with_session(app_version, gfe_version, codec_mode_support, session_url)` —— **一次 HTTP 都不发** |
| `LiGetLaunchUrlQueryParameters()` | C++ 提供，Python 使用 | 经 `moonlight.launch_url_query_parameters()` 导出（本版本返回 `&corever=1`），避免把"Sunshine 扩展参数"抄一份进 Python |

* **为什么不在 C++ 里做 TLS**：得给交叉编译再引一个 OpenSSL，而 Python 的 `ssl` 本来就在。
  `native/moonlight_connection.cpp` 那条明文路径**保留**，留给无 TLS 的 GFE 主机。
* **共存**：主机上已有应用时 `/launch` 的 400 不是失败（见上），`start_session()` 会自动退到
  `/resume` 加入**同一个**会话，画面与对方一致；反向也安全 —— 我们 `stop()` 只断开自己的
  RTSP 客户端，Sunshine 要等最后一个客户端断开才收应用，不会把对方踢下线。
* 绑定是否真的通向库，由 `tests/test_native_integration.py` 在**板端**验（它按架构自跳，
  PC/WSL 上是 skip）—— 因为 moonlight 只集成在交叉编译产物里，host 那份根本没有该符号。

---

## 6. 历史弯路（留档，避免重犯）

排查过程中我先后给出过两个**错误**结论，均已推翻：

1. "PIN 不匹配" —— 实际 AES 密钥一直是对的。判据：
   服务端回包[0:32] 恒等于 `SHA256(我们发出的**明文挑战** ‖ 服务端证书签名 ‖
   pairingsecret[0:16])`，说明服务端解出的正是我们的明文挑战。
2. "服务端相位 2/4 值不自洽，是 Sunshine 缺陷" —— 我错在把
   回包[32:48] 当成了服务端 phase4 要用的 challenge。真正的问题在
   **相位 4 的签名对象**，与哈希无关。

另外两个真实存在的坑（都在代码注释里）：

* `clientcert` 发 DER 会引发 `Invalid client certificate`（该报错具有误导性）
* Sunshine `nvhttp::pin()` 把 PIN 套到 `map_id_sess` 的**第一个**会话上，
  重复 `uniqueid` 会让 PIN 落到陈旧会话

---

## 7. 遗留事项

* `sunshine.conf` 里的 `min_log_level = debug` 是排查期的调试项，定位结束应移除
  （备份见 `sunshine.conf.bak`）；本次已从文件删除，下次重启生效。
* 授权名单里我们的证书曾同时存在 `agent-native`（早前手工注入的绕过条目）与
  `roth`（正规配对写入）两条，已被 `authorize_client.py dedup` 清理为一条。
  日常用 `authorize_client.py list` 检查是否又出现重复。
* `scripts/pair_ref.py` 是参考实现（`temp/pairing.c`）的忠实移植，保留作对照基线；
  日常使用 `scripts/pair_sunshine.py` + `pair-run.sh` 即可。
* `scripts/pair_verify.cpp` / `pair_hashprobe.cpp` / `pair_pinsearch.cpp`
  / `pair_analyze.py` / `pair_probe.py` 是排查期的诊断工具，保留作证据复算之用。
  注意其中 `pair_verify.cpp` 的结论段落基于当时的错误假设，仅供参考。
