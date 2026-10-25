"""Настоящие проверки сервисов: обход, сайты, прокси.

Ничего не запускает и не останавливает — только измеряет. Сетевые пробы идут
параллельно (не больше четырёх потоков), чтобы общий вызов укладывался в
несколько секунд: последовательные таймауты по 5 с дали бы полминуты.

Важно: прямой запрос идёт **мимо** системного прокси (``ProxyHandler({})``),
иначе включённый прокси Xray «чинил» бы все проверки сразу и результат врал.
"""

from __future__ import annotations

import concurrent.futures
import http.client
import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) bypass-tray"
_CONTEXT = ssl.create_default_context()


# ---- сетевые примитивы ----------------------------------------------------


def _http(url: str, proxy_port: int | None = None, timeout: float = 5.0) -> dict:
    """GET-запрос напрямую или через локальный прокси.

    ``ok`` — получен любой HTTP-ответ младше 500: 401/403/404 тоже доказывают,
    что сервер доступен, а не заблокирован.
    """
    started = time.perf_counter()
    status = 0
    error = ""
    try:
        if proxy_port:
            status = _http_via_proxy(url, int(proxy_port), timeout)
        else:
            status = _http_direct(url, timeout)
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, ssl.SSLCertVerificationError) or (
                isinstance(reason, ssl.SSLError)
                and "CERTIFICATE_VERIFY_FAILED" in str(reason)):
            error = "tls"
        else:
            error = type(exc).__name__
    except ssl.SSLCertVerificationError:
        error = "tls"
    except Exception as exc:  # noqa: BLE001
        error = "tls" if isinstance(exc, ssl.SSLError) else type(exc).__name__
    ms = round((time.perf_counter() - started) * 1000)
    return {
        "ok": bool(status) and status < 500,
        "status": int(status or 0),
        "ms": ms,
        "error": error,
    }


def _http_direct(url: str, timeout: float) -> int:
    """Код ответа без системного прокси."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with opener.open(request, timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def _http_via_proxy(url: str, port: int, timeout: float) -> int:
    """Код ответа через локальный HTTP/SOCKS-прокси (CONNECT-туннель)."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    secure = parts.scheme != "http"
    target_port = parts.port or (443 if secure else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    if secure:
        connection = http.client.HTTPSConnection(
            "127.0.0.1", port, timeout=timeout, context=_CONTEXT
        )
        connection.set_tunnel(host, target_port)
    else:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("GET", path, headers={"User-Agent": UA})
        return int(connection.getresponse().status)
    finally:
        connection.close()


def _http_retry(url: str, proxy_port: int | None = None, timeout: float = 5.0) -> dict:
    """GET с одной повторной попыткой.

    Туннель через сервер изредка не отвечает с первого раза (сервер занят
    соседними пробами). Одиночный таймаут — ещё не «недоступно», поэтому
    повторяем один раз; итог считается по последней попытке.
    """
    result = _http(url, proxy_port, timeout)
    if result.get("ok"):
        return result
    time.sleep(0.4)
    retry = _http(url, proxy_port, timeout)
    return retry if retry.get("ok") else result


def _tcp(host: str, port: int, timeout: float = 5.0) -> dict:
    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {
                "ok": True,
                "ms": round((time.perf_counter() - started) * 1000),
                "error": "",
            }
    except OSError as exc:
        return {
            "ok": False,
            "ms": round((time.perf_counter() - started) * 1000),
            "error": type(exc).__name__,
        }


# ---- пункты проверки ------------------------------------------------------


def _item(item_id: str, title: str, state: str, detail: str, ms: int = 0) -> dict:
    return {
        "id": item_id,
        "title": title,
        "state": state,
        "detail": detail,
        "ms": int(ms or 0),
    }


def _why(result: dict) -> str:
    if result.get("error"):
        return f"нет ответа ({result['error']})"
    return f"HTTP {result.get('status') or '?'}"


def _stream_context(cfg: dict, proxy: dict) -> tuple[bool, int | None]:
    """Нужно ли проверять YouTube/Discord через VPN и на каком порту.

    Через VPN они идут в резервном режиме (``proxy["stream"]``) или когда
    пользователь включил ``youtube_via_vpn``. Если режим выбран, а прокси не
    запущен — второй элемент ``None``, проверка вернёт «нужен VPN».
    """
    cfg = cfg or {}
    proxy = proxy or {}
    requested = bool(proxy.get("stream")) or bool(cfg.get("youtube_via_vpn"))
    if not requested:
        return False, None
    if not proxy.get("running"):
        return True, None
    try:
        port = int(proxy.get("port") or cfg.get("xray_port") or 0)
    except (TypeError, ValueError):
        port = 0
    return True, (port or None)


def _check_youtube(cfg: dict, proxy: dict, timeout: float) -> dict:
    proxied, port = _stream_context(cfg, proxy)
    if proxied:
        if port is None:
            return _item("youtube", "YouTube", "off", "нужен VPN", 0)
        page = _http_retry("https://www.youtube.com/", port, timeout)
        cdn = _http_retry("https://i.ytimg.com/generate_204", port, timeout)
        if page["ok"] and cdn["ok"]:
            return _item("youtube", "YouTube", "ok",
                         f"через VPN · {cdn['ms'] / 1000:.1f} с", cdn["ms"])
        if page["ok"]:
            return _item("youtube", "YouTube", "warn",
                         "через VPN: страница есть, видео нет", cdn["ms"])
        return _item("youtube", "YouTube", "bad", "через VPN: " + _why(page), page["ms"])
    page = _http("https://www.youtube.com/", None, timeout)
    cdn = _http("https://i.ytimg.com/generate_204", None, timeout)
    if page["ok"] and cdn["ok"]:
        return _item("youtube", "YouTube", "ok",
                     f"страница и видео · {cdn['ms'] / 1000:.1f} с", cdn["ms"])
    if page["ok"]:
        return _item("youtube", "YouTube", "warn", "страница есть, видео нет", cdn["ms"])
    return _item("youtube", "YouTube", "bad", _why(page), page["ms"])


def _check_discord(cfg: dict, proxy: dict, timeout: float) -> dict:
    proxied, port = _stream_context(cfg, proxy)
    if proxied:
        if port is None:
            return _item("discord", "Discord", "off", "нужен VPN", 0)
        page = _http_retry("https://discord.com/api/v9/gateway", port, timeout)
        gateway = _http_retry("https://gateway.discord.gg/?v=10&encoding=json",
                              port, timeout)
        if page["ok"] and gateway["ok"]:
            return _item("discord", "Discord", "ok",
                         f"через VPN · {gateway['ms'] / 1000:.1f} с", gateway["ms"])
        if page["ok"]:
            return _item("discord", "Discord", "warn",
                         "через VPN: страница есть, шлюз закрыт", gateway["ms"])
        return _item("discord", "Discord", "bad", "через VPN: " + _why(page), page["ms"])
    page = _http("https://discord.com/api/v9/gateway", None, timeout)
    gateway = _tcp("gateway.discord.gg", 443, timeout)
    if page["ok"] and gateway["ok"]:
        return _item("discord", "Discord", "ok",
                     f"страница и шлюз · {gateway['ms'] / 1000:.1f} с", gateway["ms"])
    if page["ok"]:
        return _item("discord", "Discord", "warn", "страница есть, шлюз закрыт",
                     gateway["ms"])
    return _item("discord", "Discord", "bad", _why(page), page["ms"])


def _check_deepseek(proxy_port: int | None, timeout: float) -> dict:
    direct = _http("https://api.deepseek.com/", None, timeout)
    if direct["ok"]:
        return _item("deepseek", "DeepSeek", "ok",
                     f"напрямую · {direct['ms'] / 1000:.1f} с", direct["ms"])
    if proxy_port:
        via = _http_retry("https://api.deepseek.com/", proxy_port, timeout)
        if via["ok"]:
            return _item("deepseek", "DeepSeek", "ok",
                         f"через сервер · {via['ms'] / 1000:.1f} с", via["ms"])
        return _item("deepseek", "DeepSeek", "bad", f"через сервер: {_why(via)}", via["ms"])
    return _item("deepseek", "DeepSeek", "bad", _why(direct), direct["ms"])


def _check_telegram(proxy_port: int | None, timeout: float) -> dict:
    if not proxy_port:
        return _item("telegram", "Telegram", "off", "нужен VPN", 0)
    result = _http_retry("https://api.telegram.org/", proxy_port, timeout)
    if result["ok"]:
        return _item("telegram", "Telegram", "ok",
                     f"Bot API через прокси · {result['ms'] / 1000:.1f} с", result["ms"])
    return _item("telegram", "Telegram", "bad", f"Bot API недоступен: {_why(result)}",
                 result["ms"])


def _check_zapret(status_state: dict) -> dict:
    zapret = (status_state or {}).get("zapret") or {}
    if zapret.get("running"):
        detail = str(zapret.get("strategy") or "стратегия не определена")
        return _item("zapret", "zapret", "ok", detail, 0)
    # Папка найдена, но winws не запущен — это «остановлен», а не «не найден».
    detail = "остановлен" if zapret.get("dir") else "winws.exe не найден"
    return _item("zapret", "zapret", "bad", detail, 0)


def _check_tgws(status_state: dict, cfg: dict) -> dict:
    info = (status_state or {}).get("tgwsproxy") or {}
    try:
        port = int(info.get("port") or cfg.get("tgws_port") or 1443)
    except (TypeError, ValueError):
        port = 1443
    exe = str(cfg.get("tgws_exe") or "")
    if not exe or not Path(exe).is_file():
        # Не установлен — это не поломка обхода, а просто выключенный компонент.
        return _item("tgwsproxy", "tg-ws-proxy", "off", "не установлен", 0)
    if info.get("listening"):
        return _item("tgwsproxy", "tg-ws-proxy", "ok", f"порт {port} слушает", 0)
    return _item("tgwsproxy", "tg-ws-proxy", "bad", f"порт {port} закрыт", 0)


def _check_vpn(proxy: dict) -> dict:
    """VPN проверяется настоящим запросом через туннель, а не портом.

    Открытый порт ничего не говорит: сервер может быть мёртв. ``verify`` идёт
    на заблокированный сайт (Telegram стабильнее Instagram), то есть
    обязательно через сервер.
    """
    if not (proxy or {}).get("running"):
        return _item("vpn", "VPN", "off", "не подключён", 0)
    result: dict = {"ok": False}
    for attempt in range(2):
        try:
            from . import xray as xray_module

            result = xray_module.verify(target="api.telegram.org", timeout=10)
        except Exception:  # noqa: BLE001
            result = {"ok": False}
        if result.get("ok"):
            break
        if attempt == 0:
            # Один провал ещё не «сервер мёртв»: целевой сайт мог ответить
            # медленно. Повторяем, как и остальные прокси-пробы.
            time.sleep(0.5)
    ms = int(result.get("ms") or 0)
    if result.get("ok"):
        seconds = max(0, ms) / 1000.0
        return _item("vpn", "VPN", "ok", f"через VPN · {seconds:.1f} с", ms)
    return _item("vpn", "VPN", "bad", "сервер не отвечает", ms)


# ---- сводка ---------------------------------------------------------------


def verdict(items: list[dict]) -> str:
    """Итог по пунктам: fail — есть bad, warn — есть warn, иначе ok."""
    states = {str((item or {}).get("state") or "") for item in items}
    if "bad" in states:
        return "fail"
    if "warn" in states:
        return "warn"
    return "ok"


def _proxy_status() -> dict:
    try:
        from . import xray

        return xray.status()
    except Exception:  # noqa: BLE001
        return {"running": False, "port": 0}


def _save_cache(result: dict) -> None:
    config.atomic_write(
        config.config_dir() / "health.json",
        json.dumps(result, ensure_ascii=False, indent=2),
    )


def collect(cfg: dict, status_state: dict | None = None, proxy: dict | None = None,
            timeout: float = 5.0) -> dict:
    """Проверяет все пункты и возвращает сводку с вердиктом.

    ``status_state`` — результат ``status.collect``; если не передан, состояние
    собирается здесь. ``proxy`` — словарь ``xray.status``.
    """
    if status_state is None:
        try:
            from . import status as status_module

            status_state = status_module.collect(cfg)
        except Exception:  # noqa: BLE001
            status_state = {}
    if proxy is None:
        proxy = _proxy_status()

    try:
        proxy_running = bool((proxy or {}).get("running"))
        port = int((proxy or {}).get("port") or cfg.get("xray_port") or 0)
    except (TypeError, ValueError):
        proxy_running, port = False, 0
    proxy_port = port if proxy_running else None

    tasks = [
        ("youtube", lambda: _check_youtube(cfg, proxy or {}, timeout)),
        ("discord", lambda: _check_discord(cfg, proxy or {}, timeout)),
        ("deepseek", lambda: _check_deepseek(proxy_port, timeout)),
        ("telegram", lambda: _check_telegram(proxy_port, timeout)),
        ("vpn", lambda: _check_vpn(proxy or {})),
    ]
    network_items: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(task): key for key, task in tasks}
        for future in concurrent.futures.as_completed(futures):
            key = futures[future]
            try:
                network_items[key] = future.result()
            except Exception as exc:  # noqa: BLE001
                network_items[key] = _item(
                    key, key, "bad", f"проверка не удалась: {type(exc).__name__}", 0
                )

    items = [
        _check_zapret(status_state),
        network_items.get("youtube") or _item("youtube", "YouTube", "bad", "нет данных", 0),
        network_items.get("discord") or _item("discord", "Discord", "bad", "нет данных", 0),
        network_items.get("deepseek") or _item("deepseek", "DeepSeek", "bad", "нет данных", 0),
        network_items.get("telegram") or _item("telegram", "Telegram", "off", "нет данных", 0),
        _check_tgws(status_state, cfg),
        network_items.get("vpn") or _item("vpn", "VPN", "warn", "нет данных", 0),
    ]
    result = {"checked": time.time(), "items": items, "verdict": verdict(items)}
    _save_cache(result)
    return result
