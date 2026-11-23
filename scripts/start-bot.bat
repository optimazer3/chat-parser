@echo off
rem Запуск телеграм-бота двойным щелчком.
rem Пока это окно открыто, бот работает. Закрыл окно — бот остановился.
chcp 65001 >nul
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
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

set PYTHONUNBUFFERED=1
set PYTHONUTF8=1
echo Загружаю программу. Далее появятся шаги проверки Telegram и Supabase.
echo.
".venv\Scripts\python.exe" -u -m chat_parser.cli bot
echo.
echo Бот остановлен.
pause
