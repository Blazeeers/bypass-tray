"""Настройки «Обходов»: страница внутри панели и совместимое окно.

Основной вариант — :class:`SettingsPage`: тот же контент, что раньше был в
отдельном Toplevel, но без собственной шапки. Панель встраивает страницу и
анимирует переход. :class:`SettingsWindow` оставлен тонкой обёрткой (Toplevel)
для тестов и совместимости.
"""

from __future__ import annotations

import threading
import time
import tkinter as tk
from tkinter import ttk

from . import activity, blocked, components, config, routes, xray
from .theme import Theme
from .widgets import (
    Chip, PillButton, ScrollBody, Segmented, ToggleSwitch, Tooltip, rounded_rect,
)

BEHAVIOR_TIPS = {
    "autostart": "Запускать «Обходы» при входе в Windows и подключать VPN, "
                 "если включено автоподключение.",
    "quiet": "Когда всё работает, иконка серая и не отвлекает; при проблеме "
             "снова цветная.",
    "guard": "Раз в 10 минут проверять сервисы и лечить: поднимать "
             "tg-ws-proxy, менять сервер, обновлять маршруты.",
    "youtube": "Вести YouTube и Discord через VPN, если видео застревает. "
               "Скорость зависит от сервера.",
}


class SettingsPage(tk.Frame):
    """Страница настроек без шапки: поведение, VPN, список, маршруты, пути."""

    WIDTH = 720

    def __init__(self, master: tk.Misc, theme: Theme, controller,
                 on_close=None):
        super().__init__(master, bg=theme.bg)
        self.theme = theme
        self.controller = controller
        self._on_close = on_close
        self._busy = False
        self.toggles: dict[str, ToggleSwitch] = {}
        self.rules: dict[str, str] = {}
        self.vars: dict[str, tk.Variable] = {}
        self._blocked_busy = False
        self._blocked_result = None
        self._blocked_text = ""
        self._blocked_run = 0
        self.tooltips: list[Tooltip] = []
        self._build()

    # ---- сборка ------------------------------------------------------------
    def _section(self, parent: tk.Misc, title: str) -> tk.Frame:
        theme = self.theme
        holder = tk.Frame(parent, bg=theme.card)
        holder.pack(fill="x", pady=(0, 12))
        tk.Label(holder, text=title, bg=theme.card, fg=theme.text_faint,
                 font=theme.font(8, "bold"), anchor="w").pack(fill="x", pady=(0, 6))
        body = tk.Frame(holder, bg=theme.card)
        body.pack(fill="x")
        return body

    def _label(self, parent: tk.Misc, text: str) -> None:
        tk.Label(parent, text=text, bg=self.theme.card, fg=self.theme.text_dim,
                 font=self.theme.font(8), anchor="w", justify="left",
                 wraplength=self.WIDTH - 90).pack(fill="x", pady=(6, 2))

    def _entry(self, parent: tk.Misc, key: str, width: int = 60) -> ttk.Entry:
        entry = ttk.Entry(parent, style="Settings.TEntry",
                          textvariable=self.vars[key], font=self.theme.font(9))
        entry.pack(fill="x", ipady=3)
        return entry

    def _build(self) -> None:
        theme = self.theme
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Settings.TCheckbutton", background=theme.card,
                        foreground=theme.text, focuscolor=theme.card,
                        font=theme.font(9))
        style.map("Settings.TCheckbutton",
                  background=[("active", theme.card)],
                  foreground=[("active", theme.text)],
                  indicatorcolor=[("selected", theme.accent),
                                  ("!selected", theme.btn)])
        style.configure("Settings.TEntry", fieldbackground=theme.btn,
                        foreground=theme.text, bordercolor=theme.card_border,
                        lightcolor=theme.card_border, darkcolor=theme.card_border)

        self._actions = tk.Frame(self, bg=theme.bg)
        self._actions.pack(side="bottom", fill="x", padx=16, pady=(8, 12))
        self.body = ScrollBody(self, theme, scrollbar=True, min_height=120)
        self.body.pack(fill="both", expand=True)
        pad = tk.Frame(self.body.inner, bg=theme.bg)
        pad.pack(fill="both", expand=True, padx=16, pady=12)

        cfg = dict(self.controller.cfg or {})
        self.vars = {
            "xray_subscription": tk.StringVar(value=cfg.get("xray_subscription", "")),
            "xray_port": tk.StringVar(value=str(cfg.get("xray_port", xray.DEFAULT_PORT))),
            "xray_system_proxy": tk.BooleanVar(value=bool(cfg.get("xray_system_proxy", True))),
            "xray_stream_fallback": tk.BooleanVar(
                value=bool(cfg.get("xray_stream_fallback", True))),
            "zapret_dir": tk.StringVar(value=cfg.get("zapret_dir", "")),
            "tgws_exe": tk.StringVar(value=cfg.get("tgws_exe", "")),
        }

        # --- поведение (три тумблера применяются сразу, YouTube — по «Сохранить»)
        behavior = self._section(pad, "ПОВЕДЕНИЕ")
        is_autostart = getattr(self.controller, "is_autostart", None)
        try:
            autostart_now = bool(is_autostart()) if callable(is_autostart) else bool(
                cfg.get("autoconnect"))
        except Exception:  # noqa: BLE001
            autostart_now = bool(cfg.get("autoconnect"))
        rows = (
            ("autostart", "Подключаться при входе", autostart_now, self._set_autostart),
            ("quiet", "Тихая иконка", bool(cfg.get("quiet_icon", True)), self._set_quiet),
            ("guard", "Автолечение", bool(cfg.get("guard_enabled", True)), self._set_guard),
            ("youtube", "YouTube через VPN",
             bool(cfg.get("youtube_via_vpn", False)), None),
        )
        for index, (key, title, value, setter) in enumerate(rows):
            row = tk.Frame(behavior, bg=theme.card)
            row.pack(fill="x", pady=(4, 2))
            row.grid_columnconfigure(0, weight=1)
            row.grid_columnconfigure(1, minsize=24)
            row.grid_columnconfigure(2, minsize=40)
            name = tk.Label(row, text=title, bg=theme.card, fg=theme.text,
                            font=theme.font(9), anchor="w")
            name.grid(row=0, column=0, sticky="w")
            tip = BEHAVIOR_TIPS[key]
            self.tooltips.append(Tooltip(name, tip))
            hint = tk.Label(row, text="?", width=2, bg=theme.card,
                            fg=theme.text_faint, font=theme.font(8, "bold"),
                            padx=0, cursor="question_arrow", anchor="e")
            hint.grid(row=0, column=1, sticky="e", padx=(8, 8))
            self.tooltips.append(Tooltip(hint, tip))
            switch = ToggleSwitch(row, theme, value, command=setter)
            switch.grid(row=0, column=2, sticky="e")
            self.tooltips.append(Tooltip(switch, tip))
            self.toggles[key] = switch
            if index < len(rows) - 1:
                tk.Frame(behavior, height=1, bg=theme.hairline).pack(fill="x", pady=3)
        tk.Label(behavior,
                 text="«YouTube через VPN» применится после «Сохранить»: если видео "
                      "застревает, трафик YouTube и Discord пойдёт через сервер.",
                 bg=theme.card, fg=theme.text_faint, font=theme.font(8),
                 anchor="w", justify="left",
                 wraplength=self.WIDTH - 90).pack(fill="x", pady=(6, 0))

        # --- VPN ---
        vpn = self._section(pad, "VPN — ССЫЛКА НА ПОДПИСКУ")
        self._label(vpn, "Вставьте ссылку на подписку (та, что выдаёт провайдер). "
                         "Она хранится только у вас в config.json.")
        self._entry(vpn, "xray_subscription")
        self._label(vpn, "Порт локального прокси (браузеры подключаются к нему):")
        self._entry(vpn, "xray_port")
        for key, text in (
            ("xray_system_proxy",
             "Прописывать прокси в систему (браузеры сразу начнут им пользоваться)"),
            ("xray_stream_fallback",
             "Уводить YouTube/Discord через VPN, когда zapret не работает"),
        ):
            tk.Checkbutton(
                vpn, text=text, variable=self.vars[key],
                bg=theme.card, fg=theme.text, selectcolor=theme.btn,
                activebackground=theme.card, activeforeground=theme.text,
                highlightthickness=0, bd=0, anchor="w",
                font=theme.font(9), cursor="hand2",
            ).pack(fill="x", pady=2)

        # --- список заблокированных ---
        blocked_section = self._section(pad, "СПИСОК ЗАБЛОКИРОВАННЫХ САЙТОВ")
        blocked_row = tk.Frame(blocked_section, bg=theme.card)
        blocked_row.pack(fill="x")
        tk.Label(blocked_row, text="Использовать список Re:filter",
                 bg=theme.card, fg=theme.text, font=theme.font(9),
                 anchor="w").pack(side="left")
        self.blocked_toggle = ToggleSwitch(
            blocked_row, theme, bool(cfg.get("blocked_list_enabled", True)))
        self.blocked_toggle.pack(side="right")
        tk.Label(blocked_section,
                 text="≈81 тыс. доменов · MIT · заблокированные идут через VPN",
                 bg=theme.card, fg=theme.text_faint, font=theme.font(8),
                 anchor="w", justify="left",
                 wraplength=self.WIDTH - 90).pack(fill="x", pady=(2, 0))
        self.blocked_status = tk.Label(
            blocked_section, text="", bg=theme.card, fg=theme.text_dim,
            font=theme.font(8), anchor="w", justify="left",
            wraplength=self.WIDTH - 90)
        self.blocked_status.pack(fill="x", pady=(6, 0))
        blocked_actions = tk.Frame(blocked_section, bg=theme.card)
        blocked_actions.pack(fill="x", pady=(6, 0))
        self.blocked_button = PillButton(
            blocked_actions, theme, "Обновить список", self._update_blocked,
            kind="secondary", height=30)
        self.blocked_button.pack(side="left")
        self.blocked_progress = tk.Label(
            blocked_actions, text="", bg=theme.card, fg=theme.text_faint,
            font=theme.font(8), anchor="w", justify="left")
        self.blocked_progress.pack(side="left", padx=(10, 0), fill="x", expand=True)
        tk.Label(blocked_section,
                 text="Свои правила выше списка. YouTube и Discord остаются под zapret.",
                 bg=theme.card, fg=theme.text_faint, font=theme.font(8),
                 anchor="w", justify="left",
                 wraplength=self.WIDTH - 90).pack(fill="x", pady=(6, 0))

        # --- маршруты сайтов ---
        site_routes = self._section(pad, "МАРШРУТЫ САЙТОВ")
        tk.Label(
            site_routes,
            text="Куда вести конкретные сайты. Своё правило важнее автоподбора. "
                 "Изменения применятся после «Сохранить» — прокси перезапустится.",
            bg=theme.card, fg=theme.text_dim, font=theme.font(8),
            anchor="w", justify="left", wraplength=self.WIDTH - 90,
        ).pack(fill="x")

        add_row = tk.Frame(site_routes, bg=theme.card)
        add_row.pack(fill="x", pady=(8, 0))
        self.route_var = tk.StringVar()
        self.route_entry = ttk.Entry(add_row, style="Settings.TEntry",
                                     textvariable=self.route_var, font=theme.font(9))
        self.route_entry.pack(side="left", fill="x", expand=True, ipady=3)
        self.route_entry.bind("<Return>", lambda _event: self._add_rule())
        self.route_segment = Segmented(
            add_row, theme, [("direct", "Напрямую"), ("proxy", "Через VPN")],
            "direct", bg=theme.card)
        self.route_segment.pack(side="left", padx=(8, 0))
        PillButton(add_row, theme, "Добавить", self._add_rule, kind="secondary",
                   height=30).pack(side="left", padx=(8, 0))
        tk.Label(site_routes, text="например: x.com или https://x.com/…",
                 bg=theme.card, fg=theme.text_faint, font=theme.font(8),
                 anchor="w").pack(fill="x", pady=(3, 0))
        self.route_status = tk.Label(
            site_routes, text="", bg=theme.card, fg=theme.text_faint,
            font=theme.font(8), anchor="w", justify="left",
            wraplength=self.WIDTH - 90)
        self.route_status.pack(fill="x", pady=(4, 6))
        self.routes_counter = tk.Label(
            site_routes, text="", bg=theme.card, fg=theme.text_faint,
            font=theme.font(8, "bold"), anchor="w")
        self.routes_counter.pack(fill="x")
        self.rules_holder = tk.Frame(site_routes, bg=theme.card)
        self.rules_holder.pack(fill="x", pady=(4, 0))
        self.rules_scroll = ScrollBody(self.rules_holder, theme, bg=theme.card,
                                       min_height=48)
        self.rules_scroll.pack(fill="x")
        self.rules_list = self.rules_scroll.inner
        self.routes_empty = tk.Label(
            site_routes,
            text="пока нет своих правил — все сайты идут по общим правилам",
            bg=theme.card, fg=theme.text_faint, font=theme.font(8),
            anchor="w", justify="left", wraplength=self.WIDTH - 90)

        # --- компоненты ---
        comp = self._section(pad, "КОМПОНЕНТЫ ОБХОДА")
        self.status_label = tk.Label(
            comp, text="", bg=theme.card, fg=theme.text_dim, font=theme.font(8),
            anchor="w", justify="left", wraplength=self.WIDTH - 90)
        self.status_label.pack(fill="x")
        row = tk.Frame(comp, bg=theme.card)
        row.pack(fill="x", pady=(8, 0))
        self.install_button = PillButton(
            row, theme, "Установить обходы", self._install_components, kind="primary")
        self.install_button.pack(side="left")
        self.progress_label = tk.Label(
            row, text="", bg=theme.card, fg=theme.text_faint, font=theme.font(8),
            anchor="w", justify="left")
        self.progress_label.pack(side="left", padx=(10, 0), fill="x", expand=True)

        # --- пути ---
        paths = self._section(pad, "ПУТИ (заполняется автоматически)")
        self._label(paths, "Папка zapret (с general*.bat и bin\\winws.exe):")
        self._entry(paths, "zapret_dir")
        self._label(paths, "Путь к TgWsProxy_windows.exe:")
        self._entry(paths, "tgws_exe")

        # --- кнопки ---
        PillButton(self._actions, theme, "Сохранить", self._save,
                   kind="primary").pack(side="left")
        self.close_button = PillButton(
            self._actions, theme, "Закрыть", self._close, kind="secondary")
        self.close_button.pack(side="left", padx=(8, 0))

        self.body.bind_tree()
        self.reload()

    # ---- данные -----------------------------------------------------------
    def revert(self) -> None:
        """Кнопка «Закрыть»: вернуть панель на главную."""
        self._busy = False
        self._blocked_busy = False
        self._blocked_result = None
        self._blocked_run += 1
        if callable(self._on_close):
            self._on_close()

    _close = revert

    def reload(self) -> None:
        """Перечитывает конфиг, не пересоздавая страницу."""
        cfg = dict(self.controller.cfg or {})
        if "xray_subscription" in self.vars:
            self.vars["xray_subscription"].set(cfg.get("xray_subscription", ""))
            self.vars["xray_port"].set(str(cfg.get("xray_port", xray.DEFAULT_PORT)))
            self.vars["xray_system_proxy"].set(bool(cfg.get("xray_system_proxy", True)))
            self.vars["xray_stream_fallback"].set(
                bool(cfg.get("xray_stream_fallback", True)))
            self.vars["zapret_dir"].set(cfg.get("zapret_dir", ""))
            self.vars["tgws_exe"].set(cfg.get("tgws_exe", ""))
        values = {
            "autostart": bool(cfg.get("autoconnect")),
            "quiet": bool(cfg.get("quiet_icon", True)),
            "guard": bool(cfg.get("guard_enabled", True)),
            "youtube": bool(cfg.get("youtube_via_vpn", False)),
        }
        for key, switch in self.toggles.items():
            switch.set(values.get(key, False))
        self.blocked_toggle.set(bool(cfg.get("blocked_list_enabled", True)))
        mapping = cfg.get("xray_routes")
        if not isinstance(mapping, dict):
            mapping = {}
        loaded: dict[str, str] = {}
        for raw, route in mapping.items():
            name = routes.normalize_user_domain(raw)
            if name and str(route) in ("direct", "proxy"):
                loaded[name] = str(route)
        self.rules = loaded
        self._blocked_busy = False
        self._blocked_result = None
        self._blocked_text = ""
        self._blocked_run += 1
        self.blocked_progress.configure(text="")
        self._render_rules()
        self._refresh_blocked_status()
        self._refresh_status()

    def height_request(self) -> int:
        """Сколько нужно окну под страницу (с прокруткой ограничивает панель)."""
        self.update_idletasks()
        return self.body.content_height() + self._actions.winfo_reqheight() + 8

    # ---- поведение (три первых применяются сразу) --------------------------
    def _set_autostart(self, value: bool) -> None:
        apply = getattr(self.controller, "set_autostart", None)
        if callable(apply):
            try:
                apply(bool(value))
            except Exception:  # noqa: BLE001
                pass

    def _set_quiet(self, value: bool) -> None:
        apply = getattr(self.controller, "set_quiet", None)
        if callable(apply):
            try:
                apply(bool(value))
            except Exception:  # noqa: BLE001
                pass

    def _set_guard(self, value: bool) -> None:
        apply = getattr(self.controller, "set_guard", None)
        if callable(apply):
            try:
                apply(bool(value))
            except Exception:  # noqa: BLE001
                pass

    # ---- список заблокированных -------------------------------------------
    @staticmethod
    def _dataset_date(version: str) -> str:
        text = str(version or "")
        if len(text) == 8 and text.isdigit():
            return f"{text[0:2]}.{text[2:4]}.{text[4:8]}"
        return text

    def _refresh_blocked_status(self) -> None:
        info = blocked.info(self.controller.cfg)
        if info.get("installed"):
            count = int(info.get("count") or 0)
            count_text = f"{count:,}".replace(",", " ")
            text = f"установлен · {count_text} доменов · MIT"
            dataset = self._dataset_date(str(info.get("version") or ""))
            if dataset:
                text += f" · набор {dataset}"
            state_at = info.get("state_at") or 0
            if state_at:
                text += " · обновлён " + time.strftime(
                    "%d.%m.%Y", time.localtime(state_at))
            color = self.theme.text_dim
        else:
            text = "не установлен — нажмите «Обновить список»"
            color = self.theme.warn
        self.blocked_status.configure(text=text, fg=color)

    def _update_blocked(self) -> None:
        if getattr(self, "_blocked_busy", False):
            return
        self._blocked_busy = True
        self._blocked_run += 1
        run = self._blocked_run
        self._blocked_result = None
        self._blocked_text = "Скачиваю список…"
        self.blocked_button.set_enabled(False)
        self.blocked_progress.configure(text=self._blocked_text,
                                        fg=self.theme.text_dim)

        def progress(percent: int, done: int, total: int) -> None:
            if self._blocked_run == run:
                self._blocked_text = f"Скачиваю: {done} из {total} МБ"

        def worker() -> None:
            result = blocked.update(progress)
            if self._blocked_run == run:
                self._blocked_result = result

        threading.Thread(target=worker, daemon=True).start()
        self._poll_blocked()

    def _alive(self) -> bool:
        try:
            return bool(self.winfo_exists())
        except tk.TclError:
            return False

    def _poll_blocked(self) -> None:
        if not self._alive():
            return
        if self._blocked_result is not None:
            result = self._blocked_result
            self._blocked_result = None
            self._blocked_updated(result)
            return
        if getattr(self, "_blocked_text", ""):
            self.blocked_progress.configure(text=self._blocked_text,
                                            fg=self.theme.text_dim)
        if getattr(self, "_blocked_busy", False):
            try:
                self.after(120, self._poll_blocked)
            except tk.TclError:
                pass

    def _blocked_updated(self, result: dict) -> None:
        self._blocked_busy = False
        if not self._alive():
            return
        self.blocked_button.set_enabled(True)
        if not result.get("ok"):
            self.blocked_progress.configure(
                text="Не удалось: " + str(result.get("error") or "?")[:120],
                fg=self.theme.bad)
            return
        activity.log("обновил список заблокированных", kind="ok")
        self._refresh_blocked_status()
        self.blocked_progress.configure(text="Список обновлён", fg=self.theme.ok)
        restart = getattr(self.controller, "restart_proxy", None)
        if callable(restart) and self.controller.proxy.get("running"):
            if restart() is False:
                self.blocked_progress.configure(
                    text="Обновлено, но прокси занят — перезапустите вручную",
                    fg=self.theme.warn)

    # ---- маршруты сайтов ---------------------------------------------------
    def _route_feedback(self, text: str, color: str) -> None:
        self.route_status.configure(text=text, fg=color)

    def _add_rule(self) -> None:
        value = self.route_var.get().strip()
        ok, reason = routes.is_valid_domain(value)
        if not ok:
            self._route_feedback(reason or "Похоже, это не домен", self.theme.bad)
            return
        domain = routes.normalize_user_domain(value)
        if not domain:
            self._route_feedback("Похоже, это не домен", self.theme.bad)
            return
        route = self.route_segment.get()
        current = self.rules.get(domain)
        if current == route:
            self._route_feedback("Такое правило уже есть", self.theme.warn)
            return
        self.rules[domain] = route
        self._route_feedback("Правило обновлено" if current else "Добавлено",
                             self.theme.ok)
        self.route_var.set("")
        self._render_rules()

    def _toggle_rule(self, domain: str) -> None:
        current = self.rules.get(domain)
        if current is None:
            return
        self.rules[domain] = "proxy" if current == "direct" else "direct"
        self._route_feedback("Изменения применятся после «Сохранить»",
                             self.theme.text_faint)
        self._render_rules()

    def _delete_rule(self, domain: str) -> None:
        if self.rules.pop(domain, None) is None:
            return
        self._route_feedback("Удалено", self.theme.text_faint)
        self._render_rules()

    def _render_rules(self) -> None:
        if getattr(self, "rules_list", None) is None:
            return
        theme = self.theme
        for child in self.rules_list.winfo_children():
            child.destroy()
        domains = sorted(self.rules)
        self.routes_counter.configure(text=f"Правил: {len(domains)}")
        if not domains:
            self.rules_scroll.pack_forget()
            if not self.routes_empty.winfo_manager():
                self.routes_empty.pack(fill="x", pady=(2, 0))
            return
        self.routes_empty.pack_forget()
        if not self.rules_scroll.winfo_manager():
            self.rules_scroll.pack(fill="x")
        for domain in domains:
            route = self.rules[domain]
            row = tk.Frame(self.rules_list, bg=theme.card)
            row.pack(fill="x", pady=1)
            display = domain if len(domain) <= 46 else domain[:45].rstrip() + "…"
            label = tk.Label(row, text=display, bg=theme.card, fg=theme.text,
                             font=theme.font(9, "bold"), anchor="w")
            label.pack(side="left", fill="x", expand=True, pady=3)
            if display != domain:
                Tooltip(label, domain)
            chip = Chip(row, theme,
                        "напрямую" if route == "direct" else "через VPN",
                        theme.ok if route == "direct" else theme.accent,
                        bg=theme.card)
            chip.configure(cursor="hand2")
            chip.pack(side="left", padx=(8, 6))
            chip.bind("<Button-1>", lambda _event, name=domain: self._toggle_rule(name))
            remove = tk.Label(row, text="×", bg=theme.card, fg=theme.text_faint,
                              font=theme.font(11, "bold"), cursor="hand2", padx=4)
            remove.pack(side="right")
            remove.bind("<Button-1>", lambda _event, name=domain: self._delete_rule(name))
            Tooltip(remove, "Удалить правило")
        self.rules_list.update_idletasks()
        self.rules_scroll.set_height(
            min(max(self.rules_list.winfo_reqheight(), 26), 176))
        self.rules_scroll.bind_tree()

    # ---- компоненты --------------------------------------------------------
    def _refresh_status(self) -> None:
        state = components.installed(self.controller.cfg)
        parts = []
        parts.append(("✓ zapret: " + state["zapret_dir"]) if state["zapret"]
                     else "✗ zapret не установлен")
        parts.append(("✓ tg-ws-proxy: " + state["tgws_exe"]) if state["tgws"]
                     else "✗ tg-ws-proxy не установлен")
        self.status_label.configure(
            text="\n".join(parts),
            fg=self.theme.ok if state["zapret"] and state["tgws"] else self.theme.warn)

    def _install_components(self) -> None:
        if self._busy:
            return
        self._busy = True
        self.install_button.set_enabled(False)
        self.progress_label.configure(text="Начинаю загрузку…", fg=self.theme.text_dim)

        def progress(stage: str, text: str, percent: int) -> None:
            if not self._alive():
                return
            try:
                self.after(0, lambda: self.progress_label.configure(text=text))
            except (tk.TclError, RuntimeError):
                pass

        def worker() -> None:
            try:
                result = components.install_all(progress)
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "components": {}, "error": str(exc)}
            if self._alive():
                try:
                    self.after(0, lambda: self._installed(result))
                except (tk.TclError, RuntimeError):
                    pass

        threading.Thread(target=worker, daemon=True).start()

    def _installed(self, result: dict) -> None:
        self._busy = False
        if not self._alive():
            return
        self.install_button.set_enabled(True)
        components_state = result.get("components") or {}
        zapret = components_state.get("zapret") or {}
        tgws = components_state.get("tgws") or {}
        if zapret.get("ok") and not self.vars["zapret_dir"].get().strip():
            self.vars["zapret_dir"].set(zapret["dir"])
        if tgws.get("ok") and not self.vars["tgws_exe"].get().strip():
            self.vars["tgws_exe"].set(tgws["exe"])
        if result.get("ok"):
            self.progress_label.configure(text="Готово: оба компонента установлены",
                                          fg=self.theme.ok)
        else:
            errors = [c.get("error", "") for c in components_state.values()
                      if not c.get("ok")]
            self.progress_label.configure(
                text="Не всё удалось: " + "; ".join(e for e in errors if e)[:120],
                fg=self.theme.bad)
        self._refresh_status()

    # ---- сохранение --------------------------------------------------------
    def _save(self) -> None:
        cfg = dict(self.controller.cfg)
        try:
            port = int(str(self.vars["xray_port"].get()).strip() or xray.DEFAULT_PORT)
        except ValueError:
            port = xray.DEFAULT_PORT
        cfg["xray_subscription"] = self.vars["xray_subscription"].get().strip()
        cfg["xray_port"] = port
        cfg["xray_system_proxy"] = bool(self.vars["xray_system_proxy"].get())
        cfg["xray_stream_fallback"] = bool(self.vars["xray_stream_fallback"].get())
        cfg["blocked_list_enabled"] = bool(self.blocked_toggle.get())
        cfg["youtube_via_vpn"] = bool(self.toggles["youtube"].get())
        cfg["xray_routes"] = dict(self.rules)
        cfg["xray_extra_domains"] = []
        cfg["xray_direct_domains"] = []
        cfg["zapret_dir"] = self.vars["zapret_dir"].get().strip()
        cfg["tgws_exe"] = self.vars["tgws_exe"].get().strip()
        if not config.save_config(cfg):
            self.progress_label.configure(
                text="Не сохранено: config.json недоступен", fg=self.theme.warn)
            return
        self.controller.cfg = cfg
        # Перечитываем тумблеры/поля из сохранённого cfg: старые галочки
        # не должны оставаться после смены настроек.
        self.reload()
        self.progress_label.configure(text="Сохранено", fg=self.theme.ok)
        restart = getattr(self.controller, "restart_proxy", None)
        if callable(restart) and self.controller.proxy.get("running"):
            if restart() is False:
                self.progress_label.configure(
                    text="Сохранено, но прокси занят — перезапустите вручную",
                    fg=self.theme.warn)


class SettingsWindow:
    """Совместимое отдельное окно поверх :class:`SettingsPage` (для тестов)."""

    WIDTH = 720
    HEIGHT = 940

    def __init__(self, master: tk.Misc, theme: Theme, controller):
        self.master = master
        self.theme = theme
        self.controller = controller
        self.window: tk.Toplevel | None = None
        self.page: SettingsPage | None = None
        self._blocked_busy = False

    def __getattr__(self, name):
        page = self.__dict__.get("page")
        if page is not None:
            return getattr(page, name)
        raise AttributeError(name)

    def open(self) -> None:
        if self.window is not None:
            try:
                self.window.deiconify()
                self.window.lift()
                return
            except tk.TclError:
                self.window = None
        window = tk.Toplevel(self.master)
        self.window = window
        window.title("Настройки — Обходы")
        window.configure(bg=self.theme.bg)
        window.resizable(False, False)
        window.transient(self.master)
        window.protocol("WM_DELETE_WINDOW", self._close)
        self._blocked_busy = False
        self.page = SettingsPage(window, self.theme, self.controller,
                                 on_close=self._destroy)
        self.page.pack(fill="both", expand=True)
        window.update_idletasks()
        width = max(self.WIDTH, window.winfo_reqwidth())
        height = min(self.HEIGHT, max(420, window.winfo_screenheight() - 160))
        x = self.master.winfo_rootx() + (self.master.winfo_width() - width) // 2
        y = self.master.winfo_rooty() + 60
        window.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")
        window.update()
        from .ui import _apply_titlebar

        _apply_titlebar(window, self.theme)

    def _destroy(self) -> None:
        if self.window is not None:
            try:
                self.window.destroy()
            except tk.TclError:
                pass
            self.window = None

    def _close(self) -> None:
        self._busy = False
        self._blocked_busy = False
        if self.page is not None:
            self.page._blocked_busy = False
            self.page._blocked_result = None
            self.page._blocked_run += 1
        self._destroy()
        # page оставляем: после закрытия тесты читают page.rules, а Toplevel уже нет.

