@echo off
REM Windows：从 GitHub 拉最新代码并更新依赖
REM 双击或命令行运行均可

setlocal
cd /d "%~dp0"

set REMOTE=https://github.com/yhmcslg/db_monitor.git

REM 确保 origin 指向正确地址
git remote get-url origin >nul 2>nul
if %errorlevel%==0 (
    for /f "delims=" %%i in ('git remote get-url origin') do set CURRENT=%%i
    if not "%CURRENT%"=="%REMOTE%" (
        echo 更新 origin: %CURRENT% ^-^> %REMOTE%
        git remote set-url origin %REMOTE%
    )
) else (
    git remote add origin %REMOTE%
)

echo 拉取最新代码...
git pull --ff-only
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] git pull 失败，可能有本地未提交的改动或冲突
    pause
    exit /b 1
)

echo.
echo 更新 Python 依赖...
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] pip install 失败
    pause
    exit /b 1
)

echo.
echo 完成，可运行 python run.py 启动
pause
