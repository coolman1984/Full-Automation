$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = New-Object Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
try {
    $request = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $folder = '\FullAutomation\'
    switch ($request.operation) {
        'list' {
            $tasks = @(Get-ScheduledTask -TaskPath $folder -ErrorAction SilentlyContinue | ForEach-Object {
                $info = $_ | Get-ScheduledTaskInfo
                @{ name = $_.TaskName; state = [string]$_.State; next_run = [string]$info.NextRunTime; last_result = $info.LastTaskResult }
            })
            ConvertTo-Json -InputObject $tasks -Depth 5 -Compress
        }
        'create' {
            if ($request.name -notmatch '^[\w][\w.-]{0,79}$') { throw 'Invalid task name.' }
            $service = New-Object -ComObject 'Schedule.Service'
            try {
                $service.Connect()
                $rootFolder = $service.GetFolder('\')
                try { $taskFolder = $service.GetFolder($folder) }
                catch { $taskFolder = $rootFolder.CreateFolder('FullAutomation') }
                [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($taskFolder)
                [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($rootFolder)
            } finally { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($service) }
            $at = [DateTime]::Parse($request.at, [Globalization.CultureInfo]::InvariantCulture)
            switch ($request.frequency) {
                'daily' { $trigger = New-ScheduledTaskTrigger -Daily -At $at }
                'weekly' { $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $request.weekdays -At $at }
                'once' { if ($at -le (Get-Date)) { throw 'One-time schedule must be in the future.' }; $trigger = New-ScheduledTaskTrigger -Once -At $at }
                default { throw 'Unsupported frequency.' }
            }
            $action = New-ScheduledTaskAction -Execute $request.python -Argument $request.arguments -WorkingDirectory $request.directory
            $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
            $principal = New-ScheduledTaskPrincipal -UserId $sid -LogonType Interactive -RunLevel Limited
            $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2) -StartWhenAvailable
            $task = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description ('Full Automation workflow: ' + $request.workflow)
            Register-ScheduledTask -TaskPath $folder -TaskName $request.name -InputObject $task -Force | Out-Null
            @{ registered = $true; name = $request.name; timezone = (Get-TimeZone).Id; requires_interactive_session = $true } | ConvertTo-Json -Compress
        }
        'remove' {
            if ($request.name -notmatch '^[\w][\w.-]{0,79}$') { throw 'Invalid task name.' }
            Unregister-ScheduledTask -TaskPath $folder -TaskName $request.name -Confirm:$false
            @{ removed = $true; name = $request.name } | ConvertTo-Json -Compress
        }
        default { throw 'Unsupported scheduler operation.' }
    }
} catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }
