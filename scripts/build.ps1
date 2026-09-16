$ErrorActionPreference = "Stop"

# Absolute paths only -- never depend on the current directory.
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 decodes .ps1 as ANSI
# unless a BOM is present, which turns non-ASCII text into mojibake.
# Also: param() must be the first statement if you ever add one, so put
# $ErrorActionPreference after it, not before.

$root  = Split-Path -Parent $MyInvocation.MyCommand.Path | Split-Path -Parent
if (-not $root) { $root = $PSScriptRoot | Split-Path -Parent }
$build = Join-Path $root "build-rk3568"
$toolchain = Join-Path $root "cmake/toolchain.cmake"
$sysroot = "E:/rk3568/sysroot"
$armdir  = "E:/rk3568/arm"

# ------------------------------------------------------------ preflight ------
$gpp = Join-Path $armdir "bin/aarch64-none-linux-gnu-g++.exe"
if (-not (Test-Path $gpp)) {
    throw "cross compiler not found: $gpp`nInstall Arm GNU Toolchain 9.2-2019.12 into $armdir first."
}

$pyhdr = Join-Path $sysroot "usr/include/python3.8/Python.h"
if (-not (Test-Path $pyhdr)) {
    throw "sysroot is missing Python 3.8 headers: $pyhdr`nRun scripts/setup-sysroot-deps.ps1"
}

# ------------------------------------------------------------- configure -----
if (-not (Test-Path $build)) {
    New-Item -ItemType Directory -Path $build | Out-Null
}

# Explicit -S / -B so we never rely on a relative ".."
cmake -S $root -B $build `
    -G "MinGW Makefiles" `
    -DCMAKE_TOOLCHAIN_FILE="$toolchain" `
    -DCMAKE_BUILD_TYPE=Release

if ($LASTEXITCODE -ne 0) { throw "cmake configure failed" }

# ----------------------------------------------------------------- build -----
cmake --build $build --parallel 4

if ($LASTEXITCODE -ne 0) { throw "cmake build failed" }

# ---------------------------------------------------------------- verify -----
$so = Join-Path $build "native/agent_native.cpython-38-aarch64-linux-gnu.so"
if (-not (Test-Path $so)) {
    # generators may place artifacts elsewhere
    $found = Get-ChildItem $build -Recurse -Filter "agent_native*.so" | Select-Object -First 1
    if ($found) { $so = $found.FullName }
}
if (-not $so) { throw "build finished but no agent_native*.so was produced" }

Write-Host ""
Write-Host "OK -> $so" -ForegroundColor Green

$readelf = Join-Path $armdir "bin/aarch64-none-linux-gnu-readelf.exe"
if (Test-Path $readelf) {
    Write-Host "--- ELF header ---"
    & $readelf -h $so | Select-String "Class|Machine|Type"
    Write-Host "--- highest GLIBC version required ---"
    $vers = & $readelf --version-info $so 2>$null |
            Select-String -Pattern "GLIBC_(\d+)\.(\d+)" -AllMatches |
            ForEach-Object { $_.Matches } | ForEach-Object { $_.Value } |
            Sort-Object -Unique
    if ($vers) { $vers -join ", " } else { "(no GLIBC version symbols)" }
}
