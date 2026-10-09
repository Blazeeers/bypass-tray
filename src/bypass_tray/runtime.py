"""Среда выполнения: исходники или собранный ``.exe`` (PyInstaller).

Приложение одинаково работает и из исходников (`python -m bypass_tray`), и из
собранного одним файлом `bypass-tray.exe`. Различия собраны здесь:

* :func:`is_frozen` — запущено ли из ``.exe``;
* :func:`data_dir` — постоянная папка приложения в профиле пользователя
  (скачанные компоненты, ядро Xray) — она переживает перезапуск, в отличие от
  временной распаковки ``_MEIPASS``;
* :func:`module_argv` — как запустить это же приложение отдельным процессом
  (для сторожа Happ и перебора стратегий): в ``.exe`` это сам файл, в исходниках —
  ``python -m bypass_tray``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "bypass-tray"


def is_frozen() -> bool:
    """True, когда запущено из собранного PyInstaller-файла."""
    return bool(getattr(sys, "frozen", False))


def executable() -> str:
    """Путь к текущему исполняемому файлу (python или bypass-tray.exe)."""
    return sys.executable


def data_dir() -> Path:
    """Постоянная папка приложения в профиле пользователя.

    На Windows — ``%LOCALAPPDATA%\\bypass-tray`` (права администратора не
    нужны). Здесь лежат скачанные zapret, tg-ws-proxy и ядро Xray.
    """
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            base = str(Path(os.environ.get("USERPROFILE") or Path.home())
                       / "AppData" / "Local")
        root = Path(base)
    else:  # pragma: no cover - приложение рассчитано на Windows
        root = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    directory = root / APP_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def module_argv(*args: str) -> list[str]:
    """Команда для запуска этого же приложения отдельным процессом.

    В собранном ``.exe`` запускается сам файл с аргументами; в исходниках —
    ``python -m bypass_tray``.
    """
    if is_frozen():
        return [executable(), *args]
    return [executable(), "-m", "bypass_tray", *args]


def project_dir() -> Path | None:
    """Корень исходников, если запущено из них; иначе ``None``."""
    if is_frozen():
        return None
    return Path(__file__).resolve().parents[2]


def ensure_streams() -> None:
    """Гарантирует наличие ``stdout``/``stderr``.

    У собранного без консоли ``.exe`` (PyInstaller, ``console=False``) потоки
    равны ``None``, и любой ``print()`` падал бы. Перенаправляем в «никуда»:
    сообщения всё равно идут в логи/файлы, а дочерние режимы (перебор стратегий,
    сторож) продолжают работать.
    """
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115

