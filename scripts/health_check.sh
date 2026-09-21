#!/bin/sh
# ============================================================================
#  scripts/health_check.sh — 板端部署后的自检 (在**板端**跑, 不是 PC)
#
#  用途: deploy.ps1 把 agent / tests / .so 推上去之后, 用它回答一个问题:
#        "板端现在到底能不能正常起来?" —— 而不是靠人肉敲一堆命令。
#
#  跑法:
#      sh scripts/health_check.sh              # 在仓库根目录
#      sh /tmp/health_check.sh                 # deploy.ps1 就是这么调的 (先 scp 过来)
#      sh scripts/health_check.sh --list-extra # 只打印"板端多出来、该清掉的"路径
#
#  退出码: 0 = 关键项全过; 1 = 有必需项没过 (含"落后判定"不通过)
#
#  检查项 (必需项标 *):
#      * native 扩展能 import, 且 ping() 有响应
#      * config/config.yaml 存在 (并且**没被部署覆盖**——只报告指纹, 不判断内容)
#      * python3 -m agent.main --check-config 返回 0
#      * agent/ipc/ 的关键文件在位
#      * 落后判定: 板端文件与 logs/deployed-manifest 逐个 sha256 对照
#      只报告不判定: .so 指纹 / pytest 是否可用 / creds 是否就位 / 冒烟二进制 /
#                    多出来的文件 (那些用 deploy.ps1 -Prune 清)
#
#  为什么要有"落后判定"与"多余文件"
#  ---------------------------------------------------------------------------
#  deploy.ps1 用的是 `tar -xzf`(增量解包, 没有 --delete), 所以:
#    · 仓库里**删掉**的文件会继续留在板端 —— 属于"多余", 不报就没人知道
#    · 板端被手改过的文件会与仓库不一致 —— 属于"落后"
#  两者都是"板端和 PC 悄悄不一样"的形态。清单 + 逐文件 sha256 让它们可见。
#
#  ⚠ 这里是"哪些算多余"的**唯一出处**: deploy.ps1 -Prune 调 --list-extra 取清单,
#    不在 PowerShell 侧再写一份规则 (否则两处规则一定会漂移)。
# ============================================================================

# ---- 模式 (必须在打印任何东西之前处理, --list-extra 的输出要能被程序消费) ----
MODE="full"
if [ "$1" = "--list-extra" ]; then
    MODE="list-extra"
fi

# deploy.ps1 会把它 scp 到 /tmp 再调, 所以不依赖调用时的 cwd
cd /home/kickpi/myproject/assitant 2>/dev/null || {
    echo "FATAL: 找不到 /home/kickpi/myproject/assitant"
    exit 1
}

FAIL=0
ROOT=$(pwd)

#: deploy.ps1 推上来的清单 (放在 logs/ 下: 那个目录已被 .gitignore 覆盖,
#: 所以这两个文件不会让板端 git status 变脏)
MANIFEST="$ROOT/logs/deployed-manifest"
REV_FILE="$ROOT/logs/deployed-rev"

#: 部署覆盖的范围 —— "多余文件"只在这个范围内讨论, 范围外一律不碰
SCOPE="agent tests docs scripts config/config.example.yaml"

#: 板端本地 / 生成物: 既不删, 也不算"多余"
exempt() {
    case "$1" in
        tests/test_llm_integration.py) return 0 ;;                # 板端专有测试
        docs/gui-qt5-plan.md|docs/gui-qt5-tasks.md) return 0 ;;   # 板端 GUI 迭代文档
        config/config.yaml) return 0 ;;                           # 板端实盘配置
        */__pycache__/*|*.pyc|*.pyo) return 0 ;;                  # python 生成物
        *) return 1 ;;
    esac
}

#: 清单行 "<sha256>  <path>" -> 只取 path
#: ⚠ awk 默认把连续空格当作**一个**分隔符, 所以 "<hash>  <path>" 的 NF 是 2 不是 3
manifest_paths() {
    awk 'NF>=2 { sub(/^[^ ]+ +/, ""); print }' "$MANIFEST"
}

#: 板端在部署范围内、但清单里没有的文件 (一行一个, 排序)
list_extra() {
    paths=$(manifest_paths)
    for f in $(find $SCOPE -type f 2>/dev/null | sort); do
        exempt "$f" && continue
        printf '%s\n' "$paths" | grep -Fxq -- "$f" || printf '%s\n' "$f"
    done
}

if [ "$MODE" = "list-extra" ]; then
    if [ ! -f "$MANIFEST" ]; then
        echo "FATAL: 缺少 $MANIFEST (先跑一次 deploy.ps1)" >&2
        exit 1
    fi
    list_extra
    exit 0
fi

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
echo "--- 7. 部署一致性 (板端 vs 部署清单) ---"
if [ ! -f "$MANIFEST" ]; then
    note "落后判定" "没有清单 ($MANIFEST) —— 跑一次 deploy.ps1 就会生成"
else
    REV=$(cat "$REV_FILE" 2>/dev/null || echo "?")
    TOTAL=0; MISS=0; DIFF=0; DETAIL=""
    while read -r want path; do
        [ -n "$want" ] || continue
        TOTAL=$((TOTAL + 1))
        if [ ! -f "$path" ]; then
            MISS=$((MISS + 1))
            DETAIL="${DETAIL}缺失     $path
"
        elif [ "$(sha256sum "$path" | cut -d' ' -f1)" != "$want" ]; then
            DIFF=$((DIFF + 1))
            DETAIL="${DETAIL}内容不同 $path
"
        fi
    done < "$MANIFEST"
    if [ "$MISS" -eq 0 ] && [ "$DIFF" -eq 0 ]; then
        ok "落后判定" "一致: $TOTAL 个文件全部匹配 ($REV)"
    else
        bad "落后判定" "落后 $((MISS + DIFF)) 个文件 (缺失 $MISS / 内容不同 $DIFF), 清单来自 $REV"
        printf '%s' "$DETAIL" | head -8 | sed 's/^/     /'
        [ $((MISS + DIFF)) -gt 8 ] && note "  (只列了前 8 条)" "共 $((MISS + DIFF)) 条"
    fi

    EXTRA=$(list_extra | wc -l)
    if [ "$EXTRA" -eq 0 ]; then
        ok "多余文件" "无 (板端没有仓库里已删除的文件)"
    else
        note "多余文件" "$EXTRA 个已不在仓库 (增量解包不会删它们)"
        list_extra | head -8 | sed 's/^/      /'
        note "  怎么清" "scripts/deploy.ps1 -PruneDryRun 先看, -Prune 真删"
    fi
fi

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
