"""Трей-приложение: иконка, меню, фоновое обновление статусов."""

from __future__ import annotations

import json
import sys
import threading
import time

try:
    import pystray
except ImportError:  # pragma: no cover
    pystray = None

from . import (
    actions, activity, autostart, config, guard, health, icons, networks, routes,
    status, strategy, updates, xray,
)
from .theme import STATE_COLORS
from .ui import Panel, clean_server_name



#: После автосмены сервера не трогаем его 10 минут: частые перезапуски
#: Xray рвут уже установленные соединения (в том числе YouTube).
AUTO_SWITCH_COOLDOWN = 600.0

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
        self.panel = Panel(self)
        self._icon = None
        self._signature = None
        self._checking = False

        # --- данные проверок и фоновых действий ---
        self.health_report: dict = {}
        self.health_at: float = 0.0
        self.health_running = False
        self.guard_at: float = 0.0
        self.guard_running = False
        self.route_report: dict = {}
        self.route_running = False
        self.updates_report: dict = {}
        self.updates_running = False
        self.activity_items: list = []
        self.busy: dict[str, str] = {}
        self.updated_at: float = 0.0
        self._prev_verdict = ""
        self._net_key = ""
        self._net_checked = 0.0
        # Последние отправленные авто-тосты: (время, текст) по ключу.
        self._notified: dict[str, tuple[float, str]] = {}

    # ---- прокси Xray -------------------------------------------------------
    def set_proxy(self, enable: bool) -> None:
        """Поднимает или гасит собственный прокси Xray."""
        if self.proxy.get("busy"):
            return
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
            if result.get("ok"):
                if enable:
                    server = result.get("server") or {}
                    self.proxy = {
                        "running": True, "busy": False,
                        "message": f"подключено · {server.get('name', '')}",
                        "server": server.get("name", ""),
                        "host": server.get("host", ""),
                        "latency": server.get("latency", -1),
                        "port": result.get("port", xray.DEFAULT_PORT),
                    }
                    self.server_selected = str(server.get("name") or "")
                    activity.log(f"подключил VPN: {server.get('name') or 'сервер'}", kind="ok")
                else:
                    self.proxy = {"running": False, "busy": False, "message": "отключено",
                                  "server": "", "port": xray.DEFAULT_PORT}
                    activity.log("отключил VPN")
                self.refresh_activity()
                self.notify(self.proxy["message"])
            else:
                self.proxy = {**self.proxy, "busy": False,
                              "message": "не удалось: " + str(result.get("error", "?"))}
                self.notify(str(result.get("error", "не удалось")))
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
            except Exception:  # noqa: BLE001
                pass
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
        self.proxy = {**self.proxy, "busy": True, "message": f"переключаю на {raw_name}…"}
        self.busy["proxy"] = "переключаю сервер…"
        self._signature = None

        def worker() -> None:
            try:
                server = xray.parse_vless(row["link"])
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
                return
            self._proxy_failures += 1
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
                        xray.set_system_proxy(True, port, stream)
                        self.proxy["system_proxy_owned"] = "ours"
                    elif server.strip() == own:
                        self.proxy["system_proxy_owned"] = "ours"
                    else:
                        # Чужой прокси не перезаписываем, только отмечаем.
                        self.proxy["system_proxy_owned"] = "foreign"
                        self.proxy["system_proxy_server"] = server
                except Exception:  # noqa: BLE001
                    pass
        else:
            try:
                if xray.system_proxy_enabled():
                    xray.set_system_proxy(False, port)
            except Exception:  # noqa: BLE001
                pass

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
        self.health_running = True
        self.busy["health"] = "проверяю сервисы…"
        self._signature = None
        snapshot_status = self.status
        snapshot_proxy = dict(self.proxy)

        def worker() -> None:
            try:
                report = health.collect(self.cfg, snapshot_status, snapshot_proxy)
                self.health_report = report
                self._note_verdict(str(report.get("verdict") or "ok"))
                if heal:
                    # Лечение занимает guard_running, чтобы плановый проход не
                    # начал второе одновременно.
                    self.guard_running = True
                    try:
                        done = guard.heal(self.cfg, snapshot_status, snapshot_proxy, report)
                        self._report_guard(done)
                    finally:
                        self.guard_running = False
            except Exception:  # noqa: BLE001
                pass
            finally:
                self.health_at = time.time()
                self.guard_at = time.time() if heal else self.guard_at
                self.health_running = False
                self.busy.pop("health", None)
                self.refresh_activity()
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
            except Exception:  # noqa: BLE001
                pass
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
        """Уведомляет только о переходах ok→warn/fail и fail→ok."""
        previous = self._prev_verdict
        self._prev_verdict = verdict
        if not previous or previous == verdict or self.cfg.get("quiet_icon", True):
            return
        if verdict in ("warn", "fail") and previous == "ok":
            self.notify("Проверка: " + ("есть сбой" if verdict == "fail" else "есть замечания"))
        elif verdict == "ok" and previous == "fail":
            self.notify("Снова всё работает")

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

    def check_all(self, notify: bool = False) -> bool:
        """Проверяет обновления через GitHub (в фоне)."""
        if self.updates_running:
            return False
        self.updates_running = True
        self.busy["updates"] = "проверяю обновления…"
        self._signature = None

        def worker() -> None:
            try:
                report = updates.check_all(self.cfg, self.state)
                self.updates_report = report
                outdated = list(report.get("outdated") or [])
                if outdated:
                    self.state["updates_note"] = "Обновления: " + ", ".join(outdated)
                else:
                    self.state.pop("updates_note", None)
                config.save_state(self.state)
                if notify:
                    if outdated:
                        self.notify("Обновления: " + ", ".join(outdated))
                    else:
                        self.notify("Обновлений нет")
            except Exception:  # noqa: BLE001
                pass
            finally:
                self.updates_running = False
                self.busy.pop("updates", None)
                self._signature = None
                self.update_icon()

        threading.Thread(target=worker, daemon=True, name="updates").start()
        return True

    def update_everything(self) -> bool:
        """Проверяет обновления и, если есть, ставит все компоненты (в фоне)."""
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
                if outdated:
                    self.busy["updates"] = "обновляю…"
                    result = updates.update_all(self.cfg, self.state, progress=progress)
                    errors = list(result.get("errors") or [])
                    warnings = list(result.get("warnings") or [])
                    if result.get("ok"):
                        message = "Обновил: " + ", ".join(outdated)
                        if warnings:
                            message += " (" + "; ".join(warnings)[:160] + ")"
                    else:
                        message = "Обновление: " + (
                            "; ".join(errors)[:160] or "не удалось")
                    try:
                        self.updates_report = updates.check_all(self.cfg, self.state)
                    except Exception:  # noqa: BLE001
                        pass
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
                errors = list(result.get("errors") or [])
                warnings = list(result.get("warnings") or [])
                if result.get("ok"):
                    self.notify("Компоненты обновлены"
                                + (": " + "; ".join(warnings)[:160] if warnings else ""))
                else:
                    self.notify("Обновление: " + ("; ".join(errors)[:160] or "не удалось"))
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
                applied = routes.apply_audit(report, self.cfg)
                changed = applied.get("changed") or []
                if changed:
                    activity.log("маршруты обновлены — применятся при следующем подключении")
                    self.notify(f"Маршруты: изменено {len(changed)}")
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

    def apply_routes(self) -> bool:
        """Аудит и немедленное применение — с перезапуском Xray."""
        if self.route_running:
            return False
        self.route_running = True
        self.busy["routes"] = "применяю маршруты…"
        self._signature = None

        def worker() -> None:
            try:
                outcome = routes.apply_now(self.cfg)
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
        """Перекачивает список серверов из подписки."""
        if self.busy.get("subscription"):
            return False
        self.busy["subscription"] = "обновляю подписку…"
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
                    self.notify("Подписка не вернула серверов")
            except Exception as exc:  # noqa: BLE001
                self.notify(f"Подписка недоступна: {type(exc).__name__}")
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
        return self._run_action_busy("tgws", "запускаю tg-ws-proxy…",
                                     actions.start_tgws, self.cfg)

    def restart_tgws(self) -> bool:
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
        """(вид иконки, цвет, подсказка) — приоритет по свежей проверке."""
        if self.busy:
            return "busy", STATE_COLORS["warn"], "работаю…"
        report = self.health_report or {}
        verdict = str(report.get("verdict") or "")
        running = bool(self.proxy.get("running"))
        if verdict == "fail":
            return "warn", STATE_COLORS["bad"], "есть сбой"
        if verdict == "warn":
            return "warn", STATE_COLORS["warn"], "требует внимания"
        if not running:
            return "off", STATE_COLORS["dim"], "VPN выключен"
        if verdict == "ok":
            color = STATE_COLORS["dim"] if self.cfg.get("quiet_icon", True) else STATE_COLORS["ok"]
            return "ok", color, "всё работает"
        return "off", STATE_COLORS["dim"], "нет данных проверки"

    def update_icon(self) -> None:
        if self._icon is None:
            return
        key, color, title = self.tray_state()
        signature = (key, color, title, self._menu_signature())
        if signature == self._signature:
            return
        self._signature = signature
        try:
            self._icon.icon = icons.make_icon(key, color)
            self._icon.title = f"bypass-tray · {title}"
            self._icon.menu = self.build_menu()
            self._icon.update_menu()
        except Exception:  # noqa: BLE001
            pass

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
            pystray.MenuItem("tg-ws-proxy: запустить", lambda icon, item: self.run_action(actions.start_tgws, self.cfg)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Проверить обновления", lambda icon, item: self.check_all(notify=True)),
            *([pystray.MenuItem("Обновить всё",
                                lambda icon, item: self.update_everything())]
              if (self.updates_report.get("outdated") or []) else []),
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
        launched = actions.run_elevated(command)
        if not launched:
            self.sweep = {"running": False, "message": "Запрос прав отклонён"}
            self.busy.pop("sweep", None)
            self.update_icon()
            return False

        threading.Thread(
            target=self._watch_sweep, args=(result_path, progress_path), daemon=True
        ).start()
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

    def run_action(self, fn, *args) -> None:
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001
            self.notify(str(exc))
        self.refresh()

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
            threading.Timer(3.0, self._autoconnect).start()
        self._icon.run()

    def _autoconnect(self) -> None:
        """Подключение при входе; если ядро уже живёт — только прокси."""
        try:
            if xray.is_running():
                self.sync_proxy_state()
            else:
                activity.log("подключаюсь при входе")
                self.set_proxy(True)
        except Exception:  # noqa: BLE001
            pass

    def _loop(self) -> None:
        while True:
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
            time.sleep(float(self.cfg.get("refresh_seconds") or 5))

    def stop(self) -> None:
        # Не оставляем мёртвый системный прокси после выхода.
        try:
            xray.stop(port=int(self.proxy.get("port") or 10818))
        except Exception:  # noqa: BLE001
            pass
        self.panel.close()
        if self._icon is not None:
            self._icon.stop()
