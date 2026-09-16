#!/usr/bin/env bash
# scripts/verify-authorized.sh — 用已授权客户端证书验证 HTTPS(47984) 通路并取 sessionUrl0
#
# 前提: 客户端证书已在 Sunshine 授权名单里 (scripts/authorize_client.py 或正常配对),
#       且 Sunshine 已重启以载入名单。
#
# 用法: verify-authorized.sh [host] [https_port]
set -u

HOST="${1:-172.25.32.1}"
PORT="${2:-47984}"
CERT="${CLIENT_CERT:-/mnt/e/rk3568/local/creds/client.pem}"
KEY="${CLIENT_KEY:-/mnt/e/rk3568/local/creds/client.key}"
UID_="${PAIR_UID:-0123456789ABCDEF0123456789ABCDEF}"

CURL=(curl -sk -m 12 --cert "$CERT" --key "$KEY")

echo "=== 1) /serverinfo (HTTPS $PORT) ==="
"${CURL[@]}" -o /tmp/vi_serverinfo.xml -w "HTTP %{http_code}\n" \
    "https://$HOST:$PORT/serverinfo?uniqueid=$UID_"
head -c 400 /tmp/vi_serverinfo.xml; echo

echo
echo "=== 2) /applist (需要已授权证书) ==="
"${CURL[@]}" -o /tmp/vi_applist.xml -w "HTTP %{http_code}\n" \
    "https://$HOST:$PORT/applist?uniqueid=$UID_"
head -c 500 /tmp/vi_applist.xml; echo

echo
echo "=== 3) 解析 appid ==="
APPID=$(python3 - <<'PY'
import re, sys
try:
    s = open("/tmp/vi_applist.xml", errors="replace").read()
except Exception:
    print("")
    raise SystemExit
# Sunshine 会把整个 <App> 输出在一行, 所以不能要求标签间有换行/空白
apps = re.findall(r"<App>(.*?)</App>", s, re.S)
ids = []
for a in apps:
    t = re.search(r"<AppTitle>(.*?)</AppTitle>", a, re.S)
    i = re.search(r"<ID>(.*?)</ID>", a, re.S)
    if i:
        ids.append((t.group(1).strip() if t else "?", i.group(1).strip()))
for t, i in ids:
    print("   appid=%-12s title=%s" % (i, t), file=sys.stderr)
desk = [i for t, i in ids if t.lower() == "desktop"]
print(desk[0] if desk else (ids[0][1] if ids else ""))
PY
)
echo "选中 appid=$APPID"

if [ -z "$APPID" ]; then
    echo "没有解析到 appid, 跳过 /launch"
    exit 1
fi

echo
echo "=== 4) /launch (取 sessionUrl0) ==="
"${CURL[@]}" -o /tmp/vi_launch.xml -w "HTTP %{http_code}\n" \
    "https://$HOST:$PORT/launch?uniqueid=$UID_&appid=$APPID&mode=1280x720x60&rikey=00000000000000000000000000000000&rikeyid=0&localAudioPlayMode=0&surroundAudioInfo=196610&gcmap=0&hdrMode=0&sops=0"
cat /tmp/vi_launch.xml; echo

echo
echo "=== 5) sessionUrl0 摘要 ==="
python3 - <<'PY'
import re
s=open("/tmp/vi_launch.xml",errors="replace").read()
for tag in ("sessionUrl0","sessionUrl1","resume","status_code","status_message","gamesession"):
    m=re.search(r"<%s>(.*?)</%s>" % (tag,tag), s, re.S)
    if m:
        print("   %-14s = %s" % (tag, m.group(1)[:200]))
PY
