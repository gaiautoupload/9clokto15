@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
"C:\Users\pokem\anaconda3\python.exe" broker_backtest.py
exit /b %errorlevel%
