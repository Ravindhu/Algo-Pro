@echo off
REM ==========================================================================
REM install.bat - THE ONE SCRIPT (Windows).
REM
REM   install.bat              manual run: update repo, install, upgrade, start
REM   install.bat --scheduled  scheduled run (sign-in and pre-open jobs made by
REM                            scripts\ops\register_start_jobs.ps1): start only.
REM                            No code update, no OpenAlgo upgrade, no engine.
REM
REM Safe to run again and again. Never places orders, never starts the engine.
REM Stop everything:  .venv\Scripts\python.exe scripts\ops\start_all.py --stop
REM ==========================================================================
setlocal EnableExtensions
cd /d "%~dp0"

set "SCHED=0"
if /i "%~1"=="--scheduled" set "SCHED=1"

echo.
echo   My Trading System - install, update and start
if "%SCHED%"=="1" echo   scheduled run - start only, no code update
echo   ==============================================

echo.
echo [1/6] Update this repo
if "%SCHED%"=="1" goto repo_sched
if not exist ".git" goto repo_skip
where git >nul 2>&1
if errorlevel 1 goto repo_skip
git pull --ff-only
if errorlevel 1 goto repo_pull_failed
echo   [ok] repo up to date
goto repo_done
:repo_sched
echo   [skip] scheduled run - code is not updated automatically
goto repo_done
:repo_pull_failed
echo   [warn] repo not updated - local changes or diverged. Using the current code.
goto repo_done
:repo_skip
echo   [skip] not a git clone, or git not found
:repo_done

echo.
echo [2/6] Python
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
echo [3/6] Virtual environment and dependencies (first run takes several minutes)
if exist ".venv\Scripts\python.exe" goto venv_ready
%PYCMD% -m venv .venv
if errorlevel 1 goto fail
:venv_ready
set "VPY=.venv\Scripts\python.exe"
if "%SCHED%"=="1" goto deps_sched
"%VPY%" -m pip install -q -U pip
if errorlevel 1 goto fail
"%VPY%" -m pip install -q -r requirements.txt
if errorlevel 1 goto fail
"%VPY%" -m pip install -q -e "Blue_engine[charts]"
if errorlevel 1 goto fail
echo   [ok] dependencies installed
goto deps_done
:deps_sched
"%VPY%" -m pip install -q -r requirements.txt >nul 2>&1
if errorlevel 1 echo   [warn] dependency check failed - using the packages already installed
echo   [ok] dependencies checked
:deps_done

echo.
echo [4/6] OpenAlgo platform (openalgo\)
if exist "openalgo\app.py" goto openalgo_present
where git >nul 2>&1
if errorlevel 1 goto no_git
git clone --depth 1 https://github.com/marketcalls/openalgo.git openalgo
if errorlevel 1 goto fail
echo   [ok] openalgo cloned
goto openalgo_done
:openalgo_present
if "%SCHED%"=="1" goto openalgo_kept
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\ops\upgrade_openalgo.ps1"
if errorlevel 1 echo   [warn] OpenAlgo not upgraded - a safety gate stopped it. Current version kept.
goto openalgo_done
:openalgo_kept
echo   [ok] openalgo present - scheduled run keeps the current version
:openalgo_done

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
echo   Done.
echo     OpenAlgo  http://127.0.0.1:5000
echo     Dash      http://127.0.0.1:8050
echo     Stop      .venv\Scripts\python.exe scripts\ops\start_all.py --stop
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
echo   [FAIL] Stopped. Read the message above, fix it, then run install.bat again.

:done
echo.
if "%SCHED%"=="1" goto end_quiet
pause
:end_quiet
endlocal
