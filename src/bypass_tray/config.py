"""Конфиг и состояние приложения."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

APP_NAME = "bypass-tray"

#: Каталог данных, подменяемый в тестах через ``set_config_dir``.
_OVERRIDE_DIR: Path | None = None


def is_windows() -> bool:
    return sys.platform.startswith("win")


def config_dir() -> Path:
    if _OVERRIDE_DIR is not None:
        _OVERRIDE_DIR.mkdir(parents=True, exist_ok=True)
        return _OVERRIDE_DIR
    base = os.environ.get("APPDATA") if is_windows() else None
    root = Path(base) if base else Path.home() / ".config"
    directory = root / APP_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def set_config_dir(path: Path | None) -> None:
    """Подменяет каталог данных (для тестов). ``None`` — вернуть обычный.

    Модульные ``CONFIG_FILE``/``STATE_FILE`` пересчитываются, а остальные
    модули берут путь каждый раз через ``config_dir()``, поэтому переезд
    действует на всё приложение сразу.
    """
    global _OVERRIDE_DIR, CONFIG_FILE, STATE_FILE
    _OVERRIDE_DIR = Path(path).resolve() if path is not None else None
    root = config_dir()
    CONFIG_FILE = root / "config.json"
    STATE_FILE = root / "state.json"


#: Запись одного файла из нескольких потоков не должна пересекаться.
_WRITE_LOCK = threading.Lock()
_LAST_WRITE_ERROR = ""
#: Конфиг существует, но чтение не удалось: save_config в этом состоянии
#: молча не перезаписывает пользовательские настройки умолчаниями.
_READ_ERROR = False
_READ_ERROR_MESSAGE = ""


def atomic_write(path: Path, text: str) -> bool:
    """Пишет текст атомарно; False — если запись не удалась после 3 попыток."""
    global _LAST_WRITE_ERROR
    tmp = path.with_name(
        f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    last_error = ""
    with _WRITE_LOCK:
        for attempt in range(3):
            try:
                tmp.write_text(text, encoding="utf-8")
                os.replace(tmp, path)
                return True
            except OSError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < 2:
                    time.sleep(0.15 * (attempt + 1))
        _remove_quietly(tmp)
        _LAST_WRITE_ERROR = f"{path.name}: {last_error or 'write failed'}"
        return False


def last_write_error() -> str:
    return _LAST_WRITE_ERROR


def read_error() -> str:
    return _READ_ERROR_MESSAGE


def _remove_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _backup_broken(path: Path) -> None:
    """Переименовывает нечитаемый файл, чтобы не потерять его содержимое."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    try:
        os.replace(path, path.with_name(f"{path.name}.bak-{stamp}"))
    except OSError:
        pass


CONFIG_FILE = config_dir() / "config.json"
STATE_FILE = config_dir() / "state.json"

DEFAULTS: dict = {
    "zapret_dir": "",
    "zapret_bat": "general (ALT).bat",
    "tgws_exe": "",
    "tgws_port": 1443,
    "refresh_seconds": 5,
    "update_check_hours": 24,
    # auto — по системной теме Windows, либо принудительно light / dark.
    "theme": "auto",
    # --- собственный клиент Xray ---
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
    # Устарело: мигрирует в xray_routes при первом чтении конфига.
    "xray_extra_domains": [],
    # Домены, которые идут МИМО прокси (исключения системного прокси).
    # Устарело и мигрирует в xray_routes; значения по умолчанию оставлены,
    # чтобы новые установки сохранили прежнее поведение для Steam.
    "xray_direct_domains": [
        "*.steampowered.com",
        "*.steamstatic.com",
        "*.steamcommunity.com",
        "*.steamcontent.com",
        "*.valvesoftware.com",
    ],
    # Уводить YouTube/Discord в прокси, когда zapret не работает.
    "xray_stream_fallback": True,
    # --- поведение виджета в фоне ---
    # Тихая иконка: без уведомлений о каждом автоматическом действии.
    "quiet_icon": True,
    # Надзиратель сам поднимает то, что можно поднять без UAC.
    "guard_enabled": True,
    # Как часто надзиратель проверяет состояние (минуты).
    "guard_minutes": 10,
    # Как часто проверять доступность сервисов (минуты).
    "health_minutes": 10,
    # Как часто перепроверять маршруты доменов (минуты).
    "route_audit_minutes": 60,
    # Подключать прокси при старте виджета.
    "autoconnect": False,
    # Принудительно вести YouTube/Discord через VPN (если видео застревает).
    "youtube_via_vpn": False,
    # Сколько часов журнала показывать в панели.
    "activity_hours": 3,
    # Встроенный список заблокированных доменов Re:filter (data/geosite-refilter.dat).
    "blocked_list_enabled": True,
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


def _migrate_routes(cfg: dict) -> None:
    """Однократно переносит старые списки доменов в ``xray_routes``.

    direct берётся из ``xray_direct_domains``, proxy — из
    ``xray_extra_domains``; при конфликте побеждает direct. Старые ключи
    очищаются, чтобы правила больше не дублировались.
    """
    from . import routes  # локальный импорт: routes сам импортирует config

    mapping: dict[str, str] = {}
    for raw in cfg.get("xray_direct_domains") or []:
        ok, _reason = routes.is_valid_domain(raw)
        name = routes.normalize_user_domain(raw)
        if ok and name:
            mapping.setdefault(name, "direct")
    for raw in cfg.get("xray_extra_domains") or []:
        ok, _reason = routes.is_valid_domain(raw)
        name = routes.normalize_user_domain(raw)
        if ok and name and name not in mapping:
            mapping[name] = "proxy"
    cfg["xray_routes"] = mapping
    cfg["xray_direct_domains"] = []
    cfg["xray_extra_domains"] = []


def load_config() -> dict:
    """Читает конфиг, дозаполняя пути компонентов.

    Битый JSON переименовывается в ``config.json.bak-<время>`` и **не**
    перезаписывается: иначе второй экземпляр, прочитавший половину файла,
    стёр бы подписку и настройки. Свежие умолчания без записи оставляем и в
    случае, когда файл есть, но недоступен для чтения.
    """
    global _READ_ERROR, _READ_ERROR_MESSAGE
    cfg = dict(DEFAULTS)
    existed = CONFIG_FILE.exists()
    raw = None
    last_error = ""
    for attempt in range(3):
        try:
            raw = CONFIG_FILE.read_text(encoding="utf-8")
            break
        except OSError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < 2:
                time.sleep(0.05 * (attempt + 1))
    if raw is None:
        if existed:
            _READ_ERROR = True
            _READ_ERROR_MESSAGE = last_error or "config.json не читается"
            return cfg
        _READ_ERROR = False
        _READ_ERROR_MESSAGE = ""
    else:
        _READ_ERROR = False
        _READ_ERROR_MESSAGE = ""
    if raw is not None:
        try:
            data = json.loads(raw)
        except ValueError:
            _backup_broken(CONFIG_FILE)
            return cfg
        if not isinstance(data, dict):
            _backup_broken(CONFIG_FILE)
            return cfg
        cfg.update(data)
    elif existed:
        return cfg
    # Пишем файл только когда это действительно нужно: первый запуск,
    # миграция старых списков или найденные пути компонентов. Иначе чтение
    # конфига не должно трогать файл и его mtime.
    changed = raw is None
    if "xray_routes" not in cfg:
        _migrate_routes(cfg)
        changed = True
    if not cfg.get("zapret_dir"):
        found = autodetect_zapret()
        if found != cfg.get("zapret_dir"):
            cfg["zapret_dir"] = found
            changed = True
    if not cfg.get("tgws_exe"):
        found = autodetect_tgws()
        if found != cfg.get("tgws_exe"):
            cfg["tgws_exe"] = found
            changed = True
    if changed:
        save_config(cfg)
    return cfg


def save_config(cfg: dict) -> bool:
    global _LAST_WRITE_ERROR
    if _READ_ERROR:
        _LAST_WRITE_ERROR = "config.json не читается — запись отменена"
        return False
    return atomic_write(
        CONFIG_FILE, json.dumps(cfg, ensure_ascii=False, indent=2))


def load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except OSError:
        return {}
    except ValueError:
        _backup_broken(STATE_FILE)
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict) -> bool:
    return atomic_write(
        STATE_FILE, json.dumps(state, ensure_ascii=False, indent=2))
