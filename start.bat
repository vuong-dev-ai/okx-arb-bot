@echo off
title OKX Arb Bot — Web Dashboard
cd /d %~dp0

echo.
echo  ============================================
echo   OKX FUNDING ARB BOT
echo   Web dashboard: http://localhost:5000
echo  ============================================
echo.
echo  Dang khoi dong...

:: Khoi dong Flask (web dashboard + bot controller)
start "" python app.py

:: Cho Flask khoi dong xong
timeout /t 3 /nobreak >nul

:: Mo trinh duyet tu dong
start "" http://localhost:5000

echo  Done! Trinh duyet da mo http://localhost:5000
echo.
echo  De TAT BOT: dong cua so "OKX Arb Bot" hoac nhan Ctrl+C
pause
