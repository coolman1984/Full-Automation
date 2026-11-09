$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
if (Get-Command py.exe -ErrorAction SilentlyContinue) {
    $python = (& py.exe -3 -c 'import sys; print(sys.executable)').Trim()
} elseif (Get-Command python.exe -ErrorAction SilentlyContinue) {
    $python = (& python.exe -c 'import sys; print(sys.executable)').Trim()
} else { throw 'Python 3.10+ is required. No third-party Python packages are needed.' }
if (-not $python -or -not (Test-Path -LiteralPath $python)) { throw 'A working Python installation was not found.' }
& $python -c 'import sys; assert sys.version_info >= (3,10), "Python 3.10+ required"'
if ($LASTEXITCODE -ne 0) { throw 'Python version is not supported.' }
& (Join-Path $PSScriptRoot 'Build-Windows.ps1')
& $python (Join-Path $root 'main.py') doctor
if ($LASTEXITCODE -ne 0) { throw 'Environment diagnosis failed.' }
& $python (Join-Path $root 'main.py') integrations
if ($LASTEXITCODE -ne 0) { throw 'Integration configuration generation failed.' }
Write-Host 'Ready. Run Connect-Claude.ps1 to register the tools with your existing Claude CLI.'
