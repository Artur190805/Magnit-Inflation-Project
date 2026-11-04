@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    echo Виртуальное окружение уже существует.
) else (
    echo Создаю виртуальное окружение...
    py -3.12 -m venv .venv 2>nul
    if errorlevel 1 py -3.11 -m venv .venv 2>nul
    if errorlevel 1 python -m venv .venv
)

if not exist ".venv\Scripts\python.exe" (
    echo Не удалось создать .venv. Установи Python 3.11 или новее.
    pause
    exit /b 1
)

.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt

echo.
echo Установка завершена. Запускай run_weekly.bat.
pause
