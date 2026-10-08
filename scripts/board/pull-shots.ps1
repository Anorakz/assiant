# pull-shots.ps1 -- PC 侧：把板端抓的裸帧拉回来，按帧切 PNG、自动剔黑屏/重复（统一截图的后半程 ✓）
#   用法：
#     powershell -ExecutionPolicy Bypass -File scripts\board\pull-shots.ps1
#     powershell -ExecutionPolicy Bypass -File scripts\board\pull-shots.ps1 -Host rk3568 -Out docs\images -Keep 1
param(
    [string]$Board = "rk3568",
    [string]$Remote = "/data/assistant/shots",
    [string]$Tmp = "$env:TEMP\board-shots",
    [string]$Out = "docs\images",
    [int]$Keep = 1,                    # 每个功能保留几张（默认 1 = 画面最丰富的那帧 ✓）
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
function Say($m) { Write-Output $m }

Say "== 统一截图 · PC 侧 =="
Say "  板子: $Board   远端: $Remote   落盘: $Out"

# ① 拉清单
$lst = & ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new $Board "cat $Remote/manifest.txt 2>/dev/null"
if (-not $lst) { Say "  !! 板端没有 manifest.txt ⇒ 先在板端跑 scripts/board/capture-all.sh ✗"; exit 2 }
$names = @()
foreach ($line in $lst) { $p = $line -split "`t"; if ($p.Count -ge 1 -and $p[0]) { $names += $p[0] } }
Say "  清单里有 $($names.Count) 组裸帧：$($names -join ', ')"

New-Item -ItemType Directory -Force -Path $Tmp | Out-Null
New-Item -ItemType Directory -Force -Path $Out | Out-Null

# ② 拉回来
foreach ($n in $names) {
    $dst = Join-Path $Tmp $n
    & scp -q -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new "$Board`:$Remote/$n" $dst
    $sz = (Get-Item $dst).Length
    Say ("  取回 {0}（{1:N1} MB）" -f $n, ($sz / 1MB))
}

if ($DryRun) { Say "  --DryRun ⇒ 只取回不转换 ✓"; exit 0 }

# ③ 交给 python 转 PNG（PIL：板端没有，所以放 PC ✓）
$py = Join-Path $PSScriptRoot "convert-shots.py"
if (-not (Test-Path $py)) { Say "  !! 缺 convert-shots.py（应与本脚本同目录 ✓）"; exit 2 }
& python $py --tmp $Tmp --out $Out --keep $Keep
Say "== 完成：PNG 在 $Out ✓ =="
