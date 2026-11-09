@echo off
cd /d "%~dp0"
set "FA_PY="
for /f "usebackq delims=" %%p in (`powershell.exe -NoProfile -File "%~dp0scripts\Find-Python.ps1"`) do set "FA_PY=%%p"
if not defined FA_PY (
  echo No healthy Python 3.10+ was found.
  pause
  exit /b 1
)
"%FA_PY%" "%~dp0scripts\showcase.py" --open %*
pause
