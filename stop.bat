@echo off
REM Stops the whole Live Balance app. While it is stopped, switch presses are NOT recorded.
cd /d "%~dp0"
.venv\Scripts\python.exe app\run.py --stop
pause
