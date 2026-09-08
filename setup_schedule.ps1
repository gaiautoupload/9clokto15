$intradayAction = New-ScheduledTaskAction -Execute 'C:\Windows\System32\cmd.exe' -Argument '/d /c ""C:\ccodex\intraday_15_radar\run_intraday.bat""' -WorkingDirectory 'C:\ccodex\intraday_15_radar'
$eodAction = New-ScheduledTaskAction -Execute 'C:\Windows\System32\cmd.exe' -Argument '/d /c ""C:\ccodex\intraday_15_radar\run_eod.bat""' -WorkingDirectory 'C:\ccodex\intraday_15_radar'
$intradayTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '08:50'
$eodTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '18:15'
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 8)
Register-ScheduledTask -TaskName 'Intraday15Radar' -Action $intradayAction -Trigger $intradayTrigger -Settings $settings -Force
Register-ScheduledTask -TaskName 'Intraday15RadarEOD' -Action $eodAction -Trigger $eodTrigger -Settings $settings -Force
