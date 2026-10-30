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

#: Сколько тела ответа читать при проверке ``contains``: страница целиком не
#: нужна, а гигабайтная загрузка съела бы и время, и память.
MAX_BODY = 64 * 1024


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


def _stream_context(cfg: dict, proxy: dict) -> tuple[bool, int | None]:
    """Нужно ли проверять YouTube/Discord через VPN и на каком порту.

    Через VPN они идут в резервном режиме (``proxy["stream"]``) или когда
    пользователь включил ``youtube_via_vpn``. В режиме ``all`` через VPN идёт
    всё, кроме РФ, — значит, и они тоже: прямая проба дала бы ложный «сбой».
    Если режим выбран, а прокси не запущен — второй элемент ``None``, проверка
    вернёт «нужен VPN».
    """
    cfg = cfg or {}
    proxy = proxy or {}
    mode = str(proxy.get("routing_mode") or cfg.get("routing_mode") or "")
    all_mode = config.normalise_routing_mode(mode) == "all"
    requested = (all_mode or bool(proxy.get("stream"))
                 or bool(cfg.get("youtube_via_vpn")))
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


def _check_zapret(status_state: dict, cfg: dict | None = None,
                  proxy: dict | None = None) -> dict:
    """Состояние zapret. Без работающего zapret при VPN-резерве — warn.

    В резервном режиме и в режиме ``all`` YouTube/Discord идут через туннель,
    поэтому ни остановленный, ни вовсе не найденный ``winws.exe`` не означают
    сбой обхода, пока прокси жив.
    """
    zapret = (status_state or {}).get("zapret") or {}
    if zapret.get("running"):
        detail = str(zapret.get("strategy") or "стратегия не определена")
        return _item("zapret", "zapret", "ok", detail, 0)
    cfg = cfg or {}
    proxy = proxy or {}
    mode = str(proxy.get("routing_mode") or cfg.get("routing_mode") or "")
    reserved = bool(proxy.get("running")) and (
        bool(proxy.get("stream")) or bool(cfg.get("youtube_via_vpn"))
        or config.normalise_routing_mode(mode) == "all")
    if reserved:
        prefix = "остановлен" if zapret.get("dir") else "не найден"
        return _item("zapret", "zapret", "warn",
                     f"{prefix}, YouTube/Discord через VPN", 0)
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
                   status_state: dict, timeout: float, deadline: float | None) -> dict:
    """Встроенная проба по имени ``probe`` — прежняя логика без переписывания."""
    probe = str(record.get("probe") or record.get("id") or "")
    if probe == "zapret":
        item = _check_zapret(status_state, cfg, proxy)
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
                  deadline: float | None) -> dict:
    kind = str(record.get("kind") or "builtin")
    if kind == "https":
        return _check_https(record, proxy_port, timeout)
    if kind == "tcp":
        return _check_tcp(record, timeout)
    return _check_builtin(record, cfg, proxy, proxy_port, status_state,
                          timeout, deadline)


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
            timeout: float = 5.0, total_timeout: float | None = None) -> dict:
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
                item_timeout, hard_deadline),
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
