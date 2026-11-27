# register_backup_job.ps1 - schedule the daily state backup (scripts\ops\backup_state.py).
#
# Creates one Task Scheduler job for the current user (no admin needed):
#   "MTS - daily backup" : every day at 15:45 local time, runs backup_state.py
#
#   powershell -ExecutionPolicy Bypass -File scripts\ops\register_backup_job.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\ops\register_backup_job.ps1 -Time 15:45 -Dest E:\Algopro-restore
#   powershell -ExecutionPolicy Bypass -File scripts\ops\register_backup_job.ps1 -Remove
#
# Output goes to Temp\run\backup.log.
param(
    [switch]$Remove,
    [string]$Time = "15:45",
    [string]$Dest = "E:\Algopro-restore"
)

$ErrorActionPreference = "Stop"
$root = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$py   = Join-Path $root ".venv\Scripts\python.exe"
$name = "MTS - daily backup"

if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
    Write-Host "removed: $name"
}
if ($Remove) { exit 0 }

if (-not (Test-Path $py)) { Write-Error "venv not found at $py - run install.bat first"; exit 1 }
$log = Join-Path $root "Temp\run\backup.log"
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null

$args_ = '/c ""{0}" "{1}\scripts\ops\backup_state.py" --dest "{2}" >> "{3}" 2>&1"' -f $py, $root, $Dest, $log
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $args_ -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
    -Description "My Trading System: daily backup of _restored, data and the live database" -Force | Out-Null

Get-ScheduledTask -TaskName $name | Format-Table TaskName, State -AutoSize
Write-Host "Backups to: $Dest  (keeps the last 30)   Log: $log"
