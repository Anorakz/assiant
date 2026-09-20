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
    "tests\test_vision.py",
    "tests\test_scheduler.py",
    "tests\test_main.py",
    "tests\test_ipc_protocol.py",
    "tests\test_ipc_local_server.py",
    "tests\test_chat_bus.py",
    "tests\test_io.py"
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
    & $python -c "import pytest, pytest_asyncio" 2>$null
    if ($LASTEXITCODE -eq 0) {
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
