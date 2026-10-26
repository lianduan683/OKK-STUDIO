@echo off
setlocal EnableExtensions
chcp 65001 >nul
set "OKK_TARGET=%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility\OKK GROUP"

if exist "%OKK_TARGET%\OKK_Export.py" del /q "%OKK_TARGET%\OKK_Export.py"
if exist "%OKK_TARGET%\okk_app_path.txt" del /q "%OKK_TARGET%\okk_app_path.txt"

echo.
echo OKK GROUP 达芬奇连接器已卸载。
echo 重启 DaVinci Resolve 后菜单项会消失。
echo.
pause
