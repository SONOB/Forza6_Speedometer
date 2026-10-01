@echo off
setlocal
cd /d "%~dp0"
title Forza Horizon 6 - Speedometer
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -B speedometer.py %*
    if errorlevel 1 pause
    exit /b
)
where python >nul 2>nul
if not errorlevel 1 (
    python -B speedometer.py %*
    if errorlevel 1 pause
    exit /b
)
echo Python 3.10 or newer is required.
echo Install Python from https://www.python.org/downloads/windows/
echo Include Tcl/Tk and select Add Python to PATH during setup.
pause
