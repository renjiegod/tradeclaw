param(
    [ValidateSet("help", "bootstrap-uv", "install", "sync", "lock", "start", "prod", "dev", "start-bg", "stop", "force-stop", "restart", "status", "logs", "clean", "ui-install", "ui-dev", "ui-build", "ui-preview", "ui-test")]
    [string]$Action = "help",
    [string]$PythonExe = "python",
    [string]$PythonVersion = "3.12",
    [string]$AppMode = "dev"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Set-ConsoleUtf8 {
    $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    [Console]::InputEncoding = $utf8NoBom
    [Console]::OutputEncoding = $utf8NoBom
    $OutputEncoding = $utf8NoBom
    try {
        & chcp 65001 *> $null
    } catch {
    }
}

function Get-Utf8InitializationScript {
    return {
        $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
        [Console]::InputEncoding = $utf8NoBom
        [Console]::OutputEncoding = $utf8NoBom
        $OutputEncoding = $utf8NoBom
        try {
            & chcp 65001 *> $null
        } catch {
        }
    }
}

Set-ConsoleUtf8

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$HelperScript = Join-Path $PSScriptRoot "make.helpers.ps1"
. $HelperScript
$RunDir = Join-Path $ProjectRoot ".run"
$PidFile = Join-Path $RunDir "service.pid"
$LogDir = Join-Path $ProjectRoot "logs"
$LogFile = Join-Path $LogDir "service.log"
$ErrLogFile = Join-Path $LogDir "service.err.log"
$RequiredPorts = @(8000, 50051)

function Invoke-CheckedCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed: $FilePath $($Arguments -join ' ')"
    }
}

function Test-PythonLauncher {
    try {
        & $PythonExe -c "import sys; sys.exit(0)" *> $null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Get-UvCommandSpec {
    $uvCommand = Get-Command "uv" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $uvCommand) {
        return [pscustomobject]@{
            FilePath = $uvCommand.Source
            Arguments = @()
        }
    }

    if (Test-PythonLauncher) {
        try {
            & $PythonExe -m uv --version *> $null
            if ($LASTEXITCODE -eq 0) {
                return [pscustomobject]@{
                    FilePath = $PythonExe
                    Arguments = @("-m", "uv")
                }
            }
        } catch {
        }
    }

    return $null
}

function Get-UvCommandPrefix {
    $commandSpec = Get-UvCommandSpec
    if ($null -eq $commandSpec) {
        throw "uv is not available. Run 'make bootstrap-uv' after installing a usable Python interpreter, or add uv.exe to PATH."
    }

    return @($commandSpec.FilePath) + $commandSpec.Arguments
}

function New-UvInvocation {
    param(
        [string[]]$Arguments = @()
    )

    $commandSpec = Get-UvCommandSpec
    if ($null -eq $commandSpec) {
        throw "uv is not available. Run 'make bootstrap-uv' after installing a usable Python interpreter, or add uv.exe to PATH."
    }

    $resolvedArguments = @()
    if ($commandSpec.Arguments.Count -gt 0) {
        $resolvedArguments += $commandSpec.Arguments
    }

    if ($Arguments.Count -gt 0) {
        $resolvedArguments += $Arguments
    }

    return [pscustomobject]@{
        FilePath = $commandSpec.FilePath
        Arguments = $resolvedArguments
    }
}

function Ensure-Uv {
    $uvCommand = Get-UvCommandSpec
    if ($null -ne $uvCommand) {
        Write-Host "uv is already installed."
        return
    }

    Write-Host "uv not found, installing via pip..."
    if (-not (Test-PythonLauncher)) {
        throw "Cannot install uv automatically because '$PythonExe' is not a usable Python interpreter on this machine. Install Python or rerun with -PythonExe set to a real python.exe."
    }

    Invoke-CheckedCommand -FilePath $PythonExe -Arguments @("-m", "pip", "install", "uv")

    $uvCommand = Get-UvCommandSpec
    if ($null -eq $uvCommand) {
        throw "uv installation completed but no usable uv launcher was found."
    }
}

function Use-ProjectVirtualEnv {
    $env:UV_PROJECT_ENVIRONMENT = Join-Path $ProjectRoot ".venv-windows"

    $sharedVenvPath = Join-Path $ProjectRoot ".venv"
    if (-not (Test-Path $sharedVenvPath)) {
        return
    }

    $reasons = [System.Collections.Generic.List[string]]::new()
    $windowsPython = Join-Path $sharedVenvPath "Scripts\\python.exe"
    $unixBinDir = Join-Path $sharedVenvPath "bin"
    $pyvenvCfg = Join-Path $sharedVenvPath "pyvenv.cfg"

    if (Test-Path $unixBinDir) {
        [void]$reasons.Add("contains a Unix-style 'bin' directory")
    }

    if (-not (Test-Path $windowsPython)) {
        [void]$reasons.Add("is missing 'Scripts\\python.exe'")
    }

    if (Test-Path $pyvenvCfg) {
        $pyvenvContents = Get-Content $pyvenvCfg -Raw
        if ($pyvenvContents -match "(?m)^home\s*=\s*/") {
            [void]$reasons.Add("was created from a non-Windows Python home")
        }
    }

    if ($reasons.Count -eq 0) {
        return
    }

    Write-Host "Detected an incompatible shared virtualenv at $sharedVenvPath."
    Write-Host "Reasons: $($reasons -join '; ')"
    Write-Host "Using Windows project virtualenv at $($env:UV_PROJECT_ENVIRONMENT) instead."
}

function Get-ServiceProcess {
    if (-not (Test-Path $PidFile)) {
        return $null
    }

    $servicePid = (Get-Content $PidFile | Select-Object -First 1).Trim()
    if (-not $servicePid) {
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
        return $null
    }

    $process = Get-Process -Id $servicePid -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
        return $null
    }

    return $process
}

function Quote-Single {
    param([string]$Value)
    return "'" + $Value.Replace("'", "''") + "'"
}

function Get-UvRunArguments {
    return @("run", "--python", $PythonVersion, "python", "run.py")
}

function Get-WebDirectory {
    return (Join-Path $ProjectRoot "web")
}

function New-CommandSpec {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [Parameter(Mandatory = $true)]
        [string]$WorkingDirectory
    )

    return [pscustomobject]@{
        FilePath = $FilePath
        Arguments = $Arguments
        WorkingDirectory = $WorkingDirectory
    }
}

function Get-UiCliPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$PackageRelativePath
    )

    return (Join-Path (Get-WebDirectory) $PackageRelativePath)
}

function Get-UiBindingPackagePath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Platform,
        [Parameter(Mandatory = $true)]
        [string]$Architecture
    )

    $packageName = switch ("$Platform/$Architecture") {
        "win32/arm64" { "@rolldown\binding-win32-arm64-msvc" }
        "win32/x64" { "@rolldown\binding-win32-x64-msvc" }
        "darwin/arm64" { "@rolldown\binding-darwin-arm64" }
        "darwin/x64" { "@rolldown\binding-darwin-x64" }
        "linux/arm64" { "@rolldown\binding-linux-arm64-gnu" }
        "linux/x64" { "@rolldown\binding-linux-x64-gnu" }
        default { $null }
    }

    if ($null -eq $packageName) {
        return $null
    }

    return (Join-Path (Get-WebDirectory) "node_modules\$packageName\package.json")
}

function Get-NodeRuntimeInfo {
    $platform = (& node -p "process.platform").Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to determine Node platform."
    }

    $architecture = (& node -p "process.arch").Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to determine Node architecture."
    }

    return [pscustomobject]@{
        Platform = $platform
        Architecture = $architecture
    }
}

function Get-UiPackageConfig {
    $packageJsonPath = Join-Path (Get-WebDirectory) "package.json"
    return (Get-Content $packageJsonPath -Raw | ConvertFrom-Json)
}

function Get-SemverMajor {
    param(
        [Parameter(Mandatory = $true)]
        [string]$VersionRange
    )

    if ($VersionRange -match "(\d+)") {
        return [int]$Matches[1]
    }

    throw "Unable to parse semantic version from '$VersionRange'."
}

function Test-UiNeedsRolldownBinding {
    $packageConfig = Get-UiPackageConfig
    $devDependencies = $packageConfig.devDependencies
    $viteMajor = Get-SemverMajor -VersionRange $devDependencies.vite
    $vitestMajor = Get-SemverMajor -VersionRange $devDependencies.vitest

    return ($viteMajor -ge 8) -or ($vitestMajor -ge 4)
}

function Get-UiDevCommand {
    return New-CommandSpec -FilePath "node" -Arguments @(
        (Get-UiCliPath -PackageRelativePath "node_modules\vite\bin\vite.js"),
        "--host",
        "0.0.0.0"
    ) -WorkingDirectory (Get-WebDirectory)
}

function Get-UiPreviewCommand {
    return New-CommandSpec -FilePath "node" -Arguments @(
        (Get-UiCliPath -PackageRelativePath "node_modules\vite\bin\vite.js"),
        "preview",
        "--host",
        "0.0.0.0"
    ) -WorkingDirectory (Get-WebDirectory)
}

function Get-UiTestCommand {
    return New-CommandSpec -FilePath "node" -Arguments @(
        (Get-UiCliPath -PackageRelativePath "node_modules\vitest\vitest.mjs"),
        "run"
    ) -WorkingDirectory (Get-WebDirectory)
}

function Invoke-CommandSpec {
    param(
        [Parameter(Mandatory = $true)]
        [pscustomobject]$CommandSpec
    )

    Push-Location $CommandSpec.WorkingDirectory
    try {
        Invoke-CheckedCommand -FilePath $CommandSpec.FilePath -Arguments $CommandSpec.Arguments
    } finally {
        Pop-Location
    }
}

function Quote-CmdArgument {
    param([string]$Value)

    if ([string]::IsNullOrEmpty($Value)) {
        return '""'
    }

    if ($Value -notmatch '[\s"&|<>^()]') {
        return $Value
    }

    return '"' + $Value.Replace('"', '""') + '"'
}

function Get-NpmCommandLine {
    param(
        [Parameter(Mandatory = $true)]
        [string]$WorkingDirectory,
        [Parameter(Mandatory = $true)]
        [string[]]$NpmArguments
    )

    $quotedArgs = @(
        $NpmArguments |
            ForEach-Object { Quote-CmdArgument -Value $_ }
    )

    return "pushd $(Quote-CmdArgument -Value $WorkingDirectory) && npm $($quotedArgs -join ' ')"
}

function Invoke-NpmCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$NpmArguments
    )

    $cmdCommand = Get-NpmCommandLine -WorkingDirectory (Get-WebDirectory) -NpmArguments $NpmArguments
    Invoke-CheckedCommand -FilePath "cmd.exe" -Arguments @("/d", "/c", $cmdCommand)
}

function Remove-DirectoryWithCmd {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    if (-not (Test-Path $Path)) {
        return
    }

    $cmdCommand = "if exist $(Quote-CmdArgument -Value $Path) rmdir /s /q $(Quote-CmdArgument -Value $Path)"
    try {
        & cmd.exe @("/d", "/c", $cmdCommand) 2>$null | Out-Null
    } catch {
    }
}

function Clear-UiBinArtifacts {
    $binDirectory = Join-Path (Get-WebDirectory) "node_modules\.bin"
    Remove-DirectoryWithCmd -Path $binDirectory
}

function Reset-UiNodeModules {
    Remove-DirectoryWithCmd -Path (Join-Path (Get-WebDirectory) "node_modules")
}

function Test-UiDependenciesReady {
    $requiredPaths = @(
        (Get-UiCliPath -PackageRelativePath "node_modules\vite\bin\vite.js"),
        (Get-UiCliPath -PackageRelativePath "node_modules\vitest\vitest.mjs"),
        (Get-UiCliPath -PackageRelativePath "node_modules\typescript\bin\tsc")
    )

    if (Test-UiNeedsRolldownBinding) {
        $runtimeInfo = Get-NodeRuntimeInfo
        $bindingPackagePath = Get-UiBindingPackagePath -Platform $runtimeInfo.Platform -Architecture $runtimeInfo.Architecture
        if ($null -ne $bindingPackagePath) {
            $requiredPaths += $bindingPackagePath
        }
    }

    foreach ($path in $requiredPaths) {
        if (-not (Test-Path $path)) {
            return $false
        }
    }

    return $true
}

function Invoke-UiInstall {
    try {
        Clear-UiBinArtifacts
        Invoke-NpmCommand -NpmArguments @("install", "--no-bin-links")
    } catch {
        Write-Host "Detected corrupted frontend install artifacts. Removing web/node_modules and retrying npm install..."
        Reset-UiNodeModules
        try {
            Invoke-NpmCommand -NpmArguments @("install", "--no-bin-links")
        } catch {
            throw "Frontend dependencies could not be repaired automatically. Remove 'web/node_modules' from the host filesystem, then rerun 'make ui-install'. Shared-folder link artifacts can be undeletable from Windows once they are corrupted."
        }
    }
}

function Ensure-UiDependencies {
    if (Test-UiDependenciesReady) {
        return
    }

    $runtimeInfo = Get-NodeRuntimeInfo
    Write-Host "Frontend dependencies are missing or incompatible for $($runtimeInfo.Platform)/$($runtimeInfo.Architecture)."
    Write-Host "Refreshing frontend dependencies with npm install..."
    Invoke-UiInstall

    if (-not (Test-UiDependenciesReady)) {
        throw "Frontend dependencies are still incomplete after npm install. Please remove 'web/node_modules' and rerun 'make ui-install'."
    }
}

function Invoke-UiBuild {
    Ensure-UiDependencies
    $webDirectory = Get-WebDirectory
    Push-Location $webDirectory
    try {
        Invoke-CheckedCommand -FilePath "node" -Arguments @(
            (Get-UiCliPath -PackageRelativePath "node_modules\typescript\bin\tsc"),
            "-b"
        )
        Invoke-CheckedCommand -FilePath "node" -Arguments @(
            (Get-UiCliPath -PackageRelativePath "node_modules\vite\bin\vite.js"),
            "build"
        )
    } finally {
        Pop-Location
    }
}

function Get-RequiredPortListeners {
    return @(
        Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
            Where-Object { $RequiredPorts -contains $_.LocalPort } |
            Sort-Object LocalPort, OwningProcess
    )
}

function Show-CleanupResults {
    param(
        [Parameter(Mandatory = $true)]
        [object[]]$Results
    )

    foreach ($result in $Results) {
        if ($result.ExitCode -eq 0) {
            Write-Host "Stopped stale project process tree rooted at PID $($result.ProcessId)."
        } else {
            Write-Host "Failed to stop stale project process tree rooted at PID $($result.ProcessId): $($result.Output)"
        }
    }
}

function Assert-NoExternalPortConflicts {
    for ($attempt = 0; $attempt -lt 2; $attempt++) {
        $listeners = @(Get-RequiredPortListeners)
        $projectProcessIds = @(
            Get-ProjectServiceProcesses -ProjectRoot $ProjectRoot |
                ForEach-Object { [int]$_.ProcessId }
        )

        if ($projectProcessIds.Count -eq 0) {
            $conflicts = $listeners
        } else {
            $conflicts = @(
                $listeners |
                    Where-Object { $projectProcessIds -notcontains [int]$_.OwningProcess }
            )
        }

        if ($conflicts.Count -eq 0) {
            return
        }

        if ($attempt -eq 0) {
            Start-Sleep -Seconds 2
            continue
        }

        $details = @(
            $conflicts |
                ForEach-Object { "$($_.LocalAddress):$($_.LocalPort) [PID=$($_.OwningProcess)]" }
        ) -join ", "
        throw "Required ports are already in use by another process: $details"
    }
}

function Prepare-ServiceStart {
    $staleProcesses = @(Get-ProjectServiceProcesses -ProjectRoot $ProjectRoot)
    if ($staleProcesses.Count -gt 0) {
        Write-Host "Found stale project service processes: $(@($staleProcesses | ForEach-Object { $_.ProcessId }) -join ', ')"
        $cleanupResults = @(Stop-ProjectServiceProcesses -ProjectRoot $ProjectRoot -Force)
        Show-CleanupResults -Results $cleanupResults
        Start-Sleep -Seconds 2
    }

    $remainingProjectListeners = @(Get-ProjectPortListeners -ProjectRoot $ProjectRoot -Ports $RequiredPorts)
    if ($remainingProjectListeners.Count -gt 0) {
        Write-Host "Project listeners remain after cleanup, waiting briefly before retrying port checks..."
        Start-Sleep -Seconds 2
    }

    Assert-NoExternalPortConflicts
}

function Start-ForegroundProcess {
    $backgroundProcess = Get-ServiceProcess
    if ($null -ne $backgroundProcess) {
        Write-Host "A background service is already running with PID $($backgroundProcess.Id)."
        Write-Host "Run 'make stop' first, or use 'make start-bg' only when you want background mode."
        return
    }

    Use-ProjectVirtualEnv
    Prepare-ServiceStart

    $env:APP_MODE = $AppMode
    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"
    Write-Host "Starting service in foreground with APP_MODE=$AppMode"
    $uvInvocation = New-UvInvocation -Arguments (Get-UvRunArguments)
    & $uvInvocation.FilePath @($uvInvocation.Arguments)
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed: $($uvInvocation.FilePath) $($uvInvocation.Arguments -join ' ')"
    }
}

function Receive-DevJobOutput {
    param(
        [Parameter(Mandatory = $true)]
        [System.Management.Automation.Job[]]$Jobs,
        [Parameter(Mandatory = $true)]
        [hashtable]$ExitCodes
    )

    foreach ($job in $Jobs) {
        $records = @(Receive-Job -Job $job -ErrorAction SilentlyContinue)
        foreach ($record in $records) {
            if ($null -eq $record) {
                continue
            }

            if ($record.PSObject.Properties.Name -contains "Kind" -and $record.Kind -eq "exit") {
                $ExitCodes[$record.Source] = [int]$record.ExitCode
                continue
            }

            Write-Host "[$($record.Source)] $($record.Message)"
        }
    }
}

function Start-CombinedForegroundDev {
    Use-ProjectVirtualEnv
    Prepare-ServiceStart
    Ensure-UiDependencies

    $uvInvocation = New-UvInvocation -Arguments (Get-UvRunArguments)
    $backendFilePath = $uvInvocation.FilePath
    $backendArgs = @($uvInvocation.Arguments)
    $frontendCommand = Get-UiDevCommand
    $jobInitializationScript = Get-Utf8InitializationScript

    $backendJob = Start-Job -Name "backend" -InitializationScript $jobInitializationScript -ArgumentList @($ProjectRoot, $backendFilePath, $AppMode, $backendArgs) -ScriptBlock {
        param($ProjectRoot, $BackendFilePath, $AppMode, $BackendArgs)

        Set-Location $ProjectRoot
        $env:APP_MODE = $AppMode
        $env:PYTHONUTF8 = "1"
        $env:PYTHONIOENCODING = "utf-8"

        try {
            & $BackendFilePath @BackendArgs 2>&1 |
                ForEach-Object {
                    [pscustomobject]@{
                        Kind = "line"
                        Source = "backend"
                        Message = [string]$_
                    }
                }
            $exitCode = if ($null -eq $LASTEXITCODE) { 0 } else { [int]$LASTEXITCODE }
        } catch {
            [pscustomobject]@{
                Kind = "line"
                Source = "backend"
                Message = $_.Exception.Message
            }
            $exitCode = 1
        }

        [pscustomobject]@{
            Kind = "exit"
            Source = "backend"
            ExitCode = $exitCode
        }
    }

    $frontendJob = Start-Job -Name "frontend" -InitializationScript $jobInitializationScript -ArgumentList @($frontendCommand.FilePath, $frontendCommand.Arguments, $frontendCommand.WorkingDirectory) -ScriptBlock {
        param($FrontendFilePath, $FrontendArguments, $FrontendWorkingDirectory)

        try {
            Set-Location $FrontendWorkingDirectory
            & $FrontendFilePath @FrontendArguments 2>&1 |
                ForEach-Object {
                    [pscustomobject]@{
                        Kind = "line"
                        Source = "frontend"
                        Message = [string]$_
                    }
                }
            $exitCode = if ($null -eq $LASTEXITCODE) { 0 } else { [int]$LASTEXITCODE }
        } catch {
            [pscustomobject]@{
                Kind = "line"
                Source = "frontend"
                Message = $_.Exception.Message
            }
            $exitCode = 1
        }

        [pscustomobject]@{
            Kind = "exit"
            Source = "frontend"
            ExitCode = $exitCode
        }
    }

    $jobs = @($backendJob, $frontendJob)
    $exitCodes = @{}

    Write-Host "Starting backend and frontend in foreground with APP_MODE=$AppMode"
    Write-Host "Press Ctrl+C to stop both processes."

    try {
        while ($true) {
            Receive-DevJobOutput -Jobs $jobs -ExitCodes $exitCodes

            $runningJobs = @($jobs | Where-Object { $_.State -eq "Running" })
            if ($runningJobs.Count -lt $jobs.Count) {
                break
            }

            Wait-Job -Job $jobs -Any -Timeout 1 | Out-Null
        }

        Receive-DevJobOutput -Jobs $jobs -ExitCodes $exitCodes

        $completedJobs = @($jobs | Where-Object { $_.State -ne "Running" })
        $remainingJobs = @($jobs | Where-Object { $_.State -eq "Running" })
        if ($completedJobs.Count -gt 0 -and $remainingJobs.Count -gt 0) {
            Write-Host "A dev process exited; stopping the remaining process."
        }

        if ($remainingJobs.Count -gt 0) {
            Stop-Job -Job $remainingJobs | Out-Null
            Wait-Job -Job $remainingJobs -Timeout 5 | Out-Null
            Receive-DevJobOutput -Jobs $jobs -ExitCodes $exitCodes
        }

        $failedProcesses = @(
            $exitCodes.GetEnumerator() |
                Where-Object { $_.Value -ne 0 } |
                ForEach-Object { "$($_.Key)=$($_.Value)" }
        )
        if ($failedProcesses.Count -gt 0) {
            throw "Combined dev startup exited with failures: $($failedProcesses -join ', ')"
        }
    } finally {
        $activeJobs = @($jobs | Where-Object { $_.State -eq "Running" })
        if ($activeJobs.Count -gt 0) {
            Stop-Job -Job $activeJobs | Out-Null
        }

        foreach ($job in $jobs) {
            Remove-Job -Job $job -Force -ErrorAction SilentlyContinue
        }
    }
}

function Start-BackgroundServiceProcess {
    $existingProcess = Get-ServiceProcess
    if ($null -ne $existingProcess) {
        Write-Host "Service is already running with PID $($existingProcess.Id)."
        return
    }

    Use-ProjectVirtualEnv
    Prepare-ServiceStart

    New-Item -ItemType Directory -Force -Path $RunDir | Out-Null
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    Remove-Item $LogFile -Force -ErrorAction SilentlyContinue
    Remove-Item $ErrLogFile -Force -ErrorAction SilentlyContinue

    $uvInvocation = New-UvInvocation -Arguments (Get-UvRunArguments)

    $env:APP_MODE = $AppMode
    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"

    $process = Start-Process `
        -FilePath $uvInvocation.FilePath `
        -ArgumentList $uvInvocation.Arguments `
        -WorkingDirectory $ProjectRoot `
        -RedirectStandardOutput $LogFile `
        -RedirectStandardError $ErrLogFile `
        -PassThru

    Set-Content -Path $PidFile -Value $process.Id

    Write-Host "Service started in background."
    Write-Host "PID: $($process.Id)"
    Write-Host "STDOUT: $LogFile"
    Write-Host "STDERR: $ErrLogFile"
}

function Stop-ServiceProcess {
    param([switch]$Force)

    $process = Get-ServiceProcess
    if ($null -eq $process) {
        $cleanupResults = @(Stop-ProjectServiceProcesses -ProjectRoot $ProjectRoot -Force:$Force)
        if ($cleanupResults.Count -eq 0) {
            Write-Host "Service is not running."
            return
        }

        Show-CleanupResults -Results $cleanupResults
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
        Write-Host "Removed stale project service processes."
        return
    }

    $taskkillArgs = @("/PID", "$($process.Id)", "/T")
    if ($Force) {
        $taskkillArgs = @("/F") + $taskkillArgs
    }

    $taskkillOutput = & taskkill @taskkillArgs 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to stop service: $taskkillOutput"
    }

    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue

    if ($Force) {
        Write-Host "Service force-stopped."
    } else {
        Write-Host "Service stopped."
    }
}

function Show-Status {
    $process = Get-ServiceProcess
    $projectProcesses = @(Get-ProjectServiceProcesses -ProjectRoot $ProjectRoot)
    $listeners = @(Get-RequiredPortListeners)

    if ($null -eq $process -and $projectProcesses.Count -eq 0 -and $listeners.Count -eq 0) {
        Write-Host "Service is not running."
        return
    }

    if ($null -ne $process) {
        Write-Host "Managed background service is running."
        Write-Host "PID: $($process.Id)"
        Write-Host "STDOUT: $LogFile"
        Write-Host "STDERR: $ErrLogFile"
    } else {
        Write-Host "No managed background PID file is active."
    }

    if ($projectProcesses.Count -gt 0) {
        Write-Host "Detected project service processes: $(@($projectProcesses | ForEach-Object { $_.ProcessId }) -join ', ')"
    }

    if ($listeners.Count -gt 0) {
        $listenerSummary = @(
            $listeners |
                ForEach-Object { "$($_.LocalAddress):$($_.LocalPort) [PID=$($_.OwningProcess)]" }
        ) -join ", "
        Write-Host "Listening ports: $listenerSummary"
    }
}

function Show-Logs {
    Write-Host $LogFile
    Write-Host $ErrLogFile
}

function Clean-State {
    if (Test-Path $PidFile) {
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    }

    if ((Test-Path $RunDir) -and -not (Get-ChildItem $RunDir -Force | Select-Object -First 1)) {
        Remove-Item $RunDir -Force -ErrorAction SilentlyContinue
    }

    Write-Host "Runtime state cleaned."
}

function Show-Help {
    Write-Host "Available targets:"
    Write-Host "  make install      Install uv, Python $PythonVersion, and project dependencies"
    Write-Host "  make sync         Sync project dependencies with uv"
    Write-Host "  make lock         Refresh uv.lock using Python $PythonVersion"
    Write-Host "  make start        Start the service in foreground with APP_MODE=$AppMode"
    Write-Host "  make prod         Start the service in foreground with APP_MODE=prod"
    Write-Host "  make dev          Start backend and frontend together for local development"
    Write-Host "  make start-bg     Start the service in background with APP_MODE=$AppMode"
    Write-Host "  make stop         Stop the background service"
    Write-Host "  make force-stop   Force stop the background service"
    Write-Host "  make restart      Restart the background service"
    Write-Host "  make status       Show background service status"
    Write-Host "  make logs         Print the background service log paths"
    Write-Host "  make clean        Remove runtime state"
    Write-Host "  make ui-install   Install frontend dependencies"
    Write-Host "  make ui-dev       Start the frontend development server"
    Write-Host "  make ui-build     Build the frontend assets"
    Write-Host "  make ui-preview   Preview the built frontend assets"
    Write-Host "  make ui-test      Run the frontend tests"
}

Push-Location $ProjectRoot
try {
    switch ($Action) {
        "help" { Show-Help }
        "bootstrap-uv" { Ensure-Uv }
        "install" {
            Ensure-Uv
            $uvInvocation = New-UvInvocation -Arguments @("python", "install", $PythonVersion)
            Invoke-CheckedCommand -FilePath $uvInvocation.FilePath -Arguments $uvInvocation.Arguments
            Use-ProjectVirtualEnv
            $uvInvocation = New-UvInvocation -Arguments @("sync", "--no-install-project", "--python", $PythonVersion)
            Invoke-CheckedCommand -FilePath $uvInvocation.FilePath -Arguments $uvInvocation.Arguments
        }
        "sync" {
            Ensure-Uv
            Use-ProjectVirtualEnv
            $uvInvocation = New-UvInvocation -Arguments @("sync", "--no-install-project", "--python", $PythonVersion)
            Invoke-CheckedCommand -FilePath $uvInvocation.FilePath -Arguments $uvInvocation.Arguments
        }
        "lock" {
            Ensure-Uv
            $uvInvocation = New-UvInvocation -Arguments @("lock", "--python", $PythonVersion)
            Invoke-CheckedCommand -FilePath $uvInvocation.FilePath -Arguments $uvInvocation.Arguments
        }
        "start" {
            Ensure-Uv
            Start-ForegroundProcess
        }
        "prod" {
            Ensure-Uv
            $AppMode = "prod"
            Start-ForegroundProcess
        }
        "dev" {
            Ensure-Uv
            Start-CombinedForegroundDev
        }
        "ui-install" { Invoke-UiInstall }
        "ui-dev" {
            Ensure-UiDependencies
            Invoke-CommandSpec -CommandSpec (Get-UiDevCommand)
        }
        "ui-build" { Invoke-UiBuild }
        "ui-preview" {
            Ensure-UiDependencies
            Invoke-CommandSpec -CommandSpec (Get-UiPreviewCommand)
        }
        "ui-test" {
            Ensure-UiDependencies
            Invoke-CommandSpec -CommandSpec (Get-UiTestCommand)
        }
        "start-bg" {
            Ensure-Uv
            Start-BackgroundServiceProcess
        }
        "stop" { Stop-ServiceProcess }
        "force-stop" { Stop-ServiceProcess -Force }
        "restart" {
            Stop-ServiceProcess
            Ensure-Uv
            Start-BackgroundServiceProcess
        }
        "status" { Show-Status }
        "logs" { Show-Logs }
        "clean" { Clean-State }
        default { throw "Unsupported action: $Action" }
    }
} finally {
    Pop-Location
}
