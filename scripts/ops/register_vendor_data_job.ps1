# register_vendor_data_job.ps1 - keep the pinned vendor index data fresh.
#
# RUNS AS: the current user (no elevation needed for a non-system task).
#
#   powershell -ExecutionPolicy Bypass -File scripts\ops\register_vendor_data_job.ps1
#
# Removes with:
#   powershell -ExecutionPolicy Bypass -File scripts\ops\register_vendor_data_job.ps1 -Remove
#
# WHY 08:45 IST
# -------------
# NSE closes at 15:30 and Yahoo publishes the settled daily bar for the Indian
# indices in the evening (IST) - the same reason register_daily_jobs.ps1 uses
# 15:40 for the broker's end-of-day bar. The vendor job is scheduled in the
# PRE-MARKET window (08:45) instead, so it refreshes the previous session's
# close before the 09:15 open and never competes with the portfolio job for
# Yahoo's rate limits or local disk.
#
# WHY "At logon" AND "StartWhenAvailable" ARE BOTH SET
# ---------------------------------------------------
# If the machine was asleep or off at 08:45 the trigger is skipped. Logon and
# StartWhenAvailable catch that case; the update itself is idempotent (rows are
# merged on the date index), so a duplicate run costs a few seconds and changes
# nothing.
#
# WHAT "SUCCESS" MEANS
# --------------------
# The job exits non-zero when any index fails or when the newest session is
# older than --max-age-days. That is deliberate: a silent no-op (network
# failure, upstream change, market holiday run on a stale feed) must surface in
# Task Scheduler's Last Run Result instead of being mistaken for fresh data.

param(
    [switch]$Remove,
    [string]$TaskName = "BlueEngine_VendorData",
    [string]$Time = "08:45",
    [int]$MaxAgeDays = 5
)

$ErrorActionPreference = "Stop"

# Resolve repo root from this script's location (scripts\ops\ -> repo root).
$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
# -m blue_engine.jobs.vendor_maintenance must resolve, so run from Blue_engine/.
$WorkDir = Join-Path $RepoRoot "Blue_engine"
$LogDir = Join-Path $RepoRoot "data\logs"
$LogFile = Join-Path $LogDir "vendor_maintenance.log"

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
    throw "Blue_engine not found at $WorkDir. Run this from the repo's scripts\ops\ folder."
}
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# Redirect output to a durable log: Task Scheduler keeps stdout in memory only,
# so an invisible failure would otherwise leave no trace after the run.
$cliArgs = "-m blue_engine.jobs.vendor_maintenance --max-age-days $MaxAgeDays"
$psArgs = "-NoProfile -ExecutionPolicy Bypass -Command " +
          "& '$Python' $cliArgs *>> '$LogFile'; exit `$LASTEXITCODE"

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument $psArgs `
    -WorkingDirectory $WorkDir

# Pre-market daily trigger, then catch-ups for a machine that was off.
$triggerDaily = New-ScheduledTaskTrigger -Daily -At $Time
$triggerLogon = New-ScheduledTaskTrigger -AtLogOn

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew

# Least privilege: the job only writes the vendor data\ dir and a log file.
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
    -Description "Refresh, scrub and verify the pinned vendor index parquets (Blue_engine/jobs/vendor_maintenance.py). Exits non-zero when an index fails or data is stale." | Out-Null

Write-Host "Registered '$TaskName' (daily $Time + at logon)." -ForegroundColor Green
Write-Host ""
Write-Host "Verify with:" -ForegroundColor Cyan
Write-Host "  Get-ScheduledTask -TaskName $TaskName | Format-List"
Write-Host "Run it now:" -ForegroundColor Cyan
Write-Host "  & `"$Python`" -m blue_engine.jobs.vendor_maintenance"
Write-Host "Log file:" -ForegroundColor Cyan
Write-Host "  $LogFile"
