"""Окно-панель «Обходы»: VPN, сервисы, обновления, журнал и поведение.

Окно живёт в собственном потоке (Tkinter требует, чтобы весь UI трогал только
один поток). Связь с приложением — через потокобезопасную очередь команд и
периодический опрос контроллера из ``after``. Все действия выполняет
контроллер в фоне, панель только просит и перерисовывается.
"""

from __future__ import annotations

import gc
import queue
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox

from PIL import ImageTk

from . import diagnose, history, icons, routes
from .secrets import redact_text
from .settings_ui import SettingsPage
from .theme import SPACE, Theme, resolve_dark
from .widgets import (
    AnimatedSection, Card, Chip, Divider, HoverRow, IconButton, PillButton,
    ScrollBody, StatusDot, Tooltip,
)

WINDOW_W = 720
POLL_MS = 1000
TICK_MS = 120
#: Тело панели не выше этой доли высоты экрана.
BODY_RATIO = 0.88
#: Высота кнопок в рядах действий: та же, что у полей ввода (36 px).
CONTROL_HEIGHT = SPACE["xl"] + SPACE["md"]
#: Высота компактной кнопки внутри строки (28–32 px по гайдлайну).
ROW_CONTROL_HEIGHT = SPACE["xl"] + SPACE["sm"]

#: Эмодзи и флаги из имён серверов: в интерфейсе их быть не должно.
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]"
)
_STATE_WORDS = {"ok": "ок", "warn": "внимание", "bad": "сбой", "off": "выкл"}
#: Вердикт старого кэша → трёхцветный уровень (совместимость).
_VERDICT_LEVELS = {"ok": "ok", "warn": "degraded", "fail": "down"}


def report_level(report: dict) -> str:
    """Уровень отчёта health; старый кэш без ``level`` — по вердикту."""
    level = str((report or {}).get("level") or "")
    if level:
        return level
    return _VERDICT_LEVELS.get(str((report or {}).get("verdict") or ""), "")

SERVICES = (
    ("zapret", "zapret · обход DPI", "zap"),
    ("youtube", "YouTube", "youtube"),
    ("discord", "Discord", "discord"),
    ("deepseek", "DeepSeek API", "spark"),
    ("tgwsproxy", "tg-ws-proxy", "telegram"),
)

#: Поля карточки: ширина текста = окно минус отступы карточки.
CARD_TEXT_WIDTH = WINDOW_W - 4 * SPACE["lg"] - SPACE["sm"]

#: Режим маршрутизации человеческим языком — та же подпись, что в настройках.
ROUTING_MODE_TEXT = {
    "blocked": "через VPN: только заблокированное",
    "all": "через VPN: всё, кроме РФ",
}

#: Подробности покрытия — в подсказке к строке: в карточке места нет.
COVERAGE_HINT = ("Свои настройки прокси, игры и Electron идут мимо VPN; "
                 "tg-ws-proxy отдельного прокси не требует")


def _plural(count: int, one: str, few: str, many: str) -> str:
    """Русская форма слова по числу: 1 сбой / 2 сбоя / 5 сбоев, но 11 сбоев."""
    if count % 10 == 1 and count % 100 != 11:
        return one
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return few
    return many


def _domain_word(count: int) -> str:
    """Склонение слова «домен» для счётчика исключений."""
    return _plural(count, "домен", "домена", "доменов")


def _incident_word(count: int) -> str:
    """Склонение слова «сбой»: 1 сбой / 2 сбоя / 5 сбоев."""
    return _plural(count, "сбой", "сбоя", "сбоев")


def _times_word(count: int) -> str:
    """Склонение слова «раз»: 1 раз / 2 раза / 5 раз."""
    return _plural(count, "раз", "раза", "раз")


#: Меньше стольких отчётов за сутки — карточка «Стабильность» не нужна:
#: по одной-двум проверкам проценты ничего не значат, а место они занимают.
MIN_STABILITY_REPORTS = 3


def _stability_numbers(data: dict) -> dict:
    """Числа за сутки из готовой сводки — один разбор на два формата текста."""
    def number(key: str) -> int:
        try:
            return int((data or {}).get(key) or 0)
        except (TypeError, ValueError):
            return 0

    # «uptime» уже посчитан, если на вход пришёл результат этой же функции:
    # так карточка и подпись могут звать её по очереди, не теряя процент.
    uptime = (data or {}).get("uptime")
    if uptime is None:
        uptime = (data or {}).get("uptime_pct")
    try:
        uptime = None if uptime is None else int(uptime)
    except (TypeError, ValueError):
        uptime = None
    return {
        "reports": number("reports"),
        "uptime": uptime,
        "incidents": number("incidents"),
        "switches": number("server_switches"),
        "heals": number("heals"),
    }


def _uptime_part(stats: dict) -> str:
    """Кусок про долю «в сети»: «в сети 83%», при неизвестном проценте — «—»."""
    uptime = stats["uptime"]
    return "в сети —" if uptime is None else f"в сети {uptime}%"


def _services_brief(count: int) -> str:
    """Сводка по не-ok пунктам: «всё в порядке» или «N требуют внимания»."""
    if count <= 0:
        return "всё в порядке"
    word = "требует" if count % 10 == 1 and count % 100 != 11 else "требуют"
    return f"{count} {word} внимания"


def coverage_text(system_proxy: bool, bypass: list[str]) -> str:
    """Одна строка про фактическое покрытие: системный прокси и исключения.

    Компактно намеренно: главная карточка обязана оставаться ниже страницы
    настроек, иначе окно при её открытии меняет высоту и дёргается. Поэтому
    «свои настройки прокси, игры, Electron» живут в подсказке, а не здесь.
    """
    if not system_proxy:
        return "Покрытие: системный прокси не применён"
    from . import xray as xray_module

    # В реестре каждое имя лежит дважды (``x.com`` и ``*.x.com``) вместе со
    # служебными записями — считать надо настоящие домены.
    count = xray_module.count_bypass_domains(list(bypass or []))
    if not count:
        return "Покрытие: системный прокси"
    return f"Покрытие: системный прокси · исключено {count} {_domain_word(count)}"


def clean_server_name(name: str) -> str:
    """Имя сервера без эмодзи и флагов — только текст."""
    text = _EMOJI_RE.sub("", str(name or ""))
    return " ".join(text.split()).strip(" ·-—")


def _style_titlebar(root: tk.Misc, theme: Theme) -> None:
    """Красит системную рамку и заголовок окна в цвет шапки (Windows 10/11).

    Тёмная шапка есть в обеих схемах, поэтому и рамка всегда тёмная — иначе
    светлая системная полоса выглядит чужой. Всё молча пропускается, если
    DWM недоступен или атрибут не поддерживается версией Windows.
    """
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes

        hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
        if not hwnd:
            return
        dwm = ctypes.windll.dwmapi

        def colorref(color: str) -> int:
            value = str(color or "").lstrip("#")
            red = int(value[0:2], 16)
            green = int(value[2:4], 16)
            blue = int(value[4:6], 16)
            return red | (green << 8) | (blue << 16)

        dark = ctypes.c_int(1)
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark))
        caption = ctypes.c_int(colorref(theme.header))
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption), ctypes.sizeof(caption))
        border = ctypes.c_int(colorref(theme.header))
        dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(border), ctypes.sizeof(border))
    except Exception:  # noqa: BLE001
        pass


def _apply_titlebar(root: tk.Misc, theme: Theme) -> None:
    """Применяет окраску рамки сразу и ещё раз, когда окно уже показано."""
    try:
        root.update_idletasks()
    except tk.TclError:
        pass
    _style_titlebar(root, theme)
    try:
        root.after(0, lambda: _style_titlebar(root, theme))
    except tk.TclError:
        pass


def format_uptime(seconds) -> str:
    """Длительность в формате 56:34 / 1:02:03."""
    try:
        start = float(seconds)
    except (TypeError, ValueError):
        return "0:00"
    if start <= 0:
        return "0:00"
    total = max(0, int(time.time() - start))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _window_hwnd(root: tk.Misc) -> int:
    """HWND рамки окна Tk; 0 — если Windows недоступен."""
    if not sys.platform.startswith("win"):
        return 0
    try:
        import ctypes

        return int(ctypes.windll.user32.GetParent(root.winfo_id())
                   or root.winfo_id() or 0)
    except Exception:  # noqa: BLE001
        return 0


def _work_area(root: tk.Misc) -> tuple[int, int, int, int]:
    """Рабочая область монитора, на котором стоит окно, без панели задач.

    ``SPI_GETWORKAREA`` отдаёт область основного монитора, поэтому на втором
    экране потолок высоты иначе считается неверно. Fallback — весь экран.
    """
    if sys.platform.startswith("win"):
        try:
            import ctypes
            from ctypes import wintypes

            class _MonitorInfo(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.DWORD),
                    ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD),
                ]

            hwnd = _window_hwnd(root)
            if hwnd:
                # MONITOR_DEFAULTTONEAREST = 2
                monitor = ctypes.windll.user32.MonitorFromWindow(hwnd, 2)
                info = _MonitorInfo()
                info.cbSize = ctypes.sizeof(_MonitorInfo)
                if monitor and ctypes.windll.user32.GetMonitorInfoW(
                        monitor, ctypes.byref(info)):
                    work = info.rcWork
                    return work.left, work.top, work.right, work.bottom
            rect = wintypes.RECT()
            # SPI_GETWORKAREA = 0x0030
            if ctypes.windll.user32.SystemParametersInfoW(
                    0x0030, 0, ctypes.byref(rect), 0):
                return rect.left, rect.top, rect.right, rect.bottom
        except Exception:  # noqa: BLE001
            pass
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


class Panel:
    def __init__(self, controller):
        self.controller = controller
        self._thread: threading.Thread | None = None
        self._root: tk.Tk | None = None
        #: команды панели (str) и итоги действий контроллера (tuple).
        self._queue: queue.Queue[object] = queue.Queue()
        self._lock = threading.Lock()
        self._theme = Theme(False)
        self._images: list = []
        self._icon_cache: dict[tuple, ImageTk.PhotoImage] = {}
        self._body: ScrollBody | None = None
        self._header: tk.Frame | None = None
        self._footer: tk.Label | None = None
        self._subtitle: tk.Label | None = None
        self._placed = False
        self._last_height = 0
        self._first_shown = None
        self._origin_x = 0
        self._origin_y = 0
        self._stage: tk.Canvas | None = None
        self._main_page: tk.Frame | None = None
        self._main_item: int | None = None
        self._settings_item: int | None = None
        self._stage_bg: int | None = None
        self._settings_page: SettingsPage | None = None
        self._settings_visible = False
        self._page_visible = "main"
        self._title: tk.Label | None = None
        #: Короткий статус рядом с заголовком: цветная точка + фраза.
        self._status_chip: Chip | None = None
        self._header_update: IconButton | None = None
        self._header_gear: IconButton | None = None
        self._header_back: IconButton | None = None
        self._header_update_icon: tuple | None = None
        self._header_update_text: str | None = None
        self._activity_period: tk.Label | None = None
        #: Текст ошибки в подвале: держится до следующего удачного действия.
        self._ui_error = ""
        #: Действия контроллера, уже запущенные из панели (защита от двойного клика).
        self._inflight: set = set()

        self._vpn: dict = {}
        self._services: dict[str, dict] = {}
        #: Карточка «Что не так»: показывается только при не-ok уровне.
        self._diagnose_card: Card | None = None
        self._diagnose_visible = False
        self._diagnose_key: tuple | None = None
        self._vpn_card: Card | None = None
        #: Карточки главной, которые прячутся и возвращаются: стабильность
        #: (нет данных) и журнал (точка возврата для pack(before=...)).
        self._services_card: Card | None = None
        self._stability_card: Card | None = None
        self._activity_card: Card | None = None
        self._stability_visible = True
        self._services_summary: tk.Label | None = None
        self._activity_since: tk.Label | None = None
        self._activity_rows: list[dict] = []
        self._activity_empty: tk.Label | None = None
        self._buttons: dict[str, PillButton] = {}
        #: Откат последнего изменения маршрутов: в шапке карточки журнала.
        self._rollback: PillButton | None = None
        self._servers_open = False
        self._servers_section: AnimatedSection | None = None
        self._servers_frame: tk.Frame | None = None
        self._servers_list: tk.Frame | None = None
        self._servers_signature: tuple | None = None

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
        if root is None or self._settings_page is None:
            return
        if self._settings_visible:
            return
        self._settings_page.reload()
        self._settings_visible = True
        self._switch_pages(show_settings=True)

    # ---- жизненный цикл ----------------------------------------------------
    def _run(self) -> None:
        """Поднимает окно в отдельном потоке.

        Tk-интерпретатор создаётся один раз: закрытие окна лишь прячет его
        (``withdraw``), а ``destroy`` выполняется только при выходе из
        приложения.
        """
        theme = Theme(resolve_dark(self.controller.cfg.get("theme", "auto")))
        self._theme = theme
        root = tk.Tk()
        # Окно не должно мелькнуть в (0,0) до _fit: строим скрытым, показываем
        # только когда геометрия, иконка и страницы уже готовы.
        root.withdraw()
        self._root = root
        try:
            self._build(root, theme)
            transparent = False
            try:
                root.attributes("-alpha", 0.0)
                transparent = True
            except tk.TclError:
                pass
            root.deiconify()
            root.lift()
            root.update_idletasks()
            # После map Canvas получает настоящую высоту — подгоняем ещё раз,
            # чтобы первая же видимая высота была окончательной.
            self._fit(root)
            root.update_idletasks()
            root.update()
            if transparent:
                try:
                    root.attributes("-alpha", 1.0)
                except tk.TclError:
                    pass
            # Диагностика первого появления: не (0,0) и не позиция по умолчанию.
            self._first_shown = (root.winfo_rootx(), root.winfo_rooty())
        except Exception:  # noqa: BLE001
            self._release()
            root.destroy()
            raise
        root.after(TICK_MS, self._pump)
        root.after(POLL_MS, self._tick)
        self._request_health()
        try:
            root.mainloop()
        finally:
            self._release()
            del root
            # Tk образует циклы ссылок; собираем их в потоке-владельце
            # интерпретатора, иначе Python 3.14 сообщит об утечке Tcl.
            gc.collect()

    def _release(self) -> None:
        """Забывает Tk-объекты в том же потоке, который их создал."""
        if self._body is not None:
            self._body.release()
        self._settings_page = None
        self._stage = None
        self._main_page = None
        self._main_item = None
        self._settings_item = None
        self._settings_visible = False
        self._page_visible = "main"
        self._root = None
        self._body = None
        self._header = None
        self._footer = None
        self._subtitle = None
        self._title = None
        self._status_chip = None
        self._header_update = None
        self._header_gear = None
        self._header_back = None
        self._header_update_icon = None
        self._header_update_text = None
        self._activity_period = None
        self._activity_since = None
        self._services_summary = None
        self._services_card = None
        self._stability_card = None
        self._activity_card = None
        self._stability_visible = True
        self._ui_error = ""
        self._inflight = set()
        self._vpn = {}
        self._services = {}
        self._activity_rows = []
        self._activity_empty = None
        self._buttons = {}
        self._servers_section = None
        self._servers_frame = None
        self._servers_list = None
        self._servers_signature = None
        self._servers_open = False
        self._images = []
        self._icon_cache = {}
        self._placed = False
        self._last_height = 0
        self._first_shown = None
        theme = getattr(self, "_theme", None)
        if theme is not None:
            theme.release()

    def _pump(self) -> None:
        root = self._root
        if root is None:
            return
        while True:
            try:
                command = self._queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(command, tuple):
                # Итог действия контроллера из фонового потока.
                try:
                    self._handle_event(command)
                except Exception as exc:  # noqa: BLE001
                    self._report_ui_error(exc)
                continue
            if command == "close":
                try:
                    root.destroy()
                except tk.TclError:
                    pass
                self._root = None
                return
            try:
                self._handle_command(command)
            except Exception as exc:  # noqa: BLE001
                # Одна сломанная команда не должна убивать цикл панели.
                self._report_ui_error(exc)
        try:
            root.after(TICK_MS, self._pump)
        except tk.TclError:
            self._root = None

    def _handle_command(self, command: str) -> None:
        root = self._root
        if root is None:
            return
        if command == "show":
            root.deiconify()
            root.lift()
            root.attributes("-topmost", True)
            root.after(300, lambda: root.attributes("-topmost", False))
            root.focus_force()
            self._request_health()
        elif command == "settings":
            self._open_settings()
        elif command == "render":
            self._render()

    def _handle_event(self, event: tuple) -> None:
        """Итог действия, запущенного кнопкой: успех, отказ или исключение."""
        kind, payload = event
        if kind == "ok":
            self._ui_error = ""
        elif kind == "routes_plan":
            # Сухой прогон вернулся из фонового потока: диалог — здесь.
            self._ask_routes(payload)
        elif kind == "failed":
            self._report_action_failed(str(payload))
        elif kind == "error":
            self._report_ui_error(payload, "действия")

    def _report_ui_error(self, exc: Exception, label: str = "интерфейса") -> None:
        """Показывает ошибку в подвале, не роняя цикл панели.

        Текст остаётся до следующего успешного действия: иначе ближайший
        ``_render_footer`` затрёт его через секунду.
        """
        self._ui_error = f"ошибка {label}: {type(exc).__name__}"
        self._show_ui_error()

    def _report_action_failed(self, name: str) -> None:
        """Действие вернуло False — контроллер работу не начал (занят/нельзя)."""
        self._ui_error = f"не выполнено: {name}"
        self._show_ui_error()

    def _show_ui_error(self) -> None:
        if self._footer is None or not self._ui_error:
            return
        try:
            self._footer.configure(text=self._ui_error)
        except tk.TclError:
            pass

    def _request_health(self) -> None:
        run_health = getattr(self.controller, "run_health", None)
        if callable(run_health):
            try:
                run_health()
            except Exception:  # noqa: BLE001
                pass

    def _tick(self) -> None:
        root = self._root
        if root is None:
            return
        try:
            self._render()
        except tk.TclError:
            # Окно уже уничтожено — перерисовывать нечего.
            self._root = None
        except Exception as exc:  # noqa: BLE001
            # Ошибка рендера не должна навсегда останавливать таймер:
            # иначе залипают подзаголовок, кнопки и список серверов.
            self._report_ui_error(exc)
        finally:
            if self._root is not None:
                try:
                    root.after(POLL_MS, self._tick)
                except tk.TclError:
                    self._root = None

    # ---- построение интерфейса --------------------------------------------
    def _icon(self, name: str, color: str, size: int = 20) -> ImageTk.PhotoImage:
        """Кэшированная иконка панели: PhotoImage не пересоздаётся каждый тик."""
        key = (name, color, size)
        image = self._icon_cache.get(key)
        if image is None:
            image = ImageTk.PhotoImage(icons.panel_icon(name, color, size))
            self._icon_cache[key] = image
            self._images.append(image)
        return image

    def _set_icon(self, label: tk.Label, name: str, color: str, size: int = 20) -> None:
        image = self._icon(name, color, size)
        if getattr(label, "_icon_key", None) != image:
            label.configure(image=image)
            label._icon_key = image  # type: ignore[attr-defined]

    def _build(self, root: tk.Tk, theme: Theme) -> None:
        self._theme = theme
        root.title("Обходы")
        root.configure(bg=theme.bg)
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self._hide)

        # .ico нужен панели задач/рамке; iconphoto остаётся для Tk.
        try:
            root.iconbitmap(default=str(icons.ensure_ico()))
        except (tk.TclError, OSError, ValueError):
            pass
        app_icon = ImageTk.PhotoImage(icons.make_app_icon(64))
        self._images.append(app_icon)
        try:
            root.iconphoto(True, app_icon)
        except tk.TclError:
            pass

        self._build_header(root, theme)

        self._stage = tk.Canvas(root, bg=theme.bg, bd=0, highlightthickness=0)
        self._stage.pack(fill="both", expand=True)
        self._stage_bg = self._stage.create_rectangle(
            0, 0, WINDOW_W, 1, fill=theme.bg, outline="")
        self._main_page = tk.Frame(self._stage, bg=theme.bg)
        self._main_item = self._stage.create_window(
            0, 0, anchor="nw", window=self._main_page, width=WINDOW_W)

        self._body = ScrollBody(self._main_page, theme)
        self._body.pack(fill="both", expand=True)

        # Верхний отступ первой карточки: карточки дают зазор только снизу.
        tk.Frame(self._body.inner, bg=theme.bg, height=SPACE["md"]).pack(fill="x")
        # Диагноз — самая верхняя карточка: она отвечает на «что не так»,
        # поэтому должна попадаться первой. Пустая не занимает высоту.
        self._build_diagnose()
        self._vpn_card = self._card_box()
        self._build_vpn(self._vpn_card.inner)
        self._services_card = self._card_box()
        self._build_services(self._services_card.inner)
        self._stability_card = self._card_box()
        self._build_stability(self._stability_card.inner)
        self._activity_card = self._card_box()
        self._build_activity(self._activity_card.inner)
        self._build_actions()

        self._footer = tk.Label(
            self._main_page, text="", bg=theme.bg, fg=theme.text_faint,
            font=theme.caption(), anchor="w", justify="left",
        )
        self._footer.pack(fill="x", padx=SPACE["lg"], pady=(0, SPACE["md"]))
        # Колесо вешаем один раз на готовое дерево главной; строки серверов
        # пересоздаются в _render_servers и привязываются там.
        self._body.bind_tree()

        self._settings_page = SettingsPage(
            self._stage, theme, self.controller, on_close=self._close_settings)
        self._settings_item = self._stage.create_window(
            0, 0, anchor="nw", window=self._settings_page,
            width=WINDOW_W)
        self._stage.itemconfigure(self._settings_item, state="hidden")
        self._stage.bind("<Configure>", self._on_stage_configure)

        self._render()
        self._fit(root)
        _apply_titlebar(root, theme)

    def _card(self) -> tk.Frame:
        return self._card_box().inner

    def _card_box(self) -> Card:
        card = Card(self._body.inner, self._theme)
        card.pack(fill="x", padx=SPACE["lg"], pady=(0, SPACE["md"]))
        return card

    # ---- карточка «Что не так» ---------------------------------------------
    def _build_diagnose(self) -> None:
        """Карточка диагноза: заголовок, одна строка фактов, кнопка действия.

        Собирается всегда, но показывается только при не-ok уровне: пустая
        карточка занимала бы высоту, а панель и так упирается в экран.
        """
        theme = self._theme
        card = self._card_box()
        inner = card.inner
        top = tk.Frame(inner, bg=theme.card)
        top.pack(fill="x")
        self._diagnose_title = tk.Label(
            top, text="", bg=theme.card, fg=theme.text,
            font=theme.title(), anchor="w")
        self._diagnose_title.pack(side="left", fill="x", expand=True)
        self._diagnose_button = PillButton(
            top, theme, "", None, kind="primary", min_width=140,
            height=ROW_CONTROL_HEIGHT)
        self._diagnose_button.pack(side="right", padx=(SPACE["md"], 0))
        self._diagnose_facts = tk.Label(
            inner, text="", bg=theme.card, fg=theme.text_dim,
            font=theme.caption(), anchor="w", justify="left",
            wraplength=CARD_TEXT_WIDTH)
        self._diagnose_facts.pack(fill="x", pady=(SPACE["xs"], 0))
        self._diagnose_card = card
        # ``_card_box`` уже упаковал карточку — значит, она считается видимой:
        # первый же рендер при ``ok`` уберёт её, а не оставит пустую полосу.
        self._diagnose_visible = True

    def _diagnose_command(self, kind: str):
        """Кнопка карточки — существующее действие контроллера, без новой логики."""
        controller = self.controller
        if kind == "check_all":
            return getattr(controller, "run_health", None)
        if kind == "vpn_on":
            return lambda: self._call(controller.set_proxy, True)
        if kind == "switch_server":
            return lambda: self._call(controller.pick_best_server)
        if kind == "zapret_start":
            return lambda: self._call(controller.start_zapret)
        if kind == "strategy_pick":
            return lambda: self._call(controller.start_sweep)
        if kind == "tgws_restart":
            return lambda: self._call(controller.restart_tgws)
        return None

    def _render_diagnose(self, summary: dict) -> None:
        card = getattr(self, "_diagnose_card", None)
        if card is None:
            return
        theme = self._theme
        data = diagnose.diagnose(
            getattr(self.controller, "health_report", {}) or {},
            getattr(self.controller, "proxy", {}) or {},
            getattr(self.controller, "cfg", {}) or {},
            summary,
        )
        level = str(data.get("level") or "ok")
        if level == "ok":
            if self._diagnose_visible:
                card.pack_forget()
                self._diagnose_visible = False
            self._diagnose_key = None
            return
        if not self._diagnose_visible:
            # Показ на прежнее место — над карточкой VPN, а не последней.
            card.pack(fill="x", padx=SPACE["lg"], pady=(0, SPACE["md"]),
                      before=self._vpn_card)
            self._diagnose_visible = True
        color = theme.bad if level == "down" else theme.warn
        facts = " · ".join(str(fact) for fact in (data.get("facts") or []))
        action = data.get("action") or {}
        kind = str(action.get("kind") or "")
        key = (str(data.get("title") or ""), facts, kind, color)
        if key == self._diagnose_key:
            return
        self._diagnose_key = key
        self._diagnose_title.configure(text=str(data.get("title") or ""), fg=color)
        self._diagnose_facts.configure(text=facts)
        button = self._diagnose_button
        command = self._diagnose_command(kind) if action else None
        if command is None:
            button.pack_forget()
            return
        button.set_text(str(action.get("label") or ""))
        button.set_command(command)
        if not button.winfo_ismapped():
            button.pack(side="right", padx=(SPACE["md"], 0))

    # ---- шапка -------------------------------------------------------------
    def _build_header(self, root: tk.Tk, theme: Theme) -> None:
        header = tk.Frame(root, bg=theme.header)
        header.pack(fill="x")
        self._header = header
        inner = tk.Frame(header, bg=theme.header)
        inner.pack(fill="x", padx=SPACE["lg"], pady=SPACE["md"])

        badge_image = ImageTk.PhotoImage(icons.make_app_icon(44))
        self._images.append(badge_image)

        badge = tk.Label(inner, image=badge_image, bg=theme.header, bd=0)
        badge.pack(side="left", padx=(0, SPACE["md"]))

        titles = tk.Frame(inner, bg=theme.header)
        titles.pack(side="left", fill="y")
        title_row = tk.Frame(titles, bg=theme.header)
        title_row.pack(anchor="w", fill="x")
        self._title = tk.Label(
            title_row, text="Обходы", bg=theme.header, fg=theme.header_text,
            font=theme.display(), anchor="w")
        self._title.pack(side="left")
        # Статус — плашка рядом с названием, а не отдельная строка: короткая
        # фраза читается сразу, подробности остаются в подзаголовке.
        self._status_chip = Chip(
            title_row, theme, "Проверяю…", theme.dim, bg=theme.header)
        self._status_chip.pack(side="left", padx=(SPACE["sm"], 0),
                               pady=(SPACE["xs"] - 1, 0))
        self._subtitle = tk.Label(
            titles, text="", bg=theme.header, fg=theme.header_dim,
            font=theme.body(), anchor="w",
        )
        self._subtitle.pack(anchor="w", pady=(SPACE["xs"], 0))

        self._header_back = IconButton(
            inner, theme, self._icon("chevron_left", theme.header_dim, 20),
            self._close_settings, tooltip="Назад", bg=theme.header, size=32,
        )
        self._header_update = IconButton(
            inner, theme, self._icon("refresh", theme.header_dim, 20),
            self._request_update, tooltip="Обновить всё", bg=theme.header, size=32,
        )
        self._header_gear = IconButton(
            inner, theme, self._icon("gear", theme.header_dim, 20),
            self._open_settings, tooltip="Настройки", bg=theme.header, size=32,
        )
        self._header_back.pack(side="right", padx=(0, SPACE["sm"]))
        self._header_update.pack(side="right", padx=(0, SPACE["sm"]))
        self._header_gear.pack(side="right", padx=(0, SPACE["sm"]))
        self._header_back.pack_forget()
        self._set_header_mode("main")
    def _set_header_mode(self, mode: str) -> None:
        theme = self._theme
        settings = mode == "settings"
        if self._title is not None:
            self._title.configure(text="Настройки" if settings else "Обходы")
        if self._subtitle is not None and settings:
            self._subtitle.configure(text="Подписка, маршруты и компоненты")
        for button, visible in ((self._header_gear, not settings),
                                (self._header_update, not settings),
                                (self._header_back, settings)):
            if button is None:
                continue
            if visible:
                button.pack(side="right", padx=(0, SPACE["sm"]))
            else:
                button.pack_forget()
        if not settings:
            self._render_header_update()

    def _request_update(self) -> None:
        if bool(getattr(self.controller, "updates_running", False)):
            return
        if bool((getattr(self.controller, "busy", {}) or {}).get("updates")):
            return
        action = getattr(self.controller, "update_everything", None)
        if not callable(action):
            action = getattr(self.controller, "check_all", None)
        if callable(action):
            self._call(action)

    def _render_header_update(self) -> None:
        theme = self._theme
        button = self._header_update
        if button is None or self._page_visible == "settings":
            return
        report = getattr(self.controller, "updates_report", {}) or {}
        running = bool(getattr(self.controller, "updates_running", False)) or bool(
            (getattr(self.controller, "busy", {}) or {}).get("updates"))
        outdated = list(report.get("outdated") or [])
        if running:
            color = theme.accent
            tooltip = "Проверяю…"
            badge = None
        else:
            # Иконка остаётся нейтральной в любом случае: о находке сообщает
            # точка в углу кнопки, а не перекраска всей иконки.
            color = theme.header_dim
            tooltip = "Обновить всё"
            badge = theme.accent if outdated else None
        # Кнопка перерисовывается каждую секунду: set_image/set_tooltip зовём
        # только при смене значения, иначе всплывающая подсказка гаснет на
        # следующем тике.
        icon_state = (color, badge)
        if icon_state != self._header_update_icon:
            self._header_update_icon = icon_state
            button.set_image(self._icon("refresh", color, 20))
            button.set_badge(badge)
        if tooltip != self._header_update_text:
            self._header_update_text = tooltip
            button.set_tooltip(tooltip)

    def _on_section_height(self) -> None:
        """Колбэк анимации списка серверов: плавно подгоняет окно."""
        if self._settings_visible:
            return
        self._fit(self._root)

    def _refresh_subtitle(self) -> None:
        """Возвращает подзаголовок главной после выхода из настроек."""
        controller = self.controller
        proxy = getattr(controller, "proxy", {}) or {}
        report = getattr(controller, "health_report", {}) or {}
        busy = getattr(controller, "busy", {}) or {}
        self._render_subtitle(
            bool(proxy.get("running")), report_level(report),
            busy, bool(getattr(controller, "health_running", False)))

    def _set_page_visible(self, show_settings: bool) -> None:
        """Показывает одну страницу Canvas, не двигая window-item."""
        stage = self._stage
        if stage is None or self._main_item is None or self._settings_item is None:
            return
        try:
            stage.itemconfigure(self._main_item,
                                state="hidden" if show_settings else "normal")
            stage.itemconfigure(self._settings_item,
                                state="normal" if show_settings else "hidden")
        except tk.TclError:
            return
        self._page_visible = "settings" if show_settings else "main"
        self._set_header_mode("settings" if show_settings else "main")
        if not show_settings:
            self._refresh_subtitle()

    def _switch_pages(self, show_settings: bool) -> None:
        """Мгновенно показывает нужную страницу и ставит её высоту."""
        root = self._root
        stage = self._stage
        if root is None or stage is None:
            return
        if self._main_item is None or self._settings_item is None:
            return
        self._set_page_visible(show_settings)
        target_height = (self._settings_target_height() if show_settings
                         else self._main_target_height())
        self._apply_height(target_height)
        stage.update_idletasks()
        root.update_idletasks()

    def _close_settings(self) -> None:
        if not self._settings_visible:
            return
        self._settings_visible = False
        self._switch_pages(show_settings=False)
        self._render()

    # -- карточка VPN --------------------------------------------------------
    def _build_vpn(self, inner: tk.Frame) -> None:
        theme = self._theme
        top = tk.Frame(inner, bg=theme.card)
        top.pack(fill="x")

        shield = tk.Label(top, bg=theme.card, bd=0)
        self._set_icon(shield, "shield", theme.accent, 22)
        shield.pack(side="left", padx=(0, SPACE["md"]), anchor="n")
        self._vpn["icon"] = shield

        titles = tk.Frame(top, bg=theme.card)
        titles.pack(side="left", fill="x", expand=True)
        title_row = tk.Frame(titles, bg=theme.card)
        title_row.pack(fill="x")
        tk.Label(title_row, text="VPN", bg=theme.card, fg=theme.text,
                 font=theme.title()).pack(side="left")
        self._vpn["chip"] = Chip(title_row, theme, "выключен", theme.dim)
        self._vpn["chip"].pack(side="left", padx=(SPACE["sm"], 0),
                               pady=(SPACE["xs"] // 2, 0))
        self._vpn["line"] = tk.Label(
            titles, text="выключен", bg=theme.card, fg=theme.text_dim,
            font=theme.body(), anchor="w",
        )
        self._vpn["line"].pack(fill="x", pady=(SPACE["xs"], 0))
        # Режим маршрутизации — отдельной строкой под сервером.
        self._vpn["mode"] = tk.Label(
            titles, text="", bg=theme.card, fg=theme.text_faint,
            font=theme.caption(), anchor="w")
        self._vpn["mode"].pack(fill="x", pady=(SPACE["xs"] // 2, 0))

        self._vpn["button"] = PillButton(
            top, theme, "Включить", lambda: self._call(self.controller.set_proxy, True),
            kind="primary", min_width=120, height=CONTROL_HEIGHT,
        )
        self._vpn["button"].pack(side="right", padx=(SPACE["md"], 0))

        Divider(inner, theme).pack(fill="x", pady=(SPACE["md"], SPACE["sm"]))

        toggle = HoverRow(inner, theme, command=self._toggle_servers)
        toggle.pack(fill="x")
        label = tk.Label(toggle, text="Регионы и серверы", bg=theme.card,
                         fg=theme.text, font=theme.title(), anchor="w")
        label.pack(side="left", fill="x", expand=True, pady=SPACE["sm"])
        chevron = tk.Label(toggle, bg=theme.card, bd=0)
        self._set_icon(chevron, "chevron", theme.text_dim, 14)
        chevron.pack(side="right", padx=(SPACE["sm"], SPACE["xs"]))
        self._vpn["toggle"] = toggle
        self._vpn["toggle_label"] = label
        self._vpn["chevron"] = chevron
        for widget in (chevron, label):
            toggle.track(widget)

        section = AnimatedSection(inner, theme)
        section.pack(fill="x")
        self._servers_section = section
        self._servers_frame = section.content
        self._servers_list = tk.Frame(self._servers_frame, bg=theme.card)
        self._servers_list.pack(fill="x")
        buttons = tk.Frame(self._servers_frame, bg=theme.card)
        buttons.pack(fill="x", pady=(SPACE["sm"], 0))
        self._vpn["pick"] = PillButton(
            buttons, theme, "Подобрать лучший",
            lambda: self._call(self.controller.pick_best_server),
            kind="secondary", height=ROW_CONTROL_HEIGHT, pad_x=SPACE["md"],
        )
        self._vpn["pick"].pack(side="left")
        self._vpn["refresh"] = PillButton(
            buttons, theme, "Обновить список",
            lambda: self._call(self.controller.load_servers, True),
            kind="secondary", height=ROW_CONTROL_HEIGHT, pad_x=SPACE["md"],
        )
        self._vpn["refresh"].pack(side="left", padx=(SPACE["sm"], 0))

        # Покрытие: тонкий разделитель на всю ширину и одна строка с иконкой.
        # Без разделителя блок читался как продолжение аккордеона выше.
        Divider(inner, theme).pack(fill="x", pady=(SPACE["sm"], SPACE["sm"]))
        coverage_row = tk.Frame(inner, bg=theme.card)
        coverage_row.pack(fill="x")
        info = tk.Label(coverage_row, text="ⓘ", bg=theme.card,
                        fg=theme.text_faint, font=theme.caption(), anchor="w")
        info.pack(side="left", padx=(0, SPACE["xs"]))
        self._vpn["coverage"] = tk.Label(
            coverage_row, text="", bg=theme.card, fg=theme.text_faint,
            font=theme.caption(), anchor="w", justify="left")
        self._vpn["coverage"].pack(side="left", fill="x", expand=True)
        # Подробности («свои настройки прокси, игры, Electron») — в подсказке.
        self._vpn["coverage_tip"] = Tooltip(
            self._vpn["coverage"], COVERAGE_HINT, theme=theme)

    # -- карточка сервисов ---------------------------------------------------
    def _build_services(self, inner: tk.Frame) -> None:
        theme = self._theme
        # Заголовок со сводкой: без него список читался безымянным. Рост —
        # ровно одна строка title (~17 px), дальше ряд кнопок не сдвигается.
        head = tk.Frame(inner, bg=theme.card)
        head.pack(fill="x")
        tk.Label(head, text="Сервисы", bg=theme.card, fg=theme.text,
                 font=theme.title(), anchor="w").pack(side="left")
        self._services_summary = tk.Label(
            head, text="", bg=theme.card, fg=theme.text_faint,
            font=theme.caption(), anchor="e")
        self._services_summary.pack(side="right")
        for index, (key, title, _icon_name) in enumerate(SERVICES):
            row = HoverRow(inner, theme)
            row.pack(fill="x")
            grid = tk.Frame(row, bg=theme.card)
            # Первая строка ближе к заголовку: сам заголовок уже даёт зазор,
            # и карточка не растёт лишними пикселями.
            grid.pack(fill="x", pady=((SPACE["xs"] // 2, SPACE["sm"])
                                      if index == 0 else SPACE["sm"]))
            grid.columnconfigure(1, weight=1)

            # Состояние сервиса читается по точке; иконки брендов набора
            # состояний дублировали бы смысл и добавляли визуальный шум.
            dot = StatusDot(grid, theme, size=SPACE["md"], bg=theme.card)
            dot.grid(row=0, column=0, rowspan=2, sticky="w",
                     padx=(0, SPACE["md"]))

            name = tk.Label(grid, text=title, bg=theme.card, fg=theme.text,
                            font=theme.body("bold"), anchor="w")
            name.grid(row=0, column=1, sticky="w")

            detail = tk.Label(grid, text="", bg=theme.card, fg=theme.text_faint,
                              font=theme.caption(), anchor="w")
            detail.grid(row=1, column=1, sticky="w")

            state = tk.Label(grid, text="—", bg=theme.card, fg=theme.dim,
                             font=theme.caption("bold"), anchor="e")
            state.grid(row=0, column=2, rowspan=2, sticky="e",
                       padx=(SPACE["md"], 0))

            button = None
            if key in ("zapret", "tgwsproxy"):
                button = PillButton(grid, theme, "Запустить", None,
                                    kind="ghost", height=ROW_CONTROL_HEIGHT,
                                    pad_x=SPACE["md"], min_width=104)
                button.grid(row=0, column=2, rowspan=2, sticky="e",
                            padx=(SPACE["md"], 0))
                button.grid_remove()

            self._services[key] = {
                "row": row, "grid": grid, "dot": dot, "name": name,
                "detail": detail, "state": state, "button": button,
            }
            for widget in (dot, name, detail, state):
                row.track(widget)
            if index < len(SERVICES) - 1:
                Divider(inner, theme).pack(fill="x")

    # -- карточка стабильности -----------------------------------------------
    def _build_stability(self, inner: tk.Frame) -> None:
        theme = self._theme
        header = tk.Frame(inner, bg=theme.card)
        header.pack(fill="x", pady=(0, SPACE["sm"]))
        tk.Label(header, text="Стабильность за сутки", bg=theme.card,
                 fg=theme.text, font=theme.title(), anchor="w"
                 ).pack(side="left")
        # Одна строка: панель упирается в высоту экрана, абзац тут лишний.
        self._stability_line = tk.Label(
            inner, text="", bg=theme.card, fg=theme.text_dim,
            font=theme.body(), anchor="w", justify="left",
            wraplength=CARD_TEXT_WIDTH)
        self._stability_line.pack(fill="x")

    def _render_stability(self, summary: dict | None = None) -> None:
        """Карточка — только при достаточном числе отчётов, иначе подпись.

        Пустая карточка на 90 px отвечала «пока нет данных» и занимала место;
        пока отчётов меньше ``MIN_STABILITY_REPORTS``, те же числа идут одной
        мелкой строкой в шапку журнала.
        """
        data = history.summary() if summary is None else summary
        stats = _stability_numbers(data)
        enough = stats["reports"] >= MIN_STABILITY_REPORTS
        card = getattr(self, "_stability_card", None)
        if card is not None:
            if enough and not self._stability_visible:
                card.pack(fill="x", padx=SPACE["lg"], pady=(0, SPACE["md"]),
                          before=self._activity_card)
                self._stability_visible = True
            elif not enough and self._stability_visible:
                card.pack_forget()
                self._stability_visible = False
        line = getattr(self, "_stability_line", None)
        if line is not None:
            line.configure(text=self._stability_text(stats))
        # Подпись в шапке журнала повторяет карточку — держим её только пока
        # карточки нет, иначе одни и те же числа читаются дважды.
        self._toggle_activity_since(not (enough and card is not None), stats)

    def _toggle_activity_since(self, visible: bool, stats: dict) -> None:
        """Подпись «за сутки: …» в шапке журнала — есть, только если нет карточки."""
        label = self._activity_since
        if label is None:
            return
        if not visible:
            if label.winfo_manager():
                label.pack_forget()
            return
        label.configure(text=self._stability_brief(stats))
        if not label.winfo_manager():
            label.pack(side="right", padx=(SPACE["md"], 0))
            # Кнопка отката могла занять правый край — подпись должна быть правее.
            rollback = self._rollback
            if rollback is not None and rollback.winfo_manager():
                rollback.pack_configure(before=label)

    def _stability_text(self, summary: dict | None = None) -> str:
        """Строка карточки: доля «в сети», сбои, смены сервера, лечение.

        Части с нулём не показываются: нет сбоев — нет куска, и так далее.
        """
        data = history.summary() if summary is None else summary
        stats = _stability_numbers(data)
        if not stats["reports"]:
            return "пока нет данных за сутки"
        parts = [_uptime_part(stats)]
        if stats["incidents"]:
            parts.append(f"{stats['incidents']} {_incident_word(stats['incidents'])}")
        if stats["switches"]:
            count = stats["switches"]
            parts.append(f"сервер менялся {count} {_times_word(count)}")
        if stats["heals"]:
            parts.append(f"автолечение {stats['heals']}")
        text = " · ".join(parts)
        if stats["reports"] < MIN_STABILITY_REPORTS:
            # По одному-двум отчётам процент ничего не значит — честнее так.
            return "мало данных · " + text
        return text

    def _stability_brief(self, summary: dict | None = None) -> str:
        """Та же сводка одной фразой — для шапки журнала."""
        data = history.summary() if summary is None else summary
        stats = _stability_numbers(data)
        if stats["reports"] < MIN_STABILITY_REPORTS:
            if not stats["reports"]:
                return "за сутки: нет данных"
            return "за сутки: мало данных"
        parts = [_uptime_part(stats)]
        if stats["incidents"]:
            parts.append(f"{stats['incidents']} {_incident_word(stats['incidents'])}")
        return "за сутки: " + " · ".join(parts)

    # -- карточка журнала ----------------------------------------------------
    def _build_activity(self, inner: tk.Frame) -> None:
        theme = self._theme
        header = tk.Frame(inner, bg=theme.card)
        header.pack(fill="x", pady=(0, SPACE["sm"]))
        tk.Label(header, text="Что виджет сделал сам", bg=theme.card,
                 fg=theme.text, font=theme.title(), anchor="w"
                 ).pack(side="left")
        self._activity_period = tk.Label(
            header, text="", bg=theme.card, fg=theme.text_faint,
            font=theme.caption(), anchor="w")
        self._activity_period.pack(side="left", padx=(SPACE["sm"], 0))
        # Сводка за сутки, пока карточка стабильности не нужна. Пакуется
        # раньше кнопки отката, поэтому при нехватке места уступает ей.
        self._activity_since = tk.Label(
            header, text="", bg=theme.card, fg=theme.text_faint,
            font=theme.caption(), anchor="e")
        self._activity_since.pack(side="right", padx=(SPACE["md"], 0))
        # Откат живёт рядом с журналом: там видно, что именно виджет сделал сам.
        # Пакуется только когда снимок есть — иначе это шум в шапке.
        self._rollback = PillButton(header, theme, "Откатить маршруты",
                                    self._confirm_rollback, kind="ghost",
                                    height=ROW_CONTROL_HEIGHT, pad_x=SPACE["md"])
        self._rollback.set_enabled(False)
        self._activity_empty = tk.Label(
            inner, text="", bg=theme.card, fg=theme.text_faint,
            font=theme.body(), anchor="w",
        )
        self._activity_empty.pack(fill="x")
        for _index in range(5):
            row = tk.Frame(inner, bg=theme.card)
            stamp = tk.Label(row, text="", bg=theme.card, fg=theme.text_faint,
                             font=theme.caption(), width=6, anchor="w")
            stamp.pack(side="left")
            text = tk.Label(row, text="", bg=theme.card, fg=theme.text_dim,
                            font=theme.body(), anchor="w", justify="left")
            text.pack(side="left", fill="x", expand=True)
            self._activity_rows.append({"frame": row, "stamp": stamp, "text": text})

    # -- кнопки действий -----------------------------------------------------
    def _build_actions(self) -> None:
        theme = self._theme
        frame = tk.Frame(self._body.inner, bg=theme.bg)
        frame.pack(fill="x", padx=SPACE["lg"], pady=(0, SPACE["md"]))

        # Один ряд равных кнопок одной высоты: primary выделяется цветом,
        # а не шириной, поэтому ряд не «прыгает» при смене состояния.
        gap = SPACE["sm"]
        width = (WINDOW_W - 2 * SPACE["lg"] - 3 * gap) // 4
        specs = (
            ("check_all", "Проверить всё", self.controller.run_health, "primary"),
            ("sweep", "Тест стратегий", self.controller.start_sweep, "secondary"),
            ("routes", "Проверить маршруты", self._confirm_routes, "secondary"),
            ("subscription", "Обновить подписку",
             self.controller.refresh_subscription, "secondary"),
        )
        for index, (key, text, command, kind) in enumerate(specs):
            # Подтверждение маршрутов показывает messagebox — только в UI-потоке.
            callback = command if key == "routes" else (lambda c=command: self._call(c))
            button = PillButton(frame, theme, text, callback, kind=kind,
                                width=width, height=CONTROL_HEIGHT)
            button.grid(row=0, column=index, sticky="ew",
                        padx=(0 if index == 0 else gap, 0))
            frame.columnconfigure(index, weight=1, uniform="actions")
            self._buttons[key] = button

    # ---- обновление данных -------------------------------------------------
    def _render(self) -> None:
        theme = self._theme
        controller = self.controller
        proxy = getattr(controller, "proxy", {}) or {}
        status = getattr(controller, "status", {}) or {}
        report = getattr(controller, "health_report", {}) or {}
        items = {item.get("id"): item for item in (report.get("items") or [])}
        busy = getattr(controller, "busy", {}) or {}
        health_running = bool(getattr(controller, "health_running", False))
        running = bool(proxy.get("running"))
        level = report_level(report)

        if self._page_visible != "settings":
            self._render_subtitle(running, level, busy, health_running)
        # Сводка за сутки читается один раз: её просят и диагноз, и стабильность.
        summary = history.summary()
        self._render_diagnose(summary)
        self._render_vpn(proxy, busy)
        self._render_services(items, status, busy)
        self._render_stability(summary)
        self._render_activity()
        self._render_header_update()
        self._render_footer(status, busy)
        self._render_buttons(busy, health_running)
        self._fit(self._root)

    def _render_subtitle(self, running: bool, level: str, busy: dict,
                         health_running: bool) -> None:
        """Короткий статус — в чип, сервер и задержка — в подзаголовок."""
        theme = self._theme
        proxy = getattr(self.controller, "proxy", {}) or {}
        name = clean_server_name(proxy.get("server") or "") or "сервер не выбран"
        latency = proxy.get("latency")
        latency_text = (f"{latency} мс"
                        if isinstance(latency, int) and latency >= 0 else "")
        server = f"VPN ВКЛЮЧЁН · {name}"
        if latency_text:
            server += f" · {latency_text}"
        if busy or health_running:
            chip_text, color, detail = "Проверяю…", theme.accent, "проверяю связь…"
        elif not running:
            chip_text, color = "VPN выключен", theme.dim
            detail = proxy.get("message") or "не подключён"
        elif level == "down":
            chip_text, color, detail = "Есть сбой", theme.bad, server
        elif level == "degraded":
            chip_text, color, detail = "Требует внимания", theme.warn, server
        elif level == "ok":
            chip_text, color, detail = "Всё работает", theme.ok, server
        else:
            chip_text, color, detail = "Нет данных", theme.dim, server
        if self._status_chip is not None:
            self._status_chip.set(chip_text, color)
        if self._subtitle is not None:
            self._subtitle.configure(text=detail)

    def _render_vpn(self, proxy: dict, busy: dict) -> None:
        theme = self._theme
        info = self._vpn
        if not info:
            return
        running = bool(proxy.get("running"))
        busy_here = bool(busy.get("proxy") or proxy.get("busy"))
        port = proxy.get("port") or self.controller.cfg.get("xray_port", 10818)
        level = report_level(getattr(self.controller, "health_report", {}) or {})

        color = theme.dim
        word = "выключен"
        if running:
            if level == "down":
                color, word = theme.bad, "сбой"
            elif level == "degraded":
                color, word = theme.warn, "внимание"
            else:
                color, word = theme.ok, "включён"
        self._set_icon(info["icon"], "shield", color, 22)
        info["chip"].set(word, color)

        name = clean_server_name(proxy.get("server") or "") or "сервер не выбран"
        if running:
            uptime = format_uptime(proxy.get("since"))
            line = f"{name} · локальный прокси · {uptime}"
            if busy_here and proxy.get("message"):
                line = str(proxy.get("message"))
        else:
            line = proxy.get("message") or "не подключён"
        info["line"].configure(text=line)

        # Режим и покрытие — из фактического состояния, а не из ожиданий:
        # ``proxy`` собирается из ``xray.status()``, ``status`` — из
        # ``status.collect``.
        cfg = getattr(self.controller, "cfg", {}) or {}
        state = getattr(self.controller, "status", {}) or {}
        mode = str(proxy.get("routing_mode") or cfg.get("routing_mode")
                   or "blocked")
        info["mode"].configure(
            text=ROUTING_MODE_TEXT.get(mode, ROUTING_MODE_TEXT["blocked"]))
        system_proxy = proxy.get("system_proxy")
        if system_proxy is None:
            system_proxy = state.get("system_proxy", False)
        bypass = proxy.get("bypass")
        if bypass is None:
            bypass = state.get("bypass")
        info["coverage"].configure(
            text=coverage_text(bool(system_proxy), list(bypass or [])))

        button = info["button"]
        button.set_text("Выключить" if running else "Включить")
        button.set_kind("primary" if not running else "secondary")
        button.set_command(lambda: self._call(self.controller.set_proxy, not running))
        button.set_enabled(not busy_here)

        self._render_servers()
        pick = info.get("pick")
        refresh_button = info.get("refresh")
        servers_busy = bool(busy.get("servers") or busy.get("proxy"))
        if pick is not None:
            pick.set_enabled(not servers_busy)
        if refresh_button is not None:
            refresh_button.set_enabled(not servers_busy)

    def _render_servers(self, force: bool = False) -> None:
        theme = self._theme
        controller = self.controller
        servers = list(getattr(controller, "servers", None) or [])
        proxy = getattr(controller, "proxy", {}) or {}
        busy = bool(proxy.get("busy")
                    or (getattr(controller, "busy", {}) or {}).get("proxy"))
        current = clean_server_name(proxy.get("server") or
                                    getattr(controller, "server_selected", "") or "")
        prepared = []
        for server in servers:
            raw = str(server.get("name") or server.get("host") or "?")
            clean = clean_server_name(raw) or raw
            display = clean if len(clean) <= 42 else clean[:41].rstrip() + "…"
            latency = server.get("latency")
            latency = latency if isinstance(latency, int) and latency >= 0 else None
            prepared.append({"raw": raw, "clean": clean, "display": display,
                             "latency": latency})
        best = min((item["latency"] for item in prepared if item["latency"] is not None),
                   default=None)
        signature = (tuple((item["display"], item["latency"]) for item in prepared),
                     current, busy)
        if not force and signature == self._servers_signature:
            return
        self._servers_signature = signature

        holder = self._servers_list
        if holder is None:
            return
        for child in holder.winfo_children():
            child.destroy()
        if not prepared:
            tk.Label(holder, text="список пуст — нажмите «Обновить список»",
                     bg=theme.card, fg=theme.text_faint, font=theme.caption(),
                     anchor="w").pack(fill="x", pady=SPACE["xs"])
            if self._body is not None:
                self._body.bind_tree(holder)
            return

        for item in prepared:
            is_best = item["latency"] is not None and item["latency"] == best
            is_current = item["clean"] == current
            marker = ("★" if is_best else "") + ("●" if is_current else "")
            # Пока прокси занят, строки не реагируют: повторный клик оборвал бы
            # текущее переключение.
            command = None if busy else (lambda raw=item["raw"]: self._select(raw))
            row = HoverRow(holder, theme, command=command)
            row.pack(fill="x")
            marker_label = tk.Label(
                row, text=marker, width=3, bg=theme.card,
                fg=theme.dim if busy else (theme.accent if is_best else theme.ok),
                font=theme.caption("bold"), anchor="w",
            )
            marker_label.pack(side="left")
            name_label = tk.Label(
                row, text=item["display"], bg=theme.card,
                fg=theme.text_faint if busy else theme.text,
                font=theme.body(), anchor="w",
            )
            name_label.pack(side="left", fill="x", expand=True,
                            pady=SPACE["xs"] // 2)
            latency_label = tk.Label(
                row, text=f"{item['latency']} мс" if item["latency"] is not None else "—",
                bg=theme.card, fg=theme.text_faint, font=theme.caption(), anchor="e",
            )
            latency_label.pack(side="right")
            for widget in (marker_label, name_label, latency_label):
                row.track(widget)
        if self._body is not None:
            self._body.bind_tree(holder)

    def _select(self, name: str) -> None:
        select = getattr(self.controller, "select_server", None)
        if callable(select):
            self._call(select, name)

    def _service_fallback(self, key: str, status: dict) -> tuple[str, str]:
        zapret = status.get("zapret") or {}
        tgws = status.get("tgwsproxy") or {}
        if key == "zapret":
            if zapret.get("running"):
                detail = " · ".join(
                    part for part in (zapret.get("strategy"), zapret.get("digest")) if part
                ) or "стратегия не определена"
                return "ok", detail
            return "bad", "остановлен" if zapret.get("dir") else "winws.exe не найден"
        if key == "tgwsproxy":
            port = tgws.get("port", 1443)
            if tgws.get("listening"):
                return "ok", f"порт {port} слушает"
            return ("bad" if tgws.get("running") else "off"), f"порт {port} закрыт"
        return "off", "нет данных"

    def _render_services(self, items: dict, status: dict, busy: dict) -> None:
        theme = self._theme
        zapret_status = status.get("zapret") or {}
        trouble = 0
        for key, _title, _icon_name in SERVICES:
            row = self._services.get(key)
            if not row:
                continue
            item = items.get(key)
            if item:
                state = str(item.get("state") or "off")
                detail = str(item.get("detail") or "")
            else:
                state, detail = self._service_fallback(key, status)
            if state != "ok":
                trouble += 1
            if key == "youtube" and (getattr(self.controller, "cfg", {}) or {}).get(
                    "youtube_via_vpn"):
                detail = (detail + " · через VPN") if detail else "через VPN"
            color = {"ok": theme.ok, "warn": theme.warn, "bad": theme.bad}.get(
                state, theme.dim)
            row["dot"].set(color)
            row["detail"].configure(text=detail)
            row["state"].configure(text=_STATE_WORDS.get(state, "—"), fg=color)

            button = row["button"]
            if button is None:
                continue
            if state == "ok":
                button.grid_remove()
                row["state"].grid()
                continue
            row["state"].grid_remove()
            button.grid()

            if key == "zapret":
                # Служба Windows ставит zapret без каталога в конфиге:
                # as_service=True означает «установлен» не хуже, чем dir.
                installed = bool(zapret_status.get("dir")
                                 or zapret_status.get("as_service"))
                restart = bool(zapret_status.get("running"))
                button.set_text("Перезапустить" if restart else "Запустить")
                action = (self.controller.restart_zapret if restart
                          else self.controller.start_zapret)
                button.set_command(lambda a=action: self._call(a))
                button.set_enabled(installed and not busy.get("zapret")
                                   and not busy.get("health"))
            else:
                tgws = status.get("tgwsproxy") or {}
                restart = bool(tgws.get("running"))
                button.set_text("Перезапустить" if restart else "Запустить")
                action = (self.controller.restart_tgws if restart
                          else self.controller.start_tgws)
                button.set_command(lambda a=action: self._call(a))
                installed = bool(self.controller.cfg.get("tgws_exe"))
                button.set_enabled(installed and not busy.get("tgws"))
        if self._services_summary is not None:
            self._services_summary.configure(text=_services_brief(trouble))

    def _activity_period_text(self) -> str:
        try:
            hours = int(float(self.controller.cfg.get("activity_hours") or 3))
        except (TypeError, ValueError):
            hours = 3
        if hours == 1:
            return "последний час"
        if 2 <= hours <= 4:
            return f"последние {hours} часа"
        return f"последние {hours} часов"

    def _render_activity(self) -> None:
        theme = self._theme
        period = self._activity_period_text()
        if self._activity_period is not None:
            self._activity_period.configure(text=period)
        rows = list(getattr(self.controller, "activity_items", None) or [])[:5]
        if self._activity_empty is not None:
            self._activity_empty.configure(text=f"{period} ничего не делал")
            if rows:
                if self._activity_empty.winfo_manager():
                    self._activity_empty.pack_forget()
            elif not self._activity_empty.winfo_manager():
                self._activity_empty.pack(fill="x")
        for index, holder in enumerate(self._activity_rows):
            if index >= len(rows):
                if holder["frame"].winfo_manager():
                    holder["frame"].pack_forget()
                continue
            entry = rows[index]
            try:
                stamp = time.strftime("%H:%M", time.localtime(float(entry.get("at") or 0)))
            except (TypeError, ValueError):
                stamp = "--:--"
            text = redact_text(str(entry.get("text") or ""))
            # Эмодзи и флаги из имён серверов в журнале тоже не показываем.
            text = " ".join(_EMOJI_RE.sub("", text).split())
            if len(text) > 92:
                text = text[:89].rstrip() + "…"
            holder["stamp"].configure(text=stamp)
            holder["text"].configure(text=text)
            if not holder["frame"].winfo_manager():
                holder["frame"].pack(fill="x", pady=SPACE["xs"])

    def _render_footer(self, status: dict, busy: dict) -> None:
        if self._footer is None:
            return
        if self._ui_error:
            # Ошибку показываем вместо обычной строки, пока действие не пройдёт.
            self._show_ui_error()
            return
        stamp = getattr(self.controller, "updated_at", 0) or 0
        clock = time.strftime("%H:%M:%S", time.localtime(stamp or time.time()))
        cfg = getattr(self.controller, "cfg", {}) or {}
        guard = "guard активен" if cfg.get("guard_enabled", True) else "guard выключен"
        admin = ("с правами администратора" if status.get("admin")
                 else "без прав администратора")
        youtube = " · YouTube через VPN" if cfg.get("youtube_via_vpn") else ""
        note = " · работаю…" if busy else ""
        self._footer.configure(
            text=f"обновлено {clock} · {guard} · {admin}{youtube}{note}")

    def _render_buttons(self, busy: dict, health_running: bool) -> None:
        primary = self._buttons.get("check_all")
        if primary is not None:
            primary.set_enabled(not health_running and not busy.get("health"))
        for key in ("sweep", "routes", "subscription"):
            button = self._buttons.get(key)
            if button is None:
                continue
            blocked = bool(busy.get(key))
            if key == "subscription":
                blocked = blocked or bool(busy.get("servers"))
            button.set_enabled(not blocked)
        if self._rollback is not None:
            # Один опрос снимка на кадр: has_snapshot() читает файлы с диска,
            # а кнопка нужна, только когда откат возможен.
            snapshot = routes.has_snapshot()
            if snapshot:
                if not self._rollback.winfo_manager():
                    # Подпись «за сутки: …» бывает скрыта, пока есть карточка —
                    # тогда откат встаёт на правый край сам.
                    anchor = self._activity_since
                    if anchor is not None and anchor.winfo_manager():
                        self._rollback.pack(side="right", before=anchor)
                    else:
                        self._rollback.pack(side="right")
                self._rollback.set_enabled(not busy.get("routes"))
            elif self._rollback.winfo_manager():
                self._rollback.pack_forget()

    # ---- компоновка и прокрутка -------------------------------------------
    def _header_height(self) -> int:
        return self._header.winfo_reqheight() if self._header is not None else 0

    def _body_limit(self) -> int:
        """Потолок высоты окна: 88 % рабочей области монитора, где стоит окно."""
        root = self._root
        if root is None:
            return 0
        _left, top, _right, bottom = _work_area(root)
        return max(300, int(max(1, bottom - top) * BODY_RATIO))

    def _main_target_height(self) -> int:
        body = self._body.content_height() if self._body is not None else 0
        footer = self._footer.winfo_reqheight() if self._footer is not None else 0
        total = self._header_height() + body + footer + SPACE["md"]
        limit = self._body_limit()
        return min(total, limit) if limit else total

    def _settings_target_height(self) -> int:
        page = self._settings_page
        root = self._root
        if page is None or root is None:
            return self._main_target_height()
        max_page = max(320, self._body_limit() - self._header_height()
                       - SPACE["sm"])
        return self._header_height() + min(max(320, page.height_request()), max_page)

    def _sync_stage_size(self) -> None:
        """Растягивает обе страницы на видимую область Canvas."""
        stage = self._stage
        if stage is None:
            return
        try:
            width = stage.winfo_width()
            height = stage.winfo_height()
        except tk.TclError:
            return
        if height <= 1:
            return
        if self._stage_bg is not None:
            try:
                stage.coords(self._stage_bg, 0, 0, width or WINDOW_W, height)
                stage.tag_lower(self._stage_bg)
            except tk.TclError:
                pass
        for item in (self._main_item, self._settings_item):
            if item is None:
                continue
            try:
                stage.itemconfigure(item, width=max(1, width or WINDOW_W),
                                    height=height)
            except tk.TclError:
                pass

    def _on_stage_configure(self, _event=None) -> None:
        self._sync_stage_size()

    def _remember_origin(self) -> None:
        """Запоминает живое положение окна: его мог перетащить пользователь."""
        root = self._root
        if root is None:
            return
        try:
            if not root.winfo_ismapped():
                return
            self._origin_x = int(root.winfo_x())
            self._origin_y = int(root.winfo_y())
        except tk.TclError:
            return

    def _height_limit(self) -> int:
        """Максимальная client-высота ниже текущего верха окна."""
        root = self._root
        if root is None:
            return 300
        try:
            y = int(root.winfo_y()) if root.winfo_ismapped() else self._origin_y
            title_h = max(0, root.winfo_rooty() - root.winfo_y())
        except tk.TclError:
            y, title_h = self._origin_y, 0
        work_bottom = _work_area(root)[3]
        return max(300, work_bottom - y - title_h - SPACE["lg"])

    def _apply_height(self, height: int) -> None:
        """Меняет высоту окна, не трогая его положение.

        ``geometry`` с ``+x+y`` вызывается только при первом показе: иначе
        каждую секунду окно возвращалось бы на сохранённое место и не давало
        себя перетащить (а на втором мониторе телепортировалось на первый).
        """
        root = self._root
        if root is None:
            return
        total = max(300, int(height))
        if not self._placed:
            screen = root.winfo_screenheight()
            self._origin_x = max(0, int(root.winfo_screenwidth() / 2 - WINDOW_W / 2))
            self._origin_y = max(0, int(screen / 2 - total / 2))
            self._placed = True
            # Окно ещё не показано — позицию задаём вместе с размером.
            total = min(total, self._height_limit())
            root.geometry(f"{WINDOW_W}x{total}+{self._origin_x}+{self._origin_y}")
            self._last_height = total
        else:
            self._remember_origin()
            total = min(total, self._height_limit())
            if total == self._last_height:
                return
            # Только размер: без +x+y оконный менеджер место не меняет.
            root.geometry(f"{WINDOW_W}x{total}")
            self._last_height = total
        try:
            root.update_idletasks()
        except tk.TclError:
            return
        self._sync_stage_size()

    def _fit(self, root: tk.Tk | None) -> None:
        """Подгоняет высоту окна под текущую страницу, не центрируя заново."""
        if root is None:
            return
        root.update_idletasks()
        target = (self._settings_target_height() if self._settings_visible
                  else self._main_target_height())
        self._apply_height(target)

    def _toggle_servers(self) -> None:
        section = self._servers_section
        if section is None:
            return
        self._servers_open = not self._servers_open
        chevron = self._vpn.get("chevron") if self._vpn else None
        if chevron is not None:
            self._set_icon(
                chevron,
                "chevron_down" if self._servers_open else "chevron",
                self._theme.text_dim, 14,
            )
        if self._servers_open:
            # Строки уже построены рендером; пересоздавать без нужды не надо.
            self._render_servers()
            section.expand(self._on_section_height)
        else:
            section.collapse(self._on_section_height)

    # ---- действия ----------------------------------------------------------
    def _confirm_routes(self) -> None:
        """Спрашивает подтверждение и применяет маршруты с перезапуском Xray.

        Сначала сухой прогон в фоне: в диалоге видно, что именно изменится.
        Без изменений перезапуск не предлагается вовсе.
        """
        plan_fn = getattr(self.controller, "plan_routes", None)
        if not callable(plan_fn):
            # Контроллер без сухого прогона: прежний вопрос про перезапуск.
            self._ask_routes(None)
            return
        self._call_plan(plan_fn)

    def _call_plan(self, plan_fn) -> None:
        """Сухой прогон в фоне; диалог покажет UI-поток по готовности."""
        key = (plan_fn, "plan")
        if key in self._inflight:
            return
        self._inflight.add(key)

        def run() -> None:
            try:
                plan = plan_fn()
            except Exception as exc:  # noqa: BLE001
                self._queue.put(("error", exc))
            else:
                self._queue.put(("routes_plan", plan))
            finally:
                self._inflight.discard(key)
            self._queue.put("render")

        threading.Thread(target=run, daemon=True).start()

    def _ask_routes(self, plan) -> None:
        """Подтверждение применения: резюме прогона вместо глухого вопроса."""
        summary = str((plan or {}).get("summary") or "") if isinstance(plan, dict) else ""
        if summary and not (plan or {}).get("changes"):
            try:
                messagebox.showinfo("Проверить маршруты", summary,
                                    parent=self._root)
            except tk.TclError:
                pass
            return
        question = ("Туннель перезапустится на несколько секунд. Продолжить?"
                    if not summary else
                    f"{summary}.\n\nТуннель перезапустится на несколько "
                    "секунд. Продолжить?")
        try:
            answer = messagebox.askyesno("Проверить маршруты", question,
                                         parent=self._root)
        except tk.TclError:
            return
        if not answer:
            return
        apply_now = getattr(self.controller, "apply_routes", None)
        if callable(apply_now):
            self._call(apply_now)

    def _confirm_rollback(self) -> None:
        """Откат последнего изменения маршрутов: подтверждение и контроллер."""
        try:
            answer = messagebox.askyesno(
                "Откатить маршруты",
                "Вернуть маршруты к состоянию до последнего изменения? "
                "Туннель может перезапуститься на несколько секунд.",
                parent=self._root,
            )
        except tk.TclError:
            return
        if not answer:
            return
        rollback = getattr(self.controller, "rollback_routes", None)
        if callable(rollback):
            self._call(rollback)

    def _call(self, fn, *args) -> None:
        """Просит контроллер выполнить действие; панель не блокируется.

        Перерисовку и итог просим через очередь команд: трогать Tk из фонового
        потока нельзя. Падение действия больше не проглатывается — ошибка
        уходит в подвал, а ``False`` от контроллера значит «не выполнено».
        """
        key = (fn, args)
        # Нажатие помечаем сразу в UI-потоке: второй клик до ответа
        # контроллера не должен запустить то же действие ещё раз.
        if key in self._inflight:
            return
        self._inflight.add(key)
        label = str(getattr(fn, "__name__", "") or "действие")

        def run() -> None:
            note: tuple = ("ok", None)
            try:
                if fn(*args) is False:
                    note = ("failed", label)
            except Exception as exc:  # noqa: BLE001
                note = ("error", exc)
            finally:
                self._inflight.discard(key)
            self._queue.put(note)
            self._queue.put("render")

        threading.Thread(target=run, daemon=True).start()

    def _hide(self) -> None:
        if self._root is not None:
            try:
                self._root.withdraw()
            except tk.TclError:
                pass
