"""Маршрут Telegram: через Xray (SOCKS5) или tg-ws-proxy (MTProto).

Режим хранится в ``config.json`` (``telegram_mode``). Трей управляет только
серверной стороной: поднимает нужный прокси и гасит лишний. Какой адрес
выбрать в настройках Telegram Desktop, решает пользователь — модуль отдаёт
готовую подсказку, но секрет MTProto-прокси не читает и не пишет в журнал.
"""

from __future__ import annotations

import time

from . import actions, config
from .status import port_open

#: Предел ожидания порта 1443 при переключении режима: холодный exe
#: открывает порт медленнее, чем штатные 4 с после запуска из панели.
TGWS_START_TIMEOUT = 15.0
#: Сколько ждать фактического завершения старого tg-ws-proxy и шаг опроса.
TGWS_STOP_TIMEOUT = 5.0
TGWS_STOP_STEP = 0.2


def xray_port(cfg: dict) -> int:
    try:
        return int((cfg or {}).get("xray_port") or 10818)
    except (TypeError, ValueError):
        return 10818


def tgws_port(cfg: dict) -> int:
    return actions.tgws_port(cfg or {})


def endpoint(cfg: dict, mode: str) -> str:
    """Адрес прокси для текущего режима — то, что вводится в Telegram."""
    port = xray_port(cfg) if config.normalise_telegram_mode(mode) == "xray" \
        else tgws_port(cfg)
    return f"127.0.0.1:{port}"


def hint(cfg: dict, mode: str) -> str:
    """Что выбрать в настройках Telegram. Секрет tgws не раскрывается."""
    if config.normalise_telegram_mode(mode) == "tgws":
        return ("Telegram: выберите MTProto-прокси "
                f"127.0.0.1:{tgws_port(cfg)}; секрет показан в окне tg-ws-proxy")
    return ("Telegram: выберите SOCKS5 "
            f"127.0.0.1:{xray_port(cfg)}, MTProto-прокси {tgws_port(cfg)} отключите")


def _apply_xray(cfg: dict, start_xray: bool) -> dict:
    from . import xray  # локально: xray импортирует actions

    if xray.is_running():
        if xray.has_rule():
            state = "already"
        else:
            result = xray.restart(cfg)
            if not result.get("ok"):
                return {"ok": False, "state": "failed",
                        "error": "Xray не перезапустился: "
                                 + str(result.get("error") or "?")}
            state = "restarted"
    elif start_xray:
        result = xray.start(cfg)
        if not result.get("ok"):
            return {"ok": False, "state": "failed",
                    "error": "Xray не запустился: "
                             + str(result.get("error") or "?")}
        state = "started"
    else:
        # Старт трея не подменяет автоподключение VPN: здесь только следим,
        # чтобы tg-ws-proxy не спорил с выбранным маршрутом.
        state = "off"

    if not xray.has_rule():
        detail = "xray-config.json устарел: нет правила geoip:telegram"
        if state == "off":
            return {"ok": False, "state": state,
                    "error": detail + " — оно появится после подключения VPN"}
        return {"ok": False, "state": state, "error": detail}

    stopped = False
    if actions.tgws_running():
        result = actions.stop_tgws()
        if result.get("denied"):
            return {"ok": False, "state": state,
                    "error": "tg-ws-proxy не остановился: нет прав"}
        stopped = bool(result.get("killed"))
    return {"ok": True, "state": state, "stopped_tgws": stopped,
            "changed": stopped or state in ("started", "restarted")}


def _apply_tgws(cfg: dict) -> dict:
    port = tgws_port(cfg)
    running = actions.tgws_running()
    if running and port_open(port):
        return {"ok": True, "state": "already", "changed": False}

    if running:
        # Процесс есть, а порт закрыт: мягко завершаем и ждём фактического
        # исчезновения — иначе start_tgws примет живой старый процесс за отказ.
        actions.stop_tgws()
        deadline = time.monotonic() + TGWS_STOP_TIMEOUT
        while actions.tgws_running() and time.monotonic() < deadline:
            time.sleep(TGWS_STOP_STEP)
        if actions.tgws_running():
            return {"ok": False, "state": "failed",
                    "error": "tg-ws-proxy не завершился за "
                             f"{TGWS_STOP_TIMEOUT:g} с"}

    exe = str((cfg or {}).get("tgws_exe") or "")
    if not exe:
        return {"ok": False, "state": "failed",
                "error": "не найден tg-ws-proxy: путь не задан"}
    try:
        started = actions.start_tgws(cfg, timeout=TGWS_START_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 — текст отказа показываем как есть
        return {"ok": False, "state": "failed", "error": str(exc)}
    if not started:
        return {"ok": False, "state": "failed",
                "error": f"tg-ws-proxy не слушает порт {port}"}
    return {"ok": True, "state": "started", "changed": True}


def apply(cfg: dict, mode: str, start_xray: bool = True) -> dict:
    """Приводит серверную сторону к режиму ``mode``.

    ``start_xray=False`` — стартовая синхронизация: Xray не поднимается,
    если пользователь не подключил VPN сам (это делает автоподключение).
    Возвращает ``{"ok", "mode", "endpoint", "state", "changed", "hint"}``;
    ``error`` заполняется при отказе.
    """
    mode = config.normalise_telegram_mode(mode)
    result = (_apply_tgws(cfg) if mode == "tgws"
              else _apply_xray(cfg, start_xray))
    result.update({
        "mode": mode,
        "endpoint": endpoint(cfg, mode),
        "hint": hint(cfg, mode),
    })
    return result
