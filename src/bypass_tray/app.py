"""Трей-приложение: иконка, меню, фоновое обновление статусов."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

try:
    import pystray
except ImportError:  # pragma: no cover
    pystray = None

from . import (
    actions, activity, autostart, config, diagnose, guard, health, history, icons,
    networks, routes, secrets, status, strategy, updates, xray,
)
from .theme import STATE_COLORS
from .ui import Panel, clean_server_name, report_level



#: После автосмены сервера не трогаем его 10 минут: частые перезапуски
#: Xray рвут уже установленные соединения (в том числе YouTube).
AUTO_SWITCH_COOLDOWN = 600.0

#: Не чаще раза в минуту поднимаем упавший tg-ws-proxy: каждый запуск ждёт
#: порт до 4 с, а частые перезапуски только мешают.
TGWS_RESTART_COOLDOWN = 60.0

#: Подписи пунктов для подсказки иконки (как в панели).
_LEVEL_LABELS = {
    "zapret": "zapret",
    "youtube": "YouTube",
    "discord": "Discord",
    "deepseek": "DeepSeek",
    "vpn": "VPN",
    "telegram": "Telegram",
    "tgwsproxy": "tg-ws-proxy",
}
#: Второстепенные пункты: их bad деградирует, а не красит в красный.
_SECONDARY_LABELS = {"telegram", "tgwsproxy"}
#: Подписи действий меню, ушедших в фоновый поток (ключ — имя функции).
_ACTION_TEXTS = {
    "start_zapret": "запускаю zapret…",
    "stop_zapret": "останавливаю zapret…",
    "restart_zapret": "перезапускаю zapret…",
    "control_service": "служба zapret…",
    "open_service_bat": "открываю стратегию службы…",
    "start_tgws": "запускаю tg-ws-proxy…",
    "stop_tgws": "останавливаю tg-ws-proxy…",
}

class TrayApp:
    def __init__(self):
        self.cfg = config.load_config()
        self.state = config.load_state()
        self.status: dict = {}
        self.last_check = 0.0
        self.sweep: dict = {"running": False, "message": ""}
        self.proxy: dict = {"running": False, "server": "", "port": xray.DEFAULT_PORT}
        self._stream_since: float | None = None
        self.servers: list = xray.cached_servers()
        self.server_selected: str = ""
        self._servers_loading = False
        self._failed_servers: set[str] = set()
        self._proxy_failures = 0
        self._health_checking = False
        self._health_tick = 0
        self._last_auto_switch = 0.0
        # --- сторож tg-ws-proxy ---
        self._last_tgws_restart = 0.0
        # Ручная остановка из меню: пока флаг стоит, сами не поднимаем.
        self._tgws_user_off = False
        # Мы уже сказали «упал»: вернуть ok, когда процесс снова слушает.
        self._tgws_down = False
        self.panel = Panel(self)
        self._icon = None
        self._signature = None
        self._checking = False
        # Таймер автоподключения и флаг выхода: после stop() ничего не поднимаем.
        self._autoconnect_timer: threading.Timer | None = None
        self._shutting_down = False

        # --- данные проверок и фоновых действий ---
        self.health_report: dict = {}
        self.health_at: float = 0.0
        self.health_running = False
        self.guard_at: float = 0.0
        self.guard_running = False
        self.route_report: dict = {}
        self.route_running = False
        #: Отчёт, который показан в диалоге подтверждения: применяем ровно его.
        self.route_plan_report: dict | None = None
        self.updates_report: dict = {}
        self.updates_running = False
        self.activity_items: list = []
        self.busy: dict[str, str] = {}
        self.updated_at: float = 0.0
        self._prev_verdict = ""
        # --- трёхцветный уровень иконки и гистерезис ---
        self._level: str = "off"
        self._level_candidate: str = ""
        self._level_hits = 0
        self._level_at = 0.0
        self._user_off = False
        cached = health.load_cache(self.cfg)
        if cached:
            # Свежий отчёт с прошлого запуска: не показываем «нет данных».
            self.health_report = cached
            try:
                self.health_at = float(cached.get("checked") or 0)
            except (TypeError, ValueError):
                self.health_at = 0.0
            self._level = report_level(cached) or "ok"
            self._level_at = time.time()
        self._net_key = ""
        self._net_checked = 0.0
        # Последние отправленные авто-тосты: (время, текст) по ключу.
        self._notified: dict[str, tuple[float, str]] = {}
        # Последние предупреждения в журнал: (время, текст) по ключу.
        self._logged: dict[str, tuple[float, str]] = {}

    # ---- прокси Xray -------------------------------------------------------
    def set_proxy(self, enable: bool) -> None:
        """Поднимает или гасит собственный прокси Xray."""
        if self.proxy.get("busy"):
            return
        self._user_off = not enable
        self._reset_health()
        self.proxy = {**self.proxy, "busy": True,
                      "message": "подключаю…" if enable else "отключаю…"}
        self._signature = None

        def worker() -> None:
            try:
                if enable:
                    result = xray.start(self.cfg)
                else:
                    result = xray.stop()
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "error": str(exc)}
            if not isinstance(result, dict):
                result = {"ok": False, "error": str(result)}
            # Хвост целиком в try/finally: исключение здесь не должно оставить
            # ``busy`` включённым — иначе кнопки залипают до перезапуска.
            try:
                if result.get("ok"):
                    if enable:
                        # ``server`` приходит строкой в ветке «уже запущен»:
                        # один ``.get`` по строке убил бы поток.
                        srv = result.get("server")
                        if isinstance(srv, dict):
                            name = str(srv.get("name") or "")
                            host = str(srv.get("host") or "")
                            latency = srv.get("latency", -1)
                        else:
                            name, host, latency = str(srv or ""), "", -1
                        self.proxy = {
                            "running": True, "busy": False,
                            "message": f"подключено · {name}",
                            "server": name,
                            "host": host,
                            "latency": latency,
                            "port": result.get("port", xray.DEFAULT_PORT),
                        }
                        self.server_selected = name
                        activity.log(f"подключил VPN: {name or 'сервер'}", kind="ok")
                    else:
                        self.proxy = {"running": False, "busy": False,
                                      "message": "отключено",
                                      "server": "", "port": xray.DEFAULT_PORT}
                        activity.log("отключил VPN")
                    self.refresh_activity()
                    self.notify(self.proxy["message"])
                else:
                    error = str(result.get("error", "не удалось"))
                    self.proxy = {**self.proxy, "busy": False,
                                  "message": "не удалось: " + error}
                    self.notify(error)
            except Exception as exc:  # noqa: BLE001
                activity.log(f"подключение сорвалось: {exc}", kind="warn")
                self.notify(f"Подключение не удалось: {type(exc).__name__}")
            finally:
                self.proxy = {**self.proxy, "busy": False}
                self._signature = None
                self.refresh()

        threading.Thread(target=worker, daemon=True).start()

    def restart_proxy(self) -> bool:
        """Последовательно гасит и поднимает Xray — без таймера-догадки.

        Подписка заново не качается: берём сохранённый сервер, а `switch_to`
        сохраняет режим потока и extra из ``xray-state.json``. Флаги занятости
        снимаются в ``finally``, чтобы кнопка не залипла при исключении.
        """
        if self.proxy.get("busy") or self.busy.get("proxy"):
            return False
        self._reset_health()
        self.proxy = {**self.proxy, "busy": True, "message": "перезапускаю прокси…"}
        self.busy["proxy"] = "перезапускаю прокси…"
        self._signature = None
        self.update_icon()

        def worker() -> None:
            try:
                saved = xray.saved_server()
                xray.stop(port=int(self.proxy.get("port") or 10818))
                time.sleep(0.5)
                if saved is not None:
                    result = xray.switch_to(saved, self.cfg)
                else:
                    result = xray.start(self.cfg)
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "error": str(exc)}
            try:
                if result.get("ok"):
                    server = result.get("server") or {}
                    name = str(server.get("name") or server.get("host") or "")
                    self.proxy = {
                        "running": True, "busy": False,
                        "message": f"подключено · {name}",
                        "server": name,
                        "host": server.get("host", ""),
                        "latency": server.get("latency", -1),
                        "port": result.get("port", xray.DEFAULT_PORT),
                    }
                    self.server_selected = name
                    activity.log(f"перезапустил VPN: {name}", kind="ok")
                    self.refresh_activity()
                    self.notify(f"Прокси перезапущен · {name}" if name else "Прокси перезапущен")
                else:
                    error = str(result.get("error") or "неизвестная ошибка")
                    self.proxy = {**self.proxy, "running": False, "busy": False,
                                  "message": f"не удалось: {error}"}
                    self.notify("Прокси не перезапустился: " + error)
            finally:
                self.proxy = {**self.proxy, "busy": False}
                self.busy.pop("proxy", None)
                self._signature = None
                self.refresh()

        threading.Thread(target=worker, daemon=True, name="proxy-restart").start()
        return True

    def _notify_throttled(self, key: str, message: str, hours: float = 1.0) -> None:
        """Авто-тост не чаще раза в час; при ``quiet_icon`` не показываем вовсе."""
        if self.cfg.get("quiet_icon", True) or not message:
            return
        now = time.time()
        last = self._notified.get(key)
        if last and last[1] == message and now - last[0] < hours * 3600:
            return
        self._notified[key] = (now, message)
        self.notify(message)

    def _log_throttled(self, key: str, message: str, hours: float = 1.0) -> None:
        """Предупреждение в журнал не чаще раза в ``hours`` на ту же причину.

        В отличие от ``_notify_throttled`` пишет всегда, даже при ``quiet_icon``:
        сбой, о котором пользователь узнает только по отсутствию прокси, обязан
        оставить след.
        """
        if not message:
            return
        now = time.time()
        last = self._logged.get(key)
        if last and last[1] == message and now - last[0] < hours * 3600:
            return
        self._logged[key] = (now, message)
        activity.log(message, kind="warn")

    def load_servers(self, force: bool = False) -> None:
        """Обновляет список серверов в фоне (сеть + проверка доступности)."""
        if self.servers and not force:
            return
        if self._servers_loading:
            return
        self._servers_loading = True
        self.busy["servers"] = "обновляю список…"
        self._signature = None

        def worker() -> None:
            try:
                rows = xray.scan_servers()
                if rows:
                    self.servers = rows
            except Exception as exc:  # noqa: BLE001
                # Молчать нельзя: список остаётся пустым без объяснения.
                activity.log(f"список серверов не обновился: {exc}", kind="warn")
            finally:
                self._servers_loading = False
                self.busy.pop("servers", None)
                self._signature = None
                self.update_icon()

        threading.Thread(target=worker, daemon=True).start()

    def select_server(self, name: str) -> None:
        """Переключает прокси на выбранный сервер.

        Панель показывает имя без флагов, поэтому принимаем и «сырое» имя, и
        очищенное: сравниваем оба варианта.
        """
        if self.proxy.get("busy") or self.busy.get("proxy"):
            return
        wanted = clean_server_name(name)
        row = next(
            (candidate for candidate in self.servers
             if candidate.get("name") == name
             or clean_server_name(candidate.get("name") or "") == wanted),
            None,
        )
        if row is None:
            self.notify("Сервер не найден — обновите список")
            return
        raw_name = str(row.get("name") or name)
        self._reset_health()
        self.proxy = {**self.proxy, "busy": True, "message": f"переключаю на {raw_name}…"}
        self.busy["proxy"] = "переключаю сервер…"
        self._signature = None

        def worker() -> None:
            try:
                server = xray.parse_link(row["link"])
                server["latency"] = row.get("latency", -1)
                result = xray.switch_to(server, self.cfg)
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "error": str(exc)}
            if result.get("ok"):
                self.proxy = {**self.proxy, "busy": False,
                              "message": f"подключено · {raw_name}"}
                self.server_selected = raw_name
                activity.log(f"выбрал сервер: {raw_name}", kind="ok")
                self.refresh_activity()
                self._remember_network_server(raw_name)
                self.notify(f"Сервер: {raw_name}")
            else:
                self.proxy = {**self.proxy, "busy": False,
                              "message": "не удалось: " + str(result.get("error", "?"))}
            self.busy.pop("proxy", None)
            self.refresh()

        threading.Thread(target=worker, daemon=True).start()

    def check_proxy_health(self) -> None:
        """Проверяет туннель и меняет сервер при устойчивом отказе.

        Раз в 60 секунд (каждый 12-й тик), запрос идёт через VPN на
        ``api.telegram.org`` — он стабильнее Instagram. Сервер меняется
        только после трёх неудач подряд; успех сбрасывает счётчик. После
        любой попытки смены — кулдаун 10 минут, иначе перезапуски Xray рвут
        соединения YouTube.
        """
        # Сторож tg-ws-proxy смотрит только процесс и локальный порт, без сети,
        # поэтому идёт на каждом тике — и при выключенном VPN тоже.
        self._watch_tgws()
        if self.updates_running or (self.busy or {}).get("updates"):
            self._proxy_failures = 0
            return
        if self.proxy.get("busy") or not self.proxy.get("running"):
            self._proxy_failures = 0
            return
        if self._health_checking:
            return
        now = time.time()
        if now - getattr(self, "_last_auto_switch", 0.0) < AUTO_SWITCH_COOLDOWN:
            self._proxy_failures = 0
            return
        self._health_tick = getattr(self, "_health_tick", 0) + 1
        if self._health_tick % 12 != 0:      # примерно раз в 60 секунд
            return
        self._health_checking = True

        def worker() -> None:
            try:
                result = xray.verify(target="api.telegram.org", timeout=10)
            except Exception:  # noqa: BLE001
                result = {"ok": False}
            finally:
                self._health_checking = False
            if result.get("ok"):
                self._proxy_failures = 0
                self._apply_tunnel_level(0)
                return
            self._proxy_failures += 1
            self._apply_tunnel_level(self._proxy_failures)
            if self._proxy_failures < 3:
                return
            # Пока шла проверка, мог начаться кулдаун или смена сервера.
            if time.time() - getattr(self, "_last_auto_switch", 0.0) < AUTO_SWITCH_COOLDOWN:
                self._proxy_failures = 0
                return
            if self.proxy.get("busy") or not self.proxy.get("running"):
                self._proxy_failures = 0
                return
            self._proxy_failures = 0
            failed = self.proxy.get("host") or ""
            if failed:
                self._failed_servers.add(failed)
            self.proxy = {**self.proxy, "busy": True, "message": "сервер не отвечает, меняю…"}
            self._signature = None
            try:
                switched = xray.switch_server(self._failed_servers, self.cfg)
            except Exception as exc:  # noqa: BLE001
                switched = {"ok": False, "error": str(exc)}
            # Кулдаун ставим и при провале: иначе будем долбить смену каждую
            # минуту. ``failed_servers`` не сбрасываем, чтобы не выбрать тот же.
            self._last_auto_switch = time.time()
            if switched.get("ok"):
                # Новое подключение — старый вердикт к нему не относится.
                self._reset_health()
                server = switched.get("server") or {}
                name = str(server.get("name") or server.get("host") or "другой")
                self.proxy = {**self.proxy, "busy": False, **xray.status()}
                self.server_selected = str(server.get("name") or "")
                activity.log(f"переключил сервер: {name}", kind="ok")
                self.refresh_activity()
                self._notify_throttled("vpn-switch", f"Сервер заменён: {name}")
            else:
                self.proxy = {**self.proxy, "busy": False,
                              "message": "нет доступных серверов"}
                activity.log("не удалось переключить сервер", kind="warn")
                self.refresh_activity()
                self._notify_throttled("vpn-switch", "Не удалось найти рабочий сервер")
            self.refresh()

        threading.Thread(target=worker, daemon=True, name="tunnel-check").start()

    def _watch_tgws(self) -> None:
        """Быстрый сторож tg-ws-proxy: процесс и локальный порт, без сети.

        Отчёт health собирается раз в ``health_minutes`` (10 минут по
        умолчанию), и до него мёртвый прокси выглядел зелёным: порт 1443
        закрыт, а в ``health.json`` написано «слушает». Здесь падение
        замечается за тик (5 с): пункт помечается ``bad``, прокси
        перезапускается, итог уходит в журнал. Ручная остановка
        (``_tgws_user_off``) сторожа выключает — с пользователем не воюем.
        """
        if self._tgws_user_off or self._shutting_down:
            return
        # Эти проходы сами поднимают tgws (надзиратель, обновление, кнопка):
        # второй параллельный запуск нам не нужен.
        if self.health_running or self.guard_running:
            return
        if self.updates_running or (self.busy or {}).get("updates"):
            return
        if (self.busy or {}).get("tgws"):
            return
        exe = str(self.cfg.get("tgws_exe") or "").strip()
        if not exe or not Path(exe).is_file():
            return
        port = actions.tgws_port(self.cfg)
        if actions.tgws_running():
            # Процесс вернулся (его подняли вручную или надзиратель).
            if self._tgws_down and status.port_open(port):
                self._tgws_down = False
                self._mark_service("tgwsproxy", "ok", f"порт {port} слушает")
            return
        now = time.time()
        if now - self._last_tgws_restart < TGWS_RESTART_COOLDOWN:
            return
        # Кулдаун ставим до запуска: он ждёт порт до 4 с, и следующий тик не
        # должен стартовать второй процесс.
        self._last_tgws_restart = now
        self._tgws_down = True
        self._mark_service("tgwsproxy", "bad",
                           f"порт {port} не слушает, перезапускаю")
        try:
            started = actions.start_tgws(self.cfg)
        except Exception as exc:  # noqa: BLE001
            activity.log(f"tg-ws-proxy упал — перезапустить не удалось: {exc}",
                         kind="warn")
            self.refresh_activity()
            return
        if started:
            self._tgws_down = False
            activity.log("tg-ws-proxy упал — перезапустил", kind="ok")
            self._mark_service("tgwsproxy", "ok", f"порт {port} слушает")
        else:
            activity.log("tg-ws-proxy упал — перезапустить не удалось", kind="warn")
            # Причина уже не «перезапускаю»: попытка была и не удалась.
            self._mark_service("tgwsproxy", "bad", f"порт {port} не слушает")
        self.refresh_activity()

    def sync_proxy_state(self) -> None:
        """Синхронизирует состояние прокси и наш системный прокси."""
        try:
            state = xray.status()
        except Exception:  # noqa: BLE001
            return
        if not self.proxy.get("busy"):
            self.proxy = {**self.proxy, **state, "running": state.get("running", False)}
        try:
            port = int(state.get("port") or self.cfg.get("xray_port") or 10818)
        except (TypeError, ValueError):
            port = 10818
        stream = bool(state.get("stream") or self.cfg.get("youtube_via_vpn"))
        if state.get("running"):
            if self.cfg.get("xray_system_proxy", True):
                try:
                    enabled = xray.system_proxy_enabled()
                    server = xray.system_proxy_server()
                    own = f"127.0.0.1:{port}"
                    if not enabled:
                        outcome = xray.set_system_proxy(True, port, stream)
                        outcome = outcome if isinstance(outcome, dict) else {}
                        if outcome.get("ok"):
                            self.proxy["system_proxy_owned"] = "ours"
                        else:
                            self._log_throttled(
                                "system-proxy-on",
                                "системный прокси не включился: "
                                + str(outcome.get("error") or "нет ответа"))
                    elif server.strip() == own:
                        self.proxy["system_proxy_owned"] = "ours"
                    else:
                        # Чужой прокси не перезаписываем, только отмечаем.
                        self.proxy["system_proxy_owned"] = "foreign"
                        self.proxy["system_proxy_server"] = server
                except Exception as exc:  # noqa: BLE001
                    self._log_throttled(
                        "system-proxy-on",
                        f"системный прокси не включился: {type(exc).__name__}")
        else:
            try:
                if xray.system_proxy_enabled():
                    outcome = xray.set_system_proxy(False, port)
                    outcome = outcome if isinstance(outcome, dict) else {}
                    if not outcome.get("ok"):
                        self._log_throttled(
                            "system-proxy-off",
                            "системный прокси не выключился: "
                            + str(outcome.get("error") or "нет ответа"))
            except Exception as exc:  # noqa: BLE001
                self._log_throttled(
                    "system-proxy-off",
                    f"системный прокси не выключился: {type(exc).__name__}")

    # ---- журнал действий ---------------------------------------------------
    def refresh_activity(self) -> None:
        """Обновляет журнал панели с учётом ``activity_hours``."""
        try:
            hours = float(self.cfg.get("activity_hours") or 3)
        except (TypeError, ValueError):
            hours = 3.0
        self.activity_items = activity.recent(5, hours=hours)

    # ---- проверки сервисов, надзиратель, обновления ------------------------
    def run_health(self, heal: bool = False) -> bool:
        """Проверяет сервисы в фоне; ``heal`` — заодно дать надзирателю поработать."""
        if self.health_running:
            return False
        # Надзирателя занимаем синхронно, до старта потока: иначе плановый
        # проход и heal могли пойти параллельно и дважды запустить tgws/zapret.
        if heal and self.guard_running:
            heal = False
        self.health_running = True
        if heal:
            self.guard_running = True
        self.busy["health"] = "проверяю сервисы…"
        self._signature = None
        snapshot_status = self.status
        snapshot_proxy = dict(self.proxy)

        def worker() -> None:
            ok = False
            try:
                report = health.collect(self.cfg, snapshot_status, snapshot_proxy)
                self.health_report = report
                ok = True
                self._apply_level(report)
            except Exception as exc:  # noqa: BLE001
                activity.log(f"проверка сервисов не удалась: {exc}", kind="warn")
            try:
                # Лечим только по свежим данным: упавшая проверка — не отчёт.
                if heal and ok:
                    done = guard.heal(self.cfg, snapshot_status, snapshot_proxy,
                                      self.health_report)
                    self._report_guard(done)
            except Exception as exc:  # noqa: BLE001
                activity.log(f"лечение не удалось: {exc}", kind="warn")
            finally:
                if heal:
                    self.guard_running = False
                    if ok:
                        # Отметка только за реальный проход надзирателя.
                        self.guard_at = time.time()
                if ok:
                    # Время ставим только после успешной проверки, иначе сбой
                    # выглядит как свежий успех и проверка откладывается.
                    self.health_at = time.time()
                self.health_running = False
                self.busy.pop("health", None)
                self.refresh_activity()
                # Свежая запись уже в истории: кэш сводки не должен показывать
                # старое до истечения TTL.
                history.reset_summary_cache()
                self._signature = None
                self.update_icon()

        threading.Thread(target=worker, daemon=True, name="health").start()
        return True

    def run_guard(self) -> bool:
        """Даёт надзирателю один проход (только в фоне)."""
        if self.guard_running or not self.cfg.get("guard_enabled", True):
            return False
        self.guard_running = True
        self.busy["guard"] = "надзиратель…"
        snapshot_status = self.status
        snapshot_proxy = dict(self.proxy)
        # Старый отчёт не повод переключать сервер: лечим по свежим данным.
        try:
            health_minutes = max(1, int(self.cfg.get("health_minutes") or 10))
        except (TypeError, ValueError):
            health_minutes = 10
        fresh = bool(self.health_report) and (
            time.time() - self.health_at <= 2 * health_minutes * 60)
        report = self.health_report if fresh else None

        def worker() -> None:
            try:
                done = guard.heal(self.cfg, snapshot_status, snapshot_proxy, report)
                self._report_guard(done)
            except Exception as exc:  # noqa: BLE001
                # Падение надзирателя раньше пропадало бесследно: без записи
                # в журнал «тишина» неотличима от «всё хорошо».
                self._log_throttled(
                    "guard-error:" + type(exc).__name__,
                    f"надзиратель не отработал: {type(exc).__name__}")
            finally:
                self.guard_at = time.time()
                self.guard_running = False
                self.busy.pop("guard", None)
                self.refresh_activity()
                self._signature = None
                self.update_icon()

        threading.Thread(target=worker, daemon=True, name="guard").start()
        return True

    def _note_verdict(self, verdict: str) -> None:
        """Уведомляет о переходах между ok, warn и fail."""
        previous = self._prev_verdict
        self._prev_verdict = verdict
        if not previous or previous == verdict or self.cfg.get("quiet_icon", True):
            return
        if verdict in ("warn", "fail") and previous == "ok":
            self.notify("Проверка: " + ("есть сбой" if verdict == "fail" else "есть замечания"))
        elif verdict == "fail" and previous == "warn":
            self.notify("Проверка: появился сбой")
        elif verdict == "warn" and previous == "fail":
            self.notify("Проверка: сбой снят, остались замечания")
        elif verdict == "ok" and previous == "fail":
            self.notify("Снова всё работает")

    # ---- уровень иконки ----------------------------------------------------
    _LEVEL_RANK = {"ok": 0, "degraded": 1, "down": 2}
    _LEVEL_WORDS = {"ok": "всё работает", "degraded": "требует внимания",
                    "down": "есть сбой"}

    def _reset_health(self) -> None:
        """Забывает отчёт: устаревший вердикт не переезжает на новое подключение.

        Уровень обнуляется, чтобы первая проба нового подключения применилась
        сразу, без гистерезиса и кулдауна от прежнего состояния.
        """
        self.health_report = {}
        self.health_at = 0.0
        self._level = ""
        self._level_candidate = ""
        self._level_hits = 0
        self._level_at = 0.0

    def _apply_tunnel_level(self, failures: int) -> None:
        """Быстрый уровень по отказу туннеля, не дожидаясь ``health.collect``.

        Две неудачи подряд — деградация, три — сбой; успех возвращает уровень
        свежего отчёта. Гистерезис остаётся в ``_apply_level``, здесь только
        повод пересчитать.
        """
        if failures >= 3:
            level, verdict = "down", "fail"
        elif failures >= 2:
            level, verdict = "degraded", "warn"
        else:
            report = self.health_report or {}
            try:
                minutes = max(1, int(self.cfg.get("health_minutes") or 10))
            except (TypeError, ValueError):
                minutes = 10
            if not report or time.time() - self.health_at > 2 * minutes * 60:
                return
            level = report_level(report) or "ok"
            verdict = str(report.get("verdict") or "ok")
        self._apply_level({"level": level, "verdict": verdict, "items": []})

    def _mark_service(self, item_id: str, state: str, detail: str = "") -> None:
        """Правит пункт последнего отчёта и пересчитывает уровень иконки.

        Отчёт живёт до следующего ``health.collect`` (10 минут по умолчанию),
        поэтому наблюдение сторожа (порт закрылся или снова слушает) пишется
        в него сразу: иначе панель и иконка показывают устаревшее «слушает».
        Уровень считается по всем пунктам заново и применяется через
        ``_apply_level`` — гистерезис и кулдаун остаются его заботой, поэтому
        красный VPN не позеленеет от того, что Telegram ожил.
        """
        report = dict(self.health_report or {})
        items = [dict(item) if isinstance(item, dict) else item
                 for item in (report.get("items") or [])]
        target = next((item for item in items
                       if isinstance(item, dict)
                       and str(item.get("id") or "") == item_id), None)
        if target is None:
            # Отчёта нет или в нём нет такого пункта: полный отчёт не выдумываем.
            return
        target["state"] = state
        target["detail"] = detail
        report["items"] = items
        report["verdict"] = health.verdict(items)
        report["level"] = health.level(items, self.cfg, dict(self.proxy or {}))
        self.health_report = report
        self._apply_level(report)

    def _apply_level(self, report: dict) -> None:
        """Уровень иконки с гистерезисом, чтобы одиночная проба не мигала.

        Смена не чаще раза в 60 с. Ухудшение принимается после двух одинаковых
        результатов, ``down`` — после трёх; улучшение — сразу.
        """
        self._note_verdict(str((report or {}).get("verdict") or "ok"))
        candidate = str((report or {}).get("level") or "ok")
        if candidate not in self._LEVEL_RANK:
            candidate = "ok"
        now = time.time()
        if self._level not in self._LEVEL_RANK:
            # Первый отчёт после старта показываем сразу, без накопления.
            self._level = candidate
            self._level_at = now
            self._level_candidate = ""
            self._level_hits = 0
            return
        if candidate == self._level:
            self._level_candidate = ""
            self._level_hits = 0
            return
        if candidate == self._level_candidate:
            self._level_hits += 1
        else:
            self._level_candidate = candidate
            self._level_hits = 1
        previous = self._level
        worse = self._LEVEL_RANK[candidate] > self._LEVEL_RANK[previous]
        need = (3 if candidate == "down" else 2) if worse else 1
        if self._level_at and now - self._level_at < 60:
            return
        if self._level_hits < need:
            return
        self._level = candidate
        self._level_at = now
        self._level_candidate = ""
        self._level_hits = 0
        reasons = self._level_reasons(candidate)
        text = "состояние: " + self._LEVEL_WORDS.get(candidate, candidate)
        if reasons:
            text += f" — {reasons}"
        activity.log(text, kind="ok" if candidate == "ok" else "warn")

    def _level_reasons(self, level: str) -> str:
        """Короткий список пунктов, из-за которых уровень не ``ok``."""
        items = list((self.health_report or {}).get("items") or [])
        names: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "")
            state = str(item.get("state") or "")
            if item_id not in _LEVEL_LABELS or state not in ("bad", "warn"):
                continue
            if level == "down" and state != "bad":
                continue
            if (level != "down" and state == "bad"
                    and item_id not in _SECONDARY_LABELS):
                continue
            names.append(_LEVEL_LABELS[item_id])
        return ", ".join(names[:3])

    def _diagnose_note(self) -> str:
        """Строка диагноза для подсказки иконки: коротко и без секретов.

        Кэш живёт в ``diagnose`` (``CACHE_TTL``): второй, свой, здесь только
        добавлял бы рассинхрон. При ``ok`` строки нет — подсказка не должна
        повторять «всё работает».
        """
        try:
            data = diagnose.diagnose(
                self.health_report, dict(self.proxy or {}), self.cfg,
                history.summary())
        except Exception:  # noqa: BLE001
            # Диагноз — украшение подсказки: его сбой не должен ломать трей.
            data = {}
        if str(data.get("level") or "ok") != "ok":
            return str(data.get("title") or "")
        return ""

    def _level_title(self, level: str) -> str:
        """Подсказка уровня: причина и время последней проверки."""
        title = self._LEVEL_WORDS.get(level, "всё работает")
        reasons = self._level_reasons(level)
        if reasons:
            title = f"{title}: {reasons}"
        if self.health_at:
            try:
                stamp = time.strftime("%H:%M", time.localtime(self.health_at))
            except (ValueError, OSError, OverflowError):
                stamp = ""
            if stamp:
                title += f" · {stamp}"
        return title

    def _report_guard(self, done: list) -> None:
        """Сообщает о предупреждениях надзирателя; остальное он пишет в журнал."""
        if not done:
            return
        warning = next((item for item in done if item.get("kind") == "warn"), None)
        if warning is not None:
            text = str(warning.get("text") or "Надзиратель: замечание")
            self._notify_throttled("guard:" + text, text)

    def _maybe_health(self) -> None:
        if self.updates_running or (self.busy or {}).get("updates"):
            return
        try:
            minutes = max(1, int(self.cfg.get("health_minutes") or 10))
        except (TypeError, ValueError):
            minutes = 10
        if not self.health_running and time.time() - self.health_at >= minutes * 60:
            self.run_health()

    def _maybe_guard(self) -> None:
        if not self.cfg.get("guard_enabled", True):
            return
        try:
            minutes = max(1, int(self.cfg.get("guard_minutes") or 10))
        except (TypeError, ValueError):
            minutes = 10
        if not self.guard_running and time.time() - self.guard_at >= minutes * 60:
            self.run_guard()

    @staticmethod
    def _component_names(names) -> str:
        return ", ".join(updates.label(str(name)) for name in names)

    @staticmethod
    def _version_note(report: dict) -> str:
        """«Доступна версия X» — если у самой программы есть свежий релиз."""
        block = (report or {}).get("app") or {}
        latest = str(block.get("latest") or "")
        if not block.get("outdated") or not latest:
            return ""
        installed = str(block.get("installed") or "")
        return (f"Доступна версия {latest}"
                + (f" (установлена {installed})" if installed else ""))

    def open_release(self) -> bool:
        """Открывает страницу релиза в браузере. Exe сам не подменяем."""
        import webbrowser

        block = (self.updates_report or {}).get("app") or {}
        url = str(block.get("url") or updates.APP_RELEASES_URL)
        if not url.startswith("https://"):
            return False
        try:
            return bool(webbrowser.open(url))
        except Exception:  # noqa: BLE001
            return False

    def _update_message(self, result: dict, network_error: str = "") -> str:
        """Человеческий итог установки: что поставил, что обновил, что не вышло."""
        installed = list(result.get("installed") or [])
        updated = list(result.get("updated") or [])
        errors = list(result.get("errors") or [])
        warnings = list(result.get("warnings") or [])
        parts: list[str] = []
        if installed:
            parts.append("Установил: " + self._component_names(installed))
        if updated:
            parts.append("Обновил: " + self._component_names(updated))
        if errors:
            prefix = "Нет связи с GitHub: " if network_error else "Не удалось: "
            parts.append(prefix + "; ".join(errors)[:140])
        elif network_error and not (installed or updated):
            parts.append("нет связи с GitHub — обновления не проверены")
        elif not installed and not updated:
            parts.append("Всё уже актуально")
        message = ". ".join(parts)
        if warnings:
            message += " (" + "; ".join(warnings)[:160] + ")"
        return message

    def check_all(self, notify: bool = False) -> bool:
        """Проверяет обновления через GitHub (в фоне).

        С ``notify`` проверка не обрывается сообщением: если нашлись
        устаревшие/отсутствующие компоненты, сразу запускается установка
        (``update_everything``) — так же, как кнопка обновления в панели.
        """
        if self.updates_running:
            return False
        self.updates_running = True
        self.busy["updates"] = "проверяю обновления…"
        self._signature = None
        follow_up = False

        def worker() -> None:
            nonlocal follow_up
            try:
                report = updates.check_all(self.cfg, self.state)
                self.updates_report = report
                outdated = list(report.get("outdated") or [])
                missing = list(report.get("missing") or [])
                network_error = str(report.get("network_error") or "")
                version_note = self._version_note(report)
                parts: list[str] = []
                if outdated or missing:
                    parts.append("Обновления: "
                                 + self._component_names(outdated or missing))
                if version_note:
                    parts.append(version_note)
                if parts:
                    self.state["updates_note"] = ". ".join(parts)
                elif network_error:
                    self.state["updates_note"] = "нет связи с GitHub"
                else:
                    self.state.pop("updates_note", None)
                config.save_state(self.state)
                if notify:
                    if parts:
                        text = ". ".join(parts)
                        if outdated or missing:
                            text += ". Запускаю обновление…"
                            follow_up = True
                        self.notify(text)
                    elif network_error:
                        self.notify("нет связи с GitHub — обновления не проверены")
                    else:
                        self.notify("Обновлений нет")
            except Exception:  # noqa: BLE001
                pass
            finally:
                self.updates_running = False
                self.busy.pop("updates", None)
                self._signature = None
                self.update_icon()
                if follow_up:
                    # Флаги уже сняты — update_everything начнёт новую работу.
                    self.update_everything()

        threading.Thread(target=worker, daemon=True, name="updates").start()
        return True

    def update_everything(self) -> bool:
        """Проверяет обновления и ставит всё отсутствующее/устаревшее (в фоне)."""
        if self.updates_running:
            return False
        self.updates_running = True
        self.busy["updates"] = "проверяю обновления…"
        self._signature = None
        self.update_icon()

        def progress(stage: str, text: str, percent: int) -> None:
            self.busy["updates"] = text or "обновляю…"

        def worker() -> None:
            try:
                report = updates.check_all(self.cfg, self.state)
                self.updates_report = report
                outdated = list(report.get("outdated") or [])
                missing = list(report.get("missing") or [])
                network_error = str(report.get("network_error") or "")
                version_note = self._version_note(report)
                if outdated or missing:
                    self.busy["updates"] = "устанавливаю компоненты…"
                    result = updates.update_all(self.cfg, self.state, progress=progress)
                    message = self._update_message(
                        result, network_error or str(result.get("network_error") or ""))
                    try:
                        self.updates_report = updates.check_all(self.cfg, self.state)
                        version_note = self._version_note(self.updates_report)
                    except Exception:  # noqa: BLE001
                        pass
                    if version_note:
                        message += ". " + version_note
                elif network_error:
                    message = "нет связи с GitHub — обновления не проверены"
                elif version_note:
                    message = version_note + " — откройте страницу релиза"
                else:
                    message = "Обновления не найдены"
                activity.log(message)
                self.notify(message)
            except Exception as exc:  # noqa: BLE001
                self.notify(f"Обновление не удалось: {type(exc).__name__}")
            finally:
                self.updates_running = False
                self.busy.pop("updates", None)
                self.refresh_activity()
                config.save_state(self.state)
                self._signature = None
                self.update_icon()

        threading.Thread(target=worker, daemon=True, name="update-everything").start()
        return True

    def update_all(self) -> bool:
        """Обновляет компоненты и ядро Xray (в фоне)."""
        if self.updates_running:
            return False
        self.updates_running = True
        self.busy["updates"] = "обновляю компоненты…"
        self._signature = None

        def worker() -> None:
            try:
                result = updates.update_all(self.cfg, self.state)
                message = self._update_message(
                    result, str(result.get("network_error") or ""))
                self.notify(message)
                try:
                    self.updates_report = updates.check_all(self.cfg, self.state)
                except Exception:  # noqa: BLE001
                    pass
            except Exception as exc:  # noqa: BLE001
                self.notify(f"Обновление не удалось: {type(exc).__name__}")
            finally:
                self.updates_running = False
                self.busy.pop("updates", None)
                self.refresh_activity()
                self._signature = None
                self.refresh()

        threading.Thread(target=worker, daemon=True, name="update-all").start()
        return True

    # ---- маршруты и подписка ----------------------------------------------
    def audit_routes(self) -> bool:
        """Аудит маршрутов: оверрайды сохраняются, живой туннель не рвётся."""
        if self.route_running:
            return False
        self.route_running = True
        self.busy["routes"] = "проверяю маршруты…"
        self._signature = None

        def worker() -> None:
            try:
                report = routes.audit(self.cfg)
                self.route_report = report
                applied = routes.apply_audit(report, self.cfg, reason="manual")
                changed = applied.get("changed") or []
                if changed:
                    activity.log("маршруты обновлены — применятся при следующем подключении")
                    self.notify(f"Маршруты: изменено {len(changed)}")
                elif applied.get("saved") is False:
                    # Запись не состоялась (например, файл повреждён): «нет
                    # изменений» здесь было бы неправдой.
                    self.notify(f"Маршруты не сохранены: {applied.get('error') or '?'}")
                else:
                    self.notify("Маршруты: изменений нет")
            except Exception as exc:  # noqa: BLE001
                self.notify(f"Аудит маршрутов не удался: {type(exc).__name__}")
            finally:
                self.route_running = False
                self.busy.pop("routes", None)
                self.refresh_activity()
                self._signature = None
                self.update_icon()

        threading.Thread(target=worker, daemon=True, name="routes").start()
        return True

    def plan_routes(self) -> dict:
        """Сухой прогон для диалога: что изменится. Ничего не пишет и не рвёт.

        Отчёт запоминается: если человек подтвердит, применяется именно он, а
        не результат второго аудита (сеть за это время могла ответить иначе).
        """
        report = routes.audit(self.cfg)
        self.route_report = report
        self.route_plan_report = report
        return routes.plan(self.cfg, result=report)

    def rollback_routes(self) -> bool:
        """Откат последнего изменения маршрутов — из панели."""
        if self.route_running:
            return False
        self.route_running = True
        self.busy["routes"] = "откатываю маршруты…"
        self._signature = None

        def worker() -> None:
            try:
                outcome = routes.rollback_last(self.cfg)
                if not outcome.get("ok"):
                    self.notify(f"Откат не удался: {outcome.get('error') or '?'}")
                else:
                    message = f"Маршруты откатил: {int(outcome.get('changed') or 0)}"
                    if outcome.get("restarted"):
                        message += " (прокси перезапущен)"
                    self.notify(message)
            except Exception as exc:  # noqa: BLE001
                self.notify(f"Откат не удался: {type(exc).__name__}")
            finally:
                self.route_running = False
                self.busy.pop("routes", None)
                self.refresh_activity()
                self._signature = None
                self.refresh()

        threading.Thread(target=worker, daemon=True, name="route-rollback").start()
        return True

    def apply_routes(self) -> bool:
        """Аудит и немедленное применение — с перезапуском Xray."""
        if self.route_running:
            return False
        self.route_running = True
        self.busy["routes"] = "применяю маршруты…"
        self._signature = None
        # Отчёт подтверждённого плана одноразовый.
        report = self.route_plan_report
        self.route_plan_report = None

        def worker() -> None:
            try:
                outcome = routes.apply_now(self.cfg, result=report)
                changed = int(outcome.get("changed") or 0)
                if changed:
                    message = f"Маршруты применены: {changed}"
                    if outcome.get("restarted"):
                        message += " (прокси перезапущен)"
                    activity.log(message)
                    self.notify(message)
                    if outcome.get("need_restart") and not outcome.get("restarted"):
                        # Оверрайды сохранены, но туннель остался прежним —
                        # применится при следующем подключении.
                        self._notify_throttled(
                            "routes",
                            "Маршруты сохранены, но прокси не перезапустился",
                        )
                elif outcome.get("saved") is False:
                    self.notify(f"Маршруты не применены: {outcome.get('error') or '?'}")
                else:
                    self.notify("Маршруты: изменений нет")
            except Exception as exc:  # noqa: BLE001
                self.notify(f"Маршруты не применились: {type(exc).__name__}")
            finally:
                self.route_running = False
                self.busy.pop("routes", None)
                self.refresh_activity()
                self._signature = None
                self.refresh()

        threading.Thread(target=worker, daemon=True, name="route-apply").start()
        return True

    def refresh_subscription(self) -> bool:
        """Перепроверяет серверы: свои конфиги и подписка, если она задана."""
        if self.busy.get("subscription"):
            return False
        self.busy["subscription"] = "обновляю серверы…"
        self._signature = None

        def worker() -> None:
            try:
                rows = xray.scan_servers()
                if rows:
                    self.servers = rows
                    activity.log(f"обновил список серверов: {len(rows)}", kind="ok")
                    self.refresh_activity()
                    self.notify(f"Серверы обновлены: {len(rows)}")
                else:
                    self.notify("Серверы не найдены: проверьте свои конфиги")
            except ValueError as exc:
                # Текст наш собственный («нет конфигов: …»): секретов в нём нет.
                self.notify(str(exc))
            except Exception as exc:  # noqa: BLE001
                self.notify(f"Серверы недоступны: {type(exc).__name__}")
            finally:
                self.busy.pop("subscription", None)
                self._signature = None
                self.update_icon()

        threading.Thread(target=worker, daemon=True, name="subscription").start()
        return True

    def pick_best_server(self) -> bool:
        """Подбирает и включает лучший сервер."""
        if self.busy.get("servers") or self.busy.get("proxy"):
            return False
        self.busy["servers"] = "подбираю сервер…"
        self._signature = None

        def worker() -> None:
            try:
                result = xray.switch_server(set(), self.cfg)
                if result.get("ok"):
                    server = result.get("server") or {}
                    name = str(server.get("name") or server.get("host") or "")
                    self.server_selected = name
                    self.proxy = {**self.proxy, "busy": False, **xray.status()}
                    activity.log(f"подобрал лучший сервер: {name}", kind="ok")
                    self.notify(f"Сервер: {name}")
                else:
                    self.notify("Не удалось подобрать сервер")
            except Exception as exc:  # noqa: BLE001
                self.notify(f"Сервер не выбран: {type(exc).__name__}")
            finally:
                self.busy.pop("servers", None)
                self.refresh_activity()
                self._signature = None
                self.refresh()

        threading.Thread(target=worker, daemon=True, name="pick-server").start()
        return True

    # ---- поведение ---------------------------------------------------------
    def set_quiet(self, value: bool) -> None:
        """Тихий режим — только уведомления; цвет иконки от него не зависит."""
        self.cfg["quiet_icon"] = bool(value)
        config.save_config(self.cfg)
        self._signature = None
        self.update_icon()

    def set_guard(self, value: bool) -> None:
        self.cfg["guard_enabled"] = bool(value)
        config.save_config(self.cfg)
        self._signature = None

    def set_youtube_via_vpn(self, value: bool) -> None:
        """Включает/выключает принудительный YouTube/Discord через VPN."""
        value = bool(value)
        self.cfg["youtube_via_vpn"] = value
        config.save_config(self.cfg)
        applied = True
        if self.proxy.get("running"):
            applied = self.restart_proxy() is not False
        if not applied:
            message = ("YouTube через VPN: сохранено, применится при следующем "
                       "подключении")
        elif value:
            message = "YouTube/Discord: через VPN"
        else:
            message = "YouTube/Discord: напрямую под zapret"
        activity.log(message, kind="info")
        self.refresh_activity()
        self.notify(message)
        self._signature = None

    def is_autostart(self) -> bool:
        try:
            return autostart.is_enabled()
        except Exception:  # noqa: BLE001
            return False

    def set_autostart(self, value: bool) -> bool:
        """Включает/выключает автозапуск; конфиг меняется оптимистично.

        Ярлык создаёт PowerShell, а это медленно. Тумблер не должен отскакивать:
        cfg пишем сразу, а при отказе PowerShell откатываем и сообщаем.
        """
        if self.busy.get("autostart"):
            return False
        value = bool(value)
        previous = bool(self.cfg.get("autoconnect"))
        self.cfg["autoconnect"] = value
        config.save_config(self.cfg)
        self.busy["autostart"] = "меняю автозапуск…"
        self._signature = None

        def worker() -> None:
            try:
                ok = autostart.enable() if value else autostart.disable()
            except Exception:  # noqa: BLE001
                ok = False
            if ok:
                activity.log(("включил" if value else "выключил") + " автозапуск")
                self.refresh_activity()
            else:
                self.cfg["autoconnect"] = previous
                config.save_config(self.cfg)
                self.notify("Не удалось изменить автозапуск")
            self.busy.pop("autostart", None)
            self._signature = None
            self.update_icon()

        threading.Thread(target=worker, daemon=True, name="autostart").start()
        return True

    # ---- запуск компонентов из панели -------------------------------------
    def _run_action_busy(self, key: str, text: str, fn, *args) -> bool:
        if self.busy.get(key):
            return False
        self.busy[key] = text
        self._signature = None

        def worker() -> None:
            try:
                fn(*args)
            except Exception as exc:  # noqa: BLE001
                self.notify(str(exc))
            finally:
                self.busy.pop(key, None)
                self._signature = None
                self.refresh()

        threading.Thread(target=worker, daemon=True, name=f"action-{key}").start()
        return True

    def start_zapret(self) -> bool:
        if (self.status.get("zapret") or {}).get("as_service"):
            return self._run_action_busy("zapret", "запускаю службу…",
                                         actions.control_service, "start")
        return self._run_action_busy("zapret", "запускаю zapret…",
                                     actions.start_zapret, self.cfg)

    def restart_zapret(self) -> bool:
        if (self.status.get("zapret") or {}).get("as_service"):
            return self._run_action_busy("zapret", "перезапускаю службу…",
                                         actions.control_service, "restart")
        return self._run_action_busy("zapret", "перезапускаю zapret…",
                                     actions.restart_zapret, self.cfg)

    def start_tgws(self) -> bool:
        # Ручной запуск снимает запрет, поставленный «Остановить».
        self._tgws_user_off = False
        self._tgws_down = False
        return self._run_action_busy("tgws", "запускаю tg-ws-proxy…",
                                     actions.start_tgws, self.cfg)

    def stop_tgws(self) -> bool:
        """Ручная остановка: авто-перезапуск выключается до следующего запуска."""
        self._tgws_user_off = True
        return self._run_action_busy("tgws", "останавливаю tg-ws-proxy…",
                                     actions.stop_tgws)

    def restart_tgws(self) -> bool:
        self._tgws_user_off = False
        self._tgws_down = False

        def action() -> None:
            actions.stop_tgws()
            time.sleep(0.6)
            actions.start_tgws(self.cfg)

        return self._run_action_busy("tgws", "перезапускаю tg-ws-proxy…", action)

    # ---- сеть --------------------------------------------------------------
    def check_network(self) -> None:
        """Раз в минуту сверяет сеть и вспоминает стратегию для неё."""
        now = time.time()
        if now - self._net_checked < 60:
            return
        self._net_checked = now
        try:
            net = networks.current()
        except Exception:  # noqa: BLE001
            return
        key = str(net.get("key") or "")
        if not key or key == self._net_key:
            return
        self._net_key = key
        saved = networks.strategy_for(key)
        if saved and saved != self.cfg.get("zapret_bat"):
            self.cfg["zapret_bat"] = saved
            config.save_config(self.cfg)
            activity.log(f"сеть {net.get('name') or key}: стратегия {saved}")
            self.refresh_activity()
            self._signature = None
        self._apply_network_server(key, net)

    def _remember_network_server(self, name: str) -> None:
        """Запоминает сервер за текущей сетью — его применит смена сети."""
        if not name:
            return
        try:
            net = networks.current()
            if not net.get("key"):
                return
            networks.remember(str(net["key"]), str(self.cfg.get("zapret_bat") or ""),
                              net, server=name)
        except OSError:
            activity.log("не запомнил сервер для сети", kind="warn")

    def _apply_network_server(self, key: str, net: dict) -> None:
        """Переключает на сервер, запомненный за этой сетью.

        Сервер берётся из списка, который уже есть у панели, — сеть не
        опрашивается. Переключение перезапускает Xray, поэтому идёт через
        ``select_server`` (он работает в своём потоке). Сервер, которого нет
        в списке, не трогаем: иначе смена сети уводила бы туннель на пустое
        имя. Об этом одно предупреждение в час.
        """
        name = networks.server_for(key)
        if not name or name == str(self.proxy.get("server") or ""):
            return
        if self.proxy.get("busy") or self.busy.get("proxy"):
            return
        rows = self.servers or xray.cached_servers()
        if not any(str(row.get("name") or "") == name for row in rows):
            self._notify_throttled(
                "net-server",
                f"сеть {net.get('name') or key}: сервер «{name}» не найден",
                hours=1.0)
            return
        activity.log(f"сеть {net.get('name') or key}: сервер {name}", kind="auto")
        self.refresh_activity()
        self.select_server(name)

    def check_stream_fallback(self) -> None:
        """Уводит YouTube/Discord в прокси, если zapret не работает.

        Обычно их закрывает zapret, и через сервер они не идут. Но когда
        ``winws.exe`` не запущен, напрямую они уже не откроются — тогда
        включается резервный режим, а после восстановления zapret выключается.
        """
        if not self.cfg.get("xray_stream_fallback", True):
            return
        if self.cfg.get("youtube_via_vpn"):
            # Пользователь сам выбрал VPN для YouTube/Discord — прямой режим
            # возвращать не нужно.
            self._stream_since = None
            return
        if self.proxy.get("busy") or not self.proxy.get("running"):
            self._stream_since = None
            return

        zapret_ok = bool(((self.status or {}).get("zapret") or {}).get("running"))
        need = not zapret_ok
        if need == bool(self.proxy.get("stream")):
            self._stream_since = None
            return

        now = time.time()
        if self._stream_since is None:
            self._stream_since = now
            return
        if now - self._stream_since < 45:
            return
        self._stream_since = None
        result = xray.restart_with_stream(need, self.cfg)
        if result.get("ok"):
            self.proxy = {**self.proxy, "stream": need, **xray.status()}
            says = "YouTube/Discord: через VPN (zapret не работает)" if need else \
                "YouTube/Discord: снова напрямую"
            activity.log(says)
            self.refresh_activity()
            self._notify_throttled("stream", says, hours=1.0)
        self._signature = None

    # ---- статусы -----------------------------------------------------------
    def refresh(self) -> None:
        self.status = status.collect(self.cfg)
        self.sync_proxy_state()
        if self.proxy.get("server"):
            self.server_selected = str(self.proxy.get("server") or "")
        self.refresh_activity()
        self.updated_at = time.time()
        self.update_icon()
        hours = float(self.cfg.get("update_check_hours") or 24)
        if time.time() - self.last_check > hours * 3600:
            self.last_check = time.time()
            self.check_all(notify=False)
        self.check_network()

    def tray_state(self) -> tuple[str, str, str]:
        """(вид иконки, цвет, подсказка) — трёхцветный уровень проверки.

        ``busy`` — модификатор: цвет и глиф уровня сохраняются, меняется только
        подсказка. Синий спиннер — лишь пока данных проверки ещё нет.
        """
        busy = bool(self.busy) or self.health_running
        if self._user_off or not self.proxy.get("running"):
            key, color, title = "off", STATE_COLORS["idle"], "VPN выключен"
        elif not self.health_report:
            key, color, title = "idle", STATE_COLORS["idle"], "нет данных проверки"
        else:
            level = (self._level if self._level in self._LEVEL_WORDS
                     else (report_level(self.health_report) or "ok"))
            key, color = {
                "down": ("down", STATE_COLORS["bad"]),
                "degraded": ("degraded", STATE_COLORS["warn"]),
            }.get(level, ("ok", STATE_COLORS["ok"]))
            title = self._level_title(level)
            # Строка диагноза — только при уже показанном не-ok уровне: так она
            # не спорит со сглаживанием (зелёная иконка не получает «есть сбой»).
            # Базовый текст режем заранее, чтобы обрезка [:110] не съела её.
            if level != "ok" and not busy:
                note = self._diagnose_note()
                if note and note.casefold() not in title.casefold():
                    room = 110 - len(note) - 3
                    title = f"{title[:max(20, room)]} · {note}"
        if busy:
            if key == "idle":
                key, color = "busy", STATE_COLORS["busy"]
            # Словарь меняют фоновые потоки: итерация по живому dict падает
            # с RuntimeError, поэтому берём снимок значений.
            title = next((str(text) for text in list(self.busy.values()) if text),
                         "проверяю…")
        # szTip Windows — около 127 символов, префикс «bypass-tray · » тоже там.
        return key, color, title[:110]

    def update_icon(self) -> None:
        if self._icon is None:
            return
        key, color, title = self.tray_state()
        signature = (key, color, title, self._menu_signature())
        if signature == self._signature:
            return
        try:
            self._icon.icon = icons.make_icon(key, color)
            self._icon.title = f"bypass-tray · {title}"
            self._icon.menu = self.build_menu()
            self._icon.update_menu()
        except Exception:  # noqa: BLE001
            # Подпись запоминаем только после успешной отрисовки: иначе
            # неудачная попытка залипнет и иконка не обновится.
            return
        self._signature = signature

    def _menu_signature(self) -> tuple:
        state = self.status or {}
        zapret = state.get("zapret") or {}
        tgws = state.get("tgwsproxy") or {}
        return (
            zapret.get("running"),
            zapret.get("strategy"),
            tuple(zapret.get("bats") or ()),
            tgws.get("listening"),
            self.proxy.get("running"),
            self.proxy.get("server"),
            self.state.get("updates_note"),
            (self.updates_report or {}).get("app") or {},
            tuple(self.updates_report.get("outdated") or ()),
            tuple(self.updates_report.get("missing") or ()),
            bool(self.busy),
            self._prev_verdict,
            bool(self.cfg.get("quiet_icon")),
            bool(self.cfg.get("youtube_via_vpn")),
        )

    # ---- обновления --------------------------------------------------------
    def check_updates(self, notify: bool = True, background: bool = False) -> None:
        """Совместимость со старым вызовом: проверка идёт через ``check_all``."""
        self.check_all(notify=notify)

    def notify(self, message: str, title: str = "bypass-tray") -> None:
        """Уведомление трея с запасным вариантом.

        Штатный ``icon.notify`` на Windows молча подавляется режимом «Не
        беспокоить» и фокус-ассистом, поэтому при ошибке мигаем иконкой;
        подсказка и строка «Обновления» в панели видны в любом случае.
        """
        message = secrets.redact_text(message)
        icon = self._icon
        if icon is None:
            return
        try:
            icon.notify(message, title)
            return
        except Exception:  # noqa: BLE001
            pass
        self.flash()

    def flash(self, times: int = 3) -> None:
        """Мигание иконки — резервное оповещение."""
        icon = self._icon
        if icon is None:
            return

        def worker() -> None:
            for _ in range(times):
                for state, color in (("update", STATE_COLORS["warn"]), ("ok", STATE_COLORS["ok"])):
                    try:
                        icon.icon = icons.make_icon(state, color)
                        time.sleep(0.4)
                    except Exception:  # noqa: BLE001
                        return
            self._signature = None
            self.update_icon()

        threading.Thread(target=worker, daemon=True).start()

    # ---- меню --------------------------------------------------------------
    def build_menu(self):
        state = self.status or {}
        zapret = state.get("zapret") or {}
        zapret_text = "запущен" if zapret.get("running") else "выключен"
        # Свежий релиз программы: отдельный пункт меню, без подмены exe.
        app_block = (self.updates_report or {}).get("app") or {}
        version = (str(app_block.get("latest") or "")
                   if app_block.get("outdated") else "")
        strategy = zapret.get("strategy") or zapret.get("digest") or ""
        if strategy:
            zapret_text = f"{zapret_text} · {strategy}"
        tgws = state.get("tgwsproxy") or {}
        tgws_text = f"слушает :{tgws.get('port', 1443)}" if tgws.get("listening") else "не слушает"
        bats = zapret.get("bats") or []
        service_mode = bool(zapret.get("as_service"))

        def make_strategy_action(name):
            def action(icon, item):
                self.set_strategy(name)
            return action

        strategy_items = [
            pystray.MenuItem(
                name,
                make_strategy_action(name),
                radio=True,
                checked=lambda item, n=name: self.cfg.get("zapret_bat") == n,
            )
            for name in bats
        ] or [pystray.MenuItem("(стратегии не найдены)", None, enabled=False)]

        if service_mode:
            zapret_items = [
                pystray.MenuItem("Перезапустить службу", lambda icon, item: self.run_action(actions.control_service, "restart")),
                pystray.MenuItem("Остановить службу", lambda icon, item: self.run_action(actions.control_service, "stop")),
                pystray.MenuItem("Запустить службу", lambda icon, item: self.run_action(actions.control_service, "start")),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Сменить стратегию службы…", lambda icon, item: self.run_action(actions.open_service_bat, self.cfg)),
            ]
        else:
            zapret_items = [
                pystray.MenuItem("Запустить", lambda icon, item: self.run_action(actions.start_zapret, self.cfg)),
                pystray.MenuItem("Остановить", lambda icon, item: self.run_action(actions.stop_zapret, self.cfg)),
            ]

        proxy_running = bool(self.proxy.get("running"))
        proxy_busy = bool(self.proxy.get("busy"))
        if proxy_running:
            proxy_text = f"подключён · {self.proxy.get('server') or 'сервер'}"
        else:
            proxy_text = self.proxy.get("message") or "выключен"

        return pystray.Menu(
            pystray.MenuItem(f"zapret: {zapret_text}", None, enabled=False),
            pystray.MenuItem(f"tg-ws-proxy: {tgws_text}", None, enabled=False),
            pystray.MenuItem(f"Прокси Xray: {proxy_text}", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Открыть панель", lambda icon, item: self.panel.open(), default=True),
            pystray.MenuItem("Настройки…", lambda icon, item: self.panel.show_settings()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("zapret", pystray.Menu(*zapret_items)),
            pystray.MenuItem("Стратегия zapret", pystray.Menu(*strategy_items)),
            pystray.MenuItem(
                "Подобрать лучшую стратегию…",
                lambda icon, item: self.start_sweep(),
                enabled=not self.sweep.get("running"),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Проверить всё", lambda icon, item: self.run_health(heal=True)),
            pystray.MenuItem("Аудит маршрутов", lambda icon, item: self.audit_routes()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Прокси Xray: подключить",
                lambda icon, item: self.set_proxy(True),
                enabled=not proxy_running and not proxy_busy,
            ),
            pystray.MenuItem(
                "Прокси Xray: отключить",
                lambda icon, item: self.set_proxy(False),
                enabled=proxy_running and not proxy_busy,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("tg-ws-proxy: запустить", lambda icon, item: self.start_tgws()),
            pystray.MenuItem("tg-ws-proxy: остановить", lambda icon, item: self.stop_tgws()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Проверить обновления",
                             lambda icon, item: self.check_all(notify=True)),
            *([pystray.MenuItem("Обновить всё",
                                lambda icon, item: self.update_everything())]
              if (self.updates_report.get("outdated")
                  or self.updates_report.get("missing")) else []),
            *([pystray.MenuItem(
                    f"Открыть страницу релиза {version}",
                    lambda icon, item: self.open_release())]
              if version else []),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Тихая иконка",
                lambda icon, item: self.set_quiet(not self.cfg.get("quiet_icon", True)),
                checked=lambda item: bool(self.cfg.get("quiet_icon", True)),
            ),
            pystray.MenuItem(
                "Подключаться при входе",
                lambda icon, item: self.set_autostart(not self.cfg.get("autoconnect")),
                checked=lambda item: bool(self.cfg.get("autoconnect")),
            ),
            pystray.MenuItem(
                "YouTube через VPN",
                lambda icon, item: self.set_youtube_via_vpn(
                    not self.cfg.get("youtube_via_vpn")),
                checked=lambda item: bool(self.cfg.get("youtube_via_vpn")),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", lambda icon, item: self.stop()),
        )

    def set_strategy(self, name: str) -> None:
        self.cfg["zapret_bat"] = name
        config.save_config(self.cfg)
        self._signature = None
        self.update_icon()

    # ---- автоподбор стратегии ---------------------------------------------
    def sweep_files(self) -> tuple:
        base = config.config_dir()
        return base / "sweep-result.json", base / "sweep-progress.json"

    def start_sweep(self) -> bool:
        """Запускает перебор стратегий в отдельном процессе с UAC.

        Само приложение работает без прав администратора, а перебору нужно
        останавливать и перенастраивать службу zapret, поэтому запускается
        дочерний процесс с повышением прав (один запрос UAC).
        """
        if self.sweep.get("running"):
            return False
        result_path, progress_path = self.sweep_files()
        for path in (result_path, progress_path):
            try:
                path.unlink()
            except OSError:
                pass

        self.sweep = {"running": True, "message": "Запрос прав администратора…", "index": 0}
        self.busy["sweep"] = "подбираю стратегию…"
        self._signature = None

        if getattr(sys, "frozen", False):
            command = [sys.executable, "--sweep-strategies",
                       "--out", str(result_path), "--report", str(progress_path)]
        else:
            command = [
                sys.executable, "-m", "bypass_tray", "--sweep-strategies",
                "--out", str(result_path), "--report", str(progress_path),
            ]

        def launch() -> None:
            # ``run_elevated`` блокирует поток до ответа на UAC, поэтому
            # запускаем его вне message-loop трея.
            try:
                launched = actions.run_elevated(command)
            except Exception as exc:  # noqa: BLE001
                activity.log(f"автоподбор не запустился: {exc}", kind="warn")
                launched = False
            if not launched:
                self.sweep = {"running": False, "message": "Запрос прав отклонён"}
                self.busy.pop("sweep", None)
                self._signature = None
                self.update_icon()
                return
            threading.Thread(
                target=self._watch_sweep, args=(result_path, progress_path),
                daemon=True, name="sweep-watch",
            ).start()

        threading.Thread(target=launch, daemon=True, name="sweep-launch").start()
        return True

    def _watch_sweep(self, result_path, progress_path) -> None:
        deadline = time.time() + 900
        try:
            while time.time() < deadline:
                time.sleep(1.5)
                if progress_path.exists():
                    try:
                        data = json.loads(progress_path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        data = {}
                    self.sweep.update(data)
                    self.sweep["running"] = not data.get("done")
                    self._signature = None
                    self.update_icon()
                if result_path.exists():
                    break

            report = strategy.read_report(result_path)
            self.sweep["running"] = False
            if report.get("best"):
                self.cfg["zapret_bat"] = report["best"]
                config.save_config(self.cfg)
                self.state["last_sweep"] = {
                    "at": report.get("finished", time.time()),
                    "best": report["best"],
                    "installed": report.get("installed", False),
                }
                config.save_state(self.state)
                try:
                    net = networks.current()
                    if net.get("key"):
                        networks.remember(str(net["key"]), report["best"], net)
                except Exception:  # noqa: BLE001
                    pass
                activity.log(f"подобрал стратегию: {report['best']}", kind="ok")
                self.refresh_activity()
                message = f"Лучшая стратегия: {report['best']}"
                if report.get("installed"):
                    message += " (установлена)"
                self.sweep["message"] = message
                self.notify(message)
            else:
                self.sweep["message"] = report.get("results") and "Рабочая стратегия не найдена" or "Перебор не завершён"
                self.notify("Автоподбор: " + self.sweep["message"])
        except Exception:  # noqa: BLE001
            self.sweep["running"] = False
        finally:
            self.sweep["running"] = False
            self.busy.pop("sweep", None)
            self._signature = None
            self.refresh()

    def run_action(self, fn, *args) -> bool:
        """Пункт меню: действие уходит в поток, а не в message-loop pystray.

        ``sc.exe`` живёт 10–30 с, а ``ShellExecuteW`` с runas блокирует до
        ответа на UAC: в потоке трея это «приложение не отвечает». Поэтому
        идём через ``_run_action_busy`` — как панель.
        """
        key = getattr(fn, "__name__", "") or "action"
        text = _ACTION_TEXTS.get(key, "выполняю…")
        return self._run_action_busy(key, text, fn, *args)

    # ---- жизненный цикл ----------------------------------------------------
    def start(self) -> None:
        if pystray is None:
            raise RuntimeError("pystray не установлен — выполните install.bat")
        # Наличие ярлыка автозапуска — источник истины для «Подключаться при входе».
        try:
            enabled = autostart.is_enabled()
            if bool(self.cfg.get("autoconnect")) != enabled:
                self.cfg["autoconnect"] = enabled
                config.save_config(self.cfg)
        except Exception:  # noqa: BLE001
            pass
        self.sync_proxy_state()
        icon = icons.make_icon("off", STATE_COLORS["dim"])
        self._icon = pystray.Icon("bypass-tray", icon, "bypass-tray", menu=self.build_menu())
        threading.Thread(target=self._loop, daemon=True).start()
        if self.cfg.get("autoconnect") and not self.proxy.get("running"):
            timer = threading.Timer(3.0, self._autoconnect)
            timer.daemon = True
            timer.name = "autoconnect"
            self._autoconnect_timer = timer
            timer.start()
        self._icon.run()

    def _autoconnect(self) -> None:
        """Подключение при входе; если ядро уже живёт — только прокси."""
        if self._shutting_down:
            # Пользователь вышел раньше, чем сработал таймер: поднимать Xray и
            # включать системный прокси на мёртвый порт нельзя.
            return
        try:
            if xray.is_running():
                self.sync_proxy_state()
            else:
                activity.log("подключаюсь при входе")
                self.set_proxy(True)
        except Exception:  # noqa: BLE001
            pass

    def _loop(self) -> None:
        while not self._shutting_down:
            try:
                self.refresh()
                self.check_proxy_health()
                # Во время обновления/лечения не трогаем режим потока и не
                # лечим повторно: иначе можно оборвать чужой перезапуск Xray.
                if not self.busy and not self.updates_running:
                    self.check_stream_fallback()
                    self._maybe_guard()
                self._maybe_health()
            except Exception:  # noqa: BLE001
                pass
            # Мусор в config.json не должен убивать цикл: интервал считаем в
            # try, держим в разумных пределах.
            try:
                interval = float(self.cfg.get("refresh_seconds") or 5)
            except (TypeError, ValueError):
                interval = 5.0
            time.sleep(min(600.0, max(1.0, interval)))

    def stop(self) -> None:
        # Сначала гасим отложенные действия: иначе автоподключение сработает
        # после остановки Xray и включит прокси на мёртвый порт.
        self._shutting_down = True
        timer = self._autoconnect_timer
        if timer is not None:
            timer.cancel()
            self._autoconnect_timer = None
        # Не оставляем мёртвый системный прокси после выхода.
        try:
            xray.stop(port=int(self.proxy.get("port") or 10818))
        except Exception:  # noqa: BLE001
            pass
        self.panel.close()
        if self._icon is not None:
            self._icon.stop()
