@echo off
REM One-time setup on a new PC: virtual environment, packages, checks.
REM EasyOCR (batch number reading) is installed without its own dependencies: it asks for
REM opencv-python-headless, which has no windows and breaks tools\select_crop.py.
cd /d "%~dp0"

REM Python from python.org, not the Microsoft Store one: a Store Python process stuck in a
REM file operation cannot be ended, and then no Python starts until a reboot (seen live).
if not exist .venv (
    echo Creating virtual environment...
    py -3.12 -m venv .venv 2>nul || python -m venv .venv || (echo Python not found - install Python 3.12 from python.org & exit /b 1)
)
findstr /i "WindowsApps" .venv\pyvenv.cfg >nul && (
    echo .venv uses the Microsoft Store Python. Install Python 3.12 from python.org, delete the .venv folder, run setup.bat again.
    exit /b 1
)
set PY=.venv\Scripts\python.exe

%PY% -m pip install --upgrade pip || goto :fail
%PY% -m pip install -r requirements.txt || goto :fail
echo Installing EasyOCR for batch reading (large download: PyTorch)...
%PY% -m pip install --no-deps easyocr==1.7.2 || goto :fail
%PY% -m pip install torch torchvision scipy scikit-image python-bidi PyYAML Shapely pyclipper ninja || goto :fail

REM If the headless OpenCV got in anyway, it hides the window-capable one: put the right one back.
%PY% -m pip show opencv-python-headless >nul 2>&1 && (
    %PY% -m pip uninstall -y opencv-python-headless
    %PY% -m pip install --force-reinstall --no-deps opencv-python || goto :fail
)

echo Checking...
%PY% -c "import cv2; cv2.namedWindow('check'); cv2.destroyAllWindows()" || (echo OpenCV cannot open windows - tools\select_crop.py will not work & goto :fail)
%PY% -c "import easyocr, reportlab, dotenv" || goto :fail

if not exist "%USERPROFILE%\.EasyOCR\model\*.pth" (
    echo.
    echo WARNING: EasyOCR model files are missing. Batch reading downloads them on first run,
    echo which fails without internet. Copy the folder %%USERPROFILE%%\.EasyOCR\model from a PC
    echo where batch reading works.
)
if not exist .env (
    copy .env.example .env >nul
    echo Created .env - put the camera address in LIVE_BALANCE_RTSP_URL.
)
echo.
echo Done. Next:
echo   %PY% tools\select_crop.py left     set the left balance's crop and switch LED (once)
echo   %PY% tools\select_crop.py centre   same for the centre balance
echo   live-balance-start.bat      start the app (install_autostart.bat: start at logon)
exit /b 0

:fail
echo.
echo Setup FAILED - see the error above.
exit /b 1
