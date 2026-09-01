@echo off
REM Runs the measurement from wherever this folder happens to sit - a memory
REM stick included. Everything it needs goes into lib\ beside it, so nothing
REM is installed into the machine and nothing is left behind.
setlocal
cd /d "%~dp0"

echo.
echo   AmiAmi ordering measurement
echo   ===========================
echo.

REM py is the Windows launcher and is there on most installs; python is the
REM fallback for a machine where only the plain interpreter is on PATH.
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY where python >nul 2>&1 && set "PY=python"

if not defined PY (
    echo   Python is not installed on this machine.
    echo.
    echo   Install it from https://www.python.org/downloads/ and tick
    echo   "Add python.exe to PATH" in the first screen of the installer.
    echo   Then run this file again.
    echo.
    pause
    exit /b 1
)

REM One dependency: curl_cffi. The shop refuses a plain HTTP client on its TLS
REM fingerprint, so the request has to come from something that handshakes
REM like a browser. Installed into lib\ rather than into the machine.
%PY% -c "import sys; sys.path.insert(0, 'lib'); import curl_cffi" >nul 2>&1
if errorlevel 1 (
    REM A lib folder that will not import was built against a different Python
    REM version - curl_cffi ships compiled wheels. Clear it out rather than
    REM installing on top, which leaves two half-versions fighting.
    if exist lib (
        echo   The existing setup does not match this machine's Python; redoing it.
        rmdir /s /q lib
    )
    echo   Setting up ^(first run on this machine, needs internet^)...
    echo.
    %PY% -m pip install --quiet --upgrade --target lib curl_cffi
    if errorlevel 1 (
        echo.
        echo   Could not install curl_cffi. Check the internet connection and
        echo   try again. If this machine has a different Python version from
        echo   the one the folder was prepared on, delete the lib folder first.
        echo.
        pause
        exit /b 1
    )
    echo   Ready.
    echo.
)

echo   This runs for 24 hours. It starts and ends with a full read of the
echo   pre-owned catalogue ^(about half an hour each^) and takes a snapshot of
echo   the first 30 pages every hour in between.
echo.
echo   Leave this window open. The laptop must stay awake - check that it is
echo   set not to sleep on mains power, or the measurement stops with it.
echo.
echo   Ctrl+C stops early and keeps everything collected so far. Progress is
echo   saved after every step, so starting it again resumes.
echo.

%PY% measure.py watch

echo.
echo   Finished. To see the report again, run report.bat
echo.
pause
endlocal
