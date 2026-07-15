# Make Lifecycle Hardening Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make `scripts/make.ps1` reliably detect and clean stale project service processes before start/stop operations so orphaned foreground or background instances do not keep `8000` or `50051` occupied.

**Architecture:** Extract process-discovery and cleanup logic into a small helper script that can be dot-sourced by both tests and `scripts/make.ps1`. Keep PID-file behavior for managed background services, then add project-signature-based residual process detection plus conservative port conflict reporting as a fallback.

**Tech Stack:** PowerShell, Windows process inspection cmdlets, `unittest`-style PowerShell assertions via a standalone test script

---

### Task 1: Add a failing test for residual process classification

**Files:**
- Create: `tests/unit/test_make_helpers.ps1`
- Create: `scripts/make.helpers.ps1`
- Test: `tests/unit/test_make_helpers.ps1`

**Step 1: Write the failing test**

```powershell
. "$PSScriptRoot\..\..\scripts\make.helpers.ps1"

$projectRoot = "C:\repo\qmt-proxy"
$process = [pscustomobject]@{
    ProcessId = 100
    ParentProcessId = 50
    Name = "python.exe"
    CommandLine = "`"$projectRoot\.venv\Scripts\python.exe`" run.py"
}

$result = Test-ProjectServiceProcess -ProcessInfo $process -ProjectRoot $projectRoot
if (-not $result) { throw "Expected run.py process to be recognized as project service process" }
```

**Step 2: Run test to verify it fails**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File tests\unit\test_make_helpers.ps1`
Expected: FAIL because `scripts/make.helpers.ps1` or `Test-ProjectServiceProcess` does not exist yet.

**Step 3: Write minimal implementation**

```powershell
function Test-ProjectServiceProcess {
    param($ProcessInfo, [string]$ProjectRoot)
    return $ProcessInfo.CommandLine -like "*run.py*"
}
```

**Step 4: Run test to verify it passes**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File tests\unit\test_make_helpers.ps1`
Expected: PASS

### Task 2: Cover process tree collection and conservative cleanup filtering

**Files:**
- Modify: `tests/unit/test_make_helpers.ps1`
- Modify: `scripts/make.helpers.ps1`

**Step 1: Add a failing test for project residual process collection**

```powershell
$allProcesses = @(
    [pscustomobject]@{ ProcessId = 1; ParentProcessId = 0; Name = "python.exe"; CommandLine = "`"$projectRoot\.venv\Scripts\python.exe`" run.py" }
    [pscustomobject]@{ ProcessId = 2; ParentProcessId = 1; Name = "python.exe"; CommandLine = "`"C:\Python314\python.exe`" -c `"from multiprocessing.spawn import spawn_main(...)`"" }
    [pscustomobject]@{ ProcessId = 3; ParentProcessId = 0; Name = "python.exe"; CommandLine = "`"C:\other\python.exe`" other.py" }
)
```

Assert that a helper like `Get-ProjectServiceProcessIds` returns `1,2` and excludes `3`.

**Step 2: Run test to verify it fails**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File tests\unit\test_make_helpers.ps1`
Expected: FAIL because the collection helper is not implemented yet.

**Step 3: Write minimal implementation**

Implement tree-aware helpers that:
- identify root service launcher processes by project command line
- include child processes whose parent is already classified
- avoid matching unrelated Python processes

**Step 4: Run test to verify it passes**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File tests\unit\test_make_helpers.ps1`
Expected: PASS

### Task 3: Integrate helpers into `make.ps1`

**Files:**
- Modify: `scripts/make.ps1`
- Modify: `scripts/make.helpers.ps1`
- Test: `tests/unit/test_make_helpers.ps1`

**Step 1: Dot-source helper module**

Load the helper script near the top of `scripts/make.ps1`.

**Step 2: Harden `start` and `start-bg`**

Before starting:
- check PID-file-managed service
- detect residual project processes
- stop project residuals if safe
- re-check ports `8000` and `50051`
- emit clear messages if a non-project process still owns a required port

**Step 3: Harden `stop`, `force-stop`, and `status`**

Add fallback behavior:
- if PID file is stale, inspect project residual processes
- stop them with `taskkill /T`
- report both PID-file and discovered process state

**Step 4: Run targeted verification**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File tests\unit\test_make_helpers.ps1`
Expected: PASS

### Task 4: Verify lifecycle behavior against the real script

**Files:**
- Modify: `scripts/make.ps1`
- Test: `scripts/make.ps1`

**Step 1: Run status verification**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\make.ps1 -Action status -PythonExe "python" -PythonVersion "3.12" -AppMode "dev"`
Expected: service state output is informative even without PID file.

**Step 2: Run cleanup verification**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\make.ps1 -Action stop`
Expected: no crash when only residual project processes exist, and cleanup message is explicit.

**Step 3: Run start verification**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\make.ps1 -Action start -PythonExe "python" -PythonVersion "3.12" -AppMode "dev"`
Expected: starts cleanly after helper-driven cleanup, with no duplicate-process bind conflict.
