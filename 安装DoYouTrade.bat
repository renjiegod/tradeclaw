@echo off
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
