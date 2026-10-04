#!/bin/sh
# T15-14 板端 OTA 执行器（第一版，独立脚本；验完再落进仓库的 assistant ota）
#   用法: sh ota-run.sh <包 URL> <期望 sha256>
#   步骤: 当前槽守卫 -> 下载 -> sha256 -> rkupdate（管道 fd 收进度）
#   ⚠ 包固定写 A 槽（boot_a/system_a + misc 把 A 标为活动）
set -u
URL="$1"
WANT="$2"
PKG=/data/assistant/ota/ota-assistant-ab.img

echo "==== 1. 当前槽守卫 ===="
SLOT=$(sed -n 's/.*android_slotsufix=\(_[ab]\).*/\1/p' /proc/cmdline)
echo "当前槽: ${SLOT:-（读不到）}"
case "$SLOT" in
  _b) echo "   ✓ 在 B 槽，包写 A 槽 —— 放行" ;;
  _a) echo "   ✗ 已经在 A 槽：这个包会写当前槽，拒绝执行"; exit 3 ;;
  *)  echo "   ✗ 读不到槽后缀，拒绝执行"; exit 3 ;;
esac

echo
echo "==== 2. 取包 ===="
mkdir -p /data/assistant/ota
if [ -f "$PKG" ]; then
  HAVE=$(sha256sum "$PKG" | cut -d' ' -f1)
  if [ "$HAVE" = "$WANT" ]; then
    echo "   本地已有且哈希一致，跳过下载（$PKG）"
  else
    echo "   本地那份哈希不符，重新下载"
    rm -f "$PKG"
  fi
fi
if [ ! -f "$PKG" ]; then
  if command -v wget >/dev/null 2>&1; then
    wget -O "$PKG" "$URL" || { echo "   ✗ wget 失败"; exit 4; }
  else
    curl -fL -o "$PKG" "$URL" || { echo "   ✗ curl 失败"; exit 4; }
  fi
fi
ls -l "$PKG" | sed 's/^/   /'

echo
echo "==== 3. sha256 校验 ===="
GOT=$(sha256sum "$PKG" | cut -d' ' -f1)
echo "   期望: $WANT"
echo "   实际: $GOT"
if [ "$GOT" != "$WANT" ]; then echo "   ✗ 不一致 —— 一个字节都不写，退出"; rm -f "$PKG"; exit 5; fi
echo "   ✓ 一致"

echo
echo "==== 4. 调 rkupdate（argv: 占位 - 进度fd 包路径 0）===="
python3 - "$PKG" <<'PY'
import os
import subprocess
import sys
import threading

pkg = sys.argv[1]
read_fd, write_fd = os.pipe()
print("   启动: rkupdate - %d %s 0" % (write_fd, pkg), flush=True)
proc = subprocess.Popen(["rkupdate", "-", str(write_fd), pkg, "0"], pass_fds=(write_fd,))
os.close(write_fd)

def pump():
    with os.fdopen(read_fd, "rb") as handle:
        for raw in handle:
            sys.stdout.write("   [rkupdate] " + raw.decode("utf-8", "replace"))
            sys.stdout.flush()

thread = threading.Thread(target=pump)
thread.start()
code = proc.wait()
thread.join()
print("   rkupdate 返回码 = %d（0 = INSTALL_SUCCESS）" % code, flush=True)
sys.exit(0 if code == 0 else 6)
PY
RC=$?
echo
echo "==== 5. 结果 ===="
echo "   rkupdate rc=$RC （$( [ $RC -eq 0 ] && echo 写盘成功，包里的 misc 已把 A 槽标为活动 || echo 失败 )）"
echo "   如需重启切槽: reboot"
exit $RC
