@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0WorkObserver.exe" (
    start "" "%~dp0WorkObserver.exe"
    exit /b 0
)
powershell.exe -NoProfile -STA -File "%~dp0Start.ps1"
if errorlevel 1 pause
