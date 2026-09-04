@echo off
setlocal
set "QUOTA_T_ROOT=%~dp0"
set "QUOTA_T_PYTHON=%QUOTA_T_ROOT%.venv\Scripts\python.exe"
if not exist "%QUOTA_T_PYTHON%" (
    echo [ERROR] quota-t virtual environment is missing.
    echo Expected: "%QUOTA_T_PYTHON%"
    exit /b 1
)

set "QUOTA_T_TEMP=%QUOTA_T_ROOT%Temp\quota-t"
if not exist "%QUOTA_T_TEMP%" mkdir "%QUOTA_T_TEMP%"
set "TEMP=%QUOTA_T_TEMP%"
set "TMP=%QUOTA_T_TEMP%"
set "TMPDIR=%QUOTA_T_TEMP%"
set "PYTHONPYCACHEPREFIX=%QUOTA_T_TEMP%\pycache"
set "PIP_CACHE_DIR=%QUOTA_T_TEMP%\pip-cache"
set "DUSHAN_QUOTA_WINDOW_TITLE=Quota-T"
set "DUSHAN_QUOTA_WEB_PORT=18766"

cd /d "%QUOTA_T_ROOT%"
if "%~1"=="" (
    "%QUOTA_T_PYTHON%" "%QUOTA_T_ROOT%quota.py" float
) else (
    "%QUOTA_T_PYTHON%" "%QUOTA_T_ROOT%quota.py" %*
)
set "QUOTA_T_EXIT=%ERRORLEVEL%"
endlocal & exit /b %QUOTA_T_EXIT%
