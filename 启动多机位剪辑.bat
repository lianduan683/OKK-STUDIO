@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 rtx5090_exporter.py --multicam
  if errorlevel 1 pause
  exit /b
)
python rtx5090_exporter.py --multicam
if errorlevel 1 pause
