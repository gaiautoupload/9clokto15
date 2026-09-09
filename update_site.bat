@echo off
setlocal
cd /d "%~dp0"
title Extreme Stock Radar - Update Website
echo [%date% %time%] Starting full local update...
call run_eod.bat
if errorlevel 1 (
  echo Update failed. Review the message above.
  pause
  exit /b 1
)
echo [%date% %time%] Website data updated and pushed.
pause
exit /b 0
