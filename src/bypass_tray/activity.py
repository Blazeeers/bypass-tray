"""Журнал действий виджета: что он сделал сам.

Виджет в фоне запускает tg-ws-proxy, меняет сервер, обновляет маршруты.
Пользователь должен видеть эти действия, не листая консоль: журнал лежит
рядом с конфигом (`%APPDATA%\\bypass-tray\\activity.json`) и хранит последние
60 записей. Запись атомарная (tmp-файл + ``os.replace``), чтобы чтение из
другого потока не поймало половину JSON.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .config import atomic_write, config_dir
from .secrets import redact_text

#: Сколько записей хранить. Старые вытесняются при каждой новой.
LIMIT = 60

_LOCK = threading.Lock()


def path() -> Path:
    return config_dir() / "activity.json"


def _read() -> list[dict]:
    """Записи журнала в порядке добавления (старые сверху)."""
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]


def _write(rows: list[dict]) -> None:
    """Атомарная запись журнала; ошибки файловой системы молча пропускаем."""
    atomic_write(path(), json.dumps(rows, ensure_ascii=False, indent=2))


def log(text: str, kind: str = "auto") -> None:
    """Добавляет запись в журнал. ``kind`` — auto | ok | warn | info."""
    entry = {"at": time.time(), "text": redact_text(str(text)),
             "kind": str(kind or "auto")}
    with _LOCK:
        _write((_read() + [entry])[-LIMIT:])


def _safe(row: dict) -> dict:
    """Запись с безопасным текстом.

    ``log`` маскирует при записи, но старые ``activity.json`` писались до
    этого: файл на диске не переписываем, маскируем при чтении.
    """
    text = redact_text(str(row.get("text") or ""))
    if text == row.get("text"):
        return row
    masked = dict(row)
    masked["text"] = text
    return masked


def recent(limit: int = 6, hours: float | None = None) -> list[dict]:
    """Последние записи (новые сверху), при ``hours`` — только за это время.

    История в файле не обрезается: фильтр действует только на чтение.
    """
    with _LOCK:
        rows = _read()
    if hours is not None:
        try:
            cutoff = time.time() - float(hours) * 3600
        except (TypeError, ValueError):
            cutoff = 0.0
        filtered = []
        for row in rows:
            try:
                at = float(row.get("at") or 0)
            except (TypeError, ValueError):
                at = 0.0
            if at >= cutoff:
                filtered.append(row)
        rows = filtered
    if limit <= 0:
        return []
    return [_safe(row) for row in reversed(rows[-limit:])]


def clear() -> None:
    """Очищает журнал."""
    with _LOCK:
        _write([])


def last_text() -> str:
    """Текст последней записи (с маскированием) или пустая строка."""
    with _LOCK:
        rows = _read()
    if not rows:
        return ""
    return redact_text(str(rows[-1].get("text") or ""))
