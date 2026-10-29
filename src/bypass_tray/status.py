"""Сбор статусов: процессы winws/tg-ws-proxy, порты, стратегии zapret."""

from __future__ import annotations

import re
import socket
import subprocess
import sys
from pathlib import Path

from .config import normalise_routing_mode

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

TGWS_RE = re.compile(r"tg[-_ ]?ws[-_ ]?proxy", re.I)

#: ``--dpi-desync=fake`` в .bat и ``--dpi-desync fake`` в образе службы.
DPI_DESYNC_RE = re.compile(r"--dpi-desync(?:=|\s+)([^\s\"]+)")

#: Любая опция ``--dpi-desync*`` вместе со значением (для отпечатка стратегии).
DPI_OPT_RE = re.compile(r"--(dpi-desync[\w-]*)(?:=|\s+)(\"[^\"]*\"|\S+)")
#: Переменные .bat вида ``%BIN%`` — при сравнении со службой они раскрыты.
BAT_VAR_RE = re.compile(r"%[^%]+%")

ZAPRET_SERVICE = "zapret"


def is_admin() -> bool:
    """Запущены ли мы с правами администратора."""
    if not sys.platform.startswith("win"):
        return True
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def _processes() -> list[tuple[str, str]]:
    if psutil is not None:
        result = []
        for proc in psutil.process_iter(["name", "cmdline"]):
            name = proc.info.get("name") or ""
            cmdline = " ".join(proc.info.get("cmdline") or [])
            result.append((name, cmdline))
        return result
    try:
        out = subprocess.run(
            ["tasklist", "/fo", "csv", "/nh"], capture_output=True, timeout=10
        ).stdout
    except OSError:
        return []
    # Вывод в OEM-кодировке консоли: text=True здесь падает с UnicodeDecodeError.
    for encoding in ("utf-8", "cp866", "cp1251"):
        try:
            text = (out or b"").decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = (out or b"").decode("utf-8", errors="replace")
    procs = []
    for line in text.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if parts:
            procs.append((parts[0], ""))
    return procs


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _desync_values(text: str) -> tuple[str, ...]:
    return tuple(DPI_DESYNC_RE.findall(text or ""))


def strategy_digest(cmdline: str) -> str:
    """Краткий дайджест стратегии из командной строки ``winws.exe``."""
    modes: list[str] = []
    for mode in _desync_values(cmdline):
        if mode not in modes:
            modes.append(mode)
    return " ".join(modes[:3])


def service_image_path(name: str = ZAPRET_SERVICE) -> str:
    """Командная строка службы zapret из реестра.

    Читается **без** прав администратора — в отличие от командной строки
    процесса ``winws.exe``, которая принадлежит SYSTEM и обычному
    пользователю не видна.
    """
    if not sys.platform.startswith("win"):
        return ""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            rf"SYSTEM\CurrentControlSet\Services\{name}",
        ) as key:
            return str(winreg.QueryValueEx(key, "ImagePath")[0])
    except OSError:
        return ""


def _service_exists(name: str = ZAPRET_SERVICE) -> bool:
    if not sys.platform.startswith("win"):
        return False
    try:
        import winreg

        winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf"SYSTEM\CurrentControlSet\Services\{name}").Close()
        return True
    except OSError:
        return False


def _fingerprint(text: str) -> tuple[tuple[str, str], ...]:
    """Отпечаток стратегии: пары (опция, значение) по порядку следования.

    Переменные ``%BIN%``/``%LISTS%`` из .bat вырезаются, пути сводятся к имени
    файла — так аргументы .bat и раскрытые аргументы службы становятся
    сопоставимыми.
    """
    normalised = BAT_VAR_RE.sub("", text or "")
    result: list[tuple[str, str]] = []
    for option, raw in DPI_OPT_RE.findall(normalised):
        value = raw.strip().strip('"').strip("'").rstrip("^").strip().lower()
        value = value.replace("\\", "/").rstrip("/").split("/")[-1]
        result.append((option.lower(), value))
    return tuple(result)


def identify_strategy(bats: dict[str, str], cmdline: str) -> str:
    """Определяет, какой ``general*.bat`` соответствует запущенному winws.

    Сравнивается отпечаток опций ``--dpi-desync*``: он различает даже
    стратегии с одинаковой последовательностью ``--dpi-desync``, но разными
    fake-файлами. Возвращает имя файла либо пустую строку, если совпадение
    неоднозначно.
    """
    running = _fingerprint(cmdline)
    if not running:
        return ""
    best_name = ""
    best_score = 0
    second_score = 0
    for name, text in bats.items():
        candidate = _fingerprint(text)
        score = 0
        for a, b in zip(running, candidate):
            if a != b:
                break
            score += 1
        if score > best_score:
            second_score = best_score
            best_name, best_score = name, score
        elif score > second_score:
            second_score = score
    # Требуем заметного отрыва, иначе стратегию считать неопределённой.
    if best_score >= 4 and best_score > second_score:
        return best_name
    return ""


def _read_bats(zapret_dir: Path | None) -> dict[str, str]:
    if zapret_dir is None or not zapret_dir.is_dir():
        return {}
    result: dict[str, str] = {}
    try:
        for path in sorted(zapret_dir.glob("general*.bat")):
            try:
                result[path.name] = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
    except OSError:
        return {}
    return result


def collect(cfg: dict) -> dict:
    procs = _processes()
    winws = [(name, cmd) for name, cmd in procs if name.lower().startswith("winws")]
    tgws = [
        (name, cmd)
        for name, cmd in procs
        if TGWS_RE.search(name or "") or TGWS_RE.search(cmd or "")
    ]
    try:
        port = int(cfg.get("tgws_port") or 1443)
    except (TypeError, ValueError):
        # Кривой порт в конфиге не должен ронять сбор статусов целиком.
        port = 1443

    raw_dir = cfg.get("zapret_dir") or ""
    # Пустой путь нельзя превращать в Path("") — это текущий каталог, и он
    # ложно выглядел бы как валидная папка zapret.
    zapret_dir = Path(raw_dir) if raw_dir else None
    valid_dir = zapret_dir is not None and zapret_dir.is_dir()
    bats = sorted(p.name for p in zapret_dir.glob("general*.bat")) if valid_dir else []

    # Командную строку winws нельзя прочитать без прав администратора, если
    # zapret установлен службой; тогда берём аргументы из реестра.
    process_cmd = winws[0][1] if winws else ""
    service_cmd = service_image_path() if _service_exists() else ""
    cmdline = process_cmd or service_cmd
    source = "process" if process_cmd else ("service" if service_cmd else "")
    strategy = identify_strategy(_read_bats(zapret_dir), cmdline) if valid_dir else ""

    # Режим маршрутизации и системный прокси: режим берём из уже прочитанного
    # конфига, остальное — дешёвым срезом Xray. ``xray.status()`` здесь не
    # годится: он при пустой записи о режиме зовёт ``config.load_config()``, а
    # тот на пустом ``zapret_dir`` обходит диск (~2 с), тогда как ``collect``
    # опрашивается каждые 5 секунд. Импорт внутри функции: ``xray`` сам зовёт
    # ``status.collect`` (через ``_desired_stream``), модульный импорт дал бы
    # цикл.
    routing_mode = str(cfg.get("routing_mode") or "")
    system_proxy = False
    bypass: list[str] = []
    try:
        from . import xray

        proxy = xray.coverage()
        routing_mode = routing_mode or str(proxy.get("routing_mode") or "")
        system_proxy = bool(proxy.get("system_proxy"))
        bypass = list(proxy.get("bypass") or [])
    except Exception:  # noqa: BLE001
        pass

    return {
        "zapret": {
            "running": bool(winws),
            "process": winws[0][0] if winws else "",
            "digest": strategy_digest(cmdline),
            "dir": str(zapret_dir) if valid_dir else "",
            "bats": bats,
            "strategy": strategy,
            "cmdline_source": source,
            "as_service": bool(service_cmd),
        },
        "tgwsproxy": {"running": bool(tgws), "port": port, "listening": port_open(port)},
        "admin": is_admin(),
        # Что реально покрыто прокси: режим, включён ли системный прокси и
        # какие домены из него исключены (ProxyOverride из реестра).
        "routing_mode": normalise_routing_mode(routing_mode),
        "system_proxy": system_proxy,
        "bypass": bypass,
    }
