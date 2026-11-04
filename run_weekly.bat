@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    set "PYTHON=.venv\Scripts\python.exe"
) else (
    set "PYTHON=python"
)

"%PYTHON%" main.py >> weekly_run.log 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo Код завершения: %EXIT_CODE%
echo Журнал запуска: %~dp0weekly_run.log
exit /b %EXIT_CODE%
