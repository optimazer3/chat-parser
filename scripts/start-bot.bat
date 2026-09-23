@echo off
rem Запуск телеграм-бота двойным щелчком.
rem Пока это окно открыто, бот работает. Закрыл окно — бот остановился.
chcp 65001 >nul
cd /d "%~dp0.."

if not exist ".venv\Scripts\activate.bat" (
    echo Не найдено виртуальное окружение .venv в папке %CD%
    echo Один раз выполни в этой папке:
    echo     python -m venv .venv
    echo     .venv\Scripts\activate.bat
    echo     pip install -e ".[dev]"
    pause
    exit /b 1
)
if not exist ".env" (
    echo Нет файла .env в папке %CD%
    echo Создай его:  copy .env.example .env  и заполни.
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat
echo Бот запускается. Не закрывай это окно - пока оно открыто, бот работает.
echo.
chat-parser bot
echo.
echo Бот остановлен.
pause
