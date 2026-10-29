"""Маршруты доменов: открывается напрямую или только через прокси.

Аудит проверяет каждый домен двумя способами — без прокси и через локальный
прокси Xray — и решает, куда его вести. Результат хранится в
``route_overrides.json`` в виде ``{"at": <время аудита>, "routes": {...}}``:
по нему Xray строит правила, а системный прокси получает исключения.

YouTube и Discord аудитом не управляются: их маршрут определяет zapret и
резервный режим Xray (``xray.STREAM_DOMAINS``). Если такие домены всё же
попали в список проверки, они пропускаются.

Домены — только имена, без схемы и пути (``clean_domain``).
"""

from __future__ import annotations

import concurrent.futures
import http.client
import json
import os
import socket
import ssl
import tempfile
import time
import urllib.parse
from pathlib import Path

from .config import (atomic_write, config_dir, last_write_error,
                     normalise_routing_mode)

#: Что проверяем по умолчанию. YouTube/Discord здесь нет намеренно: их закрывает
#: zapret, а резервный режим включает сам Xray.
DEFAULT_DOMAINS = [
    "api.deepseek.com",
    "api.telegram.org",
    "instagram.com",
    "huggingface.co",
    "github.com",
    "yandex.ru",
]

#: Префиксы правил Xray, которые нельзя превращать в имя хоста.
_KEEP_PREFIXES = ("geosite:", "geoip:", "full:", "regexp:", "keyword:", "ext:")


def clean_domain(value: str) -> str:
    """Имя домена: без схемы, пути, порта, ведущего «*.» и пробелов.

    ``geosite:``/``full:``/``regexp:`` и подобные префиксы Xray отдаются как
    есть — это не имена хостов.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if text.lower().startswith(_KEEP_PREFIXES):
        return text
    if text.lower().startswith("domain:"):
        text = text[len("domain:"):].strip()
    if "://" in text:
        try:
            text = urllib.parse.urlsplit(text).hostname or ""
        except ValueError:
            text = text.split("://", 1)[1]
    for separator in ("/", "?", "#"):
        text = text.split(separator, 1)[0]
    text = text.strip().strip(".").lower()
    if text.startswith("*."):
        text = text[2:]
    host, _, port = text.rpartition(":")
    if host and port.isdigit():
        text = host
    return text.strip().strip(".").lower()


_REGEX_PREFIXES = ("regexp:", "ext:")


def normalize_user_domain(value: str) -> str:
    """Домен пользовательского правила в каноническом виде.

    Снимаются схема, путь, ``*.`` и ``www.``, приводится регистр. Служебные
    префиксы Xray сохраняются: ``geosite:/domain:/full:/keyword:/geoip:`` —
    целиком в нижнем регистре, у ``regexp:/ext:`` — только префикс, шаблон не
    трогаем. Кириллические домены переводятся в punycode.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered.startswith(_REGEX_PREFIXES):
        prefix, _, payload = text.partition(":")
        return f"{prefix.lower()}:{payload.strip()}"
    name = clean_domain(text)
    if not name:
        return ""
    if name.lower().startswith(_KEEP_PREFIXES):
        return name.lower()
    if name.startswith("www."):
        name = name[4:]
    name = name.strip().lower()
    try:
        name.encode("ascii")
    except UnicodeEncodeError:
        try:
            name = name.encode("idna").decode("ascii")
        except UnicodeError:
            return name
    return name


def is_valid_domain(value: str) -> tuple[bool, str]:
    """Проверяет пользовательское правило и объясняет причину отказа."""
    text = str(value or "").strip()
    if not text:
        return False, "Укажите домен"
    lowered = text.lower()
    if lowered.startswith(_REGEX_PREFIXES):
        _prefix, _, payload = text.partition(":")
        return (True, "") if payload.strip() else (False, "Похоже, это не домен")
    if lowered.startswith(_KEEP_PREFIXES):
        _prefix, _, payload = lowered.partition(":")
        return (True, "") if payload.strip() else (False, "Похоже, это не домен")
    host = normalize_user_domain(text)
    if not host or "://" in host:
        return False, "Похоже, это не домен"
    try:
        host.encode("ascii")
    except UnicodeEncodeError:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return False, "Не удалось преобразовать домен (IDN)"
    if "." not in host or ".." in host or " " in host or "/" in host:
        return False, "Похоже, это не домен"
    labels = host.split(".")
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789-")
    if any(len(label) > 63 or label.startswith("-") or label.endswith("-")
           or any(ch not in allowed for ch in label) for label in labels):
        return False, "Похоже, это не домен"
    if (len(host) > 253 or len(labels[-1]) < 2
            or not any(ch.isalpha() for ch in labels[-1])):
        return False, "Похоже, это не домен"
    return True, ""


def user_routes(cfg: dict) -> tuple[list[str], list[str]]:
    """Пользовательские правила из ``xray_routes``: (direct, proxy).

    Домены нормализованы и не повторяются; при конфликте побеждает direct.
    """
    mapping = (cfg or {}).get("xray_routes")
    if not isinstance(mapping, dict):
        return [], []
    direct: list[str] = []
    proxy: list[str] = []
    for raw, route in mapping.items():
        name = normalize_user_domain(raw)
        if not name:
            continue
        if str(route) == "direct":
            if name not in direct:
                direct.append(name)
        elif str(route) == "proxy":
            if name not in proxy:
                proxy.append(name)
    proxy = [name for name in proxy if name not in set(direct)]
    return direct, proxy


def _probe_state() -> dict:
    try:
        from . import xray

        return xray.status()
    except Exception:  # noqa: BLE001
        return {"running": False, "port": 0}


def _is_stream_domain(name: str, blocked: set[str]) -> bool:
    """Домен относится к YouTube/Discord (включая поддомены)."""
    return any(name == item or name.endswith("." + item) for item in blocked)


# ---- проверка и решение ---------------------------------------------------

#: Признаки локального выпуска сертификата. Такой TLS-сбой говорит о доверии
#: к хранилищу на этой машине, а не о недоступности сайта.
_LOCAL_ISSUER_HINTS = ("localhost", "self", "local", "bypass")

PROBE_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) bypass-tray"


def _tls_error(result: dict) -> bool:
    """Был ли провал именно TLS-ошибкой."""
    error = str(result.get("error") or "")
    return error == "tls" or error.endswith("SSLCertVerificationError")


def _peer_certificate(host: str, port: int, timeout: float,
                      proxy_port: int | None = None) -> tuple[bytes, int]:
    """Сертификат узла и код HTTP-ответа — без проверки подписи.

    Второй проход после TLS-ошибки. Сертификат и ответ доказывают, что узел
    отвечает, а первая ошибка касалась только доверия к нему. Пустой
    сертификат с нулевым кодом — оборванное рукопожатие (типовая сигнатура
    ТСПУ и подмены сертификата провайдером).
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    connection = None
    try:
        if proxy_port:
            connection = http.client.HTTPSConnection(
                "127.0.0.1", int(proxy_port), timeout=timeout, context=context)
            connection.set_tunnel(host, port)
        else:
            connection = http.client.HTTPSConnection(
                host, port, timeout=timeout, context=context)
        connection.request("GET", "/", headers={"User-Agent": PROBE_UA})
        status = int(connection.getresponse().status)
        sock = connection.sock
        der = sock.getpeercert(binary_form=True) if sock is not None else b""
        return der or b"", status
    except (OSError, ValueError, http.client.HTTPException):
        return b"", 0
    finally:
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass


def _certificate_issuer(der: bytes) -> dict:
    """Разобранный сертификат (``issuer``/``subject``) или ``{}``.

    Разбор идёт через ``ssl`` — отдельная зависимость ради одного поля не
    нужна. Сертификат передаётся файлом: другого входа у разборщика нет.
    """
    if not der:
        return {}
    decode = getattr(getattr(ssl, "_ssl", None), "_test_decode_cert", None)
    if decode is None:
        return {}
    path = ""
    try:
        with tempfile.NamedTemporaryFile(
                "w", suffix=".pem", delete=False, encoding="ascii") as handle:
            handle.write(ssl.DER_cert_to_PEM_cert(der))
            path = handle.name
        return decode(path) or {}
    except (OSError, ValueError):
        return {}
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


def _issuer_text(decoded: dict) -> str:
    names = []
    for field in decoded.get("issuer") or ():
        for key, value in field:
            names.append(f"{key}={value}")
    return " ".join(names).lower()


def _issuer_is_local(decoded: dict) -> bool:
    """Выпущен ли сертификат локально или сам собой (self-signed)."""
    if not decoded:
        return False
    if decoded.get("subject") == decoded.get("issuer"):
        return True
    issuer = _issuer_text(decoded)
    hints = list(_LOCAL_ISSUER_HINTS)
    try:
        host = socket.gethostname().strip().lower()
    except OSError:
        host = ""
    if len(host) >= 3:
        hints.append(host)
    return any(hint in issuer for hint in hints)


def probe(domain: str, proxy_port: int | None = None, timeout: float = 5.0) -> dict:
    """Проверяет домен: напрямую или через прокси (``proxy_port``).

    TLS-сбой сам по себе недоступности не доказывает — и не опровергает.
    Обрыв рукопожатия (сигнатура ТСПУ) и подмена сертификата провайдером
    выглядят одинаково, поэтому после такой ошибки идёт второй проход без
    проверки подписи. Достижимым домен считается, только если узел предъявил
    сертификат **и** ответил, а сертификат выпущен локально или сам собой:
    тогда проблема в хранилище этой машины. Публичный сертификат при
    непройденной проверке — признак подмены, и это провал.
    """
    from . import health

    name = clean_domain(domain)
    if not name:
        return {"ok": False, "status": 0, "ms": 0, "error": "пустой домен"}
    result = health._http(f"https://{name}/", proxy_port, timeout)
    if result.get("ok") or not _tls_error(result):
        return result
    der, status = _peer_certificate(name, 443, timeout, proxy_port)
    if not der or not 0 < status < 500:
        # Сертификата нет: рукопожатие оборвано, домен недоступен.
        return {**result, "ok": False, "error": "tls"}
    decoded = _certificate_issuer(der)
    local = _issuer_is_local(decoded)
    return {
        **result,
        "ok": local,
        "status": status,
        "error": "tls",
        "tls_local": local,
        "tls_issuer": _issuer_text(decoded)[:120],
    }


def decide(direct_ok: bool, proxy_ok: bool) -> str:
    """Маршрут домена, когда прокси точно проверялся."""
    if direct_ok:
        return "direct"
    if proxy_ok:
        return "proxy"
    return "broken"


def decide_route(direct_ok: bool, proxy_checked: bool, proxy_ok: bool) -> str:
    """Трёхзначная логика: без проверки прокси провал напрямую — «unknown».

    «broken» ставится только когда точно известно, что не работает ни один
    путь: иначе выключенный прокси записал бы все домены как сломанные.
    """
    if direct_ok:
        return "direct"
    if not proxy_checked:
        return "unknown"
    return "proxy" if proxy_ok else "broken"


def _probe_via_proxy(name: str, port: int, timeout: float = 5.0) -> dict:
    """Проба через прокси с одной повторной попыткой.

    Туннель изредка отваливается на ровном месте: сервер не успевает ответить
    в общем потоке проб. Без повтора один такой сбой записал бы домен как
    «broken» и заставил надзирателя зря перезапускать прокси.
    """
    result = probe(name, port, timeout)
    if result.get("ok"):
        return result
    time.sleep(0.5)
    retry = probe(name, port, timeout)
    return retry if retry.get("ok") else result


def audit(cfg: dict, proxy_port: int | None = None,
          domains: list[str] | None = None) -> dict:
    """Проверяет домены двумя способами и предлагает маршрут каждому.

    Если прокси не запущен, проверка идёт только напрямую, а провал даёт
    маршрут ``unknown`` — такой результат не сохраняется.

    ``full`` в результате — аудит прошёл весь набор по умолчанию. Явно
    заданный список доменов делает аудит частичным: выводы по нему не
    доказывают, что остальные сохранённые маршруты устарели.
    """
    state = _probe_state()
    running = bool(state.get("running"))
    if proxy_port is None:
        try:
            proxy_port = int(state.get("port") or cfg.get("xray_port") or 0)
        except (TypeError, ValueError):
            proxy_port = 0
    port = int(proxy_port or 0) if running else None
    proxy_checked = port is not None

    try:
        from . import xray as xray_module

        stream = {clean_domain(d) for d in xray_module.STREAM_DOMAINS}
    except Exception:  # noqa: BLE001
        stream = set()
    names = [clean_domain(d) for d in (domains or DEFAULT_DOMAINS)]
    names = [name for name in names if name and not _is_stream_domain(name, stream)]

    direct_results: dict[str, dict] = {}
    proxy_results: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        direct_jobs = {pool.submit(probe, name, None, 5.0): name for name in names}
        proxy_jobs = ({pool.submit(_probe_via_proxy, name, port, 5.0): name
                       for name in names} if port else {})
        for future in concurrent.futures.as_completed(direct_jobs):
            name = direct_jobs[future]
            try:
                direct_results[name] = future.result()
            except Exception as exc:  # noqa: BLE001
                direct_results[name] = {"ok": False, "status": 0, "ms": 0,
                                        "error": type(exc).__name__}
        for future in concurrent.futures.as_completed(proxy_jobs):
            name = proxy_jobs[future]
            try:
                proxy_results[name] = future.result()
            except Exception as exc:  # noqa: BLE001
                proxy_results[name] = {"ok": False, "status": 0, "ms": 0,
                                       "error": type(exc).__name__}

    items = []
    broken = []
    for name in names:
        direct = direct_results.get(name) or {"ok": False, "status": 0, "ms": 0,
                                              "error": "нет данных"}
        via = proxy_results.get(name) or {"ok": False, "status": 0, "ms": 0,
                                          "error": "прокси выключен"}
        route = decide_route(bool(direct.get("ok")), proxy_checked, bool(via.get("ok")))
        if route == "broken":
            broken.append(name)
        items.append({"domain": name, "direct": direct, "proxy": via, "route": route})

    return {"at": time.time(), "proxy": running, "domains": names,
            "full": not domains, "items": items, "broken": broken}


# ---- оверрайды ------------------------------------------------------------


def overrides_path() -> Path:
    return config_dir() / "route_overrides.json"


def _load_file() -> dict:
    """Сырой файл: ``{"at": время, "routes": {домен: запись}}``.

    Старый плоский формат (``{домен: запись}``) читается как есть — файлы
    предыдущей версии остаются рабочими.
    """
    try:
        data = json.loads(overrides_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"at": 0.0, "routes": {}}
    if not isinstance(data, dict):
        return {"at": 0.0, "routes": {}}
    routes_part = data.get("routes")
    if isinstance(routes_part, dict):
        try:
            at = float(data.get("at") or 0)
        except (TypeError, ValueError):
            at = 0.0
        return {"at": at, "routes": routes_part}
    flat = {str(key): value for key, value in data.items() if isinstance(value, dict)}
    return {"at": 0.0, "routes": flat}


def load_overrides() -> dict:
    """Сохранённые маршруты: ``{домен: {"route", "since", "checked"}}``."""
    return dict(_load_file()["routes"])


def save_overrides(data: dict, at: float | None = None) -> bool:
    """Пишет маршруты и время аудита атомарно.

    ``False`` — запись не удалась: тогда сообщать «маршруты обновлены» нельзя,
    и перезапускать Xray незачем — он прочитал бы прежний файл.
    """
    payload = {"at": float(at if at is not None else time.time()), "routes": data}
    return bool(atomic_write(overrides_path(),
                             json.dumps(payload, ensure_ascii=False, indent=2)))


def apply_audit(result: dict, cfg: dict | None = None) -> dict:
    """Переносит результат аудита в оверрайды.

    Пользовательские правила из ``cfg["xray_routes"]`` важнее автоподбора:
    такие домены не перезаписываются (и старый оверрайд для них удаляется).

    Сохраняются и ``direct``, и ``proxy``: direct — тоже решение аудита, а не
    отсутствие записи. ``unknown`` не сохраняется вовсе. Исключение — режим
    ``all``: там любой ``direct`` аудита увёл бы трафик мимо VPN, поэтому
    direct-решения не пишутся, а оставшиеся от сессий в ``blocked`` удаляются.

    Прунинг — только у полного аудита (``result["full"]``) или по явному
    списку ``result["prune_domains"]``: тогда удаляется запись домена, который
    в этот раз не проверялся, то есть выпал из аудита. Частичный аудит не
    трогает маршруты, которых он не касался: стереть лишнее хуже, чем
    оставить. У неизменившегося маршрута сохраняется исходное ``since``.

    Возвращает ``{"changed": [...], "overrides": {...}, "saved": bool}``;
    при неудачной записи ``changed`` пуст и есть ``error``.
    """
    at = float(result.get("at") or time.time())
    overrides = dict(_load_file()["routes"])
    user_direct, user_proxy = user_routes(cfg or {})
    mode = normalise_routing_mode((cfg or {}).get("routing_mode"))
    locked = set(user_direct) | set(user_proxy)
    changed: list[str] = []
    audited: set[str] = set()
    for item in result.get("items") or []:
        domain = clean_domain(item.get("domain") or "")
        route = str(item.get("route") or "")
        if not domain:
            continue
        # Домен проверялся (пусть и без прокси) — его запись не удаляем.
        audited.add(domain)
        if domain in locked:
            # Своё правило сильнее любого автоподбора.
            if domain in overrides:
                overrides.pop(domain, None)
                changed.append(domain)
            continue
        if route not in ("direct", "proxy", "broken"):
            continue
        if route == "direct" and mode == "all":
            # В режиме "all" direct-решение аудита увело бы домен мимо VPN.
            # Такое не пишем и чистим оставшееся от прошлых сессий: маршрут
            # домена в этом режиме и так идёт через прокси, перезапуск не нужен.
            overrides.pop(domain, None)
            continue
        previous = overrides.get(domain) or {}
        if route == "broken" and previous.get("route") in ("direct", "proxy"):
            # Один сбой не должен ломать последний рабочий маршрут.
            continue
        if route == "broken":
            continue
        same = str(previous.get("route") or "") == route
        try:
            old_since = float(previous.get("since") or 0)
        except (TypeError, ValueError):
            old_since = 0.0
        if not same:
            changed.append(domain)
        overrides[domain] = {
            "route": route,
            "since": old_since if same and old_since else at,
            "checked": at,
        }
    scope = result.get("prune_domains")
    if isinstance(scope, (list, tuple)):
        prune_set = {clean_domain(str(name)) for name in scope}
        prune_set.discard("")
    elif result.get("full") is True:
        # Полный аудит: всё, что не проверено в этот раз, из маршрутов уходит.
        prune_set = None
    else:
        prune_set = set()
    if prune_set is None or prune_set:
        for domain in sorted(overrides):
            if prune_set is not None and domain not in prune_set:
                continue
            if domain not in audited:
                overrides.pop(domain, None)
                changed.append(domain)
    if not save_overrides(overrides, at=at):
        return {"changed": [], "overrides": overrides, "saved": False,
                "error": last_write_error()}
    return {"changed": changed, "overrides": overrides, "saved": True}


def _domains_by_route(route: str) -> list[str]:
    return sorted(
        domain
        for domain, info in load_overrides().items()
        if str((info or {}).get("route") or "") == route
    )


def proxy_domains() -> list[str]:
    """Домены, которые аудит требует вести через прокси.

    В конфиге Xray они должны стоять **выше** ``geosite:category-ru`` и
    ``geoip:ru``: заблокированный ресурс на российском хостинге иначе
    попадёт в правило «РФ — напрямую» и останется недоступным.
    """
    return _domains_by_route("proxy")


def direct_domains() -> list[str]:
    """Домены, которые аудит требует вести напрямую."""
    return _domains_by_route("direct")


def due(minutes: int) -> bool:
    """Пора ли снова проверять маршруты — по времени последнего аудита."""
    at = _load_file()["at"]
    return time.time() - at >= max(0, int(minutes)) * 60


def stale(hours: int = 24) -> bool:
    """Есть ли запись, проверенная раньше срока (совместимость)."""
    limit = time.time() - max(0, int(hours)) * 3600
    for info in _load_file()["routes"].values():
        try:
            checked = float((info or {}).get("checked") or 0)
        except (TypeError, ValueError):
            checked = 0.0
        if checked < limit:
            return True
    return False


def fresh(minutes: int = 60) -> bool:
    """Обратная к ``due`` (совместимость со старым именем)."""
    return not due(minutes)


def apply_now(cfg: dict) -> dict:
    """Явное применение маршрутов — для кнопки UI.

    Единственное место, где маршруты применяются сразу с перезапуском Xray.
    Надзиратель эту функцию не зовёт: живой туннель он не рвёт.
    """
    result = audit(cfg)
    applied = apply_audit(result, cfg)
    if not applied.get("saved", True):
        # Запись не удалась: применять нечего, Xray читал бы прежний файл.
        return {"changed": 0, "need_restart": False, "restarted": False,
                "saved": False, "error": applied.get("error") or ""}
    changed = len(applied.get("changed") or [])
    if not changed:
        return {"changed": 0, "need_restart": False, "restarted": False,
                "saved": True}
    from . import xray as xray_module

    restarted = False
    if xray_module.is_running():
        server = xray_module.saved_server()
        outcome = (xray_module.switch_to(server, cfg) if server is not None
                   else xray_module.restart(cfg))
        restarted = bool(outcome.get("ok"))
    return {"changed": changed, "need_restart": True, "restarted": restarted}
