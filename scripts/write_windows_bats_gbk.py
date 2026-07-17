"""Rewrite Windows .bat launchers as GBK/CP936 (no UTF-8, no chcp 65001)."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def write_gbk(path: Path, text: str) -> None:
    data = text.replace("\r\n", "\n").replace("\n", "\r\n").encode("gbk")
    path.write_bytes(data)
    print(f"wrote {path} ({len(data)} bytes, gbk)")


LAUNCH = r"""@echo off
title DoYouTrade

rem Packaging-installer shortcut target. Same logic as repo-root launcher,
rem plus a direct-path fallback when Explorer inherits a stale PATH.

set "DOYOUTRADE_CMD="

where doyoutrade >nul 2>nul
if not errorlevel 1 goto found_path
if exist "%USERPROFILE%\.local\bin\doyoutrade.exe" goto found_local
goto not_found

:found_path
set "DOYOUTRADE_CMD=doyoutrade"
goto start

:found_local
set "DOYOUTRADE_CMD=%USERPROFILE%\.local\bin\doyoutrade.exe"
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
goto start

:not_found
echo [x] 未找到 doyoutrade 命令。
echo.
echo     可能安装还没完成，或安装到了非默认位置。
echo     请重新运行一次安装程序；如果确认已装好，
echo     重启电脑后再试一次。PATH 需要重新登录才能刷新。
echo.
pause
exit /b 1

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

ROOT_LAUNCH = r"""@echo off
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

set "DOYOUTRADE_CMD="

where doyoutrade >nul 2>nul
if not errorlevel 1 goto found_path
if exist "%USERPROFILE%\.local\bin\doyoutrade.exe" goto found_local
goto not_found

:found_path
set "DOYOUTRADE_CMD=doyoutrade"
goto start

:found_local
set "DOYOUTRADE_CMD=%USERPROFILE%\.local\bin\doyoutrade.exe"
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
goto start

:not_found
echo [x] 未找到 doyoutrade 命令。
echo.
echo     请先双击 安装DoYouTrade.bat 完成安装；
echo     如果确认已经装过，重开一个新的窗口再试一次。
echo     PATH 需要新窗口才能刷新生效。
echo.
pause
exit /b 1

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
echo [i] 正在从 GitHub 下载安装脚本并运行 ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/renjiegod/doyoutrade/main/install.ps1 | iex"
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
echo     常见原因：网络无法访问 GitHub / astral.sh，或杀毒软件拦截了脚本执行。

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
