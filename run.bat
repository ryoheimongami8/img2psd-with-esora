@echo off
setlocal
cd /d "%~dp0"

REM Port 7860 is Gradio's default, so it is also the first port every other
REM local tool takes. Pass a different one as the first argument if it clashes:
REM     run.bat 7870
if not "%~1"=="" set IMG2PSD_PORT=%~1
if "%IMG2PSD_PORT%"=="" set IMG2PSD_PORT=7860

echo ============================================
echo   img2PSD - Esora backend
echo ============================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment not found.
    echo         Run setup.bat first.
    goto :fail
)

where esora-api >NUL 2>&1
if errorlevel 1 (
    echo [ERROR] The esora-api CLI was not found on PATH.
    echo         Install it with:
    echo             uv tool install "D:\40_Esora\01_CLI\esora_api_cli-0.4.5-py3-none-any.whl"
    goto :fail
)

REM Read the state out of the JSON rather than trusting an exit code: the CLI
REM reports "not signed in" as an error status, and a stale-but-refreshable
REM session must not be mistaken for one that needs the browser again.
REM The dot in the pattern stands for the quote character, which would otherwise
REM have to be escaped through two levels of batch quoting.
echo Checking the Esora sign-in ...
esora-api --json auth status 2>NUL | findstr /R /C:"signed_in.: true" >NUL
if errorlevel 1 (
    echo Not signed in. A browser window will open for Google sign-in.
    echo Use your work Google account.
    echo.
    esora-api auth login
    if errorlevel 1 (
        echo.
        echo [ERROR] Sign-in failed. Try running this by hand:
        echo             esora-api auth login
        goto :fail
    )
    echo.
)

echo Signed in. Starting the app on http://127.0.0.1:%IMG2PSD_PORT%/
echo A browser tab opens automatically. Press Ctrl+C here to stop the server.
echo.
".venv\Scripts\python.exe" app.py
if errorlevel 1 goto :fail

exit /b 0

:fail
echo.
echo [ERROR] Could not start. Read the messages above for the cause.
echo.
pause
exit /b 1
