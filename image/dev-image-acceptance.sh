#!/bin/sh
# ============================================================================
#  image/dev-image-acceptance.sh — **开发镜像**的板端验收脚本（T15-2-12 / 2-12-5）
#
#  怎么用（在 PC 上）：
#      scp image/dev-image-acceptance.sh rk3568:/tmp/
#      ssh rk3568 "sh /tmp/dev-image-acceptance.sh"
#  退出码 0 = 全部通过。
#
#  它检查的是"开发镜像的出口判据"（用户 2026-10-02 批准的清单）：
#    · ssh 能进（这条由"你能把脚本 scp 上去"本身证明，脚本里再打印登录身份）
#    · **能在板上跑 pytest**（而且真的跑一遍，不是只看 --version）
#    · gcc 能编译；gdb/strace 能用；git/make/pkgconf/ctest 在位
#    · pgrep/pkill/top（发行镜像没有，我们脚本被它坑过）
#    · vim / tcpdump / iperf3（后者 T15-9 要用）
#    · python3 有 zlib（发行镜像没有）
#
#  **已知差异（用户 2026-10-02 选"方案 A"接受，不算失败）**
#  ---------------------------------------------------------------------------
#  1) 板端**没有 g++/C++**：buildroot 的"板上 gcc"包只把 C 前端（cc1）装进 target，
#     target 里既无 cc1plus 也无 g++ 驱动（实测 `gcc -x c++` 直接失败）。
#  2) 板端**没有 cmake 驱动**：厂商那份 buildroot 的 cmake 包只把 ctest 装进
#     target（连同 share/cmake-3.28 模块），没有 `cmake` 可执行文件。
#  这两条要改就得动 buildroot 的包行为并重编板端 GCC（30–60 分钟）+ 长期维护，
#  而我们**所有交付物本来就是 PC 侧交叉编译**的，板上编译只是排障便利 →
#  按方案 A 记为"已知差异"，脚本里打印出来但不判失败。
#
#  ⚠ 只用 busybox/核心工具能提供的东西：脚本要在**最小镜像**上也能跑，
#    所以不依赖 bash 数组、不依赖 GNU 扩展。
# ============================================================================
set -u

PASS=0
FAIL=0
DIFF=0
TMP=/tmp/dev-accept.$$

ok()   { PASS=$((PASS + 1)); printf '  [ OK ] %s\n' "$1"; }
bad()  { FAIL=$((FAIL + 1)); printf '  [FAIL] %s\n' "$1"; }
diff() { DIFF=$((DIFF + 1)); printf '  [已知差异] %s\n' "$1"; }

have() {  # have <命令> <说明>
    if command -v "$1" >/dev/null 2>&1; then ok "$2：$(command -v "$1")"; else bad "$2 缺失（$1）"; fi
}

# 有就算加分，没有就记已知差异（不判失败）
have_or_diff() {  # have_or_diff <命令> <说明> <差异理由>
    if command -v "$1" >/dev/null 2>&1; then
        ok "$2：$(command -v "$1")"
    else
        diff "$2 不在板上 —— $3"
    fi
}

echo "=== 0) 身份与环境 ==="
echo "  主机名  : $(hostname)"
echo "  内核    : $(uname -r)"
echo "  谁在跑  : $(id)"
echo "  启动目标: $(systemctl is-active assistant.target 2>/dev/null || echo '（没有 systemd？）')"
echo "  槽位    : $(grep -o 'android_slotsufix=[a-z_]*' /proc/cmdline 2>/dev/null || echo '（未知）')"

echo
echo "=== 1) 编译链 ==="
have gcc     "C 编译器"
have make    "make"
have pkgconf "pkgconf"
have ctest   "ctest（板端 CMake 测试驱动）"
have ld      "链接器"
have_or_diff g++ "C++ 编译器" "板上 gcc 包只装 C 前端；C++ 在 PC 侧交叉编译（方案 A）"
have_or_diff cmake "cmake 驱动" "厂商 buildroot 只把 ctest 装进 target（方案 A）"

echo
echo "=== 2) 调试与排障 ==="
have gdb       "gdb"
have gdbserver "gdbserver"
have strace    "strace"
have top       "top（procps）"
have pgrep     "pgrep（发行镜像没有这个，实测被坑过）"
have pkill     "pkill"
have vim       "vim"

echo
echo "=== 3) 网络工具 / 版本控制 ==="
have tcpdump "tcpdump"
have iperf3  "iperf3"
have git     "git"

echo
echo "=== 4) python 与 pytest ==="
have python3 "python3"
if python3 -c 'import zlib; print(zlib.crc32(b"x"))' >/dev/null 2>&1; then
    ok "python3 有 zlib 模块（发行镜像缺它，脚本会 ModuleNotFoundError）"
else
    bad "python3 没有 zlib 模块"
fi
if python3 -m pytest --version >/dev/null 2>&1; then
    ok "pytest 可用：$(python3 -m pytest --version 2>&1 | head -1)"
else
    bad "pytest 不可用（python3 -m pytest --version 失败）"
fi

echo
echo "=== 5) 真的编一个程序 ==="
mkdir -p "$TMP" || true
cat > "$TMP/hello.c" <<'EOF'
#include <stdio.h>
int main(void) { printf("hello from board gcc\n"); return 0; }
EOF
if gcc -O2 -o "$TMP/hello" "$TMP/hello.c" 2>"$TMP/gcc.err"; then
    OUT="$("$TMP/hello")"
    if [ "$OUT" = "hello from board gcc" ]; then
        ok "gcc 编译并运行成功：$OUT"
    else
        bad "gcc 编出来的程序输出不对：$OUT"
    fi
else
    bad "gcc 编译失败：$(head -2 "$TMP/gcc.err" | tr '\n' ' ')"
fi

echo
echo "=== 6) gdb / strace 真的能用 ==="
if command -v gdb >/dev/null 2>&1 && [ -x "$TMP/hello" ]; then
    if gdb -batch -ex 'break main' -ex run -ex 'print 1+1' "$TMP/hello" >"$TMP/gdb.out" 2>&1; then
        if grep -q '\$1 = 2' "$TMP/gdb.out"; then
            ok "gdb 能下断点、能跑、能求值（print 1+1 = 2）"
        else
            bad "gdb 跑了但结果不对（见 $TMP/gdb.out）"
        fi
    else
        bad "gdb 执行失败（见 $TMP/gdb.out）"
    fi
else
    bad "gdb 或被测程序不可用"
fi
if command -v strace >/dev/null 2>&1 && [ -x "$TMP/hello" ]; then
    if strace -f -e trace=write "$TMP/hello" >"$TMP/strace.out" 2>&1 && grep -q 'write(' "$TMP/strace.out"; then
        ok "strace 能跟踪系统调用（抓到 write）"
    else
        bad "strace 没抓到 write（见 $TMP/strace.out）"
    fi
else
    bad "strace 或被测程序不可用"
fi

echo
echo "=== 7) 真的在板上跑一遍 pytest ==="
cat > "$TMP/test_smoke.py" <<'EOF'
import sys


def test_python_runs():
    assert sys.version_info >= (3, 8)


def test_math():
    assert sum(range(11)) == 55


def test_tmp_is_writable():
    import tempfile, os
    p = os.path.join(tempfile.gettempdir(), "pytest-write-probe")
    with open(p, "w") as f:
        f.write("ok")
    assert os.path.getsize(p) == 2
EOF
if python3 -m pytest -q "$TMP/test_smoke.py" >"$TMP/pytest.out" 2>&1; then
    ok "pytest 在板上跑通：$(tail -1 "$TMP/pytest.out")"
else
    bad "pytest 在板上失败：$(tail -3 "$TMP/pytest.out" | tr '\n' ' ')"
fi

echo
echo "=== 8) make 真的能构建（cmake 见已知差异） ==="
cat > "$TMP/Makefile" <<'EOF'
all: greet
greet: greet.c
	$(CC) -o greet greet.c
EOF
cat > "$TMP/greet.c" <<'EOF'
#include <stdio.h>
int main(void) { puts("make ok"); return 0; }
EOF
if (cd "$TMP" && make CC=gcc >"$TMP/make.out" 2>&1 && [ -x "$TMP/greet" ] && [ "$("$TMP/greet")" = "make ok" ]); then
    ok "板上 make 能编译"
else
    bad "板上 make 构建失败（见 $TMP/make.out）"
fi
if command -v cmake >/dev/null 2>&1; then
    mkdir -p "$TMP/cm" || true
    cat > "$TMP/cm/CMakeLists.txt" <<'EOF'
cmake_minimum_required(VERSION 3.10)
project(probe C)
add_executable(probe probe.c)
EOF
    cat > "$TMP/cm/probe.c" <<'EOF'
#include <stdio.h>
int main(void) { puts("cmake ok"); return 0; }
EOF
    if (cd "$TMP/cm" && cmake . >"$TMP/cmake.out" 2>&1 && make >"$TMP/cmake-make.out" 2>&1 \
            && [ -x "$TMP/cm/probe" ] && [ "$("$TMP/cm/probe")" = "cmake ok" ]); then
        ok "板上 cmake 能配置并构建"
    else
        bad "板上 cmake 构建失败（见 $TMP/cmake.out / $TMP/cmake-make.out）"
    fi
else
    diff "板上 cmake 配置/构建 —— 厂商 buildroot 只装 ctest（方案 A 接受）"
fi

echo
echo "=== 9) 系统数字（顺带记录） ==="
echo "  内存: $(free -m 2>/dev/null | awk '/Mem:/{printf \"used %s MB / total %s MB\", $3, $2}')"
echo "  温度: $(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null) (zone0, 千分之一度)"
echo "  根分区: $(df -h / | awk 'NR==2{print $3 \" used / \" $2 \" (\" $5 \")\"}')"
echo "  发行镜像里没有的东西（应在此镜像里有）: gcc=$(command -v gcc >/dev/null 2>&1 && echo 有 || echo 无) pgrep=$(command -v pgrep >/dev/null 2>&1 && echo 有 || echo 无) gdb=$(command -v gdb >/dev/null 2>&1 && echo 有 || echo 无)"

echo
echo "=================================================="
echo "结果：通过 $PASS 项，失败 $FAIL 项，已知差异 $DIFF 项"
if [ "$FAIL" -eq 0 ]; then
    echo "开发镜像验收：**通过**（已知差异见上，用户 2026-10-02 选方案 A 接受）"
    rm -rf "$TMP"
    exit 0
else
    echo "开发镜像验收：**有失败项**（临时目录保留在 $TMP 供排查）"
    exit 1
fi
