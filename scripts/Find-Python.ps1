# Returns the absolute path of the first healthy Python 3.10+ interpreter.
# A broken install can start yet fail to find its standard library ("Could not find platform
# independent libraries"), and then it may import modules from the current folder. Each candidate
# must report a sys.prefix that holds Lib\os.py, from a neutral working directory.
$ErrorActionPreference = 'Stop'
$candidates = [System.Collections.Generic.List[string]]::new()
# No double quotes: Windows PowerShell 5.1 strips them from native command arguments.
$check = 'import os,sys; assert sys.version_info >= (3,10); assert os.path.isfile(os.path.join(sys.base_prefix,''Lib'',''os.py'')); print(sys.executable)'
if (Get-Command py.exe -ErrorAction SilentlyContinue) {
    foreach ($line in (& py.exe -0p 2>$null)) {
        if ($line -match '(?<path>[A-Za-z]:\\.+?python\.exe)\s*$') { $candidates.Add($Matches.path) }
    }
}
foreach ($command in @(Get-Command python.exe -All -ErrorAction SilentlyContinue)) { $candidates.Add($command.Source) }
$neutral = [IO.Path]::GetTempPath()
Push-Location $neutral
try {
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if ($candidate -like '*\WindowsApps\*' -or -not (Test-Path -LiteralPath $candidate)) { continue }
        # Windows PowerShell 5.1 turns native stderr into a terminating error under 'Stop'.
        $ErrorActionPreference = 'Continue'
        $output = & $candidate -I -c $check 2>&1
        $ErrorActionPreference = 'Stop'
        if ($LASTEXITCODE -eq 0 -and -not ($output | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] })) {
            $path = ($output | Select-Object -Last 1).ToString().Trim()
            if (Test-Path -LiteralPath $path) { return $path }
        }
    }
} finally { Pop-Location }
throw 'No healthy Python 3.10+ was found. Install Python from python.org; no third-party packages are needed.'
