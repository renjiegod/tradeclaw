$ErrorActionPreference = "Stop"

$makeScript = Join-Path $PSScriptRoot "..\..\scripts\make.ps1"

function Write-Host {
    param(
        [Parameter(ValueFromRemainingArguments = $true)]
        [object[]]$Objects
    )
}

. $makeScript -Action help

$args = @(Get-UvRunArguments)
if ($args -contains "--no-project") {
    throw "Expected Get-UvRunArguments to use the project environment, but found --no-project."
}

if ($args -notcontains "run.py") {
    throw "Expected Get-UvRunArguments to launch run.py."
}

$uiInstallCommandLine = Get-NpmCommandLine -WorkingDirectory "\\Mac\Home\code\qmt-proxy\web" -NpmArguments @("install")
if ($uiInstallCommandLine -ne 'pushd \\Mac\Home\code\qmt-proxy\web && npm install') {
    throw "Expected Get-NpmCommandLine to enter the web directory with pushd before running npm install, but got: $uiInstallCommandLine"
}

$uiDevCommand = Get-UiDevCommand
if ($uiDevCommand.FilePath -ne "node") {
    throw "Expected Get-UiDevCommand to use node, but got: $($uiDevCommand.FilePath)"
}

if ($uiDevCommand.WorkingDirectory -notlike "*\web") {
    throw "Expected Get-UiDevCommand to run inside the web directory, but got: $($uiDevCommand.WorkingDirectory)"
}

if (@($uiDevCommand.Arguments)[1..2] -join "," -ne "--host,0.0.0.0") {
    throw "Expected Get-UiDevCommand to launch the Vite CLI entrypoint directly, but got: $(@($uiDevCommand.Arguments) -join ',')"
}

$uiBindingPackagePath = Get-UiBindingPackagePath -Platform "win32" -Architecture "arm64"
if ($uiBindingPackagePath -notlike "*@rolldown\binding-win32-arm64-msvc\package.json") {
    throw "Expected Get-UiBindingPackagePath to target the current platform binding package, but got: $uiBindingPackagePath"
}

if (Test-UiNeedsRolldownBinding) {
    throw "Expected the downgraded web toolchain to avoid rolldown binding checks."
}

Write-Output "make start args test passed."
