# complete_move.ps1 - Completes the physical relocation of openalgo into
# my-trading-system\openalgo (Plan Step 1 final deviation fix).
#
# Implements, in order, with verification after each step:
#   Step 1  remove the temporary junction, MOVE the real folder
#   Step 2  name is already correct (move lands directly on final name)
#   Step 3  recreate junction at the old path (keeps venv/old shortcuts alive)
#   Step 4  full verification: git, venv, engine import, wiring, smoke test
#   Step 5  update README note
#
# USAGE: run from a FRESH PowerShell window AFTER closing VS Code:
#   powershell -ExecutionPolicy Bypass -File C:\Users\KanagalakshmiM\my-trading-system\scripts\complete_move.ps1

$ErrorActionPreference = "Stop"
$oldPath  = "C:\Users\KanagalakshmiM\openalgo"
$mt       = "C:\Users\KanagalakshmiM\my-trading-system"
$newPath  = Join-Path $mt "openalgo"
$git      = "C:\Program Files\Git\cmd\git.exe"
$pwsh     = "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
$venvPy   = Join-Path $oldPath ".venv\Scripts\python.exe"

function Step($m)  { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Ok($m)    { Write-Host "    [OK] $m" -ForegroundColor Green }
function Fail($m)  { Write-Host "    [FAIL] $m" -ForegroundColor Red; exit 1 }
function Info($m)  { Write-Host "    $m" -ForegroundColor Gray }

# ---------- 0. sanity ----------
Step "0. Sanity checks"
$codeCount = @(Get-Process code,Code -ErrorAction SilentlyContinue).Count
if ($codeCount -gt 0) { Fail "VS Code is STILL RUNNING ($codeCount processes). It locks the openalgo folder. Close ALL VS Code windows (File > Exit), wait 5 seconds, then re-run this script." }
$pyCount = @(Get-Process python,uvicorn -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "*openalgo*" }).Count
if ($pyCount -gt 0) { Fail "An OpenAlgo python process is running. Stop OpenAlgo first, then re-run." }
if (-not (Test-Path $oldPath)) { Fail "$oldPath does not exist" }
$lt = (Get-Item $oldPath -Force).LinkType
if ($lt -eq "Junction") { Fail "$oldPath is already a junction - the move appears DONE. Run scripts\smoke_test.py to confirm." }

# already physically moved?
if ((Test-Path $newPath) -and ((Get-Item $newPath -Force).LinkType -eq $null) -and (Test-Path (Join-Path $newPath "app.py"))) {
    Info "openalgo is already physically inside my-trading-system. Skipping to verification."
} else {
    # ---------- Step 1+2: remove junction, move the real folder ----------
    Step "1. Remove temporary junction: $mt\openalgo"
    $j = Get-Item $newPath -Force -ErrorAction SilentlyContinue
    if ($j -and $j.LinkType -eq "Junction") {
        & "$env:ComSpec" /c rmdir "$newPath" | Out-Null
        if (Test-Path $newPath) { Fail "Could not remove junction at $newPath (refusing to delete contents)" }
        Ok "Junction removed (link only - real folder untouched)"
    } elseif (-not $j) { Ok "No junction present (nothing to remove)" }
    else { Fail "$newPath exists but is NOT a junction - manual inspection required" }

    Step "2. Move real folder: $oldPath -> $newPath (5 attempts, 10s apart - shrugs off antivirus/indexer locks)"
    $moved = $false
    foreach ($attempt in 1..5) {
        try {
            [System.IO.Directory]::Move($oldPath, $newPath)
            Ok "MOVE OK (attempt $attempt)"
            $moved = $true
            break
        } catch {
            Info "attempt $attempt failed: $($_.Exception.Message)"
            if ($attempt -lt 5) { Start-Sleep -Seconds 10 }
        }
    }
    if (-not $moved) {
        Info "Move failed ($($_.Exception.Message)). Restoring the junction..."
        & "$env:ComSpec" /c mklink /J "$newPath" "$oldPath" | Out-Null
        if ((Get-Item $newPath -Force).LinkType -eq "Junction") { Fail "System RESTORED to previous state (junction back at my-trading-system\openalgo). Close VS Code and ALL terminals inside openalgo, then re-run this script." }
        else { Fail "Junction restore FAILED. Fix manually: mklink /J $newPath $oldPath" }
    }

    Step "3. Recreate junction at old path: $oldPath -> $newPath"
    & "$env:ComSpec" /c mklink /J "$oldPath" "$newPath" | Out-Null
    if ((Get-Item $oldPath -Force).LinkType -eq "Junction") { Ok "Junction recreated - venv and old paths keep working" }
    else { Fail "Junction recreation failed - venv may break. Recreate manually: mklink /J $oldPath $newPath" }
}

# ---------- Step 4: verification ----------
Step "4a. git repo intact at new location"
& $git -C $newPath rev-parse --is-inside-work-tree 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) { Ok "git repo intact: $newPath" } else { Fail ".git missing at new location" }

Step "4b. venv works (through junction, absolute paths preserved)"
& $venvPy --version 2>$null | ForEach-Object { Ok "python: $_" }
if ($LASTEXITCODE -ne 0) { Fail "venv python broken - recreate junction or reinstall venv" }

Step "4c. pov_engine import resolves"
$imp = & $venvPy -c "import pov_engine; print(pov_engine.__file__)" 2>&1
Info "$imp"
if ($LASTEXITCODE -ne 0) { Fail "pov_engine import failed" }
Ok "engine import OK"

Step "4d. POV wiring check"
& $pwsh -ExecutionPolicy Bypass -File (Join-Path $mt "patches\apply_patches.ps1") -CheckOnly
if ($LASTEXITCODE -ne 0) { Fail "wiring check failed" }

Step "4e. full smoke test"
& $venvPy (Join-Path $mt "scripts\smoke_test.py")
if ($LASTEXITCODE -ne 0) { Fail "smoke test failed - review before trading" }

# ---------- Step 5: README ----------
Step "5. Update README note"
$readme = Join-Path $mt "README.md"
$md = [System.IO.File]::ReadAllText($readme)
$oldNote = "NOTE: openalgo physically remains at C:\Users\KanagalakshmiM\openalgo (VS Code had it\r`nlocked during migration) and is junction-linked here. Both paths work; do the real move`r`nafter closing VS Code if you want physical consolidation."
$oldNote2 = "NOTE: openalgo physically remains at C:\Users\KanagalakshmiM\openalgo (VS Code had it`nlocked during migration) and is junction-linked here. Both paths work; do the real move`nafter closing VS Code if you want physical consolidation."
$newNote = "NOTE: openalgo is now physically located at my-trading-system\openalgo. A junction at`nC:\Users\KanagalakshmiM\openalgo points to it, so the venv and any old absolute paths`nkeep working. Plan Step 1 is now 100% complete."
if ($md.Contains($oldNote)) { $md = $md.Replace($oldNote, $newNote) }
elseif ($md.Contains($oldNote2)) { $md = $md.Replace($oldNote2, $newNote) }
else { Info "NOTE paragraph not found verbatim - appending status line instead"; $md += "`r`n`r`nUPDATE: openalgo was physically moved into my-trading-system\openalgo on $(Get-Date -f yyyy-MM-dd)." }
[System.IO.File]::WriteAllText($readme, $md, (New-Object System.Text.UTF8Encoding($false)))
Ok "README updated"

Write-Host "`n========================================" -ForegroundColor Green
Write-Host "MOVE COMPLETE + ALL VERIFICATIONS PASSED" -ForegroundColor Green
Write-Host "Plan Step 1 deviation #1 is now closed." -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green