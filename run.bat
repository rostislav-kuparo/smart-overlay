@echo off
rem Запуск с консолью и журналом на экране — для отладки. Обычный запуск — ярлык «Smart Overlay» в меню «Пуск».
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    echo Run install.bat first.
    pause
    exit /b 1
)
.venv\Scripts\python.exe -m smart_overlay %*
if errorlevel 1 pause
