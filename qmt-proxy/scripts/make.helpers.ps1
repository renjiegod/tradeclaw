function Test-ProjectServiceProcess {
    param(
        [Parameter(Mandatory = $true)]
        $ProcessInfo,
        [Parameter(Mandatory = $true)]
        [string]$ProjectRoot
    )

    $commandLine = [string]$ProcessInfo.CommandLine
    if (-not $commandLine) {
        return $false
    }

    return $commandLine -like "*$ProjectRoot*" -and $commandLine -like "*run.py*"
}

function Get-ProjectServiceProcessIds {
    param(
        [Parameter(Mandatory = $true)]
        [object[]]$Processes,
        [Parameter(Mandatory = $true)]
        [string]$ProjectRoot
    )

    $matchedIds = [System.Collections.Generic.HashSet[int]]::new()
    foreach ($processInfo in $Processes) {
        if (Test-ProjectServiceProcess -ProcessInfo $processInfo -ProjectRoot $ProjectRoot) {
            [void]$matchedIds.Add([int]$processInfo.ProcessId)
        }
    }

    $changed = $true
    while ($changed) {
        $changed = $false
        foreach ($processInfo in $Processes) {
            $processId = [int]$processInfo.ProcessId
            $parentProcessId = [int]$processInfo.ParentProcessId
            if ($matchedIds.Contains($processId)) {
                continue
            }

            if ($matchedIds.Contains($parentProcessId)) {
                [void]$matchedIds.Add($processId)
                $changed = $true
            }
        }
    }

    return @([int[]]$matchedIds | Sort-Object)
}

function Get-ProjectServiceProcesses {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ProjectRoot
    )

    $allProcesses = @(Get-CimInstance Win32_Process)
    $matchedIds = @(Get-ProjectServiceProcessIds -Processes $allProcesses -ProjectRoot $ProjectRoot)
    if ($matchedIds.Count -eq 0) {
        return @()
    }

    return @(
        $allProcesses |
            Where-Object { $matchedIds -contains [int]$_.ProcessId } |
            Sort-Object ProcessId
    )
}

function Get-ProjectServiceRootProcessIds {
    param(
        [Parameter(Mandatory = $true)]
        [object[]]$Processes,
        [Parameter(Mandatory = $true)]
        [int[]]$MatchedIds
    )

    if ($MatchedIds.Count -eq 0) {
        return @()
    }

    $matchedIdSet = [System.Collections.Generic.HashSet[int]]::new()
    foreach ($matchedId in $MatchedIds) {
        [void]$matchedIdSet.Add([int]$matchedId)
    }

    return @(
        $Processes |
            Where-Object {
                $processId = [int]$_.ProcessId
                $parentProcessId = [int]$_.ParentProcessId
                $matchedIdSet.Contains($processId) -and -not $matchedIdSet.Contains($parentProcessId)
            } |
            ForEach-Object { [int]$_.ProcessId } |
            Sort-Object
    )
}

function Stop-ProjectServiceProcesses {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ProjectRoot,
        [switch]$Force
    )

    $allProcesses = @(Get-CimInstance Win32_Process)
    $matchedIds = @(Get-ProjectServiceProcessIds -Processes $allProcesses -ProjectRoot $ProjectRoot)
    if ($matchedIds.Count -eq 0) {
        return @()
    }

    $rootIds = @(Get-ProjectServiceRootProcessIds -Processes $allProcesses -MatchedIds $matchedIds)
    if ($rootIds.Count -eq 0) {
        $rootIds = $matchedIds
    }

    $results = @()
    foreach ($rootId in $rootIds) {
        $taskkillArgs = @("/PID", "$rootId", "/T")
        if ($Force) {
            $taskkillArgs = @("/F") + $taskkillArgs
        }

        $taskkillOutput = & taskkill @taskkillArgs 2>&1
        $results += [pscustomobject]@{
            ProcessId = $rootId
            ExitCode = $LASTEXITCODE
            Output = ($taskkillOutput | Out-String).Trim()
        }
    }

    return $results
}

function Get-ProjectPortListeners {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ProjectRoot,
        [Parameter(Mandatory = $true)]
        [int[]]$Ports
    )

    $matchedIds = @(Get-ProjectServiceProcessIds -Processes @(Get-CimInstance Win32_Process) -ProjectRoot $ProjectRoot)
    if ($matchedIds.Count -eq 0) {
        return @()
    }

    return @(
        Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
            Where-Object { $Ports -contains $_.LocalPort -and $matchedIds -contains [int]$_.OwningProcess } |
            Sort-Object LocalPort, OwningProcess
    )
}
