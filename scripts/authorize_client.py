#!/usr/bin/env python3
"""
scripts/authorize_client.py — 把客户端证书直接写入 Sunshine 的授权名单

为什么需要它
---------------------------------------------------------------------------
Sunshine v2025.924.154138 的 SRSAES 配对存在一个服务端侧缺陷 (本仓库已用逐字节
证据定位, 见 scripts/pair_analyze.py 的输出):

  * phase 2 服务端用双方一致的 AES 密钥解出我们的明文挑战, 并据此算出 clienthash
    —— 我们用 SHA256(明文挑战 ‖ 服务端证书签名 ‖ serversecret) 能**精确命中**
    服务端回包里的那 32 字节, 证明密钥/派生/PIN 全部正确;
  * 但服务端回包中 [32:48] 那 16 字节, 与它在 phase 4 用来重算哈希的
    sess.serverchallenge **不是同一个值** (而按源码两者同源), 于是 phase 4 的
    same_hash 永远为假 -> paired=0 -> 证书永远不会进入授权名单。

既然配对协议本身在服务端是坏的, 而又必须拿到已授权证书才能调用
/applist 与 /launch, 这里提供一条**明确、可逆、带备份**的替代路径:
把客户端证书直接写入 sunshine_state.json 的 root.named_devices。

⚠ 这是运维层面的绕过, 不具备 SRSAES 的中间人防护语义。一旦 Sunshine 修好配对,
   请优先使用 scripts/pair_sunshine.py 走正规流程, 并删除本工具写入的条目。

用法
---------------------------------------------------------------------------
    python3 authorize_client.py add    <clientcert.pem> <name> [state.json]
    python3 authorize_client.py remove <name>                  [state.json]
    python3 authorize_client.py dedup                           [state.json]
    python3 authorize_client.py list                            [state.json]

⚠ 同一张证书绝不能在名单里出现两条: Sunshine v2026.x 的
  nvhttp::is_client_enabled() 遇到重复匹配会直接返回 false, 结果是
  401 Certificate verification failed。用 `dedup` 清理。

默认 state.json：Windows 上是 D:\\tool\\sunshine\\config\\sunshine_state.json，
WSL 上是 /mnt/d/tool/sunshine/config/sunshine_state.json —— 按平台自动选，
也可以用环境变量 SUNSHINE_STATE 覆盖。
每次写入都会先生成 <state.json>.bak-<时间戳> 备份。
"""
import json
import os
import shutil
import sys
import time

# Windows 的控制台默认是 GBK：本文档里有 ⚠ 这类字符，直接 print 会
# UnicodeEncodeError 并把 `--help`/用法打印整个弄崩（2026-09-21 实测）。
# 只放宽 errors、**不动 encoding** —— 中文仍然正常显示，编不出来的符号变成 '?'。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):     # 非标准流 / 老解释器
        pass


def _default_state():
    """默认 state.json：优先环境变量，其次按平台猜。"""
    env = os.environ.get("SUNSHINE_STATE")
    if env:
        return env
    if sys.platform == "win32":
        return r"D:\tool\sunshine\config\sunshine_state.json"
    return "/mnt/d/tool/sunshine/config/sunshine_state.json"


DEFAULT_STATE = _default_state()


def load(path):
    if not os.path.isfile(path):
        raise SystemExit(
            "找不到 Sunshine 的 state.json: %s\n"
            "  · Windows 默认找 D:\\tool\\sunshine\\config\\sunshine_state.json\n"
            "  · WSL 默认找 /mnt/d/tool/sunshine/config/sunshine_state.json\n"
            "  · 别的路径用环境变量 SUNSHINE_STATE 指定, 或作为最后一个参数传进来"
            % path
        )
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save(path, data):
    bak = "%s.bak-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(path, bak)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
    os.replace(tmp, path)
    return bak


def _cert_sha(cert_pem):
    """证书 DER 的 SHA256 前 16 hex, 用于识别重复条目 (只用标准库)。"""
    import base64
    import hashlib
    import re
    m = re.search(r"-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----",
                  cert_pem or "", re.S)
    if not m:
        return None
    try:
        der = base64.b64decode(re.sub(r"\s+", "", m.group(1)))
    except Exception:
        return None
    return hashlib.sha256(der).hexdigest()[:16]


def cmd_list(state):
    d = load(state)
    nd = d.get("root", {}).get("named_devices", [])
    print("state=%s" % state)
    print("named_devices = %d" % len(nd))
    seen = {}
    for i, e in enumerate(nd):
        sha = _cert_sha(e.get("cert"))
        seen.setdefault(sha, []).append(i)
        print("  [%d] name=%-16s uuid=%-40s certlen=%-5d enabled=%-5s cert=%s"
              % (i, e.get("name"), e.get("uuid"), len(e.get("cert") or ""),
                 e.get("enabled", True), sha or "?"))
    # ⚠ Sunshine 的 nvhttp::is_client_enabled() 对**同一张证书出现多条**时直接
    #   返回 false (源码: `if (matched || !named_cert.enabled) return false;`)。
    #   重复条目会让该证书彻底无法通过 401 校验 —— 必须清成唯一一条。
    dups = {k: v for k, v in seen.items() if k is not None and len(v) > 1}
    if dups:
        print("\n!! 检测到重复证书条目 (会导致 401 Certificate verification failed):")
        for sha, idxs in dups.items():
            names = [nd[i].get("name") for i in idxs]
            print("   cert=%s 出现在索引 %s (name=%s)" % (sha, idxs, names))
        print("   修正: python3 authorize_client.py dedup   (每张证书只留最后一条)")
    return 0


def cmd_dedup(state):
    """删除重复证书条目, 每张证书只保留最后一条 (通常是最新配对写入的那条)。"""
    d = load(state)
    root = d.setdefault("root", {})
    nd = root.get("named_devices", [])
    last = {}
    for i, e in enumerate(nd):
        last[_cert_sha(e.get("cert"))] = i
    keep = [e for i, e in enumerate(nd) if last.get(_cert_sha(e.get("cert"))) == i]
    removed = len(nd) - len(keep)
    if removed == 0:
        print("没有重复条目")
        return 0
    root["named_devices"] = keep
    bak = save(state, d)
    print("已移除 %d 条重复条目 (每张证书保留最后一条)" % removed)
    print("备份: %s" % bak)
    print("重启 Sunshine 后生效。")
    return cmd_list(state)


def cmd_add(state, cert_path, name):
    with open(cert_path, "r", encoding="utf-8") as f:
        cert = f.read()
    if "-----BEGIN CERTIFICATE-----" not in cert:
        print("FATAL: %s 不是 PEM 证书" % cert_path)
        return 2

    d = load(state)
    root = d.setdefault("root", {})
    nd = root.setdefault("named_devices", [])

    for e in nd:
        if (e.get("cert") or "").strip() == cert.strip():
            print("该证书已存在 (name=%r), 不重复添加" % e.get("name"))
            return 0

    import uuid as _uuid
    entry = {"name": name, "cert": cert, "uuid": str(_uuid.uuid4()).upper()}
    nd.append(entry)
    bak = save(state, d)
    print("已添加: name=%r uuid=%s" % (name, entry["uuid"]))
    print("备份: %s" % bak)
    print("现在必须重启 Sunshine 服务才会重新载入该名单。")
    return cmd_list(state)


def cmd_remove(state, name):
    d = load(state)
    root = d.get("root", {})
    nd = root.get("named_devices", [])
    keep = [e for e in nd if e.get("name") != name]
    if len(keep) == len(nd):
        print("没有名为 %r 的条目" % name)
        return 1
    root["named_devices"] = keep
    bak = save(state, d)
    print("已移除 %d 条 name=%r" % (len(nd) - len(keep), name))
    print("备份: %s" % bak)
    return 0


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    op = sys.argv[1]
    if op == "add" and len(sys.argv) >= 4:
        state = sys.argv[4] if len(sys.argv) > 4 else DEFAULT_STATE
        return cmd_add(state, sys.argv[2], sys.argv[3])
    if op == "remove" and len(sys.argv) >= 3:
        state = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_STATE
        return cmd_remove(state, sys.argv[2])
    if op == "list":
        state = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_STATE
        return cmd_list(state)
    if op == "dedup":
        state = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_STATE
        return cmd_dedup(state)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
