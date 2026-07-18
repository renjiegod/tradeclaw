@echo off
title DoYouTrade

rem Packaging-installer shortcut target. Resolve doyoutrade via PATH,
rem default ~/.local/bin, install marker, then `uv tool dir --bin`.

set "DOYOUTRADE_CMD="

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

:not_found
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
