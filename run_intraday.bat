@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
"C:\Users\pokem\anaconda3\python.exe" radar.py prepare
if errorlevel 1 exit /b %errorlevel%
"C:\Users\pokem\anaconda3\python.exe" radar.py scan --publish
exit /b %errorlevel%
