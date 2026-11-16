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
import re
import socket
import ssl
import time
import zlib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) bypass-tray"
_CONTEXT = ssl.create_default_context()

#: Сколько тела ответа читать при проверке ``contains``: страница целиком не
#: нужна, а гигабайтная загрузка съела бы и время, и память.
MAX_BODY = 64 * 1024

#: Реальная видео-проба: watch-страница отдаёт playabilityStatus и ссылку на
#: ``*.googlevideo.com``, затем качается начало файла. Это ловит случай
#: «страница открылась, видео мёртвое», который прежняя проверка картинок
#: не видела. Innertube не используется: у него устаревший WEB-клиент.
YOUTUBE_VIDEO_ID = "jNQXAC9IVRw"
YOUTUBE_WATCH_URL = "https://www.youtube.com/watch?v={video_id}&hl=en"
#: Страница watch ~1.3 МБ; 2 МБ хватает с запасом, но не тянет гигабайты.
WATCH_BODY_LIMIT = 2 * 1024 * 1024
#: Современный YouTube отдаёт googlevideo 4xx без PO-token — это подпись CDN,
#: а не блокировка. Такие коды не считаем сбоем.
SIGNATURE_ERRORS = (400, 401, 403, 410)
#: Порог мягче Linux-виджета (100 КиБ): 64 КиБ за бюджет ~1.5×timeout уже
#: доказывают, что VIDEO-поток идёт, а не отдаётся заглушка.
VIDEO_MIN_BYTES = 64 * 1024
VIDEO_READ_LIMIT = 256 * 1024
#: Браузерный UA: без него watch-страница отдаёт упрощённый/консентный HTML.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_JS_ESCAPES = (
    ("\\u0026", "&"), ("\\u002F", "/"), ("\\u002f", "/"), ("\\/", "/"),
    ("\\u003D", "="), ("\\u003d", "="), ("\\u003F", "?"), ("\\u003f", "?"),
    ("\\u0025", "%"), ("&amp;", "&"),
)


# ---- сетевые примитивы ----------------------------------------------------


def _error_name(exc: BaseException) -> str:
    """Короткое имя ошибки для ``detail``: ``tls`` или класс исключения."""
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", None)
        if isinstance(reason, ssl.SSLCertVerificationError) or (
                isinstance(reason, ssl.SSLError)
                and "CERTIFICATE_VERIFY_FAILED" in str(reason)):
            return "tls"
        return type(exc).__name__
    if isinstance(exc, ssl.SSLError):
        return "tls"
    return type(exc).__name__


def _ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


def _read_body(response) -> str:
    """Тело ответа, не больше ``MAX_BODY``; ошибка чтения — пустая строка."""
    try:
        raw = response.read(MAX_BODY)
    except (OSError, http.client.HTTPException, ValueError):
        return ""
    if isinstance(raw, str):
        return raw
    return raw.decode("utf-8", "replace")


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
    except Exception as exc:  # noqa: BLE001
        error = _error_name(exc)
    ms = _ms(started)
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


def _fetch_direct(url: str, timeout: float) -> dict:
    """GET без системного прокси, с телом ответа (до ``MAX_BODY``)."""
    started = time.perf_counter()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with opener.open(request, timeout=timeout) as response:
            return {"status": int(response.status), "body": _read_body(response),
                    "ms": _ms(started), "error": ""}
    except urllib.error.HTTPError as exc:
        # Тело ответа 4xx/5xx тоже проверяется на подстроку: сайт мог ответить
        # ошибкой, но с ожидаемым текстом — это по-прежнему ответ.
        return {"status": int(exc.code), "body": _read_body(exc),
                "ms": _ms(started), "error": ""}
    except Exception as exc:  # noqa: BLE001
        return {"status": 0, "body": "", "ms": _ms(started),
                "error": _error_name(exc)}


def _fetch_via_proxy(url: str, port: int, timeout: float) -> dict:
    """GET через локальный прокси (CONNECT-туннель), с телом ответа."""
    started = time.perf_counter()
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
        response = connection.getresponse()
        return {"status": int(response.status), "body": _read_body(response),
                "ms": _ms(started), "error": ""}
    except Exception as exc:  # noqa: BLE001
        return {"status": 0, "body": "", "ms": _ms(started),
                "error": _error_name(exc)}
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


def _left(deadline: float | None) -> float:
    """Сколько секунд осталось до общего дедлайна (``inf`` — предела нет)."""
    if deadline is None:
        return float("inf")
    return deadline - time.monotonic()


def _run_probe(item_id: str, probe, timeout: float,
               deadline: float | None) -> dict:
    """Проба с таймаутом из общего бюджета сбора.

    Без общего предела одна медленная проба (VPN проверяется двумя запросами
    по 10 с) держала бы весь ``collect`` и индикатор «занято».
    """
    left = _left(deadline)
    if left <= 0.0:
        return _item(item_id, item_id, "warn",
                     "проверка не успела за отведённое время", 0)
    return probe(max(0.5, min(float(timeout), left)))


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


def _proxied_groups(cfg: dict, proxy: dict) -> tuple[bool, bool]:
    """Фактический маршрут YouTube и Discord: (YouTube?, Discord?) через VPN.

    Флаги берутся из статуса ядра (``youtube_proxy``/``discord_proxy``).
    Старая запись состояния знает только общий ``stream``: для Discord он
    означает резервный режим, а не включённый пользователем YouTube.
    """
    cfg = cfg or {}
    proxy = proxy or {}
    mode = str(proxy.get("routing_mode") or cfg.get("routing_mode") or "")
    if config.normalise_routing_mode(mode) == "all":
        return True, True
    stream = bool(proxy.get("stream"))
    fallback = stream and not bool(cfg.get("youtube_via_vpn"))
    youtube = (bool(proxy.get("youtube_proxy", stream))
               or bool(cfg.get("youtube_via_vpn")))
    discord = bool(proxy.get("discord_proxy", fallback))
    return youtube, discord


def _stream_context(cfg: dict, proxy: dict,
                    service: str = "") -> tuple[bool, int | None]:
    """Нужно ли проверять YouTube/Discord через VPN и на каком порту.

    Через VPN группа идёт только по своему флагу: YouTube — по потоку
    (резервный режим или ``youtube_via_vpn``), Discord — по
    ``discord_via_proxy``/резервному режиму. В режиме ``all`` через VPN идёт
    всё, кроме РФ, — значит, и обе группы.
    Если режим выбран, а прокси не запущен — второй элемент ``None``, проверка
    вернёт «нужен VPN».

    ``service`` учитывает любые proxy-оверрайды сервиса (``zapret_off``,
    ``escalation``, ручные): если домен уже ведётся через VPN, health должен
    проверять его через прокси, а не показывать прямой сбой.
    """
    cfg = cfg or {}
    proxy = proxy or {}
    youtube, discord = _proxied_groups(cfg, proxy)
    if service == "youtube":
        requested = youtube
    elif service == "discord":
        requested = discord
    else:
        requested = youtube or discord
    if not requested and service:
        try:
            from . import routes

            overrides = routes.load_overrides()
            domains = routes.SERVICE_DOMAINS.get(str(service), ())
            requested = any(
                str((overrides.get(domain) or {}).get("route") or "") == "proxy"
                for domain in domains
            )
        except Exception:  # noqa: BLE001
            requested = False
    if not requested:
        return False, None
    if not proxy.get("running"):
        return True, None
    try:
        port = int(proxy.get("port") or cfg.get("xray_port") or 0)
    except (TypeError, ValueError):
        port = 0
    return True, (port or None)


def _decode_js_url(value: str) -> str:
    """Раскодирует JS/JSON-экранирование в ссылке YouTube."""
    text = str(value or "")
    for source, target in _JS_ESCAPES:
        text = text.replace(source, target)
    return text


def _gunzip(raw: bytes, limit: int) -> bytes:
    """Распаковка gzip с жёстким пределом распакованного тела."""
    try:
        # decompressobj с wbits=31 (gzip) и max_length не даёт распакуются
        # бомбе больше ``limit``; неполный хвост просто игнорируется.
        return zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw, limit)
    except zlib.error:
        return b""


def _content_encoding(headers) -> str:
    try:
        return str(headers.get("Content-Encoding") or "").lower()
    except AttributeError:
        return ""


def _fetch_page(url: str, proxy_port: int | None, timeout: float,
                limit: int, budget: float | None = None) -> dict:
    """GET страницы с браузерным UA, gzip и ограничением тела/бюджета."""
    started = time.perf_counter()
    expire = started + (float(budget) if budget is not None
                        else max(float(timeout) * 1.5, 3.0))
    if proxy_port:
        proxy = f"http://127.0.0.1:{int(proxy_port)}"
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, headers={
        "User-Agent": BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip",
    })
    try:
        with opener.open(request, timeout=timeout) as response:
            status = int(response.status)
            # Сжатые байты тоже капнуты ``limit``: распаковка ограничена им же.
            encoding = _content_encoding(getattr(response, "headers", {}))
            # urllib-ответ не всегда отдаёт read1; read(n) дождётся n байт
            # или EOF, но для страницы это допустимо.
            reader = getattr(response, "read1", None) or response.read
            chunks: list[bytes] = []
            size = 0
            truncated = False
            while size < limit:
                left = expire - time.perf_counter()
                if left <= 0:
                    truncated = True
                    break
                try:
                    chunk = reader(min(65536, limit - size))
                except TimeoutError:
                    truncated = True
                    break
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            raw = b"".join(chunks)
            if "gzip" in encoding:
                raw = _gunzip(raw, limit)
            return {"status": status, "body": raw.decode("utf-8", "replace"),
                    "ms": _ms(started), "truncated": truncated, "error": ""}
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(limit)
            if "gzip" in _content_encoding(getattr(exc, "headers", {})):
                raw = _gunzip(raw, limit)
            body = raw.decode("utf-8", "replace")
        except (OSError, ValueError):
            body = ""
        return {"status": int(exc.code), "body": body, "ms": _ms(started),
                "truncated": False, "error": f"HTTP {exc.code}"}
    except Exception as exc:  # noqa: BLE001
        return {"status": 0, "body": "", "ms": _ms(started),
                "truncated": False, "error": _error_name(exc)}


def _playability(body: str) -> tuple[str, str]:
    """``playabilityStatus.status`` и ``reason`` из HTML watch-страницы."""
    index = body.find('"playabilityStatus"')
    if index < 0:
        return "", ""
    chunk = body[index:index + 5000]
    status_match = re.search(r'"status"\s*:\s*"([A-Z_]+)"', chunk)
    reason_match = re.search(r'"reason"\s*:\s*"((?:[^"\\]|\\.)*)"', chunk)
    status = status_match.group(1) if status_match else ""
    reason = _decode_js_url(reason_match.group(1)) if reason_match else ""
    return status, reason


def _video_url(body: str) -> str:
    """Первая ссылка ``googlevideo.com/videoplayback`` из HTML страницы."""
    match = re.search(
        r'https:[^"\s]+?googlevideo\.com[^"\s]+?videoplayback[^"\s]*', body)
    if not match:
        return ""
    return _decode_js_url(match.group(0))


def _fetch_range(url: str, proxy_port: int | None, timeout: float,
                 budget: float | None = None) -> dict:
    """Range-GET к googlevideo; возвращает диагностику, а не вердикт.

    Бюджет общий на весь запрос (как у ``strategy.probe_url``): при троттлинге
    сокет-таймаут на каждом байте не спасает, а проверка не должна держать
    весь ``collect``.
    """
    started = time.perf_counter()
    expire = started + (float(budget) if budget is not None
                        else max(float(timeout) * 1.5, 3.0))
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    secure = parts.scheme != "http"
    port = parts.port or (443 if secure else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    connection = None
    data = 0
    status = 0
    expired = False
    try:
        if proxy_port:
            if secure:
                connection = http.client.HTTPSConnection(
                    "127.0.0.1", int(proxy_port), timeout=timeout,
                    context=_CONTEXT)
                connection.set_tunnel(host, port)
            else:
                connection = http.client.HTTPConnection(
                    "127.0.0.1", int(proxy_port), timeout=timeout)
        elif secure:
            connection = http.client.HTTPSConnection(
                host, port, timeout=timeout, context=_CONTEXT)
        else:
            connection = http.client.HTTPConnection(host, port, timeout=timeout)
        connection.request("GET", path, headers={
            "User-Agent": BROWSER_UA,
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.youtube.com/",
            "Range": f"bytes=0-{VIDEO_READ_LIMIT - 1}",
        })
        response = connection.getresponse()
        status = int(response.status)
        while data < VIDEO_MIN_BYTES:
            left = expire - time.perf_counter()
            if left <= 0:
                expired = True
                break
            sock = getattr(connection, "sock", None)
            if sock is not None:
                try:
                    sock.settimeout(max(0.05, min(left, timeout)))
                except OSError:
                    pass
            try:
                chunk = response.read1(min(16384, VIDEO_MIN_BYTES - data))
            except TimeoutError:
                expired = True
                break
            if not chunk:
                break
            data += len(chunk)
        return {"status": status, "bytes": data, "ms": _ms(started),
                "expired": expired,
                "error": "timeout" if expired else ""}
    except Exception as exc:  # noqa: BLE001
        return {"status": status, "bytes": data, "ms": _ms(started),
                "expired": False, "error": _error_name(exc)}
    finally:
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass


def _youtube_video(proxy_port: int | None, timeout: float,
                   budget: float | None = None) -> dict:
    """Проверяет видео-путь: watch-страница, playability и googlevideo.

    Возвращает диагностику с ``ok``/``checked``:
    * ``stream=True`` — поток реально качается (``bytes`` — сколько пришло);
    * ``ok=True, stream=False, signature=True`` — googlevideo ответил 4xx
      (нет PO-token): это штатно, страница и плеер работают;
    * ``ok=True, stream=None`` — playability OK, ссылки на поток нет;
    * ``ok=False, checked=True`` — страница/поток недоступны;
    * ``checked=False`` — не удалось извлечь ни playability, ни поток.
    """
    page_url = YOUTUBE_WATCH_URL.format(video_id=YOUTUBE_VIDEO_ID)
    page = _fetch_page(page_url, proxy_port, timeout, WATCH_BODY_LIMIT, budget)
    if int(page.get("status") or 0) == 0:
        # Один повтор, как у лёгкой проверки: обрыв/tail-таймаут не приговор.
        page = _fetch_page(page_url, proxy_port, timeout, WATCH_BODY_LIMIT, budget)
    status = int(page.get("status") or 0)
    if not 200 <= status < 300:
        if 400 <= status < 500:
            # 404/410/429/451 — не повод уводить YouTube в VPN и рвать Xray:
            # это отказ конкретной страницы, а не блокировка обхода.
            detail = f"страница недоступна ({status})"
            return {"ok": False, "checked": True, "warn": True,
                    "page": False, "status": status, "reason": detail,
                    "ms": page["ms"], "error": detail}
        return {"ok": False, "checked": True, "page": False,
                "status": status, "ms": page["ms"],
                "error": page["error"] or f"HTTP {status}"}
    body = page["body"]
    playability, reason = _playability(body)
    if playability in ("ERROR", "UNPLAYABLE") and reason:
        return {"ok": False, "checked": True, "page": True,
                "playability": playability, "reason": reason,
                "ms": page["ms"], "error": reason}
    url = _video_url(body)
    if url:
        stream = _fetch_range(url, proxy_port, timeout, budget)
        status = int(stream.get("status") or 0)
        data = int(stream.get("bytes") or 0)
        seconds = max(stream.get("ms", 0) / 1000.0, 1e-6)
        if (status in (200, 206) and not stream.get("expired")
                and data >= VIDEO_MIN_BYTES):
            return {"ok": True, "checked": True, "page": True, "stream": True,
                    "playability": playability, "status": status,
                    "bytes": data, "kbps": round(data * 8 / 1000 / seconds),
                    "ms": stream["ms"]}
        if status in SIGNATURE_ERRORS:
            # 4xx без PO-token — не блокировка: плеер и страница живы.
            return {"ok": True, "checked": True, "page": True, "stream": False,
                    "signature": True, "playability": playability,
                    "status": status, "bytes": data, "ms": stream["ms"]}
        return {"ok": False, "checked": True, "page": True, "stream": False,
                "playability": playability, "status": status,
                "bytes": data, "ms": stream["ms"],
                "error": stream.get("error") or f"HTTP {status}"}
    if playability in ("OK", "PLAYING"):
        # Плеер жив, но ссылку из HTML не достали — это не сбой.
        return {"ok": True, "checked": True, "page": True, "stream": None,
                "playability": playability, "bytes": 0, "ms": page["ms"]}
    return {"ok": False, "checked": False, "page": True,
            "playability": playability, "bytes": 0, "ms": page["ms"],
            "error": "playability и поток не найдены"}


def probe_youtube_video(timeout: float = 6.0,
                        proxy_port: int | None = None,
                        budget: float | None = None) -> dict:
    """Общая видео-проба YouTube для health и перебора стратегий.

    Перебирающему нужен реальный проход байтов, а не открытая страница:
    ``stream=True`` и ``bytes`` — единственный признак, что googlevideo
    отдаёт видео. ``budget`` ограничивает каждый запрос по времени.
    """
    return _youtube_video(proxy_port, timeout, budget)


def _check_youtube(cfg: dict, proxy: dict, timeout: float) -> dict:
    proxied, port = _stream_context(cfg, proxy, "youtube")
    if proxied and port is None:
        return _item("youtube", "YouTube", "off", "нужен VPN", 0)
    where = "через VPN: " if proxied else ""
    page = _http_retry("https://www.youtube.com/", port if proxied else None,
                       timeout)
    if not page["ok"]:
        return _item("youtube", "YouTube", "bad", where + _why(page), page["ms"])
    # Страница открылась — проверяем то, ради чего она нужна: реальный поток.
    video = _youtube_video(port if proxied else None, timeout)
    if video.get("warn"):
        # 4xx watch-страницы: не деградируем и не запускаем fallback.
        return _item("youtube", "YouTube", "warn",
                     where + str(video.get("reason")
                                 or f"страница недоступна ({video.get('status')})"),
                     video.get("ms", 0))
    if video.get("ok"):
        prefix = "через VPN · " if proxied else ""
        if video.get("stream"):
            detail = f"{prefix}страница и видео"
        else:
            # playability ок или googlevideo 4xx без PO-token: это штатно,
            # не повод красить панель в degraded.
            detail = f"{prefix}страница и плеер ок"
        return _item("youtube", "YouTube", "ok",
                     f"{detail} · {video['ms'] / 1000:.1f} с", video["ms"])
    if video.get("checked"):
        # Реально не удалось ни открыть watch-страницу, ни получить поток.
        return _item("youtube", "YouTube", "bad",
                     where + "страница есть, видео не идёт", video["ms"])
    # Ни playability, ни ссылки на поток: ничего не проверили.
    return _item("youtube", "YouTube", "warn",
                 where + "страница есть, видео не проверено", video.get("ms", 0))


def _check_discord(cfg: dict, proxy: dict, timeout: float) -> dict:
    proxied, port = _stream_context(cfg, proxy, "discord")
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


def _check_zapret(status_state: dict, cfg: dict | None = None,
                  proxy: dict | None = None, user_off: bool = False) -> dict:
    """Состояние zapret. Без работающего zapret при VPN-резерве — warn.

    В резервном режиме и в режиме ``all`` YouTube/Discord идут через туннель,
    поэтому ни остановленный, ни вовсе не найденный ``winws.exe`` не означают
    сбой обхода, пока прокси жив. ``user_off`` — пользователь выключил zapret
    тумблером: это не авария, поэтому статус мягкий.
    """
    zapret = (status_state or {}).get("zapret") or {}
    if zapret.get("running"):
        detail = str(zapret.get("strategy") or "стратегия не определена")
        return _item("zapret", "zapret", "ok", detail, 0)
    if user_off:
        return _item("zapret", "zapret", "off", "выключен вручную", 0)
    cfg = cfg or {}
    proxy = proxy or {}
    youtube, discord = _proxied_groups(cfg, proxy)
    reserved = bool(proxy.get("running")) and (youtube or discord)
    if reserved:
        prefix = "остановлен" if zapret.get("dir") else "не найден"
        where = ("YouTube и Discord" if youtube and discord
                 else "YouTube" if youtube else "Discord")
        return _item("zapret", "zapret", "warn",
                     f"{prefix}, {where} через VPN", 0)
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


def _check_vpn(proxy: dict, timeout: float = 10.0,
               deadline: float | None = None) -> dict:
    """VPN проверяется настоящим запросом через туннель, а не портом.

    Открытый порт ничего не говорит: сервер может быть мёртв. ``verify`` идёт
    на заблокированный сайт (Telegram стабильнее Instagram), то есть
    обязательно через сервер. ``timeout`` — предел одной попытки, ``deadline``
    (``time.monotonic``) — общий на весь ``collect``: если бюджет кончился,
    честнее сказать «не успели», чем объявить живой сервер мёртвым и зря
    переключить его.
    """
    if not (proxy or {}).get("running"):
        return _item("vpn", "VPN", "off", "не подключён", 0)
    result: dict = {"ok": False}
    for attempt in range(2):
        left = _left(deadline)
        if left <= 0.0:
            return _item("vpn", "VPN", "warn",
                         "проверка не успела за отведённое время", 0)
        try:
            from . import xray as xray_module

            result = xray_module.verify(
                target="api.telegram.org",
                timeout=max(0.5, min(float(timeout), left)),
            )
        except Exception:  # noqa: BLE001
            result = {"ok": False}
        if result.get("ok"):
            break
        if attempt == 0:
            # Один провал ещё не «сервер мёртв»: целевой сайт мог ответить
            # медленно. Повторяем, как и остальные прокси-пробы.
            if _left(deadline) <= 0.5:
                break
            time.sleep(0.5)
    ms = int(result.get("ms") or 0)
    if result.get("ok"):
        seconds = max(0, ms) / 1000.0
        return _item("vpn", "VPN", "ok", f"через VPN · {seconds:.1f} с", ms)
    return _item("vpn", "VPN", "bad", "сервер не отвечает", ms)


# ---- проверки по конфигу --------------------------------------------------


def _target_label(record: dict) -> str:
    """Адрес для ``detail``: схема, хост и путь — без query и фрагмента.

    Путь показываем: по нему видно, что именно проверяется. А query и
    фрагмент — нет: в ``target`` бывают одноразовые токены (``?token=…``),
    и в отчёт, историю и журнал они попадать не должны.
    """
    target = str(record.get("target") or "")
    if str(record.get("kind") or "") == "https":
        try:
            parts = urllib.parse.urlsplit(target)
        except ValueError:
            return str(record.get("id") or "")
        return (f"{parts.scheme or 'https'}://{parts.hostname or ''}"
                f"{parts.path or ''}")
    return target


def _custom_state(record: dict, ok: bool) -> str:
    """Состояние своей проверки: отказ — ``warn``, у критичной — ``bad``."""
    if ok:
        return "ok"
    return "bad" if str(record.get("level") or "") == "critical" else "warn"


def _stamp(item: dict, record: dict) -> dict:
    """Переносит в пункт стабильные поля записи: ``id``, заголовок, уровень."""
    item_id = str(record.get("id") or item.get("id") or "")
    item["id"] = item_id
    item["title"] = str(record.get("title") or item.get("title") or item_id)
    item["level"] = str(record.get("level") or "")
    item["kind"] = str(record.get("kind") or "builtin")
    return item


def _custom_item(record: dict, state: str, detail: str, ms: int = 0) -> dict:
    item_id = str(record.get("id") or "")
    return _stamp(_item(item_id, str(record.get("title") or item_id),
                        state, detail, ms), record)


def _expect_ok(expect: dict, result: dict) -> tuple[bool, str]:
    """Совпал ли ответ с ожиданием записи; второй элемент — причина отказа."""
    status = int(result.get("status") or 0)
    if not status:
        return False, f"нет ответа ({result.get('error') or 'timeout'})"
    expect = expect or {}
    wanted = expect.get("status", "any")
    if wanted == "2xx":
        if not 200 <= status < 300:
            return False, f"HTTP {status}"
    elif isinstance(wanted, int):
        if status != wanted:
            return False, f"HTTP {status}, ждали {wanted}"
    elif status >= 500:
        # Любой ответ младше 500 доказывает доступность (как у встроенных проб).
        return False, f"HTTP {status}"
    needle = str(expect.get("contains") or "")
    if needle and needle not in str(result.get("body") or ""):
        return False, "нет ожидаемого текста"
    return True, ""


def _check_https(record: dict, proxy_port: int | None, timeout: float) -> dict:
    """Своя HTTPS-проверка: код и/или подстрока, ``via`` задаёт маршрут."""
    url = str(record.get("target") or "")
    label = _target_label(record)
    expect = record.get("expect") or {}
    via = str(record.get("via") or "auto")
    if via == "proxy":
        if not proxy_port:
            return _custom_item(record, _custom_state(record, False),
                                f"{label}: нужен VPN", 0)
        attempts = [("через сервер", proxy_port)]
    elif via == "direct":
        attempts = [("напрямую", None)]
    else:
        attempts = [("напрямую", None)]
        if proxy_port:
            attempts.append(("через сервер", proxy_port))
    result: dict = {"status": 0, "ms": 0, "error": "timeout"}
    why = "нет ответа"
    for where, port in attempts:
        result = (_fetch_via_proxy(url, port, timeout) if port
                  else _fetch_direct(url, timeout))
        ok, why = _expect_ok(expect, result)
        if ok:
            seconds = max(0, int(result.get("ms") or 0)) / 1000.0
            return _custom_item(
                record, "ok",
                f"{label} · {where} · HTTP {result.get('status')} · {seconds:.1f} с",
                int(result.get("ms") or 0))
    detail = f"{label}: {why}"
    if len(attempts) > 1:
        detail += " (напрямую и через сервер)"
    return _custom_item(record, _custom_state(record, False), detail,
                        int(result.get("ms") or 0))


def _check_tcp(record: dict, timeout: float) -> dict:
    """Своя TCP-проверка: открылось ли соединение с ``host:port``."""
    target = str(record.get("target") or "")
    host, _separator, port_text = target.rpartition(":")
    try:
        port = int(port_text)
    except ValueError:
        port = 0
    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            ms = _ms(started)
            return _custom_item(record, "ok", f"{target} · {ms / 1000:.1f} с", ms)
    except OSError as exc:
        return _custom_item(record, _custom_state(record, False),
                            f"{target}: {type(exc).__name__}", _ms(started))


def _check_builtin(record: dict, cfg: dict, proxy: dict, proxy_port: int | None,
                   status_state: dict, timeout: float, deadline: float | None,
                   zapret_off: bool = False) -> dict:
    """Встроенная проба по имени ``probe`` — прежняя логика без переписывания."""
    probe = str(record.get("probe") or record.get("id") or "")
    if probe == "zapret":
        item = _check_zapret(status_state, cfg, proxy, user_off=zapret_off)
    elif probe == "youtube":
        item = _check_youtube(cfg, proxy, timeout)
    elif probe == "discord":
        item = _check_discord(cfg, proxy, timeout)
    elif probe == "deepseek":
        item = _check_deepseek(proxy_port, timeout)
    elif probe == "telegram":
        item = _check_telegram(proxy_port, timeout)
    elif probe == "tgwsproxy":
        item = _check_tgws(status_state, cfg)
    elif probe == "vpn":
        item = _check_vpn(proxy, timeout, deadline)
    else:
        return _custom_item(record, "off", "проба не найдена", 0)
    # ``id`` из конфига стабилен (по нему живёт история), заголовок пользователь
    # может переименовать — его и показываем.
    return _stamp(item, record)


def _probe_record(record: dict, cfg: dict, proxy: dict, proxy_port: int | None,
                  status_state: dict, timeout: float,
                  deadline: float | None, zapret_off: bool = False) -> dict:
    kind = str(record.get("kind") or "builtin")
    if kind == "https":
        return _check_https(record, proxy_port, timeout)
    if kind == "tcp":
        return _check_tcp(record, timeout)
    return _check_builtin(record, cfg, proxy, proxy_port, status_state,
                          timeout, deadline, zapret_off=zapret_off)


def _failure_item(record: dict, exc: BaseException) -> dict:
    """Пункт для упавшей с исключением проверки: своя — по её уровню."""
    detail = f"проверка не удалась: {type(exc).__name__}"
    if str(record.get("kind") or "builtin") == "builtin":
        return _custom_item(record, "bad", detail, 0)
    return _custom_item(record, _custom_state(record, False), detail, 0)


def _target_record(target: str, timeout: float) -> dict | None:
    """Запись-однодневка для ``--check``: URL → https, иначе ``host:port``."""
    text = str(target or "").strip()
    if not text:
        return None
    base = {"id": "check", "title": text, "probe": "", "expect": {},
            "via": "auto", "level": "minor", "check": True, "show": True,
            "timeout": timeout}
    if "://" in text:
        try:
            parts = urllib.parse.urlsplit(text)
        except ValueError:
            return None
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None
        return dict(base, kind="https", target=text,
                    expect={"status": "any", "contains": ""})
    host, separator, port_text = text.rpartition(":")
    if not separator or not host or not port_text.isdigit():
        return None
    if not 1 <= int(port_text) <= 65535:
        return None
    return dict(base, kind="tcp", target=text)


def check_target(target: str, cfg: dict | None = None,
                 timeout: float = 5.0) -> dict:
    """Разовая проверка адреса тем же движком, что и ``health_items``.

    ``target`` — URL (http/https) или ``host:port``. Отчёт не пишется ни в
    ``health.json``, ни в историю: это проверка «до добавления в список».
    Поле ``ok`` — успех проверки, по нему CLI возвращает код 0/1.
    """
    try:
        limit = max(1.0, min(float(timeout), 30.0))
    except (TypeError, ValueError):
        limit = 5.0
    record = _target_record(target, limit)
    if record is None:
        return {"id": "check", "title": str(target or ""), "kind": "",
                "ok": False, "state": "bad", "ms": 0,
                "detail": "адрес не распознан: нужен URL или host:port"}
    proxy = _proxy_status()
    try:
        running = bool((proxy or {}).get("running"))
        port = int((proxy or {}).get("port")
                   or (cfg or {}).get("xray_port") or 0)
    except (TypeError, ValueError):
        running, port = False, 0
    proxy_port = port if running else None
    if record["kind"] == "https":
        item = _check_https(record, proxy_port, limit)
    else:
        item = _check_tcp(record, limit)
    result = dict(item)
    result["ok"] = str(item.get("state") or "") == "ok"
    return result


# ---- сводка ---------------------------------------------------------------


def verdict(items: list[dict]) -> str:
    """Итог по пунктам: fail — есть bad, warn — есть warn, иначе ok.

    Пустой отчёт — ``warn``: «проверок не было» ничем не лучше найденной
    проблемы, а зелёный «ok» на пустых данных прямо врёт пользователю.
    """
    states = {str(item.get("state") or "")
              for item in (items or []) if isinstance(item, dict)}
    if not states:
        return "warn"
    if "bad" in states:
        return "fail"
    if "warn" in states:
        return "warn"
    return "ok"


#: Критичные пункты: их отказ означает, что обход не выполняет свою работу.
CRITICAL_IDS = ("zapret", "youtube", "discord", "deepseek", "vpn")
#: Второстепенные: bad по ним — деградация, а не сбой.
SECONDARY_IDS = ("telegram", "tgwsproxy")


def _configured_severity(cfg: dict | None) -> tuple[dict[str, str], set[str]]:
    """Уровень записей и «свои» проверки из конфига.

    Возвращает ``id → critical|minor`` и множество своих (``https``/``tcp``).
    Без ``health_items`` (старый конфиг, тесты) раскладка пустая: дальше
    действуют встроенные ``CRITICAL_IDS``/``SECONDARY_IDS``.
    """
    levels: dict[str, str] = {}
    custom: set[str] = set()
    raw = (cfg or {}).get("health_items") if isinstance(cfg, dict) else None
    if isinstance(raw, (list, tuple)):
        for record in raw:
            if not isinstance(record, dict):
                continue
            item_id = str(record.get("id") or "")
            if not item_id:
                continue
            level = str(record.get("level") or "").strip().lower()
            if level not in ("critical", "minor"):
                level = "critical" if item_id in CRITICAL_IDS else "minor"
            levels[item_id] = level
            if str(record.get("kind") or "") in ("https", "tcp"):
                custom.add(item_id)
    return levels, custom


def _severity(item_id: str, levels: dict[str, str],
              item_levels: dict[str, str]) -> str:
    if item_id in levels:
        return levels[item_id]
    if item_id in item_levels:
        return item_levels[item_id]
    return "critical" if item_id in CRITICAL_IDS else "minor"


def level(items: list[dict], cfg: dict | None = None, proxy: dict | None = None,
          fresh: bool = True) -> str:
    """Трёхцветный уровень для иконки: ``ok`` | ``degraded`` | ``down``.

    ``down`` — отказ критичного пункта: VPN, DeepSeek, YouTube+Discord сразу
    или два критичных. ``degraded`` — любой одиночный bad критичного (иконка
    оранжевая, а не зелёная), warn критичных, bad второстепенных, упавшая своя
    проверка или устаревший отчёт. ``off`` у компонента ни на что не влияет.

    Уровень записи (``critical``/``minor``) берётся из ``cfg["health_items"]``,
    затем из самого пункта, затем из встроенной раскладки. Так один и тот же
    код годится и для полного отчёта, и для одиночной пробы в тесте.
    ``show`` — тот же флаг, что ``check``: скрытая запись не проверяется,
    поэтому в расчёт не попадает.
    """
    states: dict[str, str] = {}
    item_levels: dict[str, str] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "")
        states[item_id] = str(item.get("state") or "off")
        own = str(item.get("level") or "").strip().lower()
        if own in ("critical", "minor"):
            item_levels[item_id] = own

    if not states:
        return "degraded"

    levels, custom = _configured_severity(cfg)
    critical_bad = [item_id for item_id, state in states.items()
                    if state == "bad"
                    and _severity(item_id, levels, item_levels) == "critical"]
    # DeepSeek и VPN — отдельные критичные проверки: их bad сразу красный.
    if states.get("vpn") == "bad" or states.get("deepseek") == "bad":
        return "down"
    if states.get("youtube") == "bad" and states.get("discord") == "bad":
        return "down"
    if len(critical_bad) >= 2:
        return "down"

    minor_bad = [item_id for item_id, state in states.items()
                 if state == "bad"
                 and _severity(item_id, levels, item_levels) == "minor"]
    critical_warn = [item_id for item_id, state in states.items()
                     if state == "warn"
                     and _severity(item_id, levels, item_levels) == "critical"]
    # Своя проверка, отдавшая warn, — уже «требует внимания». У встроенных
    # warn второстепенных значит «компонент не нужен» (Telegram без VPN) и
    # проблемой не считается.
    custom_trouble = [item_id for item_id, state in states.items()
                      if item_id in custom and state in ("warn", "bad")]
    if (not fresh or critical_bad or minor_bad or critical_warn
            or custom_trouble):
        return "degraded"
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


def _required_ids(cfg: dict | None) -> tuple[str, ...]:
    """Включённые критичные проверки: без них отчёт неполон и «свежим» не будет."""
    raw = (cfg or {}).get("health_items") if isinstance(cfg, dict) else None
    if not isinstance(raw, (list, tuple)):
        return CRITICAL_IDS
    required: list[str] = []
    for record in raw:
        if not isinstance(record, dict) or not record.get("check", True):
            continue
        if str(record.get("level") or "").strip().lower() != "critical":
            continue
        item_id = str(record.get("id") or "")
        if item_id:
            required.append(item_id)
    return tuple(required)


def _health_records(cfg: dict | None) -> list[dict]:
    """Проверки сервисов из конфига; старый конфиг без ключа — встроенные."""
    raw = (cfg or {}).get("health_items") if isinstance(cfg, dict) else None
    records, _problems = config.normalise_health_items(raw)
    return records


def load_cache(cfg: dict | None = None) -> dict | None:
    """Последний отчёт из ``health.json``, если он ещё не устарел.

    Свежесть та же, что у ``run_guard``: не старше 2×``health_minutes``, и
    метка не из будущего. Битый файл, чужая структура, ошибки чтения и отчёт
    без включённых критичных пунктов — ``None``: по неполным данным судить
    нельзя. Отключённая в конфиге проверка (``check=false``) не требуется.
    """
    if cfg is None:
        try:
            cfg = config.load_config()
        except Exception:  # noqa: BLE001
            cfg = {}
    try:
        minutes = max(1, int((cfg or {}).get("health_minutes") or 10))
    except (TypeError, ValueError):
        minutes = 10
    try:
        data = json.loads((config.config_dir() / "health.json")
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return None
    present = {str(item.get("id") or "")
               for item in data["items"] if isinstance(item, dict)}
    if any(item_id not in present for item_id in _required_ids(cfg)):
        # Отчёт без критичных пунктов (старый формат, урезанная запись) —
        # не доказательство здоровья: показывать его как свежий нельзя.
        return None
    try:
        checked = float(data.get("checked") or 0)
    except (TypeError, ValueError):
        return None
    # Метка из будущего (съехали часы) давала отрицательный возраст и «вечно
    # свежий» отчёт: держим окно 0 … 2×health_minutes.
    age = time.time() - checked
    if not 0.0 <= age <= 2 * minutes * 60:
        return None
    return data


def collect(cfg: dict, status_state: dict | None = None, proxy: dict | None = None,
            timeout: float = 5.0, total_timeout: float | None = None,
            zapret_off: bool = False) -> dict:
    """Проверяет все включённые пункты ``health_items`` и сводит вердикт.

    ``status_state`` — результат ``status.collect``; если не передан, состояние
    собирается здесь. ``proxy`` — словарь ``xray.status``. ``timeout`` — предел
    одной пробы по умолчанию, запись может задать свой (``timeout`` в конфиге);
    ``total_timeout`` — предел всего сбора (по умолчанию 3×``timeout``). Пробы,
    не уложившиеся в бюджет, помечаются «не успела», а не держат индикатор
    десятки секунд и не объявляются сбоем.

    Записи с ``show=false`` (он же ``check``) не выполняются и в отчёт
    не попадают: один тумблер «Показывать» управляет и показом, и проверкой.
    """
    try:
        from . import strategy

        if strategy.trial_active():
            # Идёт перебор: winws намеренно гасится, и любой сбой сети —
            # свойство перебора, а не обхода. Отчёт не пишем и в историю
            # не кладём: иначе «ложный сбой» останется в статистике.
            return {
                "checked": time.time(),
                "items": [_item("strategy_trial", "Перебор стратегий", "warn",
                                "идёт перебор стратегий — проверки приостановлены")],
                "verdict": "warn",
                "level": "degraded",
                "skipped": "strategy_trial",
            }
    except Exception:  # noqa: BLE001
        # Отсутствие/поломка маркера не должна отменять проверки.
        pass
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

    records = _health_records(cfg)
    enabled = [record for record in records if record.get("check", True)]

    try:
        budget = float(total_timeout if total_timeout is not None else timeout * 3.0)
    except (TypeError, ValueError):
        budget = 15.0
    # Запись с длинным таймаутом не должна получать «не успела» из-за бюджета,
    # посчитанного по таймауту по умолчанию.
    longest = max([float(record.get("timeout") or timeout)
                   for record in enabled] or [timeout])
    hard_deadline = time.monotonic() + max(1.0, budget, longest)

    def probe(record: dict) -> dict:
        limit = float(record.get("timeout") or timeout)
        result = _run_probe(
            str(record.get("id") or ""),
            lambda item_timeout: _probe_record(
                record, cfg, proxy or {}, proxy_port, status_state,
                item_timeout, hard_deadline, zapret_off=zapret_off),
            limit, hard_deadline)
        return _stamp(result, record)

    results: dict[str, dict] = {}
    if enabled:
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(enabled))
        futures = {pool.submit(probe, record): record for record in enabled}
        try:
            done, pending = concurrent.futures.wait(
                futures, timeout=max(0.0, _left(hard_deadline)))
        finally:
            # Не ждём «хвост»: его сокеты ограничены тем же дедлайном, а сбор
            # уже отдаёт результат.
            pool.shutdown(wait=False, cancel_futures=True)
        for future in done:
            record = futures[future]
            item_id = str(record.get("id") or "")
            try:
                results[item_id] = future.result()
            except Exception as exc:  # noqa: BLE001
                results[item_id] = _failure_item(record, exc)
        for future in pending:
            record = futures[future]
            item_id = str(record.get("id") or "")
            # Бюджет кончился: не виним сервис (bad), но и не выдаём «ok».
            results[item_id] = _custom_item(
                record, "warn", "проверка не успела за отведённое время", 0)

    items = [results[str(record.get("id") or "")] for record in enabled]
    result = {
        "checked": time.time(),
        "items": items,
        "verdict": verdict(items),
        "level": level(items, cfg, proxy or {}),
    }
    _save_cache(result)
    try:
        from . import history

        history.append(result)
    except Exception:  # noqa: BLE001
        # История — вспомогательная: её сбой не должен превращать сбор
        # здоровья в ошибку. Пропуск виден по счётчику отчётов за сутки.
        pass
    return result
