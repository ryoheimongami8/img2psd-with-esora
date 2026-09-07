@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo   img2PSD - setup
echo ============================================
echo.

where python >NUL 2>&1
if errorlevel 1 (
    echo [ERROR] Python was not found on PATH.
    echo         Install Python 3.11 or newer and run this script again.
    goto :fail
)

if exist ".venv\Scripts\python.exe" (
    echo [1/3] Virtual environment already exists - skipping creation.
) else (
    echo [1/3] Creating virtual environment in .venv ...
    python -m venv .venv
    if errorlevel 1 goto :fail
)

echo [2/3] Upgrading pip ...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
if errorlevel 1 goto :fail

echo [3/3] Installing dependencies from requirements.txt ...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :fail

echo.
echo Checking the Esora CLI ...
where esora-api >NUL 2>&1
if errorlevel 1 (
    echo [WARN] The esora-api CLI was not found on PATH.
    echo        Image generation will not work until it is installed:
    echo            uv tool install "D:\40_Esora\01_CLI\esora_api_cli-0.4.5-py3-none-any.whl"
) else (
    echo       Found: esora-api
)

echo.
echo ============================================
echo   Setup finished.
echo.
echo   Next step: double-click run.bat
echo   It signs you in to Esora if needed, then starts the app.
echo ============================================
echo.
pause
exit /b 0

:fail
echo.
echo [ERROR] Setup failed. Read the messages above for the cause.
echo.
pause
exit /b 1
