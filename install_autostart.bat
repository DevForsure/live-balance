@echo off
REM Starts Live Balance automatically when this Windows user logs on (Startup folder).
REM "install_autostart.bat remove" takes it out again.
set LINK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Live Balance v2.cmd
if /i "%~1"=="remove" (
    del "%LINK%" 2>nul
    echo Auto-start removed.
    exit /b 0
)
> "%LINK%" echo @call "%~dp0live-balance-start.bat" quiet
echo Auto-start installed: Live Balance starts when %USERNAME% logs on.
echo   (%LINK%)
