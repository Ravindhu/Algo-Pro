param(
    [switch]$Remove,
    [string]$TaskName = "BlueEngine_DatabaseSampleUpdate",
    [string]$Time = "18:00"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$WorkDir = Join-Path $RepoRoot "Blue_engine"

if ($Remove) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    } else {
        Write-Host "Scheduled task '$TaskName' is not registered."
    }
    exit 0
}

if (-not (Test-Path $Python)) {
    throw "Python not found at $Python. Create the repository venv first."
}

$Action = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument "-m blue_engine.database.sample_data" `
    -WorkingDirectory $WorkDir
$Action1m = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument "-m blue_engine.database.sample_data_1min" `
    -WorkingDirectory $WorkDir
$Trigger = New-ScheduledTaskTrigger -Daily -At $Time
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew
$Principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

$Existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($Existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action, $Action1m `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Refresh local, non-commercial Indian market research sample data; see Blue_engine/database/README.md." |
    Out-Null

Write-Host "Registered '$TaskName' to run daily at $Time."
Write-Host "Run immediately with: Start-ScheduledTask -TaskName '$TaskName'"
