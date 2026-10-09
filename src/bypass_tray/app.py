"""Трей-приложение: иконка, меню, фоновое обновление статусов."""

from __future__ import annotations

import json
import threading
import time

try:
    import pystray
except ImportError:  # pragma: no cover
    pystray = None

from . import actions, config, happ, icons, runtime, status, strategy, updates, xray
from .theme import STATE_COLORS
from .ui import Panel


class TrayApp:
    def __init__(self):
        self.cfg = config.load_config()
        self.state = config.load_state()
        self.status: dict = {}
        self.last_check = 0.0
        self.sweep: dict = {"running": False, "message": ""}
        self.happ: dict = {"connected": False, "routing": False, "active": ""}
        self.tunneling: dict = {"running": False, "message": ""}
        self.proxy: dict = {"running": False, "server": "", "port": xray.DEFAULT_PORT}
        self._fallback_since: float | None = None
        self._stream_since: float | None = None
        self.servers: list = xray.cached_servers()
        self._servers_loading = False
        self._failed_servers: set[str] = set()
        self._proxy_failures = 0
        self._health_checking = False
        self._health_tick = 0
        self.panel = Panel(self)
        self._icon = None
        self._signature = None
        self._checking = False

    # ---- прокси Xray -------------------------------------------------------
    def set_proxy(self, enable: bool) -> None:
        """Поднимает или гасит собственный прокси Xray (без Happ)."""
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
                else:
                    self.proxy = {"running": False, "busy": False, "message": "отключено",
                                  "server": "", "port": xray.DEFAULT_PORT}
                self.notify(self.proxy["message"])
            else:
                self.proxy = {**self.proxy, "busy": False,
                              "message": "не удалось: " + str(result.get("error", "?"))}
                self.notify(str(result.get("error", "не удалось")))
            self._signature = None
            self.refresh()

        threading.Thread(target=worker, daemon=True).start()

    def load_servers(self, force: bool = False) -> None:
        """Обновляет список серверов в фоне (сеть + проверка доступности)."""
        if self.servers and not force:
            return
        if self._servers_loading:
            return
        self._servers_loading = True

        def worker() -> None:
            try:
                rows = xray.scan_servers()
                if rows:
                    self.servers = rows
            except Exception:  # noqa: BLE001
                pass
            finally:
                self._servers_loading = False
                self._signature = None

        threading.Thread(target=worker, daemon=True).start()

    def select_server(self, name: str) -> None:
        """Переключает прокси на выбранный сервер."""
        row = next((r for r in self.servers if r.get("name") == name), None)
        if row is None:
            self.notify("Сервер не найден — обновите список")
            return
        self.proxy = {**self.proxy, "busy": True, "message": f"переключаю на {name}…"}
        self._signature = None

        def worker() -> None:
            try:
                server = xray.parse_vless(row["link"])
                server["latency"] = row.get("latency", -1)
                result = xray.switch_to(server, self.cfg)
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "error": str(exc)}
            if result.get("ok"):
                self.proxy = {**self.proxy, "busy": False, "message": f"подключено · {name}"}
                self.notify(f"Сервер: {name}")
            else:
                self.proxy = {**self.proxy, "busy": False,
                              "message": "не удалось: " + str(result.get("error", "?"))}
            self.refresh()

        threading.Thread(target=worker, daemon=True).start()

    def check_proxy_health(self) -> None:
        """Проверяет туннель и переключает сервер, если тот перестал работать.

        Проверка настоящая: запрос идёт на заблокированный сайт, то есть
        обязательно через сервер. Две неудачи подряд — повод сменить сервер.
        """
        if self.proxy.get("busy") or not self.proxy.get("running"):
            self._proxy_failures = 0
            return
        if self._health_checking:
            return
        self._health_tick = getattr(self, "_health_tick", 0) + 1
        if self._health_tick % 6 != 0:      # примерно раз в 30 секунд
            return
        self._health_checking = True

        def worker() -> None:
            try:
                result = xray.verify(timeout=8)
            except Exception:  # noqa: BLE001
                result = {"ok": False}
            finally:
                self._health_checking = False
            if result.get("ok"):
                self._proxy_failures = 0
                return
            self._proxy_failures += 1
            if self._proxy_failures < 2:
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
            if switched.get("ok"):
                server = switched.get("server") or {}
                self.proxy = {**self.proxy, "busy": False, **xray.status()}
                self.notify(f"Сервер заменён: {server.get('name', '')}")
            else:
                self.proxy = {**self.proxy, "busy": False,
                              "message": "нет доступных серверов"}
                self.notify("Не удалось найти рабочий сервер")
            self.refresh()

        threading.Thread(target=worker, daemon=True).start()

    def sync_proxy_state(self) -> None:
        """Синхронизирует состояние прокси с реальностью (без сети)."""
        try:
            state = xray.status()
        except Exception:  # noqa: BLE001
            return
        if not self.proxy.get("busy"):
            self.proxy = {**self.proxy, **state, "running": state.get("running", False)}

    def check_stream_fallback(self) -> None:
        """Уводит YouTube/Discord в прокси, если zapret не работает.

        Обычно их закрывает zapret, и через сервер они не идут. Но когда
        ``winws.exe`` не запущен, напрямую они уже не откроются — тогда
        включается резервный режим, а после восстановления zapret выключается.
        """
        if not self.cfg.get("xray_stream_fallback", True):
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
            self.notify("YouTube/Discord: " + ("через VPN (zapret не работает)"
                                               if need else "снова напрямую"))
        self._signature = None

    # ---- статусы -----------------------------------------------------------
    def refresh(self) -> None:
        self.status = status.collect(self.cfg)
        self.sync_proxy_state()
        try:
            settings = happ.routing_settings()
            self.happ = {
                "connected": happ.is_connected(),
                "routing": str(settings.get("useRouting", "")).lower() == "true",
                "active": self.status.get("happ_routing", {}).get("active") or "",
            }
        except Exception:  # noqa: BLE001
            pass
        # Happ управляется из виджета, поэтому его окно не должно всплывать.
        if self.happ.get("routing"):
            self._hide_tick = getattr(self, "_hide_tick", 0) + 1
            if self._hide_tick % 3 == 0:
                try:
                    happ.hide_window()
                except Exception:  # noqa: BLE001
                    pass
        self.update_icon()
        hours = float(self.cfg.get("update_check_hours") or 24)
        if time.time() - self.last_check > hours * 3600:
            self.last_check = time.time()
            self.check_updates(notify=True, background=True)

    def health(self) -> tuple[str, str, str]:
        """(состояние, цвет, подсказка) — приоритет по SPEC §2.1."""
        state = self.status or {}
        if not state:
            return "idle", STATE_COLORS["dim"], "bypass-tray · опрос…"
        if not (state.get("happ") or {}).get("running"):
            return "happ", STATE_COLORS["bad"], "Happ не запущен"
        if not (state.get("zapret") or {}).get("running"):
            return "zapret", STATE_COLORS["warn"], "zapret (winws) не запущен"
        if not (state.get("tgwsproxy") or {}).get("listening"):
            return "tgws", STATE_COLORS["warn"], "tg-ws-proxy не слушает порт"
        note = self.state.get("updates_note")
        if note:
            return "update", STATE_COLORS["warn"], note
        return "ok", STATE_COLORS["ok"], "всё в порядке"

    def update_icon(self) -> None:
        if self._icon is None:
            return
        key, color, title = self.health()
        signature = (key, title, self._menu_signature())
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
        routing = state.get("happ_routing") or {}
        return (
            (state.get("happ") or {}).get("running"),
            zapret.get("running"),
            zapret.get("strategy"),
            tuple(zapret.get("bats") or ()),
            tgws.get("listening"),
            routing.get("active"),
            self.state.get("updates_note"),
        )

    # ---- обновления --------------------------------------------------------
    def check_updates(self, notify: bool = True, background: bool = False) -> None:
        if background:
            if self._checking:
                return
            self._checking = True

            def worker() -> None:
                try:
                    self._check_updates_now(notify)
                finally:
                    self._checking = False

            threading.Thread(target=worker, daemon=True).start()
            return
        self._check_updates_now(notify)

    def _check_updates_now(self, notify: bool) -> None:
        out = updates.check(self.state)
        new = out["new"]
        if new:
            self.state["updates_note"] = "Обновления: " + ", ".join(new)
            if notify:
                self.notify(self.state["updates_note"])
        else:
            self.state.pop("updates_note", None)
        config.save_state(self.state)
        self._signature = None
        self.update_icon()

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
        happ = "запущен" if (state.get("happ") or {}).get("running") else "выключен"
        zapret = state.get("zapret") or {}
        zapret_text = "запущен" if zapret.get("running") else "выключен"
        strategy = zapret.get("strategy") or zapret.get("digest") or ""
        if strategy:
            zapret_text = f"{zapret_text} · {strategy}"
        tgws = state.get("tgwsproxy") or {}
        tgws_text = f"слушает :{tgws.get('port', 1443)}" if tgws.get("listening") else "не слушает"
        routing = state.get("happ_routing") or {}
        route_text = routing.get("active") or "—"
        tunnel_text = "подключён" if self.happ.get("connected") else "не подключён"
        if self.happ.get("routing"):
            tunnel_text += " · маршруты вкл"
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
            pystray.MenuItem(f"Happ: {happ}", None, enabled=False),
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
            pystray.MenuItem("Открыть Happ", lambda icon, item: self.run_action(actions.open_happ, self.cfg)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Проверить обновления", lambda icon, item: self.run_action(self.check_updates, True)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", lambda icon, item: self.stop()),
        )

    def set_strategy(self, name: str) -> None:
        self.cfg["zapret_bat"] = name
        config.save_config(self.cfg)
        self._signature = None
        self.update_icon()

    # ---- туннелирование Happ ----------------------------------------------
    def set_tunneling(self, enable: bool, fallback: bool = False) -> None:
        """Включает или выключает маршрутизацию Happ (долгая операция).

        Включение проверяет связь и само откатывается, если российские сайты
        перестали открываться, — Happ в TUN-режиме меняет всю сетевую картину.
        """
        if self.tunneling.get("running"):
            return
        self.tunneling = {"running": True, "message": "Подключаю Happ…" if enable else "Отключаю…"}
        self._signature = None
        self.update_icon()

        def worker() -> None:
            try:
                if enable:
                    result = happ.activate(self.cfg, fallback=fallback,
                                           extra=self.cfg.get("happ_extra_sites") or None)
                else:
                    result = happ.deactivate(self.cfg)
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "reason": str(exc)}
            if result.get("ok"):
                if enable:
                    unblocked = result.get("unblocked") or []
                    message = "Туннель включён"
                    if unblocked:
                        message += ": " + ", ".join(unblocked)
                    message += f" · профиль «{result.get('active')}»"
                else:
                    message = "Туннель выключен"
            else:
                message = "Не удалось: " + str(result.get("reason") or result.get("error") or "?")
            self.tunneling = {"running": False, "message": message, "result": result}
            self.notify(message)
            self.refresh()

        threading.Thread(target=worker, daemon=True).start()

    def recover_pending(self) -> None:
        """Откатывает незавершённое включение туннеля при старте."""
        try:
            if happ.recover_if_pending(self.cfg):
                self.notify("Туннель: прошлая попытка не подтвердилась, настройки возвращены")
        except Exception:  # noqa: BLE001
            pass

    def auto_fallback_needed(self) -> bool:
        """Нужно ли уводить YouTube/Discord в туннель.

        Да, если маршрутизация Happ включена, а zapret или tg-ws-proxy не
        работают: тогда напрямую эти сервисы уже не открыть.
        """
        if not self.happ.get("routing"):
            return False
        state = self.status or {}
        zapret_ok = bool((state.get("zapret") or {}).get("running"))
        tgws_ok = bool((state.get("tgwsproxy") or {}).get("listening"))
        return not (zapret_ok and tgws_ok)

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
        self._signature = None

        command = [
            *runtime.module_argv("--sweep-strategies"),
            "--out", str(result_path), "--report", str(progress_path),
        ]
        launched = actions.run_elevated(command)
        if not launched:
            self.sweep = {"running": False, "message": "Запрос прав отклонён"}
            self.update_icon()
            return False

        threading.Thread(
            target=self._watch_sweep, args=(result_path, progress_path), daemon=True
        ).start()
        return True

    def _watch_sweep(self, result_path, progress_path) -> None:
        deadline = time.time() + 900
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
            message = f"Лучшая стратегия: {report['best']}"
            if report.get("installed"):
                message += " (установлена)"
            self.sweep["message"] = message
            self.notify(message)
        else:
            self.sweep["message"] = report.get("results") and "Рабочая стратегия не найдена" or "Перебор не завершён"
            self.notify("Автоподбор: " + self.sweep["message"])
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
        threading.Thread(target=self.recover_pending, daemon=True).start()
        icon = icons.make_icon("idle", STATE_COLORS["dim"])
        self._icon = pystray.Icon("bypass-tray", icon, "bypass-tray", menu=self.build_menu())
        threading.Thread(target=self._loop, daemon=True).start()
        self._icon.run()

    def _check_auto_fallback(self) -> None:
        """Держит профиль в согласии с состоянием обхода.

        Если маршруты Happ включены, а zapret или прокси упали, YouTube и
        Discord напрямую уже не открыть — их уводит резервный профиль. Когда
        обход возвращается, профиль переключается обратно.
        """
        if self.tunneling.get("running") or self.sweep.get("running"):
            return
        active = happ.active_profile()
        need = self.auto_fallback_needed()
        on_fallback = active == happ.FALLBACK_PROFILE
        if need == on_fallback:
            self._fallback_since = None
            return
        now = time.time()
        if self._fallback_since is None:
            self._fallback_since = now
            return
        limit = 90.0 if need else 120.0
        if now - self._fallback_since >= limit:
            self._fallback_since = None
            self.set_tunneling(True, fallback=need)

    def _loop(self) -> None:
        while True:
            try:
                self.refresh()
                self.check_proxy_health()
                self.check_stream_fallback()
                if self.cfg.get("happ_auto_fallback"):
                    self._check_auto_fallback()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(float(self.cfg.get("refresh_seconds") or 5))

    def stop(self) -> None:
        self.panel.close()
        if self._icon is not None:
            self._icon.stop()
