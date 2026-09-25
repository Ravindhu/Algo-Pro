# register_daily_jobs.ps1 - register the post-close portfolio job with Task Scheduler.
#
# RUNS AS: the current user (no elevation needed for a non-system task).
#
#   powershell -ExecutionPolicy Bypass -File scripts\register_daily_jobs.ps1
#
# Removes with:
#   powershell -ExecutionPolicy Bypass -File scripts\register_daily_jobs.ps1 -Remove
#
# WHY 15:40 IST AND NOT 15:35
# ---------------------------
# The job itself refuses to run before 15:35 (see jobs/daily_portfolio.py). We
# schedule at 15:40 to let the broker settle its end-of-day bar, so the first
# attempt almost always succeeds and we don't waste a retry on a partial candle.
#
# WHY "At logon" IS ALSO REGISTERED
# --------------------------------
# If the machine was asleep/off at 15:40, Task Scheduler can skip the trigger.
# The logon trigger catches that case: on the next logon the job runs once
# (it is idempotent - re-fetching an overlapping window updates in place, and the
# run history is append-only per day).

param(
    [switch]$Remove,
    [string]$TaskName = "BlueEngine_DailyPortfolio",
    [string]$Time = "15:40"
)

$ErrorActionPreference = "Stop"

# Resolve repo root from this script's location (scripts\ -> repo root).
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot "openalgo\.venv\Scripts\python.exe"
$WorkDir = Join-Path $RepoRoot "Blue_engine"

if ($Remove) {
    Write-Host "Removing scheduled task '$TaskName'..." -ForegroundColor Yellow
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed." -ForegroundColor Green
    } else {
        Write-Host "Task '$TaskName' not found; nothing to remove." -ForegroundColor Gray
    }
    exit 0
}

if (-not (Test-Path $Python)) {
    throw "Python not found at $Python. Create the venv first (see README.md)."
}
if (-not (Test-Path $WorkDir)) {
    throw "Blue_engine not found at $WorkDir. Run this script from the repo's scripts\ folder."
}

$action = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument "-m blue_engine.jobs.daily_portfolio" `
    -WorkingDirectory $WorkDir

# Daily trigger at the configured close+settle time.
$triggerDaily = New-ScheduledTaskTrigger -Daily -At $Time

# Run on logon too, to catch a machine that was off at the trigger time.
$triggerLogon = New-ScheduledTaskTrigger -AtLogOn

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20) `
    -MultipleInstances IgnoreNew

# Least privilege: the job only reads the broker (SDK) and writes data\ + cache.
$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Task '$TaskName' exists - replacing it." -ForegroundColor Yellow
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger @($triggerDaily, $triggerLogon) `
    -Settings $settings `
    -Principal $principal `
    -Description "Blue_engine post-close portfolio job: fetch live inputs, run the portfolio engine, persist the run. See Blue_engine/jobs/daily_portfolio.py." | Out-Null

Write-Host "Registered '$TaskName' (daily $Time + at logon)." -ForegroundColor Green
Write-Host ""
Write-Host "Verify with:" -ForegroundColor Cyan
Write-Host "  Get-ScheduledTask -TaskName $TaskName | Format-List"
Write-Host "Run it now (bypasses the market-hours guard):" -ForegroundColor Cyan
Write-Host "  & `"$Python`" -m blue_engine.jobs.daily_portfolio --force"