@echo off
REM Throws away the collected measurement so the next start begins afresh.
REM Only needed if a run was interrupted so badly that resuming makes no sense.
setlocal
cd /d "%~dp0"
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY (echo   Python is not installed on this machine. & pause & exit /b 1)
%PY% measure.py reset
echo.
pause
endlocal
