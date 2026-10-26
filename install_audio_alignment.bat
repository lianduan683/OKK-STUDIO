@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if not %errorlevel%==0 (
  echo Python launcher was not found. Install Python 3.11 or newer first.
  pause
  exit /b 1
)
echo Installing the optional NumPy high-speed alignment engine...
py -3 -m pip install --upgrade numpy
if errorlevel 1 goto :failed
echo.
echo Installation complete. Automatic audio alignment will use the high-speed engine.
pause
exit /b 0
:failed
echo.
echo Installation failed. Check your network connection and the message above.
pause
exit /b 1
