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
}


def _home(*parts: str) -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home()).joinpath(*parts)


def autodetect_zapret() -> str:
    for candidate in (
        _home("zapret-discord-youtube"),
        Path("C:/zapret-discord-youtube"),
        _home("Desktop", "zapret-discord-youtube"),
        _home("Downloads", "zapret-discord-youtube"),
    ):
        if candidate.is_dir():
            return str(candidate)
    return ""


def autodetect_tgws() -> str:
    names = ("TgWsProxy_windows.exe", "TgWsProxy.exe", "tg-ws-proxy.exe")
    folders = (
        _home("Downloads"),
        _home("Desktop"),
        _home("AppData", "Local", "Programs", "tg-ws-proxy"),
        _home("tg-ws-proxy"),
    )
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
    ):
        if path.is_file():
            return str(path)
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
