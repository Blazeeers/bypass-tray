@echo off
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0src"

rem Sources first: a stale dist build can start and exit silently.
if exist "src\bypass_tray\__main__.py" (
  if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" -m bypass_tray
    set "BYPASS_TRAY_MODE=cmdline"
    set "BYPASS_TRAY_MARK=-m bypass_tray"
    goto verify
  )
  where pythonw >nul 2>nul
  if not errorlevel 1 (
    start "" pythonw -m bypass_tray
    set "BYPASS_TRAY_MODE=cmdline"
    set "BYPASS_TRAY_MARK=-m bypass_tray"
    goto verify
  )
  echo Sources found, but pythonw is missing. Create the venv first:
  echo   py -m venv .venv
  echo   .venv\Scripts\pip install -r requirements.txt
  pause
  exit /b 1
)

rem No sources: use a dist build if one exists.
for /d %%d in ("dist\*") do (
  for %%f in ("%%~fd\*.exe") do (
    if exist "%%~ff" (
      set "BYPASS_TRAY_EXE=%%~ff"
      set "BYPASS_TRAY_MODE=exe"
      start "" "%%~ff"
      goto verify
    )
  )
)

echo Tray not found: no src\bypass_tray and no dist\*.exe
pause
exit /b 1

:verify
rem Wait up to 8 seconds for the process: a silent early exit must be visible.
powershell -NoProfile -Command "$deadline=(Get-Date).AddSeconds(8); while((Get-Date) -lt $deadline){ if($env:BYPASS_TRAY_MODE -eq 'cmdline'){ $p=Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and $_.Name -eq 'pythonw.exe' -and $_.CommandLine -like ('*' + $env:BYPASS_TRAY_MARK + '*') } } else { $p=Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -eq $env:BYPASS_TRAY_EXE } }; if($p){ exit 0 }; Start-Sleep -Milliseconds 500 }; exit 1"
if errorlevel 1 (
  echo Tray started but the process is not running.
  echo Check manually: .venv\Scripts\python.exe -m bypass_tray
  pause
  exit /b 1
)
exit /b 0
