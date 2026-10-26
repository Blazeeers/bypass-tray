"""Автозапуск: ярлык на ``run.bat`` в папке «Автозагрузка» пользователя.

Права администратора не нужны: ярлык лежит в профиле пользователя. Создаётся
через WScript.Shell (PowerShell), потому что чистого stdlib-способа сделать
.lnk нет.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .actions import CREATE_NO_WINDOW


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def run_bat() -> Path:
    return project_root() / "run.bat"


def link_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return (Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
            / "Startup" / "bypass-tray.lnk")


def is_enabled() -> bool:
    """Включён ли автозапуск (существует ли ярлык)."""
    try:
        return link_path().is_file()
    except OSError:
        return False


def _ps_quote(value: str) -> str:
    """Строка PowerShell в одинарных кавычках; апостроф внутри удваивается."""
    return "'" + str(value).replace("'", "''") + "'"


def _discard(path: Path) -> None:
    """Убирает временный файл; сбой очистки не должен скрывать причину."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def enable() -> bool:
    """Создаёт ярлык в автозагрузке. False — если не вышло.

    Новый ярлык собирается во временном файле и подменяет старый атомарно:
    при сбое PowerShell прежний автозапуск остаётся рабочим.
    """
    target = link_path()
    if not target.parent.is_dir():
        return False
    frozen = bool(getattr(sys, "frozen", False))
    if frozen:
        executable = Path(sys.executable).resolve()
        if not executable.is_file():
            return False
        workdir = executable.parent
    else:
        executable = run_bat()
        if not executable.is_file():
            return False
        workdir = project_root()
    # Ярлык собирается рядом и подменяет старый только после успеха: если
    # PowerShell недоступен, прежний автозапуск остаётся на месте.
    # WScript.Shell требует имя с расширением .lnk: иначе CreateShortcut
    # падает, и автозапуск не создаётся вовсе.
    temp = target.with_name(f"{target.stem}.tmp-{os.getpid()}{target.suffix}")
    try:
        temp.unlink(missing_ok=True)
    except OSError:
        return False
    script = (
        "$ErrorActionPreference = 'Stop';"
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut("
        + _ps_quote(str(temp)) + ");"
        "$s.TargetPath = " + _ps_quote(str(executable)) + ";"
        "$s.WorkingDirectory = " + _ps_quote(str(workdir)) + ";"
        + ("$s.IconLocation = " + _ps_quote(str(executable) + ",0") + ";"
           if frozen else "")
        + "$s.Description = 'Обходы';$s.Save()"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, timeout=30, creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        _discard(temp)
        return False
    if completed.returncode != 0 or not temp.is_file():
        _discard(temp)
        return False
    try:
        os.replace(temp, target)
    except OSError:
        _discard(temp)
        return False
    return target.is_file()


def disable() -> bool:
    """Удаляет ярлык автозапуска. True — если ярлыка больше нет."""
    target = link_path()
    try:
        target.unlink(missing_ok=True)
        return True
    except OSError:
        return not target.exists()
