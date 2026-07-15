$ErrorActionPreference = "Stop"

$makeScript = Join-Path $PSScriptRoot "..\..\scripts\make.ps1"

function Write-Host {
    param(
        [Parameter(ValueFromRemainingArguments = $true)]
        [object[]]$Objects
    )
}

. $makeScript -Action help

$pythonScriptPath = Join-Path ([System.IO.Path]::GetTempPath()) ("qmt-proxy-utf8-" + [guid]::NewGuid().ToString("N") + ".py")
Set-Content -Path $pythonScriptPath -Value 'import sys; sys.stdout.buffer.write("中文\n".encode("utf-8"))' -Encoding utf8

$job = Start-Job -InitializationScript (Get-Utf8InitializationScript) -ArgumentList @("python", $pythonScriptPath) -ScriptBlock {
    param($PythonExe, $PythonScriptPath)

    & $PythonExe $PythonScriptPath 2>&1 |
        ForEach-Object { [string]$_ }
}

try {
    Wait-Job -Job $job -Timeout 15 | Out-Null
    $output = @(
        Receive-Job -Job $job -ErrorAction Stop |
            Where-Object { $null -ne $_ } |
            ForEach-Object { [string]$_ }
    )
} finally {
    Remove-Job -Job $job -Force -ErrorAction SilentlyContinue
    Remove-Item -Path $pythonScriptPath -Force -ErrorAction SilentlyContinue
}

if ($output.Count -eq 0) {
    throw "Expected the UTF-8 initialized job to emit output, but it produced nothing."
}

if ($output[0] -ne "中文") {
    throw "Expected UTF-8 initialized job output to preserve Chinese text, but got: $($output[0])"
}

Write-Output "make dev encoding test passed."
