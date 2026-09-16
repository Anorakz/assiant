$ErrorActionPreference = "Stop"

# Absolute paths only -- never depend on the current directory.
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 decodes .ps1 as ANSI
# unless a BOM is present, which turns non-ASCII text into mojibake.

$root  = Split-Path -Parent $MyInvocation.MyCommand.Path | Split-Path -Parent
if (-not $root) { $root = $PSScriptRoot | Split-Path -Parent }
$build = Join-Path $root "build-host"

# ------------------------------------------------------------ preflight ------
$gtest = Join-Path $root "native/third_party/googletest/CMakeLists.txt"
if (-not (Test-Path $gtest)) {
    throw "googletest not found: $gtest`nRun: git submodule update --init --recursive"
}

$cmake = (Get-Command cmake -ErrorAction SilentlyContinue).Source
if (-not $cmake) {
    if (Test-Path "E:\tool\Cmake\bin\cmake.exe") {
        $cmake = "E:\tool\Cmake\bin\cmake.exe"
    } else {
        throw "cmake not found"
    }
}

# ------------------------------------------------------------- configure -----
if (-not (Test-Path $build)) {
    New-Item -ItemType Directory -Path $build | Out-Null
}

# No toolchain file => native build with the host g++
& $cmake -S $root -B $build `
    -G "MinGW Makefiles" `
    -DCMAKE_BUILD_TYPE=Debug `
    -DAGENT_BUILD_TESTS=ON

if ($LASTEXITCODE -ne 0) { throw "cmake configure failed" }

# ----------------------------------------------------------------- build -----
& $cmake --build $build --parallel 4

if ($LASTEXITCODE -ne 0) { throw "cmake build failed" }

# ------------------------------------------------------------------ test -----
Write-Host ""
& $cmake --build $build --target test 2>&1 | Select-String -NotMatch "^\s*$"

if ($LASTEXITCODE -ne 0) { throw "host tests failed" }

Write-Host ""
Write-Host "host tests OK -> $build" -ForegroundColor Green
