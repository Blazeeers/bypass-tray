"""Готовые сценарии настроек: один клик вместо нескольких переключателей.

Сценарий — это только значения реальных ключей ``config.DEFAULTS``.
``xray_routes``, ``xray_links`` и список серверов он не знает и не трогает:
какие сайты вести через VPN и каким сервером — выбор пользователя.

Модуль чистый: :func:`apply` не пишет файлов и не меняет переданный конфиг,
поэтому форму настроек можно менять сценарием до нажатия «Сохранить».
"""

from __future__ import annotations

from .config import DEFAULTS, normalise_routing_mode

#: Упорядоченный список сценариев.
#:
#: ``values`` содержит только ключи из ``config.DEFAULTS``. Каждый сценарий
#: задаёт полный набор «режим + два тумблера»: иначе один и тот же конфиг
#: подходил бы сразу под два сценария и подсветка чипа была бы случайной.
PRESETS: list[dict] = [
    {
        "id": "zapret",
        "title": "Под zapret",
        "hint": "Заблокированное из списка Re:filter идёт через VPN, а YouTube "
                "и Discord остаются под zapret. Так настроено по умолчанию.",
        "values": {
            "routing_mode": "blocked",
            "youtube_via_vpn": False,
            "blocked_list_enabled": True,
        },
    },
    {
        "id": "vpn",
        "title": "Всё через VPN",
        "hint": "Весь трафик, кроме РФ и локальной сети, идёт через VPN; YouTube "
                "принудительно через VPN. Общий список заблокированных в этом "
                "режиме не нужен — он выключен.",
        "values": {
            "routing_mode": "all",
            "youtube_via_vpn": True,
            "blocked_list_enabled": False,
        },
    },
    {
        "id": "telegram",
        "title": "Только Telegram",
        "hint": "Через VPN идут только ваши правила маршрутов — минимум трафика. "
                "Telegram работает через tg-ws-proxy, список заблокированных и "
                "YouTube через VPN выключены.",
        "values": {
            "routing_mode": "blocked",
            "youtube_via_vpn": False,
            "blocked_list_enabled": False,
        },
    },
    {
        "id": "max",
        "title": "Максимум обхода",
        "hint": "Всё через VPN и список заблокированных включён. В режиме «всё, "
                "кроме РФ» список и «YouTube через VPN» уже ничего не добавляют — "
                "они остаются включёнными на случай возврата в режим списка.",
        "values": {
            "routing_mode": "all",
            "youtube_via_vpn": True,
            "blocked_list_enabled": True,
        },
    },
]

_BY_ID: dict[str, dict] = {preset["id"]: preset for preset in PRESETS}


def get(preset_id) -> dict | None:
    """Сценарий по id или ``None``."""
    return _BY_ID.get(str(preset_id or "").strip())


def title(preset_id) -> str:
    """Название сценария для журнала; неизвестный id — пустая строка."""
    preset = get(preset_id)
    return str(preset["title"]) if preset else ""


def apply(cfg: dict, preset_id) -> dict:
    """Конфиг со значениями сценария — новый словарь, ``cfg`` не меняется.

    Переносятся все ключи как есть, включая ``xray_routes`` и ``xray_links``:
    сценарий их не касается. Неизвестный id — возвращается **тот же** объект
    ``cfg`` без изменений; проверка ``apply(cfg, "нет") is cfg`` и есть
    признак «не применено».
    """
    preset = get(preset_id)
    if preset is None or not isinstance(cfg, dict):
        return cfg
    merged = dict(cfg)
    merged.update(preset["values"])
    return merged


def _matches(cfg: dict, values: dict) -> bool:
    """Совпадает ли конфиг со значениями сценария по его ключам."""
    for key, want in values.items():
        got = cfg.get(key, DEFAULTS.get(key))
        if key == "routing_mode":
            if normalise_routing_mode(got) != want:
                return False
        elif isinstance(want, bool):
            # Строка из правленого вручную config.json не должна читаться
            # как «включено» только потому, что она непустая.
            if isinstance(got, str):
                got = got.strip().lower() in ("1", "true", "yes", "on")
            if bool(got) != want:
                return False
        elif got != want:
            return False
    return True


def active(cfg: dict) -> str | None:
    """id сценария, которому соответствует конфиг, иначе ``None``.

    Сравниваются только ключи сценария; отсутствующий ключ берётся из
    ``DEFAULTS``. Ничего не бросает: битое значение просто не совпадёт.
    """
    if not isinstance(cfg, dict):
        return None
    for preset in PRESETS:
        if _matches(cfg, preset["values"]):
            return str(preset["id"])
    return None
