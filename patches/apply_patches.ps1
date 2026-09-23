# apply_patches.ps1 - Re-applies the POV wiring into the OpenAlgo folder after any upgrade.
# Location: my-trading-system\patches\apply_patches.ps1
# Rule: this is the ONLY bridge into openalgo/. OpenAlgo itself is never committed to.

param(
    [string]$OpenalgoDir = (Split-Path $PSScriptRoot -Parent) + "\openalgo",
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"
$patchDir = $PSScriptRoot
$blueprintDst = Join-Path $OpenalgoDir "blueprints\pov_terminal.py"
$blueprintSrc = Join-Path $patchDir "pov_terminal.py"
$appPy = Join-Path $OpenalgoDir "app.py"

$importMarker  = "from blueprints.pov_terminal import pov_terminal_bp"
$registerMarker = "app.register_blueprint(pov_terminal_bp)"

function Pass([string]$msg) { Write-Host "  [PASS] $msg" -ForegroundColor Green }
function Skip([string]$msg) { Write-Host "  [SKIP] $msg (marker already present)" -ForegroundColor Yellow }

# ---- 0. sanity ----
if (-not (Test-Path (Join-Path $OpenalgoDir "app.py"))) {
    Write-Host "  [FAIL] app.py not found in $OpenalgoDir" -ForegroundColor Red
    exit 1
}
Write-Host "OpenAlgo dir : $OpenalgoDir"
Write-Host "Check-only   : $CheckOnly"
Write-Host ""

# ---- 1. copy canonical blueprint ----
if ((Test-Path $blueprintDst) -and (-not $CheckOnly)) {
    $same = ($null -eq (Compare-Object (Get-Content $blueprintSrc) (Get-Content $blueprintDst)))
    if ($same) { Skip "blueprint pov_terminal.py is identical" }
    else { Copy-Item $blueprintSrc $blueprintDst -Force; Pass "blueprint pov_terminal.py refreshed (canonical copy applied)" }
}
elseif ($CheckOnly) {
    if (Test-Path $blueprintDst) { Pass "blueprint present" } else { Write-Host "  [FAIL] blueprint missing" -ForegroundColor Red; exit 1 }
}
else {
    Copy-Item $blueprintSrc $blueprintDst -Force
    Pass "blueprint pov_terminal.py copied into blueprints/"
}

# ---- 2. inject import into app.py (after the last 'from blueprints.' import) ----
$appLines = Get-Content $appPy
$changed = $false

if ($appLines -match [regex]::Escape($importMarker)) { Skip "app.py import line" }
elseif ($CheckOnly) { Write-Host "  [FAIL] app.py import line missing" -ForegroundColor Red; exit 1 }
else {
    $lastBlueprintImport = ($appLines | Select-String "^from blueprints\." | Select-Object -Last 1).LineNumber
    if ($lastBlueprintImport -ge 1) {
        $idx = $lastBlueprintImport
        $appLines = $appLines[0..($idx)] + $importMarker + $appLines[($idx + 1)..($appLines.Count - 1)]
        $changed = $true
        Pass "app.py import line injected after last blueprints import (line $idx)"
    } else {
        # fallback: append after the last import at the top of the file
        $lastImport = ($appLines | Select-String "^(import |from )" | Select-Object -First 15 | Select-Object -Last 1).LineNumber
        if ($lastImport -ge 1) {
            $idx = $lastImport
            $appLines = $appLines[0..($idx)] + $importMarker + $appLines[($idx + 1)..($appLines.Count - 1)]
            $changed = $true
            Pass "app.py import line injected after last top import (fallback, line $idx)"
        } else {
            Write-Host "  [FAIL] app.py shape changed - could not find an import to anchor on" -ForegroundColor Red
            exit 1
        }
    }
}

# ---- 3. inject register_blueprint (after the last 'app.register_blueprint(' call) ----
if ($appLines -match [regex]::Escape($registerMarker)) { Skip "app.py register_blueprint line" }
elseif ($CheckOnly) { Write-Host "  [FAIL] app.py register_blueprint missing" -ForegroundColor Red; exit 1 }
else {
    $lastReg = ($appLines | Select-String "^\s*app\.register_blueprint\(" | Select-Object -Last 1).LineNumber
    if ($lastReg -ge 1) {
        $idx = $lastReg
        $indent = ([regex]::Match($appLines[$idx - 1], "^\s*")).Value
        $appLines = $appLines[0..($idx)] + "$indent$registerMarker  # Embedded POV Dash terminal config" + $appLines[($idx + 1)..($appLines.Count - 1)]
        $changed = $true
        Pass "app.py register_blueprint injected after last blueprint registration (line $idx)"
    } else {
        Write-Host "  [FAIL] app.py shape changed - no register_blueprint() anchor found" -ForegroundColor Red
        exit 1
    }
}

# ---- 4. write back ----
if ($changed -and (-not $CheckOnly)) {
    $u8 = New-Object System.Text.UTF8Encoding($false); [System.IO.File]::WriteAllLines($appPy, [string[]]$appLines, $u8)
    Pass "app.py written back"
}

Write-Host ""
if ($CheckOnly) { Write-Host "Patch CHECK: OK - wiring present" -ForegroundColor Green }
else { Write-Host "Patch apply: OK - POV wiring re-applied. Restart OpenAlgo to pick it up." -ForegroundColor Green }