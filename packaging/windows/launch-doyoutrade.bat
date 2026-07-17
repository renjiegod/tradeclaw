@echo off
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
