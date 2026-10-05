"""Окно-панель со статусами и действиями (Tkinter)."""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import ttk

from . import actions, config


class Panel:
    def __init__(self, controller):
        self.controller = controller
        self._thread: threading.Thread | None = None
        self._root: tk.Tk | None = None
        self._lock = threading.Lock()
        self._labels: dict[str, ttk.Label] = {}
        self._combo: ttk.Combobox | None = None
        self._strategy_var: tk.StringVar | None = None

    def open(self) -> None:
        with self._lock:
            if self._root is not None:
                try:
                    self._root.after(0, self._root.lift)
                    return
                except Exception:  # noqa: BLE001
                    self._root = None
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def _run(self) -> None:
        root = tk.Tk()
        self._root = root
        root.title("Обходы")
        root.geometry("460x360")
        root.resizable(False, False)
        frame = ttk.Frame(root, padding=12)
        frame.pack(fill="both", expand=True)

        for name in ("Happ", "zapret", "tg-ws-proxy", "Обновления"):
            row = ttk.Frame(frame)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=name, width=14).pack(side="left")
            label = ttk.Label(row, text="—")
            label.pack(side="left")
            self._labels[name] = label

        ttk.Separator(frame).pack(fill="x", pady=8)

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Zapret: запустить", command=self._start_zapret).pack(side="left")
        ttk.Button(buttons, text="Остановить", command=lambda: self._act(actions.stop_zapret)).pack(side="left", padx=6)
        ttk.Button(buttons, text="tg-ws-proxy", command=lambda: self._act(actions.start_tgws, self.controller.cfg)).pack(side="left", padx=6)
        ttk.Button(buttons, text="Happ", command=lambda: self._act(actions.open_happ, self.controller.cfg)).pack(side="left")

        strategy = ttk.Frame(frame)
        strategy.pack(fill="x", pady=8)
        ttk.Label(strategy, text="Стратегия:").pack(side="left")
        self._strategy_var = tk.StringVar(value=self.controller.cfg.get("zapret_bat", ""))
        self._combo = ttk.Combobox(strategy, textvariable=self._strategy_var, width=30)
        self._combo.pack(side="left", padx=6)
        ttk.Button(strategy, text="Применить", command=self._apply_strategy).pack(side="left")

        ttk.Button(frame, text="Проверить обновления", command=lambda: self.controller.check_updates(True)).pack(anchor="w", pady=6)

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._tick()
        root.mainloop()
        self._root = None

    def _tick(self) -> None:
        root = self._root
        if root is None:
            return
        try:
            self._render()
            root.after(2000, self._tick)
        except tk.TclError:
            self._root = None

    def _render(self) -> None:
        status = self.controller.status or {}
        happ = status.get("happ") or {}
        zapret = status.get("zapret") or {}
        tgws = status.get("tgwsproxy") or {}

        self._set("Happ", "запущен" if happ.get("running") else "выключен", bool(happ.get("running")))
        digest = (" " + (zapret.get("digest") or "")).rstrip()
        self._set("zapret", ("запущен" + digest) if zapret.get("running") else "выключен", bool(zapret.get("running")))
        self._set("tg-ws-proxy",
                  f"слушает :{tgws.get('port')}" if tgws.get("listening") else "не слушает",
                  bool(tgws.get("listening")))
        note = self.controller.state.get("updates_note")
        self._set("Обновления", note or "нет", not note)

        bats = zapret.get("bats") or []
        if self._combo is not None and list(self._combo["values"]) != bats:
            self._combo["values"] = bats

    def _set(self, name: str, text: str, good: bool) -> None:
        label = self._labels.get(name)
        if label is not None:
            try:
                label.configure(text=text, foreground=("#1a7f37" if good else "#d1242f"))
            except tk.TclError:
                pass

    def _act(self, fn, *args) -> None:
        try:
            fn(*args)
        except Exception:  # noqa: BLE001
            pass
        self.controller.refresh()

    def _start_zapret(self) -> None:
        bat = self._strategy_var.get() if self._strategy_var else None
        self._act(actions.start_zapret, self.controller.cfg, bat)

    def _apply_strategy(self) -> None:
        if not self._strategy_var:
            return
        self.controller.cfg["zapret_bat"] = self._strategy_var.get()
        config.save_config(self.controller.cfg)

    def _on_close(self) -> None:
        if self._root is not None:
            try:
                self._root.destroy()
            except tk.TclError:
                pass
