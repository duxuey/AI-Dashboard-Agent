@echo off
cd /d "%~dp0"

echo ============================================
echo    AI-Dashboard-Agent
echo ============================================
echo.

REM ---- 1. Check venv exists ----
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment .venv not found.
    echo Run the following first:
    echo     python -m venv .venv
    echo     .venv\Scripts\pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

REM ---- 2. Kill any process holding port 8000 ----
set PORT=8000
echo Checking port %PORT% ...
set "FOUND="
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%PORT% " ^| findstr "LISTENING"') do (
    set "FOUND=1"
    echo   Found old process PID %%a, killing...
    taskkill /F /PID %%a >nul 2>&1
)
if defined FOUND (
    echo   Old process cleared.
) else (
    echo   Port is free.
)
echo.

REM ---- 3. Start server ----
echo Starting dashboard. Keep this window open.
echo.
echo   URL: http://127.0.0.1:%PORT%
echo   Stop: press Ctrl+C in this window
echo.
.venv\Scripts\python.exe run.py

echo.
echo Server stopped.
pause
