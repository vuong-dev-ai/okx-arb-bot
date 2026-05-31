@echo off
chcp 65001 >nul
cd /d %~dp0
title Bot Docs

echo.
echo  ============================================
echo   Aurora Bots Docs
echo   URL: http://localhost:8765
echo  ============================================
echo.
echo  Tip: chay "python build_equity.py" truoc de cap nhat bieu do tai san.
echo.

start "" http://localhost:8765
python -m http.server 8765
