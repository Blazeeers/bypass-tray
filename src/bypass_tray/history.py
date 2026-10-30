"""История здоровья за сутки: краткая летопись отчётов ``health.collect``.

``health.json`` хранит только последний отчёт, а ``app._reset_health`` стирает
его из памяти при каждом переключении прокси — поэтому «стабильность за сутки»
приходится читать из файла. Пишем минимум: время, уровень, вердикт и состояния
пунктов по ``id``. Подробности (``detail``) отбрасываются: в них бывают адреса
серверов и причины отказа, а для счёта они не нужны.

История вспомогательная: её сбой не должен ломать сам ``health``. Поэтому
``append`` вызывает ``collect`` в защите, а чтение при битом файле отдаёт
пустой список, а не исключение.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .config import atomic_write, config_dir

#: Окно хранения: сутки с запасом, чтобы сравнивать «вчера» и «сегодня».
MAX_AGE_HOURS = 48
#: Предел записей. Штатный темп (раз в 10 минут) даёт 288 в сутки; потолок
#: нужен на случай ручных перепроверок, чтобы файл не рос без меры.
MAX_ROWS = 2000

#: Тексты ``app.py``, которыми отмечается смена сервера (сравнение подстрокой).
SERVER_SWITCH_MARKERS = (
    "переключил сервер",
    "выбрал сервер",
    "подобрал лучший сервер",
)

_LEVEL_RANK = {"ok": 0, "degraded": 1, "down": 2}

_LOCK = threading.Lock()

#: Сводку просит каждое обновление панели — раз в секунду. Пересчитывать
#: 2000 записей на каждый тик незачем: кэш живёт ``SUMMARY_TTL`` секунд и
#: сбрасывается, когда меняется mtime истории или журнала.
SUMMARY_TTL = 30.0
_SUMMARY_CACHE: dict = {}
_SUMMARY_LOCK = threading.Lock()


def _file_stamp(path: Path) -> float:
    """mtime файла или 0, если его нет: этого хватает признаку «изменилось»."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def reset_summary_cache() -> None:
    """Забыть сводку — после проверки карточка должна обновиться сразу."""
    with _SUMMARY_LOCK:
        _SUMMARY_CACHE.clear()


def path() -> Path:
    return config_dir() / "health_history.json"


def _as_time(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _read() -> list[dict]:
    """Записи истории в порядке добавления (старые сверху)."""
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]


def _write(rows: list[dict]) -> None:
    atomic_write(path(), json.dumps(rows, ensure_ascii=False, indent=2))


def append(report: dict) -> None:
    """Дописывает компактную запись из отчёта ``health.collect``.

    Отчёт приходит в формате ``health.json``: ``checked``, ``level``,
    ``verdict`` и список пунктов с ``id``/``detail``/``state``. Берём только
    то, что нужно для сводки; хвост старше окна и сверх лимита отрезаем здесь,
    чтобы файл не зависел от того, кто его читает.
    """
    if not isinstance(report, dict):
        return
    now = _as_time(report.get("checked") or report.get("at"), time.time())
    states: dict[str, str] = {}
    for item in report.get("items") or []:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "")
        if item_id:
            states[item_id] = str(item.get("state") or "")
    entry = {
        "at": now,
        "level": str(report.get("level") or ""),
        "verdict": str(report.get("verdict") or ""),
        "items": states,
    }
    cutoff = now - MAX_AGE_HOURS * 3600.0
    with _LOCK:
        kept = [row for row in _read() if _as_time(row.get("at"), 0.0) >= cutoff]
        _write((kept + [entry])[-MAX_ROWS:])


def rows(hours: float = 24, now: float | None = None) -> list[dict]:
    """Записи за окно ``hours``, старые сверху.

    Битый, отсутствующий или чужой файл — пустой список: панель и CLI не
    должны падать из-за истории.
    """
    when = _as_time(now, time.time()) if now is not None else time.time()
    try:
        cutoff = when - float(hours) * 3600.0
    except (TypeError, ValueError):
        cutoff = when - 24 * 3600.0
    with _LOCK:
        data = _read()
    return [row for row in data if _as_time(row.get("at"), 0.0) >= cutoff]


def summarise(report_rows: list[dict], activity_rows: list[dict],
              hours: float = 24, now: float | None = None) -> dict:
    """Сводка по готовым записям — чистая функция, без файлов и часов.

    ``now`` и окно передаются явно, чтобы тест не зависел от системного
    времени. ``incidents`` считает только ухудшения: два ``down`` подряд —
    один сбой, ``down → ok → down`` — два.
    """
    when = _as_time(now, time.time()) if now is not None else time.time()
    try:
        cutoff = when - float(hours) * 3600.0
    except (TypeError, ValueError):
        cutoff = when - 24 * 3600.0

    kept = [row for row in (report_rows or [])
            if isinstance(row, dict) and _as_time(row.get("at"), 0.0) >= cutoff]
    kept.sort(key=lambda row: _as_time(row.get("at"), 0.0))
    acts = [row for row in (activity_rows or [])
            if isinstance(row, dict) and _as_time(row.get("at"), 0.0) >= cutoff]

    total = len(kept)
    ok_count = 0
    by_service: dict[str, int] = {}
    worst = ""
    incidents = 0
    previous: int | None = None

    for row in kept:
        level = str(row.get("level") or "")
        if level == "ok":
            ok_count += 1
        items = row.get("items")
        if isinstance(items, dict):
            for item_id, state in items.items():
                if str(state) in ("bad", "warn"):
                    key = str(item_id)
                    by_service[key] = by_service.get(key, 0) + 1
        rank = _LEVEL_RANK.get(level)
        if rank is None:
            # Запись без уровня (старый формат) в счёт сбоев не идёт, но и
            # «здоровьем» не считается: её просто не видно в этих числах.
            continue
        if rank > _LEVEL_RANK.get(worst, -1):
            worst = level
        if previous is not None and rank > previous:
            incidents += 1
        previous = rank

    switches = 0
    heals = 0
    for row in acts:
        text = str(row.get("text") or "").lower()
        if any(marker in text for marker in SERVER_SWITCH_MARKERS):
            switches += 1
        # Успешное автолечение: «tg-ws-proxy упал — перезапустил». Неудачная
        # попытка («перезапустить не удалось») сюда не попадает.
        if "tg-ws" in text and "перезапустил" in text:
            heals += 1

    return {
        "uptime_pct": (round(100.0 * ok_count / total) if total else None),
        "incidents": incidents,
        "worst": worst or None,
        "by_service": by_service,
        "first_at": _as_time(kept[0].get("at"), 0.0) if kept else None,
        "last_at": _as_time(kept[-1].get("at"), 0.0) if kept else None,
        "reports": total,
        "server_switches": switches,
        "heals": heals,
    }


def _build_summary(hours: float, now: float | None) -> dict:
    """Свежий расчёт сводки из файлов (без кэша)."""
    from . import activity

    try:
        acts = activity.recent(limit=activity.LIMIT, hours=hours)
    except (TypeError, ValueError):
        acts = []
    return summarise(rows(hours, now), acts, hours, now)


def summary(hours: float = 24, now: float | None = None) -> dict:
    """Сводка из файла истории и журнала за последние ``hours`` часов.

    Без явного ``now`` результат кэшируется на ``SUMMARY_TTL``: панель зовёт
    функцию каждый тик. Явное время — всегда честный пересчёт (тесты, CLI).
    """
    if now is not None:
        return _build_summary(hours, now)
    from . import activity

    stamp = (float(hours), _file_stamp(path()), _file_stamp(activity.path()))
    now_mono = time.monotonic()
    with _SUMMARY_LOCK:
        cached = _SUMMARY_CACHE.get("entry")
        if (cached is not None and cached["stamp"] == stamp
                and now_mono - cached["at"] <= SUMMARY_TTL):
            return dict(cached["value"])
    value = _build_summary(hours, None)
    with _SUMMARY_LOCK:
        _SUMMARY_CACHE["entry"] = {"stamp": stamp, "at": time.monotonic(),
                                   "value": dict(value)}
    return value
