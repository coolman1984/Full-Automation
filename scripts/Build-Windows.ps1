param([switch]$Test)
$ErrorActionPreference = 'Stop'
# The compiler and UI Automation references belong to .NET Framework. PowerShell 7 runs on .NET
# (Core), which has neither, so hand the build to Windows PowerShell 5.1.
if ($PSVersionTable.PSEdition -eq 'Core') {
    $legacy = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if (-not (Test-Path -LiteralPath $legacy)) { throw 'Windows PowerShell 5.1 is required to build the Windows components.' }
    $forward = @('-NoProfile', '-File', $PSCommandPath)
    if ($Test) { $forward += '-Test' }
    & $legacy @forward
    if ($LASTEXITCODE -ne 0) { throw 'Windows component build failed.' }
    return
}
$root = Split-Path $PSScriptRoot -Parent
$framework = [Runtime.InteropServices.RuntimeEnvironment]::GetRuntimeDirectory()
$compiler = Join-Path $framework 'csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) { throw 'The Windows .NET Framework compiler was not found.' }
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, WindowsBase
$references = @('System.dll', 'System.Core.dll', 'System.Windows.Forms.dll', 'System.Drawing.dll', 'System.Web.Extensions.dll', 'System.Security.dll',
    [System.Windows.Automation.AutomationElement].Assembly.Location,
    [System.Windows.Automation.ControlType].Assembly.Location,
    [System.Windows.Point].Assembly.Location) | Select-Object -Unique
$common = @('/nologo', '/optimize+', '/platform:anycpu', '/langversion:5', '/codepage:65001')
$common += @($references | ForEach-Object { '/reference:' + $_ })
$sources = @((Join-Path $root 'desktop\WorkObserver\Core.cs'), (Join-Path $root 'desktop\WorkObserver\Observer.cs'))
New-Item -ItemType Directory -Force (Join-Path $root 'bin') | Out-Null
& $compiler @common '/target:exe' '/main:WorkObserver.WindowsBridge' ('/out:' + (Join-Path $root 'bin\WindowsBridge.exe')) @sources (Join-Path $root 'desktop\bridge\WindowsBridge.cs')
if ($LASTEXITCODE -ne 0) { throw 'Windows bridge compilation failed.' }
& $compiler @common '/target:winexe' '/main:WorkObserver.App' ('/out:' + (Join-Path $root 'desktop\WorkObserver\WorkObserver.exe')) @sources
if ($LASTEXITCODE -ne 0) { throw 'Recorder compilation failed.' }
if ($Test) {
    & (Join-Path $root 'desktop\WorkObserver\Start.ps1') -SelfTest
    if ($LASTEXITCODE -ne 0) { throw 'Windows self-test failed.' }
}
Write-Host 'Windows bridge and recorder built successfully.'
