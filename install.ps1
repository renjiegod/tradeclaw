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
function Write-Die  { param($m) Write-Host "[x] $m" -ForegroundColor Red; exit 1 }

function Ensure-Uv {
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        Write-Ok "已检测到 uv ($(uv --version))"
        return
    }
    Write-Info "未检测到 uv，正在从 astral.sh 安装…"
    try {
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    } catch {
        Write-Die "uv 安装失败，请参考 https://docs.astral.sh/uv/ 手动安装后重试。"
    }
    # 让 uv 在当前会话内立即可用（安装器已写入用户环境变量，供以后的终端使用）。
    $uvBin = Join-Path $env:USERPROFILE ".local\bin"
    if (Test-Path $uvBin) { $env:Path = "$uvBin;$env:Path" }
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Die "uv 安装后仍不可用，请重开一个 PowerShell 窗口再运行本脚本。"
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

function Update-ShellPath {
    $uvBin = Join-Path $env:USERPROFILE ".local\bin"
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
        uv tool uninstall doyoutrade
        if ($LASTEXITCODE -ne 0) { Write-Die "卸载失败。" }
    }

    Write-Info "正在安装 doyoutrade[qmt-proxy]（源：$Source）…"
    Write-Info "Windows 版内置 qmt-proxy（含 xtquant），首次安装会拉取依赖并构建，可能需要几分钟。"
    # Windows 默认安装 qmt-proxy extra：内置行情 / 交易代理，`doyoutrade` 即可 --mode both 启动。
    # 用 PEP 508 direct reference（"name[extra] @ <source>"）而不是 `--from <source> "name[extra]"`：
    # uv 的 `--from` 把位置参数当作可执行名解析，PackageName 不接受 `[extra]`，会以
    # "conflicts with install request" 误拒（uv 0.10+ 已验证）。PEP 508 写法对所有 uv 版本都稳。
    # 不加 `2>&1`：uv 把进度（"Resolved N packages..."）写到 stderr，PowerShell 5.1 在
    # `$ErrorActionPreference = "Stop"` 下会把 `2>&1` 捕获的 stderr 行包成 NativeCommandError
    # 并终止脚本，明明在正常解析/下载也会被误判成失败。stderr 直接落到控制台，靠
    # `$LASTEXITCODE` 判定真正的成败。下方 `uv tool uninstall` 同理。
    uv tool install "doyoutrade[qmt-proxy] @ $Source"
    if ($LASTEXITCODE -ne 0) { Write-Die "安装失败，请检查网络 / 安装源后重试。" }

    Update-ShellPath

    # Make the new shim visible in *this* session, then verify it exists.
    # Without this, a GUI installer that immediately launches the shortcut
    # can still report "doyoutrade not found" even after a successful install.
    $uvBin = Join-Path $env:USERPROFILE ".local\bin"
    if (Test-Path $uvBin) { $env:Path = "$uvBin;$env:Path" }
    $shim = Join-Path $uvBin "doyoutrade.exe"
    if (-not (Get-Command doyoutrade -ErrorAction SilentlyContinue) -and -not (Test-Path -LiteralPath $shim)) {
        Write-Die "uv tool install 成功，但未找到 doyoutrade 命令（期望路径：$shim）。请重开终端后运行 uv tool list 排查。"
    }

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
