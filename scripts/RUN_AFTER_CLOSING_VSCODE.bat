@echo off
echo Running the move script - output is also saved to move_log.txt
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0complete_move.ps1" > "%~dp0move_log.txt" 2>&1
type "%~dp0move_log.txt"
echo.
echo (A copy of this output is saved in scripts\move_log.txt)
pause
