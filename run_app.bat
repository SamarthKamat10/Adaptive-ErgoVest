@echo off
rem Adaptive ErgoVest launcher for Windows. Double-click, or run from a terminal.
rem   run_app.bat                 start (DEMO mode; phones on the same Wi-Fi can connect)
rem   run_app.bat --port COM3     connect to the vest on COM3 immediately
rem   run_app.bat --local         this computer only
cd /d "%~dp0"
title Adaptive ErgoVest

if not exist ".venv\Scripts\python.exe" (
  echo Creating a private Python environment...
  py -3 -m venv .venv 2>nul || python -m venv .venv
  if errorlevel 1 (
    echo.
    echo Python 3.10 or newer was not found. Install it from python.org and tick "Add Python to PATH".
    pause & exit /b 1
  )
)
if not exist ".venv\.deps_ok" (
  echo Installing required packages ^(first run only, needs internet^)...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 ( echo Package install failed. Check your internet connection. & pause & exit /b 1 )
  echo ok> ".venv\.deps_ok"
)

".venv\Scripts\python.exe" app.py %*
echo.
echo The server stopped.
pause
