@echo off
REM ===========================================================================
REM  SCHEDULED_LOGIN.cmd - Task Scheduler entry point for headless broker login
REM
REM  Task Scheduler runs this with NO working directory and NO console, so:
REM    * the repo root is derived from this file's own location, not %CD%;
REM    * the venv interpreter is used explicitly (never "python" from PATH);
REM    * output goes to Temp\run\login_task.log so a failed 08:50 run is
REM      diagnosable after the fact.
REM
REM  It never trades. It never starts OpenAlgo. It exits non-zero on failure so
REM  Task Scheduler's "last result" column is meaningful.
REM ===========================================================================
setlocal EnableExtensions

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..\..") do set "ROOT=%%~fI"
set "PY=%ROOT%\.venv\Scripts\python.exe"
set "RUN_DIR=%ROOT%\Temp\run"
set "LOG=%RUN_DIR%\login_task.log"

if not exist "%RUN_DIR%" mkdir "%RUN_DIR%" >nul 2>&1

if not exist "%PY%" (
    echo [%DATE% %TIME%] ERROR: venv python not found at "%PY%" >> "%LOG%"
    exit /b 3
)

cd /d "%ROOT%"

echo. >> "%LOG%"
echo ============================================================ >> "%LOG%"
echo [%DATE% %TIME%] scheduled_login start (cwd=%CD%) >> "%LOG%"

"%PY%" "%ROOT%\scripts\ops\scheduled_login.py" %*
set "RC=%ERRORLEVEL%"

echo [%DATE% %TIME%] scheduled_login exit=%RC% >> "%LOG%"
exit /b %RC%
