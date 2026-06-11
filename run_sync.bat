@echo off
setlocal

:: Navigate to the directory where this batch file resides
:: This ensures that paths are relative to the installation, making it portable.
cd /d "%~dp0"

:: Check for common virtual environment folder names (.venv or venv)
set "VENV_PYTHON="
if exist ".venv\Scripts\python.exe" (
    set "VENV_PYTHON=.venv\Scripts\python.exe"
) else if exist "venv\Scripts\python.exe" (
    set "VENV_PYTHON=venv\Scripts\python.exe"
)

if "%VENV_PYTHON%"=="" (
    echo [ERROR] Could not find a virtual environment in .venv or venv folders.
    pause
    exit /b 1
)

echo [INFO] Executing sync_transcoded.py...
"%VENV_PYTHON%" sync_transcoded.py
pause