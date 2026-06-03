@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist "venv" (
    echo [ERROR] Виртуальное окружение не найдено!
    echo Запустите install.bat для установки зависимостей.
    pause
    exit /b 1
)

call venv\Scripts\activate.bat
python main.py
