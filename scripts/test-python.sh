#!/bin/sh
# ---------------------------------------------------------------------------
# scripts/test-python.sh -- run the Python test suite on Linux / WSL / the board
#
# Windows twin: scripts/test-python.ps1 (same file list, same behaviour).
#
# Usage:
#     scripts/test-python.sh              # everything it can run
#     scripts/test-python.sh tests/test_ipc.py   # just one file
#
# Notes:
#   * The unittest files need NO extra packages -- a bare python3 is enough.
#   * tests/test_ipc.py is the one pytest file (pytest + pytest-asyncio). It is
#     run only when those are importable; otherwise it is reported as skipped so
#     the rest of the suite still gives a clean pass/fail.
#   * If pytest-asyncio is missing and the distro blocks pip (PEP 668), install
#     it into a throwaway dir instead of touching the system python:
#         pip3 install --target /tmp/pytest-lib pytest pytest-asyncio
#         PYTHONPATH=/tmp/pytest-lib scripts/test-python.sh
# ---------------------------------------------------------------------------

root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root" || exit 1

PY=${PYTHON:-python3}
if ! command -v "$PY" >/dev/null 2>&1; then
    echo "python3 not found on PATH" >&2
    exit 1
fi

unittest_files="
tests/test_config.py
tests/test_state_machine.py
tests/test_tool_router.py
tests/test_llm.py
tests/test_llm_service.py
tests/test_vision.py
tests/test_siglip.py
tests/test_read_intents.py
tests/test_chat_memory.py
tests/test_wall_data.py
tests/test_tag_index.py
tests/test_music_library.py
tests/test_music_player.py
tests/test_netease_cli.py
tests/test_label_spec.py
tests/test_merged_tools.py
tests/test_tool_normalize.py
tests/test_scheduler.py
tests/test_main.py
tests/test_ipc_protocol.py
tests/test_ipc_local_server.py
tests/test_sunshine_client.py
tests/test_native_integration.py
tests/test_chat_bus.py
tests/test_io.py
tests/test_docs.py
tests/test_config_source_guard.py
tests/test_schedule_parity.py
tests/test_schedule_config.py
tests/test_cli.py
tests/test_tools.py
tests/test_tool_permissions.py
tests/test_wallpaper.py
"

pytest_file=tests/test_ipc.py

# Optional: run a single file when one is given on the command line
if [ $# -gt 0 ]; then
    unittest_files=""
    pytest_file=""
    for f in "$@"; do
        case "$f" in
            *test_ipc.py) pytest_file=$f ;;
            *) unittest_files="$unittest_files
$f" ;;
        esac
    done
fi

failed=""
ran=0

run_file() {
    path=$1
    if [ ! -f "$path" ]; then
        echo "test file not found: $path" >&2
        failed="$failed $path"
        return
    fi
    echo ""
    echo "=== $path ==="
    # -u so progress is visible even when the output is piped through tee/ci logs
    timeout -s KILL 300 "$PY" -u "$path"
    code=$?
    ran=$((ran + 1))
    if [ $code -ne 0 ]; then
        echo "  -> exit=$code"
        failed="$failed $path"
    fi
}

for f in $unittest_files; do
    run_file "$f"
done

echo ""
if [ -n "$pytest_file" ] && [ -f "$pytest_file" ]; then
    if "$PY" -c "import pytest, pytest_asyncio" >/dev/null 2>&1; then
        echo "=== $pytest_file (pytest) ==="
        "$PY" -m pytest "$pytest_file" -v
        code=$?                      # 必须紧接着取, 中间插一条命令就变成它的状态了
        ran=$((ran + 1))
        if [ $code -ne 0 ]; then
            echo "  -> exit=$code"
            failed="$failed $pytest_file"
        fi
    else
        echo "skip $pytest_file (pytest / pytest-asyncio not installed -- see the header)"
    fi
fi

echo ""
if [ -n "$failed" ]; then
    echo "FAILED:"
    for f in $failed; do
        echo "  - $f"
    done
    exit 1
fi

echo "python tests OK ($ran files) -> $root/tests"
