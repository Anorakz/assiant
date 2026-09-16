# ============================================================================
#  scripts/setup-sysroot-deps.ps1
#
#  Install the *dev* packages that a board-synced sysroot is missing.
#  A rootfs pulled off the board only has runtime libs, no headers and no
#  unversioned .so link names, so the cross build fails without this.
#  Run once per sysroot; re-run after a firmware upgrade.
#
#  What gets installed and why
#  ---------------------------------------------------------------------------
#  1) libpython3.8-dev
#       pybind11 needs Python.h; the rootfs only has libpython3.8.so.1.
#       Pinned to 3.8.10 (Ubuntu focal) to match the board's interpreter.
#
#  2) ffmpeg dev (libavcodec-dev / libavutil-dev / libswscale-dev /
#                 libswresample-dev / libavformat-dev)
#       decoder.cpp includes <libavcodec/avcodec.h>; the rootfs only ships
#       libavcodec.so.58 and friends. Pinned to the board's runtime version
#       4.2.7-0ubuntu0.1 (focal-updates), NOT focal's initial 4.2.2.
#
#  Usage:
#      scripts/setup-sysroot-deps.ps1
#      scripts/setup-sysroot-deps.ps1 -Force     # re-download
#
#  Notes
#  ---------------------------------------------------------------------------
#  * .deb files are unpacked with tar (a .deb is an ar archive), so no dpkg/apt
#    is needed and this works on Windows.
#  * Windows tar cannot create the symlinks inside a .deb, so any missing
#    unversioned .so name gets a one-line linker script instead.
#  * FFmpeg headers land in the multiarch include dir, matching the -isystem
#    in cmake/toolchain.cmake.
#
#  NOTE: keep this file ASCII-only. Windows PowerShell 5.1 decodes .ps1 as ANSI
#  unless a BOM is present, which corrupts non-ASCII text and can break parsing.
# ============================================================================

param(
    [string]$Sysroot = "E:\rk3568\sysroot",
    [string]$DownloadDir = "E:\rk3568\_dl\sysroot-deps",
    [switch]$Force
)

# param() must be the FIRST STATEMENT in a script (only comments and blank
# lines may precede it). Putting $ErrorActionPreference before it makes
# PowerShell look for a command called "param" and fail with
# "The term 'param' is not recognized".
$ErrorActionPreference = "Stop"

$ports = "http://ports.ubuntu.com/ubuntu-ports/pool"

$debPackages = @(
    @{ Dir = "main/p/python3.8";  File = "libpython3.8-dev_3.8.10-0ubuntu1~20.04.18_arm64.deb" },
    @{ Dir = "universe/f/ffmpeg"; File = "libavcodec-dev_4.2.7-0ubuntu0.1_arm64.deb" },
    @{ Dir = "universe/f/ffmpeg"; File = "libavutil-dev_4.2.7-0ubuntu0.1_arm64.deb" },
    @{ Dir = "universe/f/ffmpeg"; File = "libswscale-dev_4.2.7-0ubuntu0.1_arm64.deb" },
    @{ Dir = "universe/f/ffmpeg"; File = "libswresample-dev_4.2.7-0ubuntu0.1_arm64.deb" },
    @{ Dir = "universe/f/ffmpeg"; File = "libavformat-dev_4.2.7-0ubuntu0.1_arm64.deb" }
)

if (-not (Test-Path $Sysroot)) {
    throw "sysroot not found: $Sysroot"
}
New-Item -ItemType Directory -Force -Path $DownloadDir | Out-Null

$incMulti = Join-Path $Sysroot "usr/include/aarch64-linux-gnu"
$libDir   = Join-Path $Sysroot "usr/lib/aarch64-linux-gnu"
$pcDir    = Join-Path $libDir "pkgconfig"
New-Item -ItemType Directory -Force -Path $incMulti, $libDir, $pcDir | Out-Null

# ------------------------------------------------------------------ download -
foreach ($p in $debPackages) {
    $dest = Join-Path $DownloadDir $p.File
    if ((Test-Path $dest) -and -not $Force) {
        Write-Host "cached: $($p.File)"
        continue
    }
    $url = "$ports/$($p.Dir)/$($p.File)"
    Write-Host "download: $($p.File)"
    & curl.exe -s -L --fail --retry 3 --max-time 600 -o $dest $url
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $dest)) {
        throw "download failed: $url"
    }
}

# ------------------------------------------------------------ unpack+install -
$work = Join-Path $DownloadDir "extracted"
New-Item -ItemType Directory -Force -Path $work | Out-Null

foreach ($p in $debPackages) {
    $deb = Join-Path $DownloadDir $p.File
    $dir = Join-Path $work ([System.IO.Path]::GetFileNameWithoutExtension($p.File))
    # test for the extracted include dir, not data.tar.xz -- a half-unpacked
    # directory can still contain the archive while missing the payload
    if (-not (Test-Path "$dir\usr\include")) {
        Remove-Item -Recurse -Force $dir -ErrorAction SilentlyContinue
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        & tar.exe -xf $deb -C $dir
        if ($LASTEXITCODE -ne 0) { throw "unpack failed: $($p.File)" }
        # Second pass: Windows tar cannot create the symlinks inside a .deb and
        # writes those failures to stderr. With ErrorActionPreference=Stop that
        # would abort the script, so relax it here -- the exit code is what we
        # actually care about, and the payload is already on disk either way.
        $prev = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        & tar.exe -xf "$dir\data.tar.xz" -C $dir 2>&1 | Out-Null
        $ErrorActionPreference = $prev
    }
    Write-Host "install: $($p.File)"
}

foreach ($d in (Get-ChildItem $work -Directory)) {
    $srcInc = Join-Path $d.FullName "usr/include/aarch64-linux-gnu"
    if (Test-Path $srcInc) {
        Copy-Item "$srcInc\*" -Destination $incMulti -Recurse -Force
    }
    $srcPc = Join-Path $d.FullName "usr/lib/aarch64-linux-gnu/pkgconfig"
    if (Test-Path $srcPc) {
        Copy-Item "$srcPc\*" -Destination $pcDir -Recurse -Force
    }
}

# Python 3.8 headers are arch-independent and live at usr/include/python3.8
# (NOT under the multiarch dir), matching what the board has.
$pyIncSrc = Join-Path $work "libpython3.8-dev_3.8.10-0ubuntu1~20.04.18_arm64/usr/include/python3.8"
if (Test-Path $pyIncSrc) {
    Copy-Item "$pyIncSrc\*" -Destination (Join-Path $Sysroot "usr/include/python3.8") -Recurse -Force
}

$pyCfgSrc = Join-Path $work "libpython3.8-dev_3.8.10-0ubuntu1~20.04.18_arm64/usr/lib/python3.8"
if (Test-Path $pyCfgSrc) {
    Copy-Item "$pyCfgSrc\*" -Destination (Join-Path $Sysroot "usr/lib/python3.8") -Recurse -Force
}

# ------------------------------------- fill in missing unversioned .so names -
$sonames = @(
    "libpython3.8", "libavcodec", "libavutil", "libswscale", "libswresample", "libavformat"
)
foreach ($n in $sonames) {
    $soPath = Join-Path $libDir "$n.so"
    if (Test-Path $soPath) { continue }
    $real = Get-ChildItem $libDir -Filter "$n.so.*" -File -ErrorAction SilentlyContinue |
            Sort-Object Name | Select-Object -First 1
    if ($real) {
        Set-Content -Path $soPath -NoNewline -Encoding ascii `
            -Value "INPUT ( /usr/lib/aarch64-linux-gnu/$($real.Name) )"
        Write-Host "linker script: $n.so -> $($real.Name)"
    } else {
        Write-Warning "no $n.so.* in sysroot, skipped (board may not ship this lib)"
    }
}

# ------------------------------------------------------------------- verify --
Write-Host ""
Write-Host "verify:" -ForegroundColor Cyan
$checks = @(
    (Join-Path $Sysroot "usr/include/python3.8/Python.h"),
    (Join-Path $incMulti "libavcodec/avcodec.h"),
    (Join-Path $incMulti "libavutil/frame.h"),
    (Join-Path $libDir  "libpython3.8.so"),
    (Join-Path $libDir  "libavcodec.so")
)
$bad = 0
foreach ($c in $checks) {
    $ok = Test-Path $c
    if (-not $ok) { $bad++ }
    "{0} {1}" -f $(if ($ok) { "  OK  " } else { " MISS " }), $c
}

if ($bad -gt 0) {
    throw "$bad item(s) missing, sysroot deps incomplete"
}
Write-Host ""
Write-Host "sysroot deps ready -> $Sysroot" -ForegroundColor Green
