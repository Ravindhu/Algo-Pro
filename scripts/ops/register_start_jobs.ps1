# register_start_jobs.ps1 - schedule the start-only run of install.bat.
#
# Creates two Task Scheduler jobs for the current user (no admin needed):
#   1. "MTS - start at logon"  : runs when you sign in to Windows
#   2. "MTS - pre-open start"  : Mon-Fri at 08:40 local time; wakes the PC if asleep
#
# Both run   install.bat --scheduled   = start only. No code update, no OpenAlgo
# upgrade, and the trading engine is never started by these jobs.
#
#   powershell -ExecutionPolicy Bypass -File scripts\ops\register_start_jobs.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\ops\register_start_jobs.ps1 -PreOpenTime 08:35
#   powershell -ExecutionPolicy Bypass -File scripts\ops\register_start_jobs.ps1 -Remove
#
# Output of each run goes to Temp\run\scheduled_start.log.
param(
    [switch]$Remove,
    [string]$PreOpenTime = "08:40"
)

$ErrorActionPreference = "Stop"
$root  = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$bat   = Join-Path $root "install.bat"
$log   = Join-Path $root "Temp\run\scheduled_start.log"
$names = @("MTS - start at logon", "MTS - pre-open start")

foreach ($n in $names) {
    if (Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $n -Confirm:$false
        Write-Host "removed: $n"
    }
}
if ($Remove) { exit 0 }

if (-not (Test-Path $bat)) { Write-Error "install.bat not found at $bat"; exit 1 }
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null

$args_ = '/c ""{0}" --scheduled >> "{1}" 2>&1"' -f $bat, $log
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $args_ -WorkingDirectory $root

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

$user  = "$env:USERDOMAIN\$env:USERNAME"
$logon = New-ScheduledTaskTrigger -AtLogOn -User $user
Register-ScheduledTask -TaskName $names[0] -Action $action -Trigger $logon -Settings $settings `
    -Description "My Trading System: start the stack at sign-in (no update, no engine)" -Force | Out-Null

$days    = [System.DayOfWeek[]]@("Monday","Tuesday","Wednesday","Thursday","Friday")
$preopen = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At $PreOpenTime
Register-ScheduledTask -TaskName $names[1] -Action $action -Trigger $preopen -Settings $settings `
    -Description "My Trading System: start the stack before NSE open (no update, no engine)" -Force | Out-Null

Get-ScheduledTask -TaskName $names | Format-Table TaskName, State -AutoSize
Write-Host "Log file: $log"
