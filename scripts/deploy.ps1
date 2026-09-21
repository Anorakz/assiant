# ============================================================================
#  scripts/deploy.ps1 -- cross-compile, ship to the board, then health-check.
#
#  NOTE: keep this file ASCII-only. Windows PowerShell 5.1 decodes .ps1 as ANSI
#  unless a BOM is present, which turns non-ASCII text into mojibake (the
#  previous version of this script was mangled that way).
#  Also: param() must be the FIRST statement -- so it sits above
#  $ErrorActionPreference, not below it.
#
#  Usage:
#      scripts/deploy.ps1                 # build + ship + health check
#      scripts/deploy.ps1 -NoBuild        # skip the cross build
#      scripts/deploy.ps1 -NoCheck        # skip the board-side health check
#      scripts/deploy.ps1 -PruneDryRun    # list board leftovers, delete nothing
#      scripts/deploy.ps1 -Prune          # same, but actually deletes them
#      scripts/deploy.ps1 -Target myhost  # ssh alias / host (default rk3568)
#
#  How the Python/tests/docs half is shipped (decided in Phase 6 / task A0):
#      git archive HEAD <paths>  ->  tar.gz  ->  scp  ->  tar -xzf on the board
#
#  Why git archive instead of scp-ing the working tree:
#    1) only COMMITTED content travels -- what you deploy is what you committed
#    2) line endings: see the WARNING below -- we force LF explicitly
#    3) extraction is ADDITIVE -- no --delete, so board-only files survive
#       (config/config.yaml, tests/test_llm_integration.py, docs/gui-qt5-*.md)
#
#  But additive extraction has a second face: files DELETED in the repo keep
#  living on the board. So this script also:
#    * writes logs/deployed-rev + logs/deployed-manifest (sha256 per file) to the
#      board, which is what lets health_check.sh report "behind by N files";
#    * offers -Prune / -PruneDryRun to list/remove those leftovers.
#  The rule for "what counts as a leftover" lives ONLY in health_check.sh
#  (--list-extra) -- this script consumes that list instead of re-implementing it.
#
#  WARNING about line endings (this bit us in task A1):
#    `git archive` applies the same text conversion as a checkout, so on Windows
#    with core.autocrlf=true it writes CRLF INTO THE TARBALL. That is fatal for
#    the shell scripts we ship (bash: "$'\r': command not found").
#    Measured: scripts/verify-authorized.sh came out 2635 bytes / 74 CRLF lines
#    instead of 2561 bytes / 0 CRLF.
#    => always pass -c core.autocrlf=false -c core.eol=lf to git archive.
#       Then the archive is byte-identical to the committed blob.
#
#  NEVER touched by this script (they exist on the board and not in main):
#      config/config.yaml, gui/, llm/, sig/, net/, runtimes/, temp/, creds/
#
#  Native sources are NOT shipped: the board does not compile native code, it
#  only consumes the cross-built .so files.
# ============================================================================

param(
    [switch]$NoBuild,
    [switch]$NoCheck,
    [switch]$Prune,          # remove board leftovers that are no longer in the repo
    [switch]$PruneDryRun,    # only list what -Prune would remove (do not delete)
    [string]$Target = "rk3568"
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$build = Join-Path $root "build-rk3568"
$remoteRoot = "/home/kickpi/myproject/assitant"
$remoteTmp = "/tmp"

# Paths archived with git archive (relative to repo root).
# Deliberately selective: no native/ (sources), no gui/ (the board owns it),
# no config/config.yaml (the board owns the live config).
$archivePaths = @(
    "agent",
    "tests",
    "docs",
    "scripts",
    "config/config.example.yaml"
)

function Step($n, $text) {
    Write-Host ""
    Write-Host "=== $n. $text ===" -ForegroundColor Cyan
}

function Fail($text) {
    Write-Host "FAILED: $text" -ForegroundColor Red
    exit 1
}

# --------------------------------------------------------------- 1. build ----
if (-not $NoBuild) {
    Step 1 "cross build"
    & (Join-Path $PSScriptRoot "build.ps1")
    if ($LASTEXITCODE -ne 0) { Fail "build.ps1 returned $LASTEXITCODE" }
} else {
    Step 1 "cross build (skipped: -NoBuild)"
}

# ------------------------------------------------------- 2. collect pieces ---
Step 2 "locate artifacts"

$soItem = Get-ChildItem (Join-Path $build "native") -Filter "agent_native*.so" -ErrorAction SilentlyContinue |
          Select-Object -First 1
if (-not $soItem) { Fail "no agent_native*.so under build-rk3568\native -- run the build first" }
Write-Host ("  agent_native      : {0}  ({1} bytes)" -f $soItem.Name, $soItem.Length)

$mlItem = Get-ChildItem (Join-Path $build "moonlight-common-c") -Filter "libmoonlight-common-c.so" -ErrorAction SilentlyContinue |
          Select-Object -First 1
if (-not $mlItem) { Fail "no libmoonlight-common-c.so under build-rk3568\moonlight-common-c" }
Write-Host ("  libmoonlight      : {0} bytes" -f $mlItem.Length)

$smokePath = Join-Path $build "tests\board\mpp_decode_smoke"
if (Test-Path $smokePath) {
    Write-Host ("  mpp_decode_smoke  : {0} bytes" -f (Get-Item $smokePath).Length)
} else {
    Write-Host "  mpp_decode_smoke  : (not built -- skipped)" -ForegroundColor Yellow
}

$hcPath = Join-Path $PSScriptRoot "health_check.sh"
if (-not (Test-Path $hcPath)) { Fail "scripts/health_check.sh missing" }

# --------------------------------------------------------- 3. git archive ----
Step 3 "package committed tree (git archive)"

$head = (& git -C $root rev-parse --short HEAD).Trim()
$branch = (& git -C $root rev-parse --abbrev-ref HEAD).Trim()
Write-Host "  branch/HEAD       : $branch / $head"

$tarball = Join-Path $env:TEMP "agent-deploy-$head.tar.gz"
if (Test-Path $tarball) { Remove-Item $tarball -Force }

& git -C $root -c core.autocrlf=false -c core.eol=lf archive --format=tar.gz -o $tarball HEAD @archivePaths
if ($LASTEXITCODE -ne 0) { Fail "git archive failed" }
if (-not (Test-Path $tarball)) { Fail "git archive produced no file" }
Write-Host ("  tarball           : {0}  ({1} bytes)" -f (Split-Path $tarball -Leaf), (Get-Item $tarball).Length)

# Guard: prove the tarball really is LF (the CRLF bug above was silent otherwise).
# We also unpack the WHOLE tarball here: the next step hashes every file to build
# the deploy manifest.
$probe = Join-Path $env:TEMP "agent-deploy-tree"
if (Test-Path $probe) { Remove-Item $probe -Recurse -Force }
New-Item -ItemType Directory -Force $probe | Out-Null
& tar -xf $tarball -C $probe
if ($LASTEXITCODE -ne 0) { Fail "cannot unpack the tarball (needed to build the manifest)" }

$probeFile = Join-Path $probe "scripts/verify-authorized.sh"
if (Test-Path $probeFile) {
    $pb = [System.IO.File]::ReadAllBytes($probeFile)
    $crlf = 0
    for ($i = 0; $i -lt $pb.Length - 1; $i++) { if ($pb[$i] -eq 13 -and $pb[$i + 1] -eq 10) { $crlf++ } }
    if ($crlf -gt 0) { Fail "tarball contains CRLF ($crlf lines in scripts/verify-authorized.sh) -- shell scripts would break on the board" }
    Write-Host "  line endings      : LF only (checked scripts/verify-authorized.sh)"
} else {
    Write-Host "  line endings      : (probe file not found, skipped)" -ForegroundColor Yellow
}

# ------------------------------------------------- 3.5 manifest (sha256) -----
# The board's health_check.sh uses this to answer "is the board behind?" with
# per-file sha256 comparison, so it can name HOW MANY and WHICH files differ.
# Written as LF + UTF-8 without BOM: the board reads it with `while read`, and
# CRLF would leave a trailing \r on every path so nothing would ever match.
Step 3.5 "build deploy manifest (sha256)"
$manifestFile = Join-Path $env:TEMP "agent-deploy-manifest.txt"
$revFile = Join-Path $env:TEMP "agent-deploy-rev.txt"
$entries = New-Object System.Collections.Generic.List[string]

function Add-ManifestEntry([string]$fullPath, [string]$relPath) {
    $h = (Get-FileHash -LiteralPath $fullPath -Algorithm SHA256).Hash.ToLower()
    $script:entries.Add("$h  $relPath")
}

foreach ($f in (Get-ChildItem $probe -Recurse -File)) {
    Add-ManifestEntry $f.FullName $f.FullName.Substring($probe.Length + 1).Replace('\', '/')
}
# The .so files and the smoke binary are scp'd separately (not in the tarball),
# so add them by hand.
Add-ManifestEntry $soItem.FullName $soItem.Name
Add-ManifestEntry $mlItem.FullName $mlItem.Name
if (Test-Path $smokePath) { Add-ManifestEntry $smokePath "tests/board/mpp_decode_smoke" }

$enc = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($manifestFile, (($entries | Sort-Object) -join "`n") + "`n", $enc)
[System.IO.File]::WriteAllText($revFile, "$head`n", $enc)
Write-Host ("  manifest          : {0} files -> {1}" -f $entries.Count, (Split-Path $manifestFile -Leaf))
Remove-Item $probe -Recurse -Force -ErrorAction SilentlyContinue

# ------------------------------------------------------------- 4. transfer ---
Step 4 "ship to board ($Target)"

Write-Host "  --- probing ssh ---"
& ssh $Target "echo connected: `$(hostname) `$(uname -m)"
if ($LASTEXITCODE -ne 0) { Fail "ssh $Target failed (check ~/.ssh/config alias)" }

Write-Host "  --- ensure remote dirs ---"
& ssh $Target "mkdir -p $remoteRoot/tests/board $remoteRoot/tests/data $remoteRoot/logs"
if ($LASTEXITCODE -ne 0) { Fail "mkdir on board failed" }

Write-Host "  --- scp tarball + artifacts ---"
& scp $tarball "${Target}:${remoteTmp}/deploy.tar.gz"
if ($LASTEXITCODE -ne 0) { Fail "scp tarball failed" }

# The deploy manifest lives under logs/: that directory is already gitignored, so
# these two files never make the board's `git status` dirty.
& scp $manifestFile "${Target}:${remoteRoot}/logs/deployed-manifest"
if ($LASTEXITCODE -ne 0) { Fail "scp manifest failed" }
& scp $revFile "${Target}:${remoteRoot}/logs/deployed-rev"
if ($LASTEXITCODE -ne 0) { Fail "scp deployed-rev failed" }
Write-Host "  deployed-rev      : $head"

& scp $soItem.FullName "${Target}:${remoteRoot}/"
if ($LASTEXITCODE -ne 0) { Fail "scp agent_native failed" }

& scp $mlItem.FullName "${Target}:${remoteRoot}/"
if ($LASTEXITCODE -ne 0) { Fail "scp libmoonlight failed" }

if (Test-Path $smokePath) {
    & scp $smokePath "${Target}:${remoteRoot}/tests/board/"
    if ($LASTEXITCODE -ne 0) { Fail "scp mpp_decode_smoke failed" }
}

# health_check.sh travels separately so the very first deploy works even
# before it is committed to the repo.
#
# It is copied with line endings NORMALISED to LF: on Windows this working-tree
# copy can be CRLF (core.autocrlf=true converts it on checkout), and a CRLF
# shell script fails on the board with "$'\r': command not found".
$hcSafe = Join-Path $env:TEMP "health_check.sh"
$hcText = [System.IO.File]::ReadAllText($hcPath) -replace "`r`n", "`n"
[System.IO.File]::WriteAllText($hcSafe, $hcText, (New-Object System.Text.UTF8Encoding($false)))
$hcCrlf = ([regex]::Matches($hcText, "`r")).Count
if ($hcCrlf -ne 0) { Fail "health_check.sh still has CR after normalisation" }
& scp $hcSafe "${Target}:${remoteTmp}/health_check.sh"
if ($LASTEXITCODE -ne 0) { Fail "scp health_check.sh failed" }
Write-Host "  health_check.sh   : sent with LF endings"

# ------------------------------------------------------------- 5. extract ----
Step 5 "extract on board (additive, no delete)"

& ssh $Target "cd $remoteRoot && tar -xzf $remoteTmp/deploy.tar.gz && chmod +x tests/board/mpp_decode_smoke 2>/dev/null; echo extracted"
if ($LASTEXITCODE -ne 0) { Fail "remote extract failed" }

Write-Host "  --- board-owned files must survive ---"
& ssh $Target "test -f $remoteRoot/config/config.yaml && echo 'config.yaml: present' || echo 'config.yaml: MISSING'"
& ssh $Target "test -f $remoteRoot/tests/test_llm_integration.py && echo 'test_llm_integration.py: present' || echo 'test_llm_integration.py: absent'"

# The board sits on branch `gui` while we just wrote main's agent/tests/docs on
# top of it, so those paths now show up as modified against the board's HEAD.
# That is expected for this phase (merging the two branches is the deferred
# normalisation task), but it means a future `git pull` on the board can refuse
# to run. Report it so nobody is surprised.
Write-Host "  --- board tree state (informational) ---"
$changed = (& ssh $Target "cd $remoteRoot && git status --porcelain | wc -l").Trim()
$br = (& ssh $Target "cd $remoteRoot && git rev-parse --abbrev-ref HEAD").Trim()
Write-Host "  board branch      : $br"
Write-Host "  changed entries   : $changed  (deployed files differ from the board's own HEAD)"
if ($changed -ne "0") {
    Write-Host "  note              : a 'git pull' on the board may refuse until these are stashed/committed" -ForegroundColor Yellow
}

# ------------------------------------------------------------- 5.5 prune -----
# Additive extraction never deletes, so leftovers (files removed from the repo
# but still on the board) need an explicit sweep.
# The rule for "what counts as a leftover" lives ONLY in health_check.sh
# (--list-extra); this block just re-validates and executes -- two copies of the
# rule would drift apart.
if ($Prune -or $PruneDryRun) {
    Step 5.5 "prune board leftovers (by manifest)"

    $raw = & ssh $Target "cd $remoteRoot && sh $remoteTmp/health_check.sh --list-extra"
    if ($LASTEXITCODE -ne 0) { Fail "cannot list leftovers (no manifest on the board? run a plain deploy first)" }

    # Second-pass validation: only delete paths inside the deploy scope and
    # without suspicious characters. (Piping remote output straight into rm
    # would be reckless; this layer is deliberate.)
    $candidates = @()
    $rejected = @()
    foreach ($line in $raw) {
        $p = "$line".Trim()
        if (-not $p) { continue }
        if ($p.StartsWith("/") -or $p.Contains("..")) { $rejected += $p; continue }
        if ($p -notmatch '^(agent|tests|docs|scripts)/') { $rejected += $p; continue }
        if ($p -match '[\s''"`$;&|<>]') { $rejected += $p; continue }
        if ($p -match '(^|/)__pycache__/|\.pyc$') { $rejected += $p; continue }
        if ($p -eq 'tests/test_llm_integration.py') { $rejected += $p; continue }
        if ($p -match '^docs/gui-qt5-') { $rejected += $p; continue }
        $candidates += $p
    }

    if ($rejected.Count -gt 0) {
        Write-Host "  rejected          : $($rejected.Count) (out of scope / whitelisted / suspicious)" -ForegroundColor Yellow
        $rejected | Select-Object -First 5 | ForEach-Object { Write-Host "      $_" -ForegroundColor DarkGray }
    }

    if ($candidates.Count -eq 0) {
        Write-Host "  nothing to prune  : the board has no file that the repo already deleted"
    } else {
        Write-Host ("  to remove {0} file(s):" -f $candidates.Count) -ForegroundColor Yellow
        $candidates | ForEach-Object { Write-Host "      $_" }

        if ($PruneDryRun -and -not $Prune) {
            Write-Host "  (-PruneDryRun: listed only, nothing was deleted)" -ForegroundColor Yellow
        } else {
            $joined = ($candidates -join " ")
            & ssh $Target "cd $remoteRoot && rm -f -- $joined && echo pruned"
            if ($LASTEXITCODE -ne 0) { Fail "remote delete failed" }
            Write-Host ("  removed {0} file(s)" -f $candidates.Count)
            # Also drop directories this emptied (non-recursive rmdir semantics
            # via -empty, so nothing that still holds files is touched).
            & ssh $Target "cd $remoteRoot && find agent tests docs scripts -type d -empty -delete 2>/dev/null; echo done" | Out-Null
        }
    }
}

# ------------------------------------------------------------- 6. verify -----
if (-not $NoCheck) {
    Step 6 "board health check"
    & ssh $Target "sh $remoteTmp/health_check.sh"
    $hc = $LASTEXITCODE
    if ($hc -ne 0) {
        Write-Host ""
        Write-Host "health check reported failures (exit $hc)" -ForegroundColor Red
        exit $hc
    }
} else {
    Step 6 "board health check (skipped: -NoCheck)"
}

# ------------------------------------------------------------- cleanup ------
& ssh $Target "rm -f $remoteTmp/deploy.tar.gz"
if (Test-Path $tarball) { Remove-Item $tarball -Force }
if (Test-Path $manifestFile) { Remove-Item $manifestFile -Force }
if (Test-Path $revFile) { Remove-Item $revFile -Force }

Write-Host ""
Write-Host "=== deploy ok (branch $branch @ $head) ===" -ForegroundColor Green
