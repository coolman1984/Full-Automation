param([Parameter(Mandatory=$true)][string]$WindowTitle)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$python = & (Join-Path $root 'scripts\Find-Python.ps1')
$temporary = Join-Path ([IO.Path]::GetTempPath()) ('full-automation-smoke-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temporary | Out-Null
try {
    $entry = Join-Path $root 'main.py'
    $windows = & $python $entry --data-dir $temporary call desktop_windows | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw 'Window listing failed.' }
    $matches = @($windows | Where-Object { $_.title -ceq $WindowTitle })
    if ($matches.Count -ne 1) { throw 'Open exactly one disposable window with the requested title.' }
    $inputFile = Join-Path $temporary 'window.json'
    @{window=@{hwnd=$matches[0].hwnd}} | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $inputFile -Encoding UTF8
    $snapshot = & $python $entry --data-dir $temporary call desktop_snapshot --args-file $inputFile | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0 -or $snapshot.window.title -cne $WindowTitle -or $snapshot.elements.Count -lt 1) { throw 'Window snapshot failed.' }
    $screenshot = & $python $entry --data-dir $temporary call desktop_screenshot --args-file $inputFile | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0 -or $screenshot.mimeType -ne 'image/png' -or -not $screenshot._image.StartsWith('iVBORw0KGgo')) { throw 'Screenshot failed.' }
    Write-Host 'Window listing, snapshot and PNG screenshot passed.'
} finally { Remove-Item -LiteralPath $temporary -Recurse -Force }
