"""Собственный клиент Xray: подписка, выбор живого сервера, локальный прокси.

Happ не нужен: используется открытый ядро Xray (то же, что внутри Happ) и своя
подписка. Работает в режиме локального прокси — **без прав администратора**,
без TUN-адаптера и без конфликта с zapret (тот продолжает обрабатывать трафик
как обычно).

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
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from . import actions, config, runtime

#: Ссылку на подписку каждый указывает свою в config.json — в репозитории её
#: нет намеренно: это персональный ключ доступа.
DEFAULT_SUBSCRIPTION = ""
DEFAULT_PORT = 10818

#: Файлы ядра, которые копируем к себе, чтобы не зависеть от Happ.
CORE_FILES = ("xray.exe", "geoip.dat", "geosite.dat")
#: Откуда берём ядро, если своего ещё нет.
CORE_SOURCES = (
    Path(r"C:\Program Files\FlyFrogLLC\Happ\core"),
    Path(r"C:\Program Files (x86)\FlyFrogLLC\Happ\core"),
)
#: Если Happ не установлен — скачиваем официальное ядро Xray с GitHub.
CORE_REPO = "XTLS/Xray-core"
CORE_ASSET = "Xray-windows-64.zip"

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

RUNNING: dict = {}
_LOG_LIMIT = 4000


# ---- расположение ядра ----------------------------------------------------


def bin_dir() -> Path:
    """Постоянная папка ядра Xray в профиле пользователя.

    Раньше ядро лежало рядом с исходниками (`<проект>\\bin`), но собранный
    ``.exe`` распаковывается во временную папку, которая исчезает после выхода,
    поэтому ядро храним в ``%LOCALAPPDATA%\\bypass-tray\\bin``.
    """
    return runtime.data_dir() / "bin"


def xray_path() -> Path:
    configured = (config.load_config().get("xray_exe") or "")
    if configured and Path(configured).is_file():
        return Path(configured)
    return bin_dir() / "xray.exe"


def _download_core(target: Path, progress=None) -> bool:
    """Скачивает официальное ядро Xray с GitHub (если Happ не установлен)."""
    from . import components

    asset = components.latest_asset(CORE_REPO, suffixes=(".zip",), exact=CORE_ASSET)

    def report(percent: int, done: int, total: int) -> None:
        if progress is not None:
            progress(f"Xray: {done} из {total} МБ", percent)

    archive = target / asset["name"]
    components.download(asset["url"], archive, report)
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(target)
    archive.unlink(missing_ok=True)
    components._flatten(target)
    return (target / "xray.exe").is_file()


def ensure_core(progress=None) -> Path | None:
    """Готовит локальную копию ядра Xray (и гео-файлов).

    Порядок: уже скачанное → ядро из Happ → официальный релиз XTLS/Xray-core.
    Последний вариант делает установку самодостаточной: Happ не нужен.
    """
    target = bin_dir()
    target.mkdir(parents=True, exist_ok=True)
    missing = [name for name in CORE_FILES if not (target / name).is_file()]
    if missing:
        source = next((p for p in CORE_SOURCES if (p / "xray.exe").is_file()), None)
        if source is not None:
            for name in missing:
                src = source / name
                if src.is_file():
                    try:
                        shutil.copy2(src, target / name)
                    except OSError:
                        pass
        still_missing = [name for name in CORE_FILES if not (target / name).is_file()]
        if still_missing:
            try:
                _download_core(target, progress)
            except Exception as exc:  # noqa: BLE001
                if progress is not None:
                    progress(f"ядро Xray: {type(exc).__name__}", 0)
    candidate = target / "xray.exe"
    return candidate if candidate.is_file() else None


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


def build_config(server: dict, port: int, extra_domains: list[str] | None = None,
                 stream_via_proxy: bool = False) -> dict:
    """Конфиг Xray: локальный прокси + маршрутизация.

    Маршрут по умолчанию — **напрямую** (``direct`` стоит первым в outbounds),
    поэтому через сервер идёт только явно перечисленное. Российские сервисы
    всегда напрямую. YouTube и Discord добавляются в прокси только в резервном
    режиме ``stream_via_proxy`` — когда zapret не работает.
    """
    proxy_domains = list(PROXY_DOMAINS)
    if stream_via_proxy:
        proxy_domains += [d for d in STREAM_DOMAINS if d not in proxy_domains]
    proxy_domains += [d for d in (extra_domains or []) if d and d not in proxy_domains]
    return {
        "log": {"loglevel": "warning"},
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
            "rules": [
                # Российские сервисы и локальная сеть — всегда напрямую.
                {"type": "field", "outboundTag": "direct",
                 "domain": ["geosite:category-ru", "geosite:private"]},
                {"type": "field", "outboundTag": "direct",
                 "ip": ["geoip:ru", "geoip:private"]},
                # Заблокированное — через сервер.
                {"type": "field", "outboundTag": "proxy", "domain": proxy_domains},
                # Остальное — напрямую (YouTube и Discord закрывает zapret).
            ],
        },
    }


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
    try:
        state_path().write_text(json.dumps({
            "name": server.get("name", ""),
            "host": server.get("host", ""),
            "latency": server.get("latency", -1),
            "port": port,
            "since": time.time(),
            "stream": bool(stream),
            "link": server.get("link", ""),
            "extra": server.get("extra", []),
        }, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _saved_state() -> dict:
    try:
        return json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


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
    }


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


def restart_with_stream(enable: bool, cfg: dict | None = None) -> dict:
    """Переключает режим YouTube/Discord, перезапуская Xray на том же сервере.

    ``enable=True`` — zapret не работает, поэтому YouTube и Discord уходят
    через сервер. ``False`` — снова напрямую под zapret.
    """
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
          server: dict | None = None, stream_via_proxy: bool = False) -> dict:
    """Поднимает Xray на выбранном сервере и включает системный прокси."""
    cfg = cfg or config.load_config()
    if is_running():
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
    extra = extra_domains if extra_domains is not None else cfg.get("xray_extra_domains")
    work = config.config_dir()
    config_path = work / "xray-config.json"
    try:
        config_path.write_text(
            json.dumps(build_config(server, port, extra, stream_via_proxy),
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        return {"ok": False, "error": f"не записать конфиг: {exc}"}

    env = dict(os.environ, XRAY_LOCATION_ASSET=str(exe.parent))
    try:
        process = subprocess.Popen(
            [str(exe), "-c", str(config_path)],
            cwd=str(exe.parent), env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=actions.CREATE_NO_WINDOW,
        )
    except OSError as exc:
        return {"ok": False, "error": f"не запустить Xray: {exc}"}

    RUNNING.update({"process": process, "server": server, "port": port,
                    "since": time.time(), "config": str(config_path),
                    "stream": bool(stream_via_proxy)})
    _save_state(server, port, stream_via_proxy)

    # Ждём, пока порт начнёт слушать.
    deadline = time.time() + 15
    while time.time() < deadline:
        time.sleep(0.3)
        if process.poll() is not None:
            return {"ok": False, "error": "Xray завершился при запуске",
                    "log": _read_log()}
        if _port_open(port):
            if cfg.get("xray_system_proxy", True):
                set_system_proxy(True, port)
            return {"ok": True, "server": server, "port": port,
                    "latency": server.get("latency", -1)}

    stop()
    return {"ok": False, "error": "порт прокси не открылся", "log": _read_log()}


def stop(restore_proxy: bool = True) -> dict:
    """Останавливает Xray и снимает системный прокси."""
    _kill_process()
    if restore_proxy:
        set_system_proxy(False)
    RUNNING.clear()
    return {"ok": True}


def restart(cfg: dict | None = None, server: dict | None = None) -> dict:
    stop()
    time.sleep(0.5)
    return start(cfg, server=server)


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _read_log() -> str:
    process = RUNNING.get("process")
    if process is None or process.stdout is None:
        return ""
    try:
        import select  # noqa: F401

        return ""
    except Exception:  # noqa: BLE001
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
    try:
        servers_path().write_text(
            json.dumps({"at": time.time(), "servers": rows}, ensure_ascii=False, indent=2),
            encoding="utf-8")
    except OSError:
        pass
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
    cfg = cfg or config.load_config()
    saved = _saved_state()
    server = dict(server)
    server["extra"] = saved.get("extra") or []
    stream = bool(saved.get("stream", False))
    if is_running():
        _kill_process()
        time.sleep(0.6)
    return start(cfg, extra_domains=server["extra"], server=server,
                 stream_via_proxy=stream)


def switch_server(exclude: set[str] | None = None, cfg: dict | None = None) -> dict:
    """Подбирает другой сервер (кроме тех, что уже подвели) и переключается."""
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


def set_system_proxy(enable: bool, port: int = DEFAULT_PORT) -> dict:
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
                    "localhost;127.*;10.*;172.16.*;192.168.*;<local>",
                )
            else:
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
