@echo off
title OpenAlgo (local, no Docker)
cd /d "%~dp0..\openalgo"
echo Starting OpenAlgo - close this window to stop.
echo Web app  : http://127.0.0.1:5000
echo WebSocket: ws://127.0.0.1:8765
echo.
".venv\Scripts\python.exe" "%~dp0start_openalgo.py"
echo.
echo OpenAlgo has stopped.
pause
