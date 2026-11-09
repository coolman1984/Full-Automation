$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) { throw 'Claude Code CLI is not installed or not on PATH.' }
$python = & (Join-Path $PSScriptRoot 'Find-Python.ps1')
& claude mcp add --scope user full-automation -- $python (Join-Path $root 'main.py') mcp
if ($LASTEXITCODE -ne 0) { throw 'Claude MCP registration failed. Inspect any existing full-automation entry before replacing it.' }
Write-Host 'Registered. Start Claude with: claude --model claude-opus-5-5'
