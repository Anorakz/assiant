# ============================================================================
#  scripts/run-board-tests.ps1 -- run the Python test suite ON the board.
#
#  NOTE: keep this file ASCII-only (PowerShell 5.1 + ANSI decoding).
#
#  Usage:
#      scripts/run-board-tests.ps1                 # check deps, then run
#      scripts/run-board-tests.ps1 -Install        # pip install pytest first
#      scripts/run-board-tests.ps1 -Target myhost
#
#  Design notes:
#    * The list of test files is NOT duplicated here -- it lives in
#      scripts/test-python.sh (the same file the PC/WSL runs), so the two sides
#      cannot drift apart. This script just drives it over ssh.
#    * Board-only extras (present on the board, absent in main) are run
#      separately: tests/test_llm_integration.py. It skips itself when the
#      openai SDK / llama-server are absent, so running it is harmless.
#    * Two PowerShell traps avoided on purpose:
#        1) no 2>&1 on native commands: with $ErrorActionPreference=Stop,
#           redirected native stderr becomes a TERMINATING error in PS 5.1.
#           Instead the remote side writes its own log and we scp it back.
#        2) no nested quotes in remote commands: they get mangled on the way
#           through ssh. Only simple, quote-free commands are sent inline.
# ============================================================================

param(
    [string]$Target = "rk3568",
    [switch]$Install
)

$ErrorActionPreference = "Stop"

$remoteRoot = "/home/kickpi/myproject/assitant"
$localLog = Join-Path $env:TEMP "board-tests.log"

function Step($text) {
    Write-Host ""
    Write-Host "=== $text ===" -ForegroundColor Cyan
}

function ShowLog($path) {
    if (-not (Test-Path $path)) { return }
    Get-Content $path | Where-Object {
        $_ -match '^=== ' -or $_ -match '^OK' -or $_ -match '^FAILED' -or
        $_ -match '^Ran \d+ tests' -or $_ -match '-> exit' -or
        $_ -match '^skip ' -or $_ -match '^python tests OK' -or
        $_ -match '[0-9]+ passed' -or $_ -match 'FAILED:'
    } | ForEach-Object { Write-Host $_ }
}

# ------------------------------------------------- 1. deps on the board -----
Step 1 "pytest on the board"

# Quote-free probes only.
$pytestVer = (& ssh $Target "python3 -m pytest --version").Trim()
Write-Host "  pytest         : $pytestVer"
$asyncioShow = (& ssh $Target "pip3 show pytest-asyncio").Trim()
Write-Host "  pytest-asyncio : $((($asyncioShow -split "`n" | Where-Object { $_ -match '^Version:' }) -join ''))"

$havePytest = $pytestVer -match 'pytest'
$needInstall = $Install -or (-not $havePytest)

if ($needInstall) {
    Write-Host ""
    Write-Host "installing pytest + pytest-asyncio on the board (pip)..." -ForegroundColor Yellow
    # pip 20.0.2 / Python 3.8 -> pip picks the newest versions still supporting
    # 3.8 (pytest 8.x, pytest-asyncio 0.24.x). Ubuntu 20.04 has no PEP668 guard.
    & ssh $Target "pip3 install --no-input --disable-pip-version-check pytest pytest-asyncio > /tmp/bd-pip.log 2>&1; echo EXIT=`$?"
    & scp "${Target}:/tmp/bd-pip.log" (Join-Path $env:TEMP "board-pip.log") | Out-Null
    Get-Content (Join-Path $env:TEMP "board-pip.log") | Select-Object -Last 3 | ForEach-Object { Write-Host "  $_" }
    $pytestVer = (& ssh $Target "python3 -m pytest --version").Trim()
    Write-Host "  pytest now     : $pytestVer"
    $havePytest = $pytestVer -match 'pytest'
    if (-not $havePytest) {
        Write-Host "pytest still unavailable -- the pytest-based file will be skipped by test-python.sh" -ForegroundColor Yellow
    }
}

# ------------------------------------------------- 2. main suite -------------
Step 2 "main suite (scripts/test-python.sh on the board)"

# Remote-side logging: this keeps native stderr from ever reaching PowerShell's
# error stream (see the header note).
$rc = (& ssh $Target "cd $remoteRoot && sh scripts/test-python.sh > /tmp/bd-main.log 2>&1; echo EXIT=`$?").Trim()
Write-Host "  remote says: $rc"
& scp "${Target}:/tmp/bd-main.log" $localLog | Out-Null
ShowLog $localLog

$mainExit = 1
if ($rc -match 'EXIT=(\d+)') { $mainExit = [int]$Matches[1] }
Write-Host ""
Write-Host "main suite exit code : $mainExit" -ForegroundColor $(if ($mainExit -eq 0) { "Green" } else { "Red" })
Write-Host "full log            : $localLog"

# ------------------------------------------------- 3. board-only extras ------
Step 3 "board-only extras (not in the shared list)"

$extraLog = Join-Path $env:TEMP "board-tests-extra.log"
$rc2 = (& ssh $Target "cd $remoteRoot && if [ -f tests/test_llm_integration.py ]; then python3 -m unittest tests.test_llm_integration > /tmp/bd-extra.log 2>&1; echo EXIT=`$?; else echo EXIT=0 > /tmp/bd-extra.log; echo SKIP; fi").Trim()
if ($rc2 -match 'SKIP') {
    Write-Host "  tests/test_llm_integration.py : not present, skipped"
    $extraExit = 0
} else {
    & scp "${Target}:/tmp/bd-extra.log" $extraLog | Out-Null
    Get-Content $extraLog | Select-Object -Last 6 | ForEach-Object { Write-Host "  $_" }
    $extraExit = 1
    if ($rc2 -match 'EXIT=(\d+)') { $extraExit = [int]$Matches[1] }
}
Write-Host "extra suite exit code: $extraExit"

# ------------------------------------------------- 4. summary ----------------
Step 4 "summary"

if ($mainExit -eq 0 -and $extraExit -eq 0) {
    Write-Host "board tests: ALL OK" -ForegroundColor Green
    exit 0
} else {
    Write-Host "board tests: FAILURES (main=$mainExit extra=$extraExit)" -ForegroundColor Red
    Write-Host "see $localLog" -ForegroundColor Yellow
    exit 1
}
