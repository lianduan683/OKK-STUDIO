@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if not %errorlevel%==0 (
  echo Python launcher was not found. Install Python 3.11 or newer first.
  pause
  exit /b 1
)
py -3 -m pip install --upgrade pyinstaller numpy
if not %errorlevel%==0 goto :failed
py -3 -m PyInstaller --noconfirm --clean --onefile --windowed --name RTX_Video_Render_Studio --icon "assets\app_icon.ico" --add-data "assets;assets" --hidden-import okk_cli rtx5090_exporter.py
if not %errorlevel%==0 goto :failed
copy /y "dist\RTX_Video_Render_Studio.exe" ".\RTX_Video_Render_Studio.exe" >nul
echo.
echo Build complete: RTX_Video_Render_Studio.exe
pause
exit /b 0
:failed
echo.
echo Build failed. Review the output above.
pause
exit /b 1
