@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
set "OKK_TARGET=%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility\OKK GROUP"

if not exist "davinci\OKK_Export.py" (
  echo [错误] 缺少 davinci\OKK_Export.py
  pause
  exit /b 1
)

if not exist "%OKK_TARGET%" mkdir "%OKK_TARGET%"
if errorlevel 1 goto :failed
copy /y "davinci\OKK_Export.py" "%OKK_TARGET%\OKK_Export.py" >nul
if errorlevel 1 goto :failed
> "%OKK_TARGET%\okk_app_path.txt" echo %~dp0
if errorlevel 1 goto :failed

echo.
echo 安装完成。
echo 请完全退出并重新打开 DaVinci Resolve。
echo 然后进入：工作区 ^> 脚本 ^> OKK GROUP ^> OKK_Export
echo.
pause
exit /b 0

:failed
echo.
echo 安装失败。请确认当前 Windows 账户有权写入 AppData 文件夹。
pause
exit /b 1
