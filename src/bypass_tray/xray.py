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
import re
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

#: Протоколы, которые умеем разбирать в outbound Xray. Подписка и свои
#: ссылки проходят через один разбор: маршрутизации протокол безразличен.
SUPPORTED_SCHEMES = ("vless", "vmess", "trojan", "ss")

#: Сколько ждём ответа от одного сервера и сколько всего тратим на подбор.
#: Подбор идёт под _LOCK: сорок серверов по пять секунд — это двести секунд,
#: на которые встают stop/switch из UI и надзирателя.
PROBE_TIMEOUT = 5.0
SEARCH_BUDGET = 25.0

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


# ---- ссылки: подписка и свои конфиги --------------------------------------


#: Разделитель нескольких ссылок в одной строке. Режем только там, где после
#: запятой/точки с запятой начинается новая ссылка: запятая во фрагменте
#: («#Сервер, Германия») остаётся частью имени.
_LINK_SEPARATOR = re.compile(
    r"[,;]\s*(?=(?:vless|vmess|trojan|ss)://)", re.IGNORECASE)


def _b64_text(value: str) -> str:
    """Мягко декодирует base64: мусор и битые данные дают пустую строку.

    Подписки приходят и обычным текстом, и base64 (иногда urlsafe, без
    выравнивания), поэтому ошибка разбора здесь — рядовой случай, а не сбой.
    """
    raw = "".join((value or "").split())
    if not raw:
        return ""
    raw = raw.replace("-", "+").replace("_", "/")
    raw += "=" * (-len(raw) % 4)
    try:
        return base64.b64decode(raw).decode("utf-8", "replace")
    except (TypeError, ValueError):
        return ""


def links_from_text(text: str) -> list[str]:
    """Достаёт ссылки поддерживаемых протоколов из произвольного текста.

    Принимает построчный список ссылок, base64-подписку (в том числе
    закодированную дважды), JSON-массив, несколько ссылок в одной строке
    через запятую — содержимое файла или буфера обмена. Всё, что не является
    ссылкой ``vless``/``vmess``/``trojan``/``ss``, молча отбрасывается;
    повторы убираются с сохранением порядка. На мусор не бросает исключений.
    """
    found: list[str] = []
    seen: set[str] = set()

    def take(block: str, depth: int) -> None:
        block = (block or "").replace("\ufeff", "")
        stripped = block.strip()
        # JSON-массив: и одной строкой, и разложенный по строкам.
        if depth < 3 and stripped.startswith("[") and stripped.endswith("]"):
            try:
                data = json.loads(stripped)
            except ValueError:
                data = None
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, str):
                        take(item, depth + 1)
                return
        if "://" not in block:
            # Рекурсия по факту удачного декодирования, а не по наличию
            # «://»: иначе base64 от base64 не разбирался бы.
            if depth < 3:
                decoded = _b64_text(block)
                if decoded and decoded != block:
                    take(decoded, depth + 1)
            return
        for raw in block.splitlines():
            # Скобки, кавычки и запятые — от JSON-массивов и CSV.
            line = raw.strip().strip('[]",').strip()
            if not line:
                continue
            if "://" not in line:
                if depth < 3:
                    decoded = _b64_text(line)
                    if decoded and decoded != line:
                        take(decoded, depth + 1)
                continue
            for piece in _LINK_SEPARATOR.split(line):
                link = piece.strip().strip('[]",').strip()
                if "://" not in link:
                    continue
                scheme = link.split("://", 1)[0].strip().lower()
                if scheme in SUPPORTED_SCHEMES and link not in seen:
                    seen.add(link)
                    found.append(link)

    take(text if isinstance(text, str) else "", 0)
    return found


def user_links(cfg: dict | None = None) -> list[str]:
    """Свои ссылки из ``cfg['xray_links']`` — списком или одной строкой."""
    cfg = cfg if cfg is not None else config.load_config()
    value = cfg.get("xray_links")
    if isinstance(value, str):
        return links_from_text(value)
    if isinstance(value, (list, tuple)):
        text = "\n".join(item for item in value if isinstance(item, str))
        return links_from_text(text)
    return []


def collect_links(cfg: dict | None = None) -> list[str]:
    """Все доступные ссылки: свои конфиги плюс подписка, без повторов.

    Подписка необязательна: не скачалась — работаем на своих ссылках и
    предупреждаем в журнале. Без источников — ``ValueError`` с причиной.
    """
    cfg = cfg if cfg is not None else config.load_config()
    links = user_links(cfg)
    url = cfg.get("xray_subscription") or DEFAULT_SUBSCRIPTION
    failure = ""
    if url:
        try:
            links += fetch_subscription(url)
        except Exception as exc:  # noqa: BLE001
            # Подписка — дополнительный источник: её сетевые и форматные
            # сбои не должны обнулять уже введённые свои ссылки. В журнал
            # идёт только тип ошибки: в ссылке персональный ключ.
            failure = type(exc).__name__
    result: list[str] = []
    seen: set[str] = set()
    for link in links:
        if isinstance(link, str) and link and link not in seen:
            seen.add(link)
            result.append(link)
    if not result:
        if failure:
            raise ValueError(
                f"подписка не скачалась ({failure}); своих конфигов нет")
        raise ValueError("нет конфигов: укажите подписку или свои ссылки")
    if failure:
        # Локальный импорт: activity тянет config, а xray импортируется из
        # него же — на уровне модуля это дало бы цикл.
        from . import activity

        activity.log(
            f"подписка не скачалась ({failure}) — работаю на своих конфигах",
            kind="warn")
    return result


def fetch_subscription(url: str | None = None, timeout: int = 25) -> list[str]:
    """Возвращает ссылки vless/vmess/trojan/ss из подписки.

    Подписка отдаёт либо готовый список ссылок, либо base64 с ним. Ссылка
    берётся из ``config.json`` (поле ``xray_subscription``): в коде её нет,
    потому что это персональный ключ.
    """
    url = url or config.load_config().get("xray_subscription") or DEFAULT_SUBSCRIPTION
    if not url:
        raise ValueError(
            "не задана подписка: укажите xray_subscription в config.json"
        )
    request = urllib.request.Request(url, headers={"User-Agent": "v2rayNG/1.8"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        text = response.read().decode("utf-8", "replace")
    return links_from_text(text)


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


def parse_vmess(link: str) -> dict:
    """Разбирает ``vmess://`` (base64 JSON) в описание сервера и outbound."""
    body = link.strip()
    if body.lower().startswith("vmess://"):
        body = body[len("vmess://"):]
    body = body.split("#", 1)[0]
    try:
        data = json.loads(_b64_text(body))
    except (TypeError, ValueError):
        raise ValueError("vmess: ссылка не декодируется") from None
    if not isinstance(data, dict):
        raise ValueError("vmess: ссылка не декодируется")

    host = str(data.get("add") or "").strip()
    try:
        # Порт встречается и числом, и строкой, и как 443.0 из чужого JSON.
        port = int(float(str(data.get("port") or 0)))
    except (TypeError, ValueError):
        port = 0
    user_id = str(data.get("id") or "").strip()
    if not host:
        raise ValueError("vmess: в ссылке нет адреса сервера")
    if not 0 < port < 65536:
        raise ValueError("vmess: неверный порт")
    if not user_id:
        raise ValueError("vmess: в ссылке нет id пользователя")

    net = str(data.get("net") or "tcp").strip() or "tcp"
    # Провайдеры пишут TLS по-разному: tls, reality, true, 1.
    tls = str(data.get("tls") or "").strip().lower()
    header_host = str(data.get("host") or "").strip()
    path = str(data.get("path") or "").strip()
    stream: dict = {
        "network": net,
        "security": "tls" if tls in ("tls", "reality", "true", "1") else "none",
    }
    if stream["security"] == "tls":
        stream["tlsSettings"] = {
            "serverName": (str(data.get("sni") or "").strip()
                           or header_host or host),
            "fingerprint": str(data.get("fp") or "chrome"),
        }
    if net == "ws":
        ws: dict = {"path": path or "/"}
        if header_host:
            ws["headers"] = {"Host": header_host}
        stream["wsSettings"] = ws
    elif net == "grpc":
        stream["grpcSettings"] = {"serviceName": path}
    elif net == "kcp":
        stream["kcpSettings"] = {
            "header": {"type": str(data.get("type") or "none")}}

    try:
        alter_id = int(str(data.get("aid") or 0))
    except ValueError:
        alter_id = 0
    outbound = {
        "tag": "proxy",
        "protocol": "vmess",
        "settings": {
            "vnext": [{
                "address": host,
                "port": port,
                "users": [{
                    "id": user_id,
                    "alterId": alter_id,
                    "security": str(data.get("scy") or "auto"),
                }],
            }]
        },
        "streamSettings": stream,
    }
    return {
        "name": str(data.get("ps") or "").strip() or host,
        "host": host,
        "port": port,
        "outbound": outbound,
        "link": link,
    }


def parse_trojan(link: str) -> dict:
    """Разбирает ``trojan://`` в описание сервера и outbound."""
    parts = urllib.parse.urlsplit(link.strip())
    query = dict(urllib.parse.parse_qsl(parts.query))
    try:
        port = int(parts.port or 443)
    except ValueError:
        raise ValueError("trojan: неверный порт") from None
    host = parts.hostname or ""
    password = urllib.parse.unquote(parts.username or "")
    if not host or not password:
        raise ValueError("trojan: в ссылке нет адреса или пароля")

    net = query.get("type") or "tcp"
    security = query.get("security") or "tls"
    stream: dict = {"network": net, "security": security}
    if security == "reality":
        stream["realitySettings"] = {
            "serverName": query.get("sni", ""),
            "fingerprint": query.get("fp", "chrome"),
            "publicKey": query.get("pbk", ""),
            "shortId": query.get("sid", ""),
        }
    elif security == "tls":
        stream["tlsSettings"] = {
            "serverName": query.get("sni") or host,
            "fingerprint": query.get("fp", "chrome"),
        }
    if net == "ws":
        ws: dict = {"path": query.get("path") or "/"}
        if query.get("host"):
            ws["headers"] = {"Host": query["host"]}
        stream["wsSettings"] = ws
    elif net == "grpc":
        stream["grpcSettings"] = {
            "serviceName": query.get("serviceName") or query.get("path", "")}

    server: dict = {"address": host, "port": port, "password": password}
    flow = query.get("flow", "")
    if flow:
        server["flow"] = flow
    outbound = {
        "tag": "proxy",
        "protocol": "trojan",
        "settings": {"servers": [server]},
        "streamSettings": stream,
    }
    name = urllib.parse.unquote(parts.fragment or "").strip() or host
    return {"name": name, "host": host, "port": port,
            "outbound": outbound, "link": link}


def _ss_credentials(userinfo: str) -> tuple[str, str] | None:
    """Метод и пароль Shadowsocks: открытым текстом или base64."""
    value = urllib.parse.unquote(userinfo)
    if ":" not in value:
        value = _b64_text(value)
    if ":" not in value:
        return None
    method, password = value.split(":", 1)
    method = method.strip()
    if not method or not password:
        return None
    return method, password


def parse_ss(link: str) -> dict:
    """Разбирает ``ss://`` в обоих видах: base64(метод:пароль)@хост и base64(всё)."""
    text = link.strip()
    body = text.split("://", 1)[1] if "://" in text else text
    name = ""
    if "#" in body:
        body, fragment = body.split("#", 1)
        name = urllib.parse.unquote(fragment).strip()
    body = body.split("?", 1)[0].strip().rstrip("/")
    if not body:
        raise ValueError("ss: пустая ссылка")
    if "@" in body:
        userinfo, location = body.rsplit("@", 1)
    else:
        decoded = _b64_text(body)
        if "@" not in decoded:
            raise ValueError("ss: ссылка не декодируется")
        userinfo, location = decoded.rsplit("@", 1)
    credentials = _ss_credentials(userinfo)
    if credentials is None:
        raise ValueError("ss: в ссылке нет метода и пароля")
    method, password = credentials
    address = urllib.parse.urlsplit("//" + location)
    try:
        port = int(address.port or 8388)
    except ValueError:
        raise ValueError("ss: неверный порт") from None
    host = address.hostname or ""
    if not host:
        raise ValueError("ss: в ссылке нет адреса сервера")
    outbound = {
        "tag": "proxy",
        "protocol": "shadowsocks",
        "settings": {"servers": [{
            "address": host, "port": port,
            "method": method, "password": password,
        }]},
    }
    return {"name": name or host, "host": host, "port": port,
            "outbound": outbound, "link": link}


def parse_link(link: str) -> dict:
    """Разбирает ссылку любого поддерживаемого протокола.

    Схему называем в ошибке, но саму ссылку не печатаем: в ней пароль/id.
    """
    text = (link or "").strip()
    scheme = text.split("://", 1)[0].strip().lower() if "://" in text else ""
    if scheme == "vless":
        return parse_vless(text)
    if scheme == "vmess":
        return parse_vmess(text)
    if scheme == "trojan":
        return parse_trojan(text)
    if scheme == "ss":
        return parse_ss(text)
    raise ValueError("неподдерживаемая ссылка: нужен vless, vmess, trojan или ss")


def probe_server(server: dict, timeout: float = 5.0) -> int:
    """TCP-доступность сервера. Возвращает задержку в мс или -1."""
    started = time.perf_counter()
    try:
        with socket.create_connection((server["host"], server["port"]), timeout=timeout):
            return round((time.perf_counter() - started) * 1000)
    except OSError:
        return -1


def rank_servers(links: list[str], limit: int | None = None,
                 time_budget: float = SEARCH_BUDGET) -> list[dict]:
    """Разбирает ссылки и сортирует серверы по доступности и задержке.

    Общее время проверки ограничено ``time_budget``: проба каждого сервера
    укладывается в остаток бюджета, а когда он вышел — осмотр обрывается.
    Иначе сорок серверов по пять секунд держали бы _LOCK двести секунд.
    """
    servers = []
    deadline = time.monotonic() + max(0.0, time_budget)
    for link in links:
        remaining = deadline - time.monotonic()
        if remaining < 0.5:
            break
        try:
            server = parse_link(link)
        except Exception:  # noqa: BLE001
            continue
        latency = probe_server(server, timeout=min(PROBE_TIMEOUT, remaining))
        server["latency"] = latency
        servers.append(server)
    alive = [s for s in servers if s["latency"] >= 0]
    alive.sort(key=lambda s: s["latency"])
    if limit:
        alive = alive[:limit]
    return alive


def pick_server(links: list[str] | None = None,
                time_budget: float = SEARCH_BUDGET) -> dict | None:
    """Возвращает самый быстрый из доступных серверов.

    Без явного списка берёт свои конфиги и подписку (``collect_links``).
    """
    links = links if links is not None else collect_links()
    alive = rank_servers(links, time_budget=time_budget)
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
                 blocked_rule: str | None = None,
                 routing_mode: str = "blocked") -> dict:
    """Конфиг Xray: локальный прокси + маршрутизация.

    Маршрут по умолчанию задаёт **первый outbound** — catch-all правила в
    ``routing.rules`` нет. Режим ``routing_mode``:

    * ``blocked`` — ``direct`` первым: через сервер идёт только перечисленное;
    * ``all`` — сервер первым: через прокси идёт всё, что не поймано явными
      правилами (РФ, ``private`` и пользовательские ``direct`` остаются
      напрямую выше по списку правил).

    Порядок правил:
    1) пользовательские direct, 2) пользовательские proxy, 3) audit direct
    (только вне ``all``)
    4) YouTube/Discord напрямую (если не резервный режим и не ``all``),
    5) встроенный список заблокированных ``blocked_rule`` через прокси,
    6) РФ/private напрямую, 7) встроенные прокси-домены + stream (в резервном
    режиме) + audit proxy.

    Preset перебивает РФ-direct (среди заблокированных есть российские
    домены), но пользовательские правила и YouTube/Discord остаются выше.
    """
    from . import routes

    mode = config.normalise_routing_mode(routing_mode)

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
    if mode == "all":
        # В режиме "all" прямой маршрут аудита уводит трафик мимо VPN. Файл
        # мог остаться от сессии в "blocked", поэтому защищаемся здесь, а не
        # только при записи. Пользовательские direct выше — это осознанный
        # выбор, их не трогаем.
        audit_direct = []
    audit_direct_set = set(audit_direct)
    stream_direct = []
    if not stream_via_proxy and mode != "all":
        # В режиме "all" YouTube/Discord не исключение: иначе «всё, кроме РФ»
        # оставляло бы их на прямом канале мимо VPN.
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
        # ВАЖНО: первый outbound — маршрут по умолчанию. В "blocked" это
        # `direct`, в "all" — сервер: явные правила выше в списке всё равно
        # отправляют РФ/private и пользовательские direct напрямую.
        "outbounds": _ordered_outbounds(server, mode),
        "routing": {
            "domainStrategy": "IPIfNonMatch",
            "rules": rules,
        },
    }


def _ordered_outbounds(server: dict, mode: str) -> list[dict]:
    """Outbounds с маршрутом по умолчанию первым (catch-all правила нет)."""
    direct = {"tag": "direct", "protocol": "freedom"}
    proxy = server["outbound"]
    order = [proxy, direct] if mode == "all" else [direct, proxy]
    return order + [{"tag": "block", "protocol": "blackhole"}]


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


def _save_state(server: dict, port: int, stream: bool = False,
                routing_mode: str = "blocked") -> None:
    config.atomic_write(state_path(), json.dumps({
        "name": server.get("name", ""),
        "host": server.get("host", ""),
        "latency": server.get("latency", -1),
        "port": port,
        "since": time.time(),
        "stream": bool(stream),
        # Фактический режим, на котором поднято ядро: конфиг может
        # измениться уже после запуска, а status() должен не врать.
        "routing_mode": config.normalise_routing_mode(routing_mode),
        "link": server.get("link", ""),
        "extra": server.get("extra", []),
    }, ensure_ascii=False))


def _saved_state() -> dict:
    """Сохранённое состояние; всё, что не объект, считаем пустым.

    Валидный JSON вида ``null``/``[]``/``"x"`` раньше возвращался как есть и
    ронял ``status()``/``saved_server()`` на ``.get`` — трей молча умирал.
    """
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def saved_server() -> dict | None:
    """Сервер из сохранённого состояния — чтобы перезапустить на нём же."""
    saved = _saved_state()
    link = saved.get("link") or ""
    if not link:
        return None
    try:
        server = parse_link(link)
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
        # Режим маршрутизации: фактический у работающего ядра, иначе из конфига.
        "routing_mode": _routing_mode(saved),
        # Фактические исключения системного прокси (ProxyOverride из реестра).
        "bypass": system_proxy_bypass(),
    }


def _routing_mode(saved: dict | None = None, use_config: bool = True) -> str:
    """Режим работающего ядра; без запуска — сохранённый или из конфига.

    ``use_config=False`` — только дешёвые чтения: ``config.load_config()`` на
    пустом ``zapret_dir`` запускает автопоиск каталога (обход диска), а
    ``coverage`` опрашивается каждые 5 секунд из ``status.collect``.
    """
    if RUNNING.get("routing_mode"):
        return config.normalise_routing_mode(RUNNING.get("routing_mode"))
    saved = _saved_state() if saved is None else saved
    if saved.get("routing_mode"):
        return config.normalise_routing_mode(saved.get("routing_mode"))
    if not use_config:
        return "blocked"
    try:
        return config.normalise_routing_mode(
            config.load_config().get("routing_mode"))
    except Exception:  # noqa: BLE001
        return "blocked"


def coverage() -> dict:
    """Дешёвый срез покрытия: без чтения и автопоиска конфига.

    ``status()`` при пустой записи о режиме зовёт ``config.load_config()``, а
    тот на пустом ``zapret_dir`` обходит диск (замер: 26 076 каталогов, ~2 с).
    ``status.collect`` опрашивается каждые 5 секунд, поэтому здесь только
    реестр и файл состояния.
    """
    saved = _saved_state()
    return {
        "routing_mode": _routing_mode(saved, use_config=False),
        "system_proxy": system_proxy_enabled(),
        "bypass": system_proxy_bypass(),
    }


def _status_server(info: dict | None = None) -> dict:
    """Текущий сервер словарём — единый контракт результата ``start``.

    ``status()`` отдаёт имя строкой (так его ждёт UI панели), но успешный
    ``start`` обязан отдавать объект: потребитель читает
    ``result["server"].get("name")``. Собирается из живого состояния, при его
    отсутствии — из сохранённого.
    """
    info = status() if info is None else info
    live = RUNNING.get("server") or {}
    saved = _saved_state() if info.get("running") else {}
    latency = info.get("latency", live.get("latency", saved.get("latency", -1)))
    return {
        "name": str(info.get("server") or live.get("name") or saved.get("name") or ""),
        "host": str(info.get("host") or live.get("host") or saved.get("host") or ""),
        "latency": latency if isinstance(latency, int) else -1,
        "link": str(live.get("link") or saved.get("link") or ""),
        "extra": list(live.get("extra") or saved.get("extra") or []),
    }


def _blocked_available() -> bool:
    """Есть ли валидный файл встроенного списка рядом с ядром."""
    try:
        from . import blocked

        return blocked._valid(blocked.bin_path())
    except Exception:  # noqa: BLE001
        return False


def _terminate(process) -> None:
    """terminate → wait → kill: следующий start не должен гоняться с прежним.

    Ждём завершения на каждом шаге: ``terminate`` только просит закрыться, а
    живое ядро держит порт и перезаписанный конфиг. Исключения здесь
    разнородны (TimeoutExpired у Popen, NoSuchProcess у psutil, OSError у
    Windows), поэтому ловим «что угодно» — гашение всё равно best-effort.
    """
    try:
        process.terminate()
    except Exception:  # noqa: BLE001
        pass
    try:
        process.wait(timeout=6)
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        process.kill()
        process.wait(timeout=3)
    except Exception:  # noqa: BLE001
        pass


def _kill_process() -> None:
    """Гасит наш Xray, не трогая системный прокси."""
    process = RUNNING.pop("process", None)
    if process is not None and process.poll() is None:
        _terminate(process)
    other = find_process()
    if other is not None:
        _terminate(other)
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
            server = parse_link(link)
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
        info = status()
        try:
            actual_port = int(info.get("port") or DEFAULT_PORT)
        except (TypeError, ValueError):
            actual_port = DEFAULT_PORT
        # Фактический режим потока, а не из конфига: конфиг мог измениться
        # уже после запуска ядра.
        actual_stream = bool(info.get("stream", stream))
        if cfg.get("xray_system_proxy", True):
            # Ядро уже работает, но системный прокси мог остаться выключенным
            # после падения/перезапуска — браузеры не должны ходить мимо.
            try:
                set_system_proxy(True, actual_port, actual_stream)
            except Exception:  # noqa: BLE001
                pass
        # Контракт один для обеих веток: ``server`` — словарь, порт и stream
        # фактические. Строка в ``server`` давала AttributeError в app.set_proxy
        # (``result["server"].get("name")``), и busy залипал навсегда.
        info["server"] = _status_server(info)
        info["port"] = actual_port
        info["stream"] = actual_stream
        return {"ok": True, "already": True, **info}

    exe = ensure_core()
    if exe is None:
        return {"ok": False, "error": "не найдено ядро Xray (xray.exe)"}

    if server is None:
        try:
            links = collect_links(cfg)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"конфиги недоступны: {type(exc).__name__}"}
        server = pick_server(links)
    if server is None:
        return {"ok": False, "error": "нет доступных серверов"}

    port = int(cfg.get("xray_port") or DEFAULT_PORT)
    if _port_open(port):
        # Наш Xray не найден (иначе вернулись бы выше), значит порт держит
        # чужой процесс: ядро не стартует, а ложный ok оставил бы UI уверенным,
        # что VPN поднят. Конфиг при этом не трогаем.
        return {"ok": False, "error": f"порт {port} занят другим процессом"}
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
    mode = config.normalise_routing_mode(cfg.get("routing_mode"))
    config_path = config.config_dir() / "xray-config.json"
    payload = json.dumps(build_config(server, port, extra, stream,
                                      direct_all, user_map, blocked_rule,
                                      mode),
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
                    "stream": bool(stream), "routing_mode": mode,
                    "log": log_handle})
    _save_state(server, port, stream, mode)

    # Ждём, пока порт начнёт слушать.
    deadline = time.time() + 15
    while time.time() < deadline:
        time.sleep(0.3)
        if process.poll() is not None:
            _close_log()
            RUNNING.clear()
            # Прокси мог остаться включённым с прошлого запуска: без снятия
            # после неудачного рестарта браузер ходит в мёртвый 127.0.0.1:port
            # и остаётся без интернета до следующей синхронизации.
            if cfg.get("xray_system_proxy", True):
                set_system_proxy(False, port)
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
    """Оставляет только хвост журнала: не храним историю посещений.

    Читается именно хвост: ``read_bytes()`` тянул в память весь файл, а он
    растёт с каждой ошибкой Xray.
    """
    path = _log_path()
    try:
        size = path.stat().st_size
        if size <= limit:
            return
        with open(path, "r+b") as handle:
            handle.seek(size - limit)
            data = handle.read()
            handle.seek(0)
            handle.write(data)
            handle.truncate()
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
    """Опрашивает свои конфиги и подписку, проверяет серверы и сохраняет список.

    Пустой набор источников даёт ``ValueError`` от ``collect_links`` —
    вызывающий показывает его текст как есть.
    """
    links = collect_links()
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
    server = parse_link(chosen["link"])
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
    """Текущий ProxyServer из HKCU (пусто - нет/не ставили)."""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _INTERNET_SETTINGS) as key:
            return str(winreg.QueryValueEx(key, "ProxyServer")[0] or "")
    except OSError:
        return ""


def system_proxy_bypass() -> list[str]:
    """Фактический ProxyOverride: что системный прокси обходит стороной.

    Читается из реестра, а не пересчитывается: панель показывает реальное
    состояние, даже если список исключений писал другой экземпляр.
    """
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _INTERNET_SETTINGS) as key:
            raw = str(winreg.QueryValueEx(key, "ProxyOverride")[0] or "")
    except OSError:
        return []
    return [part.strip() for part in raw.split(";") if part.strip()]


def _is_ip_like(name: str) -> bool:
    """Адрес или его маска (``127.*``, ``::1``), а не домен."""
    if ":" in name or "/" in name:
        return True
    return bool(re.fullmatch(r"[\d.*]+", name))


def count_bypass_domains(entries: list[str]) -> int:
    """Сколько настоящих доменов исключено из системного прокси.

    Реестр хранит служебные записи (``<local>``, локальные адреса) и каждое
    имя дважды — ``x.com`` и ``*.x.com``. Без свёртки «Исключено: N» выходило
    вдвое больше доменов, чем есть на самом деле.
    """
    names: set[str] = set()
    for raw in entries or []:
        text = str(raw or "").strip().lower()
        if text.startswith("*."):
            text = text[2:]
        text = text.strip(".")
        if not text or text.startswith(("*", "<")) or text == "localhost":
            continue
        if _is_ip_like(text):
            continue
        names.add(text)
    return len(names)



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
    if config.normalise_routing_mode((cfg or {}).get("routing_mode")) == "all":
        # В режиме "all" audit-direct в исключениях системного прокси означал
        # бы прямой ход мимо VPN — та же утечка, что и в правилах Xray.
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
    if not bool(stream_via_proxy) and config.normalise_routing_mode(
            (cfg or {}).get("routing_mode")) != "all":
        # В режиме "all" YouTube/Discord идут через VPN, поэтому в обход
        # системного прокси их не добавляем: иначе браузер ходил бы мимо.
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


def proxy_backup_path() -> Path:
    """Где лежат настройки прокси, снятые перед первым включением."""
    return config.config_dir() / "proxy_backup.json"


def _load_proxy_backup() -> dict | None:
    """Прежние настройки прокси; ``None`` — бэкапа нет или он битый."""
    try:
        data = json.loads(proxy_backup_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _store_proxy_backup(values: dict) -> None:
    config.atomic_write(proxy_backup_path(),
                        json.dumps(values, ensure_ascii=False, indent=2))


def _drop_proxy_backup() -> None:
    try:
        proxy_backup_path().unlink(missing_ok=True)
    except OSError:
        pass


def _read_proxy_values(key) -> dict:
    """Текущие ProxyEnable/ProxyServer/ProxyOverride; ``None`` — значения не было."""
    import winreg

    values: dict = {}
    for name in ("ProxyEnable", "ProxyServer", "ProxyOverride"):
        try:
            values[name] = winreg.QueryValueEx(key, name)[0]
        except OSError:
            values[name] = None
    return values


def _write_proxy_values(key, values: dict) -> None:
    """Возвращает прежние настройки; отсутствовавшие значения удаляются."""
    import winreg

    kinds = {"ProxyEnable": winreg.REG_DWORD, "ProxyServer": winreg.REG_SZ,
             "ProxyOverride": winreg.REG_SZ}
    for name, value in values.items():
        if name not in kinds:
            continue
        if value is None:
            try:
                winreg.DeleteValue(key, name)
            except OSError:
                pass
        else:
            winreg.SetValueEx(key, name, 0, kinds[name], value)


def set_system_proxy(enable: bool, port: int = DEFAULT_PORT,
                     stream_via_proxy: bool = False) -> dict:
    """Включает/выключает системный прокси (только HKCU — права не нужны).

    Перед первой модификацией прежние значения (например, корпоративный
    прокси) кладутся в ``proxy_backup.json`` рядом с конфигом: раньше включение
    затирало ProxyServer/ProxyOverride, а выключение просто удаляло их, и
    настройки пользователя терялись навсегда.
    """
    if not sys.platform.startswith("win"):
        return {"ok": False}
    try:
        import winreg

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _INTERNET_SETTINGS, 0,
                                winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as key:
            if enable:
                own = f"127.0.0.1:{port}"
                try:
                    current, _kind = winreg.QueryValueEx(key, "ProxyServer")
                except OSError:
                    current = ""
                # Бэкап пишем один раз и только для чужих настроек: там
                # исходные значения пользователя. Существующий не
                # перезаписываем — иначе повторный «включить» запомнил бы уже
                # наш прокси и восстанавливать стало бы нечего.
                if (str(current or "").strip() != own
                        and _load_proxy_backup() is None):
                    _store_proxy_backup(_read_proxy_values(key))
                winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
                winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, own)
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
                backup = _load_proxy_backup()
                saved_server = str((backup or {}).get("ProxyServer") or "").strip()
                if backup is not None and saved_server != own:
                    # Пользовательских настроек с тех пор не касались (иначе
                    # ProxyServer был бы другим) — возвращаем их.
                    _write_proxy_values(key, backup)
                    _drop_proxy_backup()
                    _refresh_wininet()
                    return {"ok": True, "enabled": bool(backup.get("ProxyEnable")),
                            "restored": True,
                            "server": str(backup.get("ProxyServer") or "")}
                # Бэкапа нет (или в нём наш же прокси) — просто выключаем.
                _drop_proxy_backup()
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
