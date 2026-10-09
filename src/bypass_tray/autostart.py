"""Автозапуск приложения при входе в Windows.

Кладёт ярлык в пользовательскую папку «Автозагрузка» — права администратора не
нужны. В собранном ``.exe`` ярлык указывает на сам файл, в исходниках — на
``run.bat``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from . import runtime

CREATE_NO_WINDOW = 0x08000000 if sys.platform.startswith("win") else 0


def startup_dir() -> Path | None:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    path = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    return path if path.is_dir() else None


def shortcut_path() -> Path | None:
    directory = startup_dir()
    return (directory / "bypass-tray.lnk") if directory else None


def _ps(value: str) -> str:
    """Строка в одинарных кавычках для PowerShell."""
    return "'" + value.replace("'", "''") + "'"


def _launch_target() -> tuple[Path, Path] | None:
    """(что запускать, рабочая папка) — ``.exe`` при сборке, иначе ``run.bat``."""
    if runtime.is_frozen():
        target = Path(runtime.executable())
        return target, target.parent
    project = runtime.project_dir()
    if project is None:
        return None
    target = project / "run.bat"
    if not target.is_file():
        return None
    return target, project


def enable() -> bool:
    """Создаёт ярлык автозапуска. Возвращает True, если ярлык на месте."""
    if not sys.platform.startswith("win"):
        return False
    directory = startup_dir()
    launch = _launch_target()
    if directory is None or launch is None:
        return False
    target, working = launch
    link = directory / "bypass-tray.lnk"
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut("
        + _ps(str(link)) + ");"
        "$s.TargetPath = " + _ps(str(target)) + ";"
        "$s.WorkingDirectory = " + _ps(str(working)) + ";"
        "$s.Description = 'bypass-tray';$s.Save()"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", script],
                       capture_output=True, timeout=30, creationflags=CREATE_NO_WINDOW)
        return link.is_file()
    except Exception:  # noqa: BLE001
        return False


def disable() -> bool:
    link = shortcut_path()
    if link is None:
        return False
    try:
        link.unlink()
        return True
    except OSError:
        return False


def enabled() -> bool:
    link = shortcut_path()
    try:
        return bool(link and link.is_file())
    except OSError:
        return False
