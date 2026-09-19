@echo off
REM Double-click this to open the dashboard with a working Refresh button.
REM
REM The button needs a local server, because refreshing means fetching JPX, Yahoo
REM Japan and kabutan and redoing the peer maths in pandas - a browser cannot do
REM that on its own. This starts it and opens the page. Keep this window open while
REM you use the dashboard; closing it stops the server.

cd /d "%~dp0"
title Japan screener dashboard

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo   The virtual environment is missing.
  echo   Expected: %CD%\.venv\Scripts\python.exe
  echo.
  echo   Create it with:
  echo     python -m venv .venv
  echo     .venv\Scripts\python -m pip install -r requirements.txt "pandas<3"
  echo.
  pause
  exit /b 1
)

echo.
echo   Starting the Japan screener dashboard...
echo   Your browser will open in a moment. Close this window to stop.
echo.

".venv\Scripts\python.exe" serve.py %*

echo.
echo   Server stopped.
pause
