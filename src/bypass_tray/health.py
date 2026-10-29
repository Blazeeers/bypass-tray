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


def level(items: list[dict], cfg: dict | None = None, proxy: dict | None = None,
          fresh: bool = True) -> str:
    """Трёхцветный уровень для иконки: ``ok`` | ``degraded`` | ``down``.

    ``down`` — отказ критичного пункта: VPN, DeepSeek, YouTube+Discord сразу
    или два критичных. ``degraded`` — любой одиночный bad критичного (иконка
    оранжевая, а не зелёная), warn критичных, bad второстепенных или
    устаревший отчёт. ``off`` у компонента ни на что не влияет.
    ``cfg``/``proxy`` оставлены в подписи для единообразия с ``collect``.

    Набор пунктов может быть частичным: функция считается по тому, что дали
    (одиночная проба, тест), а полноту отчёта проверяют ``load_cache`` и
    ``collect`` — только они решают, годен ли отчёт как здоровье целиком.
    Пустой список — всё равно ``degraded``: нет данных не значит «всё в порядке».
    """
    states: dict[str, str] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        states[str(item.get("id") or "")] = str(item.get("state") or "off")

    if not states:
        return "degraded"

    critical_bad = [item_id for item_id in CRITICAL_IDS
                    if states.get(item_id) == "bad"]
    # DeepSeek и VPN — отдельные критичные проверки: их bad сразу красный.
    if states.get("vpn") == "bad" or states.get("deepseek") == "bad":
        return "down"
    if states.get("youtube") == "bad" and states.get("discord") == "bad":
        return "down"
    if len(critical_bad) >= 2:
        return "down"

    if (not fresh or critical_bad
            or any(states.get(item_id) == "warn" for item_id in CRITICAL_IDS)
            or any(states.get(item_id) == "bad" for item_id in SECONDARY_IDS)):
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


def load_cache(cfg: dict | None = None) -> dict | None:
    """Последний отчёт из ``health.json``, если он ещё не устарел.

    Свежесть та же, что у ``run_guard``: не старше 2×``health_minutes``, и
    метка не из будущего. Битый файл, чужая структура, ошибки чтения и отчёт
    без критичных пунктов — ``None``: по неполным данным судить нельзя.
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
    if any(item_id not in present for item_id in CRITICAL_IDS):
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
    """Проверяет все пункты и возвращает сводку с вердиктом.

    ``status_state`` — результат ``status.collect``; если не передан, состояние
    собирается здесь. ``proxy`` — словарь ``xray.status``. ``timeout`` — предел
    одной пробы (действует и на VPN, у которой раньше был свой жёсткий 10 с),
    ``total_timeout`` — предел всего сбора (по умолчанию 3×``timeout``): пробы,
    не уложившиеся в него, помечаются «не успела», а не держат индикатор
    «занято» десятки секунд.
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

    try:
        budget = float(total_timeout if total_timeout is not None else timeout * 3.0)
    except (TypeError, ValueError):
        budget = 15.0
    hard_deadline = time.monotonic() + max(1.0, budget)

    def probe(item_id: str, check):
        return _run_probe(item_id, check, timeout, hard_deadline)

    tasks = [
        ("youtube", lambda: probe("youtube",
                                  lambda t: _check_youtube(cfg, proxy or {}, t))),
        ("discord", lambda: probe("discord",
                                  lambda t: _check_discord(cfg, proxy or {}, t))),
        ("deepseek", lambda: probe("deepseek",
                                   lambda t: _check_deepseek(proxy_port, t))),
        ("telegram", lambda: probe("telegram",
                                   lambda t: _check_telegram(proxy_port, t))),
        ("vpn", lambda: _check_vpn(proxy or {}, timeout, hard_deadline)),
    ]
    network_items: dict[str, dict] = {}
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(tasks))
    futures = {pool.submit(task): key for key, task in tasks}
    try:
        done, pending = concurrent.futures.wait(
            futures, timeout=max(0.0, _left(hard_deadline)))
    finally:
        # Не ждём «хвост»: его сокеты ограничены тем же дедлайном, а сбор
        # уже отдаёт результат.
        pool.shutdown(wait=False, cancel_futures=True)
    for future in done:
        key = futures[future]
        try:
            network_items[key] = future.result()
        except Exception as exc:  # noqa: BLE001
            network_items[key] = _item(
                key, key, "bad", f"проверка не удалась: {type(exc).__name__}", 0
            )
    for future in pending:
        key = futures[future]
        # Бюджет кончился: не виним сервис (bad), но и не выдаём «ok».
        network_items[key] = _item(
            key, key, "warn", "проверка не успела за отведённое время", 0)

    items = [
        _check_zapret(status_state, cfg, proxy or {}),
        network_items.get("youtube") or _item("youtube", "YouTube", "bad", "нет данных", 0),
        network_items.get("discord") or _item("discord", "Discord", "bad", "нет данных", 0),
        network_items.get("deepseek") or _item("deepseek", "DeepSeek", "bad", "нет данных", 0),
        network_items.get("telegram") or _item("telegram", "Telegram", "off", "нет данных", 0),
        _check_tgws(status_state, cfg),
        network_items.get("vpn") or _item("vpn", "VPN", "warn", "нет данных", 0),
    ]
    result = {
        "checked": time.time(),
        "items": items,
        "verdict": verdict(items),
        "level": level(items, cfg, proxy or {}),
    }
    _save_cache(result)
    return result
