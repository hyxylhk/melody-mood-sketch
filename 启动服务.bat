@echo off
cd /d "%~dp0"
title Melody Mood Sketch - Server Guard
echo Starting server guard... (do NOT close this window)
echo.
".venv\Scripts\python.exe" guard.py
echo.
echo Guard exited. Press any key to close.
pause >nul
