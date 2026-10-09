"""Окно-панель: карточки статусов, действия, стратегия, маршруты Happ.

Окно живёт в собственном потоке (Tkinter требует, чтобы весь UI трогал только
один поток). Связь с остальным приложением — через потокобезопасную очередь
команд и периодический опрос ``controller.status`` из ``after``.
"""

from __future__ import annotations

import gc
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

from PIL import ImageTk

from . import actions, config, icons
from .settings_ui import SettingsWindow
from .theme import STATE_COLORS, Theme, resolve_dark
from .widgets import Card, Divider, PillButton, StatusDot

WINDOW_W = 640
POLL_MS = 1500
TICK_MS = 120


class Panel:
    def __init__(self, controller):
        self.controller = controller
        self._thread: threading.Thread | None = None
        self._root: tk.Tk | None = None
        self._queue: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()
        self._theme = Theme(False)
        self._rows: dict[str, dict] = {}
        self._combobox: ttk.Combobox | None = None
        self._strategy_var: tk.StringVar | None = None
        self._hint: tk.Label | None = None
        self._footer: tk.Label | None = None
        self._pill_dot: StatusDot | None = None
        self._pill_text: tk.Label | None = None
        self._images: list = []

    # ---- публичный интерфейс ---------------------------------------------
    def open(self) -> None:
        """Показать панель (можно вызывать из любого потока)."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                self._queue.put("show")
                return
            self._thread = threading.Thread(target=self._run, daemon=True, name="panel")
            self._thread.start()

    def close(self) -> None:
        self._queue.put("close")

    def show_settings(self) -> None:
        """Открыть настройки (можно вызывать из трея)."""
        self._queue.put("settings")
        self.open()

    def _open_settings(self) -> None:
        root = self._root
        if root is None:
            return
        if getattr(self, "_settings", None) is None:
            self._settings = SettingsWindow(root, self._theme, self.controller)
        self._settings.open()

    # ---- жизненный цикл ----------------------------------------------------
    def _run(self) -> None:
        """Поднимает окно в отдельном потоке.

        Tk-интерпретатор создаётся один раз: закрытие окна лишь прячет его
        (``withdraw``), а ``destroy`` выполняется только при выходе из
        приложения. Иначе каждый показ панели плодил бы интерпретаторы Tcl.
        """
        theme = Theme(resolve_dark(self.controller.cfg.get("theme", "auto")))
        self._theme = theme
        root = tk.Tk()
        self._root = root
        try:
            self._build(root, theme)
        except Exception:  # noqa: BLE001
            self._release()
            root.destroy()
            raise
        root.after(TICK_MS, self._pump)
        root.after(POLL_MS, self._tick)
        try:
            root.mainloop()
        finally:
            self._release()
            del root
            # Tk образует циклы ссылок; собираем их здесь, в потоке-владельце
            # интерпретатора, иначе он будет освобождён в чужом потоке и Python
            # сообщит об утечке интерпретатора Tcl (gh-83274).
            gc.collect()

    def _release(self) -> None:
        """Забывает Tk-объекты в том же потоке, который их создал."""
        self._root = None
        self._rows = {}
        self._combobox = None
        self._strategy_var = None
        self._hint = None
        self._footer = None
        self._pill_dot = None
        self._pill_text = None
        self._images = []
        theme = getattr(self, "_theme", None)
        if theme is not None:
            theme.release()

    def _pump(self) -> None:
        root = self._root
        if root is None:
            return
        try:
            while True:
                command = self._queue.get_nowait()
                if command == "close":
                    root.destroy()
                    return
                if command == "show":
                    root.deiconify()
                    root.lift()
                    root.attributes("-topmost", True)
                    root.after(300, lambda: root.attributes("-topmost", False))
                    root.focus_force()
                if command == "settings":
                    self._open_settings()
        except queue.Empty:
            pass
        except tk.TclError:
            self._root = None
            return
        root.after(TICK_MS, self._pump)

    def _tick(self) -> None:
        root = self._root
        if root is None:
            return
        try:
            self._render()
        except tk.TclError:
            self._root = None
            return
        root.after(POLL_MS, self._tick)

    def _settle(self, root: tk.Tk) -> tuple[int, int]:
        """Даёт разметке устояться и возвращает требуемый размер окна.

        Карточки меняют высоту по событию ``<Configure>``, а такие события
        доставляются только полным ``update()`` — ``update_idletasks()`` их не
        обрабатывает, поэтому размер окна считается «на глаз» и контент
        обрезается.
        """
        previous: tuple[int, int] | None = None
        size = (WINDOW_W, root.winfo_reqheight())
        for _ in range(12):
            root.update()
            size = (max(WINDOW_W, root.winfo_reqwidth()), root.winfo_reqheight())
            if size == previous:
                break
            previous = size
        return size

    # ---- построение интерфейса --------------------------------------------
    def _build(self, root: tk.Tk, theme: Theme) -> None:
        root.title("Обходы")
        root.configure(bg=theme.bg)
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self._hide)

        app_icon = ImageTk.PhotoImage(icons.make_app_icon(64))
        self._images.append(app_icon)
        try:
            root.iconphoto(True, app_icon)
        except tk.TclError:
            pass

        self._style(root, theme)
        self._build_header(root, theme)

        body = tk.Frame(root, bg=theme.bg)
        body.pack(fill="both", expand=True, padx=16, pady=(14, 6))

        self._build_status_card(body, theme)
        self._build_strategy_card(body, theme)
        self._build_routing_card(body, theme)
        self._build_updates_card(body, theme)

        self._footer = tk.Label(
            root, text="", bg=theme.bg, fg=theme.text_faint,
            font=theme.font(8), anchor="w", justify="left",
        )
        self._footer.pack(fill="x", padx=18, pady=(4, 12))

        self._render()
        self._place(root)

    def _place(self, root: tk.Tk) -> None:
        """Центрирует окно по экрану.

        Размер окна не фиксируется: Tk сам подстраивает его под содержимое,
        поэтому карточки не обрезаются при изменении текста.
        """
        width, height = self._settle(root)
        root.minsize(WINDOW_W, 1)
        x = int(root.winfo_screenwidth() / 2 - width / 2)
        y = int(root.winfo_screenheight() / 2 - height / 2)
        root.geometry(f"+{max(0, x)}+{max(0, y)}")
        root.update()

    def _style(self, root: tk.Tk, theme: Theme) -> None:
        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "TCombobox",
            fieldbackground=theme.btn,
            background=theme.btn,
            foreground=theme.text,
            arrowcolor=theme.text_dim,
            bordercolor=theme.card_border,
            lightcolor=theme.card_border,
            darkcolor=theme.card_border,
            padding=6,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", theme.btn)],
            foreground=[("readonly", theme.text)],
        )
        root.option_add("*TCombobox*Listbox.background", theme.card)
        root.option_add("*TCombobox*Listbox.foreground", theme.text)
        root.option_add("*TCombobox*Listbox.selectBackground", theme.accent)
        root.option_add("*TCombobox*Listbox.selectForeground", theme.accent_text)
        root.option_add("*TCombobox*Listbox.font", theme.font(9))

    def _build_header(self, root: tk.Tk, theme: Theme) -> None:
        header = tk.Frame(root, bg=theme.header)
        header.pack(fill="x")

        inner = tk.Frame(header, bg=theme.header)
        inner.pack(fill="x", padx=16, pady=14)

        image = ImageTk.PhotoImage(icons.make_app_icon(46))
        self._images.append(image)
        badge = tk.Canvas(inner, width=46, height=46, bg=theme.header,
                          highlightthickness=0, bd=0)
        badge.create_image(0, 0, anchor="nw", image=image)
        badge.pack(side="left", padx=(0, 12))

        titles = tk.Frame(inner, bg=theme.header)
        titles.pack(side="left", fill="y")
        tk.Label(titles, text="Обходы", bg=theme.header, fg=theme.header_text,
                 font=theme.font(15, "bold"), anchor="w").pack(anchor="w")
        tk.Label(titles, text="Happ · zapret · tg-ws-proxy", bg=theme.header,
                 fg=theme.header_dim, font=theme.font(9), anchor="w").pack(anchor="w")

        pill = tk.Frame(inner, bg=theme.header)
        pill.pack(side="right")
        self._pill_dot = StatusDot(pill, theme, size=11, bg=theme.header)
        self._pill_dot.pack(side="left", padx=(0, 7))
        self._pill_text = tk.Label(pill, text="", bg=theme.header,
                                   fg=theme.header_text, font=theme.font(10, "bold"))
        self._pill_text.pack(side="left")

        PillButton(inner, theme, "Настройки", self._open_settings,
                   kind="secondary", bg=theme.header).pack(side="right", padx=(0, 12))

    def _card(self, parent: tk.Misc, theme: Theme) -> tk.Frame:
        card = Card(parent, theme)
        card.pack(fill="x", pady=(0, 10))
        return card.inner

    def _row(self, parent: tk.Misc, theme: Theme, key: str, title: str,
             buttons: list[tuple[str, str, object, tuple]]) -> tk.Frame:
        """Строка статуса: индикатор, название, значение и кнопки.

        Каждая кнопка — ``(текст, вид, функция, аргументы)``; функция
        выполняется в фоне, чтобы не подвешивать окно.
        """
        row = tk.Frame(parent, bg=theme.card)
        row.pack(fill="x", pady=1)
        row.columnconfigure(1, weight=1)

        dot = StatusDot(row, theme, size=12)
        dot.grid(row=0, column=0, rowspan=2, padx=(0, 11), sticky="n")

        name = tk.Label(row, text=title, bg=theme.card, fg=theme.text,
                        font=theme.font(10, "bold"), anchor="w")
        name.grid(row=0, column=1, sticky="w")

        detail = tk.Label(row, text="", bg=theme.card, fg=theme.text_faint,
                          font=theme.font(8), anchor="w")
        detail.grid(row=1, column=1, sticky="w")

        value = tk.Label(row, text="", bg=theme.card, fg=theme.text_dim,
                         font=theme.font(9), anchor="e")
        value.grid(row=0, column=2, rowspan=2, sticky="e", padx=(10, 2))

        holder = tk.Frame(row, bg=theme.card)
        holder.grid(row=0, column=3, rowspan=2, sticky="e", padx=(8, 0))
        made: dict[str, PillButton] = {}
        for text, kind, fn, args in buttons:
            button = PillButton(holder, theme, text, self._async(fn, *args), kind=kind)
            button.pack(side="left", padx=(6, 0))
            made[text] = button

        self._rows[key] = {
            "dot": dot, "name": name, "detail": detail,
            "value": value, "buttons": made, "holder": holder,
        }
        return row

    def _build_status_card(self, parent: tk.Misc, theme: Theme) -> None:
        cfg = self.controller.cfg
        inner = self._card(parent, theme)
        tk.Label(inner, text="СОСТОЯНИЕ", bg=theme.card, fg=theme.text_faint,
                 font=theme.font(8, "bold"), anchor="w").pack(fill="x", pady=(0, 9))

        self._row(inner, theme, "happ", "Happ",
                  [("Открыть", "secondary", actions.open_happ, (cfg,))])
        Divider(inner, theme).pack(fill="x", pady=9)
        self._row(inner, theme, "zapret", "zapret",
                  [("ЗАПУСТИТЬ", "primary", actions.start_zapret, (cfg,)),
                   ("Остановить", "secondary", actions.stop_zapret, (cfg,))])
        Divider(inner, theme).pack(fill="x", pady=9)
        self._row(inner, theme, "tgws", "tg-ws-proxy",
                  [("Запустить", "secondary", actions.start_tgws, (cfg,))])

    def _build_strategy_card(self, parent: tk.Misc, theme: Theme) -> None:
        inner = self._card(parent, theme)
        tk.Label(inner, text="СТРАТЕГИЯ ZAPRET", bg=theme.card, fg=theme.text_faint,
                 font=theme.font(8, "bold"), anchor="w").pack(fill="x", pady=(0, 9))

        line = tk.Frame(inner, bg=theme.card)
        line.pack(fill="x")
        self._strategy_var = tk.StringVar(value=self.controller.cfg.get("zapret_bat", ""))
        self._combobox = ttk.Combobox(
            line, textvariable=self._strategy_var, state="readonly",
            font=theme.font(9), width=32,
        )
        self._combobox.pack(side="left", fill="x", expand=True)
        PillButton(line, theme, "Применить", self._apply_strategy, kind="primary").pack(
            side="left", padx=(8, 0)
        )

        self._hint = tk.Label(inner, text="", bg=theme.card, fg=theme.text_faint,
                              font=theme.font(8), anchor="w", justify="left",
                              wraplength=WINDOW_W - 80)
        self._hint.pack(fill="x", pady=(9, 0))

        auto = tk.Frame(inner, bg=theme.card)
        auto.pack(fill="x", pady=(11, 0))
        PillButton(auto, theme, "Подобрать лучшую стратегию",
                   self._async(self.controller.start_sweep), kind="secondary").pack(side="left")
        self._rows["sweep"] = {
            "value": tk.Label(auto, text="", bg=theme.card, fg=theme.text_faint,
                              font=theme.font(8), anchor="w", justify="left",
                              wraplength=WINDOW_W - 260),
        }
        self._rows["sweep"]["value"].pack(side="left", padx=(10, 0), fill="x", expand=True)

    def _build_routing_card(self, parent: tk.Misc, theme: Theme) -> None:
        inner = self._card(parent, theme)
        tk.Label(inner, text="ПРОКСИ XRAY", bg=theme.card, fg=theme.text_faint,
                 font=theme.font(8, "bold"), anchor="w").pack(fill="x", pady=(0, 9))

        head = tk.Frame(inner, bg=theme.card)
        head.pack(fill="x")
        self._rows["route"] = {
            "value": tk.Label(head, text="", bg=theme.card, fg=theme.text,
                              font=theme.font(9, "bold"), anchor="w"),
        }
        self._rows["route"]["value"].pack(fill="x")

        buttons = tk.Frame(inner, bg=theme.card)
        buttons.pack(fill="x", pady=(8, 0))
        self._rows["route_buttons"] = {
            "on": PillButton(buttons, theme, "Подключить",
                             self._async(self.controller.set_proxy, True), kind="primary"),
            "off": PillButton(buttons, theme, "Отключить",
                              self._async(self.controller.set_proxy, False), kind="secondary"),
            "server": PillButton(buttons, theme, "Другой сервер",
                                 self._async(self.controller.set_proxy, True), kind="secondary"),
        }
        self._rows["route_buttons"]["on"].pack(side="left")
        self._rows["route_buttons"]["off"].pack(side="left", padx=(6, 0))
        self._rows["route_buttons"]["server"].pack(side="left", padx=(6, 0))

        checks = tk.Frame(inner, bg=theme.card)
        checks.pack(fill="x", pady=(7, 0))
        for key, text in (("tunnel", "Локальный прокси"),
                          ("yt", "Российские сервисы — напрямую"),
                          ("social", "Заблокированные сайты — через сервер")):
            label = tk.Label(checks, text=text, bg=theme.card, fg=theme.text_dim,
                             font=theme.font(9), anchor="w")
            label.pack(fill="x", pady=1)
            self._rows[key] = {"value": label}

        self._rows["route_hint"] = {
            "value": tk.Label(inner, text="", bg=theme.card, fg=theme.text_faint,
                              font=theme.font(8), anchor="w", justify="left",
                              wraplength=WINDOW_W - 80),
        }
        self._rows["route_hint"]["value"].pack(fill="x", pady=(8, 0))

        # --- выбор сервера из списка ---
        picker = tk.Frame(inner, bg=theme.card)
        picker.pack(fill="x", pady=(10, 0))
        self._server_var = tk.StringVar(value="")
        self._server_combo = ttk.Combobox(
            picker, textvariable=self._server_var, state="readonly",
            font=theme.font(9), width=30,
        )
        self._server_combo.pack(side="left", fill="x", expand=True)
        PillButton(picker, theme, "Поставить", self._apply_server, kind="primary").pack(
            side="left", padx=(8, 0))
        PillButton(picker, theme, "Обновить",
                   self._async(self.controller.load_servers, True),
                   kind="secondary").pack(side="left", padx=(6, 0))

    def _build_updates_card(self, parent: tk.Misc, theme: Theme) -> None:
        inner = self._card(parent, theme)
        row = tk.Frame(inner, bg=theme.card)
        row.pack(fill="x")
        row.columnconfigure(1, weight=1)
        tk.Label(row, text="ОБНОВЛЕНИЯ", bg=theme.card, fg=theme.text_faint,
                 font=theme.font(8, "bold"), anchor="w").grid(row=0, column=0, sticky="w")
        self._rows["updates"] = {
            "value": tk.Label(row, text="", bg=theme.card, fg=theme.text_dim,
                              font=theme.font(9), anchor="w"),
        }
        self._rows["updates"]["value"].grid(row=1, column=0, columnspan=2,
                                            sticky="w", pady=(6, 0))
        PillButton(row, theme, "Проверить", self._async(self.controller.check_updates, True),
                   kind="secondary").grid(row=0, column=2, rowspan=2, sticky="e")

    # ---- обновление данных -------------------------------------------------
    def _render(self) -> None:
        theme = self._theme
        state = self.controller.status or {}
        happ = state.get("happ") or {}
        zapret = state.get("zapret") or {}
        tgws = state.get("tgwsproxy") or {}
        routing = state.get("happ_routing") or {}

        key, color, title = self.controller.health()
        if self._pill_dot is not None:
            self._pill_dot.set(color)
        if self._pill_text is not None:
            labels = {"ok": "всё в порядке", "happ": "Happ выключен",
                      "zapret": "zapret выключен", "tgws": "прокси не слушает",
                      "update": "есть обновления", "idle": "опрос…"}
            self._pill_text.configure(text=labels.get(key, title))

        # Happ
        row = self._rows.get("happ")
        if row:
            running = bool(happ.get("running"))
            row["dot"].set(theme.ok if running else theme.bad)
            row["value"].configure(text="запущен" if running else "выключен",
                                   fg=theme.ok if running else theme.bad)
            row["detail"].configure(text=happ.get("process") or "процесс не найден")

        # zapret
        row = self._rows.get("zapret")
        if row:
            running = bool(zapret.get("running"))
            row["dot"].set(theme.ok if running else theme.warn)
            row["value"].configure(text="запущен" if running else "выключен",
                                   fg=theme.ok if running else theme.warn)
            parts = []
            if zapret.get("strategy"):
                parts.append(zapret["strategy"])
            if zapret.get("digest"):
                parts.append(zapret["digest"])
            if not parts and zapret.get("dir"):
                parts.append("стратегия не определена")
            row["detail"].configure(text=" · ".join(parts) or "winws.exe не найден")
            self._sync_zapret_buttons(row, bool(zapret.get("as_service")))

        # tg-ws-proxy
        row = self._rows.get("tgws")
        if row:
            listening = bool(tgws.get("listening"))
            row["dot"].set(theme.ok if listening else theme.warn)
            row["value"].configure(
                text=f"слушает :{tgws.get('port', 1443)}" if listening else "не слушает",
                fg=theme.ok if listening else theme.warn,
            )
            row["detail"].configure(
                text="порт открыт" if listening else "порт закрыт — Telegram без ускорения"
            )

        self._render_routing(routing)
        self._render_server_picker()
        self._render_updates()
        self._render_hint(zapret)
        self._render_sweep()

        bats = zapret.get("bats") or []
        if self._combobox is not None and list(self._combobox["values"]) != bats:
            self._combobox["values"] = bats

        if self._footer is not None:
            stamp = time.strftime("%H:%M:%S")
            note = "" if state.get("admin") else "  ·  без прав администратора"
            self._footer.configure(text=f"Обновлено в {stamp}{note}")

    def _sync_zapret_buttons(self, row: dict, service_mode: bool) -> None:
        buttons = row.get("buttons") or {}
        primary = buttons.get("ЗАПУСТИТЬ")
        if primary is None:
            return
        if service_mode:
            primary.set_text("Перезапустить")
            primary.command = self._async(actions.control_service, "restart")
            stop = buttons.get("Остановить")
            if stop is not None:
                stop.set_text("Остановить")
                stop.command = self._async(actions.control_service, "stop")
        else:
            primary.set_text("Запустить")
            primary.command = self._async(actions.start_zapret, self.controller.cfg)
            stop = buttons.get("Остановить")
            if stop is not None:
                stop.set_text("Остановить")
                stop.command = self._async(actions.stop_zapret, self.controller.cfg)

    def _render_routing(self, routing: dict) -> None:
        theme = self._theme
        controller = self.controller
        proxy = getattr(controller, "proxy", {}) or {}
        running = bool(proxy.get("running"))
        busy = bool(proxy.get("busy"))

        row = self._rows.get("route")
        if row:
            if busy:
                row["value"].configure(text=proxy.get("message") or "работаю…",
                                       fg=theme.accent)
            elif running:
                latency = proxy.get("latency", -1)
                tail = f" · {latency} мс" if isinstance(latency, int) and latency >= 0 else ""
                row["value"].configure(
                    text=f"подключено · {proxy.get('server') or 'сервер'}{tail}",
                    fg=theme.ok)
            else:
                row["value"].configure(
                    text=proxy.get("message") or "не подключено", fg=theme.text_dim)

        buttons = self._rows.get("route_buttons") or {}
        for key, enabled in (("on", not running), ("off", running),
                             ("server", not busy)):
            button = buttons.get(key)
            if button is not None:
                button.set_enabled(enabled and not busy)

        tunnel = self._rows.get("tunnel")
        if tunnel:
            port = proxy.get("port") or self.controller.cfg.get("xray_port", 10818)
            tunnel["value"].configure(
                text=f"{'✓' if running else '—'}  Локальный прокси "
                     + (f"127.0.0.1:{port}" if running else "выключен"),
                fg=theme.ok if running else theme.text_faint)

        yt = self._rows.get("yt")
        if yt:
            stream = bool(proxy.get("stream"))
            if running and stream:
                text = "⚠  YouTube/Discord — через сервер (zapret не работает)"
                color = theme.warn
            elif running:
                text = "✓  YouTube/Discord — напрямую под zapret"
                color = theme.ok
            else:
                text = "—  YouTube/Discord — напрямую под zapret"
                color = theme.text_faint
            yt["value"].configure(text=text, fg=color)
        social = self._rows.get("social")
        if social:
            mark, color = ("✓", theme.ok) if running else ("—", theme.text_faint)
            social["value"].configure(
                text=f"{mark}  Instagram/X и заблокированные — через сервер", fg=color)

        hint = self._rows.get("route_hint")
        if hint:
            if busy:
                text = proxy.get("message") or ""
            elif running:
                system = ("Системный прокси включён — браузеры уже используют его."
                          if proxy.get("system_proxy") else
                          "Системный прокси выключен: укажите 127.0.0.1:"
                          f"{proxy.get('port', 10818)} в браузере вручную.")
                if proxy.get("stream"):
                    text = (f"Работает без Happ и без прав администратора. {system} "
                            "zapret не запущен, поэтому YouTube и Discord временно "
                            "идут через сервер. Российские сервисы — напрямую. "
                            "Как только zapret вернётся, они снова пойдут напрямую.")
                else:
                    text = (f"Работает без Happ и без прав администратора. {system} "
                            "Заблокированные сайты идут через сервер, российские "
                            "сервисы и YouTube с Discord — напрямую (их закрывает "
                            "zapret). Если zapret остановится, YouTube и Discord "
                            "автоматически уйдут через сервер.")
            else:
                text = ("Свой клиент на ядре Xray: подписка берётся напрямую, из "
                        "доступных серверов выбирается самый быстрый по задержке. "
                        "Права администратора не нужны, TUN не используется, "
                        "zapret продолжает работать как обычно. Через сервер пойдут "
                        "только заблокированные сайты; YouTube и Discord останутся "
                        "напрямую под zapret. «Подключить» пропишет прокси в систему "
                        "для браузеров.")
            hint["value"].configure(text=text)

    def _apply_server(self) -> None:
        name = self._server_var.get() if self._server_var else ""
        if not name:
            return
        # В списке показываем «название — задержка», на сервер отправляем имя.
        clean = name.rsplit(" — ", 1)[0]
        self.controller.select_server(clean)

    def _render_server_picker(self) -> None:
        combo = getattr(self, "_server_combo", None)
        if combo is None:
            return
        servers = getattr(self.controller, "servers", None) or []
        labels = [f"{s.get('name', '?')} — {s.get('latency', '?')} мс" for s in servers]
        if list(combo["values"]) != labels:
            combo["values"] = labels
        current = self.controller.proxy.get("server") or ""
        if current and not self._server_var.get():
            match = next((l for l in labels if l.startswith(current + " —") or
                          l.rsplit(" — ", 1)[0] == current), "")
            if match:
                self._server_var.set(match)

    def _render_updates(self) -> None:
        theme = self._theme
        row = self._rows.get("updates")
        if not row:
            return
        note = self.controller.state.get("updates_note")
        row["value"].configure(text=note or "нет новых релизов",
                               fg=theme.warn if note else theme.text_dim)

    def _render_sweep(self) -> None:
        theme = self._theme
        row = self._rows.get("sweep")
        if not row:
            return
        sweep = getattr(self.controller, "sweep", {}) or {}
        if sweep.get("running"):
            index = sweep.get("index") or 0
            total = sweep.get("total") or 0
            prefix = f"{index}/{total} · " if total else ""
            text = prefix + (sweep.get("message") or "идёт перебор…")
            row["value"].configure(text=text, fg=theme.accent)
        else:
            last = (self.controller.state or {}).get("last_sweep") or {}
            if last.get("best"):
                text = f"Последний подбор: {last['best']}"
            else:
                text = sweep.get("message") or "проверит все general*.bat и оставит рабочую"
            row["value"].configure(text=text, fg=theme.text_faint)

    def _render_hint(self, zapret: dict) -> None:
        if self._hint is None:
            return
        if zapret.get("as_service"):
            text = ("zapret установлен службой: запуск и остановка выполняются через "
                    "службу (нужен запрос UAC). Смена стратегии — кнопкой "
                    "«Сменить стратегию службы» в меню трея.")
        elif zapret.get("running") and not zapret.get("strategy"):
            text = "Стратегию не удалось сопоставить с general*.bat."
        else:
            text = "Выбор сохраняется и применяется при запуске zapret."
        self._hint.configure(text=text)

    # ---- действия ----------------------------------------------------------
    def _async(self, fn, *args):
        """Оборачивает действие: выполняется в фоне, UI не блокируется."""

        def run() -> None:
            threading.Thread(
                target=self.controller.run_action, args=(fn, *args), daemon=True
            ).start()
            root = self._root
            if root is not None:
                try:
                    root.after(400, self._render)
                except tk.TclError:
                    pass

        return run

    def _apply_strategy(self) -> None:
        if not self._strategy_var:
            return
        self.controller.cfg["zapret_bat"] = self._strategy_var.get()
        config.save_config(self.controller.cfg)
        zapret = (self.controller.status or {}).get("zapret") or {}
        if zapret.get("as_service") and self._hint is not None:
            self._hint.configure(
                text="Сохранено. Служба использует свою стратегию — смените её "
                     "через «Сменить стратегию службы» в меню трея."
            )

    def _hide(self) -> None:
        if self._root is not None:
            try:
                self._root.withdraw()
            except tk.TclError:
                pass
