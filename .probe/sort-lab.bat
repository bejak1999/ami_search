@echo off
REM Starts the AmiAmi sort lab and opens it in your browser.
REM Double-click it, or run it from anywhere: the paths come from where this
REM file sits, not from the directory you happen to be in.
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

REM The lab imports the shop provider, which lives under backend\app.
set "PYTHONPATH=%ROOT%\backend"

echo Starting the sort lab on http://localhost:8710
echo Close this window or press Ctrl+C to stop it.
echo.
"%PYTHON%" "%ROOT%\.probe\sort_lab.py"

REM Only reached when it exits on its own, which means something went wrong.
REM Hold the window open so the reason stays readable.
if errorlevel 1 (
    echo.
    echo   The lab stopped with an error.
    pause
)
endlocal
