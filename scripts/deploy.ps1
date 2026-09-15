$ErrorActionPreference = "Stop"

$root   = Split-Path -Parent $PSScriptRoot
$build  = Join-Path $root "build-rk3568"
$target = "rk3568"                                       # 依赖 SSH config
$remote = "/home/kickpi/myproject/assitant/agent"        # 部署到 agent 子目录

Write-Host "=== 1. build ===" -ForegroundColor Cyan
& "$PSScriptRoot\build.ps1"

$soItem = Get-ChildItem "$build\native" -Filter "agent_native*.so" | Select-Object -First 1
if (-not $soItem) { throw "no .so found" }
$so = $soItem.FullName
Write-Host "found: $so" -ForegroundColor Cyan

Write-Host "=== 2. deploy ===" -ForegroundColor Cyan
ssh $target "mkdir -p $remote"
scp $so "${target}:${remote}/"

if (Test-Path "$root\agent") {
    scp -r "$root\agent" "${target}:${remote}/"
}

Write-Host "=== 3. verify ===" -ForegroundColor Cyan
ssh $target "cd $remote && python3 -c 'import agent_native; print(agent_native.ping())'"

Write-Host "=== deploy ok ===" -ForegroundColor Green