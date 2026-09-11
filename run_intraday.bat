@echo off
setlocal
title 9clokto15 盤中盯盤（請勿關閉）
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"
echo [%date% %time%] 正在準備主力資料...
"C:\Users\pokem\anaconda3\python.exe" radar.py prepare
if errorlevel 1 exit /b %errorlevel%
echo [%date% %time%] 盤中盯盤已啟動，這個視窗可縮小但請勿關閉。
"C:\Users\pokem\anaconda3\python.exe" radar.py scan --publish
exit /b %errorlevel%
