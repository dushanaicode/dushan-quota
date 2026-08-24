@echo off
setlocal
echo === Quota CLI 安装 ===

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 python，请先安装 Python 3.10+
    exit /b 1
)

pip install -r "%~dp0requirements.txt" --quiet

for /f "delims=" %%i in ("%~dp0") do set "BINDIR=%%~fi"
setx PATH "%PATH%;%BINDIR%" >nul 2>nul
echo.
echo 已将 %BINDIR% 写入用户 PATH（新开终端生效）
echo 之后任意终端执行: quota
echo Web UI:            quota ui
endlocal
