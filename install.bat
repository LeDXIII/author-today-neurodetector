@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo  author-today-neurodetector — Установка
echo ============================================
echo.

rem Проверяем наличие Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python не найден. Установите Python 3.10+
    pause
    exit /b 1
)

rem Создаём виртуальное окружение
if exist "venv" (
    echo [INFO] venv уже существует, пропускаем создание
) else (
    echo [1/3] Создание виртуального окружения...
    python -m venv venv
)

rem Активируем и устанавливаем зависимости
echo [2/3] Установка зависимостей...
call venv\Scripts\activate.bat
pip install --upgrade pip >nul
pip install -r requirements.txt

rem Скачиваем ChromeDriver для Selenium
echo [3/3] Установка ChromeDriver для Selenium...
call venv\Scripts\python -m webdriver_manager cache

echo.
echo ============================================
echo  Готово! Запустите run.bat для старта GUI
echo ============================================
pause
