"""Трей-приложение: иконка, меню, фоновое обновление статусов."""

from __future__ import annotations

import threading
import time

from PIL import Image, ImageDraw

try:
    import pystray
except ImportError:  # pragma: no cover
    pystray = None

from . import actions, config, status, updates
from .ui import Panel

COLORS = {
    "ok": "#34c759",
    "warn": "#ff9f0a",
    "bad": "#ff3b30",
    "dim": "#8e8e93",
}


def make_icon(color: str, letter: str) -> Image.Image:
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((4, 4, 60, 60), fill=color)
    draw.text((22, 20), letter, fill="white")
    return image


class TrayApp:
    def __init__(self):
        self.cfg = config.load_config()
        self.state = config.load_state()
        self.status: dict = {}
        self.last_check = 0.0
        self.panel = Panel(self)
        self._icon = None

    # ---- статусы -----------------------------------------------------------
    def refresh(self) -> None:
        self.status = status.collect(self.cfg)
        hours = float(self.cfg.get("update_check_hours") or 24)
        if time.time() - self.last_check > hours * 3600:
            self.last_check = time.time()
            self.check_updates(notify=True)
        self.update_icon()

    def health(self) -> tuple[str, str, str]:
        state = self.status or {}
        if not (state.get("happ") or {}).get("running"):
            return COLORS["bad"], "H", "Happ не запущен"
        if not (state.get("zapret") or {}).get("running"):
            return COLORS["warn"], "Z", "zapret (winws) не запущен"
        if not (state.get("tgwsproxy") or {}).get("listening"):
            return COLORS["warn"], "T", "tg-ws-proxy не слушает порт"
        note = self.state.get("updates_note")
        if note:
            return COLORS["warn"], "U", note
        return COLORS["ok"], "OK", "всё в порядке"

    def update_icon(self) -> None:
        if self._icon is None:
            return
        color, letter, title = self.health()
        try:
            self._icon.icon = make_icon(color, letter)
            self._icon.title = f"bypass-tray · {title}"
            self._icon.menu = self.build_menu()
            self._icon.update_menu()
        except Exception:  # noqa: BLE001
            pass

    # ---- обновления --------------------------------------------------------
    def check_updates(self, notify: bool = True) -> None:
        out = updates.check(self.state)
        new = out["new"]
        if new:
            self.state["updates_note"] = "Обновления: " + ", ".join(new)
            if notify and self._icon is not None:
                try:
                    self._icon.notify(self.state["updates_note"], "bypass-tray")
                except Exception:  # noqa: BLE001
                    pass
        else:
            self.state.pop("updates_note", None)
        config.save_state(self.state)

    # ---- меню --------------------------------------------------------------
    def build_menu(self):
        state = self.status or {}
        happ = "запущен" if (state.get("happ") or {}).get("running") else "выключен"
        zapret = "запущен" if (state.get("zapret") or {}).get("running") else "выключен"
        digest = (state.get("zapret") or {}).get("digest") or ""
        tgws = state.get("tgwsproxy") or {}
        tgws_text = f"слушает :{tgws.get('port', 1443)}" if tgws.get("listening") else "не слушает"
        bats = (state.get("zapret") or {}).get("bats") or []

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

        return pystray.Menu(
            pystray.MenuItem(f"Happ: {happ}", None, enabled=False),
            pystray.MenuItem(f"zapret: {zapret} {digest}".strip(), None, enabled=False),
            pystray.MenuItem(f"tg-ws-proxy: {tgws_text}", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Открыть панель", lambda icon, item: self.panel.open(), default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Zapret: запустить", lambda icon, item: self.run_action(actions.start_zapret, self.cfg)),
            pystray.MenuItem("Zapret: остановить", lambda icon, item: self.run_action(actions.stop_zapret)),
            pystray.MenuItem("Стратегия zapret", pystray.Menu(*strategy_items)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("tg-ws-proxy: запустить", lambda icon, item: self.run_action(actions.start_tgws, self.cfg)),
            pystray.MenuItem("Открыть Happ", lambda icon, item: self.run_action(actions.open_happ, self.cfg)),
            pystray.MenuItem("Проверить обновления", lambda icon, item: self.check_updates(True)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", lambda icon, item: self.stop()),
        )

    def set_strategy(self, name: str) -> None:
        self.cfg["zapret_bat"] = name
        config.save_config(self.cfg)

    def run_action(self, fn, *args) -> None:
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001
            if self._icon is not None:
                try:
                    self._icon.notify(str(exc), "bypass-tray")
                except Exception:  # noqa: BLE001
                    pass
        self.refresh()

    # ---- жизненный цикл ----------------------------------------------------
    def start(self) -> None:
        if pystray is None:
            raise RuntimeError("pystray не установлен — выполните install.bat")
        icon = make_icon(COLORS["dim"], "…")
        self._icon = pystray.Icon("bypass-tray", icon, "bypass-tray", menu=self.build_menu())
        threading.Thread(target=self._loop, daemon=True).start()
        self._icon.run()

    def _loop(self) -> None:
        while True:
            try:
                self.refresh()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(float(self.cfg.get("refresh_seconds") or 5))

    def stop(self) -> None:
        if self._icon is not None:
            self._icon.stop()
