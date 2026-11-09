$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) { throw 'Claude Code CLI is not installed or not on PATH.' }
if (Get-Command py.exe -ErrorAction SilentlyContinue) { $python = (& py.exe -3 -c 'import sys; print(sys.executable)').Trim() }
elseif (Get-Command python.exe -ErrorAction SilentlyContinue) { $python = (& python.exe -c 'import sys; print(sys.executable)').Trim() }
else { throw 'Python 3.10+ is required.' }
& claude mcp add --scope user full-automation -- $python (Join-Path $root 'main.py') mcp
if ($LASTEXITCODE -ne 0) { throw 'Claude MCP registration failed. Inspect any existing full-automation entry before replacing it.' }
Write-Host 'Registered. Start Claude with: claude --model claude-opus-5-5'
