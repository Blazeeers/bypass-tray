"""Мастер первого запуска: установка «в один клик».

Показывается один раз — пока в конфиге нет ссылки на подписку VPN. Задача:
свести настройку к «вставил ссылку → всё работает». Всё остальное мастер делает
сам:

* скачивает компоненты обхода (zapret, tg-ws-proxy) в профиль пользователя;
* готовит ядро Xray (копирует из Happ или скачивает официальное);
* прописывает автозапуск;
* поднимает локальный прокси.

От человека требуется только вставить ссылку на подписку и нажать «Подключить».
"""

from __future__ import annotations

import gc
import threading
import tkinter as tk
from tkinter import ttk

from . import autostart, components, config, xray
from .theme import Theme, resolve_dark
from .widgets import PillButton

WIDTH = 560
HEIGHT = 500


class SetupWizard:
    """Окно мастера. Живёт в главном потоке, работу делает в фоне."""

    def __init__(self, cfg: dict | None = None):
        self.cfg = dict(cfg or config.load_config())
        self.installed = False
        self.link = ""
        self._busy = False
        self._closed = False
        self.root: tk.Tk | None = None

    # ---- публичный вход ----------------------------------------------------
    def run(self) -> bool:
        """Открывает мастер и блокирует поток до закрытия окна.

        Возвращает True, если пользователь прошёл подключение.
        """
        self._theme = Theme(resolve_dark(self.cfg.get("theme", "auto")))
        root = tk.Tk()
        self.root = root
        root.title("Настройка bypass-tray")
        root.configure(bg=self._theme.bg)
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self._close)
        self._build(root)
        self._center(root)
        try:
            root.mainloop()
        finally:
            self._closed = True
            theme = getattr(self, "_theme", None)
            if theme is not None:
                theme.release()
            # Tk образует циклы ссылок — собираем их здесь, в потоке-владельце
            # интерпретатора (иначе Python 3.14 сообщит об утечке Tcl).
            gc.collect()
        return self.installed

    # ---- разметка ----------------------------------------------------------
    def _center(self, root: tk.Tk) -> None:
        root.update_idletasks()
        width = max(WIDTH, root.winfo_reqwidth())
        height = min(HEIGHT, max(420, root.winfo_screenheight() - 160))
        x = int(root.winfo_screenwidth() / 2 - width / 2)
        y = int(root.winfo_screenheight() / 2 - height / 2)
        root.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")
        root.update()

    def _build(self, root: tk.Tk) -> None:
        theme = self._theme
        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Wizard.TEntry", fieldbackground=theme.btn,
                        foreground=theme.text, bordercolor=theme.card_border,
                        lightcolor=theme.card_border, darkcolor=theme.card_border)

        header = tk.Frame(root, bg=theme.header)
        header.pack(fill="x")
        tk.Label(header, text="Настройка bypass-tray", bg=theme.header,
                 fg=theme.header_text, font=theme.font(15, "bold"),
                 anchor="w").pack(anchor="w", padx=18, pady=(14, 2))
        tk.Label(header, text="Осталось вставить ссылку на подписку — установка "
                              "и запуск пройдут сами",
                 bg=theme.header, fg=theme.header_dim, font=theme.font(9),
                 anchor="w", justify="left").pack(anchor="w", padx=18, pady=(0, 14))

        body = tk.Frame(root, bg=theme.bg)
        body.pack(fill="both", expand=True, padx=18, pady=16)

        tk.Label(body, text="Ссылка на подписку VPN", bg=theme.bg, fg=theme.text,
                 font=theme.font(11, "bold"), anchor="w").pack(fill="x")
        tk.Label(body, text="Возьмите её у провайдера VPN (обычно ссылка вида "
                            "https://… или подписка из личного кабинета). "
                            "Она хранится только у вас на компьютере.",
                 bg=theme.bg, fg=theme.text_dim, font=theme.font(9), anchor="w",
                 justify="left", wraplength=WIDTH - 60).pack(fill="x", pady=(2, 8))

        self.link_var = tk.StringVar(value=self.cfg.get("xray_subscription", ""))
        entry = ttk.Entry(body, textvariable=self.link_var, style="Wizard.TEntry",
                          font=theme.font(10))
        entry.pack(fill="x", ipady=5)
        entry.focus_set()

        self.autostart_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            body, text="Запускать при входе в Windows",
            variable=self.autostart_var, bg=theme.bg, fg=theme.text,
            selectcolor=theme.btn, activebackground=theme.bg,
            activeforeground=theme.text, highlightthickness=0, bd=0, anchor="w",
            font=theme.font(9), cursor="hand2",
        ).pack(fill="x", pady=(12, 0))

        self.status = tk.Label(body, text="", bg=theme.bg, fg=theme.text_dim,
                               font=theme.font(9), anchor="w", justify="left",
                               wraplength=WIDTH - 60)
        self.status.pack(fill="x", pady=(16, 6))

        self.progress = ttk.Progressbar(body, mode="determinate", maximum=100)
        self.progress.pack(fill="x")

        actions = tk.Frame(root, bg=theme.bg)
        actions.pack(fill="x", padx=18, pady=(0, 18))
        self.connect_button = PillButton(actions, theme, "Подключить", self._submit,
                                         kind="primary")
        self.connect_button.pack(side="left")
        self.skip_button = PillButton(actions, theme, "Пропустить", self._close,
                                      kind="secondary")
        self.skip_button.pack(side="left", padx=(8, 0))

    # ---- работа ------------------------------------------------------------
    def _set_status(self, text: str, color: str | None = None) -> None:
        root = self.root
        if root is None or self._closed:
            return
        try:
            self.status.configure(text=text, fg=color or self._theme.text_dim)
        except tk.TclError:
            pass

    def _set_progress(self, percent: int) -> None:
        root = self.root
        if root is None or self._closed:
            return
        try:
            self.progress.configure(value=percent)
        except tk.TclError:
            pass

    def _ui(self, fn, *args) -> None:
        """Планирует вызов функции в потоке Tk (из рабочего потока)."""
        root = self.root
        if root is None or self._closed:
            return
        try:
            root.after(0, lambda: fn(*args))
        except tk.TclError:
            pass

    def _submit(self) -> None:
        if self._busy:
            return
        link = self.link_var.get().strip()
        if not link:
            self._set_status("Вставьте ссылку на подписку VPN.", self._theme.bad)
            return
        self.link = link
        self.cfg["xray_subscription"] = link
        config.save_config(self.cfg)
        self._busy = True
        self.connect_button.set_enabled(False)
        self.skip_button.set_enabled(False)
        self._set_status("Начинаю установку…")
        threading.Thread(target=self._worker, daemon=True, name="wizard").start()

    def _worker(self) -> None:
        try:
            self._install_components()
            self._prepare_core()
            self._start_proxy()
            self._finish_ok()
        except Exception as exc:  # noqa: BLE001
            self._fail(str(exc) or type(exc).__name__)

    def _install_components(self) -> None:
        state = components.installed(self.cfg)
        if state["zapret"] and state["tgws"]:
            self._ui(self._set_status, "Компоненты обхода уже установлены.")
            return
        self._ui(self._set_status, "Скачиваю компоненты обхода (zapret, tg-ws-proxy)…")

        def progress(stage: str, text: str, percent: int) -> None:
            self._ui(self._set_status, text)
            self._ui(self._set_progress, percent)

        result = components.install_all(progress)
        parts = result.get("components") or {}
        zapret = parts.get("zapret") or {}
        tgws = parts.get("tgws") or {}
        if zapret.get("ok"):
            self.cfg["zapret_dir"] = zapret["dir"]
        if tgws.get("ok"):
            self.cfg["tgws_exe"] = tgws["exe"]
        config.save_config(self.cfg)
        if not result.get("ok"):
            errors = [c.get("error", "") for c in parts.values() if not c.get("ok")]
            raise RuntimeError("не удалось скачать компоненты: "
                               + "; ".join(e for e in errors if e))

    def _prepare_core(self) -> None:
        self._ui(self._set_status, "Готовлю ядро Xray…")
        self._ui(self._set_progress, 0)

        def progress(text: str, percent: int) -> None:
            self._ui(self._set_status, text)
            self._ui(self._set_progress, percent)

        if xray.ensure_core(progress) is None:
            raise RuntimeError("не удалось подготовить ядро Xray (нет доступа к GitHub?)")

    def _start_proxy(self) -> None:
        self._ui(self._set_status, "Подключаюсь к серверу…")
        self._ui(self._set_progress, 100)
        result = xray.start(self.cfg)
        if not result.get("ok"):
            raise RuntimeError(result.get("error") or "не удалось подключиться")
        if self.autostart_var.get():
            autostart.enable()

    def _finish_ok(self) -> None:
        self.installed = True
        self._ui(self._set_status, "Готово! Приложение свёрнуто в трей.", self._theme.ok)
        root = self.root
        if root is not None and not self._closed:
            try:
                root.after(1400, self._close)
            except tk.TclError:
                pass

    def _fail(self, message: str) -> None:
        self._busy = False
        self._ui(self._set_status, "Не получилось: " + message, self._theme.bad)
        self._ui(self._enable_buttons)

    def _enable_buttons(self) -> None:
        self.connect_button.set_enabled(True)
        self.skip_button.set_enabled(True)

    def _close(self) -> None:
        root = self.root
        self._closed = True
        if root is not None:
            try:
                root.destroy()
            except tk.TclError:
                pass
        self.root = None
