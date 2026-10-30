"""Перенос настроек между профилями: экспорт и импорт JSON.

Файл переноса содержит настройки (только ключи из ``config.DEFAULTS``),
маршруты сайтов и запомненные сети. По умолчанию секретов в нём нет:
подписка и свои конфиги — это ключи доступа, и они попадают в файл лишь с
явным ``include_secrets=True``.

Сетевых запросов модуль не делает: только чтение и запись локальных файлов.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from . import config, networks, routes

#: Версия формата файла переноса. Импорт понимает только её.
FORMAT_VERSION = 1
APP_NAME = config.APP_NAME

#: Ключи ``DEFAULTS``, в которых лежат доступы к серверам. В safe-экспорт
#: не попадают никогда: ``xray_links`` — сами ссылки с UUID, а
#: ``xray_subscription`` — персональный адрес подписки.
SECRET_KEYS = ("xray_links", "xray_subscription")

#: Секция настроек, которая живёт отдельно от ``settings`` в файле.
ROUTES_KEY = "xray_routes"


def _settings_from(cfg: dict, include_secrets: bool) -> dict:
    """Настройки для файла: только известные ключи, без секретов в safe-режиме."""
    settings: dict = {}
    for key in config.DEFAULTS:
        if key == ROUTES_KEY:
            continue  # отдельной секцией, в каноничном виде
        if not include_secrets and key in SECRET_KEYS:
            continue
        if key in cfg:
            settings[key] = cfg[key]
    return settings


def _routes_payload(cfg: dict) -> dict:
    """``xray_routes`` из конфига в каноничном виде (после нормализации)."""
    mapping = routes.normalise_routes(cfg or {})
    return routes.routes_payload(
        {name: info["route"] for name, info in mapping.items()},
        {name: info["server"] for name, info in mapping.items()},
    )


def export_settings(cfg: dict, path, include_secrets: bool = False) -> dict:
    """Пишет файл переноса. Возвращает отчёт: путь, число правил и сетей.

    ``include_secrets=True`` добавляет в файл ``xray_links`` и
    ``xray_subscription`` — это ключи доступа, и файл после этого равен
    резервной копии доступов.
    """
    target = Path(path)
    cfg = dict(cfg or {})
    settings = _settings_from(cfg, include_secrets)
    payload = {
        "version": FORMAT_VERSION,
        "at": time.time(),
        "app": APP_NAME,
        "secrets_included": bool(include_secrets),
        "settings": settings,
        ROUTES_KEY: _routes_payload(cfg),
        "networks": networks.all(),
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if not config.atomic_write(target, text):
        return {"ok": False, "path": str(target),
                "error": config.last_write_error() or "не удалось записать файл",
                "secrets_included": bool(include_secrets)}
    return {
        "ok": True,
        "path": str(target),
        "secrets_included": bool(include_secrets),
        "keys": sorted(settings),
        "routes": len(payload[ROUTES_KEY]),
        "networks": len(payload["networks"]),
    }


def _backup_config() -> str:
    """Копия текущего config.json рядом, ``*.bak-<время>``.

    Копия, а не переименование: импорт должен оставить рабочий конфиг на
    месте, даже если запись нового не удастся.
    """
    source = config.CONFIG_FILE
    if not source.exists():
        return ""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    target = source.with_name(f"{source.name}.bak-{stamp}")
    number = 1
    while target.exists():
        target = source.with_name(f"{source.name}.bak-{stamp}-{number}")
        number += 1
    try:
        shutil.copy2(source, target)
    except OSError:
        return ""
    return str(target)


def _read_payload(path) -> tuple[dict | None, str]:
    """Разобранный файл переноса или причина отказа."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"не прочитать файл: {type(exc).__name__}: {exc}"
    try:
        data = json.loads(text)
    except ValueError:
        return None, "файл не является JSON"
    if not isinstance(data, dict):
        return None, "чужой формат: ожидался объект JSON"
    if data.get("app") != APP_NAME:
        return None, "чужой формат: это не файл настроек «Обходов»"
    version = data.get("version")
    if not isinstance(version, int) or not 1 <= version <= FORMAT_VERSION:
        return None, "неизвестная версия файла переноса"
    if not isinstance(data.get("settings"), dict):
        return None, "чужой формат: нет раздела settings"
    return data, ""


def import_settings(path, apply: bool = True) -> dict:
    """Читает файл переноса и применяет его к текущему профилю.

    Меняются только ключи из ``DEFAULTS``; неизвестные перечисляются в
    ``skipped`` и не трогают конфиг. Маршруты проходят через
    ``routes.normalise_routes``, сети — через ``networks.remember``, то есть
    валидацию и атомарную запись дают существующие функции.

    ``apply=False`` — только отчёт о том, что было бы применено.
    """
    data, error = _read_payload(path)
    if data is None:
        return {"ok": False, "error": error, "applied": [], "skipped": [],
                "backup": "", "secrets_included": False}

    current = config.load_config()
    applied: list[str] = []
    skipped: list[str] = []
    for key, value in data["settings"].items():
        if key not in config.DEFAULTS or key == ROUTES_KEY:
            skipped.append(key)
            continue
        current[key] = value
        applied.append(key)

    raw_routes = data.get(ROUTES_KEY)
    route_count = 0
    if ROUTES_KEY in data:
        if isinstance(raw_routes, dict):
            normalised = routes.normalise_routes({ROUTES_KEY: raw_routes})
            current[ROUTES_KEY] = routes.routes_payload(
                {name: info["route"] for name, info in normalised.items()},
                {name: info["server"] for name, info in normalised.items()},
            )
            route_count = len(normalised)
            applied.append(ROUTES_KEY)
        else:
            skipped.append(ROUTES_KEY)

    raw_networks = data.get("networks")
    network_items: list[tuple[str, dict]] = []
    if "networks" in data:
        if isinstance(raw_networks, dict):
            for key, info in raw_networks.items():
                if not isinstance(key, str) or not key.strip():
                    continue
                if not isinstance(info, dict):
                    continue
                network_items.append((key.strip(), info))
            applied.append("networks")
        else:
            skipped.append("networks")

    report = {
        "ok": True,
        "applied": applied,
        "skipped": skipped,
        "backup": "",
        "secrets_included": bool(data.get("secrets_included")),
        "routes": route_count,
        "networks": len(network_items),
        "path": str(path),
    }
    if not apply:
        report["dry_run"] = True
        return report

    backup = _backup_config()
    report["backup"] = backup
    if not config.save_config(current):
        report["ok"] = False
        report["error"] = ("config.json не записан: "
                           + (config.last_write_error() or "файл недоступен"))
        return report

    # Сети — после конфига: если конфиг не записался, профиль не должен
    # остаться наполовину применённым.
    if network_items:
        for key, info in network_items:
            networks.remember(
                key, str(info.get("bat") or ""),
                {"ssid": str(info.get("ssid") or ""),
                 "gateway": str(info.get("gateway") or "")},
                server=str(info.get("server") or ""),
            )
    return report
