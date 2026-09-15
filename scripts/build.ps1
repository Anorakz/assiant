$ErrorActionPreference = "Stop"

# 路径用绝对路径，不依赖当前目录
# ($PSScriptRoot 已经是 scripts/ 目录, Split-Path -Parent 会得到 null)
$root  = Split-Path -Parent $MyInvocation.MyCommand.Path | Split-Path -Parent
if (-not $root) { $root = $PSScriptRoot | Split-Path -Parent }
$build = Join-Path $root "build-rk3568"
$toolchain = Join-Path $root "cmake/toolchain.cmake"
$sysroot = "E:/rk3568/sysroot"
$armdir  = "E:/rk3568/arm"

# ---------------------------------------------------------------- 前置检查 ----
$gpp = Join-Path $armdir "bin/aarch64-none-linux-gnu-g++.exe"
if (-not (Test-Path $gpp)) {
    throw "找不到交叉编译器: $gpp`n请先安装 Arm GNU Toolchain 9.2-2019.12 到 $armdir"
}

$pyhdr = Join-Path $sysroot "usr/include/python3.8/Python.h"
if (-not (Test-Path $pyhdr)) {
    throw "sysroot 缺少 Python 3.8 头文件: $pyhdr`n请把 libpython3.8-dev 装进 sysroot"
}

# --------------------------------------------------------------- 配置 CMake ---
if (-not (Test-Path $build)) {
    New-Item -ItemType Directory -Path $build | Out-Null
}

# 用 -S / -B 显式指定，不使用相对路径 ..
cmake -S $root -B $build `
    -G "MinGW Makefiles" `
    -DCMAKE_TOOLCHAIN_FILE="$toolchain" `
    -DCMAKE_BUILD_TYPE=Release

if ($LASTEXITCODE -ne 0) { throw "cmake configure failed" }

# ------------------------------------------------------------------ 编译 ----
cmake --build $build --parallel 4

if ($LASTEXITCODE -ne 0) { throw "cmake build failed" }

# ------------------------------------------------------------------ 校验 ----
$so = Join-Path $build "native/agent_native.cpython-38-aarch64-linux-gnu.so"
if (-not (Test-Path $so)) {
    # 兼容不同生成器把产物放在别处
    $so = (Get-ChildItem $build -Recurse -Filter "agent_native*.so" | Select-Object -First 1).FullName
}
if (-not $so) { throw "构建结束但没有找到 agent_native*.so" }

Write-Host ""
Write-Host "OK -> $so" -ForegroundColor Green

$readelf = Join-Path $armdir "bin/aarch64-none-linux-gnu-readelf.exe"
if (Test-Path $readelf) {
    Write-Host "--- ELF header ---"
    & $readelf -h $so | Select-String "Class|Machine|Type"
    Write-Host "--- 最高 GLIBC 版本需求 ---"
    $vers = & $readelf --version-info $so 2>$null |
            Select-String -Pattern "GLIBC_(\d+)\.(\d+)" -AllMatches |
            ForEach-Object { $_.Matches } | ForEach-Object { $_.Value } |
            Sort-Object -Unique
    if ($vers) { $vers -join ", " } else { "(无 GLIBC 版本符号)" }
}
