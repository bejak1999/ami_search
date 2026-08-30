@echo off
REM Measures the ordering the crawler now uses, the same way the first run
REM measured "regtimed". Start it, leave the window open, Ctrl+C to stop.
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
echo   Measuring whether the "preowned" ordering really keeps new listings
echo   near the front, over 24 hours - the same test that showed "regtimed"
echo   does not.
echo.
echo   It starts and ends with a full enumeration of the slice, about half an
echo   hour each, and snapshots the first 30 pages every hour in between.
echo.
echo   Leave this window open. Ctrl+C stops early and still writes a report.
echo   Progress is saved after every step, so starting it again resumes.
echo.

"%PYTHON%" "%ROOT%\.probe\regtime_watch.py" watch preowned 30

echo.
echo   Finished. To see the report again:
echo     python .probe\regtime_watch.py report preowned 30
echo.
echo   And to compare against the old ordering's run:
echo     python .probe\regtime_watch.py report regtimed 20
echo.
pause
endlocal
