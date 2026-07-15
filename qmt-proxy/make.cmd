@echo off
setlocal

set "ACTION=%~1"
if "%ACTION%"=="" set "ACTION=help"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\make.ps1" -Action "%ACTION%"
exit /b %ERRORLEVEL%
