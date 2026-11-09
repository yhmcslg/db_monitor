@echo off
chcp 65001 >nul
REM Windows: pull latest from GitHub and install dependencies

setlocal EnableDelayedExpansion
cd /d "%~dp0"

set REMOTE=https://github.com/yhmcslg/db_monitor.git

REM Ensure origin points to the expected URL
git remote get-url origin >nul 2>nul
if %errorlevel%==0 (
    for /f "delims=" %%i in ('git remote get-url origin') do set CURRENT=%%i
    if not "!CURRENT!"=="%REMOTE%" (
        echo Updating origin: !CURRENT! -^> %REMOTE%
        git remote set-url origin %REMOTE%
    )
) else (
    git remote add origin %REMOTE%
)

REM Detect remote default branch
for /f "tokens=*" %%b in ('git symbolic-ref refs/remotes/origin/HEAD 2^>nul') do set REMOTE_HEAD=%%b
if "!REMOTE_HEAD!"=="" (
    git remote set-head origin -a >nul 2>nul
    for /f "tokens=*" %%b in ('git symbolic-ref refs/remotes/origin/HEAD 2^>nul') do set REMOTE_HEAD=%%b
)
for %%a in ("!REMOTE_HEAD!") do set DEFAULT_BRANCH=%%~nxa
if "!DEFAULT_BRANCH!"=="" (
    echo [WARN] Could not detect remote branch, fallback to main
    set DEFAULT_BRANCH=main
)

echo Pulling origin/!DEFAULT_BRANCH! ...
REM Retry up to 3 times - GitHub access from CN is flaky
set TRY=0
:pull_retry
git pull --ff-only origin !DEFAULT_BRANCH!
if %errorlevel%==0 goto pull_ok
set /a TRY=TRY+1
if !TRY! lss 3 (
    echo [WARN] pull failed, retrying (!TRY!/3) ...
    timeout /t 2 >nul
    goto pull_retry
)
echo.
echo [ERROR] git pull failed. Check network to github.com or local conflicts.
pause
exit /b 1
:pull_ok

echo.
echo Updating Python dependencies ...
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] pip install failed
    pause
    exit /b 1
)

echo.
echo Done. Run: python run.py
pause
