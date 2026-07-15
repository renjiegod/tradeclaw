#Requires -Version 5.1
<#
.SYNOPSIS
    卸载 qmt-proxy 的 Windows 服务（install.ps1 的逆操作）。

.DESCRIPTION
    停止并删除 WinSW 托管的 qmt-proxy 服务、移除防火墙规则；默认保留 config.yml、
    日志与代码目录本身。可选删除日志与服务文件。

.PARAMETER RemoveLogs
    同时删除 logs\ 目录。

.PARAMETER RemoveServiceFiles
    同时删除 installer\service\ 下的 WinSW 可执行文件与服务 XML。

.PARAMETER NonInteractive
    非交互模式：不做任何询问，按参数执行。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File installer\uninstall.ps1
#>
[CmdletBinding()]
param(
    [switch]$RemoveLogs,
    [switch]$RemoveServiceFiles,
    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ServiceId      = "qmt-proxy"
$ServiceExeName = "qmt-proxy-service"
$FirewallRule   = "qmt-proxy-8000"

function Set-ConsoleUtf8 {
    $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    try {
        [Console]::InputEncoding = $utf8NoBom
        [Console]::OutputEncoding = $utf8NoBom
        $script:OutputEncoding = $utf8NoBom
        & chcp 65001 *> $null
    } catch {
        # 部分宿主不允许改编码，忽略
    }
}
Set-ConsoleUtf8

function Write-Step { param([string]$Text) Write-Host "`n==> $Text" -ForegroundColor Cyan }
function Write-Info { param([string]$Text) Write-Host "    $Text" }
function Write-Ok   { param([string]$Text) Write-Host "    [OK] $Text" -ForegroundColor Green }
function Write-Warn2 { param([string]$Text) Write-Host "    [警告] $Text" -ForegroundColor Yellow }
function Write-Fail { param([string]$Text) Write-Host "`n[错误] $Text" -ForegroundColor Red }

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsAdministrator)) {
    if ($PSCommandPath) {
        Write-Warn2 "删除 Windows 服务需要管理员权限，正在弹出 UAC 提权重新运行……"
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
            exit 0
        } catch {
            Write-Fail "自动提权失败：$($_.Exception.Message)。请以管理员身份运行 PowerShell 后重试。"
            exit 1
        }
    } else {
        Write-Fail "需要管理员权限。请以管理员身份打开 PowerShell 后重新运行本脚本。"
        exit 1
    }
}

$RepoDir = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$ServiceDir = Join-Path $RepoDir "installer\service"
$WinswExe = Join-Path $ServiceDir "$ServiceExeName.exe"
$ServiceXml = Join-Path $ServiceDir "$ServiceExeName.xml"

Write-Host ""
Write-Host "==============================================================" -ForegroundColor Cyan
Write-Host " qmt-proxy 卸载器（服务: $ServiceId）" -ForegroundColor Cyan
Write-Host "==============================================================" -ForegroundColor Cyan

# ── 停止并删除服务 ──────────────────────────────────────────────
Write-Step "停止并删除 Windows 服务"

$service = Get-Service -Name $ServiceId -ErrorAction SilentlyContinue
if (-not $service) {
    Write-Ok "服务 $ServiceId 不存在，无需删除"
} else {
    Write-Info "当前服务状态: $($service.Status)"
    if ((Test-Path $WinswExe) -and (Test-Path $ServiceXml)) {
        # WinSW v2 通过 exe 同名 xml 自动发现配置，命令行不传 xml 路径
        & $WinswExe stop 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Info "WinSW stop 返回非零（服务可能本就未运行），继续卸载。"
        }
        & $WinswExe uninstall 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Warn2 "WinSW uninstall 失败（退出码 $LASTEXITCODE），回退到 sc.exe 删除。"
            & sc.exe stop $ServiceId 2>&1 | Out-Null
            & sc.exe delete $ServiceId 2>&1 | Out-Null
            if ($LASTEXITCODE -ne 0) {
                Write-Fail "sc.exe delete $ServiceId 也失败了（退出码 $LASTEXITCODE）。请检查服务是否被占用（services.msc），或重启后重试。"
                exit 1
            }
        }
    } else {
        Write-Warn2 "未找到 WinSW 文件（$WinswExe），回退到 sc.exe 删除。"
        & sc.exe stop $ServiceId 2>&1 | Out-Null
        & sc.exe delete $ServiceId 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Fail "sc.exe delete $ServiceId 失败（退出码 $LASTEXITCODE）。请检查服务状态后重试。"
            exit 1
        }
    }
    Start-Sleep -Seconds 2
    if (Get-Service -Name $ServiceId -ErrorAction SilentlyContinue) {
        Write-Warn2 "服务仍在服务表中（可能处于'标记为删除'状态），重启系统后会彻底消失。"
    } else {
        Write-Ok "服务 $ServiceId 已删除"
    }
}

# ── 防火墙规则 ─────────────────────────────────────────────────
Write-Step "移除防火墙规则"
try {
    $rule = Get-NetFirewallRule -DisplayName $FirewallRule -ErrorAction SilentlyContinue
    if ($rule) {
        Remove-NetFirewallRule -DisplayName $FirewallRule
        Write-Ok "已删除防火墙规则 $FirewallRule"
    } else {
        Write-Ok "防火墙规则 $FirewallRule 不存在，跳过"
    }
} catch {
    Write-Warn2 "删除防火墙规则失败: $($_.Exception.Message)（可在'高级安全 Windows 防火墙'中手动删除）"
}

# ── 可选清理 ───────────────────────────────────────────────────
Write-Step "可选清理"

$doRemoveLogs = $RemoveLogs.IsPresent
if (-not $NonInteractive -and -not $RemoveLogs.IsPresent) {
    $answer = Read-Host "    删除日志目录 $RepoDir\logs ？[y/N]"
    $doRemoveLogs = ($answer.Trim().ToLowerInvariant() -in @("y", "yes"))
}
if ($doRemoveLogs) {
    $logDir = Join-Path $RepoDir "logs"
    if (Test-Path $logDir) {
        try {
            Remove-Item $logDir -Recurse -Force
            Write-Ok "已删除 $logDir"
        } catch {
            Write-Warn2 "删除日志目录失败: $($_.Exception.Message)（可能有句柄占用，稍后手动删除）"
        }
    } else {
        Write-Ok "日志目录不存在，跳过"
    }
} else {
    Write-Info "保留日志目录: $RepoDir\logs"
}

if ($RemoveServiceFiles.IsPresent) {
    if (Test-Path $ServiceDir) {
        try {
            Remove-Item $ServiceDir -Recurse -Force
            Write-Ok "已删除服务文件目录 $ServiceDir"
        } catch {
            Write-Warn2 "删除服务文件失败: $($_.Exception.Message)（WinSW 进程可能尚未退出，稍后手动删除）"
        }
    }
} else {
    Write-Info "保留服务文件: $ServiceDir（含 WinSW 可执行文件，便于重装）"
}

Write-Host ""
Write-Host "==============================================================" -ForegroundColor Green
Write-Host " 卸载完成" -ForegroundColor Green
Write-Host "==============================================================" -ForegroundColor Green
Write-Info "已保留（如需彻底清理请手动删除）："
Write-Info "  - 配置文件: $RepoDir\config.yml（含 API key）及其备份 config.yml.bak-*"
Write-Info "  - 配对信息: $RepoDir\installer\pairing-info.txt（含 API key）"
Write-Info "  - 虚拟环境: $RepoDir\.venv-windows"
Write-Info "  - 代码目录: $RepoDir"
Write-Info "重新安装：powershell -ExecutionPolicy Bypass -File $RepoDir\installer\install.ps1"
exit 0
