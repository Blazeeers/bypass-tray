@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================================
echo   Сборка «Обходы» (ONEDIR, Python на целевом ПК не нужен)
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [!] Python не найден. Для СБОРКИ нужен Python 3.10+.
  echo     Готовый архив собирается в GitHub Actions.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/5] Создаю окружение...
  python -m venv .venv
)
call ".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet

echo [2/5] Ставлю зависимости и PyInstaller...
call ".venv\Scripts\pip.exe" install -r requirements.txt -r requirements-build.txt --quiet
if errorlevel 1 ( echo [!] Не удалось поставить зависимости & pause & exit /b 1 )

echo [3/5] Готовлю bin: ядро Xray и список Re:filter...
set "PYTHONPATH=%CD%\src"
call ".venv\Scripts\python.exe" -c "from bypass_tray import blocked, xray; blocked.ensure(); print('xray:', xray.ensure_core())"
if errorlevel 1 ( echo [!] Не удалось подготовить bin & pause & exit /b 1 )

echo [4/5] Рисую иконку...
call ".venv\Scripts\python.exe" packaging\make_icon.py

echo [5/5] Собираю .exe...
call ".venv\Scripts\pyinstaller.exe" --clean --noconfirm packaging\bypass-tray.spec
if errorlevel 1 ( echo [!] Сборка не удалась & pause & exit /b 1 )

echo.
echo Готово: dist\Обходы\Обходы.exe
echo Переноси папку dist\Обходы целиком: рядом с exe лежат bin и data.
endlocal
pause
