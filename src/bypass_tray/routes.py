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


#: Текст ошибки для неразбираемого файла маршрутов. Политика та же, что у
#: ``config.json``: молча поверх не пишем — иначе один сбой чтения стёр бы
#: весь набор маршрутов без возможности откатиться.
BROKEN_MESSAGE = "route_overrides.json повреждён — не трогаю"


def _read_state() -> tuple[dict, str]:
    """Сырой файл и признак проблемы: ``({"at", "routes"}, error)``.

    ``error`` пуст, когда файла нет или он разобран. Неразбираемый файл
    отдаётся пустым набором, но с непустым ``error``: вызывающий код обязан
    отказаться от записи, а не затирать чужие байты.

    Старый плоский формат (``{домен: запись}``) читается как есть — файлы
    предыдущей версии остаются рабочими.
    """
    path = overrides_path()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {"at": 0.0, "routes": {}}, ""
    except OSError as exc:
        return {"at": 0.0, "routes": {}}, f"{path.name} не читается — не трогаю: {exc}"
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        return {"at": 0.0, "routes": {}}, BROKEN_MESSAGE
    if not isinstance(data, dict):
        return {"at": 0.0, "routes": {}}, BROKEN_MESSAGE
    routes_part = data.get("routes")
    if isinstance(routes_part, dict):
        try:
            at = float(data.get("at") or 0)
        except (TypeError, ValueError):
            at = 0.0
        return {"at": at, "routes": routes_part}, ""
    flat = {str(key): value for key, value in data.items() if isinstance(value, dict)}
    return {"at": 0.0, "routes": flat}, ""


def _load_file() -> dict:
    """Сырой файл: ``{"at": время, "routes": {домен: запись}}``."""
    return _read_state()[0]


def _preserve_broken() -> str:
    """Копия неразбираемого файла рядом: ``route_overrides.json.broken-<время>``.

    Сам файл не переписывается, поэтому разбирать его можно спокойно. Те же
    байты второй раз не копируются: почасовой аудит не станет плодить файлы.
    Пустая строка — копия не сделана.
    """
    path = overrides_path()
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    try:
        for old in sorted(path.parent.glob(path.name + ".broken-*")):
            try:
                if old.read_bytes() == raw:
                    return str(old)
            except OSError:
                continue
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        target = path.with_name(f"{path.name}.broken-{stamp}")
        counter = 2
        while target.exists():
            target = path.with_name(f"{path.name}.broken-{stamp}-{counter}")
            counter += 1
        temp = target.with_name(f"{target.name}.tmp-{os.getpid()}")
        try:
            temp.write_bytes(raw)
            os.replace(temp, target)
        except OSError:
            try:
                temp.unlink()
            except OSError:
                pass
            return ""
        return str(target)
    except OSError:
        return ""


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


def _compute_overrides(result: dict, cfg: dict, overrides: dict,
                       at: float) -> dict:
    """Новый набор маршрутов по результату аудита — без записи на диск.

    Общий расчёт для ``apply_audit`` и ``plan``: решения те же, разница лишь в
    том, что потом делают с результатом. Что именно изменилось, вызывающий
    считает сравнением «было/стало» (``_changed_domains``): список правок,
    собранный по ходу расчёта, пропускал удаления — например, чистку
    устаревшего ``direct`` в режиме ``all``.
    """
    overrides = dict(overrides)
    user_direct, user_proxy = user_routes(cfg)
    mode = normalise_routing_mode(cfg.get("routing_mode"))
    locked = set(user_direct) | set(user_proxy)
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
            overrides.pop(domain, None)
            continue
        if route not in ("direct", "proxy", "broken"):
            continue
        if route == "direct" and mode == "all":
            # В режиме "all" direct-решение аудита увело бы домен мимо VPN.
            # Такое не пишем и чистим оставшееся от прошлых сессий.
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
    return overrides


def apply_audit(result: dict, cfg: dict | None = None,
                reason: str = "manual") -> dict:
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

    Прежнее состояние перед записью уходит в снимок: ``reason`` — ``"auto"``
    у надзирателя, ``"manual"`` у кнопки и CLI. По снимку работает
    ``rollback_last``. Если маршруты не меняются, снимок не делается — иначе
    почасовой аудит вытеснил бы из цепочки настоящее изменение.

    Изменение считается сравнением «было/стало», а не подсчётом решений в
    ходе расчёта: в режиме ``all`` устаревший ``direct`` удаляется без нового
    правила, и это тоже изменение — со снимком и с перезапуском Xray. Так
    обещанный планом результат (``plan``) и настоящий совпадают.

    Повреждённый ``route_overrides.json`` не перезаписывается — политика та же,
    что у ``config.json``: молча поверх не пишем. Байты уходят в копию
    ``route_overrides.json.broken-<время>``, маршруты остаются как были, а в
    ответе — ``saved: False`` и ``error``; Xray не перезапускается.

    Возвращает ``{"changed": [...], "overrides": {...}, "saved": bool}``;
    при неудачной записи ``changed`` пуст и есть ``error``.
    """
    at = float(result.get("at") or time.time())
    current_file, read_error = _read_state()
    current = dict(current_file["routes"])
    if read_error:
        # Файл не разобрать: писать поверх нельзя — прежние маршруты пропали
        # бы бесследно. Копию байтов оставляем рядом для разбора руками.
        return {"changed": [], "overrides": current, "saved": False,
                "error": read_error, "broken_copy": _preserve_broken()}
    overrides = _compute_overrides(result, cfg or {}, current, at)
    changed = _changed_domains(current, overrides)
    if changed and not _snapshot_save(reason):
        # Без снимка откат невозможен: менять маршруты молча нечестно.
        return {"changed": [], "overrides": current, "saved": False,
                "error": last_write_error() or "снимок не сохранился"}
    if not save_overrides(overrides, at=at):
        return {"changed": [], "overrides": overrides, "saved": False,
                "error": last_write_error()}
    return {"changed": changed, "overrides": overrides, "saved": True}


# ---- снимки и откат -------------------------------------------------------

#: Сколько снимков прежнего состояния держим: откат работает на несколько шагов.
SNAPSHOT_LIMIT = 3


def snapshots_dir() -> Path:
    """Каталог снимков прежнего ``route_overrides.json``."""
    return config_dir() / "route_overrides_prev"


def _snapshot_files() -> list[Path]:
    """Файлы снимков от старого к новому: имя — время создания."""
    try:
        return sorted(snapshots_dir().glob("*.json"))
    except OSError:
        return []


def _snapshot_save(reason: str) -> bool:
    """Сохраняет прежнее состояние маршрутов перед записью нового.

    Формат ``route_overrides.json`` не меняется: снимок лежит рядом, в своём
    каталоге, и содержит прежние ``{"at", "routes"}`` целиком плюс причину.
    """
    previous = _load_file()
    stamp = time.time()
    directory = snapshots_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    # Имя с нулями: сортировка по имени совпадает с порядком времени.
    target = directory / f"{int(stamp * 1_000_000):016d}.json"
    while target.exists():
        # Два изменения в одну микросекунду не должны затереть друг друга.
        stamp += 0.000001
        target = directory / f"{int(stamp * 1_000_000):016d}.json"
    payload = {
        "at": stamp,
        "reason": str(reason or "manual"),
        "snapshot": {"at": previous["at"], "routes": previous["routes"]},
    }
    if not atomic_write(target, json.dumps(payload, ensure_ascii=False, indent=2)):
        return False
    for old in _snapshot_files()[:-SNAPSHOT_LIMIT]:
        try:
            old.unlink()
        except OSError:
            pass
    return True


def has_snapshot() -> bool:
    """Есть ли снимок для отката: панель показывает действие только тогда."""
    return bool(last_snapshot())


def last_snapshot() -> dict:
    """Последний читаемый снимок: ``{"at", "reason", "snapshot", "path"}``.

    Идём от новых файлов к старым и пропускаем нечитаемые: один битый снимок
    не должен запирать откат, когда глубже в цепочке лежат рабочие.
    ``{}`` — пригодных снимков нет.
    """
    for path in reversed(_snapshot_files()):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or not isinstance(data.get("snapshot"), dict):
            continue
        return {**data, "path": path}
    return {}


def _route_map(routes_part) -> dict[str, dict]:
    """Записи маршрутов из снимка в том же виде, что и в файле."""
    if not isinstance(routes_part, dict):
        return {}
    return {str(key): dict(value) for key, value in routes_part.items()
            if isinstance(value, dict)}


def _changed_domains(current: dict, target: dict) -> list[str]:
    """Домены, чей маршрут различается между двумя наборами."""
    changed = []
    for name in sorted(set(current) | set(target)):
        before = str((current.get(name) or {}).get("route") or "")
        after = str((target.get(name) or {}).get("route") or "")
        if before != after:
            changed.append(name)
    return changed


def rollback_last(cfg: dict | None = None) -> dict:
    """Возвращает предыдущий снимок в ``route_overrides.json``.

    Снимок считается израсходованным: следующий откат шагнёт на шаг назад по
    цепочке (до ``SNAPSHOT_LIMIT`` шагов). Пользовательские ``xray_routes``
    не трогаются: они живут в конфиге, а не в маршрутах. Xray перезапускается
    только если он запущен и набор маршрутов правда изменился.

    Время аудита после отката — не старее текущего: иначе ``due()`` сразу
    объявил бы маршруты просроченными и надзиратель в тот же час вернул бы
    свои решения. Так откат держится ``route_audit_minutes``.
    """
    last = last_snapshot()
    if not last:
        return {"ok": False, "changed": 0, "restarted": False,
                "error": "снимков нет — откатывать нечего"}
    target = _route_map(last["snapshot"].get("routes"))
    current = load_overrides()
    changed = _changed_domains(current, target)
    try:
        snapshot_at = float(last["snapshot"].get("at") or 0)
    except (TypeError, ValueError):
        snapshot_at = 0.0
    # Сценарий «ночью поменял, утром откатил»: старый ``at`` из снимка сделал
    # бы маршруты просроченными в ту же минуту.
    at = max(snapshot_at, time.time())
    if not save_overrides(target, at=at):
        return {"ok": False, "changed": 0, "restarted": False,
                "error": last_write_error() or "маршруты не записались"}
    restarted = False
    if changed:
        from . import xray as xray_module

        if xray_module.is_running():
            server = xray_module.saved_server()
            outcome = (xray_module.switch_to(server, cfg or {})
                       if server is not None else xray_module.restart(cfg or {}))
            restarted = bool(outcome.get("ok"))
    from . import activity

    activity.log(f"откатил маршруты: изменено {len(changed)}", kind="ok")
    try:
        last["path"].unlink()
    except (OSError, KeyError):
        pass
    return {"ok": True, "changed": len(changed), "restarted": restarted,
            "reason": str(last.get("reason") or ""),
            "at": float(last.get("at") or 0)}


_PLAN_PREVIEW = 4


def _plan_items(rows: list[dict], route_key: str) -> str:
    """Короткий перечень доменов для резюме: не длиннее ``_PLAN_PREVIEW``."""
    shown = [f"{row['domain']} → {row[route_key]}" if route_key else row["domain"]
             for row in rows[:_PLAN_PREVIEW]]
    text = ", ".join(shown)
    hidden = len(rows) - _PLAN_PREVIEW
    return f"{text} и ещё {hidden}" if hidden > 0 else text


def plan(cfg: dict, result: dict | None = None) -> dict:
    """Что изменится при применении аудита — ничего не записывая.

    ``result`` — готовый отчёт ``audit``: тогда сеть не трогается вовсе и
    прогон работает офлайн. Без него отчёт собирается здесь же; недоступная
    сеть даёт ``unknown`` и, значит, «изменений нет» — тоже без падения.

    Возвращает добавления, смены маршрута, удаления, нетронутые пользовательские
    домены и готовое человеческое резюме. Повреждённый файл маршрутов план не
    толкует по-своему: применение его не тронет, поэтому в резюме — та же
    ошибка, ``changes: 0`` и пустой ``restart_needed``.
    """
    current_file, read_error = _read_state()
    current = dict(current_file["routes"])
    mode = normalise_routing_mode((cfg or {}).get("routing_mode"))
    if read_error:
        return {
            "at": time.time(),
            "mode": mode,
            "changes": 0,
            "restart_needed": False,
            "added": [],
            "changed": [],
            "removed": [],
            "kept_user": [],
            "summary": read_error,
            "error": read_error,
        }
    if result is None:
        result = audit(cfg or {})
    at = float(result.get("at") or time.time())
    proposed = _compute_overrides(result, cfg or {}, current, at)
    user_direct, user_proxy = user_routes(cfg or {})
    locked = set(user_direct) | set(user_proxy)

    added: list[dict] = []
    retargeted: list[dict] = []
    removed: list[dict] = []
    for domain in sorted(set(current) | set(proposed)):
        old_route = str((current.get(domain) or {}).get("route") or "")
        new_route = str((proposed.get(domain) or {}).get("route") or "")
        if not old_route and new_route:
            added.append({"domain": domain, "route": new_route})
        elif old_route and not new_route:
            removed.append({"domain": domain, "route": old_route})
        elif old_route != new_route:
            retargeted.append({"domain": domain, "from": old_route,
                               "to": new_route})

    moved = {row["domain"] for row in added + retargeted + removed}
    seen = {clean_domain(item.get("domain") or "")
            for item in result.get("items") or []}
    kept_user = sorted(name for name in seen
                       if name and name in locked and name not in moved)

    parts = []
    if retargeted:
        parts.append(f"изменятся {len(retargeted)}: "
                     + _plan_items(retargeted, "to"))
    if added:
        parts.append(f"добавятся {len(added)}: " + _plan_items(added, "route"))
    if removed:
        parts.append(f"удалятся {len(removed)}: " + _plan_items(removed, ""))
    summary = "; ".join(parts) or "изменений нет"
    if kept_user:
        summary += f"; свои правила не трону: {len(kept_user)}"

    changes = len(added) + len(retargeted) + len(removed)
    return {
        "at": at,
        "mode": mode,
        "changes": changes,
        "restart_needed": changes > 0,
        "added": added,
        "changed": retargeted,
        "removed": removed,
        "kept_user": kept_user,
        "summary": summary,
    }


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


def apply_now(cfg: dict, result: dict | None = None) -> dict:
    """Явное применение маршрутов — для кнопки UI.

    Единственное место, где маршруты применяются сразу с перезапуском Xray.
    Надзиратель эту функцию не зовёт: живой туннель он не рвёт.

    ``result`` — готовый отчёт ``plan`` (диалог панели уже показал его
    пользователю): тогда повторного аудита нет и применяется ровно то, что
    человек подтвердил.
    """
    if result is None:
        result = audit(cfg)
    applied = apply_audit(result, cfg, reason="manual")
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
