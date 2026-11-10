@echo off
REM ===========================================================================
REM  SCHEDULED_PREOPEN.cmd - Task Scheduler entry for the pre-open run (08:50 IST)
REM
REM  Written by scripts\ops\install_schedule.py. No arguments.
REM    1. broker login (always, for this run only: BROKER_AUTOLOGIN=1)
REM    2. engine start only if the dashboard toggle "auto engine start" is ON
REM       (read at run time by scripts\ops\read_engine_flag.py, fails closed)
REM  It never trades on its own outside the engine's own gates.
REM ===========================================================================
setlocal EnableExtensions

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..\..") do set "ROOT=%%~fI"
set "PY=%ROOT%\.venv\Scripts\python.exe"
set "RUN_DIR=%ROOT%\Temp\run"
set "LOG=%RUN_DIR%\preopen_task.log"

if not exist "%RUN_DIR%" mkdir "%RUN_DIR%" >nul 2>&1
if not exist "%PY%" (
    echo [%DATE% %TIME%] ERROR: venv python not found at "%PY%" >> "%LOG%"
    exit /b 3
)
cd /d "%ROOT%"
echo [%DATE% %TIME%] preopen start >> "%LOG%"

set "BROKER_AUTOLOGIN=1"
"%PY%" "%ROOT%\scripts\ops\scheduled_login.py"
set "LOGIN_RC=%ERRORLEVEL%"
echo [%DATE% %TIME%] login exit=%LOGIN_RC% >> "%LOG%"
if not "%LOGIN_RC%"=="0" (
    echo [%DATE% %TIME%] login failed - engine NOT started >> "%LOG%"
    exit /b %LOGIN_RC%
)

set "ENGINE_FLAG=0"
for /f "usebackq delims=" %%F in (`"%PY%" "%ROOT%\scripts\ops\read_engine_flag.py" 2^>nul`) do set "ENGINE_FLAG=%%F"
if "%ENGINE_FLAG%"=="1" (
    "%PY%" "%ROOT%\scripts\run\run_supervised.py"
    set "ENGINE_RC=%ERRORLEVEL%"
    echo [%DATE% %TIME%] engine exit=%ENGINE_RC% >> "%LOG%"
    exit /b %ENGINE_RC%
)

echo [%DATE% %TIME%] auto engine start is OFF - engine left for the dashboard button >> "%LOG%"
exit /b 0
