$ErrorActionPreference = "Stop"

$helperPath = Join-Path $PSScriptRoot "..\..\scripts\make.helpers.ps1"
. $helperPath

$projectRoot = "C:\repo\qmt-proxy"
$process = [pscustomobject]@{
    ProcessId = 100
    ParentProcessId = 50
    Name = "python.exe"
    CommandLine = "`"$projectRoot\.venv\Scripts\python.exe`" run.py"
}

$result = Test-ProjectServiceProcess -ProcessInfo $process -ProjectRoot $projectRoot
if (-not $result) {
    throw "Expected run.py process to be recognized as project service process."
}

$allProcesses = @(
    [pscustomobject]@{
        ProcessId = 1
        ParentProcessId = 0
        Name = "python.exe"
        CommandLine = "`"$projectRoot\.venv\Scripts\python.exe`" run.py"
    },
    [pscustomobject]@{
        ProcessId = 2
        ParentProcessId = 1
        Name = "python.exe"
        CommandLine = "`"C:\Python314\python.exe`" -c `"from multiprocessing.spawn import spawn_main(parent_pid=1)`" --multiprocessing-fork"
    },
    [pscustomobject]@{
        ProcessId = 3
        ParentProcessId = 0
        Name = "python.exe"
        CommandLine = "`"C:\other\python.exe`" other.py"
    }
)

$processIds = Get-ProjectServiceProcessIds -Processes $allProcesses -ProjectRoot $projectRoot
if (@($processIds) -join "," -ne "1,2") {
    throw "Expected Get-ProjectServiceProcessIds to return 1,2 but got: $(@($processIds) -join ',')"
}

Write-Host "All make helper tests passed."
