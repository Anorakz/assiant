$ErrorActionPreference = "Stop"

# 路径用绝对路径，不依赖当前目录
# ($PSScriptRoot 已经是 scripts/ 目录, Split-Path -Parent 会得到 null)
$root  = Split-Path -Parent $MyInvocation.MyCommand.Path | Split-Path -Parent
if (-not $root) { $root = $PSScriptRoot | Split-Path -Parent }
$build = Join-Path $root "build-host"

# ---------------------------------------------------------------- 前置检查 ----
$gtest = Join-Path $root "native/third_party/googletest/CMakeLists.txt"
if (-not (Test-Path $gtest)) {
    throw "找不到 googletest: $gtest`n请先跑 git submodule update --init --recursive"
}

if (-not (Get-Command cmake -ErrorAction SilentlyContinue) -and -not (Test-Path "E:\tool\Cmake\bin\cmake.exe")) {
    throw "找不到 cmake"
}
$cmake = (Get-Command cmake -ErrorAction SilentlyContinue).Source
if (-not $cmake) { $cmake = "E:\tool\Cmake\bin\cmake.exe" }

# --------------------------------------------------------------- 配置 CMake ---
if (-not (Test-Path $build)) {
    New-Item -ItemType Directory -Path $build | Out-Null
}

# 不带 toolchain file = 本机编译, 用 MSYS/MinGW 的 g++
& $cmake -S $root -B $build `
    -G "MinGW Makefiles" `
    -DCMAKE_BUILD_TYPE=Debug `
    -DAGENT_BUILD_TESTS=ON

if ($LASTEXITCODE -ne 0) { throw "cmake configure failed" }

# ------------------------------------------------------------------ 编译 ----
& $cmake --build $build --parallel 4

if ($LASTEXITCODE -ne 0) { throw "cmake build failed" }

# ------------------------------------------------------------------ 跑测试 ---
Write-Host ""
& $cmake --build $build --target test 2>&1 | Select-String -NotMatch "^\s*$"

if ($LASTEXITCODE -ne 0) { throw "host tests failed" }

Write-Host ""
Write-Host "host tests OK -> $build" -ForegroundColor Green
