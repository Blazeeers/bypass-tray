@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python не найден в PATH. Установите Python 3.10+ и отметьте "Add Python to PATH".
  exit /b 1
)

python -m venv .venv
if errorlevel 1 exit /b 1
call ".venv\Scripts\python.exe" -m pip install --upgrade pip
call ".venv\Scripts\pip.exe" install -r requirements.txt
if errorlevel 1 exit /b 1

echo.
echo Готово. Запуск: run.bat
echo Проверка статусов без трея: ".venv\Scripts\python.exe" -m bypass_tray --status
echo Автозапуск: положите ярлык на run.bat в папку shell:startup (Win+R -^> shell:startup)
endlocal
