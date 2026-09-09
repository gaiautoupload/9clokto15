@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
"C:\Users\pokem\anaconda3\python.exe" radar.py research
if errorlevel 1 exit /b %errorlevel%
"C:\Users\pokem\anaconda3\python.exe" broker_backtest.py
if errorlevel 1 exit /b %errorlevel%
"C:\Users\pokem\anaconda3\python.exe" radar.py publish
exit /b %errorlevel%
