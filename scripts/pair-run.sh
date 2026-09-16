#!/usr/bin/env bash
# scripts/pair-run.sh — 跑一轮 Sunshine SRSAES 配对并留下完整证据
#
# 用法: pair-run.sh [pin] [clientcert.pem] [clientkey.pem]
# 默认: pin=5678, 证书在 /mnt/e/rk3568/local/creds/
#
# 运行前会打印提示, 需要操作员在 Windows 浏览器打开
#   https://localhost:47990/pin
# 输入同一个 PIN 并提交 (Sunshine 会阻塞 /pair?phrase=getservercert 直到提交)。
set -u

PIN="${1:-5678}"
CERT="${2:-/mnt/e/rk3568/local/creds/client.pem}"
KEY="${3:-/mnt/e/rk3568/local/creds/client.key}"
HOST="${PAIR_HOST:-172.25.32.1}"
PORT="${PAIR_PORT:-47989}"
EVIDIR="${PAIR_EVIDIR:-/mnt/e/rk3568/local/creds/pairev}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ⚠ 绝不能用 UID 作变量名: 它在 bash 里是 **readonly** (当前用户 id), 赋值会静默
#   失败, 导致每一轮都用同一个 uniqueid=1000 —— 而 Sunshine 的 nvhttp::pin() 是把
#   PIN 套用到 map_id_sess 的**第一个**会话上, 重复 uniqueid 会让操作员的 PIN 落到
#   陈旧的配对话塞, 于是双方 AES 密钥对不上, 配对必然失败 (表现为 paired=0 或
#   "Invalid client certificate")。每轮必须用全新的 uniqueid。
PAIR_UID="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
if [ -z "$PAIR_UID" ]; then
    echo "FATAL: 无法生成 uniqueid" >&2
    exit 2
fi
mkdir -p "$EVIDIR"
rm -f "$EVIDIR"/*.hex 2>/dev/null

echo "### uniqueid=$PAIR_UID  pin=$PIN  host=$HOST:$PORT"
echo "### 证据目录: $EVIDIR"
echo "### >>> 请现在打开 https://localhost:47990/pin 输入 PIN=$PIN 并提交 <<<"
echo

python3 "$SCRIPT_DIR/pair_sunshine.py" "$HOST" "$CERT" "$KEY" "$PIN" "$PORT" "$PAIR_UID" "$EVIDIR"
rc=$?
echo "### pair_sunshine.py exit=$rc"

echo
echo "### 47984 授权复验 (用同一张客户端证书):"
curl -sk -m 8 --cert "$CERT" --key "$KEY" -o /tmp/applist.txt \
    -w "    /applist -> HTTP %{http_code}\n" \
    "https://$HOST:47984/applist?uniqueid=$PAIR_UID"
head -c 300 /tmp/applist.txt; echo

exit $rc
