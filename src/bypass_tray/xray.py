"""Собственный клиент Xray: подписка, выбор живого сервера, локальный прокси.

Используется открытое ядро Xray и своя подписка. Работает в режиме локального
прокси — **без прав администратора**, без TUN-адаптера и без конфликта с
zapret (тот продолжает обрабатывать трафик как обычно).

Схема: браузер (системный прокси) → 127.0.0.1:<port> → Xray → сервер.
Российские сервисы уходят напрямую, заблокированные — через сервер.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from . import actions, config

#: Ссылку на подписку каждый указывает свою в config.json — в репозитории её
#: нет намеренно: это персональный ключ доступа.
DEFAULT_SUBSCRIPTION = ""
DEFAULT_PORT = 10818

#: Файлы ядра, которые держим рядом с приложением.
CORE_FILES = ("xray.exe", "geoip.dat", "geosite.dat")
#: Официальный релиз Xray-core для Windows x64 — если своих файлов нет.
CORE_URL = ("https://github.com/XTLS/Xray-core/releases/latest/download/"
            "Xray-windows-64.zip")

#: Что ведём через прокси. YouTube и Discord здесь нет намеренно: их закрывает
#: zapret, и через прокси они только потеряют в скорости.
PROXY_DOMAINS = [
    "domain:instagram.com", "domain:cdninstagram.com",
    "domain:facebook.com", "domain:fb.com", "domain:fbcdn.net",
    "domain:x.com", "domain:twitter.com", "domain:t.co", "domain:twimg.com",
    "domain:tiktok.com", "domain:tiktokcdn.com",
    "domain:reddit.com", "domain:redd.it",
    "domain:linkedin.com", "domain:licdn.com",
    "domain:threads.net", "domain:medium.com", "domain:signal.org",
    # Telegram: клиент, веб-версии и Bot API (api/bot.telegram.org) — через прокси,
    # иначе Bot API блокируется DPI и боты/приём файлов не работают.
    "geosite:telegram",
    "domain:telegram.org", "domain:telegram.me", "domain:telegram.dog",
    "domain:telegram.space", "domain:t.me", "domain:tdesktop.com", "domain:telesco.pe",
    "domain:api.telegram.org", "domain:bot.telegram.org",
    "domain:web.telegram.org", "domain:webk.telegram.org", "domain:webz.telegram.org",
    "domain:bbc.com", "domain:cnn.com", "domain:dw.com",
    "domain:openai.com", "domain:chatgpt.com", "domain:anthropic.com", "domain:claude.ai",
]

#: YouTube и Discord: по умолчанию идут напрямую — их закрывает zapret.
#: В прокси попадают только когда zapret не работает (резервный режим).
STREAM_DOMAINS = [
    "geosite:youtube", "geosite:discord",
    "domain:youtube.com", "domain:youtu.be", "domain:ytimg.com",
    "domain:googlevideo.com", "domain:youtube-nocookie.com",
    "domain:youtube.googleapis.com",
    "domain:discord.com", "domain:discord.gg", "domain:discordapp.com",
    "domain:discordapp.net", "domain:discord.media", "domain:discordcdn.com",
    "domain:gateway.discord.gg",
]

YOUTUBE_GROUP = (
    "youtube.com", "youtu.be", "ytimg.com", "googlevideo.com",
    "youtube-nocookie.com", "youtube.googleapis.com",
)
DISCORD_GROUP = (
    "discord.com", "discord.gg", "discordapp.com", "discordapp.net",
    "discord.media", "discordcdn.com", "gateway.discord.gg",
)

RUNNING: dict = {}
_LOG_LIMIT = 4000
#: UI и надзиратель запускают Xray из разных потоков. Замок один на процесс:
#: второй ``start`` не должен поднимать второе ядро рядом с первым. RLock —
#: потому что ``switch_to`` внутри себя вызывает ``start``.
_LOCK = threading.RLock()


# ---- расположение ядра ----------------------------------------------------


def bin_dir() -> Path:
    if getattr(sys, "frozen", False):
        # ONEDIR: рядом с «Обходы.exe», а не внутри _internal.
        return Path(sys.executable).resolve().parent / "bin"
    return Path(__file__).resolve().parents[2] / "bin"


def xray_path() -> Path:
    configured = (config.load_config().get("xray_exe") or "")
    if configured and Path(configured).is_file():
        return Path(configured)
    return bin_dir() / "xray.exe"


def ensure_core(force: bool = False) -> Path | None:
    """Готовит локальную копию ядра Xray (и гео-файлов).

    Свои файлы в ``bin`` приоритетны. Если их нет — ядро скачивается из
    официального релиза Xray-core (Windows x64). ``force=True`` перекачивает
    ядро даже при наличии файлов — для пункта «Обновить всё».
    """
    target = bin_dir()
    target.mkdir(parents=True, exist_ok=True)
    if force and not _download_core(target):
        return None
    missing = [name for name in CORE_FILES if not (target / name).is_file()]
    if missing:
        if not _download_core(target):
            return None
        missing = [name for name in CORE_FILES if not (target / name).is_file()]
        if missing:
            return None
    candidate = target / "xray.exe"
    return candidate if candidate.is_file() else None


def _download_core(target: Path, timeout: int = 120) -> bool:
    """Скачивает Xray-windows-64.zip и раскладывает ядро в ``bin``.

    Сначала скачивается временный zip, проверяется его целостность, потом файлы
    пишутся в ``.part`` и только затем заменяют рабочие. При обрыве сети или
    битом архиве старые файлы остаются нетронутыми.
    """
    archive = target / f"core-{os.getpid()}.zip.part"
    staged: dict[str, Path] = {}
    try:
        request = urllib.request.Request(CORE_URL, headers={"User-Agent": "bypass-tray"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            with open(archive, "wb") as handle:
                while True:
                    chunk = response.read(262144)
                    if not chunk:
                        break
                    handle.write(chunk)
        wanted = {name.lower(): name for name in CORE_FILES}
        with zipfile.ZipFile(archive) as bundle:
            members: dict[str, str] = {}
            for member in bundle.namelist():
                leaf = member.rsplit("/", 1)[-1].lower()
                if leaf in wanted and leaf not in members:
                    members[leaf] = member
            if "xray.exe" not in members:
                return False
            for leaf, member in members.items():
                part = target / f"{wanted[leaf]}.part-{os.getpid()}"
                with bundle.open(member) as src, open(part, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                staged[leaf] = part
        for leaf, part in staged.items():
            if part.stat().st_size <= 1024 * 1024:
                return False
        for leaf, part in staged.items():
            os.replace(part, target / wanted[leaf])
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        for part in staged.values():
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            archive.unlink(missing_ok=True)
        except OSError:
            pass


# ---- подписка -------------------------------------------------------------


def fetch_subscription(url: str | None = None, timeout: int = 25) -> list[str]:
    """Возвращает список ссылок ``vless://`` из подписки.

    Ссылка берётся из ``config.json`` (поле ``xray_subscription``): в коде её
    нет, потому что это персональный ключ.
    """
    url = url or config.load_config().get("xray_subscription") or DEFAULT_SUBSCRIPTION
    if not url:
        raise ValueError(
            "не задана подписка: укажите xray_subscription в config.json"
        )
    request = urllib.request.Request(url, headers={"User-Agent": "v2rayNG/1.8"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        text = response.read().decode("utf-8", "replace")
    if "://" not in text:
        padded = text + "=" * (-len(text) % 4)
        text = base64.b64decode(padded).decode("utf-8", "replace")
    return [line.strip() for line in text.splitlines() if line.strip().startswith("vless://")]


def parse_vless(link: str) -> dict:
    """Разбирает ``vless://`` в описание сервера и outbound для Xray."""
    parts = urllib.parse.urlsplit(link)
    query = dict(urllib.parse.parse_qsl(parts.query))
    name = urllib.parse.unquote(parts.fragment or "") or (parts.hostname or "сервер")

    stream: dict = {
        "network": query.get("type", "tcp"),
        "security": query.get("security", "none"),
    }
    if query.get("security") == "reality":
        stream["realitySettings"] = {
            "serverName": query.get("sni", ""),
            "fingerprint": query.get("fp", "chrome"),
            "publicKey": query.get("pbk", ""),
            "shortId": query.get("sid", ""),
        }
    elif query.get("security") == "tls":
        stream["tlsSettings"] = {
            "serverName": query.get("sni", parts.hostname or ""),
            "fingerprint": query.get("fp", "chrome"),
        }
    if query.get("type") == "grpc":
        stream["grpcSettings"] = {
            "serviceName": query.get("serviceName", ""),
            "multiMode": query.get("mode", "") == "gun",
        }
    elif query.get("type") == "ws":
        stream["wsSettings"] = {"path": query.get("path", "/")}

    outbound = {
        "tag": "proxy",
        "protocol": "vless",
        "settings": {
            "vnext": [{
                "address": parts.hostname,
                "port": int(parts.port or 443),
                "users": [{
                    "id": urllib.parse.unquote(parts.username or ""),
                    "encryption": "none",
                    "flow": query.get("flow", ""),
                }],
            }]
        },
        "streamSettings": stream,
    }
    return {
        "name": name,
        "host": parts.hostname,
        "port": int(parts.port or 443),
        "outbound": outbound,
        "link": link,
    }


def probe_server(server: dict, timeout: float = 5.0) -> int:
    """TCP-доступность сервера. Возвращает задержку в мс или -1."""
    started = time.perf_counter()
    try:
        with socket.create_connection((server["host"], server["port"]), timeout=timeout):
            return round((time.perf_counter() - started) * 1000)
    except OSError:
        return -1


def rank_servers(links: list[str], limit: int | None = None) -> list[dict]:
    """Разбирает ссылки и сортирует серверы по доступности и задержке."""
    servers = []
    for link in links:
        try:
            server = parse_vless(link)
        except Exception:  # noqa: BLE001
            continue
        latency = probe_server(server)
        server["latency"] = latency
        servers.append(server)
    alive = [s for s in servers if s["latency"] >= 0]
    alive.sort(key=lambda s: s["latency"])
    if limit:
        alive = alive[:limit]
    return alive


def pick_server(links: list[str] | None = None) -> dict | None:
    """Возвращает самый быстрый из доступных серверов."""
    links = links if links is not None else fetch_subscription()
    alive = rank_servers(links)
    return alive[0] if alive else None


# ---- конфиг и запуск ------------------------------------------------------


def _config_route_lists(cfg: dict, extra_domains: list[str] | None) -> tuple[list[str], list[str]]:
    """Домены из конфига с учётом deprecated-ключей.

    Если ``xray_routes`` уже есть (даже пустой), старые
    ``xray_extra_domains``/``xray_direct_domains`` полностью игнорируются,
    чтобы удалённые в настройках правила не воскресали.
    """
    has_routes = isinstance((cfg or {}).get("xray_routes"), dict)
    if extra_domains is not None:
        extra = list(extra_domains)
    else:
        extra = [] if has_routes else list((cfg or {}).get("xray_extra_domains") or [])
    legacy_direct = [] if has_routes else list((cfg or {}).get("xray_direct_domains") or [])
    return [d for d in extra if d], [d for d in legacy_direct if d]


def build_config(server: dict, port: int, extra_domains: list[str] | None = None,
                 stream_via_proxy: bool = False,
                 direct_domains: list[str] | None = None,
                 user_routes: dict | None = None,
                 blocked_rule: str | None = None) -> dict:
    """Конфиг Xray: локальный прокси + маршрутизация.

    Маршрут по умолчанию — **напрямую** (``direct`` стоит первым в outbounds),
    поэтому через сервер идёт только явно перечисленное.

    Порядок правил:
    1) пользовательские direct, 2) пользовательские proxy, 3) audit direct,
    4) YouTube/Discord напрямую (если не резервный режим), 5) встроенный
    список заблокированных ``blocked_rule`` через прокси, 6) РФ/private
    напрямую, 7) встроенные прокси-домены + stream (в резервном режиме) +
    audit proxy.

    Preset перебивает РФ-direct (среди заблокированных есть российские
    домены), но пользовательские правила и YouTube/Discord остаются выше.
    """
    from . import routes

    user_direct: list[str] = []
    user_proxy: list[str] = []
    for raw, route in (user_routes or {}).items():
        name = routes.normalize_user_domain(raw)
        if not name:
            continue
        if str(route) == "direct" and name not in user_direct:
            user_direct.append(name)
        elif str(route) == "proxy" and name not in user_proxy:
            user_proxy.append(name)
    # Один и тот же домен не может быть в двух правилах: direct важнее.
    user_proxy = [name for name in user_proxy if name not in set(user_direct)]

    proxy_domains = list(PROXY_DOMAINS)
    if stream_via_proxy:
        proxy_domains += STREAM_DOMAINS
    proxy_domains += [d for d in (extra_domains or []) if d]
    user_direct_set = set(user_direct)
    user_proxy_set = set(user_proxy)
    audit_direct = [
        d for d in _normalise_domains(direct_domains)
        if d not in user_direct_set and d not in user_proxy_set
    ]
    audit_direct_set = set(audit_direct)
    stream_direct = []
    if not stream_via_proxy:
        stream_direct = [
            d for d in _normalise_domains(STREAM_DOMAINS)
            if d not in user_direct_set and d not in user_proxy_set
            and d not in audit_direct_set
        ]
    proxy_keys = [
        d for d in _normalise_domains(proxy_domains)
        if d not in user_direct_set and d not in user_proxy_set
        and d not in audit_direct_set
    ]
    rules = []
    if user_direct:
        rules.append({
            "type": "field", "outboundTag": "direct",
            "domain": [_domain_rule(d) for d in user_direct],
        })
    if user_proxy:
        rules.append({
            "type": "field", "outboundTag": "proxy",
            "domain": [_domain_rule(d) for d in user_proxy],
        })
    if audit_direct:
        rules.append({
            "type": "field", "outboundTag": "direct",
            "domain": [_domain_rule(d) for d in audit_direct],
        })
    if stream_direct:
        # YouTube и Discord закрывает zapret — мимо VPN.
        rules.append({
            "type": "field", "outboundTag": "direct",
            "domain": [_domain_rule(d) for d in stream_direct],
        })
    if blocked_rule:
        # Встроенный список Re:filter — через VPN; выше стоят свои правила,
        # YouTube/Discord и аудит direct.
        rules.append({
            "type": "field", "outboundTag": "proxy",
            "domain": [str(blocked_rule)],
        })
    rules += [
        # Российские сервисы и локальная сеть — всегда напрямую.
        {"type": "field", "outboundTag": "direct",
         "domain": ["geosite:category-ru", "geosite:private"]},
        {"type": "field", "outboundTag": "direct",
         "ip": ["geoip:ru", "geoip:private"]},
    ]
    # Заблокированное — через сервер.
    rules.append({
        "type": "field", "outboundTag": "proxy",
        "domain": [_domain_rule(d) for d in proxy_keys],
    })
    return {
        "log": {"loglevel": "warning", "access": "none"},
        "inbounds": [{
            "tag": "in",
            "listen": "127.0.0.1",
            "port": int(port),
            "protocol": "mixed",          # SOCKS и HTTP на одном порту
            "settings": {"udp": True, "auth": "noauth"},
            "sniffing": {"enabled": True, "destOverride": ["http", "tls"]},
        }],
        # ВАЖНО: первый outbound — маршрут по умолчанию. Здесь это `direct`,
        # иначе в прокси уходил бы весь зарубежный трафик.
        "outbounds": [
            {"tag": "direct", "protocol": "freedom"},
            server["outbound"],
            {"tag": "block", "protocol": "blackhole"},
        ],
        "routing": {
            "domainStrategy": "IPIfNonMatch",
            "rules": rules,
        },
    }


def _normalise_domains(values: list[str] | None) -> list[str]:
    """Чистые домены без повторов; служебные префиксы Xray сохраняются."""
    from . import routes

    seen: set[str] = set()
    result: list[str] = []
    for value in values or []:
        key = routes.clean_domain(value)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result


def _domain_rule(domain: str) -> str:
    """Правило Xray для домена: голое имя получает префикс ``domain:``."""
    text = str(domain or "").strip()
    if not text:
        return ""
    if ":" in text and text.split(":", 1)[0] in (
        "domain", "geosite", "geoip", "full", "regexp", "keyword", "ext"
    ):
        return text
    return f"domain:{text}"


def config_path() -> Path:
    return config.config_dir() / "xray-config.json"


def state_path() -> Path:
    return config.config_dir() / "xray-state.json"


def _process_cmdline(proc) -> str:
    try:
        return " ".join(proc.info.get("cmdline") or [])
    except Exception:  # noqa: BLE001
        return ""


def find_process():
    """Находит наш процесс Xray — даже запущенный другим экземпляром."""
    process = RUNNING.get("process")
    if process is not None and process.poll() is None:
        return process
    target = str(config_path()).lower()
    try:
        import psutil

        for proc in psutil.process_iter(["name", "cmdline"]):
            if (proc.info.get("name") or "").lower() != "xray.exe":
                continue
            if target in _process_cmdline(proc).lower():
                return proc
    except Exception:  # noqa: BLE001
        pass
    return None


def is_running() -> bool:
    return find_process() is not None


def _save_state(server: dict, port: int, stream: bool = False) -> None:
    config.atomic_write(state_path(), json.dumps({
        "name": server.get("name", ""),
        "host": server.get("host", ""),
        "latency": server.get("latency", -1),
        "port": port,
        "since": time.time(),
        "stream": bool(stream),
        "link": server.get("link", ""),
        "extra": server.get("extra", []),
    }, ensure_ascii=False))


def _saved_state() -> dict:
    try:
        return json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def saved_server() -> dict | None:
    """Сервер из сохранённого состояния — чтобы перезапустить на нём же."""
    saved = _saved_state()
    link = saved.get("link") or ""
    if not link:
        return None
    try:
        server = parse_vless(link)
    except Exception:  # noqa: BLE001
        return None
    server["latency"] = saved.get("latency", -1)
    return server


def status() -> dict:
    running = is_running()
    saved = _saved_state() if running else {}
    server = RUNNING.get("server") or saved
    return {
        "running": running,
        "server": server.get("name") or server.get("server", ""),
        "host": server.get("host", ""),
        "latency": server.get("latency", -1),
        "port": RUNNING.get("port") or saved.get("port") or DEFAULT_PORT,
        "system_proxy": system_proxy_enabled(),
        "since": RUNNING.get("since") or saved.get("since", 0),
        # True = YouTube/Discord тоже через прокси (zapret не работает).
        "stream": bool(RUNNING.get("stream", saved.get("stream", False))),
        "blocked_list": _blocked_available(),
    }


def _blocked_available() -> bool:
    """Есть ли валидный файл встроенного списка рядом с ядром."""
    try:
        from . import blocked

        return blocked._valid(blocked.bin_path())
    except Exception:  # noqa: BLE001
        return False


def _kill_process() -> None:
    """Гасит наш Xray, не трогая системный прокси."""
    process = RUNNING.pop("process", None)
    if process is not None and process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=6)
        except Exception:  # noqa: BLE001
            try:
                process.kill()
            except Exception:  # noqa: BLE001
                pass
    other = find_process()
    if other is not None:
        try:
            other.terminate()
        except Exception:  # noqa: BLE001
            pass
    _close_log()


def restart_with_stream(enable: bool, cfg: dict | None = None) -> dict:
    """Переключает режим YouTube/Discord, перезапуская Xray на том же сервере.

    ``enable=True`` — zapret не работает, поэтому YouTube и Discord уходят
    через сервер. ``False`` — снова напрямую под zapret.
    """
    with _LOCK:
        return _restart_with_stream(enable, cfg)


def _restart_with_stream(enable: bool, cfg: dict | None = None) -> dict:
    """Тело ``restart_with_stream`` — вызывается под замком."""
    cfg = cfg or config.load_config()
    if not is_running():
        return {"ok": False, "error": "прокси не подключён"}

    saved = _saved_state()
    if bool(saved.get("stream", False)) == bool(enable):
        return {"ok": True, "unchanged": True, "stream": bool(enable)}

    port = int(saved.get("port") or cfg.get("xray_port") or DEFAULT_PORT)
    server = None
    link = saved.get("link") or ""
    if link:
        try:
            server = parse_vless(link)
            server["latency"] = saved.get("latency", -1)
        except Exception:  # noqa: BLE001
            server = None
    if server is None:
        server = pick_server()
    if server is None:
        return {"ok": False, "error": "нет доступных серверов"}

    server["extra"] = saved.get("extra") or []
    _kill_process()
    time.sleep(0.6)
    result = start(cfg, extra_domains=server["extra"], server=server,
                   stream_via_proxy=enable)
    return result


def start(cfg: dict | None = None, extra_domains: list[str] | None = None,
          server: dict | None = None, stream_via_proxy: bool = False,
          direct_domains: list[str] | None = None) -> dict:
    """Поднимает Xray на выбранном сервере и включает системный прокси.

    ``direct_domains=None`` — взять оверрайды из ``routes``. Домены прокси из
    оверрайдов добавляются к ``extra_domains``. Замок не даёт UI и надзирателю
    поднять два ядра одновременно.
    """
    with _LOCK:
        return _start(cfg, extra_domains, server, stream_via_proxy, direct_domains)


def _effective_stream(cfg: dict, stream_via_proxy: bool = False) -> bool:
    """Резервный режим или принудительный YouTube через VPN из настроек."""
    return bool(stream_via_proxy or (cfg or {}).get("youtube_via_vpn"))


def _desired_stream(cfg: dict, saved_stream: bool = False) -> bool:
    """Какой режим потока нужен сейчас (без учёта явного запроса).

    Флаг ``youtube_via_vpn`` важнее всего; иначе — только резервный режим:
    YouTube/Discord через VPN, когда zapret не запущен. Сохранённый ``stream``
    используется лишь как запасной вариант, если статус собрать не удалось —
    иначе после выключения флага режим остался бы включённым.
    """
    if (cfg or {}).get("youtube_via_vpn"):
        return True
    if not (cfg or {}).get("xray_stream_fallback", True):
        return False
    try:
        from . import status as status_module

        zapret = (status_module.collect(cfg).get("zapret") or {})
        return not bool(zapret.get("running"))
    except Exception:  # noqa: BLE001
        return bool(saved_stream)


def _blocked_preset(cfg: dict) -> tuple[str | None, bool]:
    """Правило встроенного списка Re:filter и признак «файл на месте».

    Возвращает ``(rule, ok)``: ``rule`` — ``ext:<файл>:REFILTER`` или ``None``.
    Сеть не используется, только локальная копия рядом с ядром.
    """
    if not (cfg or {}).get("blocked_list_enabled", True):
        return None, False
    try:
        from . import blocked

        path = blocked.ensure()
        if path is not None:
            return f"ext:{path.name}:{blocked.CATEGORY}", True
    except Exception:  # noqa: BLE001
        pass
    return None, False


def _start(cfg: dict | None = None, extra_domains: list[str] | None = None,
           server: dict | None = None, stream_via_proxy: bool = False,
           direct_domains: list[str] | None = None) -> dict:
    """Тело ``start`` — вызывается под замком."""
    cfg = cfg or config.load_config()
    stream = _effective_stream(cfg, stream_via_proxy)
    if is_running():
        if cfg.get("xray_system_proxy", True):
            # Ядро уже работает, но системный прокси мог остаться выключенным
            # после падения/перезапуска — браузеры не должны ходить мимо.
            try:
                set_system_proxy(True, int(cfg.get("xray_port") or DEFAULT_PORT),
                                 stream)
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True, "already": True, **status()}

    exe = ensure_core()
    if exe is None:
        return {"ok": False, "error": "не найдено ядро Xray (xray.exe)"}

    if server is None:
        try:
            links = fetch_subscription(cfg.get("xray_subscription"))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"подписка недоступна: {type(exc).__name__}"}
        server = pick_server(links)
    if server is None:
        return {"ok": False, "error": "нет доступных серверов"}

    port = int(cfg.get("xray_port") or DEFAULT_PORT)
    extra, legacy_direct = _config_route_lists(cfg, extra_domains)
    # Оверрайды аудита маршрутов: что проверки требуют вести через прокси.
    from . import routes

    try:
        extra += routes.proxy_domains()
        if direct_domains is None:
            direct_domains = routes.direct_domains()
    except Exception:  # noqa: BLE001
        pass
    # Домены «всегда напрямую»: оверрайды аудита плюс legacy-настройка конфига.
    direct_all = list(direct_domains or []) + legacy_direct
    user_map = cfg.get("xray_routes")
    if not isinstance(user_map, dict):
        user_map = {}
    blocked_rule, blocked_ok = _blocked_preset(cfg)
    config_path = config.config_dir() / "xray-config.json"
    payload = json.dumps(build_config(server, port, extra, stream,
                                      direct_all, user_map, blocked_rule),
                         ensure_ascii=False, indent=2)
    config.atomic_write(config_path, payload)
    try:
        written = config_path.read_text(encoding="utf-8")
    except OSError:
        written = ""
    if written != payload:
        return {"ok": False, "error": "не записать конфиг Xray"}

    # Вывод ядра пишем в файл: PIPE никто не вычитывает, и при заполнении
    # буфера Xray встаёт намертво.
    _trim_log()
    try:
        log_handle = open(_log_path(), "ab")
    except OSError as exc:
        return {"ok": False, "error": f"не открыть журнал Xray: {exc}"}
    env = dict(os.environ, XRAY_LOCATION_ASSET=str(exe.parent))
    try:
        process = subprocess.Popen(
            [str(exe), "-c", str(config_path)],
            cwd=str(exe.parent), env=env,
            stdin=subprocess.DEVNULL, stdout=log_handle, stderr=subprocess.STDOUT,
            creationflags=actions.CREATE_NO_WINDOW,
        )
    except OSError as exc:
        log_handle.close()
        return {"ok": False, "error": f"не запустить Xray: {exc}"}

    RUNNING.update({"process": process, "server": server, "port": port,
                    "since": time.time(), "config": str(config_path),
                    "stream": bool(stream), "log": log_handle})
    _save_state(server, port, stream)

    # Ждём, пока порт начнёт слушать.
    deadline = time.time() + 15
    while time.time() < deadline:
        time.sleep(0.3)
        if process.poll() is not None:
            _close_log()
            RUNNING.clear()
            return {"ok": False, "error": "Xray завершился при запуске",
                    "log": _read_log()}
        if _port_open(port):
            if cfg.get("xray_system_proxy", True):
                # При stream=False домены YouTube/Discord уйдут в обход прокси.
                set_system_proxy(True, port, stream)
            return {"ok": True, "server": server, "port": port,
                    "latency": server.get("latency", -1),
                    "blocked_list": blocked_ok}

    stop()
    return {"ok": False, "error": "порт прокси не открылся", "log": _read_log()}


def stop(restore_proxy: bool = True, port: int | None = None) -> dict:
    """Останавливает Xray и снимает системный прокси на фактическом порту."""
    with _LOCK:
        return _stop(restore_proxy, port)


def _stop(restore_proxy: bool = True, port: int | None = None) -> dict:
    """Тело ``stop`` — вызывается под замком."""
    actual_port = port
    if actual_port is None:
        actual_port = RUNNING.get("port")
    if actual_port is None:
        try:
            actual_port = _saved_state().get("port")
        except Exception:  # noqa: BLE001
            actual_port = None
    try:
        actual_port = int(actual_port or DEFAULT_PORT)
    except (TypeError, ValueError):
        actual_port = DEFAULT_PORT
    _kill_process()
    if restore_proxy:
        set_system_proxy(False, actual_port)
    RUNNING.clear()
    return {"ok": True, "port": actual_port}


def restart(cfg: dict | None = None, server: dict | None = None) -> dict:
    with _LOCK:
        return _restart(cfg, server)


def _restart(cfg: dict | None = None, server: dict | None = None) -> dict:
    """Тело ``restart`` — вызывается под замком."""
    stop()
    time.sleep(0.5)
    return start(cfg, server=server)


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _log_path() -> Path:
    return config.config_dir() / "xray.log"


def _trim_log(limit: int = 256 * 1024) -> None:
    """Оставляет только хвост журнала: не храним историю посещений."""
    path = _log_path()
    try:
        size = path.stat().st_size
        if size <= limit:
            return
        data = path.read_bytes()[-limit:]
        path.write_bytes(data)
    except OSError:
        pass


def _close_log() -> None:
    """Закрывает файл журнала ядра, если он открыт."""
    handle = RUNNING.pop("log", None)
    if handle is None:
        return
    try:
        handle.close()
    except OSError:
        pass


def _read_log(limit: int = 4096) -> str:
    """Хвост журнала Xray: ошибки старта отдаются вместе с ним."""
    try:
        with open(_log_path(), "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


# ---- список серверов, переключение, проверка ------------------------------


def servers_path() -> Path:
    return config.config_dir() / "xray-servers.json"


def scan_servers(timeout: float = 5.0) -> list[dict]:
    """Опрашивает подписку, проверяет серверы и сохраняет список."""
    links = fetch_subscription()
    alive = rank_servers(links, )
    rows = [{
        "name": s["name"], "host": s["host"], "port": s["port"],
        "latency": s["latency"], "link": s["link"],
    } for s in alive]
    config.atomic_write(
        servers_path(),
        json.dumps({"at": time.time(), "servers": rows}, ensure_ascii=False, indent=2),
    )
    return rows


def cached_servers() -> list[dict]:
    """Сохранённый список серверов (без обращения к сети)."""
    try:
        data = json.loads(servers_path().read_text(encoding="utf-8"))
        return data.get("servers") or []
    except (OSError, ValueError):
        return []


def verify(target: str = "www.instagram.com", timeout: float = 10.0) -> dict:
    """Проверяет, что трафик реально проходит через прокси.

    Запрос идёт на заблокированный сайт, то есть обязательно через сервер, —
    так проверяется именно туннель, а не просто «порт открыт».
    """
    import http.client
    import ssl

    if not is_running():
        return {"ok": False, "error": "не подключён"}
    port = int(status().get("port") or DEFAULT_PORT)
    started = time.perf_counter()
    connection = None
    try:
        connection = http.client.HTTPSConnection(
            "127.0.0.1", port, timeout=timeout, context=ssl.create_default_context())
        connection.set_tunnel(target, 443)
        connection.request("GET", "/", headers={"User-Agent": "Mozilla/5.0"})
        response = connection.getresponse()
        response.read(512)
        return {"ok": 200 <= response.status < 400, "status": response.status,
                "ms": round((time.perf_counter() - started) * 1000)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": type(exc).__name__,
                "ms": round((time.perf_counter() - started) * 1000)}
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:  # noqa: BLE001
                pass


def switch_to(server: dict, cfg: dict | None = None) -> dict:
    """Перезапускает Xray на указанном сервере, сохраняя режим и настройки."""
    with _LOCK:
        return _switch_to(server, cfg)


def _switch_to(server: dict, cfg: dict | None = None) -> dict:
    """Тело ``switch_to`` — вызывается под замком."""
    cfg = cfg or config.load_config()
    saved = _saved_state()
    server = dict(server)
    server["extra"] = saved.get("extra") or []
    stream = _desired_stream(cfg, bool(saved.get("stream", False)))
    if is_running():
        _kill_process()
        time.sleep(0.6)
    return start(cfg, extra_domains=server["extra"], server=server,
                 stream_via_proxy=stream)


def switch_server(exclude: set[str] | None = None, cfg: dict | None = None) -> dict:
    """Подбирает другой сервер (кроме тех, что уже подвели) и переключается."""
    with _LOCK:
        return _switch_server(exclude, cfg)


def _switch_server(exclude: set[str] | None = None, cfg: dict | None = None) -> dict:
    """Тело ``switch_server`` — вызывается под замком."""
    exclude = exclude or set()
    rows = cached_servers()
    if not rows:
        rows = scan_servers()
    candidates = [r for r in rows if r["host"] not in exclude]
    if not candidates:
        candidates = rows
    if not candidates:
        return {"ok": False, "error": "нет доступных серверов"}
    candidates.sort(key=lambda r: r.get("latency", 9999))
    chosen = candidates[0]
    server = parse_vless(chosen["link"])
    server["latency"] = chosen.get("latency", -1)
    return switch_to(server, cfg)


# ---- системный прокси -----------------------------------------------------

_INTERNET_SETTINGS = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"


def system_proxy_enabled() -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _INTERNET_SETTINGS) as key:
            return int(winreg.QueryValueEx(key, "ProxyEnable")[0]) == 1
    except OSError:
        return False


def system_proxy_server() -> str:
    """Текущий ProxyServer из HKCU (пусто — нет/не читается)."""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _INTERNET_SETTINGS) as key:
            return str(winreg.QueryValueEx(key, "ProxyServer")[0] or "")
    except OSError:
        return ""


def _group_is_proxied(group: tuple[str, ...], proxy_set: set[str]) -> bool:
    """Есть ли в пользовательских proxy-правилах домен этой группы."""
    for name in proxy_set:
        for member in group:
            if name == member or name.endswith("." + member):
                return True
    return False


def _proxy_bypass_list(stream_via_proxy: bool | None = None) -> str:
    """Исключения системного прокси.

    Базовые сети, пользовательские и аудиторские direct-домены. Когда
    YouTube/Discord идут напрямую под zapret (``stream_via_proxy`` ложно),
    их домены тоже добавляются в обход системного прокси — иначе браузер
    гонит TCP через локальный Xray лишним хопом. Если домен явно отправлен
    в VPN пользовательским правилом, он в обход не попадает.
    """
    from . import routes

    parts = ["localhost", "127.*", "10.*", "172.16.*", "192.168.*", "<local>"]
    try:
        cfg = config.load_config()
    except Exception:  # noqa: BLE001
        cfg = {}
    user_direct, user_proxy = routes.user_routes(cfg)
    proxy_set = set(user_proxy)
    try:
        audit_direct = routes.direct_domains()
    except Exception:  # noqa: BLE001
        audit_direct = []
    # Если есть xray_routes, старые списки не учитываем вовсе.
    legacy = ([] if isinstance(cfg.get("xray_routes"), dict)
              else [str(item or "") for item in (cfg.get("xray_direct_domains") or [])])

    entries: list[tuple[str, bool]] = []
    for name in user_direct + audit_direct:
        if name and name not in proxy_set:
            entries.append((name, True))
    for raw in legacy:
        name = routes.clean_domain(raw)
        if name and name not in proxy_set:
            entries.append((name, raw.strip().startswith("*.")))
    if not bool(stream_via_proxy):
        # YouTube/Discord закрывает zapret: браузер должен идти мимо Xray.
        # Если пользователь отправил в VPN хотя бы один домен группы, из
        # обхода убираем всю группу — иначе часть трафика пойдёт напрямую.
        youtube_proxied = _group_is_proxied(YOUTUBE_GROUP, proxy_set)
        discord_proxied = _group_is_proxied(DISCORD_GROUP, proxy_set)
        for raw in STREAM_DOMAINS:
            name = routes.clean_domain(raw)
            if not name or name.startswith("geosite:"):
                continue
            if name in YOUTUBE_GROUP and youtube_proxied:
                continue
            if name in DISCORD_GROUP and discord_proxied:
                continue
            if any(p == name or p.endswith("." + name) for p in proxy_set):
                continue
            entries.append((name, True))
    for name, wildcard in entries:
        if name not in parts:
            parts.append(name)
        alias = f"*.{name}"
        if wildcard and alias not in parts:
            parts.append(alias)
    return ";".join(parts)


def set_system_proxy(enable: bool, port: int = DEFAULT_PORT,
                     stream_via_proxy: bool = False) -> dict:
    """Включает/выключает системный прокси (только HKCU — права не нужны)."""
    if not sys.platform.startswith("win"):
        return {"ok": False}
    try:
        import winreg

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _INTERNET_SETTINGS, 0,
                                winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as key:
            if enable:
                winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
                winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ,
                                  f"127.0.0.1:{port}")
                winreg.SetValueEx(
                    key, "ProxyOverride", 0, winreg.REG_SZ,
                    _proxy_bypass_list(stream_via_proxy),
                )
            else:
                own = f"127.0.0.1:{int(port)}"
                try:
                    current, _kind = winreg.QueryValueEx(key, "ProxyServer")
                except OSError:
                    current = ""
                if str(current or "").strip() != own:
                    # Не наш прокси (или его вообще нет) — не трогаем.
                    return {"ok": True, "enabled": True, "kept": True,
                            "server": str(current or "")}
                winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
                try:
                    winreg.DeleteValue(key, "ProxyServer")
                except OSError:
                    pass
        _refresh_wininet()
        return {"ok": True, "enabled": enable, "port": port}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


def _refresh_wininet() -> None:
    """Сообщает Windows, что настройки прокси изменились."""
    try:
        wininet = ctypes.windll.wininet
        wininet.InternetSetOptionW(0, 39, 0, 0)  # SETTINGS_CHANGED
        wininet.InternetSetOptionW(0, 37, 0, 0)  # REFRESH
    except Exception:  # noqa: BLE001
        pass
