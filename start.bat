@echo off
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw ip_tray.py
) else (
    start "" python ip_tray.py
)
