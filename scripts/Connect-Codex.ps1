$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
if (-not (Get-Command codex -ErrorAction SilentlyContinue)) { throw 'Codex CLI is not installed or not on PATH.' }
if (Get-Command py.exe -ErrorAction SilentlyContinue) { $python = (& py.exe -3 -c 'import sys; print(sys.executable)').Trim() }
elseif (Get-Command python.exe -ErrorAction SilentlyContinue) { $python = (& python.exe -c 'import sys; print(sys.executable)').Trim() }
else { throw 'Python 3.10+ is required.' }
# Adds only the named server through Codex's own command; other Codex settings are not rewritten.
& codex mcp add full-automation -- $python (Join-Path $root 'main.py') mcp
if ($LASTEXITCODE -ne 0) { throw 'Codex MCP registration failed. Inspect any existing full-automation entry before replacing it.' }
Write-Host 'Registered. Start Codex in your project folder and ask it to call system_status.'
