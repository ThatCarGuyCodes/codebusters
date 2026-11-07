@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    py -3 -m venv .venv
    if errorlevel 1 (
        echo Python 3.11 or newer is required. Install Python and try again.
        pause
        exit /b 1
    )
)
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
    echo Installation failed. Check your internet connection.
    pause
    exit /b 1
)
echo Open http://localhost:8000 in your browser.
.venv\Scripts\python.exe -m uvicorn server:app --host 0.0.0.0 --port 8000
pause
