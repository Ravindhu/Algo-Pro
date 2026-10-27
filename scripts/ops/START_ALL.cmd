@echo off
REM ==========================================================================
REM START_ALL.cmd - double-click launcher for the whole local stack.
REM
REM Starts OpenAlgo (Docker if the daemon is up, else native), the WebSocket
REM proxy and the Dash terminal, then prints the URLs. Updates are OFF -
REM to update first, run:  start_all.py --update
REM
REM Everything that survives this window runs in the background. Close the
REM window freely. To stop everything:
REM     python scripts\ops\start_all.py --stop
REM ==========================================================================
setlocal
cd /d "%~dp0..\.."

set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY if exist "openalgo\.venv\Scripts\python.exe" set "PY=openalgo\.venv\Scripts\python.exe"

if not defined PY (
    echo.
    echo   [FAIL] No virtual environment found.
    echo          Create one first:   python -m venv .venv
    echo          Then install:       .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo          Then the engine:    .venv\Scripts\python.exe -m pip install -e "Blue_engine[dashboard]"
    echo.
    pause
    exit /b 1
)

echo.
echo   Starting My Trading System...
echo   (OpenAlgo :5000, WebSocket :8765, Dash :8050)
echo.

"%PY%" "scripts\ops\start_all.py" %*

echo.
if errorlevel 1 (
    echo   One or more services did not become healthy.
    echo   Logs: Temp\run\
) else (
    echo   All services are up.
    echo     OpenAlgo  http://127.0.0.1:5000
    echo     Dash      http://127.0.0.1:8050
)
echo.
pause
