@echo off
REM Prints the report from whatever has been collected so far.
setlocal
cd /d "%~dp0"
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY (echo   Python is not installed on this machine. & pause & exit /b 1)
%PY% measure.py report
echo.
pause
endlocal
