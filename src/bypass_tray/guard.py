"""Надзиратель: сам поднимает то, что можно поднять без UAC.

Не запускает ничего с повышением прав. zapret (winws.exe) требует
администратора, поэтому о его остановке виджет только предупреждает.

Xray надзиратель **не перезапускает**: смена маршрутов сохраняется в
оверрайды и применится при следующем подключении. Иначе фоновый аудит рвал бы
живой туннель посреди работы. Явное применение — только кнопка UI
(``routes.apply_now``).

Исключение — мёртвый сервер: если проба через туннель провалилась
(``health`` вернул ``vpn: bad``), надзиратель переключается на другой сервер,
но не чаще раза в 30 минут.

Любая ошибка одного шага превращается в предупреждение: надзиратель не должен
ронять приложение. Одинаковые предупреждения пишутся в журнал не чаще раза в
час.
"""

from __future__ import annotations

import time
from pathlib import Path

from . import actions, activity, config, networks, routes, status

ZAPRET_WARN = "zapret остановлен — запустите вручную"
#: Одно и то же предупреждение — не чаще раза в час.
_REPEAT_SECONDS = 3600
#: Сколько ждать открытия порта после запуска tg-ws-proxy.
_TGWS_PORT_WAIT = 3.0
#: Эскалация: до пяти разных серверов. После каждого провала пауза растёт
#: 15 → 30 → 60 минут: живой сервер может не ответить один раз, а гонять
#: серверы по кругу нельзя — это рвёт уже установленные соединения.
VPN_MAX_SERVERS = 5
VPN_BACKOFFS = (15 * 60, 30 * 60, 60 * 60)
_VPN_SWITCH_AT = 0.0          # время последней попытки переключения
_VPN_ATTEMPTS = 0             # сколько серверов уже перепробовано
_VPN_FAILED: list[str] = []   # имена серверов, с которых ушли
_VPN_NETWORK = ""             # сеть, для которой считается эскалация
_VPN_OVERRIDE: set[str] = set()  # сервисы, уже уведённые через VPN
_VPN_HEAL_DELAY = "VPN не отвечает — переключение сервера отложено"


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
    # Маршрут Telegram через Xray: tg-ws-proxy выключен намеренно, а не упал.
    if config.normalise_telegram_mode(cfg.get("telegram_mode")) != "tgws":
        return []
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


def _heal_zapret(status_state: dict, user_off: bool = False) -> list[dict]:
    zapret = status_state.get("zapret") or {}
    if zapret.get("running"):
        return []
    if user_off:
        # Пользователь выключил zapret тумблером: это намеренное состояние,
        # а не сбой. Иначе предупреждение спорило бы с его выбором.
        return []
    # UAC из фона не поднимаем: winws стартует только от администратора.
    _log_throttled(ZAPRET_WARN)
    return [_action("warn", ZAPRET_WARN)]


def _vpn_backoff() -> float:
    """Пауза перед следующей попыткой по числу уже перепробованных серверов."""
    index = min(max(_VPN_ATTEMPTS - 1, 0), len(VPN_BACKOFFS) - 1)
    return float(VPN_BACKOFFS[index])


def _vpn_switch_allowed() -> bool:
    """Прошла ли пауза с прошлой попытки переключить сервер."""
    return time.time() - _VPN_SWITCH_AT >= _vpn_backoff()


def reset_escalation() -> None:
    """Счётчик серверов и бэкофф — заново (успех или смена сети)."""
    global _VPN_ATTEMPTS, _VPN_FAILED, _VPN_SWITCH_AT, _VPN_OVERRIDE
    _VPN_ATTEMPTS = 0
    _VPN_FAILED = []
    _VPN_SWITCH_AT = 0.0
    _VPN_OVERRIDE = set()


def escalation_state() -> dict:
    """Срез эскалации — для панели и тестов."""
    return {
        "attempts": _VPN_ATTEMPTS,
        "max": VPN_MAX_SERVERS,
        "backoff": int(_vpn_backoff()),
        "next_at": _VPN_SWITCH_AT + _vpn_backoff(),
        "failed": list(_VPN_FAILED),
        "network": _VPN_NETWORK,
        "overridden": sorted(_VPN_OVERRIDE),
    }


def _sync_network() -> bool:
    """Сбрасывает эскалацию при смене сети. ``True`` — сеть сменилась.

    Сеть определяется ``networks.current`` (MAC/шлюз/SSID); ошибка получения
    ключа не сбрасывает счётчик — иначе каждый сбой ipconfig обнулял бы
    бэкофф.
    """
    global _VPN_NETWORK
    try:
        key = str((networks.current() or {}).get("key") or "")
    except Exception:  # noqa: BLE001
        return False
    if not key:
        return False
    changed = bool(_VPN_NETWORK) and key != _VPN_NETWORK
    if changed:
        reset_escalation()
    _VPN_NETWORK = key
    return changed


def _server_ids(row: dict | None) -> set[str]:
    """Опознание сервера для исключения: и ``host``, и ``name``.

    ``xray._switch_server`` раньше фильтровал только по ``host``, поэтому
    список имён в guard не работал. Держим оба поля и там, и там.
    """
    row = row or {}
    return {str(row.get(key) or "").strip()
            for key in ("host", "name")} - {""}


def _broken_services(health: dict | None) -> list[str]:
    """Сломанные сервисы из отчёта: их и уводим через VPN после эскалации."""
    states = {}
    for item in (health or {}).get("items") or []:
        if isinstance(item, dict):
            states[str(item.get("id") or "")] = str(item.get("state") or "")
    return [name for name in ("youtube", "discord", "telegram")
            if states.get(name) == "bad"]


def _escalate_override(cfg: dict, health: dict | None) -> list[dict]:
    """Серверы кончились: проблемные сервисы — через VPN (маршруты в файле).

    Xray надзиратель не перезапускает, поэтому оверрайд применится при
    следующем подключении — как и решения аудита маршрутов.
    """
    broken = _broken_services(health)
    if not broken:
        return [_action("warn", f"VPN: перебраны {VPN_MAX_SERVERS} серверов, "
                                "сломанных сервисов в отчёте нет")]
    key = ",".join(broken)
    if key in _VPN_OVERRIDE:
        return []
    try:
        result = routes.enable_service_proxy(broken, cfg)
    except Exception as exc:  # noqa: BLE001
        return [_action("warn", f"VPN: оверрайд маршрута не записан: {exc}")]
    if not isinstance(result, dict):
        # Совместимость с прежним контрактом (список изменённых доменов).
        result = {"ok": True, "changed": list(result or []),
                  "status": "changed" if result else "unchanged"}
    if not result.get("ok"):
        # Сбой записи/снимка/битый файл — не «уже настроено»: не закрепляем
        # _VPN_OVERRIDE и не врём в журнале, чтобы следующая попытка была.
        text = (f"VPN: оверрайд маршрута не записан "
                f"({result.get('status') or 'error'}): "
                f"{result.get('error') or 'неизвестная причина'}")
        activity.log(text, kind="warn")
        return [_action("warn", text)]
    _VPN_OVERRIDE.add(key)
    if result.get("changed"):
        text = (f"VPN: перебраны {VPN_MAX_SERVERS} серверов — "
                f"{key} переведены через VPN (маршруты)")
        kind = "ok"
    elif result.get("manual") or result.get("direct"):
        text = (f"VPN: перебраны {VPN_MAX_SERVERS} серверов — "
                f"{key}: маршрут уже задан вручную, не перебиваю")
        kind = "info"
    else:
        text = (f"VPN: перебраны {VPN_MAX_SERVERS} серверов — "
                f"{key} уже идут через VPN")
        kind = "info"
    activity.log(text, kind=kind)
    return [_action(kind, text)]


def _heal_vpn(cfg: dict, proxy: dict, health: dict | None) -> list[dict]:
    """Лечит мёртвый VPN: меняет серверы, затем уводит сервисы через VPN.

    Вызывается из ``heal`` с теми же данными, что уже собрал ``health.collect``:
    лишних сетевых проверок не делает. Между попытками — бэкофф 15/30/60 минут.
    После пяти серверов эскалация переходит на оверрайд маршрута. VPN сам не
    выключается: намерение пользователя (подключённый туннель) сохраняется.

    ``xray.switch_server`` возвращает ``ok`` только после запроса через
    поднятый туннель (TCP-контакт мёртвого сервера ничего не значит), а сам
    перебирает кандидатов и при провале обновляет список. Поэтому успех здесь
    означает рабочий сервер, а не очередной открытый порт.
    """
    global _VPN_SWITCH_AT, _VPN_ATTEMPTS
    if not (proxy or {}).get("running") or not health:
        return []
    _sync_network()
    vpn = next((i for i in (health.get("items") or []) if i.get("id") == "vpn"), None)
    if not vpn:
        return []
    if vpn.get("state") == "ok":
        # Сервер ответил — эскалация больше не нужна.
        if _VPN_ATTEMPTS:
            reset_escalation()
        return []
    if vpn.get("state") != "bad":
        return []
    if _VPN_ATTEMPTS >= VPN_MAX_SERVERS:
        return _escalate_override(cfg, health)
    if not _vpn_switch_allowed():
        _log_throttled(_VPN_HEAL_DELAY)
        return []

    current_ids = _server_ids({
        "host": (proxy or {}).get("host"),
        "name": (proxy or {}).get("server"),
    })
    for ident in sorted(current_ids):
        if ident not in _VPN_FAILED:
            _VPN_FAILED.append(ident)
    _VPN_SWITCH_AT = time.time()
    try:
        from . import xray

        result = xray.switch_server(exclude=set(_VPN_FAILED), cfg=cfg)
    except Exception as exc:  # noqa: BLE001
        _VPN_ATTEMPTS += 1
        return [_action("warn", f"переключение сервера не удалось: {exc}")]
    if not result.get("ok"):
        _VPN_ATTEMPTS += 1
        return [_action("warn", "не удалось переключить сервер: "
                        + str(result.get("error") or "?"))]
    _VPN_ATTEMPTS += 1
    server = result.get("server") or {}
    name = server.get("name") or server.get("host") or "другой"
    for ident in sorted(_server_ids(server)):
        if ident not in _VPN_FAILED:
            _VPN_FAILED.append(ident)
    text = f"переключил сервер ({_VPN_ATTEMPTS}/{VPN_MAX_SERVERS}): {name}"
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
        applied = routes.apply_audit(result, cfg, reason="auto")
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
         health: dict | None = None, zapret_off: bool = False) -> list[dict]:
    """Лечит то, что можно вылечить без UAC. Возвращает список действий."""
    if not (cfg or {}).get("guard_enabled", True):
        return []
    try:
        from . import strategy

        if strategy.trial_active():
            # Перебор сам гасит winws и меняет серверы: лечить нечего.
            return []
    except Exception:  # noqa: BLE001
        pass
    # Смена сети обнуляет эскалацию даже раньше, чем снова понадобится VPN.
    _sync_network()
    status_state = status_state or {}
    proxy = proxy or {}
    done: list[dict] = []
    for step in (
        lambda: _heal_tgws(cfg or {}, status_state),
        # VPN — по уже собранному отчёту: сервер мёртв, значит нужен другой.
        lambda: _heal_vpn(cfg or {}, proxy, health),
        lambda: _heal_zapret(status_state, zapret_off),
        lambda: _heal_routes(cfg or {}),
    ):
        try:
            done.extend(step())
        except Exception as exc:  # noqa: BLE001
            done.append(_action("warn", f"надзиратель: {type(exc).__name__}"))
    return done
