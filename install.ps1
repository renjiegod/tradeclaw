#Requires -Version 5.1
<#
.SYNOPSIS
    DoYouTrade 一键安装脚本 (Windows / PowerShell)。

.DESCRIPTION
    只负责「安装」，不启动服务、不碰任何交易账户：
      1. 检测 / 安装 uv（Astral 的 Python 包管理器，自带 Python 3.12）
      2. 默认从 GitHub / Gitee Release 安装**预构建 wheel**（已内嵌网页控制台，
         用户机无需 Node.js）
      3. uv tool install 把 doyoutrade[qmt-proxy] 装成常驻命令（内置 qmt-proxy，
         `doyoutrade` 启动、应用内自动更新）
      4. 打印下一步：运行 `doyoutrade`，首启在网页向导配置模型

    Windows 版一并安装内置的 qmt-proxy（把 xtquant 封装为本机 REST 服务）。运行
    `doyoutrade` 默认走 --mode both：同一进程内同时起 DoYouTrade(:8000) 与 qmt-proxy(:8001)，
    并自动把默认账户指向本机 qmt-proxy。只要已登录券商 miniQMT，实时行情开箱即用，
    无需手工配置 base_url。

    可重复运行：若已安装，默认会询问是否卸载后重装；加 -Force 跳过询问。

.PARAMETER Source
    安装源。未指定时按 DOYOUTRADE_MIRROR / GitHub 连通性自动选择
    GitHub 或 Gitee 的 **Release wheel**；也可指定本地目录 / fork / git+ URL。
    DOYOUTRADE_INSTALL_SOURCE 优先。

.PARAMETER Version
    要安装的发行版版本（如 0.1.10 或 v0.1.10）。未指定时取对应镜像的 latest
    Release。图形安装包会传入与 Setup.exe 一致的版本。也可用环境变量
    DOYOUTRADE_INSTALL_VERSION。

.PARAMETER Force
    若已安装，直接卸载并重新安装，不弹出确认。

.EXAMPLE
    用法一（最省事，在 PowerShell 里）：
      irm https://raw.githubusercontent.com/renjiegod/doyoutrade/main/install.ps1 | iex

.EXAMPLE
    中国网络 / Gitee 镜像：
      irm https://gitee.com/renjie-god/doyoutrade/raw/main/install.ps1 | iex
      # 或强制走 Gitee 安装源：
      $env:DOYOUTRADE_MIRROR = "gitee"; irm https://gitee.com/renjie-god/doyoutrade/raw/main/install.ps1 | iex

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
    [string]$Source = $env:DOYOUTRADE_INSTALL_SOURCE,
    [string]$Version = $env:DOYOUTRADE_INSTALL_VERSION,
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

# The last 3.12.x that shipped a python.org Windows binary installer (later
# 3.12.x are source-only security releases). Used only by the junction-free
# fallback when uv-managed Python is unusable (os error 448).
$script:DoyoutradePythonOrgVersion = "3.12.10"

# Filled in by Set-UvRuntimeEnv for the diagnostics block.
$script:UvHomeInfo = $null

# Set when the os error 448 fallback switched to an explicit interpreter path
# (instead of letting uv resolve "3.12" through its junctioned managed dir).
$script:JunctionFreePython = $null

# The source actually handed to uv. Differs from $Source only when the machine
# has no git and a git+ source was converted to an archive URL.
$script:EffectiveSource = $null

$script:GithubGitSource = "git+https://github.com/renjiegod/doyoutrade.git"
$script:GiteeGitSource = "git+https://gitee.com/renjie-god/doyoutrade.git"
$script:GithubOwnerRepo = "renjiegod/doyoutrade"
$script:GiteeOwner = "renjie-god"
$script:GiteeRepo = "doyoutrade"

function Test-GitHubReachable {
    # C: short probe — China networks often time out on github.com.
    try {
        $null = Invoke-WebRequest -Uri "https://github.com/" -UseBasicParsing -TimeoutSec 3
        return $true
    } catch {
        return $false
    }
}

function Resolve-PreferredMirror {
    # Priority: DOYOUTRADE_MIRROR=gitee|cn|china|github|gh > GitHub reachability.
    $mirror = "$($env:DOYOUTRADE_MIRROR)".Trim().ToLowerInvariant()
    switch ($mirror) {
        { $_ -in @("gitee", "cn", "china") } { return "gitee" }
        { $_ -in @("github", "gh") } { return "github" }
    }
    if (Test-GitHubReachable) {
        return "github"
    }
    Write-Warn "GitHub 不可达（或超时），改用 Gitee 镜像。"
    return "gitee"
}

function Normalize-ReleaseTag {
    param([string]$Tag)
    $t = "$Tag".Trim()
    if ([string]::IsNullOrWhiteSpace($t)) {
        throw "release tag is empty"
    }
    if ($t.StartsWith("v") -or $t.StartsWith("V")) {
        $bare = $t.Substring(1)
        return @{ Tag = "v$bare"; Version = $bare }
    }
    return @{ Tag = "v$t"; Version = $t }
}

function Get-WheelFileName {
    param([string]$Version)
    $bare = (Normalize-ReleaseTag -Tag $Version).Version
    return "doyoutrade-$bare-py3-none-any.whl"
}

function Resolve-ReleaseWheelUrl {
    param(
        [string]$Tag,
        [ValidateSet("github", "gitee")]
        [string]$Mirror = "github"
    )
    $norm = Normalize-ReleaseTag -Tag $Tag
    $file = Get-WheelFileName -Version $norm.Version
    if ($Mirror -eq "gitee") {
        return "https://gitee.com/$($script:GiteeOwner)/$($script:GiteeRepo)/releases/download/$($norm.Tag)/$file"
    }
    return "https://github.com/$($script:GithubOwnerRepo)/releases/download/$($norm.Tag)/$file"
}

function Test-RemoteUrlExists {
    param([string]$Uri)
    try {
        $resp = Invoke-WebRequest -Uri $Uri -Method Head -UseBasicParsing -TimeoutSec 15
        return ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 400)
    } catch {
        # Some hosts reject HEAD; try a tiny ranged GET.
        try {
            $headers = @{ Range = "bytes=0-0" }
            $resp = Invoke-WebRequest -Uri $Uri -Method Get -Headers $headers -UseBasicParsing -TimeoutSec 20
            return ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 400)
        } catch {
            return $false
        }
    }
}

function Get-LatestReleaseTag {
    param(
        [ValidateSet("github", "gitee")]
        [string]$Mirror = "github"
    )
    if ($Mirror -eq "gitee") {
        $api = "https://gitee.com/api/v5/repos/$($script:GiteeOwner)/$($script:GiteeRepo)/releases/latest"
    } else {
        $api = "https://api.github.com/repos/$($script:GithubOwnerRepo)/releases/latest"
    }
    try {
        $payload = Invoke-RestMethod -Uri $api -TimeoutSec 20
    } catch {
        throw "无法读取 $Mirror latest Release：$($_.Exception.Message)"
    }
    $tag = [string]$payload.tag_name
    if ([string]::IsNullOrWhiteSpace($tag)) {
        throw "$Mirror latest Release 缺少 tag_name"
    }
    return $tag
}

function Test-IsDevOrCiVersion {
    param([string]$Version)
    if ([string]::IsNullOrWhiteSpace($Version)) { return $false }
    $v = $Version.Trim().ToLowerInvariant()
    return ($v -match 'dev|ci|0\.0\.0')
}

function Resolve-DefaultInstallSource {
    # Default: prebuilt Release wheel (embeds web UI). Explicit -Source /
    # DOYOUTRADE_INSTALL_SOURCE still wins (handled by caller).
    $mirror = Resolve-PreferredMirror
    $tagInput = "$Version".Trim()
    if ([string]::IsNullOrWhiteSpace($tagInput)) {
        Write-Info "未指定版本——查询 $mirror latest Release…"
        $tagInput = Get-LatestReleaseTag -Mirror $mirror
    }
    $norm = Normalize-ReleaseTag -Tag $tagInput
    $wheelUrl = Resolve-ReleaseWheelUrl -Tag $norm.Tag -Mirror $mirror
    Write-Info "安装源：$mirror Release $($norm.Tag) → $wheelUrl"
    if (Test-RemoteUrlExists -Uri $wheelUrl) {
        return $wheelUrl
    }
    # CI / local dry-build Setup.exe may pin 0.0.0-dev with no Release asset.
    if (Test-IsDevOrCiVersion -Version $norm.Version) {
        $gitSource = if ($mirror -eq "gitee") { $script:GiteeGitSource } else { $script:GithubGitSource }
        Write-Warn "未找到预构建 wheel（开发/CI 版本 $($norm.Tag)）——回退到源码安装：$gitSource"
        Write-Warn "正式用户请安装带版本号的 Release 包；源码安装需要本机 Node.js 才能打包网页控制台。"
        return $gitSource
    }
    throw "Release wheel 不存在或无法下载：$wheelUrl（请确认 $mirror 上已发布 $($norm.Tag) 的 .whl）"
}

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

function Test-PythonExeUsable {
    # True when $Exe launches and is exactly the pinned minor version (3.12).
    # Rejects the Microsoft Store app-execution alias (exits non-zero with a
    # "Python was not found" hint) and any other-version interpreter.
    param([string]$Exe)
    if ([string]::IsNullOrWhiteSpace($Exe)) { return $false }
    if (-not (Test-Path -LiteralPath $Exe)) { return $false }
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $out = & $Exe -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -ne 0) { return $false }
        $line = ($out | Select-Object -First 1)
        return ("$line".Trim() -eq $script:DoyoutradePythonVersion)
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $prev
    }
}

function Remove-DanglingMinorVersionLinks {
    # A failed `uv python install` leaves a half-made minor-version junction
    # (cpython-3.12-windows-...) behind, and every later uv command dies on it
    # while enumerating managed pythons. uv cannot self-heal this state
    # (astral-sh/uv#19622). Deleting a junction does not traverse it, so the
    # cleanup works even on machines where traversal is what error 448 blocks.
    param([string[]]$PyDirs)
    foreach ($dir in ($PyDirs | Where-Object { $_ } | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $dir)) { continue }
        $links = Get-ChildItem -LiteralPath $dir -Directory -ErrorAction SilentlyContinue |
            Where-Object {
                ($_.Name -like "cpython-*") -and
                ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint)
            }
        foreach ($link in $links) {
            Write-Warn "清理安装失败残留的 Python 版本链接（junction）：$($link.FullName)"
            try {
                [System.IO.Directory]::Delete($link.FullName, $false)
            } catch {
                Write-Warn "  清理失败（不影响继续）：$($_.Exception.Message)"
            }
        }
    }
}

function Get-ManagedJunctionFreePython {
    # error 448 只毁掉 minor-version junction；`uv python install` 下载出来的
    # cpython-<完整版本>-... 是普通目录，本体完好可用。找出其中最新的解释器。
    param([string[]]$PyDirs)
    foreach ($dir in ($PyDirs | Where-Object { $_ } | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $dir)) { continue }
        $cands = Get-ChildItem -LiteralPath $dir -Directory -ErrorAction SilentlyContinue |
            Where-Object {
                ($_.Name -like "cpython-$($script:DoyoutradePythonVersion).*") -and
                -not ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint)
            } |
            Sort-Object -Property @{Expression = {
                $m = [regex]::Match($_.Name, "^cpython-(\d+\.\d+\.\d+)")
                if ($m.Success) { [version]$m.Groups[1].Value } else { [version]"0.0.0" }
            }} -Descending
        foreach ($cand in $cands) {
            $exe = Join-Path $cand.FullName "python.exe"
            if (Test-PythonExeUsable -Exe $exe) { return $exe }
        }
    }
    return $null
}

function Get-SystemPython {
    # 系统里已装的 CPython 3.12：优先 py 启动器（读 PEP 514 注册表，覆盖
    # python.org / 商店外的常规安装），其次 PATH 上的 python*。
    $ver = $script:DoyoutradePythonVersion
    $candidates = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $prev = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            $exe = & py "-$ver" -c "import sys; print(sys.executable)" 2>$null | Select-Object -First 1
            if (($LASTEXITCODE -eq 0) -and $exe) { $candidates += "$exe".Trim() }
        } catch {
            # py 启动器没有该版本时静默跳过，走后面的 PATH 探测。
        } finally {
            $ErrorActionPreference = $prev
        }
    }
    foreach ($name in @("python$ver", "python3", "python")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source) { $candidates += $cmd.Source }
    }
    foreach ($exe in ($candidates | Select-Object -Unique)) {
        if (Test-PythonExeUsable -Exe $exe) { return $exe }
    }
    return $null
}

function Install-PythonFromPythonOrg {
    # 终极兜底：静默安装 python.org 官方 3.12（仅当前用户、不进 PATH、无需管理员），
    # 目录里没有任何 junction / symlink，对 error 448 完全免疫。
    param([Parameter(Mandatory = $true)][string]$UvHome)
    $ver = $script:DoyoutradePythonOrgVersion
    $targetDir = Join-Path (Split-Path -Parent $UvHome) ("python" + ($script:DoyoutradePythonVersion -replace "\.", ""))
    $targetExe = Join-Path $targetDir "python.exe"
    if (Test-PythonExeUsable -Exe $targetExe) {
        Write-Ok "检测到之前兜底安装的 Python：$targetExe"
        return $targetExe
    }

    $file = "python-$ver-amd64.exe"
    # 官方源优先；国内网络不畅时退华为云 / npmmirror（均为 python.org ftp 目录镜像）。
    $urls = @(
        "https://www.python.org/ftp/python/$ver/$file",
        "https://mirrors.huaweicloud.com/python/$ver/$file",
        "https://registry.npmmirror.com/-/binary/python/$ver/$file"
    )
    $installer = Join-Path $UvHome $file
    $downloaded = $false
    foreach ($url in $urls) {
        Write-Info "下载 Python $ver 官方安装包：$url"
        try {
            Invoke-WebRequest -Uri $url -OutFile $installer -UseBasicParsing
            $downloaded = $true
            break
        } catch {
            Write-Warn "下载失败（$($_.Exception.Message)），尝试下一个源……"
        }
    }
    if (-not $downloaded) { return $null }

    Write-Info "静默安装 Python $ver（仅当前用户，无需管理员）到 $targetDir …"
    # TargetDir 手工加引号：PS 5.1 的 Start-Process 只用空格拼接 ArgumentList，
    # 用户名带空格时路径会被拆散。
    $installerArgs = @(
        "/quiet", "InstallAllUsers=0", "PrependPath=0", "Include_launcher=0",
        "Include_test=0", "AssociateFiles=0", "Shortcuts=0",
        "TargetDir=`"$targetDir`""
    )
    try {
        $proc = Start-Process -FilePath $installer -ArgumentList $installerArgs -Wait -PassThru
        if ($proc.ExitCode -ne 0) {
            Write-Warn "Python 官方安装包返回非零退出码：$($proc.ExitCode)"
            return $null
        }
    } catch {
        Write-Warn "运行 Python 官方安装包失败：$($_.Exception.Message)"
        return $null
    } finally {
        Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
    }
    if (Test-PythonExeUsable -Exe $targetExe) { return $targetExe }
    Write-Warn "Python 官方安装包退出码为 0，但 $targetExe 不可用。"
    return $null
}

function Resolve-JunctionFreePython {
    # os error 448 的拦截多是进程级（Redirection Trust / OneDrive minifilter），
    # 与目录位置无关，此时 uv 托管 Python 依赖的 minor-version junction 永远建不
    # 起来。这里返回一个完全不经过 junction 的解释器路径，显式传给 --python：
    #   1) 复用 uv 已下载的 cpython-<完整版本> 实体目录（junction 只是它旁边的别名）
    #   2) 系统已装的 Python 3.12
    #   3) 静默安装 python.org 官方 3.12（仅当前用户）
    param([string[]]$PyDirs, [Parameter(Mandatory = $true)][string]$UvHome)
    Remove-DanglingMinorVersionLinks -PyDirs $PyDirs
    $exe = Get-ManagedJunctionFreePython -PyDirs $PyDirs
    if ($exe) {
        Write-Ok "复用 uv 已下载的 Python（绕过版本链接 junction）：$exe"
        return $exe
    }
    $exe = Get-SystemPython
    if ($exe) {
        Write-Ok "使用系统已安装的 Python $script:DoyoutradePythonVersion：$exe"
        return $exe
    }
    return (Install-PythonFromPythonOrg -UvHome $UvHome)
}

function Use-Utf8ConsoleDecoding {
    # uv (Rust) always writes UTF-8 to a *piped* stdout/stderr，而中文 Windows 的
    # PowerShell 5.1 默认按系统代码页（CP936）解码管道字节——于是 uv 报错里的中文
    # 系统消息（如「无法遍历该路径…」）在捕获后变成「鏃犳硶閬嶅巻…」乱码，用户复制
    # 给支持人员的诊断没法读。捕获 uv 输出前切到 UTF-8 解码，用完由调用方还原。
    # Returns the previous encoding (or $null when the host has no console).
    try {
        $prev = [Console]::OutputEncoding
        [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
        return $prev
    } catch {
        return $null
    }
}

function Restore-ConsoleDecoding {
    param($Previous)
    if ($null -eq $Previous) { return }
    try { [Console]::OutputEncoding = $Previous } catch { }
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
    $prevEnc = Use-Utf8ConsoleDecoding
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
        Restore-ConsoleDecoding -Previous $prevEnc
    }
    return [pscustomobject]@{ ExitCode = $code; Output = ($lines -join "`n") }
}

function Invoke-QuietCapture {
    # Best-effort command capture for diagnostics; never throws.
    param([scriptblock]$Script)
    $oldEap = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    $prevEnc = Use-Utf8ConsoleDecoding
    try {
        $out = & $Script 2>&1 | Out-String
        return $out.Trim()
    } catch {
        return "(capture failed: $($_.Exception.Message))"
    } finally {
        $ErrorActionPreference = $oldEap
        Restore-ConsoleDecoding -Previous $prevEnc
    }
}

function Test-IsGitMissingError {
    # uv needs a git executable for `git+https://...` sources; GUI-installer
    # machines rarely have one. Recognise uv's message in either locale.
    param([string]$Text)
    if ([string]::IsNullOrWhiteSpace($Text)) { return $false }
    return ($Text -match "Git executable not found") `
        -or ($Text -match "Ensure that Git is installed")
}

function Convert-GitSourceToArchiveUrl {
    # git+ 源 -> 归档直链（uv 构建归档 URL 完全不需要 git）。
    #   GitHub: git+https://github.com/<owner>/<repo>[.git][@<ref>]
    #        -> https://github.com/<owner>/<repo>/archive/<ref>.zip
    #   Gitee:  git+https://gitee.com/<owner>/<repo>[.git][@<ref>]
    #        -> https://gitee.com/<owner>/<repo>/repository/archive/<ref>.zip
    # <ref> 可以是分支、tag 或 commit SHA。其他 host 返回 $null。
    param([string]$GitSource)
    if ([string]::IsNullOrWhiteSpace($GitSource)) { return $null }
    $gh = [regex]::Match($GitSource, '^git\+(https://github\.com/[^/@]+/[^/@]+?)(?:\.git)?(?:@([^@]+))?$')
    if ($gh.Success) {
        $repoUrl = $gh.Groups[1].Value
        $ref = if ($gh.Groups[2].Success -and $gh.Groups[2].Value) { $gh.Groups[2].Value } else { "main" }
        return "$repoUrl/archive/$ref.zip"
    }
    $gitee = [regex]::Match(
        $GitSource,
        '^git\+https://gitee\.com/([^/@]+)/([^/@]+?)(?:\.git)?(?:@([^@]+))?$'
    )
    if ($gitee.Success) {
        $owner = $gitee.Groups[1].Value
        $repo = $gitee.Groups[2].Value
        $ref = if ($gitee.Groups[3].Success -and $gitee.Groups[3].Value) { $gitee.Groups[3].Value } else { "main" }
        return "https://gitee.com/$owner/$repo/repository/archive/$ref.zip"
    }
    return $null
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
    if ($script:EffectiveSource -and ($script:EffectiveSource -ne $Source)) {
        Write-Host "  实际安装源(无Git归档直链): $script:EffectiveSource"
    }
    $gitVer = if (Get-Command git -ErrorAction SilentlyContinue) { Invoke-QuietCapture { git --version } } else { $null }
    Write-Host "  git: $(if ($gitVer) { $gitVer } else { '(未安装)' })"
    Write-Host "  Force: $Force"
    Write-Host "  USERPROFILE: $env:USERPROFILE"
    Write-Host "  UV_TOOL_BIN_DIR: $(if ($env:UV_TOOL_BIN_DIR) { $env:UV_TOOL_BIN_DIR } else { '(unset)' })"
    Write-Host "  XDG_BIN_HOME: $(if ($env:XDG_BIN_HOME) { $env:XDG_BIN_HOME } else { '(unset)' })"
    Write-Host "  UV_PYTHON_INSTALL_DIR: $(if ($env:UV_PYTHON_INSTALL_DIR) { $env:UV_PYTHON_INSTALL_DIR } else { '(unset)' })"
    Write-Host "  UV_TOOL_DIR: $(if ($env:UV_TOOL_DIR) { $env:UV_TOOL_DIR } else { '(unset)' })"
    Write-Host "  UV_CACHE_DIR: $(if ($env:UV_CACHE_DIR) { $env:UV_CACHE_DIR } else { '(unset)' })"
    Write-Host "  Python(固定): $script:DoyoutradePythonVersion"
    Write-Host "  免junction解释器: $(if ($script:JunctionFreePython) { $script:JunctionFreePython } else { '(未启用)' })"
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

function Check-NodeForSourceInstall {
    # Only relevant when installing from git+/archive (no prebuilt wheel).
    if ($script:EffectiveSource -notlike "git+*" `
        -and $script:EffectiveSource -notlike "*.zip" `
        -and $script:EffectiveSource -notlike "*archive*") {
        return
    }
    if ($script:EffectiveSource -like "*.whl") {
        return
    }
    if (Get-Command npm -ErrorAction SilentlyContinue) {
        Write-Ok "检测到 Node.js / npm — 源码安装时会打包网页控制台。"
    } else {
        Write-Warn "正在从源码安装且未检测到 Node.js — 打出的包可能没有网页控制台。"
        Write-Warn "  正式用户请改用 Release 预构建 wheel（不要设置 DOYOUTRADE_INSTALL_SOURCE）。"
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

    # git+ 源需要本机有 git 可执行文件，而 GUI 安装包的目标用户机器上大多没有
    # （实测报「Git executable not found」）。没有 git 时把 GitHub / Gitee git+
    # 源转成归档直链，uv 直接下载构建，零 git 依赖；版本号是 pyproject 里的静态
    # 值，不依赖 git describe，从归档构建结果一致。
    $script:EffectiveSource = $Source
    if (($Source -like "git+*") -and -not (Get-Command git -ErrorAction SilentlyContinue)) {
        $archiveUrl = Convert-GitSourceToArchiveUrl -GitSource $Source
        if ($archiveUrl) {
            Write-Warn "未检测到 Git——改用归档直链安装（无需 Git）：$archiveUrl"
            $script:EffectiveSource = $archiveUrl
        } else {
            Write-Die -Message "安装源为 git+ 但本机没有 Git，且该源无法转换为归档直链。请先安装 Git for Windows（https://git-scm.com/download/win）后重跑本脚本。" `
                -Stage "git-missing" -Detail "source: $Source | git not on PATH and source is not a github/gitee git+ URL"
        }
    }
    Check-NodeForSourceInstall
    $effectiveSource = $script:EffectiveSource

    Write-Info "正在安装 doyoutrade[qmt-proxy]（源：$effectiveSource）…"
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
    $installArgs = @("tool", "install", "--force", "--python", $script:DoyoutradePythonVersion, "doyoutrade[qmt-proxy] @ $effectiveSource")
    $result = Invoke-UvStreaming -UvArgs $installArgs

    # 防御式重试（os error 448「不受信任的装入点」）：这类拦截多是进程级的
    # （Redirection Trust 缓解 / OneDrive minifilter），换目录往往救不回来——实测
    # 干净的 C:\doyoutrade\uv 一样中招。所以一次重试里做两件事：
    #   1) 若还在 LocalAppData，顺手迁到系统盘根（排除目录被重定向的少数情况）；
    #   2) 核心修复：不再让 uv 经由它建不起来的 minor-version junction 解析
    #      "3.12"，改用 Resolve-JunctionFreePython 拿一个实体解释器路径显式传给
    #      --python（uv 对显式路径直接查询，不枚举托管目录，不再触碰 junction）。
    $sysDriveHome = Get-SystemDriveUvHome
    if (($result.ExitCode -ne 0) -and (Test-IsUntrustedMountError $result.Output)) {
        $oldPyDir = $env:UV_PYTHON_INSTALL_DIR
        if ($uvHome -ne $sysDriveHome) {
            Write-Warn "检测到「不受信任的装入点」(os error 448)。先把 uv 目录迁到系统盘 $sysDriveHome ……"
            Set-UvRuntimeEnv -UvHome $sysDriveHome | Out-Null
            $uvHome = $sysDriveHome
            Invoke-UvStreaming -UvArgs @("python", "install", $script:DoyoutradePythonVersion) | Out-Null
        } else {
            Write-Warn "检测到「不受信任的装入点」(os error 448)。"
        }
        Write-Warn "该拦截通常是进程级的（Redirection Trust / OneDrive），uv 的 Python 版本链接（junction）在本机不可用，改用免 junction 解释器重试……"
        $pyExe = Resolve-JunctionFreePython -PyDirs @($env:UV_PYTHON_INSTALL_DIR, $oldPyDir) -UvHome $uvHome
        if ($pyExe) {
            $script:JunctionFreePython = $pyExe
            $installArgs = @("tool", "install", "--force", "--python", $pyExe, "doyoutrade[qmt-proxy] @ $effectiveSource")
            $result = Invoke-UvStreaming -UvArgs $installArgs
        } else {
            Write-Warn "未能取得可用的 Python $script:DoyoutradePythonVersion（uv 托管目录 / 系统 / python.org 兜底均失败）。"
        }
    }

    if ($result.ExitCode -ne 0) {
        $pythonSpec = if ($script:JunctionFreePython) { $script:JunctionFreePython } else { $script:DoyoutradePythonVersion }
        $detail = "command: uv tool install --force --python $pythonSpec `"doyoutrade[qmt-proxy] @ $effectiveSource`""
        if (Test-IsUntrustedMountError $result.Output) {
            $detail += " | 命中 os error 448（不受信任的装入点），且免 junction 解释器回退未能完成安装。常见诱因：右键「以管理员身份运行」了安装器（Redirection Trust 会拦截提权进程穿越 junction，请用普通双击重跑）；OneDrive「文件按需」（请先退出 OneDrive 再重跑）；AppData 被组策略重定向到网络位置。"
            Write-Die -Message "安装失败：uv 无法访问其 Python 目录（不受信任的装入点）。这不是网络问题。" `
                -Stage "uv-tool-install" -ExitCode $result.ExitCode -Detail $detail
        } elseif (Test-IsGitMissingError $result.Output) {
            $detail += " | uv 报「Git executable not found」：本机没有 git 而安装源仍是 git+。这不是网络问题。请安装 Git for Windows（https://git-scm.com/download/win）后重跑，或改用 GitHub 归档直链作为安装源。"
            Write-Die -Message "安装失败：本机缺少 Git 且未能改走归档直链。" `
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

if ([string]::IsNullOrWhiteSpace($Source)) {
    try {
        $Source = Resolve-DefaultInstallSource
    } catch {
        Write-Die -Message "无法解析默认安装源（Release wheel）。$($_.Exception.Message)" `
            -Stage "resolve-wheel" -Detail $_.Exception.Message
    }
}

Write-Host ""
Write-Host "============================================================"
Write-Host "DoYouTrade 安装脚本 (Windows，内置 qmt-proxy)"
Write-Host "============================================================"
Write-Host ""
Ensure-Uv
Install-DoYouTrade

Write-Host ""
Write-Host "============================================================"
Write-Ok "安装完成！下一步："
Write-Host ""
Write-Host "  1. 在 PowerShell 运行：  doyoutrade" -ForegroundColor White
Write-Host "     （默认 --mode both：同进程起 DoYouTrade:8000 与内置 qmt-proxy:8001）"
Write-Host "     （若提示找不到命令，重开一个 PowerShell 窗口，或运行  uv tool update-shell  后重试）"
Write-Host "  2. 首次启动会自动打开浏览器控制台，在网页向导里选择大模型供应商并填入 API Key。"
Write-Host "  3. 登录券商 miniQMT / QMT 量化终端后，实时行情开箱即用（默认账户已自动指向本机 qmt-proxy）。"
Write-Host "  4. 浏览器打开  http://localhost:8000  即是完整控制台。"
Write-Host ""
Write-Host "只想跑 DoYouTrade 本体： doyoutrade --mode doyoutrade    只跑行情代理： doyoutrade --mode qmt-proxy"
Write-Host "默认使用本地 SQLite，零外部数据库依赖；进阶配置见 README。"
Write-Host "升级：应用内「设置 → 自动更新」，或重跑本安装脚本 / Setup.exe"
Write-Host "卸载：  uv tool uninstall doyoutrade"
Write-Host "============================================================"
Write-Host ""
