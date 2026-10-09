@echo off
chcp 65001 > nul
setlocal
cd /d "%~dp0"

echo ============================================================
echo   bypass-tray - установка в один шаг
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [!] Python не найден.
  echo     Установите Python 3.10+ с python.org и отметьте
  echo     "Add python.exe to PATH", затем запустите setup.bat снова.
  echo.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] Создаю окружение...
  python -m venv .venv
  if errorlevel 1 ( echo [!] Не удалось создать окружение & pause & exit /b 1 )
) else (
  echo [1/3] Окружение уже есть.
)

echo [2/3] Ставлю зависимости...
call ".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
call ".venv\Scripts\pip.exe" install -r requirements.txt --quiet
if errorlevel 1 ( echo [!] Не удалось поставить зависимости & pause & exit /b 1 )
call ".venv\Scripts\pip.exe" install -e . --quiet

echo [3/3] Скачиваю обходы и запускаю виджет...
echo.
call ".venv\Scripts\python.exe" -m bypass_tray --setup
if errorlevel 1 (
  echo.
  echo [!] Что-то пошло не так. Подробности выше.
  pause
  exit /b 1
)

endlocal
