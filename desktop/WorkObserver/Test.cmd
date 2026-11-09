@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -STA -File "%~dp0Start.ps1" -SelfTest
pause
