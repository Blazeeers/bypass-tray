"""Диагноз: что не так и что с этим делать.

Чистая функция без файлов и сети. На вход — уже собранный отчёт ``health``,
статус прокси (``xray.status``), конфиг и сводка истории за сутки; на выходе
один ответ: код, трёхцветный уровень, одна строка для человека, одно действие
и короткие факты.

Правила проверяются по приоритету: первое сработавшее и есть ответ. Порядок
не случаен — устаревшие данные хуже неверного диагноза, выключенный VPN важнее
остановленного zapret, а «туннель не отвечает» перекрывает всё, что проверяется
через этот туннель.

Секретов здесь нет по построению: факты собираются из ``id`` и ``state``
пунктов отчёта, без ``detail`` (в нём бывают адреса серверов).
"""

from __future__ import annotations

import threading
import time

#: Порядок фактов: сначала то, из-за чего обычно всё и ломается.
FACT_ORDER = ("vpn", "zapret", "youtube", "discord", "deepseek", "telegram",
              "tgwsproxy")

#: Подписи действий. ``kind`` — короткий код для UI, label — текст кнопки.
ACTIONS = {
    "check_all": "Проверить всё",
    "vpn_on": "Включить VPN",
    "switch_server": "Сменить сервер",
    "zapret_start": "Запустить zapret",
    "strategy_pick": "Подобрать стратегию",
    "tgws_restart": "Перезапустить",
}

#: Значения ``state``, которые бьют по работе обхода.
_BAD = ("bad", "off", "")

#: Меньше стольких отчётов за сутки — данных не хватает, чтобы называть
#: состояние нестабильным (6 отчётов — хотя бы час проверок при периоде 10 мин).
MIN_REPORTS_UNSTABLE = 6

#: Диагноз зовётся на каждом тике панели, а входные данные меняются редко.
#: Ответ запоминается на короткий срок: переход «устарело» опоздает максимум
#: на этот TTL, зато правила не пересчитываются каждую секунду.
CACHE_TTL = 15.0
_CACHE_LOCK = threading.Lock()
_CACHE: dict = {}


def _action(kind: str | None) -> dict | None:
    """Действие в формате ответа; неизвестный код — без кнопки."""
    if not kind:
        return None
    label = ACTIONS.get(kind)
    if not label:
        return None
    return {"label": label, "kind": kind}


def _answer(code: str, level: str, title: str, kind: str | None = None,
            facts: list[str] | None = None) -> dict:
    return {
        "code": code,
        "level": level if level in ("ok", "warn", "down") else "warn",
        "title": title,
        "action": _action(kind),
        "facts": list(facts or []),
    }


def _states(report: dict | None) -> dict[str, str]:
    """Состояния пунктов по ``id``; мусор и дубли молча отбрасываются."""
    states: dict[str, str] = {}
    for item in (report or {}).get("items") or []:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "")
        if item_id:
            states[item_id] = str(item.get("state") or "")
    return states


def _facts(states: dict[str, str], limit: int = 4) -> list[str]:
    """Короткие строки «пункт: состояние» — только проблемные пункты."""
    lines = [f"{item_id}: {states[item_id]}" for item_id in FACT_ORDER
             if states.get(item_id) not in ("ok", None, "")]
    for item_id, state in states.items():
        if item_id in FACT_ORDER or state in ("ok", "") or len(lines) >= limit:
            continue
        lines.append(f"{item_id}: {state}")
    return lines[:limit]


def _minutes(cfg: dict) -> int:
    try:
        return max(1, int((cfg or {}).get("health_minutes") or 10))
    except (TypeError, ValueError):
        return 10


def _stale(report: dict | None, minutes: int, now: float) -> bool:
    """Отчёта нет, он пуст или старше двух периодов проверки."""
    if not isinstance(report, dict) or not report.get("items"):
        return True
    try:
        checked = float(report.get("checked") or 0)
    except (TypeError, ValueError):
        return True
    if checked <= 0:
        return True
    # Метка из будущего — съехали часы; свежесть по такой метке не доказать,
    # но и «устарело» будет врать. Считаем свежим, как это делает health.
    return now - checked > 2 * minutes * 60


def _mode(proxy_status: dict, cfg: dict) -> str:
    """Режим маршрутизации: фактический у работающего ядра, иначе из конфига."""
    for source in (proxy_status or {}, cfg or {}):
        value = str(source.get("routing_mode") or "").strip().lower()
        if value in ("blocked", "all"):
            return value
    return "blocked"


def _report_level(report: dict | None) -> str:
    return str((report or {}).get("level") or "")


def _cache_key(report: dict | None, proxy: dict, cfg: dict,
               summary: dict) -> tuple:
    """Всё, от чего зависит ответ: изменилось — считаем заново."""
    checked = (report or {}).get("checked") if isinstance(report, dict) else None
    return (
        tuple(sorted(_states(report).items())),
        _report_level(report),
        str(checked),
        bool(proxy.get("running")),
        str(proxy.get("routing_mode") or ""),
        str(cfg.get("routing_mode") or ""),
        _minutes(cfg),
        summary.get("reports"),
        summary.get("incidents"),
        summary.get("uptime_pct"),
    )


def _decide(report: dict | None, proxy: dict, cfg: dict, summary: dict,
            stamp: float) -> dict:
    """Правила по приоритету; вход уже нормализован, время разрешено."""
    states = _states(report)
    facts = _facts(states)
    running = bool(proxy.get("running"))
    mode = _mode(proxy, cfg)
    vpn = states.get("vpn", "")
    youtube = states.get("youtube", "")
    discord = states.get("discord", "")
    zapret = states.get("zapret", "")
    tunnel_ok = vpn == "ok"

    if _stale(report, _minutes(cfg), stamp):
        return _answer("stale", "warn", "Данные устарели", "check_all",
                       facts or ["report: stale"])

    if not running and (youtube in _BAD or discord in _BAD):
        return _answer("vpn_off", "warn", "VPN выключен", "vpn_on", facts)

    if running and vpn == "bad":
        return _answer("tunnel_dead", "down", "Туннель не отвечает",
                       "switch_server", facts)

    if (mode == "blocked" and tunnel_ok and zapret == "bad"
            and youtube == "bad" and discord == "bad"):
        return _answer("zapret_stopped", "down", "zapret остановлен",
                       "zapret_start", facts)

    if (mode == "blocked" and tunnel_ok and zapret == "ok"
            and (youtube == "bad" or discord == "bad")):
        return _answer("zapret_weak", "warn", "Стратегия zapret не проходит",
                       "strategy_pick", facts)

    if states.get("deepseek") == "bad":
        return _answer("api_down", "down", "API недоступен", "check_all", facts)

    if states.get("telegram") == "bad" and running:
        return _answer("telegram_blocked", "warn",
                       "Telegram не проходит через прокси", None, facts)

    if states.get("tgwsproxy") == "bad":
        return _answer("tgws_down", "warn", "tg-ws-proxy не работает",
                       "tgws_restart", facts)

    if _report_level(report) == "ok" and _unstable(summary):
        return _answer("unstable", "warn", "Нестабильно за сутки", None,
                       _history_facts(summary))

    # Пункт, для которого нет отдельного правила (например, YouTube и Discord
    # сломались в режиме «all»), не должен теряться: иначе иконка красная,
    # а панель молчит.
    level = _report_level(report)
    if level == "down":
        return _answer("down", "down", "Есть сбой", "check_all", facts)
    if level == "degraded":
        return _answer("degraded", "warn", "Требует внимания", "check_all", facts)

    return _answer("ok", "ok", "Всё работает", None, [])


def diagnose(report: dict | None, proxy_status: dict | None, cfg: dict | None,
             history_summary: dict | None = None,
             now: float | None = None) -> dict:
    """Один ответ: код, уровень, строка для человека, действие и факты.

    Порядок правил — приоритет. ``now`` передаётся явно в тестах; в жизни
    берётся системное время, и тогда ответ кэшируется на ``CACHE_TTL``.
    """
    cfg = cfg or {}
    proxy = proxy_status or {}
    summary = history_summary or {}
    try:
        stamp = float(time.time() if now is None else now)
    except (TypeError, ValueError):
        stamp = time.time()

    if now is not None:
        # Явное время — детерминированный расчёт без кэша (тесты, CLI).
        return _decide(report, proxy, cfg, summary, stamp)

    key = _cache_key(report, proxy, cfg, summary)
    with _CACHE_LOCK:
        cached = _CACHE.get("entry")
        if (cached is not None and cached["key"] == key
                and time.monotonic() - cached["at"] <= CACHE_TTL):
            return dict(cached["value"])
    value = _decide(report, proxy, cfg, summary, stamp)
    with _CACHE_LOCK:
        _CACHE["entry"] = {"key": key, "at": time.monotonic(),
                           "value": dict(value)}
    return value


def _unstable(summary: dict) -> bool:
    """Много сбоев за сутки при нынешнем «ok» — повод сказать о нестабильности.

    По одному-двум отчётам «нестабильно» не объявляется: один ``degraded``
    иначе держал бы предупреждение на сутки вперёд.
    """
    try:
        # Записи без уровня не дают ни одного процента: для «нестабильно»
        # нужны именно оценённые отчёты, а не общее число строк.
        rated = summary.get("reports_rated")
        reports = int(rated if rated is not None else summary.get("reports") or 0)
    except (TypeError, ValueError):
        reports = 0
    if reports < MIN_REPORTS_UNSTABLE:
        return False
    try:
        incidents = int(summary.get("incidents") or 0)
    except (TypeError, ValueError):
        incidents = 0
    uptime = summary.get("uptime_pct")
    try:
        low_uptime = uptime is not None and float(uptime) < 70.0
    except (TypeError, ValueError):
        low_uptime = False
    return incidents >= 3 or low_uptime


def _history_facts(summary: dict) -> list[str]:
    """Факты о сутках — числа, без адресов и имён серверов."""
    facts: list[str] = []
    try:
        incidents = int(summary.get("incidents") or 0)
    except (TypeError, ValueError):
        incidents = 0
    if incidents:
        facts.append(f"history: incidents {incidents}")
    uptime = summary.get("uptime_pct")
    if uptime is not None:
        facts.append(f"history: uptime {int(uptime)}%")
    return facts[:2]
