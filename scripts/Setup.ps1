$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$python = & (Join-Path $PSScriptRoot 'Find-Python.ps1')
Write-Host "Using Python: $python"
& (Join-Path $PSScriptRoot 'Build-Windows.ps1')
& $python (Join-Path $root 'main.py') doctor
if ($LASTEXITCODE -ne 0) { throw 'Environment diagnosis failed.' }
& $python (Join-Path $root 'main.py') integrations
if ($LASTEXITCODE -ne 0) { throw 'Integration configuration generation failed.' }
Write-Host 'Ready. Run Connect-Claude.ps1 to register the tools with your existing Claude CLI.'
