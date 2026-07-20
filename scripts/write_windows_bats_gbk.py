"""Rewrite Windows .bat launchers as GBK/CP936 (no UTF-8, no chcp 65001)."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def write_gbk(path: Path, text: str) -> None:
    data = text.replace("\r\n", "\n").replace("\n", "\r\n").encode("gbk")
    path.write_bytes(data)
    print(f"wrote {path} ({len(data)} bytes, gbk)")


# Shared resolve block: PATH -> default .local\\bin -> install marker -> uv tool dir --bin.
# Keep Chinese echo out of parenthesized ``if (...)`` blocks (cmd.exe CP936 shatter).
_RESOLVE = r"""set "DOYOUTRADE_CMD="

where doyoutrade >nul 2>nul
if not errorlevel 1 goto found_path
if exist "%USERPROFILE%\.local\bin\doyoutrade.exe" goto found_local
if exist "%USERPROFILE%\.doyoutrade\tool-bin-dir.txt" goto try_marker
goto try_uv_bin

:found_path
set "DOYOUTRADE_CMD=doyoutrade"
goto start

:found_local
set "DOYOUTRADE_CMD=%USERPROFILE%\.local\bin\doyoutrade.exe"
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
goto start

:try_marker
set "DOYOUTRADE_BIN_DIR="
set /p DOYOUTRADE_BIN_DIR=<"%USERPROFILE%\.doyoutrade\tool-bin-dir.txt"
if not defined DOYOUTRADE_BIN_DIR goto try_uv_bin
if exist "%DOYOUTRADE_BIN_DIR%\doyoutrade.exe" goto found_marker
goto try_uv_bin

:found_marker
set "DOYOUTRADE_CMD=%DOYOUTRADE_BIN_DIR%\doyoutrade.exe"
set "PATH=%DOYOUTRADE_BIN_DIR%;%PATH%"
goto start

:try_uv_bin
if exist "%USERPROFILE%\.local\bin\uv.exe" set "PATH=%USERPROFILE%\.local\bin;%PATH%"
where uv >nul 2>nul
if errorlevel 1 goto not_found
for /f "usebackq delims=" %%I in (`uv tool dir --bin 2^>nul`) do (
  if exist "%%I\doyoutrade.exe" (
    set "DOYOUTRADE_CMD=%%I\doyoutrade.exe"
    set "PATH=%%I;%PATH%"
    goto found_uv_bin
  )
)
goto not_found

:found_uv_bin
goto start
"""

_NOT_FOUND_PACKAGING = r""":not_found
echo [x] 未找到 doyoutrade 命令。
echo.
echo     期望位置：
echo       %USERPROFILE%\.local\bin\doyoutrade.exe
echo       （或 %USERPROFILE%\.doyoutrade\tool-bin-dir.txt 指向的目录）
echo.
echo     请检查：
echo       1. 安装向导是否报过错；若报过错请重新运行一次安装程序
echo       2. 新开 PowerShell 运行：  uv tool list
echo          列表里应出现 doyoutrade
echo       3. 若列表有、这里仍找不到，运行：  uv tool update-shell
echo          然后关掉本窗口，从开始菜单再启动一次
echo.
pause
exit /b 1
"""

_NOT_FOUND_ROOT = r""":not_found
echo [x] 未找到 doyoutrade 命令。
echo.
echo     请先双击 安装DoYouTrade.bat 完成安装。
echo.
echo     若确认已装过，新开 PowerShell 运行：  uv tool list
echo     列表里应出现 doyoutrade；没有则重新安装。
echo     有的话再运行：  uv tool update-shell
echo     然后关掉本窗口，重新双击本脚本。
echo.
pause
exit /b 1
"""

LAUNCH = (
    r"""@echo off
title DoYouTrade

rem Packaging-installer shortcut target. Resolve doyoutrade via PATH,
rem default ~/.local/bin, install marker, then `uv tool dir --bin`.

"""
    + _RESOLVE
    + "\n"
    + _NOT_FOUND_PACKAGING
    + r"""
:start
echo ============================================================
echo   正在启动 DoYouTrade ...
echo ============================================================
echo.
echo   首次启动会自动打开网页控制台，在网页里选择大模型供应商
echo   并填入 API Key 即可；这个窗口只是后台服务，不用管它。
echo.
echo   想停止服务：直接关闭这个窗口即可。Windows 上也可以从系统
echo   托盘图标里选退出 DoYouTrade。
echo ============================================================
echo.

rem Double-click has no TTY for model setup; web wizard + tray icon instead.
set "DOYOUTRADE_WEB_SETUP=1"
set "DOYOUTRADE_TRAY=1"

rem Poll localhost:8000 then open the browser once the service is ready.
start "" /min powershell -NoProfile -WindowStyle Hidden -Command ^
  "$ProgressPreference='SilentlyContinue'; for ($i=0; $i -lt 150; $i++) { try { $c = New-Object Net.Sockets.TcpClient('127.0.0.1', 8000); if ($c.Connected) { Start-Process 'http://localhost:8000'; break } } catch {}; Start-Sleep -Seconds 2 }"

"%DOYOUTRADE_CMD%"

echo.
echo ============================================================
echo   DoYouTrade 已退出。
echo ============================================================
pause
"""
)

ROOT_LAUNCH = (
    r"""@echo off
title DoYouTrade

echo ============================================================
echo   正在启动 DoYouTrade ...
echo ============================================================
echo.
echo   首次启动会自动打开网页控制台，在网页里选择大模型供应商
echo   并填入 API Key 即可；这个窗口只是后台服务，不用管它。
echo.
echo   想停止服务：直接关闭这个窗口即可。Windows 上也可以从系统
echo   托盘图标里选退出 DoYouTrade。
echo ============================================================
echo.

rem Double-click has no TTY for model setup; web wizard + tray icon instead.
set "DOYOUTRADE_WEB_SETUP=1"
set "DOYOUTRADE_TRAY=1"

"""
    + _RESOLVE
    + "\n"
    + _NOT_FOUND_ROOT
    + r"""
:start
rem Poll localhost:8000 then open the browser once the service is ready.
start "" /min powershell -NoProfile -WindowStyle Hidden -Command ^
  "$ProgressPreference='SilentlyContinue'; for ($i=0; $i -lt 150; $i++) { try { $c = New-Object Net.Sockets.TcpClient('127.0.0.1', 8000); if ($c.Connected) { Start-Process 'http://localhost:8000'; break } } catch {}; Start-Sleep -Seconds 2 }"

"%DOYOUTRADE_CMD%"

echo.
echo ============================================================
echo   DoYouTrade 已退出。
echo ============================================================
pause
"""
)

INSTALL = r"""@echo off
title DoYouTrade 安装向导

echo ============================================================
echo   DoYouTrade 一键安装 Windows
echo ============================================================
echo.
echo   这个窗口会自动完成：
echo     1. 检测 / 安装 uv Python 包管理器，自带 Python 3.12
echo     2. 把 doyoutrade 装成常驻命令，内置 qmt-proxy
echo.
echo   过程中会弹出一些安装进度信息，属于正常现象，请耐心等待。
echo ============================================================
echo.

set "SCRIPT_DIR=%~dp0"
rem Prefer install-win.ps1: ASCII wrapper that re-encodes install.ps1 for
rem Windows PowerShell 5.1 -File on Chinese Windows (CP936). Plain
rem install.ps1 is UTF-8 without BOM and breaks under -File.
set "LOCAL_INSTALL_WIN=%SCRIPT_DIR%install-win.ps1"
set "LOCAL_INSTALL=%SCRIPT_DIR%install.ps1"

if exist "%LOCAL_INSTALL_WIN%" goto install_local_win
if exist "%LOCAL_INSTALL%" goto install_local
goto install_remote

:install_local_win
echo [i] 检测到本地 install-win.ps1，使用本地脚本安装 ...
powershell -NoProfile -ExecutionPolicy Bypass -File "%LOCAL_INSTALL_WIN%"
goto after_install

:install_local
echo [i] 检测到本地 install.ps1，使用本地脚本安装 ...
powershell -NoProfile -ExecutionPolicy Bypass -File "%LOCAL_INSTALL%"
goto after_install

:install_remote
echo [i] Selecting install mirror (GitHub / Gitee) ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$m=([string]$env:DOYOUTRADE_MIRROR).Trim().ToLowerInvariant(); if($m -in @('gitee','cn','china')){$u='https://gitee.com/renjie-god/doyoutrade/raw/main/install.ps1'} elseif($m -in @('github','gh')){$u='https://raw.githubusercontent.com/renjiegod/doyoutrade/main/install.ps1'} else { try { $null=Invoke-WebRequest https://github.com/ -UseBasicParsing -TimeoutSec 3; $u='https://raw.githubusercontent.com/renjiegod/doyoutrade/main/install.ps1' } catch { $u='https://gitee.com/renjie-god/doyoutrade/raw/main/install.ps1' } }; Write-Host ('[i] downloading install.ps1 from ' + $u); irm $u | iex"
goto after_install

:after_install
set "INSTALL_RESULT=%ERRORLEVEL%"

echo.
echo ============================================================
if not "%INSTALL_RESULT%"=="0" goto install_failed
echo [OK] 安装完成！
echo.
echo   下一步：双击 启动DoYouTrade.bat 即可启动并自动打开网页控制台。
goto install_done

:install_failed
echo [x] 安装似乎失败了，退出码 %INSTALL_RESULT%，请查看上面的错误信息。
echo     常见原因：网络无法访问 GitHub / Gitee / astral.sh，或杀毒软件拦截了脚本执行。
echo     可设置环境变量 DOYOUTRADE_MIRROR=gitee 后重试（强制走 Gitee）。

:install_done
echo ============================================================
echo.
pause
"""


def main() -> None:
    write_gbk(ROOT / "packaging" / "windows" / "launch-doyoutrade.bat", LAUNCH)
    write_gbk(ROOT / "启动DoYouTrade.bat", ROOT_LAUNCH)
    write_gbk(ROOT / "安装DoYouTrade.bat", INSTALL)


if __name__ == "__main__":
    main()
