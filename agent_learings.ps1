# File: agent_learings.ps1
# Purpose: Append agent learnings / improvement notes to AGENTS.md
# Usage:
#   .\agent_learings.ps1 -Note "Always run pytest before committing"
#   .\agent_learings.ps1 -Note "..." -File "C:\Users\KanagalakshmiM\my-trading-system\AGENTS.md"

param(
    [Parameter(Mandatory = $true)]
    [string]$Note,

    [string]$File = "C:\Users\KanagalakshmiM\my-trading-system\AGENTS.md",

    [string]$Section = "## Agent Learnings / Improvement Notes"
)

try {
    # Create file with header if it doesn't exist
    if (-not (Test-Path $File)) {
        "# AGENTS.md`n" | Out-File -FilePath $File -Encoding utf8
    }

    # Add section header if not already present
    $content = Get-Content $File -Raw
    if ($content -notmatch [regex]::Escape($Section)) {
        Add-Content -FilePath $File -Value "`n$Section" -Encoding utf8
    }

    # Append timestamped note
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm"
    Add-Content -FilePath $File -Value "- [$stamp] $Note" -Encoding utf8

    Write-Host "OK: note added to $File"
}
catch {
    Write-Host "ERROR: $($_.Exception.Message)"
    exit 1
}
