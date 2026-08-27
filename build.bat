@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo === IP Tray: сборка EXE ===
where python >nul 2>nul
if errorlevel 1 (
    echo Python не найден в PATH.
    echo Откройте «Командную строку» там, где работает python, и запустите build.bat снова.
    pause
    exit /b 1
)

python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller

echo.
echo Собираю IPTray.exe ...
python -m PyInstaller --noconfirm --clean --onefile --windowed --name IPTray --icon icon.ico --hidden-import pystray._win32 --hidden-import PIL._tkinter_finder ip_tray.py

if errorlevel 1 (
    echo.
    echo Сборка не удалась.
    pause
    exit /b 1
)

echo.
echo Готово: dist\IPTray.exe
explorer dist
pause
