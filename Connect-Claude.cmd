@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -File "%~dp0scripts\Connect-Claude.ps1"
pause
