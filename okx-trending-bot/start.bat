@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
title OKX Trend-Following Bot — Web Dashboard
cd /d %~dp0

echo.
echo  ============================================
echo   OKX TREND-FOLLOWING BOT
echo   Web dashboard: http://localhost:5001
echo  ============================================
echo.
echo  Dang khoi dong...

start "" python app.py
timeout /t 3 /nobreak >nul
start "" http://localhost:5001

echo  Done! Trinh duyet da mo http://localhost:5001
echo.
echo  De TAT BOT: dong cua so "OKX Trend-Following Bot" hoac nhan Ctrl+C
pause
