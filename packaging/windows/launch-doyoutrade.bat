@echo off
chcp 65001 >nul
title DoYouTrade

rem 图形安装包生成的快捷方式跑的就是这个文件。跟仓库根的「启动DoYouTrade.bat」逻辑一致，
rem 额外加了一层直接路径兜底：安装器同一个会话里刚 `uv tool install` 完，
rem 资源管理器 / 快捷方式继承的 PATH 可能还没刷新，直接兜底到 uv 的默认 bin 目录，
rem 免得用户第一次点快捷方式就看到"找不到命令"。

set "DOYOUTRADE_CMD="

where doyoutrade >nul 2>nul
if not errorlevel 1 (
    set "DOYOUTRADE_CMD=doyoutrade"
) else if exist "%USERPROFILE%\.local\bin\doyoutrade.exe" (
    set "DOYOUTRADE_CMD=%USERPROFILE%\.local\bin\doyoutrade.exe"
    set "PATH=%USERPROFILE%\.local\bin;%PATH%"
)

if not defined DOYOUTRADE_CMD (
    echo [x] 未找到 doyoutrade 命令。
    echo.
    echo     可能安装还没完成，或安装到了非默认位置。
    echo     请重新运行一次安装程序；如果确认已装好，
    echo     重启电脑后再试一次（PATH 需要重新登录才能刷新）。
    echo.
    pause
    exit /b 1
)

echo ============================================================
echo   正在启动 DoYouTrade ...
echo ============================================================
echo.
echo   首次启动会自动打开网页控制台，在网页里选择大模型供应商
echo   并填入 API Key 即可；这个窗口只是后台服务，不用管它。
echo.
echo   想停止服务：直接关闭这个窗口即可（Windows 上也可以从系统
echo   托盘图标里选「退出 DoYouTrade」）。
echo ============================================================
echo.

rem 双击启动没有真终端可以问模型配置，交给网页首启向导处理；
rem 同时打开系统托盘图标（Windows），提供「打开控制台/退出」入口。
set "DOYOUTRADE_WEB_SETUP=1"
set "DOYOUTRADE_TRAY=1"

rem 后台轮询本机 8000 端口，服务真正就绪后再自动打开浏览器，
rem 避免首次启动时依赖构建 / 数据库迁移较慢而打开一个打不开的页面。
start "" /min powershell -NoProfile -WindowStyle Hidden -Command ^
  "$ProgressPreference='SilentlyContinue'; for ($i=0; $i -lt 150; $i++) { try { $c = New-Object Net.Sockets.TcpClient('127.0.0.1', 8000); if ($c.Connected) { Start-Process 'http://localhost:8000'; break } } catch {}; Start-Sleep -Seconds 2 }"

"%DOYOUTRADE_CMD%"

echo.
echo ============================================================
echo   DoYouTrade 已退出。
echo ============================================================
pause
