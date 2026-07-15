#Requires -Version 5.1
<#
.SYNOPSIS
    qmt-proxy Windows 一键安装器。

.DESCRIPTION
    自动完成：环境检查 -> 获取代码 -> 安装 uv / Python 3.12 / 依赖（xtquant 在本机现场安装，
    不随安装器分发）-> 探测本机 miniQMT 的 userdata_mini 路径 -> 生成 config.yml（含随机 API key）
    -> 用 WinSW 注册 Windows 服务（开机自启）-> 健康检查 -> 输出 doyoutrade 配对信息。

    可重复运行（幂等）：已存在的服务会先停止并卸载再重装；已生成过的 qmtp_ API key 会被保留。

.PARAMETER InstallDir
    非本地模式（git clone / zip 下载）时代码落地目录，默认 C:\qmt-proxy。

.PARAMETER RepoUrl
    git clone 使用的仓库地址。qmt-proxy 现已并入 doyoutrade monorepo，克隆整仓后用 sparse-checkout
    只落地 qmt-proxy/ 子目录。

.PARAMETER ZipUrl
    指定 zip 下载地址时，跳过 git，直接下载解压（默认取 monorepo 的分支归档 zip，解压后只取
    qmt-proxy/ 子目录）。

.PARAMETER AppMode
    运行模式 mock / dev / prod。不指定时交互询问（默认 dev）。

.PARAMETER QmtUserdataPath
    直接指定 miniQMT 的 userdata_mini 路径，跳过自动探测。

.PARAMETER ClientId
    生成的 config.yml 中 xtquant.clients[0].client_id，默认 qmt_local。

.PARAMETER NonInteractive
    非交互模式：一律取默认值；探测不到 QMT 路径且未指定 -QmtUserdataPath 时直接报错退出。

.PARAMETER SkipHealthCheck
    跳过安装末尾的 /health/live 健康检查。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File installer\install.ps1

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -AppMode dev -QmtUserdataPath "C:\你的券商QMT交易端\userdata_mini"
#>
[CmdletBinding()]
param(
    [string]$InstallDir = "C:\qmt-proxy",
    [string]$RepoUrl = "https://github.com/renjiegod/doyoutrade.git",
    [string]$ZipUrl = "",
    [ValidateSet("", "mock", "dev", "prod")]
    [string]$AppMode = "",
    [string]$QmtUserdataPath = "",
    [string]$ClientId = "qmt_local",
    [switch]$NonInteractive,
    [switch]$SkipHealthCheck
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ============================================================================
# 常量
# ============================================================================
$PythonVersion   = "3.12"
$ServiceId       = "qmt-proxy"
$ServiceExeName  = "qmt-proxy-service"      # WinSW 要求 xml 与 exe 同名
$WinswVersion    = "v2.12.0"
$WinswUrl        = "https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe"
$WinswSha256     = "05b82d46ad331cc16bdc00de5c6332c1ef818df8ceefcd49c726553209b3a0da"
$FirewallRule    = "qmt-proxy-8000"
$HealthTimeoutSec = 120

# ============================================================================
# 控制台 UTF-8
# ============================================================================
function Set-ConsoleUtf8 {
    $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    try {
        [Console]::InputEncoding = $utf8NoBom
        [Console]::OutputEncoding = $utf8NoBom
        $script:OutputEncoding = $utf8NoBom
        & chcp 65001 *> $null
    } catch {
        # 部分宿主（如 ISE）不允许改编码，忽略即可
    }
}
Set-ConsoleUtf8

# PowerShell 5.1 下让 Invoke-WebRequest 走 TLS 1.2
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch {
    Write-Warning "无法启用 TLS 1.2：$($_.Exception.Message)"
}

# ============================================================================
# 输出辅助
# ============================================================================
function Write-Step { param([string]$Text) Write-Host "`n==> $Text" -ForegroundColor Cyan }
function Write-Info { param([string]$Text) Write-Host "    $Text" }
function Write-Ok   { param([string]$Text) Write-Host "    [OK] $Text" -ForegroundColor Green }
function Write-Warn2 { param([string]$Text) Write-Host "    [警告] $Text" -ForegroundColor Yellow }
function Write-Fail { param([string]$Text) Write-Host "`n[错误] $Text" -ForegroundColor Red }

function Read-WithDefault {
    param([string]$Prompt, [string]$Default)
    if ($NonInteractive) { return $Default }
    $value = Read-Host "$Prompt（默认: $Default）"
    if ([string]::IsNullOrWhiteSpace($value)) { return $Default }
    return $value.Trim()
}

function Confirm-YesNo {
    param([string]$Prompt, [bool]$DefaultYes = $true)
    if ($NonInteractive) { return $DefaultYes }
    $suffix = if ($DefaultYes) { "[Y/n]" } else { "[y/N]" }
    $value = Read-Host "$Prompt $suffix"
    if ([string]::IsNullOrWhiteSpace($value)) { return $DefaultYes }
    return ($value.Trim().ToLowerInvariant() -in @("y", "yes"))
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [string]$FailureHint = ""
    )
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        $hint = if ($FailureHint) { "`n提示: $FailureHint" } else { "" }
        throw "命令执行失败（退出码 $LASTEXITCODE）: $FilePath $($Arguments -join ' ')$hint"
    }
}

function Download-File {
    param([Parameter(Mandatory = $true)][string]$Url, [Parameter(Mandatory = $true)][string]$OutFile)
    Write-Info "下载: $Url"
    try {
        Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing
    } catch {
        throw "下载失败: $Url`n原因: $($_.Exception.Message)`n请检查网络（可能需要代理），或手动下载后放到 $OutFile 再重新运行本脚本。"
    }
}

# ============================================================================
# [1/8] 环境检查（Windows / PowerShell / 管理员）
# ============================================================================
Write-Host ""
Write-Host "==============================================================" -ForegroundColor Cyan
Write-Host " qmt-proxy Windows 一键安装器" -ForegroundColor Cyan
Write-Host "==============================================================" -ForegroundColor Cyan

Write-Step "[1/8] 环境检查"

if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
    Write-Fail "本安装器仅支持 Windows（xtquant / miniQMT 仅在 Windows 上运行）。"
    exit 1
}
Write-Ok "操作系统: Windows（$([System.Environment]::OSVersion.VersionString)）"
Write-Ok "PowerShell 版本: $($PSVersionTable.PSVersion)"

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsAdministrator)) {
    if ($PSCommandPath) {
        Write-Warn2 "当前不是管理员，注册 Windows 服务需要提权。正在弹出 UAC 以管理员身份重新运行……"
        $argList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ('"{0}"' -f $PSCommandPath))
        foreach ($entry in $PSBoundParameters.GetEnumerator()) {
            if ($entry.Value -is [System.Management.Automation.SwitchParameter]) {
                if ($entry.Value.IsPresent) { $argList += "-$($entry.Key)" }
            } else {
                $argList += "-$($entry.Key)"
                $argList += ('"{0}"' -f $entry.Value)
            }
        }
        try {
            Start-Process -FilePath "powershell.exe" -ArgumentList ($argList -join " ") -Verb RunAs | Out-Null
            Write-Info "已在新的管理员窗口中继续安装，本窗口可以关闭。"
            exit 0
        } catch {
            Write-Fail "自动提权被拒绝或失败：$($_.Exception.Message)"
            Write-Info "请手动【以管理员身份】打开 PowerShell 后重新运行本脚本。"
            exit 1
        }
    } else {
        Write-Fail "当前不是管理员，且脚本通过管道（irm | iex）方式运行，无法自动提权。"
        Write-Info "请【以管理员身份】打开 PowerShell 后重试；或先把脚本保存为 install.ps1 再运行："
        Write-Info '  iwr -useb <install.ps1 地址> -OutFile install.ps1'
        Write-Info '  powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1'
        exit 1
    }
}
Write-Ok "已具备管理员权限"

# ============================================================================
# [2/8] 获取代码（本地模式 > zip > git clone）
# ============================================================================
Write-Step "[2/8] 获取代码"

function Test-RepoDir {
    param([string]$Path)
    return ((Test-Path (Join-Path $Path "run.py")) -and
            (Test-Path (Join-Path $Path "pyproject.toml")) -and
            (Test-Path (Join-Path $Path "app")))
}

$RepoDir = $null

# 本地模式：脚本位于仓库的 installer/ 目录内
if ($PSCommandPath) {
    $scriptParent = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
    if ($scriptParent -and (Test-RepoDir $scriptParent)) {
        $RepoDir = $scriptParent
        Write-Ok "本地模式：使用当前仓库 $RepoDir"
    }
}

if (-not $RepoDir) {
    if ((Test-Path $InstallDir) -and (Test-RepoDir $InstallDir)) {
        $RepoDir = $InstallDir
        Write-Ok "复用已有安装目录: $RepoDir"
        Write-Info "（如需更新代码，请自行 git pull 或删除该目录后重装）"
    } elseif ($ZipUrl -or (-not (Get-Command git -ErrorAction SilentlyContinue))) {
        $effectiveZipUrl = if ($ZipUrl) { $ZipUrl } else { "https://github.com/renjiegod/doyoutrade/archive/refs/heads/main.zip" }
        Write-Info "下载 zip 安装: $effectiveZipUrl"
        if ((Test-Path $InstallDir) -and (Get-ChildItem $InstallDir -Force -ErrorAction SilentlyContinue | Select-Object -First 1)) {
            Write-Fail "目标目录 $InstallDir 已存在且非空，但不是一个 qmt-proxy 仓库。请清空该目录或用 -InstallDir 换一个目录。"
            exit 1
        }
        $zipFile = Join-Path $env:TEMP "qmt-proxy-release.zip"
        $extractDir = Join-Path $env:TEMP ("qmt-proxy-extract-" + [Guid]::NewGuid().ToString("N"))
        Download-File -Url $effectiveZipUrl -OutFile $zipFile
        Expand-Archive -Path $zipFile -DestinationPath $extractDir -Force
        # zip 可能是 qmt-proxy 独立发行包（run.py 在顶层）或 monorepo 分支归档（run.py 在
        # <repo>-<branch>/qmt-proxy/ 下），-Depth 3 覆盖两种布局。
        $runPy = Get-ChildItem -Path $extractDir -Filter "run.py" -Recurse -Depth 3 -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $runPy) {
            Write-Fail "zip 解压后没有找到 run.py，看起来不是 qmt-proxy 的发行包/monorepo 归档: $effectiveZipUrl"
            exit 1
        }
        $sourceRoot = Split-Path -Parent $runPy.FullName
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $InstallDir) | Out-Null
        Move-Item -Path $sourceRoot -Destination $InstallDir
        Remove-Item $zipFile -Force -ErrorAction SilentlyContinue
        Remove-Item $extractDir -Recurse -Force -ErrorAction SilentlyContinue
        $RepoDir = $InstallDir
        Write-Ok "代码已解压到 $RepoDir"
    } else {
        Write-Info "git clone --sparse $RepoUrl -> $InstallDir（仅落地 qmt-proxy/ 子目录）"
        if ((Test-Path $InstallDir) -and (Get-ChildItem $InstallDir -Force -ErrorAction SilentlyContinue | Select-Object -First 1)) {
            Write-Fail "目标目录 $InstallDir 已存在且非空，但不是一个 qmt-proxy 仓库。请清空该目录或用 -InstallDir 换一个目录。"
            exit 1
        }
        Invoke-Checked -FilePath "git" -Arguments @("clone", "--depth", "1", "--filter=blob:none", "--sparse", $RepoUrl, $InstallDir) `
            -FailureHint "检查网络是否可以访问 $RepoUrl；无法访问 GitHub 时可改用 -ZipUrl 指定发行包地址。"
        Push-Location $InstallDir
        try {
            Invoke-Checked -FilePath "git" -Arguments @("sparse-checkout", "set", "qmt-proxy") `
                -FailureHint "git sparse-checkout 失败；可删除 $InstallDir 后改用 -ZipUrl 重装。"
        } finally {
            Pop-Location
        }
        $RepoDir = Join-Path $InstallDir "qmt-proxy"
        Write-Ok "代码已克隆到 $RepoDir（monorepo sparse-checkout，仅含 qmt-proxy/）"
    }
}

if (-not (Test-RepoDir $RepoDir)) {
    Write-Fail "目录 $RepoDir 缺少 run.py / pyproject.toml / app，不是有效的 qmt-proxy 仓库。"
    exit 1
}

# ============================================================================
# [3/8] 安装 uv + Python 3.12 + 项目依赖（xtquant 在本机现场安装）
# ============================================================================
Write-Step "[3/8] 安装 uv / Python $PythonVersion / 项目依赖"

function Resolve-UvPath {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd) { return $cmd.Source }
    $candidates = @(
        (Join-Path $env:USERPROFILE ".local\bin\uv.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\uv\uv.exe")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) { return $candidate }
    }
    return $null
}

$UvPath = Resolve-UvPath
if (-not $UvPath) {
    Write-Info "本机未安装 uv，使用官方安装脚本安装……"
    $uvInstaller = Join-Path $env:TEMP "uv-install.ps1"
    Download-File -Url "https://astral.sh/uv/install.ps1" -OutFile $uvInstaller
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $uvInstaller
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "uv 官方安装脚本执行失败（退出码 $LASTEXITCODE）。可手动安装后重试：https://docs.astral.sh/uv/getting-started/installation/"
        exit 1
    }
    $env:Path = (Join-Path $env:USERPROFILE ".local\bin") + ";" + $env:Path
    $UvPath = Resolve-UvPath
    if (-not $UvPath) {
        Write-Fail "uv 安装完成但未找到 uv.exe。请重新打开 PowerShell 或手动把 uv 加入 PATH 后重试。"
        exit 1
    }
}
Write-Ok "uv: $UvPath"

Write-Info "安装 Python $PythonVersion（uv 托管，已装则跳过）……"
Invoke-Checked -FilePath $UvPath -Arguments @("python", "install", $PythonVersion)

# 与 scripts/make.ps1 保持一致：Windows 侧使用独立的 .venv-windows，避免与共享目录里的
# Unix 风格 .venv 冲突。
$VenvDir = Join-Path $RepoDir ".venv-windows"
$env:UV_PROJECT_ENVIRONMENT = $VenvDir

Write-Info "同步项目依赖（含 xtquant，从 pyproject 配置的镜像现场安装到本机，不做二次分发）……"
Push-Location $RepoDir
try {
    Invoke-Checked -FilePath $UvPath -Arguments @("sync", "--no-install-project", "--python", $PythonVersion) `
        -FailureHint "依赖安装失败时优先检查网络与镜像可达性（pyproject.toml 已配置清华镜像）。"
} finally {
    Pop-Location
}

$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Fail "uv sync 完成但没有找到 $VenvPython，虚拟环境创建异常。"
    exit 1
}
Write-Ok "虚拟环境: $VenvDir"

# ============================================================================
# [4/8] 探测本机 miniQMT 的 userdata_mini 路径
# ============================================================================
Write-Step "[4/8] 探测 QMT 安装路径（userdata_mini）"

$QmtKeywordPattern = "QMT|迅投|miniQmt|国金|东莞|中泰|国盛|华鑫|东吴"

function Find-UserdataMini {
    param([string]$BaseDir)
    $found = @()
    if (-not (Test-Path $BaseDir)) { return $found }
    try {
        $direct = Join-Path $BaseDir "userdata_mini"
        if (Test-Path $direct -PathType Container) { $found += (Resolve-Path $direct).Path }
        $nested = Get-ChildItem -Path $BaseDir -Directory -Filter "userdata_mini" -Recurse -Depth 2 -ErrorAction SilentlyContinue
        foreach ($dir in @($nested)) { $found += $dir.FullName }
    } catch {
        Write-Verbose "扫描 $BaseDir 失败: $($_.Exception.Message)"
    }
    return $found
}

function Find-QmtCandidates {
    $baseDirs = New-Object System.Collections.Generic.List[string]

    # 1) 注册表卸载项（HKLM 64/32 位 + HKCU）
    $uninstallRoots = @(
        "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
        "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    )
    foreach ($root in $uninstallRoots) {
        if (-not (Test-Path $root)) { continue }
        $items = Get-ChildItem $root -ErrorAction SilentlyContinue
        foreach ($item in @($items)) {
            $props = Get-ItemProperty -Path $item.PSPath -ErrorAction SilentlyContinue
            if (-not $props) { continue }
            $displayName = ""
            if ($props.PSObject.Properties.Name -contains "DisplayName") { $displayName = [string]$props.DisplayName }
            if (-not $displayName -or ($displayName -notmatch $QmtKeywordPattern)) { continue }
            foreach ($propName in @("InstallLocation", "DisplayIcon", "UninstallString")) {
                if ($props.PSObject.Properties.Name -notcontains $propName) { continue }
                $raw = [string]$props.$propName
                if (-not $raw) { continue }
                $raw = $raw.Trim('"')
                $dir = $raw
                if ($raw -match '\.(exe|ico),?\d*$' -or (Test-Path $raw -PathType Leaf)) {
                    $dir = Split-Path -Parent ($raw -replace ',\d+$', '')
                }
                if ($dir -and (Test-Path $dir)) { [void]$baseDirs.Add($dir) }
            }
        }
    }

    # 2) 各盘符根目录 + Program Files 下的常见目录名（*QMT* / *迅投*）
    $drives = Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue |
        Where-Object { $_.Root -match '^[A-Z]:\\$' }
    foreach ($drive in @($drives)) {
        $scanRoots = @($drive.Root)
        foreach ($pf in @("Program Files", "Program Files (x86)")) {
            $pfPath = Join-Path $drive.Root $pf
            if (Test-Path $pfPath) { $scanRoots += $pfPath }
        }
        foreach ($scanRoot in $scanRoots) {
            try {
                # 根目录第一层 + 第二层（覆盖 C:\quant\你的券商QMT模拟端 这类布局）
                $dirs = Get-ChildItem -Path $scanRoot -Directory -Depth 1 -ErrorAction SilentlyContinue |
                    Where-Object { $_.Name -match "QMT|迅投" }
                foreach ($dir in @($dirs)) { [void]$baseDirs.Add($dir.FullName) }
            } catch {
                Write-Verbose "扫描 $scanRoot 失败: $($_.Exception.Message)"
            }
        }
    }

    # 基于候选安装目录查找 userdata_mini，去重
    $results = New-Object System.Collections.Generic.List[string]
    $seen = @{}
    foreach ($baseDir in $baseDirs) {
        foreach ($path in @(Find-UserdataMini -BaseDir $baseDir)) {
            $key = $path.ToLowerInvariant().TrimEnd("\")
            if (-not $seen.ContainsKey($key)) {
                $seen[$key] = $true
                [void]$results.Add($path)
            }
        }
    }
    return @($results | Sort-Object)
}

if ($QmtUserdataPath) {
    if (-not (Test-Path $QmtUserdataPath -PathType Container)) {
        Write-Fail "-QmtUserdataPath 指定的目录不存在: $QmtUserdataPath"
        exit 1
    }
    Write-Ok "使用参数指定的 QMT 路径: $QmtUserdataPath"
} else {
    Write-Info "扫描注册表卸载项与各磁盘常见目录（关键词: QMT / 迅投 / 券商名）……"
    $candidates = @(Find-QmtCandidates)
    if ($candidates.Count -eq 0) {
        Write-Warn2 "未自动探测到 miniQMT 的 userdata_mini 目录。"
        Write-Info "常见位置示例: C:\你的券商QMT交易端\userdata_mini、C:\你的券商QMT交易端\userdata_mini"
        if ($NonInteractive) {
            Write-Fail "非交互模式下无法手动输入路径。请用 -QmtUserdataPath 指定后重试。"
            exit 1
        }
        while ($true) {
            $manual = Read-Host "请输入本机 miniQMT 的 userdata_mini 完整路径"
            if ([string]::IsNullOrWhiteSpace($manual)) { Write-Warn2 "路径不能为空。"; continue }
            $manual = $manual.Trim('"').Trim()
            if (Test-Path $manual -PathType Container) { $QmtUserdataPath = $manual; break }
            Write-Warn2 "目录不存在: $manual，请确认后重新输入（可在 QMT 安装目录下找 userdata_mini 子目录）。"
        }
    } elseif ($candidates.Count -eq 1) {
        Write-Ok "探测到 1 个候选: $($candidates[0])"
        if (Confirm-YesNo -Prompt "    使用该路径？" -DefaultYes $true) {
            $QmtUserdataPath = $candidates[0]
        } else {
            $manual = Read-Host "请输入 userdata_mini 完整路径"
            $manual = $manual.Trim('"').Trim()
            if (-not (Test-Path $manual -PathType Container)) { Write-Fail "目录不存在: $manual"; exit 1 }
            $QmtUserdataPath = $manual
        }
    } else {
        Write-Info "探测到 $($candidates.Count) 个候选路径："
        for ($i = 0; $i -lt $candidates.Count; $i++) {
            Write-Host ("      [{0}] {1}" -f ($i + 1), $candidates[$i])
        }
        if ($NonInteractive) {
            $QmtUserdataPath = $candidates[0]
            Write-Warn2 "非交互模式：自动选择第 1 个候选 $QmtUserdataPath"
        } else {
            while ($true) {
                $choice = Read-Host "请输入编号选择（1-$($candidates.Count)），或直接粘贴其他路径"
                $choice = $choice.Trim().Trim('"')
                $index = 0
                if ([int]::TryParse($choice, [ref]$index) -and $index -ge 1 -and $index -le $candidates.Count) {
                    $QmtUserdataPath = $candidates[$index - 1]
                    break
                }
                if (Test-Path $choice -PathType Container) { $QmtUserdataPath = $choice; break }
                Write-Warn2 "无效输入：既不是有效编号也不是存在的目录，请重试。"
            }
        }
    }
    Write-Ok "QMT userdata_mini: $QmtUserdataPath"
}

# ============================================================================
# [5/8] 生成配置（config.yml + 随机 API key）
# ============================================================================
Write-Step "[5/8] 生成 config.yml 与 API key"

if (-not $AppMode) {
    Write-Info "运行模式说明: mock=不连QMT纯模拟 / dev=连QMT取真实数据、拦截交易 / prod=允许真实交易"
    $AppMode = Read-WithDefault -Prompt "    选择运行模式 mock/dev/prod" -Default "dev"
    if ($AppMode -notin @("mock", "dev", "prod")) {
        Write-Warn2 "无效模式 '$AppMode'，回退为 dev。"
        $AppMode = "dev"
    }
}
if ($AppMode -eq "prod" -and -not $NonInteractive) {
    Write-Warn2 "prod 模式允许【真实下单】，资金账户将真实成交！"
    $confirmProd = Read-Host "    输入 yes 确认启用 prod（其他任意输入回退为 dev）"
    if ($confirmProd.Trim().ToLowerInvariant() -ne "yes") {
        Write-Info "已回退为 dev 模式。"
        $AppMode = "dev"
    }
}
Write-Ok "运行模式: $AppMode"

function New-RandomHex {
    param([int]$Bytes = 32)
    $buffer = New-Object byte[] $Bytes
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($buffer) } finally { $rng.Dispose() }
    return (-join ($buffer | ForEach-Object { $_.ToString("x2") }))
}

$CandidateApiKey = "qmtp_" + (New-RandomHex -Bytes 32)
$CandidateSecret = New-RandomHex -Bytes 32
$AllowRealTrading = if ($AppMode -eq "prod") { "1" } else { "0" }

$ConfigPath = Join-Path $RepoDir "config.yml"
$ReplaceClients = "1"
if (Test-Path $ConfigPath) {
    $backupPath = Join-Path $RepoDir ("config.yml.bak-" + (Get-Date -Format "yyyyMMddHHmmss"))
    Copy-Item -Path $ConfigPath -Destination $backupPath -Force
    Write-Ok "已备份现有配置到: $backupPath"
    if (-not (Confirm-YesNo -Prompt "    将 xtquant.clients 替换为本次探测到的终端（$QmtUserdataPath）？选 n 则保留现有 clients" -DefaultYes $true)) {
        $ReplaceClients = "0"
    }
} else {
    Write-Warn2 "仓库中没有 config.yml 模板，将无法生成配置。请确认代码完整后重试。"
    exit 1
}

# 用仓库自带的 PyYAML（uv sync 已装好）做结构化合并，避免手工文本替换破坏 YAML。
# 注意：PyYAML 重写文件不保留注释；原始注释请查阅上面备份的 config.yml.bak-*。
$PatchScript = @'
import argparse
import json
import sys

import yaml


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--mode", required=True, choices=["mock", "dev", "prod"])
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--secret-key", required=True)
    parser.add_argument("--qmt-path", required=True)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--allow-real-trading", required=True, choices=["0", "1"])
    parser.add_argument("--replace-clients", required=True, choices=["0", "1"])
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise SystemExit(f"config.yml 顶层必须是映射, 实际是 {type(data).__name__}: {data!r}")

    modes = data.get("modes")
    if not isinstance(modes, dict) or args.mode not in modes:
        raise SystemExit(f"config.yml 缺少 modes.{args.mode} 段, 无法写入 API key")
    mode_cfg = modes[args.mode]
    if not isinstance(mode_cfg, dict):
        raise SystemExit(
            f"modes.{args.mode} 必须是映射, 实际是 {type(mode_cfg).__name__}: {mode_cfg!r}"
        )

    existing_keys = mode_cfg.get("api_keys") or []
    if not isinstance(existing_keys, list):
        raise SystemExit(
            f"modes.{args.mode}.api_keys 必须是列表, 实际是 "
            f"{type(existing_keys).__name__}: {existing_keys!r}"
        )
    generated = [k for k in existing_keys if isinstance(k, str) and k.startswith("qmtp_")]
    if generated:
        api_key = generated[0]
        api_key_rotated = False
    else:
        api_key = args.api_key
        mode_cfg["api_keys"] = [api_key]
        api_key_rotated = True

    xt = data.setdefault("xtquant", {})
    if not isinstance(xt, dict):
        raise SystemExit(f"xtquant 段必须是映射, 实际是 {type(xt).__name__}: {xt!r}")
    clients_replaced = False
    if args.replace_clients == "1" or not xt.get("clients"):
        xt["clients"] = [
            {
                "client_id": args.client_id,
                "name": "本机QMT终端",
                "qmt_userdata_path": args.qmt_path,
                "mode": args.mode,
                "allow_real_trading": args.allow_real_trading == "1",
                "is_data_source": True,
            }
        ]
        xt["default_client_id"] = args.client_id
        xt["data_source_client_id"] = args.client_id
        clients_replaced = True

    security = data.setdefault("security", {})
    secret_rotated = False
    if isinstance(security, dict):
        current = security.get("secret_key")
        if current in (None, "", "change-this-to-secure-key-in-production"):
            security["secret_key"] = args.secret_key
            secret_rotated = True

    with open(args.config, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False, default_flow_style=False)

    port = 8000
    if isinstance(mode_cfg.get("port"), int):
        port = mode_cfg["port"]

    print(
        json.dumps(
            {
                "api_key": api_key,
                "api_key_rotated": api_key_rotated,
                "clients_replaced": clients_replaced,
                "secret_key_rotated": secret_rotated,
                "port": port,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
'@

$patchScriptPath = Join-Path $env:TEMP "qmt-proxy-patch-config.py"
[System.IO.File]::WriteAllText($patchScriptPath, $PatchScript, [System.Text.UTF8Encoding]::new($false))

$patchArgs = @(
    $patchScriptPath,
    "--config", $ConfigPath,
    "--mode", $AppMode,
    "--api-key", $CandidateApiKey,
    "--secret-key", $CandidateSecret,
    "--qmt-path", $QmtUserdataPath,
    "--client-id", $ClientId,
    "--allow-real-trading", $AllowRealTrading,
    "--replace-clients", $ReplaceClients
)
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$patchOutput = & $VenvPython @patchArgs
if ($LASTEXITCODE -ne 0) {
    Write-Fail "config.yml 生成失败（退出码 $LASTEXITCODE）。上方输出即失败原因；原配置已备份，可从备份恢复。"
    exit 1
}
$patchJsonLine = @($patchOutput | Where-Object { $_ -and $_.ToString().Trim().StartsWith("{") }) | Select-Object -Last 1
if (-not $patchJsonLine) {
    Write-Fail "config.yml 生成脚本没有返回结果 JSON，输出为: $patchOutput"
    exit 1
}
$patchResult = $patchJsonLine | ConvertFrom-Json
$ApiKey = $patchResult.api_key
$ApiPort = [int]$patchResult.port

if ($patchResult.api_key_rotated) {
    Write-Ok "已为 $AppMode 模式生成新 API key（qmtp_ 前缀 + 64 位随机 hex）"
} else {
    Write-Ok "检测到 $AppMode 模式已有 qmtp_ API key，保留不变（幂等重装）"
}
if ($patchResult.clients_replaced) {
    Write-Ok "xtquant.clients 已写入本机终端（client_id=$ClientId, path=$QmtUserdataPath）"
} else {
    Write-Ok "按选择保留了现有 xtquant.clients"
}
if ($patchResult.secret_key_rotated) {
    Write-Ok "security.secret_key 已替换默认占位值为随机值"
}
Write-Warn2 "注意：config.yml 由 PyYAML 重写，原文件注释未保留（完整原文见备份文件）。"

# ============================================================================
# [6/8] 注册 Windows 服务（WinSW，开机自启）
# ============================================================================
Write-Step "[6/8] 注册 Windows 服务（WinSW $WinswVersion）"

$ServiceDir = Join-Path $RepoDir "installer\service"
New-Item -ItemType Directory -Force -Path $ServiceDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $RepoDir "logs") | Out-Null

$WinswExe = Join-Path $ServiceDir "$ServiceExeName.exe"
$needDownload = $true
if (Test-Path $WinswExe) {
    $existingHash = (Get-FileHash -Path $WinswExe -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($existingHash -eq $WinswSha256) {
        Write-Ok "WinSW 已存在且哈希校验通过，跳过下载"
        $needDownload = $false
    } else {
        Write-Warn2 "已存在的 WinSW 哈希不匹配（$existingHash），重新下载"
        Remove-Item $WinswExe -Force
    }
}
if ($needDownload) {
    Download-File -Url $WinswUrl -OutFile $WinswExe
    $actualHash = (Get-FileHash -Path $WinswExe -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $WinswSha256) {
        Remove-Item $WinswExe -Force -ErrorAction SilentlyContinue
        Write-Fail "WinSW 下载文件 SHA-256 校验失败！`n    期望: $WinswSha256`n    实际: $actualHash`n下载可能被劫持或损坏，已删除该文件。请检查网络后重试。"
        exit 1
    }
    Write-Ok "WinSW 下载完成，SHA-256 校验通过"
}

function ConvertTo-XmlEscaped {
    param([string]$Value)
    return [System.Security.SecurityElement]::Escape($Value)
}

$xmlPython  = ConvertTo-XmlEscaped $VenvPython
$xmlRepoDir = ConvertTo-XmlEscaped $RepoDir
$xmlLogDir  = ConvertTo-XmlEscaped (Join-Path $RepoDir "logs")
$xmlMode    = ConvertTo-XmlEscaped $AppMode

# 服务直接用 .venv-windows 里的 python 启动 run.py（依赖已由 uv sync 装好），
# 不在服务运行期调用 uv：LocalSystem 账户下 uv 会找不到当前用户的托管 Python。
$serviceXml = @"
<service>
  <id>$ServiceId</id>
  <name>QMT Proxy (qmt-proxy)</name>
  <description>qmt-proxy: FastAPI/gRPC 代理服务，封装本机 miniQMT xtquant 数据与交易接口。APP_MODE=$xmlMode</description>
  <executable>$xmlPython</executable>
  <arguments>run.py</arguments>
  <workingdirectory>$xmlRepoDir</workingdirectory>
  <env name="APP_MODE" value="$xmlMode"/>
  <env name="PYTHONUTF8" value="1"/>
  <env name="PYTHONIOENCODING" value="utf-8"/>
  <startmode>Automatic</startmode>
  <onfailure action="restart" delay="10 sec"/>
  <stoptimeout>20 sec</stoptimeout>
  <logpath>$xmlLogDir</logpath>
  <log mode="roll-by-size">
    <sizeThreshold>10240</sizeThreshold>
    <keepFiles>8</keepFiles>
  </log>
</service>
"@
$serviceXmlPath = Join-Path $ServiceDir "$ServiceExeName.xml"
[System.IO.File]::WriteAllText($serviceXmlPath, $serviceXml, [System.Text.UTF8Encoding]::new($false))
Write-Ok "服务描述文件: $serviceXmlPath"

# 幂等：已存在同名服务时先 stop + uninstall 再装
# WinSW v2 通过 exe 同名的 xml（qmt-proxy-service.xml）自动发现配置，命令行不传 xml 路径
$existingService = Get-Service -Name $ServiceId -ErrorAction SilentlyContinue
if ($existingService) {
    Write-Info "检测到已安装的服务 $ServiceId（状态: $($existingService.Status)），先停止并卸载……"
    & $WinswExe stop 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Info "WinSW stop 未成功（服务可能本就未运行），继续卸载。"
    }
    & $WinswExe uninstall 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        # 兜底：可能是旧方式注册的服务，尝试 sc 删除
        Write-Warn2 "WinSW uninstall 失败，尝试 sc.exe delete $ServiceId"
        & sc.exe stop $ServiceId 2>&1 | Out-Null
        & sc.exe delete $ServiceId 2>&1 | Out-Null
    }
    Start-Sleep -Seconds 2
    Write-Ok "旧服务已卸载"
}

Write-Info "安装并启动服务 $ServiceId ……"
Invoke-Checked -FilePath $WinswExe -Arguments @("install") `
    -FailureHint "若提示服务已存在，重新运行本脚本即可自动先卸载再安装。"
Invoke-Checked -FilePath $WinswExe -Arguments @("start") `
    -FailureHint "查看 $ServiceDir\$ServiceExeName.wrapper.log 与 $RepoDir\logs\ 下的日志排查启动失败原因。"
Write-Ok "服务已安装并启动（开机自启：Automatic）"

# 防火墙放行（局域网内 doyoutrade 访问需要）
if (Confirm-YesNo -Prompt "    在 Windows 防火墙放行 TCP $ApiPort（局域网内其他机器接入需要）？" -DefaultYes $true) {
    try {
        $existingRule = Get-NetFirewallRule -DisplayName $FirewallRule -ErrorAction SilentlyContinue
        if ($existingRule) {
            Write-Ok "防火墙规则 $FirewallRule 已存在，跳过"
        } else {
            New-NetFirewallRule -DisplayName $FirewallRule -Direction Inbound -Action Allow `
                -Protocol TCP -LocalPort $ApiPort -Profile Any | Out-Null
            Write-Ok "已添加入站规则 $FirewallRule（TCP $ApiPort）"
        }
    } catch {
        Write-Warn2 "添加防火墙规则失败: $($_.Exception.Message)。局域网接入前请手动放行 TCP $ApiPort。"
    }
} else {
    Write-Info "已跳过防火墙配置。仅本机 127.0.0.1 访问不受影响；局域网接入前请手动放行 TCP $ApiPort。"
}

# ============================================================================
# [7/8] 健康检查
# ============================================================================
Write-Step "[7/8] 健康检查"

$HealthUrl = "http://127.0.0.1:$ApiPort/health/live"
$healthOk = $false
if ($SkipHealthCheck) {
    Write-Warn2 "按参数跳过健康检查（-SkipHealthCheck）。"
} else {
    Write-Info "等待服务就绪: GET $HealthUrl（最长 $HealthTimeoutSec 秒）……"
    $deadline = (Get-Date).AddSeconds($HealthTimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $resp = Invoke-WebRequest -Uri $HealthUrl -UseBasicParsing -TimeoutSec 5
            if ($resp.StatusCode -eq 200) { $healthOk = $true; break }
        } catch {
            # 服务尚未就绪，继续等待
        }
        Start-Sleep -Seconds 2
    }
    if ($healthOk) {
        Write-Ok "健康检查通过: $HealthUrl 返回 200"
    } else {
        Write-Warn2 "健康检查失败：$HealthTimeoutSec 秒内 $HealthUrl 未返回 200。"
        Write-Info "排查步骤："
        Write-Info "  1) 服务状态: Get-Service $ServiceId；WinSW 包装日志: $ServiceDir\$ServiceExeName.wrapper.log"
        Write-Info "  2) 应用日志: $RepoDir\logs\$ServiceExeName.out.log / .err.log 以及 logs\app.log、logs\error.log"
        Write-Info "  3) dev/prod 模式要求本机 miniQMT 客户端已登录运行；可先用 mock 模式验证链路："
        Write-Info "     powershell -ExecutionPolicy Bypass -File installer\install.ps1 -AppMode mock"
        Write-Info "  4) 检查端口占用: Get-NetTCPConnection -LocalPort $ApiPort -State Listen"
    }
}

# ============================================================================
# [8/8] 配对信息输出
# ============================================================================
Write-Step "[8/8] 配对信息"

function Get-LanIPv4 {
    try {
        $config = Get-NetIPConfiguration -ErrorAction SilentlyContinue |
            Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq "Up" } |
            Select-Object -First 1
        if ($config -and $config.IPv4Address) {
            return @($config.IPv4Address)[0].IPAddress
        }
    } catch {
        Write-Verbose "Get-NetIPConfiguration 失败: $($_.Exception.Message)"
    }
    try {
        $addr = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
            Select-Object -First 1
        if ($addr) { return $addr.IPAddress }
    } catch {
        Write-Verbose "Get-NetIPAddress 失败: $($_.Exception.Message)"
    }
    return $null
}

$LanIp = Get-LanIPv4
$LanBaseUrl = if ($LanIp) { "http://${LanIp}:$ApiPort" } else { "（未探测到局域网 IP，可用 ipconfig 查看后自行拼接 http://<ip>:$ApiPort）" }
$LocalBaseUrl = "http://127.0.0.1:$ApiPort"
$healthNote = if ($SkipHealthCheck) { "已跳过" } elseif ($healthOk) { "通过" } else { "未通过（见上方排查步骤）" }

$pairingLines = @(
    "# qmt-proxy 配对信息",
    "# 生成时间: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')",
    "# 注意: 本文件包含 API key（等同访问凭证），请勿提交到 git 或对外分享。",
    "",
    "运行模式 (APP_MODE): $AppMode",
    "服务名称           : $ServiceId（开机自启，WinSW $WinswVersion 托管）",
    "QMT userdata 路径  : $QmtUserdataPath",
    "健康检查           : $healthNote",
    "",
    "base_url（本机）   : $LocalBaseUrl",
    "base_url（局域网） : $LanBaseUrl",
    "gRPC 地址          : $(if ($LanIp) { "${LanIp}:50051" } else { "<本机IP>:50051" })（防火墙仅放行了 $ApiPort，如需局域网 gRPC 请另行放行 50051）",
    "API key            : $ApiKey",
    "认证方式           : HTTP 头 Authorization: Bearer $ApiKey",
    "",
    "── doyoutrade 侧接入方法 ──────────────────────────────────────",
    "在 doyoutrade 所在机器上执行（资金账号替换为你的 QMT 资金账号）：",
    "",
    "  doyoutrade-cli account create ``",
    "    --name qmt-windows ``",
    "    --mode live ``",
    "    --base-url $(if ($LanIp) { $LanBaseUrl } else { "http://<本机IP>:$ApiPort" }) ``",
    "    --token $ApiKey ``",
    "    --qmt-account-id <你的资金账号>",
    "",
    "也可以在 doyoutrade 前端的 Accounts 页面手动填写以上 base_url / token / 资金账号。",
    "",
    "── 服务管理速查 ──────────────────────────────────────────────",
    "状态   : Get-Service $ServiceId",
    "重启   : Restart-Service $ServiceId",
    "停止   : Stop-Service $ServiceId",
    "日志   : $RepoDir\logs\（app.log / error.log / $ServiceExeName.out.log / $ServiceExeName.err.log）",
    "卸载   : powershell -ExecutionPolicy Bypass -File $RepoDir\installer\uninstall.ps1"
)

$pairingPath = Join-Path $RepoDir "installer\pairing-info.txt"
[System.IO.File]::WriteAllText($pairingPath, ($pairingLines -join [Environment]::NewLine) + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($true))

Write-Host ""
Write-Host "==============================================================" -ForegroundColor Green
Write-Host " 安装完成！配对信息如下（已保存到 $pairingPath）" -ForegroundColor Green
Write-Host "==============================================================" -ForegroundColor Green
foreach ($line in $pairingLines) {
    if ($line.StartsWith("#")) { continue }
    Write-Host $line
}
Write-Host ""
Write-Warn2 "pairing-info.txt 含 API key，请妥善保管，不要提交到 git。"

if (-not $SkipHealthCheck -and -not $healthOk) {
    Write-Fail "服务已注册，但健康检查未通过。请按上面的排查步骤处理后，用 Restart-Service $ServiceId 重启验证。"
    exit 1
}
exit 0
