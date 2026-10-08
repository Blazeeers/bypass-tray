"""Конфиг и состояние приложения."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

APP_NAME = "bypass-tray"


def is_windows() -> bool:
    return sys.platform.startswith("win")


def config_dir() -> Path:
    base = os.environ.get("APPDATA") if is_windows() else None
    root = Path(base) if base else Path.home() / ".config"
    directory = root / APP_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


CONFIG_FILE = config_dir() / "config.json"
STATE_FILE = config_dir() / "state.json"

DEFAULTS: dict = {
    "zapret_dir": "",
    "zapret_bat": "general (ALT).bat",
    "tgws_exe": "",
    "tgws_port": 1443,
    "happ_exe": "",
    "refresh_seconds": 5,
    "update_check_hours": 24,
    # auto — по системной теме Windows, либо принудительно light / dark.
    "theme": "auto",
    # Дополнительные домены, которые нужно вести через туннель Happ.
    "happ_extra_sites": [],
    # Уводить YouTube/Discord в туннель, когда zapret или tg-ws-proxy упали.
    "happ_auto_fallback": False,
    # --- собственный клиент Xray (без Happ) ---
    # Подписка: отдаёт список серверов vless://. Укажите свою — в репозитории
    # её нет намеренно, это персональный ключ доступа.
    "xray_subscription": "",
    # Пусто = <проект>\bin\xray.exe; иначе путь к своему ядру.
    "xray_exe": "",
    # Локальный порт прокси (SOCKS и HTTP на одном порту).
    "xray_port": 10818,
    # Прописывать прокси в систему, чтобы им пользовались браузеры.
    "xray_system_proxy": True,
    # Дополнительные домены, которые тоже вести через прокси.
    "xray_extra_domains": [],
    # Уводить YouTube/Discord в прокси, когда zapret не работает.
    "xray_stream_fallback": True,
}


def _home(*parts: str) -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home()).joinpath(*parts)


def _drives() -> list[Path]:
    """Корни существующих дисков (A:\\ … Z:\\)."""
    return [
        Path(f"{letter}:/")
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ"
        if Path(f"{letter}:/").is_dir()
    ]


def _is_zapret_dir(path: Path) -> bool:
    """Папка zapret: есть `bin\\winws.exe` или хотя бы `general*.bat`."""
    try:
        if not path.is_dir():
            return False
    except OSError:
        return False
    if (path / "bin" / "winws.exe").is_file():
        return True
    try:
        return next(path.glob("general*.bat"), None) is not None
    except OSError:
        return False


def _search_dirs(root: Path, depth: int, want) -> Path | None:
    """Ограниченный по глубине поиск каталога под `root`."""
    try:
        entries = [p for p in root.iterdir() if p.is_dir()]
    except OSError:
        return None
    for entry in entries:
        try:
            if want(entry):
                return entry
        except OSError:
            continue
    if depth <= 0:
        return None
    for entry in entries:
        found = _search_dirs(entry, depth - 1, want)
        if found is not None:
            return found
    return None


def autodetect_zapret() -> str:
    """Ищет папку zapret по типовым местам, затем по всем дискам.

    Признак папки — наличие `general*.bat` либо `bin\\winws.exe`.
    """
    candidates = [
        _home("zapret-discord-youtube"),
        Path("C:/zapret-discord-youtube"),
        _home("Desktop", "zapret-discord-youtube"),
        _home("Downloads", "zapret-discord-youtube"),
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return str(candidate)
    for drive in _drives():
        for name in ("zapret-discord-youtube", "Zapret", "zapret"):
            candidate = drive / name
            if candidate.is_dir():
                return str(candidate)
        # Вложенные раскладки вроде `G:\Перенос с ПК\Zapret`.
        found = _search_dirs(drive, 2, _is_zapret_dir)
        if found is not None:
            return str(found)
    return ""


def autodetect_tgws() -> str:
    names = ("TgWsProxy_windows.exe", "TgWsProxy.exe", "tg-ws-proxy.exe")
    folders = [
        _home("Downloads"),
        _home("Desktop"),
        _home("AppData", "Local", "Programs", "tg-ws-proxy"),
        _home("tg-ws-proxy"),
    ]
    folders += [drive / name for drive in _drives() for name in ("tg-ws-proxy", "TgWsProxy")]
    for folder in folders:
        for name in names:
            path = folder / name
            if path.is_file():
                return str(path)
    return ""


def autodetect_happ() -> str:
    for path in (
        _home("AppData", "Local", "Programs", "Happ", "Happ.exe"),
        _home("AppData", "Local", "Happ", "Happ.exe"),
        Path("C:/Program Files/Happ/Happ.exe"),
        Path("C:/Program Files (x86)/Happ/Happ.exe"),
        # Установщик Happ кладёт файл в подпапку вендора.
        Path("C:/Program Files/FlyFrogLLC/Happ/Happ.exe"),
        Path("C:/Program Files (x86)/FlyFrogLLC/Happ/Happ.exe"),
        _home("AppData", "Local", "Programs", "FlyFrogLLC", "Happ", "Happ.exe"),
    ):
        if path.is_file():
            return str(path)
    # Запущенный Happ: берём путь прямо у процесса, если он доступен.
    try:
        import psutil

        for proc in psutil.process_iter(["name", "exe"]):
            if (proc.info.get("name") or "").lower() == "happ.exe":
                exe = proc.info.get("exe") or ""
                if exe and Path(exe).is_file():
                    return exe
    except Exception:  # noqa: BLE001
        pass
    return ""


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    if not cfg.get("zapret_dir"):
        cfg["zapret_dir"] = autodetect_zapret()
    if not cfg.get("tgws_exe"):
        cfg["tgws_exe"] = autodetect_tgws()
    if not cfg.get("happ_exe"):
        cfg["happ_exe"] = autodetect_happ()
    save_config(cfg)
    return cfg


def save_config(cfg: dict) -> None:
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
