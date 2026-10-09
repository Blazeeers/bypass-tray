@echo off
chcp 65001 > nul
setlocal
cd /d "%~dp0"

echo ============================================================
echo   Сборка bypass-tray.exe (один файл, Python не нужен)
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [!] Python не найден. Для СБОРКИ нужен Python 3.10+.
  echo     Готовый .exe собирается в GitHub Actions (см. .github/workflows).
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/4] Создаю окружение...
  python -m venv .venv
)
call ".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet

echo [2/4] Ставлю зависимости и PyInstaller...
call ".venv\Scripts\pip.exe" install -r requirements.txt -r requirements-build.txt --quiet
if errorlevel 1 ( echo [!] Не удалось поставить зависимости & pause & exit /b 1 )

echo [3/4] Рисую иконку...
call ".venv\Scripts\python.exe" packaging\make_icon.py

echo [4/4] Собираю .exe...
call ".venv\Scripts\pyinstaller.exe" --clean --noconfirm packaging\bypass-tray.spec
if errorlevel 1 ( echo [!] Сборка не удалась & pause & exit /b 1 )

echo.
echo Готово: dist\bypass-tray.exe
echo Этот файл можно переносить на любой Windows-ПК и запускать двойным щелчком.
endlocal
pause
