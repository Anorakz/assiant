$ErrorActionPreference = "Stop"

# Absolute paths only -- never depend on the current directory.
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 decodes .ps1 as ANSI
# unless a BOM is present, which turns non-ASCII text into mojibake.

# Python-side host tests (agent/config.py, agent/io/*).
# C++ tests live in scripts/test-host.ps1 (ctest). This script is the Python half;
# it uses plain unittest so it needs no extra packages (pytest is not required).
#
# Usage:
#     scripts/test-python.ps1

$root = Split-Path -Parent $MyInvocation.MyCommand.Path | Split-Path -Parent
if (-not $root) { $root = $PSScriptRoot | Split-Path -Parent }

$py = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $py) { throw "python not found on PATH" }
$python = $py.Source

$tests = @(
    "tests\test_config.py",
    "tests\test_state_machine.py",
    "tests\test_tool_router.py",
    "tests\test_llm.py",
    "tests\test_llm_service.py",
    "tests\test_vision.py",
    "tests\test_siglip.py",
    "tests\test_read_intents.py",
    "tests\test_chat_memory.py",
    "tests\test_user_profile.py",
    "tests\test_wall_data.py",
    "tests\test_tag_index.py",
    "tests\test_music_library.py",
    "tests\test_music_player.py",
    "tests\test_netease_cli.py",
    "tests\test_label_spec.py",
    "tests\test_merged_tools.py",
    "tests\test_tool_normalize.py",
    "tests\test_scheduler.py",
    "tests\test_main.py",
    "tests\test_ipc_protocol.py",
    "tests\test_ipc_local_server.py",
    "tests\test_sunshine_client.py",
    "tests\test_native_integration.py",
    "tests\test_chat_bus.py",
    "tests\test_io.py",
    "tests\test_docs.py",
    "tests\test_config_source_guard.py",
    "tests\test_schedule_parity.py",
    "tests\test_schedule_config.py",
    "tests\test_cli.py",
    "tests\test_tools.py",
    "tests\test_tool_permissions.py",
    "tests\test_wallpaper.py"
)

$failed = @()
foreach ($t in $tests) {
    $path = Join-Path $root $t
    if (-not (Test-Path $path)) { throw "test file not found: $path" }
    Write-Host ""
    Write-Host "=== $t ===" -ForegroundColor Cyan
    & $python $path
    if ($LASTEXITCODE -ne 0) { $failed += $t }
}

# tests/test_ipc.py is the one pytest-based file (pytest-asyncio fixtures).
# It is run separately and ONLY when pytest is importable, so the tests above stay
# runnable with a bare Python install (no extra packages).
$pytestFile = "tests\test_ipc.py"
$pytestPath = Join-Path $root $pytestFile
Write-Host ""
if (-not (Test-Path $pytestPath)) {
    Write-Host "skip $pytestFile (file not found)" -ForegroundColor Yellow
} else {
    # NOTE: never redirect a native command's stderr here. With
    # $ErrorActionPreference = "Stop", PS 5.1 turns a redirected native stderr
    # into a TERMINATING error -- and it does so even when stderr is empty,
    # because it re-raises stderr left over from an EARLIER native command
    # (tests\test_main.py logs deliberate tracebacks). That produced a bogus
    # NativeCommandError that aborted the whole suite with exit 1.
    # So: ask Python to report on STDOUT and judge by the text. find_spec() is
    # used instead of import so a broken/missing module cannot raise either.
    $depProbe = "import importlib.util as u; print(1 if (u.find_spec('pytest') and u.find_spec('pytest_asyncio')) else 0)"
    $havePytest = (& $python -c $depProbe).Trim()
    if ($havePytest -eq "1") {
        Write-Host "=== $pytestFile (pytest) ===" -ForegroundColor Cyan
        & $python -m pytest $pytestPath
        if ($LASTEXITCODE -ne 0) { $failed += $pytestFile }
    } else {
        Write-Host "skip $pytestFile (pytest / pytest-asyncio not installed)" -ForegroundColor Yellow
    }
}

Write-Host ""
if ($failed.Count -gt 0) {
    Write-Host "FAILED:" -ForegroundColor Red
    $failed | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
    exit 1
}

Write-Host "python tests OK -> $root\tests" -ForegroundColor Green
