# upgrade_openalgo.ps1 - compatibility shim (scripts/ was grouped 2026-10-21).
#
# The implementation now lives in scripts\ops\upgrade_openalgo.ps1. This file
# stays so documented commands keep working unchanged. Arguments are passed
# through verbatim; the working directory and exit code are preserved.
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $Rest
)

$target = Join-Path $PSScriptRoot 'ops\upgrade_openalgo.ps1'
if (-not (Test-Path $target)) {
    Write-Error "upgrade_openalgo.ps1: implementation not found at $target"
    exit 1
}
& $target @Rest
exit $LASTEXITCODE
