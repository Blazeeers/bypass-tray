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
from pathlib import Path
from tkinter import filedialog

from . import activity, blocked, components, config, presets, routes, xray
from .theme import SPACE, Theme
from .widgets import (
    Card, Divider, Dropdown, HoverRow, PillButton, ScrollBody, Section,
    Segmented, TextField, ToggleSwitch, Tooltip, blend, rounded_rect,
)

PORT_MIN = 1024
PORT_MAX = 65535

#: Человеческие имена режимов маршрутизации — подписи и строка журнала.
ROUTING_MODE_NAMES = {
    "blocked": "только заблокированное",
    "all": "всё, кроме РФ",
}

#: Высота элементов управления в настройках: та же, что у полей (36 px).
CONTROL_HEIGHT = SPACE["xl"] + SPACE["md"]
#: Высота компактного элемента в строке правила (28–32 px).
ROW_CONTROL_HEIGHT = SPACE["xl"] + SPACE["sm"]


def _text_value(cfg: dict, key: str) -> str:
    """Строковое поле конфига: ``None`` и не-строки читаются как пустая строка."""
    value = cfg.get(key)
    return value if isinstance(value, str) else ""


def _links_text(cfg: dict) -> str:
    """Свои конфиги для многострочного поля: по одной ссылке в строке.

    Строка из конфига (склеенные ссылки или base64) разбирается тем же
    ``links_from_text``, что и ввод: в поле видно ровно то, что пойдёт в дело.
    """
    value = cfg.get("xray_links")
    if isinstance(value, str):
        return "\n".join(xray.links_from_text(value))
    if isinstance(value, (list, tuple)):
        return "\n".join(item for item in value if isinstance(item, str))
    return ""


def _port_text(cfg: dict) -> str:
    """Порт для поля ввода.

    ``None``/bool не должны показываться как «None»: подставляем порт по
    умолчанию. Кривое, но заданное значение показываем как есть — пусть
    пользователь увидит его и исправит, а «Сохранить» объяснит ошибку.
    """
    raw = cfg.get("xray_port")
    if raw is None or isinstance(raw, bool):
        return str(xray.DEFAULT_PORT)
    text = str(raw).strip()
    return text or str(xray.DEFAULT_PORT)


def _parse_port(text: str) -> tuple[int | None, str]:
    """Порт из поля ввода: ``(port, "")`` или ``(None, причина)``."""
    raw = str(text or "").strip()
    if not raw:
        return xray.DEFAULT_PORT, ""
    try:
        value = int(raw)
    except ValueError:
        return None, f"Порт — число от {PORT_MIN} до {PORT_MAX}"
    if not PORT_MIN <= value <= PORT_MAX:
        return None, f"Порт должен быть от {PORT_MIN} до {PORT_MAX}"
    return value, ""


def _merge_legacy_routes(cfg: dict) -> None:
    """Переносит старые списки доменов в ``xray_routes``, затем чистит их.

    Раньше сохранение обнуляло ``xray_direct_domains``/``xray_extra_domains``
    безусловно — введённое вручную пропадало. Теперь значения становятся
    явными правилами (правила из формы главнее), только после этого списки
    пустеют.
    """
    raw_mapping = cfg.get("xray_routes")
    mapping = dict(raw_mapping) if isinstance(raw_mapping, dict) else {}
    for key, route in (("xray_direct_domains", "direct"),
                       ("xray_extra_domains", "proxy")):
        values = cfg.get(key)
        if isinstance(values, (list, tuple)):
            for raw in values:
                name = routes.normalize_user_domain(raw)
                if name:
                    mapping.setdefault(name, route)
        cfg[key] = []
    cfg["xray_routes"] = mapping


BEHAVIOR_TIPS = {
    "autostart": "Запускать «Обходы» при входе в Windows и подключать VPN, "
                 "если включено автоподключение.",
    "quiet": "Не показывать всплывающие уведомления, пока всё в порядке. "
             "Иконка всегда отражает настоящий статус — серая только когда "
             "VPN выключен.",
    "guard": "Раз в 10 минут проверять сервисы и лечить: поднимать "
             "tg-ws-proxy, менять сервер, обновлять маршруты.",
    "youtube": "Вести YouTube и Discord через VPN, если видео застревает. "
               "Скорость зависит от сервера.",
}

#: Короткие подписи под тумблерами: полный текст — в подсказке.
BEHAVIOR_HINTS = {
    "autostart": "Запуск при входе в Windows",
    "quiet": "Без всплывающих уведомлений",
    "guard": "Проверка сервисов раз в 10 минут",
    "youtube": "Трафик YouTube и Discord через сервер",
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
        #: Тумблеры полей VPN: держим ссылки, чтобы reload() их догонял.
        self.switches: dict[str, ToggleSwitch] = {}
        self.sections: dict[str, Section] = {}
        self.rules: dict[str, str] = {}
        self.vars: dict[str, tk.Variable] = {}
        self.entries: dict[str, TextField] = {}
        #: Правила, которые на момент последней синхронизации лежали в конфиге:
        #: по ним видно, есть ли несохранённые правки маршрутов.
        self._saved_rules: dict[str, str] | None = None
        #: Сервер правила («домен → свой сервер»). Лежит отдельно от ``rules``:
        #: тот остаётся ``{домен: маршрут}``, как его ждут окно и тесты.
        self.rule_servers: dict[str, str | None] = {}
        self._saved_rule_servers: dict[str, str | None] = {}
        self._blocked_busy = False
        self._blocked_result = None
        self._blocked_text = ""
        self._blocked_run = 0
        self.tooltips: list[Tooltip] = []
        self._build()

    # ---- сборка ------------------------------------------------------------
    def _section(self, parent: tk.Misc, title: str) -> tk.Frame:
        """Карточка с раскрывающейся секцией; содержимое кладётся в тело."""
        theme = self.theme
        card = Card(parent, theme)
        card.pack(fill="x", pady=(0, SPACE["md"]))
        section = Section(card.inner, theme, title, bg=theme.card,
                          start_open=True)
        section.pack(fill="x")
        self.sections[title] = section
        body = tk.Frame(section.body, bg=theme.card)
        body.pack(fill="x", padx=SPACE["xs"], pady=(0, SPACE["md"]))
        return body

    def _label(self, parent: tk.Misc, text: str, caption: bool = False) -> None:
        theme = self.theme
        tk.Label(parent, text=text, bg=theme.card,
                 fg=theme.text_faint if caption else theme.text_dim,
                 font=theme.caption() if caption else theme.body(),
                 anchor="w", justify="left",
                 wraplength=self.WIDTH - 120).pack(fill="x", pady=(0, SPACE["xs"]))

    def _entry(self, parent: tk.Misc, key: str, placeholder: str = "",
               width: int | None = None) -> TextField:
        theme = self.theme
        field = TextField(parent, theme, self.vars[key],
                          placeholder=placeholder, bg=theme.card,
                          width=width or 200)
        if width is None:
            field.pack(fill="x", pady=(0, SPACE["md"]))
        else:
            field.pack(side="left")
        self.entries[key] = field
        return field

    def _switch_row(self, parent: tk.Misc, key: str, title: str,
                    hint: str) -> ToggleSwitch:
        """Строка «подпись + тумблер» с серым описанием под ней."""
        theme = self.theme
        row = tk.Frame(parent, bg=theme.card)
        row.pack(fill="x", pady=(0, SPACE["md"]))
        row.columnconfigure(0, weight=1)
        name = tk.Label(row, text=title, bg=theme.card, fg=theme.text,
                        font=theme.body(), anchor="w")
        name.grid(row=0, column=0, sticky="w")
        switch = ToggleSwitch(
            row, theme, bool(self.vars[key].get()),
            command=lambda value, k=key: self.vars[k].set(bool(value)))
        switch.grid(row=0, column=1, sticky="e", padx=(SPACE["md"], 0))
        self.switches[key] = switch
        tk.Label(row, text=hint, bg=theme.card, fg=theme.text_faint,
                 font=theme.caption(), anchor="w", justify="left",
                 wraplength=self.WIDTH - 160).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(SPACE["xs"], 0))
        return switch

    def _mark_entry_error(self, key: str, error: bool) -> None:
        """Подсветить поле с неверным значением (или снять подсветку)."""
        entry = self.entries.get(key)
        if entry is not None:
            entry.set_error(error)

    # ---- свои конфиги -----------------------------------------------------
    def _links_value(self) -> str:
        """Текст поля своих конфигов."""
        try:
            return self.links_text.get("1.0", "end-1c")
        except tk.TclError:
            return ""

    def _set_links(self, text: str) -> None:
        """Заменить содержимое поля ссылками, по одной в строке."""
        try:
            self.links_text.delete("1.0", "end")
            self.links_text.insert("1.0", text)
        except tk.TclError:
            return
        self._update_links_status()

    def _update_links_status(self) -> None:
        """Счётчик распознанных ссылок под полем.

        Непустой текст без единой ссылки — красным: сохранить его нельзя,
        и пользователь должен увидеть это до нажатия «Сохранить».
        """
        text = self._links_value()
        if not text.strip():
            self.links_status.configure(text="", fg=self.theme.text_faint)
            return
        count = len(xray.links_from_text(text))
        color = self.theme.text_faint if count else self.theme.bad
        self.links_status.configure(text=f"Распознано: {count}", fg=color)

    def _append_links(self, text: str) -> None:
        """Дописать найденные ссылки к введённым, без повторов."""
        merged = xray.links_from_text("\n".join((self._links_value(), text)))
        self._set_links("\n".join(merged))

    def _links_from_file(self) -> None:
        """Импорт своих конфигов из файла: текст, base64 или JSON-список."""
        path = filedialog.askopenfilename(
            parent=self.winfo_toplevel(), title="Свои конфиги",
            filetypes=(("Ссылки и подписки", "*.txt *.conf *.json *.base64"),
                       ("Все файлы", "*.*")))
        if not path:
            return
        try:
            raw = None
            # UTF-8 (с BOM и без), затем UTF-16 из «Блокнота», затем ANSI:
            # подписку сохраняют из чего угодно, а терять импорт из-за
            # кодировки нельзя.
            for encoding in ("utf-8-sig", "utf-16", "latin-1"):
                try:
                    raw = Path(path).read_text(encoding=encoding)
                    break
                except UnicodeError:
                    continue
            if raw is None:
                self.links_status.configure(text="Не прочитать файл",
                                            fg=self.theme.bad)
                return
        except OSError:
            self.links_status.configure(text="Не прочитать файл",
                                        fg=self.theme.bad)
            return
        if not xray.links_from_text(raw):
            self.links_status.configure(text="Распознано: 0", fg=self.theme.bad)
            return
        self._append_links(raw)

    def _links_from_clipboard(self) -> None:
        """Вставка своих конфигов из буфера обмена."""
        try:
            raw = self.winfo_toplevel().clipboard_get()
        except tk.TclError:
            self.links_status.configure(text="В буфере нет текста",
                                        fg=self.theme.warn)
            return
        if not xray.links_from_text(raw):
            self.links_status.configure(text="Распознано: 0", fg=self.theme.bad)
            return
        self._append_links(raw)

    def _clear_links(self) -> None:
        self._set_links("")

    def _scroll_area(self, parent: tk.Misc, bg: str, min_height: int,
                     expand: bool = False) -> ScrollBody:
        """ScrollBody с тонким ползунком, нарисованным на своём Canvas.

        Полоса ttk в этом стиле просит 1 px ширины (ширину в clam задают
        только стрелки), поэтому ползунок рисуем сами: виден лишь когда
        содержимое не влезает.
        """
        theme = self.theme
        rail = tk.Canvas(parent, width=SPACE["sm"], bg=bg,
                         highlightthickness=0, bd=0, takefocus=0)
        # Полосу пакуем первой: тогда она получает свои пиксели, а не 1 px
        # остатка после растянутого canvas.
        rail.pack(side="right", fill="y")
        body = ScrollBody(parent, theme, bg=bg, min_height=min_height,
                          scrollbar=False)
        if expand:
            body.pack(side="left", fill="both", expand=True)
        else:
            body.pack(side="top", fill="x")
        state = {"first": 0.0, "last": 1.0}

        def redraw(_event=None) -> None:
            self._draw_rail(rail, bg, state)

        def on_scroll(first, last) -> None:
            state["first"], state["last"] = float(first), float(last)
            redraw()

        body.canvas.configure(yscrollcommand=on_scroll)
        body.canvas.bind("<Configure>", redraw, add="+")
        body.inner.bind("<Configure>", redraw, add="+")
        rail.bind("<Configure>", redraw)
        redraw()
        return body

    def _draw_rail(self, rail: tk.Canvas, bg: str, state: dict) -> None:
        theme = self.theme
        try:
            rail.delete("all")
            height = rail.winfo_height()
            width = rail.winfo_width()
        except tk.TclError:
            return
        if height <= 4 or width <= 1:
            return
        first, last = state["first"], state["last"]
        if last - first >= 0.999:
            return
        top = int(first * height)
        bottom = int(last * height)
        min_size = SPACE["lg"] + SPACE["xs"]
        if bottom - top < min_size:
            center = (top + bottom) / 2
            top = int(max(0, min(height - min_size, center - min_size / 2)))
            bottom = top + min_size
        inset = 1
        rounded_rect(
            rail, inset, top + inset, width - inset, bottom - inset,
            (width - 2 * inset) / 2,
            fill=blend(theme.text, bg, 0.30 if not theme.dark else 0.34),
            outline="",
        )

    def _build(self) -> None:
        theme = self.theme

        # Подвал — полоса цвета карточки: ghost-«Закрыть» не рисует на фоне
        # лишний белый прямоугольник, primary остаётся единственным акцентом.
        self._actions = tk.Frame(self, bg=theme.card)
        self._actions.pack(side="bottom", fill="x")
        Divider(self._actions, theme).pack(fill="x")
        action_row = tk.Frame(self._actions, bg=theme.card)
        action_row.pack(fill="x", padx=SPACE["lg"], pady=SPACE["md"])
        PillButton(action_row, theme, "Сохранить", self._save,
                   kind="primary", height=CONTROL_HEIGHT).pack(side="left")
        self.close_button = PillButton(
            action_row, theme, "Закрыть", self._close, kind="ghost",
            height=CONTROL_HEIGHT)
        self.close_button.pack(side="left", padx=(SPACE["sm"], 0))

        self.body_host = tk.Frame(self, bg=theme.bg)
        self.body_host.pack(fill="both", expand=True)
        self.body = self._scroll_area(self.body_host, theme.bg, 120,
                                      expand=True)
        pad = tk.Frame(self.body.inner, bg=theme.bg)
        pad.pack(fill="both", expand=True, padx=SPACE["md"], pady=SPACE["lg"])

        cfg = dict(self.controller.cfg or {})
        self.vars = {
            "xray_subscription": tk.StringVar(
                value=_text_value(cfg, "xray_subscription")),
            "xray_port": tk.StringVar(value=_port_text(cfg)),
            "xray_system_proxy": tk.BooleanVar(value=bool(cfg.get("xray_system_proxy", True))),
            "xray_stream_fallback": tk.BooleanVar(
                value=bool(cfg.get("xray_stream_fallback", True))),
            "zapret_dir": tk.StringVar(value=_text_value(cfg, "zapret_dir")),
            "tgws_exe": tk.StringVar(value=_text_value(cfg, "tgws_exe")),
        }

        # --- сценарии (до «Поведения»: это первое, что ищут в настройках) ---
        scenarios = self._section(pad, "Сценарии")
        self._label(
            scenarios,
            "Готовый набор режима и тумблеров. Применяется к форме сразу, а в "
            "файл попадает после «Сохранить». Свои маршруты и выбранные "
            "серверы сценарий не трогает.", caption=True)
        chips = tk.Frame(scenarios, bg=theme.card)
        chips.pack(fill="x", pady=(0, SPACE["xs"]))
        self.preset_buttons: dict[str, PillButton] = {}
        chip_pad = SPACE["sm"]
        for preset in presets.PRESETS:
            text = str(preset["title"])
            # Ширина по жирному начертанию: активный чип рисуется primary,
            # и обычная ширина обрезала бы его текст.
            width = theme.body("bold").measure(text) + 2 * chip_pad
            button = PillButton(
                chips, theme, text,
                lambda pid=preset["id"]: self._apply_preset(pid),
                kind="secondary", height=CONTROL_HEIGHT, pad_x=chip_pad,
                width=width)
            button.pack(side="left", padx=(0, chip_pad))
            self.tooltips.append(Tooltip(button, str(preset["hint"])))
            self.preset_buttons[preset["id"]] = button
        self.preset_status = tk.Label(
            scenarios, text="", bg=theme.card, fg=theme.text_faint,
            font=theme.caption(), anchor="w", justify="left",
            wraplength=self.WIDTH - 120)
        self.preset_status.pack(fill="x")

        # --- поведение (три тумблера применяются сразу, YouTube — по «Сохранить»)
        behavior = self._section(pad, "Поведение")
        is_autostart = getattr(self.controller, "is_autostart", None)
        try:
            autostart_now = bool(is_autostart()) if callable(is_autostart) else bool(
                cfg.get("autoconnect"))
        except Exception:  # noqa: BLE001
            autostart_now = bool(cfg.get("autoconnect"))
        rows = (
            ("autostart", "Подключаться при входе", autostart_now, self._set_autostart),
            ("quiet", "Без уведомлений", bool(cfg.get("quiet_icon", True)), self._set_quiet),
            ("guard", "Автолечение", bool(cfg.get("guard_enabled", True)), self._set_guard),
            ("youtube", "YouTube через VPN",
             bool(cfg.get("youtube_via_vpn", False)), self._set_youtube),
        )
        for index, (key, title, value, setter) in enumerate(rows):
            if index:
                Divider(behavior, theme).pack(fill="x", pady=SPACE["sm"])
            row = tk.Frame(behavior, bg=theme.card)
            row.pack(fill="x")
            row.columnconfigure(0, weight=1)
            name = tk.Label(row, text=title, bg=theme.card, fg=theme.text,
                            font=theme.body(), anchor="w")
            name.grid(row=0, column=0, sticky="w")
            tip = BEHAVIOR_TIPS[key]
            self.tooltips.append(Tooltip(name, tip))
            switch = ToggleSwitch(row, theme, value, command=setter)
            switch.grid(row=0, column=1, sticky="e", padx=(SPACE["md"], 0))
            self.tooltips.append(Tooltip(switch, tip))
            self.toggles[key] = switch
            tk.Label(row, text=BEHAVIOR_HINTS[key], bg=theme.card,
                     fg=theme.text_faint, font=theme.caption(), anchor="w",
                     justify="left", wraplength=self.WIDTH - 160).grid(
                row=1, column=0, columnspan=2, sticky="w", pady=(SPACE["xs"], 0))
        self._label(
            behavior,
            "«YouTube через VPN» применится после «Сохранить»: если видео "
            "застревает, трафик YouTube и Discord пойдёт через сервер.",
            caption=True)

        # --- VPN ---
        vpn = self._section(pad, "VPN и подписка")
        self._label(vpn, "Ссылка на подписку (её выдаёт провайдер). Хранится "
                         "только у вас в config.json.", caption=True)
        self._entry(vpn, "xray_subscription", placeholder="https://…")

        self._label(vpn, "Свои конфиги: ссылки vless/vmess/trojan/ss или "
                         "base64-подписка")
        links_box = tk.Frame(vpn, bg=theme.card)
        links_box.pack(fill="x", pady=(0, SPACE["sm"]))
        self.links_text = tk.Text(
            links_box, height=5, wrap="char", bd=0, highlightthickness=1,
            bg=theme.card, fg=theme.text, insertbackground=theme.text,
            selectbackground=theme.accent_soft, selectforeground=theme.text,
            highlightbackground=theme.card_border, highlightcolor=theme.focus,
            font=theme.body(), padx=SPACE["sm"], pady=SPACE["sm"])
        self.links_text.pack(fill="x")
        self.links_text.insert("1.0", _links_text(cfg))
        self.links_text.bind("<KeyRelease>",
                             lambda _event: self._update_links_status())
        self.links_status = tk.Label(
            vpn, text="", bg=theme.card, fg=theme.text_faint,
            font=theme.caption(), anchor="w", justify="left",
            wraplength=self.WIDTH - 120)
        self.links_status.pack(fill="x", pady=(0, SPACE["sm"]))
        links_actions = tk.Frame(vpn, bg=theme.card)
        links_actions.pack(fill="x", pady=(0, SPACE["md"]))
        for text, command in (("Из файла…", self._links_from_file),
                              ("Из буфера", self._links_from_clipboard),
                              ("Очистить", self._clear_links)):
            PillButton(links_actions, theme, text, command,
                       kind="secondary", height=CONTROL_HEIGHT).pack(
                side="left", padx=(0, SPACE["sm"]))
        self._update_links_status()

        self._label(vpn, "Порт локального прокси")
        port_row = tk.Frame(vpn, bg=theme.card)
        port_row.pack(fill="x", pady=(0, SPACE["md"]))
        self._entry(port_row, "xray_port", width=120)
        tk.Label(port_row, text=f"от {PORT_MIN} до {PORT_MAX}; браузеры "
                               "подключаются к нему",
                 bg=theme.card, fg=theme.text_faint, font=theme.caption(),
                 anchor="w", justify="left").pack(
            side="left", padx=(SPACE["sm"], 0), pady=SPACE["sm"])

        self._label(vpn, "Режим")
        mode_row = tk.Frame(vpn, bg=theme.card)
        mode_row.pack(fill="x", pady=(0, SPACE["xs"]))
        self.routing_segment = Segmented(
            mode_row, theme,
            [("blocked", "Только заблокированное"), ("all", "Всё, кроме РФ")],
            config.normalise_routing_mode(cfg.get("routing_mode")),
            command=lambda _value: self._refresh_preset_chips(),
            bg=theme.card, height=CONTROL_HEIGHT, width=340)
        self.routing_segment.pack(side="left")
        self._label(
            vpn,
            "Только заблокированное — через VPN идут заблокированные сайты. "
            "Всё, кроме РФ — через VPN идёт весь трафик, кроме РФ и локальной "
            "сети.", caption=True)

        for key, text, hint in (
            ("xray_system_proxy", "Прописывать прокси в систему",
             "Браузеры сразу начнут им пользоваться"),
            ("xray_stream_fallback", "Уводить YouTube/Discord через VPN",
             "Когда zapret не справляется"),
        ):
            self._switch_row(vpn, key, text, hint)

        # --- список заблокированных ---
        blocked_section = self._section(pad, "Список заблокированных сайтов")
        blocked_row = tk.Frame(blocked_section, bg=theme.card)
        blocked_row.pack(fill="x")
        blocked_row.columnconfigure(0, weight=1)
        tk.Label(blocked_row, text="Использовать список Re:filter",
                 bg=theme.card, fg=theme.text, font=theme.body(),
                 anchor="w").grid(row=0, column=0, sticky="w")
        self.blocked_toggle = ToggleSwitch(
            blocked_row, theme, bool(cfg.get("blocked_list_enabled", True)),
            command=lambda _value: self._refresh_preset_chips())
        self.blocked_toggle.grid(row=0, column=1, sticky="e",
                                 padx=(SPACE["md"], 0))
        self._label(blocked_section,
                    "≈81 тыс. доменов · MIT · заблокированные идут через VPN",
                    caption=True)
        self.blocked_status = tk.Label(
            blocked_section, text="", bg=theme.card, fg=theme.text_dim,
            font=theme.caption(), anchor="w", justify="left",
            wraplength=self.WIDTH - 160)
        self.blocked_status.pack(fill="x", pady=(0, SPACE["sm"]))
        blocked_actions = tk.Frame(blocked_section, bg=theme.card)
        blocked_actions.pack(fill="x")
        self.blocked_button = PillButton(
            blocked_actions, theme, "Обновить список", self._update_blocked,
            kind="secondary", height=CONTROL_HEIGHT)
        self.blocked_button.pack(side="left")
        self.blocked_progress = tk.Label(
            blocked_actions, text="", bg=theme.card, fg=theme.text_faint,
            font=theme.caption(), anchor="w", justify="left")
        self.blocked_progress.pack(side="left", padx=(SPACE["sm"], 0),
                                   fill="x", expand=True)
        self._label(blocked_section,
                    "Свои правила выше списка. YouTube и Discord остаются "
                    "под zapret.", caption=True)

        # --- маршруты сайтов ---
        site_routes = self._section(pad, "Маршруты сайтов")
        self._label(
            site_routes,
            "Куда вести конкретные сайты. Своё правило важнее автоподбора. "
            "Изменения применятся после «Сохранить» — прокси перезапустится.",
            caption=True)

        add_row = tk.Frame(site_routes, bg=theme.card)
        add_row.pack(fill="x", pady=(SPACE["xs"], 0))
        self.route_var = tk.StringVar()
        self.route_entry = TextField(
            add_row, theme, self.route_var, placeholder="например: x.com",
            bg=theme.card)
        self.route_entry.pack(side="left", fill="x", expand=True)
        self.route_entry.on_enter(self._add_rule)
        # Тесты и клавиатура: Enter обрабатывается и на самой плашке поля.
        self.route_entry.bind("<Return>", lambda _event: self._add_rule())
        self.route_segment = Segmented(
            add_row, theme, [("direct", "Напрямую"), ("proxy", "Через VPN")],
            "direct", bg=theme.card, height=CONTROL_HEIGHT)
        self.route_segment.pack(side="left", padx=(SPACE["sm"], 0))
        PillButton(add_row, theme, "Добавить", self._add_rule,
                   kind="secondary", height=CONTROL_HEIGHT).pack(
            side="left", padx=(SPACE["sm"], 0))
        self._label(site_routes, "например: x.com или https://x.com/…",
                    caption=True)
        self.route_status = tk.Label(
            site_routes, text="", bg=theme.card, fg=theme.text_faint,
            font=theme.caption(), anchor="w", justify="left",
            wraplength=self.WIDTH - 160)
        self.route_status.pack(fill="x", pady=(0, SPACE["xs"]))
        self.routes_counter = tk.Label(
            site_routes, text="", bg=theme.card, fg=theme.text_faint,
            font=theme.caption("bold"), anchor="w")
        self.routes_counter.pack(fill="x", pady=(0, SPACE["xs"]))
        self.rules_holder = tk.Frame(site_routes, bg=theme.card)
        self.rules_holder.pack(fill="x")
        self.rules_scroll = self._scroll_area(self.rules_holder, theme.card, 48)
        # Признак «есть правила» проверяется по менеджеру упаковки: пустой
        # список не должен оставлять рамку прокрутки.
        self.rules_list = self.rules_scroll.inner
        self.routes_empty = tk.Label(
            site_routes,
            text="пока нет своих правил — все сайты идут по общим правилам",
            bg=theme.card, fg=theme.text_faint, font=theme.body(),
            anchor="w", justify="left", wraplength=self.WIDTH - 160)

        # --- компоненты ---
        comp = self._section(pad, "Компоненты обхода")
        self.status_label = tk.Label(
            comp, text="", bg=theme.card, fg=theme.text_dim,
            font=theme.caption(), anchor="w", justify="left",
            wraplength=self.WIDTH - 160)
        self.status_label.pack(fill="x")
        component_row = tk.Frame(comp, bg=theme.card)
        component_row.pack(fill="x", pady=(SPACE["sm"], 0))
        self.install_button = PillButton(
            component_row, theme, "Установить обходы", self._install_components,
            kind="primary", height=CONTROL_HEIGHT)
        self.install_button.pack(side="left")
        self.progress_label = tk.Label(
            component_row, text="", bg=theme.card, fg=theme.text_faint,
            font=theme.caption(), anchor="w", justify="left")
        self.progress_label.pack(side="left", padx=(SPACE["sm"], 0),
                                 fill="x", expand=True)

        # --- пути ---
        paths = self._section(pad, "Пути")
        self._label(paths, "Заполняется автоматически: папка zapret "
                           "(с general*.bat и bin\\winws.exe).", caption=True)
        self._entry(paths, "zapret_dir", placeholder="C:\\Program Files\\Zapret")
        self._label(paths, "Путь к TgWsProxy_windows.exe", caption=True)
        self._entry(paths, "tgws_exe",
                    placeholder="C:\\Tools\\TgWsProxy_windows.exe")

        self.body.bind_tree()
        self.reload()

    # ---- данные -----------------------------------------------------------
    def _routes_dirty(self) -> bool:
        """Отличаются ли правила на экране от тех, что лежат в конфиге."""
        if self._saved_rules is None:
            return bool(self.rules) or bool(self.rule_servers)
        return (self.rules != self._saved_rules
                or self.rule_servers != (self._saved_rule_servers or {}))

    def _persist_routes(self) -> bool:
        """Тихо записать маршруты из формы, не трогая остальные поля.

        Нужно при закрытии: брошенное правило не должно пропадать. Прокси
        отсюда не перезапускаем — применение остаётся за «Сохранить».
        """
        cfg = dict(self.controller.cfg or {})
        cfg["xray_routes"] = routes.routes_payload(self.rules,
                                                   self.rule_servers)
        if not config.save_config(cfg):
            return False
        self.controller.cfg = cfg
        self._saved_rules = dict(self.rules)
        self._saved_rule_servers = dict(self.rule_servers)
        return True

    def revert(self) -> bool:
        """Кнопка «Закрыть»: сохранить маршруты и вернуть панель на главную.

        Правки маршрутов раньше терялись молча: добавил правило, закрыл
        настройки — правила нет. Теперь при закрытии изменённые правила
        записываются в config.json.
        """
        if self._routes_dirty() and not self._persist_routes():
            activity.log("не сохранил маршруты: config.json недоступен",
                         kind="warn")
        self._busy = False
        self._blocked_busy = False
        self._blocked_result = None
        self._blocked_run += 1
        if callable(self._on_close):
            self._on_close()
        return True

    _close = revert

    def reload(self) -> None:
        """Перечитывает конфиг, не пересоздавая страницу.

        Несохранённые правки маршрутов не затираются: иначе добавленное
        правило исчезало при повторном открытии настроек.
        """
        cfg = dict(self.controller.cfg or {})
        if "xray_subscription" in self.vars:
            self.vars["xray_subscription"].set(_text_value(cfg, "xray_subscription"))
            self.vars["xray_port"].set(_port_text(cfg))
            self.vars["xray_system_proxy"].set(bool(cfg.get("xray_system_proxy", True)))
            self.vars["xray_stream_fallback"].set(
                bool(cfg.get("xray_stream_fallback", True)))
            segment = getattr(self, "routing_segment", None)
            if segment is not None:
                segment.set(config.normalise_routing_mode(
                    cfg.get("routing_mode")))
            self.vars["zapret_dir"].set(_text_value(cfg, "zapret_dir"))
            self.vars["tgws_exe"].set(_text_value(cfg, "tgws_exe"))
            self._set_links(_links_text(cfg))
            self._mark_entry_error("xray_port", False)
        values = {
            "autostart": bool(cfg.get("autoconnect")),
            "quiet": bool(cfg.get("quiet_icon", True)),
            "guard": bool(cfg.get("guard_enabled", True)),
            "youtube": bool(cfg.get("youtube_via_vpn", False)),
        }
        for key, switch in self.toggles.items():
            switch.set(values.get(key, False))
        # Тумблеры VPN привязаны к BooleanVar: после перечитывания конфига
        # их надо догнать, иначе на экране останется старое положение.
        for key, switch in self.switches.items():
            switch.set(bool(self.vars[key].get()))
        self.blocked_toggle.set(bool(cfg.get("blocked_list_enabled", True)))
        mapping = cfg.get("xray_routes")
        if not isinstance(mapping, dict):
            mapping = {}
        # Читаем обе схемы: строку и объект с сервером. Раньше здесь был
        # фильтр по ``str(route) in ("direct", "proxy")``, и правило v2
        # отбрасывалось — открытие настроек молча стирало сервер.
        loaded_map = routes.normalise_routes(mapping)
        loaded = {name: info["route"] for name, info in loaded_map.items()}
        loaded_servers = {name: info["server"] for name, info in loaded_map.items()}
        if self._saved_rules is None or not self._routes_dirty():
            self.rules = loaded
            self._saved_rules = dict(loaded)
            self.rule_servers = dict(loaded_servers)
            self._saved_rule_servers = dict(loaded_servers)
        # иначе на экране есть несохранённые правки — их не выкидываем
        self._blocked_busy = False
        self._blocked_result = None
        self._blocked_text = ""
        self._blocked_run += 1
        self.blocked_progress.configure(text="")
        self._render_rules()
        self._refresh_blocked_status()
        self._refresh_status()
        self._refresh_preset_chips()

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

    def _set_youtube(self, _value: bool) -> None:
        """Тумблер YouTube — часть сценария: подсветка чипа догоняет форму."""
        self._refresh_preset_chips()

    # ---- сценарии ----------------------------------------------------------
    def _preset_values(self) -> dict:
        """Значения формы, из которых складывается сценарий."""
        return {
            "routing_mode": config.normalise_routing_mode(
                self.routing_segment.get()),
            "youtube_via_vpn": bool(self.toggles["youtube"].get()),
            "blocked_list_enabled": bool(self.blocked_toggle.get()),
        }

    def _refresh_preset_chips(self) -> None:
        """Подсветить чип сценария, который сейчас собран в форме."""
        buttons = getattr(self, "preset_buttons", None)
        if not buttons:
            return
        current = presets.active(self._preset_values())
        for preset_id, button in buttons.items():
            button.set_kind("primary" if preset_id == current else "secondary")
        status = getattr(self, "preset_status", None)
        if status is not None and current is None:
            # Набор на экране больше не совпадает ни с одним сценарием —
            # оставленный текст «нажмите Сохранить» был бы враньём.
            status.configure(text="", fg=self.theme.text_faint)

    def _apply_preset(self, preset_id) -> None:
        """Подставить сценарий в форму, не записывая config.json.

        Маршруты (``self.rules``) и выбранные серверы (``self.rule_servers``)
        не трогаются: сценарий меняет только режим и два тумблера.
        """
        current = self._preset_values()
        merged = presets.apply(current, preset_id)
        if merged is current or merged == current:
            # Тот же набор уже в форме — писать в журнал нечего.
            self._refresh_preset_chips()
            return
        self.routing_segment.set(str(merged["routing_mode"]))
        self.toggles["youtube"].set(bool(merged["youtube_via_vpn"]))
        self.blocked_toggle.set(bool(merged["blocked_list_enabled"]))
        name = presets.title(preset_id)
        self.preset_status.configure(
            text=f"«{name}»: нажмите «Сохранить», чтобы применить",
            fg=self.theme.text_dim)
        activity.log(f"сценарий: {name}", kind="info")
        self._refresh_preset_chips()

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
        if callable(restart) and self._proxy_running():
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
        # Новое правило — всегда «Текущий»: сервер выбирают отдельно.
        self.rule_servers.pop(domain, None)
        self._route_feedback("Правило обновлено" if current else "Добавлено",
                             self.theme.ok)
        self.route_var.set("")
        self._render_rules()

    def _set_rule(self, domain: str, route: str) -> None:
        """Новый маршрут домена; одинаковое значение ничего не меняет."""
        if route not in ("direct", "proxy") or self.rules.get(domain) == route:
            return
        self.rules[domain] = route
        if route == "direct":
            # Напрямую сервер не нужен: правило остаётся без него и в файле.
            self.rule_servers.pop(domain, None)
        self._route_feedback("Изменения применятся после «Сохранить»",
                             self.theme.text_faint)
        self._render_rules()

    def _toggle_rule(self, domain: str) -> None:
        current = self.rules.get(domain)
        if current is None:
            return
        self._set_rule(domain, "proxy" if current == "direct" else "direct")

    def _server_options(self, current: str | None = None) -> list[tuple[str, str]]:
        """Варианты выбора сервера: «Текущий» и сохранённый список.

        Источник тот же, что у панели (``xray.cached_servers``): без сети и
        без повторного опроса подписки. Пустой список — только «Текущий».
        `current` добавляется, если его нет в кеше: сервер мог уйти из
        подписки, но правило помнит его имя — показывать «Текущий» было бы
        враньём.
        """
        names: list[str] = []
        for row in xray.cached_servers() or []:
            name = str((row or {}).get("name") or "").strip()
            if name and name not in names:
                names.append(name)
        wanted = str(current or "").strip()
        if wanted and wanted not in names:
            names.append(wanted)
        return [("", "Текущий")] + [(name, name) for name in names]

    def _set_rule_server(self, domain: str, value: str) -> None:
        """Сервер proxy-правила; пустое значение — «Текущий».

        Список не перерисовываем: плашка уже показывает выбор, а ``_render_rules``
        уничтожил бы контрол прямо внутри его же обработчика.
        """
        server = value.strip() or None
        if self.rule_servers.get(domain) == server:
            return
        self.rule_servers[domain] = server
        self._route_feedback("Изменения применятся после «Сохранить»",
                             self.theme.text_faint)

    def _delete_rule(self, domain: str) -> None:
        self.rule_servers.pop(domain, None)
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
                self.routes_empty.pack(fill="x", pady=(SPACE["xs"], 0))
            return
        self.routes_empty.pack_forget()
        if not self.rules_scroll.winfo_manager():
            self.rules_scroll.pack(fill="x")
        for domain in domains:
            route = self.rules[domain]
            # Клик по домену переключает маршрут — как прежняя плашка, но
            # строка стала обычной hover-row с сегментами и ghost-кнопкой.
            row = HoverRow(self.rules_list, theme,
                           command=lambda d=domain: self._toggle_rule(d))
            row.pack(fill="x")
            display = domain if len(domain) <= 46 else domain[:45].rstrip() + "…"
            label = tk.Label(row, text=display, bg=theme.card, fg=theme.text,
                             font=theme.body(), anchor="w")
            label.pack(side="left", fill="x", expand=True, pady=SPACE["sm"])
            if display != domain:
                Tooltip(label, domain)
            row.track(label)
            segment = Segmented(
                row, theme, [("direct", "Напрямую"), ("proxy", "Через VPN")],
                route, command=lambda value, d=domain: self._set_rule(d, value),
                bg=theme.card, height=ROW_CONTROL_HEIGHT, width=176)
            segment.pack(side="left", padx=(SPACE["sm"], SPACE["sm"]))
            # Сервер — только для proxy: напрямую он не нужен. Место под
            # контрол занимаем всегда, иначе строки прыгают по ширине.
            if route == "proxy":
                picker = Dropdown(
                    row, theme,
                    self._server_options(self.rule_servers.get(domain)),
                    self.rule_servers.get(domain),
                    command=lambda value, d=domain: self._set_rule_server(d, value),
                    bg=theme.card, height=ROW_CONTROL_HEIGHT, width=132)
                picker.pack(side="left", padx=(0, SPACE["sm"]))
                Tooltip(picker, "Сервер для этого сайта")
            else:
                spacer = tk.Frame(row, bg=theme.card, width=132,
                                  height=ROW_CONTROL_HEIGHT)
                spacer.pack(side="left", padx=(0, SPACE["sm"]))
                spacer.pack_propagate(False)
            remove = PillButton(row, theme, "Удалить",
                                lambda d=domain: self._delete_rule(d),
                                kind="ghost", height=ROW_CONTROL_HEIGHT,
                                pad_x=SPACE["sm"])
            remove.pack(side="right", pady=SPACE["xs"])
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
        # Версии — в state сразу: иначе следующая проверка снова сочтёт
        # компоненты «неизвестными» и «Обновить всё» их не увидит.
        components.remember_install(result)
        # Пути не ждут «Сохранить»: иначе установленный компонент до
        # перезапуска считался отсутствующим.
        save_error = ""
        cfg = dict(getattr(self.controller, "cfg", None) or {})
        changed = False
        for item, key, field in ((zapret, "zapret_dir", "dir"),
                                 (tgws, "tgws_exe", "exe")):
            if not item.get("ok"):
                continue
            value = self.vars[key].get().strip() or str(item.get(field) or "")
            if value and value != str(cfg.get(key) or ""):
                cfg[key] = value
                self.vars[key].set(value)
                changed = True
        if changed:
            save = getattr(self.controller, "save_config", None)
            written = save(cfg) if callable(save) else config.save_config(cfg)
            if written is not False:
                self.controller.cfg = cfg
            else:
                save_error = "пути не сохранены: config.json недоступен"
        # Статус пересобираем сразу: строка службы и «Запустить» должны
        # стать активными без перезапуска виджета.
        refresh = getattr(self.controller, "refresh", None)
        if callable(refresh):
            try:
                refresh()
            except Exception:  # noqa: BLE001
                pass
        if result.get("ok"):
            text = "Готово: оба компонента установлены"
            color = self.theme.ok
            if save_error:
                text += " · " + save_error
                color = self.theme.warn
        else:
            errors = [c.get("error", "") for c in components_state.values()
                      if not c.get("ok")]
            text = "Не всё удалось: " + "; ".join(e for e in errors if e)[:120]
            if save_error:
                text += " · " + save_error
            color = self.theme.bad
        self.progress_label.configure(text=text, fg=color)
        self._refresh_status()

    # ---- сохранение --------------------------------------------------------
    def _proxy_running(self) -> bool:
        """Работает ли прокси (``controller.proxy`` может быть ``None``)."""
        proxy = getattr(self.controller, "proxy", None)
        return bool(isinstance(proxy, dict) and proxy.get("running"))

    def _save(self) -> bool:
        cfg = dict(self.controller.cfg or {})
        previous_mode = config.normalise_routing_mode(cfg.get("routing_mode"))
        preset_before = presets.active(cfg)
        port, port_error = _parse_port(self.vars["xray_port"].get())
        if port is None:
            self._mark_entry_error("xray_port", True)
            self.progress_label.configure(text=port_error, fg=self.theme.bad)
            return False
        self._mark_entry_error("xray_port", False)
        links_raw = self._links_value()
        links = xray.links_from_text(links_raw)
        if links_raw.strip() and not links:
            # Непустое поле без единой ссылки — почти всегда мусор или
            # недокопированный текст: сохранять его нельзя, иначе при
            # следующем запуске «конфигов нет» без объяснения.
            self._update_links_status()
            self.progress_label.configure(
                text="Свои конфиги: не найдено ни одной ссылки "
                     "vless/vmess/trojan/ss", fg=self.theme.bad)
            return False
        cfg["xray_subscription"] = self.vars["xray_subscription"].get().strip()
        cfg["xray_links"] = links
        cfg["xray_port"] = port
        cfg["xray_system_proxy"] = bool(self.vars["xray_system_proxy"].get())
        cfg["xray_stream_fallback"] = bool(self.vars["xray_stream_fallback"].get())
        cfg["routing_mode"] = config.normalise_routing_mode(
            self.routing_segment.get())
        cfg["blocked_list_enabled"] = bool(self.blocked_toggle.get())
        cfg["youtube_via_vpn"] = bool(self.toggles["youtube"].get())
        cfg["xray_routes"] = routes.routes_payload(self.rules,
                                                   self.rule_servers)
        # Старые списки не обнуляем в никуда: их значения становятся правилами.
        _merge_legacy_routes(cfg)
        cfg["zapret_dir"] = self.vars["zapret_dir"].get().strip()
        cfg["tgws_exe"] = self.vars["tgws_exe"].get().strip()
        if not config.save_config(cfg):
            self.progress_label.configure(
                text="Не сохранено: config.json недоступен", fg=self.theme.warn)
            return False
        self.controller.cfg = cfg
        self._saved_rules = dict(self.rules)
        self._saved_rule_servers = dict(self.rule_servers)
        preset_after = presets.active(cfg)
        if preset_after is not None and preset_after != preset_before:
            activity.log(
                f"сценарий: {presets.title(preset_after)} — сохранён",
                kind="info")
        # Перечитываем тумблеры/поля из сохранённого cfg: старые галочки
        # не должны оставаться после смены настроек.
        self.reload()
        self.progress_label.configure(text="Сохранено", fg=self.theme.ok)
        restart = getattr(self.controller, "restart_proxy", None)
        if callable(restart) and self._proxy_running():
            if cfg["routing_mode"] != previous_mode:
                # Применяется только перезапуском ядра: записываем в журнал,
                # чтобы смена режима не осталась невидимой.
                activity.log(
                    "режим маршрутизации: "
                    + ROUTING_MODE_NAMES.get(cfg["routing_mode"],
                                             cfg["routing_mode"]),
                    kind="info")
            if restart() is False:
                self.progress_label.configure(
                    text="Сохранено, но прокси занят — перезапустите вручную",
                    fg=self.theme.warn)
        return True


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
        page = self.page
        if page is not None:
            # page.revert сохранит несохранённые маршруты и вызовет _destroy.
            page._blocked_busy = False
            page._blocked_result = None
            page._blocked_run += 1
            page.revert()
        self._destroy()
        # page оставляем: после закрытия тесты читают page.rules, а Toplevel уже нет.

