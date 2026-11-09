param([switch]$SelfTest)
$ErrorActionPreference = 'Stop'
try {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw 'This application requires Windows 11.'
    }
    if ($ExecutionContext.SessionState.LanguageMode -ne 'FullLanguage') {
        throw 'Company policy blocks this application. Ask your IT team to approve it.'
    }
    Add-Type -AssemblyName System.Windows.Forms, System.Drawing, UIAutomationClient, UIAutomationTypes, WindowsBase
    $refs = @('System.dll', 'System.Core.dll', 'System.Windows.Forms.dll',
        'System.Drawing.dll', 'System.Web.Extensions.dll', 'System.Security.dll',
        [System.Windows.Automation.AutomationElement].Assembly.Location,
        [System.Windows.Automation.ControlType].Assembly.Location,
        [System.Windows.Point].Assembly.Location)
    $sources = @('Core.cs', 'Observer.cs')
    if ($SelfTest) {
        $sources += @('CoreChecks.cs', 'SelfTest.cs')
    }
    $sourcePaths = @($sources | ForEach-Object { Join-Path $PSScriptRoot $_ })
    Add-Type -Path $sourcePaths -ReferencedAssemblies $refs
    if ($SelfTest) {
        Write-Host ([WorkObserver.SelfTest]::Run()) -ForegroundColor Green
    } else {
        [WorkObserver.App]::Run()
    }
} catch {
    Write-Host ('Could not start: ' + $_.Exception.Message) -ForegroundColor Red
    Write-Host 'This launcher does not change Windows or company security policies.'
    exit 1
}
