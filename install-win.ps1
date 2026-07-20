#Requires -Version 5.1
<#
.SYNOPSIS
    Windows PowerShell 5.1 -File entrypoint for DoYouTrade install.

.DESCRIPTION
    install.ps1 is UTF-8 without BOM so `irm ... | iex` keeps working.
    Windows PowerShell 5.1 -File reads BOM-less scripts as system ANSI
    (CP936 on Chinese Windows), which corrupts Chinese text and raises
    ParserError -- the GUI installer and the double-click install bat
    both use -File.

    This wrapper is pure ASCII. It copies install.ps1 to a UTF-8-BOM temp
    file and re-invokes powershell -File so the real installer parses.

.PARAMETER Source
    Forwarded to install.ps1 when set. Leave empty so install.ps1 can
    auto-resolve GitHub vs Gitee via DOYOUTRADE_MIRROR / network probe.

.PARAMETER Force
    Forwarded to install.ps1.
#>
[CmdletBinding()]
param(
    [string]$Source = $env:DOYOUTRADE_INSTALL_SOURCE,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$scriptPath = Join-Path $PSScriptRoot "install.ps1"
if (-not (Test-Path -LiteralPath $scriptPath)) {
    Write-Host "[x] install.ps1 not found next to install-win.ps1: $scriptPath" -ForegroundColor Red
    exit 1
}

$raw = [System.IO.File]::ReadAllBytes($scriptPath)
if ($raw.Length -ge 3 -and $raw[0] -eq 0xEF -and $raw[1] -eq 0xBB -and $raw[2] -eq 0xBF) {
    $text = [System.Text.Encoding]::UTF8.GetString($raw, 3, $raw.Length - 3)
} else {
    $text = [System.Text.Encoding]::UTF8.GetString($raw)
}

$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("doyoutrade-install-" + [guid]::NewGuid().ToString("N") + ".ps1")
$utf8Bom = New-Object System.Text.UTF8Encoding $true
[System.IO.File]::WriteAllText($tmp, $text, $utf8Bom)

try {
    $powershell = Join-Path $PSHOME "powershell.exe"
    if (-not (Test-Path -LiteralPath $powershell)) {
        $powershell = "powershell.exe"
    }
    $argList = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", $tmp
    )
    if (-not [string]::IsNullOrWhiteSpace($Source)) {
        $argList += @("-Source", $Source)
    }
    if ($Force) {
        $argList += "-Force"
    }
    $proc = Start-Process -FilePath $powershell -ArgumentList $argList -Wait -PassThru -NoNewWindow
    exit $proc.ExitCode
} finally {
    Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
}
