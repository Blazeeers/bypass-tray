@echo off
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0src"
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" -m bypass_tray
) else (
  start "" pythonw -m bypass_tray
)
endlocal
