"""Конфиг и состояние приложения."""

from __future__ import annotations

import json
import os
import shutil
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
#: О неудачном чтении сообщаем в журнал не чаще раза в 10 минут.
_READ_LOG_AT = 0.0
_READ_LOG_EVERY = 600.0


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


def _backup_copy(path: Path) -> None:
    """Копия читаемого файла перед перезаписью: ``*.bak-<время>``.

    В отличие от ``_backup_broken`` файл остаётся на месте: его содержимое
    нормализуется или мигрирует, и прежний вариант нужен для разбора.
    """
    if not path.exists():
        return
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    target = path.with_name(f"{path.name}.bak-{stamp}")
    number = 1
    while target.exists():
        target = path.with_name(f"{path.name}.bak-{stamp}-{number}")
        number += 1
    try:
        shutil.copy2(path, target)
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
    # Тема панели: auto — по системной теме Windows, либо принудительно
    # light / dark. Прежний ключ ``theme`` мигрирует сюда при чтении.
    "theme_mode": "auto",
    # --- собственный клиент Xray ---
    # Подписка: отдаёт список серверов vless://. Укажите свою — в репозитории
    # её нет намеренно, это персональный ключ доступа.
    "xray_subscription": "",
    # Свои ссылки vless/vmess/trojan/ss — без подписки. Список строк; одна
    # строка тоже принимается (разбирается при чтении).
    "xray_links": [],
    # Пусто = <проект>\bin\xray.exe; иначе путь к своему ядру.
    "xray_exe": "",
    # Локальный порт прокси (SOCKS и HTTP на одном порту).
    "xray_port": 10818,
    # Прописывать прокси в систему, чтобы им пользовались браузеры.
    "xray_system_proxy": True,
    # Куда вести трафик: "blocked" — через VPN только заблокированное,
    # "all" — всё, кроме РФ и локальной сети. РФ и private идут напрямую
    # в обоих режимах.
    "routing_mode": "blocked",
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

#: Режимы маршрутизации Xray. Всё остальное при чтении конфига сводится к
#: ``blocked``: неизвестное значение не должно ломать запуск.
ROUTING_MODES = ("blocked", "all")

#: Режимы темы панели. ``auto`` — по системной настройке Windows; всё, что
#: не распознано, тоже становится ``auto``.
THEME_MODES = ("auto", "light", "dark")


def normalise_theme_mode(value) -> str:
    """Значение ``theme_mode`` из конфига; неизвестное — ``auto``."""
    mode = str(value).strip().lower() if isinstance(value, str) else ""
    return mode if mode in THEME_MODES else "auto"


def theme_mode_of(cfg: dict) -> str:
    """Режим темы из конфига: ``theme_mode``, затем прежний ключ ``theme``.

    Старое имя нужно контроллерам и тестам, которые ещё не прошли
    ``load_config``: без него панель молча уехала бы в ``auto``.
    """
    cfg = cfg or {}
    value = cfg.get("theme_mode")
    if value is None:
        value = cfg.get("theme")
    return normalise_theme_mode(value)


def normalise_routing_mode(value) -> str:
    """Значение ``routing_mode`` из конфига; неизвестное — ``blocked``."""
    mode = str(value).strip().lower() if isinstance(value, str) else ""
    return mode if mode in ROUTING_MODES else "blocked"


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


def _normalise_links(value) -> list[str] | str:
    """Свои ссылки из конфига: список строк, одна строка или пустой список.

    Одна строка (склеенные ссылки/base64) остаётся строкой — её разбирает
    ``xray.links_from_text``. В списке нестроковые элементы отбрасываются,
    поэтому испорченный вручную config.json не роняет ни чтение, ни запуск.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return [item for item in value if isinstance(item, str)]
    return []


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
    else:
        data = None
    # Пишем файл только когда это действительно нужно: первый запуск,
    # миграция старых списков или найденные пути компонентов. Иначе чтение
    # конфига не должно трогать файл и его mtime.
    cfg["xray_links"] = _normalise_links(cfg.get("xray_links"))
    # Старые config.json читаются как раньше: без ключа режим — "blocked",
    # неизвестное значение тоже сводится к нему (без записи файла).
    cfg["routing_mode"] = normalise_routing_mode(cfg.get("routing_mode"))
    # Прежний ключ ``theme`` (до появления ``theme_mode``) продолжает
    # работать: если нового ключа в файле нет, значение берётся из старого и
    # файл переписывается с новым именем.
    legacy_theme = False
    if isinstance(data, dict) and "theme_mode" not in data:
        stored_theme = data.get("theme")
        if stored_theme is not None:
            cfg["theme_mode"] = stored_theme
            legacy_theme = True
    cfg["theme_mode"] = normalise_theme_mode(cfg.get("theme_mode"))
    cfg.pop("theme", None)
    normalised = False
    if isinstance(data, dict):
        # Мусорное значение (``"routing_mode": 123``) или отброшенные
        # не-строки в списке: файл будет переписан нормализованным, поэтому
        # перед этим нужна копия — как у нечитаемого JSON.
        normalised = (
            ("routing_mode" in data
             and data.get("routing_mode") != cfg["routing_mode"])
            or ("xray_links" in data
                and data.get("xray_links") != cfg["xray_links"])
            or ("theme_mode" in data
                and data.get("theme_mode") != cfg["theme_mode"])
            or legacy_theme)
    changed = raw is None
    migrated = False
    if "xray_routes" not in cfg:
        _migrate_routes(cfg)
        changed = True
        migrated = True
    if legacy_theme:
        # Переименование ключа — тоже миграция: файл пишется с ``theme_mode``.
        changed = True
        migrated = True
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
        if raw is not None and (migrated or normalised):
            # Перезапись не по воле пользователя: прежние байты сохраняем
            # рядом, чтобы нормализацию можно было проверить и отменить.
            _backup_copy(CONFIG_FILE)
        save_config(cfg)
    return cfg


def _refresh_read_state() -> dict | None:
    """Повторно читает config.json, чтобы снять latch после неудачного старта.

    Приложение читает конфиг один раз (``App.__init__``), поэтому запрет
    записи, залипший на первом чтении (файл держал антивирус или OneDrive),
    иначе жил бы до перезапуска и молча терял все настройки. ``None`` — файл
    по-прежнему не читается; иначе его содержимое с диска (пустой словарь,
    если файла уже нет — терять тогда нечего).
    """
    global _READ_ERROR, _READ_ERROR_MESSAGE
    if not _READ_ERROR:
        return {}
    last_error = ""
    for attempt in range(2):
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            _READ_ERROR = False
            _READ_ERROR_MESSAGE = ""
            return {}
        except (OSError, ValueError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt == 0:
                time.sleep(0.1)
            continue
        if not isinstance(data, dict):
            last_error = "config.json: ожидался объект JSON"
            break
        _READ_ERROR = False
        _READ_ERROR_MESSAGE = ""
        return data
    _READ_ERROR_MESSAGE = last_error or _READ_ERROR_MESSAGE
    return None


def _keep_untouched(cfg: dict, disk: dict) -> dict:
    """Копия ``cfg``, где ключи, оставшиеся умолчаниями, взяты с диска.

    Сохранение после неудачного первого чтения пишет словарь, собранный из
    ``DEFAULTS``: без этой подстановки подписка, ``zapret_bat`` и
    ``autoconnect``, которых пользователь в этой сессии не касался, были бы
    стёрты умолчаниями.
    """
    merged = dict(cfg)
    for key, value in disk.items():
        if merged.get(key) == DEFAULTS.get(key):
            merged[key] = value
    return merged


def _log_read_error() -> None:
    """Пишет причину отказа в журнал действий (не чаще раза в 10 минут)."""
    global _READ_LOG_AT
    now = time.time()
    if now - _READ_LOG_AT < _READ_LOG_EVERY:
        return
    _READ_LOG_AT = now
    detail = _READ_ERROR_MESSAGE or "файл недоступен"
    try:
        from . import activity  # локальный импорт: activity импортирует config

        activity.log(f"настройки не сохраняются: config.json не читается ({detail})",
                     kind="warn")
    except (ImportError, TypeError, ValueError):
        pass


def save_config(cfg: dict) -> bool:
    global _LAST_WRITE_ERROR
    recovered = False
    if _READ_ERROR:
        disk = _refresh_read_state()
        if disk is None:
            _LAST_WRITE_ERROR = ("config.json не читается — запись отменена: "
                                 + (_READ_ERROR_MESSAGE or "файл недоступен"))
            _log_read_error()
            return False
        # Файл снова читается: сохраняем накопленное, не затирая того, что в
        # этой сессии осталось умолчаниями.
        cfg = _keep_untouched(cfg, disk)
        recovered = True
    written = atomic_write(
        CONFIG_FILE, json.dumps(cfg, ensure_ascii=False, indent=2))
    if written and recovered:
        try:
            from . import activity

            activity.log("config.json снова читается — сохранение возобновлено",
                         kind="info")
        except (ImportError, TypeError, ValueError):
            pass
    return written


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
