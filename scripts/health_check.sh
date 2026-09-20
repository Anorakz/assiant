#!/bin/sh
# ============================================================================
#  scripts/health_check.sh — 板端部署后的自检 (在**板端**跑, 不是 PC)
#
#  用途: deploy.ps1 把 agent / tests / .so 推上去之后, 用它回答一个问题:
#        "板端现在到底能不能正常起来?" —— 而不是靠人肉敲一堆命令。
#
#  跑法:
#      sh scripts/health_check.sh            # 在仓库根目录
#      sh /tmp/health_check.sh               # deploy.ps1 就是这么调的 (先 scp 过来)
#
#  退出码: 0 = 关键项全过; 1 = 有必需项没过
#
#  检查项 (必需项标 *):
#      * native 扩展能 import, 且 ping() 有响应
#      * config/config.yaml 存在 (并且**没被部署覆盖**——只报告指纹, 不判断内容)
#      * python3 -m agent.main --check-config 返回 0
#      * agent/ipc/ 的关键文件在位
#      只报告不判定: .so 指纹 / pytest 是否可用 / creds 是否就位 / 冒烟二进制
# ============================================================================

# deploy.ps1 会把它 scp 到 /tmp 再调, 所以不依赖调用时的 cwd
cd /home/kickpi/myproject/assitant 2>/dev/null || {
    echo "FATAL: 找不到 /home/kickpi/myproject/assitant"
    exit 1
}

FAIL=0
ROOT=$(pwd)

line() { printf '%-52s %s\n' "$1" "$2"; }
ok()   { line "$1" "OK   $2"; }
bad()  { line "$1" "FAIL $2"; FAIL=1; }
note() { line "$1" "$2"; }

echo "============================================================"
echo " 板端健康检查   root=$ROOT"
echo " 时间: $(date '+%F %T')   python: $(python3 -V 2>&1)"
echo "============================================================"

echo
echo "--- 1. native 扩展 (必需) ---"
SO=$(ls agent_native*.so 2>/dev/null | head -1)
if [ -z "$SO" ]; then
    bad "agent_native*.so 存在" "找不到任何 .so"
else
    ok "agent_native*.so 存在" "$SO  $(stat -c '%s bytes' "$SO" 2>/dev/null)"
fi
if [ ! -f libmoonlight-common-c.so ]; then
    bad "libmoonlight-common-c.so 存在" "找不到 (import 会失败)"
else
    ok "libmoonlight-common-c.so 存在" "$(stat -c '%s bytes' libmoonlight-common-c.so)"
fi
# 不带 LD_LIBRARY_PATH: 验证 $ORIGIN rpath 是否生效
PING=$(timeout 30 python3 -c "import agent_native; print(agent_native.ping())" 2>&1 | tail -1)
if [ "$PING" = "pong" ]; then
    ok "import agent_native (无 LD_LIBRARY_PATH)" "ping() -> pong"
else
    bad "import agent_native (无 LD_LIBRARY_PATH)" "$PING"
fi

echo
echo "--- 2. 配置 (必需) ---"
if [ -f config/config.yaml ]; then
    ok "config/config.yaml 存在" "$(stat -c '%s bytes  %y' config/config.yaml 2>/dev/null | cut -d. -f1)"
    note "  config.yaml sha256" "$(sha256sum config/config.yaml | cut -c1-16)..."
else
    bad "config/config.yaml 存在" "缺失! 用 cp config/config.example.yaml config/config.yaml 起步"
fi
if [ -f config/config.example.yaml ]; then
    ok "config/config.example.yaml 存在" "$(stat -c '%s bytes' config/config.example.yaml)"
else
    bad "config/config.example.yaml 存在" "模板没部署上来"
fi

echo
echo "--- 3. Agent 能否装配 (必需) ---"
timeout 60 python3 -m agent.main --check-config >/tmp/hc_checkcfg.log 2>&1
RC=$?
if [ $RC -eq 0 ]; then
    ok "--check-config 返回 0" "$(tail -1 /tmp/hc_checkcfg.log | cut -c1-60)"
else
    bad "--check-config 返回 0" "exit=$RC; 见 /tmp/hc_checkcfg.log"
    tail -3 /tmp/hc_checkcfg.log | sed 's/^/      /'
fi

echo
echo "--- 4. IPC 层文件 (必需) ---"
for f in agent/ipc/__init__.py agent/ipc/protocol.py agent/ipc/local_server.py agent/ipc/local_client.py; do
    if [ -f "$f" ]; then
        ok "$f" "$(stat -c '%s bytes' "$f")"
    else
        bad "$f" "缺失"
    fi
done

echo
echo "--- 5. 只报告 (不影响退出码) ---"
if timeout 20 python3 -c "import pytest; print(pytest.__version__)" >/dev/null 2>&1; then
    note "pytest" "已装: $(python3 -c 'import pytest;print(pytest.__version__)' 2>&1)"
else
    note "pytest" "未装 (A2 需要)"
fi
if [ -d creds ]; then
    note "creds/" "$(ls creds | tr '\n' ' ')"
else
    note "creds/" "缺失 (B2 配对凭据需要)"
fi
if [ -x tests/board/mpp_decode_smoke ]; then
    note "MPP 冒烟二进制" "在位 $(stat -c '%s bytes' tests/board/mpp_decode_smoke)"
else
    note "MPP 冒烟二进制" "不在 (交叉编译 tests/board/mpp_decode_smoke 后 scp)"
fi
if [ -d tests/data ]; then
    note "MPP 夹具" "$(ls tests/data | wc -l) 个文件"
fi
note "--check-config 日志" "/tmp/hc_checkcfg.log"

echo
echo "--- 6. 关键文件指纹 (和 PC 侧对照用) ---"
for f in agent/main.py agent/config.py agent/ipc/__init__.py agent/ipc/local_server.py \
         agent/ipc/local_client.py agent/ipc/protocol.py; do
    [ -f "$f" ] && note "$f" "$(sha256sum "$f" | cut -c1-16)"
done
SO=$(ls agent_native*.so 2>/dev/null | head -1)
[ -n "$SO" ] && note "$SO" "$(sha256sum "$SO" | cut -c1-16)"

echo
if [ $FAIL -eq 0 ]; then
    echo "============================================================"
    echo " 健康检查: 全部必需项通过"
    echo "============================================================"
    exit 0
else
    echo "============================================================"
    echo " 健康检查: 有必需项未通过 (见上面 FAIL)"
    echo "============================================================"
    exit 1
fi
