"""Надзиратель: сам поднимает то, что можно поднять без UAC.

Не запускает ничего с повышением прав. zapret (winws.exe) требует
администратора, поэтому о его остановке виджет только предупреждает.

Xray надзиратель **не перезапускает**: смена маршрутов сохраняется в
оверрайды и применится при следующем подключении. Иначе фоновый аудит рвал бы
живой туннель посреди работы. Явное применение — только кнопка UI
(``routes.apply_now``).

Любая ошибка одного шага превращается в предупреждение: надзиратель не должен
ронять приложение. Одинаковые предупреждения пишутся в журнал не чаще раза в
час.
"""

from __future__ import annotations

import time
from pathlib import Path

from . import actions, activity, routes, status

ZAPRET_WARN = "zapret остановлен — запустите вручную"
#: Одно и то же предупреждение — не чаще раза в час.
_REPEAT_SECONDS = 3600
#: Сколько ждать открытия порта после запуска tg-ws-proxy.
_TGWS_PORT_WAIT = 3.0


def overdue(last_ts, minutes: int) -> bool:
    """Пора ли повторять проверку: метки нет или она старше ``minutes``."""
    if not last_ts:
        return True
    try:
        last = float(last_ts)
    except (TypeError, ValueError):
        return True
    return time.time() - last >= max(0, int(minutes)) * 60


def _action(kind: str, text: str) -> dict:
    return {"kind": kind, "text": text}


def _logged_within(prefix: str, seconds: float) -> bool:
    """Есть ли в журнале запись с этим началом текста за последние ``seconds``."""
    limit = time.time() - seconds
    try:
        rows = activity.recent(activity.LIMIT)
    except Exception:  # noqa: BLE001
        return False
    for row in rows:
        if not str(row.get("text") or "").startswith(prefix):
            continue
        try:
            if float(row.get("at") or 0) >= limit:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _log_throttled(text: str, kind: str = "warn") -> None:
    """Пишет запись в журнал не чаще раза в час."""
    if not _logged_within(text, _REPEAT_SECONDS):
        activity.log(text, kind=kind)


def _tgws_port(cfg: dict) -> int:
    try:
        return int(cfg.get("tgws_port") or 1443)
    except (TypeError, ValueError):
        return 1443


def _heal_tgws(cfg: dict, status_state: dict) -> list[dict]:
    info = status_state.get("tgwsproxy") or {}
    if info.get("listening") or info.get("running"):
        return []
    exe = str(cfg.get("tgws_exe") or "")
    if not exe or not Path(exe).is_file():
        text = "tg-ws-proxy не установлен"
        _log_throttled(text, kind="info")
        return [_action("off", text)]

    try:
        started = actions.start_tgws(cfg)
    except Exception as exc:  # noqa: BLE001
        text = f"tg-ws-proxy не запустился: {exc}"
        _log_throttled(text)
        return [_action("warn", text)]

    port = _tgws_port(cfg)
    if not started:
        # Процесс уже был, но порт закрыт: это не успех запуска.
        text = f"tg-ws-proxy не слушает порт {port}"
        _log_throttled(text)
        return [_action("warn", text)]

    deadline = time.time() + _TGWS_PORT_WAIT
    while time.time() < deadline:
        if status.port_open(port):
            activity.log("запустил tg-ws-proxy", kind="ok")
            return [_action("ok", "запустил tg-ws-proxy")]
        time.sleep(0.2)
    text = f"tg-ws-proxy запущен, но порт {port} не открылся"
    _log_throttled(text)
    return [_action("warn", text)]


def _heal_zapret(status_state: dict) -> list[dict]:
    zapret = status_state.get("zapret") or {}
    if zapret.get("running"):
        return []
    # UAC из фона не поднимаем: winws стартует только от администратора.
    _log_throttled(ZAPRET_WARN)
    return [_action("warn", ZAPRET_WARN)]


def _heal_vpn(cfg: dict, proxy: dict, health: dict | None) -> list[dict]:
    if not proxy.get("running") or not health:
        return []
    vpn = next((i for i in (health.get("items") or []) if i.get("id") == "vpn"), None)
    if not vpn or vpn.get("state") != "bad":
        return []
    try:
        from . import xray

        result = xray.switch_server(cfg=cfg)
    except Exception as exc:  # noqa: BLE001
        return [_action("warn", f"переключение сервера не удалось: {exc}")]
    if not result.get("ok"):
        return [_action("warn", "не удалось переключить сервер: "
                        + str(result.get("error") or "?"))]
    server = result.get("server") or {}
    name = server.get("name") or server.get("host") or "другой"
    text = f"переключил сервер: {name}"
    activity.log(text, kind="ok")
    return [_action("ok", text)]


def _heal_routes(cfg: dict) -> list[dict]:
    """Аудит маршрутов по расписанию. Xray не перезапускается."""
    try:
        minutes = int(cfg.get("route_audit_minutes") or 60)
    except (TypeError, ValueError):
        minutes = 60
    if not routes.due(minutes):
        return []
    try:
        result = routes.audit(cfg)
        applied = routes.apply_audit(result, cfg)
    except Exception as exc:  # noqa: BLE001
        return [_action("warn", f"аудит маршрутов не удался: {exc}")]
    changed = applied.get("changed") or []
    if not changed:
        activity.log("проверил маршруты: изменений нет", kind="info")
        return [_action("info", "проверил маршруты: изменений нет")]
    activity.log("маршруты обновлены — применятся при следующем подключении", kind="ok")
    return [_action("ok", f"маршруты обновлены: {len(changed)} — "
                          "применятся при следующем подключении")]


def heal(cfg: dict, status_state: dict, proxy: dict,
         health: dict | None = None) -> list[dict]:
    """Лечит то, что можно вылечить без UAC. Возвращает список действий."""
    if not (cfg or {}).get("guard_enabled", True):
        return []
    status_state = status_state or {}
    proxy = proxy or {}
    done: list[dict] = []
    for step in (
        lambda: _heal_tgws(cfg or {}, status_state),
        lambda: _heal_zapret(status_state),
        lambda: _heal_routes(cfg or {}),
    ):
        try:
            done.extend(step())
        except Exception as exc:  # noqa: BLE001
            done.append(_action("warn", f"надзиратель: {type(exc).__name__}"))
    return done
