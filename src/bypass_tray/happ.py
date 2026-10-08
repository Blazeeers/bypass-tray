"""Управление маршрутизацией Happ: что идёт в туннель, а что — напрямую.

Принцип: ``globalProxy=false``, поэтому в VPN уходит **только** то, что
перечислено в ``proxySites``; всё остальное, включая российские сервисы, идёт
напрямую. Дополнительно российские сервисы прописываются в ``directSites`` —
чтобы они гарантированно не попали в туннель ни при каких настройках.

Готовятся два профиля:

* **Обходы** — Telegram, боты, заблокированные соцсети и страницы; YouTube и
  Discord идут напрямую, их закрывает zapret;
* **Обходы (резерв)** — то же, но YouTube и Discord тоже в туннеле; включается,
  когда zapret или tg-ws-proxy не работают.

Правила применяются записью в ``routing.json`` и перезапуском Happ: файл
принадлежит Happ, и менять его на лету он не умеет.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import actions

#: Отсоединённый процесс-сторож (переживает закрытие родителя).
DETACHED_PROCESS = 0x00000008

NORMAL_PROFILE = "Обходы"
FALLBACK_PROFILE = "Обходы (резерв)"
BACKUP_SUFFIX = ".bypass-tray-backup"

#: Telegram: обычный клиент, веб-версии и Bot API (через него работают боты).
TELEGRAM_SITES = [
    "geosite:telegram",
    "domain:telegram.org",
    "domain:telegram.me",
    "domain:telegram.dog",
    "domain:telegram.space",
    "domain:t.me",
    "domain:tdesktop.com",
    "domain:telesco.pe",
    "domain:api.telegram.org",
    "domain:web.telegram.org",
    "domain:webk.telegram.org",
    "domain:webz.telegram.org",
    "domain:bot.telegram.org",
]

#: Заблокированные в РФ западные соцсети и сервисы.
SOCIAL_SITES = [
    "geosite:instagram",
    "geosite:facebook",
    "geosite:twitter",
    "geosite:tiktok",
    "geosite:reddit",
    "geosite:linkedin",
    "domain:instagram.com",
    "domain:cdninstagram.com",
    "domain:facebook.com",
    "domain:fb.com",
    "domain:fbcdn.net",
    "domain:x.com",
    "domain:twitter.com",
    "domain:t.co",
    "domain:twimg.com",
    "domain:tiktok.com",
    "domain:tiktokcdn.com",
    "domain:reddit.com",
    "domain:redd.it",
    "domain:linkedin.com",
    "domain:licdn.com",
    "domain:threads.net",
    "domain:medium.com",
    "domain:signal.org",
]

#: Зарубежные новости и ИИ-сервисы, которые тоже удобнее держать в туннеле.
EXTRA_SITES = [
    "domain:bbc.com",
    "domain:cnn.com",
    "domain:dw.com",
    "domain:nytimes.com",
    "domain:wsj.com",
    "domain:theguardian.com",
    "domain:reuters.com",
    "domain:openai.com",
    "domain:chatgpt.com",
    "domain:anthropic.com",
    "domain:claude.ai",
]

#: YouTube и Discord — только для резервного профиля (обычно их закрывает zapret).
STREAM_SITES = [
    "geosite:youtube",
    "geosite:discord",
    "domain:youtube.com",
    "domain:youtu.be",
    "domain:ytimg.com",
    "domain:googlevideo.com",
    "domain:youtube-nocookie.com",
    "domain:youtube.googleapis.com",
    "domain:discord.com",
    "domain:discord.gg",
    "domain:discordapp.com",
    "domain:discordapp.net",
    "domain:discord.media",
    "domain:discordcdn.com",
    "domain:gateway.discord.gg",
]

#: Российские сервисы: всегда напрямую, в туннель не попадают.
RU_DIRECT_SITES = [
    "domain:yandex.ru",
    "domain:ya.ru",
    "domain:yandex.net",
    "domain:yastatic.net",
    "domain:mail.ru",
    "domain:vk.com",
    "domain:vk.ru",
    "domain:ok.ru",
    "domain:dzen.ru",
    "domain:rutube.ru",
    "domain:kinopoisk.ru",
    "domain:gosuslugi.ru",
    "domain:nalog.ru",
    "domain:sberbank.ru",
    "domain:sber.ru",
    "domain:tbank.ru",
    "domain:vtb.ru",
    "domain:alfabank.ru",
    "domain:ozon.ru",
    "domain:wildberries.ru",
    "domain:avito.ru",
    "domain:hh.ru",
    "domain:2gis.ru",
    "domain:ria.ru",
    "domain:lenta.ru",
    "domain:habr.com",
    "domain:mts.ru",
    "domain:beeline.ru",
    "domain:megafon.ru",
    "domain:tele2.ru",
    "domain:rt.ru",
    "domain:gismeteo.ru",
]


def routing_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
    return base / "Happ" / "routing.json"


def read_routing() -> dict:
    try:
        return json.loads(routing_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def backup_routing() -> Path | None:
    """Копия текущего файла маршрутов — на случай отката."""
    source = routing_path()
    if not source.is_file():
        return None
    target = source.with_name(source.name + BACKUP_SUFFIX)
    try:
        shutil.copy2(source, target)
    except OSError:
        return None
    return target


#: DNS для профиля. Исходный профиль пришёл из неудачного импорта и содержал
#: `domesticDnsType: "DoU"` — обычный UDP-запрос на 8.8.8.8, который в РФ
#: подменяется провайдером. Из-за этого ровно заблокированные домены получали
#: подставленные адреса и падали с ошибкой TLS. DoH к тем же серверам доступен
#: и отдаёт настоящие адреса (проверено с этой машины).
DNS_FIX = {
    "domesticDnsType": "DoH",
    "domesticDnsDomain": "https://dns.google/dns-query",
    "domesticDnsIp": "8.8.8.8",
    "remoteDnsType": "DoH",
    "remoteDnsDomain": "https://cloudflare-dns.com/dns-query",
    "remoteDnsIp": "1.1.1.1",
}


def _profile_sites(extra: list[str] | None, fallback: bool) -> list[str]:
    sites = list(TELEGRAM_SITES) + list(SOCIAL_SITES) + list(EXTRA_SITES)
    sites += [s for s in (extra or []) if s and s not in sites]
    if fallback:
        sites += [s for s in STREAM_SITES if s not in sites]
    return sites


def _geo_asset_dir(data: dict) -> str:
    """Каталог, где реально лежат geoip.dat/geosite.dat.

    В профиле может быть прописан путь к пустой папке (например, `routing\\0`),
    тогда Happ при подключении падает с «geo file error». Берём первый
    существующий каталог с geo-файлами — в том числе из ``defaultSnapshot``.
    """
    candidates: list[str] = []
    for routing in data.get("routings") or []:
        if routing.get("locationAssetPath"):
            candidates.append(str(routing["locationAssetPath"]))
        snapshot = routing.get("defaultSnapshot")
        if isinstance(snapshot, str) and snapshot.strip():
            try:
                parsed = json.loads(snapshot)
                if parsed.get("locationAssetPath"):
                    candidates.append(str(parsed["locationAssetPath"]))
            except ValueError:
                pass
    for candidate in candidates:
        directory = Path(candidate)
        if (directory / "geosite.dat").is_file() and (directory / "geoip.dat").is_file():
            return str(directory)
    return candidates[0] if candidates else ""


def build_profiles(data: dict, extra: list[str] | None = None) -> list[dict]:
    """Собирает оба профиля на основе первого существующего."""
    routings = data.get("routings") or []
    if not routings:
        return []
    base = copy.deepcopy(routings[0])
    asset_dir = _geo_asset_dir(data)

    profiles = []
    for name, fallback in ((NORMAL_PROFILE, False), (FALLBACK_PROFILE, True)):
        profile = copy.deepcopy(base)
        profile["name"] = name
        profile["globalProxy"] = False
        profile["proxySites"] = _profile_sites(extra, fallback)
        profile["directSites"] = list(RU_DIRECT_SITES)
        profile["blockSites"] = list(base.get("blockSites") or [])
        profile.update(DNS_FIX)
        if asset_dir:
            profile["locationAssetPath"] = asset_dir
        profile["defaultSnapshot"] = ""
        snapshot = copy.deepcopy(profile)
        profile["defaultSnapshot"] = json.dumps(snapshot, ensure_ascii=False)
        profile["lastUpdated"] = int(time.time())
        profiles.append(profile)
    return profiles


def current_subscription_id() -> int:
    """Идентификатор активной подписки Happ (из реестра)."""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, REG_PREFERENCES
        ) as key:
            return int(winreg.QueryValueEx(key, "lastSubscription")[0])
    except OSError:
        return 0


def apply_profiles(data: dict, active: str, extra: list[str] | None = None,
                   subscription_id: int | None = None) -> dict:
    """Возвращает новые данные routing.json с обоими профилями и активным.

    Профиль привязывается к **реальной** подписке: Happ выбирает правило при
    подключении по идентификатору подписки, а не по одному лишь имени, поэтому
    без такой записи он сообщает «no profile selected for sub» и профиль не
    применяется.
    """
    profiles = build_profiles(data, extra)
    if not profiles:
        return data
    data = copy.deepcopy(data)
    names = {p["name"] for p in profiles}
    remaining = [r for r in (data.get("routings") or []) if r.get("name") not in names]
    data["routings"] = remaining + profiles
    data["activeRoutingName"] = active

    sub_id = subscription_id if subscription_id else current_subscription_id()
    configs = [c for c in (data.get("subConfigs") or []) if c.get("subscriptionId") != sub_id]
    configs.append({
        "subscriptionId": sub_id,
        "enabled": True,
        "ignoreProfile": False,
        "selectedProfile": active,
        "providerRoutingDisabled": False,
    })
    data["subConfigs"] = configs
    data["version"] = max(int(data.get("version") or 1), 2)
    return data


def save_routing(data: dict) -> bool:
    path = routing_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")
        return True
    except OSError:
        return False


def hide_window(delay: float = 0.0) -> int:
    """Прячет окно Happ: управление живёт в виджете, отдельное окно не нужно."""
    if delay:
        time.sleep(delay)
    return actions.hide_console_windows("Happ")


def _hide_soon() -> None:
    """Окно Happ появляется не мгновенно — прячем его несколько раз."""

    def worker() -> None:
        for _ in range(8):
            time.sleep(1.0)
            if hide_window():
                return

    import threading

    threading.Thread(target=worker, daemon=True).start()


def restart_happ(cfg: dict, wait: float = 6.0) -> bool:
    """Перезапускает Happ так, чтобы не всплывало отдельное окно.

    Окно пользователю не нужно: подключением управляет виджет. Но запускать
    надо **обычным** способом: с флагом ``--autostart`` Happ остаётся в фоновом
    режиме и не выполняет автоподключение (проверено — туннель не поднимался
    вовсе). Поэтому просто просим стартовать свёрнутым и прячем окно.
    """
    exe = cfg.get("happ_exe") or ""
    actions.kill_process("Happ")
    actions.kill_process("happd")
    time.sleep(2.0)
    if not exe or not Path(exe).is_file():
        return False
    try:
        actions._popen([exe])
    except OSError:
        return False
    _hide_soon()
    time.sleep(wait)
    return True


def install(cfg: dict, *, fallback: bool = False, extra: list[str] | None = None,
            restart: bool = True, subscription_id: int | None = None) -> dict:
    """Применяет профили и делает нужный активным."""
    data = read_routing()
    if not data.get("routings"):
        return {"ok": False, "error": "routing.json не найден или пуст"}
    backup = backup_routing()
    active = FALLBACK_PROFILE if fallback else NORMAL_PROFILE
    updated = apply_profiles(data, active, extra, subscription_id)
    if not save_routing(updated):
        return {"ok": False, "error": "не удалось записать routing.json"}
    result = {"ok": True, "active": active, "backup": str(backup) if backup else "",
              "subscription": subscription_id or current_subscription_id()}
    if restart:
        result["restarted"] = restart_happ(cfg)
    return result


def set_active(cfg: dict, fallback: bool, extra: list[str] | None = None,
               restart: bool = True) -> dict:
    """Переключает активный профиль, не пересобирая списки заново."""
    data = read_routing()
    active = FALLBACK_PROFILE if fallback else NORMAL_PROFILE
    if not any(r.get("name") == active for r in data.get("routings") or []):
        return install(cfg, fallback=fallback, extra=extra, restart=restart)
    data["activeRoutingName"] = active
    for sub in data.get("subConfigs") or []:
        sub["selectedProfile"] = active
    if not save_routing(data):
        return {"ok": False, "error": "не удалось записать routing.json"}
    result = {"ok": True, "active": active}
    if restart:
        result["restarted"] = restart_happ(cfg)
    return result


def active_profile() -> str:
    data = read_routing()
    name = str(data.get("activeRoutingName") or "")
    if name:
        return name
    for sub in data.get("subConfigs") or []:
        if sub.get("selectedProfile"):
            return str(sub["selectedProfile"])
    return ""


def installed() -> bool:
    names = {r.get("name") for r in read_routing().get("routings") or []}
    return NORMAL_PROFILE in names and FALLBACK_PROFILE in names


# ---- настройки Happ в реестре ---------------------------------------------
#
# Happ держит переключатели в HKCU\Software\Happ\OrganizationDefaults\Preferences.
# Без `useRouting=true` профиль маршрутизации не применяется вообще («Connection
# latch: routing disabled»), поэтому одних правок routing.json недостаточно.

REG_PREFERENCES = r"Software\Happ\OrganizationDefaults\Preferences"
REG_ROUTING = REG_PREFERENCES + r"\TunnelSettings\Routing"
REG_SUBSCRIPTIONS = REG_PREFERENCES + r"\Subscriptions"
REG_ADVANCED = REG_PREFERENCES + r"\AdvancedSettings"


def _reg_read(subkey: str, name: str) -> str:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey) as key:
            return str(winreg.QueryValueEx(key, name)[0])
    except OSError:
        return ""


def _reg_write(subkey: str, name: str, value: str) -> bool:
    try:
        import winreg

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, subkey, 0,
                                winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        return True
    except OSError:
        return False


def _reg_delete(subkey: str, name: str) -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
        return True
    except OSError:
        return False


def routing_settings() -> dict:
    return {
        "useRouting": _reg_read(REG_ROUTING, "useRouting"),
        "selectedRoutingRule": _reg_read(REG_ROUTING, "selectedRoutingRule"),
        "subsConnectOnOpen": _reg_read(REG_SUBSCRIPTIONS, "subsConnectOnOpen"),
    }


def enable_routing(active: str, connect_on_open: bool = True) -> dict:
    """Включает маршрутизацию Happ и выбирает профиль.

    ``connect_on_open`` заставляет Happ подключаться при запуске — это то, что
    делает перезапуск безопасным (после него VPN поднимается сам).

    Внимание: включение маршрутизации вместе с TUN-режимом Happ меняет всю
    сетевую картину на машине. Включайте осознанно и проверяйте связь.
    """
    written = {
        "useRouting": _reg_write(REG_ROUTING, "useRouting", "true"),
        "selectedRoutingRule": _reg_write(REG_ROUTING, "selectedRoutingRule", active),
    }
    if connect_on_open:
        written["subsConnectOnOpen"] = _reg_write(REG_SUBSCRIPTIONS, "subsConnectOnOpen", "true")
    return written


def disable_routing() -> dict:
    """Возвращает Happ к состоянию «маршруты не применяются».

    Значение ``@Invalid()`` — это ровно то, что Happ хранит, когда настройка
    никогда не задавалась: так состояние возвращается к исходному.
    """
    return {
        "useRouting": _reg_write(REG_ROUTING, "useRouting", "@Invalid()"),
        "selectedRoutingRule": _reg_write(REG_ROUTING, "selectedRoutingRule", "@Invalid()"),
        "subsConnectOnOpen": _reg_delete(REG_SUBSCRIPTIONS, "subsConnectOnOpen"),
    }


def open_deep_link(kind: str) -> bool:
    """Открывает ``happ://connect`` / ``happ://disconnect``."""
    if kind not in ("connect", "disconnect"):
        return False
    try:
        os.startfile(f"happ://{kind}")  # type: ignore[attr-defined]
        return True
    except OSError:
        return False


def is_connected() -> bool:
    """Поднят ли туннель Happ (запущен ли процесс xray)."""
    try:
        import psutil

        for proc in psutil.process_iter(["name"]):
            if (proc.info.get("name") or "").lower() == "xray.exe":
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def routing_enabled() -> bool:
    return routing_settings().get("useRouting", "").lower() == "true"


# ---- безопасное включение туннелирования ----------------------------------

#: Российские адреса — контроль целостности: если после включения они
#: перестают открываться, значит маршрутизация сломала обычный интернет.
RU_CONTROL = (("ya.ru", "https://ya.ru/"), ("vk.com", "https://vk.com/"))
#: Обычный зарубежный интернет (не заблокирован, в туннеле не нуждается) —
#: если он пропал, значит туннель забрал себе весь трафик и его нельзя оставлять.
#: Требуем не «все», а большинство: отдельные адреса бывают капризны.
OUTSIDE_CONTROL = (("github", "https://api.github.com/"),
                   ("python", "https://www.python.org/"),
                   ("wikipedia", "https://www.wikipedia.org/"))
#: Сколько адресов из OUTSIDE_CONTROL обязаны открываться.
OUTSIDE_MINIMUM = 2
#: Что должно «расблокироваться», когда туннель заработает.
TUNNEL_CHECK = (
    ("instagram", "https://www.instagram.com/"),
    ("facebook", "https://www.facebook.com/"),
    ("x.com", "https://x.com/"),
)


def _pending_path() -> Path:
    from . import config

    return config.config_dir() / "happ-activation.pending.json"


def _confirm_path() -> Path:
    from . import config

    return config.config_dir() / "happ-activation.confirmed"


def set_start_minimized(value: bool = True) -> bool:
    """Просит Happ запускаться свёрнутым — чтобы не всплывало отдельное окно."""
    return _reg_write(REG_ADVANCED, "startMinimized", "true" if value else "false")


def arm_watchdog(seconds: float = 150.0) -> bool:
    """Заводит «сторожа», который откатит настройки, если их не подтвердят.

    Сторож — отдельный процесс: он переживёт падение приложения и обрыв связи.
    Именно это делает включение туннеля безопасным, ведь при нерабочем туннеле
    ни приложение, ни агент не смогут вернуть настройки сами.
    """
    confirm = _confirm_path()
    try:
        confirm.unlink()
    except OSError:
        pass
    command = [
        sys.executable, "-m", "bypass_tray", "--happ-watchdog",
        "--seconds", str(int(seconds)), "--confirm", str(confirm),
    ]
    try:
        subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True,
            creationflags=actions.CREATE_NO_WINDOW | DETACHED_PROCESS,
        )
        return True
    except OSError:
        return False


def confirm_activation() -> None:
    """Останавливает сторожа: настройки проверены и оставлены как есть."""
    try:
        _confirm_path().write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def run_watchdog(seconds: float, confirm: Path) -> int:
    """Тело сторожа: ждёт подтверждения, иначе возвращает настройки."""
    from . import config

    confirm = Path(confirm)
    deadline = time.time() + seconds
    while time.time() < deadline:
        if confirm.exists():
            return 0
        time.sleep(2)
    snapshot = pending_activation()
    if snapshot:
        _restore(snapshot)
        _clear_pending()
        try:
            resume_zapret()
        except Exception:  # noqa: BLE001
            pass
        try:
            restart_happ(config.load_config(), wait=3.0)
        except Exception:  # noqa: BLE001
            pass
        return 1
    return 2


def _write_pending(snapshot: dict) -> None:
    try:
        _pending_path().write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _clear_pending() -> None:
    try:
        _pending_path().unlink()
    except OSError:
        pass


def pending_activation() -> dict:
    try:
        return json.loads(_pending_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def recover_if_pending(cfg: dict) -> bool:
    """Откатывает незавершённое включение.

    Метка пишется до подключения и снимается только после успешной проверки.
    Если приложение видит метку при запуске, прошлая попытка не подтвердилась —
    значит, связь могла пострадать, и настройки возвращаются назад.
    """
    snapshot = pending_activation()
    if not snapshot:
        return False
    _restore(snapshot)
    _clear_pending()
    return True


def _restore(snapshot: dict) -> None:
    routing = snapshot.get("routing") or {}
    for name in ("useRouting", "selectedRoutingRule"):
        value = routing.get(name, "@Invalid()")
        _reg_write(REG_ROUTING, name, value or "@Invalid()")
    if routing.get("subsConnectOnOpen"):
        _reg_write(REG_SUBSCRIPTIONS, "subsConnectOnOpen", routing["subsConnectOnOpen"])
    else:
        _reg_delete(REG_SUBSCRIPTIONS, "subsConnectOnOpen")


def _connect(cfg: dict, timeout: float = 90.0) -> bool:
    """Поднимает туннель и ждёт его появления."""
    if is_connected():
        return True
    restart_happ(cfg, wait=4.0)
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(3)
        if is_connected():
            return True
    return False


def _zapret_paused_marker() -> Path:
    from . import config

    return config.config_dir() / "zapret-paused.json"


def pause_zapret() -> dict:
    """Останавливает службу zapret на время работы туннеля.

    zapret (WinDivert) и Happ (Wintun) перехватывают один и тот же сетевой
    стек. Пока трафик забирает туннель, правила zapret только мешают, поэтому
    обход ставится на паузу, а его прежнее состояние запоминается.
    """
    was_running = actions.service_state() == "running"
    try:
        _zapret_paused_marker().write_text(
            json.dumps({"was_running": was_running, "at": time.time()}), encoding="utf-8"
        )
    except OSError:
        pass
    if was_running:
        actions.control_service("stop")
    return {"was_running": was_running, "now": actions.service_state()}


def resume_zapret() -> dict:
    """Возвращает zapret в работу, если он был остановлен при включении туннеля."""
    was_running = True
    try:
        data = json.loads(_zapret_paused_marker().read_text(encoding="utf-8"))
        was_running = bool(data.get("was_running", True))
    except (OSError, ValueError):
        pass
    state = actions.service_state()
    if was_running and state != "running":
        try:
            actions.control_service("start")
        except Exception:  # noqa: BLE001
            pass
    try:
        _zapret_paused_marker().unlink()
    except OSError:
        pass
    return {"restored": was_running, "state": actions.service_state()}


def activate(cfg: dict, *, fallback: bool = False, extra: list[str] | None = None,
             probe=None, watchdog_seconds: float = 150.0,
             subscription_id: int | None = None, pause_zapret_service: bool = True) -> dict:
    """Включает туннелирование Happ с проверкой и автооткатом.

    ``probe(url)`` — функция проверки адреса (по умолчанию `strategy.probe_url`),
    подменяется в тестах.

    Если ``pause_zapret_service``, служба zapret останавливается на время
    туннеля, и тогда YouTube/Discord тоже уходят в туннель (резервный профиль):
    без zapret напрямую они уже не откроются.
    """
    from . import strategy

    check = probe or (lambda url: strategy.probe_url(url, timeout=12))

    if pause_zapret_service:
        fallback = True
    before = {
        "routing": routing_settings(),
        "active": active_profile(),
        "connected": is_connected(),
        "blocked_before": [name for name, url in TUNNEL_CHECK if not check(url)["ok"]],
    }
    # Снимок «что работало до»: сравнивать надо с ним, а не с абсолютом.
    # Иначе давно сломанный сайт (например, отдающий неверный сертификат)
    # навсегда блокирует включение туннеля.
    control_sites = list(RU_CONTROL) + list(OUTSIDE_CONTROL)
    baseline = {name: bool(check(url)["ok"]) for name, url in control_sites}
    before["baseline"] = baseline
    _write_pending(before)
    armed = arm_watchdog(watchdog_seconds)
    before["watchdog"] = armed
    _write_pending(before)

    result = {"ok": False, "stage": "write", "error": "неизвестно"}
    try:
        if pause_zapret_service:
            result = {"ok": False, "stage": "zapret", "zapret": pause_zapret()}
        active = FALLBACK_PROFILE if fallback else NORMAL_PROFILE
        installed = install(cfg, fallback=fallback, extra=extra, restart=False,
                            subscription_id=subscription_id)
        if not installed.get("ok"):
            result = {"ok": False, "stage": "write", **installed}
            raise RuntimeError(installed.get("error", "не удалось записать профили"))
        enable_routing(active, connect_on_open=True)
        set_start_minimized(True)

        connected = _connect(cfg)
        if not connected:
            result = {"ok": False, "stage": "connect",
                      "reason": "Happ не подключился — настройки возвращены"}
            raise RuntimeError("connect")

        time.sleep(6)  # туннель стабилизируется не мгновенно

        ru_results = {name: check(url) for name, url in RU_CONTROL}
        outside = {name: check(url) for name, url in OUTSIDE_CONTROL}

        # Ломать нельзя то, что работало до включения.
        ru_degraded = [n for n, r in ru_results.items()
                       if baseline.get(n) and not r["ok"]]
        outside_degraded = [n for n, r in outside.items()
                            if baseline.get(n) and not r["ok"]]
        if ru_degraded or len(outside_degraded) > 1:
            reason = (f"перестали открываться: {', '.join(ru_degraded + outside_degraded)}"
                      if ru_degraded or outside_degraded
                      else "обычный интернет почти недоступен")
            result = {"ok": False, "stage": "ru-broken",
                      "reason": reason + " — настройки возвращены",
                      "ru": ru_results, "outside": outside,
                      "baseline": baseline}
            raise RuntimeError("ru-broken")

        tunnel_results = {name: check(url) for name, url in TUNNEL_CHECK}
        unblocked = [n for n, r in tunnel_results.items() if r["ok"]]
        result = {
            "ok": True,
            "active": active,
            "connected": True,
            "watchdog": armed,
            "ru": ru_results,
            "outside": outside,
            "tunnel": tunnel_results,
            "unblocked": unblocked,
            "blocked_before": before["blocked_before"],
        }
        _clear_pending()
        confirm_activation()
        return result
    except Exception:  # noqa: BLE001
        # Любая неудача — возвращаем и маршруты, и zapret.
        _restore(before)
        _clear_pending()
        confirm_activation()
        try:
            resume_zapret()
        except Exception:  # noqa: BLE001
            pass
        if not before["connected"]:
            try:
                restart_happ(cfg, wait=4.0)
            except Exception:  # noqa: BLE001
                pass
        return result


def deactivate(cfg: dict) -> dict:
    """Выключает маршрутизацию и возвращает Happ к обычной работе."""
    before = routing_settings()
    result = disable_routing()
    restart_happ(cfg, wait=4.0)
    zapret = resume_zapret()
    return {"ok": True, "changed": before.get("useRouting", "").lower() == "true",
            "settings": result, "zapret": zapret}
