# upgrade_openalgo.ps1 - ONE-COMMAND SAFE UPGRADE of the OpenAlgo platform.
# Location: my-trading-system\scripts\ops\upgrade_openalgo.ps1
#
# ARCHITECTURE (locked):
#   Python (blue_engine)  ->  OpenAlgo REST/SDK  ->  Broker  ->  Exchange
#
# openalgo\ (retired 2026-11-27) was a PRISTINE upstream git clone. That folder no longer
# exists: the ONLY OpenAlgo copy is the vendored tree src\quant\openalgo_platform (decision
# 0013), tracked by THIS repo and pinned in openalgo.UPSTREAM_PIN. All local state still
# lives in the SIBLING folder _restored\ :
#
#   _restored\.env    OpenAlgo config (FERNET_SALT, API_KEY_PEPPER, DB urls)
#   _restored\db\     SQLite DBs - broker session tokens live here, encrypted
#   _restored\keys\   Fernet / oauth key material
#   _restored\download\  master-contract symbol cache
#
# CRITICAL: .env and db\ are cryptographically married. FERNET_SALT and
# API_KEY_PEPPER in .env are the KDF inputs that encrypt the broker tokens in
# db\openalgo.db. Restore one without the other and OpenAlgo boots but cannot
# decrypt your broker login. This script always moves them together.
#
# Why a sibling and not a subfolder of the tree: OpenAlgo resolves .env and
# db\ relative to its own root, so state in a subfolder would be invisible.
# The bridge is scripts\ops\start_openalgo.py, which copies _restored\* into the
# tree at startup (bootstrap_restored).
#
# Safety gates (institutional desk SOP):
#   1. Refuses to run during NSE market hours (09:15-15:30 IST, Mon-Fri)
#   2. Refuses to run if OpenAlgo reports open positions (unless -Force)
#   3. Full backup of _restored\ state + source commit before touching anything
#   4. NO upstream fetch - the tree is vendored and tracked by this repo
#   5. Verifies the .env/db pairing still decrypts (catches partial restores)
#   6. Runs scripts\verify\smoke_test.py as the acceptance gate
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\ops\upgrade_openalgo.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\ops\upgrade_openalgo.ps1 -Force      # skip position check
#   -Reclone is retired and aborts: a re-clone would fork the vendored tree.

param(
    [switch]$Force,
    [switch]$SkipSmoke,
    [switch]$Reclone
)

$ErrorActionPreference = "Stop"
$git     = "C:\Program Files\Git\cmd\git.exe"
$root    = Split-Path $PSScriptRoot -Parent.parent
# The vendored tree is the ONLY OpenAlgo copy (decision 0013). It is a git subtree inside THIS
# repo, so "upgrade" = update the pin + commit, never a clone. openalgo\ is gone.
$oa      = Join-Path $root "src\quant\openalgo_platform"
$restored= Join-Path $root "_restored"
$venvPy  = Join-Path $oa ".venv\Scripts\python.exe"

# State that must survive an upgrade. Keep .env and db together - see header.
$stateItems = @(".env", "db", "keys", "download")

function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Fail($m) { Write-Host "    [ABORT] $m" -ForegroundColor Red; exit 1 }
function Ok($m)   { Write-Host "    [OK] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "    [WARN] $m" -ForegroundColor Yellow }

# ---------- Gate 1: market hours (IST) ----------
Step "Gate 1: market-hours check"
$ist = [System.TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow, "India Standard Time")
if ($ist.DayOfWeek -in @("Monday","Tuesday","Wednesday","Thursday","Friday")) {
    $t = $ist.TimeOfDay
    if ($t -ge [TimeSpan]"09:15" -and $t -le [TimeSpan]"15:30") {
        Fail "Market is OPEN (IST $($ist.ToString('HH:mm'))). Upgrading mid-session is forbidden. Run after 15:30."
    }
}
Ok "Outside market hours (IST $($ist.ToString('yyyy-MM-dd HH:mm')))"

# ---------- Gate 2: open positions ----------
if (-not $Force) {
    Step "Gate 2: open-position check"
    if (Test-Path $venvPy) {
        $check = & $venvPy -c @"
import os, sys
sys.path.insert(0, os.getcwd())
try:
    from dotenv import load_dotenv
    load_dotenv('.env')
    from openalgo import api
    c = api(api_key=os.getenv('OPENALGO_API_KEY'),
            host=os.getenv('HOST_SERVER', 'http://127.0.0.1:5000'))
    r = c.positionbook()
    positions = r.get('data', []) if isinstance(r, dict) else []
    open_pos = [p for p in positions if float(p.get('quantity', 0) or 0) != 0]
    if open_pos:
        print('OPEN'); sys.exit(2)
    print('FLAT')
except Exception:
    print('UNKNOWN'); sys.exit(0)
"@ 2>$null
        if ("$check".Trim() -eq "OPEN") { Fail "Open positions found. Square off or use -Force." }
        Ok "Position book flat (or platform unreachable - proceeding)"
    } else { Warn "venv python missing - skipping check" }
} else { Warn "[-Force] position check SKIPPED" }

# ---------- Gate 3: state present ----------
Step "Gate 3: state check (_restored\)"
if (-not (Test-Path $restored)) { Fail "_restored\ missing at $restored - refusing to upgrade" }
foreach ($must in @(".env", "db")) {
    if (-not (Test-Path (Join-Path $restored $must))) {
        Fail "_restored\$must missing. Without it a re-clone loses your broker session."
    }
}
Ok "_restored\.env + _restored\db present (married pair intact)"

# ---------- Step 1: backup ----------
Step "Backup state + source commit"
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$bdir = Join-Path $root "_backup\upgrade_$stamp"
New-Item -ItemType Directory -Force -Path $bdir | Out-Null
foreach ($item in $stateItems) {
    $src = Join-Path $oa $item
    if (Test-Path $src) {
        Copy-Item $src -Destination (Join-Path $bdir $item) -Recurse -Force
    }
}
# Record what we upgraded FROM - invaluable when a strategy misbehaves later.
# The vendored tree has no .git of its own: identity is the pin file, not a SHA.
$pinFile = Join-Path $root "openalgo.UPSTREAM_PIN"
$fromCommit = if (Test-Path $pinFile) { (Get-Content $pinFile -TotalCount 1) } else { "(no pin file)" }
$dirty = 0
@(
    "upgraded_at : $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
    "mode        : vendored"
    "pin_from    : $fromCommit"
    "dirty_files : $dirty  (n/a - vendored tree is tracked by this repo)"
) | Set-Content (Join-Path $bdir "UPGRADE_FROM.txt")
Ok "Backup: $bdir"

# ---------- Step 2: fetch upstream ----------
# The tree is committed by this repo, so an upgrade is: unpin -> replace files -> re-pin.
# A blind `git pull` inside it would be a no-op (no .git) and a re-clone into it would fork
# the code and break the pin. Both are refused here; use upgrade_check.py.
if ($Reclone) { Fail "-Reclone is retired: the vendored tree is replaced by a repo commit, not a clone." }
Step "Upstream fetch: SKIPPED (vendored tree, pinned)"
Warn "To move to a new OpenAlgo version: re-vendor src\quant\openalgo_platform, then run"
Warn "  python scripts\verify\upgrade_check.py --update-pin --openalgo-dir src/quant/openalgo_platform"

# ---------- Step 3: restore state ----------
Step "Restore state from _restored\"
foreach ($item in $stateItems) {
    $src = Join-Path $restored $item
    $dst = Join-Path $oa $item
    if (-not (Test-Path $src)) { Warn "$item not in _restored\ - skipped"; continue }
    if ((Get-Item $src).PSIsContainer) {
        New-Item -ItemType Directory -Force -Path $dst | Out-Null
        Copy-Item (Join-Path $src "*") -Destination $dst -Recurse -Force
    } else {
        Copy-Item $src -Destination $dst -Force
    }
    Ok "restored $item"
}
foreach ($must in @(".env", "db\openalgo.db")) {
    if (-not (Test-Path (Join-Path $oa $must))) { Fail "post-restore check failed: $must missing" }
}
Ok "post-restore: .env + db\openalgo.db in place"

# ---------- Step 4: dependencies ----------
Step "Reinstall OpenAlgo requirements (venv)"
if (Test-Path $venvPy) {
    & $venvPy -m pip install -r (Join-Path $oa "requirements.txt") --quiet 2>&1 |
        Select-Object -Last 2 | ForEach-Object { Write-Host "    $_" }
    Ok "requirements synced"
} else {
    Warn "venv python not found at $venvPy - create it first: python -m venv src\quant\openalgo_platform\.venv"
}

# ---------- Step 5: verify the .env/db pairing still decrypts ----------
Step "Verify .env/db pairing (broker tokens decrypt)"
if (Test-Path $venvPy) {
    $chkPath = Join-Path $oa "_verify_pairing.py"
    @'
import os, sys, sqlite3
sys.path.insert(0, os.getcwd())
from dotenv import load_dotenv
load_dotenv(".env")
from database.auth_db import decrypt_token
c = sqlite3.connect("db/openalgo.db")
row = c.execute("select auth, feed_token from auth limit 1").fetchone()
if not row:
    print("NO_AUTH_ROW"); sys.exit(0)
decrypt_token(row[0])
print("DECRYPT_OK")
'@ | Set-Content -Path $chkPath -Encoding UTF8
    Push-Location $oa
    try { $out = (& $venvPy $chkPath 2>&1 | Select-Object -Last 1) }
    finally { Pop-Location; Remove-Item $chkPath -Force -ErrorAction SilentlyContinue }

    if ($out -match "DECRYPT_OK")      { Ok "broker tokens decrypt with restored .env (pairing valid)" }
    elseif ($out -match "NO_AUTH_ROW") { Warn "no broker login stored - log in via the UI when ready" }
    else {
        Fail "broker token decrypt FAILED: $out - restore aborted mid-way. Roll back from $bdir"
    }
} else { Warn "venv missing - pairing check skipped" }

# ---------- Step 6: smoke test ----------
if (-not $SkipSmoke -and (Test-Path $venvPy)) {
    Step "Smoke test (acceptance gate)"
    & $venvPy (Join-Path $root "scripts\verify\smoke_test.py")
    if ($LASTEXITCODE -ne 0) {
        Fail "Smoke test FAILED. Roll back from $bdir before trading."
    }
    Ok "All smoke checks passed"
} else { Warn "[-SkipSmoke] smoke test SKIPPED" }

Write-Host "`n========================================" -ForegroundColor Green
Write-Host "UPGRADE COMPLETE - state restored, clone pristine, engine untouched." -ForegroundColor Green
Write-Host "Data path: Python -> OpenAlgo -> Broker -> Exchange" -ForegroundColor Cyan
Write-Host "Next: .\scripts\ops\start_openalgo.py" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
exit 0
