@echo off
setlocal
set "QUOTA_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%QUOTA_PYTHON%" (
    echo [ERROR] quota-cli virtual environment is missing.
    echo Recreate it with: py -3.12 -m venv "%~dp0.venv"
    exit /b 1
)
"%QUOTA_PYTHON%" "%~dp0quota.py" %*
endlocal
