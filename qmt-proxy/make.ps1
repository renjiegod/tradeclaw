param(
    [string]$Action = "help"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$scriptPath = Join-Path $PSScriptRoot "scripts\make.ps1"
& $scriptPath -Action $Action
exit $LASTEXITCODE
