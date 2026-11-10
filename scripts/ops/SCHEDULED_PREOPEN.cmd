@echo off
REM ===========================================================================
REM  SCHEDULED_PREOPEN.cmd - Task Scheduler entry for the pre-open run (08:50 IST)
REM
REM  Written by scripts\ops\install_schedule.py. Arguments:
REM    %1  1 = broker login first, 0 = skip login (use the existing session)
REM    %2  1 = start the engine after a good login, 0 = leave it stopped
REM
REM  Login is the switch's opt-in, so BROKER_AUTOLOGIN is set here for this run
REM  only. The engine starts only when login succeeded (or was skipped).
REM  It never edits .env and never trades on its own outside the engine gates.
REM ===========================================================================
setlocal EnableExtensions

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..\..") do set "ROOT=%%~fI"
set "PY=%ROOT%\.venv\Scripts\python.exe"
set "RUN_DIR=%ROOT%\Temp\run"
set "LOG=%RUN_DIR%\preopen_task.log"
set "WANT_LOGIN=%~1"
set "WANT_ENGINE=%~2"

if not exist "%RUN_DIR%" mkdir "%RUN_DIR%" >nul 2>&1
if not exist "%PY%" (
    echo [%DATE% %TIME%] ERROR: venv python not found at "%PY%" >> "%LOG%"
    exit /b 3
)
cd /d "%ROOT%"
echo [%DATE% %TIME%] preopen start login=%WANT_LOGIN% engine=%WANT_ENGINE% >> "%LOG%"

set "LOGIN_RC=0"
if "%WANT_LOGIN%"=="1" (
    set "BROKER_AUTOLOGIN=1"
    "%PY%" "%ROOT%\scripts\ops\scheduled_login.py"
    set "LOGIN_RC=%ERRORLEVEL%"
    echo [%DATE% %TIME%] login exit=%LOGIN_RC% >> "%LOG%"
    if not "%LOGIN_RC%"=="0" (
        echo [%DATE% %TIME%] login failed - engine NOT started >> "%LOG%"
        exit /b %LOGIN_RC%
    )
)

if "%WANT_ENGINE%"=="1" (
    "%PY%" "%ROOT%\scripts\run\run_supervised.py"
    set "ENGINE_RC=%ERRORLEVEL%"
    echo [%DATE% %TIME%] engine exit=%ENGINE_RC% >> "%LOG%"
    exit /b %ENGINE_RC%
)

echo [%DATE% %TIME%] engine start skipped by switch >> "%LOG%"
exit /b 0
