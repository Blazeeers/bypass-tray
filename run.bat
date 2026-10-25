@echo off
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0src"
for /d %%d in ("dist\*") do (
  for %%f in ("%%~fd\*.exe") do (
    if exist "%%~f" (
      start "" "%%~f"
      exit /b
    )
  )
)
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" -m bypass_tray
) else (
  start "" pythonw -m bypass_tray
)
endlocal
