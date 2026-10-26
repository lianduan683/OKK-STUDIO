@echo off
setlocal
cd /d "%~dp0"
rem Prefer the current Python source so an old EXE left by a previous build
rem cannot silently hide newly installed 5.0.8 fixes.
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 rtx5090_exporter.py
  if errorlevel 1 (
    echo.
    echo The application exited with an error. The message is shown above.
    pause
  )
  exit /b %errorlevel%
)
where python >nul 2>nul
if %errorlevel%==0 (
  python rtx5090_exporter.py
  if errorlevel 1 (
    echo.
    echo The application exited with an error. The message is shown above.
    pause
  )
  exit /b %errorlevel%
)
if exist "RTX_Video_Render_Studio.exe" (
  start "" "RTX_Video_Render_Studio.exe"
  exit /b 0
)
if exist "RTX5090_FFmpeg_Exporter.exe" (
  start "" "RTX5090_FFmpeg_Exporter.exe"
  exit /b 0
)
echo Python 3 was not found.
echo Install Python 3.11 or newer, or build the one-file EXE with build_exe.bat.
pause
