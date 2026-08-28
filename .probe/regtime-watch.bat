@echo off
REM Runs the regtimed measurement for 24 hours. Start it, leave the window
REM open, press Ctrl+C when you want to stop early.
setlocal

REM pushd resolves the ..\ into a real path. Pasting one together by hand
REM leaves a literal \..\ in the middle, which some checks mishandle.
pushd "%~dp0.." || (echo Could not find the project folder. & pause & exit /b 1)
set "ROOT=%CD%"
popd

set "PYTHON=%ROOT%\backend\.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo.
    echo   No virtual environment at:
    echo   %PYTHON%
    echo.
    echo   Create it once:
    echo     cd backend
    echo     python -m venv .venv
    echo     .venv\Scripts\pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

set "PYTHONPATH=%ROOT%\backend"

echo.
echo   Measuring whether new pre-owned listings appear at the front of the
echo   "regtimed" ordering. This runs for 24 hours.
echo.
echo   It starts and ends with a full enumeration of the slice, about half an
echo   hour each, and takes a 20-page snapshot every hour in between.
echo.
echo   Leave this window open. Ctrl+C stops early and still writes a report.
echo   Progress is saved after every step, so starting it again resumes.
echo.

"%PYTHON%" "%ROOT%\.probe\regtime_watch.py" watch

echo.
echo   Finished. To see the report again:
echo     python .probe\regtime_watch.py report
echo.
pause
endlocal
