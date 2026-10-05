"""Действия: запуск/остановка zapret и tg-ws-proxy, открытие Happ."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

CREATE_NO_WINDOW = 0x08000000 if sys.platform.startswith("win") else 0


def _popen(args: list[str], cwd: str | None = None) -> subprocess.Popen:
    return subprocess.Popen(
        args,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
    )


def kill_process(name_prefix: str) -> int:
    if psutil is None:
        return 0
    killed = 0
    for proc in psutil.process_iter(["name"]):
        name = proc.info.get("name") or ""
        if name.lower().startswith(name_prefix.lower()):
            try:
                proc.terminate()
                killed += 1
            except Exception:  # noqa: BLE001
                pass
    return killed


def start_zapret(cfg: dict, bat: str | None = None) -> None:
    zapret_dir = Path(cfg.get("zapret_dir") or "")
    bat_name = bat or cfg.get("zapret_bat") or "general (ALT).bat"
    path = zapret_dir / bat_name
    if not path.is_file():
        raise FileNotFoundError(f"Не найден сценарий стратегии: {path}")
    kill_process("winws")
    _popen(["cmd", "/c", str(path)], cwd=str(zapret_dir))


def stop_zapret() -> int:
    return kill_process("winws")


def start_tgws(cfg: dict) -> None:
    exe = cfg.get("tgws_exe") or ""
    if not exe or not Path(exe).is_file():
        raise FileNotFoundError(f"Не найден tg-ws-proxy: {exe or '(путь не задан)'}")
    _popen([exe])


def open_happ(cfg: dict) -> None:
    exe = cfg.get("happ_exe") or ""
    if not exe or not Path(exe).is_file():
        raise FileNotFoundError(f"Не найден Happ: {exe or '(путь не задан)'}")
    if sys.platform.startswith("win"):
        os.startfile(exe)  # type: ignore[attr-defined]
    else:
        _popen([exe])
