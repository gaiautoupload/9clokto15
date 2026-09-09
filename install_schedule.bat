@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_schedule.ps1"
if errorlevel 1 (
  echo Schedule installation failed. Please run this file as administrator.
  pause
  exit /b 1
)
echo Weekday schedules installed: 08:55 intraday, 18:15 after-hours.
pause
exit /b 0
