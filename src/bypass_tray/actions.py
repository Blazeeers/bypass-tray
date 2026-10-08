"""Действия: управление zapret (служба или .bat), tg-ws-proxy, Happ."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

from .status import ZAPRET_SERVICE, is_admin

CREATE_NO_WINDOW = 0x08000000 if sys.platform.startswith("win") else 0
SW_HIDE = 0


def _popen(args: list[str], cwd: str | None = None) -> subprocess.Popen:
    return subprocess.Popen(
        args,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
    )


# ---- процессы -------------------------------------------------------------


def _pids(prefix: str) -> set[int]:
    if psutil is None:
        return set()
    found: set[int] = set()
    for proc in psutil.process_iter(["name"]):
        if (proc.info.get("name") or "").lower().startswith(prefix.lower()):
            found.add(proc.pid)
    return found


def kill_process(name_prefix: str) -> dict:
    """Завершает процессы по префиксу имени.

    Возвращает ``{"killed": n, "denied": m}`` — отказ по правам не считается
    ошибкой, но вызывающий код обязан о нём сообщить.
    """
    if psutil is None:
        return {"killed": 0, "denied": 0}
    killed = 0
    denied = 0
    access_denied = getattr(psutil, "AccessDenied", Exception)
    for proc in psutil.process_iter(["name"]):
        name = proc.info.get("name") or ""
        if not name.lower().startswith(name_prefix.lower()):
            continue
        try:
            proc.terminate()
            killed += 1
        except access_denied:  # type: ignore[misc]
            denied += 1
        except Exception:  # noqa: BLE001
            pass
    return {"killed": killed, "denied": denied}


def hide_console_windows(name_prefix: str = "winws") -> int:
    """Скрывает окна консоли процессов с указанным префиксом имени.

    ``general (ALT).bat`` запускает winws через ``start /min``, поэтому окно
    появляется свёрнутым в панели задач; прячем его совсем.
    """
    if not sys.platform.startswith("win"):
        return 0
    pids = _pids(name_prefix)
    if not pids:
        return 0
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    hidden = 0
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, _lparam):
        nonlocal hidden
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and user32.IsWindowVisible(hwnd):
            user32.ShowWindow(hwnd, SW_HIDE)
            hidden += 1
        return True

    try:
        user32.EnumWindows(callback_type(visit), 0)
    except Exception:  # noqa: BLE001
        return 0
    return hidden


def _hide_later(name_prefix: str = "winws", attempts: int = 6) -> None:
    """Несколько раз пытается спрятать окно — процесс стартует не мгновенно."""

    def worker() -> None:
        for _ in range(attempts):
            time.sleep(1.0)
            if hide_console_windows(name_prefix):
                return

    threading.Thread(target=worker, daemon=True).start()


def _decode(raw: bytes | None) -> str:
    """Декодирует вывод консольных программ.

    ``sc.exe``/``tasklist`` печатают в OEM-кодировке консоли (cp866 на русской
    Windows), поэтому ``text=True`` падает с UnicodeDecodeError и stdout
    приходит как ``None``.
    """
    if not raw:
        return ""
    for encoding in ("utf-8", "cp866", "cp1251"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _run_capture(args: list[str], timeout: int = 10) -> str:
    try:
        completed = subprocess.run(
            args, capture_output=True, timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return _decode(completed.stdout)


# ---- запуск с повышением прав --------------------------------------------


def run_elevated(args: list[str], cwd: str | None = None) -> bool:
    """Запускает команду через UAC. Возвращает False, если пользователь отказал."""
    if not sys.platform.startswith("win"):
        try:
            _popen(args, cwd)
            return True
        except OSError:
            return False
    params = subprocess.list2cmdline(args[1:])
    try:
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", args[0], params, cwd, 1
        )
    except Exception:  # noqa: BLE001
        return False
    return int(result) > 32


# ---- zapret ---------------------------------------------------------------


def service_state(name: str = ZAPRET_SERVICE) -> str:
    """Состояние службы zapret: running / stopped / absent."""
    if not sys.platform.startswith("win"):
        return "absent"
    out = _run_capture(["sc.exe", "query", name])
    if "RUNNING" in out:
        return "running"
    if "STOPPED" in out:
        return "stopped"
    return "absent"


def control_service(action: str, name: str = ZAPRET_SERVICE) -> str:
    """Останавливает/запускает службу zapret (с UAC при необходимости).

    ``action`` — ``start`` | ``stop`` | ``restart``.
    """
    commands = {"start": ["start"], "stop": ["stop"], "restart": ["stop", "start"]}
    steps = commands.get(action)
    if not steps:
        raise ValueError(f"Неизвестное действие: {action}")
    for step in steps:
        if is_admin():
            _run_capture(["sc.exe", step, name], timeout=30)
        elif not run_elevated(["sc.exe", step, name]):
            raise PermissionError(
                "Нужны права администратора: подтвердите запрос UAC, чтобы "
                f"выполнить «sc {step} {name}»."
            )
    return service_state(name)


def start_zapret(cfg: dict, bat: str | None = None) -> dict:
    """Запускает выбранный ``general*.bat`` (для установки без службы)."""
    zapret_dir = Path(cfg.get("zapret_dir") or "")
    bat_name = bat or cfg.get("zapret_bat") or "general (ALT).bat"
    path = zapret_dir / bat_name
    if not path.is_file():
        raise FileNotFoundError(f"Не найден сценарий стратегии: {path}")

    killed = kill_process("winws")
    if killed["killed"] == 0 and killed["denied"] > 0:
        raise PermissionError(
            "winws.exe запущен службой zapret и не может быть остановлен без прав "
            "администратора. Используйте «Остановить службу»."
        )
    _popen(["cmd", "/c", str(path)], cwd=str(zapret_dir))
    _hide_later("winws")
    return killed


def stop_zapret(cfg: dict | None = None) -> dict:
    """Останавливает zapret: службу, если она есть, иначе процессы winws."""
    if service_state() in ("running", "stopped"):
        control_service("stop")
        return {"service": True, **kill_process("winws")}
    return {"service": False, **kill_process("winws")}


def restart_zapret(cfg: dict | None = None) -> dict:
    """Перезапускает обход: службу целиком либо заново выбранный .bat."""
    if service_state() in ("running", "stopped"):
        control_service("restart")
        return {"service": True}
    start_zapret(cfg or {})
    return {"service": False}


def open_service_bat(cfg: dict) -> bool:
    """Открывает ``service.bat`` от администратора — смена стратегии службы."""
    zapret_dir = Path(cfg.get("zapret_dir") or "")
    path = zapret_dir / "service.bat"
    if not path.is_file():
        raise FileNotFoundError(f"Не найден service.bat: {path}")
    return run_elevated(["cmd", "/c", str(path)], cwd=str(zapret_dir))


# ---- tg-ws-proxy ----------------------------------------------------------


def tgws_running() -> bool:
    return bool(_pids("TgWsProxy") or _pids("tg-ws-proxy"))


def start_tgws(cfg: dict) -> bool:
    """Запускает tg-ws-proxy, если он ещё не запущен (иначе False)."""
    if tgws_running():
        return False
    exe = cfg.get("tgws_exe") or ""
    if not exe or not Path(exe).is_file():
        raise FileNotFoundError(f"Не найден tg-ws-proxy: {exe or '(путь не задан)'}")
    _popen([exe], cwd=str(Path(exe).parent))
    return True


def stop_tgws() -> dict:
    return kill_process("TgWsProxy")


# ---- Happ -----------------------------------------------------------------


def open_happ(cfg: dict) -> None:
    exe = cfg.get("happ_exe") or ""
    if not exe or not Path(exe).is_file():
        raise FileNotFoundError(f"Не найден Happ: {exe or '(путь не задан)'}")
    if sys.platform.startswith("win"):
        os.startfile(exe)  # type: ignore[attr-defined]
    else:
        _popen([exe])
