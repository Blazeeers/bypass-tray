"""Действия: управление zapret (служба или .bat) и tg-ws-proxy."""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import runtime

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

from .status import ZAPRET_SERVICE, is_admin, port_open

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


def _shell_execute(args: list[str], cwd: str | None = None) -> int:
    """Один вызов ShellExecuteW; возвращает код Win32."""
    if not sys.platform.startswith("win"):
        try:
            _popen(args, cwd)
            return 42
        except OSError:
            return 2
    params = subprocess.list2cmdline(args[1:])
    try:
        return int(ctypes.windll.shell32.ShellExecuteW(
            None, "runas", args[0], params, cwd, 1
        ))
    except Exception:  # noqa: BLE001
        return 0


def _elevate(args: list[str], cwd: str | None = None, attempts: int = 3,
             delay: float = 0.8) -> tuple[bool, int]:
    """UAC-запуск с повтором: код 5 (SE_ERR_ACCESSDENIED) бывает транзиентным.

    Возвращает ``(ok, last_code)``. Успех — только код > 32, но вызывающий
    обязан проверить фактическое состояние: команда могла отработать даже
    при «неудачном» коде.
    """
    last = 0
    rounds = max(1, int(attempts))
    for attempt in range(rounds):
        last = _shell_execute(args, cwd)
        if last > 32:
            return True, last
        if attempt < rounds - 1:
            time.sleep(max(0.0, float(delay)))
    return False, last


def run_elevated(args: list[str], cwd: str | None = None) -> bool:
    """Совместимость: True, если UAC-запуск принят (код > 32)."""
    return _elevate(args, cwd)[0]


def _batch_dir() -> Path:
    """Папка для временных .bat: data_dir, иначе %TEMP%."""
    try:
        directory = runtime.data_dir()
        directory.mkdir(parents=True, exist_ok=True)
        return directory
    except OSError:
        return Path(tempfile.gettempdir())


#: Сколько живёт временный .bat, пока его не заберёт elevated-процесс.
BATCH_STALE_SECONDS = 120


def _cleanup_stale_batches(directory: Path) -> None:
    """Удаляет старые ``bypass-tray-elev-*.bat``; ошибки игнорируются."""
    now = time.time()
    try:
        files = list(Path(directory).glob("bypass-tray-elev-*.bat"))
    except OSError:
        return
    for path in files:
        try:
            if now - path.stat().st_mtime <= BATCH_STALE_SECONDS:
                continue
            path.unlink()
        except OSError:
            pass


def _elevate_batch(lines: list[str], attempts: int = 3,
                   delay: float = 0.8) -> tuple[bool, int]:
    """Элевирует временный .bat со списком команд (по одной в строке).

    ``ShellExecuteW`` с длинной ``cmd /c "…&…"`` на этой машине иногда
    возвращает 5 и ничего не делает; элевация самого .bat надёжна.
    Файл НЕ удаляем сразу: ShellExecute асинхронный и elevated-cmd ещё не
    прочитал его. Старые файлы вычищаются при следующем вызове.
    """
    path: Path | None = None
    try:
        directory = _batch_dir()
        _cleanup_stale_batches(directory)
        fd, name = tempfile.mkstemp(prefix="bypass-tray-elev-", suffix=".bat",
                                    dir=str(directory))
        path = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write("@echo off\r\n")
            for line in lines:
                handle.write(str(line).replace("\r", " ").replace("\n", " ")
                             + "\r\n")
        return _elevate([str(path)], cwd=str(path.parent),
                        attempts=attempts, delay=delay)
    except (OSError, ValueError):
        # Запись не удалась — файл ещё не запускался, его можно убрать.
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        return False, 0


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
    if "PENDING" in out:
        return "pending"
    return "absent"


def _service_start_lines(name: str, action: str, drivers: list[str],
                         start_types: dict[str, str]) -> list[str]:
    """Команды .bat для запуска/перезапуска службы и драйверов."""
    parts: list[str] = []
    for item in drivers:
        if start_types.get(item) == "4":
            parts.append(f"sc.exe config {item} start= demand")
        parts.append(f"sc.exe start {item}")
    if action == "restart":
        parts.append(f"sc.exe stop {name}")
    parts.append(f"sc.exe start {name}")
    for item in drivers:
        if start_types.get(item) == "4":
            parts.append(f"sc.exe config {item} start= disabled")
    return parts


def control_service(action: str, name: str = ZAPRET_SERVICE) -> str:
    """Останавливает/запускает службу zapret (с UAC при необходимости).

    ``action`` — ``start`` | ``stop`` | ``restart``.
    """
    commands = {"start": ["start"], "stop": ["stop"], "restart": ["stop", "start"]}
    steps = commands.get(action)
    if not steps:
        raise ValueError(f"Неизвестное действие: {action}")
    if action in ("start", "restart") and sys.platform.startswith("win"):
        if not is_admin():
            # Один UAC: драйвер (может быть DISABLED) и служба вместе.
            names = _windivert_services()
            states = {item: service_state(item) for item in names}
            pending = [item for item, state in states.items()
                       if state != "running"]
            originals = {item: _service_start_type(item) for item in pending}
            parts = _service_start_lines(name, action, pending, originals)
            code = 0
            # Код ShellExecute бывает транзиентным; решает фактическое состояние.
            for _attempt in range(2):
                _ok, code = _elevate_batch(parts)
                if _wait_service_state(name, "running", 8.0) == "running":
                    return "running"
            raise PermissionError(
                "Нужны права администратора: подтвердите запрос UAC, чтобы "
                f"запустить службу {name} и драйвер WinDivert (код {code}).")
        # Служба zapret не стартует без загруженного WinDivert (может быть
        # DISABLED после ручной остановки) — поднимаем драйвер заранее.
        _ensure_windivert()
    for step in steps:
        if is_admin():
            _run_capture(["sc.exe", step, name], timeout=30)
            continue
        target = "stopped" if step == "stop" else "running"
        code = 0
        for _attempt in range(2):
            _ok, code = _elevate_batch([f"sc.exe {step} {name}"])
            if target == "stopped":
                state = _wait_services_stopped([name], 8.0).get(name, "")
                if state in ("stopped", "absent"):
                    break
            elif _wait_service_state(name, "running", 8.0) == "running":
                break
        else:
            raise PermissionError(
                "Нужны права администратора: подтвердите запрос UAC, чтобы "
                f"выполнить «sc {step} {name}» (код {code}).")
    return service_state(name)


#: Имя копии сценария без проверки обновлений: ``~bt_<stem>.bat``.
SANITIZED_PREFIX = "~bt_"

#: ``call service.bat check_updates`` подтягивает версию с GitHub и, если она
#: новее локальной, делает ``start "" "<релиз>"`` — открывает браузер. Прочие
#: ``call service.bat ...`` нужны: они задают ``GameFilterTCP/UDP`` и списки.
_CHECK_UPDATES_RE = re.compile(
    r"(?mi)^[ \t]*call[ \t]+\"?service\.bat\"?[ \t]+check_updates\b[^\r\n]*"
)


def _strip_check_updates(text: str) -> tuple[str, int]:
    """Вырезает вызовы ``check_updates`` из текста сценария (``rem`` вместо них)."""
    return _CHECK_UPDATES_RE.subn("rem", text)


def _ascii_stem(stem: str) -> str:
    """Стем для имени копии: не-ASCII (кириллица) заменяется на ``_``."""
    return "".join(char if char.isascii() else "_" for char in stem) or "bat"


def sanitized_bat(path: Path) -> Path:
    """Копия сценария без ``call service.bat check_updates``.

    Иначе каждый запуск ``general*.bat`` проверяет версию на GitHub и при
    расхождении открывает страницу релизов в браузере: «Тест стратегий»
    запускает 22 сценария подряд и открывал 22 вкладки.

    Копия кладётся рядом с оригиналом: ``%~dp0`` у неё тот же каталог, поэтому
    относительные ``bin``/``lists`` продолжают работать. Если замен не было,
    файл не читается или копию не удалось записать — возвращается исходный путь.
    """
    path = Path(path)
    if not path.is_file():
        return path
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return path
    stripped, replaced = _strip_check_updates(text)
    if not replaced:
        return path
    target = path.parent / f"{SANITIZED_PREFIX}{_ascii_stem(path.stem)}.bat"
    try:
        target.write_text(stripped, encoding="utf-8")
    except OSError:
        return path
    return target


def cleanup_sanitized(zapret_dir: Path, keep: Path | None = None) -> int:
    """Удаляет оставшиеся копии ``~bt_*.bat``, кроме ``keep``.

    Копии одноразовые: при следующем запуске сценарий пересобирается. Больше
    ничего в папке zapret не трогается, ошибки удаления игнорируются.
    """
    keep_name = Path(keep).name.lower() if keep else ""
    removed = 0
    try:
        candidates = list(Path(zapret_dir).glob(f"{SANITIZED_PREFIX}*.bat"))
    except OSError:
        return 0
    for old in candidates:
        if old.name.lower() == keep_name:
            continue
        try:
            old.unlink()
        except OSError:
            continue
        removed += 1
    return removed


#: Службы-драйверы WinDivert. Имена в системе встречаются в разном регистре,
#: поэтому и сравнение, и остановка идут без учёта регистра.
WINDIVERT_KNOWN = ("WinDivert", "windivert")
_WINDIVERT_RE = re.compile(r"(?i)^windivert")
#: Сколько ждать перехода службы в STOPPED после команды.
WINDIVERT_WAIT = 5.0


def _windivert_services() -> list[str]:
    """Имена служб WinDivert: известные плюс всё, что нашлось в ``sc``.

    Ошибки ``sc.exe`` не фатальны: при отсутствии службы возвращается пустой
    список, и остановка считается уже выполненной.
    """
    found: dict[str, str] = {}
    for kind in ("driver", "service"):
        out = _run_capture(["sc.exe", "query", "type=", kind], timeout=10)
        for line in out.splitlines():
            if ":" not in line:
                continue
            value = line.split(":", 1)[1].strip()
            if value and _WINDIVERT_RE.match(value):
                # Регистр сохраняем: в системе встречаются оба написания, и
                # остановить нужно каждое найденное имя.
                found.setdefault(value, value)
    for name in WINDIVERT_KNOWN:
        found.setdefault(name, name)
    return sorted(found.values(), key=str.lower)


def _wait_services_stopped(names: list[str], timeout: float) -> dict[str, str]:
    """Опрашивает состояния служб, пока все не станут STOPPED/absent."""
    deadline = time.time() + max(0.0, timeout)
    states: dict[str, str] = {}
    while True:
        states = {name: service_state(name) for name in names}
        if all(state in ("stopped", "absent") for state in states.values()):
            return states
        if time.time() >= deadline:
            return states
        time.sleep(0.25)


def _wait_service_state(name: str, target: str, timeout: float) -> str:
    """Ждёт целевого состояния службы; возвращает последнее наблюдённое."""
    deadline = time.time() + max(0.0, timeout)
    state = service_state(name)
    while state != target and time.time() < deadline:
        time.sleep(0.25)
        state = service_state(name)
    return state


def _wait_process_gone(name: str, timeout: float) -> None:
    deadline = time.time() + max(0.0, timeout)
    while _pids(name) and time.time() < deadline:
        time.sleep(0.25)


def stop_windivert() -> dict:
    """Останавливает все службы-драйверы WinDivert. Идемпотентно.

    ``state``: ``stopped`` | ``not_running`` | ``absent`` | ``denied`` |
    ``failed``. Нет службы или она уже STOPPED — не ошибка.
    """
    names = _windivert_services()
    states = {name: service_state(name) for name in names}
    running = [name for name, state in states.items() if state == "running"]
    if not running:
        return {"state": "not_running" if any(
            state == "stopped" for state in states.values()) else "absent",
            "services": states}
    if is_admin():
        for name in running:
            _run_capture(["sc.exe", "stop", name], timeout=15)
        states = _wait_services_stopped(running, WINDIVERT_WAIT)
    else:
        # Без кавычек: list2cmdline экранирует внутренние " как \", и cmd.exe
        # получает имя службы вместе с кавычками (ошибка 1060). Имена
        # WinDivert безопасны — они пришли из ^windivert.
        commands = [f"sc.exe stop {name}" for name in running]
        code = 0
        for _attempt in range(2):
            _ok, code = _elevate_batch(commands)
            states = _wait_services_stopped(running, WINDIVERT_WAIT)
            if all(state in ("stopped", "absent")
                   for state in states.values()):
                return {"state": "stopped", "services": states}
        raise PermissionError(
            "Нужны права администратора: подтвердите запрос UAC, чтобы "
            f"остановить драйверы WinDivert (код {code}).")
    ok = all(state in ("stopped", "absent") for state in states.values())
    return {"state": "stopped" if ok else "failed", "services": states}


def _stop_all_lines(stop_service: bool, drivers: list[str]) -> list[str]:
    """Команды для .bat: служба → winws → драйверы, по одной в строке."""
    parts: list[str] = []
    if stop_service:
        parts.append(f"sc.exe stop {ZAPRET_SERVICE}")
    parts.append("taskkill /f /im winws.exe")
    if drivers:
        # Дать winws завершиться и отпустить драйвер до его остановки.
        parts.append("ping -n 2 127.0.0.1 >nul")
        parts.extend(f"sc.exe stop {name}" for name in drivers)
    return parts


def _stop_all_command(stop_service: bool, drivers: list[str]) -> str:
    """Совместимость: те же команды одной строкой через ``&``."""
    return " & ".join(_stop_all_lines(stop_service, drivers))


#: Коды ``START_TYPE`` из ``sc qc`` и имена для ``sc config start=``.
_START_TYPE_NAMES = {"0": "boot", "1": "system", "2": "auto",
                     "3": "demand", "4": "disabled"}


def _service_start_type(name: str) -> str:
    """Код типа запуска службы (``4`` — DISABLED); пусто — не распознан."""
    out = _run_capture(["sc.exe", "qc", name], timeout=10)
    match = re.search(
        r"(?im)(?:START_TYPE|\u0422\u0418\u041f_\u0417\u0410\u041f\u0423\u0421\u041a\u0410)"
        r"\s*:\s*([0-9]+)", out)
    if match:
        return match.group(1)
    # Локализованный вывод: код мог не найтись, но DISABLED узнаётся по слову.
    if re.search(r"(?i)\bDISABLED\b", out):
        return "4"
    return ""


def _ensure_windivert(timeout: float = 3.0) -> str:
    """Поднимает драйвер WinDivert, снимая DISABLED при необходимости.

    Службы-драйверы бывают в состоянии ``START_TYPE : 4 DISABLED``: обычный
    ``sc start`` даёт 1058. Поэтому тип запоминается, временно ставится
    ``demand``, драйвер стартует, затем исходный тип возвращается (уже
    загруженному драйверу это не мешает). Ошибки не роняют старт zapret.
    """
    if not sys.platform.startswith("win"):
        return "absent"
    names = _windivert_services()
    if not names:
        return "absent"
    states = {name: service_state(name) for name in names}
    pending = [name for name, state in states.items() if state != "running"]
    if not pending:
        return "running"

    originals = {name: _service_start_type(name) for name in pending}
    commands: list[list[str]] = []
    for name in pending:
        if originals.get(name) == "4":
            commands.append(["sc.exe", "config", name, "start=", "demand"])
        commands.append(["sc.exe", "start", name])
    # Возврат DISABLED — уже после старта, best-effort.
    for name in pending:
        if originals.get(name) == "4":
            commands.append(["sc.exe", "config", name, "start=", "disabled"])
    if not is_admin():
        _elevate_batch([subprocess.list2cmdline(args) for args in commands])
    else:
        for args in commands:
            _run_capture(args, timeout=10)

    deadline = time.time() + max(0.0, timeout)
    while time.time() < deadline:
        states = {name: service_state(name) for name in names}
        if any(state == "running" for state in states.values()):
            return "running"
        time.sleep(0.25)
    return service_state("WinDivert")


def start_zapret(cfg: dict, bat: str | None = None) -> dict:
    """Запускает выбранный ``general*.bat`` (для установки без службы)."""
    raw_dir = str(cfg.get("zapret_dir") or "").strip()
    if not raw_dir:
        raise FileNotFoundError("Не задана папка zapret")
    zapret_dir = Path(raw_dir)
    bat_name = bat or cfg.get("zapret_bat") or "general (ALT).bat"
    path = zapret_dir / bat_name
    if not path.is_file():
        raise FileNotFoundError(f"Не найден сценарий стратегии: {path}")
    # Запускаем копию без проверки обновлений: она и открывает браузер.
    launch = sanitized_bat(path)
    cleanup_sanitized(zapret_dir, keep=launch)

    killed = kill_process("winws")
    if killed["killed"] == 0 and killed["denied"] > 0:
        raise PermissionError(
            "winws.exe запущен службой zapret и не может быть остановлен без прав "
            "администратора. Используйте «Остановить службу»."
        )
    # Драйвер поднимаем ДО winws: если он DISABLED, winws сам его не запустит.
    windivert = _ensure_windivert()
    _popen(["cmd", "/c", str(launch)], cwd=str(zapret_dir))
    _hide_later("winws")
    # winws стартует из .bat не мгновенно; короткий poll для честного отчёта.
    deadline = time.time() + 2.5
    while not _pids("winws") and time.time() < deadline:
        time.sleep(0.25)
    return {**killed, "windivert": windivert, "winws": bool(_pids("winws"))}


def stop_zapret(cfg: dict | None = None) -> dict:
    """Останавливает zapret и драйверы WinDivert.

    Порядок важен: служба zapret → winws → драйверы. Драйвер нельзя
    остановить, пока winws держит его открытым. Без прав администратора всё
    уходит одной elevated-командой, чтобы был один запрос UAC.
    """
    service = service_state()
    service_exists = service in ("running", "stopped")
    drivers = _windivert_services()
    running_drivers = [name for name in drivers
                       if service_state(name) == "running"]

    if not is_admin():
        commands = _stop_all_lines(service == "running", running_drivers)
        code = 0
        states: dict[str, str] = {}
        stopped = False
        for _attempt in range(2):
            _ok, code = _elevate_batch(commands)
            # Код ShellExecute бывает 5/1223, но команда могла выполниться:
            # успех определяем только по фактическому состоянию.
            _wait_services_stopped([ZAPRET_SERVICE], 3.0)
            _wait_process_gone("winws", 2.0)
            if running_drivers:
                states = _wait_services_stopped(running_drivers, WINDIVERT_WAIT)
            else:
                states = {name: service_state(name) for name in drivers}
            stopped = (
                service_state(ZAPRET_SERVICE) in ("stopped", "absent")
                and not _pids("winws")
                and all(state in ("stopped", "absent")
                        for state in states.values())
            )
            if stopped:
                break
        if not stopped:
            raise PermissionError(
                "Нужны права администратора: подтвердите запрос UAC, чтобы "
                f"остановить zapret и драйверы WinDivert (код {code}).")
        if running_drivers:
            windivert = "stopped"
        else:
            windivert = "not_running" if any(
                state == "stopped" for state in states.values()) else "absent"
        return {"service": service_exists, **kill_process("winws"),
                "windivert": windivert}

    if service_exists:
        control_service("stop")
    killed = kill_process("winws")
    # Дать winws завершиться, иначе драйвер останется занятым.
    _wait_process_gone("winws", 3.0)
    wind = stop_windivert()
    return {"service": service_exists, **killed, "windivert": wind["state"]}


def restart_zapret(cfg: dict | None = None) -> dict:
    """Перезапускает обход: службу целиком либо заново выбранный .bat."""
    if service_state() in ("running", "stopped"):
        control_service("restart")
        return {"service": True}
    start_zapret(cfg or {})
    return {"service": False}


def open_service_bat(cfg: dict) -> bool:
    """Открывает ``service.bat`` от администратора — смена стратегии службы."""
    raw_dir = str(cfg.get("zapret_dir") or "").strip()
    if not raw_dir:
        raise FileNotFoundError("Не задана папка zapret")
    zapret_dir = Path(raw_dir)
    path = zapret_dir / "service.bat"
    if not path.is_file():
        raise FileNotFoundError(f"Не найден service.bat: {path}")
    return run_elevated(["cmd", "/c", str(path)], cwd=str(zapret_dir))


# ---- tg-ws-proxy ----------------------------------------------------------


def tgws_running() -> bool:
    return bool(_pids("TgWsProxy") or _pids("tg-ws-proxy"))


#: Сколько ждать открытия порта после запуска tg-ws-proxy.
TGWS_PORT_WAIT = 4.0
#: Шаг опроса порта: Popen отвечает за процесс, а порт открывается не сразу.
TGWS_PORT_STEP = 0.25


def tgws_port(cfg: dict) -> int:
    try:
        return int(cfg.get("tgws_port") or 1443)
    except (TypeError, ValueError):
        return 1443


def start_tgws(cfg: dict) -> bool:
    """Запускает tg-ws-proxy и дожидается открытия порта.

    ``True`` — только если порт действительно слушает. Раньше успехом
    считался сам факт ``Popen``, поэтому упавший сразу после старта прокси
    отрапортовал «запустил» и в панели, и в надзирателе.
    """
    if tgws_running():
        return False
    exe = cfg.get("tgws_exe") or ""
    if not exe or not Path(exe).is_file():
        raise FileNotFoundError(f"Не найден tg-ws-proxy: {exe or '(путь не задан)'}")
    port = tgws_port(cfg)
    process = _popen([exe], cwd=str(Path(exe).parent))
    poll = getattr(process, "poll", None)
    deadline = time.monotonic() + TGWS_PORT_WAIT
    while True:
        if port_open(port):
            return True
        if poll is not None and poll() is not None:
            # Процесс умер: ждать нечего, порт уже не откроется.
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(TGWS_PORT_STEP)


def stop_tgws() -> dict:
    return kill_process("TgWsProxy")
