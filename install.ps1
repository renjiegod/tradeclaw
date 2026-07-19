#Requires -Version 5.1
<#
.SYNOPSIS
    DoYouTrade 一键安装脚本 (Windows / PowerShell)。

.DESCRIPTION
    只负责「安装」，不启动服务、不碰任何交易账户：
      1. 检测 / 安装 uv（Astral 的 Python 包管理器，自带 Python 3.12）
      2. 提示是否有 Node.js（有则安装时自动打包网页控制台，没有则退化为 API + CLI）
      3. uv tool install 把 doyoutrade[qmt-proxy] 装成常驻命令（内置 qmt-proxy，
         `doyoutrade` 启动、`uv tool upgrade` 升级）
      4. 打印下一步：运行 `doyoutrade`，首启进入安装向导配置模型

    Windows 版一并安装内置的 qmt-proxy（把 xtquant 封装为本机 REST 服务）。运行
    `doyoutrade` 默认走 --mode both：同一进程内同时起 DoYouTrade(:8000) 与 qmt-proxy(:8001)，
    并自动把默认账户指向本机 qmt-proxy。只要已登录券商 miniQMT，实时行情开箱即用，
    无需手工配置 base_url。

    可重复运行：若已安装，默认会询问是否卸载后重装；加 -Force 跳过询问。

.PARAMETER Source
    安装源。默认从 GitHub 主分支安装；可指定本地目录 / fork 做测试。

.PARAMETER Force
    若已安装，直接卸载并重新安装，不弹出确认。

.EXAMPLE
    用法一（最省事，在 PowerShell 里）：
      irm https://raw.githubusercontent.com/renjiegod/doyoutrade/main/install.ps1 | iex

.EXAMPLE
    用法二（先审阅再执行，推荐谨慎用户；Windows 请走 install-win.ps1）：
      irm https://raw.githubusercontent.com/renjiegod/doyoutrade/main/install.ps1 -OutFile install.ps1
      irm https://raw.githubusercontent.com/renjiegod/doyoutrade/main/install-win.ps1 -OutFile install-win.ps1
      notepad install.ps1     # 看清楚它做了什么
      powershell -NoProfile -ExecutionPolicy Bypass -File install-win.ps1

.EXAMPLE
    非交互 / CI 场景强制覆盖重装：
      powershell -NoProfile -ExecutionPolicy Bypass -File install-win.ps1 -Force
#>
[CmdletBinding()]
param(
    [string]$Source = $(if ($env:DOYOUTRADE_INSTALL_SOURCE) { $env:DOYOUTRADE_INSTALL_SOURCE } else { "git+https://github.com/renjiegod/doyoutrade.git" }),
    [switch]$Force
)

$ErrorActionPreference = "Stop"

function Write-Info { param($m) Write-Host "==> $m" -ForegroundColor Cyan }
function Write-Ok   { param($m) Write-Host "[OK] $m" -ForegroundColor Green }
function Write-Warn { param($m) Write-Host "[!] $m" -ForegroundColor Yellow }

# Pin the interpreter. uv defaults to the newest release satisfying
# requires-python (>=3.12); left alone it grabs 3.14, which (a) has no xtquant
# wheel and (b) is what got downloaded into %APPDATA%\uv and tripped the
# untrusted-mount (os error 448) failure. 3.12 is the stable floor we build on.
$script:DoyoutradePythonVersion = "3.12"

# Filled in by Set-UvRuntimeEnv for the diagnostics block.
$script:UvHomeInfo = $null

function Test-PathBehindReparsePoint {
    # True if $Path or any existing ancestor is a reparse point (junction /
    # symlink / OneDrive cloud placeholder / redirected folder) — exactly the
    # class of thing that makes Windows return ERROR_UNTRUSTED_MOUNT_POINT
    # (os error 448) when uv traverses it to query its managed python.
    param([string]$Path)
    try {
        $current = [System.IO.Path]::GetFullPath($Path)
    } catch {
        return $false
    }
    while ($current) {
        if (Test-Path -LiteralPath $current) {
            try {
                $attr = [System.IO.File]::GetAttributes($current)
                if ($attr -band [System.IO.FileAttributes]::ReparsePoint) { return $true }
            } catch {
                # Unable to read attributes -> treat as suspect so we prefer a
                # cleaner candidate.
                return $true
            }
        }
        $parent = [System.IO.Path]::GetDirectoryName($current)
        if ([string]::IsNullOrEmpty($parent) -or $parent -eq $current) { break }
        $current = $parent
    }
    return $false
}

function Get-SystemDriveUvHome {
    $sysDrive = if ($env:SystemDrive) { $env:SystemDrive } else { "C:" }
    return (Join-Path $sysDrive "doyoutrade\uv")
}

function Resolve-SafeUvHome {
    # Pick a uv home (managed python + cache + tools) that avoids Roaming /
    # OneDrive and any reparse point, so uv never hits os error 448 querying its
    # interpreter. Prefer LocalAppData (not roamed, rarely synced); fall back to
    # a plain dir on the system-drive root if LocalAppData is itself behind a
    # mount point or unwritable.
    $candidates = @()
    if ($env:LOCALAPPDATA) { $candidates += (Join-Path $env:LOCALAPPDATA "doyoutrade\uv") }
    $candidates += (Get-SystemDriveUvHome)

    foreach ($cand in $candidates) {
        try {
            New-Item -ItemType Directory -Force -Path $cand -ErrorAction Stop | Out-Null
        } catch {
            continue
        }
        if (Test-PathBehindReparsePoint -Path $cand) { continue }
        try {
            $probe = Join-Path $cand ".write-probe"
            [System.IO.File]::WriteAllText($probe, "ok")
            Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
        } catch {
            continue
        }
        return $cand
    }
    # Last resort: the system-drive path, even unvalidated — better than Roaming.
    return (Get-SystemDriveUvHome)
}

function Set-UvRuntimeEnv {
    # Point uv's python / cache / tool directories at a safe local home for
    # *this* process (so the child uv calls inherit it) and persist to the User
    # scope so later `uv tool upgrade` / `uv tool list` (and the launcher's
    # install check) resolve the same relocated tools.
    param([Parameter(Mandatory = $true)][string]$UvHome)
    $pyDir    = Join-Path $UvHome "python"
    $cacheDir = Join-Path $UvHome "cache"
    $toolDir  = Join-Path $UvHome "tools"
    foreach ($d in @($pyDir, $cacheDir, $toolDir)) {
        New-Item -ItemType Directory -Force -Path $d | Out-Null
    }
    $env:UV_PYTHON_INSTALL_DIR = $pyDir
    $env:UV_CACHE_DIR          = $cacheDir
    $env:UV_TOOL_DIR           = $toolDir
    try {
        [System.Environment]::SetEnvironmentVariable("UV_PYTHON_INSTALL_DIR", $pyDir, "User")
        [System.Environment]::SetEnvironmentVariable("UV_CACHE_DIR", $cacheDir, "User")
        [System.Environment]::SetEnvironmentVariable("UV_TOOL_DIR", $toolDir, "User")
    } catch {
        Write-Warn "无法把 uv 环境变量持久化到用户环境（不影响本次安装）：$($_.Exception.Message)"
    }
    Write-Ok "uv 运行目录已固定到本地安全路径：$UvHome"
    $script:UvHomeInfo = [pscustomobject]@{ Home = $UvHome; Python = $pyDir; Cache = $cacheDir; Tools = $toolDir }
    return $script:UvHomeInfo
}

function Test-IsUntrustedMountError {
    # Recognise ERROR_UNTRUSTED_MOUNT_POINT in whatever locale uv surfaced it.
    param([string]$Text)
    if ([string]::IsNullOrWhiteSpace($Text)) { return $false }
    return ($Text -match "os error 448") `
        -or ($Text -match "untrusted mount") `
        -or ($Text -match "不受信任的装入点")
}

function Invoke-UvStreaming {
    # Run uv, stream every line to the console live *and* capture it so we can
    # inspect the output (e.g. detect os error 448) and drive a retry. We flip
    # ErrorActionPreference to Continue for the duration: under the script-wide
    # "Stop", PowerShell 5.1 wraps uv's stderr progress ("Resolved N packages…")
    # into a terminating NativeCommandError and would abort a perfectly healthy
    # download. Success/failure is judged solely by $LASTEXITCODE.
    param([Parameter(Mandatory = $true)][string[]]$UvArgs)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $lines = New-Object System.Collections.Generic.List[string]
    try {
        & uv @UvArgs 2>&1 | ForEach-Object {
            $line = if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.ToString() } else { "$_" }
            Write-Host $line
            $lines.Add($line)
        }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
    return [pscustomobject]@{ ExitCode = $code; Output = ($lines -join "`n") }
}

function Invoke-QuietCapture {
    # Best-effort command capture for diagnostics; never throws.
    param([scriptblock]$Script)
    $oldEap = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    try {
        $out = & $Script 2>&1 | Out-String
        return $out.Trim()
    } catch {
        return "(capture failed: $($_.Exception.Message))"
    } finally {
        $ErrorActionPreference = $oldEap
    }
}

function Write-InstallDiagnostics {
    param(
        [string]$Stage = "unknown",
        [object]$ExitCode,
        [string]$Detail
    )
    # Printed directly to the console so GUI-installer users can read it in
    # the PowerShell window Inno opens (SW_SHOW) — no log file required.
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Yellow
    Write-Host "[诊断] 安装失败详情（请整段复制给支持人员）" -ForegroundColor Yellow
    Write-Host "============================================================" -ForegroundColor Yellow
    Write-Host "  阶段: $Stage"
    if ($null -ne $ExitCode -and "$ExitCode" -ne "") {
        Write-Host "  退出码: $ExitCode"
    }
    Write-Host "  时间: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
    Write-Host "  Source: $Source"
    Write-Host "  Force: $Force"
    Write-Host "  USERPROFILE: $env:USERPROFILE"
    Write-Host "  UV_TOOL_BIN_DIR: $(if ($env:UV_TOOL_BIN_DIR) { $env:UV_TOOL_BIN_DIR } else { '(unset)' })"
    Write-Host "  XDG_BIN_HOME: $(if ($env:XDG_BIN_HOME) { $env:XDG_BIN_HOME } else { '(unset)' })"
    Write-Host "  UV_PYTHON_INSTALL_DIR: $(if ($env:UV_PYTHON_INSTALL_DIR) { $env:UV_PYTHON_INSTALL_DIR } else { '(unset)' })"
    Write-Host "  UV_TOOL_DIR: $(if ($env:UV_TOOL_DIR) { $env:UV_TOOL_DIR } else { '(unset)' })"
    Write-Host "  UV_CACHE_DIR: $(if ($env:UV_CACHE_DIR) { $env:UV_CACHE_DIR } else { '(unset)' })"
    Write-Host "  Python(固定): $script:DoyoutradePythonVersion"
    if (-not [string]::IsNullOrWhiteSpace($Detail)) {
        Write-Host "  详情: $Detail"
    }

    $uvVer = Invoke-QuietCapture { uv --version }
    Write-Host "  uv --version: $(if ($uvVer) { $uvVer } else { '(uv 不可用)' })"

    $toolBinRaw = Invoke-QuietCapture { uv tool dir --bin }
    if ($toolBinRaw) {
        $toolBin = ($toolBinRaw -split "`r?`n" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -First 1).Trim()
    } else {
        $toolBin = Join-Path $env:USERPROFILE ".local\bin"
    }
    Write-Host "  uv tool dir --bin: $toolBin"
    $shim = Join-Path $toolBin "doyoutrade.exe"
    Write-Host "  期望 shim: $shim  存在=$(Test-Path -LiteralPath $shim)"

    $toolList = Invoke-QuietCapture { uv tool list }
    Write-Host "  --- uv tool list ---"
    if ($toolList) {
        foreach ($line in ($toolList -split "`r?`n")) {
            if (-not [string]::IsNullOrWhiteSpace($line)) {
                Write-Host "    $line"
            }
        }
    } else {
        Write-Host "    (empty / unavailable)"
    }

    $pathUser = [System.Environment]::GetEnvironmentVariable("Path", "User")
    Write-Host "  User PATH 含 tool bin: $($pathUser -like "*$toolBin*")"
    Write-Host "============================================================" -ForegroundColor Yellow
    Write-Host ""
}

function Pause-OnInstallFailure {
    # Keep the console open when launched from the GUI installer so the
    # diagnostics block above stays readable. CI / automation can skip.
    if ($env:DOYOUTRADE_INSTALL_NO_PAUSE -eq "1") { return }
    if ($env:CI -eq "true") { return }
    Write-Host "以上诊断已打印在本命令行窗口。看完后按 Enter 关闭..." -ForegroundColor Yellow
    try {
        [void](Read-Host)
    } catch {
        Start-Sleep -Seconds 60
    }
}

function Write-Die {
    param(
        [Parameter(Mandatory = $true)][string]$Message,
        [string]$Stage = "unknown",
        [object]$ExitCode,
        [string]$Detail
    )
    Write-Host "[x] $Message" -ForegroundColor Red
    Write-InstallDiagnostics -Stage $Stage -ExitCode $ExitCode -Detail $Detail
    Pause-OnInstallFailure
    exit 1
}

function Ensure-Uv {
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        Write-Ok "已检测到 uv ($(uv --version))"
        return
    }
    Write-Info "未检测到 uv，正在从 astral.sh 安装…"
    try {
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    } catch {
        Write-Die -Message "uv 安装失败，请参考 https://docs.astral.sh/uv/ 手动安装后重试。" `
            -Stage "uv-install" -Detail $_.Exception.Message
    }
    # 让 uv 在当前会话内立即可用（安装器已写入用户环境变量，供以后的终端使用）。
    $uvBin = Join-Path $env:USERPROFILE ".local\bin"
    if (Test-Path $uvBin) { $env:Path = "$uvBin;$env:Path" }
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Die -Message "uv 安装后仍不可用，请重开一个 PowerShell 窗口再运行本脚本。" `
            -Stage "uv-post-install" -Detail "uv.exe not on PATH after astral install; checked $uvBin"
    }
    Write-Ok "uv 安装完成 ($(uv --version))"
}

function Check-Node {
    if (Get-Command npm -ErrorAction SilentlyContinue) {
        Write-Ok "检测到 Node.js / npm — 安装时会自动打包网页控制台。"
    } else {
        Write-Warn "未检测到 Node.js — 将安装为「API + CLI」模式（没有网页界面）。"
        Write-Warn "  想要网页控制台：装好 Node.js LTS 后重跑本脚本即可。"
    }
}

function Test-DoYouTradeInstalled {
    # 以 uv 管理的工具列表为准，避免 PATH 未刷新导致误判。
    try {
        $tools = (uv tool list 2>$null | Out-String)
        return $tools -match "^\s*doyoutrade\s"
    } catch {
        return $false
    }
}

function Get-OrphanDoYouTradeShims {
    # UV_TOOL_DIR 迁走后，uv tool list 可能为空，但 %USERPROFILE%\.local\bin 仍留有旧 shim；
    # 不带 --force 的 uv tool install 会报 Executables already exist。
    $bin = Get-UvToolBinDir
    $found = @()
    foreach ($name in @("doyoutrade.exe", "doyoutrade-cli.exe")) {
        $path = Join-Path $bin $name
        if (Test-Path -LiteralPath $path) {
            $found += $path
        }
    }
    return $found
}

function Confirm-Reinstall {
    while ($true) {
        $response = Read-Host -Prompt "检测到 doyoutrade 已安装。是否先卸载再重新安装？(Y/n)"
        if ([string]::IsNullOrWhiteSpace($response)) { $response = "Y" }
        switch ($response.ToUpper()) {
            "Y" { return $true }
            "N" { return $false }
            default { Write-Warn "请输入 Y 或 n" }
        }
    }
}

function Get-UvToolBinDir {
    # Prefer uv's own answer (honours UV_TOOL_BIN_DIR / XDG_BIN_HOME); fall back
    # to the documented default so older uv without `tool dir --bin` still works.
    $fallback = Join-Path $env:USERPROFILE ".local\bin"
    try {
        $out = & uv tool dir --bin 2>$null
        if (($LASTEXITCODE -eq 0) -and $out) {
            $dir = ($out | Select-Object -First 1).ToString().Trim()
            if (-not [string]::IsNullOrWhiteSpace($dir)) { return $dir }
        }
    } catch {
        # fall through to default
    }
    return $fallback
}

function Write-ToolBinDirMarker {
    param([Parameter(Mandatory = $true)][string]$BinDir)
    # Launcher bat reads this when PATH / default .local\bin miss the shim.
    $markerDir = Join-Path $env:USERPROFILE ".doyoutrade"
    New-Item -ItemType Directory -Force -Path $markerDir | Out-Null
    $marker = Join-Path $markerDir "tool-bin-dir.txt"
    $line = $BinDir.Trim().TrimEnd('\', '/')
    [System.IO.File]::WriteAllText($marker, $line + [Environment]::NewLine)
    Write-Ok "已写入工具目录标记：$marker -> $line"
}

function Update-ShellPath {
    $uvBin = Get-UvToolBinDir
    $userPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
    if ($userPath -like "*$uvBin*") {
        Write-Ok "PATH 已包含 $uvBin"
        return
    }

    Write-Info "正在更新 PATH ..."
    $oldErrorAction = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    try {
        $null = (uv tool update-shell) 2>&1
    } catch {
        # uv tool update-shell 在旧版 uv 或某些 shell 下可能输出信息性消息并被 PowerShell
        # 包装为 NativeCommandError；只要不影响后续命令可用，即可忽略。
    } finally {
        $ErrorActionPreference = $oldErrorAction
    }

    if ($LASTEXITCODE -ne 0) {
        Write-Warn "uv tool update-shell 返回非零退出码。若后续找不到命令，请重开终端。"
    }
}

function Install-DoYouTrade {
    # Route uv's managed python / cache / tools off Roaming (OneDrive / folder
    # redirection territory) *before* touching uv, so both the install-check and
    # any uninstall target the same safe home. This is the root fix for the
    # os error 448 "untrusted mount point" failure.
    $uvHome = Resolve-SafeUvHome
    Set-UvRuntimeEnv -UvHome $uvHome | Out-Null

    $alreadyInstalled = Test-DoYouTradeInstalled

    if ($alreadyInstalled) {
        if ($Force) {
            Write-Info "检测到已安装，使用 -Force 强制卸载重装 ..."
        } else {
            if (-not (Confirm-Reinstall)) {
                Write-Ok "已取消，保留现有安装。"
                exit 0
            }
            Write-Info "用户确认重新安装，正在卸载现有版本 ..."
        }
        Write-Info "重装前 uv tool list："
        $beforeList = Invoke-QuietCapture { uv tool list }
        if ($beforeList) {
            foreach ($line in ($beforeList -split "`r?`n")) {
                if (-not [string]::IsNullOrWhiteSpace($line)) {
                    Write-Host "    $line"
                }
            }
        } else {
            Write-Host "    (empty / unavailable)"
        }
        Write-Info "正在卸载现有 doyoutrade ..."
        uv tool uninstall doyoutrade
        if ($LASTEXITCODE -ne 0) {
            Write-Die -Message "卸载失败。" -Stage "uv-tool-uninstall" -ExitCode $LASTEXITCODE
        }
        Write-Ok "旧版本已卸载，开始重新安装。"
    } else {
        # uv tool list 为空但 bin 里仍有 exe：常见于 UV_TOOL_DIR 迁到 LocalAppData 之后。
        $orphanShims = @(Get-OrphanDoYouTradeShims)
        if ($orphanShims.Count -gt 0) {
            Write-Warn "检测到孤儿 shim（uv tool list 无记录，但可执行文件仍在）："
            foreach ($p in $orphanShims) {
                Write-Host "    $p"
            }
            Write-Info "将用 uv tool install --force 覆盖这些文件（与 install.sh 行为一致）。"
        }
    }

    Write-Info "正在安装 doyoutrade[qmt-proxy]（源：$Source）…"
    Write-Info "Windows 版内置 qmt-proxy（含 xtquant），首次安装会拉取依赖并构建，可能需要几分钟。"

    # 先把固定版本的 Python 预置到安全目录里（best-effort）。uv 默认会抓满足
    # requires-python 的最新版（如 3.14），既没有 xtquant wheel，又正是它被下载进
    # Roaming 后触发 os error 448 的根源。钉到 3.12 从源头规避。
    Write-Info "准备 Python $script:DoyoutradePythonVersion（固定版本，兼容 xtquant，落到本地安全目录）…"
    $pyProvision = Invoke-UvStreaming -UvArgs @("python", "install", $script:DoyoutradePythonVersion)
    if ($pyProvision.ExitCode -ne 0) {
        Write-Warn "预置 Python $script:DoyoutradePythonVersion 返回非零（$($pyProvision.ExitCode)），继续尝试安装……"
    }

    # 用 PEP 508 direct reference（"name[extra] @ <source>"）而不是 `--from <source> "name[extra]"`：
    # uv 的 `--from` 把位置参数当作可执行名解析，PackageName 不接受 `[extra]`，会以
    # "conflicts with install request" 误拒（uv 0.10+ 已验证）。PEP 508 写法对所有 uv 版本都稳。
    # `--force`：幂等覆盖（含孤儿 shim），与 install.sh / 应用内 updater 一致；脚本级
    # -Force 只控制「是否先卸载」，不能代替此处传给 uv 的 --force。
    # `--python` 钉住解释器；输出经 Invoke-UvStreaming 边显示边捕获（内部把
    # ErrorActionPreference 临时切到 Continue，避免 stderr 进度被包成 NativeCommandError
    # 误杀），成败只看 $LASTEXITCODE。
    $installArgs = @("tool", "install", "--force", "--python", $script:DoyoutradePythonVersion, "doyoutrade[qmt-proxy] @ $Source")
    $result = Invoke-UvStreaming -UvArgs $installArgs

    # 防御式重试：若已选的安全目录仍被判为「不受信任的装入点」(os error 448)——
    # 例如 LocalAppData 也被 OneDrive「文件按需」接管、或 reparse 检测漏网——
    # 退到系统盘根目录（绝不会被漫游 / 重定向）再来一次。
    $sysDriveHome = Get-SystemDriveUvHome
    if (($result.ExitCode -ne 0) -and (Test-IsUntrustedMountError $result.Output) -and ($uvHome -ne $sysDriveHome)) {
        Write-Warn "检测到「不受信任的装入点」(os error 448)。当前 uv 目录疑似被 OneDrive / 文件夹重定向接管，改用系统盘目录 $sysDriveHome 重试……"
        Set-UvRuntimeEnv -UvHome $sysDriveHome | Out-Null
        $uvHome = $sysDriveHome
        Invoke-UvStreaming -UvArgs @("python", "install", $script:DoyoutradePythonVersion) | Out-Null
        $result = Invoke-UvStreaming -UvArgs $installArgs
    }

    if ($result.ExitCode -ne 0) {
        $detail = "command: uv tool install --force --python $script:DoyoutradePythonVersion `"doyoutrade[qmt-proxy] @ $Source`""
        if (Test-IsUntrustedMountError $result.Output) {
            $detail += " | 命中 os error 448（不受信任的装入点）。多为 OneDrive「文件按需」接管了用户目录，或 AppData 被组策略重定向到网络位置。请关闭该目录的 OneDrive 同步 /「文件按需」，或把账户目录移出重定向后重试。"
            Write-Die -Message "安装失败：uv 无法访问其 Python 目录（不受信任的装入点 / OneDrive）。这不是网络问题。" `
                -Stage "uv-tool-install" -ExitCode $result.ExitCode -Detail $detail
        } else {
            Write-Die -Message "安装失败，请检查网络 / 安装源后重试。" `
                -Stage "uv-tool-install" -ExitCode $result.ExitCode -Detail $detail
        }
    }

    Update-ShellPath

    # Make the new shim visible in *this* session, then verify it exists.
    # Without this, a GUI installer that immediately launches the shortcut
    # can still report "doyoutrade not found" even after a successful install.
    # Also persist the real bin dir (may differ from ~/.local/bin when
    # UV_TOOL_BIN_DIR / XDG_BIN_HOME is set) so the .bat launcher can find it
    # even when Explorer inherits a stale PATH.
    $uvBin = Get-UvToolBinDir
    if (Test-Path $uvBin) { $env:Path = "$uvBin;$env:Path" }
    $shim = Join-Path $uvBin "doyoutrade.exe"
    if (-not (Get-Command doyoutrade -ErrorAction SilentlyContinue) -and -not (Test-Path -LiteralPath $shim)) {
        Write-Die -Message "uv tool install 成功，但未找到 doyoutrade 命令（期望路径：$shim）。" `
            -Stage "shim-verify" -Detail "Get-Command miss and shim file missing at $shim"
    }
    Write-ToolBinDirMarker -BinDir $uvBin

    Write-Ok "doyoutrade 安装完成（已内置 qmt-proxy）。"
}

Write-Host ""
Write-Host "============================================================"
Write-Host "DoYouTrade 安装脚本 (Windows，内置 qmt-proxy)"
Write-Host "============================================================"
Write-Host ""
Ensure-Uv
Check-Node
Install-DoYouTrade

Write-Host ""
Write-Host "============================================================"
Write-Ok "安装完成！下一步："
Write-Host ""
Write-Host "  1. 在 PowerShell 运行：  doyoutrade" -ForegroundColor White
Write-Host "     （默认 --mode both：同进程起 DoYouTrade:8000 与内置 qmt-proxy:8001）"
Write-Host "     （若提示找不到命令，重开一个 PowerShell 窗口，或运行  uv tool update-shell  后重试）"
Write-Host "  2. 首次启动会进入安装向导，按提示选择一个大模型供应商并填入 API Key。"
Write-Host "  3. 登录券商 miniQMT / QMT 量化终端后，实时行情开箱即用（默认账户已自动指向本机 qmt-proxy）。"
Write-Host "  4. 浏览器打开  http://localhost:8000  即是完整控制台。"
Write-Host ""
Write-Host "只想跑 DoYouTrade 本体： doyoutrade --mode doyoutrade    只跑行情代理： doyoutrade --mode qmt-proxy"
Write-Host "默认使用本地 SQLite，零外部数据库依赖；进阶配置见 README。"
Write-Host "升级：  uv tool upgrade doyoutrade      卸载：  uv tool uninstall doyoutrade"
Write-Host "============================================================"
Write-Host ""
