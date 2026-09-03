@echo off
setlocal EnableExtensions
cd /d "%~dp0"
echo === Dushan Quota install ===

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

set "PY="
python -c "import sys; raise SystemExit(0 if sys.hexversion >= 0x030A0000 else 1)" >nul 2>nul && set "PY=python"
if not defined PY (
  py -3 -c "import sys; raise SystemExit(0 if sys.hexversion >= 0x030A0000 else 1)" >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  echo [error] Python 3.10+ not found. Install Python or enable the py launcher.
  exit /b 1
)

set "VENV_PY=%ROOT%\.venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
  echo Creating virtual environment...
  %PY% -m venv "%ROOT%\.venv"
  if errorlevel 1 exit /b 1
)

echo Installing dependencies...
"%VENV_PY%" -m pip install -r "%ROOT%\requirements.txt"
if errorlevel 1 exit /b 1

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-path.ps1" -BinDir "%ROOT%"
if errorlevel 1 exit /b 1

set "SKILL_SRC=%ROOT%\skills\dushan-quota"
set "SKILL_DST=%USERPROFILE%\.config\opencode\skills\dushan-quota"
if exist "%SKILL_SRC%\SKILL.md" (
  if not exist "%SKILL_DST%" mkdir "%SKILL_DST%"
  copy /Y "%SKILL_SRC%\SKILL.md" "%SKILL_DST%\SKILL.md" >nul
  echo Skill copied to %SKILL_DST%
)

set "PATH=%ROOT%;%PATH%"
echo.
echo Done. Open a new terminal and run: quota
echo This terminal: "%ROOT%\quota.cmd"
exit /b 0
