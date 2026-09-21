# ============================================================================
#  scripts/sync-gui.ps1 -- sync gui/ to the board and rebuild it THERE.
#
#  NOTE: keep this file ASCII-only (PowerShell 5.1 + ANSI decoding).
#
#  Usage:
#      scripts/sync-gui.ps1                 # guard, ship committed gui/, rebuild
#      scripts/sync-gui.ps1 -NoBuild        # ship only
#      scripts/sync-gui.ps1 -Test           # rebuild, then ctest on the board
#      scripts/sync-gui.ps1 -Force          # overwrite board-side gui/ edits
#      scripts/sync-gui.ps1 -Target myhost
#
#  Why this is a separate script from deploy.ps1:
#    deploy.ps1 deliberately does NOT ship gui/. The GUI is built ON the board
#    (Qt 5.12 aarch64 is installed there and the board owns gui/build), so the
#    two halves are split: deploy.ps1 ships agent/tests/docs + the .so files,
#    this script ships the GUI source and rebuilds it natively.
#
#  What this script never touches:
#    gui/build/           -- the tarball contains no build dir and the extract is
#                            additive, so the configured build tree survives
#                            (re-running cmake -S gui -B gui/build with no -D
#                            args reuses CMakeCache.txt).
#    config/config.yaml   -- the single source of config truth, board-owned and
#                            gitignored. It lives OUTSIDE gui/, so shipping gui/
#                            cannot reach it; step 5 still checks it survived.
#
#  Source of truth: this repo's HEAD (via `git archive`), not the working tree.
#  That matches deploy.ps1: only committed content is ever shipped, so what runs
#  on the board is always traceable to a commit.
#
#  Rebuild note: `git archive` restores each file's mtime from the commit and
#  make is mtime-driven. So the normal flow (commit, THEN sync) always rebuilds
#  what it should, because the new commit is the newest mtime in the tree.
#  Consequence: commit before syncing -- uncommitted edits are not shipped, and
#  a sync of content that is already on the board may still relink a few autogen
#  targets purely because the mtimes moved.
#
#  Conflict guard -- three-way, and no state file on the board:
#    base   = the board's own git HEAD for gui/
#    theirs = the board's working tree
#    ours   = this repo's HEAD for gui/
#  A path that differs from BOTH base and ours is a real board-side edit and
#  aborts the run unless -Force. A path that differs from base merely because an
#  earlier sync put OUR content there is not a conflict -- which is what keeps
#  the guard from nagging on every run after the first real change.
#
#  Two PowerShell 5.1 traps avoided on purpose (same as run-board-tests.ps1):
#    1) no 2>&1 on native commands: with $ErrorActionPreference=Stop a
#       redirected native stderr becomes a TERMINATING error. Remote commands
#       write their own logs and we scp them back instead.
#    2) no nested quotes: remote commands are built as single-quoted PowerShell
#       strings so "$p" / "$?" reach the remote shell untouched.
# ============================================================================

param(
    [switch]$NoBuild,
    [switch]$Test,
    [switch]$Force,
    [int]$Jobs = 4,
    [string]$Target = "rk3568"
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$remoteRoot = "/home/kickpi/myproject/assitant"
$remoteTmp = "/tmp"

function Step($n, $text) {
    Write-Host ""
    Write-Host "=== $n. $text ===" -ForegroundColor Cyan
}

function Fail($text) {
    Write-Host "FAILED: $text" -ForegroundColor Red
    exit 1
}

# ------------------------------------------------- 1. target and revision ----
Step 1 "target and revision"

& ssh $Target "echo connected: `$(hostname) `$(uname -m)"
if ($LASTEXITCODE -ne 0) { Fail "ssh $Target failed (check ~/.ssh/config alias)" }

$branch = (& git -C $root rev-parse --abbrev-ref HEAD).Trim()
$head = (& git -C $root rev-parse --short HEAD).Trim()
$guiTree = (& git -C $root rev-parse "HEAD:gui").Trim()
Write-Host "  local             : $branch @ $head   (gui tree $($guiTree.Substring(0,8)))"

$mbr = (& ssh $Target ('cd ' + $remoteRoot + ' && git rev-parse --abbrev-ref HEAD')).Trim()
Write-Host "  board branch      : $mbr  (only used as the merge base)"

# Our blobs, keyed by path relative to gui/.
$ours = @{}
foreach ($line in (& git -C $root ls-tree -r HEAD:gui)) {
    $parts = $line -split "`t"
    if ($parts.Count -lt 2) { continue }
    $ours[$parts[1]] = ($parts[0] -split ' ')[2]
}
Write-Host "  gui/ blobs        : $($ours.Count)"

# A dirty working tree is not fatal (we ship HEAD), but it is worth knowing that
# what runs on the board will not include these edits.
$pcDirty = @(& git -C $root status --porcelain -- gui | Where-Object { $_ -and $_.Trim() -ne "" })
if ($pcDirty.Count -gt 0) {
    Write-Host "  note              : $($pcDirty.Count) uncommitted file(s) under gui/ -- NOT shipped (we ship HEAD)" -ForegroundColor Yellow
    $pcDirty | ForEach-Object { Write-Host "                      $_" -ForegroundColor Yellow }
}

# ------------------------------------------- 2. board-side edits (guard) -----
Step 2 "board-side gui/ edits (conflict guard)"

# One round trip: porcelain status -> path per line -> blob hash per path.
# `cut -c4-` strips the "XY " status prefix; -z is deliberately not used so the
# output stays line-oriented and readable.
$bdCmd = 'cd ' + $remoteRoot + ' && git status --porcelain -- gui/ | cut -c4- | while read p; do if [ -f "$p" ]; then echo "$(git hash-object -- "$p")  $p"; fi; done'
$bdHashes = @(& ssh $Target $bdCmd | Where-Object { $_ -and $_.Trim() -ne "" })

$conflicts = @()
foreach ($l in $bdHashes) {
    $m = [regex]::Match($l, '^([0-9a-f]{40})\s+(.+)$')
    if (-not $m.Success) { continue }
    $sha = $m.Groups[1].Value
    $p = $m.Groups[2].Value.Trim()
    if (-not $p.StartsWith("gui/")) { continue }
    $rel = $p.Substring(4)
    if (-not $ours.ContainsKey($rel)) {
        Write-Host "  board-only, left alone : $rel" -ForegroundColor Yellow
        continue
    }
    if ($sha -ne $ours[$rel]) { $conflicts += $rel }
}

if ($conflicts.Count -gt 0 -and -not $Force) {
    Write-Host ""
    Write-Host "these board-side gui/ edits differ from our HEAD and would be overwritten:" -ForegroundColor Red
    $conflicts | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
    Fail "refusing to overwrite board-side work (re-run with -Force, or copy it back first)"
}
if ($conflicts.Count -gt 0) {
    Write-Host "  -Force            : overwriting $($conflicts.Count) board-side edit(s)" -ForegroundColor Yellow
    $conflicts | ForEach-Object { Write-Host "                      $_" -ForegroundColor Yellow }
} else {
    Write-Host "  no board-side edits that we would destroy"
}

# ------------------------------------------------------ 3. package -----------
Step 3 "package committed gui/ (git archive)"

$tarball = Join-Path $env:TEMP "gui-sync-$head.tar.gz"
if (Test-Path $tarball) { Remove-Item $tarball -Force }

# autocrlf=false + eol=lf is mandatory: plain `git archive` on this Windows box
# (core.autocrlf=true) bakes CRLF into the tarball, which would rewrite every
# C++ source on the board with CRLF.
& git -C $root -c core.autocrlf=false -c core.eol=lf archive --format=tar.gz -o $tarball HEAD gui
if ($LASTEXITCODE -ne 0) { Fail "git archive failed" }
if (-not (Test-Path $tarball)) { Fail "git archive produced no file" }
Write-Host ("  tarball           : {0}  ({1} bytes)" -f (Split-Path $tarball -Leaf), (Get-Item $tarball).Length)

# Guard: prove it really is LF. Silent otherwise, and every source file on the
# board would quietly become CRLF.
$probe = Join-Path $env:TEMP "gui-sync-probe"
if (Test-Path $probe) { Remove-Item $probe -Recurse -Force }
New-Item -ItemType Directory -Force $probe | Out-Null
& tar -xf $tarball -C $probe
$crlfFiles = @()
foreach ($f in (Get-ChildItem $probe -Recurse -File)) {
    $b = [System.IO.File]::ReadAllBytes($f.FullName)
    for ($i = 0; $i -lt $b.Length - 1; $i++) {
        if ($b[$i] -eq 13 -and $b[$i + 1] -eq 10) {
            $crlfFiles += $f.FullName.Substring($probe.Length + 1)
            break
        }
    }
}
$probeCount = (Get-ChildItem $probe -Recurse -File | Measure-Object).Count
Remove-Item $probe -Recurse -Force -ErrorAction SilentlyContinue
if ($crlfFiles.Count -gt 0) {
    Fail "tarball contains CRLF in $($crlfFiles.Count) file(s): $($crlfFiles[0]) ..."
}
Write-Host "  line endings      : LF only (checked $probeCount files)"

# ------------------------------------------------------ 4. ship --------------
Step 4 "ship to board ($Target)"

& scp $tarball "${Target}:${remoteTmp}/gui-sync.tar.gz"
if ($LASTEXITCODE -ne 0) { Fail "scp tarball failed" }
Write-Host "  tarball           : sent"

# ------------------------------------------------------ 5. extract -----------
Step 5 "extract on board (additive, no delete)"

$exCmd = 'cd ' + $remoteRoot + ' && tar -xzf ' + $remoteTmp + '/gui-sync.tar.gz && echo extracted'
& ssh $Target $exCmd
if ($LASTEXITCODE -ne 0) { Fail "remote extract failed" }

# Quote-free remote commands only: quotes inside the ssh argument get mangled on
# the way through PowerShell 5.1 (a bare "(" then reaches bash and is a syntax
# error). Plain tokens avoid the whole class of problem.
Write-Host "  --- board-owned things must survive ---"
& ssh $Target ('test -f ' + $remoteRoot + '/config/config.yaml && echo config-truth-present || echo config-truth-MISSING')
& ssh $Target ('test -f ' + $remoteRoot + '/gui/build/CMakeCache.txt && echo gui-build-configured || echo gui-build-NOT-configured')

$changed = (& ssh $Target ('cd ' + $remoteRoot + ' && git status --porcelain -- gui/ | wc -l')).Trim()
Write-Host "  board gui/ vs HEAD: $changed changed entries (expected: the files we just shipped)"

# ------------------------------------------------------ 6. rebuild -----------
if (-not $NoBuild) {
    Step 6 "rebuild on the board (native aarch64 cmake)"

    # -S/-B reuses CMakeCache.txt (RelWithDebInfo, Qt5 from /usr/lib/aarch64-linux-gnu),
    # so this picks up CMakeLists/autogen changes without discarding the config.
    $bbCmd = 'cd ' + $remoteRoot + ' && (cmake -S gui -B gui/build > ' + $remoteTmp + '/gui-cmake.log 2>&1 && cmake --build gui/build -j' + $Jobs + ' > ' + $remoteTmp + '/gui-build.log 2>&1); echo EXIT=$?'
    $rc = (& ssh $Target $bbCmd).Trim()
    Write-Host "  remote says       : $rc"

    $logDir = Join-Path $env:TEMP "gui-sync-logs"
    if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force $logDir | Out-Null }
    & scp "${Target}:${remoteTmp}/gui-build.log" (Join-Path $logDir "gui-build.log") | Out-Null
    Write-Host "  --- build tail ---"
    if (Test-Path (Join-Path $logDir "gui-build.log")) {
        Get-Content (Join-Path $logDir "gui-build.log") | Select-Object -Last 8 | ForEach-Object { Write-Host "  $_" }
    }

    $buildExit = 1
    if ($rc -match 'EXIT=(\d+)') { $buildExit = [int]$Matches[1] }
    if ($buildExit -ne 0) {
        Write-Host "  (configure log: ${remoteTmp}/gui-cmake.log on the board)" -ForegroundColor Yellow
        Fail "cmake build failed with exit $buildExit"
    }
    Write-Host "  build             : OK (log: $(Join-Path $logDir 'gui-build.log'))"
} else {
    Step 6 "rebuild (skipped: -NoBuild)"
}

# ------------------------------------------------------ 7. ctest (optional) --
if ($Test) {
    Step 7 "board GUI unit tests (ctest)"
    $ctCmd = 'cd ' + $remoteRoot + '/gui/build && (ctest --output-on-failure > ' + $remoteTmp + '/gui-ctest.log 2>&1); echo EXIT=$?'
    $cr = (& ssh $Target $ctCmd).Trim()
    $ctLog = Join-Path $env:TEMP "gui-ctest.log"
    & scp "${Target}:${remoteTmp}/gui-ctest.log" $ctLog | Out-Null
    if (Test-Path $ctLog) {
        Get-Content $ctLog | Select-Object -Last 6 | ForEach-Object { Write-Host "  $_" }
    }
    $ctExit = 1
    if ($cr -match 'EXIT=(\d+)') { $ctExit = [int]$Matches[1] }
    if ($ctExit -ne 0) { Fail "ctest failed with exit $ctExit (full log: $ctLog)" }
    Write-Host "  ctest             : OK (full log: $ctLog)"
} 

# ------------------------------------------------------ 8. report ------------
Step 8 "board binary"

& ssh $Target ('ls -l ' + $remoteRoot + '/gui/build/agent_gui')

# ------------------------------------------------------ cleanup --------------
& ssh $Target ('rm -f ' + $remoteTmp + '/gui-sync.tar.gz')
if (Test-Path $tarball) { Remove-Item $tarball -Force }

Write-Host ""
Write-Host "=== gui sync ok (board $mbr, source $branch @ $head) ===" -ForegroundColor Green
