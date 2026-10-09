"""Окно настроек: подписка VPN, прокси и установка компонентов обхода."""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import ttk

from . import components, config, xray
from .theme import Theme
from .widgets import PillButton, rounded_rect


class SettingsWindow:
    """Отдельное окно настроек, собранное из тех же виджетов, что и панель."""

    WIDTH = 560
    HEIGHT = 660

    def __init__(self, master: tk.Misc, theme: Theme, controller):
        self.master = master
        self.theme = theme
        self.controller = controller
        self.window: tk.Toplevel | None = None
        self._busy = False

    # ---- открытие ---------------------------------------------------------
    def open(self) -> None:
        theme = self.theme
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
        window.configure(bg=theme.bg)
        window.resizable(False, False)
        window.transient(self.master)
        window.protocol("WM_DELETE_WINDOW", self._close)

        cfg = dict(self.controller.cfg)
        self.vars: dict[str, tk.Variable] = {
            "xray_subscription": tk.StringVar(value=cfg.get("xray_subscription", "")),
            "xray_port": tk.StringVar(value=str(cfg.get("xray_port", xray.DEFAULT_PORT))),
            "xray_system_proxy": tk.BooleanVar(value=bool(cfg.get("xray_system_proxy", True))),
            "xray_stream_fallback": tk.BooleanVar(
                value=bool(cfg.get("xray_stream_fallback", True))),
            "xray_extra_domains": tk.StringVar(
                value=", ".join(cfg.get("xray_extra_domains") or [])),
            "zapret_dir": tk.StringVar(value=cfg.get("zapret_dir", "")),
            "tgws_exe": tk.StringVar(value=cfg.get("tgws_exe", "")),
        }

        self._build(window, theme)
        window.update_idletasks()
        width = max(self.WIDTH, window.winfo_reqwidth())
        # Содержимое живёт в прокручиваемой области, поэтому её высоту берём
        # фиксированной, а не по запросу canvas (иначе окно выходит низким).
        height = min(self.HEIGHT, max(420, window.winfo_screenheight() - 160))
        x = self.master.winfo_rootx() + (self.master.winfo_width() - width) // 2
        y = self.master.winfo_rooty() + 60
        window.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")
        window.update()

    def _close(self) -> None:
        if self.window is not None:
            try:
                self.window.destroy()
            except tk.TclError:
                pass
            self.window = None

    # ---- разметка ---------------------------------------------------------
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
        entry = ttk.Entry(parent, style="Settings.TEntry", textvariable=self.vars[key], font=self.theme.font(9))
        entry.pack(fill="x", ipady=3)
        return entry

    def _build(self, window: tk.Toplevel, theme: Theme) -> None:
        # Чекбоксы ttk по умолчанию рисуются под светлую тему — подгоняем.
        style = ttk.Style(window)
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
        style.configure("Vertical.TScrollbar", background=theme.btn,
                        troughcolor=theme.bg, bordercolor=theme.bg,
                        arrowcolor=theme.text_dim)

        # Заголовок
        header = tk.Frame(window, bg=theme.header)
        header.pack(fill="x")
        tk.Label(header, text="Настройки", bg=theme.header, fg=theme.header_text,
                 font=theme.font(14, "bold")).pack(anchor="w", padx=16, pady=(12, 2))
        tk.Label(header, text="Подписка VPN, прокси и компоненты обхода",
                 bg=theme.header, fg=theme.header_dim,
                 font=theme.font(9)).pack(anchor="w", padx=16, pady=(0, 12))

        canvas = tk.Canvas(window, bg=theme.bg, highlightthickness=0, bd=0)
        scroll = ttk.Scrollbar(window, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        # Нижнюю панель с кнопками размещаем ПЕРВОЙ: pack отдаёт место по
        # порядку, иначе растягивающийся canvas выдавливает её наверх.
        self._actions = tk.Frame(window, bg=theme.bg)
        self._actions.pack(side="bottom", fill="x", padx=16, pady=(8, 14))
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        body = tk.Frame(canvas, bg=theme.bg)
        canvas.create_window((0, 0), window=body, anchor="nw", width=self.WIDTH - 40)
        body.bind("<Configure>",
                  lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))

        pad = tk.Frame(body, bg=theme.bg)
        pad.pack(fill="both", expand=True, padx=16, pady=12)

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
        self._label(vpn, "Свои сайты через VPN (через запятую, формат domain:имя "
                         "или geosite:набор):")
        self._entry(vpn, "xray_extra_domains")

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
        actions = self._actions
        PillButton(actions, theme, "Сохранить", self._save, kind="primary").pack(side="left")
        PillButton(actions, theme, "Закрыть", self._close, kind="secondary").pack(
            side="left", padx=(8, 0))

        self._refresh_status()

    # ---- данные -----------------------------------------------------------
    def _refresh_status(self) -> None:
        state = components.installed(self.controller.cfg)
        parts = []
        parts.append(("✓ zapret: " + state["zapret_dir"]) if state["zapret"]
                     else "✗ zapret не установлен")
        parts.append(("✓ tg-ws-proxy: " + state["tgws_exe"]) if state["tgws"]
                     else "✗ tg-ws-proxy не установлен")
        if hasattr(self, "status_label"):
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
            window = self.window
            if window is None:
                return
            try:
                window.after(0, lambda: self.progress_label.configure(text=text))
            except tk.TclError:
                pass

        def worker() -> None:
            try:
                result = components.install_all(progress)
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "components": {}, "error": str(exc)}
            window = self.window
            if window is not None:
                try:
                    window.after(0, lambda: self._installed(result))
                except tk.TclError:
                    pass

        threading.Thread(target=worker, daemon=True).start()

    def _installed(self, result: dict) -> None:
        self._busy = False
        if self.install_button is not None:
            self.install_button.set_enabled(True)
        components_state = result.get("components") or {}
        zapret = components_state.get("zapret") or {}
        tgws = components_state.get("tgws") or {}
        # Подставляем пути, если поля пустые: чужую рабочую установку не трогаем.
        if zapret.get("ok") and not self.vars["zapret_dir"].get().strip():
            self.vars["zapret_dir"].set(zapret["dir"])
        if tgws.get("ok") and not self.vars["tgws_exe"].get().strip():
            self.vars["tgws_exe"].set(tgws["exe"])
        if result.get("ok"):
            self.progress_label.configure(text="Готово: оба компонента установлены",
                                          fg=self.theme.ok)
        else:
            errors = [c.get("error", "") for c in components_state.values() if not c.get("ok")]
            self.progress_label.configure(
                text="Не всё удалось: " + "; ".join(e for e in errors if e)[:120],
                fg=self.theme.bad)
        self._refresh_status()

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
        cfg["xray_extra_domains"] = [
            d.strip() for d in self.vars["xray_extra_domains"].get().split(",") if d.strip()
        ]
        cfg["zapret_dir"] = self.vars["zapret_dir"].get().strip()
        cfg["tgws_exe"] = self.vars["tgws_exe"].get().strip()
        self.controller.cfg = cfg
        config.save_config(cfg)
        self.progress_label.configure(text="Сохранено", fg=self.theme.ok)
        # Перезапускаем прокси, чтобы новые настройки вступили в силу.
        if self.controller.proxy.get("running"):
            self.controller.set_proxy(False)
            threading.Timer(2.0, lambda: self.controller.set_proxy(True)).start()
