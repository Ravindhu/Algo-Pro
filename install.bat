@echo off
REM ==========================================================================
REM install.bat - ONE-CLICK INSTALL (Windows).
REM
REM Double-click this file. Safe to run again: finished steps are skipped.
REM   1. find Python 3.11+
REM   2. create .venv
REM   3. install requirements.txt and Blue_engine
REM   4. clone OpenAlgo into openalgo\ (if missing)
REM   5. create .env from .env.example (if missing)
REM   6. start the stack (scripts\ops\start_all.py)
REM
REM It never places orders and never starts the trading engine.
REM ==========================================================================
setlocal EnableExtensions
cd /d "%~dp0"

echo.
echo   My Trading System - one-click install
echo   ======================================

echo.
echo [1/6] Python
set "PYCMD="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if errorlevel 1 goto try_python
set "PYCMD=py -3"
goto py_found
:try_python
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if errorlevel 1 goto no_python
set "PYCMD=python"
:py_found
echo   [ok] %PYCMD%

echo.
echo [2/6] Virtual environment (.venv)
if exist ".venv\Scripts\python.exe" goto venv_ready
%PYCMD% -m venv .venv
if errorlevel 1 goto fail
:venv_ready
set "VPY=.venv\Scripts\python.exe"
echo   [ok] .venv ready

echo.
echo [3/6] Dependencies (first run takes several minutes)
"%VPY%" -m pip install -q -U pip
if errorlevel 1 goto fail
"%VPY%" -m pip install -q -r requirements.txt
if errorlevel 1 goto fail
"%VPY%" -m pip install -q -e "Blue_engine[dashboard]"
if errorlevel 1 goto fail
echo   [ok] dependencies installed

echo.
echo [4/6] OpenAlgo platform (openalgo\)
if exist "openalgo\app.py" goto openalgo_ready
where git >nul 2>&1
if errorlevel 1 goto no_git
git clone --depth 1 https://github.com/marketcalls/openalgo.git openalgo
if errorlevel 1 goto fail
:openalgo_ready
echo   [ok] openalgo present

echo.
echo [5/6] Configuration (.env)
if exist ".env" goto env_ready
if exist ".env.example" copy /y ".env.example" ".env" >nul
echo   [ok] .env created from .env.example
echo   [todo] Fill in .env (API keys) before you connect a broker.
:env_ready

echo.
echo [6/6] Starting the stack (OpenAlgo, WebSocket, Dash)
"%VPY%" scripts\ops\start_all.py
if errorlevel 1 goto fail
echo.
echo   Install complete.
echo     OpenAlgo  http://127.0.0.1:5000
echo     Dash      http://127.0.0.1:8050
echo     Stop      scripts\ops\start_all.py --stop
goto done

:no_python
echo   [FAIL] Python 3.11 or newer was not found.
echo          Install it from https://www.python.org/downloads/windows/
echo          Tick "Add python.exe to PATH", then run install.bat again.
goto fail

:no_git
echo   [FAIL] Git was not found.
echo          Install Git for Windows from https://git-scm.com/download/win
echo          then run install.bat again.
goto fail

:fail
echo.
echo   [FAIL] Install stopped. Read the message above, fix it, then run install.bat again.

:done
echo.
pause
endlocal
