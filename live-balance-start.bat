@echo off
REM Starts the whole Live Balance app (recorder + batch page) in the background, no window,
REM then opens the batch page. Safe to run twice: a second copy does not start.
REM "live-balance-start.bat quiet" skips opening the browser (used at Windows logon).
cd /d "%~dp0"
if not exist .venv\Scripts\pythonw.exe (
    echo Not set up yet - run setup.bat first.
    pause
    exit /b 1
)
start "" .venv\Scripts\pythonw.exe app\run.py
if /i not "%~1"=="quiet" (
    timeout /t 4 /nobreak >nul
    start "" http://localhost:8086/
)
