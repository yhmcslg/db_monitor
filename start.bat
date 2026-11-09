@echo off
chcp 65001 >nul
REM Windows: activate venv and start service

setlocal
cd /d "%~dp0"

if not exist "venv\Scripts\activate.bat" (
    echo [ERROR] Virtual env not found. Run: python -m venv venv
    pause
    exit /b 1
)

REM Activate venv
call venv\Scripts\activate.bat

REM Playwright browser location
set PLAYWRIGHT_BROWSERS_PATH=%~dp0ms-playwright

echo Starting db_monitor ...
python run.py

pause
