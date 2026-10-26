"""Дымовые проверки bypass-tray без разрушительных действий.

Запуск:
    .venv\\Scripts\\python.exe tests\\smoke_test.py

По умолчанию набор работает офлайн: проверки, которым нужна сеть (GitHub,
подписка, замер связи), помечаются ``SKIP``. Включить их:

    $env:BYPASS_TRAY_LIVE = "1"
    .venv\\Scripts\\python.exe tests\\smoke_test.py

Проверяет схему статусов, определение стратегии, иконки, меню, отрисовку
панели, атомарную запись и маршруты. Живое приложение не затрагивается:
ничего не останавливает и не запускает.

Примечание: при выходе CPython 3.14 может напечатать
``RuntimeWarning: the Tcl interpreter is leaked ...`` (gh-83274) — это
особенность завершения процесса с Tk-интерпретатором, а не сбой проверок.
Код возврата остаётся 0, если все проверки прошли.
"""

from __future__ import annotations

import gc
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bypass_tray import (  # noqa: E402
    actions, activity, autostart, blocked, components, config, guard, health,
    icons, networks, routes, status, strategy, theme, updates,
)
from bypass_tray.app import TrayApp  # noqa: E402
from bypass_tray.settings_ui import SettingsWindow  # noqa: E402
from bypass_tray.widgets import AnimatedSection, HoverRow, ToggleSwitch  # noqa: E402
from bypass_tray.ui import Panel  # noqa: E402

#: Сетевые проверки включаются только явно.
LIVE = os.environ.get("BYPASS_TRAY_LIVE") == "1"

RESULTS: list[tuple[bool, str]] = []
SKIPPED: list[tuple[str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    RESULTS.append((bool(condition), name))
    mark = "OK  " if condition else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))
    return bool(condition)


def skip(name: str, reason: str) -> None:
    SKIPPED.append((name, reason))
    print(f"[SKIP] {name} — {reason}")


def widget_texts(widget) -> list[str]:
    """Все тексты подписей в дереве виджетов."""
    found: list[str] = []
    try:
        text = widget.cget("text")
    except tk.TclError:
        text = ""
    if isinstance(text, str) and text:
        found.append(text)
    for child in widget.winfo_children():
        found.extend(widget_texts(child))
    return found


def find_buttons(widget, found: list | None = None) -> list:
    """Все экземпляры PillButton в дереве."""
    from bypass_tray.widgets import PillButton

    found = found if found is not None else []
    if isinstance(widget, PillButton):
        found.append(widget)
    for child in widget.winfo_children():
        find_buttons(child, found)
    return found


class FakeController:
    """Контроллер-заглушка для проверок панели: ничего не делает, считает вызовы."""

    def __init__(self):
        now = time.time()
        self.cfg = {
            "theme": "light", "xray_port": 10818, "quiet_icon": False,
            "guard_enabled": True, "autoconnect": True,
            "tgws_exe": r"C:\Tools\TgWsProxy_windows.exe",
            "zapret_bat": "general (ALT11).bat",
        }
        self.state: dict = {}
        self.status = {
            "zapret": {"running": True, "dir": r"C:\Program Files\Zapret",
                       "strategy": "general (ALT11).bat", "digest": "fake,fake"},
            "tgwsproxy": {"running": True, "listening": True, "port": 1443},
            "admin": False,
        }
        self.proxy = {
            "running": True, "server": "🇫🇮 Финляндия", "host": "fi.example.net",
            "since": now - 3394, "port": 10818, "latency": 51,
        }
        self.health_report = {
            "checked": now, "verdict": "ok",
            "items": [
                {"id": "zapret", "state": "ok", "detail": "general (ALT11).bat"},
                {"id": "youtube", "state": "ok", "detail": "страница и видео · 3.2 с"},
                {"id": "discord", "state": "ok", "detail": "страница и шлюз · 1.4 с"},
                {"id": "deepseek", "state": "ok", "detail": "напрямую · 0.4 с"},
                {"id": "tgwsproxy", "state": "ok", "detail": "порт 1443 слушает"},
                {"id": "vpn", "state": "ok", "detail": "подключён"},
            ],
        }
        self.health_running = False
        self.activity_items = [
            {"at": now - 640, "text": "подобрал стратегию: general (ALT11).bat", "kind": "ok"},
            {"at": now - 1900, "text": "подключил VPN: 🇫🇮 Финляндия", "kind": "ok"},
            {"at": now - 5400, "text": "обновил список серверов: 6", "kind": "ok"},
        ]
        self.updates_report = {
            "checked": now, "outdated": ["zapret", "xray"],
            "components": {
                "zapret": {"latest": "1.10.5", "installed": "1.10.4", "outdated": True},
                "xray": {"latest": "v26.10.21", "installed": "26.7.28", "outdated": True},
            },
        }
        self.updates_running = False
        self.route_report: dict = {}
        self.route_running = False
        self.sweep = {"running": False}
        self.servers = [
            {"name": "🇫🇮 Финляндия", "latency": 51},
            {"name": "🇳🇱 Нидерланды", "latency": 88},
            {"name": "🇩🇪 Германия", "latency": 104},
            {"name": "🇸🇪 Швеция", "latency": 76},
            {"name": "🇫🇷 Франция", "latency": 121},
            {"name": "🇹🇷 Турция", "latency": 165},
        ]
        self.server_selected = "🇫🇮 Финляндия"
        self.busy: dict[str, str] = {}
        self.updated_at = now
        self.calls: dict[str, int] = {}

    def _record(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    def set_proxy(self, value):
        self._record(f"set_proxy:{value}")

    def select_server(self, name):
        self._record("select_server")
        self.calls["select_server_name"] = name

    def load_servers(self, force=False):
        self._record("load_servers")

    def pick_best_server(self):
        self._record("pick_best_server")

    def start_sweep(self):
        self._record("start_sweep")

    def audit_routes(self):
        self._record("audit_routes")

    def apply_routes(self):
        self._record("apply_routes")

    def update_all(self):
        self._record("update_all")

    def check_all(self, notify=False):
        self._record("check_all")

    def refresh_subscription(self):
        self._record("refresh_subscription")

    def set_quiet(self, value):
        self._record(f"set_quiet:{value}")

    def set_guard(self, value):
        self._record(f"set_guard:{value}")

    def set_autostart(self, value):
        self._record(f"set_autostart:{value}")

    def is_autostart(self):
        return True

    def restart_proxy(self):
        self._record("restart_proxy")
        return True

    def update_everything(self):
        self._record("update_everything")
        return True

    def run_health(self, heal=False):
        self._record("run_health")

    def start_zapret(self):
        self._record("start_zapret")

    def restart_zapret(self):
        self._record("restart_zapret")

    def start_tgws(self):
        self._record("start_tgws")

    def restart_tgws(self):
        self._record("restart_tgws")


def main() -> int:
    print("=" * 68)
    print("bypass-tray: дымовые проверки")
    print("=" * 68)

    cfg = config.load_config()

    # --- 1. конфиг и автопоиск ------------------------------------------
    print("\n-- конфиг --")
    zapret_ready = bool(cfg.get("zapret_dir"))
    check("zapret_dir найден", zapret_ready, cfg.get("zapret_dir", ""))
    check("tgws_exe найден", bool(cfg.get("tgws_exe")), cfg.get("tgws_exe", ""))
    if zapret_ready:
        check("папка zapret существует", Path(cfg.get("zapret_dir") or "").is_dir())
    else:
        skip("папка zapret существует", "zapret_dir не задан — чистая машина")

    # --- 2. схема статусов (SPEC §3) ------------------------------------
    print("\n-- статусы --")
    state = status.collect(cfg)
    for section in ("zapret", "tgwsproxy"):
        check(f"секция {section} присутствует", section in state)
    check("zapret.running — bool", isinstance(state["zapret"]["running"], bool))
    check("zapret.bats — список", isinstance(state["zapret"]["bats"], list))
    if zapret_ready:
        check("zapret.bats непуст", len(state["zapret"]["bats"]) > 0,
              f"{len(state['zapret']['bats'])} стратегий")
    else:
        skip("zapret.bats непуст", "zapret_dir не задан — чистая машина")
    check("tgwsproxy.listening — bool", isinstance(state["tgwsproxy"]["listening"], bool))

    # --- 3. определение запущенной стратегии ----------------------------
    print("\n-- стратегия (SPEC §6 п.3) --")
    zapret = state["zapret"]
    if zapret_ready:
        check("командная строка получена", bool(zapret["cmdline_source"]),
              zapret["cmdline_source"])
        check("дайджест стратегии непуст", bool(zapret["digest"]), zapret["digest"])
        check("стратегия сопоставлена с .bat", bool(zapret["strategy"]), zapret["strategy"])
        check("найденная стратегия входит в список", zapret["strategy"] in zapret["bats"],
              zapret["strategy"])
    else:
        for name in ("командная строка получена", "дайджест стратегии непуст",
                     "стратегия сопоставлена с .bat",
                     "найденная стратегия входит в список"):
            skip(name, "zapret_dir не задан — чистая машина")

    # --- 4. порт tg-ws-proxy --------------------------------------------
    print("\n-- tg-ws-proxy --")
    if zapret_ready:
        check("порт слушается", state["tgwsproxy"]["listening"],
              f":{state['tgwsproxy']['port']}")
    else:
        skip("порт слушается", "zapret_dir не задан — чистая машина")

    # --- 5. иконки -------------------------------------------------------
    print("\n-- иконки --")
    for key, color in (("zapret", theme.STATE_COLORS["warn"]),
                       ("tgws", theme.STATE_COLORS["warn"]),
                       ("proxy", theme.STATE_COLORS["warn"]),
                       ("update", theme.STATE_COLORS["warn"]),
                       ("ok", theme.STATE_COLORS["ok"]),
                       ("idle", theme.STATE_COLORS["dim"])):
        image = icons.make_icon(key, color)
        check(f"иконка «{key}» {image.size[0]}px", image.size == (64, 64) and image.mode == "RGBA")

    check("глиф bad — cross", icons.STATE_GLYPHS.get("bad") == "cross")
    check("глифы degraded/down — alert и cross",
          icons.STATE_GLYPHS.get("degraded") == "alert"
          and icons.STATE_GLYPHS.get("down") == "cross")
    same_color = theme.STATE_COLORS["warn"]
    degraded_icon = icons.make_icon("degraded", same_color)
    down_icon = icons.make_icon("down", same_color)
    check("иконки degraded/down различаются пикселями при одном цвете",
          degraded_icon.tobytes() != down_icon.tobytes())

    ico = icons.ensure_ico()
    check("иконка .ico создана", ico.is_file() and ico.stat().st_size > 1000,
          str(ico))
    with Image.open(ico) as ico_image:
        ico_sizes = set(ico_image.ico.sizes())
    check("иконка .ico многоразмерная",
          {(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)}
          <= ico_sizes, str(sorted(ico_sizes))[:120])
    ico_root = tk.Tk()
    ico_root.withdraw()
    ico_detail = ""
    try:
        ico_root.iconbitmap(default=str(ico))
        ico_ok = True
    except tk.TclError as exc:
        ico_ok = False
        ico_detail = str(exc)
    finally:
        ico_root.destroy()
    check("iconbitmap читает .ico", ico_ok, ico_detail)

    # --- 6. приложение, меню, здоровье -----------------------------------
    print("\n-- приложение --")
    controller = TrayApp()
    controller.last_check = time.time()
    controller.refresh()
    key, color, title = controller.tray_state()
    check("tray_state() возвращает состояние",
          key in ("ok", "warn", "bad", "busy", "off", "idle", "degraded", "down"),
          f"{key} / {title}")
    check("health_report — словарь", isinstance(controller.health_report, dict))
    check("поля журнала/обновлений/занятости есть",
          isinstance(controller.activity_items, list)
          and isinstance(controller.updates_report, dict)
          and isinstance(controller.busy, dict))
    menu = controller.build_menu()
    items = list(menu.items)
    check("меню собрано", len(items) >= 10, f"{len(items)} пунктов")
    if zapret["as_service"]:
        labels = [it.text for it in items]
        check("пункт «zapret» в меню", any("zapret" in (t or "").lower() for t in labels))

    # --- 7. обновления ---------------------------------------------------
    print("\n-- обновления --")
    check("репозитории заданы",
          set(updates.REPOS) == {"zapret", "tgwsproxy", "xray"},
          ", ".join(sorted(updates.REPOS)))
    if LIVE:
        result = updates.check(dict(controller.state))
        components = result["result"]["components"]
        check("проверка обновлений выполнена", "checked" in result["result"])
        check("опрошены все репозитории", set(components) == {"zapret", "tgwsproxy", "xray"},
              ", ".join(sorted(components)))
        for name, info in components.items():
            check(f"релиз {name} получен", bool(info["latest"]), info["latest"] or "нет ответа")
    else:
        skip("проверка обновлений через GitHub", "нужна сеть: BYPASS_TRAY_LIVE=1")

    # --- 8. панель на фейковом контроллере --------------------------------
    print("\n-- панель --")
    ui_tmp = Path(tempfile.mkdtemp(prefix="bypass-tray-ui-"))
    try:
        config.set_config_dir(ui_tmp)
        fake = FakeController()
        panel = Panel(fake)
        root = tk.Tk()
        panel._root = root
        panel._build(root, theme.Theme(False))
        for _ in range(8):
            root.update()
            time.sleep(0.03)
        labels = widget_texts(root)
        check("заголовок «Обходы»", "Обходы" in labels)
        check("подзаголовок состояния",
              any(text.startswith("VPN ВКЛЮЧЁН") for text in labels))
        for title in ("zapret · обход DPI", "YouTube", "Discord",
                      "DeepSeek API", "tg-ws-proxy"):
            check(f"строка «{title}»", title in labels)
        check("детали проверок видны",
              any("страница и видео" in text for text in labels))
        check("журнал: записи виджета",
              any(text.startswith("подобрал стратегию") for text in labels))
        check("флаги из журнала вырезаны", not any("🇫" in text for text in labels))
        main_labels = widget_texts(panel._main_page)
        check("нет карточки обновлений", "Обновления" not in main_labels)
        check("нет карточки поведения", "ПОВЕДЕНИЕ" not in main_labels)
        check("кнопка обновления в шапке", panel._header_update is not None)
        fake.calls.pop("update_everything", None)
        panel._request_update()
        deadline = time.time() + 1.0
        while not fake.calls.get("update_everything") and time.time() < deadline:
            time.sleep(0.02)
        check("кнопка шапки вызывает update_everything",
              bool(fake.calls.get("update_everything")))
        check("подвал панели", any(text.startswith("обновлено") for text in labels))
        check("панель отрисована", panel._last_height > 500,
              f"высота {panel._last_height}px")

        origin = (panel._origin_x, panel._origin_y)
        main_height = panel._last_height
        before_pos = (root.winfo_x(), root.winfo_y())
        import bypass_tray.ui as ui_module
        real_work = ui_module._work_area
        ui_module._work_area = lambda _root: (0, 0, 2560, 1392)
        try:
            panel._apply_height(10000)
            root.update()
            work_bottom = 1392
            title_h = root.winfo_rooty() - root.winfo_y()
            outer_bottom = (root.winfo_y() + title_h
                            + root.winfo_height() + 8)
            check("рост высоты не двигает окно",
                  (root.winfo_x(), root.winfo_y()) == before_pos,
                  f"{root.winfo_x()},{root.winfo_y()} vs {before_pos}")
            check("высота ограничена рабочей областью",
                  root.winfo_height() <= work_bottom - origin[1] - title_h - 16
                  and outer_bottom <= work_bottom,
                  f"{root.winfo_height()}px, низ {outer_bottom} при y={origin[1]}")
        finally:
            ui_module._work_area = real_work
        panel._apply_height(main_height)
        root.update()
        check("главная вернула прежнюю высоту",
              abs(root.winfo_height() - main_height) <= 1
              and (root.winfo_x(), root.winfo_y()) == before_pos,
              f"{root.winfo_height()}px vs {main_height}px")

        panel._toggle_servers()
        for _ in range(20):
            root.update()
            time.sleep(0.01)
        check("секция серверов раскрыта",
              panel._servers_section.winfo_height() > 20,
              f"{panel._servers_section.winfo_height()}px")
        rows = panel._servers_list.winfo_children()
        check("список серверов раскрыт", len(rows) == len(fake.servers),
              f"{len(rows)} из {len(fake.servers)}")
        check("строки серверов получают колесо",
              bool(rows and rows[0].winfo_children()[0].bind("<MouseWheel>")))
        markers = [row.winfo_children()[0].cget("text") for row in rows]
        check("лучший и текущий помечены",
              any("★" in marker for marker in markers)
              and any("●" in marker for marker in markers),
              f"{len(markers)} меток")
        names = [row.winfo_children()[1].cget("text") for row in rows]
        check("флаги из имён серверов вырезаны",
              all("🇫" not in name and "🇳" not in name for name in names), str(names))

        primary = next((button for button in find_buttons(root)
                        if button._text == "Проверить всё"), None)
        check("кнопка «Проверить всё» есть", primary is not None)
        if primary is not None:
            primary._on_click(None)
            deadline = time.time() + 1.0
            while not fake.calls.get("run_health") and time.time() < deadline:
                time.sleep(0.02)
            check("кнопка вызывает controller.run_health",
                  bool(fake.calls.get("run_health")))

        check("ScrollBody без глобального колеса",
              panel._body.canvas.bind_all("<MouseWheel>") == "")
        check("stage — Canvas", isinstance(panel._stage, tk.Canvas))
        check("главная — окно Canvas",
              bool(panel._main_item is not None
                   and panel._stage.itemcget(panel._main_item, "window")))
        check("главная: колесо привязано",
              bool(panel._body.inner.bind("<MouseWheel>")))
        wheel_calls = {"n": 0}
        original_bind_tree = panel._body.bind_tree

        def spy_bind_tree(*_args):
            wheel_calls["n"] += 1

        panel._body.bind_tree = spy_bind_tree
        panel._render()
        panel._body.bind_tree = original_bind_tree
        check("_render не обходит дерево колесом", wheel_calls["n"] == 0,
              f"вызовов {wheel_calls['n']}")

        while not panel._queue.empty():
            panel._queue.get_nowait()
        original_render = panel._render
        render_calls = {"n": 0}

        def boom() -> None:
            render_calls["n"] += 1
            raise RuntimeError("boom")

        panel._render = boom
        panel._queue.put("render")
        panel._pump()
        check("ошибка команды не роняет цикл панели",
              "ошибка интерфейса" in (panel._footer.cget("text") or ""))
        panel._render = original_render
        panel._queue.put("render")
        panel._pump()
        check("после ошибки команды продолжают обрабатываться",
              render_calls["n"] >= 1)

        # --- встроенные настройки ---------------------------------------
        settings_page = panel._settings_page
        check("страница настроек построена", settings_page is not None)
        if settings_page is not None:
            check("подсказки поведения есть", len(settings_page.tooltips) >= 8,
                  f"{len(settings_page.tooltips)}")
            check("текст подсказки «Автолечение»",
                  any("Раз в 10 минут" in tooltip.text
                      for tooltip in settings_page.tooltips))
            before_y = root.winfo_y()
            before_pos = (root.winfo_x(), root.winfo_y())
            main_height = panel._last_height
            panel._open_settings()
            root.update()
            check("вход в настройки мгновенный",
                  panel._settings_visible and panel._page_visible == "settings"
                  and abs(panel._last_height - root.winfo_height()) <= 1
                  and panel._last_height > main_height)
            check("нет анимационных полей",
                  not hasattr(panel, "_anim_after")
                  and not hasattr(panel, "_anim_token"))
            check("x/y не менялись",
                  (root.winfo_x(), root.winfo_y()) == before_pos,
                  f"{root.winfo_x()},{root.winfo_y()} vs {before_pos}")
            check("настройки не двигают окно",
                  root.winfo_y() == before_y,
                  f"y={root.winfo_y()} vs {before_y}")
            import bypass_tray.ui as ui_module
            work = ui_module._work_area(root)
            title_h = root.winfo_rooty() - root.winfo_y()
            outer_bottom = (root.winfo_y() + title_h
                            + root.winfo_height() + 8)
            check("настройки влезают в рабочую область",
                  root.winfo_height() <= work[3] - before_y - title_h - 16
                  and outer_bottom <= work[3],
                  f"{root.winfo_height()}px, низ {outer_bottom} при work={work[3]}")
            check("настройки: колесо на странице",
                  bool(settings_page.body.inner.bind("<MouseWheel>")))
            check("настройки — окно Canvas, x не двигался",
                  bool(panel._settings_item is not None
                       and panel._stage.itemcget(panel._settings_item, "window"))
                  and panel._stage.coords(panel._settings_item)[0] == 0
                  and panel._stage.coords(panel._main_item)[0] == 0
                  and panel._stage.itemcget(panel._settings_item, "state") == "normal"
                  and panel._stage.itemcget(panel._main_item, "state") == "hidden"
                  and panel._page_visible == "settings")
            check("настройки без второго окна",
                  not [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)])
            check("заголовок «Настройки»", panel._title.cget("text") == "Настройки")
            check("подзаголовок настроек",
                  "Подписка, маршруты" in panel._subtitle.cget("text"))
            check("кнопка «Назад» видна",
                  bool(panel._header_back.winfo_manager()))
            check("шестерёнка скрыта", not panel._header_gear.winfo_manager())
            settings_page.reload()
            check("reload не пересоздаёт страницу",
                  panel._settings_page is settings_page)
            settings_page.toggles["youtube"].set(True)
            settings_page._save()
            check("встроенное сохранение", fake.cfg.get("youtube_via_vpn") is True)
            page_id = id(panel._settings_page)
            panel._close_settings()
            root.update()
            check("«Назад» вернул главную",
                  not panel._settings_visible
                  and panel._title.cget("text") == "Обходы")
            check("после возврата высота главной прежняя",
                  abs(panel._last_height - main_height) <= 1
                  and root.winfo_y() == before_y,
                  f"{panel._last_height}px vs {main_height}px, y={root.winfo_y()}")
            check("шестерёнка снова видна", bool(panel._header_gear.winfo_manager()))
            panel._open_settings()
            root.update()
            check("повторное открытие — та же страница",
                  id(panel._settings_page) == page_id and panel._settings_visible)
            panel._close_settings()
            root.update()
            check("после выхода страница снова main",
                  panel._page_visible == "main"
                  and panel._stage.itemcget(panel._main_item, "state") == "normal"
                  and panel._stage.itemcget(panel._settings_item, "state") == "hidden")

            # Быстрый повторный клик просто приводит к конечному состоянию.
            repeat_height = panel._last_height
            repeat_pos = (root.winfo_x(), root.winfo_y())
            panel._open_settings()
            panel._close_settings()
            root.update()
            check("повторный клик — сразу конечное состояние",
                  not panel._settings_visible and panel._page_visible == "main"
                  and panel._title.cget("text") == "Обходы"
                  and abs(panel._last_height - repeat_height) <= 1
                  and (root.winfo_x(), root.winfo_y()) == repeat_pos,
                  f"h={panel._last_height} vs {repeat_height}, "
                  f"page={panel._page_visible}")

        panel._release()
        root.destroy()
        del root
        gc.collect()
    finally:
        config.set_config_dir(None)
        shutil.rmtree(ui_tmp, ignore_errors=True)

    # --- 9. безопасность действий ----------------------------------------
    print("\n-- действия (не разрушительные) --")
    # Реальный tg-ws-proxy не трогаем: проверяем путь «файла нет» на копии
    # функции, чтобы не зависеть от того, запущен ли компонент сейчас.
    original_tgws_running = actions.tgws_running
    actions.tgws_running = lambda: False
    try:
        missing_raised = False
        try:
            actions.start_tgws({**cfg, "tgws_exe": str(ROOT / "__нет_такого__.exe")})
        except FileNotFoundError:
            missing_raised = True
    finally:
        actions.tgws_running = original_tgws_running
    check("start_tgws без файла — FileNotFoundError", missing_raised)
    check("kill_process возвращает отчёт",
          set(actions.kill_process("__нет_такого__")) == {"killed", "denied"})
    check("hide_console_windows не падает",
          isinstance(actions.hide_console_windows("__нет_такого__"), int))
    if zapret["as_service"]:
        check("служба zapret обнаружена", actions.service_state() in ("running", "stopped"),
              actions.service_state())
    check("is_admin() доступен", isinstance(state["admin"], bool), str(state["admin"]))

    # --- 10. панель: показ, повторный показ, закрытие (SPEC §6 п.4) ------
    print("\n-- панель: жизненный цикл --")
    live = Panel(FakeController())
    live.open()
    time.sleep(1.6)
    first = live._root
    check("панель открылась и видима",
          first is not None and bool(first.viewable() if hasattr(first, "viewable") else first.winfo_viewable()))
    live.open()  # повторный показ должен быть безопасным
    time.sleep(0.8)
    check("повторный показ не создаёт второе окно", live._root is first)
    live.close()
    time.sleep(1.5)
    check("панель закрылась и освободила объекты", live._root is None)

    # --- 11. разбор командной строки стратегии ----------------------------
    print("\n-- стратегии --")
    exe, rest = strategy.split_command('"C:\\Program Files\\Zapret\\bin\\winws.exe" --wf-tcp=80,443')
    check("split_command: путь к exe", exe == r"C:\Program Files\Zapret\bin\winws.exe", exe)
    check("split_command: аргументы", rest == "--wf-tcp=80,443", rest)
    exe2, rest2 = strategy.split_command("")
    check("split_command: пустая строка", exe2 == "" and rest2 == "")
    check("цели проверки заданы", len(strategy.TARGETS) >= 2)
    check("контрольный адрес задан", bool(strategy.CONTROL[1]))
    check("кандидаты найдены", len(strategy.default_bats(cfg["zapret_dir"])) > 0,
          f"{len(strategy.default_bats(cfg['zapret_dir']))} шт.")
    if LIVE:
        probe = strategy.measure(timeout=10)
        check("замер связи выполняется", probe["total"] >= 2,
              f"{probe['score']}/{probe['total']}, контроль {'ок' if probe['control_ok'] else 'нет'}")
    else:
        skip("замер связи (strategy.measure)", "нужна сеть: BYPASS_TRAY_LIVE=1")

    # --- 12. собственный клиент Xray (офлайн-часть) -----------------------
    print("\n-- прокси Xray --")
    from bypass_tray import xray  # noqa: E402

    check("ядро Xray на месте", xray.xray_path().is_file(), str(xray.xray_path()))
    sample = xray.parse_vless("vless://uuid@example.com:443?type=tcp&security=none#test")
    check("ссылка vless разобрана", bool(sample["host"]) and sample["port"] > 0,
          f"{sample['host']}:{sample['port']}")
    check("outbound содержит vless", sample["outbound"]["protocol"] == "vless")

    config_json = xray.build_config(sample, 10899)
    rules = config_json["routing"]["rules"]
    check("конфиг: РФ напрямую",
          any("geosite:category-ru" in (r.get("domain") or []) for r in rules))
    check("конфиг: заблокированное через прокси",
          any(r.get("outboundTag") == "proxy" for r in rules))
    check("конфиг: прокси слушает только 127.0.0.1",
          config_json["inbounds"][0]["listen"] == "127.0.0.1")
    check("конфиг: маршрут по умолчанию — напрямую",
          config_json["outbounds"][0]["tag"] == "direct")
    proxy_rule = next(r for r in rules if r.get("outboundTag") == "proxy")
    check("YouTube/Discord НЕ в прокси (идут через zapret)",
          not any(("youtube" in d or "discord" in d) for d in proxy_rule["domain"]))
    check("Instagram в прокси", any("instagram" in d for d in proxy_rule["domain"]))

    stream_cfg = xray.build_config(sample, 10899, None, True)
    stream_rule = next(r for r in stream_cfg["routing"]["rules"]
                       if r.get("outboundTag") == "proxy")
    check("резервный режим: YouTube/Discord уходят в прокси",
          any("youtube" in d for d in stream_rule["domain"])
          and any("discord" in d for d in stream_rule["domain"]))
    check("резервный режим: россияне всё равно напрямую",
          all(r.get("outboundTag") == "direct"
              for r in stream_cfg["routing"]["rules"][:2]))

    if LIVE:
        links = xray.fetch_subscription()
        check("подписка отдаёт серверы", len(links) > 0, f"{len(links)} шт.")
        if links:
            alive = xray.rank_servers(links)
            check("есть доступные серверы", len(alive) > 0,
                  f"{len(alive)} из {len(links)}, лучший {alive[0]['latency']}мс"
                  if alive else "нет доступных")
    else:
        skip("подписка и доступность серверов", "нужна сеть: BYPASS_TRAY_LIVE=1")

    proxy_status = xray.status()
    check("status() возвращает состояние",
          "running" in proxy_status and "port" in proxy_status,
          "подключён" if proxy_status.get("running") else "выключен")
    check("system_proxy_enabled() — bool", isinstance(xray.system_proxy_enabled(), bool))

    # --- 13. изолированные проверки: временный каталог данных ------------
    print("\n-- атомарная запись, журнал, маршруты --")
    tmp = Path(tempfile.mkdtemp(prefix="bypass-tray-test-"))
    try:
        config.set_config_dir(tmp)
        isolated_checks(cfg, tmp)
    finally:
        config.set_config_dir(None)
        shutil.rmtree(tmp, ignore_errors=True)

    # --- итог -------------------------------------------------------------
    failed = [name for ok, name in RESULTS if not ok]
    print("\n" + "=" * 68)
    print(f"Пройдено: {len(RESULTS) - len(failed)} из {len(RESULTS)}")
    if SKIPPED:
        print(f"Пропущено (нужна сеть): {len(SKIPPED)}")
        for name, reason in SKIPPED:
            print(f"  - {name}: {reason}")
    if failed:
        print("Провалено:")
        for name in failed:
            print(f"  - {name}")
    print("=" * 68)
    return 1 if failed else 0


def isolated_checks(cfg: dict, tmp: Path) -> None:
    """Проверки, которым нужен свой каталог данных и никакой сети."""
    from bypass_tray import xray  # noqa: E402

    # Чтобы TrayApp не искал zapret по дискам, кладём минимальный конфиг.
    config.save_config({"zapret_dir": cfg.get("zapret_dir", ""),
                        "tgws_exe": cfg.get("tgws_exe", "")})

    # --- конфиг: новые ключи -------------------------------------------
    print("\n-- новые ключи конфига --")
    for key, expected in (("quiet_icon", True), ("guard_enabled", True),
                          ("guard_minutes", 10), ("health_minutes", 10),
                          ("route_audit_minutes", 60), ("autoconnect", False)):
        check(f"ключ «{key}» = {expected}", config.DEFAULTS.get(key) == expected,
              repr(config.DEFAULTS.get(key)))

    # --- атомарная запись ----------------------------------------------
    target = tmp / "atomic.txt"
    config.atomic_write(target, "первая")
    config.atomic_write(target, "вторая строка")
    check("atomic_write: содержимое целое",
          target.read_text(encoding="utf-8") == "вторая строка")
    check("atomic_write: временных файлов не осталось",
          not list(tmp.glob("atomic.txt.tmp*")))

    # --- атомарная запись из потоков ----------------------------------
    concurrent_target = tmp / "atomic-concurrent.json"
    write_errors: list[str] = []
    payloads: list[dict] = []
    payload_lock = threading.Lock()

    def atomic_writer(writer_id: int) -> None:
        for index in range(3000):
            payload = {"writer": writer_id, "index": index}
            ok = config.atomic_write(
                concurrent_target, json.dumps(payload))
            if not ok:
                write_errors.append(f"writer {writer_id}:{index}")
            else:
                with payload_lock:
                    payloads.append(payload)

    writers = [threading.Thread(target=atomic_writer, args=(n,))
               for n in range(3)]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join()
    final_ok = False
    try:
        final_ok = isinstance(json.loads(concurrent_target.read_text(encoding="utf-8")),
                            dict)
    except (OSError, ValueError):
        final_ok = False
    check("atomic_write: 3×3000 без битых чтений",
          not write_errors and len(payloads) == 9000 and final_ok,
          f"write={write_errors[:2]}, записей={len(payloads)}")
    check("atomic_write: временные файлы потоков убраны",
          not list(tmp.glob("atomic-concurrent.json.tmp*")))

    # --- health.load_cache: свежесть отчёта -----------------------------
    print("\n-- кэш проверок --")
    cache_file = tmp / "health.json"
    # Валидный кэш обязан содержать все критичные пункты: без них судить о
    # здоровье нельзя (проверка полноты живёт в load_cache).
    fresh_report = {"checked": time.time(),
                    "items": [{"id": item_id, "title": item_id, "state": "ok",
                               "detail": "", "ms": 0}
                              for item_id in health.CRITICAL_IDS],
                    "verdict": "ok", "level": "ok"}
    config.atomic_write(cache_file, json.dumps(fresh_report, ensure_ascii=False))
    cached = health.load_cache({"health_minutes": 10})
    check("load_cache: свежий отчёт читается",
          bool(cached) and cached.get("level") == "ok")
    partial_report = {**fresh_report,
                      "items": [{"id": "telegram", "title": "Telegram",
                                 "state": "ok", "detail": "", "ms": 0}]}
    config.atomic_write(cache_file, json.dumps(partial_report, ensure_ascii=False))
    check("load_cache: отчёт без критичных пунктов — None",
          health.load_cache({"health_minutes": 10}) is None)
    stale_report = {**fresh_report, "checked": time.time() - 21 * 60}
    config.atomic_write(cache_file, json.dumps(stale_report, ensure_ascii=False))
    check("load_cache: старый отчёт отбрасывается",
          health.load_cache({"health_minutes": 10}) is None)
    config.atomic_write(cache_file, "{ это не json")
    check("load_cache: битый файл — None",
          health.load_cache({"health_minutes": 10}) is None)
    cache_file.unlink()

    # --- уровень: гистерезис --------------------------------------------
    print("\n-- гистерезис уровня --")
    level_app = TrayApp()
    level_app._level = "ok"
    level_app._level_at = 0.0
    level_app._level_candidate = ""
    level_app._level_hits = 0

    def level_report(name: str) -> dict:
        return {"checked": time.time(), "items": [], "verdict": "ok", "level": name}

    level_app._apply_level(level_report("degraded"))
    check("гистерезис: один плохой отчёт не меняет уровень",
          level_app._level == "ok")
    level_app._apply_level(level_report("degraded"))
    check("гистерезис: два подряд — уровень понижается",
          level_app._level == "degraded")
    level_app._level_at = time.time()
    level_app._apply_level(level_report("ok"))
    check("гистерезис: кулдаун 60 с держит уровень",
          level_app._level == "degraded")
    level_app._level_at = time.time() - 120
    for _ in range(2):
        level_app._apply_level(level_report("down"))
    held = level_app._level
    level_app._apply_level(level_report("down"))
    check("гистерезис: down после трёх отчётов",
          held == "degraded" and level_app._level == "down")
    level_app._level_at = time.time() - 120
    level_app._apply_level(level_report("ok"))
    check("гистерезис: улучшение применяется сразу",
          level_app._level == "ok")

    # --- сброс отчёта: новая проба не ждёт гистерезиса -------------------
    level_app._level = "down"
    level_app._level_at = time.time()
    level_app._reset_health()
    check("_reset_health обнуляет уровень",
          level_app._level == "" and level_app._level_at == 0.0)
    level_app._apply_level(level_report("ok"))
    check("после _reset_health свежий ok применяется сразу",
          level_app._level == "ok")

    # --- старый кэш без level: трей не должен быть зелёным --------------
    legacy_cache = {"checked": time.time(), "verdict": "fail",
                    "items": [{"id": "youtube", "title": "YouTube",
                               "state": "bad", "detail": "", "ms": 0}]}
    config.atomic_write(tmp / "health.json",
                        json.dumps(legacy_cache, ensure_ascii=False))
    legacy_app = TrayApp()
    legacy_app.proxy = {**legacy_app.proxy, "running": True}
    legacy_key, legacy_color, _legacy_title = legacy_app.tray_state()
    check("кэш без level при verdict=fail — трей не зелёный",
          legacy_color != theme.STATE_COLORS["ok"],
          f"{legacy_key} / {legacy_color}")
    check("кэш без критичных пунктов не выдаётся за свежий",
          legacy_app.health_report == {}, str(legacy_app.health_report))
    (tmp / "health.json").unlink()

    # --- нечитаемый config.json не перезаписывается --------------------
    saved_cfg = {"quiet_icon": True, "guard_enabled": True,
                 "zapret_dir": "", "tgws_exe": "", "xray_routes": {}}
    config.save_config(saved_cfg)
    real_read_text = Path.read_text
    before_unreadable = open(config.CONFIG_FILE, "rb").read()

    def blocked_read(self, *args, **kwargs):
        if self == config.CONFIG_FILE:
            raise PermissionError("unreadable in test")
        return real_read_text(self, *args, **kwargs)

    Path.read_text = blocked_read
    try:
        fallback = config.load_config()
        check("нечитаемый конфиг: умолчания и read_error",
              fallback.get("refresh_seconds") == config.DEFAULTS["refresh_seconds"]
              and bool(config.read_error()))
        check("нечитаемый конфиг: save_config не пишет",
              config.save_config({**saved_cfg, "quiet_icon": False}) is False)
        check("нечитаемый конфиг: файл не изменён",
              open(config.CONFIG_FILE, "rb").read() == before_unreadable)
    finally:
        Path.read_text = real_read_text
    config.load_config()
    check("после восстановления чтения запись работает",
          config.save_config({**saved_cfg, "quiet_icon": True}) is True)

    # --- релизные мелочи: run.bat, версия, пути, SSL, log --------
    run_bat_bytes = (ROOT / "run.bat").read_bytes()
    check("run.bat ASCII-only и ищет dist exe",
          all(byte < 128 for byte in run_bat_bytes)
          and b"for /d" in run_bat_bytes
          and b"dist\\*" in run_bat_bytes
          and b"*.exe" in run_bat_bytes)
    check("widget_version 0.6.4", updates.widget_version() == "0.6.4",
          updates.widget_version())
    empty_components = components.installed({"zapret_dir": "", "tgws_exe": ""})
    check("components.installed: пустые пути — не cwd",
          empty_components["zapret"] is False and empty_components["tgws"] is False)
    empty_raised = False
    try:
        actions.start_zapret({"zapret_dir": ""})
    except FileNotFoundError:
        empty_raised = True
    check("start_zapret: пустой путь — FileNotFoundError", empty_raised)
    empty_service_raised = False
    try:
        actions.open_service_bat({"zapret_dir": ""})
    except FileNotFoundError:
        empty_service_raised = True
    check("open_service_bat: пустой путь — FileNotFoundError", empty_service_raised)

    import ssl
    import urllib.error
    real_direct = health._http_direct
    real_certificate = routes._peer_certificate
    real_issuer = routes._certificate_issuer
    real_local = routes._issuer_is_local

    def tls_failure(url, timeout):
        raise urllib.error.URLError(
            ssl.SSLCertVerificationError("local certificate store"))

    health._http_direct = tls_failure
    try:
        # Второй проход не нашёл ни сертификата, ни ответа — рукопожатие
        # оборвано, домен недоступен.
        routes._peer_certificate = (
            lambda name, port, timeout, proxy_port=None: (b"", 0))
        tls_route = routes.probe("yandex.ru")
        check("routes.probe: обрыв рукопожатия — недоступен",
              tls_route.get("ok") is False and tls_route.get("error") == "tls",
              str(tls_route))
        # Узел ответил, а сертификат выпущен локально: проблема в хранилище
        # этой машины, домен достижим.
        routes._peer_certificate = (
            lambda name, port, timeout, proxy_port=None: (b"PEM", 200))
        routes._certificate_issuer = lambda der: {"issuer": (), "subject": ()}
        routes._issuer_is_local = lambda decoded: True
        local_route = routes.probe("yandex.ru")
        check("routes.probe: локальный сертификат — достижим",
              local_route.get("ok") is True
              and local_route.get("error") == "tls"
              and local_route.get("tls_local") is True,
              str(local_route))
    finally:
        health._http_direct = real_direct
        routes._peer_certificate = real_certificate
        routes._certificate_issuer = real_issuer
        routes._issuer_is_local = real_local

    # Реальный cmd: копия run.bat с start→echo под cp866.
    run_bat_text = (ROOT / "run.bat").read_text(encoding="ascii")
    variant = run_bat_text.replace('start "" "%%~f"', 'echo FOUND "%%~f"')
    bat_project = tmp / "runbat"
    (bat_project / "dist" / "Test").mkdir(parents=True)
    (bat_project / "dist" / "Test" / "Test.exe").write_bytes(b"")
    (bat_project / "run.bat").write_text(variant, encoding="ascii", newline="\r\n")
    bat_run = subprocess.run(["cmd", "/c", str(bat_project / "run.bat")],
                             cwd=str(bat_project), capture_output=True,
                             text=True, encoding="cp866", errors="replace")
    check("run.bat реально находит exe (cmd/cp866)",
          "FOUND" in bat_run.stdout and "Test.exe" in bat_run.stdout,
          repr(bat_run.stdout[-200:]))


    routes.save_overrides({"stable.example": {"route": "proxy", "since": 10,
                                               "checked": 10}}, at=time.time())
    routes.apply_audit({"at": time.time(),
                        "items": [{"domain": "stable.example", "route": "broken"}]})
    stable = routes.load_overrides().get("stable.example") or {}
    check("audit: broken не ломает последний рабочий маршрут",
          stable.get("route") == "proxy" and stable.get("since") == 10,
          str(stable))
    routes.save_overrides({}, at=time.time())

    sample_log = xray.build_config(
        xray.parse_vless("vless://uuid@example.com:443?type=tcp&security=none#t"),
        10899, None, False)
    check("xray log: warning + access none",
          sample_log.get("log") == {"loglevel": "warning", "access": "none"},
          str(sample_log.get("log")))
    log_path = tmp / "xray.log"
    log_path.write_bytes(b"x" * 300_000)
    real_log_path = xray._log_path
    xray._log_path = lambda: log_path
    try:
        xray._trim_log()
    finally:
        xray._log_path = real_log_path
    check("xray.log обрезан до 256 KiB", log_path.stat().st_size <= 256 * 1024,
          str(log_path.stat().st_size))

    menu_app = TrayApp()
    menu_app.cfg["youtube_via_vpn"] = False
    menu_before = menu_app._menu_signature()
    menu_app.cfg["youtube_via_vpn"] = True
    menu_after = menu_app._menu_signature()
    check("menu signature: youtube_via_vpn", menu_before != menu_after)

    # --- single instance и proxy lifecycle ------------------------------
    from bypass_tray import __main__ as main_module
    class MutexCall:
        restype = None
        argtypes = None
        def __call__(self, *args):
            return 123
    class FakeKernel:
        def __init__(self):
            self.CreateMutexW = MutexCall()
            self.closed = False
        def GetLastError(self):
            return 183
        def CloseHandle(self, handle):
            self.closed = True
    class FakeUser:
        def __init__(self):
            self.messages = []
        def MessageBoxW(self, *args):
            self.messages.append(args)
    fake_kernel = FakeKernel(); fake_user = FakeUser()
    mutex, first = main_module._acquire_single_instance(fake_kernel, fake_user)
    check("single-instance: второй экземпляр отклонён",
          first is False and mutex is None and fake_kernel.closed
          and bool(fake_user.messages))

    lifecycle_app = TrayApp()
    lifecycle_app.proxy = {**lifecycle_app.proxy, "running": True}
    real_status = xray.status
    real_enabled = xray.system_proxy_enabled
    real_set = xray.set_system_proxy
    stop_calls = []
    xray.status = lambda: {"running": False, "port": 10818}
    xray.system_proxy_enabled = lambda: True
    xray.set_system_proxy = lambda *args: stop_calls.append(args)
    try:
        lifecycle_app.sync_proxy_state()
    finally:
        xray.status = real_status
        xray.system_proxy_enabled = real_enabled
        xray.set_system_proxy = real_set
    check("sync_proxy_state снимает наш мёртвый прокси",
          stop_calls and stop_calls[0][0] is False, str(stop_calls))

    real_stop = xray.stop
    real_panel_close = lifecycle_app.panel.close
    stopped = []
    xray.stop = lambda *args, **kwargs: stopped.append(True)
    lifecycle_app.panel.close = lambda: None
    try:
        lifecycle_app.stop()
    finally:
        xray.stop = real_stop
        lifecycle_app.panel.close = real_panel_close
    check("TrayApp.stop останавливает Xray", stopped == [True])

    sync_app = TrayApp()
    sync_app.cfg["xray_system_proxy"] = True
    sync_calls = []
    real_status = xray.status
    real_enabled = xray.system_proxy_enabled
    real_server = xray.system_proxy_server
    real_set = xray.set_system_proxy
    xray.status = lambda: {"running": True, "port": 10818, "stream": False}
    xray.system_proxy_enabled = lambda: False
    xray.system_proxy_server = lambda: ""
    xray.set_system_proxy = lambda *args: sync_calls.append(args)
    try:
        sync_app.sync_proxy_state()
    finally:
        xray.status = real_status
        xray.system_proxy_enabled = real_enabled
        xray.system_proxy_server = real_server
        xray.set_system_proxy = real_set
    check("sync running: ProxyEnable=0 включает наш",
          sync_calls == [(True, 10818, False)], str(sync_calls))

    sync_calls.clear()
    xray.status = lambda: {"running": True, "port": 10818, "stream": False}
    xray.system_proxy_enabled = lambda: True
    xray.system_proxy_server = lambda: "127.0.0.1:9999"
    xray.set_system_proxy = lambda *args: sync_calls.append(args)
    try:
        sync_app.sync_proxy_state()
    finally:
        xray.status = real_status
        xray.system_proxy_enabled = real_enabled
        xray.system_proxy_server = real_server
        xray.set_system_proxy = real_set
    check("sync running: чужой ProxyServer не трогаем",
          not sync_calls and sync_app.proxy.get("system_proxy_owned") == "foreign",
          str(sync_calls))

    real_kill = xray._kill_process
    real_stop_proxy = xray.set_system_proxy
    stop_port_calls = []
    old_running = dict(xray.RUNNING)
    xray._kill_process = lambda: None
    xray.set_system_proxy = lambda *args: stop_port_calls.append(args)
    xray.RUNNING["port"] = 10819
    try:
        xray._stop(True)
    finally:
        xray.RUNNING.clear(); xray.RUNNING.update(old_running)
        xray._kill_process = real_kill
        xray.set_system_proxy = real_stop_proxy
    check("xray._stop снимает прокси на фактическом порту",
          stop_port_calls == [(False, 10819)], str(stop_port_calls))


    # --- журнал действий -----------------------------------------------
    activity.clear()
    activity.log("проверка журнала", kind="test")
    rows = activity.recent(5)
    check("журнал: запись видна", bool(rows) and rows[0].get("text") == "проверка журнала",
          str(rows[:1]))
    check("журнал: last_text()", activity.last_text() == "проверка журнала")
    for index in range(65):
        activity.log(f"строка {index}")
    rows = activity.recent(100)
    check("журнал: лимит 60 записей", len(rows) == 60, f"{len(rows)}")
    check("журнал: новые сверху", bool(rows) and rows[0].get("text") == "строка 64",
          str(rows[0].get("text") if rows else ""))
    activity.clear()
    check("журнал: очистка", activity.recent(5) == [] and activity.last_text() == "")

    # --- журнал: фильтр по часам -----------------------------------------
    journal_file = activity.path()
    backup_journal = journal_file.read_bytes() if journal_file.is_file() else None
    try:
        config.atomic_write(journal_file, json.dumps([
            {"at": time.time() - 4 * 3600, "text": "старое", "kind": "auto"},
            {"at": time.time() - 3600, "text": "свежее", "kind": "auto"},
        ], ensure_ascii=False))
        fresh_only = activity.recent(10, hours=3)
        check("activity.recent(hours): старые скрыты",
              [row["text"] for row in fresh_only] == ["свежее"],
              str(fresh_only))
        check("config.activity_hours по умолчанию",
              config.DEFAULTS.get("activity_hours") == 3)
    finally:
        if backup_journal is None:
            try:
                journal_file.unlink(missing_ok=True)
            except OSError:
                pass
        else:
            journal_file.write_bytes(backup_journal)

    # --- сети: чистые парсеры ------------------------------------------
    print("\n-- сети --")
    ssid_text = (
        "\r\n    Имя                   : Wi-Fi\r\n"
        "    Описание              : Intel(R) Wi-Fi 6 AX201\r\n"
        "    Физический адрес      : aa:bb:cc:dd:ee:ff\r\n"
        "    SSID                   : Home Net\r\n"
        "    BSSID                  : 11:22:33:44:55:66\r\n"
    )
    check("SSID разобран", networks.parse_ssid(ssid_text) == "Home Net",
          networks.parse_ssid(ssid_text))
    check("BSSID не принят за SSID", "11:22" not in networks.parse_ssid(ssid_text))
    check("SSID: пустой вывод", networks.parse_ssid("") == "")
    check("шлюз из реестра (REG_SZ)",
          networks.parse_gateway_registry("192.168.1.1") == "192.168.1.1")
    check("шлюз из реестра (REG_MULTI_SZ)",
          networks.parse_gateway_registry(["", "0.0.0.0", ["10.0.0.1"]]) == "10.0.0.1",
          networks.parse_gateway_registry(["", "0.0.0.0", ["10.0.0.1"]]))
    check("шлюз: пусто", networks.parse_gateway_registry(["", "0.0.0.0"]) == "")
    mac_text = "  192.168.1.1           aa-bb-cc-dd-ee-ff     динамический\r\n"
    check("MAC из arp через дефис",
          networks.parse_arp_mac(mac_text) == "aa:bb:cc:dd:ee:ff",
          networks.parse_arp_mac(mac_text))
    check("MAC из arp через двоеточие",
          networks.parse_arp_mac("AA:BB:CC:DD:EE:FF") == "aa:bb:cc:dd:ee:ff")
    check("MAC: нет адреса", networks.parse_arp_mac("нет данных") == "")
    current_net = networks.current()
    check("текущая сеть описана",
          set(current_net) == {"key", "ssid", "gateway", "mac", "name"}
          and bool(current_net["key"]), current_net["key"])

    # --- здоровье: вердикт и пункты ------------------------------------
    print("\n-- здоровье --")

    def health_item(item_state: str) -> dict:
        return {"id": "x", "title": "x", "state": item_state, "detail": "", "ms": 0}

    check("verdict: ok и off — ok",
          health.verdict([health_item("ok"), health_item("off")]) == "ok")
    check("verdict: warn — warn", health.verdict([health_item("ok"), health_item("warn")]) == "warn")
    check("verdict: bad — fail", health.verdict([health_item("warn"), health_item("bad")]) == "fail")
    check("verdict: пусто — warn", health.verdict([]) == "warn")

    def level_items(**states: str) -> list[dict]:
        return [{"id": key, "title": key, "state": value, "detail": "", "ms": 0}
                for key, value in states.items()]

    all_ok = level_items(zapret="ok", youtube="ok", discord="ok", deepseek="ok",
                         vpn="ok", telegram="ok", tgwsproxy="ok")
    check("level: всё ok — ok", health.level(all_ok) == "ok")
    check("level: пустой набор — degraded", health.level([]) == "degraded")
    check("level: off не влияет",
          health.level(level_items(zapret="off", vpn="off", telegram="off")) == "ok")
    check("level: deepseek bad — down",
          health.level(level_items(deepseek="bad")) == "down")
    check("level: youtube и discord bad — down",
          health.level(level_items(youtube="bad", discord="bad")) == "down")
    check("level: одиночный youtube bad — degraded",
          health.level(level_items(youtube="bad")) == "degraded")
    check("level: одиночный zapret bad — degraded",
          health.level(level_items(zapret="bad")) == "degraded")
    check("level: vpn bad — down",
          health.level(level_items(vpn="bad")) == "down")
    check("level: telegram bad — degraded",
          health.level(level_items(telegram="bad")) == "degraded")
    check("level: tgwsproxy bad — degraded",
          health.level(level_items(tgwsproxy="bad")) == "degraded")
    check("level: zapret warn — degraded",
          health.level(level_items(zapret="warn")) == "degraded")
    check("level: устаревший отчёт — degraded",
          health.level(all_ok, fresh=False) == "degraded")

    reserve_item = health._check_zapret(
        {"zapret": {"running": False, "dir": "C:/Zapret"}},
        {"youtube_via_vpn": True},
        {"running": True, "stream": False})
    check("zapret: остановлен при VPN-резерве — warn",
          reserve_item["state"] == "warn" and "через VPN" in reserve_item["detail"],
          reserve_item["detail"])
    check("level: zapret warn при резерве — degraded",
          health.level([reserve_item]) == "degraded")
    missing_item = health._check_zapret(
        {"zapret": {"running": False, "dir": ""}},
        {"youtube_via_vpn": True},
        {"running": True, "stream": False})
    check("zapret: не найден при VPN-резерве — warn",
          missing_item["state"] == "warn"
          and "не найден" in missing_item["detail"], missing_item["detail"])

    zapret_item = health._check_zapret({"zapret": {"running": False, "dir": "C:/Zapret"}})
    check("zapret: остановлен", zapret_item["state"] == "bad"
          and zapret_item["detail"] == "остановлен", zapret_item["detail"])
    zapret_item = health._check_zapret({"zapret": {"running": False, "dir": ""}})
    check("zapret: не найден", zapret_item["detail"] == "winws.exe не найден",
          zapret_item["detail"])
    tgws_item = health._check_tgws({"tgwsproxy": {"listening": False}}, {"tgws_exe": ""})
    check("tgws: не установлен — off", tgws_item["state"] == "off"
          and tgws_item["detail"] == "не установлен", tgws_item["detail"])

    real_verify = xray.verify
    vpn_seen: list[tuple] = []

    def fake_verify_fail(target="", timeout=0):
        vpn_seen.append((target, timeout))
        return {"ok": False, "error": "TimeoutError", "ms": 6000}

    xray.verify = fake_verify_fail
    try:
        vpn_item = health._check_vpn({"running": True, "server": "test"})
    finally:
        xray.verify = real_verify
    check("vpn: провал туннеля — bad", vpn_item["state"] == "bad"
          and vpn_item["detail"] == "сервер не отвечает", vpn_item["detail"])
    check("vpn: проверка идёт на Telegram, timeout 10",
          len(vpn_seen) == 2 and all(target == "api.telegram.org" and timeout == 10
                                     for target, timeout in vpn_seen),
          str(vpn_seen))
    xray.verify = lambda target="", timeout=0: {"ok": True, "ms": 1500}
    try:
        vpn_ok = health._check_vpn({"running": True, "server": "test"})
    finally:
        xray.verify = real_verify
    check("vpn: успех — «через VPN · X с»",
          vpn_ok["state"] == "ok" and vpn_ok["detail"] == "через VPN · 1.5 с",
          vpn_ok["detail"])
    check("vpn: прокси выключен — off", health._check_vpn({"running": False})["state"] == "off")

    real_http = health._http
    real_tcp = health._tcp
    http_calls: list[tuple] = []
    tcp_calls: list[str] = []

    def fake_http(url, proxy_port=None, timeout=5.0):
        http_calls.append((url, proxy_port))
        return {"ok": True, "status": 200, "ms": 120, "error": ""}

    def fake_tcp(host, port, timeout=5.0):
        tcp_calls.append(host)
        return {"ok": True, "ms": 30, "error": ""}

    health._http = fake_http
    health._tcp = fake_tcp
    try:
        http_calls.clear()
        yt_vpn = health._check_youtube(
            {"youtube_via_vpn": True},
            {"running": True, "port": 10818, "stream": False}, 5)
        vpn_calls = list(http_calls)
        http_calls.clear()
        dc_stream = health._check_discord(
            {}, {"running": True, "port": 10818, "stream": True}, 5)
        stream_calls = list(http_calls)
        http_calls.clear()
        yt_off = health._check_youtube(
            {"youtube_via_vpn": True}, {"running": False}, 5)
        off_calls = list(http_calls)
        http_calls.clear()
        yt_direct = health._check_youtube(
            {}, {"running": True, "port": 10818, "stream": False}, 5)
        direct_calls = list(http_calls)
    finally:
        health._http = real_http
        health._tcp = real_tcp
    check("youtube через VPN: ok и порт прокси",
          yt_vpn["state"] == "ok" and "через VPN" in yt_vpn["detail"]
          and bool(vpn_calls) and all(port == 10818 for _url, port in vpn_calls),
          str(yt_vpn))
    check("discord stream: ok через прокси без TCP",
          dc_stream["state"] == "ok" and bool(stream_calls)
          and all(port == 10818 for _url, port in stream_calls)
          and not tcp_calls, str(dc_stream))
    check("youtube через VPN без прокси — off",
          yt_off["state"] == "off" and "нужен VPN" in yt_off["detail"]
          and not off_calls, str(yt_off))
    check("youtube напрямую: без порта прокси",
          bool(direct_calls) and all(port is None for _url, port in direct_calls))

    # --- маршруты: чистка, решение, оверрайды --------------------------
    print("\n-- маршруты --")
    check("clean_domain: схема, порт, путь",
          routes.clean_domain("https://WWW.Example.COM:8443/path?x=1") == "www.example.com",
          routes.clean_domain("https://WWW.Example.COM:8443/path?x=1"))
    check("clean_domain: wildcard", routes.clean_domain("*.steampowered.com") == "steampowered.com")
    check("clean_domain: префикс domain:", routes.clean_domain("domain:instagram.com") == "instagram.com")
    check("clean_domain: geosite как есть",
          routes.clean_domain("geosite:telegram") == "geosite:telegram")
    check("decide: доступно везде — direct", routes.decide(True, True) == "direct")
    check("decide: только через прокси — proxy", routes.decide(False, True) == "proxy")
    check("decide: нигде — broken", routes.decide(False, False) == "broken")
    check("decide_route: direct", routes.decide_route(True, True, True) == "direct")
    check("decide_route: proxy", routes.decide_route(False, True, True) == "proxy")
    check("decide_route: без проверки прокси — unknown",
          routes.decide_route(False, False, False) == "unknown")
    check("decide_route: оба пути мертвы — broken",
          routes.decide_route(False, True, False) == "broken")

    routes.save_overrides({}, at=time.time())
    sample_audit = {
        "at": time.time(),
        "items": [
            {"domain": "instagram.com", "route": "proxy"},
            {"domain": "huggingface.co", "route": "direct"},
            {"domain": "github.com", "route": "unknown"},
        ],
    }
    routes.apply_audit(sample_audit)
    saved = routes.load_overrides()
    check("apply_audit: proxy сохранён", saved.get("instagram.com", {}).get("route") == "proxy")
    check("apply_audit: direct сохранён", saved.get("huggingface.co", {}).get("route") == "direct")
    check("apply_audit: unknown не сохранён", "github.com" not in saved)
    check("direct_domains по маршруту", routes.direct_domains() == ["huggingface.co"])
    check("proxy_domains по маршруту", routes.proxy_domains() == ["instagram.com"])

    original_probe_state = routes._probe_state
    routes._probe_state = lambda: {"running": False, "port": 0}
    try:
        stream_audit = routes.audit(
            {}, domains=["youtube.com", "www.youtube.com", "discord.com"])
    finally:
        routes._probe_state = original_probe_state
    check("audit пропускает YouTube/Discord и поддомены",
          stream_audit["items"] == [], str(stream_audit["items"]))
    check("audit: явный список доменов — частичный",
          stream_audit.get("full") is False, str(stream_audit.get("full")))

    # Полный аудит: домен выпал из проверенного набора — его маршрут уходит.
    routes.apply_audit({"at": time.time(), "full": True,
                        "items": [{"domain": "instagram.com", "route": "proxy"}]})
    saved = routes.load_overrides()
    check("apply_audit: снятый с аудита домен удалён",
          "huggingface.co" not in saved and "instagram.com" in saved, str(sorted(saved)))

    routes.save_overrides({"instagram.com": {"route": "proxy", "since": 1, "checked": 1},
                           "other.example": {"route": "direct", "since": 1,
                                             "checked": 1}},
                          at=time.time())
    routes.apply_audit({"at": time.time(),
                        "items": [{"domain": "instagram.com", "route": "proxy"}]})
    partial_saved = routes.load_overrides()
    check("apply_audit: частичный аудит не стирает остальные маршруты",
          "other.example" in partial_saved and "instagram.com" in partial_saved,
          str(sorted(partial_saved)))

    routes.save_overrides({"instagram.com": {"route": "proxy", "since": 1, "checked": 1}},
                          at=time.time())
    routes.apply_audit({"at": time.time(),
                        "items": [{"domain": "instagram.com", "route": "unknown"}]})
    check("apply_audit: unknown сохраняет прежний маршрут",
          routes.load_overrides().get("instagram.com", {}).get("route") == "proxy")

    routes.save_overrides({}, at=time.time() - 61 * 60)
    check("routes.due: давний аудит", routes.due(60) is True)
    routes.save_overrides({}, at=time.time())
    check("routes.due: свежий аудит", routes.due(60) is False)
    check("routes.fresh: свежий аудит", routes.fresh(60) is True)

    routes.overrides_path().write_text(
        json.dumps({"instagram.com": {"route": "proxy", "since": 1, "checked": 1}}),
        encoding="utf-8",
    )
    check("старый плоский формат читается",
          routes.load_overrides().get("instagram.com", {}).get("route") == "proxy")
    check("оверрайды: freshness — bool", isinstance(routes.stale(24), bool))
    routes.save_overrides({}, at=time.time())

    # --- надзиратель ---------------------------------------------------
    print("\n-- надзиратель --")
    check("overdue: метки нет", guard.overdue(0, 10) is True)
    check("overdue: только что", guard.overdue(time.time(), 10) is False)
    check("overdue: час назад", guard.overdue(time.time() - 3600, 10) is True)
    check("overdue: мусор в метке", guard.overdue("не дата", 10) is True)
    check("надзиратель выключен — действий нет",
          guard.heal({**cfg, "guard_enabled": False}, {}, {}) == [])
    actions_done = guard.heal(
        {**cfg, "tgws_exe": "", "guard_enabled": True},
        {"zapret": {"running": True, "dir": cfg.get("zapret_dir", "")},
         "tgwsproxy": {"running": False, "listening": False}},
        {"running": False},
    )
    kinds = [item["kind"] for item in actions_done]
    check("guard: tg-ws-proxy не установлен — off", kinds == ["off"], str(actions_done))

    guard_calls: list = []
    real_switch = xray.switch_server
    real_guard_routes = guard._heal_routes
    real_switch_at = guard._VPN_SWITCH_AT
    xray.switch_server = lambda *args, **kwargs: (
        guard_calls.append((args, kwargs))
        or {"ok": True, "server": {"name": "Не должен"}})
    guard._heal_routes = lambda cfg: []
    guard_status = {"zapret": {"running": True, "dir": cfg.get("zapret_dir", "")},
                    "tgwsproxy": {"running": False, "listening": False}}
    guard_cfg = {**cfg, "tgws_exe": "", "guard_enabled": True}
    try:
        # warn — «не успели проверить», а не «сервер мёртв»: менять нечего.
        guard._VPN_SWITCH_AT = 0.0
        guard.heal(guard_cfg, guard_status, {"running": True},
                   {"items": [{"id": "vpn", "state": "warn",
                               "detail": "проверка не успела"}]})
        check("guard heal: warn по VPN сервер не меняет",
              not guard_calls, str(guard_calls))
        # bad — сервер не отвечает: одно переключение с троттлингом 30 минут.
        guard._VPN_SWITCH_AT = 0.0
        guard.heal(guard_cfg, guard_status, {"running": True},
                   {"items": [{"id": "vpn", "state": "bad",
                               "detail": "сервер не отвечает"}]})
        check("guard heal: мёртвый VPN — одно переключение",
              len(guard_calls) == 1, str(guard_calls))
        guard.heal(guard_cfg, guard_status, {"running": True},
                   {"items": [{"id": "vpn", "state": "bad",
                               "detail": "сервер не отвечает"}]})
        check("guard heal: повтор в пределах 30 мин — без переключения",
              len(guard_calls) == 1, str(guard_calls))
    finally:
        xray.switch_server = real_switch
        guard._heal_routes = real_guard_routes
        guard._VPN_SWITCH_AT = real_switch_at

    # --- автозапуск на временном каталоге ------------------------------
    print("\n-- автозапуск --")
    startup = tmp / "Startup"
    startup.mkdir()
    original_link = autostart.link_path
    autostart.link_path = lambda: startup / "bypass-tray.lnk"
    try:
        created = autostart.enable()
        check("автозапуск: ярлык создан", created and autostart.is_enabled())
        again = autostart.enable()
        check("автозапуск: повторное создание перезаписывает", again and autostart.is_enabled())
        removed = autostart.disable()
        check("автозапуск: ярлык удалён", removed and not autostart.is_enabled())
    finally:
        autostart.link_path = original_link

    # --- обновления: сравнение версий ----------------------------------
    print("\n-- обновления: сравнение версий --")
    check("версии совпали (v-префикс)", updates._is_outdated("25.6.8", "v25.6.8") is False)
    check("версии разошлись", updates._is_outdated("25.6.7", "v25.6.8") is True)
    check("установленная неизвестна", updates._is_outdated("", "v25.6.8") is False)
    check("последний тег неизвестен", updates._is_outdated("25.6.7", "") is False)
    check("версия виджета прочитана", bool(updates.widget_version()),
          updates.widget_version())

    # --- direct-домены в конфиге Xray ----------------------------------
    print("\n-- direct-домены в конфиге Xray --")
    sample = xray.parse_vless("vless://uuid@example.com:443?type=tcp&security=none#test")
    routed = xray.build_config(sample, 10899, ["instagram.com"], False, ["instagram.com"])
    routed_rules = routed["routing"]["rules"]
    proxy_rule = next(r for r in routed_rules if r.get("outboundTag") == "proxy")
    check("direct вычтен из proxy",
          "domain:instagram.com" not in proxy_rule["domain"],
          str([d for d in proxy_rule["domain"] if "instagram" in d]))
    direct_rule = next(
        (rule for rule in routed_rules
         if rule.get("outboundTag") == "direct"
         and any("instagram" in str(d) for d in (rule.get("domain") or []))),
        None,
    )
    check("direct-правило добавлено", direct_rule is not None)
    if direct_rule is not None:
        proxy_index = next(index for index, rule in enumerate(routed_rules)
                           if rule.get("outboundTag") == "proxy")
        check("правило direct стоит раньше proxy",
              routed_rules.index(direct_rule) < proxy_index)
        ru_index = next(index for index, rule in enumerate(routed_rules)
                        if "geosite:category-ru" in (rule.get("domain") or []))
        check("РФ-правила остались выше proxy", ru_index < proxy_index)

    cleaned = xray.build_config(sample, 10899, ["domain:*.example.com"], False, ["*.example.com"])
    direct_names = [d for rule in cleaned["routing"]["rules"]
                    if rule.get("outboundTag") == "direct"
                    for d in (rule.get("domain") or [])]
    check("домены нормализованы без дублей",
          "domain:example.com" in direct_names
          and not any(d.startswith("domain:domain:") or d.startswith("*.")
                      for d in direct_names),
          str(direct_names))

    # --- панель: серверы, занятость, подтверждение маршрутов -----------
    print("\n-- панель: серверы и маршруты --")
    fake = FakeController()
    panel = Panel(fake)
    ui_root = tk.Tk()
    panel._root = ui_root
    panel._build(ui_root, theme.Theme(False))
    for _ in range(6):
        ui_root.update()
        time.sleep(0.02)
    panel._toggle_servers()
    ui_root.update()
    rows = panel._servers_list.winfo_children()
    rows[1].command()
    deadline = time.time() + 1.0
    while not fake.calls.get("select_server_name") and time.time() < deadline:
        time.sleep(0.02)
    check("панель передаёт сырое имя сервера",
          fake.calls.get("select_server_name") == "🇳🇱 Нидерланды")

    fake.proxy["busy"] = True
    panel._render_servers(force=True)
    ui_root.update()
    busy_rows = panel._servers_list.winfo_children()
    check("пока прокси занят, строки серверов не кликаются",
          bool(busy_rows) and all(row.command is None for row in busy_rows))
    fake.proxy["busy"] = False
    panel._render_servers(force=True)
    ui_root.update()

    fake.updates_report = {"checked": time.time(), "outdated": ["xray"],
                           "components": {}}
    panel._render_header_update()
    check("бейдж обновлений в шапке",
          panel._header_update._badge is not None)
    fake.updates_report = {"checked": time.time(), "outdated": [], "components": {}}
    panel._render_header_update()
    check("без обновлений бейджа нет", panel._header_update._badge is None)

    import bypass_tray.ui as ui_module

    real_ask = ui_module.messagebox.askyesno
    try:
        ui_module.messagebox.askyesno = lambda *a, **k: True
        routes_button = next(button for button in find_buttons(ui_root)
                             if button._text == "Проверить маршруты")
        routes_button._on_click(None)
        deadline = time.time() + 1.0
        while not fake.calls.get("apply_routes") and time.time() < deadline:
            time.sleep(0.02)
        check("маршруты применяются после подтверждения",
              bool(fake.calls.get("apply_routes")))

        fake.calls.pop("apply_routes", None)
        ui_module.messagebox.askyesno = lambda *a, **k: False
        routes_button._on_click(None)
        time.sleep(0.3)
        check("отказ в подтверждении не перезапускает туннель",
              not fake.calls.get("apply_routes"))
    finally:
        ui_module.messagebox.askyesno = real_ask

    panel._release()
    ui_root.destroy()
    del ui_root
    gc.collect()

    # --- автозапуск: оптимистичный конфиг и откат -----------------------
    print("\n-- автозапуск: оптимистичный переключатель --")
    real_enable, real_disable = autostart.enable, autostart.disable
    try:
        autostart.enable = lambda: True
        autostart.disable = lambda: True
        app = TrayApp()
        app.cfg["autoconnect"] = True
        config.save_config(app.cfg)
        app.set_autostart(False)
        check("тумблер меняет конфиг сразу", app.cfg.get("autoconnect") is False)
        deadline = time.time() + 2.0
        while app.busy.get("autostart") and time.time() < deadline:
            time.sleep(0.02)
        check("busy автозапуска снимается", not app.busy.get("autostart"))
        check("после успеха конфиг остаётся выключенным",
              app.cfg.get("autoconnect") is False)

        autostart.disable = lambda: False
        app.cfg["autoconnect"] = True
        config.save_config(app.cfg)
        app.set_autostart(False)
        deadline = time.time() + 2.0
        while app.busy.get("autostart") and time.time() < deadline:
            time.sleep(0.02)
        check("при отказе автозапуск откатывается", app.cfg.get("autoconnect") is True)
        check("busy снят и после отказа", not app.busy.get("autostart"))
    finally:
        autostart.enable, autostart.disable = real_enable, real_disable

    # --- restart_proxy: сохранённый сервер, без перекачки подписки -------
    print("\n-- перезапуск прокси --")
    proxy_app = TrayApp()
    proxy_app.last_check = time.time()
    proxy_app._net_checked = time.time()
    calls: dict = {}
    saved_server = {"name": "Тестовый", "host": "test.local", "port": 443,
                    "link": "vless://test@test.local:443"}
    real_saved = xray.saved_server
    real_switch = xray.switch_to
    real_start = xray.start
    real_stop = xray.stop

    def fake_switch(server, cfg):
        calls["switch"] = server
        return {"ok": True, "server": server, "port": 10818}

    xray.saved_server = lambda: saved_server
    xray.switch_to = fake_switch
    xray.start = lambda cfg=None: calls.setdefault("start", {"ok": True})
    xray.stop = lambda *args, **kwargs: calls.setdefault("stop", {"ok": True})
    try:
        started = proxy_app.restart_proxy()
        deadline = time.time() + 2.0
        while proxy_app.busy.get("proxy") and time.time() < deadline:
            time.sleep(0.02)
        check("restart_proxy возвращает True", started is True)
        check("restart_proxy берёт сохранённый сервер",
              calls.get("switch") is saved_server)
        check("restart_proxy не перекачивает подписку",
              "start" not in calls, str(sorted(calls)))
        check("busy прокси снят после успеха",
              not proxy_app.busy.get("proxy")
              and not proxy_app.proxy.get("busy"))

        def broken_switch(server, cfg):
            raise RuntimeError("boom")

        xray.switch_to = broken_switch
        proxy_app.restart_proxy()
        deadline = time.time() + 2.0
        while proxy_app.busy.get("proxy") and time.time() < deadline:
            time.sleep(0.02)
        check("busy прокси снимается и при исключении",
              not proxy_app.busy.get("proxy"))
        check("причина попала в сообщение",
              "не удалось" in str(proxy_app.proxy.get("message") or ""))
    finally:
        xray.saved_server = real_saved
        xray.switch_to = real_switch
        xray.start = real_start
        xray.stop = real_stop

    # --- миграция старых списков в xray_routes --------------------------
    print("\n-- миграция маршрутов --")
    legacy_cfg = {
        "xray_subscription": "https://example.com/sub/secret-token",
        "xray_direct_domains": ["*.steampowered.com", "https://www.x.com/a?b=1"],
        "xray_extra_domains": ["domain:epicgames.com", "geosite:youtube"],
        "zapret_dir": cfg.get("zapret_dir", ""),
        "tgws_exe": cfg.get("tgws_exe", ""),
    }
    config.save_config(legacy_cfg)
    migrated = config.load_config()
    check("миграция: direct из старого списка",
          migrated.get("xray_routes", {}).get("steampowered.com") == "direct")
    check("миграция: www и путь нормализованы",
          migrated.get("xray_routes", {}).get("x.com") == "direct")
    check("миграция: proxy из extra",
          migrated.get("xray_routes", {}).get("epicgames.com") == "proxy"
          and migrated["xray_routes"].get("geosite:youtube") == "proxy")
    check("миграция: старые списки очищены",
          migrated.get("xray_direct_domains") == []
          and migrated.get("xray_extra_domains") == [])
    check("миграция: подписка цела",
          migrated.get("xray_subscription") == legacy_cfg["xray_subscription"])

    second = dict(migrated)
    second["xray_routes"] = {"kept.com": "proxy"}
    second["xray_direct_domains"] = ["*.old.com"]
    second["xray_extra_domains"] = ["old-extra.com"]
    config.save_config(second)
    reloaded = config.load_config()
    check("повторная загрузка не пересобирает правила",
          reloaded.get("xray_routes") == {"kept.com": "proxy"},
          str(reloaded.get("xray_routes")))

    # Повторное чтение не должно перезаписывать файл.
    config.save_config({"zapret_dir": cfg.get("zapret_dir", r"C:\Zapret"),
                        "tgws_exe": cfg.get("tgws_exe", r"C:\TgWsProxy.exe"),
                        "xray_routes": {}})
    before_bytes = config.CONFIG_FILE.read_bytes()
    before_mtime = config.CONFIG_FILE.stat().st_mtime_ns
    time.sleep(0.02)
    config.load_config()
    check("повторный load не меняет содержимое",
          config.CONFIG_FILE.read_bytes() == before_bytes)
    check("повторный load не меняет mtime",
          config.CONFIG_FILE.stat().st_mtime_ns == before_mtime)

    config.save_config({"zapret_dir": "", "tgws_exe": "", "xray_routes": {}})
    real_autoz = config.autodetect_zapret
    real_autot = config.autodetect_tgws
    config.autodetect_zapret = lambda: r"C:\Found\Zapret"
    config.autodetect_tgws = lambda: r"C:\Found\TgWsProxy.exe"
    try:
        config.load_config()
    finally:
        config.autodetect_zapret = real_autoz
        config.autodetect_tgws = real_autot
    saved_file = json.loads(config.CONFIG_FILE.read_text(encoding="utf-8"))
    check("автодетект сохраняется в конфиг",
          saved_file.get("zapret_dir") == r"C:\Found\Zapret"
          and saved_file.get("tgws_exe") == r"C:\Found\TgWsProxy.exe")

    # --- системный прокси: чужой не трогаем ---------------------------
    class FakeKey:
        def __init__(self, values):
            self.values = values
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False

    class FakeWinreg:
        HKEY_CURRENT_USER = object()
        KEY_SET_VALUE = 1
        KEY_QUERY_VALUE = 2
        REG_DWORD = 4
        REG_SZ = 1
        def __init__(self, values):
            self.values = values
        def CreateKeyEx(self, *args):
            return FakeKey(self.values)
        def QueryValueEx(self, key, name):
            if name not in key.values:
                raise FileNotFoundError(name)
            return key.values[name], self.REG_SZ
        def SetValueEx(self, key, name, _reserved, _kind, value):
            key.values[name] = value
        def DeleteValue(self, key, name):
            key.values.pop(name, None)

    real_winreg = sys.modules.get("winreg")
    real_refresh = xray._refresh_wininet
    xray._refresh_wininet = lambda: None
    try:
        foreign = {"ProxyEnable": 1, "ProxyServer": "127.0.0.1:9999"}
        sys.modules["winreg"] = FakeWinreg(foreign)
        foreign_result = xray.set_system_proxy(False, 10818)
        check("system proxy: чужой прокси не снят",
              foreign_result.get("kept") is True
              and foreign.get("ProxyEnable") == 1
              and foreign.get("ProxyServer") == "127.0.0.1:9999")
        own = {"ProxyEnable": 1, "ProxyServer": "127.0.0.1:10818"}
        sys.modules["winreg"] = FakeWinreg(own)
        own_result = xray.set_system_proxy(False, 10818)
        check("system proxy: свой прокси снят",
              own_result.get("ok") is True and own.get("ProxyEnable") == 0
              and "ProxyServer" not in own)
    finally:
        if real_winreg is None:
            sys.modules.pop("winreg", None)
        else:
            sys.modules["winreg"] = real_winreg
        xray._refresh_wininet = real_refresh

    real_running = xray.is_running
    real_status = xray.status
    real_set_proxy = xray.set_system_proxy
    proxy_calls = []
    xray.is_running = lambda: True
    xray.status = lambda: {"running": True, "port": 10818}
    xray.set_system_proxy = lambda *args: proxy_calls.append(args)
    try:
        already = xray.start({"xray_system_proxy": True, "xray_port": 10818})
    finally:
        xray.is_running = real_running
        xray.status = real_status
        xray.set_system_proxy = real_set_proxy
    check("xray.start already включает системный прокси",
          already.get("already") is True and proxy_calls == [(True, 10818, False)],
          str(proxy_calls))

    # --- проверка доменов и пользовательские правила --------------------
    print("\n-- правила сайтов --")
    for value in ("x.com", "https://x.com/a?b=1", "www.x.com", "sub.x.com",
                  "*.x.com", "geosite:youtube"):
        ok, _reason = routes.is_valid_domain(value)
        check(f"домен «{value}» допустим", ok)
    for value in ("", "привет", "http://"):
        ok, reason = routes.is_valid_domain(value)
        check(f"домен «{value or 'пусто'}» отклонён", not ok and bool(reason),
              reason or "нет причины")
    direct, proxy = routes.user_routes({
        "xray_routes": {"a.com": "direct", "b.com": "proxy",
                        "www.c.com": "direct", "bad.com": "unknown"},
    })
    check("user_routes: direct отдельно", direct == ["a.com", "c.com"], str(direct))
    check("user_routes: proxy отдельно", proxy == ["b.com"], str(proxy))
    check("префикс geosite в нижнем регистре",
          routes.normalize_user_domain("GEOSITE:YouTube") == "geosite:youtube")
    check("regexp: префикс lower, шаблон не тронут",
          routes.normalize_user_domain(r"REGEXP:^.*\.Example\.COM$")
          == r"regexp:^.*\.Example\.COM$")
    idn_ok, idn_reason = routes.is_valid_domain("яндекс.рф")
    check("IDN-домен принимается", idn_ok, idn_reason or "")
    check("IDN переводится в punycode",
          routes.normalize_user_domain("яндекс.рф") == "xn--d1acpjx3f.xn--p1ai",
          routes.normalize_user_domain("яндекс.рф"))

    legacy_cfg = {"xray_routes": {},
                  "xray_extra_domains": ["legacy-proxy.example"],
                  "xray_direct_domains": ["legacy-direct.example"]}
    sample_server = xray.parse_vless(
        "vless://uuid@example.com:443?type=tcp&security=none#t")
    legacy_extra, legacy_direct = xray._config_route_lists(legacy_cfg, None)
    legacy_conf = xray.build_config(sample_server, 10899, legacy_extra, False,
                                    legacy_direct, {})
    legacy_text = json.dumps(legacy_conf)
    check("legacy игнорируется при наличии xray_routes",
          "legacy-proxy.example" not in legacy_text
          and "legacy-direct.example" not in legacy_text)
    plain_extra, plain_direct = xray._config_route_lists(
        {"xray_extra_domains": ["legacy-proxy.example"],
         "xray_direct_domains": ["legacy-direct.example"]}, None)
    plain_text = json.dumps(xray.build_config(sample_server, 10899, plain_extra,
                                              False, plain_direct, {}))
    check("legacy используется, пока ключа xray_routes нет",
          "legacy-proxy.example" in plain_text
          and "legacy-direct.example" in plain_text)

    # --- встроенный список заблокированных ------------------------------
    print("\n-- список заблокированных --")
    real_data = ROOT / "data" / "geosite-refilter.dat"
    parsed_real = blocked._parse(real_data)
    check("_parse реального файла",
          parsed_real["ok"] and parsed_real["category"] == "REFILTER"
          and parsed_real["count"] == 81395, str(parsed_real))
    raw = real_data.read_bytes()
    btmp = Path(tempfile.mkdtemp(prefix="bt-blocked-"))
    try:
        cut = btmp / "cut.dat"
        cut.write_bytes(raw[:600000])
        check("обрезанный файл невалиден", not blocked._valid(cut))
        junk = btmp / "junk.dat"
        junk.write_bytes(b"REFILTER" + b"\xff" * (1_200_000))
        check("мусор с сигнатурой невалиден", not blocked._valid(junk))

        source_file = btmp / "src.dat"
        source_file.write_bytes(raw)
        dest_file = btmp / "bin" / blocked.FILE_NAME
        copied = blocked.ensure(source_file, dest_file, auto_download=False)
        check("blocked.ensure копирует поставку",
              copied == dest_file and blocked._valid(dest_file))
        before_bytes = dest_file.read_bytes()
        again = blocked.ensure(source_file, dest_file, auto_download=False)
        check("повторный ensure не перезаписывает валидный файл",
              again == dest_file and dest_file.read_bytes() == before_bytes)

        bad_dest = btmp / "bad" / blocked.FILE_NAME
        bad_dest.parent.mkdir(parents=True, exist_ok=True)
        bad_dest.write_bytes(cut.read_bytes())
        fixed = blocked.ensure(source_file, bad_dest, auto_download=False)
        check("ensure заменяет битый файл",
              fixed == bad_dest and blocked._valid(bad_dest))
        check("ensure: битая поставка не принимается",
              blocked.ensure(cut, btmp / "x" / blocked.FILE_NAME,
                             auto_download=False) is None)

        update_dest = btmp / "update" / blocked.FILE_NAME
        real_bin = blocked.bin_path
        real_url = blocked.SOURCE_URL
        progress_calls = []
        blocked.bin_path = lambda: update_dest
        blocked.SOURCE_URL = source_file.as_uri()
        try:
            updated = blocked.update(lambda *args: progress_calls.append(args))
        finally:
            blocked.bin_path = real_bin
            blocked.SOURCE_URL = real_url
        check("blocked.update скачивает и проверяет",
              bool(updated.get("ok")) and updated.get("count") == 81395
              and blocked._valid(update_dest))
        check("blocked.update сообщает прогресс", bool(progress_calls),
              str(progress_calls[:1]))
        state_file = config.config_dir() / "blocked-list.json"
        state = json.loads(state_file.read_text(encoding="utf-8"))
        check("blocked.update пишет состояние",
              state.get("size") == update_dest.stat().st_size
              and state.get("count") == 81395
              and state.get("category") == "REFILTER")

        blocked.bin_path = lambda: update_dest
        try:
            info_on = blocked.info({"blocked_list_enabled": True})
            info_off = blocked.info({"blocked_list_enabled": False})
        finally:
            blocked.bin_path = real_bin
        check("blocked.info: число, версия, дата",
              info_on["installed"] and info_on["enabled"]
              and info_on["count"] == 81395
              and info_on["version"] == state.get("version")
              and len(info_on["version"]) == 8
              and info_on["date"] == state.get("date")
              and info_on["updated"] > 0)
        check("blocked.info: выключение отражается", info_off["enabled"] is False)

        part_file = update_dest.with_name(update_dest.name + ".part")
        blocked.bin_path = lambda: update_dest
        blocked.SOURCE_URL = cut.as_uri()
        try:
            failed = blocked.update()
        finally:
            blocked.bin_path = real_bin
            blocked.SOURCE_URL = real_url
        check("update с обрезанным файлом отклонён",
              failed.get("ok") is False and not part_file.exists()
              and blocked._valid(update_dest))

        # автоскачивание и троттлинг
        auto_dest = btmp / "auto" / blocked.FILE_NAME
        calls = {"n": 0}
        real_packaged = blocked.packaged_path
        real_update = blocked.update
        real_dest_bin = blocked.bin_path

        def fake_update(progress=None):
            calls["n"] += 1
            auto_dest.parent.mkdir(parents=True, exist_ok=True)
            auto_dest.write_bytes(raw)
            blocked._write_state({"last_attempt": time.time(), "count": 81395})
            return {"ok": True, "size": len(raw), "updated": time.time(),
                    "count": 81395}

        state_file.write_text("{}", encoding="utf-8")
        blocked.packaged_path = lambda: btmp / "no-packaged.dat"
        blocked.bin_path = lambda: auto_dest
        blocked.update = fake_update
        try:
            first = blocked.ensure()
            check("ensure скачивает при отсутствии поставки",
                  calls["n"] == 1 and first == auto_dest)
            auto_dest.unlink()
            second = blocked.ensure()
            check("автоскачивание не чаще раза в час",
                  second is None and calls["n"] == 1)
            third = blocked.ensure(auto_download=False)
            check("auto_download=False не ходит в сеть",
                  third is None and calls["n"] == 1)
        finally:
            blocked.packaged_path = real_packaged
            blocked.bin_path = real_dest_bin
            blocked.update = real_update
    finally:
        shutil.rmtree(btmp, ignore_errors=True)

    # --- порядок правил с preset ----------------------------------------
    blocked_conf = xray.build_config(
        sample_server, 10899, None, False, ["audit-direct.example"],
        {"user-direct.example": "direct", "user-proxy.example": "proxy"},
        "ext:blocked:REFILTER")
    blocked_rules = blocked_conf["routing"]["rules"]

    def rule_index(predicate) -> int:
        return next(index for index, rule in enumerate(blocked_rules)
                    if predicate(rule))

    idx_user_direct = rule_index(
        lambda r: "domain:user-direct.example" in (r.get("domain") or []))
    idx_user_proxy = rule_index(
        lambda r: "domain:user-proxy.example" in (r.get("domain") or []))
    idx_audit = rule_index(
        lambda r: "domain:audit-direct.example" in (r.get("domain") or []))
    idx_stream = rule_index(
        lambda r: "geosite:youtube" in (r.get("domain") or []))
    idx_blocked = rule_index(
        lambda r: (r.get("domain") or []) == ["ext:blocked:REFILTER"])
    idx_ru = rule_index(
        lambda r: "geosite:category-ru" in (r.get("domain") or []))
    idx_builtin = max(index for index, rule in enumerate(blocked_rules)
                      if rule.get("outboundTag") == "proxy")
    check("порядок правил с preset",
          idx_user_direct < idx_user_proxy < idx_audit < idx_stream
          < idx_blocked < idx_ru < idx_builtin,
          f"{idx_user_direct},{idx_user_proxy},{idx_audit},{idx_stream},"
          f"{idx_blocked},{idx_ru},{idx_builtin}")

    stream_conf = xray.build_config(sample_server, 10899, None, True, None, {},
                                    "ext:blocked:REFILTER")
    stream_rules = stream_conf["routing"]["rules"]
    check("в резервном режиме нет stream direct",
          not any(rule.get("outboundTag") == "direct"
                  and "geosite:youtube" in (rule.get("domain") or [])
                  for rule in stream_rules)
          and any((rule.get("domain") or []) == ["ext:blocked:REFILTER"]
                  for rule in stream_rules))

    user_youtube = xray.build_config(sample_server, 10899, None, False, None,
                                     {"youtube.com": "proxy"},
                                     "ext:blocked:REFILTER")
    yt_rules = user_youtube["routing"]["rules"]
    idx_user_yt = next(index for index, rule in enumerate(yt_rules)
                       if "domain:youtube.com" in (rule.get("domain") or [])
                       and rule.get("outboundTag") == "proxy")
    idx_stream_yt = next(index for index, rule in enumerate(yt_rules)
                         if "geosite:youtube" in (rule.get("domain") or [])
                         and rule.get("outboundTag") == "direct")
    check("user proxy перебивает stream direct", idx_user_yt < idx_stream_yt)
    check("preset не попадает в обход системного прокси",
          "REFILTER" not in xray._proxy_bypass_list())

    real_ensure_blocked = blocked.ensure
    try:
        blocked.ensure = lambda: Path(r"C:\x\geosite-refilter.dat")
        rule_on, ok_on = xray._blocked_preset({"blocked_list_enabled": True})
        rule_off, ok_off = xray._blocked_preset({"blocked_list_enabled": False})
        blocked.ensure = lambda: None
        rule_none, ok_none = xray._blocked_preset({"blocked_list_enabled": True})
    finally:
        blocked.ensure = real_ensure_blocked
    check("start включает preset при валидном файле",
          ok_on and rule_on == "ext:geosite-refilter.dat:REFILTER")
    check("start не включает preset при выключенном флаге",
          not ok_off and rule_off is None)
    check("нет файла — нет preset", not ok_none and rule_none is None)

    # --- обход системного прокси для YouTube/Discord ---------------------
    original_cfg = config.load_config()
    config.save_config({**original_cfg, "xray_routes": {}})
    direct_bypass = set(xray._proxy_bypass_list(False).split(";"))
    check("bypass stream=False: YouTube и Discord мимо прокси",
          "youtube.com" in direct_bypass and "*.youtube.com" in direct_bypass
          and "*.googlevideo.com" in direct_bypass
          and "discord.com" in direct_bypass and "*.discord.com" in direct_bypass)
    stream_bypass = set(xray._proxy_bypass_list(True).split(";"))
    check("bypass stream=True: stream-домены не добавлены",
          "youtube.com" not in stream_bypass and "discord.com" not in stream_bypass)
    config.save_config({**original_cfg, "xray_routes": {"youtube.com": "proxy"}})
    user_bypass = set(xray._proxy_bypass_list(False).split(";"))
    check("user proxy исключает YouTube из bypass",
          "youtube.com" not in user_bypass and "*.youtube.com" not in user_bypass)

    def group_in_bypass(items: set[str], group: tuple[str, ...]) -> bool:
        return any(member in items or f"*.{member}" in items for member in group)

    config.save_config({**original_cfg, "xray_routes": {"youtube.com": "proxy"}})
    youtube_bypass = set(xray._proxy_bypass_list(False).split(";"))
    check("user proxy youtube убирает всю YouTube-группу",
          not group_in_bypass(youtube_bypass, xray.YOUTUBE_GROUP),
          str([m for m in xray.YOUTUBE_GROUP if m in youtube_bypass]))
    config.save_config({**original_cfg, "xray_routes": {"discord.com": "proxy"}})
    discord_bypass = set(xray._proxy_bypass_list(False).split(";"))
    check("user proxy discord убирает всю Discord-группу",
          not group_in_bypass(discord_bypass, xray.DISCORD_GROUP),
          str([m for m in xray.DISCORD_GROUP if m in discord_bypass]))
    config.save_config(original_cfg)

    sample_server = xray.parse_vless("vless://uuid@example.com:443?type=tcp&security=none#t")
    no_stream_cfg = xray.build_config(sample_server, 10899, None, False)
    no_stream_rules = no_stream_cfg["routing"]["rules"]
    check("stream=False: direct-правило YouTube есть",
          any(rule.get("outboundTag") == "direct"
              and "geosite:youtube" in (rule.get("domain") or [])
              for rule in no_stream_rules))
    no_stream_proxy = next(rule for rule in no_stream_rules
                           if rule.get("outboundTag") == "proxy")
    check("stream=False: YouTube не в proxy",
          not any("youtube" in str(d) for d in no_stream_proxy["domain"]))
    with_stream_cfg = xray.build_config(sample_server, 10899, None, True)
    with_stream_rules = with_stream_cfg["routing"]["rules"]
    check("stream=True: direct-правила YouTube нет",
          not any(rule.get("outboundTag") == "direct"
                  and "geosite:youtube" in (rule.get("domain") or [])
                  for rule in with_stream_rules))
    with_stream_proxy = next(rule for rule in with_stream_rules
                             if rule.get("outboundTag") == "proxy")
    check("stream=True: YouTube в proxy",
          any("youtube.com" in str(d) for d in with_stream_proxy["domain"]))
    check("youtube_via_vpn включает stream",
          xray._effective_stream({"youtube_via_vpn": True}, False) is True
          and xray._effective_stream({"youtube_via_vpn": True}, True) is True
          and xray._effective_stream({"youtube_via_vpn": False}, False) is False)
    real_status_collect = status.collect
    try:
        status.collect = lambda cfg=None: {"zapret": {"running": True}}
        check("desired stream: zapret работает — прямой",
              xray._desired_stream({"youtube_via_vpn": False}, True) is False)
        check("desired stream: флаг YouTube важнее",
              xray._desired_stream({"youtube_via_vpn": True}, False) is True)
        status.collect = lambda cfg=None: {"zapret": {"running": False}}
        check("desired stream: zapret стоит — резервный",
              xray._desired_stream({"youtube_via_vpn": False}, False) is True)
    finally:
        status.collect = real_status_collect

    # --- strategy: реальная загрузка 512 КБ ------------------------------
    print("\n-- strategy: загрузка 512 КБ --")
    import http.server
    import socket

    class _BigFile(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(512 * 1024))
            self.end_headers()
            self.wfile.write(b"x" * (512 * 1024))

        def log_message(self, *args):
            pass

    class _SlowFile(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "1048576")
            self.end_headers()
            time.sleep(1.0)
            try:
                self.wfile.write(b"x")
            except OSError:
                pass

        def log_message(self, *args):
            pass

    big_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _BigFile)
    big_thread = threading.Thread(target=big_server.serve_forever, daemon=True)
    big_thread.start()
    try:
        probe_result = strategy.probe_url(
            f"http://127.0.0.1:{big_server.server_address[1]}/data", timeout=5)
    finally:
        big_server.shutdown()
        big_server.server_close()
    check("probe_url скачивает 512 КБ",
          probe_result["ok"] and probe_result["bytes"] >= 512 * 1024,
          str(probe_result))
    check("probe_url считает скорость", probe_result["kbps"] > 0,
          str(probe_result.get("kbps")))

    slow_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SlowFile)
    slow_thread = threading.Thread(target=slow_server.serve_forever, daemon=True)
    slow_thread.start()
    try:
        slow_result = strategy.probe_url(
            f"http://127.0.0.1:{slow_server.server_address[1]}/slow", timeout=0.2)
    finally:
        slow_server.shutdown()
        slow_server.server_close()
    check("probe_url: таймаут — ok=False", not slow_result["ok"])

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    free_port = sock.getsockname()[1]
    sock.close()
    broken = strategy.probe_url(f"http://127.0.0.1:{free_port}/", timeout=0.5)
    check("probe_url: обрыв — ok=False", not broken["ok"])
    check("порог 64 КБ: мало",
          not strategy._target_ok({"ok": True, "bytes": 63 * 1024}))
    check("порог 64 КБ: достаточно",
          strategy._target_ok({"ok": True, "bytes": 64 * 1024}))
    check("порог: ошибка не засчитывается",
          not strategy._target_ok({"ok": False, "bytes": 512 * 1024}))
    low = {"name": "low", "score": 1,
           "targets": {"t": {"bytes": 100000, "ms": 100}}}
    big_slow = {"name": "big-slow", "score": 2,
                "targets": {"t": {"bytes": 70000, "ms": 200}}}
    big_fast = {"name": "big-fast", "score": 2,
                "targets": {"t": {"bytes": 70000, "ms": 50}}}
    ranked = strategy._rank_candidates([low, big_slow, big_fast])
    check("sweep-сортировка: score → bytes → ms",
          [entry["name"] for entry in ranked] == ["big-fast", "big-slow", "low"],
          str([entry["name"] for entry in ranked]))
    priority = xray.build_config(sample_server, 10899, None, False, None,
                                 {"instagram.com": "direct"})
    priority_rules = priority["routing"]["rules"]
    check("user direct: правило первое",
          priority_rules[0].get("outboundTag") == "direct"
          and "domain:instagram.com" in (priority_rules[0].get("domain") or []))
    priority_proxy = next(r for r in priority_rules if r.get("outboundTag") == "proxy")
    check("user direct вычтен из proxy",
          "domain:instagram.com" not in priority_proxy["domain"])

    only_proxy = xray.build_config(sample_server, 10899, None, False, None,
                                   {"yandex.ru": "proxy"})
    proxy_rules = only_proxy["routing"]["rules"]
    yandex_index = next(
        index for index, rule in enumerate(proxy_rules)
        if "domain:yandex.ru" in (rule.get("domain") or [])
        and rule.get("outboundTag") == "proxy")
    ru_index = next(
        index for index, rule in enumerate(proxy_rules)
        if "geosite:category-ru" in (rule.get("domain") or []))
    check("user proxy стоит раньше РФ-direct", yandex_index < ru_index)

    audit_case = xray.build_config(sample_server, 10899, None, False,
                                   ["instagram.com"], {"instagram.com": "proxy"})
    audit_rules = audit_case["routing"]["rules"]
    check("user proxy важнее audit direct",
          audit_rules[0].get("outboundTag") == "proxy"
          and "domain:instagram.com" in (audit_rules[0].get("domain") or []))
    check("audit direct не дублирует user proxy",
          not any("instagram.com" in str(d) and rule.get("outboundTag") == "direct"
                  for rule in audit_rules for d in (rule.get("domain") or [])))

    config.save_config({**config.load_config(),
                        "xray_routes": {"bypass-direct.example": "direct",
                                        "bypass-proxy.example": "proxy"},
                        "xray_direct_domains": [],
                        "xray_extra_domains": []})
    bypass = xray._proxy_bypass_list()
    check("_proxy_bypass_list: user direct включён",
          "bypass-direct.example" in bypass)
    check("_proxy_bypass_list: user proxy исключён",
          "bypass-proxy.example" not in bypass)

    routes.save_overrides({"x.com": {"route": "proxy", "since": 1, "checked": 1}},
                          at=time.time())
    routes.apply_audit(
        {"at": time.time(),
         "items": [{"domain": "x.com", "route": "proxy"},
                   {"domain": "audit-only.example", "route": "proxy"}]},
        {"xray_routes": {"x.com": "direct"}},
    )
    overrides = routes.load_overrides()
    check("apply_audit не пишет оверрайд для своего правила", "x.com" not in overrides)
    check("apply_audit остальные домены сохраняет",
          overrides.get("audit-only.example", {}).get("route") == "proxy")

    # --- HoverRow и AnimatedSection --------------------------------------
    print("\n-- виджеты: строка и анимация --")
    hover_root = tk.Tk()
    hover_root.geometry("300x200+300+300")
    try:
        plain = HoverRow(hover_root, theme.Theme(False))
        plain.pack(fill="x")
        plain_label = tk.Label(plain, text="некликабельно", bg=theme.LIGHT["card"])
        plain_label.pack(fill="x")
        hover_root.update()
        before = plain.cget("bg")
        plain.event_generate("<Enter>")
        hover_root.update()
        check("HoverRow без command не подсвечивает",
              plain.cget("bg") == before)

        clicks: list[int] = []
        active = HoverRow(hover_root, theme.Theme(False),
                          command=lambda: clicks.append(1))
        active.pack(fill="x")
        active_label = tk.Label(active, text="кликабельно", bg=theme.LIGHT["card"])
        active_label.pack(fill="x")
        active.track(active_label)
        hover_root.update()
        base = active.cget("bg")
        active.event_generate("<Enter>")
        hover_root.update()
        check("HoverRow с command подсвечивает", active.cget("bg") != base)
        active_label.event_generate("<Button-1>", x=2, y=2)
        hover_root.update()
        check("клик по дочернему элементу строки", bool(clicks))

        section_root = tk.Tk()
        section_root.geometry("300x300+300+300")
        section = AnimatedSection(section_root, theme.Theme(False))
        section.pack(fill="x")
        tk.Label(section.content, text="строка", bg=theme.LIGHT["card"]).pack()
        section_root.update()
        check("AnimatedSection — Canvas", isinstance(section, tk.Canvas))
        check("AnimatedSection закрыт — item скрыт",
              section.itemcget(section._item, "state") == "hidden")
        section.expand()
        section_root.update()
        expected = section.content.winfo_reqheight()
        check("AnimatedSection раскрыт мгновенно на reqheight",
              section.winfo_height() == expected
              and section.itemcget(section._item, "state") == "normal",
              f"{section.winfo_height()} vs {expected}")
        section.collapse()
        section_root.update()
        check("AnimatedSection закрыт мгновенно",
              int(section.cget("height")) == 0
              and not section.winfo_ismapped()
              and section.winfo_manager() == ""
              and section.itemcget(section._item, "state") == "hidden",
              f"height={section.cget('height')}, mapped={section.winfo_ismapped()}")

        toggle = ToggleSwitch(section_root, theme.Theme(False))
        toggle.pack()
        toggle.set(True)
        check("ToggleSwitch без анимации",
              toggle.get() is True and toggle._pos == 1.0
              and not hasattr(toggle, "_after"))
        toggle.set(False)
        check("ToggleSwitch выключается сразу",
              toggle.get() is False and toggle._pos == 0.0)
        notified: list[bool] = []
        toggle.command = notified.append
        toggle.set(True, notify=True)
        check("ToggleSwitch сразу уведомляет", notified == [True])
        section_root.destroy()
    finally:
        hover_root.destroy()

    # --- гистерезис проверки туннеля ------------------------------------
    print("\n-- проверка туннеля: гистерезис --")
    tunnel_app = TrayApp()
    tunnel_app.proxy = {**tunnel_app.proxy, "running": True, "busy": False,
                        "host": "old.example", "server": "Старый"}
    tunnel_app.refresh = lambda: None
    notifications: list[str] = []
    tunnel_app._notify_throttled = (
        lambda key, message, hours=1.0: notifications.append(message))
    verify_calls: list[tuple] = []
    switch_calls: list[tuple] = []
    verify_state = {"ok": False}
    switch_state = {"ok": True}

    real_verify = xray.verify
    real_switch = xray.switch_server
    real_status = xray.status
    xray.verify = lambda target="", timeout=0: (
        verify_calls.append((target, timeout))
        or {"ok": verify_state["ok"], "ms": 120})
    xray.switch_server = lambda exclude=None, cfg=None: (
        switch_calls.append((set(exclude or ()), dict(cfg or {})))
        or ({"ok": True, "server": {"name": "Новый", "host": "new.example"}}
            if switch_state["ok"] else {"ok": False, "error": "нет"}))
    xray.status = lambda: {"running": True, "server": "Новый",
                           "host": "new.example", "port": 10818}

    def tunnel_trigger() -> None:
        tunnel_wait(lambda: not tunnel_app._health_checking, 0.5)
        tunnel_app._health_tick = 11
        tunnel_app.check_proxy_health()

    def tunnel_wait(predicate, timeout: float = 1.5) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return predicate()

    try:
        tunnel_trigger()
        tunnel_wait(lambda: tunnel_app._proxy_failures == 1)
        tunnel_trigger()
        tunnel_wait(lambda: tunnel_app._proxy_failures == 2)
        check("туннель: две неудачи — без смены",
              len(switch_calls) == 0 and tunnel_app._proxy_failures == 2,
              f"failed={tunnel_app._proxy_failures}, switch={len(switch_calls)}")

        verify_state["ok"] = True
        tunnel_app._proxy_failures = 2
        tunnel_trigger()
        tunnel_wait(lambda: tunnel_app._proxy_failures == 0)
        check("туннель: успех сбрасывает счётчик",
              tunnel_app._proxy_failures == 0 and not switch_calls)

        verify_state["ok"] = False
        for expected in (1, 2):
            tunnel_trigger()
            tunnel_wait(lambda n=expected: tunnel_app._proxy_failures == n)
        tunnel_trigger()
        tunnel_wait(lambda: tunnel_app._last_auto_switch > 0
                    and any("Сервер заменён" in text for text in notifications))
        check("туннель: третья неудача — смена",
              len(switch_calls) == 1 and tunnel_app._last_auto_switch > 0,
              f"switch={len(switch_calls)}, last={tunnel_app._last_auto_switch}")
        check("туннель: verify(target=Telegram, timeout=10)",
              verify_calls[-1] == ("api.telegram.org", 10), str(verify_calls[-1]))
        check("туннель: в журнале «переключил сервер: Новый»",
              any(row.get("text") == "переключил сервер: Новый"
                  for row in activity.recent(20)))
        check("туннель: кулдаун установлен", tunnel_app._last_auto_switch > 0)

        before_verify = len(verify_calls)
        before_switch = len(switch_calls)
        tunnel_trigger()
        time.sleep(0.12)
        check("туннель: кулдаун блокирует повторную проверку",
              len(verify_calls) == before_verify
              and len(switch_calls) == before_switch)

        tunnel_app._last_auto_switch = 0.0
        tunnel_app.proxy = {**tunnel_app.proxy, "running": False}
        tunnel_trigger()
        time.sleep(0.12)
        check("туннель: не running — проверки нет",
              len(verify_calls) == before_verify)
        tunnel_app.proxy = {**tunnel_app.proxy, "running": True, "busy": True}
        tunnel_trigger()
        time.sleep(0.12)
        check("туннель: busy — проверки нет",
              len(verify_calls) == before_verify)
        tunnel_app.proxy = {**tunnel_app.proxy, "running": True, "busy": False}

        switch_state["ok"] = False
        tunnel_app._health_tick = 0
        tunnel_app._last_auto_switch = 0.0
        tunnel_app._proxy_failures = 0
        for expected in (1, 2):
            tunnel_trigger()
            tunnel_wait(lambda n=expected: tunnel_app._proxy_failures == n)
        tunnel_trigger()
        tunnel_wait(lambda: any("Не удалось" in text for text in notifications))
        check("туннель: провал смены тоже ставит кулдаун",
              tunnel_app._last_auto_switch > 0
              and any("Не удалось" in text for text in notifications),
              f"last={tunnel_app._last_auto_switch}, {notifications}")
        check("туннель: провал смены в журнале",
              any(row.get("text") == "не удалось переключить сервер"
                  for row in activity.recent(20)))
    finally:
        xray.verify = real_verify
        xray.switch_server = real_switch
        xray.status = real_status

    # --- обновление всего ------------------------------------------------
    print("\n-- обновление всего --")
    from bypass_tray import app as app_module
    up_app = TrayApp()
    up_app.last_check = time.time()
    up_app._net_checked = time.time()
    real_check_all = app_module.updates.check_all
    real_update_all = app_module.updates.update_all
    calls = {"check": 0, "update": 0}
    app_module.updates.check_all = lambda cfg, state: (
        calls.__setitem__("check", calls["check"] + 1)
        or {"checked": time.time(), "outdated": ["zapret"], "components": {}})
    app_module.updates.update_all = lambda cfg, state, progress=None: (
        calls.__setitem__("update", calls["update"] + 1)
        or {"ok": True, "errors": []})
    try:
        started = up_app.update_everything()
        deadline = time.time() + 3
        while up_app.updates_running and time.time() < deadline:
            time.sleep(0.02)
        check("update_everything запускается", started is True)
        check("update_everything проверяет и обновляет",
              calls == {"check": 2, "update": 1}, str(calls))
        check("update_everything снимает busy",
              not up_app.updates_running and not up_app.busy.get("updates"))
    finally:
        app_module.updates.check_all = real_check_all
        app_module.updates.update_all = real_update_all

    # --- update_all: служба zapret и tgws --------------------------------
    print("\n-- update_all: служба и tgws --")
    real_service = updates.status.service_image_path
    real_install_z = updates.components.install_zapret
    real_install_t = updates.components.install_tgws
    real_tgws_running = updates.actions.tgws_running
    real_stop_tgws = updates.actions.stop_tgws
    real_start_tgws = updates.actions.start_tgws
    real_ensure_core = updates.xray.ensure_core
    real_xray_running = updates.xray.is_running
    real_xray_version = updates._xray_version
    service_cfg = {"zapret_dir": r"C:\Custom\Zapret", "zapret_bat": "old.bat",
                   "tgws_exe": ""}
    service_state: dict = {}
    install_z_calls = []
    updates.status.service_image_path = lambda: r"C:\Windows\winws.exe"
    updates.components.install_zapret = lambda *a, **k: install_z_calls.append(1)
    updates.components.install_tgws = lambda *a, **k: {
        "ok": True, "version": "tgws-test", "exe": service_cfg["tgws_exe"]}
    updates.actions.tgws_running = lambda: False
    updates.xray.is_running = lambda: False
    updates.xray.ensure_core = lambda force=False: Path("xray.exe")
    updates._xray_version = lambda: "xray-test"
    try:
        service_result = updates.update_all(service_cfg, service_state)
    finally:
        updates.status.service_image_path = real_service
        updates.components.install_zapret = real_install_z
        updates.components.install_tgws = real_install_t
        updates.actions.tgws_running = real_tgws_running
        updates.xray.is_running = real_xray_running
        updates.xray.ensure_core = real_ensure_core
        updates._xray_version = real_xray_version
    check("update_all: служба zapret не переустанавливается",
          not install_z_calls and service_cfg["zapret_dir"] == r"C:\Custom\Zapret"
          and any("службой" in text for text in service_result.get("warnings", [])),
          str(service_result.get("warnings")))

    portable = tmp / "portable-zapret"
    portable.mkdir()
    (portable / "general.bat").write_text("@echo off", encoding="utf-8")
    portable_cfg = {"zapret_dir": str(portable), "zapret_bat": "missing.bat",
                    "tgws_exe": "old-tgws.exe"}
    portable_state: dict = {}
    target_calls = []
    tgws_calls = []
    updates.status.service_image_path = lambda: ""
    def fake_install_z(progress=None, target=None):
        target_calls.append(target)
        return {"ok": True, "dir": str(target), "version": "z-test"}
    updates.components.install_zapret = fake_install_z
    updates.components.install_tgws = lambda *a, **k: {
        "ok": True, "version": "tgws-test", "exe": "new-tgws.exe"}
    updates.actions.tgws_running = lambda: True
    updates.actions.stop_tgws = lambda: tgws_calls.append("stop") or {"ok": True}
    updates.actions.start_tgws = lambda cfg: tgws_calls.append("start") or True
    updates.xray.is_running = lambda: False
    updates.xray.ensure_core = lambda force=False: Path("xray.exe")
    updates._xray_version = lambda: "xray-test"
    try:
        portable_result = updates.update_all(portable_cfg, portable_state)
    finally:
        updates.status.service_image_path = real_service
        updates.components.install_zapret = real_install_z
        updates.components.install_tgws = real_install_t
        updates.actions.tgws_running = real_tgws_running
        updates.actions.stop_tgws = real_stop_tgws
        updates.actions.start_tgws = real_start_tgws
        updates.xray.is_running = real_xray_running
        updates.xray.ensure_core = real_ensure_core
        updates._xray_version = real_xray_version
    check("update_all: портативный путь сохранён",
          target_calls == [portable] and portable_cfg["zapret_dir"] == str(portable),
          str(target_calls))
    check("update_all: стратегия fallback по general*.bat",
          portable_cfg.get("zapret_bat") == "general.bat",
          portable_cfg.get("zapret_bat"))
    check("update_all: tgws остановлен и запущен снова",
          tgws_calls == ["stop", "start"], str(tgws_calls))
    check("update_all: portable завершился", portable_result.get("ok") is True,
          str(portable_result.get("errors")))

    # --- настройки: маршруты сайтов -------------------------------------
    print("\n-- настройки: маршруты сайтов --")
    settings_root = tk.Tk()
    settings_root.geometry("400x200+200+200")
    rule_fake = FakeController()
    rule_fake.cfg["xray_routes"] = {"x.com": "direct", "instagram.com": "proxy"}
    import bypass_tray.settings_ui as settings_module

    real_blocked_info = settings_module.blocked.info
    settings_module.blocked.info = lambda cfg=None: {
        "enabled": True, "installed": True, "size": 1739390,
        "updated": time.time(), "state_at": time.time(),
        "source": "1andrevich/Re-filter-lists",
        "category": "REFILTER", "count": 81395, "version": "19102026",
    }
    settings = SettingsWindow(settings_root, theme.Theme(False), rule_fake)
    settings.open()
    for _ in range(6):
        settings_root.update()
        time.sleep(0.02)
    check("настройки: правила загружены из конфига",
          settings.rules == {"x.com": "direct", "instagram.com": "proxy"})
    check("настройки: тумблер списка включён",
          settings.blocked_toggle.get() is True)
    status_text = settings.blocked_status.cget("text")
    check("настройки: статус с числом доменов и набором",
          "81 395 доменов" in status_text
          and "набор 19.10.2026" in status_text
          and "обновлён" in status_text, status_text)
    check("настройки: YouTube через VPN выключен по умолчанию",
          settings.toggles["youtube"].get() is False)
    check("колесо повешено на список и страницу настроек",
          bool(settings.rules_scroll.inner.bind("<MouseWheel>"))
          and bool(settings.body.canvas.bind("<MouseWheel>")))
    settings.route_var.set("https://www.example.org/path?x=1")
    settings.route_segment.set("proxy")
    settings._add_rule()
    check("настройки: добавление нормализует домен",
          settings.rules.get("example.org") == "proxy")
    settings.route_var.set("example.org")
    settings.route_segment.set("proxy")
    settings._add_rule()
    check("настройки: дубль не добавляется",
          settings.rules.get("example.org") == "proxy"
          and "уже есть" in settings.route_status.cget("text"))
    settings._toggle_rule("example.org")
    check("настройки: переключение пути", settings.rules["example.org"] == "direct")
    settings.route_var.set("example.org")
    settings.route_segment.set("proxy")
    settings._add_rule()
    check("настройки: дубль с другим путём обновляет правило",
          settings.rules.get("example.org") == "proxy"
          and "обновлено" in settings.route_status.cget("text"))
    settings.route_var.set("enter.example")
    settings.route_segment.set("direct")
    settings.window.focus_force()
    settings.route_entry.focus_set()
    settings.route_entry.event_generate("<Return>", when="now")
    settings_root.update()
    check("настройки: Enter добавляет правило",
          settings.rules.get("enter.example") == "direct")
    settings._delete_rule("instagram.com")
    check("настройки: удаление правила", "instagram.com" not in settings.rules)

    update_calls = {"n": 0}
    real_blocked_update = settings_module.blocked.update

    def fake_blocked_update(progress=None):
        update_calls["n"] += 1
        return {"ok": True, "size": 1739390, "updated": time.time(), "path": "x"}

    settings_module.blocked.update = fake_blocked_update
    rule_fake.calls.pop("restart_proxy", None)
    try:
        settings._update_blocked()
        deadline = time.time() + 3
        while settings._blocked_busy and time.time() < deadline:
            settings_root.update()
            time.sleep(0.02)
        for _ in range(20):
            settings_root.update()
            time.sleep(0.01)
        check("настройки: обновление списка вызывает blocked.update",
              update_calls["n"] == 1)
        check("настройки: после обновления прокси перезапускается",
              bool(rule_fake.calls.get("restart_proxy")))
        check("настройки: статус обновления показан",
              "Список обновлён" in settings.blocked_progress.cget("text"))
    finally:
        settings_module.blocked.update = real_blocked_update

    settings.blocked_toggle.set(False)
    settings.toggles["youtube"].set(True)
    check("настройки: YouTube через VPN ждёт «Сохранить»",
          not rule_fake.cfg.get("youtube_via_vpn"))
    settings._save()
    saved_cfg = config.load_config()
    check("настройки: правила сохранены",
          saved_cfg.get("xray_routes") == dict(settings.rules))
    check("настройки: старые списки очищены при сохранении",
          saved_cfg.get("xray_direct_domains") == []
          and saved_cfg.get("xray_extra_domains") == [])
    check("настройки: тумблер списка сохранён",
          saved_cfg.get("blocked_list_enabled") is False)
    check("настройки: YouTube через VPN сохранён",
          saved_cfg.get("youtube_via_vpn") is True)
    settings._close()
    reopened = SettingsWindow(settings_root, theme.Theme(False), rule_fake)
    reopened.open()
    for _ in range(6):
        settings_root.update()
        time.sleep(0.02)
    check("настройки: повторное открытие видит сохранённое",
          reopened.rules == dict(settings.rules))
    check("настройки: повторное открытие видит выключенный список",
          reopened.blocked_toggle.get() is False)
    check("настройки: повторно видит YouTube через VPN",
          reopened.toggles["youtube"].get() is True)
    for domain in list(reopened.rules):
        reopened._delete_rule(domain)
    check("настройки: пустой список показывает подсказку",
          reopened.rules == {}
          and reopened.routes_empty.winfo_manager() != ""
          and reopened.rules_scroll.winfo_manager() == "")
    reopened.route_var.set("one.example")
    reopened.route_segment.set("direct")
    reopened._add_rule()
    check("настройки: появление правила возвращает список",
          reopened.rules.get("one.example") == "direct"
          and reopened.routes_empty.winfo_manager() == ""
          and reopened.rules_scroll.winfo_manager() != "")
    reopened._close()

    bad_fake = FakeController()
    bad_fake.cfg["xray_routes"] = ["x.com", "instagram.com"]
    bad_window = SettingsWindow(settings_root, theme.Theme(False), bad_fake)
    bad_window.open()
    for _ in range(4):
        settings_root.update()
        time.sleep(0.02)
    check("настройки: не-dict xray_routes не ломает окно",
          bad_window.rules == {} and bool(bad_window.route_entry.winfo_exists()))
    bad_window._close()

    opened_windows = []
    for _index in range(5):
        window = SettingsWindow(settings_root, theme.Theme(False), rule_fake)
        window.open()
        for _ in range(3):
            settings_root.update()
            time.sleep(0.02)
        window._close()
        opened_windows.append(window)
    check("нет глобальной привязки колеса после 5 открытий",
          settings_root.bind_all("<MouseWheel>") == "")
    check("закрытые окна настроек освобождены",
          all(window.window is None for window in opened_windows))
    check("лишних окон настроек не осталось",
          not [child for child in settings_root.winfo_children()
               if isinstance(child, tk.Toplevel)])

    busy_window = SettingsWindow(settings_root, theme.Theme(False), rule_fake)
    busy_window.open()
    settings_root.update()
    busy_window._blocked_busy = True
    busy_window._close()
    check("настройки: закрытие снимает занятость списка",
          busy_window._blocked_busy is False)
    busy_window.open()
    settings_root.update()
    check("настройки: повторное открытие сбрасывает занятость",
          busy_window._blocked_busy is False)
    busy_window._close()

    settings_module.blocked.info = real_blocked_info
    settings_root.destroy()
    del settings_root
    gc.collect()

    # --- YouTube через VPN: отложенное применение -----------------------
    print("\n-- YouTube через VPN --")
    vpn_app = TrayApp()
    vpn_app.last_check = time.time()
    vpn_app._net_checked = time.time()
    vpn_app.proxy = {**vpn_app.proxy, "running": True, "port": 10818}
    messages: list[str] = []
    vpn_app.notify = lambda message, title="bypass-tray": messages.append(message)
    vpn_app.restart_proxy = lambda: False
    vpn_app.set_youtube_via_vpn(True)
    check("YouTube VPN: занятый прокси — «применится при подключении»",
          vpn_app.cfg.get("youtube_via_vpn") is True
          and any("применится при следующем подключении" in text
                  for text in messages),
          str(messages))

    # --- битый конфиг: бэкап без перезаписи ----------------------------
    print("\n-- битый конфиг --")
    config_path = tmp / "config.json"
    config_path.write_text("{ это не json", encoding="utf-8")
    recovered = config.load_config()
    backups = list(tmp.glob("config.json.bak-*"))
    check("битый конфиг: возвращены умолчания", recovered.get("zapret_dir") == "",
          repr(recovered.get("zapret_dir")))
    check("битый конфиг: сделан бэкап", len(backups) == 1,
          backups[0].name if backups else "нет")
    check("битый конфиг: файл не перезаписан", not config_path.exists())
    # Возвращаем рабочий конфиг, чтобы временный каталог остался валидным.
    config.save_config({"zapret_dir": cfg.get("zapret_dir", ""),
                        "tgws_exe": cfg.get("tgws_exe", "")})


if __name__ == "__main__":
    raise SystemExit(main())
